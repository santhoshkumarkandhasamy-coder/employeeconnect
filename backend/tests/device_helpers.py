"""Shared fixtures for the attendance-device tests (plan 18): a controllable clock, the fake ZK server with the read-only
assertion after every test, and helpers to create devices, device users and punches.

Office timezone is Asia/Kolkata (UTC+5:30, no DST). NOW = Wednesday 2026-10-21 09:00 IST; the seeded shift is 09:30-18:30 with
10 minutes grace, so a check-in after 09:40 is late."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.models import Device, DevicePunch, DeviceUser, User
from app.services import device_sync
from app.services import devices as devices_svc
from app.services.notifications import OUTBOX
from tests.conftest import USERS
from tests.fakes.zk_server import FakeZK

IST = dt.timedelta(hours=5, minutes=30)
NOW = dt.datetime(2026, 10, 21, 3, 30, tzinfo=dt.UTC)  # 09:00 IST
TODAY = dt.date(2026, 10, 21)
_CLOCK_MODULES = ("attendance", "device_attendance", "device_sync", "devices", "late", "leave", "device_matching")


def local(day: dt.date | str, hm: str, sec: int = 0) -> dt.datetime:
    """Naive office wall-clock time, like the device reports it."""
    d = dt.date.fromisoformat(day) if isinstance(day, str) else day
    h, m = hm.split(":")
    return dt.datetime(d.year, d.month, d.day, int(h), int(m), sec)


def to_utc(naive_local: dt.datetime) -> dt.datetime:
    return (naive_local - IST).replace(tzinfo=dt.UTC)


class Clock:
    def __init__(self, monkeypatch):
        self.now = NOW
        self._mp = monkeypatch
        for mod in _CLOCK_MODULES:
            monkeypatch.setattr(f"app.services.{mod}.utcnow", lambda: self.now)

    def set(self, when: dt.datetime) -> None:
        self.now = when

    def device_local(self) -> dt.datetime:
        return (self.now + IST).replace(tzinfo=None, microsecond=0)


@pytest.fixture
def clk(monkeypatch):
    OUTBOX.clear()
    return Clock(monkeypatch)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(device_sync, "RETRY_DELAYS", (0.0, 0.0))


@pytest.fixture
def allow_loopback(monkeypatch):
    """The fake server listens on 127.0.0.1, which the address guard refuses in production."""
    monkeypatch.setattr(devices_svc, "address_problem", lambda ip: None)


@pytest.fixture
def fast_client(monkeypatch):
    """Short protocol timeouts so failure tests do not wait 10 seconds."""
    from app.integrations.zk import ZKClient

    monkeypatch.setattr(
        device_sync,
        "make_client",
        lambda h, p, t, k: ZKClient(h, p, t, k, connect_timeout=1, packet_timeout=0.4, session_timeout=5),
    )


@pytest.fixture
async def fake_zk(clk, allow_loopback, fast_client):
    servers: list[FakeZK] = []

    async def _make(**kw) -> FakeZK:
        kw.setdefault("clock_fn", clk.device_local)
        f = FakeZK(**kw)
        await f.start()
        servers.append(f)
        return f

    yield _make
    for f in servers:
        f.assert_only_allowed()  # read-only proof after every test
        await f.stop()


async def add_device(
    db, fake: FakeZK | None = None, *, name="Main entrance", ingest_from=TODAY - dt.timedelta(days=3), **kw
) -> Device:
    n = (await db.execute(select(Device))).scalars().all()
    d = Device(
        name=name,
        ip=kw.pop("ip", "127.0.0.1"),
        port=kw.pop("port", fake.port if fake else 4370 + len(n)),
        transport=kw.pop("transport", fake.transport if fake else "tcp"),
        comm_key=kw.pop("comm_key", str(fake.comm_key) if fake and fake.comm_key else None),
        ingest_from=ingest_from,
        **kw,
    )
    db.add(d)
    await db.commit()
    return d


async def add_device_user(db, device: Device, device_user_id: str, *, user: str | None = None, method="auto", **kw) -> DeviceUser:
    uid = None
    if user:
        uid = (await db.execute(select(User.id).where(User.employee_id == USERS.get(user, user)))).scalar_one()
    du = DeviceUser(
        device_id=device.id, device_user_id=device_user_id, user_id=uid, match_method=method if uid or method in ("ignored", "unmatched") else "unmatched",
        **kw,
    )  # fmt: skip
    db.add(du)
    await db.commit()
    return du


async def add_punch(
    db, device: Device, device_user_id: str, when: dt.datetime, *, state=0, verify=1, quarantine=None
) -> DevicePunch:
    """`when` is the device wall-clock time (naive office time); no offset."""
    p = DevicePunch(
        device_id=device.id, device_user_id=device_user_id, device_time=when, punch_at=to_utc(when), offset_applied_s=0,
        state=state, verify_mode=verify, quarantine=quarantine,
    )  # fmt: skip
    db.add(p)
    await db.flush()
    return p


def new_id() -> uuid.UUID:
    return uuid.uuid4()
