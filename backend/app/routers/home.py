from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..services import celebrations, home

router = APIRouter(tags=["home"])
Self = Depends(require_permission())


@router.get("/home")
async def get_home(session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Self):
    await rate_limit.enforce(redis, [(f"rl:home:{actor.id}", 60, 60)])
    return await home.build(session, redis, actor)


@router.get("/home/celebrations")
async def get_celebrations(
    days: int = Query(7, ge=1, le=celebrations.HORIZON_DAYS),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await rate_limit.enforce(redis, [(f"rl:home:{actor.id}", 60, 60)])
    return {"items": await celebrations.upcoming(session, redis, days)}
