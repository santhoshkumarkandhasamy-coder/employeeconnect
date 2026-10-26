"""Duty locations admin (`duty_location.manage`; contract 14.5). Reads 60/min, writes 30/min per admin, assignments PUT 10/min."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.on_duty import AssignmentsIn, DutyLocationIn
from ..services import duty_locations as svc

router = APIRouter(prefix="/admin", tags=["duty-locations"])
Manage = Depends(require_permission("duty_location.manage"))


async def _read(redis: Redis, actor: Principal) -> None:
    await rate_limit.enforce(redis, [(f"rl:admin:dutyloc:read:{actor.id}", 60, 60)])


async def _write(redis: Redis, actor: Principal) -> None:
    await rate_limit.enforce(redis, [(f"rl:admin:dutyloc:write:{actor.id}", 30, 60)], fail_closed=True)


@router.get("/duty-locations")
async def list_locations(
    params: PageParams = Depends(),
    q: str | None = Query(None, max_length=100),
    is_active: bool | None = None,
    assign_mode: str | None = Query(None, pattern="^(all|selected)$"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await _read(redis, actor)
    return await svc.list_locations(session, params, q=q, is_active=is_active, assign_mode=assign_mode)


@router.post("/duty-locations", status_code=201)
async def create_location(
    body: DutyLocationIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await _write(redis, actor)
    return await svc.create_location(session, actor, body)


@router.get("/duty-locations/{location_id}")
async def get_location(
    location_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await _read(redis, actor)
    return await svc.get_location(session, location_id)


@router.put("/duty-locations/{location_id}")
async def update_location(
    location_id: uuid.UUID,
    body: DutyLocationIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await _write(redis, actor)
    return await svc.update_location(session, actor, location_id, body)


@router.delete("/duty-locations/{location_id}", status_code=204)
async def delete_location(
    location_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await _write(redis, actor)
    await svc.delete_location(session, actor, location_id)
    return Response(status_code=204)


@router.get("/duty-locations/{location_id}/assignments")
async def get_assignments(
    location_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await _read(redis, actor)
    return await svc.get_assignments(session, location_id)


@router.put("/duty-locations/{location_id}/assignments")
async def put_assignments(
    location_id: uuid.UUID,
    body: AssignmentsIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await rate_limit.enforce(redis, [(f"rl:admin:dutyloc:assign:{actor.id}", 10, 60)], fail_closed=True)
    return await svc.put_assignments(session, actor, location_id, body)


@router.get("/employees/{user_id}/duty-locations")
async def employee_locations(
    user_id: uuid.UUID, session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Manage
):
    await _read(redis, actor)
    return await svc.employee_locations(session, user_id)
