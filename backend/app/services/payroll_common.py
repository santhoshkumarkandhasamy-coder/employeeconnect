"""Shared payroll helpers: DB error mapping, the denied-access audit, rate limits and small serialisers."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from types import SimpleNamespace
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.audit import audit
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import User
from . import attendance as att

log = logging.getLogger("app.payroll")


def sqlstate(exc: BaseException) -> str | None:
    """SQLSTATE of a database error, whichever driver wrapper carries it (asyncpg: `sqlstate`, psycopg: `pgcode`)."""
    orig = getattr(exc, "orig", exc)
    for obj in (orig, getattr(orig, "__cause__", None)):
        for attr in ("sqlstate", "pgcode"):
            code = getattr(obj, attr, None)
            if isinstance(code, str):
                return code
    return None


def map_integrity(exc: IntegrityError) -> AppError:
    """The lock triggers of migration 0018 raise SQLSTATE 23000 when published data would change: PAYROLL_LOCKED.
    Unique violations (23505) mean a concurrent writer won the race."""
    state = sqlstate(exc)
    if state == "23000":
        return AppError("PAYROLL_LOCKED", "Published payroll data is locked and cannot be changed")
    if state == "23505":
        return AppError("CONFLICT", "The data was changed concurrently; reload and retry")
    log.error("payroll integrity error (sqlstate %s)", state)
    return AppError("CONFLICT", "The change conflicts with existing data")


async def commit(session: AsyncSession) -> None:
    """commit() with trigger errors mapped to PAYROLL_LOCKED."""
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise map_integrity(exc) from None


async def flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise map_integrity(exc) from None


async def deny(
    session: AsyncSession, actor: Principal, *, entity_type: str, entity_id: Any, reason: str, status: int = 403
) -> AppError:
    """Audit a refused access (403/404 probe, outcome failure; own commit) and return the error to raise."""
    who = SimpleNamespace(id=actor.id, employee_id=actor.employee_id)  # plain values survive the rollback below
    await session.rollback()
    audit(
        session, action="payroll.denied", actor=who, entity_type=entity_type, entity_id=entity_id,
        outcome="failure", after={"reason": reason, "status": status},
    )  # fmt: skip
    await session.commit()
    if status == 404:
        return AppError("NOT_FOUND", "Not found")
    return AppError("FORBIDDEN", "You cannot change your own payroll data")


async def forbid_self(
    session: AsyncSession, actor: Principal, target_user_id: uuid.UUID, *, entity_type: str, entity_id: Any
) -> None:
    """Nobody creates/edits/revises/processes/publishes their OWN structure, details or payslip (plan 12 D9)."""
    if target_user_id == actor.id:
        raise await deny(session, actor, entity_type=entity_type, entity_id=entity_id, reason="self")


# ---------------------------------------------------------------------------------------------- rate limits
async def limit(redis: Redis, actor: Principal, name: str, *windows: tuple[int, int], closed: bool = False) -> None:
    """windows = (limit, seconds)... per user. `closed` = fail closed (503) when Redis is down."""
    await rate_limit.enforce(redis, [(f"rl:payroll:{name}:{actor.id}:{w}", n, w) for n, w in windows], fail_closed=closed)


# ---------------------------------------------------------------------------------------------- serialisers
def user_out(u: User | None) -> dict[str, Any] | None:
    return att.user_summary(u) if u is not None else None


def iso(ts: dt.datetime | None) -> str | None:
    from ..core.clock import iso_z

    return iso_z(ts) if ts is not None else None


def period_str(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def mask_tail(value: str | None, keep: int = 4) -> str | None:
    if not value:
        return None
    return "X" * max(0, len(value) - keep) + value[-keep:]


def mask_pan(value: str | None) -> str | None:
    """ABCDE1234F -> XXXXX1234X"""
    if not value:
        return None
    return "XXXXX" + value[5:9] + "X" if len(value) == 10 else mask_tail(value)
