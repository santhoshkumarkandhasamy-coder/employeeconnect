"""GET /home aggregator (contract section 2). Every part is computed independently: a failure is logged, the session is
rolled back and that part is null (celebrations: empty list), so one broken module never takes the dashboard down."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.rbac import Principal
from ..models import LeaveRequest, OnDutyRequest, PermissionRequest
from . import announcements, celebrations
from . import attendance as att
from . import food as food_svc
from . import leave as leave_svc

log = logging.getLogger("app.home")
HOME_CELEBRATION_DAYS = 7


async def _safe(session: AsyncSession, name: str, fn: Callable[[], Awaitable[Any]], default: Any = None) -> Any:
    try:
        return await fn()
    except Exception:  # noqa: BLE001 - see module docstring
        log.exception("home part %s failed", name)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return default


async def _attendance(session: AsyncSession, actor: Principal) -> dict[str, Any]:
    rec = await att.today(session, actor)
    if not rec or rec.get("record", 1) is None:
        return {"check_in_at": None, "check_out_at": None, "status": None}
    return {"check_in_at": rec["check_in_at"], "check_out_at": rec["check_out_at"], "status": rec["status"]}


async def _pending(session: AsyncSession, actor: Principal) -> int:
    total = 0
    for model in (LeaveRequest, PermissionRequest, OnDutyRequest):
        total += (
            await session.execute(
                select(func.count()).select_from(model).where(model.user_id == actor.id, model.status == "pending")
            )
        ).scalar_one()
    return int(total)


async def build(session: AsyncSession, redis: Redis | None, actor: Principal) -> dict[str, Any]:
    balances = await _safe(session, "balances", lambda: leave_svc.balance(session, actor, None))
    return {
        "today_attendance": await _safe(session, "attendance", lambda: _attendance(session, actor)),
        "leave_balances": (
            [{"leave_type": r["name"], "remaining": r["remaining"]} for r in balances["leave"]] if balances else None
        ),
        "permission_remaining": (
            {"count": balances["permission"]["remaining_count"], "minutes": balances["permission"]["remaining_minutes"]}
            if balances
            else None
        ),
        "unread_announcements": await _safe(session, "announcements", lambda: announcements.unread_count(session, actor.user)),
        "pending_requests": await _safe(session, "pending", lambda: _pending(session, actor)),
        "food_today": await _safe(session, "food", lambda: food_svc.today_summary(session, actor.user)),
        "celebrations": await _safe(
            session, "celebrations", lambda: celebrations.upcoming(session, redis, HOME_CELEBRATION_DAYS), []
        ),
    }
