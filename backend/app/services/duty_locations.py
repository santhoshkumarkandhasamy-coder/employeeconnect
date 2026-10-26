"""Duty locations (client sites / branches) and their employee assignments (plan 13, `duty_location.manage`).

Coordinates are business data and live in clear. The employee view never carries `radius_m` (it would let a client bisect the
boundary); the admin view does. `assign_mode='all'` ignores the explicit rows (they are kept so switching back restores the list).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import iso_z
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    Department,
    DutyLocation,
    DutyLocationAssignment,
    OnDutyRequest,
    OnDutyVisit,
    Setting,
    User,
)
from . import admin_security
from . import attendance as att
from . import files as files_svc

MAX_ASSIGNED = 5000
MAX_BAD_IDS_LISTED = 20
# Review 11 M2. `role.assign` is held by the Admin role only (HR has `duty_location.manage` but not this), so it is the
# "admin-only" permission that lets someone assign themselves to a duty location.
SELF_ASSIGN_PERMISSION = "role.assign"
# A location open to ALL employees may not be larger than this many metres (setting `on_duty.all_mode_max_radius_m`,
# validated 10..2000 in services/settings.py; the DB check still caps every radius at 2000).
ALL_MODE_MAX_RADIUS_KEY = "on_duty.all_mode_max_radius_m"
ALL_MODE_MAX_RADIUS_DEFAULT = 300


async def all_mode_max_radius(session: AsyncSession) -> int:
    row = (await session.execute(select(Setting.value).where(Setting.key == ALL_MODE_MAX_RADIUS_KEY))).scalar_one_or_none()
    try:
        return int(row) if row is not None else ALL_MODE_MAX_RADIUS_DEFAULT
    except (TypeError, ValueError):
        return ALL_MODE_MAX_RADIUS_DEFAULT


async def _check_all_mode_radius(session: AsyncSession, assign_mode: str, radius_m: int) -> None:
    if assign_mode != "all":
        return
    cap = await all_mode_max_radius(session)
    if radius_m > cap:
        raise AppError(
            "VALIDATION_ERROR",
            f"A location open to all employees can have a radius of at most {cap} m",
            details=[{"field": "radius_m", "message": f"at most {cap} m while assign_mode is 'all'"}],
        )


async def _alert_admins(session: AsyncSession, actor: Principal, loc: DutyLocation, what: str) -> None:
    """Admin alert after commit (names only, never coordinates). Best effort."""
    await admin_security.notify_admins(
        session,
        entity_id=loc.id,
        title="Duty location changed",
        body=f"{actor.user.full_name} ({actor.employee_id}) {what}: '{loc.name}'.",
    )


def _active_user() -> list:
    return [User.status == "active", User.deleted_at.is_(None)]


def employee_out(loc: DutyLocation) -> dict[str, Any]:
    """Employee view: coordinates for navigation, NEVER the radius."""
    return {"id": str(loc.id), "name": loc.name, "address": loc.address, "lat": loc.lat, "lng": loc.lng}


def assigned_condition(user_id: uuid.UUID):
    """SQL condition: the location applies to `user_id` (all employees, or an explicit assignment row)."""
    return or_(
        DutyLocation.assign_mode == "all",
        DutyLocation.id.in_(select(DutyLocationAssignment.location_id).where(DutyLocationAssignment.user_id == user_id)),
    )


async def assigned_active_locations(session: AsyncSession, user_id: uuid.UUID) -> list[DutyLocation]:
    q = (
        select(DutyLocation)
        .where(DutyLocation.is_active.is_(True), assigned_condition(user_id))
        .order_by(func.lower(DutyLocation.name), DutyLocation.id)
    )
    return list((await session.execute(q)).scalars())


async def assigned_active_location(session: AsyncSession, user_id: uuid.UUID, location_id: uuid.UUID) -> DutyLocation | None:
    """Unknown, inactive and unassigned all look the same (None): callers answer with one DUTY_LOCATION_UNAVAILABLE."""
    q = select(DutyLocation).where(DutyLocation.id == location_id, DutyLocation.is_active.is_(True), assigned_condition(user_id))
    return (await session.execute(q)).scalar_one_or_none()


# ---------------------------------------------------------------- admin views
async def _users_by_id(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    if not ids:
        return {}
    rows = (await session.execute(select(User).where(User.id.in_(ids)))).unique().scalars()
    return {u.id: u for u in rows}


async def _admin_items(session: AsyncSession, rows: list[DutyLocation]) -> list[dict[str, Any]]:
    if not rows:
        return []
    ids = [r.id for r in rows]
    total_active = (await session.execute(select(func.count()).select_from(User).where(*_active_user()))).scalar_one()
    sel_counts = dict(
        (
            await session.execute(
                select(DutyLocationAssignment.location_id, func.count())
                .join(User, User.id == DutyLocationAssignment.user_id)
                .where(DutyLocationAssignment.location_id.in_(ids), *_active_user())
                .group_by(DutyLocationAssignment.location_id)
            )
        ).all()
    )
    used_visit = set(
        (await session.execute(select(OnDutyVisit.location_id).where(OnDutyVisit.location_id.in_(ids)).distinct())).scalars()
    )
    used_req = set(
        (await session.execute(select(OnDutyRequest.location_id).where(OnDutyRequest.location_id.in_(ids)).distinct())).scalars()
    )
    users = await _users_by_id(
        session, {r.created_by for r in rows if r.created_by} | {r.updated_by for r in rows if r.updated_by}
    )
    photos = await files_svc.photo_file_ids(session, list(users))

    def summ(uid: uuid.UUID | None):
        return att.user_summary(users[uid], photos.get(uid)) if uid and uid in users else None

    return [
        {
            "id": str(r.id),
            "name": r.name,
            "address": r.address,
            "lat": r.lat,
            "lng": r.lng,
            "radius_m": r.radius_m,
            "assign_mode": r.assign_mode,
            "is_active": r.is_active,
            "assigned_count": int(total_active) if r.assign_mode == "all" else int(sel_counts.get(r.id, 0)),
            "in_use": r.id in used_visit or r.id in used_req,
            "created_at": iso_z(r.created_at),
            "updated_at": iso_z(r.updated_at),
            "created_by": summ(r.created_by),
            "updated_by": summ(r.updated_by),
        }
        for r in rows
    ]


async def _get_or_404(session: AsyncSession, location_id: uuid.UUID, *, lock: bool = False) -> DutyLocation:
    q = select(DutyLocation).where(DutyLocation.id == location_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    loc = (await session.execute(q)).scalar_one_or_none()
    if loc is None:
        raise AppError("NOT_FOUND", "Duty location not found")
    return loc


async def list_locations(
    session: AsyncSession,
    params: PageParams,
    *,
    q: str | None,
    is_active: bool | None,
    assign_mode: str | None,
) -> dict[str, Any]:
    cond: list = []
    if q and q.strip():
        esc = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        cond.append(or_(DutyLocation.name.ilike(like, escape="\\"), DutyLocation.address.ilike(like, escape="\\")))
    if is_active is not None:
        cond.append(DutyLocation.is_active.is_(is_active))
    if assign_mode is not None:
        cond.append(DutyLocation.assign_mode == assign_mode)
    total = (await session.execute(select(func.count()).select_from(DutyLocation).where(*cond))).scalar_one()
    rows = list(
        (
            await session.execute(
                select(DutyLocation)
                .where(*cond)
                .order_by(func.lower(DutyLocation.name), DutyLocation.id)
                .limit(params.page_size)
                .offset(params.offset)
            )
        ).scalars()
    )
    return make_page(await _admin_items(session, rows), params, total)


async def get_location(session: AsyncSession, location_id: uuid.UUID) -> dict[str, Any]:
    loc = await _get_or_404(session, location_id)
    return (await _admin_items(session, [loc]))[0]


async def _name_taken(session: AsyncSession, name: str, except_id: uuid.UUID | None = None) -> bool:
    q = select(DutyLocation.id).where(func.lower(func.trim(DutyLocation.name)) == name.strip().lower())
    if except_id:
        q = q.where(DutyLocation.id != except_id)
    return (await session.execute(q.limit(1))).first() is not None


def _dup() -> AppError:
    return AppError(
        "CONFLICT", "A duty location with this name already exists", details=[{"field": "name", "message": "duplicate"}]
    )


def _audit_view(loc: DutyLocation) -> dict[str, Any]:
    # coordinates are business data and may appear in the audit log (visit coordinates never do)
    return {
        "name": loc.name,
        "address": loc.address,
        "lat": loc.lat,
        "lng": loc.lng,
        "radius_m": loc.radius_m,
        "assign_mode": loc.assign_mode,
        "is_active": loc.is_active,
    }


async def create_location(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    if await _name_taken(session, body.name):
        raise _dup()
    await _check_all_mode_radius(session, body.assign_mode, body.radius_m)
    loc = DutyLocation(
        name=body.name,
        address=body.address,
        lat=body.lat,
        lng=body.lng,
        radius_m=body.radius_m,
        assign_mode=body.assign_mode,
        is_active=body.is_active,
        created_by=actor.id,
        updated_by=actor.id,
    )
    session.add(loc)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise _dup() from None
    audit(
        session, action="duty_location.create", actor=actor, entity_type="duty_location", entity_id=loc.id, after=_audit_view(loc)
    )
    await session.commit()
    await _alert_admins(session, actor, loc, "created a duty location")
    await session.refresh(loc)
    return (await _admin_items(session, [loc]))[0]


async def update_location(session: AsyncSession, actor: Principal, location_id: uuid.UUID, body: Any) -> dict[str, Any]:
    loc = await _get_or_404(session, location_id, lock=True)
    if await _name_taken(session, body.name, loc.id):
        raise _dup()
    await _check_all_mode_radius(session, body.assign_mode, body.radius_m)
    before = _audit_view(loc)
    geo_changed = (loc.lat, loc.lng, loc.radius_m) != (body.lat, body.lng, body.radius_m)
    loc.name, loc.address, loc.lat, loc.lng = body.name, body.address, body.lat, body.lng
    loc.radius_m, loc.assign_mode, loc.is_active, loc.updated_by = body.radius_m, body.assign_mode, body.is_active, actor.id
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise _dup() from None
    audit(
        session, action="duty_location.update", actor=actor, entity_type="duty_location", entity_id=loc.id,
        before=before, after={**_audit_view(loc), "geo_changed": geo_changed},
    )  # fmt: skip
    await session.commit()
    if geo_changed:
        await _alert_admins(session, actor, loc, "changed the coordinates or radius of a duty location")
    await session.refresh(loc)  # updated_at is set by the database
    return (await _admin_items(session, [loc]))[0]


async def delete_location(session: AsyncSession, actor: Principal, location_id: uuid.UUID) -> None:
    loc = await _get_or_404(session, location_id, lock=True)
    in_use = (
        await session.execute(select(OnDutyVisit.id).where(OnDutyVisit.location_id == loc.id).limit(1))
    ).first() is not None or (
        await session.execute(select(OnDutyRequest.id).where(OnDutyRequest.location_id == loc.id).limit(1))
    ).first() is not None
    if in_use:
        raise AppError("IN_USE", "This location has visits or requests. Deactivate it instead.")
    before = _audit_view(loc)
    await session.execute(delete(DutyLocationAssignment).where(DutyLocationAssignment.location_id == loc.id))
    await session.delete(loc)
    audit(session, action="duty_location.delete", actor=actor, entity_type="duty_location", entity_id=location_id, before=before)
    await session.commit()


# ---------------------------------------------------------------- assignments
async def _assignment_payload(session: AsyncSession, loc: DutyLocation) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(DutyLocationAssignment.created_at, User)
                .join(User, User.id == DutyLocationAssignment.user_id)
                .where(DutyLocationAssignment.location_id == loc.id, *_active_user())
                .order_by(User.employee_id)
                .limit(MAX_ASSIGNED)
            )
        )
        .unique()
        .all()
    )
    photos = await files_svc.photo_file_ids(session, [u.id for _, u in rows])
    return {
        "location_id": str(loc.id),
        "assign_mode": loc.assign_mode,
        "items": [{"user": att.user_summary(u, photos.get(u.id)), "assigned_at": iso_z(at)} for at, u in rows],
    }


async def get_assignments(session: AsyncSession, location_id: uuid.UUID) -> dict[str, Any]:
    return await _assignment_payload(session, await _get_or_404(session, location_id))


def _bad_ids(field: str, ids: list[str]) -> AppError:
    return AppError(
        "VALIDATION_ERROR",
        "Some ids are not valid",
        details=[{"field": field, "message": i} for i in ids[:MAX_BAD_IDS_LISTED]],
    )


async def put_assignments(session: AsyncSession, actor: Principal, location_id: uuid.UUID, body: Any) -> dict[str, Any]:
    loc = await _get_or_404(session, location_id, lock=True)
    ids = list(body.user_ids)
    if len(set(ids)) != len(ids):
        dups = [str(i) for i in {i for i in ids if ids.count(i) > 1}]
        raise _bad_ids("user_ids", sorted(dups))
    wanted = set(ids)
    if wanted:
        ok: set[uuid.UUID] = set()
        id_list = list(wanted)
        for i in range(0, len(id_list), 500):
            ok |= set(
                (await session.execute(select(User.id).where(User.id.in_(id_list[i : i + 500]), *_active_user()))).scalars()
            )
        bad = sorted(str(i) for i in wanted - ok)
        if bad:
            raise _bad_ids("user_ids", bad)
    dept_ids = set(body.add_department_ids)
    if dept_ids:
        known = set((await session.execute(select(Department.id).where(Department.id.in_(dept_ids)))).scalars())
        bad_d = sorted(str(i) for i in dept_ids - known)
        if bad_d:
            raise _bad_ids("add_department_ids", bad_d)
        members = (await session.execute(select(User.id).where(User.department_id.in_(dept_ids), *_active_user()))).scalars()
        wanted |= set(members)
    if len(wanted) > MAX_ASSIGNED:
        raise AppError(
            "VALIDATION_ERROR",
            f"At most {MAX_ASSIGNED} employees can be assigned to one location",
            details=[{"field": "user_ids", "message": f"{len(wanted)} employees after merging departments"}],
        )
    current = set(
        (
            await session.execute(select(DutyLocationAssignment.user_id).where(DutyLocationAssignment.location_id == loc.id))
        ).scalars()
    )
    removed, added = current - wanted, wanted - current
    # Review 11 M2: HR may not give itself presence. Adding the actor (directly or through a department) needs the admin-only
    # permission; assign_mode 'all' includes the actor too, so it is flagged and limited by the radius cap instead.
    self_added = actor.id in added
    if self_added and not actor.has(SELF_ASSIGN_PERMISSION):
        raise AppError(
            "FORBIDDEN",
            "You cannot assign yourself to a duty location. Ask an administrator.",
            details=[{"field": "user_ids", "message": "self-assignment needs an administrator"}],
        )
    await _check_all_mode_radius(session, body.assign_mode, loc.radius_m)
    self_assigned = self_added or body.assign_mode == "all"
    if removed:
        rm = list(removed)
        for i in range(0, len(rm), 500):
            await session.execute(
                delete(DutyLocationAssignment).where(
                    DutyLocationAssignment.location_id == loc.id, DutyLocationAssignment.user_id.in_(rm[i : i + 500])
                )
            )
    for uid in sorted(added, key=str):
        session.add(DutyLocationAssignment(location_id=loc.id, user_id=uid, created_by=actor.id))
    mode_before = loc.assign_mode
    loc.assign_mode, loc.updated_by = body.assign_mode, actor.id
    await session.flush()
    audit(
        session,
        action="duty_location.assignments",
        actor=actor,
        entity_type="duty_location",
        entity_id=loc.id,
        before={"assign_mode": mode_before},
        after={
            "assign_mode": loc.assign_mode,
            "added": sorted(str(i) for i in added),
            "removed": sorted(str(i) for i in removed),
            "self_assigned": self_assigned,
        },
    )
    await session.commit()
    if self_added or (body.assign_mode == "all" and mode_before != "all"):
        await _alert_admins(session, actor, loc, "assigned themselves to a duty location" if self_added else "opened a duty location to all employees")
    return await _assignment_payload(session, loc)


async def employee_locations(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    u = await session.get(User, user_id)
    if u is None or u.deleted_at is not None:
        raise AppError("NOT_FOUND", "Employee not found")
    q = select(DutyLocation).where(assigned_condition(user_id)).order_by(func.lower(DutyLocation.name), DutyLocation.id)
    rows = (await session.execute(q)).scalars()
    return {"items": [{"id": str(r.id), "name": r.name, "is_active": r.is_active, "assign_mode": r.assign_mode} for r in rows]}
