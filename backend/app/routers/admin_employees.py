from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.employees import (
    DeactivateIn,
    EmployeeCreate,
    EmployeeCreated,
    EmployeeDetail,
    EmployeeOut,
    EmployeeUpdate,
    ResetPasswordIn,
    TemporaryPassword,
)
from ..services import employees as svc

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/employees")
async def list_employees(
    params: PageParams = Depends(),
    q: str | None = Query(None, max_length=100),
    department_id: uuid.UUID | None = None,
    role: str | None = Query(None, max_length=50),
    status: str | None = Query(None, pattern="^(active|inactive)$"),
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("employee.view")),
):
    return await svc.list_employees(session, actor, params, q=q, department_id=department_id, role=role, status=status)


@router.post("/employees", status_code=201, response_model=EmployeeCreated)
async def create_employee(
    body: EmployeeCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("employee.create")),
):
    await rate_limit.enforce(redis, [(f"rl:admin:create:{actor.id}", 30, 60)])
    return await svc.create_employee(session, redis, actor, body)


@router.get("/employees/{user_id}", response_model=EmployeeDetail, response_model_exclude_unset=True)
async def get_employee(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("employee.view")),
):
    return await svc.get_employee(session, redis, actor, user_id)


@router.put("/employees/{user_id}", response_model=EmployeeDetail, response_model_exclude_unset=True)
async def update_employee(
    user_id: uuid.UUID,
    body: EmployeeUpdate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("employee.update")),
):
    return await svc.update_employee(session, redis, actor, user_id, body)


@router.delete("/employees/{user_id}", status_code=204)
async def delete_employee(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("employee.delete")),
):
    await svc.delete_employee(session, actor, user_id)
    return Response(status_code=204)


@router.get("/password-reset-requests")
async def password_reset_requests(
    params: PageParams = Depends(),
    status: str | None = Query(None, pattern="^(open|handled|expired)$"),
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("employee.reset_password")),
):
    return await svc.list_reset_requests(session, actor, params, status=status)


@router.post("/employees/{user_id}/deactivate", response_model=EmployeeOut, response_model_exclude_unset=True)
async def deactivate(
    user_id: uuid.UUID,
    body: DeactivateIn | None = None,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("employee.deactivate")),
):
    return await svc.set_active(session, actor, user_id, active=False, reason=body.reason if body else "other")


@router.post("/employees/{user_id}/activate", response_model=EmployeeOut, response_model_exclude_unset=True)
async def activate(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("employee.deactivate")),
):
    return await svc.set_active(session, actor, user_id, active=True)


@router.post("/employees/{user_id}/unlock", response_model=EmployeeOut, response_model_exclude_unset=True)
async def unlock(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("employee.unlock")),
):
    return await svc.unlock(session, actor, user_id)


@router.post("/employees/{user_id}/reset-password", response_model=TemporaryPassword)
async def reset_password(
    user_id: uuid.UUID,
    body: ResetPasswordIn | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("employee.reset_password")),
):
    await rate_limit.enforce(redis, [(f"rl:admin:reset:{actor.id}", 10, 60)], fail_closed=True)
    return await svc.reset_password(session, redis, actor, user_id, body.current_password if body else None)
