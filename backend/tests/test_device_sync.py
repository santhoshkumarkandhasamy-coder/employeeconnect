"""Device sync (plan 18 section 4): users and punches ingest, matching, clock, quarantine, identity pinning, failures, circuit
breaker, locks, queue, retention. Runs the real sync service against the fake ZK server (TCP and UDP)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from sqlalchemy import func, select, text

from app.core.crypto import encrypt_bytes
from app.integrations import zk
from app.models import (
    Attendance,
    AuditLog,
    Base,
    Device,
    DevicePunch,
    DevicePunchOutcome,
    DeviceSync,
    DeviceUser,
    Setting,
    User,
)
from app.services import device_matching, device_settings, device_sync
from app.services import devices as devices_svc
from app.services.notifications import OUTBOX
from tests.conftest import USERS
from tests.device_helpers import NOW, TODAY, add_device, add_device_user, local
from tests.fakes.zk_server import FakePunch, FakeUser

pytestmark = pytest.mark.usefixtures("seeded")


def _users():
    return [FakeUser(1, "EMP001", "Test Employee", card=987654321, password="4321"), FakeUser(2, "900", "Unknown Person")]


def _punches(*pairs):
    return [FakePunch(uid, local(TODAY, hm), uid=1 if uid == "EMP001" else 2) for uid, hm in pairs]


async def run(sm, redis, dev, kind="manual"):
    sid = await device_sync.run_sync(sm, redis, dev.id, kind=kind)
    async with sm() as s:
        return await s.get(DeviceSync, sid)


async def reload(sm, model, pk):
    async with sm() as s:
        return await s.get(model, pk)


async def scalar(sm, q):
    async with sm() as s:
        return (await s.execute(q)).scalar_one()


# ---------------------------------------------------------------- happy path
@pytest.mark.parametrize("transport", ["tcp", "udp"])
async def test_first_sync_ingests_users_and_punches_and_builds_attendance(fake_zk, db, sessionmaker_, redis, seeded, transport):
    f = await fake_zk(transport=transport, users=_users(), punches=_punches(("EMP001", "08:55"), ("900", "08:56")))
    dev = await add_device(db, f)
    row = await run(sessionmaker_, redis, dev)
    assert (row.status, row.error_code, row.fetched, row.inserted, row.quarantined) == ("ok", None, 2, 2, 0)
    assert (row.records_on_device, row.users_on_device, row.days_recomputed) == (2, 2, 1)
    d = await reload(sessionmaker_, Device, dev.id)
    assert d.serial_number == "SN-TEST-0001" and d.model == "K40" and d.firmware.startswith("Ver 6.60")
    assert (d.clock_status, d.last_clock_offset_s, d.last_error_code, d.consecutive_failures) == ("ok", 0, None, 0)
    async with sessionmaker_() as s:
        dus = {r.device_user_id: r for r in (await s.execute(select(DeviceUser))).scalars()}
        assert dus["EMP001"].match_method == "auto" and dus["EMP001"].user_id == seeded["users"]["employee"]
        assert dus["900"].match_method == "unmatched" and dus["900"].user_id is None
        assert dus["EMP001"].name == "Test Employee" and dus["EMP001"].card == "987654321"
        att = (await s.execute(select(Attendance))).scalars().all()
        assert len(att) == 1
        a = att[0]
        assert a.user_id == seeded["users"]["employee"] and a.check_in_source == "device" and a.check_in_device_id == dev.id
        assert a.check_in_within_fence is True and a.check_in_distance_m is None and a.check_in_selfie_file_id is None
        assert a.minutes_late == 0 and a.status == "present" and a.check_out_at is None and a.app_check_in_at is None
        outs = {o.outcome for o in (await s.execute(select(DevicePunchOutcome))).scalars()}
        assert outs == {"check_in", "unmatched"}
    f.assert_only_allowed()


async def test_secrets_are_not_stored_pin_dropped_name_and_card_encrypted(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")))
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)
    async with sessionmaker_() as s:
        raw = (
            await s.execute(text("SELECT name_enc, card_enc, device_user_id FROM device_users WHERE device_user_id = 'EMP001'"))
        ).one()
        assert b"Test Employee" not in bytes(raw[0]) and b"987654321" not in bytes(raw[1])
        parts = []
        for t in ("device_users", "device_punches", "devices", "device_syncs"):
            parts += [str(r) for r in (await s.execute(text(f"SELECT * FROM {t}"))).all()]
        dump = " ".join(parts)
        assert "4321" not in dump.replace("987654321", "") and "9876" not in dump.replace("987654321", "")


def test_no_biometric_columns_or_tables_exist():
    bad = ("template", "finger", "face", "biometric", "palm")
    for t in Base.metadata.tables.values():
        if t.name.startswith("device"):
            assert not any(b in t.name for b in bad)
            assert not any(any(b in c.name for b in bad) for c in t.columns), t.name
    assert not hasattr(DeviceUser, "password") and not hasattr(DeviceUser, "pin")


async def test_second_sync_is_idempotent_and_skips_unchanged_log(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")))
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)
    f.commands.clear()
    row = await run(sessionmaker_, redis, dev)
    assert row.status == "ok" and row.inserted == 0 and row.fetched in (0, None)
    assert 1503 not in [c for c, _ in f.commands]  # counts unchanged: no table read at all
    assert await scalar(sessionmaker_, select(func.count()).select_from(DevicePunch)) == 1
    # a forced full re-read of the same log (e.g. after a failed run) inserts nothing new
    async with sessionmaker_() as s:
        d = await s.get(Device, dev.id)
        d.last_error_code = "DEVICE_TIMEOUT"
        await s.commit()
    row = await run(sessionmaker_, redis, dev)
    assert row.fetched == 1 and row.inserted == 0
    f.punches.append(FakePunch("EMP001", local(TODAY, "08:58"), uid=1))
    row = await run(sessionmaker_, redis, dev)
    assert row.inserted == 1 and await scalar(sessionmaker_, select(func.count()).select_from(DevicePunch)) == 2


async def test_count_decrease_is_audited_and_alerted_and_never_cleared_by_us(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:50"), ("EMP001", "08:58")))
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)
    OUTBOX.clear()
    f.punches.pop()  # somebody cleared part of the log on the machine
    await run(sessionmaker_, redis, dev)
    async with sessionmaker_() as s:
        acts = [a for a in (await s.execute(select(AuditLog.action))).scalars()]
        assert "device.log_count_decreased" in acts
        assert await s.scalar(select(func.count()).select_from(DevicePunch)) == 2  # our copy is untouched
    assert any(o["payload"]["type"] == "admin_alert" and "logs were cleared" in o["payload"]["title"] for o in OUTBOX)
    assert all(c not in (15, 14) for c, _ in f.commands)


async def test_ingest_from_skips_older_punches(fake_zk, db, sessionmaker_, redis):
    old = FakePunch("EMP001", local(TODAY - dt.timedelta(days=5), "09:00"), uid=1)
    f = await fake_zk(users=_users(), punches=[old, *_punches(("EMP001", "08:55"))])
    dev = await add_device(db, f, ingest_from=TODAY - dt.timedelta(days=1))
    row = await run(sessionmaker_, redis, dev)
    assert row.fetched == 1 and row.inserted == 1


# ---------------------------------------------------------------- clock and quarantine
async def test_future_punch_and_pre_2015_punch_are_quarantined(fake_zk, db, sessionmaker_, redis):
    ancient = FakePunch("EMP001", dt.datetime(2009, 5, 1, 9, 0), uid=1)
    future = FakePunch("EMP001", local(TODAY, "09:30"), uid=1)  # 30 minutes ahead of the 09:00 clock
    f = await fake_zk(users=_users(), punches=[ancient, future, *_punches(("EMP001", "08:55"))])
    dev = await add_device(db, f, ingest_from=dt.date(2009, 1, 1))
    row = await run(sessionmaker_, redis, dev)
    assert row.inserted == 3 and row.quarantined == 2
    async with sessionmaker_() as s:
        qs = sorted(
            p.quarantine for p in (await s.execute(select(DevicePunch).where(DevicePunch.quarantine.is_not(None)))).scalars()
        )
        assert qs == ["clock_invalid", "future"]
        outs = sorted(o.outcome for o in (await s.execute(select(DevicePunchOutcome))).scalars())
        assert outs == ["check_in", "quarantined", "quarantined"]
        assert (await s.execute(select(Attendance))).scalars().one().check_in_at.replace(tzinfo=dt.UTC) == NOW - dt.timedelta(
            minutes=5
        )


async def test_drift_is_corrected_only_after_two_consecutive_syncs_with_the_same_offset(fake_zk, db, sessionmaker_, redis, clk):
    # the machine runs 200 s ahead: a punch it stamps 09:07:00 really happened at 09:03:40
    ahead = lambda: clk.device_local() + dt.timedelta(seconds=200)  # noqa: E731
    f = await fake_zk(users=_users(), clock_fn=ahead, punches=[FakePunch("EMP001", local(TODAY, "08:58"), uid=1)])
    dev = await add_device(db, f)
    row = await run(sessionmaker_, redis, dev)
    assert row.clock_offset_s == 200 and row.quarantined == 1  # first sight of a drifting clock: nothing is corrected, held
    d = await reload(sessionmaker_, Device, dev.id)
    assert (d.clock_status, d.last_clock_offset_s, d.last_clock_reading_offset_s) == ("drift", 200, 200)
    async with sessionmaker_() as s:
        p = (await s.execute(select(DevicePunch))).scalars().one()
        assert (p.quarantine, p.offset_applied_s) == ("clock_changed", 0)
    clk.set(NOW + dt.timedelta(minutes=5))
    f.punches.append(FakePunch("EMP001", local(TODAY, "09:07"), uid=1))
    row = await run(sessionmaker_, redis, dev)  # the same offset twice in a row: corrected from here on
    assert row.inserted == 1 and row.quarantined == 0
    async with sessionmaker_() as s:
        p = (await s.execute(select(DevicePunch).where(DevicePunch.quarantine.is_(None)))).scalars().one()
        assert p.offset_applied_s == 200 and p.punch_at.replace(tzinfo=dt.UTC) == NOW + dt.timedelta(minutes=3, seconds=40)
    f2 = await fake_zk(users=_users(), clock_fn=ahead, punches=[FakePunch("EMP001", local(TODAY, "08:58"), uid=1)])
    dev2 = await add_device(db, f2, name="Back door", clock_correction="off")
    await run(sessionmaker_, redis, dev2)
    async with sessionmaker_() as s:
        p2 = (await s.execute(select(DevicePunch).where(DevicePunch.device_id == dev2.id))).scalars().one()
        assert p2.offset_applied_s == 0 and p2.punch_at.replace(tzinfo=dt.UTC) == NOW - dt.timedelta(minutes=2)
        assert p2.quarantine is None  # correction off: nothing is held for a steady drift


async def test_small_offset_is_within_tolerance(fake_zk, db, sessionmaker_, redis, clk):
    f = await fake_zk(users=_users(), clock_fn=lambda: clk.device_local() + dt.timedelta(seconds=90))
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)
    d = await reload(sessionmaker_, Device, dev.id)
    assert (d.clock_status, d.last_clock_offset_s) == ("ok", 90)


async def test_wildly_wrong_clock_quarantines_new_punches_and_alerts(fake_zk, db, sessionmaker_, redis, clk):
    f = await fake_zk(
        users=_users(),
        clock_fn=lambda: clk.device_local() - dt.timedelta(days=3),
        punches=[FakePunch("EMP001", local(TODAY - dt.timedelta(days=3), "08:55"), uid=1)],
    )
    dev = await add_device(db, f, ingest_from=TODAY - dt.timedelta(days=30))
    OUTBOX.clear()
    row = await run(sessionmaker_, redis, dev)
    assert row.status == "ok" and row.error_code == "DEVICE_CLOCK_INVALID" and row.quarantined == 1
    d = await reload(sessionmaker_, Device, dev.id)
    assert d.clock_status == "invalid" and d.last_error_code == "DEVICE_CLOCK_INVALID"
    assert await scalar(sessionmaker_, select(func.count()).select_from(Attendance)) == 0
    assert any("clock is wrong" in o["payload"]["title"] for o in OUTBOX)
    assert all(dev.name == o["payload"]["body"] for o in OUTBOX)  # device name only


# ---------------------------------------------------------------- identity, auth, busy, timeout
async def test_identity_pinning_blocks_a_different_machine_until_accepted(fake_zk, db, sessionmaker_, redis, seeded):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")))
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)
    assert (await reload(sessionmaker_, Device, dev.id)).serial_number == "SN-TEST-0001"
    f.serial = "SN-IMPOSTOR"
    f.commands.clear()
    OUTBOX.clear()
    row = await run(sessionmaker_, redis, dev)
    assert (row.status, row.error_code) == ("failed", "DEVICE_IDENTITY_CHANGED")
    assert not any(c in (50, 1503, 1504) for c, _ in f.commands)  # nothing was read from the other machine
    async with sessionmaker_() as s:
        assert "device.identity_changed" in (await s.execute(select(AuditLog.action))).scalars().all()
        d = await s.get(Device, dev.id)
        assert d.serial_number == "SN-TEST-0001" and d.last_error_code == "DEVICE_IDENTITY_CHANGED"
    assert any("identity" in o["payload"]["title"].lower() for o in OUTBOX)
    async with sessionmaker_() as s:  # the admin accepts the replacement
        d = await s.get(Device, dev.id)
        d.serial_number = None
        d.last_error_code = None
        await s.commit()
    assert (await run(sessionmaker_, redis, dev)).status == "ok"
    assert (await reload(sessionmaker_, Device, dev.id)).serial_number == "SN-IMPOSTOR"


async def test_wrong_comm_key_fails_once_without_retries(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users(), comm_key=424242)
    dev = await add_device(db, f, comm_key="111")
    row = await run(sessionmaker_, redis, dev)
    assert (row.status, row.error_code) == ("failed", "DEVICE_AUTH_FAILED")
    assert [c for c, _ in f.commands].count(1000) == 1 and not any(c == 1502 for c, _ in f.commands)


async def test_comm_key_is_used_when_correct(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")), comm_key=424242)
    dev = await add_device(db, f, comm_key="424242")
    assert (await run(sessionmaker_, redis, dev)).status == "ok"


async def test_busy_device_is_not_counted_toward_the_circuit_for_the_first_three(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users(), busy=True)
    dev = await add_device(db, f)
    for _ in range(3):
        row = await run(sessionmaker_, redis, dev)
        assert (row.status, row.error_code) == ("failed", "DEVICE_BUSY")
    assert (await reload(sessionmaker_, Device, dev.id)).consecutive_failures == 0
    await run(sessionmaker_, redis, dev)
    assert (await reload(sessionmaker_, Device, dev.id)).consecutive_failures == 1


async def test_timeout_is_reported(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users(), silent=True)
    dev = await add_device(db, f)
    row = await run(sessionmaker_, redis, dev)
    assert (row.status, row.error_code) == ("failed", "DEVICE_TIMEOUT")
    assert devices_svc.ERROR_TEXT["DEVICE_TIMEOUT"] == "Device did not answer within 5 seconds"


async def test_transient_errors_are_retried_within_the_run(fake_zk, db, sessionmaker_, redis, monkeypatch):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")))
    dev = await add_device(db, f)
    real = device_sync.make_client
    calls = {"n": 0}

    def flaky(h, p, t, k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise zk.DeviceUnreachable("boom")
        return real(h, p, t, k)

    monkeypatch.setattr(device_sync, "make_client", flaky)
    row = await run(sessionmaker_, redis, dev)
    assert row.status == "ok" and calls["n"] == 3
    assert (await reload(sessionmaker_, Device, dev.id)).consecutive_failures == 0


async def test_address_guard_is_rechecked_by_the_worker(fake_zk, db, sessionmaker_, redis, monkeypatch):
    f = await fake_zk(users=_users())
    dev = await add_device(db, f)
    monkeypatch.undo()  # back to the real guard: 127.0.0.1 is refused
    row = await run(sessionmaker_, redis, dev)
    assert row.status == "failed" and f.commands == []


# ---------------------------------------------------------------- circuit breaker
async def test_circuit_opens_at_five_failures_doubles_and_resets(fake_zk, db, sessionmaker_, redis, clk, monkeypatch):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")))
    dev = await add_device(db, f)
    real = device_sync.make_client
    down = {"on": True}

    def maybe_down(h, p, t, k):
        if down["on"]:
            raise zk.DeviceUnreachable("network down")
        return real(h, p, t, k)

    monkeypatch.setattr(device_sync, "make_client", maybe_down)
    OUTBOX.clear()
    for i in range(4):
        row = await run(sessionmaker_, redis, dev, "schedule")
        assert (row.status, row.error_code) == ("failed", "DEVICE_UNREACHABLE"), i
    d = await reload(sessionmaker_, Device, dev.id)
    assert d.consecutive_failures == 4 and d.circuit_open_until is None
    await run(sessionmaker_, redis, dev, "schedule")  # 5th
    d = await reload(sessionmaker_, Device, dev.id)
    assert d.consecutive_failures == 5
    assert d.circuit_open_until.replace(tzinfo=dt.UTC) == NOW + dt.timedelta(minutes=15)
    assert devices_svc.device_status(d, NOW) == "offline"
    alerts = [o for o in OUTBOX if o["payload"]["type"] == "admin_alert" and "offline" in o["payload"]["title"]]
    assert len(alerts) == 1
    # open circuit: scheduled runs are skipped, no connection attempt
    clk.set(NOW + dt.timedelta(minutes=5))
    row = await run(sessionmaker_, redis, dev, "schedule")
    assert (row.status, row.error_code) == ("skipped", "CIRCUIT_OPEN")
    # half-open after 15 minutes: one probe; failing again doubles the wait
    clk.set(NOW + dt.timedelta(minutes=16))
    await run(sessionmaker_, redis, dev, "schedule")
    d = await reload(sessionmaker_, Device, dev.id)
    assert d.circuit_open_until.replace(tzinfo=dt.UTC) == NOW + dt.timedelta(minutes=16 + 30)
    clk.set(NOW + dt.timedelta(minutes=50))
    await run(sessionmaker_, redis, dev, "schedule")
    d = await reload(sessionmaker_, Device, dev.id)
    assert d.circuit_open_until.replace(tzinfo=dt.UTC) == NOW + dt.timedelta(minutes=50 + 60)  # capped at 60
    assert len([o for o in OUTBOX if "offline" in o["payload"]["title"]]) == 1  # still one alert per day
    # the machine comes back: a manual sync bypasses the open circuit and resets everything
    down["on"] = False
    row = await run(sessionmaker_, redis, dev, "manual")
    assert row.status == "ok"
    d = await reload(sessionmaker_, Device, dev.id)
    assert (d.consecutive_failures, d.circuit_open_until, d.last_error_code) == (0, None, None)


async def test_disabled_device_is_skipped(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users())
    dev = await add_device(db, f, enabled=False)
    row = await run(sessionmaker_, redis, dev)
    assert (row.status, row.error_code) == ("skipped", "DEVICE_DISABLED") and f.commands == []


# ---------------------------------------------------------------- one connection per device
async def test_one_connection_per_device_at_a_time(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")), latency=0.15)
    dev = await add_device(db, f)
    first = asyncio.create_task(device_sync.run_sync(sessionmaker_, redis, dev.id, kind="manual"))
    while not f.commands:  # the first run is now talking to the device (no database work during the network phase)
        await asyncio.sleep(0.01)
    b = await device_sync.run_sync(sessionmaker_, redis, dev.id, kind="manual")
    a = await first
    async with sessionmaker_() as s:
        statuses = sorted([(await s.get(DeviceSync, a)).status, (await s.get(DeviceSync, b)).status])
    assert statuses == ["ok", "skipped"]
    assert [c for c, _ in f.commands].count(1000) == 1


async def test_redis_lock_blocks_a_second_worker(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users())
    dev = await add_device(db, f)
    await redis.set(f"device-lock:{dev.id}", "1", ex=180)
    row = await run(sessionmaker_, redis, dev)
    assert row.status == "skipped" and f.commands == []


# ---------------------------------------------------------------- queue and schedule
async def test_queue_runs_sync_and_unsaved_test_jobs(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")))
    dev = await add_device(db, f)
    async with sessionmaker_() as s:
        row = DeviceSync(device_id=dev.id, kind="manual", status="queued")
        s.add(row)
        test = DeviceSync(device_id=None, kind="test", status="queued")
        s.add(test)
        await s.commit()
        sync_id, test_id = row.id, test.id
    await redis.set(
        devices_svc.TESTJOB_KEY.format(test_id),
        encrypt_bytes(json.dumps({"ip": "127.0.0.1", "port": f.port, "transport": "tcp", "comm_key": 0}).encode()).decode(), ex=120,
    )  # fmt: skip
    await devices_svc.push_job(redis, {"kind": "sync", "sync_id": str(sync_id), "device_id": str(dev.id)})
    await devices_svc.push_job(redis, {"kind": "test", "sync_id": str(test_id), "device_id": None})
    assert await device_sync.process_queue(sessionmaker_, redis) == 2
    s1, s2 = await reload(sessionmaker_, DeviceSync, sync_id), await reload(sessionmaker_, DeviceSync, test_id)
    assert s1.status == "ok" and s1.inserted == 1
    assert s2.status == "ok" and s2.device_id is None
    assert s2.result == {
        "serial_number": "SN-TEST-0001", "model": "K40", "firmware": "Ver 6.60 Apr  1 2020", "platform": "ZMM220_TFT",
        "device_time": "2026-10-21T09:00:00", "clock_offset_s": 0, "users_on_device": 2, "records_on_device": 1,
    }  # fmt: skip
    assert await redis.get(devices_svc.TESTJOB_KEY.format(test_id)) is None  # the encrypted payload is removed
    assert await scalar(sessionmaker_, select(func.count()).select_from(DevicePunch)) == 1  # a test never reads punches


async def test_test_job_without_its_payload_fails_cleanly(fake_zk, db, sessionmaker_, redis):
    async with sessionmaker_() as s:
        test = DeviceSync(device_id=None, kind="test", status="queued")
        s.add(test)
        await s.commit()
        tid = test.id
    await devices_svc.push_job(redis, {"kind": "test", "sync_id": str(tid), "device_id": None})
    await device_sync.process_queue(sessionmaker_, redis)
    assert (await reload(sessionmaker_, DeviceSync, tid)).status == "failed"


async def test_tick_runs_due_devices_only_and_respects_the_window(fake_zk, db, sessionmaker_, redis, clk):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")))
    dev = await add_device(db, f)
    assert await device_sync.tick(sessionmaker_, redis) == 1  # never synced -> due
    assert await device_sync.tick(sessionmaker_, redis) == 0  # just synced
    clk.set(NOW + dt.timedelta(minutes=5))
    assert await device_sync.tick(sessionmaker_, redis) == 1  # 5-minute interval inside 05:00-23:30
    clk.set(dt.datetime(2026, 10, 21, 19, 0, tzinfo=dt.UTC))  # 00:30 IST: outside the window -> hourly
    d = await reload(sessionmaker_, Device, dev.id)
    async with sessionmaker_() as s:
        dd = await s.get(Device, dev.id)
        dd.last_sync_at = clk.now - dt.timedelta(minutes=30)
        await s.commit()
    assert await device_sync.tick(sessionmaker_, redis) == 0
    async with sessionmaker_() as s:
        dd = await s.get(Device, dev.id)
        dd.last_sync_at = clk.now - dt.timedelta(minutes=61)
        await s.commit()
    assert await device_sync.tick(sessionmaker_, redis) == 1
    del d


async def test_tick_skips_offline_devices_and_fails_stuck_runs(fake_zk, db, sessionmaker_, redis, clk):
    f = await fake_zk(users=_users())
    dev = await add_device(db, f, circuit_open_until=NOW + dt.timedelta(minutes=10))
    async with sessionmaker_() as s:
        stuck = DeviceSync(device_id=dev.id, kind="schedule", status="running", started_at=NOW - dt.timedelta(minutes=30))
        s.add(stuck)
        await s.commit()
        sid = stuck.id
    assert await device_sync.tick(sessionmaker_, redis) == 0
    assert (await reload(sessionmaker_, DeviceSync, sid)).status == "failed" and f.commands == []


async def test_tick_runs_queued_rows_the_queue_lost(fake_zk, db, sessionmaker_, redis, clk):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")))
    dev = await add_device(db, f, last_sync_at=NOW, last_success_at=NOW)
    async with sessionmaker_() as s:
        row = DeviceSync(device_id=dev.id, kind="manual", status="queued", queued_at=NOW - dt.timedelta(minutes=2))
        s.add(row)
        await s.commit()
        sid = row.id
    await device_sync.tick(sessionmaker_, redis)
    assert (await reload(sessionmaker_, DeviceSync, sid)).status == "ok"


async def test_reconcile_syncs_everything_and_recomputes_the_window(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:55")))
    dev = await add_device(db, f)
    await device_sync.reconcile(sessionmaker_, redis)
    async with sessionmaker_() as s:
        kinds = (await s.execute(select(DeviceSync.kind).where(DeviceSync.device_id == dev.id))).scalars().all()
        assert kinds == ["reconcile"]
        assert await s.scalar(select(func.count()).select_from(Attendance)) == 1


# ---------------------------------------------------------------- matching
async def _mk_users(sm, specs):
    from app.core import security

    async with sm() as s:
        role = (await s.execute(select(User.role_id).where(User.employee_id == "EMP001"))).scalar_one()
        for eid, status in specs:
            s.add(User(employee_id=eid, password_hash=security.hash_password("Str0ng!Passw0rd#1"), full_name=eid, official_email=f"{eid.lower()}@corp-mail.com",
                       role_id=role, must_change_password=False, status=status))  # fmt: skip
        await s.commit()


async def _match(sm, prefix, ids):
    async with sm() as s:
        idx = await device_matching.build_index(s, prefix)
        out = {}
        for i in ids:
            uid = idx.find(i)
            out[i] = (await s.get(User, uid)).employee_id if uid else None
        return out


async def test_matching_exact_prefix_and_leading_zeros(sessionmaker_):
    await _mk_users(sessionmaker_, [("EMP101", "active"), ("0205", "active"), ("EMP300", "inactive")])
    got = await _match(sessionmaker_, "EMP", ["emp001", " EMP001 ", "0101", "101", "205", "0205", "00300", "999", "EMP-1"])
    assert got == {
        "emp001": "EMP001", " EMP001 ": "EMP001", "0101": "EMP101", "101": "EMP101", "205": "0205", "0205": "0205",
        "00300": None, "999": None, "EMP-1": None,
    }  # fmt: skip
    assert (await _match(sessionmaker_, "", ["101"]))["101"] is None  # without the prefix setting EMP101 is not numeric


async def test_ambiguous_match_stays_unmatched(sessionmaker_):
    await _mk_users(sessionmaker_, [("EMP101", "active"), ("101", "active")])
    got = await _match(sessionmaker_, "EMP", ["0101", "101"])
    assert got["0101"] is None  # EMP101 and 101 both equal 101
    assert got["101"] == "101"  # the exact match wins


async def test_sync_auto_matches_with_the_prefix_setting_and_never_touches_manual_or_ignored(
    fake_zk, db, sessionmaker_, redis, seeded
):
    await _mk_users(sessionmaker_, [("EMP101", "active"), ("EMP102", "active")])
    async with sessionmaker_() as s:
        s.add(Setting(key="device.match_prefix", value="EMP"))
        await s.commit()
    users = [FakeUser(1, "0101", "A"), FakeUser(2, "0102", "B"), FakeUser(3, "0103", "C")]
    f = await fake_zk(users=users)
    dev = await add_device(db, f)
    await add_device_user(db, dev, "0102", method="ignored")
    await run(sessionmaker_, redis, dev)
    async with sessionmaker_() as s:
        rows = {r.device_user_id: r for r in (await s.execute(select(DeviceUser))).scalars()}
        assert (
            rows["0101"].match_method == "auto"
            and rows["0102"].match_method == "ignored"
            and rows["0103"].match_method == "unmatched"
        )


async def test_absent_users_are_flagged_and_admins_recorded(fake_zk, db, sessionmaker_, redis):
    f = await fake_zk(users=[FakeUser(1, "EMP001", "A", privilege=14), FakeUser(2, "900", "B")])
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)
    f.users.pop()
    await run(sessionmaker_, redis, dev)
    async with sessionmaker_() as s:
        rows = {r.device_user_id: r for r in (await s.execute(select(DeviceUser))).scalars()}
        assert rows["EMP001"].is_device_admin and rows["EMP001"].present_on_device and not rows["900"].present_on_device


# ---------------------------------------------------------------- retention
async def test_retention_deletes_only_expired_punches_syncs_and_stale_names(fake_zk, db, sessionmaker_):
    f = await fake_zk(users=[])
    dev = await add_device(db, f)
    from tests.device_helpers import add_punch

    old = await add_punch(db, dev, "EMP001", local(TODAY - dt.timedelta(days=1200), "09:00"))
    recent = await add_punch(db, dev, "EMP001", local(TODAY - dt.timedelta(days=100), "09:00"))
    db.add(DeviceSync(device_id=dev.id, kind="schedule", status="ok", queued_at=NOW - dt.timedelta(days=120)))
    db.add(DeviceSync(device_id=dev.id, kind="schedule", status="ok", queued_at=NOW - dt.timedelta(days=5)))
    db.add(DeviceUser(device_id=dev.id, device_user_id="777", name="Gone Person", card="123456", present_on_device=False,
                      last_seen_at=NOW - dt.timedelta(days=100)))  # fmt: skip
    db.add(DeviceUser(device_id=dev.id, device_user_id="778", name="Still Here", present_on_device=True, last_seen_at=NOW))
    await db.commit()
    old_id, recent_id = old.id, recent.id
    out = await device_sync.retention(sessionmaker_, NOW)
    assert out == {"punches": 1, "syncs": 1, "names": 1}
    async with sessionmaker_() as s:
        ids = (await s.execute(select(DevicePunch.id))).scalars().all()
        assert ids == [recent_id] and old_id not in ids
        rows = {
            r[0]: (r[1], r[2])
            for r in (await s.execute(text("SELECT device_user_id, name_enc, card_enc FROM device_users"))).all()
        }
    assert rows["777"] == (None, None) and rows["778"][0] is not None


async def test_retention_zero_keeps_everything(fake_zk, db, sessionmaker_):
    f = await fake_zk(users=[])
    dev = await add_device(db, f)
    from tests.device_helpers import add_punch

    await add_punch(db, dev, "EMP001", local(TODAY - dt.timedelta(days=2000), "09:00"))
    db.add(Setting(key="device.punch_retention_days", value=0))
    await db.commit()
    out = await device_sync.retention(sessionmaker_, NOW)
    assert out["punches"] == 0


def test_settings_defaults_match_the_contract():
    d = device_settings.DEFAULTS
    assert (
        d["device.sync_interval_minutes"] == 5 and d["device.apply_window_days"] == 7 and d["device.punch_retention_days"] == 1095
    )
    assert USERS["admin"] == "ADM001"


# ================================================================ security review fixes
class Shift:
    """A device clock that an 'attacker' can move: real time plus `seconds`."""

    def __init__(self, clk):
        self.clk, self.seconds = clk, 0

    def __call__(self):
        return self.clk.device_local() + dt.timedelta(seconds=self.seconds)


def _alerts(title_part):
    return [o for o in OUTBOX if o["payload"]["type"] == "admin_alert" and title_part in o["payload"]["title"]]


async def _punch_rows(sm):
    async with sm() as s:
        return {(p.device_user_id, p.device_time): p for p in (await s.execute(select(DevicePunch))).scalars()}


# ---- H1 attack A: the clock is changed between two syncs, so a correction would shift punches made with the right clock
async def test_h1_clock_moved_forward_quarantines_new_punches_instead_of_backdating_them(fake_zk, db, sessionmaker_, redis, clk, seeded):
    shift = Shift(clk)
    f = await fake_zk(users=_users(), clock_fn=shift, punches=_punches(("EMP001", "08:50")))
    dev = await add_device(db, f)
    assert (await run(sessionmaker_, redis, dev)).quarantined == 0  # sync 1: clock right
    clk.set(NOW + dt.timedelta(minutes=5))  # 09:05
    f.punches.append(FakePunch("EMP001", local(TODAY, "09:02"), uid=1))  # made with the right clock
    shift.seconds = 3 * 3600  # then somebody sets the machine 3 hours ahead
    OUTBOX.clear()
    row = await run(sessionmaker_, redis, dev)  # sync 2: measures +10800
    assert (row.inserted, row.quarantined) == (1, 1)
    p = (await _punch_rows(sessionmaker_))[("EMP001", local(TODAY, "09:02"))]
    assert p.quarantine == "clock_changed" and p.offset_applied_s == 0
    assert p.punch_at.replace(tzinfo=dt.UTC) == NOW + dt.timedelta(minutes=2)  # NOT moved 3 hours back to 06:02
    async with sessionmaker_() as s:
        o = (await s.execute(select(DevicePunchOutcome).where(DevicePunchOutcome.punch_id == p.id))).scalar_one()
        assert (o.outcome, o.reason) == ("quarantined", "clock_changed")
        assert "device.clock_changed" in (await s.execute(select(AuditLog.action))).scalars().all()
    hr = _alerts("clock was changed")
    assert hr and str(seeded["users"]["hr"]) in hr[0]["user_ids"] and str(seeded["users"]["admin"]) in hr[0]["user_ids"]
    # sync 3: the same wrong offset twice in a row is a stable drift and gets corrected from now on
    clk.set(NOW + dt.timedelta(minutes=10))
    f.punches.append(FakePunch("EMP001", local(TODAY, "12:08"), uid=1))  # 09:08 real, stamped by the +3 h clock
    row = await run(sessionmaker_, redis, dev)
    assert row.quarantined == 0
    p3 = (await _punch_rows(sessionmaker_))[("EMP001", local(TODAY, "12:08"))]
    assert (p3.offset_applied_s, p3.quarantine) == (10800, None)
    assert p3.punch_at.replace(tzinfo=dt.UTC) == NOW + dt.timedelta(minutes=8)


async def test_h1_clock_set_back_and_restored_is_held_both_times(fake_zk, db, sessionmaker_, redis, clk):
    shift = Shift(clk)
    f = await fake_zk(users=_users(), clock_fn=shift, punches=[])
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)
    clk.set(NOW + dt.timedelta(minutes=5))
    shift.seconds = -1800  # 30 minutes back (inside the 12 h invalid limit)
    f.punches.append(FakePunch("EMP001", local(TODAY, "08:34"), uid=1))
    assert (await run(sessionmaker_, redis, dev)).quarantined == 1
    clk.set(NOW + dt.timedelta(minutes=10))
    shift.seconds = 0  # restored: the offset moved again, so this sync's new punches are held too
    f.punches.append(FakePunch("EMP001", local(TODAY, "09:09"), uid=1))
    assert (await run(sessionmaker_, redis, dev)).quarantined == 1
    clk.set(NOW + dt.timedelta(minutes=15))
    f.punches.append(FakePunch("EMP001", local(TODAY, "09:14"), uid=1))
    row = await run(sessionmaker_, redis, dev)  # steady again
    assert (row.inserted, row.quarantined) == (1, 0)


# ---- H1 attack B: set the clock back, punch, restore it before the next sync (the offset looks normal at the sync)
async def test_h1_backdated_punch_is_quarantined_even_when_the_clock_is_back_to_normal(fake_zk, db, sessionmaker_, redis, clk, seeded):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:50")))
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)  # sync 1 at 09:00: reading 09:00
    clk.set(NOW + dt.timedelta(minutes=20))  # 09:20
    f.punches.append(FakePunch("EMP001", local(TODAY, "08:21"), uid=1))  # stamped while the clock was set back one hour
    f.punches.append(FakePunch("EMP001", local(TODAY, "09:15"), uid=1))  # an honest punch
    OUTBOX.clear()
    row = await run(sessionmaker_, redis, dev)  # offset is 0 again
    assert (row.inserted, row.quarantined) == (2, 1)
    rows = await _punch_rows(sessionmaker_)
    assert rows[("EMP001", local(TODAY, "08:21"))].quarantine == "backdated"
    assert rows[("EMP001", local(TODAY, "09:15"))].quarantine is None
    async with sessionmaker_() as s:
        a = (await s.execute(select(Attendance))).scalars().one()
        assert a.check_in_at.replace(tzinfo=dt.UTC) == NOW - dt.timedelta(minutes=10)  # 08:50 stays the check-in, not 08:21
        assert "device.punches_backdated" in (await s.execute(select(AuditLog.action))).scalars().all()
    assert _alerts("dated before the last sync")


async def test_h1_new_punches_after_the_last_reading_are_never_backdated(fake_zk, db, sessionmaker_, redis, clk):
    f = await fake_zk(users=_users(), punches=_punches(("EMP001", "08:50")))
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)
    clk.set(NOW + dt.timedelta(minutes=5))
    f.punches.append(FakePunch("EMP001", local(TODAY, "09:00"), uid=1))  # exactly at the previous reading
    f.punches.append(FakePunch("EMP001", local(TODAY, "08:58"), uid=1))  # inside the tolerance (120 s)
    row = await run(sessionmaker_, redis, dev)
    assert (row.inserted, row.quarantined) == (2, 0)


async def test_h1_invalid_clock_does_not_move_the_reference(fake_zk, db, sessionmaker_, redis, clk):
    shift = Shift(clk)
    f = await fake_zk(users=_users(), clock_fn=shift, punches=[])
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)
    before = await reload(sessionmaker_, Device, dev.id)
    shift.seconds = -3 * 86400
    await run(sessionmaker_, redis, dev)
    after = await reload(sessionmaker_, Device, dev.id)
    assert after.clock_status == "invalid"
    assert (after.last_clock_reading, after.last_clock_reading_offset_s) == (before.last_clock_reading, before.last_clock_reading_offset_s)


def test_h1_classify_clock_table():
    cfg = device_settings.DeviceCfg(**{k.removeprefix("device."): v for k, v in device_settings.DEFAULTS.items()})
    t = dt.datetime(2026, 10, 21, 9, 0)

    def f(off):
        return device_sync.Fetched("S", "M", "P", "F", t, off, 1, 1, None, None, 0, False)

    cc = device_sync.classify_clock
    assert cc(f(10), cfg, "auto", None) == ("ok", 0, None)
    assert cc(f(10), cfg, "auto", 0) == ("ok", 0, None)
    assert cc(f(10), cfg, "auto", 3600) == ("ok", 0, "clock_changed")  # fixed on the machine: ambiguous sync
    assert cc(f(200), cfg, "auto", None) == ("drift", 0, "clock_changed")  # unconfirmed
    assert cc(f(200), cfg, "auto", 195) == ("drift", 200, None)  # confirmed (within jitter)
    assert cc(f(200), cfg, "off", 195) == ("drift", 0, None)
    assert cc(f(200), cfg, "off", None) == ("drift", 0, None)
    assert cc(f(3600), cfg, "auto", 0) == ("drift", 0, "clock_changed")
    assert cc(f(99999), cfg, "auto", 0) == ("invalid", 0, None)
    nothing = device_sync.Fetched("S", "M", "P", "F", None, None, 1, 1, None, None, 0, False)
    assert cc(nothing, cfg, "auto", 0) == ("invalid", 0, None)


# ---- M4: unresolved slots are dropped, new users are held, device administrators are flagged
async def test_m4_punch_with_an_unknown_slot_is_dropped_not_guessed(fake_zk, db, sessionmaker_, redis):
    punches = [FakePunch("EMP001", local(TODAY, "08:50"), uid=1), FakePunch("77", local(TODAY, "08:51"), uid=77)]
    f = await fake_zk(att_format=8, users=_users(), punches=punches)
    dev = await add_device(db, f)
    row = await run(sessionmaker_, redis, dev)
    assert row.inserted == 1
    ids = {k[0] for k in await _punch_rows(sessionmaker_)}
    assert ids == {"EMP001"}  # slot 77 never became a user id "77"
    async with sessionmaker_() as s:
        assert "device.punches_unresolved" in (await s.execute(select(AuditLog.action))).scalars().all()


async def test_m4_users_first_seen_after_the_first_sync_are_held_for_hr(fake_zk, db, sessionmaker_, redis, clk, seeded):
    f = await fake_zk(users=_users(), punches=[])
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)  # first sync: EMP001 is matched as before
    async with sessionmaker_() as s:
        got = {r.device_user_id: (r.match_method, r.auto_hold) for r in (await s.execute(select(DeviceUser))).scalars()}
        assert got == {"EMP001": ("auto", False), "900": ("unmatched", False)}
    clk.set(NOW + dt.timedelta(minutes=5))
    f.users.append(FakeUser(3, USERS["manager"], "Planted", privilege=14))  # somebody enrols the manager's id on the machine
    OUTBOX.clear()
    await run(sessionmaker_, redis, dev)
    async with sessionmaker_() as s:
        planted = (await s.execute(select(DeviceUser).where(DeviceUser.device_user_id == USERS["manager"]))).scalar_one()
        assert (planted.match_method, planted.user_id, planted.auto_hold, planted.is_device_admin) == ("unmatched", None, True, True)
    assert _alerts("need confirmation") and _alerts("administrator appeared")
    clk.set(NOW + dt.timedelta(hours=2))
    await run(sessionmaker_, redis, dev)  # later syncs (and user list refreshes) keep holding it
    async with sessionmaker_() as s:
        method = (await s.execute(select(DeviceUser.match_method).where(DeviceUser.device_user_id == USERS["manager"]))).scalar_one()
        assert method == "unmatched"


async def test_m4_first_sync_rows_still_match_when_the_employee_appears_later(fake_zk, db, sessionmaker_, redis, clk, seeded):
    f = await fake_zk(users=[FakeUser(3, USERS["manager"], "Mgr")], punches=[])
    dev = await add_device(db, f)
    async with sessionmaker_() as s:  # the manager is not active yet at the first sync
        m = await s.get(User, seeded["users"]["manager"])
        m.status = "inactive"
        await s.commit()
    await run(sessionmaker_, redis, dev)
    async with sessionmaker_() as s:
        m = await s.get(User, seeded["users"]["manager"])
        m.status = "active"
        await s.commit()
    clk.set(NOW + dt.timedelta(hours=2))  # past the one hour user-list refresh
    await run(sessionmaker_, redis, dev)
    async with sessionmaker_() as s:
        assert (await s.execute(select(DeviceUser.match_method))).scalar_one() == "auto"  # first-sync rows are not held


# ---- L11: unusual punch volume
async def test_l11_unusual_punch_volume_raises_an_alert_but_not_on_the_first_sync(fake_zk, db, sessionmaker_, redis, clk, monkeypatch):
    monkeypatch.setattr(device_sync, "VOLUME_MIN", 3)
    monkeypatch.setattr(device_sync, "VOLUME_PER_USER", 1)
    first = [FakePunch("EMP001", local(TODAY, "08:00") + dt.timedelta(minutes=10 * i), uid=1) for i in range(5)]
    f = await fake_zk(users=_users(), punches=first)
    dev = await add_device(db, f)
    OUTBOX.clear()
    await run(sessionmaker_, redis, dev)
    assert not _alerts("Unusual number")  # the first sync pulls history by design
    clk.set(NOW + dt.timedelta(minutes=30))
    f.punches += [FakePunch("EMP001", local(TODAY, "09:10") + dt.timedelta(minutes=i), uid=1) for i in range(8)]
    await run(sessionmaker_, redis, dev)
    assert _alerts("Unusual number")
    async with sessionmaker_() as s:
        assert "device.punch_volume" in (await s.execute(select(AuditLog.action))).scalars().all()


# ---- L4: punches without an outcome are applied by the next sync; outcomes upsert
async def test_l4_punches_without_an_outcome_are_applied_on_the_next_sync(fake_zk, db, sessionmaker_, redis, clk, seeded):
    f = await fake_zk(users=_users(), punches=[])
    dev = await add_device(db, f)
    await run(sessionmaker_, redis, dev)
    from tests.device_helpers import add_punch

    async with sessionmaker_() as s:  # a run died after the insert: the punch is stored, no outcome row, no attendance
        d = await s.get(Device, dev.id)
        await add_punch(s, d, "EMP001", local(TODAY, "08:45"))
        await s.commit()
    assert await scalar(sessionmaker_, select(func.count()).select_from(DevicePunchOutcome)) == 0
    clk.set(NOW + dt.timedelta(minutes=5))
    await run(sessionmaker_, redis, dev)
    assert await scalar(sessionmaker_, select(func.count()).select_from(DevicePunchOutcome)) == 1
    async with sessionmaker_() as s:
        assert (await s.execute(select(Attendance))).scalars().one().check_in_source == "device"


async def test_l4_set_outcomes_upserts_and_tolerates_duplicates(db, sessionmaker_, seeded):
    from app.services import device_attendance as da
    from tests.device_helpers import add_punch

    dev = await add_device(db, None)
    p = await add_punch(db, dev, "EMP001", local(TODAY, "08:45"))
    await db.commit()
    await da.set_outcomes(db, [(p.id, None, TODAY, "unmatched", None), (p.id, None, TODAY, "ignored", "mapping_ignored")])
    await db.commit()
    async with sessionmaker_() as s:
        await da.set_outcomes(s, [(p.id, None, TODAY, "quarantined", "future")])  # existing row: updated in place
        await s.commit()
    async with sessionmaker_() as s:
        rows = (await s.execute(select(DevicePunchOutcome.outcome, DevicePunchOutcome.reason))).all()
    assert [tuple(r) for r in rows] == [("quarantined", "future")]


async def test_l4_recompute_waits_for_the_device_apply_lock(sessionmaker_, redis):
    import uuid

    did = uuid.uuid4()
    lock = device_sync.apply_lock(did)
    await lock.acquire()
    task = asyncio.create_task(device_sync.run_job(sessionmaker_, redis, {"kind": "recompute", "keys": [[str(did), "1"]]}))
    await asyncio.sleep(0.05)
    assert not task.done()  # blocked behind the sync's database phase
    lock.release()
    await asyncio.wait_for(task, 5)


# ---- L8: separate lanes, no waiting for the batch
async def test_l8_test_jobs_use_their_own_list(sessionmaker_, redis):
    await devices_svc.push_job(redis, {"kind": "test", "sync_id": "a", "device_id": None})
    await devices_svc.push_job(redis, {"kind": "sync", "sync_id": "b", "device_id": "c"})
    assert await redis.llen(devices_svc.QUEUE_TEST_KEY) == 1 and await redis.llen(devices_svc.QUEUE_KEY) == 1
    assert (await devices_svc.pop_job(redis))["kind"] == "sync"
    assert (await devices_svc.pop_job(redis, devices_svc.QUEUE_TEST_KEY))["kind"] == "test"


async def test_l8_process_queue_without_waiting_returns_while_slow_jobs_run(sessionmaker_, redis, monkeypatch):
    gate = asyncio.Event()
    seen = []

    async def slow(sm, r, job):
        seen.append(job["kind"])
        await gate.wait()

    monkeypatch.setattr(device_sync, "run_job", slow)
    for i in range(5):
        await devices_svc.push_job(redis, {"kind": "sync", "sync_id": str(i), "device_id": str(i)})
    await devices_svc.push_job(redis, {"kind": "test", "sync_id": "t", "device_id": None})
    started = await asyncio.wait_for(device_sync.process_queue(sessionmaker_, redis, wait=False), 2)
    await asyncio.sleep(0)
    assert started == 4 and sorted(seen) == ["sync", "sync", "sync", "test"]  # 3 sync slots + 1 test slot
    assert await redis.llen(devices_svc.QUEUE_KEY) == 2  # the rest waits in Redis for the next tick
    gate.set()
    await asyncio.gather(*device_sync._running["sync"], *device_sync._running["test"])
    assert await device_sync.process_queue(sessionmaker_, redis, wait=False) == 2
    await asyncio.gather(*device_sync._running["sync"])
