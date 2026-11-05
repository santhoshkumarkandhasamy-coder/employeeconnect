"""Reimbursement claims (contract 17.6). Employee routes need `reimbursement.submit` and only ever see their own claims; the admin routes
split by stage: the reporting manager (verify / reject, direct reports only) and HR (`payroll.manage`: approve, payout, reject)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.payroll import ClaimApprove, ClaimCreate, ClaimPayout, ClaimReject, ClaimVerify
from ..services import payroll_common as pc
from ..services import payroll_reimbursements as claims

router = APIRouter(prefix="/reimbursements", tags=["reimbursements"])
admin_router = APIRouter(prefix="/admin/reimbursements", tags=["reimbursements-admin"])

SUBMIT = require_permission("reimbursement.submit")
STAGE_READ = require_permission("request.view_team", "request.approve_team", "request.approve_all", "payroll.view_all")
VERIFY = require_permission("request.approve_team", "request.approve_all")
REJECT = require_permission("request.approve_team", "request.approve_all", "payroll.manage")
MANAGE = require_permission("payroll.manage")

ClaimStatus = Literal["submitted", "verified", "approved", "processing", "paid", "rejected", "cancelled"]


# ============================================================================================ employee
@router.get("/categories")
async def categories(
    session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis), actor: Principal = Depends(SUBMIT)
):
    await pc.limit(redis, actor, "own_list", (60, 60))
    return await claims.categories_out(session)


@router.post("", status_code=201)
async def submit_claim(
    body: ClaimCreate,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(SUBMIT),
):
    await pc.limit(redis, actor, "claims:write", (10, 60), (30, 86400), closed=True)
    return await claims.submit(session, redis, actor, body)


@router.get("")
async def my_claims(
    status: ClaimStatus | None = None,
    year: int | None = Query(None, ge=2000, le=2200),
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(SUBMIT),
):
    await pc.limit(redis, actor, "own_list", (60, 60))
    return await claims.list_own(session, actor, status, year, page)


@router.get("/{claim_id}")
async def my_claim(
    claim_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(SUBMIT),
):
    await pc.limit(redis, actor, "own_detail", (30, 60), (200, 86400))
    return await claims.get_own(session, actor, claim_id)


@router.post("/{claim_id}/cancel")
async def cancel_claim(
    claim_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(SUBMIT),
):
    await pc.limit(redis, actor, "claims:write", (10, 60), (30, 86400), closed=True)
    return await claims.cancel(session, actor, claim_id)


# ============================================================================================ manager / HR
@admin_router.get("")
async def list_claims(
    stage: Literal["manager", "hr", "all"] | None = None,
    status: ClaimStatus | None = None,
    employee_id: str | None = Query(None, min_length=1, max_length=32),
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
    category: str | None = Query(None, pattern=r"^[a-z][a-z0-9_]{1,19}$"),
    page: PageParams = Depends(),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(STAGE_READ),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    stage = stage or ("all" if actor.has("payroll.view_all") else "manager")
    return await claims.list_admin(
        session, actor, stage=stage, status=status, employee_id=employee_id, period=period, category=category, page=page
    )


@admin_router.get("/{claim_id}")
async def get_claim(
    claim_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(STAGE_READ),
):
    await pc.limit(redis, actor, "admin_read", (120, 60))
    return await claims.get_admin(session, actor, claim_id)


@admin_router.put("/{claim_id}/verify")
async def verify_claim(
    claim_id: uuid.UUID,
    body: ClaimVerify | None = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(VERIFY),
):
    await pc.limit(redis, actor, "claims:decide", (60, 60), closed=True)
    return await claims.verify(session, redis, actor, claim_id, body or ClaimVerify())


@admin_router.put("/{claim_id}/approve")
async def approve_claim(
    claim_id: uuid.UUID,
    body: ClaimApprove,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "claims:decide", (60, 60), closed=True)
    return await claims.approve(session, actor, claim_id, body)


@admin_router.put("/{claim_id}/reject")
async def reject_claim(
    claim_id: uuid.UUID,
    body: ClaimReject,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(REJECT),
):
    await pc.limit(redis, actor, "claims:decide", (60, 60), closed=True)
    return await claims.reject(session, actor, claim_id, body)


@admin_router.put("/{claim_id}/payout")
async def payout_claim(
    claim_id: uuid.UUID,
    body: ClaimPayout,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(MANAGE),
):
    await pc.limit(redis, actor, "claims:decide", (60, 60), closed=True)
    return await claims.payout(session, actor, claim_id, body)
