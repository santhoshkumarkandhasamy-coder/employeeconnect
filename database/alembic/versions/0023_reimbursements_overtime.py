"""Reimbursement claims + overtime (plan 15 work package 3b) and the loan installment numbering fix

Revision ID: 0023
Revises: 0022

Plan: docs/plans/15-payroll-phase3.md section 2.2; contract: docs/api-contract.md sections 17.6 to 17.9.

Added
* reimbursement_claims     expense claims (two-step approval: reporting manager verifies, HR approves the amount and the payout
                           month). Amount, approved amount, description and decision comment are application-encrypted.
                           A bill (files row, purpose `reimbursement_bill`) backs at most one LIVE claim (partial unique index).
* overtime_days            one row per employee and work date with the overtime minutes derived from attendance; HR reviews
                           every row before it counts. Minutes are not money: plaintext.
* files.ck_files_purpose   accepts 'reimbursement_bill'.
* employee_payroll_details.overtime_eligible (default false).
* permission `reimbursement.submit` granted to EVERY existing role (own claims and bill upload).
* settings payroll.reimbursement_* and payroll.overtime_* (also defaults in code; overtime is OFF by default).
* payroll_item_paid_lock() is replaced: as before, plus a paid overtime row survives the ON DELETE SET NULL of its attendance_id.
  Both new tables get the trigger (a `paid` row is immutable except payslip_id / version / updated_*; key rotation passes).
* REVOKE DELETE, TRUNCATE FROM app_user on both tables (cancelling is a status, never a delete).

Changed (loan installment numbering, review 3a L7)
* payroll_loan_installments: UNIQUE (loan_id, seq) over ALL rows and CHECK seq BETWEEN 1 AND 120 are dropped. Numbers must now be
  unique only among LIVE installments (partial unique index `uq_payroll_loan_installments_live_seq`, status not cancelled /
  skipped) and the check is `seq >= 1`. The service numbers a new installment max(live seq) + 1, so skipping, rescheduling and
  foreclosing never exhaust the numbers.

Downgrade drops both tables (ALL claims and overtime data is lost), removes the permission, the settings and the column and
restores the old function and the loan constraints. It FAILS (and rolls back as a whole) while `files` still holds
reimbursement bills (delete them first) and when cancelled or skipped installments share a number with a live one or a number
is above 120 (renumbering is impossible because `paid` rows are immutable).
"""

from migration_sql import run

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_PAID_LOCK_HEAD = r"""
CREATE OR REPLACE FUNCTION payroll_item_paid_lock() RETURNS trigger LANGUAGE plpgsql AS $$
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
"""
_PAID_LOCK_ATT = r"""  IF (to_jsonb(NEW) -> 'attendance_id') = 'null'::jsonb THEN
    j_new := j_new - 'attendance_id';  -- ON DELETE SET NULL of the attendance row behind an overtime day
    j_old := j_old - 'attendance_id';
  END IF;
"""
_PAID_LOCK_TAIL = r"""  IF j_new IS DISTINCT FROM j_old OR NEW.payslip_id IS NULL THEN
    RAISE EXCEPTION 'payroll item %% is paid and locked', OLD.id USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
"""

UPGRADE = (
    r"""
ALTER TABLE files DROP CONSTRAINT ck_files_purpose;
ALTER TABLE files ADD CONSTRAINT ck_files_purpose CHECK (purpose IN ('selfie','leave_attachment','announcement_image','announcement_pdf','profile_photo','company_logo','reimbursement_bill'));

ALTER TABLE employee_payroll_details ADD COLUMN overtime_eligible boolean NOT NULL DEFAULT false;

CREATE TABLE reimbursement_claims (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  category text NOT NULL,
  expense_date date NOT NULL,
  amount_enc bytea NOT NULL,
  approved_amount_enc bytea,
  description_enc bytea NOT NULL,
  bill_file_id uuid REFERENCES files(id) ON DELETE RESTRICT,
  status text NOT NULL DEFAULT 'submitted',
  manager_step text NOT NULL DEFAULT 'pending',
  verifier_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  verified_at timestamptz,
  approver_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  decided_at timestamptz,
  decision_comment_enc bytea,
  period date,
  payslip_id uuid REFERENCES payslips(id) ON DELETE RESTRICT,
  version integer NOT NULL DEFAULT 1,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_reimbursement_claims_category CHECK (category ~ '^[a-z][a-z0-9_]{1,19}$'),
  CONSTRAINT ck_reimbursement_claims_status CHECK (status IN ('submitted','verified','approved','processing','paid','rejected','cancelled')),
  CONSTRAINT ck_reimbursement_claims_manager_step CHECK (manager_step IN ('pending','done','skipped')),
  CONSTRAINT ck_reimbursement_claims_period_first CHECK (period IS NULL OR extract(day from period) = 1),
  CONSTRAINT ck_reimbursement_claims_period_year CHECK (period IS NULL OR period BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_reimbursement_claims_payable CHECK (status NOT IN ('approved','processing','paid') OR (period IS NOT NULL AND approved_amount_enc IS NOT NULL)),
  CONSTRAINT ck_reimbursement_claims_payslip CHECK ((status IN ('processing','paid')) = (payslip_id IS NOT NULL)),
  CONSTRAINT ck_reimbursement_claims_verified CHECK ((verifier_id IS NULL) = (verified_at IS NULL)),
  CONSTRAINT ck_reimbursement_claims_decided CHECK ((approver_id IS NULL) = (decided_at IS NULL)),
  CONSTRAINT ck_reimbursement_claims_rejected CHECK (status <> 'rejected' OR decision_comment_enc IS NOT NULL),
  CONSTRAINT ck_reimbursement_claims_version CHECK (version >= 1)
);
CREATE UNIQUE INDEX uq_reimbursement_claims_live_bill ON reimbursement_claims (bill_file_id) WHERE bill_file_id IS NOT NULL AND status NOT IN ('rejected','cancelled');
CREATE INDEX ix_reimbursement_claims_user_status ON reimbursement_claims (user_id, status);
CREATE INDEX ix_reimbursement_claims_status_created ON reimbursement_claims (status, created_at);
CREATE INDEX ix_reimbursement_claims_user_period ON reimbursement_claims (user_id, period, status);
CREATE INDEX ix_reimbursement_claims_verifier_id ON reimbursement_claims (verifier_id);
CREATE INDEX ix_reimbursement_claims_approver_id ON reimbursement_claims (approver_id);
CREATE INDEX ix_reimbursement_claims_payslip_id ON reimbursement_claims (payslip_id);
CREATE INDEX ix_reimbursement_claims_created_by ON reimbursement_claims (created_by);
CREATE INDEX ix_reimbursement_claims_updated_by ON reimbursement_claims (updated_by);
CREATE TRIGGER trg_reimbursement_claims_updated_at BEFORE UPDATE ON reimbursement_claims FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE overtime_days (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  work_date date NOT NULL,
  period date NOT NULL,
  attendance_id uuid REFERENCES attendance(id) ON DELETE SET NULL,
  computed_minutes integer NOT NULL,
  approved_minutes integer,
  flags text[] NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'computed',
  reviewer_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  reviewed_at timestamptz,
  comment_enc bytea,
  payslip_id uuid REFERENCES payslips(id) ON DELETE RESTRICT,
  version integer NOT NULL DEFAULT 1,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_overtime_days_user_date UNIQUE (user_id, work_date),
  CONSTRAINT ck_overtime_days_period CHECK (period = work_date - (extract(day from work_date)::integer - 1)),
  CONSTRAINT ck_overtime_days_period_year CHECK (period BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_overtime_days_computed CHECK (computed_minutes BETWEEN 0 AND 1440),
  CONSTRAINT ck_overtime_days_approved CHECK (approved_minutes IS NULL OR (approved_minutes BETWEEN 0 AND 1440 AND approved_minutes <= computed_minutes)),
  CONSTRAINT ck_overtime_days_flags CHECK (flags <@ ARRAY['AUTO_CLOSED','SUSPICIOUS','OFF_DAY','ATTENDANCE_CHANGED']::text[]),
  CONSTRAINT ck_overtime_days_status CHECK (status IN ('computed','approved','rejected','processing','paid')),
  CONSTRAINT ck_overtime_days_minutes_set CHECK (status IN ('computed','rejected') OR approved_minutes IS NOT NULL),
  CONSTRAINT ck_overtime_days_payslip CHECK ((status IN ('processing','paid')) = (payslip_id IS NOT NULL)),
  CONSTRAINT ck_overtime_days_reviewed CHECK ((reviewer_id IS NULL) OR (reviewed_at IS NOT NULL)),
  CONSTRAINT ck_overtime_days_version CHECK (version >= 1)
);
CREATE INDEX ix_overtime_days_period_status ON overtime_days (period, status);
CREATE INDEX ix_overtime_days_user_period ON overtime_days (user_id, period, status);
CREATE INDEX ix_overtime_days_attendance_id ON overtime_days (attendance_id);
CREATE INDEX ix_overtime_days_reviewer_id ON overtime_days (reviewer_id);
CREATE INDEX ix_overtime_days_payslip_id ON overtime_days (payslip_id);
CREATE INDEX ix_overtime_days_created_by ON overtime_days (created_by);
CREATE INDEX ix_overtime_days_updated_by ON overtime_days (updated_by);
CREATE TRIGGER trg_overtime_days_updated_at BEFORE UPDATE ON overtime_days FOR EACH ROW EXECUTE FUNCTION set_updated_at();

"""
    + _PAID_LOCK_HEAD
    + _PAID_LOCK_ATT
    + _PAID_LOCK_TAIL
    + r"""
CREATE TRIGGER trg_reimbursement_claims_paid_lock BEFORE UPDATE OR DELETE ON reimbursement_claims FOR EACH ROW EXECUTE FUNCTION payroll_item_paid_lock();
CREATE TRIGGER trg_overtime_days_paid_lock BEFORE UPDATE OR DELETE ON overtime_days FOR EACH ROW EXECUTE FUNCTION payroll_item_paid_lock();

ALTER TABLE payroll_loan_installments DROP CONSTRAINT uq_payroll_loan_installments_loan_seq;
ALTER TABLE payroll_loan_installments DROP CONSTRAINT ck_payroll_loan_installments_seq;
ALTER TABLE payroll_loan_installments ADD CONSTRAINT ck_payroll_loan_installments_seq CHECK (seq >= 1);
CREATE UNIQUE INDEX uq_payroll_loan_installments_live_seq ON payroll_loan_installments (loan_id, seq) WHERE status NOT IN ('cancelled','skipped');

INSERT INTO permissions (code, module, description) VALUES ('reimbursement.submit', 'reimbursement', 'Submit own reimbursement claims and upload bills') ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE p.code = 'reimbursement.submit' ON CONFLICT DO NOTHING;

INSERT INTO settings (key, value) VALUES ('payroll.reimbursement_categories', '[{"code":"travel","name":"Travel","max_amount":null,"bill_required":true},{"code":"food","name":"Food","max_amount":null,"bill_required":true},{"code":"medical","name":"Medical","max_amount":null,"bill_required":true},{"code":"phone","name":"Phone","max_amount":null,"bill_required":true},{"code":"internet","name":"Internet","max_amount":null,"bill_required":true},{"code":"other","name":"Other","max_amount":null,"bill_required":true}]'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.reimbursement_max_age_days', '60'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.reimbursement_max_claims_per_month', '30'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.overtime_enabled', 'false'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.overtime_multiplier', '2'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.overtime_month_days', '26'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.overtime_min_minutes', '30'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.overtime_rounding_minutes', '30'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.overtime_max_hours_month', '50'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.overtime_offday_full', 'true'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.overtime_include_auto_closed', 'false'::jsonb) ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  REVOKE DELETE, TRUNCATE ON reimbursement_claims FROM app_user;
  REVOKE DELETE, TRUNCATE ON overtime_days FROM app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'revoke on reimbursement_claims / overtime_days skipped';
END
$$;
"""
)

DOWNGRADE = (
    r"""
DELETE FROM settings WHERE key IN ('payroll.reimbursement_categories', 'payroll.reimbursement_max_age_days', 'payroll.reimbursement_max_claims_per_month', 'payroll.overtime_enabled', 'payroll.overtime_multiplier', 'payroll.overtime_month_days', 'payroll.overtime_min_minutes', 'payroll.overtime_rounding_minutes', 'payroll.overtime_max_hours_month', 'payroll.overtime_offday_full', 'payroll.overtime_include_auto_closed');
DELETE FROM permissions WHERE code = 'reimbursement.submit';

DROP INDEX IF EXISTS uq_payroll_loan_installments_live_seq;
ALTER TABLE payroll_loan_installments DROP CONSTRAINT IF EXISTS ck_payroll_loan_installments_seq;
ALTER TABLE payroll_loan_installments ADD CONSTRAINT ck_payroll_loan_installments_seq CHECK (seq BETWEEN 1 AND 120);
ALTER TABLE payroll_loan_installments ADD CONSTRAINT uq_payroll_loan_installments_loan_seq UNIQUE (loan_id, seq);

DROP TABLE IF EXISTS overtime_days;
DROP TABLE IF EXISTS reimbursement_claims;

"""
    + _PAID_LOCK_HEAD
    + _PAID_LOCK_TAIL
    + r"""
ALTER TABLE employee_payroll_details DROP COLUMN IF EXISTS overtime_eligible;

ALTER TABLE files DROP CONSTRAINT ck_files_purpose;
ALTER TABLE files ADD CONSTRAINT ck_files_purpose CHECK (purpose IN ('selfie','leave_attachment','announcement_image','announcement_pdf','profile_photo','company_logo'));
"""
)


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
