"""Payroll and payslips (contract section 13). Every endpoint declares its permission; the services enforce the self rule."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from ..core.audit import audit
from ..core.db import get_session
from ..core.errors import AppError
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..models import Payslip
from ..schemas.payroll import (
    DetailsIn,
    PayrollSettingsIn,
    PayslipUpdate,
    PublishIn,
    RegisterIn,
    ReviseIn,
    RunCreate,
    StructureIn,
)
from ..services import admin_security, payroll_register, payroll_settings, payroll_structures, payslip_pdf, xlsx
from ..services import payroll_common as pc
from ..services import payroll_runs as runs

log = logging.getLogger("app.payroll")

PDF_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}

employee_router = APIRouter(prefix="/payslips", tags=["payslips"])
admin_router = APIRouter(prefix="/admin/payroll", tags=["payroll-admin"])

VIEW_OWN = require_permission("payroll.view_own")
VIEW_ALL = require_permission("payroll.view_all")
MANAGE = require_permission("payroll.manage")
PUBLISH = require_permission("payroll.publish")


def _pdf_response(data: bytes, name: str) -> Response:
    return Response(
        content=data, media_type="application/pdf",
        headers={**PDF_HEADERS, "Content-Disposition": f'attachment; filename="{name}"'},
    )  # fmt: skip


# ============================================================================================ employee
@employee_router.get("")
async def my_payslips(
    year: int | None = Query(None, ge=2000, le=2200),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await pc.limit(redis, actor, "own_list", (60, 60))
    return await runs.list_own(session, actor, year)


@employee_router.get("/{payslip_id}")
async def my_payslip(
    payslip_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await pc.limit(redis, actor, "own_detail", (30, 60), (200, 86400))
    ps = await runs.own_payslip_or_404(session, actor, payslip_id)
    return await runs.own_view(session, actor, ps, audit_action="payroll.payslip.view_own")


@employee_router.get("/{payslip_id}/pdf")
async def my_payslip_pdf(
    payslip_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await pc.limit(redis, actor, "own_detail", (30, 60), (200, 86400), closed=True)  # PDF building is expensive
    ps = await runs.own_payslip_or_404(session, actor, payslip_id)
    view, footer, logo = await runs.pdf_parts(session, ps, admin=False)
    data = await payslip_pdf.build_pdf(view, footer, logo, payslip_pdf.watermark_for(ps.status))
    audit(session, action="payroll.payslip.download", actor=actor, entity_type="payslip", entity_id=payslip_id)
    await pc.commit(session)
    return _pdf_response(data, payslip_pdf.filename_for(view))


# ============================================================================================ settings
@admin_router.get("/settings")
async def get_settings_(
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Depends(VIEW_ALL)
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_settings.read_all(session)


@admin_router.put("/settings")
async def put_settings(
    body: PayrollSettingsIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_settings.update(session, actor, body.settings, redis=redis, current_password=body.current_password)


# ============================================================================================ employees / structures / details
@admin_router.get("/employees")
async def employees(
    q: str | None = Query(None, max_length=100),
    department_id: uuid.UUID | None = None,
    has_structure: bool | None = None,
    status: Literal["active", "inactive"] | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_structures.list_employees(session, q, department_id, has_structure, status, page)


@admin_router.get("/employees/{user_id}/structures")
async def list_structures(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_structures.list_structures(session, actor, user_id)


@admin_router.post("/employees/{user_id}/structures", status_code=201)
async def create_structure(
    user_id: uuid.UUID,
    body: StructureIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_structures.create_structure(session, actor, user_id, body)


@admin_router.put("/structures/{structure_id}")
async def update_structure(
    structure_id: uuid.UUID,
    body: StructureIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_structures.update_structure(session, actor, structure_id, body)


@admin_router.delete("/structures/{structure_id}", status_code=204)
async def delete_structure(
    structure_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    await payroll_structures.delete_structure(session, actor, structure_id)
    return Response(status_code=204)


@admin_router.get("/employees/{user_id}/details")
async def get_details(
    user_id: uuid.UUID,
    reveal: bool = False,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    if reveal:
        await pc.limit(redis, actor, "reveal", (30, 60), (200, 86400), closed=True)
    else:
        await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_structures.get_details(session, redis, actor, user_id, reveal)


@admin_router.put("/employees/{user_id}/details")
async def put_details(
    user_id: uuid.UUID,
    body: DetailsIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_structures.put_details(session, redis, actor, user_id, body)


# ============================================================================================ runs
@admin_router.get("/runs")
async def list_runs(
    year: int | None = Query(None, ge=2000, le=2200),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await runs.list_runs(session, year)


@admin_router.post("/runs", status_code=201)
async def create_run(
    body: RunCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await runs.create_run(session, actor, body)


@admin_router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await runs.get_run(session, actor, run_id)


@admin_router.delete("/runs/{run_id}", status_code=204)
async def delete_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    await runs.delete_run(session, actor, run_id)
    return Response(status_code=204)


@admin_router.post("/runs/{run_id}/refresh")
async def refresh_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await runs.refresh_run(session, actor, run_id)


@admin_router.post("/runs/{run_id}/process")
async def process_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await runs.process_run(session, actor, run_id)


@admin_router.post("/runs/{run_id}/reopen")
async def reopen_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await runs.reopen_run(session, actor, run_id)


@admin_router.post("/runs/{run_id}/publish")
async def publish_run(
    run_id: uuid.UUID,
    body: PublishIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(PUBLISH),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await runs.publish_run(session, redis, actor, run_id, body.current_password)


@admin_router.get("/runs/{run_id}/payslips")
async def run_payslips(
    run_id: uuid.UUID,
    q: str | None = Query(None, max_length=100),
    status: Literal["draft", "processed", "published", "superseded"] | None = None,
    flag: Literal["error", "warning", "hold"] | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await runs.list_run_payslips(session, run_id, q, status, flag, page)


@admin_router.post("/runs/{run_id}/employees/{user_id}/revise", status_code=201)
async def revise(
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    body: ReviseIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await runs.revise(session, actor, run_id, user_id, body.reason)


@admin_router.post("/runs/{run_id}/register")
async def register(
    run_id: uuid.UUID,
    body: RegisterIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    who = SimpleNamespace(id=actor.id, employee_id=actor.employee_id)
    await pc.limit(redis, actor, "register", (5, 60), closed=True)
    if body.include_bank:
        if not actor.has("payroll.manage"):
            raise AppError("FORBIDDEN", "You do not have permission to export bank details")
        await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.register.bank")
    built, name, rows = await payroll_register.build(session, run_id, body.include_bank)
    try:
        audit(session, action="payroll.register.export", actor=who, entity_type="payroll_run", entity_id=run_id,
              after={"include_bank": body.include_bank, "rows": rows})  # fmt: skip
        await pc.commit(session)
    except BaseException:
        built.close()
        raise
    return StreamingResponse(
        xlsx.iter_file(built),
        media_type=xlsx.XLSX_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(built.close),
    )


# ============================================================================================ payslips (admin)
@admin_router.get("/payslips/{payslip_id}")
async def get_payslip(
    payslip_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await runs.get_payslip(session, actor, payslip_id)


@admin_router.put("/payslips/{payslip_id}")
async def update_payslip(
    payslip_id: uuid.UUID,
    body: PayslipUpdate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await runs.update_payslip(session, actor, payslip_id, body)


@admin_router.delete("/payslips/{payslip_id}", status_code=204)
async def delete_payslip(
    payslip_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    await runs.delete_payslip(session, actor, payslip_id)
    return Response(status_code=204)


@admin_router.post("/payslips/{payslip_id}/process")
async def process_payslip(
    payslip_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await runs.process_payslip(session, actor, payslip_id)


@admin_router.post("/payslips/{payslip_id}/reopen")
async def reopen_payslip(
    payslip_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await runs.reopen_payslip(session, actor, payslip_id)


@admin_router.post("/payslips/{payslip_id}/publish")
async def publish_payslip(
    payslip_id: uuid.UUID,
    body: PublishIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(PUBLISH),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await runs.publish_payslip(session, redis, actor, payslip_id, body.current_password)


@admin_router.get("/payslips/{payslip_id}/pdf")
async def admin_payslip_pdf(
    payslip_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60), closed=True)  # PDF building is expensive
    ps = (await session.execute(select(Payslip).where(Payslip.id == payslip_id))).scalar_one_or_none()
    if ps is None:
        raise AppError("NOT_FOUND", "Payslip not found")
    view, footer, logo = await runs.pdf_parts(session, ps, admin=True)
    data = await payslip_pdf.build_pdf(view, footer, logo, payslip_pdf.watermark_for(ps.status))
    audit(session, action="payroll.payslip.download", actor=actor, entity_type="payslip", entity_id=payslip_id,
          after={"admin": True})  # fmt: skip
    await pc.commit(session)
    return _pdf_response(data, payslip_pdf.filename_for(view))
