"""Payroll and payslips (contract section 13). Every endpoint declares its permission; the services enforce the self rule."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import SecretStr
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
    ArrearsAccept,
    ArrearsBulkAccept,
    BatchApprove,
    BatchReject,
    BulkIncrementCreate,
    ChallanIn,
    CtcCalculateIn,
    DetailsIn,
    EmptyIn,
    EncashmentCreate,
    FnfApprove,
    FnfCreate,
    FnfReject,
    FnfRevoke,
    FnfUpdate,
    IncrementApprove,
    IncrementCreate,
    IncrementReject,
    IncrementUpdate,
    LoanCancel,
    LoanCreate,
    LoanForeclose,
    LoanReschedule,
    LoanSkip,
    OneTimeItemCreate,
    OneTimeItemUpdate,
    OptionalReasonIn,
    OvertimeBulk,
    OvertimeUpdate,
    PayrollSettingsIn,
    PayslipUpdate,
    PublishIn,
    ReasonIn,
    RegisterIn,
    ReviseIn,
    RunCreate,
    StatutoryFileIn,
    StructureIn,
    VersionIn,
)
from ..services import (
    admin_security,
    fnf_pdf,
    payroll_arrears,
    payroll_ctc_gratuity,
    payroll_delivery,
    payroll_encashment,
    payroll_fnf,
    payroll_increments,
    payroll_items,
    payroll_loans,
    payroll_overtime,
    payroll_register,
    payroll_reports,
    payroll_settings,
    payroll_statutory,
    payroll_structure_import,
    payroll_structures,
    payslip_pdf,
    xlsx,
    xlsx_import,
)
from ..services import payroll_common as pc
from ..services import payroll_runs as runs

log = logging.getLogger("app.payroll")

PDF_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}

employee_router = APIRouter(prefix="/payslips", tags=["payslips"])
admin_router = APIRouter(prefix="/admin/payroll", tags=["payroll-admin"])
me_router = APIRouter(prefix="/me", tags=["payroll-own"])

VIEW_OWN = require_permission("payroll.view_own")
VIEW_ALL = require_permission("payroll.view_all")
MANAGE = require_permission("payroll.manage")
APPROVE = require_permission("payroll.approve")
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
    await payroll_delivery.mark_seen(session, ps, downloaded=True)
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


# ============================================================================================ phase 2: dashboard and reports
@admin_router.get("/dashboard")
async def dashboard(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "dashboard", (30, 60))
    out = await payroll_reports.dashboard(session, period)
    audit(session, action="payroll.dashboard.view", actor=actor, entity_type="payroll_dashboard", after={"period": out["period"]})
    await pc.commit(session)
    return out


@admin_router.get("/variance")
async def variance(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "variance", (20, 60))
    out = await payroll_reports.variance(session, period)
    audit(session, action="payroll.variance.view", actor=actor, entity_type="payroll_dashboard", after={"period": out["period"]})
    await pc.commit(session)
    return out


@admin_router.post("/reports/gratuity")
async def gratuity_report_xlsx(
    body: RegisterIn | None = None,
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    """Gratuity accrual of the month as .xlsx (contract 18.3). Static path: it must come before `/reports/{kind}`."""
    await pc.limit(redis, actor, "report", (10, 60), closed=True)
    built, name, rows, _as_of = await payroll_ctc_gratuity.gratuity_xlsx(session, period)
    try:
        audit(session, action="payroll.report.export", actor=actor, entity_type="payroll_dashboard",
              after={"kind": "gratuity", "period": period, "rows": rows})  # fmt: skip
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


@admin_router.post("/reports/{kind}")
async def extra_report(
    kind: Literal["department", "headcount", "ctc", "lop", "bank_advice"],
    body: RegisterIn,
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    """Body reuses RegisterIn: `current_password` is required (and `payroll.manage`) only for `bank_advice`."""
    await pc.limit(redis, actor, "report", (10, 60), closed=True)
    if kind == "bank_advice":
        if not actor.has("payroll.manage"):
            raise AppError("FORBIDDEN", "You do not have permission to export bank details")
        await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.report.bank")
    built, name, rows = await payroll_reports.report(session, kind, period)
    try:
        audit(session, action="payroll.report.export", actor=actor, entity_type="payroll_dashboard",
              after={"kind": kind, "period": period, "rows": rows})  # fmt: skip
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


# ============================================================================================ phase 2: statutory
_NOSTORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@admin_router.get("/statutory/dues")
async def statutory_dues(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    months: int = Query(1, ge=1, le=payroll_statutory.MAX_MONTHS),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "statutory", (30, 60), (500, 86400), closed=True)
    out = await payroll_statutory.dues(session, period, months)
    audit(session, action="payroll.statutory.dues_view", actor=actor, entity_type="statutory_challan",
          after={"period": period, "months": months})  # fmt: skip
    await pc.commit(session)
    return out


@admin_router.post("/statutory/{kind}")
async def statutory_file(
    kind: Literal["pf_ecr", "esi", "pt"],
    body: StatutoryFileIn | None = None,
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    """PF ECR (.txt), ESI contribution (.xlsx), PT summary (.xlsx). 409 STATUTORY_BLOCKERS when an employee lacks UAN / ESI IP.
    pf_ecr and esi carry UAN / ESI IP numbers: they need the caller's own password (`current_password`, step-up), a daily cap
    and raise an admin alert; pt carries no identifiers and is unchanged."""
    sensitive = kind != "pt"
    if sensitive:
        await pc.limit(redis, actor, "statutory_file", (10, 60), (30, 86400), closed=True)
        await admin_security.require_reauth(
            session, redis, actor, body.current_password if body else None, action=f"payroll.statutory.{kind}"
        )
    else:
        await pc.limit(redis, actor, "report", (10, 60), closed=True)
    if kind == "pf_ecr":
        data, name, rows, meta = await payroll_statutory.pf_ecr(session, period)
        audit(session, action="payroll.statutory.export", actor=actor, entity_type="statutory_challan",
              after={"kind": kind, "period": meta["period"], "rows": rows})  # fmt: skip
        await pc.commit(session)
        await _notify_identifier_export(session, actor, kind, meta["period"])
        return Response(content=data, media_type="text/plain; charset=us-ascii",
                        headers={**_NOSTORE, "Content-Disposition": f'attachment; filename="{name}"'})  # fmt: skip
    built, name, rows, meta = await (payroll_statutory.esi_contribution if kind == "esi" else payroll_statutory.pt_summary)(
        session, period
    )
    try:
        audit(session, action="payroll.statutory.export", actor=actor, entity_type="statutory_challan",
              after={"kind": kind, "period": meta["period"], "rows": rows})  # fmt: skip
        await pc.commit(session)
    except BaseException:
        built.close()
        raise
    if sensitive:
        await _notify_identifier_export(session, actor, kind, meta["period"])
    return StreamingResponse(
        xlsx.iter_file(built),
        media_type=xlsx.XLSX_MIME,
        headers={**_NOSTORE, "Content-Disposition": f'attachment; filename="{name}"'},
        background=BackgroundTask(built.close),
    )


async def _notify_identifier_export(session: AsyncSession, actor: Principal, kind: str, period: str) -> None:
    """Best effort push to the admins (names / ids only) after a file with UAN / ESI IP numbers left the system."""
    label = "PF ECR" if kind == "pf_ecr" else "ESI contribution"
    await admin_security.notify_admins(
        session, entity_id=actor.id, title=f"{label} file downloaded",
        body=f"{actor.user.full_name} ({actor.employee_id}) downloaded the {label} file for {period}.",
    )  # fmt: skip


@admin_router.put("/statutory/challans")
async def put_challan(
    body: ChallanIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_statutory.set_challan(
        session, redis, actor, type_=body.type, raw_period=body.period, status=body.status, paid_on=body.paid_on,
        reference=body.reference, amount=body.amount, password=body.current_password,
        bsr_code=body.bsr_code, challan_serial=body.challan_serial, deposited_on=body.deposited_on,
    )  # fmt: skip


# ============================================================================================ plan 15: one-time items (3a)
@admin_router.get("/items")
async def list_items(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    user_id: uuid.UUID | None = None,
    code: Literal["BONUS", "INCENTIVE", "OTHER_EARNING", "OTHER_DEDUCTION"] | None = None,
    status: Literal["pending", "processing", "paid", "cancelled"] | None = None,
    batch_id: uuid.UUID | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    out = await payroll_items.list_items(
        session, period=period, user_id=user_id, code=code, status=status, batch_id=batch_id, page=page
    )
    await pc.audit_read(session, actor, "payroll.item.list", "payroll_item", count=len(out["items"]))
    return out


@admin_router.post("/items", status_code=201)
async def create_item(
    body: OneTimeItemCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_items.create_item(session, actor, body)


@admin_router.get("/items/template")
async def items_template(redis: Redis = Depends(get_redis), actor: Principal = Depends(MANAGE)):
    await pc.limit(redis, actor, "import", (10, 60), (50, 86400), closed=True)
    built = await payroll_items.template()
    return StreamingResponse(
        xlsx.iter_file(built),
        media_type=xlsx.XLSX_MIME,
        headers={**_NOSTORE, "Content-Disposition": 'attachment; filename="payroll-items-template.xlsx"'},
        background=BackgroundTask(built.close),
    )


@admin_router.post("/items/import")
async def import_items(
    response: Response,
    file: UploadFile = File(...),
    period: str = Form(..., pattern=r"^\d{4}-\d{2}$"),
    file_sha256: str | None = Form(None, max_length=64),
    current_password: SecretStr | None = Form(None, max_length=128),
    dry_run: bool = Query(True),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    """xlsx bulk upload. `dry_run=true` (default) validates and reports; `dry_run=false` re-sends the same file with its
    `file_sha256` and the caller's own password and imports everything or nothing."""
    if dry_run:
        await pc.limit(redis, actor, "import", (10, 60), (50, 86400), closed=True)
    else:
        await pc.limit(redis, actor, "action", (10, 60), closed=True)
    data = await xlsx_import.read_upload(file)
    out = await payroll_items.import_items(
        session, redis, actor, data=data, raw_period=period, dry_run=dry_run, file_sha256=file_sha256, password=current_password
    )
    response.headers["Cache-Control"] = "no-store"
    if not dry_run:
        response.status_code = 201
    return out


@admin_router.get("/items/batches")
async def list_batches(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    out = await payroll_items.list_batches(session, period)
    await pc.audit_read(session, actor, "payroll.import.list", "payroll_import_batch", count=len(out["items"]))
    return out


@admin_router.post("/items/batches/{batch_id}/cancel")
async def cancel_batch(
    batch_id: uuid.UUID,
    body: ReasonIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_items.cancel_batch(session, actor, batch_id, body.reason)


@admin_router.put("/items/{item_id}")
async def update_item(
    item_id: uuid.UUID,
    body: OneTimeItemUpdate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_items.update_item(session, actor, item_id, body)


@admin_router.post("/items/{item_id}/cancel")
async def cancel_item(
    item_id: uuid.UUID,
    body: ReasonIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_items.cancel_item(session, actor, item_id, body.reason)


# ============================================================================================ plan 15: loans and advances (3a)
@admin_router.get("/loans")
async def list_loans(
    user_id: uuid.UUID | None = None,
    status: Literal["active", "closed", "cancelled"] | None = None,
    kind: Literal["loan", "advance"] | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    out = await payroll_loans.list_loans(session, user_id=user_id, status=status, kind=kind, page=page)
    await pc.audit_read(session, actor, "payroll.loan.list", "payroll_loan", count=len(out["items"]))
    return out


@admin_router.post("/loans", status_code=201)
async def create_loan(
    body: LoanCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_loans.create_loan(session, redis, actor, body)


@admin_router.get("/loans/{loan_id}")
async def get_loan(
    loan_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_loans.get_loan(session, actor, loan_id)


@admin_router.post("/loans/{loan_id}/skip")
async def skip_installment(
    loan_id: uuid.UUID,
    body: LoanSkip,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_loans.skip(session, actor, loan_id, body)


@admin_router.post("/loans/{loan_id}/reschedule")
async def reschedule_loan(
    loan_id: uuid.UUID,
    body: LoanReschedule,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_loans.reschedule(session, redis, actor, loan_id, body)


@admin_router.post("/loans/{loan_id}/foreclose")
async def foreclose_loan(
    loan_id: uuid.UUID,
    body: LoanForeclose,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_loans.foreclose(session, redis, actor, loan_id, body)


@admin_router.post("/loans/{loan_id}/cancel")
async def cancel_loan(
    loan_id: uuid.UUID,
    body: LoanCancel,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_loans.cancel(session, redis, actor, loan_id, body)


@me_router.get("/loans")
async def my_loans(
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Depends(VIEW_OWN)
):
    await pc.limit(redis, actor, "own_list", (60, 60))
    return await payroll_loans.my_loans(session, actor)


@me_router.get("/loans/{loan_id}")
async def my_loan(
    loan_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await pc.limit(redis, actor, "own_detail", (30, 60), (200, 86400))
    return await payroll_loans.my_loan(session, actor, loan_id)


# ============================================================================================ plan 15: auto arrears (3a)
@admin_router.get("/arrears")
async def list_arrears(
    status: Literal["proposed", "pending", "processing", "paid", "dismissed"] | None = None,
    user_id: uuid.UUID | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    out = await payroll_arrears.list_arrears(session, status=status, user_id=user_id, page=page)
    await pc.audit_read(session, actor, "payroll.arrears.list", "payroll_arrears", count=len(out["items"]))
    return out


@admin_router.get("/arrears/{arrears_id}")
async def get_arrears(
    arrears_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_arrears.get_arrears(session, actor, arrears_id)


@admin_router.post("/employees/{user_id}/arrears/recalculate")
async def recalculate_arrears(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_arrears.recalculate(session, actor, user_id)


@admin_router.post("/arrears/{arrears_id}/accept")
async def accept_arrears(
    arrears_id: uuid.UUID,
    body: ArrearsAccept,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_arrears.accept(session, redis, actor, arrears_id, body)


@admin_router.post("/arrears/{arrears_id}/dismiss")
async def dismiss_arrears(
    arrears_id: uuid.UUID,
    body: ReasonIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_arrears.dismiss(session, actor, arrears_id, body.reason)


# ============================================================================================ plan 15: leave encashment (3a)
@admin_router.get("/encashments")
async def list_encashments(
    user_id: uuid.UUID | None = None,
    year: int | None = Query(None, ge=2000, le=2200),
    status: Literal["pending", "processing", "paid", "cancelled"] | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    out = await payroll_encashment.list_encashments(session, user_id=user_id, year=year, status=status, page=page)
    await pc.audit_read(session, actor, "payroll.encashment.list", "leave_encashment", count=len(out["items"]))
    return out


@admin_router.get("/employees/{user_id}/encashment-quote")
async def encashment_quote(
    user_id: uuid.UUID,
    leave_type_id: uuid.UUID,
    year: int = Query(..., ge=2000, le=2200),
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    days: Decimal | None = Query(None, gt=0, le=Decimal("999.9"), max_digits=4, decimal_places=1),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    out = await payroll_encashment.quote(session, user_id, leave_type_id, year, days, period)
    await pc.audit_read(session, actor, "payroll.encashment.quote", "user", count=1, entity_id=user_id)
    return out


@admin_router.post("/encashments", status_code=201)
async def create_encashment(
    body: EncashmentCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_encashment.create(session, redis, actor, body)


@admin_router.post("/encashments/{enc_id}/cancel")
async def cancel_encashment(
    enc_id: uuid.UUID,
    body: OptionalReasonIn | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_encashment.cancel(session, actor, enc_id, body.reason if body else None)


# ============================================================================================ plan 15: overtime (3b)
@admin_router.post("/overtime/compute")
async def compute_overtime(
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_overtime.compute(session, actor, period)


@admin_router.get("/overtime")
async def list_overtime(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    user_id: uuid.UUID | None = None,
    status: Literal["computed", "approved", "rejected", "processing", "paid"] | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_overtime.list_days(session, actor, period=period, user_id=user_id, status=status, page=page)


@admin_router.post("/overtime/bulk")
async def bulk_overtime(
    body: OvertimeBulk,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_overtime.bulk(session, actor, body)


@admin_router.put("/overtime/{day_id}")
async def update_overtime(
    day_id: uuid.UUID,
    body: OvertimeUpdate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_overtime.update_day(session, actor, day_id, body)


@me_router.get("/overtime")
async def my_overtime(
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await pc.limit(redis, actor, "own_list", (60, 60))
    return await payroll_overtime.my_overtime(session, actor, period)


# ============================================================================================ plan 16 4a: arrears bulk accept
@admin_router.post("/arrears/bulk-accept")
async def bulk_accept_arrears(
    body: ArrearsBulkAccept,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_arrears.bulk_accept(session, redis, actor, body)


# ============================================================================================ plan 16 4a: increment batches (bulk hike)
# static paths first: `/increments/batches` must not be read as `/increments/{increment_id}`
@admin_router.post("/increments/bulk", status_code=201)
async def create_increment_batch(
    body: BulkIncrementCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_increments.create_batch(session, actor, body)


@admin_router.get("/increments/batches")
async def list_increment_batches(
    kind: Literal["percent", "import"] | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_increments.list_batches(session, kind, page)


@admin_router.get("/increments/batches/{batch_id}")
async def get_increment_batch(
    batch_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_increments.get_batch(session, batch_id)


@admin_router.post("/increments/batches/{batch_id}/submit")
async def submit_increment_batch(
    batch_id: uuid.UUID,
    body: EmptyIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_increments.submit_batch(session, redis, actor, batch_id)


@admin_router.post("/increments/batches/{batch_id}/approve")
async def approve_increment_batch(
    batch_id: uuid.UUID,
    body: BatchApprove,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_increments.approve_batch(session, redis, actor, batch_id, body)


@admin_router.post("/increments/batches/{batch_id}/reject")
async def reject_increment_batch(
    batch_id: uuid.UUID,
    body: BatchReject,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_increments.reject_batch(session, actor, batch_id, body)


@admin_router.post("/increments/batches/{batch_id}/cancel")
async def cancel_increment_batch(
    batch_id: uuid.UUID,
    body: ReasonIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_increments.cancel_batch(session, actor, batch_id, body.reason)


# ============================================================================================ plan 16 4a: increments
@admin_router.get("/increments")
async def list_increments(
    status: Literal["draft", "submitted", "approved", "rejected", "cancelled"] | None = None,
    user_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    effective_from: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_increments.list_increments(
        session, actor, status=status, user_id=user_id, batch_id=batch_id, effective_from=effective_from, page=page
    )


@admin_router.get("/increments/{increment_id}")
async def get_increment(
    increment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_increments.get_increment(session, actor, increment_id)


@admin_router.post("/employees/{user_id}/increments", status_code=201)
async def create_increment(
    user_id: uuid.UUID,
    body: IncrementCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_increments.create_increment(session, actor, user_id, body)


@admin_router.put("/increments/{increment_id}")
async def update_increment(
    increment_id: uuid.UUID,
    body: IncrementUpdate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_increments.update_increment(session, actor, increment_id, body)


@admin_router.post("/increments/{increment_id}/submit")
async def submit_increment(
    increment_id: uuid.UUID,
    body: VersionIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_increments.submit_increment(session, redis, actor, increment_id, body)


@admin_router.post("/increments/{increment_id}/withdraw")
async def withdraw_increment(
    increment_id: uuid.UUID,
    body: VersionIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_increments.withdraw_increment(session, actor, increment_id, body)


@admin_router.post("/increments/{increment_id}/approve")
async def approve_increment(
    increment_id: uuid.UUID,
    body: IncrementApprove,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_increments.approve_increment(session, redis, actor, increment_id, body)


@admin_router.post("/increments/{increment_id}/reject")
async def reject_increment(
    increment_id: uuid.UUID,
    body: IncrementReject,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_increments.reject_increment(session, actor, increment_id, body)


@admin_router.post("/increments/{increment_id}/cancel")
async def cancel_increment(
    increment_id: uuid.UUID,
    body: ReasonIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_increments.cancel_increment(session, actor, increment_id, body.reason)


@admin_router.get("/employees/{user_id}/salary-history")
async def salary_history(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_increments.salary_history(session, actor, user_id)


# ============================================================================================ plan 16 4a step 2: structure import, CTC, gratuity
@admin_router.get("/structures/template")
async def structures_template(redis: Redis = Depends(get_redis), actor: Principal = Depends(MANAGE)):
    await pc.limit(redis, actor, "import", (10, 60), (50, 86400), closed=True)
    built = await payroll_structure_import.template()
    return StreamingResponse(
        xlsx.iter_file(built),
        media_type=xlsx.XLSX_MIME,
        headers={**_NOSTORE, "Content-Disposition": 'attachment; filename="salary-structures-template.xlsx"'},
        background=BackgroundTask(built.close),
    )


@admin_router.post("/structures/import")
async def import_structures(
    response: Response,
    file: UploadFile = File(...),
    file_sha256: str | None = Form(None, max_length=64),
    reason: str | None = Form(None, max_length=400),
    current_password: SecretStr | None = Form(None, max_length=128),
    dry_run: bool = Query(True),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    """Salary-structure xlsx import (contract 18.6). `dry_run=true` (default) validates and reports; `dry_run=false` re-sends
    the same file with its `file_sha256`, a reason and the caller's own password and imports everything or nothing."""
    if dry_run:
        await pc.limit(redis, actor, "import", (10, 60), (50, 86400), closed=True)
    else:
        await pc.limit(redis, actor, "action", (10, 60), closed=True)
    data = await xlsx_import.read_upload(file)
    out = await payroll_structure_import.import_structures(
        session, redis, actor, data=data, dry_run=dry_run, file_sha256=file_sha256, reason=reason, password=current_password
    )
    response.headers["Cache-Control"] = "no-store"
    if not dry_run:
        response.status_code = 201
    return out


@admin_router.post("/ctc/calculate")
async def ctc_calculate(
    body: CtcCalculateIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "calc", (60, 60), closed=True)
    return await payroll_ctc_gratuity.calculate(session, actor, body)


@admin_router.get("/gratuity")
async def gratuity(
    as_of: str | None = Query(None, max_length=10),
    department_id: uuid.UUID | None = None,
    eligible_only: bool = False,
    q: str | None = Query(None, max_length=100),
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_ctc_gratuity.gratuity_report(
        session, actor, raw_as_of=as_of, department_id=department_id, eligible_only=eligible_only, q=q, page=page
    )


# ============================================================================================ plan 16 4b: full and final settlement
@admin_router.get("/fnf")
async def list_fnf(
    status: Literal["draft", "submitted", "approved", "settled", "cancelled"] | None = None,
    q: str | None = Query(None, max_length=100),
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_fnf.list_fnf(session, actor, status=status, q=q, page=page)


@admin_router.post("/employees/{user_id}/fnf", status_code=201)
async def create_fnf(
    user_id: uuid.UUID,
    body: FnfCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_fnf.create_fnf(session, actor, user_id, body)


@admin_router.get("/fnf/{fnf_id}")
async def get_fnf(
    fnf_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_fnf.get_fnf(session, actor, fnf_id)


@admin_router.put("/fnf/{fnf_id}")
async def update_fnf(
    fnf_id: uuid.UUID,
    body: FnfUpdate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_fnf.update_fnf(session, actor, fnf_id, body)


@admin_router.get("/fnf/{fnf_id}/preview")
async def preview_fnf(
    fnf_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await payroll_fnf.preview(session, actor, fnf_id)


@admin_router.post("/fnf/{fnf_id}/submit")
async def submit_fnf(
    fnf_id: uuid.UUID,
    body: VersionIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_fnf.submit_fnf(session, redis, actor, fnf_id, body)


@admin_router.post("/fnf/{fnf_id}/withdraw")
async def withdraw_fnf(
    fnf_id: uuid.UUID,
    body: VersionIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_fnf.withdraw_fnf(session, actor, fnf_id, body)


@admin_router.post("/fnf/{fnf_id}/approve")
async def approve_fnf(
    fnf_id: uuid.UUID,
    body: FnfApprove,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_fnf.approve_fnf(session, redis, actor, fnf_id, body)


@admin_router.post("/fnf/{fnf_id}/reject")
async def reject_fnf(
    fnf_id: uuid.UUID,
    body: FnfReject,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_fnf.reject_fnf(session, actor, fnf_id, body)


@admin_router.post("/fnf/{fnf_id}/revoke")
async def revoke_fnf(
    fnf_id: uuid.UUID,
    body: FnfRevoke,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await pc.limit(redis, actor, "action", (10, 60), closed=True)
    return await payroll_fnf.revoke_fnf(session, redis, actor, fnf_id, body)


@admin_router.post("/fnf/{fnf_id}/cancel")
async def cancel_fnf(
    fnf_id: uuid.UUID,
    body: ReasonIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "write", (60, 60), closed=True)
    return await payroll_fnf.cancel_fnf(session, actor, fnf_id, body.reason)


@admin_router.get("/fnf/{fnf_id}/statement")
async def fnf_statement(
    fnf_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "report", (10, 60), closed=True)
    f = await payroll_fnf.out.or_404(session, fnf_id)
    data, name = await fnf_pdf.build(session, f)
    audit(session, action="payroll.fnf.statement", actor=actor, entity_type=payroll_fnf.ENTITY, entity_id=f.id)
    await pc.commit(session)
    return _pdf_response(data, name)


@me_router.get("/fnf")
async def my_fnf(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await pc.limit(redis, actor, "own_list", (60, 60))
    return await payroll_fnf.my_fnf(session, actor)


@me_router.get("/fnf/statement")
async def my_fnf_statement(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await pc.limit(redis, actor, "own_detail", (30, 60), (200, 86400), closed=True)
    f = await payroll_fnf.my_settled_or_404(session, actor)
    data, name = await fnf_pdf.build(session, f)
    audit(session, action="payroll.fnf.view_own", actor=actor, entity_type=payroll_fnf.ENTITY, entity_id=f.id)
    await pc.commit(session)
    return _pdf_response(data, name)
