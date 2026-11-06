"""TDS engine, pure part (plan 17 work package 5c, decisions T6 / T7, contract 19.9).

`compute(rules, inp)` takes the VERIFIED rules JSON (see `tax_rules.validate_rules`) and the figures of one employee and returns the
annual tax computation and the TDS to deduct this month. There is no database, no clock and no I/O here, and NO tax constant:
slabs, rebate, surcharge, standard deduction, cess, rounding steps, the no-PAN rate, section limits, group limits and the HRA percentages
all come from `rules`. (The only literals are structural: 100 for percentages, 12 months a year, and the section code
`OTHER_INCOME`, which marks the "other income" declaration section because the rules schema has no field for it; see `section_kind`.)

Method (average rate, s.192 as described in plan 17 T7), all amounts are `Decimal` rupees:
  taxable salary of the FY = taxable earnings of the payslips already paid + this month + the structure in force for the remaining
  months; less HRA exemption (least of three, per month, only in a regime whose rules allow it), standard deduction, professional tax
  (when the regime allows it), Chapter VI-A deductions within section and group limits; plus previous-employer income and (when asked)
  other income. Tax by slabs, rebate with marginal relief, surcharge with marginal relief, cess, rounding per rules.
  Monthly TDS = (annual tax - TDS already deducted - previous employer TDS) / remaining months, never negative (`TDS_EXCESS` when the
  employee was over-deducted), plus the extra tax caused by one-time earnings of this month (when `one_time_in_month`).
  Without PAN the TDS is at least `no_pan_min_rate_pct` (or the average rate when that is higher) of this month's taxable earnings.
Proofs: declared amounts count until the proof month; from then on a section that needs a proof counts only its VERIFIED amount.
Previous-employer income, TDS and PT (Form 12B) count ONLY when the proof is verified, whatever the month (flag `PREV_EMPLOYER_UNVERIFIED`
while a declared item has no verified proof); the TDS of an item never exceeds its income.

This is a computation aid. The figures are only as good as the verified rules and the declaration; they are not legal advice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

ZERO = Decimal("0.00")
HUNDRED = Decimal(100)
MONTHS_IN_YEAR = 12
OTHER_INCOME_SECTION = "OTHER_INCOME"  # code of the "other income" section (not a tax value; the rules schema cannot mark it)
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


# ============================================================================================ small helpers
def q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money(x: Decimal) -> str:
    return f"{q2(x):.2f}"


def rupee(x: Decimal) -> Decimal:
    return q2(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def round_to(x: Decimal, step: int) -> Decimal:
    """Round half up to a multiple of `step` (the rules' `round_income_to` / `round_tax_to`)."""
    if step <= 1:
        return rupee(x)
    return q2((x / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step)


def fy_months(fy: str) -> list[str]:
    """`YYYY-MM` of the twelve months of the FY, April first."""
    y = int(fy[:4])
    return [f"{y}-{m:02d}" for m in range(4, 13)] + [f"{y + 1}-{m:02d}" for m in range(1, 4)]


def months_between(a: str, b: str) -> list[str]:
    ma, mb = _MONTH_RE.match(a), _MONTH_RE.match(b)
    if not ma or not mb:
        return []
    y, m = int(ma.group(1)), int(ma.group(2))
    ey, em = int(mb.group(1)), int(mb.group(2))
    out: list[str] = []
    while (y, m) <= (ey, em) and len(out) < 600:
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def treatment_of(treatment: dict[str, str], code: str) -> str:
    return treatment.get(code, treatment.get("default", "taxable"))


def taxable_part(treatment: dict[str, str], code: str, amount: Decimal) -> Decimal:
    """The taxable part of one earning line under `rules.component_treatment` (`exempt` lines are not taxed)."""
    return ZERO if treatment_of(treatment, code) == "exempt" else amount


def section_kind(sec: dict[str, Any]) -> str:
    """How the engine treats a declaration section. Derived from the section's declared fields (no schema change in 5b):
    rent fields -> `rent` (HRA exemption), an `income` field -> `prev_employer`, the code OTHER_INCOME -> `other_income`,
    anything else -> `deduction` (Chapter VI-A style, limited by `limit` / `senior_limit` / group limits)."""
    fields = set(sec.get("fields", []))
    if {"monthly_rent", "from_month", "to_month"} <= fields:
        return "rent"
    if "income" in fields:
        return "prev_employer"
    if sec.get("code") == OTHER_INCOME_SECTION:
        return "other_income"
    return "deduction"


# ============================================================================================ input
@dataclass(frozen=True)
class MonthWage:
    """Basic and HRA received in one month of the FY (paid, this month, or projected from the structure), for the HRA exemption."""

    month: str
    basic: Decimal
    hra: Decimal


@dataclass
class EngineInput:
    fy: str
    period: str  # YYYY-MM, the month being computed
    declared_regime: str | None  # from the SUBMITTED declaration; None = nothing declared
    has_declaration: bool
    declaration_version: int | None
    items: list[dict[str, Any]]  # SUBMITTED declaration items
    verified: dict[str, Decimal]  # declaration item id -> verified proof amount
    proofs_from: str  # YYYY-MM: from this month on, sections that need a proof count only verified amounts
    salary_paid: Decimal  # taxable earnings of payslips already paid in this FY
    salary_this_month: Decimal  # regular (structure) taxable earnings of this month
    one_time_this_month: Decimal  # one-time taxable earnings of this month (bonus, arrears, ...)
    salary_projected: Decimal  # structure in force x remaining months after this one
    pt_paid: Decimal  # professional tax deducted in earlier months plus this month
    wages: list[MonthWage]
    tds_paid: Decimal  # TDS already deducted by this employer in this FY (earlier months)
    remaining_months: int  # including this month
    include_other_income: bool = False
    one_time_in_month: bool = True
    has_pan: bool = True


# ============================================================================================ tax on an income
def slab_tax(slabs: list[dict[str, Any]], income: Decimal) -> Decimal:
    tax = ZERO
    for s in slabs:
        lo = Decimal(s["from"])
        if income <= lo:
            break
        hi = None if s["to"] is None else Decimal(s["to"])
        top = income if hi is None else min(income, hi)
        tax += (top - lo) * Decimal(s["rate_pct"]) / HUNDRED
    return q2(tax)


def _after_rebate(reg: dict[str, Any], income: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """(tax on slabs, rebate, tax after rebate). Marginal relief keeps the tax at most the income above the rebate limit."""
    tax = slab_tax(reg["slabs"], income)
    rb = reg["rebate"]
    limit, cap = Decimal(rb["income_limit"]), Decimal(rb["max"])
    rebate = ZERO
    if income <= limit:
        rebate = min(tax, cap)
    elif rb["marginal_relief"]:
        rebate = min(cap, max(ZERO, tax - (income - limit)))
    return tax, q2(rebate), q2(tax - rebate)


def _surcharge_band(reg: dict[str, Any], income: Decimal) -> tuple[Decimal, Decimal | None, Decimal]:
    """(rate %, threshold crossed, rate % of the band below)."""
    rate, thr, prev = ZERO, None, ZERO
    for s in sorted(reg["surcharge"], key=lambda x: Decimal(x["above"])):
        above = Decimal(s["above"])
        if income > above:
            prev, rate, thr = rate, Decimal(s["rate_pct"]), above
    return rate, thr, prev


def _surcharge(reg: dict[str, Any], income: Decimal, after: Decimal) -> Decimal:
    rate, thr, prev = _surcharge_band(reg, income)
    if rate <= 0 or after <= 0:
        return ZERO
    sc = after * rate / HUNDRED
    if reg["surcharge_marginal_relief"] and thr is not None:
        after_t = _after_rebate(reg, thr)[2]
        limit = after_t * (1 + prev / HUNDRED) + (income - thr)
        if after + sc > limit:
            sc = max(ZERO, limit - after)
    return q2(sc)


def tax_for(reg: dict[str, Any], income: Decimal, cess_pct: Decimal, round_tax: int) -> dict[str, Decimal]:
    tax, rebate, after = _after_rebate(reg, income)
    sc = _surcharge(reg, income, after)
    cess = q2((after + sc) * cess_pct / HUNDRED)
    return {
        "tax_on_income": tax, "rebate": rebate, "surcharge": sc, "cess": cess,
        "total": round_to(after + sc + cess, round_tax),
    }  # fmt: skip


# ============================================================================================ declaration items
@dataclass
class _Counted:
    item: dict[str, Any]
    section: dict[str, Any]
    kind: str
    amount: Decimal  # counted amount (declared, or verified-capped)
    factor: Decimal  # counted / declared, applied to the money details of the item


def _count_items(rules: dict[str, Any], inp: EngineInput, basis: str) -> list[_Counted]:
    sections = {s["code"]: s for s in rules.get("sections", [])}
    out: list[_Counted] = []
    for it in inp.items:
        sec = sections.get(it.get("section"))
        if sec is None:
            continue
        try:
            declared = Decimal(str(it.get("amount", "0")))
        except Exception:  # noqa: BLE001 - a malformed stored amount counts as nothing
            continue
        if declared <= 0:
            continue
        counted = declared
        kind = section_kind(sec)
        if kind == "prev_employer" or (basis == "verified" and sec.get("proof_required")):
            v = inp.verified.get(it.get("id"))
            counted = min(declared, v) if v is not None else ZERO
        out.append(_Counted(it, sec, kind, q2(counted), counted / declared))
    return out


def prev_employer_unverified(rules: dict[str, Any], inp: EngineInput) -> bool:
    """A declared previous-employer item that has no verified proof (its figures are NOT counted)."""
    sections = {s["code"]: s for s in rules.get("sections", [])}
    for it in inp.items:
        sec = sections.get(it.get("section"))
        if sec is None or section_kind(sec) != "prev_employer" or it.get("id") in inp.verified:
            continue
        try:
            if Decimal(str(it.get("amount", "0"))) > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _scaled(c: _Counted, key: str) -> Decimal:
    try:
        return q2(Decimal(str((c.item.get("details") or {}).get(key, "0"))) * c.factor)
    except Exception:  # noqa: BLE001
        return ZERO


def _deductions(counted: list[_Counted], rules: dict[str, Any], allowed: set[str]) -> tuple[Decimal, list[dict[str, str]]]:
    per: dict[str, Decimal] = {}
    senior: dict[str, bool] = {}
    for c in counted:
        if c.kind != "deduction" or c.section["code"] not in allowed:
            continue
        code = c.section["code"]
        per[code] = per.get(code, ZERO) + c.amount
        senior[code] = senior.get(code, False) or (c.item.get("details") or {}).get("senior") is True
    capped: dict[str, Decimal] = {}
    for code, amount in per.items():
        sec = next(s for s in rules["sections"] if s["code"] == code)
        limit = sec.get("senior_limit") if senior[code] and sec.get("senior_limit") else sec.get("limit")
        capped[code] = min(amount, Decimal(limit)) if limit is not None else amount
    total = ZERO
    grouped: set[str] = set()
    for g in rules.get("group_limits", []):
        members = [s["code"] for s in rules["sections"] if s.get("group") == g["group"] and s["code"] in capped]
        grouped.update(members)
        total += min(sum((capped[m] for m in members), ZERO), Decimal(g["limit"]))
    total += sum((v for k, v in capped.items() if k not in grouped), ZERO)
    steps = [{"section": k, "declared_or_verified": money(per[k]), "allowed": money(capped[k])} for k in sorted(per)]
    return q2(total), steps


def _hra(counted: list[_Counted], rules: dict[str, Any], allowed: set[str], wages: dict[str, MonthWage]) -> Decimal:
    hra = rules.get("hra")
    if not hra:
        return ZERO
    metros = {c.strip().lower() for c in hra["metro_cities"]}
    left = {m: w.hra for m, w in wages.items()}
    total = ZERO
    for c in counted:
        if c.kind != "rent" or c.section["code"] not in allowed:
            continue
        d = c.item.get("details") or {}
        rent = _scaled(c, "monthly_rent")
        metro = d.get("metro") is True or str(d.get("city") or "").strip().lower() in metros
        pct = Decimal(hra["metro_pct"] if metro else hra["non_metro_pct"])
        for m in months_between(str(d.get("from_month", "")), str(d.get("to_month", ""))):
            w = wages.get(m)
            if w is None:
                continue
            least = min(left[m], rent - Decimal(hra["rent_minus_basic_pct"]) / HUNDRED * w.basic, pct / HUNDRED * w.basic)
            e = max(ZERO, least)
            left[m] -= e
            total += e
    return q2(total)


# ============================================================================================ compute
def compute(rules: dict[str, Any], inp: EngineInput, regime: str | None = None) -> dict[str, Any]:
    """The computation of contract 19.9 (`regime`, `annual`, `suggested`, `flags`, ...) without the run-level fields (mode, rules id,
    applied, override), which the integration adds. `regime` forces a regime (comparison); otherwise the declared one, else the rules'
    `default_regime`."""
    chosen = regime or inp.declared_regime or rules["default_regime"]
    source = "default" if (regime is None and inp.declared_regime is None) else "declared"
    reg = rules["regimes"][chosen]
    basis = "verified" if inp.period >= inp.proofs_from else "declared"
    allowed = set(reg["allowed_sections"])
    counted = _count_items(rules, inp, basis)
    wages = {w.month: w for w in inp.wages}

    chapter_via, section_steps = _deductions(counted, rules, allowed)
    hra_ex = _hra(counted, rules, allowed, wages) if reg["hra_exemption"] else ZERO
    prev_income = q2(sum((_scaled(c, "income") for c in counted if c.kind == "prev_employer"), ZERO))
    prev_tds = q2(
        sum((min(_scaled(c, "tds"), _scaled(c, "income")) for c in counted if c.kind == "prev_employer"), ZERO)
    )  # tds <= income
    prev_pt = q2(sum((_scaled(c, "pt") for c in counted if c.kind == "prev_employer"), ZERO))
    other = q2(sum((c.amount for c in counted if c.kind == "other_income"), ZERO)) if inp.include_other_income else ZERO
    pt_total = q2(inp.pt_paid + prev_pt) if reg["pt_deductible"] else ZERO
    cess_pct = Decimal(rules["cess_pct"])

    def pack(with_one_time: bool) -> dict[str, Any]:
        salary = (
            inp.salary_paid + inp.salary_this_month + inp.salary_projected + (inp.one_time_this_month if with_one_time else ZERO)
        )
        exemptions = min(hra_ex, salary)
        sd = max(ZERO, min(Decimal(reg["standard_deduction"]), salary + prev_income - exemptions))
        taxable = max(ZERO, salary + prev_income + other - exemptions - sd - pt_total - chapter_via)
        taxable = round_to(taxable, int(rules["round_income_to"]))
        t = tax_for(reg, taxable, cess_pct, int(rules["round_tax_to"]))
        return {"salary": salary, "exemptions": exemptions, "sd": q2(sd), "taxable": taxable, **t}

    full = pack(True)
    one_time = inp.one_time_this_month > 0 and inp.one_time_in_month
    base = pack(False) if one_time else full
    one_time_tax = max(ZERO, full["total"] - base["total"]) if one_time else ZERO

    paid_all = q2(inp.tds_paid + prev_tds)
    n = max(1, inp.remaining_months)
    raw = (base["total"] - paid_all) / n + one_time_tax
    excess = raw < 0
    suggested = ZERO if raw <= 0 else rupee(raw)

    flags: list[str] = []
    if source == "default":
        flags.append("REGIME_DEFAULT")
    if not inp.has_declaration:
        flags.append("NO_DECLARATION")
    if excess:
        flags.append("TDS_EXCESS")
    if prev_employer_unverified(rules, inp):
        flags.append("PREV_EMPLOYER_UNVERIFIED")

    month_taxable = inp.salary_this_month + inp.one_time_this_month
    average_rate = (full["total"] / full["taxable"] * HUNDRED) if full["taxable"] > 0 else ZERO
    if not inp.has_pan:
        flags.append("NO_PAN_206AA")
        floor = rupee(max(average_rate, Decimal(rules["no_pan_min_rate_pct"])) / HUNDRED * month_taxable)
        suggested = max(suggested, floor)

    return {
        "regime": chosen,
        "regime_source": source,
        "declaration_version": inp.declaration_version,
        "proof_basis": basis,
        "remaining_months": n,
        "annual": {
            "salary_paid": money(inp.salary_paid),
            "salary_this_month": money(inp.salary_this_month + inp.one_time_this_month),
            "salary_projected": money(inp.salary_projected),
            "previous_employer_income": money(prev_income),
            "other_income": money(other),
            "exemptions": money(full["exemptions"]),
            "standard_deduction": money(full["sd"]),
            "pt": money(pt_total),
            "chapter_via": money(chapter_via),
            "taxable_income": money(full["taxable"]),
            "tax_on_income": money(full["tax_on_income"]),
            "rebate": money(full["rebate"]),
            "surcharge": money(full["surcharge"]),
            "cess": money(full["cess"]),
            "total_tax": money(full["total"]),
        },
        "tds_paid_so_far": money(inp.tds_paid),
        "previous_employer_tds": money(prev_tds),
        "remaining_tax": money(max(ZERO, full["total"] - paid_all)),
        "one_time_tax": money(one_time_tax),
        "suggested": money(suggested),
        "flags": flags,
        "steps": {
            "sections": section_steps, "hra_exemption": money(hra_ex), "average_rate_pct": f"{average_rate:.2f}",
            "month_taxable": money(month_taxable), "recurring_total_tax": money(base["total"]),
        },
    }  # fmt: skip
