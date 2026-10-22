from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.admin import RoleCreateIn, RoleIn, RolePermissionsIn
from ..services import roles as svc

router = APIRouter(prefix="/admin", tags=["admin-roles"])


async def _write_limit(redis: Redis, actor: Principal) -> None:
    await rate_limit.enforce(redis, [(f"rl:admin:rolewrite:{actor.id}", 10, 60)])


@router.get("/roles")
async def list_roles(session: AsyncSession = Depends(get_session), _: Principal = Depends(require_permission("role.view"))):
    return await svc.list_roles(session)


@router.post("/roles", status_code=201)
async def create_role(
    body: RoleCreateIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("role.manage")),
):
    await _write_limit(redis, actor)
    return await svc.create_role(session, actor, body)


@router.get("/roles/{role_id}")
async def get_role(
    role_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission("role.view")),
):
    return await svc.get_role(session, role_id)


@router.put("/roles/{role_id}")
async def update_role(
    role_id: uuid.UUID,
    body: RoleIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("role.manage")),
):
    await _write_limit(redis, actor)
    return await svc.update_role(session, actor, role_id, body)


@router.put("/roles/{role_id}/permissions")
async def set_permissions(
    role_id: uuid.UUID,
    body: RolePermissionsIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("role.manage")),
):
    await _write_limit(redis, actor)
    return await svc.set_permissions(session, redis, actor, role_id, body.permission_codes, body.current_password)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(
    role_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("role.manage")),
):
    await _write_limit(redis, actor)
    await svc.delete_role(session, actor, role_id)
    return Response(status_code=204)


@router.get("/permissions")
async def permissions(session: AsyncSession = Depends(get_session), _: Principal = Depends(require_permission("role.view"))):
    return await svc.permission_catalogue(session)
