"""Payroll components (plan 15 work package 3a): one-time items + bulk import, loans, auto arrears, leave encashment, money history

Revision ID: 0023
Revises: 0022

Plan: docs/plans/15-payroll-phase3.md section 2.1; contract: docs/api-contract.md section 17.

Added
* payroll_item_events         APPEND-ONLY money history of every phase 3 source row (one-time, loan, installment, arrears,
                              encashment; reimbursement / overtime in 0024). Same protection as statutory_challan_events (0022):
                              trigger refuses UPDATE / DELETE / TRUNCATE except a key rotation (`app.key_rotation = 'on'`, only
                              `*_enc` columns changed), REVOKE UPDATE, DELETE, TRUNCATE, column-level UPDATE on `*_enc` only.
* payroll_import_batches      one row per committed bulk upload (file sha256 + period are unique: a file is imported once).
* payroll_one_time_items      bonus / incentive / other earning / other deduction for one employee-month.
* payroll_loans               loans and salary advances (interest-free, reference L-0001 from a sequence).
* payroll_loan_installments   the recovery schedule (one row per month).
* payroll_arrears             proposals and accepted arrears / recoveries after a structure change.
* leave_encashments           encashed leave days with the rate fixed at creation.
* leave_balances.encashed     days reserved by encashments; ck_leave_balances_not_overdrawn now reads used + pending + encashed <= quota.
* leave_types                 encashable / encash_max_days_per_year / encash_min_balance.
* function payroll_item_paid_lock() + trigger on every source table that carries payslip_id: a row whose OLD.status is 'paid'
  is immutable except `payslip_id` (carry-over into a payslip revision), version / updated_* and (for payroll_arrears) a
  trigger_structure_id that is set to NULL by the FK; a key rotation (only `*_enc` changed) always passes. DELETE of a paid row is refused.
* REVOKE DELETE, TRUNCATE FROM app_user on every new table (cancelling is a status, never a delete).
* settings payroll.loan_max_tenure_months / loan_max_active / encashment_basis / encashment_divisor (also defaults in code).

Money is encrypted (`*_enc`, model EncryptedString / EncryptedJSON); no SQL aggregates on money.

Downgrade drops the tables (ALL phase 3 data is lost) and restores the old balance constraint; it FAILS when a leave balance
holds encashed days (cancel the encashments first).
"""

from migration_sql import run

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE payroll_item_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  item_type text NOT NULL,
  item_id uuid NOT NULL,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  action text NOT NULL,
  status text NOT NULL,
  period date,
  prev_period date,
  amount_enc bytea,
  prev_amount_enc bytea,
  detail_enc bytea,
  payslip_id uuid,
  actor_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_payroll_item_events_item_type CHECK (item_type IN ('one_time','loan','loan_installment','arrears','encashment','reimbursement','overtime')),
  CONSTRAINT ck_payroll_item_events_action CHECK (action IN ('create','update','cancel','skip','reschedule','foreclose','close','propose','accept','dismiss','verify','approve','reject','process','reopen','pay','carry')),
  CONSTRAINT ck_payroll_item_events_period_first CHECK (period IS NULL OR extract(day from period) = 1),
  CONSTRAINT ck_payroll_item_events_prev_period_first CHECK (prev_period IS NULL OR extract(day from prev_period) = 1)
);
CREATE INDEX ix_payroll_item_events_item ON payroll_item_events (item_type, item_id, created_at);
CREATE INDEX ix_payroll_item_events_user ON payroll_item_events (user_id, created_at);
CREATE INDEX ix_payroll_item_events_actor_id ON payroll_item_events (actor_id);

CREATE FUNCTION payroll_item_events_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  RAISE EXCEPTION 'payroll_item_events is append-only' USING ERRCODE = 'insufficient_privilege', DETAIL = TG_OP;
END
$$;
CREATE TRIGGER trg_payroll_item_events_no_update_delete BEFORE UPDATE OR DELETE ON payroll_item_events
  FOR EACH ROW EXECUTE FUNCTION payroll_item_events_lock();
CREATE TRIGGER trg_payroll_item_events_no_truncate BEFORE TRUNCATE ON payroll_item_events
  FOR EACH STATEMENT EXECUTE FUNCTION payroll_item_events_lock();

CREATE TABLE payroll_import_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  period date NOT NULL,
  file_sha256 text NOT NULL,
  rows integer NOT NULL,
  created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  cancelled_at timestamptz,
  cancelled_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT uq_payroll_import_batches_file_period UNIQUE (file_sha256, period),
  CONSTRAINT ck_payroll_import_batches_period_first CHECK (extract(day from period) = 1),
  CONSTRAINT ck_payroll_import_batches_period_year CHECK (period BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_payroll_import_batches_sha256 CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_payroll_import_batches_rows CHECK (rows BETWEEN 1 AND 500),
  CONSTRAINT ck_payroll_import_batches_cancelled CHECK ((cancelled_at IS NULL) = (cancelled_by IS NULL))
);
CREATE INDEX ix_payroll_import_batches_period ON payroll_import_batches (period);
CREATE INDEX ix_payroll_import_batches_created_by ON payroll_import_batches (created_by);
CREATE INDEX ix_payroll_import_batches_cancelled_by ON payroll_import_batches (cancelled_by);

CREATE TABLE payroll_one_time_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  period date NOT NULL,
  code text NOT NULL,
  label_enc bytea NOT NULL,
  amount_enc bytea NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  batch_id uuid REFERENCES payroll_import_batches(id) ON DELETE RESTRICT,
  payslip_id uuid REFERENCES payslips(id) ON DELETE RESTRICT,
  cancel_reason_enc bytea,
  version integer NOT NULL DEFAULT 1,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_payroll_one_time_items_period_first CHECK (extract(day from period) = 1),
  CONSTRAINT ck_payroll_one_time_items_period_year CHECK (period BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_payroll_one_time_items_code CHECK (code IN ('BONUS','INCENTIVE','OTHER_EARNING','OTHER_DEDUCTION')),
  CONSTRAINT ck_payroll_one_time_items_status CHECK (status IN ('pending','processing','paid','cancelled')),
  CONSTRAINT ck_payroll_one_time_items_payslip CHECK ((status IN ('processing','paid')) = (payslip_id IS NOT NULL)),
  CONSTRAINT ck_payroll_one_time_items_cancelled CHECK (status <> 'cancelled' OR cancel_reason_enc IS NOT NULL),
  CONSTRAINT ck_payroll_one_time_items_version CHECK (version >= 1)
);
CREATE INDEX ix_payroll_one_time_items_user_period ON payroll_one_time_items (user_id, period, status);
CREATE INDEX ix_payroll_one_time_items_period ON payroll_one_time_items (period, status);
CREATE INDEX ix_payroll_one_time_items_batch_id ON payroll_one_time_items (batch_id);
CREATE INDEX ix_payroll_one_time_items_payslip_id ON payroll_one_time_items (payslip_id);
CREATE INDEX ix_payroll_one_time_items_created_by ON payroll_one_time_items (created_by);
CREATE INDEX ix_payroll_one_time_items_updated_by ON payroll_one_time_items (updated_by);
CREATE TRIGGER trg_payroll_one_time_items_updated_at BEFORE UPDATE ON payroll_one_time_items FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE SEQUENCE payroll_loan_ref_seq;
CREATE TABLE payroll_loans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  kind text NOT NULL,
  reference text NOT NULL DEFAULT ('L-' || lpad(nextval('payroll_loan_ref_seq')::text, 4, '0')),
  principal_enc bytea NOT NULL,
  emi_enc bytea NOT NULL,
  start_period date NOT NULL,
  reason_enc bytea NOT NULL,
  status text NOT NULL DEFAULT 'active',
  closed_at timestamptz,
  close_mode text,
  version integer NOT NULL DEFAULT 1,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_payroll_loans_reference UNIQUE (reference),
  CONSTRAINT ck_payroll_loans_reference CHECK (reference ~ '^L-[0-9]{4,}$'),
  CONSTRAINT ck_payroll_loans_kind CHECK (kind IN ('loan','advance')),
  CONSTRAINT ck_payroll_loans_start_first CHECK (extract(day from start_period) = 1),
  CONSTRAINT ck_payroll_loans_start_year CHECK (start_period BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_payroll_loans_status CHECK (status IN ('active','closed','cancelled')),
  CONSTRAINT ck_payroll_loans_close_mode CHECK (close_mode IS NULL OR close_mode IN ('recovered','foreclosed_payroll','foreclosed_outside','cancelled')),
  CONSTRAINT ck_payroll_loans_closed CHECK (
    (status = 'active' AND closed_at IS NULL AND close_mode IS NULL)
    OR (status = 'closed' AND closed_at IS NOT NULL AND close_mode IN ('recovered','foreclosed_payroll','foreclosed_outside'))
    OR (status = 'cancelled' AND closed_at IS NOT NULL AND close_mode = 'cancelled')
  ),
  CONSTRAINT ck_payroll_loans_version CHECK (version >= 1)
);
CREATE INDEX ix_payroll_loans_user_status ON payroll_loans (user_id, status);
CREATE INDEX ix_payroll_loans_created_by ON payroll_loans (created_by);
CREATE INDEX ix_payroll_loans_updated_by ON payroll_loans (updated_by);
CREATE TRIGGER trg_payroll_loans_updated_at BEFORE UPDATE ON payroll_loans FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE payroll_loan_installments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  loan_id uuid NOT NULL REFERENCES payroll_loans(id) ON DELETE RESTRICT,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  seq smallint NOT NULL,
  period date NOT NULL,
  amount_enc bytea NOT NULL,
  kind text NOT NULL DEFAULT 'emi',
  status text NOT NULL DEFAULT 'pending',
  payslip_id uuid REFERENCES payslips(id) ON DELETE RESTRICT,
  version integer NOT NULL DEFAULT 1,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_payroll_loan_installments_loan_seq UNIQUE (loan_id, seq),
  CONSTRAINT ck_payroll_loan_installments_seq CHECK (seq BETWEEN 1 AND 120),
  CONSTRAINT ck_payroll_loan_installments_period_first CHECK (extract(day from period) = 1),
  CONSTRAINT ck_payroll_loan_installments_period_year CHECK (period BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_payroll_loan_installments_kind CHECK (kind IN ('emi','foreclosure')),
  CONSTRAINT ck_payroll_loan_installments_status CHECK (status IN ('pending','processing','paid','skipped','cancelled')),
  CONSTRAINT ck_payroll_loan_installments_payslip CHECK ((status IN ('processing','paid')) = (payslip_id IS NOT NULL)),
  CONSTRAINT ck_payroll_loan_installments_version CHECK (version >= 1)
);
CREATE UNIQUE INDEX uq_payroll_loan_installments_live ON payroll_loan_installments (loan_id, period) WHERE status IN ('pending','processing','paid');
CREATE INDEX ix_payroll_loan_installments_user_period ON payroll_loan_installments (user_id, period, status);
CREATE INDEX ix_payroll_loan_installments_payslip_id ON payroll_loan_installments (payslip_id);
CREATE INDEX ix_payroll_loan_installments_created_by ON payroll_loan_installments (created_by);
CREATE INDEX ix_payroll_loan_installments_updated_by ON payroll_loan_installments (updated_by);
CREATE TRIGGER trg_payroll_loan_installments_updated_at BEFORE UPDATE ON payroll_loan_installments FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE payroll_arrears (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  period date,
  from_period date NOT NULL,
  to_period date NOT NULL,
  trigger_structure_id uuid REFERENCES salary_structures(id) ON DELETE SET NULL,
  code text NOT NULL,
  amount_enc bytea NOT NULL,
  breakdown_enc bytea NOT NULL,
  status text NOT NULL DEFAULT 'proposed',
  dismiss_reason_enc bytea,
  payslip_id uuid REFERENCES payslips(id) ON DELETE RESTRICT,
  version integer NOT NULL DEFAULT 1,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_payroll_arrears_period_first CHECK (period IS NULL OR extract(day from period) = 1),
  CONSTRAINT ck_payroll_arrears_period_year CHECK (period IS NULL OR period BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_payroll_arrears_from_first CHECK (extract(day from from_period) = 1),
  CONSTRAINT ck_payroll_arrears_to_first CHECK (extract(day from to_period) = 1),
  CONSTRAINT ck_payroll_arrears_range CHECK (from_period <= to_period),
  CONSTRAINT ck_payroll_arrears_code CHECK (code IN ('ARREARS','OTHER_DEDUCTION')),
  CONSTRAINT ck_payroll_arrears_status CHECK (status IN ('proposed','pending','processing','paid','dismissed')),
  CONSTRAINT ck_payroll_arrears_period_set CHECK (status IN ('proposed','dismissed') OR period IS NOT NULL),
  CONSTRAINT ck_payroll_arrears_payslip CHECK ((status IN ('processing','paid')) = (payslip_id IS NOT NULL)),
  CONSTRAINT ck_payroll_arrears_version CHECK (version >= 1)
);
CREATE UNIQUE INDEX uq_payroll_arrears_open_proposal ON payroll_arrears (user_id) WHERE status = 'proposed';
CREATE INDEX ix_payroll_arrears_user_period ON payroll_arrears (user_id, period, status);
CREATE INDEX ix_payroll_arrears_status ON payroll_arrears (status);
CREATE INDEX ix_payroll_arrears_trigger_structure_id ON payroll_arrears (trigger_structure_id);
CREATE INDEX ix_payroll_arrears_payslip_id ON payroll_arrears (payslip_id);
CREATE INDEX ix_payroll_arrears_created_by ON payroll_arrears (created_by);
CREATE INDEX ix_payroll_arrears_updated_by ON payroll_arrears (updated_by);
CREATE TRIGGER trg_payroll_arrears_updated_at BEFORE UPDATE ON payroll_arrears FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE leave_encashments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  leave_type_id uuid NOT NULL REFERENCES leave_types(id) ON DELETE RESTRICT,
  year integer NOT NULL,
  days numeric(4,1) NOT NULL,
  daily_rate_enc bytea NOT NULL,
  amount_enc bytea NOT NULL,
  period date NOT NULL,
  source text NOT NULL DEFAULT 'request',
  reason_enc bytea,
  status text NOT NULL DEFAULT 'pending',
  payslip_id uuid REFERENCES payslips(id) ON DELETE RESTRICT,
  version integer NOT NULL DEFAULT 1,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_leave_encashments_year CHECK (year BETWEEN 2000 AND 2200),
  CONSTRAINT ck_leave_encashments_days CHECK (days > 0 AND days * 2 = trunc(days * 2)),
  CONSTRAINT ck_leave_encashments_period_first CHECK (extract(day from period) = 1),
  CONSTRAINT ck_leave_encashments_period_year CHECK (period BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_leave_encashments_source CHECK (source IN ('request','fnf')),
  CONSTRAINT ck_leave_encashments_status CHECK (status IN ('pending','processing','paid','cancelled')),
  CONSTRAINT ck_leave_encashments_payslip CHECK ((status IN ('processing','paid')) = (payslip_id IS NOT NULL)),
  CONSTRAINT ck_leave_encashments_version CHECK (version >= 1)
);
CREATE INDEX ix_leave_encashments_user_year ON leave_encashments (user_id, year);
CREATE INDEX ix_leave_encashments_user_period ON leave_encashments (user_id, period, status);
CREATE INDEX ix_leave_encashments_leave_type_id ON leave_encashments (leave_type_id);
CREATE INDEX ix_leave_encashments_payslip_id ON leave_encashments (payslip_id);
CREATE INDEX ix_leave_encashments_created_by ON leave_encashments (created_by);
CREATE INDEX ix_leave_encashments_updated_by ON leave_encashments (updated_by);
CREATE TRIGGER trg_leave_encashments_updated_at BEFORE UPDATE ON leave_encashments FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE leave_balances ADD COLUMN encashed numeric(4,1) NOT NULL DEFAULT 0;
ALTER TABLE leave_balances ADD CONSTRAINT ck_leave_balances_encashed CHECK (encashed >= 0);
ALTER TABLE leave_balances DROP CONSTRAINT ck_leave_balances_not_overdrawn;
ALTER TABLE leave_balances ADD CONSTRAINT ck_leave_balances_not_overdrawn CHECK (used + pending + encashed <= quota);

ALTER TABLE leave_types ADD COLUMN encashable boolean NOT NULL DEFAULT false;
ALTER TABLE leave_types ADD COLUMN encash_max_days_per_year numeric(4,1);
ALTER TABLE leave_types ADD COLUMN encash_min_balance numeric(4,1) NOT NULL DEFAULT 0;
ALTER TABLE leave_types ADD CONSTRAINT ck_leave_types_encash_max CHECK (encash_max_days_per_year IS NULL OR (encash_max_days_per_year > 0 AND encash_max_days_per_year * 2 = trunc(encash_max_days_per_year * 2)));
ALTER TABLE leave_types ADD CONSTRAINT ck_leave_types_encash_min CHECK (encash_min_balance >= 0 AND encash_min_balance * 2 = trunc(encash_min_balance * 2));

CREATE FUNCTION payroll_item_paid_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  j_new jsonb;
  j_old jsonb;
BEGIN
  IF OLD.status <> 'paid' THEN
    IF TG_OP = 'DELETE' THEN
      RETURN OLD;
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'payroll item %% is paid and cannot be deleted', OLD.id USING ERRCODE = '23000';
  END IF;
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  j_new := to_jsonb(NEW) - 'payslip_id' - 'version' - 'updated_by' - 'updated_at';
  j_old := to_jsonb(OLD) - 'payslip_id' - 'version' - 'updated_by' - 'updated_at';
  IF (to_jsonb(NEW) -> 'trigger_structure_id') = 'null'::jsonb THEN
    j_new := j_new - 'trigger_structure_id';  -- ON DELETE SET NULL of the structure that triggered an arrears row
    j_old := j_old - 'trigger_structure_id';
  END IF;
  IF j_new IS DISTINCT FROM j_old OR NEW.payslip_id IS NULL THEN
    RAISE EXCEPTION 'payroll item %% is paid and locked', OLD.id USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payroll_one_time_items_paid_lock BEFORE UPDATE OR DELETE ON payroll_one_time_items FOR EACH ROW EXECUTE FUNCTION payroll_item_paid_lock();
CREATE TRIGGER trg_payroll_loan_installments_paid_lock BEFORE UPDATE OR DELETE ON payroll_loan_installments FOR EACH ROW EXECUTE FUNCTION payroll_item_paid_lock();
CREATE TRIGGER trg_payroll_arrears_paid_lock BEFORE UPDATE OR DELETE ON payroll_arrears FOR EACH ROW EXECUTE FUNCTION payroll_item_paid_lock();
CREATE TRIGGER trg_leave_encashments_paid_lock BEFORE UPDATE OR DELETE ON leave_encashments FOR EACH ROW EXECUTE FUNCTION payroll_item_paid_lock();

INSERT INTO settings (key, value) VALUES ('payroll.loan_max_tenure_months', '60'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.loan_max_active', '3'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.encashment_basis', '"basic"'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.encashment_divisor', '30'::jsonb) ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  REVOKE UPDATE, DELETE, TRUNCATE ON payroll_item_events FROM app_user;
  GRANT UPDATE (amount_enc, prev_amount_enc, detail_enc) ON payroll_item_events TO app_user;
  REVOKE DELETE, TRUNCATE ON payroll_import_batches FROM app_user;
  REVOKE DELETE, TRUNCATE ON payroll_one_time_items FROM app_user;
  REVOKE DELETE, TRUNCATE ON payroll_loans FROM app_user;
  REVOKE DELETE, TRUNCATE ON payroll_loan_installments FROM app_user;
  REVOKE DELETE, TRUNCATE ON payroll_arrears FROM app_user;
  REVOKE DELETE, TRUNCATE ON leave_encashments FROM app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'revoke on phase 3 payroll tables skipped';
END
$$;
"""

DOWNGRADE = r"""
DELETE FROM settings WHERE key IN ('payroll.loan_max_tenure_months', 'payroll.loan_max_active', 'payroll.encashment_basis', 'payroll.encashment_divisor');

ALTER TABLE leave_types DROP CONSTRAINT IF EXISTS ck_leave_types_encash_min;
ALTER TABLE leave_types DROP CONSTRAINT IF EXISTS ck_leave_types_encash_max;
ALTER TABLE leave_types DROP COLUMN IF EXISTS encash_min_balance;
ALTER TABLE leave_types DROP COLUMN IF EXISTS encash_max_days_per_year;
ALTER TABLE leave_types DROP COLUMN IF EXISTS encashable;

ALTER TABLE leave_balances DROP CONSTRAINT IF EXISTS ck_leave_balances_not_overdrawn;
ALTER TABLE leave_balances ADD CONSTRAINT ck_leave_balances_not_overdrawn CHECK (used + pending <= quota);
ALTER TABLE leave_balances DROP CONSTRAINT IF EXISTS ck_leave_balances_encashed;
ALTER TABLE leave_balances DROP COLUMN IF EXISTS encashed;

DROP TABLE IF EXISTS leave_encashments;
DROP TABLE IF EXISTS payroll_arrears;
DROP TABLE IF EXISTS payroll_loan_installments;
DROP TABLE IF EXISTS payroll_loans;
DROP SEQUENCE IF EXISTS payroll_loan_ref_seq;
DROP TABLE IF EXISTS payroll_one_time_items;
DROP TABLE IF EXISTS payroll_import_batches;
DROP TABLE IF EXISTS payroll_item_events;
DROP FUNCTION IF EXISTS payroll_item_paid_lock();
DROP FUNCTION IF EXISTS payroll_item_events_lock();
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
