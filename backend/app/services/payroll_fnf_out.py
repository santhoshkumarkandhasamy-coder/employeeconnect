"""Full and final settlement: loading, estimate (preview), checklist, serialisation and the statement data (plan 16, contract 18.8).

Read side of `payroll_fnf`. Nothing here writes. Money is decrypted in Python only; the audit rows written by the callers carry
ids, dates and counts, never amounts.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import AppError
from ..models import (
    EmployeePayrollDetails,
    FnfSettlement,
    LeaveEncashment,
    LeaveType,
    OvertimeDay,
    PayrollArrears,
    PayrollLoan,
    PayrollLoanInstallment,
    PayrollOneTimeItem,
    PayrollRun,
    Payslip,
    ReimbursementClaim,
    SalaryIncrement,
    SalaryStructure,
    User,
)
from . import leave as leave_svc
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_ctc as ctc
from . import payroll_days as pdays
from . import payroll_encashment as enc_svc
from . import payroll_fnf_calc as fc
from . import payroll_items as items_svc
from . import payroll_loans as loans_svc
from . import payroll_runs as runs
from . import payroll_settings as pset
from . import payroll_structures as structs_svc

ZERO = Decimal("0.00")
LIVE = ("draft", "submitted", "approved")


def blank_plan() -> dict[str, Any]:
    return {"override": None, "encashments": [], "loans": [], "items": [], "move_open_items": True}


def plan_of(f: FnfSettlement) -> dict[str, Any]:
    return {**blank_plan(), **(f.plan or {})}


def _d(x: Any) -> Decimal:
    return Decimal(str(x))


# ============================================================================================ loading
async def or_404(session: AsyncSession, fnf_id: uuid.UUID, *, lock: bool = False) -> FnfSettlement:
    q = select(FnfSettlement).where(FnfSettlement.id == fnf_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    f = (await session.execute(q)).scalar_one_or_none()
    if f is None:
        raise AppError("NOT_FOUND", "Final settlement not found")
    return f


async def structures_of(session: AsyncSession, user_id: uuid.UUID) -> list[SalaryStructure]:
    q = select(SalaryStructure).where(SalaryStructure.user_id == user_id).order_by(SalaryStructure.effective_from)
    return list((await session.execute(q)).scalars())


def _pairs(structs: list[SalaryStructure]) -> list[tuple[dt.date, SalaryStructure]]:
    return [(s.effective_from, s) for s in structs]


async def settings_of(session: AsyncSession) -> dict[str, Any]:
    return (await pset.read_all(session))["settings"]


# ============================================================================================ notice
async def notice_money(
    session: AsyncSession, f: FnfSettlement, structs: list[SalaryStructure], settings: dict[str, Any]
) -> dict[str, Any]:
    """basis / divisor / daily rate / amount of the notice recovery: the approval snapshot once approved, else a quote."""
    if f.status in ("approved", "settled") and f.summary and f.summary.get("notice"):
        return {**f.summary["notice"], "missing": False}
    basis = str(settings["payroll.notice_recovery_basis"])
    divisor = int(settings["payroll.notice_recovery_divisor"])
    s = calc.structure_for(_pairs(structs), fc.month_of(f.last_working_day))
    if s is None:
        return {"basis": basis, "divisor": divisor, "daily_rate": "0.00", "amount": "0.00", "missing": True}
    data = structs_svc.data_of(s)
    rate = fc.daily_rate(data.monthly_gross if basis == "gross" else data.basic, divisor)
    amount = fc.notice_amount(f.shortfall_days, rate)
    return {
        "basis": basis,
        "divisor": divisor,
        "daily_rate": calc.money_str(rate),
        "amount": calc.money_str(amount),
        "missing": False,
    }


def notice_checked(money: dict[str, Any]) -> Decimal:
    """The amount with the money ceiling of the calculation engine (a huge basic must not overflow a payslip)."""
    amount = _d(money["amount"])
    if amount > calc.MAX_MONEY:
        raise AppError("VALIDATION_ERROR", "the notice amount would exceed the maximum allowed",
                       details=[{"field": "notice", "message": "the amount would exceed the maximum allowed"}])  # fmt: skip
    return amount


# ============================================================================================ encashment quotes
async def encashment_quote(
    session: AsyncSession, f: FnfSettlement, user: User, line: dict[str, Any], *, lock: bool = False
) -> dict[str, Any]:
    """Quote for one plan line (leave type, year, days) for the settlement month. Raises NOT_ENCASHABLE / INSUFFICIENT_BALANCE /
    STRUCTURE_MISSING exactly like 17.5. `lock` takes the balance row FOR UPDATE (approve)."""
    lt = await enc_svc._leave_type(session, uuid.UUID(str(line["leave_type_id"])))
    year = int(line["year"])
    days = leave_svc.d1(line["days"])
    rate, _basis, _div = await enc_svc._rate_for(session, user.id, f.settlement_period)
    amount = enc_svc.checked_amount(days, rate)
    bal = await leave_svc.get_balance(session, user, lt, year, lock=lock)
    available = await enc_svc._available(session, user.id, lt, year, bal, bal.quota)
    if days > available:
        raise enc_svc._insufficient(available)
    return {"lt": lt, "year": year, "days": days, "available": available, "rate": rate, "amount": amount, "balance": bal}


# ============================================================================================ open items
OPEN_MODELS = (
    ("one_time", PayrollOneTimeItem, "pending"),
    ("encashment", LeaveEncashment, "pending"),
    ("arrears", PayrollArrears, "pending"),
    ("reimbursement", ReimbursementClaim, "approved"),
)


async def open_items(
    session: AsyncSession, f: FnfSettlement, *, lock: bool = False, kinds: tuple[str, ...] | None = None
) -> list[tuple[str, Any, list[str]]]:  # fmt: skip
    """Payable rows of the employee in a month other than the settlement month that will never be paid by a normal run:
    their month is after the LWD month, or it is published and the employee has no open payslip in it (PERIOD_CLOSED)."""
    out: list[tuple[str, Any, list[str]]] = []
    lwd_month = fc.month_of(f.last_working_day)
    for kind, model, payable in OPEN_MODELS:
        if kinds is not None and kind not in kinds:
            continue
        q = select(model).where(model.user_id == f.user_id, model.status == payable, model.period.is_not(None),
                                model.period != f.settlement_period)  # fmt: skip
        if kind in ("one_time", "encashment"):
            q = q.where(model.fnf_id.is_(None))  # rows an F&F created are its own
        q = q.order_by(model.id)
        if lock:
            q = q.with_for_update().execution_options(populate_existing=True)
        rows = list((await session.execute(q)).scalars())
        if not rows:
            continue
        states = await comps.row_states(session, rows, payable)
        for r in rows:
            closed = "PERIOD_CLOSED" in states[r.id][1]
            if r.period > lwd_month or closed:
                out.append((kind, r, ["PERIOD_CLOSED"] if closed else []))
    return out


# ============================================================================================ estimate
async def estimate(
    session: AsyncSession, f: FnfSettlement, user: User, det: EmployeePayrollDetails | None, extra: list[dict[str, Any]],
    today: dt.date, *, run: PayrollRun | None = None, payslip: Payslip | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Estimate of the payslip of the settlement month: the structure in force, LOP so far, the pending pay items that would be
    pulled, plus `extra` planned lines. Writes nothing (the rows read are not locked)."""
    period = f.settlement_period
    cfg = run.config if run is not None else await pset.snapshot(session)
    ctx = await pdays.load_context(session, period, [user.id])
    cutoff = min(pdays.month_bounds(period)[1], today)
    days = pdays.compute_days(user, ctx, cutoff, cfg, f.last_working_day)
    settlement_only = days is None
    if days is None:
        days = pdays.settlement_only_days(ctx, cfg)
    base = runs._blank_inputs()
    if payslip is not None:  # the open payslip's manual inputs (TDS, PT override, adjustments) also shape the net (review L4)
        base.update({k: copy.deepcopy((payslip.inputs or {}).get(k, base[k])) for k in ("tds", "pt_override", "adjustments")})
    ps = SimpleNamespace(id=None, user_id=user.id, supersedes_id=None, inputs=base)
    await comps.pull_for(session, [ps], period, lock=False)  # type: ignore[list-item]
    inputs = copy.deepcopy(ps.inputs)
    inputs["components"] = [*inputs.get("components", []), *extra]
    ps.inputs = inputs
    structs = (await runs._load_structs(session, [user.id], period)).get(user.id, [])
    prior = (await runs._prior_gross(session, [user.id], period)).get(user.id, [])
    chosen = calc.structure_for(structs, period)
    warnings = list(inputs.get("component_warnings", []))
    if settlement_only:
        warnings.append("FNF_SETTLEMENT_ONLY")
    if chosen is None:
        return {"error": "STRUCTURE_MISSING", "days": days, "warnings": warnings, "result": None, "source": "structure"}
    data = structs_svc.data_of(chosen)
    runs._finalize_overtime(ps, user, data.basic, cfg, warnings)  # type: ignore[arg-type]
    rules = calc.Rules.from_config(cfg)
    eligible = calc.esi_eligible(
        [(e, structs_svc.data_of(s)) for e, s in structs], period, user.join_date, rules.esi_wage_threshold
    )
    paid = max(Decimal(0), Decimal(str(days.employed_days)) - Decimal(str(days.lop_auto)))
    res = calc.calculate(
        data, rules, period, Decimal(str(days.basis_days)), paid, runs._pay_inputs(ps.inputs),
        esi_is_eligible=eligible, prior_gross=prior,
    )  # fmt: skip
    return {
        "error": None,
        "days": days,
        "paid_days": paid,
        "warnings": warnings,
        "result": res,
        "source": "structure",
        "tds": data.tds_monthly,
    }


async def planned_lines(
    session: AsyncSession, f: FnfSettlement, user: User, structs: list[SalaryStructure], settings: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The lines an approval would add (not yet rows): notice recovery, encashments, foreclosures, other items. Money is not
    ceiling-checked here (the approve does that)."""
    plan = plan_of(f)
    lines: list[dict[str, Any]] = []
    flags: list[str] = []
    money = await notice_money(session, f, structs, settings)
    if f.shortfall_days > 0 and not money["missing"]:
        lines.append(
            {
                "code": "NOTICE_RECOVERY",
                "label": f"Notice period shortfall ({f.shortfall_days} d)",
                "amount": money["amount"],
                "sources": [],
            }
        )
    elif f.shortfall_days > 0:
        flags.append("STRUCTURE_MISSING")
    for e in plan["encashments"]:
        try:
            q = await encashment_quote(session, f, user, e)
        except AppError:
            flags.append("ENCASHMENT_INVALID")
            continue
        lines.append(
            {
                "code": "LEAVE_ENCASH",
                "label": f"Leave encashment ({leave_svc.num(q['days'])} d)",
                "amount": calc.money_str(q["amount"]),
                "sources": [],
            }
        )
    modes = {str(x["loan_id"]): x for x in plan["loans"]}
    if modes:
        loans = list(
            (
                await session.execute(
                    select(PayrollLoan).where(PayrollLoan.id.in_([uuid.UUID(k) for k in modes]), PayrollLoan.user_id == f.user_id)
                )
            ).scalars()
        )
        by_loan = await loans_svc._insts_by_loan(session, [lo.id for lo in loans])
        for lo in loans:
            if modes[str(lo.id)]["mode"] == "payroll" and lo.status == "active":
                out = loans_svc._outstanding(lo, by_loan[lo.id])
                if out > 0:
                    word = "Advance" if lo.kind == "advance" else "Loan"
                    lines.append(
                        {
                            "code": "ADVANCE_RECOVERY" if lo.kind == "advance" else "LOAN_EMI",
                            "label": f"{word} {lo.reference} foreclosure",
                            "amount": calc.money_str(out),
                            "sources": [],
                        }
                    )
    for it in plan["items"]:
        lines.append({"code": it["code"], "label": it["label"], "amount": it["amount"], "sources": []})
    return lines, flags


async def live_payslip(session: AsyncSession, f: FnfSettlement) -> Payslip | None:
    """The employee's open payslip of the settlement month, else the published one."""
    q = (
        select(Payslip)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(Payslip.user_id == f.user_id, PayrollRun.period == f.settlement_period, Payslip.status.in_(("draft", "processed", "published")))
        .order_by(Payslip.revision.desc())
    )  # fmt: skip
    return (await session.execute(q)).scalars().first()


async def payslip_estimate(session: AsyncSession, f: FnfSettlement) -> dict[str, Any] | None:
    """Estimate shim from the stored payslip (source `payslip`): None when there is none or it is not calculated."""
    p = await live_payslip(session, f)
    if p is None:
        return None
    r = p.result
    res = (
        SimpleNamespace(net=_d(r["net"]), gross=_d(r["gross"]), total_deductions=_d(r["total_deductions"]),
                        earnings=r["earnings"], deductions=r["deductions"])
        if r else None
    )  # fmt: skip
    days = SimpleNamespace(basis_days=p.basis_days, employed_days=p.employed_days, lop_auto=runs._lop_days(p))
    return {"error": None if r else "NOT_CALCULATED", "days": days, "paid_days": p.paid_days, "warnings": list(p.warnings or []),
            "result": res, "source": "payslip", "payslip": p}  # fmt: skip


def _line(x: dict[str, Any]) -> dict[str, Any]:
    return {"code": x["code"], "label": x["label"], "amount": x["amount"]}


def estimate_view(est: dict[str, Any], source: str = "structure") -> dict[str, Any]:
    res, days = est["result"], est["days"]
    lop = float(days.lop_auto)
    return {
        "estimate": True,
        "days": {"basis_days": float(days.basis_days), "employed_days": float(days.employed_days), "lop_days": lop,
                 "paid_days": float(est.get("paid_days", 0))},
        "earnings": [_line(x) for x in res.earnings] if res else [],
        "deductions": [_line(x) for x in res.deductions] if res else [],
        "totals": {"gross_earnings": calc.money_str(res.gross), "total_deductions": calc.money_str(res.total_deductions),
                   "net_pay": calc.money_str(res.net)} if res else None,
        "warnings": sorted(set(est["warnings"])),
        "source": source,
    }  # fmt: skip


# ============================================================================================ checklist
async def checklist(
    session: AsyncSession, f: FnfSettlement, user: User, det: EmployeePayrollDetails | None, today: dt.date,
    est: dict[str, Any] | None, plan_flags: list[str], *, light: bool = False,
) -> list[str]:  # fmt: skip
    out = set(plan_flags)
    uid = f.user_id
    plan = plan_of(f)

    async def any_row(model: Any, *cond: Any) -> bool:
        return (await session.execute(select(model.id).where(model.user_id == uid, *cond).limit(1))).first() is not None

    if await any_row(PayrollArrears, PayrollArrears.status == "proposed"):
        out.add("ARREARS_PROPOSED")
    if await any_row(ReimbursementClaim, ReimbursementClaim.status.in_(("submitted", "verified"))):
        out.add("CLAIMS_OPEN")
    if await any_row(OvertimeDay, OvertimeDay.status == "computed"):
        out.add("OVERTIME_UNREVIEWED")
    if await any_row(SalaryIncrement, SalaryIncrement.status.in_(("draft", "submitted"))):
        out.add("INCREMENT_PENDING")
    if user.join_date is None:
        out.add("NO_JOIN_DATE")
    if user.status == "active" and f.last_working_day < today:
        out.add("ACCOUNT_STILL_ACTIVE")
    if plan["encashments"] or any(i["code"] in ("BONUS", "INCENTIVE", "OTHER_EARNING") for i in plan["items"]):
        out.add("TDS_REVIEW")
    if est is not None:
        out.update(w for w in est["warnings"] if w in ("STRUCTURE_MISSING", "OVERTIME_UNREVIEWED"))
        if est["result"] is not None:
            if est["result"].net < 0:
                out.add("NEGATIVE_NET_ESTIMATE")
            if _d(est.get("tds", 0)) > 0:
                out.add("TDS_REVIEW")
        elif est["error"]:
            out.add(est["error"])
    if light:
        return sorted(out)
    # PAID_AFTER_EXIT: pay already processed / published for months the employee had left, or too many days in the LWD month
    lwd_month = fc.month_of(f.last_working_day)
    q = (
        select(Payslip, PayrollRun.period)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(Payslip.user_id == uid, Payslip.status.in_(("processed", "published")), PayrollRun.period >= lwd_month)
    )  # fmt: skip
    for p, period in (await session.execute(q)).all():
        if period > lwd_month and period != f.settlement_period:
            out.add("PAID_AFTER_EXIT")
        elif period == lwd_month and not (p.inputs or {}).get("settlement_only"):
            ctx = await pdays.load_context(session, period, [uid])
            cfg = (await runs._run_or_404(session, p.run_id)).config
            allowed = pdays.compute_days(user, ctx, pdays.month_bounds(period)[1], cfg, f.last_working_day)
            if allowed is not None and _d(p.employed_days) > Decimal(str(allowed.employed_days)):
                out.add("PAID_AFTER_EXIT")
    return sorted(out)


# ============================================================================================ gratuity (information only)
def gratuity_info(
    f: FnfSettlement, user: User, structs: list[SalaryStructure], settings: dict[str, Any]
) -> dict[str, Any] | None:  # fmt: skip
    if user.join_date is None:
        return None
    s = calc.structure_for(_pairs(structs), f.last_working_day)
    if s is None:
        return None
    basis = str(settings["payroll.gratuity_wage_basis"])
    data = structs_svc.data_of(s)
    wage = ctc.gratuity_wage(data.basic, data.monthly_gross, basis)
    g = ctc.gratuity(
        wage, user.join_date, f.last_working_day, min_years=int(settings["payroll.gratuity_min_years"]),
        cap=calc.parse_money(settings["payroll.gratuity_cap"]), death=f.exit_reason == "death",
    )  # fmt: skip
    return {
        "eligible": g.eligible, "service": {"years": g.years, "months": g.months, "days": g.days}, "counted_years": g.counted,
        "wage": calc.money_str(wage), "amount": calc.money_str(g.accrued), "capped": g.capped, "basis": basis,
        "note": "For information. Gratuity is paid separately, outside payroll.",
    }  # fmt: skip


# ============================================================================================ serialisation
async def _created_status(session: AsyncSession, f: FnfSettlement) -> dict[str, tuple[str, uuid.UUID | None]]:
    """id (text) -> (status, row id) for the rows an approval created."""
    created = (f.summary or {}).get("created") or {}
    ids: list[uuid.UUID] = []
    for key in ("notice", "items", "encashments"):
        v = created.get(key)
        ids += [uuid.UUID(x) for x in ([v] if isinstance(v, str) else v or [])]
    ids += [uuid.UUID(x) for x in (created.get("installments") or {}).values()]
    out: dict[str, tuple[str, uuid.UUID | None]] = {}
    if not ids:
        return out
    for model in (PayrollOneTimeItem, LeaveEncashment, PayrollLoanInstallment):
        for row in (await session.execute(select(model).where(model.id.in_(ids)))).scalars():
            out[str(row.id)] = (row.status, row.id)
    return out


async def full_out(
    session: AsyncSession, f: FnfSettlement, *, today: dt.date | None = None, light: bool = False
) -> dict[str, Any]:
    today = today or await runs._today(session)
    user = await session.get(User, f.user_id)
    det = await session.get(EmployeePayrollDetails, f.user_id)
    structs = await structs_for_out(session, f)
    settings = await settings_of(session)
    plan = plan_of(f)
    approved = f.status in ("approved", "settled")
    summary = f.summary or {}
    created = summary.get("created") or {}
    status_of = await _created_status(session, f) if approved else {}
    run = (await session.execute(select(PayrollRun).where(PayrollRun.period == f.settlement_period))).scalar_one_or_none()

    n = fc.notice_for(
        f.exit_reason, f.resignation_date, f.last_working_day, f.notice_required_days, waived=f.notice_waived,
        override_days=(plan["override"] or {}).get("days"), override_reason=(plan["override"] or {}).get("reason"),
    )  # fmt: skip
    money = await notice_money(session, f, structs, settings)
    notice_id = created.get("notice")
    notice = {
        "required_days": n.required, "served_days": n.served, "shortfall_days": f.shortfall_days,
        "computed_shortfall_days": n.computed, "waived": f.notice_waived,
        "override": {"days": n.override_days, "reason": n.override_reason} if plan["override"] else None,
        "basis": money["basis"], "divisor": money["divisor"], "daily_rate": money["daily_rate"], "amount": money["amount"],
        "item_id": notice_id, "status": status_of.get(notice_id, (None,))[0] if notice_id else None,
    }  # fmt: skip

    flags: list[str] = []
    # ---- encashments
    enc_out: list[dict[str, Any]] = []
    for i, e in enumerate(plan["encashments"]):
        lt = await session.get(LeaveType, uuid.UUID(str(e["leave_type_id"])))
        row = {"leave_type": {"id": str(lt.id), "code": lt.code, "name": lt.name} if lt else None, "year": int(e["year"]),
               "days": leave_svc.num(e["days"]), "available_days": None, "daily_rate": "0.00", "amount": "0.00",
               "encashment_id": None, "status": None}  # fmt: skip
        if approved and i < len(summary.get("encashments", [])):
            snap = summary["encashments"][i]
            row.update(daily_rate=snap["rate"], amount=snap["amount"], available_days=snap.get("available"))
            eid = (created.get("encashments") or [None] * (i + 1))[i]
            row["encashment_id"], row["status"] = eid, status_of.get(eid, (None,))[0] if eid else None
        elif lt is not None:
            try:
                q = await encashment_quote(session, f, user, e)
                row.update(
                    available_days=leave_svc.num(q["available"]),
                    daily_rate=calc.money_str(q["rate"]),
                    amount=calc.money_str(q["amount"]),
                )
            except AppError as exc:
                flags.append("ENCASHMENT_INVALID")
                if exc.code == "INSUFFICIENT_BALANCE":
                    row["available_days"] = float(exc.details[0]["message"]) if exc.details else None
        enc_out.append(row)

    # ---- loans: every active loan of the employee (mode null until chosen) plus the ones in the plan
    modes = {str(x["loan_id"]): x for x in plan["loans"]}
    lq = select(PayrollLoan).where(PayrollLoan.user_id == f.user_id)
    all_loans = list((await session.execute(lq.order_by(PayrollLoan.created_at, PayrollLoan.id))).scalars())
    show = [lo for lo in all_loans if lo.status == "active" or str(lo.id) in modes]
    by_loan = await loans_svc._insts_by_loan(session, [lo.id for lo in show])
    loan_out: list[dict[str, Any]] = []
    snaps = {s["loan_id"]: s for s in summary.get("loans", [])}
    for lo in show:
        m = modes.get(str(lo.id))
        outstanding = (
            snaps[str(lo.id)]["outstanding"]
            if approved and str(lo.id) in snaps
            else calc.money_str(loans_svc._outstanding(lo, by_loan[lo.id]))
        )
        inst_id = (created.get("installments") or {}).get(str(lo.id))
        status = None
        if approved and m:
            status = status_of.get(inst_id, (None,))[0] if inst_id else ("closed" if lo.status == "closed" else None)
        loan_out.append({
            "loan": {"id": str(lo.id), "reference": lo.reference, "kind": lo.kind}, "outstanding": outstanding,
            "mode": m["mode"] if m else None, "paid_on": m.get("paid_on") if m else None, "reference": m.get("reference") if m else None,
            "installment_id": inst_id, "status": status,
        })  # fmt: skip

    # ---- items
    item_ids = created.get("items") or []
    items_out = []
    for i, it in enumerate(plan["items"]):
        iid = item_ids[i] if i < len(item_ids) else None
        items_out.append({"code": it["code"], "label": it["label"], "amount": it["amount"], "item_id": iid,
                          "status": status_of.get(iid, (None,))[0] if iid else None})  # fmt: skip

    # ---- open items
    if approved:
        open_out = summary.get("open_items", [])
    else:
        open_out = [
            {"type": k, "id": str(r.id), "period": pc.period_str(r.period), "code": getattr(r, "code", None),
             "status": r.status, "flags": fl, "action": "move" if plan["move_open_items"] else "keep"}
            for k, r, fl in await open_items(session, f)
        ]  # fmt: skip

    # ---- estimate / warnings
    est = None
    plan_flags: list[str] = list(dict.fromkeys(flags))
    if f.status in ("draft", "submitted"):
        extra, pf = await planned_lines(session, f, user, structs, settings)
        plan_flags += pf
        est = await estimate(session, f, user, det, extra, today, run=run)
    elif f.status == "approved":
        est = await payslip_estimate(session, f)
    warnings = await checklist(session, f, user, det, today, est, plan_flags, light=light)
    if money["missing"] and f.shortfall_days > 0:
        warnings = sorted({*warnings, "STRUCTURE_MISSING"})

    people = await items_svc._people(session, {f.user_id, f.created_by, f.submitted_by, f.decided_by})
    fp = await session.get(Payslip, f.final_payslip_id) if f.final_payslip_id else None
    return {
        "id": str(f.id), "user": pc.user_out(people.get(f.user_id)), "status": f.status,
        "resignation_date": f.resignation_date.isoformat() if f.resignation_date else None,
        "last_working_day": f.last_working_day.isoformat(), "exit_reason": f.exit_reason,
        "settlement_period": pc.period_str(f.settlement_period), "run_status": run.status if run else "none",
        "notice": notice, "encashments": enc_out, "loans": loan_out, "items": items_out,
        "move_open_items": bool(plan["move_open_items"]), "open_items": open_out,
        "gratuity": gratuity_info(f, user, structs, settings), "warnings": warnings,
        "summary": (
            {k: summary[k] for k in ("earnings", "deductions", "net_estimate", "computed_at")} if approved and summary else None
        ),
        "final_payslip": {"id": str(fp.id), "revision": fp.revision, "status": fp.status} if fp else None,
        "remarks": f.remarks, "created_by": pc.user_out(people.get(f.created_by)), "created_at": pc.iso(f.created_at),
        "submitted_by": pc.user_out(people.get(f.submitted_by)), "submitted_at": pc.iso(f.submitted_at),
        "decided_by": pc.user_out(people.get(f.decided_by)), "decided_at": pc.iso(f.decided_at),
        "decision_comment": f.decision_comment, "version": f.version,
    }  # fmt: skip


async def structs_for_out(session: AsyncSession, f: FnfSettlement) -> list[SalaryStructure]:
    return await structures_of(session, f.user_id)


def list_item(f: FnfSettlement, user: User | None, run_status: str, warnings: list[str]) -> dict[str, Any]:
    return {
        "id": str(f.id), "user": pc.user_out(user), "status": f.status, "last_working_day": f.last_working_day.isoformat(),
        "settlement_period": pc.period_str(f.settlement_period), "exit_reason": f.exit_reason, "run_status": run_status,
        "warnings": warnings, "created_at": pc.iso(f.created_at),
    }  # fmt: skip
