"""Migration 0024 (salary increments, increment batches, structure provenance; plan 16 work package 4a).

The trigger / privilege behaviour needs PostgreSQL (not available in this harness); here the SQL text, the model metadata, the seed
catalogue and the generated schema.sql are cross-checked. The CHECK constraints that SQLite can evaluate are exercised through the
ORM in test_payroll_increments.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "database"
TABLES = ("salary_increment_batches", "salary_increments")


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0024", DB / "alembic" / "versions" / "0024_salary_increments.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0024", "0023")
    for t in TABLES:
        assert f"CREATE TABLE {t} (" in mod.UPGRADE, t
        assert f"DROP TABLE IF EXISTS {t};" in mod.DOWNGRADE, t
    sys.path.insert(0, str(DB))
    try:
        from migration_sql import split_sql  # type: ignore

        up, down = split_sql(mod.UPGRADE), split_sql(mod.DOWNGRADE)
        assert all(up) and all(down)
        # the trigger function and the DO block are kept whole (semicolons inside $$ ... $$)
        assert sum(s.startswith("CREATE FUNCTION salary_increments_lock") for s in up) == 1
        assert sum(s.startswith("DO $$") for s in up) == 1
        assert not any(s.startswith("BEGIN") or s.startswith("DECLARE") for s in up)
    finally:
        sys.path.remove(str(DB))
    # downgrade order: dependants first (structures lose their FK columns before the increment tables go)
    down = mod.DOWNGRADE
    assert down.index("DROP COLUMN IF EXISTS increment_id") < down.index("DROP TABLE IF EXISTS salary_increments;")
    assert down.index("DROP TABLE IF EXISTS salary_increments;") < down.index("DROP TABLE IF EXISTS salary_increment_batches;")
    assert "DROP FUNCTION IF EXISTS salary_increments_lock()" in down


def test_maker_checker_checks_live_in_the_database():
    up = _load().UPGRADE
    assert "CONSTRAINT ck_salary_increments_maker_not_employee CHECK (created_by <> user_id)" in up
    assert (
        "CONSTRAINT ck_salary_increments_checker CHECK (status NOT IN ('approved','rejected') OR (decided_by IS NOT NULL "
        "AND decided_by <> user_id AND decided_by IS DISTINCT FROM created_by AND decided_by IS DISTINCT FROM last_edited_by "
        "AND decided_by IS DISTINCT FROM submitted_by))" in up
    )
    assert "last_edited_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT" in up
    assert "contributors text[] NOT NULL DEFAULT '{}'" in up
    assert (
        "CONSTRAINT ck_salary_increments_checker_history CHECK (status NOT IN ('approved','rejected') "
        "OR decided_by::text <> ALL (contributors))" in up
    )
    assert "created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT" in up.split("CREATE TABLE salary_increments (")[1]
    assert "CONSTRAINT ck_salary_increments_remove CHECK ((kind = 'remove') = (proposed_enc IS NULL))" in up
    assert (
        "CONSTRAINT ck_salary_increments_rejected_comment CHECK (status <> 'rejected' OR decision_comment_enc IS NOT NULL)" in up
    )
    assert "(kind = 'import') = (file_sha256 IS NOT NULL)" in up
    assert (
        "CREATE UNIQUE INDEX uq_salary_increments_live ON salary_increments (user_id, effective_from) WHERE status IN ('draft','submitted')"
        in up
    )
    assert (
        "CREATE UNIQUE INDEX uq_salary_increment_batches_import_sha ON salary_increment_batches (file_sha256) WHERE kind = 'import'"
        in up
    )


def test_lock_trigger_and_privileges():
    up = _load().UPGRADE
    body = up.split("CREATE FUNCTION salary_increments_lock")[1].split("$$;")[0]
    for needle in (
        "OLD.status NOT IN ('approved','rejected','cancelled')",
        "app.key_rotation",
        "payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD))",
        "'structure_id','base_structure_id','replaces_structure_id'",
        "OLD.status <> 'draft'",
        "IF pg_trigger_depth() > 1 AND (to_jsonb(NEW) -> col) = 'null'::jsonb",  # L2: only the FK action nulls a structure id
    ):
        assert needle in body, needle
    # the exception compares everything but *_enc, updated_at included: key rotation keeps updated_at unchanged (commit f3cdaa5)
    assert "trg_salary_increments_lock BEFORE UPDATE OR DELETE ON salary_increments" in up
    # alphabetical trigger order: the lock sees the row before set_updated_at bumps updated_at
    assert "trg_salary_increments_lock" < "trg_salary_increments_updated_at"
    assert "trg_salary_increments_updated_at BEFORE UPDATE ON salary_increments" in up
    for t in TABLES:
        assert f"REVOKE DELETE, TRUNCATE ON {t} FROM app_user" in up, t


def test_structures_provenance_and_event_checks():
    mod = _load()
    up, down = mod.UPGRADE, mod.DOWNGRADE
    assert "ALTER TABLE salary_structures ADD COLUMN source text NOT NULL DEFAULT 'manual'" in up
    assert "ADD COLUMN increment_id uuid REFERENCES salary_increments(id) ON DELETE RESTRICT" in up
    assert "ADD COLUMN import_batch_id uuid REFERENCES salary_increment_batches(id) ON DELETE RESTRICT" in up
    assert "CHECK ((source = 'increment') = (increment_id IS NOT NULL))" in up
    # circular: the increment tables are created BEFORE the structure columns that point at them
    assert up.index("CREATE TABLE salary_increments (") < up.index("ADD COLUMN increment_id")
    assert "structure_id uuid REFERENCES salary_structures(id) ON DELETE SET NULL" in up
    # events: new item types and actions in the recreated CHECKs, old ones restored on downgrade
    assert "'overtime','increment','fnf'" in up and "'carry','submit','withdraw','settle','revoke','remove'" in up
    assert (
        "'overtime','increment'" not in down
        and "'revoke'" not in down.split("ck_payroll_item_events_action CHECK")[1].split(";")[0]
    )
    assert (
        "DISABLE TRIGGER trg_payroll_item_events_no_update_delete" in down
        and "ENABLE TRIGGER trg_payroll_item_events_no_update_delete" in down
    )


def test_encrypted_columns():
    up = _load().UPGRADE
    inc = up.split("CREATE TABLE salary_increments (")[1].split(");")[0]
    for col in (
        "percent_enc bytea",
        "proposed_enc bytea",
        "old_gross_enc bytea",
        "new_gross_enc bytea",
        "reason_enc bytea NOT NULL",
        "decision_comment_enc bytea",
    ):
        assert col in inc, col
    batch = up.split("CREATE TABLE salary_increment_batches (")[1].split(");")[0]
    assert "percent_enc bytea" in batch and "reason_enc bytea NOT NULL" in batch


def test_permission_and_settings():
    mod = _load()
    up = mod.UPGRADE
    assert "'payroll.approve'" in up and "r.name IN ('admin', 'hr') AND p.code = 'payroll.approve'" in up
    assert "DELETE FROM permissions WHERE code = 'payroll.approve'" in mod.DOWNGRADE
    keys = ("structure_changes_need_approval", "split_basic_pct", "split_hra_pct_of_basic", "split_allowance_code", "split_allowance_name",
            "increment_max_pct", "increment_backdate_months", "ctc_include_gratuity", "gratuity_min_years", "gratuity_cap",
            "gratuity_wage_basis")  # fmt: skip
    from app.services import payroll_settings as pset

    for k in keys:
        assert f"payroll.{k}" in up and f"payroll.{k}" in mod.DOWNGRADE, k
        assert f"payroll.{k}" in pset.DEFAULTS, k
    # the same defaults as the migration seeds
    assert (
        "('payroll.structure_changes_need_approval', 'true'::jsonb)" in up
        and pset.DEFAULTS["payroll.structure_changes_need_approval"] is True
    )
    assert "('payroll.split_basic_pct', '50'::jsonb)" in up and pset.DEFAULTS["payroll.split_basic_pct"] == 50
    assert "('payroll.split_hra_pct_of_basic', '40'::jsonb)" in up and pset.DEFAULTS["payroll.split_hra_pct_of_basic"] == 40
    assert (
        "('payroll.split_allowance_code', '\"SPECIAL\"'::jsonb)" in up
        and pset.DEFAULTS["payroll.split_allowance_code"] == "SPECIAL"
    )
    assert "('payroll.increment_max_pct', '50'::jsonb)" in up and pset.DEFAULTS["payroll.increment_max_pct"] == 50
    assert "('payroll.increment_backdate_months', '12'::jsonb)" in up and pset.DEFAULTS["payroll.increment_backdate_months"] == 12
    assert "('payroll.gratuity_cap', '\"2000000.00\"'::jsonb)" in up and pset.DEFAULTS["payroll.gratuity_cap"] == "2000000.00"
    assert "('payroll.gratuity_wage_basis', '\"basic\"'::jsonb)" in up and pset.DEFAULTS["payroll.gratuity_wage_basis"] == "basic"
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("ec_seed24", DB / "seed.py")
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
    finally:
        sys.path.remove(str(DB))
    assert seed.PERMISSIONS["payroll.approve"][1] == {"admin", "hr"}


def test_models_match_migration():
    from app.core.crypto import EncryptedJSON, EncryptedString
    from app.models import Base

    up = _load().UPGRADE
    for name in (*TABLES, "salary_structures"):
        t = Base.metadata.tables[name]
        for idx in t.indexes:
            if name == "salary_structures" and not idx.name.startswith(
                ("ix_salary_structures_increment", "ix_salary_structures_import")
            ):
                continue  # older indexes belong to 0018
            assert idx.name in up, (name, idx.name)
        for con in t.constraints:
            if con.name and name != "salary_structures":
                assert con.name in up, (name, con.name)
        if name != "salary_structures":
            for col in t.columns:
                assert col.name in up, (name, col.name)
            fk_cols = {c.name for fk in t.foreign_key_constraints for c in fk.columns}
            indexed = {c.name for i in t.indexes for c in i.columns} | {c.name for c in t.primary_key.columns}
            if name == "salary_increments":
                indexed.add(
                    "user_id"
                )  # covered by (user_id, created_at); the partial unique index is dropped in the SQLite harness
            assert fk_cols <= indexed, (name, fk_cols - indexed)
    sc = Base.metadata.tables["salary_structures"]
    for col in ("source", "increment_id", "import_batch_id"):
        assert col in sc.c and col in up
    for c in ("ck_salary_structures_source", "ck_salary_structures_increment"):
        assert c in {x.name for x in sc.constraints} and c in up
    inc = Base.metadata.tables["salary_increments"]
    for c, typ in (("percent_enc", EncryptedString), ("proposed_enc", EncryptedJSON), ("old_gross_enc", EncryptedString),
                   ("new_gross_enc", EncryptedString), ("reason_enc", EncryptedString), ("decision_comment_enc", EncryptedString)):  # fmt: skip
        assert isinstance(inc.c[c].type, typ), c
    assert isinstance(Base.metadata.tables["salary_increment_batches"].c.percent_enc.type, EncryptedString)
    ev = {c.name: str(c.sqltext) for c in Base.metadata.tables["payroll_item_events"].constraints if c.name}
    assert "'increment','fnf'" in ev["ck_payroll_item_events_item_type"]
    assert "'settle','revoke','remove'" in ev["ck_payroll_item_events_action"]


def test_schema_sql_mirror():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0023 -> 0024" in sql and "version_num='0024'" in sql
    for t in TABLES:
        assert f"CREATE TABLE {t} (" in sql, t
        assert f"REVOKE DELETE, TRUNCATE ON {t} FROM app_user" in sql, t
    assert "CREATE FUNCTION salary_increments_lock()" in sql
    assert "ck_salary_increments_checker" in sql
    assert "ALTER TABLE salary_structures ADD COLUMN increment_id uuid REFERENCES salary_increments(id) ON DELETE RESTRICT" in sql
    assert "'payroll.approve'" in sql
    # the %% escapes of the DDL layer are resolved in the generated script
    assert "salary increment % is % and locked" in sql
