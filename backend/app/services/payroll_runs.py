"""Payroll runs and payslips (plan 12 s.4.2 - 4.6, contract 13.5 / 13.6).

Locking order (always): run row -> payslip rows. Run level actions take `SELECT ... FOR UPDATE` on the run; payslip level
actions take it on the run first and then on the payslip. Money is only ever computed by `payroll_calc`; this module pulls
the inputs (structures, details, days), stores the result encrypted and drives the lifecycle
draft -> processed -> published (+ revisions that supersede the previous published copy in the SAME transaction).
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import EmployeePayrollDetails, PayrollPublishRequest, PayrollRun, Payslip, PayslipDelivery, SalaryStructure, User
from ..schemas.payroll import PayslipUpdate, RunCreate
from . import admin_security, notifications
from . import attendance as att
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_days as pdays
from . import payroll_settings as pset
from . import payroll_tds as ptds
from .company import _logo_url
from .license import get_company

log = logging.getLogger("app.payroll")

TENTH = Decimal("0.1")
MIN_YEAR, MAX_YEAR = 2000, 2200
OPEN = ("draft", "processed")
MONTHS = "January February March April May June July August September October November December".split()


def month_label(period: dt.date) -> str:
    return f"{MONTHS[period.month - 1]} {period.year}"


def _d1(x: Decimal | float | int) -> Decimal:
    return Decimal(str(x)).quantize(TENTH)


def _split(warnings: list[str] | None) -> tuple[list[str], list[str]]:
    w = list(warnings or [])
    return [c for c in w if c in calc.ERROR_CODES], [c for c in w if c not in calc.ERROR_CODES]


# ============================================================================================ data loading
async def _today(session: AsyncSession) -> dt.date:
    return dt.datetime.now(att.office_tz(await att.load_settings(session))).date()


def parse_period(raw: str, today: dt.date) -> dt.date:
    from ..schemas.payroll import PERIOD_RE

    m = PERIOD_RE.match(raw or "")
    if not m:
        raise AppError("INVALID_PERIOD", "period must be YYYY-MM", details=[{"field": "period", "message": "must be YYYY-MM"}])
    y, mo = int(m.group(1)), int(m.group(2))
    if not MIN_YEAR <= y <= MAX_YEAR:
        raise AppError("INVALID_PERIOD", "period year out of range", details=[{"field": "period", "message": "year 2000-2200"}])
    p = dt.date(y, mo, 1)
    if p > today.replace(day=1):
        raise AppError("INVALID_PERIOD", "A payroll run for a future month is not allowed",
                       details=[{"field": "period", "message": "cannot be in the future"}])  # fmt: skip
    return p


async def _load_structs(
    session: AsyncSession, user_ids: list[uuid.UUID], period: dt.date, *, share_lock: bool = False
) -> dict[uuid.UUID, list[tuple[dt.date, SalaryStructure]]]:
    """`share_lock` = SELECT ... FOR SHARE: a concurrent structure update / delete (which takes FOR UPDATE and then checks
    whether a processed payslip uses it) waits until this transaction ends, so a structure cannot change between being read
    for a calculation and the payslip becoming processed."""
    out: dict[uuid.UUID, list[tuple[dt.date, SalaryStructure]]] = {}
    if not user_ids:
        return out
    q = select(SalaryStructure).where(SalaryStructure.user_id.in_(user_ids), SalaryStructure.effective_from <= period)
    if share_lock:
        q = q.order_by(SalaryStructure.id).with_for_update(read=True)
    for s in (await session.execute(q)).scalars():
        out.setdefault(s.user_id, []).append((s.effective_from, s))
    return out


async def _load_details(session: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, EmployeePayrollDetails]:
    if not user_ids:
        return {}
    q = select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id.in_(user_ids))
    return {d.user_id: d for d in (await session.execute(q)).scalars()}


def _months_before(period: dt.date, n: int) -> list[dt.date]:
    out = []
    y, m = period.year, period.month
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(dt.date(y, m, 1))
    return sorted(out)


async def _prior_gross(session: AsyncSession, user_ids: list[uuid.UUID], period: dt.date) -> dict[uuid.UUID, list[Decimal]]:
    """Gross of the up to 5 months before `period` (live copy: published, else the open one) per user, oldest first.
    Used for the half-yearly professional tax slab."""
    if not user_ids:
        return {}
    periods = _months_before(period, 5)
    q = (
        select(Payslip, PayrollRun.period)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(
            Payslip.user_id.in_(user_ids), PayrollRun.period.in_(periods), Payslip.status.in_(("published", "processed", "draft"))
        )
    )
    best: dict[tuple[uuid.UUID, dt.date], Payslip] = {}
    for ps, per in (await session.execute(q)).all():
        if ps.result is None:
            continue
        cur = best.get((ps.user_id, per))
        if cur is None or (cur.status != "published" and (ps.status == "published" or ps.revision > cur.revision)):
            best[(ps.user_id, per)] = ps
    out: dict[uuid.UUID, list[Decimal]] = {}
    for (uid, _per), ps in sorted(best.items(), key=lambda kv: kv[0][1]):
        out.setdefault(uid, []).append(Decimal(ps.result["gross"]))
    return out


# ============================================================================================ payslip fill / recompute
def _identity(det: EmployeePayrollDetails | None) -> dict | None:
    if det is None:
        return None
    return {
        "pan": det.pan, "uan": det.uan, "esi_ip_number": det.esi_ip_number,
        "bank_name": det.bank_name, "bank_account": det.bank_account, "bank_ifsc": det.bank_ifsc,
    }  # fmt: skip


def _snapshot(user: User, det: EmployeePayrollDetails | None) -> dict:
    return {
        "employee_id": user.employee_id,
        "full_name": user.full_name,
        "department": user.department.name if user.department else None,
        "designation": det.designation if det else None,
        "join_date": user.join_date.isoformat() if user.join_date else None,
        "payment_mode": det.payment_mode if det else "bank_transfer",
    }


def _blank_inputs() -> dict:
    return {"tds": None, "pt_override": None, "adjustments": [], "components": [], "note": None}


def _pay_inputs(inputs: dict) -> calc.PayInputs:
    return calc.PayInputs(
        tds=Decimal(inputs["tds"]) if inputs.get("tds") is not None else None,
        pt_override=Decimal(inputs["pt_override"]) if inputs.get("pt_override") is not None else None,
        adjustments=tuple(calc.Adjustment(a["code"], a["label"], Decimal(a["amount"])) for a in inputs.get("adjustments", [])),
        components=tuple(calc.Adjustment(c["code"], c["label"], Decimal(c["amount"])) for c in inputs.get("components", [])),
    )


def pull_identity(ps: Payslip, user: User, det: EmployeePayrollDetails | None) -> None:
    ps.employee_snapshot = _snapshot(user, det)
    ps.identity = _identity(det)


def pull_days(
    ps: Payslip, user: User, ctx: pdays.DayContext, cutoff: dt.date, cfg: dict,
    det: EmployeePayrollDetails | None = None, *, settlement: bool = False,
) -> bool:  # fmt: skip
    """Re-pull basis / employed / LOP from attendance and leave. False = not employed in the period. `settlement` = the
    employee has an approved F&F for this month: when not employed in it, the payslip is settlement-only (employed 0)."""
    res = pdays.compute_days(user, ctx, cutoff, cfg, det.last_working_day if det is not None else None)
    only = res is None and settlement
    if only:
        res = pdays.settlement_only_days(ctx, cfg)
    inputs = copy.deepcopy(ps.inputs or _blank_inputs())
    if only:
        inputs["settlement_only"] = True
    else:
        inputs.pop("settlement_only", None)
    ps.inputs = inputs
    if res is None:
        return False
    ps.basis_days = _d1(res.basis_days)
    ps.employed_days = _d1(res.employed_days)
    ps.lop_auto = _d1(res.lop_auto)
    ps.lop_breakdown = res.breakdown
    ps.paid_days = ps.employed_days  # fixed by recompute()
    return True


def _hours_label(minutes: int) -> str:
    return f"{(Decimal(minutes) / Decimal(60)).quantize(Decimal('0.01')):f}".rstrip("0").rstrip(".")


def _finalize_overtime(ps: Payslip, user: User, basic: Decimal, cfg: dict, warns: list[str]) -> None:
    """The OVERTIME line is pulled with the approved MINUTES only; its amount is computed here from the structure the payslip
    adopts, the employee's shift and the run settings (plan 15 P10), so it always matches the payslip. The line is dropped
    (its sources then stay `approved`) when overtime is off, the employee has no shift or the amount is zero."""
    lines = (ps.inputs or {}).get("components", [])
    if not any(c["code"] == "OVERTIME" for c in lines):
        return
    shift_min = calc.shift_length_minutes(user.shift.start_time, user.shift.end_time) if user.shift is not None else 0
    out: list[dict] = []
    mult, mdays = Decimal(str(cfg.get("overtime_multiplier", 2))), int(cfg.get("overtime_month_days", 26))
    max_hours, mode = int(cfg.get("overtime_max_hours_month", 50)), cfg.get("rounding", "rupee")
    for c in lines:
        if c["code"] != "OVERTIME":
            out.append(c)
            continue
        total = int(c.get("minutes_total", c.get("minutes", 0)))
        carried = c.get("carried")
        if carried is None:  # nothing paid yet: the whole line is priced from the adopted structure
            if not cfg.get("overtime_enabled", False):
                continue
            amount, paid, capped = calc.overtime_amount(
                basic, shift_min, total, month_days=mdays, multiplier=mult, max_hours=max_hours, mode=mode
            )  # fmt: skip
            if amount <= 0:
                continue
            if capped:
                warns.append("OVERTIME_CAPPED")
            out.append({**c, "label": f"Overtime ({_hours_label(paid)} h)", "amount": calc.money_str(amount),
                        "minutes": paid, "minutes_total": total})  # fmt: skip
            continue
        # M1: the paid part keeps its published amount; only the newly approved minutes are priced (and capped on what is left)
        paid_total = int(c.get("paid_minutes", 0))
        old_amount, old_min = Decimal(carried["amount"]), int(carried["minutes"])
        new_min = max(0, total - paid_total) if cfg.get("overtime_enabled", False) else 0
        if max_hours > 0 and new_min > max(0, max_hours * 60 - old_min):
            new_min = max(0, max_hours * 60 - old_min)
            warns.append("OVERTIME_CAPPED")
        extra = (
            calc.overtime_amount(basic, shift_min, new_min, month_days=mdays, multiplier=mult, max_hours=0, mode=mode)[0]
            if new_min > 0
            else Decimal("0.00")
        )
        line = {**c, "amount": calc.money_str(old_amount + extra)}
        if extra > 0:
            line.update(minutes=old_min + new_min, minutes_total=total)
        else:  # nothing new is payable (overtime off, no shift): the new rows stay `approved`, only the paid rows are kept
            keep = set(c.get("paid_ids", []))
            line.update(minutes=old_min, minutes_total=paid_total, sources=[x for x in c["sources"] if x["id"] in keep])
        line["label"] = f"Overtime ({_hours_label(line['minutes'])} h)"
        out.append(line)
    inputs = copy.deepcopy(ps.inputs)
    inputs["components"] = out
    ps.inputs = inputs


def recompute(
    ps: Payslip,
    *,
    user: User,
    period: dt.date,
    structs: list[tuple[dt.date, SalaryStructure]],
    adopt_latest: bool,
    cfg: dict,
    prior: list[Decimal],
    keep: tuple[str, ...] = ("NOT_ELIGIBLE",),
    lwd: dt.date | None = None,
    tds: ptds.Ctx | None = None,
) -> None:
    """Sets paid_days, structure_id, result and warnings from the stored days / inputs. Pure apart from mutating `ps`.
    `tds` = the TDS engine context of the run (plan 17 5c); None = engine off for this run (manual TDS, as before)."""
    from .payroll_structures import data_of

    ps._tds = False  # type: ignore[attr-defined] - no computation row unless the engine produces one below
    ptds.prepare(ps, tds)
    warns: list[str] = [w for w in (ps.warnings or []) if w in keep]
    lop = ps.lop_override if ps.lop_override is not None else ps.lop_auto
    ps.paid_days = max(Decimal("0.0"), _d1(ps.employed_days) - _d1(lop))
    if ps.lop_override is not None:
        warns.append("LOP_OVERRIDDEN")
    if user.shift is None:
        warns.append("NO_SHIFT")
    if float((ps.lop_breakdown or {}).get("pending_leave_days", 0)) > 0:
        warns.append("PENDING_LEAVE")
    if (ps.inputs or {}).get("settlement_only"):
        warns.append("FNF_SETTLEMENT_ONLY")
    elif ps.paid_days == 0:
        warns.append("ZERO_PAID_DAYS")
    if lwd is not None and lwd.replace(day=1) == period:
        warns.append("EXIT_IN_MONTH")
    system_codes = {c["code"] for c in (ps.inputs or {}).get("components", [])}
    if system_codes & {a["code"] for a in (ps.inputs or {}).get("adjustments", [])}:
        warns.append("MANUAL_COMPONENT_OVERLAP")  # likely double counting: a manual line and a system line share a code
    warns.extend((ps.inputs or {}).get("component_warnings", []))  # e.g. ARREARS_PROPOSED, set by the component providers
    snap, ident = ps.employee_snapshot or {}, ps.identity or {}
    if snap.get("payment_mode", "bank_transfer") == "bank_transfer" and not (
        ident.get("bank_account") and ident.get("bank_ifsc")
    ):
        warns.append("NO_BANK_DETAILS")
    if not ident.get("pan"):
        warns.append("NO_PAN")

    latest = calc.structure_for(structs, period)
    by_id = {s.id: s for _, s in structs}
    chosen = latest if adopt_latest else (by_id.get(ps.structure_id) or latest)
    if chosen is not None and chosen is not latest:
        warns.append("STRUCTURE_CHANGED")
    ps.structure_id = chosen.id if chosen else None
    if chosen is None:
        warns.append("STRUCTURE_MISSING")
        ps.result = None
    else:
        rules = calc.Rules.from_config(cfg)
        data = data_of(chosen)
        _finalize_overtime(ps, user, data.basic, cfg, warns)
        eligible = calc.esi_eligible([(e, data_of(s)) for e, s in structs], period, user.join_date, rules.esi_wage_threshold)

        def _calc() -> calc.CalcResult:
            return calc.calculate(
                data, rules, period, Decimal(ps.basis_days), Decimal(ps.paid_days), _pay_inputs(ps.inputs),
                esi_is_eligible=eligible, prior_gross=prior,
            )  # fmt: skip

        res = _calc()
        if tds is not None and ptds.evaluate(ps, res, tds, structs=structs, period=period, lwd=lwd, warns=warns):
            res = _calc()  # mode apply: the engine set inputs.tds
        if res.error:
            warns.append(res.error)
            ps.result = None
        else:
            ps.result = res.to_json()
    ps.warnings = list(dict.fromkeys(warns))
    ps.version = (ps.version or 1) + 1


def _new_payslip(run: PayrollRun, user: User, actor: Principal, revision: int = 1) -> Payslip:
    return Payslip(
        run_id=run.id, user_id=user.id, revision=revision, status="draft", hold=False,
        employee_snapshot={}, basis_days=Decimal("0.0"), employed_days=Decimal("0.0"), lop_auto=Decimal("0.0"),
        paid_days=Decimal("0.0"), lop_breakdown={}, inputs=_blank_inputs(), warnings=[], version=1,
        created_by=actor.id, updated_by=actor.id, contributors=[str(actor.id)],
    )  # fmt: skip


def _lwd(det: EmployeePayrollDetails | None) -> dt.date | None:
    return det.last_working_day if det is not None else None


def _eligible(user: User, det: EmployeePayrollDetails | None, period: dt.date, tz, settlement: bool = False) -> bool:
    """Employed in the month (the window ends at the last working day when one is set) or, with `settlement`, the employee has
    an approved F&F that pays in this month (settlement-only payslip, plan 16 F13)."""
    if det is not None and not det.include_in_payroll:
        return False
    start, end = pdays.month_bounds(period)
    if pdays.employment_window(user, start, end, tz, _lwd(det)) is not None:
        return True
    return settlement


@dataclass
class _Bulk:
    users: list[User]
    details: dict
    structs: dict
    ctx: pdays.DayContext
    prior: dict
    fnf: set  # users with an approved / settled F&F that pays in the month


async def fnf_users(session: AsyncSession, period: dt.date, user_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Employees whose approved (or settled) F&F has `period` as its settlement month. Plain read, takes no lock."""
    if not user_ids:
        return set()
    from ..models import FnfSettlement

    q = select(FnfSettlement.user_id).where(
        FnfSettlement.user_id.in_(user_ids), FnfSettlement.settlement_period == period,
        FnfSettlement.status.in_(("approved", "settled")),
    )  # fmt: skip
    return set((await session.execute(q)).scalars())


async def _bulk(
    session: AsyncSession, period: dt.date, user_ids: list[uuid.UUID] | None = None, *, share_lock_structs: bool = False
) -> _Bulk:
    q = select(User)
    if user_ids is not None:
        q = q.where(User.id.in_(user_ids))
    users = list((await session.execute(q.order_by(User.employee_id))).unique().scalars())
    ids = [u.id for u in users]
    return _Bulk(
        users, await _load_details(session, ids), await _load_structs(session, ids, period, share_lock=share_lock_structs),
        await pdays.load_context(session, period, ids), await _prior_gross(session, ids, period),
        await fnf_users(session, period, ids),
    )  # fmt: skip


# ============================================================================================ run access
async def _run_or_404(session: AsyncSession, run_id: uuid.UUID, *, lock: bool = False) -> PayrollRun:
    q = select(PayrollRun).where(PayrollRun.id == run_id)
    if lock:
        q = q.with_for_update()
    run = (await session.execute(q)).scalar_one_or_none()
    if run is None:
        raise AppError("NOT_FOUND", "Payroll run not found")
    return run


async def _run_payslips(session: AsyncSession, run_id: uuid.UUID, *, lock: bool = False) -> list[Payslip]:
    q = select(Payslip).where(Payslip.run_id == run_id).order_by(Payslip.user_id, Payslip.revision)
    if lock:
        q = q.with_for_update()
    return list((await session.execute(q)).scalars())


def _live(rows: list[Payslip]) -> list[Payslip]:
    """One copy per employee: the published one, else the open one."""
    by_user: dict[uuid.UUID, Payslip] = {}
    for p in rows:
        if p.status == "superseded":
            continue
        cur = by_user.get(p.user_id)
        if cur is None or (cur.status != "published" and p.status == "published"):
            by_user[p.user_id] = p
    return list(by_user.values())


# ============================================================================================ output
async def run_out(
    session: AsyncSession, run: PayrollRun, rows: list[Payslip] | None = None, *, with_request: bool = True
) -> dict[str, Any]:
    rows = rows if rows is not None else await _run_payslips(session, run.id)
    present = [p for p in rows if p.status != "superseded"]
    counts = {
        "employees": len({p.user_id for p in present}),
        "draft": sum(p.status == "draft" for p in present),
        "processed": sum(p.status == "processed" for p in present),
        "published": sum(p.status == "published" for p in present),
        "held": sum(p.hold and p.status in OPEN for p in present),
        "errors": sum(bool(_split(p.warnings)[0]) for p in present if p.status in OPEN),
        "warnings": sum(bool(_split(p.warnings)[1]) for p in present if p.status in OPEN),
        "open_revisions": sum(p.status in OPEN and p.revision > 1 for p in present),
    }
    gross = ded = net = pf = esi = recovered = Decimal("0.00")
    for p in _live(rows):
        if p.hold and p.status in OPEN or p.result is None:
            continue
        gross += Decimal(p.result["gross"])
        ded += Decimal(p.result["total_deductions"])
        net += Decimal(p.result["net"])
        pf += calc.employer_amount(p.result, "PF_ER")
        esi += calc.employer_amount(p.result, "ESI_ER")
        for d in p.result["deductions"]:
            if d["code"] in calc.RECOVERED_CODES:
                recovered += Decimal(d["amount"])
    users = {}
    for uid in {run.created_by, run.processed_by, run.published_by} - {None}:
        users[uid] = await session.get(User, uid)
    return {
        "id": str(run.id),
        "period": pc.period_str(run.period),
        "status": run.status,
        "pay_date": run.pay_date.isoformat() if run.pay_date else None,
        "attendance_cutoff": run.attendance_cutoff.isoformat(),
        "counts": counts,
        "totals": {
            "gross_earnings": calc.money_str(gross),
            "total_deductions": calc.money_str(ded),
            "net_pay": calc.money_str(net),
            "employer_pf": calc.money_str(pf),
            "employer_esi": calc.money_str(esi),
            "employer_recovered": calc.money_str(recovered),
            "employer_cost": calc.money_str(pf + esi - recovered),
        },  # fmt: skip
        "config": run.config,
        "company_name": run.company_name,
        "created_at": pc.iso(run.created_at),
        "created_by": pc.user_out(users.get(run.created_by)),
        "processed_at": pc.iso(run.processed_at),
        "processed_by": pc.user_out(users.get(run.processed_by)),
        "published_at": pc.iso(run.published_at),
        "published_by": pc.user_out(users.get(run.published_by)),
        "publish_request": await _latest_request(session, run_id=run.id) if with_request else None,
    }


async def _latest_request(
    session: AsyncSession, *, run_id: uuid.UUID | None = None, payslip_id: uuid.UUID | None = None
) -> dict[str, Any] | None:
    """The newest publish request of a run (scope run) or of one payslip (scope payslip), 13.5 / 13.6 `publish_request`."""
    q = select(PayrollPublishRequest).order_by(PayrollPublishRequest.requested_at.desc(), PayrollPublishRequest.id).limit(1)
    if payslip_id is not None:
        q = q.where(PayrollPublishRequest.payslip_id == payslip_id)
    else:
        q = q.where(PayrollPublishRequest.run_id == run_id, PayrollPublishRequest.scope == "run")
    r = (await session.execute(q)).scalar_one_or_none()
    if r is None:
        return None
    return {
        "id": str(r.id),
        "status": r.status,
        "requested_by": pc.user_out(await session.get(User, r.requested_by)),
        "requested_at": pc.iso(r.requested_at),
        "stale_reason": r.stale_reason,
    }


async def list_runs(session: AsyncSession, year: int | None) -> dict[str, Any]:
    year = year or (await _today(session)).year
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise AppError("VALIDATION_ERROR", "year out of range", details=[{"field": "year", "message": "2000-2200"}])
    q = (
        select(PayrollRun)
        .where(PayrollRun.period >= dt.date(year, 1, 1), PayrollRun.period <= dt.date(year, 12, 1))
        .order_by(PayrollRun.period.desc())
    )
    items = []
    for r in (await session.execute(q)).scalars():
        o = await run_out(session, r, with_request=False)
        items.append(
            {
                "id": o["id"],
                "period": o["period"],
                "status": o["status"],
                "employees": o["counts"]["employees"],
                "net_pay_total": o["totals"]["net_pay"],
                "published_at": o["published_at"],
            }  # fmt: skip
        )
    return {"year": year, "items": items}


def _payslip_money(p: Payslip) -> dict[str, str | None]:
    r = p.result
    return {
        "gross_earnings": r["gross"] if r else None,
        "total_deductions": r["total_deductions"] if r else None,
        "net_pay": r["net"] if r else None,
    }


def _lop_days(p: Payslip) -> float:
    return float(p.lop_override if p.lop_override is not None else p.lop_auto)


async def settled_fnf(session: AsyncSession, user_id: uuid.UUID, period: dt.date, *, live: bool = False):
    """The F&F that pays in `period` for the employee: settled (employee view) or approved / settled (`live`, admin view)."""
    from ..models import FnfSettlement

    st = ("approved", "settled") if live else ("settled",)
    q = select(FnfSettlement).where(
        FnfSettlement.user_id == user_id, FnfSettlement.settlement_period == period, FnfSettlement.status.in_(st)
    ).order_by(FnfSettlement.created_at.desc(), FnfSettlement.id)  # fmt: skip
    return (await session.execute(q)).scalars().first()


def employee_view(
    p: Payslip, run: PayrollRun, *, successor_id: uuid.UUID | None, company, logo_url: str | None, admin: bool = False,
    final: bool = False,
) -> dict[str, Any]:  # fmt: skip
    """The `Payslip` object of contract 13.2 (also the base of AdminPayslip). `admin=True` (admin JSON and admin PDF) ALWAYS
    masks PAN, UAN and ESI IP number: admins get unmasked values only through the audited, rate-limited `details?reveal=true`."""
    cfg = run.config
    snap, ident = p.employee_snapshot or {}, p.identity or {}
    r = p.result
    pan = ident.get("pan")
    show_employer = bool(cfg.get("show_employer_contributions", False))
    view = {
        "id": str(p.id),
        "period": pc.period_str(run.period),
        "revision": p.revision,
        "status": p.status,
        "is_final": final,
        "published_at": pc.iso(p.published_at),
        "pay_date": run.pay_date.isoformat() if run.pay_date else None,
        "company": {
            "name": run.company_name or (company.name if company else None) or "",
            "address": (cfg.get("company_address") or None),
            "logo_url": logo_url,
        },
        "employee": {
            "employee_id": snap.get("employee_id"),
            "full_name": snap.get("full_name"),
            "department": snap.get("department"),
            "designation": snap.get("designation"),
            "join_date": snap.get("join_date"),
            "pan": (pc.mask_pan(pan) if admin or cfg.get("mask_pan", True) else pan),
            "uan": pc.mask_tail(ident.get("uan")) if admin else ident.get("uan"),
            "esi_ip_number": pc.mask_tail(ident.get("esi_ip_number")) if admin else ident.get("esi_ip_number"),
            "bank_name": ident.get("bank_name"),
            "bank_account": pc.mask_tail(ident.get("bank_account")),
            "payment_mode": snap.get("payment_mode", "bank_transfer"),
        },
        "days": {
            "basis_days": float(p.basis_days),
            "employed_days": float(p.employed_days),
            "lop_days": _lop_days(p),
            "paid_days": float(p.paid_days),
        },  # fmt: skip
        "earnings": r["earnings"] if r else [],
        "deductions": r["deductions"] if r else [],
        "employer_contributions": (r["employer"] if r and show_employer else None),
        "totals": (
            {
                "gross_earnings": r["gross"],
                "total_deductions": r["total_deductions"],
                "net_pay": r["net"],
                "net_pay_words": calc.amount_in_words(Decimal(r["net"])),
            }  # fmt: skip
            if r
            else None
        ),
        "revision_info": (
            {
                "reason": p.revision_reason,
                "supersedes_id": str(p.supersedes_id) if p.supersedes_id else None,
                "superseded_by_id": str(successor_id) if successor_id else None,
            }
            if p.revision > 1 or successor_id
            else None
        ),
    }
    return view


async def _view_parts(session: AsyncSession, p: Payslip, *, admin: bool) -> tuple[PayrollRun, uuid.UUID | None, Any, str | None]:
    run = await _run_or_404(session, p.run_id)
    q = select(Payslip.id).where(Payslip.supersedes_id == p.id)
    if not admin:  # an employee must not learn that an UNPUBLISHED revision exists
        q = q.where(Payslip.status.in_(("published", "superseded")))
    succ = (await session.execute(q)).scalars().first()
    company = await get_company(session)
    return run, succ, company, _logo_url(company)


async def admin_payslip_out(session: AsyncSession, p: Payslip) -> dict[str, Any]:
    await session.refresh(p)  # server-side onupdate columns (updated_at) are expired after a write
    run, succ, company, logo = await _view_parts(session, p, admin=True)
    fnf = await settled_fnf(session, p.user_id, run.period, live=True)
    out = employee_view(p, run, successor_id=succ, company=company, logo_url=logo, admin=True, final=fnf is not None)
    out["final_settlement"] = {"fnf_id": str(fnf.id), "status": fnf.status} if fnf is not None else None
    user = await session.get(User, p.user_id)
    struct = await session.get(SalaryStructure, p.structure_id) if p.structure_id else None
    errors, warnings = _split(p.warnings)
    inputs = p.inputs or _blank_inputs()
    tds = inputs.get("tds")
    if tds is None and struct is not None:
        tds = struct.components.get("tds_monthly")
    uids = {x for x in (p.processed_by, p.published_by) if x}
    people = {u: await session.get(User, u) for u in uids}
    out.update(
        {
            "run_id": str(p.run_id),
            "user": pc.user_out(user),
            "hold": p.hold,
            "version": p.version,
            "structure_id": str(p.structure_id) if p.structure_id else None,
            "lop": {
                "auto": float(p.lop_auto),
                "override": float(p.lop_override) if p.lop_override is not None else None,
                "override_reason": p.lop_override_reason,
                "breakdown": p.lop_breakdown,
            },
            "inputs": {
                "tds": tds,
                "pt_override": inputs.get("pt_override"),
                "adjustments": inputs.get("adjustments", []),
                "components": inputs.get("components", []),
                "note": inputs.get("note"),
            },
            "errors": errors,
            "warnings": warnings,
            "processed_at": pc.iso(p.processed_at),
            "processed_by": pc.user_out(people.get(p.processed_by)),
            "published_by": pc.user_out(people.get(p.published_by)),
            "updated_at": pc.iso(p.updated_at),
            "publish_request": await _latest_request(session, payslip_id=p.id),
            "tds_computation": await ptds.payslip_computation(session, p.id),
        }
    )
    return out


# ============================================================================================ run lifecycle
async def create_run(session: AsyncSession, actor: Principal, body: RunCreate) -> dict[str, Any]:
    today = await _today(session)
    period = parse_period(body.period, today)
    if (await session.execute(select(PayrollRun.id).where(PayrollRun.period == period))).first():
        raise AppError("PAYROLL_RUN_EXISTS", "A payroll run for this month already exists")
    cfg = await ptds.augment_config(session, await pset.snapshot(session), period)
    _, end = pdays.month_bounds(period)
    run = PayrollRun(
        period=period, status="draft", pay_date=body.pay_date, attendance_cutoff=min(end, today), config=cfg, created_by=actor.id,
        contributors=[str(actor.id)],
    )
    session.add(run)
    try:
        await pc.flush(session)
    except AppError as exc:
        if exc.code == "CONFLICT":
            raise AppError("PAYROLL_RUN_EXISTS", "A payroll run for this month already exists") from None
        raise
    b = await _bulk(session, period)
    tz = b.ctx.tz
    fresh: list[tuple[User, Payslip]] = []
    for u in b.users:
        det = b.details.get(u.id)
        if not _eligible(u, det, period, tz, u.id in b.fnf):
            continue
        ps = _new_payslip(run, u, actor)
        pull_identity(ps, u, det)
        if not pull_days(ps, u, b.ctx, run.attendance_cutoff, cfg, det, settlement=u.id in b.fnf):
            continue
        fresh.append((u, ps))
    await comps.pull_for(session, [ps for _, ps in fresh], period, lock=False)  # pay components (plan 15 P2)
    rc = await ptds.run_ctx(session, run)
    tdata = await ptds.load_data(session, rc.fy, period, [u.id for u, _ in fresh]) if rc else {}
    for u, ps in fresh:
        recompute(
            ps, user=u, period=period, structs=b.structs.get(u.id, []), adopt_latest=True, cfg=cfg, prior=b.prior.get(u.id, []),
            lwd=_lwd(b.details.get(u.id)), tds=ptds.Ctx(rc, tdata[u.id]) if rc else None,
        )  # fmt: skip
        ps.version = 1
        session.add(ps)
    await ptds.persist(session, [ps for _, ps in fresh], actor.id)
    n = len(fresh)
    audit(session, action="payroll.run.create", actor=actor, entity_type="payroll_run", entity_id=run.id,
          after={"period": pc.period_str(period), "employees": n})  # fmt: skip
    await pc.commit(session)
    return await run_out(session, run)


async def get_run(session: AsyncSession, actor: Principal, run_id: uuid.UUID) -> dict[str, Any]:
    run = await _run_or_404(session, run_id)
    out = await run_out(session, run)
    audit(session, action="payroll.run.view", actor=actor, entity_type="payroll_run", entity_id=run.id)
    await pc.commit(session)
    return out


def _require_status(run: PayrollRun, status: str) -> None:
    if run.status == status:
        return
    if run.status == "published":
        raise AppError("PAYROLL_LOCKED", "This payroll run is published and locked")
    raise AppError("CONFLICT", f"The run is {run.status}; this action needs a {status} run")


async def refresh_run(session: AsyncSession, actor: Principal, run_id: uuid.UUID) -> dict[str, Any]:
    run = await _run_or_404(session, run_id, lock=True)
    _require_status(run, "draft")
    pc.touch(run, actor.id)
    today = await _today(session)
    run.config = await ptds.augment_config(session, await pset.snapshot(session), run.period)
    _, end = pdays.month_bounds(run.period)
    run.attendance_cutoff = min(end, today)
    rows = await _run_payslips(session, run.id, lock=True)
    b = await _bulk(session, run.period)
    existing = {p.user_id: p for p in rows if p.status in OPEN}
    added = updated = 0
    todo: list[tuple[User, Payslip, bool, tuple[str, ...]]] = []  # (user, payslip, is_new, keep)
    for u in b.users:
        det = b.details.get(u.id)
        ps = existing.get(u.id)
        eligible = _eligible(u, det, run.period, b.ctx.tz, u.id in b.fnf)
        if ps is None:
            if not eligible:
                continue
            ps = _new_payslip(run, u, actor)
            pull_identity(ps, u, det)
            if not pull_days(ps, u, b.ctx, run.attendance_cutoff, run.config, det, settlement=u.id in b.fnf):
                continue
            todo.append((u, ps, True, ("NOT_ELIGIBLE",)))
            continue
        keep = ("NOT_ELIGIBLE",) if not eligible else ()
        pull_identity(ps, u, det)
        pull_days(ps, u, b.ctx, run.attendance_cutoff, run.config, det, settlement=u.id in b.fnf)
        if not eligible:
            ps.warnings = [*(ps.warnings or []), "NOT_ELIGIBLE"]
        ps.updated_by = actor.id
        pc.touch(ps, actor.id)
        todo.append((u, ps, False, keep))
    await comps.pull_for(session, [ps for _, ps, _, _ in todo], run.period, lock=False)  # pay components (plan 15 P2)
    rc = await ptds.run_ctx(session, run)
    tdata = await ptds.load_data(session, rc.fy, run.period, [u.id for u, _, _, _ in todo]) if rc else {}
    for u, ps, is_new, keep in todo:
        recompute(ps, user=u, period=run.period, structs=b.structs.get(u.id, []), adopt_latest=True, cfg=run.config,
                  prior=b.prior.get(u.id, []), keep=keep, lwd=_lwd(b.details.get(u.id)),
                  tds=ptds.Ctx(rc, tdata[u.id]) if rc else None)  # fmt: skip
        if is_new:
            ps.version = 1
            session.add(ps)
            added += 1
        else:
            updated += 1
    await ptds.persist(session, [ps for _, ps, _, _ in todo], actor.id)
    audit(session, action="payroll.run.refresh", actor=actor, entity_type="payroll_run", entity_id=run.id,
          after={"added": added, "updated": updated})  # fmt: skip
    await _stale_requests(session, actor, run, "RUN_REFRESHED")
    await pc.commit(session)
    return await run_out(session, run)


async def _recompute_open(
    session: AsyncSession, run: PayrollRun, payslips: list[Payslip], actor_id: uuid.UUID | None = None
) -> list[tuple[str, str]]:
    """Recompute the given open payslips against current structures / identity / settings. Returns (employee_id, code) errors.
    The TDS computation rows are written (and flushed) here, while the payslips are still `draft`: the caller moves them to
    `processed` afterwards and the database freezes the rows with them (T8)."""
    uids = list({p.user_id for p in payslips})
    b = await _bulk(session, run.period, uids, share_lock_structs=True)  # processing makes a structure 'in use'
    users = {u.id: u for u in b.users}
    errors: list[tuple[str, str]] = []
    # plan 17 lock order: structures (above), tax rules, declarations, proofs (share), THEN the pay component rows
    rc = await ptds.run_ctx(session, run, lock=True)
    tdata = await ptds.load_data(session, rc.fy, run.period, uids, lock=True) if rc else {}
    await comps.pull_for(session, payslips, run.period, lock=True)  # source rows locked FOR UPDATE (plan 15 P6 / P7)
    for p in payslips:
        u = users[p.user_id]
        pull_identity(p, u, b.details.get(u.id))
        recompute(p, user=u, period=run.period, structs=b.structs.get(u.id, []), adopt_latest=True, cfg=run.config,
                  prior=b.prior.get(u.id, []), lwd=_lwd(b.details.get(u.id)),
                  tds=ptds.Ctx(rc, tdata[u.id]) if rc else None)  # fmt: skip
        for code in _split(p.warnings)[0]:
            errors.append((u.employee_id, code))
    if not errors:
        await ptds.persist(session, payslips, actor_id)
    return errors


async def _blocked(
    session: AsyncSession, actor: Principal, entity_type: str, entity_id: Any, errors: list[tuple[str, str]]
) -> AppError:
    who = _plain(actor)  # plain values: the rollback below expires the ORM objects
    await session.rollback()
    audit(session, action=f"payroll.{'run' if entity_type == 'payroll_run' else 'payslip'}.process", actor=who,
          entity_type=entity_type, entity_id=entity_id, outcome="failure",
          after={"blocked": len(errors), "codes": sorted({c for _, c in errors})})  # fmt: skip
    await session.commit()
    return AppError(
        "PAYROLL_BLOCKED",
        "Some payslips have errors; fix them and process again",
        details=[{"field": eid, "message": code} for eid, code in errors],
    )


def _plain(actor: Principal):
    from types import SimpleNamespace

    return SimpleNamespace(id=actor.id, employee_id=actor.employee_id)


async def process_run(session: AsyncSession, actor: Principal, run_id: uuid.UUID) -> dict[str, Any]:
    who_plain = _plain(actor)
    run = await _run_or_404(session, run_id, lock=True)
    _require_status(run, "draft")
    rows = await _run_payslips(session, run.id, lock=True)
    todo = [p for p in rows if p.status == "draft" and not p.hold]
    errors = await _recompute_open(session, run, todo, actor.id)
    if errors:
        raise await _blocked(session, actor, "payroll_run", run.id, errors)
    await comps.attach_processing(session, todo, actor.id)
    now = utcnow()
    for p in todo:
        p.status, p.processed_at, p.processed_by, p.updated_by = "processed", now, actor.id, actor.id
        pc.touch(p, actor.id)
    run.status, run.processed_at, run.processed_by = "processed", now, actor.id
    pc.touch(run, actor.id)
    audit(session, action="payroll.run.process", actor=who_plain, entity_type="payroll_run", entity_id=run.id,
          after={"payslips": len(todo), "held": sum(p.hold for p in rows if p.status == "draft")})  # fmt: skip
    await pc.commit(session)
    return await run_out(session, run)


async def reopen_run(session: AsyncSession, actor: Principal, run_id: uuid.UUID) -> dict[str, Any]:
    run = await _run_or_404(session, run_id, lock=True)
    _require_status(run, "processed")
    rows = await _run_payslips(session, run.id, lock=True)
    await comps.release_processing(session, [p.id for p in rows if p.status == "processed"], actor.id)
    for p in rows:
        if p.status == "processed":
            p.status, p.processed_at, p.processed_by, p.updated_by = "draft", None, None, actor.id
            p.version += 1
            pc.touch(p, actor.id)
    run.status, run.processed_at, run.processed_by = "draft", None, None
    pc.touch(run, actor.id)
    audit(session, action="payroll.run.reopen", actor=actor, entity_type="payroll_run", entity_id=run.id)
    await _stale_requests(session, actor, run, "RUN_REOPENED")
    await pc.commit(session)
    return await run_out(session, run)


async def delete_run(session: AsyncSession, actor: Principal, run_id: uuid.UUID) -> None:
    run = await _run_or_404(session, run_id, lock=True)
    rows = await _run_payslips(session, run.id, lock=True)
    if run.status != "draft" or any(p.status in ("published", "superseded") for p in rows):
        raise AppError("PAYROLL_LOCKED", "Only a draft run that was never published can be deleted")
    if (await session.execute(select(PayrollPublishRequest.id).where(PayrollPublishRequest.run_id == run.id).limit(1))).first():
        raise AppError("CONFLICT", "This run has publish requests (kept as evidence) and cannot be deleted")
    info = {"period": pc.period_str(run.period), "payslips": len(rows)}
    await session.execute(delete(Payslip).where(Payslip.run_id == run.id))
    await session.delete(run)
    audit(session, action="payroll.run.delete", actor=actor, entity_type="payroll_run", entity_id=run_id, after=info)
    await pc.commit(session)


async def maker_checker_applies(session: AsyncSession, run: PayrollRun) -> bool:
    """Publish maker-checker applies when the run's snapshot OR the CURRENT setting is on (switching the setting off after the run
    was created does not bypass it). While it applies, publishing goes through a publish request (plan 17 G1, contract 19.2)."""
    return bool(run.config.get("publish_requires_second_person") or await pset.current_value(session, pset.SECOND_PERSON_KEY))


async def _stale_requests(
    session: AsyncSession, actor: Principal, run: PayrollRun, reason: str, *, payslip_id: uuid.UUID | None = None
) -> None:
    """Open publish requests of the run become `stale` in the caller's transaction (the request row is locked LAST)."""
    from . import payroll_publish_requests as prs  # local import: that module imports this one

    await prs.mark_stale(session, run.id, reason, actor.id, payslip_id=payslip_id, actor=_plain(actor))


async def _share_lock_structures(session: AsyncSession, payslips: list[Payslip]) -> None:
    ids = sorted({p.structure_id for p in payslips if p.structure_id})
    if ids:
        q = select(SalaryStructure.id).where(SalaryStructure.id.in_(ids)).order_by(SalaryStructure.id)
        await session.execute(q.with_for_update(read=True))


async def _publish_one(session: AsyncSession, p: Payslip, actor_id: uuid.UUID, now: dt.datetime) -> None:
    """processed -> published. A revision first sets the previous live copy to 'superseded' and FLUSHES it (partial unique
    index: one live copy), all in the caller's transaction."""
    if p.supersedes_id:
        old = (await session.execute(select(Payslip).where(Payslip.id == p.supersedes_id).with_for_update())).scalar_one_or_none()
        if old is not None and old.status == "published":
            old.status, old.superseded_at = "superseded", now
            await pc.flush(session)
    p.status, p.published_at, p.published_by, p.updated_by = "published", now, actor_id, actor_id
    await pc.flush(session)
    session.add(PayslipDelivery(payslip_id=p.id, user_id=p.user_id))  # push-only delivery tracking (contract 19.3)
    from . import payroll_fnf  # local import: payroll_fnf imports this module

    await payroll_fnf.on_published(session, p, actor_id, now)  # locks the approved F&F LAST (plan 16 F15)


async def _resync_arrears(session: AsyncSession, actor: Principal, revised_users: list[uuid.UUID]) -> None:
    """A published REVISION changes what a month paid: recompute the employee's arrears proposal in the same transaction
    (H1). An accepted-but-unpaid row returns to 'proposed' with a new version; arrears already paid are subtracted, so a
    revision that adopted the new structure yields a recovery instead of paying the same difference twice."""
    from . import payroll_arrears  # local import: payroll_arrears imports this module

    for uid in sorted(set(revised_users)):
        row = await payroll_arrears.sync_proposal(session, uid, actor.id)
        audit(session, action="payroll.arrears.propose", actor=actor, entity_type="payroll_arrears",
              entity_id=row.id if row else None,
              after={"user_id": str(uid), "proposal": row is not None, "trigger": "revision"})  # fmt: skip


async def _push_settled(session: AsyncSession) -> None:
    from . import payroll_fnf

    await payroll_fnf.push_settled(session)


async def _push_published(session: AsyncSession, items: list[tuple[uuid.UUID, uuid.UUID, dt.date, bool]]) -> None:
    """Data-only push after commit (best effort, never amounts). A push that reached a device stamps the delivery row."""
    stamped: list[uuid.UUID] = []
    for user_id, payslip_id, period, revised in items:
        try:
            n = await notifications.notify(
                session, [user_id], type="payslip_published", entity_id=payslip_id,
                title="Revised payslip available" if revised else "Payslip available",
                body=f"Your payslip for {month_label(period)} is ready.",
            )  # fmt: skip
            if n:
                stamped.append(payslip_id)
        except Exception:  # noqa: BLE001
            log.exception("payslip_published push failed (ignored)")
    if stamped:
        try:
            now = utcnow()
            for d in (await session.execute(select(PayslipDelivery).where(PayslipDelivery.payslip_id.in_(stamped)))).scalars():
                if d.pushed_at is None:
                    d.pushed_at = now
                d.push_count = min(10, (d.push_count or 0) + 1)
            await session.commit()
        except Exception:  # noqa: BLE001 - delivery statistics only
            await session.rollback()
            log.exception("delivery stamp failed (ignored)")


async def publish_run_core(
    session: AsyncSession, actor: Principal, run: PayrollRun, todo: list[Payslip], *, request_id: uuid.UUID | None = None
) -> list[tuple[uuid.UUID, uuid.UUID, dt.date, bool]]:
    """The publish itself (13.5 internals): the caller holds the run and payslip locks and commits. Direct publish and the approval
    of a publish request both run exactly this code in their own transaction. Returns the pushes to send AFTER the commit."""
    who = _plain(actor)
    await _share_lock_structures(session, todo)
    await comps.mark_paid(session, todo, actor.id)  # processing -> paid, carried rows follow the new revision
    now = utcnow()
    company = await get_company(session)
    pushes = []
    for p in todo:
        await _publish_one(session, p, actor.id, now)
        pushes.append((p.user_id, p.id, run.period, p.revision > 1))
    await _resync_arrears(session, who, [p.user_id for p in todo if p.revision > 1])
    run.status, run.published_at, run.published_by, run.company_name = (
        "published",
        now,
        actor.id,
        (company.name or "")[:100] or None,
    )
    after: dict[str, Any] = {"payslips": len(todo), "self_included": any(p.user_id == actor.id for p in todo)}
    if request_id is not None:
        after["request_id"] = str(request_id)
    audit(session, action="payroll.run.publish", actor=who, entity_type="payroll_run", entity_id=run.id, after=after)
    return pushes


async def publish_run(session: AsyncSession, redis: Redis, actor: Principal, run_id: uuid.UUID, password) -> dict[str, Any]:
    await admin_security.require_reauth(session, redis, actor, password, action="payroll.run.publish")
    run = await _run_or_404(session, run_id, lock=True)
    if run.status != "processed":
        raise AppError("PAYROLL_LOCKED", "Only a processed run can be published (it may already be published)")
    if await maker_checker_applies(session, run):
        raise AppError("PUBLISH_REQUEST_REQUIRED", "Publishing needs a second person: create a publish request")
    rows = await _run_payslips(session, run.id, lock=True)
    todo = [p for p in rows if p.status == "processed" and not p.hold]
    if not todo:
        raise AppError("NOTHING_TO_PUBLISH", "There is no processed payslip to publish")
    pushes = await publish_run_core(session, actor, run, todo)
    await pc.commit(session)
    await _push_published(session, pushes)
    await _push_settled(session)
    return await run_out(session, run)


# ============================================================================================ payslip level
async def _payslip_locked(session: AsyncSession, payslip_id: uuid.UUID) -> tuple[PayrollRun, Payslip]:
    """Run row first, then the payslip row (the global locking order)."""
    run_id = (await session.execute(select(Payslip.run_id).where(Payslip.id == payslip_id))).scalar_one_or_none()
    if run_id is None:
        raise AppError("NOT_FOUND", "Payslip not found")
    run = await _run_or_404(session, run_id, lock=True)
    ps = (await session.execute(select(Payslip).where(Payslip.id == payslip_id).with_for_update())).scalar_one_or_none()
    if ps is None:
        raise AppError("NOT_FOUND", "Payslip not found")
    return run, ps


async def get_payslip(session: AsyncSession, actor: Principal, payslip_id: uuid.UUID) -> dict[str, Any]:
    ps = (await session.execute(select(Payslip).where(Payslip.id == payslip_id))).scalar_one_or_none()
    if ps is None:
        raise AppError("NOT_FOUND", "Payslip not found")
    out = await admin_payslip_out(session, ps)
    audit(session, action="payroll.payslip.view", actor=actor, entity_type="payslip", entity_id=ps.id)
    await pc.commit(session)
    return out


async def list_run_payslips(
    session: AsyncSession, run_id: uuid.UUID, q: str | None, status: str | None, flag: str | None, page: PageParams
) -> dict[str, Any]:
    run = await _run_or_404(session, run_id)
    rows = [p for p in await _run_payslips(session, run.id) if (status == "superseded" or p.status != "superseded")]
    users = {
        u.id: u
        for u in (await session.execute(select(User).where(User.id.in_(list({p.user_id for p in rows}))))).unique().scalars()
    }
    needle = (q or "").strip().lower()
    items = []
    for p in rows:
        u = users[p.user_id]
        if status and p.status != status:
            continue
        if needle and needle not in u.full_name.lower() and needle not in u.employee_id.lower():
            continue
        errors, warnings = _split(p.warnings)
        if flag == "error" and not errors or flag == "warning" and not warnings or flag == "hold" and not p.hold:
            continue
        items.append((u.full_name.lower(), u.employee_id, p, errors, warnings))
    items.sort(key=lambda t: (t[0], t[1], t[2].revision))
    total = len(items)
    window = items[page.offset : page.offset + page.page_size]
    return make_page(
        [
            {
                "id": str(p.id),
                "user": pc.user_out(users[p.user_id]),
                "revision": p.revision,
                "status": p.status,
                "hold": p.hold,
                "paid_days": float(p.paid_days),
                "lop_days": _lop_days(p),
                **_payslip_money(p),
                "errors": errors,
                "warnings": warnings,
            }  # fmt: skip
            for _, _, p, errors, warnings in window
        ],
        page,
        total,
    )


async def update_payslip(session: AsyncSession, actor: Principal, payslip_id: uuid.UUID, body: PayslipUpdate) -> dict[str, Any]:
    run, ps = await _payslip_locked(session, payslip_id)
    await pc.forbid_self(session, actor, ps.user_id, entity_type="payslip", entity_id=ps.id)
    if ps.status != "draft":
        raise AppError("PAYROLL_LOCKED", "Only a draft payslip can be edited (reopen it first)")
    if ps.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The payslip was changed by someone else; reload it")
    sent = body.model_fields_set - {"expected_version"}
    inputs = copy.deepcopy(ps.inputs or _blank_inputs())
    if "lop_override" in sent:
        if body.lop_override is None:
            ps.lop_override, ps.lop_override_reason = None, None
        else:
            v = _d1(body.lop_override)
            if v > ps.employed_days:
                raise AppError("VALIDATION_ERROR", "lop_override cannot exceed the employed days",
                               details=[{"field": "lop_override", "message": "cannot exceed the employed days"}])  # fmt: skip
            ps.lop_override, ps.lop_override_reason = v, body.lop_override_reason
    elif "lop_override_reason" in sent and ps.lop_override is not None and body.lop_override_reason:
        ps.lop_override_reason = body.lop_override_reason
    rc = await ptds.run_ctx(session, run)
    if "tds" in sent:
        ptds.set_manual_tds(inputs, body.tds, body.tds_override_reason, rc)
    if "pt_override" in sent:
        inputs["pt_override"] = calc.money_str(body.pt_override) if body.pt_override is not None else None
    if "adjustments" in sent:
        inputs["adjustments"] = [
            {"code": a.code, "label": a.label, "amount": calc.money_str(a.amount)} for a in body.adjustments or []
        ]
    if "note" in sent:
        inputs["note"] = body.note
    if "hold" in sent:
        ps.hold = bool(body.hold)
    ps.inputs = inputs
    user = await session.get(User, ps.user_id)
    structs = (await _load_structs(session, [ps.user_id], run.period)).get(ps.user_id, [])
    prior = (await _prior_gross(session, [ps.user_id], run.period)).get(ps.user_id, [])
    tdata = await ptds.load_data(session, rc.fy, run.period, [ps.user_id]) if rc else {}
    recompute(ps, user=user, period=run.period, structs=structs, adopt_latest=False, cfg=run.config, prior=prior,
              lwd=_lwd((await _load_details(session, [ps.user_id])).get(ps.user_id)),
              tds=ptds.Ctx(rc, tdata[ps.user_id]) if rc else None)  # fmt: skip
    ps.updated_by = actor.id
    pc.touch(ps, actor.id)
    await ptds.persist(session, [ps], actor.id)
    audit(session, action="payroll.payslip.update", actor=actor, entity_type="payslip", entity_id=ps.id,
          after={"fields": sorted(sent)})  # fmt: skip
    await pc.commit(session)
    return await admin_payslip_out(session, ps)


async def delete_payslip(session: AsyncSession, actor: Principal, payslip_id: uuid.UUID) -> None:
    run, ps = await _payslip_locked(session, payslip_id)
    await pc.forbid_self(session, actor, ps.user_id, entity_type="payslip", entity_id=ps.id)
    if ps.status != "draft":
        raise AppError("PAYROLL_LOCKED", "Only a draft payslip can be deleted")
    info = {"run_id": str(run.id), "user_id": str(ps.user_id), "revision": ps.revision}
    if (await session.execute(select(PayrollPublishRequest.id).where(PayrollPublishRequest.payslip_id == ps.id).limit(1))).first():
        raise AppError("CONFLICT", "This payslip has publish requests (kept as evidence) and cannot be deleted")
    await session.delete(ps)
    audit(session, action="payroll.payslip.delete", actor=actor, entity_type="payslip", entity_id=payslip_id, after=info)
    await pc.commit(session)


async def process_payslip(session: AsyncSession, actor: Principal, payslip_id: uuid.UUID) -> dict[str, Any]:
    run, ps = await _payslip_locked(session, payslip_id)
    await pc.forbid_self(session, actor, ps.user_id, entity_type="payslip", entity_id=ps.id)
    if run.status != "published":
        raise AppError("CONFLICT", "Process the whole run instead; single payslips are processed after the run is published")
    if ps.status != "draft":
        raise AppError("PAYROLL_LOCKED", "Only a draft payslip can be processed")
    errors = await _recompute_open(session, run, [ps], actor.id)
    if errors:
        raise await _blocked(session, actor, "payslip", ps.id, errors)
    await comps.attach_processing(session, [ps], actor.id)
    now = utcnow()
    ps.status, ps.processed_at, ps.processed_by, ps.updated_by, ps.hold = "processed", now, actor.id, actor.id, False
    pc.touch(ps, actor.id)
    audit(session, action="payroll.payslip.process", actor=actor, entity_type="payslip", entity_id=ps.id)
    await _stale_requests(session, actor, run, "PAYSLIP_CHANGED")
    await pc.commit(session)
    return await admin_payslip_out(session, ps)


async def reopen_payslip(session: AsyncSession, actor: Principal, payslip_id: uuid.UUID) -> dict[str, Any]:
    run, ps = await _payslip_locked(session, payslip_id)
    await pc.forbid_self(session, actor, ps.user_id, entity_type="payslip", entity_id=ps.id)
    if run.status != "published" or ps.status != "processed":
        raise AppError("PAYROLL_LOCKED", "Only a processed payslip of a published run can be reopened")
    await comps.release_processing(session, [ps.id], actor.id)
    ps.status, ps.processed_at, ps.processed_by, ps.updated_by = "draft", None, None, actor.id
    ps.version += 1
    pc.touch(ps, actor.id)
    audit(session, action="payroll.payslip.reopen", actor=actor, entity_type="payslip", entity_id=ps.id)
    await _stale_requests(session, actor, run, "PAYSLIP_CHANGED")
    await pc.commit(session)
    return await admin_payslip_out(session, ps)


async def publish_payslip_core(
    session: AsyncSession, actor: Principal, run: PayrollRun, ps: Payslip, *, request_id: uuid.UUID | None = None
) -> list[tuple[uuid.UUID, uuid.UUID, dt.date, bool]]:
    """Single-payslip publish internals (13.6), shared by the direct endpoint and the approval of a publish request."""
    who = _plain(actor)
    await _share_lock_structures(session, [ps])
    await comps.mark_paid(session, [ps], actor.id)
    now = utcnow()
    await _publish_one(session, ps, actor.id, now)
    if ps.revision > 1:
        await _resync_arrears(session, actor, [ps.user_id])
    after: dict[str, Any] = {"revision": ps.revision, "run_id": str(run.id)}
    if request_id is not None:
        after["request_id"] = str(request_id)
    audit(session, action="payroll.payslip.publish", actor=who, entity_type="payslip", entity_id=ps.id, after=after)
    return [(ps.user_id, ps.id, run.period, ps.revision > 1)]


async def publish_payslip(
    session: AsyncSession, redis: Redis, actor: Principal, payslip_id: uuid.UUID, password
) -> dict[str, Any]:
    owner_id = (await session.execute(select(Payslip.user_id).where(Payslip.id == payslip_id))).scalar_one_or_none()
    if owner_id is None:
        raise AppError("NOT_FOUND", "Payslip not found")
    await pc.forbid_self(session, actor, owner_id, entity_type="payslip", entity_id=payslip_id)
    await admin_security.require_reauth(session, redis, actor, password, action="payroll.payslip.publish")
    run, ps = await _payslip_locked(session, payslip_id)
    if run.status != "published" or ps.status != "processed":
        raise AppError("PAYROLL_LOCKED", "Only a processed payslip of a published run can be published")
    if await maker_checker_applies(session, run):
        raise AppError("PUBLISH_REQUEST_REQUIRED", "Publishing needs a second person: create a publish request")
    push = await publish_payslip_core(session, actor, run, ps)
    await pc.commit(session)
    await _push_published(session, push)
    await _push_settled(session)
    return await admin_payslip_out(session, ps)


async def revise(session: AsyncSession, actor: Principal, run_id: uuid.UUID, user_id: uuid.UUID, reason: str) -> dict[str, Any]:
    run = await _run_or_404(session, run_id, lock=True)
    await pc.forbid_self(session, actor, user_id, entity_type="payslip", entity_id=run_id)
    if run.status != "published":
        raise AppError("PAYROLL_LOCKED", "Revisions are only for published runs")
    rows = [p for p in await _run_payslips(session, run.id, lock=True) if p.user_id == user_id]
    if any(p.status in OPEN for p in rows):
        raise AppError("CONFLICT", "An open revision already exists for this employee")
    live = next((p for p in rows if p.status == "published"), None)
    b = await _bulk(session, run.period, [user_id])
    if not b.users:
        raise AppError("NOT_FOUND", "Employee not found")
    u = b.users[0]
    det = b.details.get(u.id)
    if live is not None and live.revision >= 99:
        raise AppError("CONFLICT", "Revision limit reached")
    new = _new_payslip(run, u, actor, revision=(live.revision + 1) if live else 1)
    new.revision_reason = reason
    pull_identity(new, u, det)
    if live is not None:
        new.supersedes_id = live.id
        new.inputs = copy.deepcopy(live.inputs)
        new.lop_override, new.lop_override_reason = live.lop_override, live.lop_override_reason
    elif not _eligible(u, det, run.period, b.ctx.tz, u.id in b.fnf):
        raise AppError("VALIDATION_ERROR", "The employee is not eligible for payroll in this period",
                       details=[{"field": "user_id", "message": "not eligible"}])  # fmt: skip
    cutoff = min(pdays.month_bounds(run.period)[1], await _today(session))  # the run itself is locked: never touched
    settlement = u.id in b.fnf or (live is not None and bool((live.inputs or {}).get("settlement_only")))
    if not pull_days(new, u, b.ctx, cutoff, run.config, det, settlement=settlement):
        raise AppError("VALIDATION_ERROR", "The employee was not employed in this period",
                       details=[{"field": "user_id", "message": "not employed"}])  # fmt: skip
    await comps.pull_for(session, [new], run.period, lock=False)  # carried `paid` rows of the live copy + new pending rows
    rc = await ptds.run_ctx(session, run)
    tdata = await ptds.load_data(session, rc.fy, run.period, [u.id]) if rc else {}
    recompute(new, user=u, period=run.period, structs=b.structs.get(u.id, []), adopt_latest=True, cfg=run.config,
              prior=b.prior.get(u.id, []), lwd=_lwd(det), tds=ptds.Ctx(rc, tdata[u.id]) if rc else None)  # fmt: skip
    new.version = 1
    session.add(new)
    await pc.flush(session)
    await ptds.persist(session, [new], actor.id)
    audit(session, action="payroll.payslip.revise", actor=actor, entity_type="payslip", entity_id=new.id,
          after={"run_id": str(run.id), "user_id": str(user_id), "revision": new.revision})  # fmt: skip
    await pc.commit(session)
    return await admin_payslip_out(session, new)


# ============================================================================================ employee side
async def list_own(session: AsyncSession, actor: Principal, year: int | None) -> dict[str, Any]:
    year = year or (await _today(session)).year
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise AppError("VALIDATION_ERROR", "year out of range", details=[{"field": "year", "message": "2000-2200"}])
    q = (
        select(Payslip, PayrollRun)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(
            Payslip.user_id == actor.id,
            Payslip.status == "published",
            PayrollRun.period >= dt.date(year, 1, 1),
            PayrollRun.period <= dt.date(year, 12, 1),
        )
        .order_by(PayrollRun.period.desc())
    )
    from ..models import FnfSettlement

    final_periods = set(
        (
            await session.execute(
                select(FnfSettlement.settlement_period).where(FnfSettlement.user_id == actor.id, FnfSettlement.status == "settled")
            )
        ).scalars()
    )
    items = [
        {
            "id": str(p.id),
            "period": pc.period_str(r.period),
            "is_final": r.period in final_periods,
            "revision": p.revision,
            "published_at": pc.iso(p.published_at),
            "revised": p.revision > 1,
            "pay_date": r.pay_date.isoformat() if r.pay_date else None,
        }  # fmt: skip
        for p, r in (await session.execute(q)).all()
    ]
    return {"year": year, "items": items}


async def own_payslip_or_404(session: AsyncSession, actor: Principal, payslip_id: uuid.UUID) -> Payslip:
    """Anything that is not the caller's published / superseded payslip is a 404 (no existence leak), audited as a probe."""
    ps = (await session.execute(select(Payslip).where(Payslip.id == payslip_id))).scalar_one_or_none()
    if ps is None or ps.user_id != actor.id or ps.status not in ("published", "superseded"):
        raise await pc.deny(
            session, actor, entity_type="payslip", entity_id=payslip_id, reason="not_own_or_not_published", status=404
        )
    return ps


async def own_view(session: AsyncSession, actor: Principal, ps: Payslip, *, audit_action: str) -> dict[str, Any]:
    run, succ, company, logo = await _view_parts(session, ps, admin=False)
    out = employee_view(
        ps, run, successor_id=succ, company=company, logo_url=logo,
        final=await settled_fnf(session, ps.user_id, run.period) is not None,
    )  # fmt: skip
    audit(session, action=audit_action, actor=actor, entity_type="payslip", entity_id=ps.id)
    from . import payroll_delivery  # local import: that module imports this one

    await payroll_delivery.mark_seen(session, ps, downloaded=False)
    await pc.commit(session)
    return out


async def pdf_parts(session: AsyncSession, ps: Payslip, *, admin: bool) -> tuple[dict[str, Any], str, bytes | None]:
    """(view, footer, logo png) for the PDF renderer. `admin=True` masks PAN / UAN / ESI IP number (see employee_view)."""
    from . import company as company_svc

    run, succ, company, logo_url = await _view_parts(session, ps, admin=admin)
    view = employee_view(
        ps, run, successor_id=succ, company=company, logo_url=logo_url, admin=admin,
        final=await settled_fnf(session, ps.user_id, run.period, live=admin) is not None,
    )  # fmt: skip
    view["pay_date"] = run.pay_date.isoformat() if run.pay_date else None
    if ps.supersedes_id:
        prev = await session.get(Payslip, ps.supersedes_id)
        view["supersedes_published_at"] = pc.iso(prev.published_at) if prev else None
    logo = await company_svc.logo_bytes(session)
    return view, str(run.config.get("payslip_footer") or ""), (logo[0] if logo else None)
