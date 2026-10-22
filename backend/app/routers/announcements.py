from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core import rate_limit
from ..core.db import get_session
from ..core.errors import AppError
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.announcements import AnnouncementIn
from ..services import announcements as svc

router = APIRouter(tags=["announcements"])
Self = Depends(require_permission())
Manage = Depends(require_permission("announcement.manage"))


async def _read_limit(redis: Redis, actor: Principal) -> None:
    await rate_limit.enforce(redis, [(f"rl:ann:get:{actor.id}", 120, 60)])


async def _admin_read_limit(redis: Redis, actor: Principal) -> None:
    await rate_limit.enforce(redis, [(f"rl:ann:adminget:{actor.id}", 30, 60)])


def _parse_if_match(value: str | None) -> int | None:
    """`If-Match: "3"` (version from the admin view). Absent = no concurrency check; `*` = any."""
    if value is None or value.strip() == "*":
        return None
    raw = value.strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    try:
        return int(raw.strip('"'))
    except ValueError:
        raise AppError(
            "VALIDATION_ERROR", "If-Match must be the announcement version", details=[{"field": "If-Match", "message": "invalid"}]
        ) from None


@router.get("/announcements")
async def list_announcements(
    unread: bool | None = None,
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _read_limit(redis, actor)
    return await svc.list_mine(session, actor, params, unread)


@router.get("/announcements/unread-count")
async def unread_count(session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Self):
    await _read_limit(redis, actor)
    return {"unread_count": await svc.unread_count(session, actor.user)}


@router.get("/announcements/{ann_id}")
async def get_announcement(
    ann_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _read_limit(redis, actor)
    return await svc.get_mine(session, actor, ann_id)


@router.post("/announcements/{ann_id}/read", status_code=204)
async def mark_read(
    ann_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await rate_limit.enforce(redis, [(f"rl:ann:read:{actor.id}", 120, 60)])
    await svc.mark_read(session, actor, ann_id)
    return Response(status_code=204)


@router.get("/admin/announcements")
async def admin_list(
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await _admin_read_limit(redis, actor)
    return await svc.admin_list(session, params)


@router.post("/admin/announcements", status_code=201)
async def create(
    body: AnnouncementIn,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await rate_limit.enforce(redis, [(f"rl:ann:write:{actor.id}", 30, 60)])
    maker = async_sessionmaker(session.bind, expire_on_commit=False)  # type: ignore[arg-type]
    out, ann_id, high = await svc.create(session, redis, actor, body)
    background.add_task(svc.push, maker, ann_id, high)
    return out


@router.put("/admin/announcements/{ann_id}")
async def update(
    ann_id: uuid.UUID,
    body: AnnouncementIn,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
    if_match: str | None = Header(default=None),
):
    await rate_limit.enforce(redis, [(f"rl:ann:write:{actor.id}", 30, 60)])
    maker = async_sessionmaker(session.bind, expire_on_commit=False)  # type: ignore[arg-type]
    out, push = await svc.update(session, redis, actor, ann_id, body, _parse_if_match(if_match))
    if push is not None:
        background.add_task(svc.push, maker, ann_id, push)
    return out


@router.delete("/admin/announcements/{ann_id}", status_code=204)
async def delete(
    ann_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await rate_limit.enforce(redis, [(f"rl:ann:delete:{actor.id}", 10, 60)])
    await svc.delete_(session, actor, ann_id)
    return Response(status_code=204)


@router.get("/admin/announcements/{ann_id}/reads")
async def reads(
    ann_id: uuid.UUID,
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Manage,
):
    await _admin_read_limit(redis, actor)
    return await svc.reads(session, ann_id, params)
