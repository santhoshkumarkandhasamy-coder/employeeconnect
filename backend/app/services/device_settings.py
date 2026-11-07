"""`device.*` settings (plan 18 section 3 / contract 21.6): defaults, validation specs and a typed loader. No service imports,
so services/settings.py can include these without an import cycle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Setting

DEFAULTS: dict[str, Any] = {
    "device.sync_interval_minutes": 5,
    "device.sync_window_start": "05:00",
    "device.sync_window_end": "23:30",
    "device.off_window_interval_minutes": 60,
    "device.apply_window_days": 7,
    "device.checkout_min_gap_minutes": 5,
    "device.drift_tolerance_s": 120,
    "device.drift_invalid_s": 43200,
    "device.future_tolerance_s": 300,
    "device.match_prefix": "",
    "device.apply_on_on_duty_days": False,
    "device.use_punch_state": False,
    "device.push_on_punch": False,
    "device.punch_retention_days": 1095,
    "device.sync_history_retention_days": 90,
}
# (kind, lo, hi) - the settings service builds its Spec objects from this table
BOUNDS: dict[str, tuple[str, float | None, float | None]] = {
    "device.sync_interval_minutes": ("int", 2, 60),
    "device.sync_window_start": ("str", None, None),
    "device.sync_window_end": ("str", None, None),
    "device.off_window_interval_minutes": ("int", 15, 240),
    "device.apply_window_days": ("int", 0, 31),
    "device.checkout_min_gap_minutes": ("int", 1, 120),
    "device.drift_tolerance_s": ("int", 0, 900),
    "device.drift_invalid_s": ("int", 900, 604800),
    "device.future_tolerance_s": ("int", 0, 3600),
    "device.match_prefix": ("str", None, None),
    "device.apply_on_on_duty_days": ("bool", None, None),
    "device.use_punch_state": ("bool", None, None),
    "device.push_on_punch": ("bool", None, None),
    "device.punch_retention_days": ("int", 0, 3650),
    "device.sync_history_retention_days": ("int", 7, 366),
}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_PREFIX = re.compile(r"^[A-Za-z0-9-]{0,10}$")


def validate_device_setting(key: str, value: Any) -> str | None:
    """Extra checks beyond type/range. Returns an error message or None."""
    if key in ("device.sync_window_start", "device.sync_window_end") and not _HHMM.match(str(value)):
        return "must be HH:MM (24 hour)"
    if key == "device.match_prefix" and not _PREFIX.match(str(value)):
        return "at most 10 letters, digits or hyphens"
    if key == "device.punch_retention_days" and value != 0 and value < 90:
        return "must be 0 (keep) or at least 90"
    return None


@dataclass(frozen=True)
class DeviceCfg:
    sync_interval_minutes: int
    sync_window_start: str
    sync_window_end: str
    off_window_interval_minutes: int
    apply_window_days: int
    checkout_min_gap_minutes: int
    drift_tolerance_s: int
    drift_invalid_s: int
    future_tolerance_s: int
    match_prefix: str
    apply_on_on_duty_days: bool
    use_punch_state: bool
    push_on_punch: bool
    punch_retention_days: int
    sync_history_retention_days: int


async def load(session: AsyncSession) -> DeviceCfg:
    rows = {r.key: r.value for r in (await session.execute(select(Setting).where(Setting.key.in_(list(DEFAULTS))))).scalars()}
    merged = {k: rows.get(k, d) for k, d in DEFAULTS.items()}
    return DeviceCfg(**{k.removeprefix("device."): v for k, v in merged.items()})
