"""Migration 0020 (statutory challans): content, model, schema.sql mirror, encrypted columns."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "database"


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0020", DB / "alembic" / "versions" / "0020_statutory_challans.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0020", "0019")
    for stmt in (
        "CREATE TABLE statutory_challans",
        "uq_statutory_challans_type_period",
        "ck_statutory_challans_type",
        "ck_statutory_challans_paid",
        "amount_enc bytea NOT NULL",
        "trg_statutory_challans_updated_at",
    ):
        assert stmt in mod.UPGRADE, stmt
    assert "DROP TABLE IF EXISTS statutory_challans" in mod.DOWNGRADE
    sys.path.insert(0, str(DB))
    try:
        from migration_sql import split_sql  # type: ignore

        for stmt in split_sql(mod.UPGRADE) + split_sql(mod.DOWNGRADE):
            assert stmt
    finally:
        sys.path.remove(str(DB))


def test_model_matches_migration_and_money_is_encrypted():
    from app.core.crypto import EncryptedString
    from app.models import Base

    t = Base.metadata.tables["statutory_challans"]
    assert isinstance(t.c.amount_enc.type, EncryptedString) and isinstance(t.c.reference_enc.type, EncryptedString)
    assert not t.c.amount_enc.nullable
    mod = _load()
    for idx in t.indexes:
        assert idx.name in mod.UPGRADE, idx.name
    for con in t.constraints:
        if con.name:
            assert con.name in mod.UPGRADE, con.name
    # FK columns are indexed
    indexed = {c.name for i in t.indexes for c in i.columns}
    assert {"created_by", "updated_by"} <= indexed


def test_key_rotation_discovers_challan_columns():
    from app.services import key_rotation

    names = {(t.name, c.name) for t, c in key_rotation.encrypted_columns()}
    assert ("statutory_challans", "amount_enc") in names and ("statutory_challans", "reference_enc") in names


def test_schema_sql_mirror():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0019 -> 0020" in sql and "version_num='0020'" in sql
    assert "CREATE TABLE statutory_challans" in sql
