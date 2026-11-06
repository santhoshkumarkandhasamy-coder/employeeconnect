"""Admin console endpoints (contract s.12.4 - 12.6): dashboard, attendance/day, system health, sessions, license."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..services import admin_security, console, sessions, system_health
from ..services import license as license_svc

router = APIRouter(prefix="/admin", tags=["admin-console"])


class LicenseInstall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    license_key: str = Field(min_length=10, max_length=license_svc.MAX_TOKEN_LEN)
    current_password: SecretStr = Field(min_length=1, max_length=128)  # step-up: always required


# ---------------------------------------------------------------- dashboard / attendance day / health
@router.get("/dashboard")
async def dashboard(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("console.access")),
):
    await rate_limit.enforce(redis, [(f"rl:admin:dashboard:{actor.id}", 60, 60)])
    return await console.dashboard(session, actor)


@router.get("/attendance/day")
async def attendance_day(
    params: PageParams = Depends(),
    date: dt.date | None = None,
    status: Literal["all", "checked_in", "late", "not_checked_in", "on_leave"] = "all",
    department_id: uuid.UUID | None = None,
    q: str | None = Query(None, max_length=100),
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("attendance.view_all", "attendance.view_team")),
):
    return await console.attendance_day(session, actor, params, day=date, status=status, department_id=department_id, q=q)


@router.get("/system/health")
async def system_health_(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("system.view")),
):
    await rate_limit.enforce(redis, [(f"rl:admin:health:{actor.id}", 30, 60)])
    return await system_health.health(session, redis)


# ---------------------------------------------------------------- sessions
@router.get("/sessions")
async def list_sessions(
    params: PageParams = Depends(),
    user_id: uuid.UUID | None = None,
    employee_id: str | None = Query(None, max_length=32),
    client_type: Literal["mobile", "web"] | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("session.manage")),
):
    await rate_limit.enforce(redis, [(f"rl:admin:sessions:{actor.id}", 30, 60)])
    return await sessions.list_sessions(session, actor, params, user_id=user_id, employee_id=employee_id, client_type=client_type)


@router.get("/employees/{user_id}/sessions")
async def employee_sessions(
    user_id: uuid.UUID,
    params: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("session.manage")),
):
    await rate_limit.enforce(redis, [(f"rl:admin:sessions:{actor.id}", 30, 60)])
    return await sessions.list_for_employee(session, actor, user_id, params)


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(
    session_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("session.manage")),
):
    await sessions.revoke_one(session, actor, session_id)
    return Response(status_code=204)


@router.post("/employees/{user_id}/revoke-sessions")
async def revoke_sessions(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("session.manage")),
):
    return await sessions.revoke_all(session, actor, user_id)


# ---------------------------------------------------------------- license
@router.get("/license")
async def get_license(session: AsyncSession = Depends(get_session), _: Principal = Depends(require_permission("license.manage"))):
    return await license_svc.license_out(session)


@router.put("/license")
async def put_license(
    body: LicenseInstall,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("license.manage")),
):
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="license.install")  # 5/min
    return await license_svc.install_token(session, body.license_key, actor=actor)
