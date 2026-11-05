"""Payroll dashboard, month-over-month variance and extra reports (plan 14 phase 1). Read-only.

Money is encrypted at rest (plan 12 D2), so everything is computed in Python from the decrypted payslip results of ONE
published-or-open copy per employee (`runs._live`). Output money is 2-decimal strings; xlsx amounts are numeric cells.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import AppError
from ..models import EmployeePayrollDetails, FnfSettlement, PayrollLoan, PayrollRun, Payslip, User
from . import payroll_calc as calc
from . import payroll_runs as runs
from . import payroll_settings, xlsx

MAX_ROWS = 5000
TREND_MONTHS = 12
REPORT_KINDS = ("department", "headcount", "ctc", "lop", "bank_advice")
ZERO = Decimal("0.00")


def _d(x: Any) -> Decimal:
    return Decimal(str(x)) if x is not None else ZERO


def _s(x: Decimal) -> str:
    return calc.money_str(x)


def _num(x: Decimal) -> float:
    return float(x)


def _month_back(period: dt.date, n: int) -> dt.date:
    idx = period.year * 12 + (period.month - 1) - n
    return dt.date(idx // 12, idx % 12 + 1, 1)


def _employer_cost(r: dict) -> Decimal:
    ded = {d["code"]: _d(d["amount"]) for d in r["deductions"]}
    return (
        _d(r["gross"])
        + calc.employer_amount(r, "PF_ER")
        + calc.employer_amount(r, "ESI_ER")
        - ded.get("PF_ER_REC", ZERO)
        - ded.get("ESI_ER_REC", ZERO)
    )


def _lop(p: Payslip) -> Decimal:
    return Decimal(str(p.lop_override if p.lop_override is not None else p.lop_auto))


async def _run_by_period(session: AsyncSession, period: dt.date) -> PayrollRun | None:
    return (await session.execute(select(PayrollRun).where(PayrollRun.period == period))).scalar_one_or_none()


async def _live_rows(session: AsyncSession, run: PayrollRun | None) -> list[Payslip]:
    if run is None:
        return []
    return [p for p in runs._live(await runs._run_payslips(session, run.id)) if p.result is not None]


def _period(raw: str | None, today: dt.date) -> dt.date:
    return today.replace(day=1) if not raw else runs.parse_period(raw, today)


def _totals(rows: list[Payslip]) -> dict[str, Decimal]:
    return {
        "gross": sum((_d(p.result["gross"]) for p in rows), ZERO),
        "deductions": sum((_d(p.result["total_deductions"]) for p in rows), ZERO),
        "net": sum((_d(p.result["net"]) for p in rows), ZERO),
        "employer_cost": sum((_employer_cost(p.result) for p in rows), ZERO),
    }


def _dept(p: Payslip) -> str:
    return (p.employee_snapshot or {}).get("department") or "Unassigned"


# ================================================================================================== calendar
async def calendar(session: AsyncSession, period: dt.date, run: PayrollRun | None, today: dt.date) -> dict[str, Any]:
    cfg = await payroll_settings.read_all(session)
    s = cfg["settings"]
    process_day, pay_day = int(s["payroll.process_day"]), int(s["payroll.pay_day"])
    nxt = _month_back(period, -1)  # pay day falls in the month after the period
    pay_date = run.pay_date if run is not None and run.pay_date else dt.date(nxt.year, nxt.month, min(pay_day, 28))
    process_by = dt.date(period.year, period.month, min(process_day, 28))  # process day lies in the period month
    alerts: list[str] = []
    done = run is not None and run.status in ("processed", "published")
    if today > process_by and not done:
        alerts.append("RUN_NOT_CREATED" if run is None else "RUN_NOT_PROCESSED")
    if run is not None and run.status == "processed" and today >= pay_date:
        alerts.append("RUN_NOT_PUBLISHED")
    alerts.extend(await _fnf_alerts(session, today))
    return {
        "cutoff_day": int(s["payroll.cutoff_day"]),
        "process_by": process_by.isoformat(),
        "pay_date": pay_date.isoformat(),
        "alerts": alerts,
    }


# ================================================================================================== dashboard
EXIT_ALERT_DAYS = 90  # a deactivation older than this without an F&F is history, not an alert


async def _leavers(session: AsyncSession, period: dt.date, end: dt.date) -> list[dict[str, Any]]:
    """Leavers of the month: the last working day of an F&F when one is set (plan 16 F12), else the deactivation date."""
    out: list[tuple[dt.date, User]] = []
    q = (
        select(User, EmployeePayrollDetails.last_working_day)
        .join(EmployeePayrollDetails, EmployeePayrollDetails.user_id == User.id)
        .where(EmployeePayrollDetails.last_working_day >= period, EmployeePayrollDetails.last_working_day < end)
    )  # fmt: skip
    seen: set[Any] = set()
    for u, lwd in (await session.execute(q)).unique().all():
        out.append((lwd, u))
        seen.add(u.id)
    lo, hi = dt.datetime.combine(period, dt.time.min, tzinfo=dt.UTC), dt.datetime.combine(end, dt.time.min, tzinfo=dt.UTC)
    q2 = select(User).where(User.deactivated_at >= lo, User.deactivated_at < hi)
    for u in (await session.execute(q2)).unique().scalars():
        if u.id in seen:
            continue
        det = await session.get(EmployeePayrollDetails, u.id)
        if det is not None and det.last_working_day is not None:
            continue  # the F&F decides: its last working day is in another month
        out.append((u.deactivated_at.date(), u))
    out.sort(key=lambda t: (t[0], t[1].employee_id))
    return [{"employee_id": u.employee_id, "name": u.full_name, "date": d.isoformat()} for d, u in out]


async def _fnf_alerts(session: AsyncSession, today: dt.date) -> list[str]:
    """FNF_PENDING_APPROVAL: a settlement waits for a checker. EXIT_WITHOUT_FNF: an employee who left (deactivated, or last working
    day passed) has an active loan, or was deactivated recently and has no settlement at all."""
    alerts: list[str] = []
    if (await session.execute(select(FnfSettlement.id).where(FnfSettlement.status == "submitted").limit(1))).first():
        alerts.append("FNF_PENDING_APPROVAL")
    live_fnf = select(FnfSettlement.id).where(
        FnfSettlement.user_id == User.id, FnfSettlement.status.in_(("draft", "submitted", "approved", "settled"))
    )
    since = dt.datetime.combine(today - dt.timedelta(days=EXIT_ALERT_DAYS), dt.time.min, tzinfo=dt.UTC)
    exited = (User.deactivated_at.is_not(None)) | (
        select(EmployeePayrollDetails.user_id)
        .where(EmployeePayrollDetails.user_id == User.id, EmployeePayrollDetails.last_working_day < today)
        .exists()
    )
    no_fnf = select(User.id).where(User.deleted_at.is_(None), User.deactivated_at >= since, ~live_fnf.exists())
    active_loan = select(User.id).where(
        User.deleted_at.is_(None), exited,
        select(PayrollLoan.id).where(PayrollLoan.user_id == User.id, PayrollLoan.status == "active").exists(),
    )  # fmt: skip
    if (await session.execute(no_fnf.limit(1))).first() or (await session.execute(active_loan.limit(1))).first():
        alerts.append("EXIT_WITHOUT_FNF")
    return alerts


async def dashboard(session: AsyncSession, raw_period: str | None) -> dict[str, Any]:
    today = await runs._today(session)
    period = _period(raw_period, today)
    run = await _run_by_period(session, period)
    rows = await _live_rows(session, run)
    tot = _totals(rows)

    by_dept: dict[str, dict[str, Any]] = defaultdict(lambda: {"employees": 0, "gross": ZERO, "net": ZERO, "employer_cost": ZERO})
    for p in rows:
        d = by_dept[_dept(p)]
        d["employees"] += 1
        d["gross"] += _d(p.result["gross"])
        d["net"] += _d(p.result["net"])
        d["employer_cost"] += _employer_cost(p.result)

    trend = []
    for i in range(TREND_MONTHS - 1, -1, -1):
        m = _month_back(period, i)
        r = await _run_by_period(session, m)
        rr = await _live_rows(session, r) if r is not None and r.status != "draft" else []
        t = _totals(rr)
        trend.append({"period": f"{m:%Y-%m}", "employees": len(rr), "gross": _s(t["gross"]), "net": _s(t["net"]),
                      "employer_cost": _s(t["employer_cost"])})  # fmt: skip

    end = _month_back(period, -1)
    joiners = [
        {"employee_id": u.employee_id, "name": u.full_name, "date": u.join_date.isoformat()}
        for u in (await session.execute(select(User).where(User.join_date >= period, User.join_date < end).order_by(User.join_date)))
        .unique().scalars()
    ]  # fmt: skip
    leavers = await _leavers(session, period, end)

    return {
        "period": f"{period:%Y-%m}",
        "run": None if run is None else {"id": str(run.id), "status": run.status},
        "headcount": len(rows),
        "held": sum(1 for p in rows if p.hold),
        "errors": sum(1 for p in rows if set(p.warnings or []) & calc.ERROR_CODES),
        "warnings": sum(1 for p in rows if set(p.warnings or []) - calc.ERROR_CODES),
        "totals": {k: _s(v) for k, v in tot.items()},
        "departments": [
            {"department": k, "employees": v["employees"], "gross": _s(v["gross"]), "net": _s(v["net"]),
             "employer_cost": _s(v["employer_cost"])}
            for k, v in sorted(by_dept.items())
        ],  # fmt: skip
        "trend": trend,
        "joiners": joiners,
        "leavers": leavers,
        "calendar": await calendar(session, period, run, today),
    }


# ================================================================================================== variance
def _component_signature(ps: Any) -> list[tuple[str, str]]:
    """System lines (plan 15 `inputs.components`) of a payslip as comparable (code, amount) pairs."""
    return sorted((x["code"], x["amount"]) for x in (ps.inputs or {}).get("components", []))


async def variance(session: AsyncSession, raw_period: str | None) -> dict[str, Any]:
    today = await runs._today(session)
    period = _period(raw_period, today)
    prev_period = _month_back(period, 1)
    cur = {p.user_id: p for p in await _live_rows(session, await _run_by_period(session, period))}
    prev = {p.user_id: p for p in await _live_rows(session, await _run_by_period(session, prev_period))}
    ids = list(cur.keys() | prev.keys())
    users = {
        u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).unique().scalars()
    } if ids else {}  # fmt: skip
    items: list[dict[str, Any]] = []
    for uid in ids:
        c, p = cur.get(uid), prev.get(uid)
        u = users.get(uid)
        reasons: list[str] = []
        if c is not None and p is None:
            reasons.append("NEW")
        elif c is None:
            reasons.append("LEFT_OR_NOT_PAID")
        else:
            if c.structure_id != p.structure_id:
                reasons.append("STRUCTURE_CHANGE")
            if _lop(c) != _lop(p):
                reasons.append("LOP_CHANGE")
            if _component_signature(c) != _component_signature(p):
                reasons.append("COMPONENTS")
            codes_c = {e["code"] for e in c.result["earnings"] if e["code"] in calc.EARNING_ADJUSTMENTS}
            if codes_c:
                reasons.append("ADJUSTMENTS")
            if {d["code"]: d["amount"] for d in c.result["deductions"]} .get("PT") != {d["code"]: d["amount"] for d in p.result["deductions"]}.get("PT"):
                reasons.append("PT_CHANGE")
        g1, g0 = (_d(c.result["gross"]) if c else ZERO), (_d(p.result["gross"]) if p else ZERO)
        n1, n0 = (_d(c.result["net"]) if c else ZERO), (_d(p.result["net"]) if p else ZERO)
        if n1 == n0 and not reasons:
            continue
        snap = ((c or p).employee_snapshot) or {}
        items.append(
            {
                "employee_id": u.employee_id if u else snap.get("employee_id"),
                "name": snap.get("full_name") or (u.full_name if u else None),
                "department": snap.get("department"),
                "gross_prev": _s(g0), "gross": _s(g1), "net_prev": _s(n0), "net": _s(n1), "net_delta": _s(n1 - n0),
                "lop_prev": str(_lop(p)) if p else None, "lop": str(_lop(c)) if c else None,
                "reasons": reasons,
            }
        )  # fmt: skip
    items.sort(key=lambda i: (abs(Decimal(i["net_delta"])) * -1, i["employee_id"] or ""))
    return {"period": f"{period:%Y-%m}", "previous": f"{prev_period:%Y-%m}", "items": items}


# ================================================================================================== xlsx reports
async def _one_sheet(name: str, headers: list[str], table: list[tuple]) -> xlsx.BuiltWorkbook:
    async def rows() -> AsyncIterator[tuple]:
        for r in table:
            yield r

    async def count() -> int:
        return len(table)

    return await xlsx.build_workbook([xlsx.SheetSpec(name, headers, count, rows, MAX_ROWS)])


async def report(session: AsyncSession, kind: str, raw_period: str | None) -> tuple[xlsx.BuiltWorkbook, str, int]:
    if kind not in REPORT_KINDS:
        raise AppError("NOT_FOUND", "Unknown report")
    today = await runs._today(session)
    period = _period(raw_period, today)
    run = await _run_by_period(session, period)
    if run is None:
        raise AppError("NOT_FOUND", "No payroll run for that month")
    rows = await _live_rows(session, run)
    users = {
        u.id: u for u in (await session.execute(select(User).where(User.id.in_([p.user_id for p in rows])))).unique().scalars()
    } if rows else {}  # fmt: skip
    rows.sort(key=lambda p: ((users[p.user_id].full_name or "").lower(), users[p.user_id].employee_id))

    table: list[tuple]
    if kind == "department":
        agg: dict[str, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO, ZERO, ZERO])
        cnt: dict[str, int] = defaultdict(int)
        for p in rows:
            a = agg[_dept(p)]
            a[0] += _d(p.result["gross"]); a[1] += _d(p.result["total_deductions"]); a[2] += _d(p.result["net"]); a[3] += _employer_cost(p.result)  # noqa: E702
            cnt[_dept(p)] += 1
        table = [(k, cnt[k], *[_num(x) for x in v]) for k, v in sorted(agg.items())]
        built = await _one_sheet("Departments", ["Department", "Employees", "Gross", "Deductions", "Net Pay", "Employer Cost"], table)
    elif kind == "headcount":
        agg2: dict[str, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
        cnt2: dict[str, int] = defaultdict(int)
        for p in rows:
            agg2[_dept(p)][0] += _employer_cost(p.result); agg2[_dept(p)][1] += _d(p.result["net"]); cnt2[_dept(p)] += 1  # noqa: E702
        table = [(k, cnt2[k], _num(v[0]), _num(v[0] / cnt2[k]), _num(v[1] / cnt2[k])) for k, v in sorted(agg2.items())]
        built = await _one_sheet("Headcount cost", ["Department", "Employees", "Employer Cost", "Avg Cost / Employee", "Avg Net / Employee"], table)
    elif kind == "ctc":
        table = []
        for p in rows:
            u, snap, r = users[p.user_id], p.employee_snapshot or {}, p.result
            monthly = _employer_cost(r)
            table.append((u.employee_id, snap.get("full_name") or u.full_name, _dept(p), snap.get("designation"),
                          _num(_d(r["gross"])), _num(monthly), _num(monthly * 12)))  # fmt: skip
        built = await _one_sheet("CTC", ["Employee ID", "Name", "Department", "Designation", "Monthly Gross", "Monthly Cost to Company", "Annual Cost to Company"], table)
    elif kind == "lop":
        table = []
        for p in rows:
            lop = _lop(p)
            if lop <= 0:
                continue
            u, snap = users[p.user_id], p.employee_snapshot or {}
            b = p.lop_breakdown or {}
            table.append((u.employee_id, snap.get("full_name") or u.full_name, _dept(p), float(p.basis_days), float(lop),
                          float(b.get("unpaid_leave", 0)), float(b.get("absent", 0)), float(b.get("half_day_attendance", 0)),
                          "Yes" if p.lop_override is not None else "No"))  # fmt: skip
        built = await _one_sheet("LOP", ["Employee ID", "Name", "Department", "Basis Days", "LOP Days", "Unpaid Leave", "Absent", "Half Day", "Overridden"], table)
    else:  # bank_advice (the router demands payroll.manage + step-up)
        table = []
        for p in rows:
            if p.hold and p.status in runs.OPEN:
                continue
            u, ident, snap = users[p.user_id], p.identity or {}, p.employee_snapshot or {}
            table.append((u.employee_id, snap.get("full_name") or u.full_name, ident.get("bank_name"), ident.get("bank_account"),
                          ident.get("bank_ifsc"), _num(_d(p.result["net"])), snap.get("payment_mode"),
                          f"Salary {period:%b %Y}"))  # fmt: skip
        built = await _one_sheet("Bank advice", ["Employee ID", "Name", "Bank", "Account", "IFSC", "Net Pay", "Payment Mode", "Narration"], table)
    return built, f"payroll-{kind}-{period:%Y-%m}.xlsx", len(table)
