"""CTC calculator and gratuity arithmetic (plan 16 F8 / F10, contract 18.2 / 18.3): PURE functions, no database, no clock.

CTC (monthly) = gross + employer PF + employer ESI - recovered employer share (when `employer_share_from_employee`)
                + gratuity accrual (when `payroll.ctc_include_gratuity`).

The gross is a whole rupee and is split by the company rule (basic % of gross, HRA % of basic, the rest one allowance). The
statutory lines use the same functions as the payslip calculation (`payroll_calc`), so the calculator and a processed payslip
agree for the same gross. `from_ctc` finds the largest whole-rupee gross whose monthly cost fits the target. The cost is
monotonic in the gross inside one ESI regime (ESI applies up to the threshold, not above it) but NOT across the threshold (the
employer ESI drops away), so each regime is searched separately and the larger answer wins.

Gratuity: 15 / 26 x monthly wage x counted years (completed years + 1 when the remainder is more than 6 months), capped.
This is an undiscounted estimate, not an actuarial valuation, and it is never a payslip line.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from . import payroll_calc as calc
from .payroll_calc import money_str, q2

ZERO = Decimal("0.00")
HUNDRED = Decimal(100)
MAX_MONTHLY = Decimal("10000000.00")
MAX_ANNUAL = Decimal("120000000.00")
GRATUITY_FRACTION = Decimal(15) / Decimal(26)


class CtcError(ValueError):
    """The request cannot be answered (for example a target that no gross fits). The message never echoes a value."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def rupee(x: Decimal) -> Decimal:
    """Whole rupee, half-up, as a 2-decimal value."""
    return q2(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ================================================================================================ gratuity (pure)
def add_months(day: dt.date, months: int) -> dt.date:
    """`day` plus whole months, clipped to the end of a shorter month."""
    idx = day.year * 12 + (day.month - 1) + months
    year, month = divmod(idx, 12)
    month += 1
    last = (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month, min(day.day, last))


def service_between(start: dt.date, end: dt.date) -> tuple[int, int, int]:
    """(years, months, days) from `start` up to `end` (the end day is not counted: 2019-06-01 -> 2026-11-30 is 7 y 5 m 29 d).
    An end before the start is (0, 0, 0)."""
    if end <= start:
        return 0, 0, 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if add_months(start, months) > end:
        months -= 1
    days = (end - add_months(start, months)).days
    return months // 12, months % 12, days


def counted_years(years: int, months: int, days: int) -> int:
    """Completed years, plus one when the remainder is more than 6 months."""
    return years + (1 if (months, days) > (6, 0) else 0)


def gratuity_wage(basic: Decimal, gross: Decimal, wage_basis: str) -> Decimal:
    """Monthly wage for gratuity: the basic, or the larger of basic and half the gross (`basic_or_half_gross`)."""
    if wage_basis == "basic_or_half_gross":
        return q2(max(basic, gross / 2))
    return q2(basic)


def monthly_gratuity_accrual(wage: Decimal) -> Decimal:
    """wage x 15 / 26 / 12, two decimals."""
    return q2(wage * 15 / 26 / 12)


@dataclass(frozen=True)
class Gratuity:
    years: int
    months: int
    days: int
    counted: int
    eligible: bool
    accrued: Decimal
    capped: bool


def gratuity(wage: Decimal, start: dt.date, end: dt.date, *, min_years: int, cap: Decimal, death: bool = False) -> Gratuity:
    """accrued = min(cap, whole-rupee(wage x 15 / 26 x counted years)); shown for every employee, `eligible` says whether the
    minimum service (or a death) makes it payable."""
    y, m, d = service_between(start, end)
    n = counted_years(y, m, d)
    raw = rupee(wage * 15 * n / 26)
    capped = raw > cap
    return Gratuity(y, m, d, n, death or y >= min_years, min(cap, raw), capped)


# ================================================================================================ CTC (pure)
@dataclass(frozen=True)
class CtcSettings:
    rules: calc.Rules
    split_basic_pct: int
    split_hra_pct: int
    split_code: str
    split_name: str
    include_gratuity: bool
    wage_basis: str
    # the raw setting values, echoed in the response `rules` block
    raw: dict[str, Any]

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> CtcSettings:
        """`cfg` = payroll settings WITH the `payroll.` prefix (as `payroll_settings.read_all` returns them)."""
        stripped = {k.removeprefix("payroll."): v for k, v in cfg.items()}
        return cls(
            rules=calc.Rules.from_config(stripped),
            split_basic_pct=int(stripped["split_basic_pct"]),
            split_hra_pct=int(stripped["split_hra_pct_of_basic"]),
            split_code=str(stripped["split_allowance_code"]),
            split_name=str(stripped["split_allowance_name"]),
            include_gratuity=bool(stripped["ctc_include_gratuity"]),
            wage_basis=str(stripped["gratuity_wage_basis"]),
            raw=stripped,
        )


@dataclass(frozen=True)
class Options:
    pf_applicable: bool = True
    pf_on_full_basic: bool = False
    esi_applicable: bool = True
    pt_applicable: bool = True
    basic_pct: int = 50
    hra_pct: int = 40
    include_gratuity: bool = True
    employer_share_from_employee: bool = True


def split_gross(gross: Decimal, basic_pct: int, hra_pct_of_basic: int, code: str, name: str) -> dict[str, Any]:
    """Whole-rupee split: basic = basic_pct % of gross, HRA = hra_pct_of_basic % of basic (never above what is left), the rest
    one allowance (omitted when it is 0). basic + hra + allowance == gross always."""
    basic = rupee(gross * basic_pct / HUNDRED)
    hra = min(rupee(basic * hra_pct_of_basic / HUNDRED), q2(gross - basic))
    rest = q2(gross - basic - hra)
    allowances = [{"code": code, "name": name, "amount": money_str(rest)}] if rest > 0 else []
    return {"basic": basic, "hra": hra, "allowances": allowances}


@dataclass(frozen=True)
class Cost:
    gross: Decimal
    basic: Decimal
    hra: Decimal
    allowances: list[dict[str, str]]
    employer_pf: Decimal
    employer_esi: Decimal
    recovered: Decimal
    gratuity: Decimal
    ctc: Decimal
    employee_pf: Decimal
    employee_esi: Decimal
    pt_annual: Decimal
    net_monthly: Decimal
    esi_applies: bool
    pf_ceiling_applied: bool


def cost_of(gross: Decimal, s: CtcSettings, o: Options) -> Cost:
    """Monthly cost of one whole-rupee gross."""
    rules = s.rules
    mode = rules.rounding
    parts = split_gross(gross, o.basic_pct, o.hra_pct, s.split_code, s.split_name)
    basic, hra = parts["basic"], parts["hra"]

    pf_er = pf_ee = ZERO
    ceiling_applied = False
    if o.pf_applicable:
        unlimited = o.pf_on_full_basic or rules.pf_wage_ceiling == 0
        wage = basic if unlimited else min(basic, rules.pf_wage_ceiling)
        ceiling_applied = not unlimited and basic > rules.pf_wage_ceiling
        pf_er = calc.rnd(wage * rules.pf_employer_rate_pct / HUNDRED, mode)
        pf_ee = calc.rnd(wage * rules.pf_employee_rate_pct / HUNDRED, mode)

    esi_er = esi_ee = ZERO
    esi_applies = o.esi_applicable and gross <= rules.esi_wage_threshold
    if esi_applies:  # the whole gross is ESI wage (every allowance of the split is flagged as ESI wage)
        esi_er = calc.ceil_rupee(gross * rules.esi_employer_rate_pct / HUNDRED)
        esi_ee = calc.ceil_rupee(gross * rules.esi_employee_rate_pct / HUNDRED)

    recovered = q2(pf_er + esi_er) if o.employer_share_from_employee else ZERO
    accrual = monthly_gratuity_accrual(gratuity_wage(basic, gross, s.wage_basis)) if o.include_gratuity else ZERO
    ctc = q2(gross + pf_er + esi_er - recovered + accrual)

    pt_annual = ZERO
    if o.pt_applicable:
        pt_annual = q2(sum((calc.professional_tax(rules, m, gross, (gross,) * 5) for m in range(1, 13)), ZERO))
    net = q2(gross - pf_ee - esi_ee - recovered - pt_annual / 12)
    return Cost(
        gross, basic, hra, parts["allowances"], pf_er, esi_er, recovered, accrual, ctc, pf_ee, esi_ee, pt_annual, net,
        esi_applies, ceiling_applied,
    )  # fmt: skip


def _largest_fitting(lo: int, hi: int, target: Decimal, s: CtcSettings, o: Options) -> int | None:
    """Largest g in [lo, hi] with cost(g) <= target, the cost being monotonic non-decreasing on that range."""
    if lo > hi or cost_of(Decimal(lo), s, o).ctc > target:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cost_of(Decimal(mid), s, o).ctc <= target:
            lo = mid
        else:
            hi = mid - 1
    return lo


def gross_for_ctc(target: Decimal, s: CtcSettings, o: Options) -> int | None:
    """The largest whole-rupee gross whose monthly CTC is <= `target`, or None when not even a gross of 1 fits. ESI regime
    boundary: gross <= threshold (ESI applies) versus above it."""
    top = int(target.to_integral_value(rounding=ROUND_FLOOR))  # cost >= gross, so no larger gross can fit
    if top < 1:
        return None
    found: list[int] = []
    if o.esi_applicable and s.rules.esi_wage_threshold >= 1:
        edge = int(s.rules.esi_wage_threshold.to_integral_value(rounding=ROUND_FLOOR))
        for lo, hi in ((1, min(edge, top)), (edge + 1, top)):
            g = _largest_fitting(lo, hi, target, s, o)
            if g is not None:
                found.append(g)
    else:
        g = _largest_fitting(1, top, target, s, o)
        if g is not None:
            found.append(g)
    return max(found) if found else None


def monthly_amount(amount: Decimal, frequency: str) -> Decimal:
    """Annual amounts are divided by 12 and rounded to paise."""
    if frequency == "annual":
        return q2(amount / 12)
    return q2(amount)


def calculate(mode: str, amount: Decimal, frequency: str, s: CtcSettings, o: Options) -> dict[str, Any]:
    """The 18.2 response. Raises CtcError for a gross below 1 rupee or a CTC target that no gross fits."""
    monthly = monthly_amount(amount, frequency)
    flags: list[str] = []
    if mode == "from_gross":
        gross = rupee(monthly)
        if gross < 1:
            raise CtcError("amount", "the monthly gross must be at least 1 rupee")
        if gross > MAX_MONTHLY:
            raise CtcError("amount", "the monthly gross is too large")
        unallocated = ZERO
    else:
        found = gross_for_ctc(monthly, s, o)
        if found is None:
            raise CtcError("amount", "TARGET_TOO_LOW: no salary fits this CTC")
        gross = Decimal(found)
    c = cost_of(gross, s, o)
    if mode == "from_ctc":
        unallocated = q2(monthly - c.ctc)
    if c.esi_applies:
        flags.append("ESI_APPLIES")
    if not o.pf_applicable:
        flags.append("PF_NOT_APPLICABLE")
    elif c.pf_ceiling_applied:
        flags.append("PF_CEILING_APPLIED")
    if c.pt_annual == 0:
        flags.append("NO_PT")
    r = s.rules
    return {
        "mode": mode,
        "frequency": frequency,
        "split": {
            "basic": money_str(c.basic),
            "hra": money_str(c.hra),
            "allowances": [{"code": a["code"], "name": a["name"], "amount": a["amount"]} for a in c.allowances],
        },  # fmt: skip
        "monthly": {
            "gross": money_str(c.gross),
            "employer_pf": money_str(c.employer_pf),
            "employer_esi": money_str(c.employer_esi),
            "employer_recovered": money_str(c.recovered),
            "gratuity_accrual": money_str(c.gratuity),
            "ctc": money_str(c.ctc),
        },  # fmt: skip
        "annual": {"gross": money_str(c.gross * 12), "ctc": money_str(c.ctc * 12)},
        "employee": {
            "pf": money_str(c.employee_pf),
            "esi": money_str(c.employee_esi),
            "pt_annual_estimate": money_str(c.pt_annual),
            "net_monthly_estimate": money_str(c.net_monthly),
        },  # fmt: skip
        "unallocated_monthly": money_str(unallocated),
        "flags": flags,
        "rules": {
            "pf_employee_rate_pct": _num(r.pf_employee_rate_pct),
            "pf_employer_rate_pct": _num(r.pf_employer_rate_pct),
            "pf_wage_ceiling": _num(r.pf_wage_ceiling),
            "esi_wage_threshold": _num(r.esi_wage_threshold),
            "esi_employee_rate_pct": _num(r.esi_employee_rate_pct),
            "esi_employer_rate_pct": _num(r.esi_employer_rate_pct),
            "pt_frequency": r.pt_frequency,
            "rounding": r.rounding,
            "gratuity_wage_basis": s.wage_basis,
        },  # fmt: skip
    }


def _num(x: Decimal) -> int | float:
    return int(x) if x == x.to_integral_value() else float(x)
