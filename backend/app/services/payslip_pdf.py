"""Payslip PDF (plan 12 s.5): generated on demand from the stored snapshot with fpdf2 and a bundled DejaVu Sans font (which has
the Rupee sign). Nothing is written to disk.

* Every user-controlled string (names, labels, address, footer) is drawn as PLAIN text (no markdown / html parsing) and is
  filtered to the glyphs the font has.
* No links, JavaScript or attachments; the metadata holds only the title "Payslip YYYY-MM".
* Built in a worker thread under a 2-slot semaphore and a 10 second budget. A thread cannot be killed, so on timeout (or when
  the request is cancelled) the slot is released only when the thread really finishes (done-callback): at most 2 builds can
  ever run, however many requests time out.
* The logo is skipped when it is over 1 MB or 4 million pixels.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont
from fpdf import FPDF

from ..core.errors import AppError
from . import payroll_calc as calc

log = logging.getLogger("app.payslip_pdf")

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
REGULAR, BOLD = FONT_DIR / "DejaVuSans.ttf", FONT_DIR / "DejaVuSans-Bold.ttf"
RUPEE = "₹"
SLOTS = 2
SLOT_WAIT_S = 5
BUILD_TIMEOUT_S = 10
MAX_LOGO_BYTES = 1_000_000
MAX_LOGO_PIXELS = 4_000_000  # decoded size is bounded before the image is handed to the PDF library
MONTHS = "January February March April May June July August September October November December".split()

_sem: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _semaphore() -> asyncio.Semaphore:
    global _sem
    loop = asyncio.get_running_loop()
    if _sem is None or _sem[0] is not loop:
        _sem = (loop, asyncio.Semaphore(SLOTS))
    return _sem[1]


@lru_cache(maxsize=1)
def _glyphs() -> frozenset[int]:
    return frozenset(TTFont(str(REGULAR), lazy=True).getBestCmap())


def has_rupee_glyph() -> bool:
    return ord(RUPEE) in _glyphs()


def safe(text: Any) -> str:
    """Drop characters the font cannot draw (and control characters) so a stray emoji or script never breaks the page."""
    if text is None:
        return ""
    g = _glyphs()
    return "".join(c if (ord(c) in g and c.isprintable()) else ("?" if c.strip() else " ") for c in str(text))


def inr(amount: str | Decimal | None) -> str:
    """Indian digit grouping: 1,25,000.00"""
    if amount is None:
        return ""
    d = Decimal(amount).quantize(Decimal("0.01"))
    sign = "-" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join([*parts, tail])
    return f"{sign}{whole}.{frac}"


def period_label(period: str) -> str:
    y, m = period.split("-")
    return f"{MONTHS[int(m) - 1]} {y}"


def watermark_for(status: str) -> str | None:
    if status == "published":
        return None
    return "SUPERSEDED" if status == "superseded" else "DRAFT - NOT VALID"


def filename_for(view: dict[str, Any]) -> str:
    import re

    emp = re.sub(r"[^A-Za-z0-9._-]", "_", str(view["employee"].get("employee_id") or "EMP"))[:32]
    suffix = f"-r{view['revision']}" if view["revision"] > 1 else ""
    return f"payslip-{emp}-{view['period']}{suffix}.pdf"


class _Pdf(FPDF):
    def footer(self) -> None:  # no automatic footer
        pass


def _fit(pdf: FPDF, text: str, width: float) -> str:
    t = safe(text)
    if pdf.get_string_width(t) <= width:
        return t
    while t and pdf.get_string_width(t + "...") > width:
        t = t[:-1]
    return t + "..."


def render(view: dict[str, Any], footer: str, logo: bytes | None, watermark: str | None) -> bytes:
    pdf = _Pdf(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(False)
    pdf.set_margins(15, 15, 15)
    pdf.add_font("DejaVu", "", str(REGULAR))
    pdf.add_font("DejaVu", "B", str(BOLD))
    pdf.set_title(f"Payslip {view['period']}")
    pdf.set_creator("Employee Connect")
    pdf.set_producer("Employee Connect")
    pdf.set_creation_date(dt.datetime.now(dt.UTC))
    pdf.add_page()
    left, width = 15.0, 180.0

    if watermark:
        with pdf.rotation(45, x=105, y=150):
            pdf.set_font("DejaVu", "B", 54)
            pdf.set_text_color(225, 225, 225)
            w = pdf.get_string_width(watermark)
            pdf.text(105 - w / 2, 150, watermark)

    # ---- header
    pdf.set_text_color(0, 0, 0)
    x_text, y = left, 15.0
    if logo:
        try:
            from PIL import Image

            if len(logo) > MAX_LOGO_BYTES:
                raise ValueError("logo too large")
            with Image.open(io.BytesIO(logo)) as im:
                if im.width * im.height > MAX_LOGO_PIXELS:
                    raise ValueError("logo has too many pixels")
                ratio = im.width / max(1, im.height)
            lw = min(40.0, 16.0 * ratio)
            pdf.image(io.BytesIO(logo), x=left, y=y, w=lw, h=lw / ratio)
            x_text = left + lw + 4
        except Exception:  # noqa: BLE001 - a bad logo must not block the payslip
            log.warning("payslip logo could not be drawn")
    company = view["company"]
    pdf.set_xy(x_text, y)
    pdf.set_font("DejaVu", "B", 14)
    pdf.cell(left + width - x_text, 7, _fit(pdf, company.get("name") or "", left + width - x_text))
    if company.get("address"):
        pdf.set_xy(x_text, y + 7)
        pdf.set_font("DejaVu", "", 8)
        pdf.set_text_color(90, 90, 90)
        pdf.multi_cell(left + width - x_text, 4, safe(company["address"]), max_line_height=4)
        pdf.set_text_color(0, 0, 0)
    y = 36.0
    pdf.set_draw_color(150, 150, 150)
    pdf.line(left, y, left + width, y)
    y += 3
    pdf.set_xy(left, y)
    pdf.set_font("DejaVu", "B", 12)
    pdf.cell(width, 7, f"Payslip for {period_label(view['period'])}", align="C")
    y += 10

    # ---- employee block
    e = view["employee"]
    bank = " ".join(x for x in (e.get("bank_name"), e.get("bank_account")) if x)
    pairs_l = [
        ("Employee ID", e.get("employee_id")), ("Name", e.get("full_name")), ("Department", e.get("department")),
        ("Designation", e.get("designation")), ("Date of joining", e.get("join_date")),
    ]  # fmt: skip
    pairs_r = [
        ("PAN", e.get("pan")), ("UAN", e.get("uan")), ("ESI IP No.", e.get("esi_ip_number")),
        ("Bank", bank or None), ("Payment mode", str(e.get("payment_mode", "")).replace("_", " ").title()),
    ]  # fmt: skip
    pdf.set_font("DejaVu", "", 9)
    for i in range(5):
        for x, pairs in ((left, pairs_l), (left + 92, pairs_r)):
            label, value = pairs[i]
            pdf.set_xy(x, y)
            pdf.set_text_color(90, 90, 90)
            pdf.cell(28, 5, label)
            pdf.set_text_color(0, 0, 0)
            pdf.cell(60, 5, _fit(pdf, value or "-", 60))
        y += 5.5
    y += 2
    d = view["days"]
    pdf.set_xy(left, y)
    pdf.set_fill_color(240, 243, 248)
    pdf.cell(
        width, 6.5,
        f"  Days in period: {d['basis_days']:g}      Loss of pay: {d['lop_days']:g}      Paid days: {d['paid_days']:g}",
        fill=True,
    )  # fmt: skip
    y += 11

    totals = view.get("totals")
    if totals is None:
        pdf.set_xy(left, y)
        pdf.set_font("DejaVu", "B", 10)
        pdf.cell(width, 8, "This payslip has not been calculated yet (see the errors on the payroll screen).")
    else:
        # ---- earnings | deductions
        col_w, gap = 87.0, 6.0
        xs = (left, left + col_w + gap)
        heads = ("Earnings", "Deductions")
        tables = (
            [(r["label"], r["amount"]) for r in view["earnings"]],
            [(r["label"], r["amount"]) for r in view["deductions"]],
        )
        pdf.set_font("DejaVu", "B", 9)
        pdf.set_fill_color(31, 78, 120)
        pdf.set_text_color(255, 255, 255)
        for x, h in zip(xs, heads, strict=True):
            pdf.set_xy(x, y)
            pdf.cell(col_w - 32, 6.5, "  " + h, fill=True)
            pdf.cell(32, 6.5, "Amount (INR)  ", align="R", fill=True)
        y += 7.5
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("DejaVu", "", 9)
        for i in range(max(len(tables[0]), len(tables[1]))):
            for x, rows in zip(xs, tables, strict=True):
                if i < len(rows):
                    pdf.set_xy(x, y)
                    pdf.cell(col_w - 32, 5.5, "  " + _fit(pdf, rows[i][0], col_w - 36))
                    pdf.cell(32, 5.5, inr(rows[i][1]) + "  ", align="R")
            y += 5.5
        pdf.set_draw_color(150, 150, 150)
        pdf.line(left, y + 1, left + width, y + 1)
        y += 2.5
        pdf.set_font("DejaVu", "B", 9)
        for x, label, val in (
            (xs[0], "Gross earnings", totals["gross_earnings"]),
            (xs[1], "Total deductions", totals["total_deductions"]),
        ):
            pdf.set_xy(x, y)
            pdf.cell(col_w - 32, 6, "  " + label)
            pdf.cell(32, 6, inr(val) + "  ", align="R")
        y += 10
        # ---- net pay
        pdf.set_fill_color(232, 240, 250)
        pdf.rect(left, y, width, 17, style="F")
        pdf.set_xy(left, y + 2)
        pdf.set_font("DejaVu", "B", 12)
        pdf.cell(width / 2, 6, "  Net pay")
        pdf.cell(width / 2, 6, f"{RUPEE} {inr(totals['net_pay'])}  ", align="R")
        pdf.set_xy(left, y + 9.5)
        pdf.set_font("DejaVu", "", 8.5)
        pdf.cell(width, 5, "  " + safe(totals["net_pay_words"]))
        y += 21
        if view.get("employer_contributions"):
            pdf.set_xy(left, y)
            pdf.set_font("DejaVu", "B", 9)
            recovered = any(d["code"] in calc.RECOVERED_CODES for d in view.get("deductions", []))
            pdf.cell(
                width,
                5.5,
                "Employer contributions (recovered from pay, see deductions)"
                if recovered
                else "Employer contributions (not deducted from pay)",
            )
            y += 5.5
            pdf.set_font("DejaVu", "", 9)
            for r in view["employer_contributions"]:
                pdf.set_xy(left, y)
                pdf.cell(60, 5, "  " + safe(r["label"]))
                pdf.cell(32, 5, inr(r["amount"]), align="R")
                y += 5
            y += 2

    info = view.get("revision_info")
    if info and view["revision"] > 1:
        when = (view.get("supersedes_published_at") or "")[:10]
        pdf.set_xy(left, y)
        pdf.set_font("DejaVu", "", 8.5)
        note = f"Revision {view['revision']}" + (f" - supersedes the payslip published on {when}" if when else "")
        if info.get("reason"):
            note += f". Reason: {info['reason']}"
        pdf.multi_cell(width, 4.5, safe(note), max_line_height=4.5)

    # ---- footer
    pdf.set_xy(left, 280)
    pdf.set_font("DejaVu", "", 7.5)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(width, 4, _fit(pdf, footer, width), align="C")
    pdf.set_xy(left, 284)
    pdf.cell(width, 4, "Generated on " + dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"), align="C")
    return bytes(pdf.output())


async def build_pdf(view: dict[str, Any], footer: str, logo: bytes | None, watermark: str | None) -> bytes:
    """Bounded: 2 concurrent builds, 5 s to get a slot (else 503 + Retry-After), 10 s to build."""
    sem = _semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), SLOT_WAIT_S)
    except TimeoutError:
        raise AppError(
            "TEMPORARILY_UNAVAILABLE", "The PDF service is busy; try again shortly.", headers={"Retry-After": "10"}
        ) from None
    fut = asyncio.ensure_future(asyncio.to_thread(render, view, footer, logo, watermark))

    def _done(f: asyncio.Future) -> None:
        sem.release()  # only now is the worker thread really free
        if not f.cancelled():
            f.exception()  # mark retrieved: a result nobody awaits any more must not log "never retrieved"

    fut.add_done_callback(_done)
    try:
        return await asyncio.wait_for(asyncio.shield(fut), BUILD_TIMEOUT_S)
    except TimeoutError:
        log.error("payslip pdf build exceeded %ss", BUILD_TIMEOUT_S)
        raise AppError(
            "TEMPORARILY_UNAVAILABLE", "The PDF service is busy; try again shortly.", headers={"Retry-After": "10"}
        ) from None
