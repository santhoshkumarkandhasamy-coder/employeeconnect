"""Attendance business logic (plan 02). Server time is authoritative; all "dates" are office-timezone dates."""

from __future__ import annotations

import asyncio
import calendar as _cal
import datetime as dt
import logging
import re
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import as_utc, iso_z, utcnow
from ..core.errors import AppError
from ..core.geo import haversine_m
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    Attendance,
    ClientLocation,
    File,
    Holiday,
    LateRecord,
    LeaveRequest,
    LeaveType,
    OnDutyRequest,
    PermissionRequest,
    Setting,
    Shift,
    User,
)
from . import files as files_svc
from .notifications import notify

log = logging.getLogger("app.attendance")

_DEFAULTS: dict[str, Any] = {
    "office.lat": 12.9716,
    "office.lng": 77.5946,
    "office.radius_m": 150,
    "office.timezone": "Asia/Kolkata",
    "attendance.selfie_required": False,
    "attendance.checkout_geofence": False,
    "attendance.max_accuracy_m": 100,
    "attendance.half_day_minutes": 240,
    "attendance.max_fix_age_s": 60,  # a GPS fix older than this (vs server time) is rejected
    "attendance.max_fix_future_s": 30,  # ... or this far in the future
    "attendance.reject_mock_location": True,  # False = accept but flag the row as suspicious
    "attendance.selfie_max_age_s": 600,  # a selfie must have been uploaded this recently
    "attendance.location_retention_days": 90,  # 0 = keep forever
    "attendance.selfie_retention_days": 90,  # 0 = keep forever
}
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
DEFAULT_WORKING_DAYS = (1, 2, 3, 4, 5)
MAX_RANGE_DAYS = 366  # cap for team / admin list queries
OVERNIGHT_BUFFER = dt.timedelta(hours=4)  # a night-shift record may be closed this long after the shift ends
SELFIE_CLOCK_TOLERANCE_S = 60
PUNCH_FAIL_CODES = frozenset(
    {"OUTSIDE_GEOFENCE", "OUTSIDE_CLIENT_LOCATION", "LOCATION_ACCURACY_LOW", "LOCATION_FIX_STALE", "MOCK_LOCATION_DETECTED"}
)


# ---------------------------------------------------------------- settings / time helpers
async def load_settings(session: AsyncSession) -> dict[str, Any]:
    q = select(Setting).where(Setting.key.in_(list(_DEFAULTS)))
    rows = {r.key: r.value for r in (await session.execute(q)).scalars()}
    return {k: rows.get(k, d) for k, d in _DEFAULTS.items()}


def office_tz(cfg: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(str(cfg["office.timezone"]))
    except Exception:  # noqa: BLE001 - a bad setting must not break attendance
        log.error("invalid office.timezone setting; falling back to default")
        return ZoneInfo(_DEFAULTS["office.timezone"])


def validate_office_setting(key: str, value: Any) -> None:
    """For the settings-write endpoint (module 09): reject values that would silently break attendance."""
    if key == "office.timezone":
        try:
            ZoneInfo(str(value))
        except Exception:  # noqa: BLE001
            raise AppError(
                "VALIDATION_ERROR", "Unknown timezone", details=[{"field": "value", "message": "must be an IANA timezone name"}]
            ) from None


def parse_month(month: str | None, default: dt.date) -> tuple[dt.date, dt.date]:
    if month is None:
        month = f"{default.year:04d}-{default.month:02d}"
    m = _MONTH.match(month)
    if not m:
        raise AppError("VALIDATION_ERROR", "month must be YYYY-MM", details=[{"field": "month", "message": "must be YYYY-MM"}])
    y, mo = int(m.group(1)), int(m.group(2))
    return dt.date(y, mo, 1), dt.date(y, mo, _cal.monthrange(y, mo)[1])


def check_range(d_from: dt.date | None, d_to: dt.date | None) -> None:
    if d_from and d_to and d_to < d_from:
        raise AppError("VALIDATION_ERROR", "to must be >= from", details=[{"field": "to", "message": "must be >= from"}])


def cap_range(
    d_from: dt.date | None, d_to: dt.date | None, today_d: dt.date, on_date: dt.date | None = None
) -> tuple[dt.date | None, dt.date | None]:
    """Bound a list query to MAX_RANGE_DAYS. Missing bounds are filled in, so there is never an unbounded scan."""
    check_range(d_from, d_to)
    if on_date is not None:
        return d_from, d_to
    span = dt.timedelta(days=MAX_RANGE_DAYS - 1)
    if d_from and d_to:
        if d_to - d_from > span:
            raise AppError(
                "INVALID_DATE_RANGE",
                f"Date range may not exceed {MAX_RANGE_DAYS} days",
                details=[{"field": "to", "message": f"at most {MAX_RANGE_DAYS} days after from"}],
            )
        return d_from, d_to
    if d_from:
        return d_from, d_from + span
    if d_to:
        return d_to - span, d_to
    return today_d - span, today_d


def _fmt_hm(t: dt.time) -> str:
    return t.strftime("%H:%M")


# ---------------------------------------------------------------- working-day helpers
async def holiday_map(session: AsyncSession, d_from: dt.date, d_to: dt.date) -> dict[dt.date, str]:
    q = select(Holiday).where(Holiday.date >= d_from, Holiday.date <= d_to, Holiday.is_active.is_(True))
    return {h.date: h.name for h in (await session.execute(q)).scalars()}


def is_weekly_off(shift: Shift | None, day: dt.date) -> bool:
    wd = list(shift.working_days) if shift is not None and shift.working_days else list(DEFAULT_WORKING_DAYS)
    return day.isoweekday() not in wd


async def working_day(session: AsyncSession, user: User, day: dt.date) -> bool:
    """True when `day` is a shift working day and not a holiday (leave is handled separately)."""
    if is_weekly_off(user.shift, day):
        return False
    return day not in await holiday_map(session, day, day)


async def _approved_leaves(session: AsyncSession, user_id: uuid.UUID, d_from: dt.date, d_to: dt.date) -> list[LeaveRequest]:
    q = select(LeaveRequest).where(
        LeaveRequest.user_id == user_id,
        LeaveRequest.status == "approved",
        LeaveRequest.from_date <= d_to,
        LeaveRequest.to_date >= d_from,
    )
    return list((await session.execute(q)).scalars())


async def on_duty_site(session: AsyncSession, user_id: uuid.UUID, day: dt.date) -> ClientLocation | None:
    """The client location of the user's APPROVED on-duty request covering `day` (plan 13), else None.
    On such a day punches are checked against this site instead of the office."""
    q = (
        select(ClientLocation)
        .join(OnDutyRequest, OnDutyRequest.client_location_id == ClientLocation.id)
        .where(
            OnDutyRequest.user_id == user_id,
            OnDutyRequest.status == "approved",
            OnDutyRequest.from_date <= day,
            OnDutyRequest.to_date >= day,
        )
        .limit(1)
    )
    return (await session.execute(q)).scalar_one_or_none()


async def _approved_on_duty(session: AsyncSession, user_id: uuid.UUID, d_from: dt.date, d_to: dt.date) -> list[tuple]:
    """[(from_date, to_date, client location name)] of approved on-duty requests touching the range."""
    q = (
        select(OnDutyRequest.from_date, OnDutyRequest.to_date, ClientLocation.name)
        .join(ClientLocation, ClientLocation.id == OnDutyRequest.client_location_id)
        .where(
            OnDutyRequest.user_id == user_id,
            OnDutyRequest.status == "approved",
            OnDutyRequest.from_date <= d_to,
            OnDutyRequest.to_date >= d_from,
        )
    )
    return list((await session.execute(q)).all())


def site_out(c: ClientLocation) -> dict[str, Any]:
    """The employee's own assigned site for today: coordinates are included (like the office in GET /config) so the app
    can show the live distance before punching."""
    return {"id": str(c.id), "name": c.name, "address": c.address, "lat": c.lat, "lng": c.lng, "radius_m": c.radius_m}


# ---------------------------------------------------------------- output
def user_summary(u: User, photo_file_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "id": str(u.id),
        "employee_id": u.employee_id,
        "full_name": u.full_name,
        "department": u.department.name if u.department else None,
        "photo_url": f"/api/files/{photo_file_id}" if photo_file_id else None,
    }


def _record_out(
    a: Attendance, shift: Shift | None, expose_distance: bool = False, site: ClientLocation | None = None
) -> dict[str, Any]:
    """`distance_m` is only exposed to HR/admin viewers (to an employee it would map the office boundary)."""
    return {
        "id": str(a.id),
        "work_date": a.work_date.isoformat(),
        "check_in_at": iso_z(a.check_in_at),
        "check_out_at": iso_z(a.check_out_at) if a.check_out_at else None,
        "status": a.status,
        "minutes_late": a.minutes_late,
        "worked_minutes": a.worked_minutes,
        "within_fence": a.check_in_within_fence,
        "distance_m": a.check_in_distance_m if expose_distance else None,
        "selfie_url": f"/api/files/{a.check_in_selfie_file_id}" if a.check_in_selfie_file_id else None,
        "auto_closed": a.auto_closed,
        "manual": a.manual,  # entered or corrected by HR/Admin
        # on-duty punch: the client site it was checked against (null = office)
        "client_location": {"id": str(site.id), "name": site.name} if site is not None else None,
        "shift": (
            {"start": _fmt_hm(shift.start_time), "end": _fmt_hm(shift.end_time), "grace_minutes": shift.grace_minutes}
            if shift
            else None
        ),
    }


async def _records_out(session: AsyncSession, rows: list[Attendance], expose_distance: bool = False) -> list[dict[str, Any]]:
    ids = {a.shift_id for a in rows if a.shift_id}
    shifts: dict[uuid.UUID, Shift] = {}
    if ids:
        shifts = {s.id: s for s in (await session.execute(select(Shift).where(Shift.id.in_(ids)))).scalars()}
    loc_ids = {a.client_location_id for a in rows if a.client_location_id}
    sites: dict[uuid.UUID, ClientLocation] = {}
    if loc_ids:
        sites = {c.id: c for c in (await session.execute(select(ClientLocation).where(ClientLocation.id.in_(loc_ids)))).scalars()}
    return [
        _record_out(a, shifts.get(a.shift_id), expose_distance, sites.get(a.client_location_id) if a.client_location_id else None)
        for a in rows
    ]


# ---------------------------------------------------------------- punch validation
async def _validate_selfie(
    session: AsyncSession,
    actor: Principal,
    file_id: uuid.UUID | None,
    required: bool,
    cfg: dict[str, Any],
    now: dt.datetime,
) -> uuid.UUID | None:
    """Owner + purpose + freshness + single use (a selfie is bound to exactly one punch of its owner)."""
    if file_id is None:
        if required:
            raise AppError("SELFIE_REQUIRED", "A selfie is required to mark attendance")
        return None
    f = await session.get(File, file_id)
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
    if used.first() is not None:
        raise AppError(
            "SELFIE_REUSED",
            "This selfie was already used. Take a new one.",
            details=[{"field": "selfie_file_id", "message": "already used"}],
        )
    return f.id


def _skew_s(body: Any, now: dt.datetime) -> int | None:
    if body.client_time is None:
        return None
    return int((now - as_utc(body.client_time)).total_seconds())  # type: ignore[operator]


def _log_diagnostics(actor: Principal, body: Any, now: dt.datetime) -> None:
    skew = _skew_s(body, now)
    if skew is not None and abs(skew) > 300:
        log.warning("client clock skew %ds for user %s", abs(skew), actor.id)
    if body.is_mock_location:
        log.warning("mock location flag reported by user %s", actor.id)


def _fence(cfg: dict[str, Any], body: Any, site: ClientLocation | None = None) -> tuple[int, bool, int]:
    """Distance to the office, or to the client `site` on an approved on-duty day."""
    if site is not None:
        dist = haversine_m(body.lat, body.lng, site.lat, site.lng)
        radius = site.radius_m
    else:
        dist = haversine_m(body.lat, body.lng, float(cfg["office.lat"]), float(cfg["office.lng"]))
        radius = int(cfg["office.radius_m"])
    return int(round(dist)), dist <= radius, radius


def _require_fix(cfg: dict[str, Any], body: Any, now: dt.datetime) -> None:
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


def _enforce_mock(cfg: dict[str, Any], body: Any) -> None:
    if body.is_mock_location and cfg["attendance.reject_mock_location"]:
        raise AppError("MOCK_LOCATION_DETECTED", "Mock or simulated locations are not allowed for attendance")


def _enforce_accuracy(cfg: dict[str, Any], body: Any) -> None:
    mx = float(cfg["attendance.max_accuracy_m"])
    if body.accuracy_m is None or body.accuracy_m > mx:
        raise AppError(
            "LOCATION_ACCURACY_LOW",
            "Location accuracy is too low. Move to an open area and try again.",
            details=[{"field": "accuracy_m", "message": "too low"}],
        )


def _outside(site: ClientLocation | None = None) -> AppError:
    # Deliberately no distance/radius: exact values let a client trilaterate the office and bisect the boundary.
    if site is not None:
        return AppError(
            "OUTSIDE_CLIENT_LOCATION",
            f"You are on duty today. Check in at the client location: {site.name}",
            details=[{"field": "within_fence", "message": "false"}],
        )
    return AppError(
        "OUTSIDE_GEOFENCE", "You are outside the office area", details=[{"field": "within_fence", "message": "false"}]
    )


async def _audit_reject(session: AsyncSession, actor: Principal, kind: str, reason: str) -> None:
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


def is_overnight(shift: Shift | None) -> bool:
    return shift is not None and shift.end_time < shift.start_time


async def _open_overnight_record(
    session: AsyncSession, user_id: uuid.UUID, local_now: dt.datetime, *, lock: bool = False
) -> Attendance | None:
    """Yesterday's still-open record of a night shift whose end (plus buffer) has not passed yet."""
    prev = local_now.date() - dt.timedelta(days=1)
    q = select(Attendance).where(Attendance.user_id == user_id, Attendance.work_date == prev, Attendance.check_out_at.is_(None))
    rec = (await session.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if rec is None or rec.shift_id is None:
        return None
    shift = await session.get(Shift, rec.shift_id)
    if not is_overnight(shift):
        return None
    end = dt.datetime.combine(prev + dt.timedelta(days=1), shift.end_time, tzinfo=local_now.tzinfo)  # type: ignore[union-attr]
    return rec if local_now.astimezone(dt.UTC) <= end.astimezone(dt.UTC) + OVERNIGHT_BUFFER else None


async def minutes_late_for(session: AsyncSession, user: User, shift: Shift, work_date: dt.date, local_in: dt.datetime) -> int:
    """Whole minutes after shift start (0 = not late). Late iff check-in > shift_start + grace (strict).
    All arithmetic is done on UTC instants, so DST transition days (and repeated/missing local times) are exact."""
    tz = local_in.tzinfo

    def at(day: dt.date, t: dt.time) -> dt.datetime:
        return dt.datetime.combine(day, t, tzinfo=tz).astimezone(dt.UTC)

    start = at(work_date, shift.start_time)
    grace = dt.timedelta(minutes=shift.grace_minutes)
    limit = start + grace
    check_in_utc = local_in.astimezone(dt.UTC)
    # An approved permission covering shift start .. to_time moves the limit to to_time + grace.
    perms = (
        await session.execute(
            select(PermissionRequest).where(
                PermissionRequest.user_id == user.id,
                PermissionRequest.request_date == work_date,
                PermissionRequest.status == "approved",
                PermissionRequest.from_time <= shift.start_time,
                PermissionRequest.to_time > shift.start_time,
            )
        )
    ).scalars()
    for p in perms:
        limit = max(limit, at(work_date, p.to_time) + grace)
    for lv in await _approved_leaves(session, user.id, work_date, work_date):
        if lv.half_day and lv.half_day_session == "first":  # morning is on leave: never late
            return 0
    if check_in_utc <= limit:
        return 0
    return max(1, int((check_in_utc - start).total_seconds() // 60))


# ---------------------------------------------------------------- check-in / check-out
async def check_in(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    user = actor.user
    now = utcnow()
    cfg = await load_settings(session)
    local = now.astimezone(office_tz(cfg))
    work_date = local.date()
    shift = user.shift
    if shift is None or not shift.is_active:
        raise AppError("NO_SHIFT_ASSIGNED", "No shift is assigned to you. Contact HR.")
    q = select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == work_date)
    existing = (await session.execute(q)).scalar_one_or_none()
    if existing is not None:
        await _audit_reject(session, actor, "check_in", "ALREADY_CHECKED_IN")
        raise AppError(
            "ALREADY_CHECKED_IN",
            "You have already checked in today",
            details=[{"field": "record_id", "message": str(existing.id)}],
        )
    for lv in await _approved_leaves(session, user.id, work_date, work_date):
        if not lv.half_day:
            raise AppError("ON_LEAVE", "You are on approved leave today")
    site = await on_duty_site(session, user.id, work_date)  # approved on duty: only the client site counts today
    try:
        _require_fix(cfg, body, now)
        _enforce_mock(cfg, body)
        _enforce_accuracy(cfg, body)
        selfie_id = await _validate_selfie(session, actor, body.selfie_file_id, bool(cfg["attendance.selfie_required"]), cfg, now)
        dist, within, _radius = _fence(cfg, body, site)
        if not within:
            raise _outside(site)
    except AppError as exc:
        await _audit_reject(session, actor, "check_in", exc.code)
        raise
    _log_diagnostics(actor, body, now)

    minutes_late = 0
    if await working_day(session, user, work_date):  # holiday / weekly-off check-in is overtime: never late
        minutes_late = await minutes_late_for(session, user, shift, work_date, local)
    mock = bool(body.is_mock_location)
    rec = Attendance(
        user_id=user.id,
        work_date=work_date,
        shift_id=shift.id,
        check_in_at=now,
        check_in_loc={"lat": body.lat, "lng": body.lng, "accuracy_m": body.accuracy_m},
        check_in_distance_m=dist,
        check_in_within_fence=within,
        check_in_selfie_file_id=selfie_id,
        client_location_id=site.id if site is not None else None,
        check_in_accuracy_m=body.accuracy_m,
        check_in_clock_skew_s=_skew_s(body, now),
        check_in_mock_location=mock,
        suspicious=mock,
        status="late" if minutes_late else "present",
        minutes_late=minutes_late,
    )
    session.add(rec)
    try:
        await session.flush()
        if minutes_late:
            session.add(
                LateRecord(
                    user_id=user.id,
                    attendance_id=rec.id,
                    work_date=work_date,
                    shift_start=shift.start_time,
                    grace_minutes=shift.grace_minutes,
                    check_in_at=now,
                    minutes_late=minutes_late,
                )
            )
            await session.flush()
        if mock:  # only reachable when attendance.reject_mock_location is off
            audit(session, action="attendance.suspicious_punch", actor=actor, entity_type="attendance", entity_id=rec.id,
                  after={"kind": "check_in", "reason": "mock_flag"})  # fmt: skip
        await session.commit()
    except IntegrityError:  # concurrent double tap (UNIQUE user/day) or a concurrent reuse of the same selfie
        await session.rollback()
        again = (await session.execute(q)).scalar_one_or_none()
        if again is None:
            raise AppError("SELFIE_REUSED", "This selfie was already used. Take a new one.") from None
        raise AppError("ALREADY_CHECKED_IN", "You have already checked in today") from None
    return _record_out(rec, shift, site=site)


async def check_out(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    user = actor.user
    now = utcnow()
    cfg = await load_settings(session)
    local = now.astimezone(office_tz(cfg))
    work_date = local.date()
    q = select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == work_date).with_for_update()
    rec = (await session.execute(q)).scalar_one_or_none()
    if rec is None:  # night shift: check-out after midnight belongs to yesterday's open record
        rec = await _open_overnight_record(session, user.id, local, lock=True)
    if rec is None:
        raise AppError("NOT_CHECKED_IN", "You have not checked in today")
    if rec.check_out_at is not None:
        await _audit_reject(session, actor, "check_out", "ALREADY_CHECKED_OUT")
        raise AppError("ALREADY_CHECKED_OUT", "You have already checked out today")
    mock = bool(body.is_mock_location)
    # an on-duty check-in is checked out against the same client site
    site = await session.get(ClientLocation, rec.client_location_id) if rec.client_location_id else None
    try:
        if cfg["attendance.checkout_geofence"]:
            _require_fix(cfg, body, now)
            _enforce_mock(cfg, body)
            _enforce_accuracy(cfg, body)
            _dist, within, _radius = _fence(cfg, body, site)
            if not within:
                raise _outside(site)
        selfie_id = await _validate_selfie(session, actor, body.selfie_file_id, False, cfg, now)
    except AppError as exc:
        await _audit_reject(session, actor, "check_out", exc.code)
        raise
    _log_diagnostics(actor, body, now)
    ci = as_utc(rec.check_in_at)
    if now <= ci:  # type: ignore[operator]
        raise AppError("BAD_REQUEST", "Check-out must be after check-in")
    worked = int((now - ci).total_seconds() // 60)  # type: ignore[operator]
    rec.check_out_at = now
    rec.check_out_loc = {"lat": body.lat, "lng": body.lng, "accuracy_m": body.accuracy_m}
    rec.check_out_selfie_file_id = selfie_id
    rec.check_out_mock_location = mock
    if mock:
        rec.suspicious = True
        audit(session, action="attendance.suspicious_punch", actor=actor, entity_type="attendance", entity_id=rec.id,
              after={"kind": "check_out", "reason": "mock_flag"})  # fmt: skip
    rec.worked_minutes = worked
    if worked < int(cfg["attendance.half_day_minutes"]):
        rec.status = "half_day"
    shift = await session.get(Shift, rec.shift_id) if rec.shift_id else None
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("SELFIE_REUSED", "This selfie was already used. Take a new one.") from None
    return _record_out(rec, shift, site=site)


async def today(session: AsyncSession, actor: Principal) -> dict[str, Any]:
    cfg = await load_settings(session)
    local = utcnow().astimezone(office_tz(cfg))
    q = select(Attendance).where(Attendance.user_id == actor.id, Attendance.work_date == local.date())
    rec = (await session.execute(q)).scalar_one_or_none()
    if rec is None:
        rec = await _open_overnight_record(session, actor.id, local)  # night shift still running after midnight
    site = await on_duty_site(session, actor.id, local.date())
    on_duty = site_out(site) if site is not None else None  # where to punch today (null = office)
    if rec is None:
        return {"record": None, "on_duty": on_duty}
    return {**(await _records_out(session, [rec]))[0], "on_duty": on_duty}


# ---------------------------------------------------------------- manual entry (HR / Admin, plan 14)
MANUAL_MAX_PAST_DAYS = 366


async def _manual_target(session: AsyncSession, actor: Principal, user_id: uuid.UUID) -> User:
    u = await session.get(User, user_id)
    if u is None or u.deleted_at is not None:
        raise AppError("NOT_FOUND", "Employee not found")
    if u.id == actor.id:
        raise AppError("FORBIDDEN", "You cannot enter or change your own attendance")
    return u


def _manual_date(cfg: dict[str, Any], work_date: dt.date) -> dt.date:
    today_d = utcnow().astimezone(office_tz(cfg)).date()
    if work_date > today_d:
        raise AppError("INVALID_DATE_RANGE", "Attendance cannot be entered for a future date",
                       details=[{"field": "work_date", "message": "future"}])  # fmt: skip
    if work_date < today_d - dt.timedelta(days=MANUAL_MAX_PAST_DAYS):
        raise AppError("INVALID_DATE_RANGE", f"Attendance can be entered at most {MANUAL_MAX_PAST_DAYS} days back",
                       details=[{"field": "work_date", "message": "too old"}])  # fmt: skip
    return today_d


def _manual_snapshot(a: Attendance | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "check_in_at": iso_z(a.check_in_at),
        "check_out_at": iso_z(a.check_out_at) if a.check_out_at else None,
        "status": a.status,
        "minutes_late": a.minutes_late,
        "manual": a.manual,
    }


async def manual_get(session: AsyncSession, actor: Principal, user_id: uuid.UUID, work_date: dt.date) -> dict[str, Any]:
    u = await session.get(User, user_id)
    if u is None or u.deleted_at is not None:
        raise AppError("NOT_FOUND", "Employee not found")
    q = select(Attendance).where(Attendance.user_id == u.id, Attendance.work_date == work_date)
    rec = (await session.execute(q)).scalar_one_or_none()
    photos = await files_svc.photo_file_ids(session, [u.id])
    out = (await _records_out(session, [rec], expose_distance=True))[0] if rec else None
    if rec is not None and rec.manual:
        out["manual_reason"] = rec.manual_reason  # type: ignore[index]
    return {"user": user_summary(u, photos.get(u.id)), "work_date": work_date.isoformat(), "record": out}


async def manual_upsert(
    session: AsyncSession, actor: Principal, user_id: uuid.UUID, work_date: dt.date, body: Any
) -> dict[str, Any]:
    """Create or correct one attendance day. Status, lateness and worked time are derived exactly like punches;
    the late record is rebuilt. Location / selfie evidence of an existing punch is kept. Audited and pushed."""
    user = await _manual_target(session, actor, user_id)
    cfg = await load_settings(session)
    tz = office_tz(cfg)
    _manual_date(cfg, work_date)
    q = select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == work_date).with_for_update()
    rec = (await session.execute(q)).scalar_one_or_none()
    shift = (await session.get(Shift, rec.shift_id)) if rec is not None and rec.shift_id else user.shift
    if shift is None:
        raise AppError("NO_SHIFT_ASSIGNED", "This employee has no shift. Assign one first.")
    for lv in await _approved_leaves(session, user.id, work_date, work_date):
        if not lv.half_day:
            raise AppError("ON_LEAVE", "The employee is on approved full-day leave that day. Cancel the leave first.")
    local_in = dt.datetime.combine(work_date, body.check_in.replace(second=0, microsecond=0), tzinfo=tz)
    local_out = None
    if body.check_out is not None:
        local_out = dt.datetime.combine(work_date, body.check_out.replace(second=0, microsecond=0), tzinfo=tz)
        if local_out <= local_in:
            if not is_overnight(shift):
                raise AppError("VALIDATION_ERROR", "Check-out must be after check-in",
                               details=[{"field": "check_out", "message": "before check-in"}])  # fmt: skip
            local_out += dt.timedelta(days=1)  # night shift: check-out the next morning
    now = utcnow()
    in_utc = local_in.astimezone(dt.UTC)
    out_utc = local_out.astimezone(dt.UTC) if local_out else None
    if in_utc > now or (out_utc is not None and out_utc > now):
        raise AppError("VALIDATION_ERROR", "Times cannot be in the future",
                       details=[{"field": "check_out" if in_utc <= now else "check_in", "message": "future"}])  # fmt: skip
    minutes_late = 0
    if await working_day(session, user, work_date):
        minutes_late = await minutes_late_for(session, user, shift, work_date, local_in)
    worked = int((out_utc - in_utc).total_seconds() // 60) if out_utc else None
    status = "late" if minutes_late else "present"
    if worked is not None and worked < int(cfg["attendance.half_day_minutes"]):
        status = "half_day"
    before = _manual_snapshot(rec)
    if rec is None:
        rec = Attendance(user_id=user.id, work_date=work_date, shift_id=shift.id, check_in_at=in_utc)
        session.add(rec)
    else:
        await session.execute(delete(LateRecord).where(LateRecord.attendance_id == rec.id))
    rec.check_in_at, rec.check_out_at, rec.worked_minutes = in_utc, out_utc, worked
    rec.status, rec.minutes_late, rec.auto_closed = status, minutes_late, False
    rec.manual, rec.manual_by, rec.manual_reason = True, actor.id, body.reason
    await session.flush()
    if minutes_late:
        session.add(
            LateRecord(
                user_id=user.id, attendance_id=rec.id, work_date=work_date, shift_start=shift.start_time,
                grace_minutes=shift.grace_minutes, check_in_at=in_utc, minutes_late=minutes_late,
            )
        )  # fmt: skip
    audit(
        session, action="attendance.manual", actor=actor, entity_type="attendance", entity_id=rec.id,
        before=before, after={**_manual_snapshot(rec), "owner": user.employee_id, "work_date": work_date.isoformat()},  # type: ignore[dict-item]
    )  # fmt: skip
    await session.commit()
    await notify(
        session, [user.id], type="attendance_manual", entity_id=rec.id,
        title="Attendance updated", body=f"HR updated your attendance for {work_date.strftime('%d %b %Y')}",
    )  # fmt: skip
    return await manual_get(session, actor, user.id, work_date)


async def manual_delete(session: AsyncSession, actor: Principal, user_id: uuid.UUID, work_date: dt.date) -> None:
    """Remove a MANUAL entry (a real punch cannot be deleted, only corrected)."""
    user = await _manual_target(session, actor, user_id)
    q = select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == work_date).with_for_update()
    rec = (await session.execute(q)).scalar_one_or_none()
    if rec is None:
        raise AppError("NOT_FOUND", "No attendance for this date")
    if not rec.manual:
        raise AppError("CONFLICT", "Only manual entries can be removed. Correct the times instead.", status=409)
    if rec.check_in_within_fence is not None or rec.check_in_selfie_file_id is not None:  # set only by a real punch
        raise AppError("CONFLICT", "This day has a real punch; correct the times instead of removing it.", status=409)
    before = _manual_snapshot(rec)
    await session.execute(delete(LateRecord).where(LateRecord.attendance_id == rec.id))
    await session.delete(rec)
    audit(
        session, action="attendance.manual_delete", actor=actor, entity_type="attendance", entity_id=rec.id,
        before={**before, "owner": user.employee_id, "work_date": work_date.isoformat()},  # type: ignore[dict-item]
    )  # fmt: skip
    await session.commit()


# ---------------------------------------------------------------- history / team
async def history(
    session: AsyncSession, actor: Principal, params: PageParams, d_from: dt.date | None, d_to: dt.date | None
) -> dict[str, Any]:
    check_range(d_from, d_to)
    cond = [Attendance.user_id == actor.id]
    if d_from:
        cond.append(Attendance.work_date >= d_from)
    if d_to:
        cond.append(Attendance.work_date <= d_to)
    total = (await session.execute(select(func.count()).select_from(Attendance).where(*cond))).scalar_one()
    q = select(Attendance).where(*cond).order_by(Attendance.work_date.desc()).limit(params.page_size).offset(params.offset)
    rows = list((await session.execute(q)).scalars())
    return make_page(await _records_out(session, rows), params, total)


def scope_conditions(
    actor: Principal, all_perm: str, team_perm: str, department_id: uuid.UUID | None, employee_id: str | None
) -> list:
    """Server-side scope: `*.view_all` sees everyone, `*.view_team` only direct reports."""
    cond: list = []
    if not actor.has(all_perm):
        if not actor.has(team_perm):
            raise AppError("FORBIDDEN", "You do not have permission to perform this action")
        cond.append(User.manager_id == actor.id)
    if department_id:
        cond.append(User.department_id == department_id)
    if employee_id:
        cond.append(User.employee_id == employee_id.strip().upper())
    return cond


async def team(
    session: AsyncSession,
    actor: Principal,
    params: PageParams,
    *,
    on_date: dt.date | None,
    d_from: dt.date | None,
    d_to: dt.date | None,
    department_id: uuid.UUID | None,
    employee_id: str | None,
) -> dict[str, Any]:
    cfg = await load_settings(session)
    d_from, d_to = cap_range(d_from, d_to, utcnow().astimezone(office_tz(cfg)).date(), on_date)
    cond = scope_conditions(actor, "attendance.view_all", "attendance.view_team", department_id, employee_id)
    if on_date:
        cond.append(Attendance.work_date == on_date)
    if d_from:
        cond.append(Attendance.work_date >= d_from)
    if d_to:
        cond.append(Attendance.work_date <= d_to)
    base = select(Attendance, User).join(User, User.id == Attendance.user_id).where(*cond)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    page_q = base.order_by(Attendance.work_date.desc(), User.employee_id).limit(params.page_size).offset(params.offset)
    rows = (await session.execute(page_q)).unique().all()
    outs = await _records_out(session, [a for a, _ in rows], expose_distance=actor.has("attendance.view_all"))
    photos = await files_svc.photo_file_ids(session, [u.id for _, u in rows])
    items = [{**o, "user": user_summary(u, photos.get(u.id))} for o, (_, u) in zip(outs, rows, strict=True)]
    return make_page(items, params, total)


# ---------------------------------------------------------------- calendar / summary
async def _month_days(session: AsyncSession, user: User, month: str | None) -> tuple[str, list[dict[str, Any]], list[Attendance]]:
    cfg = await load_settings(session)
    today_d = utcnow().astimezone(office_tz(cfg)).date()
    first, last = parse_month(month, today_d)
    q = select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date >= first, Attendance.work_date <= last)
    recs = {a.work_date: a for a in (await session.execute(q)).scalars()}
    holidays = await holiday_map(session, first, last)
    leaves = await _approved_leaves(session, user.id, first, last)
    duties = await _approved_on_duty(session, user.id, first, last)
    type_names: dict[uuid.UUID, str] = {}
    if leaves:
        ids = {lv.leave_type_id for lv in leaves}
        type_names = {lt.id: lt.name for lt in (await session.execute(select(LeaveType).where(LeaveType.id.in_(ids)))).scalars()}
    days: list[dict[str, Any]] = []
    d = first
    while d <= last:
        holiday_name = holidays.get(d)
        leave_type = next((type_names.get(lv.leave_type_id) for lv in leaves if lv.from_date <= d <= lv.to_date), None)
        covered = any(lv.from_date <= d <= lv.to_date for lv in leaves)
        on_duty_at = next((name for f, t, name in duties if f <= d <= t), None)
        if d in recs:
            status = recs[d].status
        elif holiday_name is not None:
            status = "holiday"
        elif is_weekly_off(user.shift, d):
            status = "weekly_off"
        elif covered:
            status = "on_leave"
        elif d < today_d:
            status = "absent"
        else:
            status = "future"  # today without a check-in yet is "future" (not absent until the day is over)
        days.append(
            {
                "date": d.isoformat(),
                "status": status,
                "holiday_name": holiday_name,
                "leave_type": leave_type if covered else None,
                "on_duty": on_duty_at,  # client location name on an approved on-duty day
            }
        )
        d += dt.timedelta(days=1)
    return f"{first.year:04d}-{first.month:02d}", days, list(recs.values())


async def calendar(session: AsyncSession, actor: Principal, month: str | None) -> dict[str, Any]:
    m, days, _ = await _month_days(session, actor.user, month)
    return {"month": m, "days": days}


async def summary(session: AsyncSession, actor: Principal, month: str | None) -> dict[str, Any]:
    """Counts are disjoint by calendar status (a late day counts under `late` only, not also `present`)."""
    m, days, recs = await _month_days(session, actor.user, month)
    counts = {k: 0 for k in ("present", "late", "absent", "on_leave", "half_day")}
    for d in days:
        if d["status"] in counts:
            counts[d["status"]] += 1
    return {"month": m, **counts, "total_worked_minutes": sum(a.worked_minutes or 0 for a in recs)}


# ---------------------------------------------------------------- holidays
def _holiday_out(h: Holiday) -> dict[str, Any]:
    return {"id": str(h.id), "date": h.date.isoformat(), "name": h.name}


async def list_holidays(session: AsyncSession, year: int | None) -> dict[str, Any]:
    year = year or utcnow().year
    q = (
        select(Holiday)
        .where(Holiday.date >= dt.date(year, 1, 1), Holiday.date <= dt.date(year, 12, 31), Holiday.is_active.is_(True))
        .order_by(Holiday.date)
    )
    return {"items": [_holiday_out(h) for h in (await session.execute(q)).scalars()]}


async def _holiday_or_404(session: AsyncSession, holiday_id: uuid.UUID) -> Holiday:
    h = await session.get(Holiday, holiday_id)
    if h is None:
        raise AppError("NOT_FOUND", "Holiday not found")
    return h


async def _date_taken(session: AsyncSession, day: dt.date, except_id: uuid.UUID | None = None) -> bool:
    q = select(Holiday.id).where(Holiday.date == day)
    if except_id:
        q = q.where(Holiday.id != except_id)
    return (await session.execute(q)).first() is not None


async def create_holiday(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    if await _date_taken(session, body.date):
        raise AppError("CONFLICT", "A holiday already exists on that date")
    h = Holiday(date=body.date, name=body.name.strip())
    session.add(h)
    await session.flush()
    audit(session, action="holiday.create", actor=actor, entity_type="holiday", entity_id=h.id, after=_holiday_out(h))
    await session.commit()
    return _holiday_out(h)


async def update_holiday(session: AsyncSession, actor: Principal, holiday_id: uuid.UUID, body: Any) -> dict[str, Any]:
    h = await _holiday_or_404(session, holiday_id)
    before = {**_holiday_out(h), "is_active": h.is_active}
    if body.date is not None and body.date != h.date:
        if await _date_taken(session, body.date, h.id):
            raise AppError("CONFLICT", "A holiday already exists on that date")
        h.date = body.date
    if body.name is not None:
        h.name = body.name.strip()
    if body.is_active is not None:
        h.is_active = body.is_active
    audit(
        session, action="holiday.update", actor=actor, entity_type="holiday", entity_id=h.id,
        before=before, after={**_holiday_out(h), "is_active": h.is_active},
    )  # fmt: skip
    await session.commit()
    return _holiday_out(h)


async def delete_holiday(session: AsyncSession, actor: Principal, holiday_id: uuid.UUID) -> None:
    h = await _holiday_or_404(session, holiday_id)
    audit(session, action="holiday.delete", actor=actor, entity_type="holiday", entity_id=h.id, before=_holiday_out(h))
    await session.delete(h)
    await session.commit()


# ---------------------------------------------------------------- jobs (the worker schedules them)
async def auto_close_open_records(session: AsyncSession, now: dt.datetime | None = None) -> int:
    """Flag records of past work dates that never checked out: auto_closed=true while check_out_at and worked_minutes
    stay NULL (HR reviews them). Idempotent. Night-shift records stay open until their shift end + a buffer.
    Returns the number of records flagged."""
    now = as_utc(now) or utcnow()
    cfg = await load_settings(session)
    tz = office_tz(cfg)
    today_d = now.astimezone(tz).date()
    q = select(Attendance).where(
        Attendance.check_out_at.is_(None), Attendance.auto_closed.is_(False), Attendance.work_date < today_d
    )
    rows = list((await session.execute(q.with_for_update(skip_locked=True))).scalars())
    shifts = {}
    ids = {a.shift_id for a in rows if a.shift_id}
    if ids:
        shifts = {s.id: s for s in (await session.execute(select(Shift).where(Shift.id.in_(ids)))).scalars()}
    closed = 0
    for a in rows:
        shift = shifts.get(a.shift_id)
        if is_overnight(shift):
            end = dt.datetime.combine(a.work_date + dt.timedelta(days=1), shift.end_time, tzinfo=tz)  # type: ignore[union-attr]
            if now <= end.astimezone(dt.UTC) + OVERNIGHT_BUFFER:
                continue
        a.auto_closed = True
        closed += 1
    if closed:
        audit(
            session,
            action="attendance.auto_close",
            entity_type="attendance",
            after={"count": closed, "before_date": today_d.isoformat()},
        )
    await session.commit()
    return closed


async def purge_pii(session: AsyncSession, now: dt.datetime | None = None, batch: int = 500) -> dict[str, int]:
    """Retention: null encrypted punch coordinates and delete selfie files of attendance older than the configured
    number of days (`attendance.location_retention_days` / `attendance.selfie_retention_days`, 0 = keep)."""
    now = as_utc(now) or utcnow()
    cfg = await load_settings(session)
    today_d = now.astimezone(office_tz(cfg)).date()
    out = {"locations": 0, "selfies": 0}

    loc_days = int(cfg["attendance.location_retention_days"])
    if loc_days > 0:
        cutoff = today_d - dt.timedelta(days=loc_days)
        res = await session.execute(
            update(Attendance)
            .where(
                Attendance.work_date < cutoff,
                or_(Attendance.check_in_loc.is_not(None), Attendance.check_out_loc.is_not(None)),
            )
            .values(check_in_loc=None, check_out_loc=None)
            .execution_options(synchronize_session=False)
        )
        out["locations"] = res.rowcount or 0

    selfie_days = int(cfg["attendance.selfie_retention_days"])
    if selfie_days > 0:
        cutoff = today_d - dt.timedelta(days=selfie_days)
        q = (
            select(File)
            .join(Attendance, or_(Attendance.check_in_selfie_file_id == File.id, Attendance.check_out_selfie_file_id == File.id))
            .where(File.purpose == "selfie", Attendance.work_date < cutoff)
            .distinct()
            .limit(batch)
        )
        for f in (await session.execute(q)).scalars().all():
            await session.execute(
                update(Attendance).where(Attendance.check_in_selfie_file_id == f.id).values(check_in_selfie_file_id=None)
            )
            await session.execute(
                update(Attendance).where(Attendance.check_out_selfie_file_id == f.id).values(check_out_selfie_file_id=None)
            )
            try:
                await asyncio.to_thread(files_svc._unlink, files_svc._resolve(f.storage_path))
            except AppError:
                log.error("selfie %s has an invalid storage path; row dropped only", f.id)
            await session.delete(f)
            out["selfies"] += 1
    if any(out.values()):
        audit(
            session,
            action="attendance.retention_purge",
            entity_type="attendance",
            after={"loc_rows": out["locations"], "selfie_files": out["selfies"]},
        )
    await session.commit()
    return out
