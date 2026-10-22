"""GET /admin/system/health data (contract s.12.5). Nothing secret is returned; every probe is bounded and failure-tolerant."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import as_utc, iso_z, utcnow
from ..core.config import get_settings
from . import fcm

log = logging.getLogger("app.health")

HEARTBEAT_KEY = "ec:worker:heartbeat"
HEARTBEAT_STALE_S = 180
BACKUP_STALE = dt.timedelta(hours=26)
TLS_WARN_DAYS = 14
PROBE_TIMEOUT_S = 3


def _parse(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(d if d.tzinfo else d.replace(tzinfo=dt.UTC))


async def _db(session: AsyncSession) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), PROBE_TIMEOUT_S)
        latency = round((time.perf_counter() - started) * 1000)
    except Exception:  # noqa: BLE001
        log.error("health: database probe failed")
        return {"ok": False, "latency_ms": None, "migration": None}
    migration = None
    try:
        async with session.begin_nested():  # a missing table must not poison the transaction
            migration = (await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - e.g. hand-built database without alembic_version
        migration = None
    return {"ok": True, "latency_ms": latency, "migration": migration}


async def _redis(redis: Redis) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        await asyncio.wait_for(redis.ping(), PROBE_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        log.error("health: redis probe failed")
        return {"ok": False}, {"ok": False, "last_heartbeat_at": None}
    last = None
    try:
        last = _parse(await asyncio.wait_for(redis.get(HEARTBEAT_KEY), PROBE_TIMEOUT_S))
    except Exception:  # noqa: BLE001
        pass
    fresh = last is not None and (utcnow() - last).total_seconds() <= HEARTBEAT_STALE_S
    return {"ok": True}, {"ok": fresh, "last_heartbeat_at": iso_z(last) if last else None}


def _read_backup() -> dict[str, Any]:
    path = Path(get_settings().status_dir) / "backup.json"
    try:
        if path.stat().st_size > 16_384:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


async def _backup() -> dict[str, Any]:
    data = await asyncio.to_thread(_read_backup)
    db_at, up_at = _parse(data.get("db_last_success_at")), _parse(data.get("uploads_last_success_at"))
    now = utcnow()
    ok = db_at is not None and up_at is not None and now - db_at <= BACKUP_STALE and now - up_at <= BACKUP_STALE
    return {
        "db_last_success_at": iso_z(db_at) if db_at else None,
        "uploads_last_success_at": iso_z(up_at) if up_at else None,
        "ok": ok,
    }


def _storage() -> dict[str, Any]:
    try:
        u = shutil.disk_usage(get_settings().file_storage_dir)
        return {"uploads_free_bytes": u.free, "uploads_free_pct": round(u.free * 100 / u.total) if u.total else None}
    except OSError:
        return {"uploads_free_bytes": None, "uploads_free_pct": None}


def _tls() -> dict[str, Any]:
    path = get_settings().tls_cert_file
    empty = {"not_after": None, "days_left": None}
    if not path:
        return empty
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(Path(path).read_bytes())
        not_after = as_utc(cert.not_valid_after_utc)
        return {"not_after": iso_z(not_after), "days_left": (not_after - utcnow()).days}  # type: ignore[operator]
    except (OSError, ValueError):
        return empty


async def health(session: AsyncSession, redis: Redis) -> dict[str, Any]:
    db = await _db(session)
    redis_ok, worker = await _redis(redis)
    backup = await _backup()
    storage = await asyncio.to_thread(_storage)
    tls = await asyncio.to_thread(_tls)
    degraded = (
        not db["ok"]
        or not redis_ok["ok"]
        or not worker["ok"]
        or not backup["ok"]
        or (tls["days_left"] is not None and tls["days_left"] < TLS_WARN_DAYS)
    )
    return {
        "status": "degraded" if degraded else "ok",
        "app_version": get_settings().app_version,
        "db": db,
        "redis": redis_ok,
        "worker": worker,
        "backup": backup,
        "storage": storage,
        "tls": tls,
        "push": {"configured": fcm.is_configured()},
    }
