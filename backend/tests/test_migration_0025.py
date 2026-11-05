"""Migration 0025 (full and final settlement; plan 16 work package 4b).

The trigger / privilege behaviour needs PostgreSQL (not available in this harness; it was probed by hand against PostgreSQL 16 when the
migration was written, see the commit notes); here the SQL text, the model metadata, the settings catalogue and the generated schema.sql
are cross-checked. The CHECK constraints that SQLite can evaluate are exercised through the ORM below.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

DB = Path(__file__).resolve().parents[2] / "database"
MODEL_SRC = "".join(
    (Path(__file__).resolve().parents[1] / "app" / "models" / f).read_text(encoding="utf-8") for f in ("payroll.py", "payroll_items.py")
)


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0025", DB / "alembic" / "versions" / "0025_fnf_settlements.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0025", "0024")
    assert "CREATE TABLE fnf_settlements (" in mod.UPGRADE
    assert "DROP TABLE IF EXISTS fnf_settlements;" in mod.DOWNGRADE
    sys.path.insert(0, str(DB))
    try:
        from migration_sql import split_sql  # type: ignore

        up, down = split_sql(mod.UPGRADE), split_sql(mod.DOWNGRADE)
        assert all(up) and all(down)
        # trigger functions and DO blocks are kept whole (semicolons inside $$ ... $$)
        assert sum(s.startswith("CREATE FUNCTION fnf_settlements_lock") for s in up) == 1
        assert sum(s.startswith("CREATE FUNCTION payroll_fnf_id_immutable") for s in up) == 1
        assert sum(s.startswith("DO $$") for s in up) == 1
        assert sum(s.startswith("DO $$") for s in down) == 1
        assert not any(s.startswith(("BEGIN", "DECLARE", "IF ")) for s in up + down)
    finally:
        sys.path.remove(str(DB))
    d = mod.DOWNGRADE
    # the guard runs first; the tagged columns go before the table they reference
    assert d.lstrip().startswith("DO $$") and "cannot downgrade 0025" in d
    assert d.index("ALTER TABLE payroll_one_time_items DROP COLUMN IF EXISTS fnf_id") < d.index("DROP TABLE IF EXISTS fnf_settlements;")
    assert d.index("ALTER TABLE leave_encashments DROP COLUMN IF EXISTS fnf_id") < d.index("DROP TABLE IF EXISTS fnf_settlements;")
    assert d.index("ALTER TABLE payroll_loan_installments DROP COLUMN IF EXISTS fnf_id") < d.index("DROP TABLE IF EXISTS fnf_settlements;")
    assert "DROP FUNCTION IF EXISTS fnf_settlements_lock()" in d and "DROP FUNCTION IF EXISTS payroll_fnf_id_immutable()" in d
    # the old one-time item code CHECK is restored without NOTICE_RECOVERY, after the system rows are deleted
    assert d.index("DELETE FROM payroll_one_time_items WHERE code = 'NOTICE_RECOVERY'") < d.index(
        "ADD CONSTRAINT ck_payroll_one_time_items_code CHECK (code IN ('BONUS','INCENTIVE','OTHER_EARNING','OTHER_DEDUCTION'))"
    )
    assert "ALTER TABLE employee_payroll_details DROP COLUMN IF EXISTS last_working_day" in d
    assert "ALTER TABLE employee_payroll_details DROP COLUMN IF EXISTS notice_period_days" in d


def test_maker_checker_checks_live_in_the_database():
    up = _load().UPGRADE
    assert "CONSTRAINT ck_fnf_settlements_maker_not_employee CHECK (created_by <> user_id)" in up
    # the checker must differ from the employee, creator, last editor, submitter and every contributor, for EVERY status
    assert (
        "CONSTRAINT ck_fnf_settlements_checker CHECK (decided_by IS NULL OR (decided_by <> user_id "
        "AND decided_by IS DISTINCT FROM created_by AND decided_by IS DISTINCT FROM last_edited_by "
        "AND decided_by IS DISTINCT FROM submitted_by))" in up
    )
    assert (
        "CONSTRAINT ck_fnf_settlements_checker_history CHECK (decided_by IS NULL OR decided_by::text <> ALL (contributors))" in up
    )
    assert "last_edited_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT" in up
    assert "created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT" in up
    assert "contributors text[] NOT NULL DEFAULT '{}'" in up
    assert "CONSTRAINT ck_fnf_settlements_decided_status CHECK (status NOT IN ('approved','settled') OR decided_by IS NOT NULL)" in up
    assert "CONSTRAINT ck_fnf_settlements_summary CHECK (status NOT IN ('approved','settled') OR summary_enc IS NOT NULL)" in up
    assert "CONSTRAINT ck_fnf_settlements_settled CHECK ((status = 'settled') = (final_payslip_id IS NOT NULL))" in up
    assert "CONSTRAINT ck_fnf_settlements_cancelled CHECK (status <> 'cancelled' OR cancel_reason_enc IS NOT NULL)" in up
    assert "CREATE UNIQUE INDEX uq_fnf_settlements_live ON fnf_settlements (user_id) WHERE status IN ('draft','submitted','approved')" in up
    assert "CONSTRAINT ck_fnf_settlements_resignation CHECK (resignation_date IS NULL OR resignation_date <= last_working_day)" in up
    assert "CONSTRAINT ck_fnf_settlements_shortfall_reason CHECK (exit_reason = 'resignation' OR shortfall_days = 0)" in up
    assert "CONSTRAINT ck_fnf_settlements_shortfall_waived CHECK (NOT notice_waived OR shortfall_days = 0)" in up
    # settlement month: LWD month .. LWD month + 3, immutable arithmetic (no date_trunc -> timestamptz)
    assert "make_date(extract(year from last_working_day)::int" in up and "INTERVAL '3 months'" in up
    assert "date_trunc" not in up


def test_lock_trigger_and_privileges():
    up = _load().UPGRADE
    body = up.split("CREATE FUNCTION fnf_settlements_lock")[1].split("$$;")[0]
    for needle in (
        "NEW.status <> 'draft'",  # an insert must be a draft
        "OLD.status <> 'draft'",  # only a draft can be deleted
        "NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.created_by IS DISTINCT FROM OLD.created_by",
        "NOT (NEW.contributors @> OLD.contributors)",  # contributors only grow
        "app.key_rotation",
        "payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD))",
        "'draft>submitted','draft>cancelled','submitted>draft','submitted>approved','submitted>cancelled','approved>draft','approved>settled'",
        "OLD.status = 'submitted' AND NEW.status = 'submitted'",  # frozen between submit and approve
        "OLD.status = 'approved' AND NEW.status = 'approved'",
        "ARRAY['status','decided_by','decided_at','decision_comment_enc','summary_enc']",  # approve changes only the decision columns
        "allowed || ARRAY['final_payslip_id']",  # arrays, not an untyped literal (malformed array literal on PostgreSQL 16)
        "j_new IS DISTINCT FROM j_old",
    ):
        assert needle in body, needle
    # no FK of the table is ON DELETE SET NULL, hence no pg_trigger_depth() escape
    assert "pg_trigger_depth" not in body
    assert "SET NULL" not in up.split("CREATE TABLE fnf_settlements (")[1].split("CREATE UNIQUE INDEX")[0]
    assert "trg_fnf_settlements_lock BEFORE INSERT OR UPDATE OR DELETE ON fnf_settlements" in up
    # alphabetical trigger order: the lock sees the row before set_updated_at bumps updated_at
    assert "trg_fnf_settlements_lock" < "trg_fnf_settlements_updated_at"
    assert "trg_fnf_settlements_updated_at BEFORE UPDATE ON fnf_settlements" in up
    assert "REVOKE DELETE, TRUNCATE ON fnf_settlements FROM app_user" in up


def test_tagged_columns_and_reserved_code():
    mod = _load()
    up = mod.UPGRADE
    for t in ("payroll_one_time_items", "leave_encashments", "payroll_loan_installments"):
        assert f"ALTER TABLE {t} ADD COLUMN fnf_id uuid REFERENCES fnf_settlements(id) ON DELETE RESTRICT" in up, t
        assert f"CREATE INDEX ix_{t}_fnf_id ON {t} (fnf_id)" in up, t
        assert f"trg_{t}_fnf_id_immutable BEFORE UPDATE OF fnf_id ON {t}" in up, t
        assert f"DROP TRIGGER IF EXISTS trg_{t}_fnf_id_immutable ON {t}" in mod.DOWNGRADE, t
    body = up.split("CREATE FUNCTION payroll_fnf_id_immutable")[1].split("$$;")[0]
    assert "OLD.fnf_id IS NOT NULL AND NEW.fnf_id IS DISTINCT FROM OLD.fnf_id" in body
    assert "CHECK (code IN ('BONUS','INCENTIVE','OTHER_EARNING','OTHER_DEDUCTION','NOTICE_RECOVERY'))" in up
    assert "CHECK (code <> 'NOTICE_RECOVERY' OR fnf_id IS NOT NULL)" in up  # system-only
    assert "CHECK (fnf_id IS NULL OR batch_id IS NULL)" in up
    assert "ck_leave_encashments_fnf_source CHECK (fnf_id IS NULL OR source = 'fnf')" in up
    assert "ck_payroll_loan_installments_fnf_kind CHECK (fnf_id IS NULL OR kind = 'foreclosure')" in up
    # payroll_item_events: 0024 already allows 'fnf' and the F&F actions, 0025 does not touch the CHECKs
    assert "payroll_item_events" not in up
    prev = (DB / "alembic" / "versions" / "0024_salary_increments.py").read_text(encoding="utf-8")
    assert "'overtime','increment','fnf'" in prev and "'submit','withdraw','settle','revoke','remove'" in prev


def test_details_columns_and_encrypted_columns():
    up = _load().UPGRADE
    assert "ALTER TABLE employee_payroll_details ADD COLUMN last_working_day date" in up
    assert "ALTER TABLE employee_payroll_details ADD COLUMN notice_period_days smallint" in up
    assert "notice_period_days IS NULL OR notice_period_days BETWEEN 0 AND 365" in up
    tbl = up.split("CREATE TABLE fnf_settlements (")[1].split(");")[0]
    for col in (
        "plan_enc bytea NOT NULL",
        "summary_enc bytea",
        "remarks_enc bytea",
        "decision_comment_enc bytea",
        "cancel_reason_enc bytea",
    ):
        assert col in tbl, col


def test_settings():
    mod = _load()
    up = mod.UPGRADE
    from app.services import payroll_settings as pset

    keys = ("notice_period_days", "notice_recovery_basis", "notice_recovery_divisor")
    for k in keys:
        assert f"payroll.{k}" in up and f"payroll.{k}" in mod.DOWNGRADE and f"payroll.{k}" in pset.DEFAULTS, k
    assert "('payroll.notice_period_days', '30'::jsonb)" in up and pset.DEFAULTS["payroll.notice_period_days"] == 30
    assert "('payroll.notice_recovery_basis', '\"gross\"'::jsonb)" in up and pset.DEFAULTS["payroll.notice_recovery_basis"] == "gross"
    assert "('payroll.notice_recovery_divisor', '30'::jsonb)" in up and pset.DEFAULTS["payroll.notice_recovery_divisor"] == 30
    # ranges of 18.11
    assert pset.validate("payroll.notice_period_days", 365) == 365 and pset.validate("payroll.notice_period_days", 0) == 0
    assert pset.validate("payroll.notice_recovery_divisor", 31) == 31
    assert pset.validate("payroll.notice_recovery_basis", "basic") == "basic"
    from app.core.errors import AppError

    for key, bad in (
        ("payroll.notice_period_days", 366),
        ("payroll.notice_period_days", -1),
        ("payroll.notice_recovery_divisor", 0),
        ("payroll.notice_recovery_divisor", 32),
        ("payroll.notice_recovery_basis", "net"),
    ):
        with pytest.raises(AppError):
            pset.validate(key, bad)


def test_models_match_migration():
    from app.core.crypto import EncryptedJSON, EncryptedString
    from app.models import Base

    up = _load().UPGRADE
    t = Base.metadata.tables["fnf_settlements"]
    for idx in t.indexes:
        assert idx.name in up, idx.name
    for con in t.constraints:
        if con.name:
            assert con.name in up, con.name
    for col in t.columns:
        assert col.name in up, col.name
    fk_cols = {c.name for fk in t.foreign_key_constraints for c in fk.columns}
    indexed = {c.name for i in t.indexes for c in i.columns} | {c.name for c in t.primary_key.columns}
    assert fk_cols <= indexed, fk_cols - indexed
    assert isinstance(t.c.plan_enc.type, EncryptedJSON) and isinstance(t.c.summary_enc.type, EncryptedJSON)
    for c in ("remarks_enc", "decision_comment_enc", "cancel_reason_enc"):
        assert isinstance(t.c[c].type, EncryptedString), c
    for name, cols, chk in (
        ("payroll_one_time_items", ("fnf_id",), ("ck_payroll_one_time_items_notice_system", "ck_payroll_one_time_items_fnf_batch")),
        ("leave_encashments", ("fnf_id",), ("ck_leave_encashments_fnf_source",)),
        ("payroll_loan_installments", ("fnf_id",), ("ck_payroll_loan_installments_fnf_kind",)),
        ("employee_payroll_details", ("last_working_day", "notice_period_days"),
         ("ck_employee_payroll_details_lwd_year", "ck_employee_payroll_details_notice_days")),
    ):  # fmt: skip
        tab = Base.metadata.tables[name]
        for c in cols:
            assert c in tab.c and c in up
        for c in chk:
            # the SQLite harness (conftest) drops checks it cannot evaluate (DATE literals ...): look in the model source as well
            assert c in up and (c in {x.name for x in tab.constraints} or c in MODEL_SRC), c
        if "fnf_id" in cols:
            assert any(i.name == f"ix_{name}_fnf_id" for i in tab.indexes), name
    ck = {c.name: str(c.sqltext) for c in Base.metadata.tables["payroll_one_time_items"].constraints if c.name}
    assert "'NOTICE_RECOVERY'" in ck["ck_payroll_one_time_items_code"]


def test_schema_sql_mirror():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0024 -> 0025" in sql and "version_num='0025'" in sql
    assert "CREATE TABLE fnf_settlements (" in sql
    assert "REVOKE DELETE, TRUNCATE ON fnf_settlements FROM app_user" in sql
    assert "CREATE FUNCTION fnf_settlements_lock()" in sql and "CREATE FUNCTION payroll_fnf_id_immutable()" in sql
    assert "ck_fnf_settlements_checker_history" in sql
    assert "ALTER TABLE payroll_one_time_items ADD COLUMN fnf_id uuid REFERENCES fnf_settlements(id) ON DELETE RESTRICT" in sql
    assert "'payroll.notice_recovery_divisor'" in sql
    # the %% escapes of the DDL layer are resolved in the generated script
    assert "final settlement % is % and locked" in sql


# ---- CHECK constraints SQLite can evaluate, through the ORM ----------------------------------------------------------------


async def _users(session, n: int):
    from app.models import Role, User

    role = Role(name=f"r{uuid.uuid4().hex[:6]}")
    session.add(role)
    await session.flush()
    out = []
    for i in range(n):
        u = User(employee_id=f"T{uuid.uuid4().hex[:6]}", password_hash="x", full_name=f"U{i}", role_id=role.id)
        session.add(u)
        out.append(u)
    await session.flush()
    return out


def _fnf(emp, maker, **kw):
    from app.models import FnfSettlement

    base = dict(
        user_id=emp.id, last_working_day=dt.date(2026, 12, 15), exit_reason="resignation", settlement_period=dt.date(2026, 12, 1),
        notice_required_days=30, plan={"items": []}, created_by=maker.id, contributors=[str(maker.id)],
    )  # fmt: skip
    base.update(kw)
    return FnfSettlement(**base)


@pytest.mark.asyncio
async def test_orm_checks(engine):
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(engine, expire_on_commit=False) as s:
        emp, maker, checker = await _users(s, 3)
        ok = _fnf(emp, maker)
        s.add(ok)
        await s.flush()
        assert ok.last_edited_by == maker.id  # defaults to the creator
        assert ok.status == "draft" and ok.version == 1 and ok.shortfall_days == 0 and ok.notice_waived is False
        await s.commit()

        async def refused(**kw):
            async with AsyncSession(engine) as s2:
                s2.add(_fnf(emp, maker, **kw))
                with pytest.raises(IntegrityError):
                    await s2.flush()

        await refused(created_by=emp.id)  # maker is the employee
        await refused(exit_reason="fired")
        await refused(status="settled")  # settled needs a final payslip
        await refused(status="cancelled")  # cancelled needs a reason
        await refused(status="approved", submitted_by=checker.id, submitted_at=dt.datetime(2026, 12, 1, tzinfo=dt.UTC))  # no decision
        await refused(notice_required_days=366)
        await refused(shortfall_days=31)  # more than the notice
        await refused(notice_required_days=30, shortfall_days=5, notice_waived=True)
        await refused(resignation_date=dt.date(2026, 12, 20))  # after the last working day
        await refused(decided_by=maker.id, decided_at=dt.datetime(2026, 12, 1, tzinfo=dt.UTC))  # checker = creator
        await refused(decided_by=emp.id, decided_at=dt.datetime(2026, 12, 1, tzinfo=dt.UTC))  # checker = employee
        await refused(decided_by=checker.id)  # decided_by without decided_at
        await refused(version=0)
