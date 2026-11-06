"""Device sync (plan 18 section 4): the worker pulls users and punches read-only, ingests them idempotently and recomputes attendance.

One connection per device, ever: a Redis lock `device-lock:{id}` (TTL 180 s) plus an in-process lock; up to 3 devices run
concurrently. Network I/O happens in `_fetch` (no database session held); the result is applied in short transactions.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.audit import audit
from ..core.clock import as_utc, utcnow
from ..core.crypto import decrypt_bytes
from ..core.errors import AppError
from ..integrations.zk import (
    DeviceError,
    DeviceIdentityChanged,
    DeviceProtocolError,
    ForbiddenCommand,
    ZKClient,
    ZKPunch,
    ZKUser,
)
from ..models import (
    Device,
    DevicePunch,
    DevicePunchOutcome,
    DeviceSync,
    DeviceUser,
    Permission,
    RolePermission,
    User,
)
from . import attendance as att
from . import device_attendance, device_matching, device_settings
from . import devices as devices_svc
from .notifications import notify

log = logging.getLogger("app.device_sync")

CIRCUIT_THRESHOLD = 5
CIRCUIT_MINUTES = (15, 30, 60)
BUSY_NOT_COUNTED = 3
MAX_PARALLEL = 3
LOCK_TTL_S = 180
USERS_REFRESH = dt.timedelta(hours=1)
MIN_VALID_YEAR = 2015
RETRY_DELAYS: tuple[float, ...] = (3.0, 10.0)  # patched to zero in tests
STUCK_AFTER = dt.timedelta(minutes=10)
_locks: dict[uuid.UUID, asyncio.Lock] = {}


def make_client(host: str, port: int, transport: str, comm_key: int) -> ZKClient:
    """Factory seam (tests may swap it); the address guard has already been applied by the caller."""
    return ZKClient(host, port, transport, comm_key)


# ---------------------------------------------------------------- fetch (network only, no database)
@dataclass
class Snapshot:
    device_id: uuid.UUID | None
    ip: str
    port: int
    transport: str
    comm_key: int
    serial: str | None
    tz: ZoneInfo
    last_log_count: int | None
    last_user_count: int | None
    last_users_read_at: dt.datetime | None
    last_ok: bool
    mode: str = "sync"  # sync | test


@dataclass
class Fetched:
    serial: str | None
    model: str | None
    platform: str | None
    firmware: str | None
    device_time: dt.datetime | None  # naive
    offset_s: int | None
    user_count: int
    log_count: int
    users: list[ZKUser] | None
    punches: list[ZKPunch] | None
    skipped_records: int
    log_decreased: bool


def _snapshot(d: Device, office_tz: ZoneInfo, mode: str = "sync") -> Snapshot:
    try:
        tz = ZoneInfo(d.timezone) if d.timezone else office_tz
    except Exception:  # noqa: BLE001
        tz = office_tz
    return Snapshot(
        device_id=d.id, ip=str(d.ip).split("/")[0], port=d.port, transport=d.transport, comm_key=int(d.comm_key or 0),
        serial=d.serial_number, tz=tz, last_log_count=d.last_log_count, last_user_count=d.last_user_count,
        last_users_read_at=as_utc(d.last_users_read_at), last_ok=d.last_error_code in (None, "DEVICE_CLOCK_INVALID"), mode=mode,
    )  # fmt: skip


async def _fetch(snap: Snapshot) -> Fetched:
    devices_svc.check_address(snap.ip)  # defence in depth: the row may have been edited in the database
    async with make_client(snap.ip, snap.port, snap.transport, snap.comm_key) as zk:
        opts = await zk.get_options()
        serial = opts.get("SerialNumber") or None
        if snap.serial and serial != snap.serial:
            raise DeviceIdentityChanged("a different machine answered")  # nothing else is read
        try:
            version = await zk.get_version()
        except DeviceProtocolError:
            version = None
        t0 = utcnow()
        dev_time = await zk.get_time()
        t1 = utcnow()
        offset = None
        if dev_time is not None:
            dev_utc = dev_time.replace(tzinfo=snap.tz).astimezone(dt.UTC)
            offset = round((dev_utc - (t0 + (t1 - t0) / 2)).total_seconds())
        sizes = await zk.get_sizes()
        decreased = snap.last_log_count is not None and sizes.records < snap.last_log_count
        out = Fetched(
            serial=serial, model=opts.get("DeviceName"), platform=opts.get("Platform"), firmware=version, device_time=dev_time,
            offset_s=offset, user_count=sizes.users, log_count=sizes.records, users=None, punches=None, skipped_records=0,
            log_decreased=decreased,
        )  # fmt: skip
        if snap.mode == "test":
            return out
        users_due = (
            snap.last_user_count != sizes.users
            or snap.last_users_read_at is None
            or utcnow() - snap.last_users_read_at >= USERS_REFRESH
        )
        if users_due:
            out.users = await zk.get_users(sizes.users) if sizes.users else []
        skip_logs = snap.last_log_count is not None and sizes.records == snap.last_log_count and snap.last_ok
        if not skip_logs and sizes.records > 0:
            out.punches, out.skipped_records = await zk.get_attendance(sizes.records)
            if out.users is None and any(p.user_id is None for p in out.punches):  # 8-byte format: needs uid -> user id
                out.users = await zk.get_users(sizes.users) if sizes.users else []
        elif not skip_logs:
            out.punches = []
        return out


# ---------------------------------------------------------------- clock
def classify_clock(f: Fetched, dcfg: device_settings.DeviceCfg, correction: str) -> tuple[str, int]:
    """-> (clock_status, offset_to_apply_s)."""
    if f.device_time is None or f.device_time.year < MIN_VALID_YEAR or f.offset_s is None:
        return "invalid", 0
    off = abs(f.offset_s)
    if off <= dcfg.drift_tolerance_s:
        return "ok", 0
    if off <= dcfg.drift_invalid_s:
        return "drift", f.offset_s if correction == "auto" else 0
    return "invalid", 0


# ---------------------------------------------------------------- DB phase helpers
def _insert(session: AsyncSession, table):
    d = session.get_bind().dialect.name
    return (postgresql.insert if d == "postgresql" else sqlite.insert)(table)


async def upsert_users(
    session: AsyncSession, device: Device, users: list[ZKUser], now: dt.datetime, prefix: str
) -> list[DeviceUser]:
    """Upsert the device's user list (PIN never reaches here), flag the absent ones, auto-match. Returns newly matched rows."""
    existing = {
        r.device_user_id: r
        for r in (await session.execute(select(DeviceUser).where(DeviceUser.device_id == device.id))).scalars()
    }
    seen: set[str] = set()
    for u in users:
        if not 1 <= len(u.user_id) <= 24 or u.user_id in seen:
            continue
        seen.add(u.user_id)
        row = existing.get(u.user_id)
        if row is None:
            row = DeviceUser(device_id=device.id, device_user_id=u.user_id, match_method="unmatched", first_seen_at=now)
            session.add(row)
            existing[u.user_id] = row
        row.device_uid, row.name, row.card, row.is_device_admin = u.uid, u.name or None, u.card, u.is_admin
        row.present_on_device, row.last_seen_at = True, now
    for key, row in existing.items():
        if key not in seen and row.present_on_device:
            row.present_on_device = False
    await session.flush()
    idx = await device_matching.build_index(session, prefix)
    return await device_matching.auto_match_rows(session, list(existing.values()), idx)


async def ingest_punches(
    session: AsyncSession, device: Device, punches: list[ZKPunch], users: dict[int, str], *, sync_id: uuid.UUID,
    tz: ZoneInfo, office: ZoneInfo, status: str, applied: int, now: dt.datetime, dcfg: device_settings.DeviceCfg,
) -> tuple[list[tuple[int, str, dt.datetime, str | None]], int, int]:  # fmt: skip
    """Insert new punches with ON CONFLICT DO NOTHING (never an id). Returns (inserted rows, quarantined count, fetched)."""
    rows: dict[tuple[str, dt.datetime], dict[str, Any]] = {}
    for p in punches:
        uid = p.user_id or (users.get(p.uid) if p.uid is not None else None) or (str(p.uid) if p.uid is not None else None)
        if not uid or len(uid) > 24:
            continue
        quarantine = None
        off = applied
        local = p.time.replace(tzinfo=tz)
        punch_at = local.astimezone(dt.UTC)
        if p.time.year < MIN_VALID_YEAR or status == "invalid":
            quarantine, off = "clock_invalid", 0
        else:
            punch_at -= dt.timedelta(seconds=off)
            if punch_at > now + dt.timedelta(seconds=dcfg.future_tolerance_s):
                quarantine = "future"
        if punch_at.astimezone(office).date() < device.ingest_from:
            continue
        key = (uid, p.time)
        if key in rows:
            continue
        rows[key] = {
            "device_id": device.id, "device_user_id": uid, "device_uid": p.uid, "device_time": p.time, "punch_at": punch_at,
            "offset_applied_s": off, "state": p.state, "verify_mode": p.verify, "quarantine": quarantine, "sync_id": sync_id,
        }  # fmt: skip
    values = list(rows.values())
    inserted: list[tuple[int, str, dt.datetime, str | None]] = []
    for i in range(0, len(values), 500):
        stmt = (
            _insert(session, DevicePunch)
            .values(values[i : i + 500])
            .on_conflict_do_nothing(index_elements=["device_id", "device_user_id", "device_time"])
            .returning(DevicePunch.id, DevicePunch.device_user_id, DevicePunch.punch_at, DevicePunch.quarantine)
        )
        inserted += [(r[0], r[1], r[2], r[3]) for r in (await session.execute(stmt)).all()]
    return inserted, sum(1 for r in inserted if r[3]), len(values)


async def _admin_ids(session: AsyncSession) -> list[uuid.UUID]:
    q = (
        select(User.id)
        .join(RolePermission, RolePermission.role_id == User.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(Permission.code == "device.manage", User.status == "active", User.deleted_at.is_(None))
    )
    return list((await session.execute(q)).scalars().unique())


async def alert_admins(
    session: AsyncSession, redis: Redis | None, device: Device, event: str, title: str, now: dt.datetime
) -> bool:
    """admin_alert push to device.manage holders; device name only. At most once per event and day per device."""
    allowed = True
    if redis is not None:
        try:
            allowed = bool(await redis.set(f"ec:device:alert:{device.id}:{event}", "1", nx=True, ex=86400))
        except Exception:  # noqa: BLE001
            allowed = True
    last = as_utc(device.last_alert_at)
    if redis is None and last is not None and now - last < dt.timedelta(hours=24):
        allowed = False
    if not allowed:
        return False
    ids = await _admin_ids(session)
    device.last_alert_at = now
    if ids:
        await notify(session, ids, type="admin_alert", entity_id=device.id, title=title, body=device.name, high_priority=True)
    return True


def _circuit_minutes(failures: int) -> int:
    return CIRCUIT_MINUTES[min(max(failures - CIRCUIT_THRESHOLD, 0), len(CIRCUIT_MINUTES) - 1)]


# ---------------------------------------------------------------- finishing a run
async def _finish_failed(
    sm: async_sessionmaker[AsyncSession], redis: Redis | None, device_id: uuid.UUID, sync_id: uuid.UUID, code: str, kind: str
) -> None:
    now = utcnow()
    async with sm() as s:
        d = await s.get(Device, device_id)
        row = await s.get(DeviceSync, sync_id)
        if d is None or row is None:
            return
        row.status, row.error_code, row.finished_at = "failed", code, now
        counted = True
        if code == "DEVICE_BUSY":
            n = 1
            if redis is not None:
                try:
                    n = int(await redis.incr(f"ec:device:busy:{d.id}"))
                    await redis.expire(f"ec:device:busy:{d.id}", 3600)
                except Exception:  # noqa: BLE001
                    n = BUSY_NOT_COUNTED + 1
            counted = n > BUSY_NOT_COUNTED
        d.last_sync_at, d.last_error_code, d.last_error_at = now, code, now
        opened = False
        if counted:
            d.consecutive_failures += 1
            if d.consecutive_failures >= CIRCUIT_THRESHOLD:
                d.circuit_open_until = now + dt.timedelta(minutes=_circuit_minutes(d.consecutive_failures))
                opened = True
        audit(
            s,
            action="device.sync_failed",
            entity_type="device",
            entity_id=d.id,
            after={"error_code": code, "kind": kind},
            outcome="failure",
        )
        if opened:
            audit(
                s, action="device.circuit_open", entity_type="device", entity_id=d.id, after={"failures": d.consecutive_failures}
            )
            await alert_admins(s, redis, d, "offline", "Attendance device offline", now)
        if code == "DEVICE_IDENTITY_CHANGED":
            audit(s, action="device.identity_changed", entity_type="device", entity_id=d.id)
            await alert_admins(s, redis, d, "identity", "Device identity changed", now)
        await s.commit()


async def _apply_success(
    sm: async_sessionmaker[AsyncSession], redis: Redis | None, device_id: uuid.UUID, sync_id: uuid.UUID, f: Fetched, kind: str
) -> None:
    now = utcnow()
    async with sm() as s:
        d = await s.get(Device, device_id)
        row = await s.get(DeviceSync, sync_id)
        if d is None or row is None:
            return
        cfg = await att.load_settings(s)
        dcfg = await device_settings.load(s)
        office = att.office_tz(cfg)
        tz = _snapshot(d, office).tz
        status, applied = classify_clock(f, dcfg, d.clock_correction)
        if f.serial and d.serial_number is None:
            d.serial_number = f.serial
        d.model, d.platform, d.firmware = f.model or d.model, f.platform or d.platform, f.firmware or d.firmware
        prev_status = d.clock_status
        d.last_clock_offset_s, d.clock_status = f.offset_s, status
        if status != "ok":
            if prev_status != status:
                audit(
                    s,
                    action="device.clock_drift",
                    entity_type="device",
                    entity_id=d.id,
                    after={"status": status, "offset_s": f.offset_s},
                )
            await alert_admins(s, redis, d, "clock", "Device clock is wrong", now)  # once a day per device
        if f.log_decreased:
            audit(
                s,
                action="device.log_count_decreased",
                entity_type="device",
                entity_id=d.id,
                after={"before": d.last_log_count, "after": f.log_count},
            )
            await alert_admins(s, redis, d, "logs", "Device logs were cleared", now)
        newly: list[DeviceUser] = []
        if f.users is not None:
            newly = await upsert_users(s, d, f.users, now, dcfg.match_prefix)
            d.last_users_read_at = now
        d.last_user_count, d.last_log_count = f.user_count, f.log_count
        inserted: list[tuple[int, str, dt.datetime, str | None]] = []
        fetched = quarantined = 0
        if f.punches:
            uid_map = {u.uid: u.user_id for u in (f.users or [])}
            if f.users is None:  # uid lookup for the 8-byte format from the stored list
                rows = (
                    await s.execute(select(DeviceUser.device_uid, DeviceUser.device_user_id).where(DeviceUser.device_id == d.id))
                ).all()
                uid_map = {a: b for a, b in rows if a is not None}
            inserted, quarantined, fetched = await ingest_punches(
                s,
                d,
                f.punches,
                uid_map,
                sync_id=sync_id,
                tz=tz,
                office=office,
                status=status,
                applied=applied,
                now=now,
                dcfg=dcfg,
            )
        d.last_sync_at, d.last_success_at = now, now
        d.consecutive_failures, d.circuit_open_until = 0, None
        d.last_error_code = "DEVICE_CLOCK_INVALID" if status == "invalid" else None
        d.last_error_at = now if status == "invalid" else None
        if redis is not None:
            try:
                await redis.delete(f"ec:device:busy:{d.id}")
            except Exception:  # noqa: BLE001
                pass
        row.status, row.finished_at, row.error_code = "ok", now, d.last_error_code
        row.records_on_device, row.users_on_device = f.log_count, f.user_count
        row.fetched, row.inserted, row.quarantined, row.clock_offset_s = fetched, len(inserted), quarantined, f.offset_s
        await s.commit()
        seen: set = set()
        days = await device_attendance.apply_new_punches(s, d.id, inserted, cfg, dcfg, seen)
        if newly:
            days += await device_attendance.recompute_device_users(
                s, [(r.device_id, r.device_user_id) for r in newly], cfg=cfg, dcfg=dcfg, done=seen
            )
        row = await s.get(DeviceSync, sync_id)
        row.days_recomputed = days  # type: ignore[union-attr]
        if kind != "schedule" or inserted or quarantined or newly:
            audit(
                s, action="device.sync", entity_type="device", entity_id=d.id,
                after={"kind": kind, "fetched": fetched, "inserted": len(inserted), "quarantined": quarantined, "days_recomputed": days},
            )  # fmt: skip
        await s.commit()


# ---------------------------------------------------------------- one sync run
async def _acquire(redis: Redis | None, device_id: uuid.UUID) -> bool:
    if redis is None:
        return True
    try:
        return bool(await redis.set(f"device-lock:{device_id}", "1", nx=True, ex=LOCK_TTL_S))
    except Exception:  # noqa: BLE001 - Redis down: the in-process lock still holds
        return True


async def _release(redis: Redis | None, device_id: uuid.UUID) -> None:
    if redis is not None:
        try:
            await redis.delete(f"device-lock:{device_id}")
        except Exception:  # noqa: BLE001
            pass


async def run_sync(
    sm: async_sessionmaker[AsyncSession], redis: Redis | None, device_id: uuid.UUID, *, kind: str = "schedule",
    sync_id: uuid.UUID | None = None, requested_by: uuid.UUID | None = None,
) -> uuid.UUID:  # fmt: skip
    """Run (or skip) one sync of one device; returns the DeviceSync id. Never raises for device problems."""
    lock = _locks.setdefault(device_id, asyncio.Lock())
    async with sm() as s:
        row = await s.get(DeviceSync, sync_id) if sync_id else None
        if row is None:
            row = DeviceSync(device_id=device_id, kind=kind, requested_by=requested_by, status="queued")
            s.add(row)
            await s.flush()
        sync_id = row.id
        await s.commit()

    def skipped(code: str | None) -> Any:
        async def _do() -> None:
            async with sm() as s2:
                r = await s2.get(DeviceSync, sync_id)
                if r is not None and r.status == "queued":
                    r.status, r.error_code, r.finished_at = "skipped", code, utcnow()
                    await s2.commit()

        return _do()

    if lock.locked() or not await _acquire(redis, device_id):
        await skipped(None)  # another run owns the single connection
        return sync_id
    try:
        async with lock:
            async with sm() as s:
                d = await s.get(Device, device_id)
                row = await s.get(DeviceSync, sync_id)
                if d is None or row is None or row.status != "queued":
                    return sync_id
                now = utcnow()
                if not d.enabled:
                    await skipped("DEVICE_DISABLED")
                    return sync_id
                until = as_utc(d.circuit_open_until)
                if until is not None and until > now and row.kind in ("schedule", "reconcile"):
                    await skipped("CIRCUIT_OPEN")
                    return sync_id
                kind = row.kind
                row.status, row.started_at = "running", now
                snap = _snapshot(d, att.office_tz(await att.load_settings(s)))
                await s.commit()
            err: DeviceError | None = None
            fetched: Fetched | None = None
            for attempt in range(len(RETRY_DELAYS) + 1):
                if redis is not None:
                    try:
                        await redis.expire(f"device-lock:{device_id}", LOCK_TTL_S)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    fetched = await _fetch(snap)
                    err = None
                    break
                except DeviceError as exc:
                    err = exc
                    if not exc.retryable or attempt >= len(RETRY_DELAYS):
                        break
                    await asyncio.sleep(RETRY_DELAYS[attempt])
                except ForbiddenCommand:
                    log.critical("a command outside the read-only allow-list was blocked for device %s", device_id)
                    err = DeviceProtocolError("blocked command")
                    break
                except AppError:
                    err = DeviceProtocolError("address not allowed")
                    await _finish_failed(sm, redis, device_id, sync_id, "DEVICE_UNREACHABLE", kind)
                    return sync_id
                except Exception:  # noqa: BLE001 - a bug must not leave the row 'running'
                    log.exception("unexpected error while syncing device %s", device_id)
                    err = DeviceProtocolError("unexpected error")
                    break
            if fetched is None:
                await _finish_failed(sm, redis, device_id, sync_id, (err or DeviceProtocolError()).code, kind)
            else:
                try:
                    await _apply_success(sm, redis, device_id, sync_id, fetched, kind)
                except Exception:  # noqa: BLE001
                    log.exception("applying the sync of device %s failed", device_id)
                    await _finish_failed(sm, redis, device_id, sync_id, "DEVICE_PROTOCOL_ERROR", kind)
    finally:
        await _release(redis, device_id)
    return sync_id


# ---------------------------------------------------------------- test connection
async def run_test(
    sm: async_sessionmaker[AsyncSession], redis: Redis | None, sync_id: uuid.UUID, device_id: uuid.UUID | None
) -> None:
    async with sm() as s:
        row = await s.get(DeviceSync, sync_id)
        if row is None or row.status != "queued":
            return
        row.status, row.started_at = "running", utcnow()
        office = att.office_tz(await att.load_settings(s))
        snap: Snapshot | None = None
        if device_id is not None:
            d = await s.get(Device, device_id)
            if d is not None:
                snap = _snapshot(d, office, mode="test")
        elif redis is not None:
            try:
                raw = await redis.get(devices_svc.TESTJOB_KEY.format(sync_id))
                if raw:
                    p = json.loads(decrypt_bytes(raw.encode()).decode())
                    snap = Snapshot(
                        None,
                        p["ip"],
                        int(p["port"]),
                        p["transport"],
                        int(p.get("comm_key") or 0),
                        None,
                        office,
                        None,
                        None,
                        None,
                        False,
                        "test",
                    )
            except Exception:  # noqa: BLE001
                snap = None
        await s.commit()
    code: str | None = None
    result: dict[str, Any] | None = None
    if snap is None:
        code = "DEVICE_TIMEOUT"  # the job expired (120 s) or the device is gone
    else:
        try:
            f = await _fetch(snap)
            result = {
                "serial_number": f.serial, "model": f.model, "firmware": f.firmware, "platform": f.platform,
                "device_time": f.device_time.strftime("%Y-%m-%dT%H:%M:%S") if f.device_time else None,
                "clock_offset_s": f.offset_s, "users_on_device": f.user_count, "records_on_device": f.log_count,
            }  # fmt: skip
        except DeviceError as exc:
            code = exc.code
        except (ForbiddenCommand, AppError):
            code = "DEVICE_PROTOCOL_ERROR"
        except Exception:  # noqa: BLE001
            log.exception("test connection failed unexpectedly")
            code = "DEVICE_PROTOCOL_ERROR"
    async with sm() as s:
        row = await s.get(DeviceSync, sync_id)
        if row is None:
            return
        row.finished_at = utcnow()
        if code is None and result is not None:
            row.status, row.result = "ok", result
            row.records_on_device, row.users_on_device, row.clock_offset_s = (
                result["records_on_device"],
                result["users_on_device"],
                result["clock_offset_s"],
            )
        else:
            row.status, row.error_code = "failed", code
        await s.commit()
    if redis is not None and device_id is None:
        try:
            await redis.delete(devices_svc.TESTJOB_KEY.format(sync_id))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- queue consumer and scheduler tick
async def run_job(sm: async_sessionmaker[AsyncSession], redis: Redis | None, job: dict[str, Any]) -> None:
    kind = job.get("kind")
    try:
        if kind == "sync":
            await run_sync(sm, redis, uuid.UUID(job["device_id"]), kind="manual", sync_id=uuid.UUID(job["sync_id"]))
        elif kind == "test":
            await run_test(sm, redis, uuid.UUID(job["sync_id"]), uuid.UUID(job["device_id"]) if job.get("device_id") else None)
        elif kind == "recompute":
            async with sm() as s:
                await device_attendance.recompute_device_users(s, [(uuid.UUID(d), u) for d, u in job["keys"]])
    except Exception:  # noqa: BLE001
        log.exception("device job %s failed", kind)


async def process_queue(sm: async_sessionmaker[AsyncSession], redis: Redis, limit: int = 20) -> int:
    """Pop and run the queued jobs (Sync now, Test connection, recompute after a mapping change). Devices run concurrently."""
    jobs = []
    for _ in range(limit):
        job = await devices_svc.pop_job(redis)
        if job is None:
            break
        jobs.append(job)
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def _one(j: dict[str, Any]) -> None:
        async with sem:
            await run_job(sm, redis, j)

    await asyncio.gather(*[_one(j) for j in jobs])
    return len(jobs)


def in_sync_window(local_hhmm: str, start: str, end: str) -> bool:
    return start <= local_hhmm <= end if start <= end else (local_hhmm >= start or local_hhmm <= end)


async def tick(sm: async_sessionmaker[AsyncSession], redis: Redis | None, now: dt.datetime | None = None) -> int:
    """Every minute: sync the devices that are due, run orphaned queued jobs, fail runs stuck as 'running'."""
    now = now or utcnow()
    async with sm() as s:
        dcfg = await device_settings.load(s)
        tz = att.office_tz(await att.load_settings(s))
        devices = list((await s.execute(select(Device).where(Device.enabled.is_(True)))).scalars())
        await s.execute(
            update(DeviceSync)
            .where(DeviceSync.status == "running", DeviceSync.started_at < now - STUCK_AFTER)
            .values(status="failed", error_code="DEVICE_TIMEOUT", finished_at=now)
        )
        stale_q = (
            await s.execute(
                select(DeviceSync.id, DeviceSync.device_id, DeviceSync.kind).where(
                    DeviceSync.status == "queued",
                    DeviceSync.queued_at < now - dt.timedelta(seconds=30),
                    DeviceSync.kind != "test",
                )
            )
        ).all()
        await s.commit()
    local = now.astimezone(tz).strftime("%H:%M")
    interval = dt.timedelta(
        minutes=dcfg.sync_interval_minutes if in_sync_window(local, dcfg.sync_window_start, dcfg.sync_window_end)
        else dcfg.off_window_interval_minutes
    )  # fmt: skip
    due: list[uuid.UUID] = []
    for d in devices:
        last = as_utc(d.last_sync_at)
        until = as_utc(d.circuit_open_until)
        if until is not None and until > now:
            continue  # offline: wait for the half-open probe
        if last is None or now - last >= interval - dt.timedelta(seconds=15):
            due.append(d.id)
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def _sched(did: uuid.UUID) -> None:
        async with sem:
            await run_sync(sm, redis, did, kind="schedule")

    async def _orphan(sid: uuid.UUID, did: uuid.UUID) -> None:
        async with sem:
            await run_sync(sm, redis, did, kind="manual", sync_id=sid)

    await asyncio.gather(*[_sched(i) for i in due], *[_orphan(sid, did) for sid, did, _k in stale_q if did is not None])
    return len(due) + len(stale_q)


async def reconcile(sm: async_sessionmaker[AsyncSession], redis: Redis | None) -> int:
    """Daily 00:20: sync every enabled device, then recompute the apply window for every mapped employee."""
    async with sm() as s:
        ids = list((await s.execute(select(Device.id).where(Device.enabled.is_(True)))).scalars())
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def _one(did: uuid.UUID) -> None:
        async with sem:
            await run_sync(sm, redis, did, kind="reconcile")

    await asyncio.gather(*[_one(i) for i in ids])
    async with sm() as s:
        return await device_attendance.recompute_window(s)


# ---------------------------------------------------------------- retention
async def retention(sm: async_sessionmaker[AsyncSession], now: dt.datetime | None = None) -> dict[str, int]:
    now = now or utcnow()
    out = {"punches": 0, "syncs": 0, "names": 0}
    async with sm() as s:
        dcfg = await device_settings.load(s)
        if dcfg.punch_retention_days > 0:
            cutoff = now - dt.timedelta(days=max(dcfg.punch_retention_days, 90))  # the trigger refuses younger rows anyway
            while True:
                ids = list((await s.execute(select(DevicePunch.id).where(DevicePunch.punch_at < cutoff).limit(5000))).scalars())
                if not ids:
                    break
                await s.execute(delete(DevicePunchOutcome).where(DevicePunchOutcome.punch_id.in_(ids)))
                await s.execute(delete(DevicePunch).where(DevicePunch.id.in_(ids)))
                await s.commit()
                out["punches"] += len(ids)
        res = await s.execute(
            delete(DeviceSync).where(
                DeviceSync.queued_at < now - dt.timedelta(days=dcfg.sync_history_retention_days),
                DeviceSync.status.notin_(("queued", "running")),
            )
        )
        out["syncs"] = res.rowcount or 0
        res = await s.execute(
            update(DeviceUser)
            .where(
                DeviceUser.present_on_device.is_(False),
                DeviceUser.last_seen_at < now - dt.timedelta(days=90),
                (DeviceUser.name.is_not(None)) | (DeviceUser.card.is_not(None)),
            )
            .values(name=None, card=None)
            .execution_options(synchronize_session=False)
        )
        out["names"] = res.rowcount or 0
        if any(out.values()):
            audit(s, action="device.retention", entity_type="device", after=dict(out))
        await s.commit()
    return out
