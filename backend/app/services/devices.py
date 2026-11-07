"""Device registry (plan 18, contract 21.1-21.2): CRUD, address guard, serial pinning, job queue for Test / Sync now.

The API never opens a socket to a device. Test connection and Sync now are written to `device_syncs` (status queued) and announced
on a Redis list; the worker (services/device_sync.py + jobs/scheduler.py) runs them. The comm key is encrypted at rest, write-only
in the API and travels to the worker inside a Fernet-encrypted Redis value with a 120 s TTL (unsaved tests only).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import socket
import uuid
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.audit import audit
from ..core.clock import as_utc, iso_z, utcnow
from ..core.config import SPECIAL_RANGES, get_settings
from ..core.crypto import encrypt_bytes
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import Attendance, Device, DevicePunch, DeviceSync, DeviceUser, Permission, RolePermission, User
from . import attendance as att
from . import device_settings
from .notifications import notify
from .system_health import HEARTBEAT_KEY, HEARTBEAT_STALE_S, _parse

log = logging.getLogger("app.devices")

QUEUE_KEY = "ec:devices:jobs"
TESTJOB_KEY = "ec:devices:testjob:{}"
TESTJOB_TTL_S = 120
MAX_DEVICES = 50
DOCKER_DEFAULT_NET = ipaddress.ip_network("172.17.0.0/16")
QUEUE_TEST_KEY = "ec:devices:jobs:test"  # Test connection jobs get their own list so they cannot starve or be starved by syncs

ERROR_TEXT = {
    "DEVICE_UNREACHABLE": "The device could not be reached on the network",
    "DEVICE_TIMEOUT": "Device did not answer within 5 seconds",
    "DEVICE_AUTH_FAILED": "The device rejected the communication key",
    "DEVICE_BUSY": "Another program is connected to the device right now",
    "DEVICE_PROTOCOL_ERROR": "The device sent an unexpected reply",
    "DEVICE_IDENTITY_CHANGED": "This is a different machine than the one registered. Accept the new identity if it was replaced",
    "DEVICE_CLOCK_INVALID": "The device clock is wrong",
    "CIRCUIT_OPEN": "Skipped after repeated failures; will retry later",
    "DEVICE_ADDRESS_NOT_ALLOWED": "The device address is not allowed",
    "DEVICE_DISABLED": "The device is disabled",
}


# ---------------------------------------------------------------- address guard (SSRF)
@lru_cache(maxsize=1)
def _own_addresses() -> tuple[str, ...]:
    out: set[str] = set()
    try:
        host = socket.gethostname()
        out.update(i[4][0] for i in socket.getaddrinfo(host, None, socket.AF_INET))
    except OSError:
        pass
    return tuple(out)


def address_problem(ip: str) -> str | None:
    """None when `ip` may be a device, else a short reason. Pure apart from the settings and the host's own addresses."""
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return "not an IPv4 address"
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return "loopback, link-local, multicast and reserved addresses are not allowed"
    if addr == ipaddress.IPv4Address("255.255.255.255"):
        return "broadcast address"
    st = get_settings()
    allowed = [n for n in st.device_allowed_networks if n.version == 4]
    net = next((n for n in allowed if addr in n), None)
    if net is None:
        return "outside DEVICE_ALLOWED_CIDRS"
    if net.prefixlen <= 30 and addr in (net.network_address, net.broadcast_address):
        return "network or broadcast address"
    if any(addr in n for n in (*st.device_blocked_networks, DOCKER_DEFAULT_NET, *SPECIAL_RANGES) if n.version == 4):
        return "docker network, special-purpose range or this server"
    if str(addr) in _own_addresses():
        return "this server's own address"
    return None


def port_problem(port: int) -> str | None:
    """None when `port` is in DEVICE_ALLOWED_PORTS (default 4370 only)."""
    if port not in get_settings().device_allowed_port_set:
        return "port is not in DEVICE_ALLOWED_PORTS"
    return None


def check_address(ip: str, port: int | None = None) -> None:
    """Refuse an address (and, when given, a port) that may not be a device. The port is part of the SSRF guard (review L1)."""
    problem = address_problem(ip)
    if problem:
        raise AppError(
            "DEVICE_ADDRESS_NOT_ALLOWED",
            "This address cannot be used for an attendance device",
            details=[{"field": "ip", "message": problem}],
        )
    problem = port_problem(port) if port is not None else None
    if problem:
        raise AppError(
            "DEVICE_ADDRESS_NOT_ALLOWED",
            "This port cannot be used for an attendance device",
            details=[{"field": "port", "message": problem}],
        )


# ---------------------------------------------------------------- output
def device_status(d: Device, now: dt.datetime) -> str:
    if not d.enabled:
        return "disabled"
    open_until = as_utc(d.circuit_open_until)
    if open_until is not None and open_until > now:
        return "offline"
    if d.last_error_code is not None:
        return "failing"
    if d.last_sync_at is None or d.last_success_at is None:
        return "never_synced"
    return "ok"


def _ts(v: dt.datetime | None) -> str | None:
    return iso_z(v) if v else None


def device_out(d: Device, unmatched: int, now: dt.datetime) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "name": d.name,
        "location_label": d.location_label,
        "ip": str(d.ip).split("/")[0],
        "port": d.port,
        "transport": d.transport,
        "has_comm_key": d.comm_key is not None,  # the key itself is never returned
        "enabled": d.enabled,
        "timezone": d.timezone,
        "clock_correction": d.clock_correction,
        "ingest_from": d.ingest_from.isoformat(),
        "serial_number": d.serial_number,
        "model": d.model,
        "firmware": d.firmware,
        "status": device_status(d, now),
        "clock_status": d.clock_status,
        "last_clock_offset_s": d.last_clock_offset_s,
        "last_sync_at": _ts(d.last_sync_at),
        "last_success_at": _ts(d.last_success_at),
        "last_error_code": d.last_error_code,
        "last_error_at": _ts(d.last_error_at),
        "circuit_open_until": _ts(d.circuit_open_until),
        "records_on_device": d.last_log_count,
        "users_on_device": d.last_user_count,
        "unmatched_users": unmatched,
    }


async def _unmatched_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    q = select(DeviceUser.device_id, func.count()).where(DeviceUser.match_method == "unmatched").group_by(DeviceUser.device_id)
    return {k: int(v) for k, v in (await session.execute(q)).all()}


async def sync_out_many(session: AsyncSession, rows: list[DeviceSync]) -> list[dict[str, Any]]:
    ids = {r.requested_by for r in rows if r.requested_by}
    users: dict[uuid.UUID, User] = {}
    if ids:
        users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).unique().scalars()}
    out = []
    for r in rows:
        u = users.get(r.requested_by) if r.requested_by else None
        out.append(
            {
                "id": str(r.id),
                "device_id": str(r.device_id) if r.device_id else None,
                "kind": r.kind,
                "status": r.status,
                "error_code": r.error_code,
                "error_message": ERROR_TEXT.get(r.error_code or "", r.error_code) if r.error_code else None,
                "requested_by": att.user_summary(u) if u else None,
                "queued_at": _ts(r.queued_at),
                "started_at": _ts(r.started_at),
                "finished_at": _ts(r.finished_at),
                "records_on_device": r.records_on_device,
                "users_on_device": r.users_on_device,
                "fetched": r.fetched,
                "inserted": r.inserted,
                "quarantined": r.quarantined,
                "days_recomputed": r.days_recomputed,
                "clock_offset_s": r.clock_offset_s,
                "result": r.result if r.kind == "test" else None,
            }
        )
    return out


# ---------------------------------------------------------------- registry
async def get_or_404(session: AsyncSession, device_id: uuid.UUID, *, lock: bool = False) -> Device:
    q = select(Device).where(Device.id == device_id)
    d = (await session.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if d is None:
        raise AppError("NOT_FOUND", "Device not found")
    return d


async def list_devices(session: AsyncSession) -> dict[str, Any]:
    now = utcnow()
    counts = await _unmatched_counts(session)
    rows = (await session.execute(select(Device).order_by(func.lower(Device.name)))).scalars().all()
    return {"items": [device_out(d, counts.get(d.id, 0), now) for d in rows]}


async def get_device(session: AsyncSession, device_id: uuid.UUID) -> dict[str, Any]:
    d = await get_or_404(session, device_id)
    counts = await _unmatched_counts(session)
    recent = (
        (
            await session.execute(
                select(DeviceSync).where(DeviceSync.device_id == d.id).order_by(DeviceSync.queued_at.desc()).limit(10)
            )
        )
        .scalars()
        .all()
    )
    return {**device_out(d, counts.get(d.id, 0), utcnow()), "recent_syncs": await sync_out_many(session, list(recent))}


async def _office_today(session: AsyncSession) -> dt.date:
    return utcnow().astimezone(att.office_tz(await att.load_settings(session))).date()


def _check_timezone(tz: str | None) -> None:
    if tz is None:
        return
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        raise AppError(
            "VALIDATION_ERROR", "Unknown timezone", details=[{"field": "timezone", "message": "must be an IANA name"}]
        ) from None


async def _ingest_from(session: AsyncSession, value: dt.date | None, keep: dt.date | None = None) -> dt.date:
    today_d = await _office_today(session)
    if value is None:
        return keep or today_d
    if keep is not None and value == keep:
        return value  # unchanged on a full-replace PUT
    keep_days = (await device_settings.load(session)).punch_retention_days or 3650  # 0 = keep forever; cap at ten years
    if value > today_d or value < today_d - dt.timedelta(days=keep_days):
        msg = f"today or at most {keep_days} days back (the punch retention)"
        raise AppError("VALIDATION_ERROR", f"ingest_from must be {msg}", details=[{"field": "ingest_from", "message": msg}])
    return value


async def _check_unique(session: AsyncSession, name: str, ip: str, port: int, except_id: uuid.UUID | None) -> None:
    q = select(Device.id).where(func.lower(Device.name) == name.lower())
    if except_id:
        q = q.where(Device.id != except_id)
    if (await session.execute(q)).first():
        raise AppError("CONFLICT", "A device with this name already exists", details=[{"field": "name", "message": "duplicate"}])
    q2 = select(Device.id).where(Device.ip == ip, Device.port == port)
    if except_id:
        q2 = q2.where(Device.id != except_id)
    if (await session.execute(q2)).first():
        raise AppError(
            "CONFLICT", "A device with this address and port already exists", details=[{"field": "ip", "message": "duplicate"}]
        )


def _jsonable(v: Any) -> Any:
    return v.isoformat() if isinstance(v, dt.date) else v


# non-secret values worth keeping in the audit trail (the comm key and the free-text label never go there)
_AUDITED = ("name", "ip", "port", "transport", "enabled", "timezone", "clock_correction", "ingest_from")


async def create_device(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    check_address(body.ip, body.port)
    _check_timezone(body.timezone)
    if (await session.execute(select(func.count()).select_from(Device))).scalar_one() >= MAX_DEVICES:
        raise AppError("CONFLICT", "Too many devices")
    await _check_unique(session, body.name, body.ip, body.port, None)
    d = Device(
        name=body.name,
        ip=body.ip,
        port=body.port,
        transport=body.transport,
        comm_key=str(body.comm_key) if body.comm_key else None,  # 0 = no key
        enabled=body.enabled,
        location_label=body.location_label,
        timezone=body.timezone,
        clock_correction=body.clock_correction,
        ingest_from=await _ingest_from(session, body.ingest_from),
        consecutive_failures=0,
        created_by=actor.id,
    )
    session.add(d)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError("CONFLICT", "A device with this name or address already exists") from None
    audit(
        session, action="device.create", actor=actor, entity_type="device", entity_id=d.id,
        after={
            "fields": sorted(body.model_fields_set | {"name", "ip", "port"}), "comm_key_changed": bool(body.comm_key),
            "name": body.name, "ip": body.ip, "port": body.port, "transport": body.transport,
        },
    )  # fmt: skip
    await session.commit()
    return device_out(d, 0, utcnow())


async def update_device(session: AsyncSession, actor: Principal, device_id: uuid.UUID, body: Any) -> dict[str, Any]:
    d = await get_or_404(session, device_id, lock=True)
    check_address(body.ip, body.port)
    _check_timezone(body.timezone)
    await _check_unique(session, body.name, body.ip, body.port, d.id)
    changed: list[str] = []
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    new_ingest = await _ingest_from(session, body.ingest_from, keep=d.ingest_from)
    for field, new in (
        ("name", body.name), ("ip", body.ip), ("port", body.port), ("transport", body.transport), ("enabled", body.enabled),
        ("location_label", body.location_label), ("timezone", body.timezone), ("clock_correction", body.clock_correction),
        ("ingest_from", new_ingest),
    ):  # fmt: skip
        old = str(d.ip).split("/")[0] if field == "ip" else getattr(d, field)
        if old != new:
            changed.append(field)
            if field in _AUDITED:
                before[field], after[field] = _jsonable(old), _jsonable(new)
            setattr(d, field, new)
    comm_changed = False
    if "comm_key" in body.model_fields_set:
        new_key = str(body.comm_key) if body.comm_key else None
        if new_key != d.comm_key:
            d.comm_key = new_key
            comm_changed = True
    if ("ip" in changed or "port" in changed or "transport" in changed or comm_changed) and d.circuit_open_until:
        d.circuit_open_until = None  # a new address / key deserves an immediate retry
        d.consecutive_failures = 0
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError("CONFLICT", "A device with this name or address already exists") from None
    audit(
        session, action="device.update", actor=actor, entity_type="device", entity_id=d.id,
        before=before, after={**after, "fields": changed, "comm_key_changed": comm_changed},
    )  # fmt: skip
    if "enabled" in changed:
        audit(
            session, action="device.enable" if d.enabled else "device.disable", actor=actor, entity_type="device", entity_id=d.id
        )
    await session.commit()
    counts = await _unmatched_counts(session)
    return device_out(d, counts.get(d.id, 0), utcnow())


async def delete_device(session: AsyncSession, actor: Principal, device_id: uuid.UUID) -> None:
    d = await get_or_404(session, device_id, lock=True)
    in_use = (await session.execute(select(DevicePunch.id).where(DevicePunch.device_id == d.id).limit(1))).first() is not None
    if not in_use:
        in_use = (
            await session.execute(
                select(Attendance.id)
                .where((Attendance.check_in_device_id == d.id) | (Attendance.check_out_device_id == d.id))
                .limit(1)
            )
        ).first() is not None
    if in_use:
        raise AppError("IN_USE", "This device has recorded punches and cannot be deleted. Disable it instead.")
    await session.execute(delete(DeviceUser).where(DeviceUser.device_id == d.id))  # RESTRICT: clear the user list first
    await session.execute(delete(DeviceSync).where(DeviceSync.device_id == d.id))
    audit(
        session,
        action="device.delete",
        actor=actor,
        entity_type="device",
        entity_id=d.id,
        before={"name": d.name, "ip": str(d.ip).split("/")[0], "port": d.port, "transport": d.transport, "serial_number": d.serial_number},
    )
    await session.delete(d)
    await session.commit()


async def accept_identity(session: AsyncSession, actor: Principal, device_id: uuid.UUID) -> dict[str, Any]:
    d = await get_or_404(session, device_id, lock=True)
    old_serial = d.serial_number
    d.serial_number = None  # the next successful sync pins whatever machine answers
    if d.last_error_code == "DEVICE_IDENTITY_CHANGED":
        d.last_error_code = None
        d.last_error_at = None
    d.consecutive_failures = 0
    d.circuit_open_until = None
    audit(
        session, action="device.identity_accepted", actor=actor, entity_type="device", entity_id=d.id,
        before={"serial_number": old_serial}, after={"ip": str(d.ip).split("/")[0], "port": d.port},
    )  # fmt: skip
    await session.commit()
    counts = await _unmatched_counts(session)
    return device_out(d, counts.get(d.id, 0), utcnow())


# ---------------------------------------------------------------- alerts to the people who run the devices
async def permission_holders(session: AsyncSession, perms: tuple[str, ...]) -> list[uuid.UUID]:
    q = (
        select(User.id)
        .join(RolePermission, RolePermission.role_id == User.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(Permission.code.in_(perms), User.status == "active", User.deleted_at.is_(None))
    )
    return list((await session.execute(q)).scalars().unique())


async def alert_holders(
    session: AsyncSession, redis: Redis | None, *, key: str, title: str, body: str, entity_id: uuid.UUID | None,
    perms: tuple[str, ...] = ("device.manage", "device.map"),
) -> bool:
    """admin_alert push (no personal data) to the holders of `perms`; at most once a day per `key` (Redis down = send)."""
    if redis is not None:
        try:
            if not await redis.set(f"ec:device:alert:{key}", "1", nx=True, ex=86400):
                return False
        except Exception:  # noqa: BLE001
            pass
    ids = await permission_holders(session, perms)
    if ids:
        await notify(session, ids, type="admin_alert", entity_id=entity_id, title=title, body=body, high_priority=True)
    return True


# ---------------------------------------------------------------- job queue (API side)
async def ensure_worker(redis: Redis) -> None:
    """503 TEMPORARILY_UNAVAILABLE when Redis or the worker (heartbeat older than 180 s) is down."""
    unavailable = AppError(
        "TEMPORARILY_UNAVAILABLE", "The background worker is not available. Try again shortly.", headers={"Retry-After": "30"}
    )
    try:
        last = _parse(await redis.get(HEARTBEAT_KEY))
    except Exception:  # noqa: BLE001
        raise unavailable from None
    if last is None or (utcnow() - last).total_seconds() > HEARTBEAT_STALE_S:
        raise unavailable


async def push_job(redis: Redis, job: dict[str, Any]) -> None:
    try:
        await redis.lpush(QUEUE_TEST_KEY if job.get("kind") == "test" else QUEUE_KEY, json.dumps(job))
    except Exception:  # noqa: BLE001
        raise AppError(
            "TEMPORARILY_UNAVAILABLE", "The job queue is not available. Try again shortly.", headers={"Retry-After": "30"}
        ) from None


async def pop_job(redis: Redis, queue: str = QUEUE_KEY) -> dict[str, Any] | None:
    raw = await redis.rpop(queue)
    if not raw:
        return None
    try:
        job = json.loads(raw)
        return job if isinstance(job, dict) else None
    except ValueError:
        return None


async def _open_sync(session: AsyncSession, device_id: uuid.UUID) -> DeviceSync | None:
    q = (
        select(DeviceSync)
        .where(DeviceSync.device_id == device_id, DeviceSync.kind != "test", DeviceSync.status.in_(("queued", "running")))
        .order_by(DeviceSync.queued_at.desc())
        .limit(1)
    )
    return (await session.execute(q)).scalar_one_or_none()


async def request_sync(session: AsyncSession, redis: Redis, actor: Principal, device_id: uuid.UUID) -> DeviceSync:
    await ensure_worker(redis)
    d = await get_or_404(session, device_id)
    if not d.enabled:
        raise AppError("DEVICE_DISABLED", "The device is disabled")
    existing = await _open_sync(session, d.id)
    if existing is not None:
        return existing
    await rate_limit.enforce(redis, [(f"rl:device-sync:{d.id}", 1, 60)], fail_closed=True)
    row = DeviceSync(device_id=d.id, kind="manual", requested_by=actor.id, status="queued")
    session.add(row)
    await session.commit()
    await push_job(redis, {"kind": "sync", "sync_id": str(row.id), "device_id": str(d.id)})
    return row


async def request_sync_all(session: AsyncSession, redis: Redis, actor: Principal) -> list[tuple[uuid.UUID, uuid.UUID]]:
    await ensure_worker(redis)
    ids = (
        (await session.execute(select(Device.id).where(Device.enabled.is_(True)).order_by(func.lower(Device.name))))
        .scalars()
        .all()
    )
    out = []
    for did in ids:
        try:
            row = await request_sync(session, redis, actor, did)
        except AppError as exc:
            if exc.code == "RATE_LIMITED":
                continue  # synced within the last minute
            raise
        out.append((did, row.id))
    return out


async def request_test(
    session: AsyncSession, redis: Redis, actor: Principal, *, device_id: uuid.UUID | None, params: Any | None
) -> DeviceSync:
    await ensure_worker(redis)
    await rate_limit.enforce(redis, [(f"rl:device-test:{actor.id}", 6, 60)], fail_closed=True)
    if device_id is not None:
        d = await get_or_404(session, device_id)
        check_address(str(d.ip).split("/")[0], d.port)
        row = DeviceSync(device_id=d.id, kind="test", requested_by=actor.id, status="queued")
        session.add(row)
        audit(
            session, action="device.test", actor=actor, entity_type="device", entity_id=d.id,
            after={"ip": str(d.ip).split("/")[0], "port": d.port, "transport": d.transport},
        )  # fmt: skip
        await session.commit()
        await push_job(redis, {"kind": "test", "sync_id": str(row.id), "device_id": str(d.id)})
        return row
    check_address(params.ip, params.port)
    row = DeviceSync(device_id=None, kind="test", requested_by=actor.id, status="queued")
    session.add(row)
    await session.commit()
    secret = json.dumps({"ip": params.ip, "port": params.port, "transport": params.transport, "comm_key": params.comm_key or 0})
    try:
        await redis.set(TESTJOB_KEY.format(row.id), encrypt_bytes(secret.encode()).decode(), ex=TESTJOB_TTL_S)
    except Exception:  # noqa: BLE001
        raise AppError("TEMPORARILY_UNAVAILABLE", "The job queue is not available. Try again shortly.") from None
    await push_job(redis, {"kind": "test", "sync_id": str(row.id), "device_id": None})
    audit(
        session, action="device.test", actor=actor, entity_type="device",
        after={"unsaved": True, "ip": params.ip, "port": params.port, "transport": params.transport},
    )  # fmt: skip
    await session.commit()
    return row


async def get_sync(session: AsyncSession, actor: Principal, sync_id: uuid.UUID) -> dict[str, Any]:
    row = await session.get(DeviceSync, sync_id)
    if row is None:
        raise AppError("NOT_FOUND", "Sync not found")
    if row.kind == "test" and not actor.has("device.manage"):
        raise AppError("FORBIDDEN", "You do not have permission to perform this action")
    return (await sync_out_many(session, [row]))[0]


async def list_syncs(session: AsyncSession, device_id: uuid.UUID, params: Any) -> dict[str, Any]:
    from ..core.pagination import make_page

    await get_or_404(session, device_id)
    total = (
        await session.execute(select(func.count()).select_from(DeviceSync).where(DeviceSync.device_id == device_id))
    ).scalar_one()
    q = (
        select(DeviceSync)
        .where(DeviceSync.device_id == device_id)
        .order_by(DeviceSync.queued_at.desc(), DeviceSync.id)
        .limit(params.page_size)
        .offset(params.offset)
    )
    rows = list((await session.execute(q)).scalars())
    return make_page(await sync_out_many(session, rows), params, total)


# ---------------------------------------------------------------- health block (GET /admin/system/health)
async def health_block(session: AsyncSession) -> dict[str, Any]:
    now = utcnow()
    try:
        devices = (await session.execute(select(Device).where(Device.enabled.is_(True)))).scalars().all()
        cfg = await device_settings.load(session)
        tz = att.office_tz(await att.load_settings(session))
    except Exception:  # noqa: BLE001 - e.g. a database without the 0030 tables
        return {"ok": True, "enabled": 0, "failing": 0, "offline": 0, "last_success_at": None}
    local = now.astimezone(tz).strftime("%H:%M")
    in_window = cfg.sync_window_start <= local <= cfg.sync_window_end
    failing = offline = 0
    stale = False
    last_ok: dt.datetime | None = None
    for d in devices:
        st = device_status(d, now)
        failing += st == "failing"
        offline += st == "offline"
        ok_at = as_utc(d.last_success_at)
        if ok_at is not None and (last_ok is None or ok_at > last_ok):
            last_ok = ok_at
        ref = ok_at or as_utc(d.created_at)
        if in_window and ref is not None and (now - ref) > dt.timedelta(minutes=60):
            stale = True
    return {
        "ok": not stale,
        "enabled": len(devices),
        "failing": failing,
        "offline": offline,
        "last_success_at": iso_z(last_ok) if last_ok else None,
    }
