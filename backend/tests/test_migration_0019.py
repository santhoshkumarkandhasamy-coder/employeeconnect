"""Migration 0019 (On Duty): content, models, schema.sql mirror, seed, encrypted columns, and (optional) real-Postgres constraint tests.

The constraint / backfill tests need PostgreSQL. Set ONDUTY_PG_URL to a SYNC URL of a database migrated to 0019, e.g.
  ONDUTY_PG_URL=postgresql+psycopg://postgres:pw@localhost:55432/ec
They create their own rows inside a transaction that is rolled back. Without the variable they are skipped.
The backfill test additionally needs the DB to be at 0018 beforehand; see test_backfill_sql_semantics, which replays the
backfill statement on a scratch table instead, so it also runs against a 0019 database.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

DB = Path(__file__).resolve().parents[2] / "database"


def _load_file(name: str, path: Path):
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def _load():
    return _load_file("mig0019", DB / "alembic" / "versions" / "0019_on_duty.py")


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0019", "0018")
    for stmt in (
        "CREATE TABLE duty_locations",
        "CREATE TABLE duty_location_assignments",
        "CREATE TABLE on_duty_requests",
        "CREATE TABLE on_duty_visits",
        "uq_duty_locations_name",
        "uq_on_duty_visits_one_open",
        "uq_on_duty_visits_check_in_selfie",
        "uq_on_duty_visits_check_out_selfie",
        "ix_on_duty_visits_suspicious",
        "ALTER TABLE attendance ADD COLUMN work_mode",
        "ALTER TABLE attendance ADD COLUMN check_out_source",
        "UPDATE attendance SET check_out_source = 'office' WHERE check_out_at IS NOT NULL",
        "ck_attendance_check_out_source_pair",
        "ix_attendance_work_mode",
        "duty_location.manage",
        "on_duty.max_visits_per_day",
    ):
        assert stmt in mod.UPGRADE, stmt
    for stmt in (
        "DROP TABLE IF EXISTS on_duty_visits",
        "DROP TABLE IF EXISTS on_duty_requests",
        "DROP TABLE IF EXISTS duty_location_assignments",
        "DROP TABLE IF EXISTS duty_locations",
        "DROP COLUMN IF EXISTS work_mode",
        "DROP COLUMN IF EXISTS check_out_source",
        "DELETE FROM settings WHERE key LIKE 'on_duty.",
        "DELETE FROM permissions WHERE code = 'duty_location.manage'",
    ):
        assert stmt in mod.DOWNGRADE, stmt
    # the backfill runs before the pairing check is added (otherwise the ALTER would fail on existing closed rows)
    assert mod.UPGRADE.index("UPDATE attendance SET check_out_source") < mod.UPGRADE.index("ck_attendance_check_out_source_pair")
    # every statement survives the splitter and is DDL-safe (no stray percent signs: sa.DDL formats them)
    from migration_sql import split_sql  # type: ignore

    sys.path.insert(0, str(DB))
    try:
        for stmt in split_sql(mod.UPGRADE) + split_sql(mod.DOWNGRADE):
            assert stmt
    finally:
        sys.path.remove(str(DB))


def test_models_match_and_sensitive_columns_are_encrypted():
    from app.core.crypto import EncryptedJSON, EncryptedString
    from app.models import Base

    t = Base.metadata.tables
    assert {"duty_locations", "duty_location_assignments", "on_duty_requests", "on_duty_visits"} <= t.keys()
    enc = {
        ("on_duty_requests", "purpose_enc"): EncryptedString,
        ("on_duty_requests", "decision_comment_enc"): EncryptedString,
        ("on_duty_visits", "check_in_loc_enc"): EncryptedJSON,
        ("on_duty_visits", "check_out_loc_enc"): EncryptedJSON,
        ("on_duty_visits", "note_enc"): EncryptedString,
    }
    for (table, col), typ in enc.items():
        assert isinstance(t[table].c[col].type, typ), (table, col)
    # no plaintext coordinate / free-text column on visits or requests
    for name in ("on_duty_visits", "on_duty_requests"):
        for c in t[name].c:
            assert c.name not in ("lat", "lng", "latitude", "longitude", "purpose", "note", "comment"), c.name

    att = t["attendance"]
    assert not att.c.work_mode.nullable and att.c.check_out_source.nullable
    names = {c.name for c in att.constraints}
    assert {"ck_attendance_work_mode", "ck_attendance_check_out_source", "ck_attendance_check_out_source_pair"} <= names
    # partial indexes are stripped from the metadata by the SQLite test harness (conftest); the model source and the real
    # PostgreSQL test below cover them
    assert "ix_attendance_work_mode" in (Path(__file__).resolve().parents[1] / "app" / "models" / "attendance.py").read_text(
        encoding="utf-8"
    )

    # every model index / unique / check name in the migration SQL exists on the model and vice versa
    mod = _load()
    for table in ("duty_locations", "duty_location_assignments", "on_duty_requests", "on_duty_visits"):
        for idx in t[table].indexes:
            assert idx.name in mod.UPGRADE, idx.name
        for con in t[table].constraints:
            if con.name:
                assert con.name in mod.UPGRADE, con.name


def test_key_rotation_discovers_on_duty_columns():
    from app.services import key_rotation

    names = {(t.name, c.name) for t, c in key_rotation.encrypted_columns()}
    for pair in (
        ("on_duty_requests", "purpose_enc"),
        ("on_duty_requests", "decision_comment_enc"),
        ("on_duty_visits", "check_in_loc_enc"),
        ("on_duty_visits", "check_out_loc_enc"),
        ("on_duty_visits", "note_enc"),
    ):
        assert pair in names, pair


def test_schema_sql_and_seed():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0018 -> 0019" in sql and "version_num='0019'" in sql
    assert "CREATE TABLE on_duty_visits" in sql and "ck_attendance_check_out_source_pair" in sql
    seed = _load_file("ec_seed_0019", DB / "seed.py")
    assert seed.PERMISSIONS["duty_location.manage"][1] == {"admin", "hr"}
    s = seed.DEFAULT_SETTINGS
    assert {k: v for k, v in s.items() if k.startswith("on_duty.")} == {
        "on_duty.enabled": True,
        "on_duty.approval_required": False,
        "on_duty.apply_late_rule": False,
        "on_duty.checkout_geofence": False,
        "on_duty.selfie_required": False,
        "on_duty.max_visits_per_day": 10,
        "on_duty.request_backdate_days": 0,
        "on_duty.request_max_advance_days": 60,
    }
    # the migration seeds the same defaults as seed.py
    mod = _load()
    for key, value in s.items():
        if key.startswith("on_duty."):
            assert f"('{key}', '{str(value).lower()}'::jsonb)" in mod.UPGRADE, key


# ---------------------------------------------------------------- real PostgreSQL (optional)
PG = os.environ.get("ONDUTY_PG_URL")
pg = pytest.mark.skipif(not PG, reason="ONDUTY_PG_URL not set (needs PostgreSQL migrated to 0019)")


@pytest.fixture
def conn():
    import sqlalchemy as sa

    engine = sa.create_engine(PG)
    c = engine.connect()
    tx = c.begin()
    try:
        yield c
    finally:
        tx.rollback()
        c.close()
        engine.dispose()


def _mk_user(c):
    import sqlalchemy as sa

    n = uuid.uuid4().hex[:8].upper()
    role = c.execute(sa.text("INSERT INTO roles (name) VALUES (:n) RETURNING id"), {"n": "r" + n}).scalar_one()
    return c.execute(
        sa.text(
            "INSERT INTO users (employee_id, password_hash, full_name, official_email, role_id) "
            "VALUES (:e, 'x', 'T U', :m, :r) RETURNING id"
        ),
        {"e": "T" + n, "m": f"{n}@example.com", "r": role},
    ).scalar_one()


def _mk_loc(c, name=None, **over):
    import sqlalchemy as sa

    vals = {"name": name or "Site " + uuid.uuid4().hex[:6], "lat": 12.97, "lng": 77.59, "radius_m": 100}
    vals.update(over)
    cols = ", ".join(vals)
    ph = ", ".join(":" + k for k in vals)
    return c.execute(sa.text(f"INSERT INTO duty_locations ({cols}) VALUES ({ph}) RETURNING id"), vals).scalar_one()


def _mk_att(c, uid, day="2026-09-01", out=False):
    import sqlalchemy as sa

    return c.execute(
        sa.text(
            "INSERT INTO attendance (user_id, work_date, check_in_at, check_out_at, check_out_source) "
            "VALUES (:u, :d, CAST(:d AS date) + time '04:00', "
            "CASE WHEN :o THEN CAST(:d AS date) + time '12:00' END, CASE WHEN :o THEN 'office' END) RETURNING id"
        ),
        {"u": uid, "d": day, "o": out},
    ).scalar_one()


def _mk_visit(c, uid, att, loc, day="2026-09-01", **over):
    import sqlalchemy as sa

    vals = {
        "user_id": uid,
        "attendance_id": att,
        "work_date": day,
        "location_id": loc,
        "location_name": "Site",
        "check_in_at": "2026-09-01 05:00+00",
    }
    vals.update(over)
    cols = ", ".join(vals)
    ph = ", ".join(":" + k for k in vals)
    return c.execute(sa.text(f"INSERT INTO on_duty_visits ({cols}) VALUES ({ph}) RETURNING id"), vals).scalar_one()


def _fails(c, sql, **params):
    import sqlalchemy as sa
    from sqlalchemy.exc import DBAPIError

    sp = c.begin_nested()
    with pytest.raises(DBAPIError):
        c.execute(sa.text(sql), params)
        sp.commit()
    sp.rollback()


@pg
def test_duty_location_constraints(conn):
    _mk_loc(conn, "Client One")
    _fails(
        conn, "INSERT INTO duty_locations (name, lat, lng, radius_m) VALUES ('  client one ', 1, 1, 50)"
    )  # case/space-insensitive unique
    for bad in (
        "('x', 1, 1, 50)",  # name too short
        "('Okay', 91, 1, 50)",
        "('Okay', 1, -181, 50)",
        "('Okay', 1, 1, 9)",
        "('Okay', 1, 1, 2001)",
    ):
        _fails(conn, f"INSERT INTO duty_locations (name, lat, lng, radius_m) VALUES {bad}")
    _fails(conn, "INSERT INTO duty_locations (name, lat, lng, radius_m, assign_mode) VALUES ('Okay', 1, 1, 50, 'some')")
    _fails(conn, "INSERT INTO duty_locations (name, address, lat, lng, radius_m) VALUES ('Okay', repeat('a', 301), 1, 1, 50)")
    _mk_loc(conn, "Edge", lat=90, lng=-180, radius_m=10)
    _mk_loc(conn, "Edge2", lat=-90, lng=180, radius_m=2000)


@pg
def test_assignments_cascade_and_pk(conn):
    import sqlalchemy as sa

    uid, loc = _mk_user(conn), None
    loc = _mk_loc(conn)
    conn.execute(sa.text("INSERT INTO duty_location_assignments (location_id, user_id) VALUES (:l, :u)"), {"l": loc, "u": uid})
    _fails(conn, "INSERT INTO duty_location_assignments (location_id, user_id) VALUES (:l, :u)", l=loc, u=uid)
    conn.execute(sa.text("DELETE FROM duty_locations WHERE id = :l"), {"l": loc})  # CASCADE
    assert (
        conn.execute(sa.text("SELECT count(*) FROM duty_location_assignments WHERE user_id = :u"), {"u": uid}).scalar_one() == 0
    )


@pg
def test_request_constraints(conn):
    uid = _mk_user(conn)
    ins = "INSERT INTO on_duty_requests (user_id, from_date, to_date, purpose_enc{}) VALUES (:u, '{}', '{}', 'x'::bytea{})"
    conn.execute(__import__("sqlalchemy").text(ins.format("", "2026-09-01", "2026-10-01", "")), {"u": uid})  # exactly 30 days
    _fails(conn, ins.format("", "2026-09-01", "2026-10-02", ""), u=uid)  # 31 days
    _fails(conn, ins.format("", "2026-09-05", "2026-09-04", ""), u=uid)  # end before start
    _fails(conn, ins.format(", status", "2026-09-01", "2026-09-01", ", 'done'"), u=uid)
    loc = _mk_loc(conn)
    conn.execute(
        __import__("sqlalchemy").text(
            "INSERT INTO on_duty_requests (user_id, from_date, to_date, purpose_enc, location_id) VALUES (:u, '2026-09-01', '2026-09-01', 'x'::bytea, :l)"
        ),
        {"u": uid, "l": loc},
    )
    _fails(conn, "DELETE FROM duty_locations WHERE id = :l", l=loc)  # RESTRICT: deactivate instead
    _fails(conn, "DELETE FROM users WHERE id = :u", u=uid)  # RESTRICT: soft-delete only


@pg
def test_visit_constraints_and_one_open_index(conn):
    import sqlalchemy as sa

    uid = _mk_user(conn)
    loc = _mk_loc(conn)
    att = _mk_att(conn, uid)
    v1 = _mk_visit(conn, uid, att, loc)
    # second open visit for the same user is refused (race backstop)
    _fails(
        conn,
        "INSERT INTO on_duty_visits (user_id, attendance_id, work_date, location_id, location_name, check_in_at) "
        "VALUES (:u, :a, '2026-09-01', :l, 'S', '2026-09-01 06:00+00')",
        u=uid,
        a=att,
        l=loc,
    )
    _fails(conn, "UPDATE on_duty_visits SET check_out_at = check_in_at WHERE id = :i", i=v1)  # end must be after start
    _fails(conn, "UPDATE on_duty_visits SET check_in_distance_m = -1 WHERE id = :i", i=v1)
    _fails(conn, "UPDATE on_duty_visits SET check_out_distance_m = -1 WHERE id = :i", i=v1)
    # closing it frees the slot; an auto-closed visit (check_out_at stays NULL) does not count as open either
    conn.execute(sa.text("UPDATE on_duty_visits SET check_out_at = check_in_at + interval '1 hour' WHERE id = :i"), {"i": v1})
    v2 = _mk_visit(conn, uid, att, loc, check_in_at="2026-09-01 07:00+00")
    conn.execute(sa.text("UPDATE on_duty_visits SET auto_closed = true WHERE id = :i"), {"i": v2})
    _mk_visit(conn, uid, att, loc, check_in_at="2026-09-01 08:00+00")
    # another user may have an open visit at the same time
    uid2 = _mk_user(conn)
    _mk_visit(conn, uid2, _mk_att(conn, uid2), loc)


@pg
def test_visit_selfie_single_use_and_fk_behaviour(conn):
    import sqlalchemy as sa

    cols = sa.text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'files' AND is_nullable = 'NO' AND column_default IS NULL"
    )
    needed = [r[0] for r in conn.execute(cols)]
    uid = _mk_user(conn)
    loc = _mk_loc(conn)
    att = _mk_att(conn, uid)
    # build a minimal files row from whatever NOT NULL columns lack a default
    sample = {
        "owner_id": uid,
        "purpose": "selfie",
        "content_type": "image/jpeg",
        "size_bytes": 1,
        "storage_path": "p" + uuid.uuid4().hex,
    }
    vals = {c: sample[c] for c in needed}  # KeyError here means files grew a new required column: extend `sample`
    fid = conn.execute(
        sa.text(f"INSERT INTO files ({', '.join(vals)}) VALUES ({', '.join(':' + k for k in vals)}) RETURNING id"), vals
    ).scalar_one()
    v = _mk_visit(conn, uid, att, loc, check_in_selfie_file_id=fid, check_out_at="2026-09-01 06:00+00")
    uid2 = _mk_user(conn)
    att2 = _mk_att(conn, uid2)
    _fails(  # the same selfie cannot back a second visit check-in
        conn,
        "INSERT INTO on_duty_visits (user_id, attendance_id, work_date, location_id, location_name, check_in_at, check_in_selfie_file_id) "
        "VALUES (:u, :a, '2026-09-01', :l, 'S', '2026-09-01 05:00+00', :f)",
        u=uid2,
        a=att2,
        l=loc,
        f=fid,
    )
    # deleting the file only clears the reference
    conn.execute(sa.text("DELETE FROM files WHERE id = :f"), {"f": fid})
    assert (
        conn.execute(sa.text("SELECT check_in_selfie_file_id FROM on_duty_visits WHERE id = :i"), {"i": v}).scalar_one() is None
    )
    # deleting the attendance row cascades to its visits
    conn.execute(sa.text("DELETE FROM attendance WHERE id = :a"), {"a": att})
    assert conn.execute(sa.text("SELECT count(*) FROM on_duty_visits WHERE id = :i"), {"i": v}).scalar_one() == 0


@pg
def test_attendance_work_mode_and_checkout_source_checks(conn):
    import sqlalchemy as sa

    uid = _mk_user(conn)
    att = _mk_att(conn, uid)
    row = conn.execute(sa.text("SELECT work_mode, check_out_source FROM attendance WHERE id = :a"), {"a": att}).one()
    assert tuple(row) == ("office", None)  # default mode, open row
    _fails(conn, "UPDATE attendance SET work_mode = 'remote' WHERE id = :a", a=att)
    _fails(conn, "UPDATE attendance SET check_out_source = 'office' WHERE id = :a", a=att)  # source without check_out_at
    _fails(
        conn, "UPDATE attendance SET check_out_at = check_in_at + interval '1 hour' WHERE id = :a", a=att
    )  # check_out_at without source
    _fails(
        conn,
        "UPDATE attendance SET check_out_at = check_in_at + interval '1 hour', check_out_source = 'phone' WHERE id = :a",
        a=att,
    )
    for mode, src in (("on_duty", "on_duty"), ("mixed", "office"), ("mixed", "on_duty")):
        conn.execute(
            sa.text(
                "UPDATE attendance SET work_mode = :m, check_out_at = check_in_at + interval '1 hour', check_out_source = :s WHERE id = :a"
            ),
            {"m": mode, "s": src, "a": att},
        )
    conn.execute(sa.text("UPDATE attendance SET check_out_at = NULL, check_out_source = NULL WHERE id = :a"), {"a": att})


@pg
def test_backfill_sql_semantics(conn):
    """Replays the migration's backfill statement on a scratch table (the real table already carries the new columns)."""
    import sqlalchemy as sa

    mod = _load()
    stmt = next(s for s in mod.UPGRADE.splitlines() if s.startswith("UPDATE attendance SET check_out_source"))
    conn.execute(sa.text("CREATE TEMP TABLE attendance (id int, check_out_at timestamptz, check_out_source text) ON COMMIT DROP"))
    conn.execute(sa.text("INSERT INTO attendance VALUES (1, now(), NULL), (2, NULL, NULL)"))
    conn.execute(sa.text(stmt.rstrip(";")))
    got = dict(conn.execute(sa.text("SELECT id, check_out_source FROM attendance")).all())
    assert got == {1: "office", 2: None}


@pg
def test_indexes_seed_rows_and_updated_at_triggers(conn):
    import sqlalchemy as sa

    idx = {r[0] for r in conn.execute(sa.text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"))}
    for name in (
        "uq_duty_locations_name",
        "ix_duty_locations_active",
        "ix_duty_location_assignments_user",
        "ix_on_duty_requests_user_from",
        "ix_on_duty_requests_status",
        "ix_on_duty_requests_dates",
        "ix_on_duty_visits_user_date",
        "ix_on_duty_visits_work_date",
        "ix_on_duty_visits_location_id",
        "uq_on_duty_visits_one_open",
        "ix_on_duty_visits_suspicious",
        "ix_attendance_work_mode",
    ):
        assert name in idx, name
    settings = {r[0]: r[1] for r in conn.execute(sa.text("SELECT key, value FROM settings WHERE key LIKE 'on_duty.%'"))}
    assert settings["on_duty.enabled"] is True and settings["on_duty.max_visits_per_day"] == 10
    assert len(settings) == 8
    assert conn.execute(sa.text("SELECT count(*) FROM permissions WHERE code = 'duty_location.manage'")).scalar_one() == 1
    # updated_at trigger
    loc = _mk_loc(conn)
    conn.execute(sa.text("UPDATE duty_locations SET updated_at = now() - interval '1 day' WHERE id = :i"), {"i": loc})
    conn.execute(sa.text("UPDATE duty_locations SET radius_m = 200 WHERE id = :i"), {"i": loc})
    assert conn.execute(
        sa.text("SELECT updated_at > now() - interval '1 minute' FROM duty_locations WHERE id = :i"), {"i": loc}
    ).scalar_one()
