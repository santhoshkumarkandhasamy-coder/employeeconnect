"""Migration 0023 (reimbursement claims, overtime days, loan installment numbering; plan 15 work package 3b).

The trigger / privilege behaviour needs PostgreSQL (not available in this harness); here the SQL text, the model metadata, the seed
catalogue and the generated schema.sql are cross-checked.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "database"
TABLES = ("reimbursement_claims", "overtime_days")


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0023", DB / "alembic" / "versions" / "0023_reimbursements_overtime.py")
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

        up, down = split_sql(mod.UPGRADE), split_sql(mod.DOWNGRADE)
        assert all(up) and all(down)
        # the replaced function and the DO block are kept whole
        assert sum(s.startswith("CREATE OR REPLACE FUNCTION payroll_item_paid_lock") for s in up) == 1
        assert sum(s.startswith("CREATE OR REPLACE FUNCTION payroll_item_paid_lock") for s in down) == 1
        assert sum(s.startswith("DO $$") for s in up) == 1
    finally:
        sys.path.remove(str(DB))


def test_paid_lock_append_only_and_revokes():
    up = _load().UPGRADE
    for t in TABLES:
        assert f"trg_{t}_paid_lock BEFORE UPDATE OR DELETE ON {t}" in up, t
        assert f"REVOKE DELETE, TRUNCATE ON {t} FROM app_user" in up, t
        assert f"trg_{t}_updated_at BEFORE UPDATE ON {t}" in up, t
        assert f"CONSTRAINT ck_{t}_payslip CHECK ((status IN ('processing','paid')) = (payslip_id IS NOT NULL))" in up, t
    body = up.split("CREATE OR REPLACE FUNCTION payroll_item_paid_lock")[1].split("$$;")[0]
    for allowed in (
        "payslip_id",
        "version",
        "updated_by",
        "updated_at",
        "trigger_structure_id",
        "attendance_id",
        "app.key_rotation",
    ):
        assert allowed in body, allowed
    # the old function body is restored on downgrade (without the attendance exception)
    down_body = _load().DOWNGRADE.split("CREATE OR REPLACE FUNCTION payroll_item_paid_lock")[1].split("$$;")[0]
    assert "attendance_id" not in down_body and "trigger_structure_id" in down_body


def test_claims_table_rules():
    up = _load().UPGRADE
    for col in ("amount_enc bytea NOT NULL", "approved_amount_enc bytea", "description_enc bytea NOT NULL", "decision_comment_enc bytea",
                "bill_file_id uuid REFERENCES files(id) ON DELETE RESTRICT", "manager_step text NOT NULL DEFAULT 'pending'"):  # fmt: skip
        assert col in up, col
    assert "status NOT IN ('approved','processing','paid') OR (period IS NOT NULL AND approved_amount_enc IS NOT NULL)" in up
    assert (
        "uq_reimbursement_claims_live_bill ON reimbursement_claims (bill_file_id) WHERE bill_file_id IS NOT NULL AND status NOT IN ('rejected','cancelled')"
        in up
    )
    assert "'reimbursement_bill'" in up.split("ck_files_purpose")[-1]
    assert "ADD COLUMN overtime_eligible boolean NOT NULL DEFAULT false" in up


def test_overtime_table_rules():
    up = _load().UPGRADE
    assert "CONSTRAINT uq_overtime_days_user_date UNIQUE (user_id, work_date)" in up
    assert "attendance_id uuid REFERENCES attendance(id) ON DELETE SET NULL" in up
    assert "status IN ('computed','rejected') OR approved_minutes IS NOT NULL" in up
    assert "approved_minutes <= computed_minutes" in up
    assert "flags text[] NOT NULL DEFAULT '{}'" in up and "'ATTENDANCE_CHANGED'" in up


def test_loan_installment_numbering_fix():
    mod = _load()
    up, down = mod.UPGRADE, mod.DOWNGRADE
    assert "DROP CONSTRAINT uq_payroll_loan_installments_loan_seq" in up
    assert "DROP CONSTRAINT ck_payroll_loan_installments_seq" in up
    assert "ADD CONSTRAINT ck_payroll_loan_installments_seq CHECK (seq >= 1)" in up
    assert (
        "CREATE UNIQUE INDEX uq_payroll_loan_installments_live_seq ON payroll_loan_installments (loan_id, seq) "
        "WHERE status NOT IN ('cancelled','skipped')" in up
    )
    assert "ADD CONSTRAINT ck_payroll_loan_installments_seq CHECK (seq BETWEEN 1 AND 120)" in down
    assert "UNIQUE (loan_id, seq)" in down and "DROP INDEX IF EXISTS uq_payroll_loan_installments_live_seq" in down


def test_permission_and_settings():
    mod = _load()
    up = mod.UPGRADE
    assert (
        "'reimbursement.submit'" in up and "FROM roles r, permissions p WHERE p.code = 'reimbursement.submit'" in up
    )  # every role
    assert "DELETE FROM permissions WHERE code = 'reimbursement.submit'" in mod.DOWNGRADE
    keys = ("reimbursement_categories", "reimbursement_max_age_days", "reimbursement_max_claims_per_month", "overtime_enabled",
            "overtime_multiplier", "overtime_month_days", "overtime_min_minutes", "overtime_rounding_minutes",
            "overtime_max_hours_month", "overtime_offday_full", "overtime_include_auto_closed")  # fmt: skip
    for k in keys:
        assert f"payroll.{k}" in up and f"payroll.{k}" in mod.DOWNGRADE, k
    assert "('payroll.overtime_enabled', 'false'::jsonb)" in up  # off by default
    from app.services import payroll_settings as pset

    for k in keys:
        assert f"payroll.{k}" in pset.DEFAULTS
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("ec_seed23", DB / "seed.py")
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
    finally:
        sys.path.remove(str(DB))
    assert seed.PERMISSIONS["reimbursement.submit"][1] == {"admin", "hr", "manager", "employee"}


def test_models_match_migration():
    from app.core.crypto import EncryptedString
    from app.models import Base

    up = _load().UPGRADE
    for name in TABLES:
        t = Base.metadata.tables[name]
        for idx in t.indexes:
            assert idx.name in up, (name, idx.name)
        for con in t.constraints:
            if con.name:
                assert con.name in up, (name, con.name)
        for col in t.columns:
            assert col.name in up, (name, col.name)
        fk_cols = {c.name for fk in t.foreign_key_constraints for c in fk.columns}
        indexed = {c.name for i in t.indexes for c in i.columns} | {c.name for c in t.primary_key.columns}
        indexed |= {c.name for con in t.constraints if con.__class__.__name__ == "UniqueConstraint" for c in con.columns}
        if name == "reimbursement_claims":
            indexed.add("bill_file_id")  # covered by the partial unique index, which the SQLite harness drops
        assert fk_cols <= indexed, (name, fk_cols - indexed)
    claims = Base.metadata.tables["reimbursement_claims"]
    for c in ("amount_enc", "approved_amount_enc", "description_enc", "decision_comment_enc"):
        assert isinstance(claims.c[c].type, EncryptedString), c
    assert isinstance(Base.metadata.tables["overtime_days"].c.comment_enc.type, EncryptedString)
    assert "overtime_eligible" in Base.metadata.tables["employee_payroll_details"].c
    inst = Base.metadata.tables["payroll_loan_installments"]
    assert "uq_payroll_loan_installments_loan_seq" not in {c.name for c in inst.constraints if c.name}
    assert "ck_files_purpose" in {c.name for c in Base.metadata.tables["files"].constraints if c.name}
    assert "reimbursement_bill" in str(
        [c for c in Base.metadata.tables["files"].constraints if c.name == "ck_files_purpose"][0].sqltext
    )


def test_schema_sql_mirror():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0022 -> 0023" in sql and "version_num='0023'" in sql
    for t in TABLES:
        assert f"CREATE TABLE {t} (" in sql, t
    assert "CREATE UNIQUE INDEX uq_payroll_loan_installments_live_seq" in sql
    assert "CREATE OR REPLACE FUNCTION payroll_item_paid_lock()" in sql
    assert "REVOKE DELETE, TRUNCATE ON reimbursement_claims FROM app_user" in sql
    assert "'reimbursement_bill'" in sql
