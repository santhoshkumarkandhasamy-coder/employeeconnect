"""Punch validators shared by office attendance and On Duty visits (plan 13, backend task 1).

Extracted from services/attendance.py without behaviour change for office punches: a GPS fix must carry accuracy and a fresh
`fix_time` (server time is the reference), mock locations and low accuracy are refused per settings, a selfie must be the
caller's own, recent, and used at most once across office AND visit punches. Refusals are audited (reason only, never
coordinates) and committed before the error is raised."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import as_utc
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import Attendance, File, OnDutyVisit

log = logging.getLogger("app.attendance")

SELFIE_CLOCK_TOLERANCE_S = 60
# Refused punches that count towards the shared punch-failure cool-down (office and on-duty share ONE budget per user).
PUNCH_FAIL_CODES = frozenset(
    {"OUTSIDE_GEOFENCE", "OUTSIDE_DUTY_LOCATION", "LOCATION_ACCURACY_LOW", "LOCATION_FIX_STALE", "MOCK_LOCATION_DETECTED"}
)


async def validate_selfie(
    session: AsyncSession,
    actor: Principal,
    file_id: uuid.UUID | None,
    required: bool,
    cfg: dict[str, Any],
    now: dt.datetime,
) -> uuid.UUID | None:
    """Owner + purpose + freshness + single use (a selfie is bound to exactly one punch of its owner, office or visit)."""
    if file_id is None:
        if required:
            raise AppError("SELFIE_REQUIRED", "A selfie is required to mark attendance")
        return None
    # Row lock on the file (review 11 L1): held until this punch transaction commits or rolls back, so a concurrent office /
    # visit punch with the same selfie waits here and then SEES the committed use below (READ COMMITTED re-reads per
    # statement). `files` has no used_at column, and the "used" state lives in two other tables, hence a lock instead of
    # `UPDATE ... WHERE used_at IS NULL`.
    f = (
        await session.execute(
            select(File).where(File.id == file_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if f is None or f.owner_id != actor.id or f.purpose != "selfie":
        raise AppError(
            "VALIDATION_ERROR", "Invalid selfie file", details=[{"field": "selfie_file_id", "message": "unknown or not yours"}]
        )
    age = (now - as_utc(f.created_at)).total_seconds()  # type: ignore[operator]
    if age > float(cfg["attendance.selfie_max_age_s"]) or age < -SELFIE_CLOCK_TOLERANCE_S:
        raise AppError(
            "SELFIE_STALE", "This selfie is too old. Take a new one.", details=[{"field": "selfie_file_id", "message": "stale"}]
        )
    used = await session.execute(
        select(Attendance.id)
        .where(or_(Attendance.check_in_selfie_file_id == f.id, Attendance.check_out_selfie_file_id == f.id))
        .limit(1)
    )
    used_visit = await session.execute(
        select(OnDutyVisit.id)
        .where(or_(OnDutyVisit.check_in_selfie_file_id == f.id, OnDutyVisit.check_out_selfie_file_id == f.id))
        .limit(1)
    )
    if used.first() is not None or used_visit.first() is not None:
        raise AppError(
            "SELFIE_REUSED",
            "This selfie was already used. Take a new one.",
            details=[{"field": "selfie_file_id", "message": "already used"}],
        )
    return f.id


def skew_s(body: Any, now: dt.datetime) -> int | None:
    if body.client_time is None:
        return None
    return int((now - as_utc(body.client_time)).total_seconds())  # type: ignore[operator]


def log_diagnostics(actor: Principal, body: Any, now: dt.datetime) -> None:
    skew = skew_s(body, now)
    if skew is not None and abs(skew) > 300:
        log.warning("client clock skew %ds for user %s", abs(skew), actor.id)
    if body.is_mock_location:
        log.warning("mock location flag reported by user %s", actor.id)


def require_fix(cfg: dict[str, Any], body: Any, now: dt.datetime) -> None:
    """accuracy_m and a fresh fix_time are mandatory; server time is the reference (the client clock is never trusted)."""
    missing = [f for f in ("accuracy_m", "fix_time") if getattr(body, f) is None]
    if missing:
        raise AppError(
            "VALIDATION_ERROR",
            "Location accuracy and fix time are required",
            details=[{"field": f, "message": "required"} for f in missing],
        )
    age = (now - as_utc(body.fix_time)).total_seconds()  # type: ignore[operator]
    if age > float(cfg["attendance.max_fix_age_s"]) or age < -float(cfg["attendance.max_fix_future_s"]):
        raise AppError(
            "LOCATION_FIX_STALE",
            "Your location reading is out of date. Wait for a fresh GPS fix and try again.",
            details=[{"field": "fix_time", "message": "stale" if age > 0 else "in the future"}],
        )


def enforce_mock(cfg: dict[str, Any], body: Any) -> None:
    if body.is_mock_location and cfg["attendance.reject_mock_location"]:
        raise AppError("MOCK_LOCATION_DETECTED", "Mock or simulated locations are not allowed for attendance")


def enforce_accuracy(cfg: dict[str, Any], body: Any) -> None:
    mx = float(cfg["attendance.max_accuracy_m"])
    if body.accuracy_m is None or body.accuracy_m > mx:
        raise AppError(
            "LOCATION_ACCURACY_LOW",
            "Location accuracy is too low. Move to an open area and try again.",
            details=[{"field": "accuracy_m", "message": "too low"}],
        )


async def audit_reject(session: AsyncSession, actor: Principal, kind: str, reason: str) -> None:
    """Refused punches are audited (reason only, never coordinates) and committed before the error is raised."""
    audit(
        session,
        action="attendance.punch_rejected",
        actor=actor,
        entity_type="attendance",
        after={"kind": kind, "reason": reason},
        outcome="failure",
    )
    await session.commit()
