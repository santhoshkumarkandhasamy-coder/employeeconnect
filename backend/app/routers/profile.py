from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.config import get_settings
from ..core.db import get_session
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.profile import ProfileUpdate
from ..services import profile as svc

router = APIRouter(tags=["profile"])
Self = Depends(require_permission())  # self-service: the user always comes from the token


@router.get("/me/profile")
async def get_profile(session: AsyncSession = Depends(get_session), actor: Principal = Self):
    return await svc.get_profile(session, actor)


@router.put("/me/profile")
async def put_profile(
    body: ProfileUpdate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await rate_limit.enforce(redis, [(f"rl:profile:write:{actor.id}", 30, 60)])
    return await svc.update_profile(session, redis, actor, body)


@router.post("/me/profile/photo")
async def post_photo(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    # same shared per-user upload limiter as POST /files; fails closed
    await rate_limit.enforce(redis, [(f"rl:upload:{actor.id}", get_settings().upload_rate_per_min, 60)], fail_closed=True)
    return await svc.set_photo(session, redis, actor, file)
