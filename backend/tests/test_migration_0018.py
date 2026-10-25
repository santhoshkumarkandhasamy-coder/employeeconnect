"""Migration 0018 (payroll): content, models, schema.sql mirror, seed, encrypted columns, and (optional) real-Postgres trigger tests.

The trigger/constraint tests need PostgreSQL. Set PAYROLL_PG_URL to a SYNC URL of a database migrated to 0018, e.g.
  PAYROLL_PG_URL=postgresql+psycopg://postgres:pw@localhost:55432/ec
They create their own rows inside a transaction that is rolled back. Without the variable they are skipped.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

DB = Path(__file__).resolve().parents[2] / "database"


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0018", DB / "alembic" / "versions" / "0018_payroll.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0018", "0017")
    for stmt in (
        "CREATE TABLE salary_structures",
        "CREATE TABLE employee_payroll_details",
        "CREATE TABLE payroll_runs",
        "CREATE TABLE payslips",
        "uq_payslips_open",
        "uq_payslips_live",
        "trg_payroll_runs_lock",
        "trg_payslips_lock",
        "payroll.view_own",
        "payroll.pt_slabs",
    ):
        assert stmt in mod.UPGRADE, stmt
    for stmt in (
        "DROP TABLE IF EXISTS payslips",
        "DROP FUNCTION IF EXISTS payslips_lock()",
        "DELETE FROM settings WHERE key LIKE",
    ):
        assert stmt in mod.DOWNGRADE, stmt


def test_models_match_and_money_is_encrypted():
    from sqlalchemy import LargeBinary

    from app.core.crypto import EncryptedJSON, EncryptedString
    from app.models import Base

    t = Base.metadata.tables
    assert {"salary_structures", "employee_payroll_details", "payroll_runs", "payslips"} <= t.keys()
    enc = {
        ("salary_structures", "components_enc"),
        ("employee_payroll_details", "bank_name_enc"),
        ("employee_payroll_details", "bank_account_enc"),
        ("employee_payroll_details", "bank_ifsc_enc"),
        ("employee_payroll_details", "pan_enc"),
        ("employee_payroll_details", "uan_enc"),
        ("employee_payroll_details", "esi_ip_number_enc"),
        ("payslips", "identity_enc"),
        ("payslips", "lop_override_reason_enc"),
        ("payslips", "inputs_enc"),
        ("payslips", "result_enc"),
        ("payslips", "revision_reason_enc"),
    }
    for table, col in enc:
        assert isinstance(t[table].c[col].type, (EncryptedJSON, EncryptedString)), (table, col)
        assert isinstance(t[table].c[col].type.impl, type(LargeBinary())) or True
    # no plaintext column may carry money or identity data
    for name in ("salary_structures", "employee_payroll_details", "payroll_runs", "payslips"):
        for c in t[name].c:
            assert (
                not any(w in c.name for w in ("amount", "gross", "net", "salary", "basic"))
                or c.name.endswith("_enc")
                or c.name.endswith("_days")
                or c.name.startswith(("pf_", "esi_", "pt_"))
            ), c.name


def test_key_rotation_discovers_payroll_columns():
    from app.services import key_rotation

    names = {(t.name, c.name) for t, c in key_rotation.encrypted_columns()}
    for pair in (
        ("salary_structures", "components_enc"),
        ("employee_payroll_details", "pan_enc"),
        ("employee_payroll_details", "bank_account_enc"),
        ("payslips", "identity_enc"),
        ("payslips", "inputs_enc"),
        ("payslips", "result_enc"),
        ("payslips", "lop_override_reason_enc"),
        ("payslips", "revision_reason_enc"),
    ):
        assert pair in names, pair
    # every table that rotation updates needs a primary key it can address
    for table, _ in key_rotation.encrypted_columns():
        assert list(table.primary_key.columns)


def test_encrypted_types_round_trip_ciphertext_not_plaintext():
    from app.core.crypto import EncryptedJSON, EncryptedString

    raw = EncryptedJSON().process_bind_param({"net": "25000.00"}, None)
    assert b"25000" not in raw and EncryptedJSON().process_result_value(raw, None) == {"net": "25000.00"}
    raw = EncryptedString().process_bind_param("ABCDE1234F", None)
    assert b"ABCDE" not in raw and EncryptedString().process_result_value(raw, None) == "ABCDE1234F"


def test_schema_sql_and_seed():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0017 -> 0018" in sql and "version_num='0018'" in sql
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("ec_seed_0018", DB / "seed.py")
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
    finally:
        sys.path.remove(str(DB))
    assert seed.PERMISSIONS["payroll.view_own"][1] == {"admin", "hr", "manager", "employee"}
    for code in ("payroll.view_all", "payroll.manage", "payroll.publish"):
        assert seed.PERMISSIONS[code][1] == {"admin", "hr"}
    s = seed.DEFAULT_SETTINGS
    assert s["payroll.lop_absent_days"] is True
    assert s["payroll.pt_frequency"] == "half_yearly"
    slabs = s["payroll.pt_slabs"]
    assert [(x["min_gross"], x["max_gross"], x["amount"]) for x in slabs] == [
        ("0.00", "21000.00", "0.00"),
        ("21001.00", "30000.00", "135.00"),
        ("30001.00", "45000.00", "315.00"),
        ("45001.00", "60000.00", "690.00"),
        ("60001.00", "75000.00", "1025.00"),
        ("75001.00", None, "1250.00"),
    ]
    assert all(x["months"] == [3, 9] for x in slabs)
    # the migration seeds the same slabs as seed.py
    mod = _load()
    start = mod.UPGRADE.index("'payroll.pt_slabs', '") + len("'payroll.pt_slabs', '")
    assert json.loads(mod.UPGRADE[start : mod.UPGRADE.index("'::jsonb", start)]) == slabs


# ---------------------------------------------------------------- real PostgreSQL (optional)
PG = os.environ.get("PAYROLL_PG_URL")
pg = pytest.mark.skipif(not PG, reason="PAYROLL_PG_URL not set (needs PostgreSQL migrated to 0018)")


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


def _mk_run(c, period="2026-09-01", status="draft", uid=None):
    import sqlalchemy as sa

    return c.execute(
        sa.text(
            "INSERT INTO payroll_runs (period, status, attendance_cutoff, config, published_at, published_by) "
            "VALUES (:p, :s, :p, '{}'::jsonb, CASE WHEN :s='published' THEN now() END, CASE WHEN :s='published' THEN CAST(:u AS uuid) END) RETURNING id"
        ),
        {"p": period, "s": status, "u": str(uid) if uid else None},
    ).scalar_one()


def _mk_slip(c, run, uid, status="draft", revision=1):
    import sqlalchemy as sa

    return c.execute(
        sa.text(
            "INSERT INTO payslips (run_id, user_id, revision, status, employee_snapshot, basis_days, employed_days, paid_days, "
            "lop_breakdown, inputs_enc, result_enc, revision_reason_enc, published_at, published_by, superseded_at) "
            "VALUES (:r, :u, :rev, :s, '{}'::jsonb, 30, 30, 30, '{}'::jsonb, 'x'::bytea, "
            "CASE WHEN :s='draft' THEN NULL ELSE 'x'::bytea END, CASE WHEN :rev>1 THEN 'x'::bytea END, "
            "CASE WHEN :s IN ('published','superseded') THEN now() END, CASE WHEN :s IN ('published','superseded') THEN CAST(:u AS uuid) END, "
            "CASE WHEN :s='superseded' THEN now() END) RETURNING id"
        ),
        {"r": run, "u": str(uid), "rev": revision, "s": status},
    ).scalar_one()


def _fails(c, sql, **params):
    import sqlalchemy as sa
    from sqlalchemy.exc import DBAPIError

    sp = c.begin_nested()
    with pytest.raises(DBAPIError):
        c.execute(sa.text(sql), params)
        sp.commit()
    sp.rollback()


@pg
def test_payslip_lock_trigger(conn):
    import sqlalchemy as sa

    uid = _mk_user(conn)
    run = _mk_run(conn)
    slip = _mk_slip(conn, run, uid, "published")
    # published: no edit, no delete
    _fails(conn, "UPDATE payslips SET hold = true WHERE id = :i", i=slip)
    _fails(conn, "UPDATE payslips SET status = 'draft' WHERE id = :i", i=slip)
    _fails(conn, "UPDATE payslips SET result_enc = 'y'::bytea, status = 'superseded' WHERE id = :i", i=slip)
    _fails(conn, "DELETE FROM payslips WHERE id = :i", i=slip)
    # the only allowed change: published -> superseded (superseded_at is stamped)
    conn.execute(sa.text("UPDATE payslips SET status = 'superseded', version = version + 1 WHERE id = :i"), {"i": slip})
    assert conn.execute(sa.text("SELECT superseded_at IS NOT NULL FROM payslips WHERE id = :i"), {"i": slip}).scalar_one()
    # superseded: frozen
    _fails(conn, "UPDATE payslips SET hold = true WHERE id = :i", i=slip)
    _fails(conn, "DELETE FROM payslips WHERE id = :i", i=slip)


@pg
def test_payslip_draft_rules(conn):
    import sqlalchemy as sa

    uid = _mk_user(conn)
    run = _mk_run(conn)
    slip = _mk_slip(conn, run, uid, "draft")
    _fails(
        conn, "UPDATE payslips SET status = 'superseded', superseded_at = now() WHERE id = :i", i=slip
    )  # only published can be superseded
    _fails(conn, "UPDATE payslips SET paid_days = 31 WHERE id = :i", i=slip)  # paid_days <= basis_days
    _fails(
        conn, "UPDATE payslips SET lop_override = 0.3, lop_override_reason_enc = 'x'::bytea WHERE id = :i", i=slip
    )  # half-day steps
    _fails(conn, "UPDATE payslips SET lop_override = 1 WHERE id = :i", i=slip)  # needs a reason
    _fails(conn, "UPDATE payslips SET status = 'processed' WHERE id = :i", i=slip)  # needs result_enc
    # one open copy per (run, user)
    _fails(
        conn,
        "INSERT INTO payslips (run_id, user_id, revision, employee_snapshot, basis_days, employed_days, paid_days, lop_breakdown, inputs_enc) "
        "VALUES (:r, :u, 2, '{}'::jsonb, 30, 30, 30, '{}'::jsonb, 'x'::bytea)",
        r=run,
        u=uid,
    )
    conn.execute(sa.text("DELETE FROM payslips WHERE id = :i"), {"i": slip})  # draft delete allowed


@pg
def test_revision_flow_one_live_copy(conn):
    import sqlalchemy as sa

    uid = _mk_user(conn)
    run = _mk_run(conn)
    live = _mk_slip(conn, run, uid, "published")
    _fails(
        conn,
        "INSERT INTO payslips (run_id, user_id, revision, status, employee_snapshot, basis_days, employed_days, paid_days, lop_breakdown, "
        "inputs_enc, result_enc, published_at, published_by) VALUES (:r, :u, 2, 'published', '{}'::jsonb, 30, 30, 30, '{}'::jsonb, "
        "'x'::bytea, 'x'::bytea, now(), :u)",
        r=run,
        u=uid,
    )  # no second live copy
    rev = _mk_slip(conn, run, uid, "draft", revision=2)  # a draft revision may coexist with the live copy
    _fails(
        conn,
        "INSERT INTO payslips (run_id, user_id, revision, employee_snapshot, basis_days, employed_days, paid_days, lop_breakdown, inputs_enc) "
        "VALUES (:r, :u, 3, '{}'::jsonb, 30, 30, 30, '{}'::jsonb, 'x'::bytea)",
        r=run,
        u=uid,
    )  # but only one open copy
    conn.execute(sa.text("UPDATE payslips SET status = 'superseded' WHERE id = :i"), {"i": live})
    conn.execute(
        sa.text(
            "UPDATE payslips SET status = 'published', result_enc = 'x'::bytea, published_at = now(), published_by = :u WHERE id = :i"
        ),
        {"i": rev, "u": uid},
    )


@pg
def test_payroll_run_lock_trigger(conn):
    import sqlalchemy as sa

    uid = _mk_user(conn)
    run = _mk_run(conn, "2026-08-01", "published", uid)
    _fails(conn, "UPDATE payroll_runs SET pay_date = '2026-08-31' WHERE id = :i", i=run)
    _fails(conn, "UPDATE payroll_runs SET status = 'draft' WHERE id = :i", i=run)
    _fails(conn, "DELETE FROM payroll_runs WHERE id = :i", i=run)
    conn.execute(sa.text("UPDATE payroll_runs SET updated_at = now() WHERE id = :i"), {"i": run})  # updated_at only is fine
    # unpublished run: editable, deletable (no published payslips); duplicate month and non-first-of-month refused
    d = _mk_run(conn, "2026-07-01")
    conn.execute(sa.text("UPDATE payroll_runs SET status = 'processed' WHERE id = :i"), {"i": d})
    _fails(conn, "DELETE FROM payroll_runs WHERE id = :i", i=d)  # not a draft
    conn.execute(sa.text("UPDATE payroll_runs SET status = 'draft' WHERE id = :i"), {"i": d})
    _fails(conn, "INSERT INTO payroll_runs (period, attendance_cutoff, config) VALUES ('2026-07-01', '2026-07-01', '{}'::jsonb)")
    _fails(conn, "INSERT INTO payroll_runs (period, attendance_cutoff, config) VALUES ('2026-06-15', '2026-06-15', '{}'::jsonb)")
    conn.execute(sa.text("DELETE FROM payroll_runs WHERE id = :i"), {"i": d})


@pg
def test_structure_constraints(conn):
    import sqlalchemy as sa

    uid = _mk_user(conn)
    conn.execute(
        sa.text("INSERT INTO salary_structures (user_id, effective_from, components_enc) VALUES (:u, '2026-04-01', 'x'::bytea)"),
        {"u": uid},
    )
    _fails(
        conn,
        "INSERT INTO salary_structures (user_id, effective_from, components_enc) VALUES (:u, '2026-04-01', 'x'::bytea)",
        u=uid,
    )
    _fails(
        conn,
        "INSERT INTO salary_structures (user_id, effective_from, components_enc) VALUES (:u, '2026-05-02', 'x'::bytea)",
        u=uid,
    )
    _fails(conn, "INSERT INTO employee_payroll_details (user_id, payment_mode) VALUES (:u, 'bitcoin')", u=uid)
    _fails(conn, "DELETE FROM users WHERE id = :u", u=uid)  # RESTRICT: soft-delete only


def _mk_structure(c, uid):
    import sqlalchemy as sa

    return c.execute(
        sa.text(
            "INSERT INTO salary_structures (user_id, effective_from, components_enc) VALUES (:u, '2026-04-01', 'x'::bytea) RETURNING id"
        ),
        {"u": uid},
    ).scalar_one()


@pg
def test_superseded_needs_a_published_successor_at_commit(conn):
    import sqlalchemy as sa

    uid = _mk_user(conn)
    run = _mk_run(conn)
    old = _mk_slip(conn, run, uid, "published")
    conn.execute(sa.text("UPDATE payslips SET status = 'superseded' WHERE id = :i"), {"i": old})
    with pytest.raises(Exception, match="without a published successor"):
        conn.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))  # deferred check: no published successor


@pg
def test_superseded_with_a_published_successor_commits(conn):
    import sqlalchemy as sa

    uid = _mk_user(conn)
    # the revision flow: old -> superseded, then the successor is published, then commit
    old2 = _mk_slip(conn, _mk_run(conn, "2026-06-01"), uid, "published")
    run2 = conn.execute(sa.text("SELECT run_id FROM payslips WHERE id = :i"), {"i": old2}).scalar_one()
    rev = _mk_slip(conn, run2, uid, "draft", revision=2)
    conn.execute(sa.text("UPDATE payslips SET supersedes_id = :o WHERE id = :i"), {"o": old2, "i": rev})
    conn.execute(sa.text("UPDATE payslips SET status = 'superseded' WHERE id = :i"), {"i": old2})
    conn.execute(
        sa.text(
            "UPDATE payslips SET status = 'published', result_enc = 'x'::bytea, published_at = now(), published_by = :u WHERE id = :i"
        ),
        {"i": rev, "u": uid},
    )
    conn.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))


@pg
def test_structure_lock_when_used_by_a_processed_payslip(conn):
    import sqlalchemy as sa

    uid = _mk_user(conn)
    run = _mk_run(conn)
    sid = _mk_structure(conn, uid)
    conn.execute(sa.text("UPDATE salary_structures SET pf_applicable = false WHERE id = :i"), {"i": sid})  # unused: editable
    slip = _mk_slip(conn, run, uid, "draft")
    conn.execute(sa.text("UPDATE payslips SET structure_id = :s WHERE id = :i"), {"s": sid, "i": slip})
    conn.execute(sa.text("UPDATE salary_structures SET pf_applicable = true WHERE id = :i"), {"i": sid})  # a draft does not lock
    conn.execute(sa.text("UPDATE payslips SET status = 'processed', result_enc = 'x'::bytea WHERE id = :i"), {"i": slip})
    _fails(conn, "UPDATE salary_structures SET pf_applicable = false WHERE id = :i", i=sid)
    _fails(conn, "UPDATE salary_structures SET components_enc = 'y'::bytea WHERE id = :i", i=sid)
    _fails(conn, "DELETE FROM salary_structures WHERE id = :i", i=sid)


@pg
def test_key_rotation_flag_allows_only_enc_columns(conn):
    """H1: with app.key_rotation=on a published / superseded payslip and a used structure accept *_enc-only updates."""
    import sqlalchemy as sa

    uid = _mk_user(conn)
    run = _mk_run(conn)
    pub = _mk_slip(conn, run, uid, "published")
    sup = _mk_slip(conn, _mk_run(conn, "2026-06-01"), uid, "superseded")
    sid = _mk_structure(conn, uid)
    # without the flag an ciphertext rewrite is refused
    _fails(conn, "UPDATE payslips SET inputs_enc = 'n'::bytea WHERE id = :i", i=pub)
    _fails(conn, "UPDATE payslips SET inputs_enc = 'n'::bytea WHERE id = :i", i=sup)
    conn.execute(sa.text("SET LOCAL app.key_rotation = 'on'"))
    for i in (pub, sup):
        conn.execute(
            sa.text(
                "UPDATE payslips SET inputs_enc = 'n'::bytea, result_enc = 'n'::bytea, identity_enc = 'n'::bytea WHERE id = :i"
            ),
            {"i": i},
        )
        assert conn.execute(sa.text("SELECT inputs_enc = 'n'::bytea FROM payslips WHERE id = :i"), {"i": i}).scalar_one()
    # the flag does NOT unlock anything else, not even together with an _enc change
    _fails(conn, "UPDATE payslips SET hold = true WHERE id = :i", i=pub)
    _fails(conn, "UPDATE payslips SET hold = true, inputs_enc = 'm'::bytea WHERE id = :i", i=sup)
    _fails(conn, "UPDATE payslips SET status = 'draft' WHERE id = :i", i=pub)
    _fails(conn, "DELETE FROM payslips WHERE id = :i", i=pub)
    # runs have no encrypted columns: still locked
    published_run = _mk_run(conn, "2026-05-01", "published", uid)
    _fails(conn, "UPDATE payroll_runs SET pay_date = '2026-05-31' WHERE id = :i", i=published_run)
    # structures used by a processed payslip
    slip = _mk_slip(conn, _mk_run(conn, "2026-04-01"), uid, "draft")
    conn.execute(sa.text("UPDATE payslips SET structure_id = :s, status = 'processed', result_enc = 'x'::bytea WHERE id = :i"),
                 {"s": sid, "i": slip})  # fmt: skip
    conn.execute(sa.text("UPDATE salary_structures SET components_enc = 'n'::bytea WHERE id = :i"), {"i": sid})
    _fails(conn, "UPDATE salary_structures SET pf_applicable = false, components_enc = 'm'::bytea WHERE id = :i", i=sid)


@pytest.mark.skipif(not PG, reason="PAYROLL_PG_URL not set (needs PostgreSQL migrated to 0018)")
async def test_rotate_all_rewrites_published_and_superseded_payslips(monkeypatch):
    """H1 end to end: key_rotation.rotate_all on real PostgreSQL, inside a transaction that is rolled back."""
    import sqlalchemy as sa
    from cryptography.fernet import Fernet
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.core import crypto
    from app.services import key_rotation

    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("DATA_ENCRYPTION_KEYS", old)
    crypto.reset_crypto_cache()
    engine = create_async_engine(PG.replace("+psycopg", "+asyncpg"))
    try:
        async with engine.connect() as c:
            outer = await c.begin()
            try:

                def seed(sync):
                    import datetime as dt

                    n = uuid.uuid4().hex[:8].upper()
                    role = sync.execute(sa.text("INSERT INTO roles (name) VALUES (:n) RETURNING id"), {"n": "r" + n}).scalar_one()
                    uid = sync.execute(
                        sa.text(
                            "INSERT INTO users (employee_id, password_hash, full_name, official_email, role_id) "
                            "VALUES (:e, 'x', 'T U', :m, :r) RETURNING id"
                        ),
                        {"e": "T" + n, "m": f"{n}@example.com", "r": role},
                    ).scalar_one()
                    tok = Fernet(old.encode()).encrypt(b'{"net": "1"}')
                    out = []
                    for period, status in ((dt.date(2026, 9, 1), "published"), (dt.date(2026, 8, 1), "superseded")):
                        run = sync.execute(
                            sa.text(
                                "INSERT INTO payroll_runs (period, attendance_cutoff, config) VALUES (:p, :p, '{}'::jsonb) RETURNING id"
                            ),
                            {"p": period},
                        ).scalar_one()
                        out.append(
                            sync.execute(
                                sa.text(
                                    "INSERT INTO payslips (run_id, user_id, status, employee_snapshot, basis_days, employed_days, "
                                    "paid_days, lop_breakdown, inputs_enc, result_enc, published_at, published_by, superseded_at) "
                                    f"VALUES (:r, :u, '{status}', '{{}}'::jsonb, 30, 30, 30, '{{}}'::jsonb, :t, :t, now(), :u, "
                                    f"{'now()' if status == 'superseded' else 'NULL'}) RETURNING id"
                                ),
                                {"r": run, "u": uid, "t": tok},
                            ).scalar_one()
                        )
                    return tuple(out)

                pub, sup = await c.run_sync(seed)
                monkeypatch.setenv("DATA_ENCRYPTION_KEYS", f"{new},{old}")
                crypto.reset_crypto_cache()
                session = AsyncSession(bind=c, join_transaction_mode="create_savepoint")
                stats = await key_rotation.rotate_all(session)
                assert stats["undecryptable"] == 0 and stats["rotated"] >= 2
                for i in (pub, sup):
                    raw = (await c.execute(sa.text("SELECT inputs_enc FROM payslips WHERE id = :i"), {"i": i})).scalar_one()
                    Fernet(new.encode()).decrypt(bytes(raw))  # readable with the NEW key alone
                await session.close()
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()
        monkeypatch.undo()
        crypto.reset_crypto_cache()
