"""Login sessions / devices (plan 11 s.3.1, contract s.12.4). A session = one refresh-token family; the live one is the
family's head token (not revoked, not replaced, not expired). IP addresses are personal data: only `session.manage`
holders reach this module, every list read and every revoke is audited, and the employee privilege rules apply."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import iso_z, utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import RefreshToken, Role, User
from . import employees as emp
from .auth import bump_token_epoch, revoke_all_for_user, revoke_family


def _live(now) -> list:
    return [RefreshToken.revoked_at.is_(None), RefreshToken.replaced_by.is_(None), RefreshToken.expires_at > now]


async def _visible_role_ids(session: AsyncSession, actor: Principal) -> list[uuid.UUID]:
    """Roles whose accounts the actor may manage (containment + elevated rule), the same rules as deactivate/unlock."""
    ids = []
    for role in (await session.execute(select(Role))).unique().scalars():
        perms = frozenset(p.code for p in role.permissions) - emp.SELF_SERVICE_PERMISSIONS
        if perms <= actor.permissions and (not perms or actor.has("role.assign")):
            ids.append(role.id)
    return ids


def _item(t: RefreshToken, u: User) -> dict[str, Any]:
    from .attendance import user_summary  # lazy: keeps this module light for the CLI

    return {
        "id": str(t.family_id),
        "user": user_summary(u),
        "client_type": t.client_type,
        "platform": t.platform,
        "device_name": t.device_name,
        "device_id": t.device_id,
        "app_version": t.app_version,
        "ip": str(t.ip) if t.ip else None,
        "started_at": iso_z(t.family_started_at),
        "last_seen_at": iso_z(t.issued_at),
        "expires_at": iso_z(t.expires_at),
    }


async def list_sessions(
    session: AsyncSession,
    actor: Principal,
    params: PageParams,
    *,
    user_id: uuid.UUID | None = None,
    employee_id: str | None = None,
    client_type: str | None = None,
    audit_read: bool = True,
) -> dict[str, Any]:
    now = utcnow()
    cond = _live(now)
    scope = [User.id == actor.id, User.role_id.in_(await _visible_role_ids(session, actor))]
    cond.append(or_(*scope))
    if not emp._sees_all(actor):  # team-scoped custom role: direct reports (and yourself) only
        cond.append(or_(User.manager_id == actor.id, User.id == actor.id))
    if user_id:
        cond.append(RefreshToken.user_id == user_id)
    if employee_id:
        cond.append(User.employee_id == employee_id.strip().upper())
    if client_type:
        cond.append(RefreshToken.client_type == client_type)
    base = select(RefreshToken, User).join(User, User.id == RefreshToken.user_id).where(*cond)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (await session.execute(base.order_by(RefreshToken.issued_at.desc()).limit(params.page_size).offset(params.offset)))
        .unique()
        .all()
    )
    if audit_read:  # filters + paging only, never the returned content (IPs)
        audit(
            session,
            action="session.view",
            actor=actor,
            entity_type="session",
            after={
                "user_id": str(user_id) if user_id else None,
                "employee_id": employee_id,
                "client_type": client_type,
                "page": params.page,
            },
        )
        await session.commit()
    return make_page([_item(t, u) for t, u in rows], params, total)


async def _target(session: AsyncSession, actor: Principal, user_id: uuid.UUID) -> User:
    u = await emp._get(session, user_id, lock=True)
    emp.ensure_in_scope(actor, u)
    if u.id != actor.id:
        emp.guard_target(actor, u, elevated_needs_assign=True)
    return u


async def list_for_employee(session: AsyncSession, actor: Principal, user_id: uuid.UUID, params: PageParams) -> dict[str, Any]:
    u = await emp._get(session, user_id)
    emp.ensure_in_scope(actor, u)
    if u.id != actor.id:
        emp.guard_target(actor, u, elevated_needs_assign=True)
    return await list_sessions(session, actor, params, user_id=u.id)


async def revoke_one(session: AsyncSession, actor: Principal, family_id: uuid.UUID) -> None:
    now = utcnow()
    head = (
        await session.execute(select(RefreshToken).where(RefreshToken.family_id == family_id, *_live(now)).limit(1))
    ).scalar_one_or_none()
    if head is None:
        raise AppError("NOT_FOUND", "Session not found")
    u = await _target(session, actor, head.user_id)
    await revoke_family(session, family_id)
    bump_token_epoch(u)  # the device's access token dies now (other devices refresh and carry on)
    audit(
        session,
        action="session.revoke",
        actor=actor,
        entity_type="user",
        entity_id=u.id,
        after={"family_id": str(family_id), "client_type": head.client_type},
    )
    await session.commit()


async def revoke_all(session: AsyncSession, actor: Principal, user_id: uuid.UUID) -> dict[str, int]:
    u = await _target(session, actor, user_id)
    n = (
        await session.execute(
            select(func.count(func.distinct(RefreshToken.family_id))).where(RefreshToken.user_id == u.id, *_live(utcnow()))
        )
    ).scalar_one()
    await revoke_all_for_user(session, u.id)
    bump_token_epoch(u)
    audit(session, action="session.revoke_all", actor=actor, entity_type="user", entity_id=u.id, after={"revoked": n})
    await session.commit()
    return {"revoked": n}
