"""Payroll controls (contract section 19, work package 5a): publish requests, payslip delivery, company bank accounts, payment batches.
Every endpoint declares its permission; the services enforce the self rule, the maker-checker rules and the step-up."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.payroll_controls import (
    BankAccountCreate,
    BankAccountUpdate,
    DeliveryRemind,
    PaymentBatchApprove,
    PaymentBatchCancel,
    PaymentBatchCreate,
    PaymentBatchReject,
    PaymentBatchUpdate,
    PaymentConfirm,
    PaymentFileIn,
    PublishRequestApprove,
    PublishRequestCreate,
    PublishRequestReject,
    VersionIn,
)
from ..services import (
    payroll_bank_accounts,
    payroll_delivery,
    payroll_payments,
    payroll_publish_requests,
)
from ..services import payroll_common as pc

admin_router = APIRouter(prefix="/admin/payroll", tags=["payroll-controls"])

VIEW_ALL = require_permission("payroll.view_all")
MANAGE = require_permission("payroll.manage")
APPROVE = require_permission("payroll.approve")
PUBLISH = require_permission("payroll.publish")

CSV_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


async def _read(redis: Redis, actor: Principal) -> None:
    await pc.limit(redis, actor, "admin_read", (120, 60))


async def _write(redis: Redis, actor: Principal) -> None:
    await pc.limit(redis, actor, "write", (60, 60), closed=True)


async def _action(redis: Redis, actor: Principal) -> None:
    await pc.limit(redis, actor, "action", (10, 60), closed=True)


# ============================================================================================ 19.2 publish requests
@admin_router.get("/runs/{run_id}/publish-check")
async def run_publish_check(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await payroll_publish_requests.publish_check(session, actor, run_id=run_id)


@admin_router.get("/payslips/{payslip_id}/publish-check")
async def payslip_publish_check(
    payslip_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await payroll_publish_requests.publish_check(session, actor, payslip_id=payslip_id)


@admin_router.post("/runs/{run_id}/publish-requests", status_code=201)
async def create_run_publish_request(
    run_id: uuid.UUID,
    body: PublishRequestCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_publish_requests.create(session, redis, actor, run_id=run_id, payslip_id=None, body=body)


@admin_router.post("/payslips/{payslip_id}/publish-requests", status_code=201)
async def create_payslip_publish_request(
    payslip_id: uuid.UUID,
    body: PublishRequestCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_publish_requests.create(session, redis, actor, run_id=None, payslip_id=payslip_id, body=body)


@admin_router.get("/publish-requests")
async def list_publish_requests(
    status: Literal["submitted", "approved", "rejected", "withdrawn", "stale"] | None = None,
    run_id: uuid.UUID | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await payroll_publish_requests.list_requests(session, actor, status=status, run_id=run_id, page=page)


@admin_router.get("/publish-requests/{request_id}")
async def get_publish_request(
    request_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await payroll_publish_requests.get_request(session, actor, request_id)


@admin_router.post("/publish-requests/{request_id}/withdraw")
async def withdraw_publish_request(
    request_id: uuid.UUID,
    body: VersionIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_publish_requests.withdraw(session, actor, request_id, body)


@admin_router.post("/publish-requests/{request_id}/approve")
async def approve_publish_request(
    request_id: uuid.UUID,
    body: PublishRequestApprove,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(PUBLISH),
):
    await _action(redis, actor)
    return await payroll_publish_requests.approve(session, redis, actor, request_id, body)


@admin_router.post("/publish-requests/{request_id}/reject")
async def reject_publish_request(
    request_id: uuid.UUID,
    body: PublishRequestReject,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(PUBLISH),
):
    await _action(redis, actor)
    return await payroll_publish_requests.reject(session, actor, request_id, body)


# ============================================================================================ 19.3 delivery
@admin_router.get("/runs/{run_id}/delivery")
async def delivery_report(
    run_id: uuid.UUID,
    state: Literal["viewed", "not_viewed", "no_device"] | None = None,
    q: str | None = Query(None, max_length=100),
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await payroll_delivery.report(session, actor, run_id, state=state, q=q, page=page)


@admin_router.post("/runs/{run_id}/delivery/remind")
async def delivery_remind(
    run_id: uuid.UUID,
    body: DeliveryRemind,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _action(redis, actor)
    return await payroll_delivery.remind(session, actor, run_id, body)


# ============================================================================================ 19.4 bank accounts
@admin_router.get("/bank-accounts")
async def list_bank_accounts(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await payroll_bank_accounts.list_accounts(session, actor)


@admin_router.post("/bank-accounts", status_code=201)
async def create_bank_account(
    body: BankAccountCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_bank_accounts.create_account(session, redis, actor, body)


@admin_router.put("/bank-accounts/{account_id}")
async def update_bank_account(
    account_id: uuid.UUID,
    body: BankAccountUpdate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_bank_accounts.update_account(session, actor, account_id, body)


# ============================================================================================ 19.5 payment batches
@admin_router.post("/runs/{run_id}/payment-batches", status_code=201)
async def create_payment_batch(
    run_id: uuid.UUID,
    body: PaymentBatchCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_payments.create_batch(session, actor, run_id, body)


@admin_router.get("/payment-batches")
async def list_payment_batches(
    run_id: uuid.UUID | None = None,
    status: Literal["draft", "submitted", "approved", "exported", "closed", "cancelled"] | None = None,
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await payroll_payments.list_batches(session, actor, run_id=run_id, status=status, page=page)


@admin_router.get("/payment-batches/{batch_id}")
async def get_payment_batch(
    batch_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VIEW_ALL),
):
    await _read(redis, actor)
    return await payroll_payments.get_batch(session, actor, batch_id)


@admin_router.put("/payment-batches/{batch_id}")
async def update_payment_batch(
    batch_id: uuid.UUID,
    body: PaymentBatchUpdate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_payments.update_batch(session, actor, batch_id, body)


@admin_router.post("/payment-batches/{batch_id}/submit")
async def submit_payment_batch(
    batch_id: uuid.UUID,
    body: VersionIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_payments.submit_batch(session, redis, actor, batch_id, body)


@admin_router.post("/payment-batches/{batch_id}/withdraw")
async def withdraw_payment_batch(
    batch_id: uuid.UUID,
    body: VersionIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_payments.withdraw_batch(session, actor, batch_id, body)


@admin_router.post("/payment-batches/{batch_id}/approve")
async def approve_payment_batch(
    batch_id: uuid.UUID,
    body: PaymentBatchApprove,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await _action(redis, actor)
    return await payroll_payments.approve_batch(session, redis, actor, batch_id, body)


@admin_router.post("/payment-batches/{batch_id}/reject")
async def reject_payment_batch(
    batch_id: uuid.UUID,
    body: PaymentBatchReject,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(APPROVE),
):
    await _action(redis, actor)
    return await payroll_payments.reject_batch(session, actor, batch_id, body)


@admin_router.post("/payment-batches/{batch_id}/cancel")
async def cancel_payment_batch(
    batch_id: uuid.UUID,
    body: PaymentBatchCancel,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_payments.cancel_batch(session, actor, batch_id, body.reason)


@admin_router.post("/payment-batches/{batch_id}/file")
async def payment_batch_file(
    batch_id: uuid.UUID,
    body: PaymentFileIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "bank_file", (10, 60), (30, 86400), closed=True)
    data, name = await payroll_payments.download_file(session, redis, actor, batch_id, body)
    return Response(
        content=data, media_type="text/csv; charset=us-ascii",
        headers={**CSV_HEADERS, "Content-Disposition": f'attachment; filename="{name}"'},
    )  # fmt: skip


@admin_router.post("/payment-batches/{batch_id}/confirm")
async def confirm_payment_batch(
    batch_id: uuid.UUID,
    body: PaymentConfirm,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await _write(redis, actor)
    return await payroll_payments.confirm_batch(session, redis, actor, batch_id, body)
