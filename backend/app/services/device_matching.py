"""Device users: auto-matching to employees, manual map / ignore / reset, listing (plan 18 section 4, contract 21.3).

Auto-match, in order: exact employee id (case-insensitive, trimmed); else numeric equality ignoring leading zeros after stripping
`device.match_prefix` from the employee id (prefix `EMP`: `0101` <-> `EMP101`). Only active employees. 2+ candidates = ambiguous =
stays unmatched. Manual mapping and "ignore" override and are never touched by auto-matching.

Security review M3 / M4: nobody maps a device user to themselves or changes a device user that is mapped to themselves; every
manual map / ignore needs a reason and is audited with the previous employee; changing a user that is already matched needs
`device.manage`; a device user first seen after the device's first sync is held (`auto_hold`) until HR confirms it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import iso_z, utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import Device, DevicePunch, DevicePunchOutcome, DeviceUser, User
from . import attendance as att
from . import devices as devices_svc

log = logging.getLogger("app.device_matching")
UNAPPLIED = ("unmatched", "ignored", "not_applied", "quarantined")
MAPPED = ("auto", "manual")


@dataclass
class MatchIndex:
    prefix: str = ""
    exact: dict[str, list[uuid.UUID]] = field(default_factory=dict)
    numeric: dict[str, list[uuid.UUID]] = field(default_factory=dict)

    def find(self, device_user_id: str) -> uuid.UUID | None:
        key = device_user_id.strip().upper()
        hits = self.exact.get(key, [])
        if not hits and key.isdigit():
            hits = self.numeric.get(key.lstrip("0") or "0", [])
        return hits[0] if len(hits) == 1 else None  # ambiguous or none -> unmatched


async def build_index(session: AsyncSession, prefix: str) -> MatchIndex:
    idx = MatchIndex(prefix=prefix.strip().upper())
    q = select(User.id, User.employee_id).where(User.status == "active", User.deleted_at.is_(None))
    for uid, eid in (await session.execute(q)).all():
        e = eid.strip().upper()
        idx.exact.setdefault(e, []).append(uid)
        rest = e[len(idx.prefix) :] if idx.prefix and e.startswith(idx.prefix) else e
        if rest.isdigit():
            idx.numeric.setdefault(rest.lstrip("0") or "0", []).append(uid)
    return idx


def mask_card(card: str | None) -> str | None:
    if not card:
        return None
    return "****" + card[-4:].rjust(4, "0")


# ---------------------------------------------------------------- output
async def device_users_out(session: AsyncSession, rows: list[DeviceUser]) -> list[dict[str, Any]]:
    if not rows:
        return []
    dev_ids = {r.device_id for r in rows}
    devices = {d.id: d for d in (await session.execute(select(Device).where(Device.id.in_(dev_ids)))).scalars()}
    uids = {r.user_id for r in rows if r.user_id}
    users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(uids)))).unique().scalars()} if uids else {}
    cond = or_(*[and_(DevicePunch.device_id == r.device_id, DevicePunch.device_user_id == r.device_user_id) for r in rows])
    last = {
        (d, u): t
        for d, u, t in (
            await session.execute(
                select(DevicePunch.device_id, DevicePunch.device_user_id, func.max(DevicePunch.punch_at))
                .where(cond)
                .group_by(DevicePunch.device_id, DevicePunch.device_user_id)
            )
        ).all()
    }
    unapplied = {
        (d, u): int(n)
        for d, u, n in (
            await session.execute(
                select(DevicePunch.device_id, DevicePunch.device_user_id, func.count())
                .join(DevicePunchOutcome, DevicePunchOutcome.punch_id == DevicePunch.id)
                .where(cond, DevicePunchOutcome.outcome.in_(UNAPPLIED))
                .group_by(DevicePunch.device_id, DevicePunch.device_user_id)
            )
        ).all()
    }
    out = []
    for r in rows:
        u = users.get(r.user_id) if r.user_id else None
        key = (r.device_id, r.device_user_id)
        out.append(
            {
                "id": str(r.id),
                "device": {"id": str(r.device_id), "name": devices[r.device_id].name},
                "device_user_id": r.device_user_id,
                "name": r.name,
                "card_masked": mask_card(r.card),
                "is_device_admin": r.is_device_admin,
                "pending_confirmation": bool(r.auto_hold and r.match_method == "unmatched"),
                "present_on_device": r.present_on_device,
                "status": {"auto": "matched", "manual": "matched"}.get(r.match_method, r.match_method),
                "match_method": r.match_method,
                "user": att.user_summary(u) if u else None,
                "last_punch_at": iso_z(last[key]) if key in last else None,
                "unapplied_punches": unapplied.get(key, 0),
                "last_seen_at": iso_z(r.last_seen_at),
            }
        )
    return out


async def list_device_users(
    session: AsyncSession, params: PageParams, *, device_id: uuid.UUID | None, status: str, q: str | None
) -> dict[str, Any]:
    cond = []
    if device_id:
        cond.append(DeviceUser.device_id == device_id)
    if status == "unmatched":
        cond.append(DeviceUser.match_method == "unmatched")
    elif status == "matched":
        cond.append(DeviceUser.match_method.in_(("auto", "manual")))
    elif status == "ignored":
        cond.append(DeviceUser.match_method == "ignored")
    order = (DeviceUser.is_device_admin.desc(), func.length(DeviceUser.device_user_id), DeviceUser.device_user_id, DeviceUser.device_id)
    term = (q or "").strip().lower()
    if term:  # names are encrypted at rest, so the text match runs here (the list is a few hundred rows at most)
        rows = [
            r
            for r in (await session.execute(select(DeviceUser).where(*cond).order_by(*order))).scalars()
            if term in r.device_user_id.lower() or term in (r.name or "").lower()
        ]
        total, page = len(rows), rows[params.offset : params.offset + params.page_size]
    else:
        total = (await session.execute(select(func.count()).select_from(DeviceUser).where(*cond))).scalar_one()
        page = list(
            (
                await session.execute(
                    select(DeviceUser).where(*cond).order_by(*order).limit(params.page_size).offset(params.offset)
                )
            ).scalars()
        )
    return make_page(await device_users_out(session, page), params, total)


# ---------------------------------------------------------------- auto matching of a device's rows
async def auto_match_rows(session: AsyncSession, rows: list[DeviceUser], idx: MatchIndex) -> list[DeviceUser]:
    """Match the `unmatched` rows; returns the rows that were matched now."""
    now = utcnow()
    hit = []
    for r in rows:
        if r.match_method != "unmatched" or r.auto_hold:
            continue
        uid = idx.find(r.device_user_id)
        if uid is not None:
            r.user_id, r.match_method, r.matched_at, r.matched_by = uid, "auto", now, None
            hit.append(r)
    return hit


# ---------------------------------------------------------------- manual map / ignore / auto (API)
async def _get(session: AsyncSession, du_id: uuid.UUID) -> DeviceUser:
    du = await session.get(DeviceUser, du_id)
    if du is None:
        raise AppError("NOT_FOUND", "Device user not found")
    return du


async def _queue_recompute(session: AsyncSession, redis: Redis | None, rows: list[DeviceUser]) -> bool:
    keys = sorted({(str(r.device_id), r.device_user_id) for r in rows})
    if redis is not None:
        try:
            await devices_svc.push_job(redis, {"kind": "recompute", "keys": [list(k) for k in keys]})
            return True
        except AppError:
            pass
    from . import device_attendance  # inline fallback when the queue is down

    await device_attendance.recompute_device_users(session, [(uuid.UUID(d), u) for d, u in keys])
    return True


async def second_slot_users(session: AsyncSession, user_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Employees (of `user_ids`) that own two or more mapped slots on ONE device: a sign of a planted duplicate id."""
    if not user_ids:
        return []
    q = (
        select(DeviceUser.user_id)
        .where(DeviceUser.user_id.in_(set(user_ids)), DeviceUser.match_method.in_(MAPPED))
        .group_by(DeviceUser.user_id, DeviceUser.device_id)
        .having(func.count() > 1)
    )
    return list((await session.execute(q)).scalars().unique())


def _forbidden(message: str, field: str = "user_id") -> AppError:
    return AppError("FORBIDDEN", message, details=[{"field": field, "message": message}])


async def _employee_ids(session: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return {i: e for i, e in (await session.execute(select(User.id, User.employee_id).where(User.id.in_(wanted)))).all()}


async def set_mapping(
    session: AsyncSession, redis: Redis | None, actor: Principal, du_id: uuid.UUID, user_id: uuid.UUID | None, apply_all: bool,
    reason: str,
) -> dict[str, Any]:
    du = await _get(session, du_id)
    target: User | None = None
    if user_id is not None:
        target = (await session.execute(select(User).where(User.id == user_id))).unique().scalar_one_or_none()
        if target is None or target.deleted_at is not None or target.status != "active":
            raise AppError(
                "VALIDATION_ERROR",
                "Unknown or inactive employee",
                details=[{"field": "user_id", "message": "unknown or inactive"}],
            )
        if target.id == actor.id:
            raise _forbidden("You cannot map a device user to yourself")
    rows = [du]
    if apply_all:
        rows += list(
            (
                await session.execute(
                    select(DeviceUser).where(DeviceUser.device_user_id == du.device_user_id, DeviceUser.id != du.id)
                )
            ).scalars()
        )
    if any(r.user_id == actor.id for r in rows):
        raise _forbidden("A device user mapped to you can only be changed by someone else")
    new_id = target.id if target else None
    if any(r.match_method in MAPPED and r.user_id != new_id for r in rows) and not actor.has("device.manage"):
        raise _forbidden("Changing a device user that is already matched needs the device.manage permission", field="id")
    previous = await _employee_ids(session, {r.user_id for r in rows})
    now = utcnow()
    for r in rows:
        before = {
            "device_user_id": r.device_user_id, "match_method": r.match_method,
            "employee_id": previous.get(r.user_id) if r.user_id else None,
        }  # fmt: skip
        r.user_id = new_id
        r.match_method = "manual" if target else "ignored"
        r.matched_by, r.matched_at, r.auto_hold = actor.id, now, False
        audit(
            session, action="device_user.map" if target else "device_user.ignore", actor=actor, entity_type="device_user", entity_id=r.id,
            before=before, after={"device_user_id": r.device_user_id, "employee_id": target.employee_id if target else None, "reason": reason},
        )  # fmt: skip
    await session.flush()
    if target is not None and await second_slot_users(session, [target.id]):
        audit(session, action="device_user.second_slot", actor=actor, entity_type="user", entity_id=target.id)
        await devices_svc.alert_holders(
            session, redis, key=f"second_slot:{target.id}", title="An employee has two users on one device", body="Device users",
            entity_id=du.device_id,
        )  # fmt: skip
    await session.commit()
    queued = await _queue_recompute(session, redis, rows)
    return {"items": await device_users_out(session, rows), "recompute_queued": queued}


async def reset_auto(session: AsyncSession, redis: Redis | None, actor: Principal, du_id: uuid.UUID) -> dict[str, Any]:
    """Back to automatic matching. This is also HR's confirmation of a held new user (`auto_hold`)."""
    from . import device_settings

    du = await _get(session, du_id)
    if du.user_id == actor.id:
        raise _forbidden("A device user mapped to you can only be changed by someone else", field="id")
    if du.match_method in MAPPED and not actor.has("device.manage"):
        raise _forbidden("Changing a device user that is already matched needs the device.manage permission", field="id")
    idx = await build_index(session, (await device_settings.load(session)).match_prefix)
    candidate = idx.find(du.device_user_id)
    if candidate == actor.id:
        raise _forbidden("You cannot map a device user to yourself")
    before = {
        "device_user_id": du.device_user_id, "match_method": du.match_method,
        "employee_id": (await _employee_ids(session, {du.user_id})).get(du.user_id) if du.user_id else None,
    }  # fmt: skip
    du.user_id, du.match_method, du.matched_by, du.matched_at, du.auto_hold = None, "unmatched", None, None, False
    await auto_match_rows(session, [du], idx)
    audit(
        session, action="device_user.auto", actor=actor, entity_type="device_user", entity_id=du.id,
        before=before, after={"device_user_id": du.device_user_id, "matched": du.match_method == "auto"},
    )  # fmt: skip
    await session.flush()
    if du.user_id is not None and await second_slot_users(session, [du.user_id]):
        audit(session, action="device_user.second_slot", actor=actor, entity_type="user", entity_id=du.user_id)
        await devices_svc.alert_holders(
            session, redis, key=f"second_slot:{du.user_id}", title="An employee has two users on one device", body="Device users",
            entity_id=du.device_id,
        )  # fmt: skip
    await session.commit()
    queued = await _queue_recompute(session, redis, [du])
    return {"items": await device_users_out(session, [du]), "recompute_queued": queued}
