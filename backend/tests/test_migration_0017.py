"""Migration 0017 (company, license, offboarding, sessions, no OTP): content, schema.sql mirror, seed and models."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "database"


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location(
            "mig0017", DB / "alembic" / "versions" / "0017_company_console_offboarding.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0017", "0016")
    for stmt in (
        "CREATE TABLE company",
        "CREATE TABLE license",
        "INSERT INTO company (id) VALUES (1)",
        "company_logo",
        "DROP COLUMN IF EXISTS otp_hash",
        "ix_refresh_tokens_live",
        "deactivation_reason",
    ):
        assert stmt in mod.UPGRADE, stmt
    for stmt in (
        "DROP TABLE IF EXISTS license",
        "DROP TABLE IF EXISTS company",
        "ADD COLUMN otp_hash",
        "DROP INDEX IF EXISTS ix_refresh_tokens_live",
    ):
        assert stmt in mod.DOWNGRADE, stmt


def test_models_match():
    from app.models import Base, PasswordResetRequest

    t = Base.metadata.tables
    assert {"company", "license"} <= t.keys()
    assert not {"otp_hash", "otp_expires_at", "otp_attempts"} & set(PasswordResetRequest.__table__.c.keys())
    assert {"deactivated_at", "deactivated_by", "deactivation_reason"} <= set(t["users"].c.keys())
    assert {"client_type", "app_version", "platform"} <= set(t["refresh_tokens"].c.keys())


def test_schema_sql_and_seed():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0016 -> 0017" in sql and "version_num='0017'" in sql
    seed = (DB / "seed.py").read_text(encoding="utf-8")
    for code in (
        "company.manage",
        "console.access",
        "session.manage",
        "system.view",
        "license.manage",
        "console.session_max_hours",
        "console.inactivity_timeout_minutes",
        "auth.offline_cache_max_hours",
    ):
        assert code in seed, code
