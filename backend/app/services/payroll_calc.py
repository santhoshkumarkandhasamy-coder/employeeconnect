"""Payroll calculation (plan 12 s.4.4): PURE functions on `Decimal`, no database, no clock, no I/O.

Money is handled as `Decimal` rupees quantised to paise (0.01), rounded half-up. In JSON it is a string with 2 decimals.
Rounding: `payroll.rounding = rupee` (default) rounds every COMPUTED line (prorated earnings, PF, ESI, PT) to the rupee;
amounts typed in by HR (adjustments, fixed deductions, TDS, PT override) are used exactly as entered. ESI contributions are
always rounded UP to the next rupee (statutory rule), PT slab lookups use the gross rounded UP to a whole rupee.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

PAISE = Decimal("0.01")
RUPEE = Decimal("1")
HUNDRED = Decimal("100")
MAX_MONEY = Decimal("10000000.00")
_MONEY_RE = re.compile(r"^\d{1,8}(\.\d{1,2})?$")
CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,15}$")

EARNING_ADJUSTMENTS = ("BONUS", "ARREARS", "INCENTIVE", "REIMBURSEMENT", "OTHER_EARNING")
DEDUCTION_ADJUSTMENTS = ("ADVANCE_RECOVERY", "LOAN_EMI", "OTHER_DEDUCTION")
RESERVED_CODES = frozenset(
    {"BASIC", "HRA", "PF", "ESI", "PF_ER_REC", "ESI_ER_REC", "PT", "TDS", "LOP", *EARNING_ADJUSTMENTS, *DEDUCTION_ADJUSTMENTS}
)
ESI_ADJUSTMENTS = frozenset({"INCENTIVE", "OTHER_EARNING"})  # adjustment earnings that are ESI wages

ERROR_CODES = frozenset({"STRUCTURE_MISSING", "NEGATIVE_NET_PAY"})
WARNING_CODES = frozenset(
    {
        "NO_SHIFT",
        "PENDING_LEAVE",
        "LOP_OVERRIDDEN",
        "ZERO_PAID_DAYS",
        "NO_BANK_DETAILS",
        "NO_PAN",
        "STRUCTURE_CHANGED",
        "NOT_ELIGIBLE",
    }
)

LABELS = {
    "BASIC": "Basic",
    "HRA": "House Rent Allowance",
    "PF": "Provident Fund",
    "ESI": "ESI",
    "PT": "Professional Tax",
    "TDS": "Income Tax (TDS)",
    "PF_ER": "Employer PF",
    "ESI_ER": "Employer ESI",
    "PF_ER_REC": "PF - employer share (recovered)",
    "ESI_ER_REC": "ESI - employer share (recovered)",
}
LABELS_SPLIT = {"PF": "PF - employee share", "ESI": "ESI - employee share"}  # used when the employer share is recovered
RECOVERED_CODES = frozenset({"PF_ER_REC", "ESI_ER_REC"})


# ---------------------------------------------------------------------------------------------- money helpers
def q2(x: Decimal) -> Decimal:
    return x.quantize(PAISE, rounding=ROUND_HALF_UP)


def rnd(x: Decimal, mode: str) -> Decimal:
    """Round a computed line: whole rupee (half-up) in `rupee` mode, paise otherwise."""
    if mode == "rupee":
        return q2(x.quantize(RUPEE, rounding=ROUND_HALF_UP))
    return q2(x)


def ceil_rupee(x: Decimal) -> Decimal:
    return q2(x.quantize(RUPEE, rounding=ROUND_CEILING))


def money_str(x: Decimal) -> str:
    return f"{q2(x):.2f}"


def parse_money(value: Any) -> Decimal:
    """Strict: a string with at most 2 decimals, 0..10,000,000.00. The message never contains the submitted value."""
    if not isinstance(value, str):
        raise ValueError('must be an amount as a string such as "25000.00"')
    v = value.strip()
    if not _MONEY_RE.match(v):
        raise ValueError("must be an amount with at most 2 decimals")
    d = Decimal(v)
    if d > MAX_MONEY:
        raise ValueError("must be at most 10000000.00")
    return q2(d)


# ---------------------------------------------------------------------------------------------- amount in words
_ONES = (
    "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen Sixteen Seventeen "
    "Eighteen Nineteen"
).split()
_TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _below_100(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + (f"-{_ONES[o]}" if o else "")


def _below_1000(n: int) -> str:
    h, r = divmod(n, 100)
    parts = []
    if h:
        parts.append(f"{_ONES[h]} Hundred")
    if r:
        parts.append(_below_100(r))
    return " ".join(parts)


def int_in_words(n: int) -> str:
    """Indian numbering: thousand, lakh, crore."""
    if n == 0:
        return "Zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1000)
    if crore:
        parts.append(f"{int_in_words(crore)} Crore")
    if lakh:
        parts.append(f"{_below_100(lakh)} Lakh")
    if thousand:
        parts.append(f"{_below_100(thousand)} Thousand")
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


def amount_in_words(amount: Decimal) -> str:
    a = q2(amount)
    rupees, paise = divmod(int((a * 100).to_integral_value()), 100)
    out = f"Rupees {int_in_words(rupees)}"
    if paise:
        out += f" and {_below_100(paise)} Paise"
    return out + " Only"


# ---------------------------------------------------------------------------------------------- data classes
@dataclass(frozen=True)
class Allowance:
    code: str
    name: str
    amount: Decimal
    prorate: bool = True
    esi_wage: bool = True


@dataclass(frozen=True)
class FixedDeduction:
    code: str
    name: str
    amount: Decimal


@dataclass(frozen=True)
class StructureData:
    basic: Decimal
    hra: Decimal = Decimal("0.00")
    allowances: tuple[Allowance, ...] = ()
    fixed_deductions: tuple[FixedDeduction, ...] = ()
    tds_monthly: Decimal = Decimal("0.00")
    pf_applicable: bool = True
    pf_on_full_basic: bool = False
    esi_applicable: bool = True
    pt_applicable: bool = True

    @property
    def monthly_gross(self) -> Decimal:
        return q2(self.basic + self.hra + sum((a.amount for a in self.allowances), Decimal(0)))

    @classmethod
    def from_stored(
        cls, components: dict, *, pf_applicable=True, pf_on_full_basic=False, esi_applicable=True, pt_applicable=True
    ):
        return cls(
            basic=parse_money(components["basic"]),
            hra=parse_money(components.get("hra") or "0"),
            allowances=tuple(
                Allowance(
                    a["code"], a["name"], parse_money(a["amount"]), bool(a.get("prorate", True)), bool(a.get("esi_wage", True))
                )
                for a in components.get("allowances", [])
            ),
            fixed_deductions=tuple(
                FixedDeduction(d["code"], d["name"], parse_money(d["amount"])) for d in components.get("fixed_deductions", [])
            ),
            tds_monthly=parse_money(components.get("tds_monthly") or "0"),
            pf_applicable=pf_applicable,
            pf_on_full_basic=pf_on_full_basic,
            esi_applicable=esi_applicable,
            pt_applicable=pt_applicable,
        )


@dataclass(frozen=True)
class PtSlab:
    min_gross: Decimal
    max_gross: Decimal | None
    amount: Decimal
    months: tuple[int, ...] | None = None


@dataclass(frozen=True)
class Rules:
    """The statutory settings of one run (the `payroll.*` snapshot in payroll_runs.config)."""

    pf_employee_rate_pct: Decimal = Decimal("12")
    pf_employer_rate_pct: Decimal = Decimal("12")
    pf_wage_ceiling: Decimal = Decimal("15000")
    esi_wage_threshold: Decimal = Decimal("20000")
    esi_employee_rate_pct: Decimal = Decimal("0.75")
    esi_employer_rate_pct: Decimal = Decimal("3.25")
    pt_frequency: str = "half_yearly"
    pt_slabs: tuple[PtSlab, ...] = ()
    rounding: str = "rupee"
    employer_share_from_employee: bool = True  # employer PF/ESI are also deducted from the employee's pay

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> Rules:
        """`cfg` is the run snapshot: keys WITHOUT the `payroll.` prefix."""
        slabs = tuple(
            PtSlab(
                Decimal(str(s["min_gross"])),
                None if s.get("max_gross") is None else Decimal(str(s["max_gross"])),
                Decimal(str(s["amount"])),
                None if s.get("months") is None else tuple(int(m) for m in s["months"]),
            )
            for s in cfg.get("pt_slabs", [])
        )
        d = lambda k, default: Decimal(str(cfg.get(k, default)))  # noqa: E731
        return cls(
            pf_employee_rate_pct=d("pf_employee_rate_pct", 12),
            pf_employer_rate_pct=d("pf_employer_rate_pct", 12),
            pf_wage_ceiling=d("pf_wage_ceiling", 15000),
            esi_wage_threshold=d("esi_wage_threshold", 20000),
            esi_employee_rate_pct=d("esi_employee_rate_pct", "0.75"),
            esi_employer_rate_pct=d("esi_employer_rate_pct", "3.25"),
            pt_frequency=str(cfg.get("pt_frequency", "half_yearly")),
            pt_slabs=slabs,
            rounding=str(cfg.get("rounding", "rupee")),
            employer_share_from_employee=bool(cfg.get("employer_share_from_employee", True)),
        )


@dataclass(frozen=True)
class Adjustment:
    code: str
    label: str
    amount: Decimal


@dataclass(frozen=True)
class PayInputs:
    tds: Decimal | None = None  # None = the structure default
    pt_override: Decimal | None = None
    adjustments: tuple[Adjustment, ...] = ()


@dataclass
class CalcResult:
    earnings: list[dict] = field(default_factory=list)
    deductions: list[dict] = field(default_factory=list)
    employer: list[dict] = field(default_factory=list)
    gross: Decimal = Decimal("0.00")
    total_deductions: Decimal = Decimal("0.00")
    net: Decimal = Decimal("0.00")
    error: str | None = None  # NEGATIVE_NET_PAY

    def to_json(self) -> dict:
        return {
            "earnings": self.earnings,
            "deductions": self.deductions,
            "employer": self.employer,
            "gross": money_str(self.gross),
            "total_deductions": money_str(self.total_deductions),
            "net": money_str(self.net),
        }


# ---------------------------------------------------------------------------------------------- statutory rules
def structure_for(structures: Sequence[tuple[dt.date, Any]], day: dt.date) -> Any | None:
    """The latest (effective_from, item) with effective_from <= day (D3: effective-dated per month)."""
    best = None
    for eff, item in structures:
        if eff <= day and (best is None or eff > best[0]):
            best = (eff, item)
    return best[1] if best else None


def esi_period_start(period: dt.date) -> dt.date:
    """ESI contribution periods: Apr-Sep and Oct-Mar."""
    if 4 <= period.month <= 9:
        return dt.date(period.year, 4, 1)
    return dt.date(period.year if period.month >= 10 else period.year - 1, 10, 1)


def esi_eligible(
    structures: Sequence[tuple[dt.date, StructureData]], period: dt.date, join_date: dt.date | None, threshold: Decimal
) -> bool:
    """Eligible for the WHOLE contribution period when the structure's monthly gross at the period's first month (or the
    joining month, for a joiner inside the period) was <= the threshold."""
    ref = esi_period_start(period)
    if join_date is not None and join_date > ref:
        ref = join_date.replace(day=1)
    ref = min(ref, period)
    s = structure_for(structures, ref) or structure_for(structures, period)
    return s is not None and s.monthly_gross <= threshold


def pt_slab_amount(slabs: Sequence[PtSlab], gross: Decimal, month: int) -> Decimal:
    """Amount of the slab matching `gross` (already a whole rupee) whose months is null or contains `month`. A slab with
    `months` wins over a `months: null` slab for the same range. No match = 0."""
    hit = [s for s in slabs if s.min_gross <= gross and (s.max_gross is None or gross <= s.max_gross)]
    specific = [s for s in hit if s.months is not None and month in s.months]
    if specific:
        return specific[0].amount
    general = [s for s in hit if s.months is None]
    return general[0].amount if general else Decimal("0.00")


def professional_tax(rules: Rules, month: int, gross: Decimal, prior_gross: Sequence[Decimal] = ()) -> Decimal:
    """`monthly`: slab on this month's gross. `half_yearly` (Tamil Nadu): slab on the sum of the gross of the 6 months ending
    with this one (`prior_gross` = the up to 5 earlier months), deducted only in the months the slab lists. The gross is
    rounded UP to a whole rupee before the slab lookup."""
    if rules.pt_frequency == "half_yearly":
        base = gross + sum(prior_gross[-5:], Decimal(0))
    else:
        base = gross
    return q2(pt_slab_amount(rules.pt_slabs, ceil_rupee(base), month))


def pf_label(code: str, rules: Rules) -> str:
    return LABELS_SPLIT[code] if rules.employer_share_from_employee else LABELS[code]


# ---------------------------------------------------------------------------------------------- main calculation
def calculate(
    structure: StructureData,
    rules: Rules,
    period: dt.date,
    basis_days: Decimal,
    paid_days: Decimal,
    inputs: PayInputs,
    *,
    esi_is_eligible: bool = True,
    prior_gross: Sequence[Decimal] = (),
) -> CalcResult:
    mode = rules.rounding
    factor = (paid_days / basis_days) if basis_days > 0 else Decimal(0)

    def earned(monthly: Decimal, prorate: bool = True) -> Decimal:
        if prorate:
            return rnd(monthly * factor, mode)
        return rnd(monthly, mode) if paid_days > 0 else Decimal("0.00")

    r = CalcResult()
    basic = earned(structure.basic)
    r.earnings.append(
        {"code": "BASIC", "label": LABELS["BASIC"], "monthly": money_str(structure.basic), "amount": money_str(basic)}
    )
    hra = earned(structure.hra)
    r.earnings.append({"code": "HRA", "label": LABELS["HRA"], "monthly": money_str(structure.hra), "amount": money_str(hra)})
    esi_wage = basic + hra  # basic and HRA are ESI wages; allowances carry their own flag
    for a in structure.allowances:
        amt = earned(a.amount, a.prorate)
        r.earnings.append({"code": a.code, "label": a.name, "monthly": money_str(a.amount), "amount": money_str(amt)})
        if a.esi_wage:
            esi_wage += amt
    gross = basic + hra + sum((Decimal(e["amount"]) for e in r.earnings[2:]), Decimal(0))
    for adj in inputs.adjustments:
        if adj.code in EARNING_ADJUSTMENTS:
            r.earnings.append({"code": adj.code, "label": adj.label, "monthly": None, "amount": money_str(adj.amount)})
            gross += adj.amount
            if adj.code in ESI_ADJUSTMENTS:
                esi_wage += adj.amount
    gross = q2(gross)

    ded: list[tuple[str, str, Decimal]] = []
    employer: list[tuple[str, str, Decimal]] = []

    if structure.pf_applicable:
        wage = basic if (structure.pf_on_full_basic or rules.pf_wage_ceiling == 0) else min(basic, rules.pf_wage_ceiling)
        pf_er = rnd(wage * rules.pf_employer_rate_pct / HUNDRED, mode)
        ded.append(("PF", pf_label("PF", rules), rnd(wage * rules.pf_employee_rate_pct / HUNDRED, mode)))
        if rules.employer_share_from_employee:
            ded.append(("PF_ER_REC", LABELS["PF_ER_REC"], pf_er))
        employer.append(("PF_ER", LABELS["PF_ER"], pf_er))

    if structure.esi_applicable and esi_is_eligible and esi_wage > 0:
        esi_er = ceil_rupee(esi_wage * rules.esi_employer_rate_pct / HUNDRED)
        ded.append(("ESI", pf_label("ESI", rules), ceil_rupee(esi_wage * rules.esi_employee_rate_pct / HUNDRED)))
        if rules.employer_share_from_employee:
            ded.append(("ESI_ER_REC", LABELS["ESI_ER_REC"], esi_er))
        employer.append(("ESI_ER", LABELS["ESI_ER"], esi_er))

    if inputs.pt_override is not None:
        pt = q2(inputs.pt_override)
    elif structure.pt_applicable:
        pt = professional_tax(rules, period.month, gross, prior_gross)
    else:
        pt = Decimal("0.00")
    ded.append(("PT", LABELS["PT"], pt))

    ded.append(("TDS", LABELS["TDS"], q2(inputs.tds if inputs.tds is not None else structure.tds_monthly)))
    for f in structure.fixed_deductions:
        ded.append((f.code, f.name, f.amount))
    for adj in inputs.adjustments:
        if adj.code in DEDUCTION_ADJUSTMENTS:
            ded.append((adj.code, adj.label, adj.amount))

    r.deductions = [{"code": c, "label": lb, "amount": money_str(a)} for c, lb, a in ded if a > 0]
    r.employer = [{"code": c, "label": lb, "amount": money_str(a)} for c, lb, a in employer if a > 0]
    r.gross = gross
    r.total_deductions = q2(sum((a for _, _, a in ded), Decimal(0)))
    r.net = q2(gross - r.total_deductions)
    if r.net < 0:
        r.error = "NEGATIVE_NET_PAY"
    return r


def employer_amount(result: dict | None, code: str) -> Decimal:
    if not result:
        return Decimal("0.00")
    for e in result.get("employer", []):
        if e["code"] == code:
            return Decimal(e["amount"])
    return Decimal("0.00")
