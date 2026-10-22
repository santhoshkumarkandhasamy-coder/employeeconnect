"""Per-user daily volume ceiling for the Excel export (review 08 M3).

On top of the 5/min rate limit, every user may run at most EXPORT_DAILY_MAX_EXPORTS exports and receive at most
EXPORT_DAILY_MAX_ROWS rows per UTC day. A stolen HR token therefore cannot pull the whole company history in bulk, only a
bounded slice per day. Counters live in Redis (two keys per user and day, expiring after two days). Redis unavailable ->
fail CLOSED (503), like the rate limiter on this endpoint.

Crossing the (non-blocking) warn thresholds, or a single large export, writes a WARNING line to the `app.export` logger so
log-based alerting can pick it up. Step-up re-authentication is deliberately not used here.
"""

from __future__ import annotations

import logging
import uuid

from redis.asyncio import Redis

from ..core.clock import utcnow
from ..core.config import get_settings
from ..core.errors import AppError

log = logging.getLogger("app.export")
KEY_TTL_S = 2 * 86400


def _keys(user_id: uuid.UUID) -> tuple[str, str]:
    day = utcnow().strftime("%Y%m%d")
    return f"quota:export:{user_id}:{day}:n", f"quota:export:{user_id}:{day}:rows"


def _seconds_to_midnight() -> int:
    now = utcnow()
    return max(1, 86400 - (now.hour * 3600 + now.minute * 60 + now.second))


def _quota_error(what: str) -> AppError:
    return AppError(
        "EXPORT_QUOTA_EXCEEDED",
        f"Daily export limit reached ({what}). Try again tomorrow or narrow what you need.",
        headers={"Retry-After": str(_seconds_to_midnight())},
    )


def _unavailable() -> AppError:
    return AppError(
        "TEMPORARILY_UNAVAILABLE", "Service temporarily unavailable. Try again shortly.", headers={"Retry-After": "30"}
    )


async def reserve(redis: Redis, user_id: uuid.UUID) -> None:
    """Count one export for today before it is built; refuse when today's export count or row total is already used up."""
    cfg = get_settings()
    n_key, rows_key = _keys(user_id)
    try:
        used_rows = int(await redis.get(rows_key) or 0)
        if used_rows >= cfg.export_daily_max_rows:
            raise _quota_error("rows")
        n = int(await redis.incr(n_key))
        await redis.expire(n_key, KEY_TTL_S)
        if n > cfg.export_daily_max_exports:
            await redis.decr(n_key)
            raise _quota_error("exports")
    except AppError:
        raise
    except Exception:  # noqa: BLE001 - any Redis/network failure
        log.error("export quota store unavailable; failing closed")
        raise _unavailable() from None
    if n >= cfg.export_daily_warn_exports:
        log.warning(
            "export volume: user %s has started %s exports today (warn threshold %s)", user_id, n, cfg.export_daily_warn_exports
        )


async def refund(redis: Redis, user_id: uuid.UUID) -> None:
    """The export produced nothing (refused / failed): give the count back. Best effort."""
    try:
        await redis.decr(_keys(user_id)[0])
    except Exception:  # noqa: BLE001
        log.error("export quota refund failed")


async def record_rows(redis: Redis, user_id: uuid.UUID, rows: int) -> tuple[int, int]:
    """Add the rows of a finished build to today's total. Raises EXPORT_QUOTA_EXCEEDED (the rows are given back; the caller
    refunds the export count) when that would pass the daily row ceiling. Returns (exports today, rows today) for the audit row."""
    cfg = get_settings()
    n_key, rows_key = _keys(user_id)
    try:
        total = int(await redis.incrby(rows_key, rows))
        await redis.expire(rows_key, KEY_TTL_S)
        n = int(await redis.get(n_key) or 0)
    except Exception:  # noqa: BLE001
        log.error("export quota store unavailable; failing closed")
        raise _unavailable() from None
    if total > cfg.export_daily_max_rows:
        try:
            await redis.decrby(rows_key, rows)
        except Exception:  # noqa: BLE001
            log.error("export quota rollback failed")
        raise _quota_error("rows")
    if rows >= cfg.export_warn_rows:
        log.warning(
            "large export: user %s received %s rows in one export (warn threshold %s)", user_id, rows, cfg.export_warn_rows
        )
    if total >= cfg.export_daily_warn_rows:
        log.warning(
            "export volume: user %s has received %s rows today (warn threshold %s)", user_id, total, cfg.export_daily_warn_rows
        )
    return n, total
