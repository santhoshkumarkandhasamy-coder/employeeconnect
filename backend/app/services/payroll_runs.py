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
from ..models import EmployeePayrollDetails, PayrollRun, Payslip, SalaryStructure, User
from ..schemas.payroll import PayslipUpdate, RunCreate
from . import admin_security, notifications
from . import attendance as att
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_days as pdays
from . import payroll_settings as pset
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
    return {"tds": None, "pt_override": None, "adjustments": [], "note": None}


def _pay_inputs(inputs: dict) -> calc.PayInputs:
    return calc.PayInputs(
        tds=Decimal(inputs["tds"]) if inputs.get("tds") is not None else None,
        pt_override=Decimal(inputs["pt_override"]) if inputs.get("pt_override") is not None else None,
        adjustments=tuple(calc.Adjustment(a["code"], a["label"], Decimal(a["amount"])) for a in inputs.get("adjustments", [])),
    )


def pull_identity(ps: Payslip, user: User, det: EmployeePayrollDetails | None) -> None:
    ps.employee_snapshot = _snapshot(user, det)
    ps.identity = _identity(det)


def pull_days(ps: Payslip, user: User, ctx: pdays.DayContext, cutoff: dt.date, cfg: dict) -> bool:
    """Re-pull basis / employed / LOP from attendance and leave. False = not employed in the period."""
    res = pdays.compute_days(user, ctx, cutoff, cfg)
    if res is None:
        return False
    ps.basis_days = _d1(res.basis_days)
    ps.employed_days = _d1(res.employed_days)
    ps.lop_auto = _d1(res.lop_auto)
    ps.lop_breakdown = res.breakdown
    ps.paid_days = ps.employed_days  # fixed by recompute()
    return True


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
) -> None:
    """Sets paid_days, structure_id, result and warnings from the stored days / inputs. Pure apart from mutating `ps`."""
    from .payroll_structures import data_of

    warns: list[str] = [w for w in (ps.warnings or []) if w in keep]
    lop = ps.lop_override if ps.lop_override is not None else ps.lop_auto
    ps.paid_days = max(Decimal("0.0"), _d1(ps.employed_days) - _d1(lop))
    if ps.lop_override is not None:
        warns.append("LOP_OVERRIDDEN")
    if user.shift is None:
        warns.append("NO_SHIFT")
    if float((ps.lop_breakdown or {}).get("pending_leave_days", 0)) > 0:
        warns.append("PENDING_LEAVE")
    if ps.paid_days == 0:
        warns.append("ZERO_PAID_DAYS")
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
        eligible = calc.esi_eligible([(e, data_of(s)) for e, s in structs], period, user.join_date, rules.esi_wage_threshold)
        res = calc.calculate(
            data, rules, period, Decimal(ps.basis_days), Decimal(ps.paid_days), _pay_inputs(ps.inputs),
            esi_is_eligible=eligible, prior_gross=prior,
        )  # fmt: skip
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
        created_by=actor.id, updated_by=actor.id,
    )  # fmt: skip


def _eligible(user: User, det: EmployeePayrollDetails | None, period: dt.date, tz) -> bool:
    if det is not None and not det.include_in_payroll:
        return False
    start, end = pdays.month_bounds(period)
    return pdays.employment_window(user, start, end, tz) is not None


@dataclass
class _Bulk:
    users: list[User]
    details: dict
    structs: dict
    ctx: pdays.DayContext
    prior: dict


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
async def run_out(session: AsyncSession, run: PayrollRun, rows: list[Payslip] | None = None) -> dict[str, Any]:
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
        o = await run_out(session, r)
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


def employee_view(
    p: Payslip, run: PayrollRun, *, successor_id: uuid.UUID | None, company, logo_url: str | None, admin: bool = False
) -> dict[str, Any]:
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
    out = employee_view(p, run, successor_id=succ, company=company, logo_url=logo, admin=True)
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
                "note": inputs.get("note"),
            },
            "errors": errors,
            "warnings": warnings,
            "processed_at": pc.iso(p.processed_at),
            "processed_by": pc.user_out(people.get(p.processed_by)),
            "published_by": pc.user_out(people.get(p.published_by)),
            "updated_at": pc.iso(p.updated_at),
        }
    )
    return out


# ============================================================================================ run lifecycle
async def create_run(session: AsyncSession, actor: Principal, body: RunCreate) -> dict[str, Any]:
    today = await _today(session)
    period = parse_period(body.period, today)
    if (await session.execute(select(PayrollRun.id).where(PayrollRun.period == period))).first():
        raise AppError("PAYROLL_RUN_EXISTS", "A payroll run for this month already exists")
    cfg = await pset.snapshot(session)
    _, end = pdays.month_bounds(period)
    run = PayrollRun(
        period=period, status="draft", pay_date=body.pay_date, attendance_cutoff=min(end, today), config=cfg, created_by=actor.id
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
    n = 0
    for u in b.users:
        det = b.details.get(u.id)
        if not _eligible(u, det, period, tz):
            continue
        ps = _new_payslip(run, u, actor)
        pull_identity(ps, u, det)
        if not pull_days(ps, u, b.ctx, run.attendance_cutoff, cfg):
            continue
        recompute(
            ps, user=u, period=period, structs=b.structs.get(u.id, []), adopt_latest=True, cfg=cfg, prior=b.prior.get(u.id, [])
        )
        ps.version = 1
        session.add(ps)
        n += 1
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
    today = await _today(session)
    run.config = await pset.snapshot(session)
    _, end = pdays.month_bounds(run.period)
    run.attendance_cutoff = min(end, today)
    rows = await _run_payslips(session, run.id, lock=True)
    b = await _bulk(session, run.period)
    existing = {p.user_id: p for p in rows if p.status in OPEN}
    added = updated = 0
    for u in b.users:
        det = b.details.get(u.id)
        ps = existing.get(u.id)
        eligible = _eligible(u, det, run.period, b.ctx.tz)
        if ps is None:
            if not eligible:
                continue
            ps = _new_payslip(run, u, actor)
            pull_identity(ps, u, det)
            if not pull_days(ps, u, b.ctx, run.attendance_cutoff, run.config):
                continue
            recompute(ps, user=u, period=run.period, structs=b.structs.get(u.id, []), adopt_latest=True, cfg=run.config,
                      prior=b.prior.get(u.id, []))  # fmt: skip
            ps.version = 1
            session.add(ps)
            added += 1
            continue
        keep = ("NOT_ELIGIBLE",) if not eligible else ()
        pull_identity(ps, u, det)
        pull_days(ps, u, b.ctx, run.attendance_cutoff, run.config)
        if not eligible:
            ps.warnings = [*(ps.warnings or []), "NOT_ELIGIBLE"]
        ps.updated_by = actor.id
        recompute(ps, user=u, period=run.period, structs=b.structs.get(u.id, []), adopt_latest=True, cfg=run.config,
                  prior=b.prior.get(u.id, []), keep=keep)  # fmt: skip
        updated += 1
    audit(session, action="payroll.run.refresh", actor=actor, entity_type="payroll_run", entity_id=run.id,
          after={"added": added, "updated": updated})  # fmt: skip
    await pc.commit(session)
    return await run_out(session, run)


async def _recompute_open(session: AsyncSession, run: PayrollRun, payslips: list[Payslip]) -> list[tuple[str, str]]:
    """Recompute the given open payslips against current structures / identity / settings. Returns (employee_id, code) errors."""
    uids = list({p.user_id for p in payslips})
    b = await _bulk(session, run.period, uids, share_lock_structs=True)  # processing makes a structure 'in use'
    users = {u.id: u for u in b.users}
    errors: list[tuple[str, str]] = []
    for p in payslips:
        u = users[p.user_id]
        pull_identity(p, u, b.details.get(u.id))
        recompute(p, user=u, period=run.period, structs=b.structs.get(u.id, []), adopt_latest=True, cfg=run.config,
                  prior=b.prior.get(u.id, []))  # fmt: skip
        for code in _split(p.warnings)[0]:
            errors.append((u.employee_id, code))
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
    errors = await _recompute_open(session, run, todo)
    if errors:
        raise await _blocked(session, actor, "payroll_run", run.id, errors)
    now = utcnow()
    for p in todo:
        p.status, p.processed_at, p.processed_by, p.updated_by = "processed", now, actor.id, actor.id
    run.status, run.processed_at, run.processed_by = "processed", now, actor.id
    audit(session, action="payroll.run.process", actor=who_plain, entity_type="payroll_run", entity_id=run.id,
          after={"payslips": len(todo), "held": sum(p.hold for p in rows if p.status == "draft")})  # fmt: skip
    await pc.commit(session)
    return await run_out(session, run)


async def reopen_run(session: AsyncSession, actor: Principal, run_id: uuid.UUID) -> dict[str, Any]:
    run = await _run_or_404(session, run_id, lock=True)
    _require_status(run, "processed")
    rows = await _run_payslips(session, run.id, lock=True)
    for p in rows:
        if p.status == "processed":
            p.status, p.processed_at, p.processed_by, p.updated_by = "draft", None, None, actor.id
            p.version += 1
    run.status, run.processed_at, run.processed_by = "draft", None, None
    audit(session, action="payroll.run.reopen", actor=actor, entity_type="payroll_run", entity_id=run.id)
    await pc.commit(session)
    return await run_out(session, run)


async def delete_run(session: AsyncSession, actor: Principal, run_id: uuid.UUID) -> None:
    run = await _run_or_404(session, run_id, lock=True)
    rows = await _run_payslips(session, run.id, lock=True)
    if run.status != "draft" or any(p.status in ("published", "superseded") for p in rows):
        raise AppError("PAYROLL_LOCKED", "Only a draft run that was never published can be deleted")
    info = {"period": pc.period_str(run.period), "payslips": len(rows)}
    await session.execute(delete(Payslip).where(Payslip.run_id == run.id))
    await session.delete(run)
    audit(session, action="payroll.run.delete", actor=actor, entity_type="payroll_run", entity_id=run_id, after=info)
    await pc.commit(session)


async def _check_second_person(
    session: AsyncSession,
    run: PayrollRun,
    payslips: list[Payslip],
    actor: Principal,
    *,
    run_processed_by: uuid.UUID | None = None,
) -> None:
    """Maker-checker. Enforced when the run's snapshot OR the CURRENT setting is on (switching the setting off after the run
    was created does not bypass it). The publisher must not be the person who processed the run, nor the last person to
    process or edit any payslip being published (`processed_by` / `updated_by`)."""
    if not (run.config.get("publish_requires_second_person") or await pset.current_value(session, pset.SECOND_PERSON_KEY)):
        return
    makers = {run_processed_by} | {x for p in payslips for x in (p.processed_by, p.updated_by)}
    if actor.id in makers:
        raise AppError("SECOND_PERSON_REQUIRED", "A different person must publish what you processed or edited")


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


async def _push_published(session: AsyncSession, items: list[tuple[uuid.UUID, uuid.UUID, dt.date, bool]]) -> None:
    """Data-only push after commit (best effort, never amounts)."""
    for user_id, payslip_id, period, revised in items:
        try:
            await notifications.notify(
                session, [user_id], type="payslip_published", entity_id=payslip_id,
                title="Revised payslip available" if revised else "Payslip available",
                body=f"Your payslip for {month_label(period)} is ready.",
            )  # fmt: skip
        except Exception:  # noqa: BLE001
            log.exception("payslip_published push failed (ignored)")


async def publish_run(session: AsyncSession, redis: Redis, actor: Principal, run_id: uuid.UUID, password) -> dict[str, Any]:
    await admin_security.require_reauth(session, redis, actor, password, action="payroll.run.publish")
    who = _plain(actor)
    run = await _run_or_404(session, run_id, lock=True)
    if run.status != "processed":
        raise AppError("PAYROLL_LOCKED", "Only a processed run can be published (it may already be published)")
    rows = await _run_payslips(session, run.id, lock=True)
    todo = [p for p in rows if p.status == "processed" and not p.hold]
    if not todo:
        raise AppError("NOTHING_TO_PUBLISH", "There is no processed payslip to publish")
    await _check_second_person(session, run, todo, actor, run_processed_by=run.processed_by)
    await _share_lock_structures(session, todo)
    now = utcnow()
    company = await get_company(session)
    pushes = []
    for p in todo:
        await _publish_one(session, p, actor.id, now)
        pushes.append((p.user_id, p.id, run.period, p.revision > 1))
    run.status, run.published_at, run.published_by, run.company_name = (
        "published",
        now,
        actor.id,
        (company.name or "")[:100] or None,
    )
    audit(session, action="payroll.run.publish", actor=who, entity_type="payroll_run", entity_id=run.id,
          after={"payslips": len(todo), "self_included": any(p.user_id == actor.id for p in todo)})  # fmt: skip
    await pc.commit(session)
    await _push_published(session, pushes)
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
    if "tds" in sent:
        inputs["tds"] = calc.money_str(body.tds) if body.tds is not None else None
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
    recompute(ps, user=user, period=run.period, structs=structs, adopt_latest=False, cfg=run.config, prior=prior)
    ps.updated_by = actor.id
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
    errors = await _recompute_open(session, run, [ps])
    if errors:
        raise await _blocked(session, actor, "payslip", ps.id, errors)
    now = utcnow()
    ps.status, ps.processed_at, ps.processed_by, ps.updated_by, ps.hold = "processed", now, actor.id, actor.id, False
    audit(session, action="payroll.payslip.process", actor=actor, entity_type="payslip", entity_id=ps.id)
    await pc.commit(session)
    return await admin_payslip_out(session, ps)


async def reopen_payslip(session: AsyncSession, actor: Principal, payslip_id: uuid.UUID) -> dict[str, Any]:
    run, ps = await _payslip_locked(session, payslip_id)
    await pc.forbid_self(session, actor, ps.user_id, entity_type="payslip", entity_id=ps.id)
    if run.status != "published" or ps.status != "processed":
        raise AppError("PAYROLL_LOCKED", "Only a processed payslip of a published run can be reopened")
    ps.status, ps.processed_at, ps.processed_by, ps.updated_by = "draft", None, None, actor.id
    ps.version += 1
    audit(session, action="payroll.payslip.reopen", actor=actor, entity_type="payslip", entity_id=ps.id)
    await pc.commit(session)
    return await admin_payslip_out(session, ps)


async def publish_payslip(
    session: AsyncSession, redis: Redis, actor: Principal, payslip_id: uuid.UUID, password
) -> dict[str, Any]:
    owner_id = (await session.execute(select(Payslip.user_id).where(Payslip.id == payslip_id))).scalar_one_or_none()
    if owner_id is None:
        raise AppError("NOT_FOUND", "Payslip not found")
    await pc.forbid_self(session, actor, owner_id, entity_type="payslip", entity_id=payslip_id)
    await admin_security.require_reauth(session, redis, actor, password, action="payroll.payslip.publish")
    who = _plain(actor)
    run, ps = await _payslip_locked(session, payslip_id)
    if run.status != "published" or ps.status != "processed":
        raise AppError("PAYROLL_LOCKED", "Only a processed payslip of a published run can be published")
    await _check_second_person(session, run, [ps], actor)
    await _share_lock_structures(session, [ps])
    now = utcnow()
    await _publish_one(session, ps, actor.id, now)
    audit(session, action="payroll.payslip.publish", actor=who, entity_type="payslip", entity_id=ps.id,
          after={"revision": ps.revision, "run_id": str(run.id)})  # fmt: skip
    push = [(ps.user_id, ps.id, run.period, ps.revision > 1)]
    await pc.commit(session)
    await _push_published(session, push)
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
    elif not _eligible(u, det, run.period, b.ctx.tz):
        raise AppError("VALIDATION_ERROR", "The employee is not eligible for payroll in this period",
                       details=[{"field": "user_id", "message": "not eligible"}])  # fmt: skip
    cutoff = min(pdays.month_bounds(run.period)[1], await _today(session))  # the run itself is locked: never touched
    if not pull_days(new, u, b.ctx, cutoff, run.config):
        raise AppError("VALIDATION_ERROR", "The employee was not employed in this period",
                       details=[{"field": "user_id", "message": "not employed"}])  # fmt: skip
    recompute(new, user=u, period=run.period, structs=b.structs.get(u.id, []), adopt_latest=True, cfg=run.config,
              prior=b.prior.get(u.id, []))  # fmt: skip
    new.version = 1
    session.add(new)
    await pc.flush(session)
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
    items = [
        {
            "id": str(p.id),
            "period": pc.period_str(r.period),
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
    out = employee_view(ps, run, successor_id=succ, company=company, logo_url=logo)
    audit(session, action=audit_action, actor=actor, entity_type="payslip", entity_id=ps.id)
    await pc.commit(session)
    return out


async def pdf_parts(session: AsyncSession, ps: Payslip, *, admin: bool) -> tuple[dict[str, Any], str, bytes | None]:
    """(view, footer, logo png) for the PDF renderer. `admin=True` masks PAN / UAN / ESI IP number (see employee_view)."""
    from . import company as company_svc

    run, succ, company, logo_url = await _view_parts(session, ps, admin=admin)
    view = employee_view(ps, run, successor_id=succ, company=company, logo_url=logo_url, admin=admin)
    view["pay_date"] = run.pay_date.isoformat() if run.pay_date else None
    if ps.supersedes_id:
        prev = await session.get(Payslip, ps.supersedes_id)
        view["supersedes_published_at"] = pc.iso(prev.published_at) if prev else None
    logo = await company_svc.logo_bytes(session)
    return view, str(run.config.get("payslip_footer") or ""), (logo[0] if logo else None)
