"""Migration 0023 (payroll components, plan 15 work package 3a): content, models mirror it, schema.sql mirror, key rotation.

The trigger / privilege behaviour itself needs PostgreSQL (not available in this harness); here the SQL text, the model
metadata and the generated schema.sql are cross-checked.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "database"
TABLES = (
    "payroll_item_events",
    "payroll_import_batches",
    "payroll_one_time_items",
    "payroll_loans",
    "payroll_loan_installments",
    "payroll_arrears",
    "leave_encashments",
)
SOURCE_TABLES = ("payroll_one_time_items", "payroll_loan_installments", "payroll_arrears", "leave_encashments")


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0023", DB / "alembic" / "versions" / "0023_payroll_components.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0023", "0022")
    for t in TABLES:
        assert f"CREATE TABLE {t} (" in mod.UPGRADE, t
        assert f"DROP TABLE IF EXISTS {t};" in mod.DOWNGRADE, t
    sys.path.insert(0, str(DB))
    try:
        from migration_sql import split_sql  # type: ignore

        up = split_sql(mod.UPGRADE)
        assert all(up) and all(split_sql(mod.DOWNGRADE))
        # function bodies and the DO block are kept whole
        assert sum(s.startswith("CREATE FUNCTION payroll_item_paid_lock") for s in up) == 1
        assert sum(s.startswith("CREATE FUNCTION payroll_item_events_lock") for s in up) == 1
        assert sum(s.startswith("DO $$") for s in up) == 1
    finally:
        sys.path.remove(str(DB))


def test_events_are_append_only_like_0022():
    up = _load().UPGRADE
    assert "BEFORE UPDATE OR DELETE ON payroll_item_events" in up
    assert "BEFORE TRUNCATE ON payroll_item_events" in up
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON payroll_item_events FROM app_user" in up
    assert "GRANT UPDATE (amount_enc, prev_amount_enc, detail_enc) ON payroll_item_events" in up
    assert "app.key_rotation" in up  # rotation of the encrypted columns stays possible
    for action in ("create", "update", "cancel", "skip", "reschedule", "foreclose", "close", "propose", "accept", "dismiss",
                   "verify", "approve", "reject", "process", "reopen", "pay", "carry"):  # fmt: skip
        assert f"'{action}'" in up.split("ck_payroll_item_events_action")[1].split("\n")[0], action


def test_no_delete_grant_and_paid_lock_on_every_source_table():
    up = _load().UPGRADE
    for t in TABLES[1:]:
        assert f"REVOKE DELETE, TRUNCATE ON {t} FROM app_user" in up, t
    for t in SOURCE_TABLES:
        assert f"trg_{t}_paid_lock BEFORE UPDATE OR DELETE ON {t}" in up, t
        assert f"CONSTRAINT ck_{t}_payslip CHECK ((status IN ('processing','paid')) = (payslip_id IS NOT NULL))" in up, t
    body = up.split("CREATE FUNCTION payroll_item_paid_lock")[1].split("$$;")[0]
    for allowed in ("payslip_id", "version", "updated_by", "updated_at", "trigger_structure_id", "app.key_rotation"):
        assert allowed in body, allowed
    assert "OLD.status <> 'paid'" in body and "TG_OP = 'DELETE'" in body


def test_leave_changes_and_settings():
    mod = _load()
    up = mod.UPGRADE
    assert "ALTER TABLE leave_balances ADD COLUMN encashed numeric(4,1) NOT NULL DEFAULT 0" in up
    assert "CHECK (used + pending + encashed <= quota)" in up
    assert "CHECK (used + pending <= quota)" in mod.DOWNGRADE  # restored on downgrade
    for col in ("encashable boolean NOT NULL DEFAULT false", "encash_max_days_per_year numeric(4,1)",
                "encash_min_balance numeric(4,1) NOT NULL DEFAULT 0"):  # fmt: skip
        assert col in up, col
    for key in ("loan_max_tenure_months", "loan_max_active", "encashment_basis", "encashment_divisor"):
        assert f"payroll.{key}" in up and f"payroll.{key}" in mod.DOWNGRADE, key
    assert "CREATE SEQUENCE payroll_loan_ref_seq" in up and "DROP SEQUENCE IF EXISTS payroll_loan_ref_seq" in mod.DOWNGRADE


def test_models_match_migration():
    from app.core.crypto import EncryptedJSON, EncryptedString
    from app.models import Base

    # 0024 replaced the installment numbering constraints (live-only unique index), so its names count as well
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0024", DB / "alembic" / "versions" / "0024_reimbursements_overtime.py")
        m23 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m23)
    finally:
        sys.path.remove(str(DB))
    # 0026 (F&F) added fnf_id columns / indexes / checks to three of these tables and a code to the one-time item CHECK
    try:
        sys.path.insert(0, str(DB))
        spec = importlib.util.spec_from_file_location("mig0026_22", DB / "alembic" / "versions" / "0026_fnf_settlements.py")
        m25 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m25)
    finally:
        sys.path.remove(str(DB))
    up = _load().UPGRADE + m23.UPGRADE + m25.UPGRADE
    for name in TABLES:
        t = Base.metadata.tables[name]
        for idx in t.indexes:
            assert idx.name in up, (name, idx.name)
        for con in t.constraints:
            if con.name:
                assert con.name in up, (name, con.name)
        for col in t.columns:
            assert col.name in up, (name, col.name)
        # every foreign key column is indexed (events: item_id / payslip_id are deliberately not foreign keys)
        fk_cols = {c.name for fk in t.foreign_key_constraints for c in fk.columns}
        indexed = {c.name for i in t.indexes for c in i.columns} | {c.name for c in t.primary_key.columns}
        indexed |= {c.name for con in t.constraints if con.__class__.__name__ == "UniqueConstraint" for c in con.columns}
        if name == "payroll_loan_installments":
            indexed.add("loan_id")  # the live-only unique (loan_id, seq) index of 0024 is partial: the SQLite harness drops it
        assert fk_cols <= indexed, (name, fk_cols - indexed)
    # money, labels, reasons, breakdowns are encrypted columns
    enc = {
        "payroll_item_events": ("amount_enc", "prev_amount_enc", "detail_enc"),
        "payroll_one_time_items": ("label_enc", "amount_enc", "cancel_reason_enc"),
        "payroll_loans": ("principal_enc", "emi_enc", "reason_enc"),
        "payroll_loan_installments": ("amount_enc",),
        "payroll_arrears": ("amount_enc", "breakdown_enc", "dismiss_reason_enc"),
        "leave_encashments": ("daily_rate_enc", "amount_enc", "reason_enc"),
    }
    for table, cols in enc.items():
        for c in cols:
            assert isinstance(Base.metadata.tables[table].c[c].type, (EncryptedString, EncryptedJSON)), (table, c)
    lb = Base.metadata.tables["leave_balances"]
    assert "encashed" in lb.c
    assert {c.name for c in lb.constraints if c.name} >= {"ck_leave_balances_encashed", "ck_leave_balances_not_overdrawn"}
    lt = Base.metadata.tables["leave_types"]
    assert {"encashable", "encash_max_days_per_year", "encash_min_balance"} <= set(lt.c.keys())


def test_key_rotation_discovers_new_columns():
    from app.services import key_rotation

    names = {(t.name, c.name) for t, c in key_rotation.encrypted_columns()}
    for table, col in (
        ("payroll_item_events", "amount_enc"),
        ("payroll_item_events", "detail_enc"),
        ("payroll_one_time_items", "label_enc"),
        ("payroll_one_time_items", "amount_enc"),
        ("payroll_loans", "principal_enc"),
        ("payroll_arrears", "breakdown_enc"),
        ("leave_encashments", "daily_rate_enc"),
    ):
        assert (table, col) in names, (table, col)


def test_schema_sql_mirror():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0022 -> 0023" in sql and "version_num='0023'" in sql
    for t in TABLES:
        assert f"CREATE TABLE {t} (" in sql, t
    assert "CREATE FUNCTION payroll_item_paid_lock()" in sql
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON payroll_item_events FROM app_user" in sql
    assert "CHECK (used + pending + encashed <= quota)" in sql
    assert "RAISE EXCEPTION 'payroll item % is paid and locked'" in sql  # %% rendered as % in the generated mirror
