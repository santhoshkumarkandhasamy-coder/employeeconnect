"""Payroll settings (plan 12 s.4.1, contract 13.3): validation, read/update and the snapshot copied into every run.

The `payroll.*` keys are edited ONLY through `/admin/payroll/settings`; they are not part of services.settings.KNOWN, so the
generic `PUT /admin/settings` refuses them as unknown. Changes never alter an existing run (the run keeps its snapshot) until
a draft run is refreshed.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.rbac import Principal
from ..core.text import clean_text
from ..models import Setting
from . import admin_security
from .payroll_calc import money_str, parse_money

PREFIX = "payroll."
SECOND_PERSON_KEY = "payroll.publish_requires_second_person"
SECOND_PERSON_PERMISSION = "role.assign"  # the maker-checker switch is an admin-level control, not a payroll.manage one
MAX_SLABS = 20

DEFAULTS: dict[str, Any] = {
    "payroll.pay_days_basis": "calendar",
    "payroll.lop_absent_days": True,
    "payroll.lop_half_day_attendance": True,
    "payroll.pf_employee_rate_pct": 12,
    "payroll.pf_employer_rate_pct": 12,
    "payroll.pf_wage_ceiling": 15000,
    "payroll.esi_wage_threshold": 20000,
    "payroll.esi_employee_rate_pct": 0.75,
    "payroll.esi_employer_rate_pct": 3.25,
    "payroll.pt_frequency": "half_yearly",
    # Tamil Nadu half-yearly defaults. VERIFY AGAINST CURRENT TN RULES before the first real run.
    "payroll.pt_slabs": [
        {"min_gross": "0.00", "max_gross": "21000.00", "amount": "0.00", "months": [3, 9]},
        {"min_gross": "21001.00", "max_gross": "30000.00", "amount": "135.00", "months": [3, 9]},
        {"min_gross": "30001.00", "max_gross": "45000.00", "amount": "315.00", "months": [3, 9]},
        {"min_gross": "45001.00", "max_gross": "60000.00", "amount": "690.00", "months": [3, 9]},
        {"min_gross": "60001.00", "max_gross": "75000.00", "amount": "1025.00", "months": [3, 9]},
        {"min_gross": "75001.00", "max_gross": None, "amount": "1250.00", "months": [3, 9]},
    ],
    "payroll.rounding": "rupee",
    "payroll.show_employer_contributions": False,
    "payroll.employer_share_from_employee": True,
    "payroll.mask_pan": True,
    "payroll.publish_requires_second_person": False,
    "payroll.company_address": "",
    "payroll.payslip_footer": "This is a computer-generated payslip and does not require a signature.",
}

_ENUMS = {
    "payroll.pay_days_basis": ("calendar", "fixed_30", "working_days"),
    "payroll.pt_frequency": ("monthly", "half_yearly"),
    "payroll.rounding": ("rupee", "paise"),
}
_BOOLS = {
    "payroll.lop_absent_days",
    "payroll.lop_half_day_attendance",
    "payroll.show_employer_contributions",
    "payroll.employer_share_from_employee",
    "payroll.mask_pan",
    "payroll.publish_requires_second_person",
}
_NUMBERS = {  # key -> (min, max), at most 2 decimals
    "payroll.pf_employee_rate_pct": (0, 20),
    "payroll.pf_employer_rate_pct": (0, 20),
    "payroll.pf_wage_ceiling": (0, 1_000_000),
    "payroll.esi_wage_threshold": (0, 1_000_000),
    "payroll.esi_employee_rate_pct": (0, 10),
    "payroll.esi_employer_rate_pct": (0, 10),
}
_TEXTS = {"payroll.company_address": (0, 300), "payroll.payslip_footer": (0, 200)}


def _bad(key: str, msg: str) -> AppError:
    return AppError("VALIDATION_ERROR", msg, details=[{"field": key, "message": msg}])


def _validate_slabs(key: str, value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_SLABS:
        raise _bad(key, f"must be a list of at most {MAX_SLABS} slabs")
    out: list[dict] = []
    for i, s in enumerate(value):
        if not isinstance(s, dict) or set(s) - {"min_gross", "max_gross", "amount", "months"}:
            raise _bad(key, f"slab {i + 1}: expected min_gross, max_gross, amount, months")
        try:
            lo = parse_money(s.get("min_gross"))
            hi = None if s.get("max_gross") is None else parse_money(s["max_gross"])
            amt = parse_money(s.get("amount"))
        except ValueError as exc:
            raise _bad(key, f"slab {i + 1}: {exc}") from None
        if hi is not None and hi < lo:
            raise _bad(key, f"slab {i + 1}: max_gross must be >= min_gross")
        months = s.get("months")
        if months is not None:
            if (
                not isinstance(months, list)
                or any(isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 12 for m in months)
                or len(set(months)) != len(months)
            ):
                raise _bad(key, f"slab {i + 1}: months must be null or a list of distinct months 1..12")
            months = sorted(months)
        out.append(
            {
                "min_gross": money_str(lo),
                "max_gross": None if hi is None else money_str(hi),
                "amount": money_str(amt),
                "months": months,
            }
        )
    # Overlap check. Slabs with months win over `months: null` slabs for the same range, so only slabs that compete for the
    # same month must not overlap: all-month slabs among themselves, and for each month the slabs listing that month.
    inf = Decimal("1e12")

    def clash(group: list[dict]) -> bool:
        spans = sorted((Decimal(g["min_gross"]), Decimal(g["max_gross"]) if g["max_gross"] else inf) for g in group)
        return any(spans[j][0] <= spans[j - 1][1] for j in range(1, len(spans)))

    if clash([s for s in out if s["months"] is None]):
        raise _bad(key, "slabs that apply to every month overlap")
    for m in range(1, 13):
        if clash([s for s in out if s["months"] is not None and m in s["months"]]):
            raise _bad(key, f"slabs overlap in month {m}")
    return out


def validate(key: str, value: Any) -> Any:
    """Returns the normalised value to store. Messages never contain the submitted value."""
    if key not in DEFAULTS:
        raise _bad(key, "unknown setting")
    if key in _ENUMS:
        if not isinstance(value, str) or value not in _ENUMS[key]:
            raise _bad(key, "must be one of: " + ", ".join(_ENUMS[key]))
        return value
    if key in _BOOLS:
        if not isinstance(value, bool):
            raise _bad(key, "must be true or false")
        return value
    if key in _NUMBERS:
        lo, hi = _NUMBERS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _bad(key, "must be a number")
        if isinstance(value, float) and not math.isfinite(value):
            raise _bad(key, "must be a finite number")
        try:
            d = Decimal(str(value))
        except InvalidOperation:
            raise _bad(key, "must be a number") from None
        if d != d.quantize(Decimal("0.01")):
            raise _bad(key, "at most 2 decimals")
        if not lo <= d <= hi:
            raise _bad(key, f"must be between {lo} and {hi}")
        return int(d) if d == d.to_integral_value() else float(d)
    if key in _TEXTS:
        lo, hi = _TEXTS[key]
        if not isinstance(value, str):
            raise _bad(key, "must be text")
        v = clean_text(value, multiline=key == "payroll.company_address")
        if len(v) > hi:
            raise _bad(key, f"at most {hi} characters")
        return v
    if key == "payroll.pt_slabs":
        return _validate_slabs(key, value)
    raise _bad(key, "setting is not editable")  # unreachable: every DEFAULTS key is covered above


async def read_all(session: AsyncSession) -> dict[str, Any]:
    rows = {r.key: r.value for r in (await session.execute(select(Setting).where(Setting.key.like("payroll.%")))).scalars()}
    return {"settings": {k: rows.get(k, d) for k, d in DEFAULTS.items()}}


async def snapshot(session: AsyncSession) -> dict[str, Any]:
    """The run config: every payroll.* setting, keys without the prefix (rates only, nothing personal)."""
    return {k[len(PREFIX) :]: v for k, v in (await read_all(session))["settings"].items()}


def _audit_value(key: str, v: Any) -> Any:
    return json.dumps(v, separators=(",", ":")) if key == "payroll.pt_slabs" else v  # a string: not walked by the redactor


async def current_value(session: AsyncSession, key: str) -> Any:
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return row.value if row is not None else DEFAULTS[key]


async def update(
    session: AsyncSession,
    actor: Principal,
    changes: dict[str, Any],
    *,
    redis: Redis,
    current_password: SecretStr | None = None,
) -> dict[str, Any]:
    """Merge validated settings. Changing `payroll.publish_requires_second_person` (either direction) additionally needs the
    `role.assign` permission AND the caller's own password (`current_password`); other keys do not."""
    changes = {k: validate(k, v) for k, v in changes.items()}
    if SECOND_PERSON_KEY in changes and changes[SECOND_PERSON_KEY] != await current_value(session, SECOND_PERSON_KEY):
        if not actor.has(SECOND_PERSON_PERMISSION):
            raise AppError("FORBIDDEN", "You do not have permission to change the second-person publish rule")
        await admin_security.require_reauth(session, redis, actor, current_password, action="payroll.settings.second_person")
    rows = {r.key: r for r in (await session.execute(select(Setting).where(Setting.key.in_(list(changes))))).scalars()}
    log_changes: list[dict[str, Any]] = []
    for k, v in changes.items():
        row = rows.get(k)
        old = row.value if row is not None else DEFAULTS[k]
        if row is None:
            try:
                async with session.begin_nested():
                    session.add(Setting(key=k, value=v, updated_by=actor.id))
                    await session.flush()
            except IntegrityError:  # a concurrent first write of the same key: update its row instead
                row = (
                    await session.execute(select(Setting).where(Setting.key == k).execution_options(populate_existing=True))
                ).scalar_one_or_none()
                if row is None:
                    raise AppError("CONFLICT", "Settings were changed concurrently; retry") from None
                old = row.value
                row.value, row.updated_by = v, actor.id
        elif row.value != v:
            row.value, row.updated_by = v, actor.id
        if old != v:
            log_changes.append({"key": k, "before": _audit_value(k, old), "after": _audit_value(k, v)})
    if log_changes:
        audit(
            session, action="payroll.settings.update", actor=actor, entity_type="payroll_settings", after={"changes": log_changes}
        )
    await session.commit()
    return await read_all(session)
