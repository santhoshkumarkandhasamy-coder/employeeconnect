"""Attendance devices (plan 18, contract section 21). The API never connects to a device: test / sync are queued for the worker."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.devices import DeviceIn, DeviceTestIn, EmptyBody, MappingIn
from ..services import device_attendance, device_matching
from ..services import devices as svc

router = APIRouter(prefix="/admin", tags=["devices"])

_VIEW = Depends(require_permission("device.view"))
_MANAGE = Depends(require_permission("device.manage"))
_MAP = Depends(require_permission("device.map"))
_SYNC = Depends(require_permission("device.map", "device.manage"))


async def _list_limit(redis: Redis, actor: Principal) -> None:
    await rate_limit.enforce(redis, [(f"rl:device-list:{actor.id}", 60, 60)])  # 60 / min


@router.get("/devices")
async def list_devices(session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = _VIEW):
    await _list_limit(redis, actor)
    return await svc.list_devices(session)


@router.post("/devices", status_code=201)
async def create_device(body: DeviceIn, session: AsyncSession = Depends(get_session), actor: Principal = _MANAGE):
    return await svc.create_device(session, actor, body)


# fixed paths before /devices/{id}
@router.post("/devices/test", status_code=202)
async def test_unsaved(
    body: DeviceTestIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _MANAGE,
):
    row = await svc.request_test(session, redis, actor, device_id=None, params=body)
    return {"sync_id": str(row.id)}


@router.post("/devices/sync-all", status_code=202)
async def sync_all(session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = _SYNC):
    pairs = await svc.request_sync_all(session, redis, actor)
    return {"items": [{"device_id": str(d), "sync_id": str(s)} for d, s in pairs]}


@router.get("/devices/{device_id}")
async def get_device(
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _VIEW,
):
    await _list_limit(redis, actor)
    return await svc.get_device(session, device_id)


@router.put("/devices/{device_id}")
async def update_device(
    device_id: uuid.UUID, body: DeviceIn, session: AsyncSession = Depends(get_session), actor: Principal = _MANAGE
):
    return await svc.update_device(session, actor, device_id, body)


@router.delete("/devices/{device_id}", status_code=204)
async def delete_device(device_id: uuid.UUID, session: AsyncSession = Depends(get_session), actor: Principal = _MANAGE):
    await svc.delete_device(session, actor, device_id)
    return Response(status_code=204)


@router.post("/devices/{device_id}/test", status_code=202)
async def test_saved(
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _MANAGE,
):
    row = await svc.request_test(session, redis, actor, device_id=device_id, params=None)
    return {"sync_id": str(row.id)}


@router.post("/devices/{device_id}/sync", status_code=202)
async def sync_now(
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _SYNC,
):
    row = await svc.request_sync(session, redis, actor, device_id)
    return {"sync_id": str(row.id)}


@router.post("/devices/{device_id}/accept-identity")
async def accept_identity(
    device_id: uuid.UUID, body: EmptyBody, session: AsyncSession = Depends(get_session), actor: Principal = _MANAGE
):
    return await svc.accept_identity(session, actor, device_id)


@router.get("/devices/{device_id}/syncs")
async def device_syncs(
    device_id: uuid.UUID,
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _VIEW,
):
    await _list_limit(redis, actor)
    return await svc.list_syncs(session, device_id, params)


@router.get("/device-syncs/{sync_id}")
async def get_sync(sync_id: uuid.UUID, session: AsyncSession = Depends(get_session), actor: Principal = _VIEW):
    return await svc.get_sync(session, actor, sync_id)


# ---------------------------------------------------------------- device users
@router.get("/device-users")
async def device_users(
    params: PageParams = Depends(),
    device_id: uuid.UUID | None = None,
    status: Literal["unmatched", "matched", "ignored", "all"] = "unmatched",
    q: str | None = Query(None, max_length=60),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _VIEW,
):
    await _list_limit(redis, actor)
    return await device_matching.list_device_users(session, params, device_id=device_id, status=status, q=q)


@router.put("/device-users/{du_id}/mapping")
async def set_mapping(
    du_id: uuid.UUID,
    body: MappingIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _MAP,
):
    return await device_matching.set_mapping(session, redis, actor, du_id, body.user_id, body.apply_to_all_devices)


@router.post("/device-users/{du_id}/auto")
async def reset_auto(
    du_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _MAP,
):
    return await device_matching.reset_auto(session, redis, actor, du_id)


# ---------------------------------------------------------------- punch log
@router.get("/device-punches")
async def device_punches(
    params: PageParams = Depends(),
    device_id: uuid.UUID | None = None,
    employee_id: str | None = Query(None, max_length=32),
    device_user_id: str | None = Query(None, max_length=24),
    from_: dt.date = Query(..., alias="from"),
    to: dt.date = Query(...),
    outcome: Literal["check_in", "check_out", "extra", "unmatched", "ignored", "quarantined", "not_applied"] | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _VIEW,
):
    await _list_limit(redis, actor)
    return await device_attendance.punch_log(
        session, params, device_id=device_id, employee_id=employee_id, device_user_id=device_user_id,
        d_from=from_, d_to=to, outcome=outcome,
    )  # fmt: skip
