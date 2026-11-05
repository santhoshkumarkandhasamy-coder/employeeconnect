"""Migration 0026 (publish requests, payment batches, payslip delivery; plan 17 work package 5a).

The trigger / privilege behaviour needs PostgreSQL (not available in this harness; it was probed by hand against PostgreSQL 16 when the
migration was written: up / down / up, every CHECK, trigger, partial unique and the guarded downgrade). Here the SQL text, the model
metadata, the settings catalogue and the generated schema.sql are cross-checked, and the CHECK constraints that SQLite can evaluate are
exercised through the ORM.
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
MODEL_SRC = (Path(__file__).resolve().parents[1] / "app" / "models" / "payroll_controls.py").read_text(encoding="utf-8")
NEW_TABLES = (
    "payroll_publish_requests",
    "payslip_deliveries",
    "company_bank_accounts",
    "payroll_payment_batches",
    "payroll_payment_lines",
)


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0026", DB / "alembic" / "versions" / "0026_publish_requests_payments.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_chain_and_reversible():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0026", "0025")
    sys.path.insert(0, str(DB))
    try:
        from migration_sql import split_sql  # type: ignore

        up, down = split_sql(mod.UPGRADE), split_sql(mod.DOWNGRADE)
    finally:
        sys.path.remove(str(DB))
    assert all(up) and all(down)
    # trigger functions and DO blocks are kept whole (semicolons inside $$ ... $$)
    for fn in (
        "payroll_contributors_guard",
        "company_bank_accounts_lock",
        "employee_payroll_details_bank_guard",
        "payroll_publish_requests_lock",
        "payroll_publish_requests_tagged",
        "payroll_publish_requests_checker_guard",
        "payslip_deliveries_lock",
        "payroll_payment_batches_lock",
        "payroll_payment_batches_consistency",
        "payroll_payment_lines_lock",
    ):
        assert sum(s.startswith(f"CREATE FUNCTION {fn}()") for s in up) == 1, fn
        assert f"DROP FUNCTION IF EXISTS {fn}();" in mod.DOWNGRADE, fn
    assert sum(s.startswith("DO $$") for s in up) == 1 and sum(s.startswith("DO $$") for s in down) == 1
    assert not any(s.startswith(("BEGIN", "DECLARE", "IF ")) for s in up + down)
    d = mod.DOWNGRADE
    # the guard runs first; dependants go before what they reference
    assert d.lstrip().startswith("DO $$") and "cannot downgrade 0026" in d
    assert "status IN ('submitted','approved')" in d and "status NOT IN ('draft','cancelled')" in d
    for t in NEW_TABLES:
        assert f"DROP TABLE IF EXISTS {t};" in d, t
    assert d.index("DROP TABLE IF EXISTS payroll_payment_lines") < d.index("DROP TABLE IF EXISTS payroll_payment_batches")
    assert d.index("DROP TABLE IF EXISTS payroll_payment_batches") < d.index("DROP TABLE IF EXISTS company_bank_accounts")
    assert d.index("DROP TABLE IF EXISTS payroll_publish_requests") < d.index("DROP FUNCTION IF EXISTS payroll_contributors_guard")
    for col in ("payroll_runs.contributors", "payslips.contributors"):
        t, c = col.split(".")
        assert f"ALTER TABLE {t} DROP COLUMN IF EXISTS {c}" in d
    assert "ALTER TABLE employee_payroll_details DROP COLUMN IF EXISTS bank_changed_by" in d
    assert "DROP TRIGGER IF EXISTS trg_employee_payroll_details_bank_guard ON employee_payroll_details" in d
    # the events CHECKs are restored after the new history rows are deleted (append-only trigger disabled for that statement)
    assert d.index("DISABLE TRIGGER trg_payroll_item_events_no_update_delete") < d.index("DELETE FROM payroll_item_events")
    assert d.index("DELETE FROM payroll_item_events") < d.index("ENABLE TRIGGER trg_payroll_item_events_no_update_delete")
    assert d.index("ENABLE TRIGGER trg_payroll_item_events_no_update_delete") < d.index("ADD CONSTRAINT ck_payroll_item_events_action")
    assert "'export','confirm','expire','stale'" not in d.split("ck_payroll_item_events_action CHECK")[1].split(";")[0]
    # nothing in the new tables is ON DELETE SET NULL: no pg_trigger_depth() escape anywhere
    assert "SET NULL" not in mod.UPGRADE.replace("ON DELETE SET NULL of", "") and "pg_trigger_depth" not in mod.UPGRADE.replace("pg_trigger_depth() escape", "")


def test_contributors_columns_backfill_and_guard():
    up = _load().UPGRADE
    assert "ALTER TABLE payroll_runs ADD COLUMN contributors text[] NOT NULL DEFAULT '{}'" in up
    assert "ALTER TABLE payslips ADD COLUMN contributors text[] NOT NULL DEFAULT '{}'" in up
    # backfill with the lock triggers switched off (published rows are locked), switched back on afterwards
    assert up.index("DISABLE TRIGGER trg_payslips_lock") < up.index("UPDATE payslips SET contributors") < up.index("ENABLE TRIGGER trg_payslips_lock")
    assert up.index("DISABLE TRIGGER trg_payroll_runs_lock") < up.index("UPDATE payroll_runs SET contributors") < up.index("ENABLE TRIGGER trg_payroll_runs_lock")
    assert "created_by::text, updated_by::text, processed_by::text" in up and "created_by::text, processed_by::text" in up
    assert up.index("UPDATE payslips SET contributors") < up.index("CREATE TRIGGER trg_payslips_contributors")  # triggers come after the backfill
    body = up.split("CREATE FUNCTION payroll_contributors_guard")[1].split("$$;")[0]
    assert "NOT (NEW.contributors @> OLD.contributors)" in body  # only grows
    assert "OLD.status NOT IN ('draft','processed')" in body  # frozen once published / superseded
    assert "BEFORE UPDATE OF contributors ON payroll_runs" in up and "BEFORE UPDATE OF contributors ON payslips" in up


def test_publish_request_maker_checker_in_the_database():
    up = _load().UPGRADE
    tbl = up.split("CREATE TABLE payroll_publish_requests (")[1].split("\n);")[0]
    for needle in (
        "CONSTRAINT ck_payroll_publish_requests_checker CHECK (decided_by IS NULL OR decided_by <> requested_by)",
        "CONSTRAINT ck_payroll_publish_requests_checker_history CHECK (decided_by IS NULL OR decided_by::text <> ALL (makers))",
        "CONSTRAINT ck_payroll_publish_requests_requester_in_makers CHECK (decided_by IS NULL OR requested_by::text = ANY (makers))",
        "CONSTRAINT ck_payroll_publish_requests_decided_status CHECK ((status IN ('approved','rejected')) = (decided_by IS NOT NULL))",
        "CONSTRAINT ck_payroll_publish_requests_scope_payslip CHECK ((scope = 'payslip') = (payslip_id IS NOT NULL))",
        "CONSTRAINT ck_payroll_publish_requests_status CHECK (status IN ('submitted','approved','rejected','withdrawn','stale'))",
        "'RUN_REOPENED','RUN_REFRESHED','PAYSLIP_CHANGED','EXPIRED'",
        "CONSTRAINT ck_payroll_publish_requests_stale_pair CHECK ((status = 'stale') = (stale_reason IS NOT NULL))",
        "CONSTRAINT ck_payroll_publish_requests_mac CHECK (snapshot_mac ~ '^[0-9a-f]{64}$')",
        "CONSTRAINT ck_payroll_publish_requests_rejected_comment CHECK (status <> 'rejected' OR decision_comment_enc IS NOT NULL)",
        "snapshot_enc bytea NOT NULL",
        "makers text[] NOT NULL DEFAULT '{}'",
        "run_id uuid NOT NULL REFERENCES payroll_runs(id) ON DELETE RESTRICT",
        "payslip_id uuid REFERENCES payslips(id) ON DELETE RESTRICT",
    ):
        assert needle in tbl, needle
    # one open request per run (scope run) and per payslip (scope payslip)
    assert "uq_payroll_publish_requests_open_run ON payroll_publish_requests (run_id) WHERE scope = 'run' AND status = 'submitted'" in up
    assert "uq_payroll_publish_requests_open_payslip ON payroll_publish_requests (payslip_id) WHERE scope = 'payslip' AND status = 'submitted'" in up
    lock = up.split("CREATE FUNCTION payroll_publish_requests_lock")[1].split("$$;")[0]
    for needle in (
        "NEW.status <> 'submitted' OR NEW.decided_by IS NOT NULL",  # inserts are plain submitted requests
        "r.status = 'processed'",  # scope run needs a processed run
        "r.status = 'published' AND p.id = NEW.payslip_id AND p.status = 'processed'",  # scope payslip: revision / held payslip of a published run
        "NOT (NEW.makers @> OLD.makers)",
        "app.key_rotation",
        "payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD))",
        "('submitted>approved','submitted>rejected','submitted>withdrawn','submitted>stale')",
        "ARRAY['status','decided_by','decided_at','decision_comment_enc','makers']",
        "ARRAY['status','withdrawn_by','withdrawn_at']",
        "ARRAY['status','stale_reason']",
        "j_new IS DISTINCT FROM j_old",
        "cannot be deleted",
    ):
        assert needle in lock, needle
    guard = up.split("CREATE FUNCTION payroll_publish_requests_checker_guard")[1].split("$$;")[0]
    assert "NEW.decided_by::text = ANY (r.contributors)" in guard and "NEW.decided_by::text = ANY (p.contributors)" in guard
    assert "p.user_id = NEW.decided_by" in guard and "NOT p.hold" in guard
    assert "BEFORE INSERT OR UPDATE OF decided_by ON payroll_publish_requests" in up
    assert "p.run_id" in up.split("CREATE FUNCTION payroll_publish_requests_tagged")[1].split("$$;")[0]
    assert "REVOKE DELETE, TRUNCATE ON payroll_publish_requests FROM app_user" in up


def test_payment_batches_and_lines():
    up = _load().UPGRADE
    b = up.split("CREATE TABLE payroll_payment_batches (")[1].split("\n);")[0]
    for needle in (
        "CONSTRAINT uq_payroll_payment_batches_reference UNIQUE (reference)",
        "CHECK (status IN ('draft','submitted','approved','exported','closed','cancelled'))",
        "CONSTRAINT ck_payroll_payment_batches_file CHECK ((status IN ('exported','closed')) = (file_sha256 IS NOT NULL))",
        "CONSTRAINT ck_payroll_payment_batches_exported CHECK ((status IN ('exported','closed')) = (exported_by IS NOT NULL))",
        "CONSTRAINT ck_payroll_payment_batches_checker CHECK (decided_by IS NULL OR (decided_by IS DISTINCT FROM created_by "
        "AND decided_by IS DISTINCT FROM last_edited_by AND decided_by IS DISTINCT FROM submitted_by))",
        "CONSTRAINT ck_payroll_payment_batches_checker_history CHECK (decided_by IS NULL OR decided_by::text <> ALL (contributors))",
        "CONSTRAINT ck_payroll_payment_batches_contributors CHECK (created_by::text = ANY (contributors) AND last_edited_by::text = ANY (contributors) "
        "AND (submitted_by IS NULL OR submitted_by::text = ANY (contributors)))",
        "CONSTRAINT ck_payroll_payment_batches_decided_status CHECK (status NOT IN ('approved','exported','closed') OR decided_by IS NOT NULL)",
        "CONSTRAINT ck_payroll_payment_batches_live_mac CHECK (status NOT IN ('submitted','approved','exported','closed') OR lines_mac IS NOT NULL)",
        "CONSTRAINT ck_payroll_payment_batches_draft_mac CHECK (status <> 'draft' OR lines_mac IS NULL)",
        "CONSTRAINT ck_payroll_payment_batches_cancelled CHECK (status <> 'cancelled' OR cancel_reason_enc IS NOT NULL)",
        "CHECK (reference ~ '^PAY-[0-9]{4}-[0-9]{2}-[0-9]{2,4}$')",
        "format text NOT NULL DEFAULT 'generic_neft_csv'",
        "payment_type text NOT NULL DEFAULT 'NEFT'",
        "debit_account_id uuid NOT NULL REFERENCES company_bank_accounts(id) ON DELETE RESTRICT",
    ):
        assert needle in b, needle
    lock = up.split("CREATE FUNCTION payroll_payment_batches_lock")[1].split("$$;")[0]
    for needle in (
        "NEW.status <> 'draft'",
        "r.status = 'published'",
        "a.active",
        "OLD.status <> 'draft'",  # only a draft can be deleted
        "NEW.reference IS DISTINCT FROM OLD.reference",
        "NOT (NEW.contributors @> OLD.contributors)",
        "app.key_rotation",
        "('draft>submitted','draft>cancelled','submitted>draft','submitted>approved','submitted>cancelled','approved>exported','approved>cancelled','exported>closed')",
        "submitting clears an earlier decision",
        "l.status = 'pending'",  # nothing to submit
        "l.user_id = NEW.decided_by",  # the approver is paid by the batch
        "d.bank_changed_by = NEW.decided_by",  # recent bank change by the approver
        "payroll.bank_change_alert_days",
        "ARRAY['status','exported_by','exported_at','file_sha256']",
        "j_new IS DISTINCT FROM j_old",
    ):
        assert needle in lock, needle
    assert "CREATE CONSTRAINT TRIGGER trg_payroll_payment_batches_consistency AFTER INSERT OR UPDATE ON payroll_payment_batches DEFERRABLE INITIALLY DEFERRED" in up
    ln = up.split("CREATE TABLE payroll_payment_lines (")[1].split("\n);")[0]
    for needle in (
        "CONSTRAINT uq_payroll_payment_lines_batch_seq UNIQUE (batch_id, seq)",
        "batch_id uuid NOT NULL REFERENCES payroll_payment_batches(id) ON DELETE RESTRICT",
        "payslip_id uuid NOT NULL REFERENCES payslips(id) ON DELETE RESTRICT",
        "amount_enc bytea NOT NULL",
        "beneficiary_enc bytea NOT NULL",
        "'BANK_DETAILS_CHANGED','RECENT_BANK_CHANGE','REVISION_DELTA','NO_PAN'",
        "CHECK (status IN ('pending','excluded','exported','paid','failed'))",
        "CHECK (account_source = 'snapshot' OR 'BANK_DETAILS_CHANGED' = ANY (flags))",
    ):
        assert needle in ln, needle
    # one live line per payslip
    assert "uq_payroll_payment_lines_live ON payroll_payment_lines (payslip_id) WHERE status IN ('pending','exported','paid')" in up
    ll = up.split("CREATE FUNCTION payroll_payment_lines_lock")[1].split("$$;")[0]
    for needle in (
        "ps.run_id IS DISTINCT FROM b_run",  # tagged row: payslip of the batch's run
        "ps.user_id IS DISTINCT FROM NEW.user_id",  # tagged row: same employee
        "ps.status <> 'published' OR ps.hold",
        "b_status IS DISTINCT FROM 'draft'",
        "NEW.batch_id IS DISTINCT FROM OLD.batch_id OR NEW.payslip_id IS DISTINCT FROM OLD.payslip_id OR NEW.user_id IS DISTINCT FROM OLD.user_id",
        "('pending>excluded','excluded>pending','pending>exported','exported>paid','exported>failed')",
        "ARRAY['status','amount_enc','beneficiary_enc','flags','account_source']",
        "ARRAY['status','reference_enc','paid_on']",
        "j_new IS DISTINCT FROM j_old",
    ):
        assert needle in ll, needle
    for t in ("payroll_payment_batches", "payroll_payment_lines", "company_bank_accounts", "payslip_deliveries"):
        assert f"REVOKE DELETE, TRUNCATE ON {t} FROM app_user" in up, t
    # alphabetical trigger order: the lock sees the row before set_updated_at bumps updated_at
    assert "trg_payroll_payment_batches_lock" < "trg_payroll_payment_batches_updated_at"


def test_delivery_accounts_and_details_marker():
    up = _load().UPGRADE
    dl = up.split("CREATE TABLE payslip_deliveries (")[1].split("\n);")[0]
    assert "payslip_id uuid PRIMARY KEY REFERENCES payslips(id) ON DELETE RESTRICT" in dl
    assert "push_count BETWEEN 0 AND 10" in dl and "reminders BETWEEN 0 AND 3" in dl
    body = up.split("CREATE FUNCTION payslip_deliveries_lock")[1].split("$$;")[0]
    assert "write-once" in body and "NEW.push_count < OLD.push_count" in body and "p.user_id" in body
    acct = up.split("CREATE TABLE company_bank_accounts (")[1].split("\n);")[0]
    for col in ("bank_name_enc bytea NOT NULL", "account_enc bytea NOT NULL", "ifsc_enc bytea NOT NULL"):
        assert col in acct, col
    assert "uq_company_bank_accounts_default ON company_bank_accounts (is_default) WHERE is_default AND active" in up
    lock = up.split("CREATE FUNCTION company_bank_accounts_lock")[1].split("$$;")[0]
    assert "NEW.account_enc IS DISTINCT FROM OLD.account_enc OR NEW.ifsc_enc IS DISTINCT FROM OLD.ifsc_enc" in lock and "app.key_rotation" in lock
    assert "ALTER TABLE employee_payroll_details ADD COLUMN bank_changed_by uuid REFERENCES users(id) ON DELETE RESTRICT" in up
    assert "ALTER TABLE employee_payroll_details ADD COLUMN bank_changed_at timestamptz" in up
    assert "CREATE INDEX ix_employee_payroll_details_bank_changed_by ON employee_payroll_details (bank_changed_by)" in up
    g = up.split("CREATE FUNCTION employee_payroll_details_bank_guard")[1].split("$$;")[0]
    assert "NEW.bank_changed_at IS NULL OR NEW.bank_changed_by IS NULL OR NEW.bank_changed_at < OLD.bank_changed_at" in g
    assert "NEW.payment_mode IS DISTINCT FROM OLD.payment_mode" in g and "app.key_rotation" in g


def test_events_and_settings():
    mod = _load()
    up = mod.UPGRADE
    assert "'increment','fnf','publish_request','payment_batch'" in up
    assert "'export','confirm','expire','stale'" in up
    from app.services import payroll_settings as pset

    expected = {
        "payroll.publish_request_ttl_hours": ("72", 72),
        "payroll.bank_file_requires_second_person": ("true", True),
        "payroll.bank_file_format": ('"generic_neft_csv"', "generic_neft_csv"),
        "payroll.bank_payment_type": ('"NEFT"', "NEFT"),
        "payroll.bank_change_alert_days": ("30", 30),
    }
    for key, (sql, py) in expected.items():
        assert f"('{key}', '{sql}'::jsonb)" in up, key
        assert key in mod.DOWNGRADE, key
        assert pset.DEFAULTS[key] == py and type(pset.DEFAULTS[key]) is type(py), key
    assert pset.validate("payroll.publish_request_ttl_hours", 1) == 1 and pset.validate("payroll.publish_request_ttl_hours", 336) == 336
    assert pset.validate("payroll.bank_change_alert_days", 0) == 0 and pset.validate("payroll.bank_change_alert_days", 90) == 90
    assert pset.validate("payroll.bank_payment_type", "RTGS") == "RTGS"
    assert pset.validate("payroll.bank_file_requires_second_person", False) is False
    from app.core.errors import AppError

    for key, bad in (
        ("payroll.publish_request_ttl_hours", 0),
        ("payroll.publish_request_ttl_hours", 337),
        ("payroll.bank_change_alert_days", -1),
        ("payroll.bank_change_alert_days", 91),
        ("payroll.bank_payment_type", "UPI"),
        ("payroll.bank_file_format", "swift"),
        ("payroll.bank_file_requires_second_person", "yes"),
        ("payroll.bank_change_alert_days", True),
    ):
        with pytest.raises(AppError):
            pset.validate(key, bad)
    # no new permission in 5a: the approve / publish / manage codes already exist
    assert "INSERT INTO permissions" not in up


def test_models_match_migration():
    from app.core.crypto import EncryptedJSON, EncryptedString
    from app.models import Base

    up = _load().UPGRADE
    for name in NEW_TABLES:
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
        assert fk_cols <= indexed, (name, fk_cols - indexed)
        # the reverse: every constraint / index the migration creates for the table is in the model
        model_names = {c.name for c in t.constraints if c.name} | {i.name for i in t.indexes}
        mig_obj = set()
        for line in up.splitlines():
            s = line.strip()
            if s.startswith("CONSTRAINT ") and s.split()[1].startswith((f"ck_{name}_", f"uq_{name}_")):
                mig_obj.add(s.split()[1])
            for pre in ("CREATE UNIQUE INDEX ", "CREATE INDEX "):
                if s.startswith(pre) and f" ON {name} " in s:
                    mig_obj.add(s[len(pre) :].split()[0])
        # the SQLite harness (conftest) drops checks / partial indexes it cannot evaluate: look in the model source as well
        missing = {n for n in mig_obj if n not in model_names and n not in MODEL_SRC}
        assert not missing, (name, missing)
    import re

    for n in re.findall(r'name="([a-z_0-9]+)"', MODEL_SRC) + re.findall(r'Index\(\s*"([a-z_0-9]+)"', MODEL_SRC):
        assert n in up, n  # includes the checks / partial indexes the SQLite harness drops
    enc = {
        "payroll_publish_requests": {"snapshot_enc": EncryptedJSON, "comment_enc": EncryptedString, "decision_comment_enc": EncryptedString},
        "company_bank_accounts": {"bank_name_enc": EncryptedString, "account_enc": EncryptedString, "ifsc_enc": EncryptedString},
        "payroll_payment_batches": {"decision_comment_enc": EncryptedString, "cancel_reason_enc": EncryptedString},
        "payroll_payment_lines": {"amount_enc": EncryptedString, "beneficiary_enc": EncryptedJSON, "reference_enc": EncryptedString},
    }
    for table, cols in enc.items():
        for c, typ in cols.items():
            assert isinstance(Base.metadata.tables[table].c[c].type, typ), (table, c)
    for table in ("payroll_runs", "payslips"):
        t = Base.metadata.tables[table]
        assert "contributors" in t.c and not t.c.contributors.nullable
    det = Base.metadata.tables["employee_payroll_details"]
    assert {"bank_changed_by", "bank_changed_at"} <= set(det.c.keys())
    assert any(i.name == "ix_employee_payroll_details_bank_changed_by" for i in det.indexes)
    assert "ck_employee_payroll_details_bank_changed" in {c.name for c in det.constraints}
    ev = {c.name: str(c.sqltext) for c in Base.metadata.tables["payroll_item_events"].constraints if c.name}
    assert "'publish_request','payment_batch'" in ev["ck_payroll_item_events_item_type"]
    assert "'export','confirm','expire','stale'" in ev["ck_payroll_item_events_action"]


def test_schema_sql_mirror():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0025 -> 0026" in sql and "version_num='0026'" in sql
    for t in NEW_TABLES:
        assert f"CREATE TABLE {t} (" in sql, t
        assert f"REVOKE DELETE, TRUNCATE ON {t} FROM app_user" in sql, t
    for fn in ("payroll_contributors_guard", "payroll_publish_requests_lock", "payroll_payment_batches_lock", "payroll_payment_lines_lock"):
        assert f"CREATE FUNCTION {fn}()" in sql, fn
    assert "uq_payroll_payment_lines_live" in sql and "ck_payroll_publish_requests_checker_history" in sql
    assert "'payroll.bank_change_alert_days'" in sql
    # the %% escapes of the DDL layer are resolved in the generated script
    assert "payment batch % is % and locked" in sql


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


@pytest.mark.asyncio
async def test_orm_defaults_and_checks(engine):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import (
        CompanyBankAccount,
        PayrollPaymentBatch,
        PayrollPaymentLine,
        PayrollPublishRequest,
        PayrollRun,
        Payslip,
        PayslipDelivery,
    )

    now = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        maker, checker, emp = await _users(s, 3)
        run = PayrollRun(period=dt.date(2026, 9, 1), attendance_cutoff=dt.date(2026, 9, 30), config={}, status="published",
                         published_by=checker.id, published_at=now)  # fmt: skip
        s.add(run)
        await s.flush()
        assert run.contributors == []
        slip = Payslip(run_id=run.id, user_id=emp.id, status="published", employee_snapshot={}, basis_days=30, employed_days=30,
                       paid_days=30, lop_breakdown={}, inputs={}, result={}, published_by=checker.id, published_at=now)  # fmt: skip
        s.add(slip)
        acct = CompanyBankAccount(label="Main", bank_name="HDFC", account="123456789012", ifsc="HDFC0001234", created_by=maker.id)
        s.add(acct)
        await s.flush()
        assert slip.contributors == [] and acct.is_default is False and acct.active is True
        assert acct.account == "123456789012"  # encrypted at rest, transparent in python
        s.add(PayslipDelivery(payslip_id=slip.id, user_id=emp.id))
        batch = PayrollPaymentBatch(run_id=run.id, reference="PAY-2026-09-01", debit_account_id=acct.id, value_date=dt.date(2026, 10, 31),
                                    created_by=maker.id, contributors=[str(maker.id)])  # fmt: skip
        s.add(batch)
        await s.flush()
        assert batch.status == "draft" and batch.last_edited_by == maker.id and batch.format == "generic_neft_csv"
        assert batch.payment_type == "NEFT" and batch.version == 1
        line = PayrollPaymentLine(batch_id=batch.id, payslip_id=slip.id, user_id=emp.id, seq=1, amount="35300.00", beneficiary={"name": "A B"})
        s.add(line)
        req = PayrollPublishRequest(run_id=run.id, scope="run", snapshot={"payslips": []}, snapshot_mac="a" * 64, requested_by=maker.id,
                                    expires_at=now + dt.timedelta(hours=72), requested_at=now)  # fmt: skip
        s.add(req)
        await s.flush()
        assert line.status == "pending" and line.account_source == "snapshot" and line.flags == []
        assert req.status == "submitted" and req.makers == [] and req.version == 1
        await s.commit()

        async def refused(obj):
            async with AsyncSession(engine) as s2:
                s2.add(obj)
                with pytest.raises(IntegrityError):
                    await s2.flush()

        def request(**kw):
            base = dict(run_id=run.id, scope="run", snapshot={}, snapshot_mac="a" * 64, requested_by=maker.id, requested_at=now,
                        expires_at=now + dt.timedelta(hours=1))  # fmt: skip
            base.update(kw)
            return PayrollPublishRequest(**base)

        await refused(request(scope="batch"))
        await refused(request(scope="payslip"))  # payslip scope needs a payslip
        await refused(request(payslip_id=slip.id))  # run scope must not carry one
        await refused(request(status="done"))
        await refused(request(stale_reason="EXPIRED"))  # reason only on stale
        await refused(request(status="stale"))  # stale needs a reason
        await refused(request(expires_at=now - dt.timedelta(hours=1)))
        await refused(request(status="approved"))  # approved needs a decider
        await refused(request(decided_by=maker.id, decided_at=now))  # decider = requester (status/decision mismatch too)
        await refused(request(status="rejected", decided_by=checker.id, decided_at=now, makers=[str(maker.id)]))  # reject needs a comment
        await refused(request(status="withdrawn"))  # withdrawn needs who
        await refused(request(version=0))
        await refused(PayrollPaymentBatch(run_id=run.id, reference="PAY-2026-09-02", debit_account_id=acct.id, value_date=dt.date(2026, 10, 31),
                                          created_by=maker.id, last_edited_by=maker.id, contributors=[str(maker.id)], status="exported"))  # fmt: skip
        await refused(PayrollPaymentBatch(run_id=run.id, reference="PAY-2026-09-02", debit_account_id=acct.id, value_date=dt.date(2026, 10, 31),
                                          created_by=maker.id, last_edited_by=maker.id, contributors=[str(maker.id)], status="submitted"))  # fmt: skip
        await refused(PayrollPaymentBatch(run_id=run.id, reference="PAY-2026-09-02", debit_account_id=acct.id, value_date=dt.date(2026, 10, 31),
                                          created_by=maker.id, last_edited_by=maker.id, contributors=[str(maker.id)], status="cancelled"))  # fmt: skip
        await refused(PayrollPaymentBatch(run_id=run.id, reference="PAY-2026-09-02", debit_account_id=acct.id, value_date=dt.date(2026, 10, 31),
                                          created_by=maker.id, last_edited_by=maker.id, contributors=[str(maker.id)], status="nope"))  # fmt: skip
        await refused(PayrollPaymentBatch(run_id=run.id, reference="PAY-2026-09-02", debit_account_id=acct.id, value_date=dt.date(2026, 10, 31),
                                          created_by=maker.id, last_edited_by=maker.id, contributors=[str(maker.id)], payment_type="UPI"))  # fmt: skip
        await refused(PayrollPaymentBatch(run_id=run.id, reference="PAY-2026-09-01", debit_account_id=acct.id, value_date=dt.date(2026, 10, 31),
                                          created_by=maker.id, contributors=[str(maker.id)]))  # fmt: skip  # duplicate reference
        await refused(PayrollPaymentBatch(run_id=run.id, reference="PAY-2026-09-03", debit_account_id=acct.id, value_date=dt.date(2026, 10, 31),
                                          created_by=maker.id, contributors=[str(maker.id)], decided_by=maker.id, decided_at=now))  # fmt: skip  # decider = creator

        def pline(**kw):
            base = dict(batch_id=batch.id, payslip_id=slip.id, user_id=emp.id, seq=2, amount="1.00", beneficiary={})
            base.update(kw)
            return PayrollPaymentLine(**base)

        await refused(pline(seq=1))  # (batch, seq) unique
        await refused(pline(seq=0))
        await refused(pline(status="settled"))
        await refused(pline(account_source="other"))
        await refused(pline(status="paid"))  # paid needs paid_on
        await refused(pline(status="pending", paid_on=dt.date(2026, 10, 31)))  # confirmation fields only on paid / failed
        await refused(pline(status="pending", reference="UTR1"))
        await refused(PayslipDelivery(payslip_id=slip.id, user_id=emp.id))  # one row per payslip (primary key)
        await refused(PayslipDelivery(payslip_id=uuid.uuid4(), user_id=emp.id, push_count=11))
