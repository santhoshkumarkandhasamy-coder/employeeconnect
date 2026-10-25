"""Step-up re-authentication and security alerts for privileged admin actions (review 06 M1/M6).

* `require_reauth` verifies the ACTING admin's own password (`current_password` in the request body). Attempts are rate
  limited to 5/min per actor and the limiter FAILS CLOSED (Redis down -> 503 instead of unlimited password guessing).
  The password is never logged, audited or echoed; only the fact of a failed attempt is audited.
* `notify_admins` pushes names/ids only (no secrets, no personal data) to every active holder of `role.assign`.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit, security
from ..core.audit import audit
from ..core.config import get_settings
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import Permission, Role, User
from . import notifications

log = logging.getLogger("app.admin_security")

REAUTH_PER_MINUTE = 5
ADMIN_CAPABLE = frozenset({"role.assign", "role.manage"})


# Permissions every role holds for its own data: they do NOT make a role "elevated" (plain self-service stays plain).
SELF_SERVICE_PERMISSIONS = frozenset({"payroll.view_own"})


def is_admin_capable(permissions: frozenset[str] | set[str]) -> bool:
    return bool(ADMIN_CAPABLE & set(permissions))


async def require_reauth(
    session: AsyncSession, redis: Redis, actor: Principal, password: SecretStr | None, *, action: str
) -> None:
    if password is None:
        raise AppError(
            "VALIDATION_ERROR",
            "Request validation failed",
            details=[{"field": "current_password", "message": "your own password is required for this action"}],
        )
    await rate_limit.enforce(redis, [(f"rl:admin:reauth:{actor.id}", REAUTH_PER_MINUTE, 60)], fail_closed=True)
    if not await security.averify_password(actor.user.password_hash, password.get_secret_value()):
        audit(
            session, action="admin.reauth_failed", actor=actor, entity_type="user", entity_id=actor.id,
            outcome="failure", after={"for": action},
        )  # fmt: skip
        await session.commit()
        raise AppError("REAUTH_FAILED", "Your password is incorrect")


async def admin_ids(session: AsyncSession) -> list[uuid.UUID]:
    q = (
        select(User.id)
        .join(Role, Role.id == User.role_id)
        .where(
            User.status == "active",
            User.deleted_at.is_(None),
            Role.permissions.any(Permission.code == "role.assign"),
        )
    )
    return list((await session.execute(q)).unique().scalars())


async def notify_admins(session: AsyncSession, *, entity_id: uuid.UUID | str, title: str, body: str) -> None:
    """Best effort (notify never raises). Call AFTER the change was committed."""
    try:
        await notifications.notify(
            session, await admin_ids(session), type="admin_alert", entity_id=entity_id, title=title, body=body,
            high_priority=True,
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.exception("admin alert failed (ignored)")


async def note_sensitive_view(session: AsyncSession, redis: Redis, actor: Principal) -> None:
    """Counts employee-detail views that include personal data per actor and hour; alerts the admins once when the
    threshold is crossed."""
    limit = get_settings().sensitive_view_alert_per_hour
    try:
        key = f"ctr:admin:sensview:{actor.id}"
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, 3600)
    except Exception:  # noqa: BLE001 - monitoring only; the rate limit is the control
        return
    if n == limit + 1:
        await notify_admins(
            session,
            entity_id=actor.id,
            title="Unusual number of personal-data views",
            body=f"{actor.user.full_name} ({actor.employee_id}) opened more than {limit} employee records within an hour.",
        )


async def notify_reset_request(session: AsyncSession, redis: Redis, requester: User) -> None:
    """Best effort push `type: reset_request` (id = requester, name + employee id only) to the active holders of
    `employee.reset_password` who may act on that account (same privilege rules as the reset itself). At most
    RESET_REQUEST_PUSH_PER_HOUR per requester and hour. Call AFTER the request row was committed."""
    try:
        key = f"ctr:resetreq:{requester.id}"
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, 3600)
        if n > get_settings().reset_request_push_per_hour:
            return
        holders = (
            (
                await session.execute(
                    select(User)
                    .join(Role, Role.id == User.role_id)
                    .where(
                        User.status == "active",
                        User.deleted_at.is_(None),
                        User.id != requester.id,
                        Role.permissions.any(Permission.code == "employee.reset_password"),
                    )
                )
            )
            .unique()
            .scalars()
            .all()
        )
        target = frozenset(p.code for p in requester.role.permissions) - SELF_SERVICE_PERMISSIONS
        ids = []
        for h in holders:
            perms = frozenset(p.code for p in h.role.permissions)
            if not target <= perms or (target and "role.assign" not in perms):
                continue  # privilege rules: containment + elevated targets need role.assign
            if not ({"employee.update", "employee.create"} & perms) and requester.manager_id != h.id:
                continue  # team-scoped holder: direct reports only
            ids.append(h.id)
        await notifications.notify(
            session,
            ids,
            type="reset_request",
            entity_id=requester.id,
            title="Password reset requested",
            body=f"{requester.full_name} ({requester.employee_id}) asked for a password reset.",
        )
    except Exception:  # noqa: BLE001
        log.exception("reset_request push failed (ignored)")
