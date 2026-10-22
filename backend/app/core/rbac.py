"""Authentication + RBAC dependencies.

Every endpoint declares `Depends(require_permission(...))`. With no codes it means "any authenticated
user" (self-service). The forced-password-change gate is applied here, so it covers every endpoint
except those that opt out with `allow_password_change=True` (change-password, logout, me).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User
from .clock import as_utc
from .db import get_session
from .errors import AppError
from .redis import get_redis
from .security import decode_access_token

_bearer = HTTPBearer(auto_error=False)
log = logging.getLogger("app.rbac")


@dataclass
class Principal:
    user: User
    role: str
    permissions: frozenset[str]
    sid: str | None = None  # set for console (web) access tokens only

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def employee_id(self) -> str:
        return self.user.employee_id

    @property
    def must_change_password(self) -> bool:
        return self.user.must_change_password

    def has(self, code: str) -> bool:
        return code in self.permissions


REVOKED_SID_PREFIX = "revsid:"


async def revoke_session_access(redis, sid: str, ttl_seconds: int) -> None:
    """Denylist one console session's access tokens until they would have expired anyway (other sessions of the same
    user are untouched, unlike the per-user epoch bump). Best effort: Redis down = the token lives out its 15 minutes."""
    try:
        await redis.set(REVOKED_SID_PREFIX + sid, "1", ex=max(1, ttl_seconds))
    except Exception:  # noqa: BLE001
        log.error("session denylist unavailable; web logout leaves the access token valid until it expires")


async def is_session_revoked(redis, sid: str) -> bool:
    try:
        return bool(await redis.exists(REVOKED_SID_PREFIX + sid))
    except Exception:  # noqa: BLE001 - fail open like the rate limiter; the token is short-lived
        return False


async def get_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise AppError("UNAUTHORIZED", "Missing or invalid Authorization header")
    claims = decode_access_token(creds.credentials)
    if claims.sid and await is_session_revoked(redis, claims.sid):  # console logout (review L1)
        raise AppError("UNAUTHORIZED", "Session no longer valid")
    user = (await session.execute(select(User).where(User.id == claims.user_id))).unique().scalar_one_or_none()
    if user is None:
        raise AppError("UNAUTHORIZED", "Invalid access token")
    if user.status != "active" or user.deleted_at is not None:
        raise AppError("ACCOUNT_INACTIVE", "Account is inactive")
    valid_after = as_utc(user.token_valid_after)
    if valid_after is not None and claims.iat < int(valid_after.timestamp()):
        # Logout / logout-all / password change / admin reset happened after this token was issued. The client
        # treats UNAUTHORIZED like an expired token: it refreshes once, and a revoked session then ends cleanly.
        raise AppError("UNAUTHORIZED", "Session no longer valid")
    # expired license with signed on_expiry=read_only: writes answer 403 LICENSE_EXPIRED (exemptions live in the service)
    from ..services import license as license_svc  # lazy: services import this module

    await license_svc.enforce_not_read_only(session, request.method, request.url.path)
    perms = frozenset(p.code for p in user.role.permissions)
    return Principal(user=user, role=user.role.name, permissions=perms, sid=claims.sid)


def require_permission(*codes: str, allow_password_change: bool = False) -> Callable:
    """Dependency factory. Holding ANY of `codes` grants access (e.g. `_team` or `_all`)."""
    needed = frozenset(codes)

    async def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.must_change_password and not allow_password_change:
            raise AppError("PASSWORD_CHANGE_REQUIRED", "You must change your password first")
        if needed and not (needed & principal.permissions):
            raise AppError("FORBIDDEN", "You do not have permission to perform this action")
        return principal

    return _dep
