from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.food import BulkIn, EnrollmentIn
from ..services import food as svc

router = APIRouter(tags=["food"])
Self = Depends(require_permission())  # self-service: the user id always comes from the token, never from the client


@router.get("/food/my-enrollments")
async def my_enrollments(
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await rate_limit.enforce(redis, [(f"rl:food:read:{actor.id}", 60, 60)])
    return await svc.my_enrollments(session, actor, from_, to)


@router.put("/food/enrollment")
async def put_enrollment(
    body: EnrollmentIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await rate_limit.enforce(redis, [(f"rl:food:write:{actor.id}", 30, 60)])
    return await svc.set_enrollment(session, actor, body.date, body.need_food)


@router.put("/food/enrollments")
async def put_enrollments(
    body: BulkIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await rate_limit.enforce(redis, [(f"rl:food:bulk:{actor.id}", 10, 60)])
    return await svc.set_bulk(session, actor, [(i.date, i.need_food) for i in body.items])


@router.get("/admin/food/daily")
async def daily(
    date: dt.date | None = None,
    department_id: uuid.UUID | None = None,
    include: Literal["yes", "all"] = "yes",
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(svc.DAILY_PAGE_SIZE, ge=1, le=svc.DAILY_PAGE_SIZE),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("food.view_all")),
):
    await rate_limit.enforce(redis, [(f"rl:food:admin:{actor.id}", 60, 60)])
    return await svc.daily(session, actor, date, department_id, include, page, page_size)
