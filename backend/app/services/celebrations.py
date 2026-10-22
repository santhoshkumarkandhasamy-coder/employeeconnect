"""Birthdays and wedding anniversaries for the home feed (plan 07).

Dates of birth are decrypted in memory only. The Redis cache (one key per generation and office day, 30-day horizon) holds
user ids, the upcoming month/day and, for anniversaries, the year count - never a date of birth. Feb 29 is celebrated on
Feb 28 in non-leap years. Only users who OPTED IN (`show_celebrations = true`, the default is false) and are active appear;
the opt-in is re-checked against the database on every read, so a stale cache can never publish someone who opted out.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import LargeBinary, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import utcnow
from ..core.crypto import DataDecryptionError, decrypt_bytes
from ..models import EmployeeProfile, User
from . import attendance as att
from . import files as files_svc

log = logging.getLogger("app.celebrations")
HORIZON_DAYS = 30
CACHE_TTL_S = 6 * 3600
_KEY = "celebrations:v2:"
_GEN = "celebrations:gen"


def occurrence(month: int, day: int, today: dt.date) -> dt.date:
    """Next date on or after `today` on which (month, day) falls; Feb 29 -> Feb 28 in non-leap years."""
    for year in (today.year, today.year + 1):
        try:
            d = dt.date(year, month, day)
        except ValueError:
            d = dt.date(year, 2, 28)
        if d >= today:
            return d
    raise AssertionError("unreachable")


def _plain(value: bytes | None, who: uuid.UUID) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(decrypt_bytes(value).decode())
    except (DataDecryptionError, ValueError):
        log.error("celebration date of user %s is unreadable; skipped", who)
        return None


async def _compute(session: AsyncSession, today: dt.date) -> list[dict[str, Any]]:
    t = EmployeeProfile.__table__.c
    q = (
        select(User.id, type_coerce(t.dob_enc, LargeBinary), type_coerce(t.wedding_anniversary_enc, LargeBinary))
        .join(EmployeeProfile, EmployeeProfile.user_id == User.id)
        .where(
            User.status == "active",
            User.deleted_at.is_(None),
            EmployeeProfile.show_celebrations.is_(True),
            (t.dob_enc.is_not(None)) | (t.wedding_anniversary_enc.is_not(None)),
        )
    )
    horizon = today + dt.timedelta(days=HORIZON_DAYS)
    out: list[dict[str, Any]] = []
    for uid, dob_raw, anniv_raw in (await session.execute(q)).all():
        for kind, raw in (("birthday", dob_raw), ("anniversary", anniv_raw)):
            d = _plain(raw, uid)
            if d is None:
                continue
            occ = occurrence(d.month, d.day, today)
            if occ > horizon:
                continue
            years = occ.year - d.year if kind == "anniversary" and occ.year > d.year else None  # birthday: never the age
            out.append({"user_id": str(uid), "type": kind, "date": occ.isoformat(), "years": years})
    out.sort(key=lambda e: (e["date"], e["type"], e["user_id"]))
    return out


async def _generation(redis: Redis) -> str:
    raw = await redis.get(_GEN)
    return str(int(raw)) if raw else "0"


async def invalidate(redis: Redis) -> None:
    """Bumps the cache generation (old keys become unreachable and expire by TTL), then best-effort deletes them."""
    try:
        await redis.incr(_GEN)
    except Exception:  # noqa: BLE001 - cache only; reads still filter on the current opt-in
        log.warning("could not bump the celebrations cache generation")
    try:
        async for key in redis.scan_iter(match=_KEY + "*", count=100):
            await redis.delete(key)
    except Exception:  # noqa: BLE001
        log.warning("could not invalidate the celebrations cache")


async def upcoming(session: AsyncSession, redis: Redis | None, days: int) -> list[dict[str, Any]]:
    cfg = await att.load_settings(session)
    today = utcnow().astimezone(att.office_tz(cfg)).date()
    key = _KEY + today.isoformat()
    entries: list[dict[str, Any]] | None = None
    if redis is not None:
        try:
            key = f"{_KEY}{await _generation(redis)}:{today.isoformat()}"
            raw = await redis.get(key)
            entries = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            entries = None
    if entries is None:
        entries = await _compute(session, today)
        if redis is not None:
            try:
                await redis.set(key, json.dumps(entries), ex=CACHE_TTL_S)
            except Exception:  # noqa: BLE001
                log.warning("could not cache celebrations")
    last = (today + dt.timedelta(days=days)).isoformat()
    entries = [e for e in entries if e["date"] <= last]
    if not entries:
        return []
    ids = {uuid.UUID(e["user_id"]) for e in entries}
    users = {  # the opt-in is checked here again, against the database, whatever the cache says
        u.id: u
        for u in (
            await session.execute(
                select(User)
                .join(EmployeeProfile, EmployeeProfile.user_id == User.id)
                .where(User.id.in_(ids), User.status == "active", EmployeeProfile.show_celebrations.is_(True))
            )
        )
        .unique()
        .scalars()
    }
    photos = await files_svc.photo_file_ids(session, list(users))
    return [
        {"user": att.user_summary(users[uuid.UUID(e["user_id"])], photos.get(uuid.UUID(e["user_id"]))),
         "type": e["type"], "date": e["date"], "years": e["years"]}
        for e in entries
        if uuid.UUID(e["user_id"]) in users
    ]  # fmt: skip
