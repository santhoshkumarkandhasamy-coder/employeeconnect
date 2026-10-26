"""On Duty: visit check-in / check-out at approved client sites and branches, plus on-duty requests (plan 13).

Scope guard: ONE-SHOT location reads at visit check-in and check-out only (same as office attendance). Nothing here tracks
position continuously.

The `attendance` row stays the single "present" source of truth: a visit writes INTO the day's row (work_mode, check_out_at via
max(), status re-evaluated on every closing punch), so payroll, the calendar and the dashboards need no extra logic.
Visit coordinates, notes, request purposes and decision comments are encrypted at rest and never reach audit rows, push
payloads or exports.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from types import SimpleNamespace
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import as_utc, iso_z
from ..core.errors import AppError
from ..core.geo import haversine_m
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    Attendance,
    DutyLocation,
    LateRecord,
    LeaveRequest,
    OnDutyRequest,
    OnDutyVisit,
    Setting,
    Shift,
    User,
)
from . import attendance as att
from . import duty_locations as loc_svc
from . import files as files_svc
from . import leave as leave_svc
from . import punch
from .notifications import notify

log = logging.getLogger("app.on_duty")

DEFAULTS: dict[str, Any] = {
    "on_duty.enabled": True,
    "on_duty.approval_required": False,
    "on_duty.apply_late_rule": False,
    "on_duty.checkout_geofence": False,
    "on_duty.selfie_required": False,
    "on_duty.max_visits_per_day": 10,
    "on_duty.request_backdate_days": 0,
    "on_duty.request_max_advance_days": 60,
}
MAX_REQUEST_DAYS = 31
MAX_VISIT_HISTORY_DAYS = 93


# ---------------------------------------------------------------- settings / small helpers
async def load_cfg(session: AsyncSession) -> dict[str, Any]:
    cfg = await att.load_settings(session)
    rows = {r.key: r.value for r in (await session.execute(select(Setting).where(Setting.key.in_(list(DEFAULTS))))).scalars()}
    cfg.update({k: rows.get(k, d) for k, d in DEFAULTS.items()})
    return cfg


def _now() -> dt.datetime:
    return att.utcnow()  # one patch point shared with office attendance


def _today(cfg: dict[str, Any]) -> dt.date:
    return _now().astimezone(att.office_tz(cfg)).date()


def _err(code: str, message: str, field: str | None = None, value: str | None = None) -> AppError:
    return AppError(code, message, details=[{"field": field, "message": value or message}] if field else None)


def _outside() -> AppError:
    # no distance and no radius: either would let a client bisect the site boundary
    return AppError(
        "OUTSIDE_DUTY_LOCATION", "You are outside the duty location", details=[{"field": "within_fence", "message": "false"}]
    )


def _unavailable() -> AppError:
    # unknown, inactive and unassigned answer identically (no enumeration of locations)
    return _err("DUTY_LOCATION_UNAVAILABLE", "This location is not available to you", "location_id", "unavailable")


def _open_visit_cond(user_id: uuid.UUID) -> list:
    return [OnDutyVisit.user_id == user_id, OnDutyVisit.check_out_at.is_(None), OnDutyVisit.auto_closed.is_(False)]


async def _open_visit(session: AsyncSession, user_id: uuid.UUID, *, lock: bool = False) -> OnDutyVisit | None:
    q = select(OnDutyVisit).where(*_open_visit_cond(user_id))
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(q)).scalars().first()


async def _approved_covering(session: AsyncSession, user_id: uuid.UUID, day: dt.date) -> list[OnDutyRequest]:
    q = select(OnDutyRequest).where(
        OnDutyRequest.user_id == user_id,
        OnDutyRequest.status == "approved",
        OnDutyRequest.from_date <= day,
        OnDutyRequest.to_date >= day,
    )
    return list((await session.execute(q)).scalars())


async def _full_day_leave_today(session: AsyncSession, user_id: uuid.UUID, day: dt.date) -> bool:
    return any(not lv.half_day for lv in await att._approved_leaves(session, user_id, day, day))


# ---------------------------------------------------------------- output
def visit_out(v: OnDutyVisit) -> dict[str, Any]:
    ci, co = as_utc(v.check_in_at), as_utc(v.check_out_at)
    return {
        "id": str(v.id),
        "work_date": v.work_date.isoformat(),
        "location": {"id": str(v.location_id), "name": v.location_name},
        "check_in_at": iso_z(v.check_in_at),
        "check_out_at": iso_z(v.check_out_at) if v.check_out_at else None,
        "visit_minutes": int((co - ci).total_seconds() // 60) if co is not None and ci is not None else None,
        "open": v.check_out_at is None and not v.auto_closed,
        "auto_closed": v.auto_closed,
        "note": v.note,
        "check_in_selfie_url": files_svc.file_url(v.check_in_selfie_file_id),
        "check_out_selfie_url": files_svc.file_url(v.check_out_selfie_file_id),
        "request_id": str(v.request_id) if v.request_id else None,
    }


async def _attendance_out(session: AsyncSession, rec: Attendance) -> dict[str, Any]:
    shift = await session.get(Shift, rec.shift_id) if rec.shift_id else None
    locs = await att.visit_locations(session, [rec.id])
    return att._record_out(rec, shift, on_duty_locations=locs.get(rec.id))


# ---------------------------------------------------------------- employee: locations / today / history
async def my_locations(session: AsyncSession, actor: Principal) -> dict[str, Any]:
    cfg = await load_cfg(session)
    if not cfg["on_duty.enabled"]:
        return {"items": []}
    return {"items": [loc_svc.employee_out(loc) for loc in await loc_svc.assigned_active_locations(session, actor.id)]}


async def has_locations(session: AsyncSession, user_id: uuid.UUID, cfg: dict[str, Any]) -> bool:
    if not cfg["on_duty.enabled"]:
        return False
    return bool(await loc_svc.assigned_active_locations(session, user_id))


async def config_block(session: AsyncSession, actor: Principal | None) -> dict[str, Any]:
    """`has_locations` = enabled AND the caller has >= 1 active assigned location (False without a caller)."""
    cfg = await load_cfg(session)
    return {
        "enabled": bool(cfg["on_duty.enabled"]),
        "has_locations": actor is not None and await has_locations(session, actor.id, cfg),
        "approval_required": bool(cfg["on_duty.approval_required"]),
        "selfie_required": bool(cfg["on_duty.selfie_required"]),
        "checkout_geofence": bool(cfg["on_duty.checkout_geofence"]),
        "max_visits_per_day": int(cfg["on_duty.max_visits_per_day"]),
        "request_backdate_days": int(cfg["on_duty.request_backdate_days"]),
        "request_max_advance_days": int(cfg["on_duty.request_max_advance_days"]),
    }


async def today(session: AsyncSession, actor: Principal) -> dict[str, Any]:
    cfg = await load_cfg(session)
    day = _today(cfg)
    user = actor.user
    rec = (
        await session.execute(select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == day))
    ).scalar_one_or_none()
    visits = list(
        (
            await session.execute(
                select(OnDutyVisit)
                .where(OnDutyVisit.user_id == user.id, OnDutyVisit.work_date == day)
                .order_by(OnDutyVisit.check_in_at)
            )
        ).scalars()
    )
    open_visit = await _open_visit(session, user.id)
    covering = await _approved_covering(session, user.id, day)
    location_ids = sorted({str(r.location_id) for r in covering if r.location_id})
    any_location = any(r.location_id is None for r in covering)
    required = bool(cfg["on_duty.approval_required"])
    max_visits = int(cfg["on_duty.max_visits_per_day"])
    assigned = await loc_svc.assigned_active_locations(session, user.id) if cfg["on_duty.enabled"] else []

    blocked: str | None = None
    if not cfg["on_duty.enabled"]:
        blocked = "ON_DUTY_DISABLED"
    elif user.shift is None or not user.shift.is_active:
        blocked = "NO_SHIFT_ASSIGNED"
    elif not assigned:
        blocked = "NO_LOCATIONS"
    elif await _full_day_leave_today(session, user.id, day):
        blocked = "ON_LEAVE"
    elif open_visit is not None:
        blocked = "VISIT_OPEN"
    elif len(visits) >= max_visits:
        blocked = "MAX_VISITS_REACHED"
    elif required and not (any_location or {str(loc.id) for loc in assigned} & set(location_ids)):
        blocked = "ON_DUTY_NOT_APPROVED"
    return {
        "work_date": day.isoformat(),
        "enabled": bool(cfg["on_duty.enabled"]),
        "attendance": await _attendance_out(session, rec) if rec is not None else None,
        "open_visit": visit_out(open_visit) if open_visit is not None else None,
        "visits": [visit_out(v) for v in visits],
        "visits_remaining": max(0, max_visits - len(visits)),
        "approval": {"required": required, "covered": bool(covering), "location_ids": location_ids, "any_location": any_location},
        "can_check_in": blocked is None,
        "blocked_reason": blocked,
    }


async def my_visits(
    session: AsyncSession, actor: Principal, params: PageParams, d_from: dt.date | None, d_to: dt.date | None
) -> dict[str, Any]:
    cfg = await load_cfg(session)
    d_to = d_to or _today(cfg)
    d_from = d_from or d_to - dt.timedelta(days=30)
    if d_to < d_from or (d_to - d_from).days > MAX_VISIT_HISTORY_DAYS:
        raise _err("INVALID_DATE_RANGE", f"Range must be 0-{MAX_VISIT_HISTORY_DAYS} days with to >= from", "to")
    cond = [OnDutyVisit.user_id == actor.id, OnDutyVisit.work_date >= d_from, OnDutyVisit.work_date <= d_to]
    total = (await session.execute(select(func.count()).select_from(OnDutyVisit).where(*cond))).scalar_one()
    rows = (
        await session.execute(
            select(OnDutyVisit)
            .where(*cond)
            .order_by(OnDutyVisit.check_in_at.desc())
            .limit(params.page_size)
            .offset(params.offset)
        )
    ).scalars()
    return make_page([visit_out(v) for v in rows], params, total)


# ---------------------------------------------------------------- check-in
async def _lock_or_create_day(
    session: AsyncSession, user: User, shift: Shift, work_date: dt.date, now: dt.datetime, local: dt.datetime, cfg: dict[str, Any]
) -> tuple[Attendance, bool]:
    q = select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == work_date)
    locked = q.with_for_update().execution_options(populate_existing=True)
    rec = (await session.execute(locked)).scalar_one_or_none()
    if rec is not None:
        return rec, False
    minutes_late = 0
    if cfg["on_duty.apply_late_rule"] and await att.working_day(session, user, work_date):
        minutes_late = await att.minutes_late_for(session, user, shift, work_date, local)
    rec = Attendance(
        user_id=user.id,
        work_date=work_date,
        shift_id=shift.id,
        check_in_at=now,
        status="late" if minutes_late else "present",
        minutes_late=minutes_late,
        work_mode="on_duty",
    )
    try:
        async with session.begin_nested():
            session.add(rec)
            await session.flush()
    except IntegrityError:  # a concurrent first punch of the day created it: use that row
        rec = (await session.execute(locked)).scalar_one_or_none()
        if rec is None:
            raise
        return rec, False
    return rec, True


async def check_in(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    user = actor.user
    user_id = user.id
    now = _now()
    cfg = await load_cfg(session)
    local = now.astimezone(att.office_tz(cfg))
    work_date = local.date()
    if not cfg["on_duty.enabled"]:
        raise AppError("ON_DUTY_DISABLED", "On Duty is not enabled")
    shift = user.shift
    if shift is None or not shift.is_active:
        raise AppError("NO_SHIFT_ASSIGNED", "No shift is assigned to you. Contact HR.")
    loc = await loc_svc.assigned_active_location(session, user.id, body.location_id)
    if loc is None:
        raise _unavailable()
    if await _full_day_leave_today(session, user.id, work_date):
        raise AppError("ON_LEAVE", "You are on approved leave today")
    open_v = await _open_visit(session, user.id)
    if open_v is not None:
        raise _err("VISIT_OPEN", "You already have an open on-duty visit", "visit_id", str(open_v.id))
    n_today = (
        await session.execute(
            select(func.count())
            .select_from(OnDutyVisit)
            .where(OnDutyVisit.user_id == user.id, OnDutyVisit.work_date == work_date)
        )
    ).scalar_one()
    if n_today >= int(cfg["on_duty.max_visits_per_day"]):
        raise AppError("MAX_VISITS_REACHED", "You have reached the maximum number of on-duty visits for today")
    covering = await _approved_covering(session, user.id, work_date)
    usable = [r for r in covering if r.location_id in (loc.id, None)]
    if cfg["on_duty.approval_required"] and not usable:
        raise AppError("ON_DUTY_NOT_APPROVED", "An approved on-duty request is needed for this location today")
    request = next((r for r in usable if r.location_id == loc.id), usable[0] if usable else None)
    request_id = request.id if request is not None else None

    try:
        punch.require_fix(cfg, body, now)
        punch.enforce_mock(cfg, body)
        punch.enforce_accuracy(cfg, body)
        selfie_id = await punch.validate_selfie(
            session, actor, body.selfie_file_id, bool(cfg["on_duty.selfie_required"]), cfg, now
        )
        dist = haversine_m(body.lat, body.lng, loc.lat, loc.lng)
        if dist > loc.radius_m:
            raise _outside()
    except AppError as exc:
        await punch.audit_reject(session, actor, "on_duty_check_in", exc.code)
        raise
    punch.log_diagnostics(actor, body, now)

    mock = bool(body.is_mock_location)
    rec, created = await _lock_or_create_day(session, user, shift, work_date, now, local, cfg)
    if not created and rec.work_mode == "office":
        rec.work_mode = "mixed"
    visit = OnDutyVisit(
        user_id=user.id,
        attendance_id=rec.id,
        work_date=work_date,
        location_id=loc.id,
        location_name=loc.name,
        request_id=request_id,
        check_in_at=now,
        check_in_loc={"lat": body.lat, "lng": body.lng, "accuracy_m": body.accuracy_m},
        check_in_distance_m=int(round(dist)),
        check_in_accuracy_m=body.accuracy_m,
        check_in_clock_skew_s=punch.skew_s(body, now),
        check_in_mock_location=mock,
        check_in_selfie_file_id=selfie_id,
        note=body.note,
        suspicious=mock,
    )
    session.add(visit)
    try:
        await session.flush()
        if created and rec.minutes_late:
            session.add(
                LateRecord(
                    user_id=user.id,
                    attendance_id=rec.id,
                    work_date=work_date,
                    shift_start=shift.start_time,
                    grace_minutes=shift.grace_minutes,
                    check_in_at=now,
                    minutes_late=rec.minutes_late,
                )
            )
        if mock:  # only reachable when attendance.reject_mock_location is off
            audit(session, action="on_duty.suspicious_punch", actor=actor, entity_type="on_duty_visit", entity_id=visit.id,
                  after={"kind": "check_in", "reason": "mock_flag"})  # fmt: skip
        await session.commit()
    except IntegrityError:  # double tap (one-open-visit unique index) or a concurrent reuse of the same selfie
        await session.rollback()  # expires the ORM objects: only plain values below
        again = await _open_visit(session, user_id)
        if again is not None:
            raise _err("VISIT_OPEN", "You already have an open on-duty visit", "visit_id", str(again.id)) from None
        raise AppError("SELFIE_REUSED", "This selfie was already used. Take a new one.") from None
    return {"visit": visit_out(visit), "attendance": await _attendance_out(session, rec)}


# ---------------------------------------------------------------- check-out
async def check_out(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    now = _now()
    cfg = await load_cfg(session)
    visit = await _open_visit(session, actor.id, lock=True)
    if visit is None:
        raise AppError("NO_OPEN_VISIT", "You have no open on-duty visit")
    if body.visit_id is not None and body.visit_id != visit.id:
        raise AppError("NOT_FOUND", "Visit not found")
    loc = await session.get(DutyLocation, visit.location_id)  # works even if the location is inactive or unassigned now
    mock = bool(body.is_mock_location)
    dist: float | None = None
    within: bool | None = None
    try:
        punch.require_fix(cfg, body, now)
        punch.enforce_mock(cfg, body)
        punch.enforce_accuracy(cfg, body)
        selfie_id = await punch.validate_selfie(session, actor, body.selfie_file_id, False, cfg, now)
        if loc is not None:  # measured against the location's CURRENT lat/lng/radius
            dist = haversine_m(body.lat, body.lng, loc.lat, loc.lng)
            within = dist <= loc.radius_m
            if not within and cfg["on_duty.checkout_geofence"]:
                raise _outside()
    except AppError as exc:
        await punch.audit_reject(session, actor, "on_duty_check_out", exc.code)
        raise
    punch.log_diagnostics(actor, body, now)
    if now <= as_utc(visit.check_in_at):  # type: ignore[operator]
        raise AppError("BAD_REQUEST", "Check-out must be after check-in")

    visit.check_out_at = now
    visit.check_out_loc = {"lat": body.lat, "lng": body.lng, "accuracy_m": body.accuracy_m}
    visit.check_out_distance_m = None if dist is None else int(round(dist))
    visit.check_out_within_fence = within
    visit.check_out_accuracy_m = body.accuracy_m
    visit.check_out_mock_location = mock
    visit.check_out_selfie_file_id = selfie_id
    if body.note is not None:
        visit.note = body.note
    if mock:
        visit.suspicious = True
        audit(session, action="on_duty.suspicious_punch", actor=actor, entity_type="on_duty_visit", entity_id=visit.id,
              after={"kind": "check_out", "reason": "mock_flag"})  # fmt: skip

    rec = (
        await session.execute(
            select(Attendance)
            .where(Attendance.id == visit.attendance_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    existing = as_utc(rec.check_out_at)
    if existing is None or existing <= now:
        rec.check_out_at, rec.check_out_source = now, "on_duty"  # a later OFFICE check-out is never overwritten
    att.apply_close(rec, cfg, rec.check_out_at)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("SELFIE_REUSED", "This selfie was already used. Take a new one.") from None
    return {"visit": visit_out(visit), "attendance": await _attendance_out(session, rec)}


# ---------------------------------------------------------------- admin: visits
async def admin_visits(
    session: AsyncSession,
    actor: Principal,
    params: PageParams,
    *,
    on_date: dt.date | None,
    d_from: dt.date | None,
    d_to: dt.date | None,
    location_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
    employee_id: str | None,
    open_only: bool,
) -> dict[str, Any]:
    cfg = await att.load_settings(session)
    d_from, d_to = att.cap_range(d_from, d_to, _today(cfg), on_date)
    cond = att.scope_conditions(actor, "attendance.view_all", "attendance.view_team", department_id, employee_id)
    if on_date:
        cond.append(OnDutyVisit.work_date == on_date)
    else:
        if d_from:
            cond.append(OnDutyVisit.work_date >= d_from)
        if d_to:
            cond.append(OnDutyVisit.work_date <= d_to)
    if location_id:
        cond.append(OnDutyVisit.location_id == location_id)
    if open_only:
        cond += [OnDutyVisit.check_out_at.is_(None), OnDutyVisit.auto_closed.is_(False)]
    base = select(OnDutyVisit, User).join(User, User.id == OnDutyVisit.user_id).where(*cond)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(OnDutyVisit.check_in_at.desc(), OnDutyVisit.id).limit(params.page_size).offset(params.offset)
            )
        )
        .unique()
        .all()
    )
    photos = await files_svc.photo_file_ids(session, [u.id for _, u in rows])
    full = actor.has("attendance.view_all")
    items = []
    for v, u in rows:
        item = visit_out(v)
        item["user"] = att.user_summary(u, photos.get(u.id))
        item["check_in_distance_m"] = v.check_in_distance_m if full else None
        item["check_out_distance_m"] = v.check_out_distance_m if full else None
        item["check_out_within_fence"] = v.check_out_within_fence if full else None
        item["check_in_accuracy_m"] = v.check_in_accuracy_m if full else None
        item["check_out_accuracy_m"] = v.check_out_accuracy_m if full else None
        item["suspicious"] = v.suspicious if full else None
        items.append(item)
    return make_page(items, params, total)


# ---------------------------------------------------------------- requests
def _days(r: OnDutyRequest) -> int:
    return (r.to_date - r.from_date).days + 1


async def _render(session: AsyncSession, rows: list[OnDutyRequest], *, admin: bool) -> list[dict[str, Any]]:
    uids = {r.approver_id for r in rows if r.approver_id}
    if admin:
        uids |= {r.user_id for r in rows}
    users = await leave_svc._users(session, uids)
    photos = await files_svc.photo_file_ids(session, list(users))
    loc_ids = {r.location_id for r in rows if r.location_id}
    names: dict[uuid.UUID, str] = {}
    if loc_ids:
        names = dict(
            (await session.execute(select(DutyLocation.id, DutyLocation.name).where(DutyLocation.id.in_(loc_ids)))).all()
        )
    out = []
    for r in rows:
        item = {
            "id": str(r.id),
            "from_date": r.from_date.isoformat(),
            "to_date": r.to_date.isoformat(),
            "days": _days(r),
            "location": {"id": str(r.location_id), "name": names.get(r.location_id, "")} if r.location_id else None,
            "purpose": r.purpose or "",  # "" once the retention job purged it
            "status": r.status,
            "approver": att.user_summary(users[r.approver_id], photos.get(r.approver_id)) if r.approver_id else None,
            "decision_comment": r.decision_comment,
            "decided_at": iso_z(r.decided_at) if r.decided_at else None,
            "created_at": iso_z(r.created_at),
        }
        if admin:
            u = users[r.user_id]
            item["type"] = "on_duty"
            item["user"] = att.user_summary(u, photos.get(u.id))
        out.append(item)
    return out


async def _one(session: AsyncSession, row: OnDutyRequest, *, admin: bool = False) -> dict[str, Any]:
    return (await _render(session, [row], admin=admin))[0]


def _first_clash(ranges: list[tuple[dt.date, dt.date]], lo: dt.date, hi: dt.date) -> dt.date | None:
    best: dt.date | None = None
    for f, t in ranges:
        if f <= hi and t >= lo:
            d = max(f, lo)
            best = d if best is None or d < best else best
    return best


async def _leave_clash(
    session: AsyncSession, user_id: uuid.UUID, lo: dt.date, hi: dt.date, statuses: tuple[str, ...]
) -> dt.date | None:
    """First date in lo..hi covered by a FULL-day leave in one of `statuses`."""
    rows = (
        await session.execute(
            select(LeaveRequest.from_date, LeaveRequest.to_date).where(
                LeaveRequest.user_id == user_id,
                LeaveRequest.status.in_(statuses),
                LeaveRequest.half_day.is_(False),
                LeaveRequest.from_date <= hi,
                LeaveRequest.to_date >= lo,
            )
        )
    ).all()
    return _first_clash([(f, t) for f, t in rows], lo, hi)


def _overlap_error(day: dt.date) -> AppError:
    return AppError(
        "OVERLAPPING_REQUEST",
        "You already have a pending or approved request on these dates",
        details=[{"field": "date", "message": day.isoformat()}],
    )


async def apply(session: AsyncSession, actor: Principal, body: Any, redis: Redis | None = None) -> dict[str, Any]:
    user = actor.user
    cfg = await load_cfg(session)
    if not cfg["on_duty.enabled"]:
        raise AppError("ON_DUTY_DISABLED", "On Duty is not enabled")
    today = _today(cfg)
    if body.to_date < body.from_date:
        raise _err("INVALID_DATE_RANGE", "to_date must be on or after from_date", "to_date")
    if (body.to_date - body.from_date).days + 1 > MAX_REQUEST_DAYS:
        raise _err("INVALID_DATE_RANGE", f"A request can cover at most {MAX_REQUEST_DAYS} days", "to_date")
    if body.from_date < today - dt.timedelta(days=int(cfg["on_duty.request_backdate_days"])):
        raise _err("INVALID_DATE_RANGE", "The start date is too far in the past", "from_date")
    if body.to_date > today + dt.timedelta(days=int(cfg["on_duty.request_max_advance_days"])):
        raise _err("INVALID_DATE_RANGE", "The end date is too far ahead", "to_date")
    if body.location_id is not None and await loc_svc.assigned_active_location(session, user.id, body.location_id) is None:
        raise _unavailable()

    await leave_svc._lock_user(session, user.id)  # serialises concurrent submits of one user
    rows = (
        await session.execute(
            select(OnDutyRequest.from_date, OnDutyRequest.to_date).where(
                OnDutyRequest.user_id == user.id,
                OnDutyRequest.status.in_(("pending", "approved")),
                OnDutyRequest.from_date <= body.to_date,
                OnDutyRequest.to_date >= body.from_date,
            )
        )
    ).all()
    clashes = [
        d
        for d in (
            _first_clash([(f, t) for f, t in rows], body.from_date, body.to_date),
            await _leave_clash(session, user.id, body.from_date, body.to_date, ("pending", "approved")),
        )
        if d
    ]
    if clashes:
        raise _overlap_error(min(clashes))
    req = OnDutyRequest(
        user_id=user.id,
        from_date=body.from_date,
        to_date=body.to_date,
        location_id=body.location_id,
        purpose=body.purpose,
        status="pending",
    )
    session.add(req)
    await session.flush()
    audit(
        session,
        action="on_duty.apply",
        actor=actor,
        entity_type="on_duty_request",
        entity_id=req.id,
        after={
            "from": body.from_date.isoformat(),
            "to": body.to_date.isoformat(),
            "days": _days(req),
            "any_location": body.location_id is None,
        },
    )
    approvers, fanout = await leave_svc._approvers(session, user)
    await session.commit()
    ids = await leave_svc._coalesce(redis, user.id, approvers) if fanout else approvers
    await notify(
        session,
        ids,
        type="new_request",
        entity_id=req.id,
        title="New on duty request",
        body=f"{user.full_name} ({user.employee_id})",
        extra={"request_type": "on_duty"},
    )
    return await _one(session, req)


async def list_mine(
    session: AsyncSession, actor: Principal, params: PageParams, status: str | None, d_from: dt.date | None, d_to: dt.date | None
) -> dict[str, Any]:
    cond = [OnDutyRequest.user_id == actor.id, *leave_svc._status_cond(OnDutyRequest, status), *_overlap_conditions(d_from, d_to)]
    total = (await session.execute(select(func.count()).select_from(OnDutyRequest).where(*cond))).scalar_one()
    rows = list(
        (
            await session.execute(
                select(OnDutyRequest)
                .where(*cond)
                .order_by(OnDutyRequest.created_at.desc(), OnDutyRequest.id)
                .limit(params.page_size)
                .offset(params.offset)
            )
        ).scalars()
    )
    return make_page(await _render(session, rows, admin=False), params, total)


def _overlap_conditions(d_from: dt.date | None, d_to: dt.date | None) -> list:
    if d_from and d_to and d_to < d_from:
        raise _err("VALIDATION_ERROR", "to must be >= from", "to")
    cond: list = []
    if d_from:
        cond.append(OnDutyRequest.to_date >= d_from)
    if d_to:
        cond.append(OnDutyRequest.from_date <= d_to)
    return cond


async def get_mine(session: AsyncSession, actor: Principal, req_id: uuid.UUID) -> dict[str, Any]:
    r = await session.get(OnDutyRequest, req_id)
    if r is None or r.user_id != actor.id:
        raise AppError("NOT_FOUND", "Request not found")
    return await _one(session, r)


async def _load_locked(session: AsyncSession, req_id: uuid.UUID) -> OnDutyRequest | None:
    q = select(OnDutyRequest).where(OnDutyRequest.id == req_id).with_for_update().execution_options(populate_existing=True)
    return (await session.execute(q)).scalar_one_or_none()


async def cancel(session: AsyncSession, actor: Principal, req_id: uuid.UUID) -> dict[str, Any]:
    probe = await session.get(OnDutyRequest, req_id)  # authorise BEFORE taking the row lock
    if probe is None or probe.user_id != actor.id:
        raise AppError("NOT_FOUND", "Request not found")
    r = await _load_locked(session, req_id)
    if r is None:
        raise AppError("NOT_FOUND", "Request not found")
    cfg = await leave_svc._cfg(session)
    today = att.utcnow().astimezone(att.office_tz(cfg)).date()
    before = r.status
    if r.status == "pending":
        pass
    elif r.status == "approved" and cfg["leave.allow_cancel_approved"] and r.from_date > today:
        pass
    else:
        raise AppError("NOT_PENDING", "Only pending requests can be cancelled")
    r.status = "cancelled"
    audit(session, action="on_duty.cancel", actor=actor, entity_type="on_duty_request", entity_id=r.id,
          before={"status": before}, after={"status": "cancelled"})  # fmt: skip
    await session.commit()
    return await _one(session, r)


async def cancel_pending_for_user(session: AsyncSession, user: User, actor: Principal, *, why: str) -> int:
    """Deactivate / delete: cancel the user's PENDING on-duty requests. Caller commits. Open visits are left to the 00:30 job."""
    rows = (
        (
            await session.execute(
                select(OnDutyRequest)
                .where(OnDutyRequest.user_id == user.id, OnDutyRequest.status == "pending")
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        r.status = "cancelled"
        audit(
            session,
            action="on_duty.auto_cancel",
            actor=actor,
            entity_type="on_duty_request",
            entity_id=r.id,
            before={"status": "pending"},
            after={"status": "cancelled", "reason": why, "owner": user.employee_id},
        )
    await session.flush()
    return len(rows)


# ---------------------------------------------------------------- admin: requests
async def admin_list(
    session: AsyncSession,
    actor: Principal,
    params: PageParams,
    *,
    status: str | None,
    department_id: uuid.UUID | None,
    employee_id: str | None,
    d_from: dt.date | None,
    d_to: dt.date | None,
) -> dict[str, Any]:
    scope = att.scope_conditions(actor, "request.view_all", "request.view_team", department_id, employee_id)
    cond = [*scope, *leave_svc._status_cond(OnDutyRequest, status), *_overlap_conditions(d_from, d_to)]
    base = select(OnDutyRequest).join(User, User.id == OnDutyRequest.user_id).where(*cond)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = list(
        (
            await session.execute(
                base.order_by(OnDutyRequest.from_date.desc(), OnDutyRequest.created_at.desc(), OnDutyRequest.id)
                .limit(params.page_size)
                .offset(params.offset)
            )
        )
        .scalars()
        .unique()
    )
    return make_page(await _render(session, rows, admin=True), params, total)


async def decide(
    session: AsyncSession, actor: Principal, req_id: uuid.UUID, *, approve: bool, comment: str | None
) -> dict[str, Any]:
    kind = "on_duty"
    decision = "approve" if approve else "reject"
    probe = await session.get(OnDutyRequest, req_id)  # authorise on an UNLOCKED read: unknown == out of scope (404)
    if probe is None:
        raise AppError("NOT_FOUND", "Request not found")
    requester = (await leave_svc._users(session, {probe.user_id}))[probe.user_id]
    problem = leave_svc._scope_problem(actor, requester)
    if problem == "self":
        await leave_svc._audit_denied(session, actor, kind, req_id, problem, decision)
        raise AppError("FORBIDDEN", "You cannot approve or reject your own request")
    if problem:
        await leave_svc._audit_denied(session, actor, kind, req_id, problem, decision)
        raise AppError("NOT_FOUND", "Request not found")
    who = SimpleNamespace(id=actor.id, employee_id=actor.employee_id)  # a rollback expires the ORM objects below
    try:
        return await _decide_locked(session, actor, req_id, requester, approve=approve, comment=comment)
    except AppError as exc:
        await session.rollback()
        audit(
            session,
            action="request.decide_refused",
            actor=who,
            entity_type="on_duty_request",
            entity_id=req_id,
            after={"code": exc.code, "decision": decision},
            outcome="failure",
        )
        await session.commit()
        raise


def _range_label(r: OnDutyRequest) -> str:
    return f"{r.from_date:%d %b} - {r.to_date:%d %b %Y}"


async def _decide_locked(
    session: AsyncSession, actor: Principal, req_id: uuid.UUID, requester: User, *, approve: bool, comment: str | None
) -> dict[str, Any]:
    r = await _load_locked(session, req_id)
    if r is None:
        raise AppError("NOT_FOUND", "Request not found")
    if r.status != "pending":
        raise AppError("NOT_PENDING", "This request is no longer pending")
    if approve and (requester.status != "active" or requester.deleted_at is not None):
        raise AppError("REQUESTER_INACTIVE", "The employee is no longer active; this request cannot be approved")
    if approve:
        clash = await _leave_clash(session, r.user_id, r.from_date, r.to_date, ("pending", "approved"))
        if clash is not None:
            raise _overlap_error(clash)
    r.status = "approved" if approve else "rejected"
    r.approver_id = actor.id
    r.decision_comment = (comment or "").strip() or None
    r.decided_at = att.utcnow()
    audit(
        session,
        action="request.approve" if approve else "request.reject",
        actor=actor,
        entity_type="on_duty_request",
        entity_id=r.id,
        before={"status": "pending"},
        after={"status": r.status, "owner": requester.employee_id, "has_comment": bool(r.decision_comment)},
    )
    await session.commit()
    await notify(
        session,
        [r.user_id],
        type="on_duty_decision",
        entity_id=r.id,
        title=f"On duty request {r.status}",
        body=_range_label(r),
    )
    return await _one(session, r, admin=True)


# ---------------------------------------------------------------- leave admin items: on_duty_conflict
async def conflict_flags(session: AsyncSession, leaves: list[LeaveRequest]) -> dict[uuid.UUID, bool]:
    """leave request id -> an APPROVED on-duty request of the requester overlaps it (approving is still allowed)."""
    if not leaves:
        return {}
    uids = {lv.user_id for lv in leaves}
    lo, hi = min(lv.from_date for lv in leaves), max(lv.to_date for lv in leaves)
    rows = (
        await session.execute(
            select(OnDutyRequest.user_id, OnDutyRequest.from_date, OnDutyRequest.to_date).where(
                OnDutyRequest.user_id.in_(uids),
                OnDutyRequest.status == "approved",
                OnDutyRequest.from_date <= hi,
                OnDutyRequest.to_date >= lo,
            )
        )
    ).all()
    return {lv.id: any(u == lv.user_id and f <= lv.to_date and t >= lv.from_date for u, f, t in rows) for lv in leaves}


# ---------------------------------------------------------------- home
async def home_open_visit(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any] | None:
    v = await _open_visit(session, user_id)
    if v is None:
        return None
    return {"id": str(v.id), "location_name": v.location_name, "check_in_at": iso_z(v.check_in_at)}


async def pending_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(OnDutyRequest)
                .where(OnDutyRequest.user_id == user_id, OnDutyRequest.status == "pending")
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------- retention
async def purge_old_text(session: AsyncSession, batch: int = 500) -> int:
    """`leave.reason_retention_days` > 0: purpose -> "" and decision comment -> NULL for decided requests whose last day is
    older than that. The purpose column is NOT NULL and encrypted, so already-purged rows are recognised by decrypting
    (one keyset pass per run, only rows that still hold text are written)."""
    cfg = await leave_svc._cfg(session)
    days = int(cfg["leave.reason_retention_days"])
    if days <= 0:
        return 0
    cutoff = att.utcnow().astimezone(att.office_tz(cfg)).date() - dt.timedelta(days=days)
    purged = 0
    last: uuid.UUID | None = None
    while True:
        q = (
            select(OnDutyRequest)
            .where(OnDutyRequest.status.in_(("approved", "rejected", "cancelled")), OnDutyRequest.to_date < cutoff)
            .order_by(OnDutyRequest.id)
            .limit(batch)
        )
        if last is not None:
            q = q.where(OnDutyRequest.id > last)
        rows = list((await session.execute(q)).scalars())
        if not rows:
            break
        last = rows[-1].id
        for r in rows:
            if r.purpose or r.decision_comment is not None:
                r.purpose, r.decision_comment = "", None
                purged += 1
        await session.commit()
    if purged:
        audit(
            session,
            action="on_duty.retention_purge",
            entity_type="on_duty_request",
            after={"requests": purged, "retention_days": days},
        )
        await session.commit()
    return purged
