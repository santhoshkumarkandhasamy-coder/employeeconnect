"""On duty (plan 13): client locations set by the admin and on-duty requests.

An on-duty request covers whole working days at ONE client location and goes through the same approval flow as leave and
permission (services/leave.py KINDS["on_duty"] handles list / get / cancel / approve / reject). On an approved on-duty day
check-in (and check-out when the check-out geofence is on) is accepted only inside the client location's radius; the office
geofence does not apply that day (services/attendance.py). There is no balance: an on-duty day is a worked day.

Rules
  * dates: from_date >= today (the employee must punch at the client site, so a past day cannot be covered);
    at most MAX_SPAN_DAYS calendar days; not beyond `leave.max_advance_days`; must contain at least one working day.
  * overlaps: no other pending/approved on-duty request (DB EXCLUDE is the final guard) and no pending/approved leave
    (full or half day) on any of the days. Leave applied later is refused the same way (services/leave.py `on_duty_overlaps`).
  * the client location must be active at submit time. Deactivating it later does not cancel approved requests.
The location picker shows employees only a location's name and address. Coordinates and radius are listed to admins, and
to an employee only for the site of their own approved on-duty day (GET /attendance/today `on_duty`, like the office in
GET /config) so the app can show the live distance before punching.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import Attendance, ClientLocation, LeaveRequest, OnDutyRequest
from . import leave as lv
from .admin_masters import _commit, _count, _delete, _dup, _in_use, _like, _taken

MAX_SPAN_DAYS = 31


# ================================================================ client locations
def location_out(c: ClientLocation) -> dict[str, Any]:
    """Admin view (coordinates included)."""
    return {
        "id": str(c.id),
        "name": c.name,
        "address": c.address,
        "lat": c.lat,
        "lng": c.lng,
        "radius_m": c.radius_m,
        "is_active": c.is_active,
    }


def location_brief(c: ClientLocation) -> dict[str, Any]:
    """Employee view: never the coordinates or radius."""
    return {"id": str(c.id), "name": c.name, "address": c.address}


async def _location(session: AsyncSession, loc_id: uuid.UUID) -> ClientLocation:
    c = await session.get(ClientLocation, loc_id)
    if c is None:
        raise AppError("NOT_FOUND", "Client location not found")
    return c


async def list_locations(session: AsyncSession, params: PageParams, q: str | None, active: bool | None) -> dict[str, Any]:
    cond: list[Any] = []
    if q:
        cond.append(or_(ClientLocation.name.ilike(_like(q), escape="\\"), ClientLocation.address.ilike(_like(q), escape="\\")))
    if active is not None:
        cond.append(ClientLocation.is_active.is_(active))
    total = await _count(session, ClientLocation, *cond)
    rows = (
        await session.execute(
            select(ClientLocation).where(*cond).order_by(ClientLocation.name).limit(params.page_size).offset(params.offset)
        )
    ).scalars()
    return make_page([location_out(c) for c in rows], params, total)


async def list_active_brief(session: AsyncSession) -> dict[str, Any]:
    """Picker for the on-duty request form."""
    q = select(ClientLocation).where(ClientLocation.is_active.is_(True)).order_by(ClientLocation.name)
    return {"items": [location_brief(c) for c in (await session.execute(q)).scalars()]}


async def get_location(session: AsyncSession, loc_id: uuid.UUID) -> dict[str, Any]:
    return location_out(await _location(session, loc_id))


def _apply(c: ClientLocation, body: Any) -> None:
    c.name, c.address, c.lat, c.lng = body.name, body.address, body.lat, body.lng
    c.radius_m, c.is_active = body.radius_m, body.is_active


async def create_location(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    if await _taken(session, ClientLocation, ClientLocation.name, body.name, None):
        raise _dup("name")
    c = ClientLocation()
    _apply(c, body)
    session.add(c)
    await session.flush()
    audit(
        session, action="client_location.create", actor=actor, entity_type="client_location", entity_id=c.id,
        after=location_out(c),
    )  # fmt: skip
    await _commit(session)
    return location_out(c)


async def update_location(session: AsyncSession, actor: Principal, loc_id: uuid.UUID, body: Any) -> dict[str, Any]:
    c = await _location(session, loc_id)
    if await _taken(session, ClientLocation, ClientLocation.name, body.name, c.id):
        raise _dup("name")
    before = location_out(c)
    _apply(c, body)
    await session.flush()
    audit(
        session, action="client_location.update", actor=actor, entity_type="client_location", entity_id=c.id,
        before=before, after=location_out(c),
    )  # fmt: skip
    await _commit(session)
    return location_out(c)


async def delete_location(session: AsyncSession, actor: Principal, loc_id: uuid.UUID) -> None:
    c = await _location(session, loc_id)
    if await _count(session, OnDutyRequest, OnDutyRequest.client_location_id == c.id) or await _count(
        session, Attendance, Attendance.client_location_id == c.id
    ):
        raise _in_use("Client location")
    before = location_out(c)
    await _delete(session, c, "Client location")
    audit(
        session, action="client_location.delete", actor=actor, entity_type="client_location", entity_id=loc_id,
        before=before,
    )  # fmt: skip
    await _commit(session)


# ================================================================ apply
async def _overlaps_leave(session: AsyncSession, user_id: uuid.UUID, d_from: dt.date, d_to: dt.date, statuses) -> bool:
    q = select(LeaveRequest.id).where(
        LeaveRequest.user_id == user_id,
        LeaveRequest.status.in_(statuses),
        LeaveRequest.from_date <= d_to,
        LeaveRequest.to_date >= d_from,
    )
    return (await session.execute(q.limit(1))).first() is not None


async def apply_on_duty(session: AsyncSession, actor: Principal, body: Any, redis: Redis | None = None) -> dict[str, Any]:
    user = actor.user
    cfg = await lv._cfg(session)
    today = lv._today(cfg)
    shift = user.shift
    if shift is None or not shift.is_active:
        raise AppError("NO_SHIFT_ASSIGNED", "No shift is assigned to you. Contact HR.")
    if body.from_date < today:
        raise lv._err("INVALID_DATE_RANGE", "On duty cannot be requested for a past date", "from_date")
    if (body.to_date - body.from_date).days + 1 > MAX_SPAN_DAYS:
        raise lv._err("INVALID_DATE_RANGE", f"One on-duty request can cover at most {MAX_SPAN_DAYS} days", "to_date")
    horizon = int(cfg["leave.max_advance_days"])
    if horizon > 0 and body.to_date > today + dt.timedelta(days=horizon):
        raise lv._err("INVALID_DATE_RANGE", f"On duty can be requested at most {horizon} days ahead", "to_date")
    loc = await session.get(ClientLocation, body.client_location_id)
    if loc is None or not loc.is_active:
        raise lv._err("VALIDATION_ERROR", "Choose an active client location", "client_location_id")
    days = await lv.working_days(session, user, body.from_date, body.to_date)
    if days <= 0:
        raise lv._err("ZERO_WORKING_DAYS", "The selected dates have no working days", "from_date")

    await lv._lock_user(session, user.id)
    live = ("pending", "approved")
    if await lv.on_duty_overlaps(session, user.id, body.from_date, body.to_date, live):
        raise lv._err("OVERLAPPING_REQUEST", "You already have an on-duty request on these dates", "from_date")
    if await _overlaps_leave(session, user.id, body.from_date, body.to_date, live):
        raise lv._err("OVERLAPPING_REQUEST", "You have a pending or approved leave on these dates", "from_date")
    req = OnDutyRequest(
        user_id=user.id,
        client_location_id=loc.id,
        from_date=body.from_date,
        to_date=body.to_date,
        days=days,
        reason=body.reason,
        status="pending",
    )
    session.add(req)
    try:
        await session.flush()
    except IntegrityError as exc:  # EXCLUDE constraint: a concurrent submit won
        await session.rollback()
        raise lv._err("OVERLAPPING_REQUEST", "You already have an on-duty request on these dates", "from_date") from exc
    audit(
        session,
        action="on_duty.apply",
        actor=actor,
        entity_type="on_duty_request",
        entity_id=req.id,
        after={
            "from": body.from_date.isoformat(),
            "to": body.to_date.isoformat(),
            "days": days,
            "client_location_id": str(loc.id),
        },
    )
    approvers, fanout = await lv._approvers(session, user)
    await session.commit()
    await lv._push_new_request(session, redis, user, req.id, "on duty", approvers, fanout)
    return await lv._one(session, req)
