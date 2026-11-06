"""Tax rules, investment declarations / proofs and Form 16 (contract 19.6 to 19.8, work package 5b). Every endpoint declares its
permission; the services enforce the self rule, the maker-checker rules, the windows and the step-up."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from ..core.db import get_session
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.payroll import EmptyIn
from ..schemas.payroll_tax import (
    DeclarationSave,
    DeclarationSaveOnBehalf,
    DeclarationsReportIn,
    DeclarationSubmit,
    DeclarationSubmitOnBehalf,
    DeclarationUnlock,
    Form16Publish,
    Form16Withdraw,
    ProofCreate,
    ProofReject,
    ProofVerify,
    RulesReject,
    RulesUpdate,
    RulesVerify,
    RulesVersionIn,
    WindowsIn,
)
from ..services import form16, tax_declarations, tax_rules, xlsx
from ..services import payroll_common as pc

admin_router = APIRouter(prefix="/admin/payroll/tax", tags=["payroll-tax"])
me_router = APIRouter(prefix="/me/tax", tags=["payroll-tax-own"])

VIEW_ALL = require_permission("payroll.view_all")
VIEW_OWN = require_permission("payroll.view_own")
MANAGE = require_permission("payroll.manage")
APPROVE = require_permission("payroll.approve")

FY_Q = r"^\d{4}-\d{2}$"
PDF_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}


async def _read(redis: Redis, actor: Principal) -> None:
    await pc.limit(redis, actor, "admin_read", (120, 60))


async def _write(redis: Redis, actor: Principal) -> None:
    await pc.limit(redis, actor, "write", (60, 60), closed=True)


async def _action(redis: Redis, actor: Principal) -> None:
    await pc.limit(redis, actor, "action", (10, 60), closed=True)


async def _own_read(redis: Redis, actor: Principal) -> None:
    await pc.limit(redis, actor, "own_list", (60, 60))


async def _own_write(redis: Redis, actor: Principal) -> None:
    await pc.limit(redis, actor, "tax_write", (30, 60), (200, 86400), closed=True)


def _pdf(data: bytes, name: str) -> Response:
    return Response(
        content=data,
        media_type="application/pdf",
        headers={**PDF_HEADERS, "Content-Disposition": f'attachment; filename="{name}"'},
    )


# ============================================================================================ 19.6 tax year rules
@admin_router.get("/years")
async def list_years(
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Depends(VIEW_ALL)
):
    await _read(redis, actor)
    return await tax_rules.list_years(session)


@admin_router.get("/years/{fy}/rules")
async def list_rules(
    fy: str, session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Depends(VIEW_ALL)
):
    await _read(redis, actor)
    return await tax_rules.list_versions(session, fy)


@admin_router.post("/years/{fy}/rules", status_code=201)
async def create_rules(
    fy: str,
    body: EmptyIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await tax_rules.create_draft(session, actor, fy)


@admin_router.put("/years/{fy}/windows")
async def set_windows(
    fy: str,
    body: WindowsIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await tax_rules.set_windows(session, actor, fy, body)


@admin_router.get("/rules/{rules_id}")
async def get_rules(
    rules_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await tax_rules.get_rules(session, rules_id)


@admin_router.put("/rules/{rules_id}")
async def update_rules(
    rules_id: uuid.UUID,
    body: RulesUpdate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await tax_rules.update_rules(session, actor, rules_id, body)


@admin_router.post("/rules/{rules_id}/submit")
async def submit_rules(
    rules_id: uuid.UUID,
    body: RulesVersionIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await tax_rules.submit(session, actor, rules_id, body)


@admin_router.post("/rules/{rules_id}/withdraw")
async def withdraw_rules(
    rules_id: uuid.UUID,
    body: RulesVersionIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await tax_rules.withdraw(session, actor, rules_id, body)


@admin_router.post("/rules/{rules_id}/verify")
async def verify_rules(
    rules_id: uuid.UUID,
    body: RulesVerify,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await _action(redis, actor)
    return await tax_rules.verify(session, redis, actor, rules_id, body)


@admin_router.post("/rules/{rules_id}/reject")
async def reject_rules(
    rules_id: uuid.UUID,
    body: RulesReject,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await _action(redis, actor)
    return await tax_rules.reject(session, actor, rules_id, body)


# ============================================================================================ 19.7 declarations and proofs (admin)
@admin_router.get("/declarations")
async def list_declarations(
    fy: str | None = Query(None, pattern=FY_Q),
    status: Literal["none", "draft", "submitted"] | None = None,
    regime: Literal["old", "new"] | None = None,
    q: str | None = Query(None, max_length=60),
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await tax_declarations.list_declarations(session, actor, fy=fy, status=status, regime=regime, q=q, page=page)


@admin_router.get("/declarations/{user_id}")
async def get_declaration(
    user_id: uuid.UUID,
    fy: str | None = Query(None, pattern=FY_Q),
    reveal: bool = False,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    if reveal:
        await pc.limit(redis, actor, "reveal", (30, 60), (200, 86400), closed=True)
    return await tax_declarations.get_admin(session, actor, user_id, fy, reveal)


@admin_router.put("/declarations/{user_id}")
async def save_declaration_on_behalf(
    user_id: uuid.UUID,
    body: DeclarationSaveOnBehalf,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await tax_declarations.save_on_behalf(session, redis, actor, user_id, body, body.current_password)


@admin_router.post("/declarations/{user_id}/submit")
async def submit_declaration_on_behalf(
    user_id: uuid.UUID,
    body: DeclarationSubmitOnBehalf,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await tax_declarations.submit_on_behalf(session, redis, actor, user_id, body, body.current_password)


@admin_router.post("/declarations/{user_id}/unlock")
async def unlock_declaration(
    user_id: uuid.UUID,
    body: DeclarationUnlock,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await tax_declarations.unlock(session, actor, user_id, body)


@admin_router.get("/proofs")
async def list_proofs(
    fy: str | None = Query(None, pattern=FY_Q),
    status: Literal["submitted", "verified", "rejected", "cancelled"] | None = None,
    user_id: uuid.UUID | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await tax_declarations.list_proofs(session, actor, fy=fy, status=status, user_id=user_id, page=page)


@admin_router.post("/proofs/{proof_id}/verify")
async def verify_proof(
    proof_id: uuid.UUID,
    body: ProofVerify,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _action(redis, actor)
    return await tax_declarations.verify_proof(session, actor, proof_id, body)


@admin_router.post("/proofs/{proof_id}/reject")
async def reject_proof(
    proof_id: uuid.UUID,
    body: ProofReject,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _action(redis, actor)
    return await tax_declarations.reject_proof(session, actor, proof_id, body)


@admin_router.post("/reports/declarations")
async def declarations_report(
    body: DeclarationsReportIn,
    fy: str | None = Query(None, pattern=FY_Q),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "statutory_file", (10, 60), (30, 86400), closed=True)
    built, name, _rows = await tax_declarations.declarations_xlsx(session, redis, actor, fy, body)
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


# ============================================================================================ 19.8 Form 16 (admin)
@admin_router.post("/form16/upload")
async def form16_upload(
    fy: str = Query(..., pattern=FY_Q),
    kind: Literal["form16", "part_a", "part_b", "form12ba"] = Query(...),
    dry_run: bool = Query(True),
    files: list[UploadFile] = File(...),
    manifest_sha256: str | None = Form(None, max_length=64),
    current_password: str | None = Form(None, max_length=128),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "import", (10, 60), (50, 86400), closed=True)
    status, body = await form16.upload(
        session, redis, actor, fy=fy, kind=kind, dry_run=dry_run, uploads=files, manifest_sha256=manifest_sha256,
        password=SecretStr(current_password) if current_password else None,
    )  # fmt: skip
    return JSONResponse(status_code=status, content=body, headers={"Cache-Control": "no-store"})


@admin_router.get("/form16")
async def list_form16(
    fy: str | None = Query(None, pattern=FY_Q),
    kind: Literal["form16", "part_a", "part_b", "form12ba"] | None = None,
    status: Literal["uploaded", "published", "withdrawn"] | None = None,
    q: str | None = Query(None, max_length=60),
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await form16.list_documents(session, actor, fy=fy, kind=kind, status=status, q=q, page=page)


@admin_router.get("/form16/{doc_id}/file")
async def form16_admin_file(
    doc_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await pc.limit(redis, actor, "own_detail", (30, 60), (200, 86400), closed=True)
    data, name = await form16.admin_file(session, actor, doc_id)
    return _pdf(data, name)


@admin_router.post("/form16/publish")
async def publish_form16(
    body: Form16Publish,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await _action(redis, actor)
    return await form16.publish(session, redis, actor, body)


@admin_router.post("/form16/{doc_id}/withdraw")
async def withdraw_form16(
    doc_id: uuid.UUID,
    body: Form16Withdraw,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await form16.withdraw(session, redis, actor, doc_id, body)


# ============================================================================================ employee
@me_router.get("/years")
async def my_years(
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Depends(VIEW_OWN)
):
    await _own_read(redis, actor)
    return await tax_declarations.years_own(session)


@me_router.get("/declaration")
async def my_declaration(
    fy: str | None = Query(None, pattern=FY_Q),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await _own_read(redis, actor)
    return await tax_declarations.get_own(session, actor, fy)


@me_router.put("/declaration")
async def save_my_declaration(
    body: DeclarationSave,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await _own_write(redis, actor)
    return await tax_declarations.save_own(session, actor, body)


@me_router.post("/declaration/submit")
async def submit_my_declaration(
    body: DeclarationSubmit,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await _own_write(redis, actor)
    return await tax_declarations.submit_own(session, actor, body)


@me_router.post("/proofs", status_code=201)
async def create_my_proof(
    body: ProofCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await _own_write(redis, actor)
    return await tax_declarations.create_proof(session, actor, body)


@me_router.get("/proofs")
async def my_proofs(
    fy: str | None = Query(None, pattern=FY_Q),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await _own_read(redis, actor)
    return await tax_declarations.list_own_proofs(session, actor, fy)


@me_router.post("/proofs/{proof_id}/cancel")
async def cancel_my_proof(
    proof_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await _own_write(redis, actor)
    return await tax_declarations.cancel_proof(session, actor, proof_id)


@me_router.get("/form16")
async def my_form16(
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Depends(VIEW_OWN)
):
    await _own_read(redis, actor)
    return await form16.list_own(session, actor)


@me_router.get("/form16/{doc_id}/file")
async def my_form16_file(
    doc_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_OWN),
):
    await pc.limit(redis, actor, "own_detail", (30, 60), (200, 86400), closed=True)
    data, name = await form16.own_file(session, actor, doc_id)
    return _pdf(data, name)
