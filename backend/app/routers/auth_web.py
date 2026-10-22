"""Admin web console authentication (plan 11 s.2.2). The refresh token lives ONLY in an HttpOnly cookie; the access token is
returned in the body and kept in memory by the console. CSRF: the cookie is sent only to /api/auth/web/*, and these
endpoints require header `X-EC-Client: console` plus `Origin == PUBLIC_ORIGIN`. All other API calls use the bearer header."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Cookie, Depends, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.db import get_session
from ..core.errors import AppError, error_response
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.auth import ChangePasswordRequest, WebLoginRequest
from ..services import auth as svc

COOKIE = "__Secure-ec_rt"
COOKIE_PATH = "/api/auth/web"

router = APIRouter(prefix="/auth/web", tags=["auth-web"])


def csrf_guard(request: Request) -> None:
    s = get_settings()
    origin = request.headers.get("origin", "").strip().rstrip("/").lower()
    if not s.public_origin or request.headers.get("x-ec-client") != "console" or origin != s.public_origin:
        raise AppError("FORBIDDEN", "Request not allowed")


async def _max_age(session: AsyncSession) -> int:
    hours, _ = await svc.console_limits(session)
    return hours * 3600


def _set_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(COOKIE, token, max_age=max_age, path=COOKIE_PATH, secure=True, httponly=True, samesite="strict")


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path=COOKIE_PATH, secure=True, httponly=True, samesite="strict")


def _body(tokens: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in tokens.items() if k != "refresh_token"}  # the refresh token is never in the body


@router.post("/login", dependencies=[Depends(csrf_guard)])
async def web_login(
    body: WebLoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    tokens = await svc.login(session, redis, employee_id=body.employee_id, password=body.password, web=True)
    _set_cookie(response, tokens["refresh_token"], await _max_age(session))
    return _body(tokens)


@router.post("/refresh", dependencies=[Depends(csrf_guard)])
async def web_refresh(
    response: Response,
    session: AsyncSession = Depends(get_session),
    ec_rt: str | None = Cookie(default=None, alias=COOKIE),
):
    if not ec_rt:
        return _failure(AppError("UNAUTHORIZED", "No session"))
    try:
        tokens = await svc.refresh(session, refresh_token=ec_rt, web=True)
    except AppError as exc:
        return _failure(exc)
    _set_cookie(response, tokens["refresh_token"], await _max_age(session))
    return _body(tokens)


def _failure(exc: AppError) -> JSONResponse:
    out = error_response(exc.status, exc.code, exc.message, exc.details, exc.headers)
    if exc.status in (401, 403):  # the cookie is dead: drop it
        _clear_cookie(out)
    return out


@router.post("/logout", status_code=204, dependencies=[Depends(csrf_guard)])
async def web_logout(
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    ec_rt: str | None = Cookie(default=None, alias=COOKIE),
):
    await svc.logout_web(session, redis, refresh_token=ec_rt)
    out = Response(status_code=204)
    _clear_cookie(out)
    return out


@router.post("/change-password")
async def web_change_password(
    body: ChangePasswordRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(require_permission("console.access", allow_password_change=True)),
    _csrf: None = Depends(csrf_guard),  # after the bearer check: an anonymous call answers 401, not 403
):
    """Console variant of POST /auth/change-password: the new refresh token goes into the cookie, not the body."""
    tokens = await svc.change_password(
        session, principal, current_password=body.current_password, new_password=body.new_password, web=True, redis=redis
    )
    _set_cookie(response, tokens["refresh_token"], await _max_age(session))
    return _body(tokens)
