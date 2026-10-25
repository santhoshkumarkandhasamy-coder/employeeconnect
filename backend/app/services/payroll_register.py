"""Payroll register .xlsx (plan 12 s.5, contract 13.5): sheets Register, Summary and (include_bank) Bank. Built on services.xlsx
(formula-injection guard, private temp dir, bounded concurrency). Amounts are numeric cells."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User
from . import payroll_calc as calc
from . import payroll_runs as runs
from . import xlsx

MAX_ROWS = 5000


def _num(x: Decimal | str | None) -> float:
    return float(Decimal(x)) if x is not None else 0.0


def _lines(rows: list[dict]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in rows:
        out[r["code"]] = out.get(r["code"], Decimal(0)) + Decimal(r["amount"])
    return out


async def build(session: AsyncSession, run_id: uuid.UUID, include_bank: bool) -> tuple[xlsx.BuiltWorkbook, str, int]:
    run = await runs._run_or_404(session, run_id)
    live = [p for p in runs._live(await runs._run_payslips(session, run.id)) if p.result is not None]
    users = {
        u.id: u
        for u in (await session.execute(select(User).where(User.id.in_(list({p.user_id for p in live}))))).unique().scalars()
    }
    live.sort(key=lambda p: (users[p.user_id].full_name.lower(), users[p.user_id].employee_id))

    allowances: set[str] = set()
    adj_earn: set[str] = set()
    fixed: set[str] = set()
    adj_ded: set[str] = set()
    for p in live:
        for e in p.result["earnings"]:
            c = e["code"]
            if c in ("BASIC", "HRA"):
                continue
            (adj_earn if c in calc.EARNING_ADJUSTMENTS else allowances).add(c)
        for d in p.result["deductions"]:
            c = d["code"]
            if c in ("PF", "ESI", "PF_ER_REC", "ESI_ER_REC", "PT", "TDS"):
                continue
            (adj_ded if c in calc.DEDUCTION_ADJUSTMENTS else fixed).add(c)
    a_cols, ae_cols, f_cols, ad_cols = sorted(allowances), sorted(adj_earn), sorted(fixed), sorted(adj_ded)

    text_headers = ["Employee ID", "Name", "Department", "Designation", "Status", "Revision", "Hold"]
    money_headers = (
        ["Basis Days", "LOP Days", "Paid Days", "BASIC", "HRA", *a_cols, *ae_cols, "Gross", "PF", "ESI", "PF Employer Share Recovered", "ESI Employer Share Recovered", "PT", "TDS", *f_cols, *ad_cols]
        + ["Total Deductions", "Net Pay", "Employer PF", "Employer ESI", "Employer Cost (net of recovery)"]
    )  # fmt: skip
    headers = text_headers + money_headers

    table: list[tuple] = []
    for p in live:
        u, snap, r = users[p.user_id], p.employee_snapshot or {}, p.result
        earn, ded = _lines(r["earnings"]), _lines(r["deductions"])
        money = [
            float(p.basis_days), float(p.lop_override if p.lop_override is not None else p.lop_auto), float(p.paid_days),
            _num(earn.get("BASIC")), _num(earn.get("HRA")),
            *[_num(earn.get(c)) for c in a_cols], *[_num(earn.get(c)) for c in ae_cols],
            _num(r["gross"]), _num(ded.get("PF")), _num(ded.get("ESI")),
            _num(ded.get("PF_ER_REC")), _num(ded.get("ESI_ER_REC")), _num(ded.get("PT")), _num(ded.get("TDS")),
            *[_num(ded.get(c)) for c in f_cols], *[_num(ded.get(c)) for c in ad_cols],
            _num(r["total_deductions"]), _num(r["net"]),
            float(calc.employer_amount(r, "PF_ER")), float(calc.employer_amount(r, "ESI_ER")),
            float(calc.employer_amount(r, "PF_ER") + calc.employer_amount(r, "ESI_ER")
                  - Decimal(str(ded.get("PF_ER_REC") or 0)) - Decimal(str(ded.get("ESI_ER_REC") or 0))),
        ]  # fmt: skip
        table.append(
            (u.employee_id, snap.get("full_name") or u.full_name, snap.get("department"), snap.get("designation"),
             p.status, p.revision, "Yes" if p.hold else "No", *money)
        )  # fmt: skip

    async def reg_rows() -> AsyncIterator[tuple]:
        for row in table:
            yield row

    async def reg_count() -> int:
        return len(table)

    def totals() -> list[tuple]:
        out: list[tuple] = [
            ("Period", f"{run.period:%Y-%m}"),
            ("Run status", run.status),
            ("Employees", len(live)),
            ("Published", sum(p.status == "published" for p in live)),
            ("Processed", sum(p.status == "processed" for p in live)),
            ("Draft", sum(p.status == "draft" for p in live)),
            ("Held", sum(p.hold for p in live)),
        ]
        for i, h in enumerate(money_headers):
            if h in ("Basis Days", "LOP Days", "Paid Days"):
                continue
            out.append((f"Total {h}", round(sum(r[len(text_headers) + i] for r in table), 2)))
        return out

    summary = totals()

    async def sum_rows() -> AsyncIterator[tuple]:
        for row in summary:
            yield row

    async def sum_count() -> int:
        return len(summary)

    specs = [
        xlsx.SheetSpec("Register", headers, reg_count, reg_rows, MAX_ROWS),
        xlsx.SheetSpec("Summary", ["Item", "Value"], sum_count, sum_rows, MAX_ROWS),
    ]
    if include_bank:
        bank_rows: list[tuple] = []
        for p in live:
            if p.hold and p.status in runs.OPEN:
                continue
            u, ident, snap = users[p.user_id], p.identity or {}, p.employee_snapshot or {}
            bank_rows.append(
                (u.employee_id, snap.get("full_name") or u.full_name, ident.get("bank_name"), ident.get("bank_account"),
                 ident.get("bank_ifsc"), _num(p.result["net"]), snap.get("payment_mode"))
            )  # fmt: skip

        async def bank_iter() -> AsyncIterator[tuple]:
            for row in bank_rows:
                yield row

        async def bank_count() -> int:
            return len(bank_rows)

        specs.append(
            xlsx.SheetSpec(
                "Bank",
                ["Employee ID", "Name", "Bank", "Account", "IFSC", "Net Pay", "Payment Mode"],
                bank_count,
                bank_iter,
                MAX_ROWS,
            )
        )
    built = await xlsx.build_workbook(specs)
    return built, f"payroll-register-{run.period:%Y-%m}.xlsx", len(table)
