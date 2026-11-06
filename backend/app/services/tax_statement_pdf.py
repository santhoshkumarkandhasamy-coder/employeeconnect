"""Tax computation statement PDF (plan 17 work package 5c, decision T9, contract 19.9), on the payslip PDF engine.

Working data for HR and the employee, NOT a Form 16 (the watermark says so on every page). Same rules as the payslip PDF: fpdf2 with the
bundled DejaVu font, every user-controlled string drawn as plain text and filtered to the font's glyphs, no links or attachments, built in
a worker thread under the shared 2-slot semaphore (`payslip_pdf.build_pdf`), nothing written to disk. The PAN is masked.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from . import payslip_pdf as pp
from .fnf_pdf import _Doc, _row

WATERMARK = "TAX COMPUTATION - NOT FORM 16"
ANNUAL_ROWS = (
    ("salary_paid", "Taxable salary paid so far"), ("salary_this_month", "Taxable salary, current month"),
    ("salary_projected", "Projected taxable salary, remaining months"), ("previous_employer_income", "Previous employer income"),
    ("other_income", "Other income"), ("exemptions", "Exemptions (HRA)"), ("standard_deduction", "Standard deduction"),
    ("pt", "Professional tax"), ("chapter_via", "Chapter VI-A deductions"), ("taxable_income", "Taxable income"),
    ("tax_on_income", "Tax on income"), ("rebate", "Rebate"), ("surcharge", "Surcharge"), ("cess", "Cess"),
    ("total_tax", "Total tax for the year"),
)  # fmt: skip


def filename_for(view: dict[str, Any]) -> str:
    emp = re.sub(r"[^A-Za-z0-9._-]", "_", str(view["employee"].get("employee_id") or "EMP"))[:32]
    return f"tax-computation-{emp}-{view['fy']}.pdf"


def render(view: dict[str, Any], footer: str, logo: bytes | None, watermark: str | None) -> bytes:
    pdf = _Doc(orientation="P", unit="mm", format="A4")
    pdf.watermark = watermark or WATERMARK
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_font("DejaVu", "", str(pp.REGULAR))
    pdf.add_font("DejaVu", "B", str(pp.BOLD))
    pdf.set_title(f"Tax computation {view['fy']}")
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
    pdf.cell(width, 7, "Tax computation statement", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    e = view["employee"]
    for label, value in (
        ("Employee ID", e.get("employee_id")), ("Name", e.get("full_name")), ("PAN", e.get("pan")),
        ("Department", e.get("department")), ("Designation", e.get("designation")), ("Financial year", view["fy"]),
        ("Tax regime", f"{view['regime']} ({view['regime_source']})"),
    ):  # fmt: skip
        _row(pdf, label, value)
    pdf.ln(2)
    pdf.set_fill_color(31, 78, 120)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("DejaVu", "B", 9)
    pdf.cell(width - 40, 6.5, "  Annual computation (estimate)", fill=True)
    pdf.cell(40, 6.5, "Amount (INR)  ", align="R", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("DejaVu", "", 9)
    for key, label in ANNUAL_ROWS:
        pdf.cell(width - 40, 5.5, "  " + label)
        pdf.cell(40, 5.5, pp.inr(view["annual"][key]) + "  ", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1.5)
    for label, key in (
        ("TDS deducted so far", "tds_paid_so_far"), ("TDS deducted by previous employer", "previous_employer_tds"),
        ("Tax still to be deducted", "remaining_tax"),
    ):  # fmt: skip
        pdf.set_font("DejaVu", "B", 9)
        pdf.cell(width - 40, 5.5, "  " + label)
        pdf.cell(40, 5.5, pp.inr(view[key]) + "  ", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    if view["months"]:
        pdf.set_font("DejaVu", "B", 9)
        pdf.cell(width, 6, "TDS deducted month by month (published payslips)", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 9)
        for m in view["months"]:
            pdf.cell(60, 5.2, "  " + pp.period_label(m["period"]))
            pdf.cell(60, 5.2, "Gross " + pp.inr(m["gross"]), align="R")
            pdf.cell(60, 5.2, "TDS " + pp.inr(m["tds"]) + "  ", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    if view["declarations"]:
        pdf.set_font("DejaVu", "B", 9)
        pdf.cell(width, 6, "Declarations used (submitted)", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 9)
        for d in view["declarations"]:
            pdf.cell(110, 5.2, "  " + pp._fit(pdf, d["name"], 104))
            pdf.cell(35, 5.2, "Declared " + pp.inr(d["declared"]), align="R")
            ver = pp.inr(d["verified"]) if d["verified"] is not None else "-"
            pdf.cell(35, 5.2, "Verified " + ver + "  ", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    pdf.set_font("DejaVu", "", 8.5)
    note = (
        "This statement is working data computed from the verified tax rules (version "
        f"{view['rules_version']}), the submitted declaration and the payslips. It is an estimate and is not a Form 16."
    )
    pdf.multi_cell(width, 4.5, pp.safe(note), max_line_height=4.5, new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(-18)
    pdf.set_font("DejaVu", "", 7.5)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(width, 4, pp._fit(pdf, footer, width), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(width, 4, "Generated on " + dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"), align="C")
    return bytes(pdf.output())


async def build(view: dict[str, Any], footer: str, logo: bytes | None) -> tuple[bytes, str]:
    data = await pp.build_pdf(view, footer, logo, WATERMARK, renderer=render)
    return data, filename_for(view)
