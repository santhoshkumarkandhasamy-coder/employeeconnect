from __future__ import annotations

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.food import SettingsIn
from ..services import settings as svc

router = APIRouter(prefix="/admin", tags=["admin-settings"])


@router.get("/settings")
async def get_settings_(
    session: AsyncSession = Depends(get_session), _: Principal = Depends(require_permission("settings.manage"))
):
    return await svc.read_all(session)


@router.put("/settings")
async def put_settings(
    body: SettingsIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("settings.manage")),
):
    await rate_limit.enforce(redis, [(f"rl:settings:{actor.id}", 20, 60)])
    return await svc.update(session, actor, body.settings)
