"""Full and final settlement arithmetic (plan 16 s.3, contract 18.8): PURE functions, no database, no clock.

Notice period: served = LWD - resignation date + 1 (calendar days); shortfall = max(0, required - served), only for a
resignation; a waiver makes it 0; an override (0..required days, with a reason) replaces the computed value.
Amount = round(shortfall x daily rate, 2) with daily rate = round(monthly gross or basic / divisor, 2).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from . import payroll_calc as calc

EXIT_REASONS = ("resignation", "termination", "retirement", "death", "contract_end", "absconding", "other")
MAX_LWD_AHEAD_DAYS = 180
MAX_MONTHS_AFTER_LWD = 3


def served_days(resignation: dt.date | None, lwd: dt.date) -> int | None:
    """Calendar days from the resignation date to the last working day, both included. None without a resignation date."""
    return (lwd - resignation).days + 1 if resignation is not None else None


def computed_shortfall(reason: str, required: int, served: int | None) -> int:
    """Only a resignation has a notice shortfall; an unknown resignation date cannot produce one."""
    if reason != "resignation" or served is None:
        return 0
    return max(0, required - served)


def effective_shortfall(computed: int, required: int, *, waived: bool, override_days: int | None, reason: str) -> int:
    if reason != "resignation" or waived:
        return 0
    return min(required, override_days) if override_days is not None else min(required, computed)


def daily_rate(monthly: Decimal, divisor: int) -> Decimal:
    return calc.q2(monthly / Decimal(divisor))


def notice_amount(days: int, rate: Decimal) -> Decimal:
    return calc.q2(Decimal(days) * rate)


def month_of(d: dt.date) -> dt.date:
    return d.replace(day=1)


def settlement_window(lwd: dt.date) -> tuple[dt.date, dt.date]:
    """The settlement month is the LWD month or up to three months later."""
    first = month_of(lwd)
    idx = first.year * 12 + first.month - 1 + MAX_MONTHS_AFTER_LWD
    return first, dt.date(idx // 12, idx % 12 + 1, 1)


@dataclass(frozen=True)
class Notice:
    required: int
    served: int | None
    computed: int
    shortfall: int
    waived: bool
    override_days: int | None
    override_reason: str | None


def notice_for(
    reason: str, resignation: dt.date | None, lwd: dt.date, required: int, *, waived: bool, override_days: int | None,
    override_reason: str | None = None,
) -> Notice:  # fmt: skip
    served = served_days(resignation, lwd)
    comp = computed_shortfall(reason, required, served)
    eff = effective_shortfall(comp, required, waived=waived, override_days=override_days, reason=reason)
    return Notice(required, served, comp, eff, waived, override_days, override_reason)
