"""Migration 0030 (eSSL / ZK attendance devices; plan 18 step 1).

Trigger behaviour needs PostgreSQL (verified by hand on a scratch postgres:16 as the non-owner app_user, see the work report). Here the SQL
text, the model metadata, the permissions, settings and schema.sql are cross-checked, and the CHECKs SQLite can evaluate run through the ORM.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

DB = Path(__file__).resolve().parents[2] / "database"
NEW_TABLES = ("devices", "device_users", "device_punches", "device_punch_outcomes", "device_syncs")
SETTINGS = {
    "device.sync_interval_minutes": "5",
    "device.sync_window_start": '"05:00"',
    "device.sync_window_end": '"23:30"',
    "device.off_window_interval_minutes": "60",
    "device.apply_window_days": "7",
    "device.checkout_min_gap_minutes": "5",
    "device.drift_tolerance_s": "120",
    "device.drift_invalid_s": "43200",
    "device.future_tolerance_s": "300",
    "device.match_prefix": '""',
    "device.apply_on_on_duty_days": "false",
    "device.use_punch_state": "false",
    "device.push_on_punch": "false",
    "device.punch_retention_days": "1095",
    "device.sync_history_retention_days": "90",
}


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0030", DB / "alembic" / "versions" / "0030_essl_devices.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0030", "0029")
    sys.path.insert(0, str(DB))
    try:
        from migration_sql import split_sql  # type: ignore

        up, down = split_sql(mod.UPGRADE), split_sql(mod.DOWNGRADE)
    finally:
        sys.path.remove(str(DB))
    assert all(up) and all(down)
    assert not any(s.startswith(("BEGIN", "DECLARE", "IF ")) for s in up + down)
    assert sum(s.startswith("CREATE FUNCTION device_punches_block_mutation(") for s in up) == 1
    assert "DROP FUNCTION IF EXISTS device_punches_block_mutation();" in mod.DOWNGRADE
    d = mod.DOWNGRADE
    assert d.lstrip().startswith("DO $$")
    guard = d.split("$$;")[0]
    assert "EXISTS (SELECT 1 FROM device_punches)" in guard and "check_in_source = 'device' OR check_out_source = 'device'" in guard
    for t in NEW_TABLES:
        assert f"DROP TABLE IF EXISTS {t};" in d, t
    # FK-dependent objects go first
    assert d.index("DROP TABLE IF EXISTS device_punch_outcomes") < d.index("DROP TABLE IF EXISTS device_punches")
    assert d.index("DROP TABLE IF EXISTS device_punches") < d.index("DROP TABLE IF EXISTS device_syncs")
    assert d.index("DROP TABLE IF EXISTS device_users") < d.index("DROP TABLE IF EXISTS devices;")
    assert d.index("DROP COLUMN check_in_source") < d.index("DROP TABLE IF EXISTS devices;")
    for k in SETTINGS:
        assert f"'{k}'" in d, k
    for code in ("device.view", "device.map", "device.manage"):
        assert f"'{code}'" in d, code
    assert "%" not in mod.UPGRADE  # nothing for the DDL layer to escape


def test_tables_triggers_and_grants():
    up = _load().UPGRADE
    punches = up.split("CREATE TABLE device_punches (")[1].split("\n);")[0]
    for needle in (
        "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
        "device_id uuid NOT NULL REFERENCES devices(id) ON DELETE RESTRICT",
        "device_time timestamp NOT NULL",
        "punch_at timestamptz NOT NULL",
        "sync_id uuid REFERENCES device_syncs(id) ON DELETE SET NULL",
        "CONSTRAINT uq_device_punches_device_user_time UNIQUE (device_id, device_user_id, device_time)",
        "CHECK (quarantine IS NULL OR quarantine IN ('clock_invalid','future'))",
    ):
        assert needle in punches, needle
    fn = up.split("CREATE FUNCTION device_punches_block_mutation")[1].split("$$;")[0]
    assert "OLD.punch_at < now() - interval '90 days'" in fn
    assert "pg_trigger_depth() > 1" in fn and "(to_jsonb(NEW) - 'sync_id') = (to_jsonb(OLD) - 'sync_id')" in fn
    assert "CREATE TRIGGER device_punches_append_only BEFORE UPDATE OR DELETE ON device_punches" in up
    assert "CREATE TRIGGER device_punches_no_truncate BEFORE TRUNCATE ON device_punches" in up
    users = up.split("CREATE TABLE device_users (")[1].split("\n);")[0]
    assert "device_id uuid NOT NULL REFERENCES devices(id) ON DELETE RESTRICT" in users
    assert "CHECK ((user_id IS NOT NULL) = (match_method IN ('auto','manual')))" in users
    assert "name_enc bytea" in users and "card_enc bytea" in users
    assert "comm_key_enc bytea" in up.split("CREATE TABLE devices (")[1].split("\n);")[0]
    assert "CREATE UNIQUE INDEX uq_devices_name_lower ON devices (lower(name))" in up
    assert "UNIQUE (ip, port)" in up
    assert "WHERE match_method = 'unmatched'" in up
    # revokes: TRUNCATE everywhere, DELETE stays (retention purge, device_users clear); never UPDATE/DELETE revoked on these tables
    dobody = up.split("DO $$")[-1]
    for t in ("device_punches", "device_users", "devices", "device_syncs", "device_punch_outcomes"):
        assert f"REVOKE TRUNCATE ON {t} FROM app_user;" in dobody, t
    assert "REVOKE DELETE" not in up and "REVOKE UPDATE" not in up
    # permissions: view / map for admin + hr, manage for admin only
    assert "r.name IN ('admin', 'hr') AND p.code = 'device.view'" in up
    assert "r.name IN ('admin', 'hr') AND p.code = 'device.map'" in up
    assert "r.name IN ('admin') AND p.code = 'device.manage'" in up
    for k, v in SETTINGS.items():
        assert f"INSERT INTO settings (key, value) VALUES ('{k}', '{v}'::jsonb) ON CONFLICT (key) DO NOTHING;" in up, k
    # attendance backfill
    assert "WHERE manual;" in up and "app_check_in_at = check_in_at, app_check_out_at = check_out_at WHERE NOT manual;" in up


def test_no_biometric_names():
    """Plan 18: no template / fingerprint / face / palm / biometric column or table may ever exist (we never read or store them)."""
    from app.models import Base

    banned = re.compile(r"template|finger|face|palm|biometric|iris|\bpin\b|password_enc", re.IGNORECASE)
    allowed = {("notifications", "template")}  # none expected; keep the list explicit
    for tname, table in Base.metadata.tables.items():
        if tname in NEW_TABLES:
            assert not banned.search(tname), tname
        for col in table.columns:
            if (tname, col.name) in allowed:
                continue
            if tname in NEW_TABLES or tname == "attendance":
                assert not banned.search(col.name), (tname, col.name)
    sql = _load().UPGRADE
    for ident in re.findall(r"(?:CREATE TABLE|ADD COLUMN)\s+(\w+)", sql):
        assert not banned.search(ident), ident
    for body in re.findall(r"CREATE TABLE \w+ \((.*?)\n\);", sql, re.S):
        for line in body.splitlines():
            m = re.match(r"\s{2}(\w+)\s", line)
            if m and m.group(1) != "CONSTRAINT":
                assert not banned.search(m.group(1)), m.group(1)


def test_models_match_migration_and_encryption():
    from app.core.crypto import EncryptedString
    from app.models import Base

    up = _load().UPGRADE
    t = Base.metadata.tables
    for table, cols in (("devices", ("comm_key_enc",)), ("device_users", ("name_enc", "card_enc"))):
        for c in cols:
            assert isinstance(t[table].c[c].type, EncryptedString), (table, c)
    for name in re.findall(r'name="((?:ck|uq)_(?:devices|device_[a-z_]+|attendance_[a-z_]+))"', _models_src()):
        if name.startswith(("ck_attendance_", "uq_attendance_")) and name not in up:
            continue  # older attendance constraints from earlier migrations
        assert name in up, name
    for col in ("check_in_source", "check_out_source", "check_in_device_id", "check_out_device_id", "app_check_in_at", "app_check_out_at"):
        assert col in t["attendance"].c and f"ADD COLUMN {col} " in up, col
    assert t["attendance"].c.check_in_source.nullable is False and t["attendance"].c.check_out_source.nullable is True
    assert {c.name for c in t["device_punches"].c} == {
        "id", "device_id", "device_user_id", "device_uid", "device_time", "punch_at", "offset_applied_s",
        "state", "verify_mode", "quarantine", "sync_id", "ingested_at",
    }


def _models_src() -> str:
    base = Path(__file__).resolve().parents[1] / "app" / "models"
    return (base / "devices.py").read_text(encoding="utf-8") + (base / "attendance.py").read_text(encoding="utf-8")


def test_seed_permissions():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("ec_seed_0030", DB / "seed.py")
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
    finally:
        sys.path.remove(str(DB))
    mod = _load()
    assert seed.PERMISSIONS["device.view"][1] == {"admin", "hr"}
    assert seed.PERMISSIONS["device.map"][1] == {"admin", "hr"}
    assert seed.PERMISSIONS["device.manage"][1] == {"admin"}
    for code, desc, _ in mod._PERMISSIONS:
        assert seed.PERMISSIONS[code][0] == desc  # same text in the migration and in the seed


def test_schema_sql_mirror():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0029 -> 0030" in sql
    for t in NEW_TABLES:
        assert f"CREATE TABLE {t} (" in sql, t
    assert "CREATE FUNCTION device_punches_block_mutation()" in sql
    assert "CREATE TRIGGER device_punches_append_only" in sql
    assert "REVOKE TRUNCATE ON device_punches FROM app_user" in sql
    assert "ALTER TABLE attendance ADD COLUMN check_in_source text NOT NULL DEFAULT 'app'" in sql
    for key in SETTINGS:
        assert f"'{key}'" in sql, key


# ---- CHECK constraints SQLite can evaluate, through the ORM -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orm_defaults_and_checks(engine):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import (
        Attendance,
        Device,
        DevicePunch,
        DevicePunchOutcome,
        DeviceSync,
        DeviceUser,
        Role,
        User,
    )

    async with AsyncSession(engine, expire_on_commit=False) as s:
        role = Role(name=f"r{uuid.uuid4().hex[:6]}")
        s.add(role)
        await s.flush()
        u = User(employee_id=f"T{uuid.uuid4().hex[:6]}", password_hash="x", full_name="U", role_id=role.id)
        dev = Device(name="Main entrance", ip="192.168.0.201", ingest_from=dt.date(2026, 11, 1), comm_key="123456")
        s.add_all([u, dev])
        await s.flush()
        sync = DeviceSync(device_id=dev.id, kind="manual")
        s.add(sync)
        await s.flush()
        assert (dev.port, dev.transport, dev.enabled, dev.clock_correction, dev.consecutive_failures) == (4370, "tcp", True, "auto", 0)
        assert dev.comm_key == "123456" and sync.status == "queued"

        du = DeviceUser(device_id=dev.id, device_user_id="101", name="Asha", card="998877", user_id=u.id, match_method="manual")
        p = DevicePunch(
            device_id=dev.id, device_user_id="101", device_time=dt.datetime(2026, 11, 10, 9, 2, 11),
            punch_at=dt.datetime(2026, 11, 10, 3, 32, 11, tzinfo=dt.timezone.utc), sync_id=sync.id,
        )
        s.add_all([du, p])
        await s.flush()
        assert du.is_device_admin is False and du.present_on_device is True and p.offset_applied_s == 0 and p.id
        s.add(DevicePunchOutcome(punch_id=p.id, user_id=u.id, work_date=dt.date(2026, 11, 10), outcome="check_in"))
        att = Attendance(
            user_id=u.id, work_date=dt.date(2026, 11, 10), check_in_at=p.punch_at, check_in_source="device", check_in_device_id=dev.id,
        )
        s.add(att)
        await s.flush()
        assert att.check_out_source is None and att.app_check_in_at is None
        await s.commit()
        dev_id, user_id, punch_time = dev.id, u.id, p.device_time

    async def refused(obj):
        async with AsyncSession(engine) as s2:
            s2.add(obj)
            with pytest.raises(IntegrityError):
                await s2.flush()

    await refused(DevicePunch(device_id=dev_id, device_user_id="101", device_time=punch_time, punch_at=dt.datetime.now(dt.timezone.utc)))  # idempotency key
    await refused(Device(name="x", ip="192.168.0.201", ingest_from=dt.date(2026, 11, 1)))  # (ip, port) unique
    await refused(Device(name="MAIN ENTRANCE", ip="192.168.0.202", ingest_from=dt.date(2026, 11, 1)))  # lower(name) unique
    await refused(Device(name="b", ip="192.168.0.202", port=0, ingest_from=dt.date(2026, 11, 1)))
    await refused(Device(name="b", ip="192.168.0.202", transport="http", ingest_from=dt.date(2026, 11, 1)))
    await refused(Device(name="b", ip="192.168.0.202", clock_status="bad", ingest_from=dt.date(2026, 11, 1)))
    await refused(DeviceSync(kind="nightly"))
    await refused(DeviceSync(kind="manual", result={"a": 1}))  # result only for a test
    await refused(DeviceUser(device_id=dev_id, device_user_id="102", match_method="auto"))  # mapped method needs a user
    await refused(DeviceUser(device_id=dev_id, device_user_id="103", match_method="unmatched", user_id=user_id))  # and the reverse
    await refused(DeviceUser(device_id=dev_id, device_user_id="104", match_method="guess"))
    await refused(DeviceUser(device_id=dev_id, device_user_id="101", match_method="unmatched"))  # unique per device
    await refused(DevicePunchOutcome(punch_id=1, outcome="maybe"))
    await refused(DevicePunchOutcome(punch_id=1, outcome="extra", reason="bored"))
    await refused(Attendance(user_id=user_id, work_date=dt.date(2026, 11, 11), check_in_at=dt.datetime.now(dt.timezone.utc), check_in_source="sms"))
    await refused(Attendance(user_id=user_id, work_date=dt.date(2026, 11, 12), check_in_at=dt.datetime.now(dt.timezone.utc), check_in_device_id=dev_id))  # device id needs source device
