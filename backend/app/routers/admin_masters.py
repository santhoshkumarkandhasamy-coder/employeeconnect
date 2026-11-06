"""Departments, shifts, leave types, client locations, per-employee leave balances and the audit-log viewer (contract
section 9 / 14). GET of departments, shifts and leave types is open to any signed-in user (pickers in forms); writes need the
`*.manage` permission. Client locations carry geo coordinates, so every endpoint needs `client_location.manage`."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.db import get_session
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.admin import ApplyDefaultsIn, BalancesIn, ClientLocationIn, DepartmentIn, LeaveTypeIn, ShiftIn
from ..services import admin_masters as svc
from ..services import onduty as onduty_svc

router = APIRouter(prefix="/admin", tags=["admin-masters"])
Self = Depends(require_permission())


class MasterPage(PageParams):
    """Masters are small and feed dropdowns, so the default page is the maximum (100)."""

    def __init__(self, page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=100)):
        super().__init__(page, page_size)


# ---- departments
@router.get("/departments")
async def list_departments(
    params: MasterPage = Depends(),
    q: str | None = Query(None, max_length=100),
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Self,
):
    return await svc.list_departments(session, params, q, is_active)


@router.post("/departments", status_code=201)
async def create_department(
    body: DepartmentIn,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("department.manage")),
):
    return await svc.create_department(session, actor, body)


@router.get("/departments/{dept_id}")
async def get_department(dept_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: Principal = Self):
    return await svc.get_department(session, dept_id)


@router.put("/departments/{dept_id}")
async def update_department(
    dept_id: uuid.UUID,
    body: DepartmentIn,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("department.manage")),
):
    return await svc.update_department(session, actor, dept_id, body)


@router.delete("/departments/{dept_id}", status_code=204)
async def delete_department(
    dept_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("department.manage")),
):
    await svc.delete_department(session, actor, dept_id)
    return Response(status_code=204)


# ---- client locations (on duty). Coordinates are admin data: even GET needs client_location.manage.
_LOC = Depends(require_permission("client_location.manage"))


@router.get("/client-locations")
async def list_client_locations(
    params: MasterPage = Depends(),
    q: str | None = Query(None, max_length=100),
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = _LOC,
):
    return await onduty_svc.list_locations(session, params, q, is_active)


@router.post("/client-locations", status_code=201)
async def create_client_location(body: ClientLocationIn, session: AsyncSession = Depends(get_session), actor: Principal = _LOC):
    return await onduty_svc.create_location(session, actor, body)


@router.get("/client-locations/{loc_id}")
async def get_client_location(loc_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: Principal = _LOC):
    return await onduty_svc.get_location(session, loc_id)


@router.put("/client-locations/{loc_id}")
async def update_client_location(
    loc_id: uuid.UUID, body: ClientLocationIn, session: AsyncSession = Depends(get_session), actor: Principal = _LOC
):
    return await onduty_svc.update_location(session, actor, loc_id, body)


@router.delete("/client-locations/{loc_id}", status_code=204)
async def delete_client_location(loc_id: uuid.UUID, session: AsyncSession = Depends(get_session), actor: Principal = _LOC):
    await onduty_svc.delete_location(session, actor, loc_id)
    return Response(status_code=204)


# ---- shifts
@router.get("/shifts")
async def list_shifts(
    params: MasterPage = Depends(),
    q: str | None = Query(None, max_length=100),
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Self,
):
    return await svc.list_shifts(session, params, q, is_active)


@router.post("/shifts", status_code=201)
async def create_shift(
    body: ShiftIn,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("shift.manage")),
):
    return await svc.create_shift(session, actor, body)


@router.get("/shifts/{shift_id}")
async def get_shift(shift_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: Principal = Self):
    return await svc.get_shift(session, shift_id)


@router.put("/shifts/{shift_id}")
async def update_shift(
    shift_id: uuid.UUID,
    body: ShiftIn,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("shift.manage")),
):
    return await svc.update_shift(session, actor, shift_id, body)


@router.delete("/shifts/{shift_id}", status_code=204)
async def delete_shift(
    shift_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("shift.manage")),
):
    await svc.delete_shift(session, actor, shift_id)
    return Response(status_code=204)


# ---- leave types
@router.get("/leave-types")
async def list_leave_types(
    params: MasterPage = Depends(),
    q: str | None = Query(None, max_length=100),
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_session),
    _: Principal = Self,
):
    return await svc.list_leave_types(session, params, q, is_active)


@router.post("/leave-types", status_code=201)
async def create_leave_type(
    body: LeaveTypeIn,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("leave_type.manage")),
):
    return await svc.create_leave_type(session, actor, body)


@router.get("/leave-types/{type_id}")
async def get_leave_type(type_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: Principal = Self):
    return await svc.get_leave_type(session, type_id)


@router.put("/leave-types/{type_id}")
async def update_leave_type(
    type_id: uuid.UUID,
    body: LeaveTypeIn,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("leave_type.manage")),
):
    return await svc.update_leave_type(session, actor, type_id, body)


@router.delete("/leave-types/{type_id}", status_code=204)
async def delete_leave_type(
    type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("leave_type.manage")),
):
    await svc.delete_leave_type(session, actor, type_id)
    return Response(status_code=204)


# ---- leave balances
@router.get("/employees/{user_id}/leave-balances")
async def get_balances(
    user_id: uuid.UUID,
    year: int | None = Query(None, ge=2000, le=2200),
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("leave_balance.manage")),
):
    return await svc.get_balances(session, actor, user_id, year)


@router.put("/employees/{user_id}/leave-balances")
async def put_balances(
    user_id: uuid.UUID,
    body: BalancesIn,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("leave_balance.manage")),
):
    return await svc.set_balances(session, actor, user_id, body)


@router.post("/leave-balances/apply-defaults")
async def apply_defaults(
    body: ApplyDefaultsIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("leave_balance.manage")),
):
    await rate_limit.enforce(redis, [(f"rl:admin:applydefaults:{actor.id}", 2, 60)])
    return await svc.apply_default_quota(session, actor, body.year)


# ---- audit logs
@router.get("/audit-logs")
async def audit_logs(
    params: PageParams = Depends(),
    actor: str | None = Query(None, max_length=64),
    action: str | None = Query(None, max_length=80),
    entity_type: str | None = Query(None, max_length=50),
    entity_id: str | None = Query(None, max_length=64),
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    viewer: Principal = Depends(require_permission("audit.view")),
):
    await rate_limit.enforce(redis, [(f"rl:admin:auditview:{viewer.id}", 30, 60)])
    return await svc.list_audit_logs(
        session, viewer, params, actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, from_=from_, to=to
    )
