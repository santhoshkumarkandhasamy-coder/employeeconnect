"""On Duty endpoints (plan 13 / docs/plans/13-on-duty-contract.md 14.3, 14.6). Employee endpoints need only authentication and
only ever touch the caller's own data; the admin visit list reuses attendance.view_team / attendance.view_all."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.on_duty import DutyCheckIn, DutyCheckOut, OnDutyApply
from ..services import on_duty as svc
from .attendance import _punch_limit, _run_punch
from .leave import _limit_write

router = APIRouter(tags=["on-duty"])
Self = Depends(require_permission())


async def _read_limit(redis: Redis, actor: Principal) -> None:
    await rate_limit.enforce(redis, [(f"rl:onduty:read:{actor.id}", 60, 60)])


@router.get("/on-duty/locations")
async def locations(session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Self):
    await _read_limit(redis, actor)
    return await svc.my_locations(session, actor)


@router.get("/on-duty/today")
async def today(session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Self):
    await _read_limit(redis, actor)
    return await svc.today(session, actor)


@router.post("/on-duty/check-in", status_code=201)
async def check_in(
    body: DutyCheckIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _punch_limit(redis, actor)  # the SAME limiter and failure counter as office punches: one budget per user
    return await _run_punch(redis, actor, lambda: svc.check_in(session, actor, body))


@router.post("/on-duty/check-out")
async def check_out(
    body: DutyCheckOut,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _punch_limit(redis, actor)
    return await _run_punch(redis, actor, lambda: svc.check_out(session, actor, body))


@router.get("/on-duty/visits")
async def visits(
    params: PageParams = Depends(),
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _read_limit(redis, actor)
    return await svc.my_visits(session, actor, params, from_, to)


@router.post("/on-duty/requests", status_code=201)
async def apply(
    body: OnDutyApply,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _limit_write(redis, actor)  # shared leave write limiter (fail closed)
    return await svc.apply(session, actor, body, redis)


@router.get("/on-duty/requests")
async def list_requests(
    params: PageParams = Depends(),
    status: str | None = Query(None, max_length=16),
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _read_limit(redis, actor)
    return await svc.list_mine(session, actor, params, status, from_, to)


@router.get("/on-duty/requests/{req_id}")
async def get_request(
    req_id: uuid.UUID, session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Self
):
    await _read_limit(redis, actor)
    return await svc.get_mine(session, actor, req_id)


@router.post("/on-duty/requests/{req_id}/cancel")
async def cancel_request(
    req_id: uuid.UUID, session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Self
):
    await _limit_write(redis, actor)
    return await svc.cancel(session, actor, req_id)


# ---------------------------------------------------------------- admin
@router.get("/admin/on-duty/visits")
async def admin_visits(
    params: PageParams = Depends(),
    date: dt.date | None = None,
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    location_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    employee_id: str | None = Query(None, max_length=32),
    open: bool = False,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("attendance.view_team", "attendance.view_all")),
):
    await rate_limit.enforce(redis, [(f"rl:admin:onduty:visits:{actor.id}", 60, 60)])
    return await svc.admin_visits(
        session,
        actor,
        params,
        on_date=date,
        d_from=from_,
        d_to=to,
        location_id=location_id,
        department_id=department_id,
        employee_id=employee_id,
        open_only=open,
    )
