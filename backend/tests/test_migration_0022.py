"""Migration 0022 (statutory challan events, challan privileges): content, model, schema.sql mirror, key rotation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "database"
LATER = (Path(__file__).resolve().parents[2] / "database" / "alembic" / "versions" / "0029_tds_engine.py").read_text(encoding="utf-8")  # 0029 adds columns / constraints


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0022", DB / "alembic" / "versions" / "0022_statutory_challan_events.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0022", "0021")
    for stmt in (
        "CREATE TABLE statutory_challan_events",
        "REFERENCES statutory_challans(id) ON DELETE RESTRICT",
        "REVOKE UPDATE, DELETE, TRUNCATE ON statutory_challan_events FROM app_user",
        "REVOKE DELETE, TRUNCATE ON statutory_challans FROM app_user",
        "BEFORE TRUNCATE ON statutory_challan_events",
        "BEFORE UPDATE OR DELETE ON statutory_challan_events",
        "ck_statutory_challan_events_action",
    ):
        assert stmt in mod.UPGRADE, stmt
    assert "DROP TABLE IF EXISTS statutory_challan_events" in mod.DOWNGRADE
    sys.path.insert(0, str(DB))
    try:
        from migration_sql import split_sql  # type: ignore

        up = split_sql(mod.UPGRADE)
        assert all(up) and all(split_sql(mod.DOWNGRADE))
        # the DO block and the function body are kept whole
        assert sum(s.startswith("DO $$") for s in up) == 1
        assert sum(s.startswith("CREATE FUNCTION statutory_challan_events_lock") for s in up) == 1
    finally:
        sys.path.remove(str(DB))


def test_model_matches_migration_and_values_are_encrypted():
    from app.core.crypto import EncryptedString
    from app.models import Base

    t = Base.metadata.tables["statutory_challan_events"]
    for name in ("reference_enc", "amount_enc", "prev_reference_enc", "prev_amount_enc"):
        assert isinstance(t.c[name].type, EncryptedString), name
    assert not t.c.challan_id.nullable and not t.c.actor_id.nullable
    mod = _load()
    for idx in t.indexes:
        assert idx.name in mod.UPGRADE, idx.name
    for con in t.constraints:
        if con.name:
            assert con.name in mod.UPGRADE or con.name in LATER, con.name
    for col in t.columns:
        assert col.name in mod.UPGRADE or col.name in LATER, col.name
    indexed = {c.name for i in t.indexes for c in i.columns}
    assert {"challan_id", "actor_id"} <= indexed  # every FK column is indexed


def test_key_rotation_discovers_event_columns():
    from app.services import key_rotation

    names = {(t.name, c.name) for t, c in key_rotation.encrypted_columns()}
    for col in ("reference_enc", "amount_enc", "prev_reference_enc", "prev_amount_enc"):
        assert ("statutory_challan_events", col) in names
    mod = _load()
    # rotation must stay possible: column-level UPDATE on exactly the encrypted columns, and the trigger lets a rotation through
    assert "GRANT UPDATE (reference_enc, amount_enc, prev_reference_enc, prev_amount_enc)" in mod.UPGRADE
    assert "app.key_rotation" in mod.UPGRADE


def test_schema_sql_mirror():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0021 -> 0022" in sql and "version_num='0022'" in sql
    assert "CREATE TABLE statutory_challan_events" in sql
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON statutory_challan_events FROM app_user" in sql
    assert "REVOKE DELETE, TRUNCATE ON statutory_challans FROM app_user" in sql
