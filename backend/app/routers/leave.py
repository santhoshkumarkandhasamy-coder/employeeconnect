from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.config import get_settings
from ..core.db import get_session
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.leave import ApproveBody, LeaveApply, OnDutyApply, PermissionApply, RejectBody
from ..services import leave as svc
from ..services import onduty as onduty_svc

router = APIRouter(tags=["leave"])
Self = Depends(require_permission())


async def _limit_write(redis: Redis, actor: Principal) -> None:
    """Apply + cancel (both kinds) share one per-user budget. Fail CLOSED: each call writes audit rows and may push."""
    st = get_settings()
    await rate_limit.enforce(
        redis,
        [
            (f"rl:leave:write:min:{actor.id}", st.leave_write_rate_per_min, 60),
            (f"rl:leave:write:day:{actor.id}", st.leave_write_rate_per_day, 86400),
        ],
        fail_closed=True,
    )


async def _limit_decide(redis: Redis, actor: Principal) -> None:
    await rate_limit.enforce(
        redis, [(f"rl:leave:decide:{actor.id}", get_settings().leave_decide_rate_per_min, 60)], fail_closed=True
    )


@router.get("/leave/types")
async def types(session: AsyncSession = Depends(get_session), _: Principal = Self):
    return await svc.list_types(session)


@router.get("/leave/balance")
async def balance(
    year: int | None = Query(None, ge=2000, le=2200), session: AsyncSession = Depends(get_session), actor: Principal = Self
):
    return await svc.balance(session, actor, year)


@router.post("/leave/requests", status_code=201)
async def apply_leave(
    body: LeaveApply,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _limit_write(redis, actor)
    return await svc.apply_leave(session, actor, body, redis)


@router.post("/permission/requests", status_code=201)
async def apply_permission(
    body: PermissionApply,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _limit_write(redis, actor)
    return await svc.apply_permission(session, actor, body, redis)


@router.post("/on-duty/requests", status_code=201)
async def apply_on_duty(
    body: OnDutyApply,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Self,
):
    await _limit_write(redis, actor)
    return await onduty_svc.apply_on_duty(session, actor, body, redis)


@router.get("/on-duty/locations")
async def on_duty_locations(session: AsyncSession = Depends(get_session), _: Principal = Self):
    """Active client locations for the request form (name + address only)."""
    return await onduty_svc.list_active_brief(session)


def _make_mine(kind: str, path: str) -> None:
    @router.get(f"/{path}/requests")
    async def _list(
        params: PageParams = Depends(),
        status: str | None = Query(None, max_length=16),
        from_: dt.date | None = Query(None, alias="from"),
        to: dt.date | None = None,
        session: AsyncSession = Depends(get_session),
        actor: Principal = Self,
    ):
        return await svc.list_mine(session, actor, kind, params, status, from_, to)

    @router.get(f"/{path}/requests/{{req_id}}")
    async def _get(req_id: uuid.UUID, session: AsyncSession = Depends(get_session), actor: Principal = Self):
        return await svc.get_mine(session, actor, kind, req_id)

    @router.post(f"/{path}/requests/{{req_id}}/cancel")
    async def _cancel(
        req_id: uuid.UUID,
        session: AsyncSession = Depends(get_session),
        redis: Redis = Depends(get_redis),
        actor: Principal = Self,
    ):
        await _limit_write(redis, actor)
        return await svc.cancel(session, actor, kind, req_id)


_make_mine("leave", "leave")
_make_mine("permission", "permission")
_make_mine("on_duty", "on-duty")


@router.get("/admin/requests")
async def admin_requests(
    params: PageParams = Depends(),
    type: Literal["leave", "permission", "on_duty"] = "leave",
    status: str | None = Query(None, max_length=16),
    department_id: uuid.UUID | None = None,
    employee_id: str | None = Query(None, max_length=32),
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("request.view_team", "request.view_all")),
):
    return await svc.admin_list(
        session,
        actor,
        params,
        kind=type,
        status=status,
        department_id=department_id,
        employee_id=employee_id,
        d_from=from_,
        d_to=to,
    )


_APPROVE = Depends(require_permission("request.approve_team", "request.approve_all"))


@router.put("/admin/requests/{type}/{req_id}/approve")
async def approve(
    type: Literal["leave", "permission", "on_duty"],
    req_id: uuid.UUID,
    body: ApproveBody | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _APPROVE,
):
    await _limit_decide(redis, actor)
    return await svc.decide(session, actor, type, req_id, approve=True, comment=body.comment if body else None)


@router.put("/admin/requests/{type}/{req_id}/reject")
async def reject(
    type: Literal["leave", "permission", "on_duty"],
    req_id: uuid.UUID,
    body: RejectBody,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _APPROVE,
):
    await _limit_decide(redis, actor)
    return await svc.decide(session, actor, type, req_id, approve=False, comment=body.comment)
