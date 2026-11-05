"""Pure arithmetic of salary increments (plan 16 F4, contract 18.4): no database, no clock, no I/O.

A structure is handled in its STORED form (the `components` JSON of `salary_structures` plus the four flags):

    {"basic": "20000.00", "hra": "8000.00",
     "allowances": [{"code", "name", "amount", "prorate", "esi_wage"}],
     "fixed_deductions": [{"code", "name", "amount"}], "tds_monthly": "0.00",
     "pf_applicable": true, "pf_on_full_basic": false, "esi_applicable": true, "pt_applicable": true}

Every amount is a 2-decimal string in and out, computed with Decimal and half-up rounding (never floats).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .payroll_calc import money_str, parse_money, q2

RUPEE = Decimal("1")
HUNDRED = Decimal("100")
FLAGS = ("pf_applicable", "pf_on_full_basic", "esi_applicable", "pt_applicable")
MONEY_KEYS = ("basic", "hra", "allowances", "fixed_deductions", "tds_monthly")


class IncrementCalcError(ValueError):
    """The requested change cannot be expressed (for example a split that would leave no basic)."""


def rupee(x: Decimal) -> Decimal:
    """Whole rupee, half-up, as a 2-decimal value."""
    return q2(x.quantize(RUPEE, rounding=ROUND_HALF_UP))


def d(value: Any) -> Decimal:
    return parse_money(value) if isinstance(value, str) else Decimal(value)


def gross_of(c: dict[str, Any]) -> Decimal:
    return q2(d(c["basic"]) + d(c.get("hra") or "0") + sum((d(a["amount"]) for a in c.get("allowances", [])), Decimal(0)))


def esi_wage_of(c: dict[str, Any]) -> Decimal:
    return q2(
        d(c["basic"])
        + d(c.get("hra") or "0")
        + sum((d(a["amount"]) for a in c.get("allowances", []) if a.get("esi_wage", True)), Decimal(0))
    )


def normalise(c: dict[str, Any]) -> dict[str, Any]:
    """Canonical stored form (2-decimal strings, all keys present) so that two structures compare with `==`."""
    return {
        "basic": money_str(d(c["basic"])),
        "hra": money_str(d(c.get("hra") or "0")),
        "allowances": [
            {
                "code": a["code"],
                "name": a["name"],
                "amount": money_str(d(a["amount"])),
                "prorate": bool(a.get("prorate", True)),
                "esi_wage": bool(a.get("esi_wage", True)),
            }
            for a in c.get("allowances", [])
        ],  # fmt: skip
        "fixed_deductions": [
            {"code": x["code"], "name": x["name"], "amount": money_str(d(x["amount"]))} for x in c.get("fixed_deductions", [])
        ],
        "tds_monthly": money_str(d(c.get("tds_monthly") or "0")),
        "pf_applicable": bool(c.get("pf_applicable", True)),
        "pf_on_full_basic": bool(c.get("pf_on_full_basic", False)),
        "esi_applicable": bool(c.get("esi_applicable", True)),
        "pt_applicable": bool(c.get("pt_applicable", True)),
    }


def percent_target(old_gross: Decimal, pct: Decimal) -> Decimal:
    """Target gross of a percentage hike: the whole rupee nearest to old gross x (1 + pct / 100), half-up."""
    return rupee(old_gross * (HUNDRED + pct) / HUNDRED)


def _clone(old: dict[str, Any]) -> dict[str, Any]:
    c = normalise(old)
    return c


def scale_proportional(old: dict[str, Any], target: Decimal, split_code: str) -> dict[str, Any]:
    """BASIC, HRA and every allowance are scaled by target / old gross and rounded half-up to the rupee; the rounding
    remainder goes to the allowance `split_code` if there is one, else the last allowance, else BASIC. Fixed deductions, flags
    and tds_monthly are copied."""
    base = _clone(old)
    old_gross = gross_of(base)
    if old_gross <= 0 or target <= 0:
        raise IncrementCalcError("gross must be greater than 0")

    def scaled(x: str) -> Decimal:
        return rupee(d(x) * target / old_gross)

    out = dict(base)
    out["basic"] = money_str(scaled(base["basic"]))
    out["hra"] = money_str(scaled(base["hra"]))
    out["allowances"] = [{**a, "amount": money_str(scaled(a["amount"]))} for a in base["allowances"]]
    remainder = q2(target - gross_of(out))
    if remainder != 0:
        idx = next((i for i, a in enumerate(out["allowances"]) if a["code"] == split_code), None)
        if idx is None and out["allowances"]:
            idx = len(out["allowances"]) - 1
        if idx is not None and d(out["allowances"][idx]["amount"]) + remainder >= 0:
            out["allowances"][idx]["amount"] = money_str(d(out["allowances"][idx]["amount"]) + remainder)
        elif d(out["basic"]) + remainder > 0:
            out["basic"] = money_str(d(out["basic"]) + remainder)
        else:
            raise IncrementCalcError("the remainder cannot be placed")
    return out


def split_from_gross(
    old: dict[str, Any], target: Decimal, *, basic_pct: int, hra_pct_of_basic: int, code: str, name: str
) -> dict[str, Any]:
    """Re-split the target gross by the company rule (F9): basic = basic_pct % of gross, HRA = hra_pct_of_basic % of basic
    (both whole rupees, half-up), the rest is one allowance `code`. Existing allowances are replaced. Fixed deductions, flags and
    tds_monthly are copied."""
    if target <= 0:
        raise IncrementCalcError("gross must be greater than 0")
    base = _clone(old)
    basic = rupee(target * Decimal(basic_pct) / HUNDRED)
    if basic <= 0:
        raise IncrementCalcError("basic must be greater than 0")
    hra = min(rupee(basic * Decimal(hra_pct_of_basic) / HUNDRED), q2(target - basic))
    rest = q2(target - basic - hra)
    out = dict(base)
    out["basic"], out["hra"] = money_str(basic), money_str(hra)
    out["allowances"] = (
        [{"code": code, "name": name, "amount": money_str(rest), "prorate": True, "esi_wage": True}] if rest > 0 else []
    )
    return out


def change(old_gross: Decimal | None, new_gross: Decimal | None) -> dict[str, str | None]:
    """The `change` block of the contract: amount and percent (negative for a decrease), percent rounded to 2 decimals."""
    if old_gross is None or new_gross is None:
        return {"old_gross": money_str(old_gross) if old_gross is not None else None,
                "new_gross": money_str(new_gross) if new_gross is not None else None, "amount": None, "percent": None}  # fmt: skip
    pct = q2((new_gross - old_gross) * HUNDRED / old_gross) if old_gross > 0 else None
    return {
        "old_gross": money_str(old_gross),
        "new_gross": money_str(new_gross),
        "amount": money_str(new_gross - old_gross),
        "percent": money_str(pct) if pct is not None else None,
    }


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, str]]:
    """Lines whose monthly amount differs: BASIC, HRA, allowances, fixed deductions and TDS (code `TDS`)."""
    o, n = normalise(old), normalise(new)
    rows: list[tuple[str, str, str]] = [("BASIC", o["basic"], n["basic"]), ("HRA", o["hra"], n["hra"])]
    for key in ("allowances", "fixed_deductions"):
        om = {a["code"]: a["amount"] for a in o[key]}
        nm = {a["code"]: a["amount"] for a in n[key]}
        for code in [*om, *[c for c in nm if c not in om]]:
            rows.append((code, om.get(code, "0.00"), nm.get(code, "0.00")))
    rows.append(("TDS", o["tds_monthly"], n["tds_monthly"]))
    return [{"code": c, "old": a, "new": b} for c, a, b in rows if a != b]


def identical(old: dict[str, Any], new: dict[str, Any]) -> bool:
    return normalise(old) == normalise(new)


def warnings_for(
    old: dict[str, Any],
    new: dict[str, Any] | None,
    *,
    kind: str,
    esi_threshold: Decimal,
    pf_ceiling: Decimal,
    max_pct: Decimal,
    backdated: bool,
) -> list[str]:
    """Plan 16 section 3 warnings, in a fixed order. `new` None = the structure is removed (only BACKDATED can apply)."""
    out: list[str] = []
    if backdated:
        out.append("BACKDATED")
    if new is None:
        return out
    o, n = normalise(old), normalise(new)
    og, ng = gross_of(o), gross_of(n)
    if d(n["tds_monthly"]) > 0 and ng != og:
        out.append("TDS_REVIEW")
    if ng < og:
        out.append("DECREASE")
    if kind in ("amount", "structure", "import") and og > 0 and abs(ng - og) * HUNDRED / og > max_pct:
        out.append("LARGE_CHANGE")
    if (o["esi_applicable"] or n["esi_applicable"]) and (og <= esi_threshold) != (ng <= esi_threshold):
        out.append("ESI_THRESHOLD_CROSSED")
    if (
        (o["pf_applicable"] or n["pf_applicable"])
        and not (o["pf_on_full_basic"] and n["pf_on_full_basic"])
        and pf_ceiling > 0
        and (d(o["basic"]) <= pf_ceiling) != (d(n["basic"]) <= pf_ceiling)
    ):
        out.append("PF_CEILING_CROSSED")
    if identical(o, n):
        out.append("NO_CHANGE")
    return out
