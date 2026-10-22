"""Roles and permissions admin (plan 09). Permissions are read from the database on every request, so a change to a role
takes effect immediately for every holder (nothing is cached). System roles (`admin,hr,manager,employee`) cannot be
deleted or renamed; `admin` is immutable (neither its description nor its permissions can change)."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import Permission, Role, User
from . import admin_security

SYSTEM_ROLES = ("admin", "hr", "manager", "employee")


def _codes(role: Role) -> list[str]:
    return sorted(p.code for p in role.permissions)


async def _role(session: AsyncSession, role_id: uuid.UUID, *, lock: bool = False) -> Role:
    """`lock=True` takes a row lock (FOR UPDATE) and re-reads the role with its permissions, so two concurrent writers to
    the same role serialise instead of losing an update (review L2)."""
    r = await session.get(Role, role_id, populate_existing=lock, with_for_update=lock)
    if r is None:
        raise AppError("NOT_FOUND", "Role not found")
    return r


async def _user_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    rows = (
        await session.execute(select(User.role_id, func.count()).where(User.deleted_at.is_(None)).group_by(User.role_id))
    ).all()
    return {rid: n for rid, n in rows}


def role_out(r: Role, user_count: int) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "name": r.name,
        "description": r.description,
        "is_system": r.is_system,
        "user_count": user_count,
        "permission_codes": _codes(r),
    }


async def _out(session: AsyncSession, r: Role) -> dict[str, Any]:
    n = (
        await session.execute(select(func.count()).select_from(User).where(User.role_id == r.id, User.deleted_at.is_(None)))
    ).scalar_one()
    return role_out(r, n)


async def list_roles(session: AsyncSession) -> dict[str, Any]:
    counts = await _user_counts(session)
    rows = (await session.execute(select(Role).order_by(Role.name))).scalars().all()
    return {"items": [role_out(r, counts.get(r.id, 0)) for r in rows]}


async def get_role(session: AsyncSession, role_id: uuid.UUID) -> dict[str, Any]:
    return await _out(session, await _role(session, role_id))


async def permission_catalogue(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Permission).order_by(Permission.module, Permission.code))).scalars()
    return {"items": [{"code": p.code, "module": p.module, "description": p.description} for p in rows]}


async def _resolve(session: AsyncSession, codes: list[str]) -> list[Permission]:
    wanted = set(codes)
    found = (
        {p.code: p for p in (await session.execute(select(Permission).where(Permission.code.in_(wanted)))).scalars()}
        if wanted
        else {}
    )
    unknown = sorted(wanted - set(found))
    if unknown:
        raise AppError(
            "VALIDATION_ERROR",
            "Unknown permission code",
            details=[{"field": "permission_codes", "message": f"unknown code {c[:60]}"} for c in unknown[:20]],
        )
    return [found[c] for c in sorted(wanted)]


def _may_grant(actor: Principal, codes: set[str]) -> None:
    """Nobody can hand out a permission they do not hold themselves (no privilege escalation through a custom role)."""
    if not codes <= actor.permissions:
        raise AppError("FORBIDDEN", "You cannot grant permissions you do not hold yourself")


async def _name_taken(session: AsyncSession, name: str, except_id: uuid.UUID | None) -> bool:
    q = select(Role.id).where(func.lower(Role.name) == name.lower())
    if except_id:
        q = q.where(Role.id != except_id)
    return (await session.execute(q)).first() is not None


def _dup() -> AppError:
    return AppError(
        "CONFLICT", "A role with this name already exists", status=409, details=[{"field": "name", "message": "already exists"}]
    )


async def create_role(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    if await _name_taken(session, body.name, None):
        raise _dup()
    perms = await _resolve(session, body.permission_codes)
    _may_grant(actor, {p.code for p in perms})
    role = Role(name=body.name, description=body.description, is_system=False)
    role.permissions = perms
    session.add(role)
    try:
        await session.flush()
        audit(
            session, action="role.create", actor=actor, entity_type="role", entity_id=role.id,
            after={"name": role.name, "description": role.description, "permission_codes": _codes(role)},
        )  # fmt: skip
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _dup() from exc
    return role_out(role, 0)


async def update_role(session: AsyncSession, actor: Principal, role_id: uuid.UUID, body: Any) -> dict[str, Any]:
    role = await _role(session, role_id)
    if role.name == "admin":
        raise AppError("FORBIDDEN", "The admin role cannot be modified")
    if role.is_system and body.name != role.name:
        raise AppError("FORBIDDEN", "System roles cannot be renamed", details=[{"field": "name", "message": "system role"}])
    if body.name != role.name and await _name_taken(session, body.name, role.id):
        raise _dup()
    before = {"name": role.name, "description": role.description}
    role.name, role.description = body.name, body.description
    audit(
        session, action="role.update", actor=actor, entity_type="role", entity_id=role.id,
        before=before, after={"name": role.name, "description": role.description},
    )  # fmt: skip
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _dup() from exc
    return await _out(session, role)


async def set_permissions(
    session: AsyncSession,
    redis: Redis,
    actor: Principal,
    role_id: uuid.UUID,
    codes: list[str],
    current_password: SecretStr | None,
) -> dict[str, Any]:
    role = await _role(session, role_id, lock=True)
    if role.name == "admin":
        raise AppError("FORBIDDEN", "The permissions of the admin role cannot be changed")
    # step-up (review M1): the acting admin confirms with their own password before any permission change
    await admin_security.require_reauth(session, redis, actor, current_password, action="role.permissions")
    perms = await _resolve(session, codes)
    new = {p.code for p in perms}
    old = set(_codes(role))
    added, removed = new - old, old - new
    _may_grant(actor, added)
    if removed and role.id == actor.user.role_id:
        raise AppError("FORBIDDEN", "You cannot remove permissions from your own role")
    if added or removed:
        role.permissions = perms
        audit(
            session, action="role.permissions", actor=actor, entity_type="role", entity_id=role.id,
            before={"permission_codes": sorted(old)}, after={"permission_codes": sorted(new), "added": sorted(added), "removed": sorted(removed)},
        )  # fmt: skip
    await session.commit()
    return await _out(session, role)


async def delete_role(session: AsyncSession, actor: Principal, role_id: uuid.UUID) -> None:
    role = await _role(session, role_id, lock=True)
    if role.is_system or role.name in SYSTEM_ROLES:
        raise AppError("FORBIDDEN", "System roles cannot be deleted")
    if (await session.execute(select(func.count()).select_from(User).where(User.role_id == role.id))).scalar_one():
        raise AppError("IN_USE", "Role is assigned to users and cannot be deleted; reassign them first", status=409)
    before = {"name": role.name, "description": role.description, "permission_codes": _codes(role)}
    try:
        async with session.begin_nested():
            await session.delete(role)  # the ORM also removes the role_permissions rows
    except IntegrityError as exc:  # a user was assigned to the role between the check and the delete
        raise AppError("IN_USE", "Role is assigned to users and cannot be deleted; reassign them first", status=409) from exc
    audit(session, action="role.delete", actor=actor, entity_type="role", entity_id=role_id, before=before)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise AppError("IN_USE", "Role is assigned to users and cannot be deleted; reassign them first", status=409) from exc
