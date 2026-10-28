"""Tasks with calendar reminders (plan 14, contract section 15)."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.tasks import TaskDone, TaskIn
from ..services import tasks as svc

router = APIRouter(tags=["tasks"])
Self = Depends(require_permission())


async def _limit_write(redis: Redis, actor: Principal) -> None:
    await rate_limit.enforce(redis, [(f"rl:task:write:{actor.id}", 60, 60)], fail_closed=True)


@router.get("/tasks")
async def list_tasks(
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    assigned_by_me: bool = False,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Self,
):
    return await svc.list_tasks(session, actor, from_, to, assigned_by_me=assigned_by_me)


@router.post("/tasks", status_code=201)
async def create_task(
    body: TaskIn, session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Self
):
    await _limit_write(redis, actor)
    return await svc.create_task(session, actor, body)


@router.get("/tasks/{task_id}")
async def get_task(task_id: uuid.UUID, session: AsyncSession = Depends(get_session), actor: Principal = Self):
    return await svc.get_task(session, actor, task_id)


@router.put("/tasks/{task_id}")
async def update_task(
    task_id: uuid.UUID,
    body: TaskIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _limit_write(redis, actor)
    return await svc.update_task(session, actor, task_id, body)


@router.post("/tasks/{task_id}/done")
async def set_done(
    task_id: uuid.UUID,
    body: TaskDone,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _limit_write(redis, actor)
    return await svc.set_done(session, actor, task_id, body.done)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: uuid.UUID, session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Self
):
    await _limit_write(redis, actor)
    await svc.delete_task(session, actor, task_id)
    return Response(status_code=204)
