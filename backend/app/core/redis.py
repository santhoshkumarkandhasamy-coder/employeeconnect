from __future__ import annotations

from redis.asyncio import Redis

from .config import get_settings

_client: Redis | None = None


def get_redis_client() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(get_settings().redis_url, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=1.0)
    return _client


async def get_redis() -> Redis:
    """FastAPI dependency (overridden in tests with fakeredis)."""
    return get_redis_client()


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
