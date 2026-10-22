"""Company branding: public reads (login screen, server setup) and admin management (plan 11 s.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import net, rate_limit
from ..core.config import get_settings
from ..core.context import get_request_context
from ..core.db import get_session
from ..core.errors import AppError
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..services import company as svc

public_router = APIRouter(prefix="/public", tags=["public"])
router = APIRouter(prefix="/admin/company", tags=["admin"])


class CompanyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)


async def _public_limit(redis: Redis, bucket: str) -> None:
    ip = net.rate_limit_key(get_request_context().ip) or "unknown"
    await rate_limit.enforce(redis, [(f"rl:public:{bucket}:{ip}", 60, 60)])  # 60/min/IP, fails open like login


@public_router.get("/company")
async def public_company(session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis)):
    await _public_limit(redis, "company")
    return await svc.public_info(session)


@public_router.get("/company/logo")
async def public_logo(
    request: Request, v: int | None = None, session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis)
):
    await _public_limit(redis, "logo")
    found = await svc.logo_bytes(session)
    if found is None:
        raise AppError("NOT_FOUND", "No logo")
    data, sha = found
    etag = f'"{sha}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=86400",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    if request.headers.get("if-none-match", "").strip() == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="image/png", headers=headers)


@router.get("")
async def get_company(session: AsyncSession = Depends(get_session), _: Principal = Depends(require_permission("company.manage"))):
    return await svc.admin_out(session)


@router.put("")
async def update_company(
    body: CompanyUpdate,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("company.manage")),
):
    return await svc.update_name(session, actor, body.name)


@router.post("/logo")
async def upload_logo(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("company.manage")),
):
    await rate_limit.enforce(redis, [(f"rl:upload:{actor.id}", get_settings().upload_rate_per_min, 60)], fail_closed=True)
    return await svc.set_logo(session, actor, file)


@router.delete("/logo", status_code=204)
async def delete_logo(
    session: AsyncSession = Depends(get_session), actor: Principal = Depends(require_permission("company.manage"))
):
    await svc.delete_logo(session, actor)
    return Response(status_code=204)
