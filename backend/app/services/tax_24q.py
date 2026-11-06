"""24Q quarterly working file (plan 17 work package 5c, decision T9, contract 19.9): an .xlsx for the return preparation utility.

Nothing is filed by the app. Sheets: `Deductees` (one row per month and employee with TDS), `Salary Details` (quarter 4 only: annual
figures and regime per employee), `Challans` (the TDS dues of the quarter with BSR code, serial and deposit date) and `Blockers`
(`NO_PAN`, `CHALLAN_MISSING`, `RUN_NOT_PUBLISHED`). Only PUBLISHED payslips are used (one live copy per employee and month); PAN comes
from the employee's CURRENT payroll details, so fixing it clears a blocker without a new run. The file holds PANs and challan details:
the router requires the caller's password, rate-limits it and alerts the admins. Audit rows carry the FY, quarter and counts only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import AppError
from ..models import PayrollRun, PayrollTaxComputation, Payslip, StatutoryChallan
from . import payroll_statutory as stat
from . import payroll_tds as ptds
from . import tax_rules as tr
from . import xlsx
from .tax_declarations import MAX_REPORT_ROWS

ZERO = Decimal("0.00")
DEDUCTEES = ["Month", "Employee ID", "Name", "PAN", "Section", "Amount paid", "TDS deducted", "Date paid", "Date deducted",
             "Challan BSR code", "Challan serial", "Challan deposit date"]  # fmt: skip
SALARY = ["Employee ID", "Name", "PAN", "Regime", "Gross salary paid", "Exemptions", "Standard deduction", "Professional tax",
          "Chapter VI-A deductions", "Previous employer income", "Taxable income", "Total tax", "TDS deducted"]  # fmt: skip
CHALLANS = [
    "Month",
    "TDS due",
    "Employees",
    "Status",
    "Paid on",
    "Amount paid",
    "Challan BSR code",
    "Challan serial",
    "Challan deposit date",
]
BLOCKERS = ["Month", "Employee ID", "Code", "Detail"]


def quarter_months(fy: str, quarter: int) -> list[dt.date]:
    y = int(fy[:4])
    plan = {
        1: [(y, 4), (y, 5), (y, 6)],
        2: [(y, 7), (y, 8), (y, 9)],
        3: [(y, 10), (y, 11), (y, 12)],
        4: [(y + 1, 1), (y + 1, 2), (y + 1, 3)],
    }
    return [dt.date(a, b, 1) for a, b in plan[quarter]]


def _s(x: Decimal) -> Decimal:
    return stat._d(x).quantize(ZERO)  # a Decimal cell, formatted 0.00 by the workbook builder (no float rounding)


async def build(session: AsyncSession, fy: str, quarter: int) -> tuple[xlsx.BuiltWorkbook, str, int, dict[str, Any]]:
    rc = await ptds.current_ctx(session, fy)  # 409 TDS_ENGINE_OFF / TAX_RULES_NOT_VERIFIED
    section = str(((rc.rules or {}).get("labels") or {}).get("tds_section") or "")
    months = quarter_months(fy, quarter)
    loaded: dict[dt.date, tuple[PayrollRun | None, list[stat._Row]]] = {}
    for per in months:
        loaded[per] = await stat._load(session, per, missing_ok=True)
        if len(loaded[per][1]) > MAX_REPORT_ROWS:
            raise AppError("TOO_MANY_ROWS", "Too many payslips for one workbook")
    if not any(run is not None and run.status == "published" for run, _ in loaded.values()):
        raise AppError("RUN_NOT_PUBLISHED", "No payroll run of this quarter is published yet", status=409)
    challans = {
        c.period: c
        for c in (
            await session.execute(
                select(StatutoryChallan).where(StatutoryChallan.type == "tds", StatutoryChallan.period.in_(months))
            )
        ).scalars()
    }
    deductees: list[tuple] = []
    chal_rows: list[tuple] = []
    blockers: list[tuple] = []
    for per in months:
        run, rows = loaded[per]
        label = f"{per:%Y-%m}"
        if run is None:
            continue
        if run.status != "published":
            blockers.append((label, "", "RUN_NOT_PUBLISHED", "The payroll run of this month is not published"))
            continue
        due = ZERO
        n = 0
        for r in rows:
            tds = stat._ded(r.result, "TDS")
            if tds <= 0:
                continue
            due += tds
            n += 1
            pan = (r.ident.get("pan") or "").strip().upper()
            if not stat.PAN_RE.match(pan):
                blockers.append((label, r.employee_id or "", "NO_PAN", "PAN missing or invalid"))
            paid_on = run.pay_date.isoformat() if run.pay_date else ""
            c = challans.get(per)
            deductees.append(
                (
                    label, r.employee_id, r.name, pan, section, _s(stat._d(r.result["gross"])), _s(tds), paid_on, paid_on,
                    (c.bsr_code if c and c.status == "paid" else "") or "", (c.challan_serial if c and c.status == "paid" else "") or "",
                    c.deposited_on.isoformat() if c and c.status == "paid" and c.deposited_on else "",
                )
            )  # fmt: skip
        c = challans.get(per)
        ok = bool(c and c.status == "paid" and c.bsr_code and c.challan_serial)
        if due > 0 and not ok:
            blockers.append((label, "", "CHALLAN_MISSING", "No paid TDS challan with BSR code and serial for this month"))
        chal_rows.append(
            (
                label, _s(due), n, c.status if c else "unpaid", c.paid_on.isoformat() if c and c.paid_on else "",
                _s(stat._d(c.amount)) if c and c.status == "paid" else "", (c.bsr_code if c else "") or "",
                (c.challan_serial if c else "") or "", c.deposited_on.isoformat() if c and c.deposited_on else "",
            )
        )  # fmt: skip
    sheets: list[tuple[str, list[str], list[tuple]]] = [("Deductees", DEDUCTEES, deductees)]
    if quarter == 4:
        sheets.append(("Salary Details", SALARY, await _salary_details(session, fy)))
    sheets += [("Challans", CHALLANS, chal_rows), ("Blockers", BLOCKERS, blockers)]
    built = await stat._book(sheets)
    return built, f"payroll-24q-{fy}-q{quarter}.xlsx", len(deductees), {"fy": fy, "quarter": quarter, "blockers": len(blockers)}


async def _salary_details(session: AsyncSession, fy: str) -> list[tuple]:
    first, last = tr.fy_bounds(fy)
    per_user: dict[uuid.UUID, dict[str, Any]] = {}
    q = (
        select(Payslip, PayrollRun.period)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(PayrollRun.period >= first, PayrollRun.period <= last, Payslip.status == "published")
        .limit(MAX_REPORT_ROWS + 1)
    )
    slips: dict[tuple[uuid.UUID, dt.date], Payslip] = {}
    got = (await session.execute(q)).all()
    if len(got) > MAX_REPORT_ROWS:
        raise AppError("TOO_MANY_ROWS", "Too many payslips for one workbook")
    for p, per in got:
        if p.result is not None and ((p.user_id, per) not in slips or p.revision > slips[(p.user_id, per)].revision):
            slips[(p.user_id, per)] = p
    for (uid, _per), p in sorted(slips.items(), key=lambda kv: kv[0][1]):
        d = per_user.setdefault(
            uid, {"gross": ZERO, "tds": ZERO, "snap": p.employee_snapshot or {}, "ident": p.identity or {}, "ids": []}
        )
        d["gross"] += stat._d(p.result["gross"])
        d["tds"] += stat._ded(p.result, "TDS")
        d["ids"].append(p.id)
    comp: dict[uuid.UUID, tuple[dt.date, PayrollTaxComputation]] = {}
    cq = (
        select(PayrollTaxComputation, PayrollRun.period)
        .join(Payslip, Payslip.id == PayrollTaxComputation.payslip_id)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(PayrollTaxComputation.fy == fy, Payslip.status == "published")
        .limit(MAX_REPORT_ROWS + 1)
    )
    cgot = (await session.execute(cq)).all()
    if len(cgot) > MAX_REPORT_ROWS:
        raise AppError("TOO_MANY_ROWS", "Too many tax computations for one workbook")
    for tc, per in cgot:
        if tc.user_id not in comp or per > comp[tc.user_id][0]:
            comp[tc.user_id] = (per, tc)
    dets = await stat.runs._load_details(session, list(per_user))  # current PAN
    out: list[tuple] = []
    for uid, d in per_user.items():
        tc = comp.get(uid, (None, None))[1]
        annual = (tc.computation or {}).get("annual", {}) if tc is not None else {}
        det = dets.get(uid)
        pan = (det.pan if det is not None else None) or ""

        def g(k: str, annual: dict = annual) -> Decimal:
            return Decimal(annual.get(k, "0")).quantize(ZERO)

        out.append(
            (
                d["snap"].get("employee_id", ""), d["snap"].get("full_name", ""), pan, tc.regime if tc is not None else "",
                _s(d["gross"]), g("exemptions"), g("standard_deduction"), g("pt"), g("chapter_via"), g("previous_employer_income"),
                g("taxable_income"), g("total_tax"), _s(d["tds"]),
            )
        )  # fmt: skip
    out.sort(key=lambda r: (r[1].lower(), r[0]))
    return out
