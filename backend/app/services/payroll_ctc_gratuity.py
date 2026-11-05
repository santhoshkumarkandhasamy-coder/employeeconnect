"""CTC calculator endpoint and gratuity accrual report (plan 16 work package 4a, contract 18.2 / 18.3).

Both are read-only: nothing is stored. Gratuity is an ACCRUAL estimate shown here and (in 4b) as information on the settlement
statement; it never becomes a payslip line. Money is decrypted in Python (no SQL aggregates, plan 12 D2).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import EmployeePayrollDetails, SalaryStructure, User
from ..schemas.payroll import CtcCalculateIn
from . import attendance as att
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_ctc as ctc
from . import payroll_increments as ig
from . import payroll_settings as pset
from . import payroll_structures as structs_svc
from . import xlsx
from .payroll_calc import money_str

MIN_AS_OF = dt.date(2000, 1, 1)
MAX_EMPLOYEES = 5000
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ============================================================================================ CTC calculator
async def calculate(session: AsyncSession, actor: Principal, body: CtcCalculateIn) -> dict[str, Any]:
    settings = (await pset.read_all(session))["settings"]
    s = ctc.CtcSettings.from_config(settings)
    split = body.split
    opts = ctc.Options(
        pf_applicable=body.pf_applicable, pf_on_full_basic=body.pf_on_full_basic, esi_applicable=body.esi_applicable,
        pt_applicable=body.pt_applicable,
        basic_pct=split.basic_pct if split else s.split_basic_pct,
        hra_pct=split.hra_pct_of_basic if split else s.split_hra_pct,
        include_gratuity=s.include_gratuity if body.include_gratuity is None else body.include_gratuity,
        employer_share_from_employee=(
            s.rules.employer_share_from_employee
            if body.employer_share_from_employee is None
            else body.employer_share_from_employee
        ),
    )  # fmt: skip
    try:
        out = ctc.calculate(body.mode, body.amount, body.frequency, s, opts)
    except ctc.CtcError as exc:
        raise AppError("VALIDATION_ERROR", str(exc), details=[{"field": exc.field, "message": str(exc)}]) from None
    audit(session, action="payroll.ctc.calculate", actor=actor, entity_type="payroll_ctc", after={"mode": body.mode})
    await pc.commit(session)
    return out


# ============================================================================================ gratuity
def parse_as_of(raw: str | None, today: dt.date) -> dt.date:
    if raw is None:
        return today
    try:
        day = dt.date.fromisoformat(raw) if _DATE_RE.match(raw) else None
    except ValueError:
        day = None
    if day is None or not MIN_AS_OF <= day <= today:
        raise AppError(
            "INVALID_DATE", "as_of must be a date from 2000-01-01 up to today",
            details=[{"field": "as_of", "message": "must be a date from 2000-01-01 up to today"}],
        )  # fmt: skip
    return day


def last_day_of_month(period: dt.date) -> dt.date:
    return (dt.date(period.year + (period.month == 12), period.month % 12 + 1, 1)) - dt.timedelta(days=1)


async def _rows(
    session: AsyncSession,
    as_of: dt.date,
    *,
    department_id: uuid.UUID | None,
    q: str | None,
    eligible_only: bool,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    """Every matching employee with the gratuity figures, the totals of the whole filtered set and the header settings."""
    cfg = await ig.load_cfg(session)
    settings = (await pset.read_all(session))["settings"]
    basis = str(settings["payroll.gratuity_wage_basis"])
    min_years = int(settings["payroll.gratuity_min_years"])
    cap = calc.parse_money(settings["payroll.gratuity_cap"])

    cond = [User.deleted_at.is_(None)]
    if department_id:
        cond.append(User.department_id == department_id)
    if q:
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cond.append(func.lower(User.full_name).like(func.lower(like), escape="\\") | User.employee_id.ilike(like, escape="\\"))
    excluded = exists().where(EmployeePayrollDetails.user_id == User.id, EmployeePayrollDetails.include_in_payroll.is_(False))
    cond.append(~excluded)
    users = list(
        (await session.execute(select(User).where(*cond).order_by(func.lower(User.full_name), User.employee_id).limit(MAX_EMPLOYEES + 1)))
        .unique()
        .scalars()
    )  # fmt: skip
    if len(users) > MAX_EMPLOYEES:
        raise AppError("TOO_MANY_ROWS", f"More than {MAX_EMPLOYEES} employees match; narrow the filter")
    structures: dict[uuid.UUID, list[tuple[dt.date, SalaryStructure]]] = {}
    if users:
        sq = select(SalaryStructure).where(SalaryStructure.user_id.in_([u.id for u in users]))
        for st in (await session.execute(sq)).scalars():
            structures.setdefault(st.user_id, []).append((st.effective_from, st))

    lwds: dict[uuid.UUID, dt.date] = {}
    if users:
        lq = select(EmployeePayrollDetails.user_id, EmployeePayrollDetails.last_working_day).where(
            EmployeePayrollDetails.user_id.in_([u.id for u in users]), EmployeePayrollDetails.last_working_day.is_not(None)
        )
        lwds = {uid_: d for uid_, d in (await session.execute(lq)).all()}
    month_start = as_of.replace(day=1)
    items: list[dict[str, Any]] = []
    tot_accrued = tot_eligible = tot_monthly = Decimal("0.00")
    for u in users:
        if u.join_date is not None and u.join_date > as_of:
            continue  # not yet employed on that date
        last = ig.last_employed_day(u, cfg.tz, lwds.get(u.id))
        if last is not None and last < month_start:
            continue  # left before the month of as_of
        flags: list[str] = []
        end = min(as_of, last) if last is not None else as_of
        item: dict[str, Any] = {
            "user": pc.user_out(u),
            "join_date": u.join_date.isoformat() if u.join_date else None,
            "exit_date": last.isoformat() if last is not None else None,
            "service": None, "counted_years": None, "eligible": False,
            "wage": None, "accrued": None, "monthly_accrual": None, "flags": flags,
        }  # fmt: skip
        struct = calc.structure_for(structures.get(u.id, []), as_of)
        if struct is None:
            flags.append("NO_STRUCTURE")
        accrued = monthly = None
        if u.join_date is None:
            flags.append("NO_JOIN_DATE")
        else:
            wage = None
            if struct is not None:
                data = structs_svc.data_of(struct)
                wage = ctc.gratuity_wage(data.basic, data.monthly_gross, basis)
            g = ctc.gratuity(wage or Decimal(0), u.join_date, end, min_years=min_years, cap=cap)
            item["service"] = {"years": g.years, "months": g.months, "days": g.days}
            item["counted_years"], item["eligible"] = g.counted, g.eligible
            if wage is not None:
                accrued, monthly = g.accrued, ctc.monthly_gratuity_accrual(wage)
                item["wage"], item["accrued"], item["monthly_accrual"] = money_str(wage), money_str(accrued), money_str(monthly)
                if g.capped:
                    flags.append("CAPPED")
        if eligible_only and not item["eligible"]:
            continue
        if accrued is not None and monthly is not None:  # totals cover the whole filtered set, not the page
            tot_accrued += accrued
            tot_monthly += monthly
            if item["eligible"]:
                tot_eligible += accrued
        items.append(item)
    totals = {
        "accrued": money_str(tot_accrued),
        "eligible_accrued": money_str(tot_eligible),
        "monthly_accrual": money_str(tot_monthly),
    }
    head = {"basis": basis, "cap": money_str(cap), "min_years": min_years}
    return items, totals, head


async def gratuity_report(
    session: AsyncSession,
    actor: Principal,
    *,
    raw_as_of: str | None,
    department_id: uuid.UUID | None,
    eligible_only: bool,
    q: str | None,
    page: PageParams,
) -> dict[str, Any]:
    today = dt.datetime.now(att.office_tz(await att.load_settings(session))).date()
    as_of = parse_as_of(raw_as_of, today)
    items, totals, head = await _rows(session, as_of, department_id=department_id, q=q, eligible_only=eligible_only)
    window = items[page.offset : page.offset + page.page_size]
    out = {"as_of": as_of.isoformat(), **head, "items": window, "totals": totals}
    out = {**out, **{k: v for k, v in make_page(window, page, len(items)).items() if k != "items"}}
    await pc.audit_read(session, actor, "payroll.gratuity.view", "payroll_gratuity", count=len(window))
    return out


async def gratuity_xlsx(session: AsyncSession, raw_period: str | None) -> tuple[xlsx.BuiltWorkbook, str, int, dt.date]:
    """Sheet `Gratuity` for the month (`as_of` = its last day; the current month is allowed, a later one is not)."""
    today = dt.datetime.now(att.office_tz(await att.load_settings(session))).date()
    period = today.replace(day=1)
    if raw_period is not None:
        period = _period(raw_period)
        if period > today.replace(day=1):
            raise AppError(
                "INVALID_PERIOD", "The month is in the future", details=[{"field": "period", "message": "must not be in the future"}]
            )  # fmt: skip
    as_of = last_day_of_month(period)
    items, _totals, _head = await _rows(session, as_of, department_id=None, q=None, eligible_only=False)
    table = [_xlsx_row(i) for i in items]

    async def rows() -> AsyncIterator[tuple]:
        for r in table:
            yield r

    async def count() -> int:
        return len(table)

    headers = [
        "Employee ID", "Name", "Department", "Join Date", "Exit Date", "Service Years", "Service Months", "Service Days",
        "Counted Years", "Eligible", "Wage", "Accrued", "Monthly Accrual", "Flags",
    ]  # fmt: skip
    built = await xlsx.build_workbook([xlsx.SheetSpec("Gratuity", headers, count, rows, MAX_EMPLOYEES)])
    return built, f"payroll-gratuity-{period:%Y-%m}.xlsx", len(table), as_of


def _period(raw: str) -> dt.date:
    m = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", raw)
    if not m or not 2000 <= int(m.group(1)) <= 2200:
        raise AppError("INVALID_PERIOD", "period must be YYYY-MM", details=[{"field": "period", "message": "must be YYYY-MM"}])
    return dt.date(int(m.group(1)), int(m.group(2)), 1)


def _xlsx_row(i: dict[str, Any]) -> tuple:
    def num(v: str | None) -> float | None:
        return float(v) if v is not None else None

    sv = i["service"] or {}
    u = i["user"]
    return (
        u["employee_id"], u["full_name"], u["department"],
        dt.date.fromisoformat(i["join_date"]) if i["join_date"] else None,
        dt.date.fromisoformat(i["exit_date"]) if i["exit_date"] else None,
        sv.get("years"), sv.get("months"), sv.get("days"), i["counted_years"], "Yes" if i["eligible"] else "No",
        num(i["wage"]), num(i["accrued"]), num(i["monthly_accrual"]), ", ".join(i["flags"]),
    )  # fmt: skip
