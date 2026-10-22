from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.config import get_settings
from ..core.db import get_session
from ..core.errors import AppError
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..services import files as svc

router = APIRouter(tags=["files"])


@router.post("/files", status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    purpose: str = Form(..., max_length=32),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission()),
):
    await rate_limit.enforce(redis, [(f"rl:upload:{actor.id}", get_settings().upload_rate_per_min, 60)], fail_closed=True)
    if purpose not in svc.GENERIC_PURPOSES:  # profile_photo only via POST /me/profile/photo (512 px cap, replaces the old one)
        raise AppError("VALIDATION_ERROR", "Unknown purpose", details=[{"field": "purpose", "message": "unsupported value"}])
    return await svc.upload(session, actor, purpose, file)


@router.get("/files/{file_id}")
async def get_file(
    file_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission()),
):
    f, data = await svc.read(session, actor, file_id)
    disposition = "inline" if f.content_type.startswith("image/") else "attachment"
    return Response(
        content=data,
        media_type=f.content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{f.id}.{svc.EXT[f.content_type]}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )
