"""Statutory reports and dues tracking (plan 14 phase 2): PF ECR text file, ESI contribution xlsx, PT summary xlsx,
monthly dues (PF, ESI, PT, TDS) and challan paid / unpaid marking.

Only PUBLISHED runs are used (one published copy per employee, `runs._live`). Employees whose statutory identifier is missing or
malformed (UAN for PF, ESI IP number for ESI, PAN for TDS) are returned as `blockers`; a file is never produced with such an
employee silently left out (409 STATUTORY_BLOCKERS carries the list). Money is never logged and never written to the audit log.

!! The ECR / ESI column layouts below follow the formats known at the time of writing and MUST be verified by the user against
the current EPFO and ESIC portals before use.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from collections.abc import AsyncIterator
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import PayrollRun, Payslip, SalaryStructure, StatutoryChallan, User
from . import admin_security, xlsx
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_reports as reports
from . import payroll_runs as runs

TYPES = ("pf", "esi", "pt", "tds")
MAX_MONTHS = 12
MAX_ROWS = 5000
EPS_WAGE_CEILING = Decimal("15000")  # EPS / EDLI wage cap (statutory constant, not a setting)
EPS_RATE_PCT = Decimal("8.33")
ZERO = Decimal("0.00")
UAN_RE = re.compile(r"^\d{12}$")
ESI_IP_RE = re.compile(r"^\d{10,17}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
SEP = "#~#"
ECR_HEADER = (
    "UAN", "MEMBER NAME", "GROSS WAGES", "EPF WAGES", "EPS WAGES", "EDLI WAGES", "EPF CONTRI REMITTED",
    "EPS CONTRI REMITTED", "EPF EPS DIFF REMITTED", "NCP DAYS", "REFUND OF ADVANCES",
)  # fmt: skip


def _d(x: Any) -> Decimal:
    return Decimal(str(x)) if x is not None else ZERO


def _int(x: Decimal) -> int:
    return int(x.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _ded(result: dict, code: str) -> Decimal:
    for d in result.get("deductions", []):
        if d["code"] == code:
            return _d(d["amount"])
    return ZERO


def _earn(result: dict, code: str) -> Decimal:
    for e in result.get("earnings", []):
        if e["code"] == code:
            return _d(e["amount"])
    return ZERO


def _name(snap: dict, user: User | None) -> str:
    return (
        (snap.get("full_name") or (user.full_name if user else "") or "").replace(SEP, " ").replace("\r", " ").replace("\n", " ")
    )


# ================================================================================================== data
class _Row:
    __slots__ = ("p", "user", "snap", "ident", "result", "structure")

    def __init__(self, p: Payslip, user: User | None, structure: SalaryStructure | None):
        self.p, self.user, self.structure = p, user, structure
        self.snap, self.ident, self.result = p.employee_snapshot or {}, p.identity or {}, p.result or {}

    @property
    def employee_id(self) -> str | None:
        return self.user.employee_id if self.user else self.snap.get("employee_id")

    @property
    def name(self) -> str:
        return _name(self.snap, self.user)


async def _load(session: AsyncSession, period: dt.date, *, missing_ok: bool = False) -> tuple[PayrollRun | None, list[_Row]]:
    run = await reports._run_by_period(session, period)
    if run is None:
        if missing_ok:
            return None, []
        raise AppError("NOT_FOUND", "No payroll run for that month")
    if run.status != "published":
        if missing_ok:
            return run, []
        raise AppError("RUN_NOT_PUBLISHED", "Statutory data is only available for a published payroll run", status=409)
    slips = [p for p in runs._live(await runs._run_payslips(session, run.id)) if p.status == "published" and p.result is not None]
    users = (
        {u.id: u for u in (await session.execute(select(User).where(User.id.in_([p.user_id for p in slips])))).unique().scalars()}
        if slips
        else {}
    )
    sids = {p.structure_id for p in slips if p.structure_id}
    structs = (
        {s.id: s for s in (await session.execute(select(SalaryStructure).where(SalaryStructure.id.in_(sids)))).scalars()}
        if sids
        else {}
    )
    rows = [_Row(p, users.get(p.user_id), structs.get(p.structure_id)) for p in slips]
    rows.sort(key=lambda r: ((r.name or "").lower(), r.employee_id or ""))
    return run, rows


def _pf_amounts(r: _Row) -> tuple[Decimal, Decimal]:
    return _ded(r.result, "PF"), calc.employer_amount(r.result, "PF_ER")


def _esi_amounts(r: _Row) -> tuple[Decimal, Decimal]:
    return _ded(r.result, "ESI"), calc.employer_amount(r.result, "ESI_ER")


def _check(missing: list[dict[str, str]], label: str, value: Any, rx: re.Pattern[str]) -> None:
    v = value.strip().upper() if isinstance(value, str) else ""
    if not v:
        missing.append({"field": label, "reason": "missing"})
    elif not rx.match(v):
        missing.append({"field": label, "reason": "invalid"})


def _blockers(rows: list[_Row], kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    """Employees with a statutory amount but a missing / malformed identifier. Identifier values are never returned."""
    out: list[dict[str, Any]] = []
    for r in rows:
        missing: list[dict[str, str]] = []
        if "pf" in kinds and any(a > 0 for a in _pf_amounts(r)):
            _check(missing, "UAN", r.ident.get("uan"), UAN_RE)
        if "esi" in kinds and any(a > 0 for a in _esi_amounts(r)):
            _check(missing, "ESI_IP", r.ident.get("esi_ip_number"), ESI_IP_RE)
        if "tds" in kinds and _ded(r.result, "TDS") > 0:
            _check(missing, "PAN", r.ident.get("pan"), PAN_RE)
        if missing:
            out.append({"employee_id": r.employee_id, "name": r.name, "missing": missing})
    return out


def _raise_blockers(blockers: list[dict[str, Any]]) -> None:
    if blockers:
        raise AppError(
            "STATUTORY_BLOCKERS",
            f"{len(blockers)} employee(s) have a missing or invalid statutory identifier",
            status=409,
            details=[
                {"field": b["employee_id"], "message": ", ".join(m["field"] + " " + m["reason"] for m in b["missing"])}
                for b in blockers
            ],
        )


def _totals(rows: list[_Row]) -> dict[str, tuple[Decimal, int]]:
    pf = esi = pt = tds = ZERO
    n = {"pf": 0, "esi": 0, "pt": 0, "tds": 0}
    for r in rows:
        a, b = _pf_amounts(r)
        c, d = _esi_amounts(r)
        e, f = _ded(r.result, "PT"), _ded(r.result, "TDS")
        pf, esi, pt, tds = pf + a + b, esi + c + d, pt + e, tds + f
        n["pf"] += (a + b) > 0
        n["esi"] += (c + d) > 0
        n["pt"] += e > 0
        n["tds"] += f > 0
    return {"pf": (pf, n["pf"]), "esi": (esi, n["esi"]), "pt": (pt, n["pt"]), "tds": (tds, n["tds"])}


# ================================================================================================== dues
async def dues(session: AsyncSession, raw_period: str | None, months: int) -> dict[str, Any]:
    today = await runs._today(session)
    end = reports._period(raw_period, today)
    periods = [reports._month_back(end, i) for i in range(months - 1, -1, -1)]
    challans = {
        (c.type, c.period): c
        for c in (await session.execute(select(StatutoryChallan).where(StatutoryChallan.period.in_(periods)))).scalars()
    }
    out = []
    for per in periods:
        run, rows = await _load(session, per, missing_ok=True)
        items: list[dict[str, Any]] = []
        if run is not None and run.status == "published":
            tot = _totals(rows)
            for t in TYPES:
                amount, count = tot[t]
                c = challans.get((t, per))
                items.append(
                    {
                        "type": t, "amount": calc.money_str(amount), "employees": count,
                        "status": c.status if c else "unpaid",
                        "paid_on": c.paid_on.isoformat() if c and c.paid_on else None,
                        "reference": c.reference if c else None,
                        "paid_amount": calc.money_str(_d(c.amount)) if c and c.status == "paid" else None,
                        "mismatch": bool(c and c.status == "paid" and _d(c.amount) != amount),
                    }
                )  # fmt: skip
        out.append(
            {
                "period": f"{per:%Y-%m}",
                "run_status": run.status if run else None,
                "items": items,
                "blockers": _blockers(rows, ("pf", "esi", "tds")),
            }
        )
    return {"months": out}


# ================================================================================================== challans
def _pay_period(raw: str, today: dt.date) -> dt.date:
    return runs.parse_period(raw, today)


async def set_challan(
    session: AsyncSession, redis: Redis, actor: Principal, *, type_: str, raw_period: str, status: str,
    paid_on: dt.date | None, reference: str | None, amount: str | None, password: Any,
) -> dict[str, Any]:  # fmt: skip
    await admin_security.require_reauth(session, redis, actor, password, action="payroll.statutory.challan")
    today = await runs._today(session)
    period = _pay_period(raw_period, today)
    run, rows = await _load(session, period)
    due = _totals(rows)[type_][0]
    existing = (
        await session.execute(
            select(StatutoryChallan).where(StatutoryChallan.type == type_, StatutoryChallan.period == period).with_for_update()
        )
    ).scalar_one_or_none()
    before = {"status": existing.status} if existing else {"status": "none"}

    if status == "paid":
        if paid_on is None or paid_on > today or paid_on < period:
            raise AppError(
                "INVALID_DATE", "paid_on must be on or after the period start and not in the future", status=422,
                details=[{"field": "paid_on", "message": "must be between the period start and today"}],
            )  # fmt: skip
        if existing is not None and existing.status == "paid":
            raise AppError("CHALLAN_ALREADY_PAID", "This due is already marked paid; mark it unpaid first", status=409)
        paid_amount = calc.parse_money(amount) if amount is not None else due
        if existing is None:
            existing = StatutoryChallan(type=type_, period=period, created_by=actor.id)
            session.add(existing)
        existing.status, existing.paid_on, existing.reference = "paid", paid_on, reference
        existing.amount = calc.money_str(paid_amount)
        existing.updated_by = actor.id
        action = "payroll.statutory.challan_paid"
    else:
        if existing is None or existing.status != "paid":
            raise AppError("CHALLAN_NOT_PAID", "This due is not marked paid", status=409)
        existing.status, existing.paid_on, existing.reference = "unpaid", None, None
        existing.updated_by = actor.id
        action = "payroll.statutory.challan_unpaid"

    audit(
        session, action=action, actor=actor, entity_type="statutory_challan", entity_id=existing.id,
        before=before, after={"type": type_, "period": f"{period:%Y-%m}", "status": status},
    )  # fmt: skip
    await pc.commit(session)
    return {
        "type": type_, "period": f"{period:%Y-%m}", "status": existing.status,
        "paid_on": existing.paid_on.isoformat() if existing.paid_on else None, "reference": existing.reference,
    }  # fmt: skip


# ================================================================================================== PF ECR
async def pf_ecr(session: AsyncSession, raw_period: str | None) -> tuple[bytes, str, int, dict[str, Any]]:
    today = await runs._today(session)
    period = reports._period(raw_period, today)
    run, rows = await _load(session, period)
    rows = [r for r in rows if any(a > 0 for a in _pf_amounts(r))]
    _raise_blockers(_blockers(rows, ("pf",)))
    rules = calc.Rules.from_config(run.config or {})
    lines: list[str] = []
    for r in rows:
        ee, er = _pf_amounts(r)
        basic = _earn(r.result, "BASIC")
        full = bool(r.structure.pf_on_full_basic) if r.structure is not None else False
        epf_wage = basic if (full or rules.pf_wage_ceiling == 0) else min(basic, rules.pf_wage_ceiling)
        eps_wage = min(epf_wage, EPS_WAGE_CEILING)
        eps_share = _int(eps_wage * EPS_RATE_PCT / 100)
        er_i = _int(er)
        ncp = int((_d(r.p.basis_days) - _d(r.p.paid_days)).to_integral_value(rounding=ROUND_CEILING))
        vals = (
            r.ident["uan"].strip(), r.name, _int(_d(r.result["gross"])), _int(epf_wage), _int(eps_wage), _int(eps_wage),
            _int(ee), eps_share, er_i - eps_share, max(ncp, 0), 0,
        )  # fmt: skip
        lines.append(SEP.join(str(v) for v in vals))
    body = ("\r\n".join(lines) + "\r\n") if lines else ""
    return body.encode("ascii", errors="replace"), f"payroll-pf-ecr-{period:%Y-%m}.txt", len(lines), {"period": f"{period:%Y-%m}"}


# ================================================================================================== xlsx
def _spec(title: str, headers: list[str], table: list[tuple]) -> xlsx.SheetSpec:
    async def rows() -> AsyncIterator[tuple]:
        for r in table:
            yield r

    async def count() -> int:
        return len(table)

    return xlsx.SheetSpec(title, headers, count, rows, MAX_ROWS)


async def _book(sheets: list[tuple[str, list[str], list[tuple]]]) -> xlsx.BuiltWorkbook:
    return await xlsx.build_workbook([_spec(*t) for t in sheets])


def _esi_wage(r: _Row) -> Decimal:
    flags: dict[str, bool] = {}
    if r.structure is not None:
        flags = {a["code"]: bool(a.get("esi_wage", True)) for a in (r.structure.components or {}).get("allowances", [])}
    total = ZERO
    for e in r.result.get("earnings", []):
        code = e["code"]
        if code in ("BASIC", "HRA") or code in calc.ESI_ADJUSTMENTS or flags.get(code, False):
            total += _d(e["amount"])
    return total


async def esi_contribution(session: AsyncSession, raw_period: str | None) -> tuple[xlsx.BuiltWorkbook, str, int, dict[str, Any]]:
    today = await runs._today(session)
    period = reports._period(raw_period, today)
    _run, rows = await _load(session, period)
    rows = [r for r in rows if any(a > 0 for a in _esi_amounts(r))]
    _raise_blockers(_blockers(rows, ("esi",)))
    nxt = reports._month_back(period, -1)
    template, recon = [], []
    for r in rows:
        ee, er = _esi_amounts(r)
        wage = _esi_wage(r)
        days = _int(_d(r.p.paid_days))
        last = ""
        if r.user is not None and r.user.deactivated_at is not None:
            d0 = r.user.deactivated_at.date()
            if period <= d0 < nxt:
                last = d0.strftime("%d/%m/%Y")
        template.append((r.ident["esi_ip_number"].strip(), r.name, days, float(wage), "", last))
        recon.append((r.employee_id, r.name, float(wage), float(ee), float(er), float(ee + er)))
    built = await _book(
        [
            ("ESI Contribution", ["IP Number", "IP Name", "No of Days", "Total Monthly Wages", "Reason Code for 0 wages", "Last Working Day"], template),
            ("Reconciliation", ["Employee ID", "Name", "ESI Wages", "Employee Share", "Employer Share", "Total"], recon),
        ]
    )  # fmt: skip
    return built, f"payroll-esi-{period:%Y-%m}.xlsx", len(template), {"period": f"{period:%Y-%m}"}


def _slab_label(rules: calc.Rules, amount: Decimal) -> str:
    for s in rules.pt_slabs:
        if s.amount == amount:
            hi = "and above" if s.max_gross is None else f"to {calc.money_str(s.max_gross)}"
            return f"{calc.money_str(s.min_gross)} {hi}"
    return "Other / override"


async def pt_summary(session: AsyncSession, raw_period: str | None) -> tuple[xlsx.BuiltWorkbook, str, int, dict[str, Any]]:
    today = await runs._today(session)
    period = reports._period(raw_period, today)
    run, rows = await _load(session, period)
    rules = calc.Rules.from_config(run.config or {})
    per_emp: list[tuple] = []
    agg: dict[tuple[Decimal, str], list[Decimal | int]] = defaultdict(lambda: [0, ZERO])
    for r in rows:
        pt = _ded(r.result, "PT")
        if pt <= 0:
            continue
        label = _slab_label(rules, pt)
        per_emp.append(
            (r.employee_id, r.name, r.snap.get("department") or "Unassigned", float(_d(r.result["gross"])), label, float(pt))
        )
        a = agg[(pt, label)]
        a[0] += 1
        a[1] += pt
    slabs = [(label, float(amt), cnt, float(total)) for (amt, label), (cnt, total) in sorted(agg.items())]
    built = await _book(
        [
            ("PT by Employee", ["Employee ID", "Name", "Department", "Gross", "Slab (gross range)", "PT"], per_emp),
            ("PT by Slab", ["Slab (gross range)", "PT per Employee", "Employees", "Total PT"], slabs),
        ]
    )  # fmt: skip
    meta = {"period": f"{period:%Y-%m}", "frequency": rules.pt_frequency}
    return built, f"payroll-pt-{period:%Y-%m}.xlsx", len(per_emp), meta
