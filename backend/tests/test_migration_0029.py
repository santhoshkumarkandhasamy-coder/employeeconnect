"""Migration 0029 (TDS engine: tax computation snapshots, TDS challan details, settings; plan 17 work package 5c).

Trigger behaviour needs PostgreSQL (verified by hand on a scratch postgres:16, see the work report). Here the SQL text, the model
metadata, the settings and schema.sql are cross-checked, and the CHECKs SQLite can evaluate are exercised through the ORM.
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
MODEL_SRC = (Path(__file__).resolve().parents[1] / "app" / "models" / "payroll_tax.py").read_text(encoding="utf-8")
CHALLAN_SRC = (Path(__file__).resolve().parents[1] / "app" / "models" / "payroll.py").read_text(encoding="utf-8")
SETTINGS = {
    "payroll.tds_engine_enabled": "false",
    "payroll.tds_engine_mode": '"suggest"',
    "payroll.tds_one_time_in_month": "true",
    "payroll.tds_proofs_from_month": "1",
    "payroll.tds_include_other_income": "false",
}


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0029", DB / "alembic" / "versions" / "0029_tds_engine.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0029", "0028")
    sys.path.insert(0, str(DB))
    try:
        from migration_sql import split_sql  # type: ignore

        up, down = split_sql(mod.UPGRADE), split_sql(mod.DOWNGRADE)
    finally:
        sys.path.remove(str(DB))
    assert all(up) and all(down)
    assert sum(s.startswith("CREATE FUNCTION payroll_tax_computations_lock(") for s in up) == 1
    assert "DROP FUNCTION IF EXISTS payroll_tax_computations_lock();" in mod.DOWNGRADE
    assert sum(s.startswith("DO $$") for s in up) == 1 and sum(s.startswith("DO $$") for s in down) == 1
    assert not any(s.startswith(("BEGIN", "DECLARE", "IF ")) for s in up + down)
    d = mod.DOWNGRADE
    assert d.lstrip().startswith("DO $$") and "cannot downgrade 0029" in d
    guard = d.split("$$;")[0]
    assert "EXISTS (SELECT 1 FROM payroll_tax_computations)" in guard
    for col in ("bsr_code_enc", "challan_serial_enc", "deposited_on", "prev_bsr_code_enc", "prev_challan_serial_enc", "prev_deposited_on"):
        assert f"{col} IS NOT NULL" in guard, col
        assert f"DROP COLUMN {col};" in d, col
    # constraints go before the columns they use; table after settings
    assert d.index("DROP CONSTRAINT ck_statutory_challans_tds_fields") < d.index("ALTER TABLE statutory_challans DROP COLUMN bsr_code_enc")
    assert d.index("DROP CONSTRAINT ck_statutory_challan_events_tds_fields") < d.index("DROP COLUMN prev_bsr_code_enc")
    assert "DROP TABLE IF EXISTS payroll_tax_computations;" in d
    for k in SETTINGS:
        assert f"'{k}'" in d, k
    assert "SET NULL" not in mod.UPGRADE and "pg_trigger_depth" not in mod.UPGRADE
    for body in re.findall(r"RAISE EXCEPTION '([^']*(?:''[^']*)*)'", mod.UPGRADE):
        assert not re.search(r"(?<!%)%(?!%)", body.replace("%%", "")), body
    assert "(y + 1) %% 100" in mod.UPGRADE  # the modulo is escaped for the DDL layer


def test_tax_computation_table_and_lock():
    up = _load().UPGRADE
    tbl = up.split("CREATE TABLE payroll_tax_computations (")[1].split("\n);")[0]
    for needle in (
        "payslip_id uuid NOT NULL REFERENCES payslips(id) ON DELETE CASCADE",
        "user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT",
        "rules_id uuid NOT NULL REFERENCES tax_year_rules(id) ON DELETE RESTRICT",
        "computation_enc bytea NOT NULL",
        "suggested_enc bytea NOT NULL",
        "override_reason_enc bytea",
        "declaration_version integer,",
        "applied boolean NOT NULL DEFAULT false",
        "CONSTRAINT uq_payroll_tax_computations_payslip UNIQUE (payslip_id)",
        "CHECK (fy ~ '^[0-9]{4}-[0-9]{2}$')",
        "CHECK (regime IN ('old','new'))",
        "CHECK (mode IN ('suggest','apply'))",
        "CHECK (NOT applied OR mode = 'apply')",
        "CHECK (override_reason_enc IS NULL OR mode = 'apply')",
        "CHECK (declaration_version IS NULL OR declaration_version >= 1)",
        "CHECK (rules_version >= 1)",
    ):
        assert needle in tbl, needle
    lock = up.split("CREATE FUNCTION payroll_tax_computations_lock")[1].split("$$;")[0]
    for needle in (
        "IF FOUND AND slip.status <> 'draft'",  # delete only while the payslip is draft or gone (cascade)
        "NEW.payslip_id IS DISTINCT FROM OLD.payslip_id OR NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.fy IS DISTINCT FROM OLD.fy",
        "app.key_rotation",
        "payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD))",
        "slip.status <> 'draft'",  # frozen once the payslip is processed
        "slip.user_id IS DISTINCT FROM NEW.user_id",
        "CASE WHEN extract(month FROM slip.period) < 4 THEN 1 ELSE 0 END",  # April-March FY
        "lpad(((y + 1) %% 100)::text, 2, '0')",
        "rl.status NOT IN ('verified','superseded')",
        "rl.version IS DISTINCT FROM NEW.rules_version",
        "rl.fy IS DISTINCT FROM NEW.fy",
    ):
        assert needle in lock, needle
    # key rotation is checked before the freeze so it works for processed payslips
    assert lock.index("app.key_rotation") < lock.index("slip.status <> 'draft'", lock.index("app.key_rotation"))
    assert "BEFORE INSERT OR UPDATE OR DELETE ON payroll_tax_computations" in up
    assert "CREATE TRIGGER trg_payroll_tax_computations_updated_at BEFORE UPDATE ON payroll_tax_computations" in up
    assert "REVOKE DELETE, TRUNCATE ON payroll_tax_computations FROM app_user" in up
    for ix in ("user_fy", "rules_id", "created_by", "updated_by"):
        assert f"ix_payroll_tax_computations_{ix}" in up, ix


def test_challan_columns_and_settings():
    mod = _load()
    up = mod.UPGRADE
    for stmt in (
        "ALTER TABLE statutory_challans ADD COLUMN bsr_code_enc bytea;",
        "ALTER TABLE statutory_challans ADD COLUMN challan_serial_enc bytea;",
        "ALTER TABLE statutory_challans ADD COLUMN deposited_on date;",
        "ALTER TABLE statutory_challan_events ADD COLUMN prev_bsr_code_enc bytea;",
        "ALTER TABLE statutory_challan_events ADD COLUMN prev_challan_serial_enc bytea;",
        "ALTER TABLE statutory_challan_events ADD COLUMN prev_deposited_on date;",
        "ALTER TABLE statutory_challan_events ADD COLUMN bsr_code_enc bytea;",
        "ALTER TABLE statutory_challan_events ADD COLUMN challan_serial_enc bytea;",
        "ALTER TABLE statutory_challan_events ADD COLUMN deposited_on date;",
    ):
        assert stmt in up, stmt
    assert "CHECK ((type = 'tds' AND status = 'paid') OR (bsr_code_enc IS NULL AND challan_serial_enc IS NULL AND deposited_on IS NULL))" in up
    assert "GRANT UPDATE (bsr_code_enc, challan_serial_enc, prev_bsr_code_enc, prev_challan_serial_enc) ON statutory_challan_events TO app_user" in up
    for key, sql in SETTINGS.items():
        assert f"('{key}', '{sql}'::jsonb) ON CONFLICT (key) DO NOTHING" in up, key
    assert "INSERT INTO permissions" not in up


def test_models_match_migration():
    from app.core.crypto import EncryptedJSON, EncryptedString
    from app.models import Base

    up = _load().UPGRADE
    t = Base.metadata.tables["payroll_tax_computations"]
    for idx in t.indexes:
        assert idx.name in up, idx.name
    for con in t.constraints:
        if con.name:
            assert con.name in up, con.name
    for col in t.columns:
        assert col.name in up, col.name
    fk_cols = {c.name for fk in t.foreign_key_constraints for c in fk.columns}
    indexed = {c.name for i in t.indexes for c in i.columns} | {c.name for c in t.primary_key.columns}
    indexed |= {c.name for con in t.constraints if con.__class__.__name__ == "UniqueConstraint" for c in con.columns}
    assert fk_cols <= indexed, fk_cols - indexed
    cls_src = MODEL_SRC.split("class PayrollTaxComputation")[1]
    for n in re.findall(r'name="([a-z_0-9]+)"', cls_src) + re.findall(r'Index\(\s*"([a-z_0-9]+)"', cls_src):
        assert n in up, n  # includes the checks the SQLite harness drops
    assert {c.name for c in t.columns if c.name.endswith("_enc")} == {"computation_enc", "suggested_enc", "override_reason_enc"}
    assert isinstance(t.c.computation_enc.type, EncryptedJSON)
    assert isinstance(t.c.suggested_enc.type, EncryptedString) and isinstance(t.c.override_reason_enc.type, EncryptedString)
    fk = {c.parent.name: c.ondelete for c in t.foreign_keys}
    assert fk["payslip_id"] == "CASCADE" and fk["rules_id"] == "RESTRICT" and fk["user_id"] == "RESTRICT"
    for table, cols in (
        ("statutory_challans", ("bsr_code_enc", "challan_serial_enc")),
        ("statutory_challan_events", ("bsr_code_enc", "challan_serial_enc", "prev_bsr_code_enc", "prev_challan_serial_enc")),
    ):
        for c in cols:
            assert isinstance(Base.metadata.tables[table].c[c].type, EncryptedString), (table, c)
    assert "deposited_on" in Base.metadata.tables["statutory_challans"].c
    assert "prev_deposited_on" in Base.metadata.tables["statutory_challan_events"].c
    for n in re.findall(r'name="(ck_statutory_challan[a-z_]*(?:tds_fields|bsr_pair|deposited_on))"', CHALLAN_SRC):
        assert n in up, n


def test_schema_sql_mirror():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0028 -> 0029" in sql and "version_num='0029'" in sql
    assert "CREATE TABLE payroll_tax_computations (" in sql
    assert "CREATE FUNCTION payroll_tax_computations_lock()" in sql
    assert "REVOKE DELETE, TRUNCATE ON payroll_tax_computations FROM app_user" in sql
    assert "ck_statutory_challans_tds_fields" in sql and "prev_bsr_code_enc" in sql
    for key in SETTINGS:
        assert f"'{key}'" in sql, key
    assert "tax computation %s is frozen" not in sql and "(y + 1) % 100" in sql  # %% resolved by the DDL layer


# ---- CHECK constraints SQLite can evaluate, through the ORM ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_orm_defaults_and_checks(engine):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import PayrollRun, PayrollTaxComputation, Payslip, Role, StatutoryChallan, TaxYearRule, User

    async with AsyncSession(engine, expire_on_commit=False) as s:
        role = Role(name=f"r{uuid.uuid4().hex[:6]}")
        s.add(role)
        await s.flush()
        u = User(employee_id=f"T{uuid.uuid4().hex[:6]}", password_hash="x", full_name="U", role_id=role.id)
        s.add(u)
        rule = TaxYearRule(fy="2026-27", version=1, rules={}, rules_sha256="a" * 64)
        run = PayrollRun(period=dt.date(2026, 11, 1), attendance_cutoff=dt.date(2026, 11, 30), config={})
        s.add_all([rule, run])
        await s.flush()
        slip = Payslip(run_id=run.id, user_id=u.id, employee_snapshot={}, basis_days=30, employed_days=30, paid_days=30, lop_breakdown={}, inputs={})
        s.add(slip)
        await s.flush()

        def comp(**kw):
            base = dict(payslip_id=slip.id, user_id=u.id, fy="2026-27", rules_id=rule.id, rules_version=1, regime="new", proofs_digest="d",
                        mode="suggest", computation={"annual": {}}, suggested="100.00")  # fmt: skip
            base.update(kw)
            return PayrollTaxComputation(**base)

        c = comp()
        s.add(c)
        await s.flush()
        assert c.applied is False and c.override_reason is None and c.declaration_version is None
        assert c.computation == {"annual": {}} and c.suggested == "100.00"  # encrypted at rest, transparent in python
        await s.commit()

        async def refused(obj):
            async with AsyncSession(engine) as s2:
                s2.add(obj)
                with pytest.raises(IntegrityError):
                    await s2.flush()

        await refused(comp())  # one computation per payslip
        slip2 = Payslip(run_id=run.id, user_id=u.id, revision=2, employee_snapshot={}, basis_days=30, employed_days=30, paid_days=30,
                        lop_breakdown={}, inputs={}, revision_reason="r")  # fmt: skip
        async with AsyncSession(engine, expire_on_commit=False) as s3:
            s3.add(slip2)
            await s3.commit()
        await refused(comp(payslip_id=slip2.id, mode="auto"))
        await refused(comp(payslip_id=slip2.id, regime="hybrid"))
        await refused(comp(payslip_id=slip2.id, rules_version=0))
        await refused(comp(payslip_id=slip2.id, declaration_version=0))
        await refused(comp(payslip_id=slip2.id, applied=True))  # applied only in mode apply
        await refused(comp(payslip_id=slip2.id, override_reason="why"))  # override only in mode apply

        # TDS challan details: only a paid TDS challan, serial and BSR together
        def ch(**kw):
            base = dict(type="tds", period=dt.date(2026, 9, 1), amount="1.00", status="paid", paid_on=dt.date(2026, 10, 7))
            base.update(kw)
            return StatutoryChallan(**base)

        ok = ch(bsr_code="0510308", challan_serial="00123", deposited_on=dt.date(2026, 10, 7))
        async with AsyncSession(engine, expire_on_commit=False) as s4:
            s4.add(ok)
            await s4.commit()
        assert ok.bsr_code == "0510308" and ok.challan_serial == "00123"
        await refused(ch(period=dt.date(2026, 8, 1), bsr_code="0510308"))  # BSR without serial
        await refused(ch(period=dt.date(2026, 8, 1), type="pf", bsr_code="0510308", challan_serial="1"))  # not a TDS challan
        await refused(ch(period=dt.date(2026, 8, 1), status="unpaid", paid_on=None, deposited_on=dt.date(2026, 10, 7)))  # unpaid
