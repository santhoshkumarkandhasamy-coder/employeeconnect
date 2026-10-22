"""Redis sliding-window rate limiting. Fails OPEN if Redis is unavailable (decision O1): the
per-account DB lockout still applies, and the error is logged."""

from __future__ import annotations

import logging
import math
import time
import uuid

from redis.asyncio import Redis

from .errors import AppError

log = logging.getLogger("app.ratelimit")


async def hit(redis: Redis, key: str, limit: int, window_s: int, *, fail_closed: bool = False) -> int | None:
    """Count this attempt. Returns None when allowed, else seconds until retry. Redis errors -> allowed."""
    try:
        now = time.time()
        pipe = redis.pipeline()
        pipe.zremrangebyscore(key, 0, now - window_s)
        pipe.zadd(key, {f"{now}:{uuid.uuid4().hex[:8]}": now})
        pipe.zcard(key)
        pipe.zrange(key, 0, 0, withscores=True)
        pipe.expire(key, window_s)
        _, _, count, oldest, _ = await pipe.execute()
        if count > limit:
            first = oldest[0][1] if oldest else now
            return max(1, math.ceil(first + window_s - now))
        return None
    except Exception:  # noqa: BLE001 - any Redis/network failure
        family = key.split(":")[1] if ":" in key else key
        if fail_closed:
            log.error("rate limiter unavailable (key family %s); failing closed", family)
            raise AppError(
                "TEMPORARILY_UNAVAILABLE", "Service temporarily unavailable. Try again shortly.", headers={"Retry-After": "30"}
            ) from None
        log.error("rate limiter unavailable (key family %s); failing open", family)
        return None


async def enforce(redis: Redis, checks: list[tuple[str, int, int]], *, fail_closed: bool = False) -> None:
    """checks = [(key, limit, window_s)]. All keys are counted; raises RATE_LIMITED if any exceeded.
    `fail_closed=True` (expensive endpoints such as image uploads) rejects instead of allowing when Redis is down."""
    worst = 0
    for key, limit, window in checks:
        retry = await hit(redis, key, limit, window, fail_closed=fail_closed)
        worst = max(worst, retry or 0)
    if worst:
        raise AppError("RATE_LIMITED", "Too many attempts. Try again later.", headers={"Retry-After": str(worst)})


# ---- failure gate: only FAILED attempts count; a cool-down starts once `limit` failures happened in the window
async def gate_check(redis: Redis, key: str, limit: int) -> None:
    try:
        raw = await redis.get(key)
        if raw is not None and int(raw) >= limit:
            ttl = await redis.ttl(key)
            raise AppError(
                "RATE_LIMITED", "Too many failed attempts. Try again later.", headers={"Retry-After": str(max(1, ttl))}
            )
    except AppError:
        raise
    except Exception:  # noqa: BLE001
        log.error("failure gate unavailable (key family %s); failing open", key.split(":")[1] if ":" in key else key)


async def gate_fail(redis: Redis, key: str, window_s: int) -> None:
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, window_s)
    except Exception:  # noqa: BLE001
        log.error("failure gate unavailable (key family %s)", key.split(":")[1] if ":" in key else key)


async def gate_reset(redis: Redis, key: str) -> None:
    try:
        await redis.delete(key)
    except Exception:  # noqa: BLE001
        pass
