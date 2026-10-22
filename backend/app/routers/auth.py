from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_session
from ..core.errors import AppError
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.auth import (
    ChangePasswordRequest,
    DeviceTokenRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    RefreshRequest,
    TokenResponse,
)
from ..services import admin_security
from ..services import auth as svc
from ..services import company as company_svc

router = APIRouter(prefix="/auth", tags=["auth"])
public_router = APIRouter(tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis)):
    return await svc.login(
        session,
        redis,
        employee_id=body.employee_id,
        password=body.password,
        device_id=body.device_id,
        device_name=body.device_name,
        app_version=body.app_version,
        platform=body.platform,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, session: AsyncSession = Depends(get_session)):
    return await svc.refresh(session, refresh_token=body.refresh_token)


@router.post("/logout", status_code=204)
async def logout(
    body: RefreshRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission(allow_password_change=True)),
):
    await svc.logout(session, principal, refresh_token=body.refresh_token)
    return Response(status_code=204)


@router.post("/logout-all", status_code=204)
async def logout_all(session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_permission())):
    await svc.logout_all(session, principal)
    return Response(status_code=204)


@router.post("/change-password", response_model=LoginResponse)
async def change_password(
    body: ChangePasswordRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(require_permission(allow_password_change=True)),
):
    if principal.sid:  # a console access token must not mint a mobile refresh token (review L10)
        raise AppError("FORBIDDEN", "Console sessions change the password through the console")
    return await svc.change_password(
        session, principal, current_password=body.current_password, new_password=body.new_password, redis=redis
    )


@router.post("/forgot-password", status_code=202)
async def forgot_password(
    body: ForgotPasswordRequest,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    requester = await svc.forgot_password(session, redis, employee_id=body.employee_id)
    if requester is not None:  # push after the response: its latency must not reveal that the id exists (review L2)
        background.add_task(admin_security.notify_reset_request, session, redis, requester)
    return {"message": "If the account exists, a request was sent."}


@router.get("/me", response_model=MeResponse)
async def me(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_permission(allow_password_change=True)),
):
    u = principal.user
    return {
        "company": await company_svc.public_block(session),
        **svc.auth_user_payload(u),
        "must_change_password": u.must_change_password,
        "department": {"id": str(u.department.id), "name": u.department.name} if u.department else None,
    }


@public_router.get("/config")
async def client_config(session: AsyncSession = Depends(get_session), _: Principal = Depends(require_permission())):
    return await svc.client_config(session)


@public_router.post("/me/device-tokens", status_code=204)
async def register_device_token(
    body: DeviceTokenRequest, session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_permission())
):
    await svc.register_device_token(
        session, principal, fcm_token=body.fcm_token, platform=body.platform, device_id=body.device_id
    )
    return Response(status_code=204)


@public_router.delete("/me/device-tokens/{fcm_token}", status_code=204)
async def delete_device_token(
    fcm_token: str, session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_permission())
):
    await svc.delete_device_token(session, principal, fcm_token=fcm_token)
    return Response(status_code=204)
