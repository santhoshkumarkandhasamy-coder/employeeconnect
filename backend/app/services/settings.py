"""Admin settings read/update (contract section 9). Only known keys are accepted. Every key has an entry in SPECS
(type + min/max, review 04 M4): NaN/Infinity, wrong types, floats for integer keys and out-of-range values are refused with
a clean 422 (Postgres JSONB would otherwise answer 500). Every change is audited as a key/before/after list (M5)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import Setting
from . import attendance as att
from . import food as food_svc
from . import leave as leave_svc
from .auth import _CONFIG_DEFAULTS

KNOWN: dict[str, Any] = {
    **att._DEFAULTS,
    **{k: v for k, v in _CONFIG_DEFAULTS.items() if v is not None},
    **food_svc.FOOD_DEFAULTS,
    **leave_svc._DEFAULTS,
    "console.session_max_hours": 12,
    "console.inactivity_timeout_minutes": 15,
}


@dataclass(frozen=True)
class Spec:
    kind: Literal["int", "float", "bool", "str"]
    lo: float | None = None
    hi: float | None = None


def _i(lo: int, hi: int) -> Spec:
    return Spec("int", lo, hi)


# Every key of KNOWN must be listed (enforced by a test). Bounds are deliberately tight: they protect the geofence, location
# retention and request limits from a typo or a compromised admin session.
SPECS: dict[str, Spec] = {
    "office.lat": Spec("float", -90, 90),
    "office.lng": Spec("float", -180, 180),
    "office.radius_m": _i(10, 2000),
    "office.timezone": Spec("str"),
    "attendance.selfie_required": Spec("bool"),
    "attendance.checkout_geofence": Spec("bool"),
    "attendance.max_accuracy_m": _i(5, 500),
    "attendance.half_day_minutes": _i(30, 720),
    "attendance.max_fix_age_s": _i(5, 300),
    "attendance.max_fix_future_s": _i(0, 120),
    "attendance.reject_mock_location": Spec("bool"),
    "attendance.selfie_max_age_s": _i(30, 3600),
    "attendance.location_retention_days": _i(1, 3650),  # 0 ("keep forever") cannot be set through the API
    "attendance.selfie_retention_days": _i(1, 3650),
    "food.cutoff_mode": Spec("str"),
    "food.cutoff_time": Spec("str"),
    "food.max_advance_days": _i(1, 60),
    "auth.inactivity_timeout_minutes": _i(1, 30),
    "permission.monthly_count_limit": _i(0, 100),
    "permission.monthly_minutes_limit": _i(0, 10080),
    "permission.max_minutes_per_request": _i(1, 1440),
    "permission.past_grace_minutes": _i(0, 1440),
    "leave.backdate_days": _i(0, 365),
    "leave.allow_cancel_approved": Spec("bool"),
    "leave.max_advance_days": _i(0, 3650),  # 0 = no horizon
    "leave.reason_retention_days": _i(0, 3650),  # 0 = keep forever
    "auth.offline_cache_max_hours": _i(0, 720),  # 0 = off, otherwise 12..720 (checked in validate)
    "console.session_max_hours": _i(1, 24),
    "console.inactivity_timeout_minutes": _i(5, 60),
}


def _bad(key: str, msg: str) -> AppError:
    return AppError("VALIDATION_ERROR", msg, details=[{"field": key, "message": msg}])


def validate(key: str, value: Any) -> Any:
    """Returns the normalised value to store (integral floats become ints for integer keys)."""
    if key not in KNOWN:
        raise _bad(key, "unknown setting")
    spec = SPECS.get(key)
    if spec is None:  # a KNOWN key without a spec is a programming error; never accept it blindly
        raise _bad(key, "setting is not editable")
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise _bad(key, "must be true or false")
    elif spec.kind == "str":
        if not isinstance(value, str) or len(value) > 200:
            raise _bad(key, "must be a string of at most 200 characters")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _bad(key, "must be a number")
        if isinstance(value, float) and not math.isfinite(value):
            raise _bad(key, "must be a finite number")
        if spec.kind == "int":
            if isinstance(value, float):
                if not value.is_integer():
                    raise _bad(key, "must be a whole number")
                value = int(value)
        else:
            value = float(value)
        assert spec.lo is not None and spec.hi is not None
        if not spec.lo <= value <= spec.hi:
            raise _bad(key, f"must be between {spec.lo:g} and {spec.hi:g}")
    if key == "auth.offline_cache_max_hours" and 0 < value < 12:
        raise _bad(key, "must be 0 (off) or between 12 and 720")
    food_svc.validate_food_setting(key, value)
    att.validate_office_setting(key, value)
    return value


async def read_all(session: AsyncSession) -> dict[str, Any]:
    rows = {r.key: r.value for r in (await session.execute(select(Setting))).scalars()}
    return {"settings": {k: rows.get(k, d) for k, d in KNOWN.items()}}


async def update(session: AsyncSession, actor: Principal, changes: dict[str, Any]) -> dict[str, Any]:
    changes = {k: validate(k, v) for k, v in changes.items()}
    keys = list(changes)
    if "food.cutoff_time" in changes or "office.timezone" in changes:
        keys += ["food.cutoff_time", "office.timezone"]
    rows = {r.key: r for r in (await session.execute(select(Setting).where(Setting.key.in_(keys)))).scalars()}

    def effective(k: str) -> Any:
        return changes[k] if k in changes else (rows[k].value if k in rows else KNOWN[k])

    if "food.cutoff_time" in changes or "office.timezone" in changes:
        # L2: a wall-clock cut-off inside a DST gap does not exist in the office timezone and would resolve ~1h late
        t, tzname = effective("food.cutoff_time"), effective("office.timezone")
        if food_svc.parse_hhmm(t) is not None and food_svc.cutoff_time_nonexistent(t, tzname):
            raise _bad(
                "food.cutoff_time", f"{t} does not exist on some days in {tzname} (daylight-saving gap); pick another time"
            )
    log_changes: list[dict[str, Any]] = []
    for k, v in changes.items():
        row = rows.get(k)
        old = row.value if row is not None else KNOWN[k]
        if row is None:
            try:
                async with session.begin_nested():  # a concurrent first write of the same key must not answer 500 (review L2)
                    session.add(Setting(key=k, value=v, updated_by=actor.id))
                    await session.flush()
            except IntegrityError as exc:  # the other writer won: update its row instead
                row = (
                    await session.execute(select(Setting).where(Setting.key == k).execution_options(populate_existing=True))
                ).scalar_one_or_none()
                if row is None:
                    raise AppError("CONFLICT", "Settings were changed concurrently; retry", status=409) from exc
                old = row.value
                row.value, row.updated_by = v, actor.id
        elif row.value != v:
            row.value = v
            row.updated_by = actor.id
        if old != v:
            log_changes.append({"key": k, "before": old, "after": v})  # list of objects: key names are values, never masked
    if log_changes:
        audit(session, action="settings.update", actor=actor, entity_type="settings", after={"changes": log_changes})
    await session.commit()
    return await read_all(session)
