"""Full and final settlement statement PDF (plan 16, contract 18.8), on the payslip PDF engine.

Same rules as the payslip PDF: fpdf2 with the bundled DejaVu font, every user-controlled string drawn as plain text and filtered to the
font's glyphs, no links or attachments, built in a worker thread under the SAME 2-slot semaphore and 10 s budget (`build_pdf`), nothing
written to disk. The watermark `DRAFT - NOT FINAL` stays until the settlement is `settled`.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import Any

from fpdf import FPDF
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import EmployeePayrollDetails, FnfSettlement, PayrollLoan, Payslip, User
from . import company as company_svc
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_fnf_out as out
from . import payroll_runs as runs
from . import payslip_pdf as pp
from .license import get_company

WATERMARK = "DRAFT - NOT FINAL"
SALARY_SKIP = {"LEAVE_ENCASH", "BONUS", "INCENTIVE", "OTHER_EARNING", "ARREARS", "REIMBURSEMENT", "OVERTIME"}
STATUTORY = {"PF", "ESI", "PF_ER_REC", "ESI_ER_REC", "PT", "TDS"}
LOAN_CODES = {"LOAN_EMI", "ADVANCE_RECOVERY"}


def filename_for(view: dict[str, Any]) -> str:
    emp = re.sub(r"[^A-Za-z0-9._-]", "_", str(view["employee"].get("employee_id") or "EMP"))[:32]
    return f"fnf-{emp}-{view['settlement_period']}.pdf"


def watermark_for(status: str) -> str | None:
    return None if status == "settled" else WATERMARK


def _group(earnings: list[dict[str, Any]], deductions: list[dict[str, Any]], month_label: str) -> list[dict[str, Any]]:
    def pick(rows: list[dict[str, Any]], pred: Any) -> list[tuple[str, str]]:
        return [(r["label"], r["amount"]) for r in rows if pred(r["code"])]

    groups = [
        ("Salary for " + month_label, "earning", pick(earnings, lambda c: c not in SALARY_SKIP)),
        ("Leave encashment", "earning", pick(earnings, lambda c: c == "LEAVE_ENCASH")),
        ("Other earnings", "earning", pick(earnings, lambda c: c in SALARY_SKIP and c != "LEAVE_ENCASH")),
        ("Notice recovery", "deduction", pick(deductions, lambda c: c == "NOTICE_RECOVERY")),
        ("Loan recovery", "deduction", pick(deductions, lambda c: c in LOAN_CODES)),
        ("Statutory deductions", "deduction", pick(deductions, lambda c: c in STATUTORY)),
        ("Other deductions", "deduction", pick(deductions, lambda c: c not in STATUTORY | LOAN_CODES | {"NOTICE_RECOVERY"})),
    ]
    return [{"title": t, "kind": k, "lines": ls} for t, k, ls in groups if ls]


async def build_view(session: AsyncSession, f: FnfSettlement) -> tuple[dict[str, Any], str, bytes | None]:
    """(view, footer, logo png). Lines come from the final payslip once settled, else from the estimate (same data as `preview`)."""
    user = await session.get(User, f.user_id)
    det = await session.get(EmployeePayrollDetails, f.user_id)
    settings = await out.settings_of(session)
    structs = await out.structures_of(session, f.user_id)
    today = await runs._today(session)
    earnings: list[dict[str, Any]] = []
    deductions: list[dict[str, Any]] = []
    gross = ded = net = Decimal("0.00")
    p: Payslip | None = None
    if f.status == "settled" and f.final_payslip_id:
        p = await session.get(Payslip, f.final_payslip_id)
    elif f.status == "approved":
        est0 = await out.payslip_estimate(session, f)
        p = est0["payslip"] if est0 else None
    if p is not None and p.result:
        earnings, deductions = p.result["earnings"], p.result["deductions"]
        gross, ded, net = Decimal(p.result["gross"]), Decimal(p.result["total_deductions"]), Decimal(p.result["net"])
    else:
        extra: list[dict[str, Any]] = []
        if f.status in ("draft", "submitted"):
            extra, _flags = await out.planned_lines(session, f, user, structs, settings)
        est = await out.estimate(session, f, user, det, extra, today)
        if est["result"] is not None:
            res = est["result"]
            earnings, deductions, gross, ded, net = res.earnings, res.deductions, res.gross, res.total_deductions, res.net
    plan = out.plan_of(f)
    outside: list[dict[str, str]] = []
    for x in plan["loans"]:
        if x["mode"] == "outside":
            lo = await session.get(PayrollLoan, uuid.UUID(str(x["loan_id"])))
            outside.append(
                {"loan": lo.reference if lo else "", "paid_on": x.get("paid_on") or "", "reference": x.get("reference") or ""}
            )
    from . import payroll_fnf_calc as fc

    n = fc.notice_for(
        f.exit_reason, f.resignation_date, f.last_working_day, f.notice_required_days, waived=f.notice_waived,
        override_days=(plan["override"] or {}).get("days"),
    )  # fmt: skip
    company = await get_company(session)
    period_s = pc.period_str(f.settlement_period)
    view = {
        "company": {"name": company.name or "", "address": (settings.get("payroll.company_address") or None)},
        "employee": {
            "employee_id": user.employee_id, "full_name": user.full_name,
            "department": user.department.name if user.department else None,
            "designation": det.designation if det else None,
            "join_date": user.join_date.isoformat() if user.join_date else None,
        },
        "settlement_period": period_s, "status": f.status,
        "resignation_date": f.resignation_date.isoformat() if f.resignation_date else None,
        "last_working_day": f.last_working_day.isoformat(), "exit_reason": f.exit_reason,
        "notice": {"required": n.required, "served": n.served, "shortfall": f.shortfall_days, "waived": f.notice_waived},
        "groups": _group(earnings, deductions, pp.period_label(period_s)),
        "outside_loans": outside,
        "totals": {"gross": calc.money_str(gross), "deductions": calc.money_str(ded), "net": calc.money_str(net),
                   "net_words": calc.amount_in_words(net) if net >= 0 else ""},
        "gratuity": out.gratuity_info(f, user, structs, settings),
    }  # fmt: skip
    logo = await company_svc.logo_bytes(session)
    return view, str(settings.get("payroll.payslip_footer") or ""), (logo[0] if logo else None)


class _Doc(FPDF):
    watermark: str | None = None

    def header(self) -> None:
        if not self.watermark:
            return
        with self.rotation(45, x=105, y=150):
            self.set_font("DejaVu", "B", 44)
            self.set_text_color(225, 225, 225)
            w = self.get_string_width(self.watermark)
            self.text(105 - w / 2, 150, self.watermark)
        self.set_text_color(0, 0, 0)

    def footer(self) -> None:
        pass


def _row(pdf: FPDF, label: str, value: str | None, w1: float = 52.0) -> None:
    pdf.set_font("DejaVu", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(w1, 5.2, pp.safe(label))
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 5.2, pp._fit(pdf, value or "-", 180 - w1), new_x="LMARGIN", new_y="NEXT")


def render(view: dict[str, Any], footer: str, logo: bytes | None, watermark: str | None) -> bytes:
    pdf = _Doc(orientation="P", unit="mm", format="A4")
    pdf.watermark = watermark
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_font("DejaVu", "", str(pp.REGULAR))
    pdf.add_font("DejaVu", "B", str(pp.BOLD))
    pdf.set_title(f"Final settlement {view['settlement_period']}")
    pdf.set_creator("Employee Connect")
    pdf.set_producer("Employee Connect")
    pdf.set_creation_date(dt.datetime.now(dt.UTC))
    pdf.add_page()
    width = 180.0
    company = view["company"]
    pdf.set_font("DejaVu", "B", 14)
    pdf.cell(width, 7, pp._fit(pdf, company.get("name") or "", width), new_x="LMARGIN", new_y="NEXT")
    if company.get("address"):
        pdf.set_font("DejaVu", "", 8)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(width, 4, pp.safe(company["address"]), max_line_height=4, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
    pdf.ln(2)
    pdf.set_draw_color(150, 150, 150)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("DejaVu", "B", 12)
    pdf.cell(width, 7, "Full and final settlement statement", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    e = view["employee"]
    for label, value in (
        ("Employee ID", e.get("employee_id")), ("Name", e.get("full_name")), ("Department", e.get("department")),
        ("Designation", e.get("designation")), ("Date of joining", e.get("join_date")),
        ("Resignation date", view.get("resignation_date")), ("Last working day", view["last_working_day"]),
        ("Reason for exit", str(view["exit_reason"]).replace("_", " ").title()),
        ("Settlement month", pp.period_label(view["settlement_period"])),
    ):  # fmt: skip
        _row(pdf, label, value)
    pdf.ln(2)
    n = view["notice"]
    pdf.set_font("DejaVu", "B", 10)
    pdf.cell(width, 6, "Notice period", new_x="LMARGIN", new_y="NEXT")
    served = "-" if n["served"] is None else f"{n['served']} days"
    _row(pdf, "Required", f"{n['required']} days")
    _row(pdf, "Served", served)
    _row(pdf, "Shortfall recovered", "Waived" if n["waived"] else f"{n['shortfall']} days")
    pdf.ln(2)
    for g in view["groups"]:
        pdf.set_font("DejaVu", "B", 9)
        pdf.set_fill_color(31, 78, 120)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(width - 40, 6.5, "  " + pp.safe(g["title"]), fill=True)
        pdf.cell(40, 6.5, "Amount (INR)  ", align="R", fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("DejaVu", "", 9)
        sign = "-" if g["kind"] == "deduction" else ""
        for label, amount in g["lines"]:
            pdf.cell(width - 40, 5.5, "  " + pp._fit(pdf, label, width - 46))
            pdf.cell(40, 5.5, sign + pp.inr(amount) + "  ", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)
    if view["outside_loans"]:
        pdf.set_font("DejaVu", "B", 9)
        pdf.cell(width, 6, "Loans settled outside payroll", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 9)
        for x in view["outside_loans"]:
            pdf.cell(width, 5.2, "  " + pp._fit(pdf, f"{x['loan']}  paid on {x['paid_on']}  ref. {x['reference']}", width - 4),
                     new_x="LMARGIN", new_y="NEXT")  # fmt: skip
        pdf.ln(1.5)
    t = view["totals"]
    pdf.set_fill_color(232, 240, 250)
    pdf.set_font("DejaVu", "B", 12)
    pdf.cell(width / 2, 8, "  Net payable", fill=True)
    pdf.cell(width / 2, 8, f"{pp.RUPEE} {pp.inr(t['net'])}  ", align="R", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 8.5)
    pdf.cell(width, 5, "  " + pp.safe(t["net_words"]), new_x="LMARGIN", new_y="NEXT")
    g = view.get("gratuity")
    if g:
        pdf.ln(3)
        pdf.set_font("DejaVu", "B", 9)
        pdf.cell(width, 6, "Gratuity (for information)", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 8.5)
        s = g["service"]
        line = f"Service {s['years']}y {s['months']}m {s['days']}d, eligible: {'yes' if g['eligible'] else 'no'}, estimate INR {pp.inr(g['amount'])}."
        pdf.multi_cell(width, 4.5, pp.safe(line), max_line_height=4.5, new_x="LMARGIN", new_y="NEXT")
        pdf.multi_cell(width, 4.5, pp.safe(g["note"]), max_line_height=4.5, new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(-18)
    pdf.set_font("DejaVu", "", 7.5)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(width, 4, pp._fit(pdf, footer, width), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(width, 4, "Generated on " + dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"), align="C")
    return bytes(pdf.output())


async def build(session: AsyncSession, f: FnfSettlement) -> tuple[bytes, str]:
    view, footer, logo = await build_view(session, f)
    data = await pp.build_pdf(view, footer, logo, watermark_for(f.status), renderer=render)
    return data, filename_for(view)
