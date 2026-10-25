"""Payroll: salary structures, employee payroll details, payroll runs, payslips (+ revisions), lock triggers, permissions, settings

Revision ID: 0018
Revises: 0017

Plan: docs/plans/12-payroll.md section 2; contract: docs/api-contract.md section 13.

Added
* salary_structures        effective-dated (always the 1st of a month), components encrypted (components_enc)
* employee_payroll_details 1:1 with users, bank/PAN/UAN/ESI numbers encrypted
* payroll_runs             one per month (UNIQUE period); status draft -> processed -> published
* payslips                 one row per (run, employee, revision); money in inputs_enc / result_enc / identity_enc (encrypted)
* functions payroll_runs_lock() / payslips_lock() / salary_structures_lock() + triggers: PUBLISHED DATA IS IMMUTABLE even against a buggy service
    - payroll_runs: a published run refuses any UPDATE except updated_at and any DELETE; DELETE only for a draft run
      that has no published/superseded payslip.
    - payslips: published -> superseded is the ONLY change allowed on a published row (superseded_at is stamped; status,
      superseded_at, version, updated_by, updated_at are the only columns that may differ); superseded rows are frozen;
      DELETE only for draft rows. A deferred constraint trigger refuses to commit a published -> superseded change that has no
      published successor (supersedes_id) in the same transaction.
    - salary_structures: a structure used by a processed / published / superseded payslip cannot be updated or deleted.
    - KEY ROTATION BYPASS: when the transaction has `SET LOCAL app.key_rotation = 'on'` (only services/key_rotation.py sets it),
      an UPDATE that changes nothing but `*_enc` columns passes the payslips / salary_structures locks, so an old encryption
      key can always be retired. Any other column change is still refused. payroll_strip_enc() removes the `*_enc` keys.
* permissions payroll.view_own (granted to EVERY existing role), payroll.view_all / payroll.manage / payroll.publish (admin, hr)
* settings payroll.* defaults (Tamil Nadu: half-yearly Professional Tax, see below)

Professional Tax defaults (decision of the product owner: office in Tamil Nadu). payroll.pt_frequency = 'half_yearly': PT is
collected only in the September and March payroll months (slab `months` = [3, 9]) and the slab is looked up on the HALF-YEARLY
gross. THESE SLABS MUST BE VERIFIED AGAINST CURRENT TAMIL NADU RULES before the first real run; they are editable settings.
LOP default: payroll.lop_absent_days = true (working day with no attendance and no approved leave is automatic LOP).

Money is never stored in plaintext columns and there are no SQL aggregates on money. Format rules for PAN/IFSC/etc. are
validated in the application (the columns are encrypted).

Downgrade drops the four tables (ALL payroll data is lost), the functions, permissions and settings.
"""

from migration_sql import run

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE salary_structures (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  effective_from date NOT NULL,
  components_enc bytea NOT NULL,
  pf_applicable boolean NOT NULL DEFAULT true,
  pf_on_full_basic boolean NOT NULL DEFAULT false,
  esi_applicable boolean NOT NULL DEFAULT true,
  pt_applicable boolean NOT NULL DEFAULT true,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_salary_structures_user_effective UNIQUE (user_id, effective_from),
  CONSTRAINT ck_salary_structures_first_of_month CHECK (extract(day from effective_from) = 1),
  CONSTRAINT ck_salary_structures_year CHECK (effective_from BETWEEN DATE '2000-01-01' AND DATE '2200-12-31')
);
CREATE INDEX ix_salary_structures_user_effective ON salary_structures (user_id, effective_from DESC);
CREATE INDEX ix_salary_structures_created_by ON salary_structures (created_by);
CREATE INDEX ix_salary_structures_updated_by ON salary_structures (updated_by);
CREATE TRIGGER trg_salary_structures_updated_at BEFORE UPDATE ON salary_structures FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE employee_payroll_details (
  user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
  designation text,
  payment_mode text NOT NULL DEFAULT 'bank_transfer',
  include_in_payroll boolean NOT NULL DEFAULT true,
  bank_name_enc bytea,
  bank_account_enc bytea,
  bank_ifsc_enc bytea,
  pan_enc bytea,
  uan_enc bytea,
  esi_ip_number_enc bytea,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_employee_payroll_details_designation CHECK (designation IS NULL OR char_length(designation) <= 60),
  CONSTRAINT ck_employee_payroll_details_payment_mode CHECK (payment_mode IN ('bank_transfer','cheque','cash'))
);
CREATE INDEX ix_employee_payroll_details_updated_by ON employee_payroll_details (updated_by);
CREATE TRIGGER trg_employee_payroll_details_updated_at BEFORE UPDATE ON employee_payroll_details FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE payroll_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  period date NOT NULL,
  status text NOT NULL DEFAULT 'draft',
  pay_date date,
  attendance_cutoff date NOT NULL,
  config jsonb NOT NULL,
  company_name text,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  processed_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  published_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  processed_at timestamptz,
  published_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_payroll_runs_period UNIQUE (period),
  CONSTRAINT ck_payroll_runs_period_first CHECK (extract(day from period) = 1),
  CONSTRAINT ck_payroll_runs_status CHECK (status IN ('draft','processed','published')),
  CONSTRAINT ck_payroll_runs_published CHECK (status <> 'published' OR (published_at IS NOT NULL AND published_by IS NOT NULL)),
  CONSTRAINT ck_payroll_runs_company_name CHECK (company_name IS NULL OR char_length(company_name) <= 100)
);
CREATE INDEX ix_payroll_runs_status ON payroll_runs (status);
CREATE INDEX ix_payroll_runs_created_by ON payroll_runs (created_by);
CREATE INDEX ix_payroll_runs_processed_by ON payroll_runs (processed_by);
CREATE INDEX ix_payroll_runs_published_by ON payroll_runs (published_by);
CREATE TRIGGER trg_payroll_runs_updated_at BEFORE UPDATE ON payroll_runs FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE payslips (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES payroll_runs(id) ON DELETE RESTRICT,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  revision smallint NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'draft',
  hold boolean NOT NULL DEFAULT false,
  structure_id uuid REFERENCES salary_structures(id) ON DELETE RESTRICT,
  employee_snapshot jsonb NOT NULL,
  identity_enc bytea,
  basis_days numeric(4,1) NOT NULL,
  employed_days numeric(4,1) NOT NULL,
  lop_auto numeric(4,1) NOT NULL DEFAULT 0,
  lop_override numeric(4,1),
  lop_override_reason_enc bytea,
  paid_days numeric(4,1) NOT NULL,
  lop_breakdown jsonb NOT NULL,
  inputs_enc bytea NOT NULL,
  result_enc bytea,
  warnings text[] NOT NULL DEFAULT '{}',
  revision_reason_enc bytea,
  supersedes_id uuid REFERENCES payslips(id) ON DELETE RESTRICT,
  version integer NOT NULL DEFAULT 1,
  processed_at timestamptz,
  processed_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  published_at timestamptz,
  published_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  superseded_at timestamptz,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_payslips_run_user_revision UNIQUE (run_id, user_id, revision),
  CONSTRAINT ck_payslips_revision CHECK (revision BETWEEN 1 AND 99),
  CONSTRAINT ck_payslips_status CHECK (status IN ('draft','processed','published','superseded')),
  CONSTRAINT ck_payslips_days CHECK (basis_days >= 0 AND employed_days >= 0 AND lop_auto >= 0 AND paid_days >= 0 AND paid_days <= basis_days),
  CONSTRAINT ck_payslips_lop_override CHECK (lop_override IS NULL OR (lop_override >= 0 AND lop_override * 2 = trunc(lop_override * 2))),
  CONSTRAINT ck_payslips_lop_override_reason CHECK (lop_override IS NULL OR lop_override_reason_enc IS NOT NULL),
  CONSTRAINT ck_payslips_result_when_processed CHECK (status = 'draft' OR result_enc IS NOT NULL),
  CONSTRAINT ck_payslips_revision_reason CHECK (revision = 1 OR revision_reason_enc IS NOT NULL),
  CONSTRAINT ck_payslips_version CHECK (version >= 1),
  CONSTRAINT ck_payslips_published CHECK (status NOT IN ('published','superseded') OR (published_at IS NOT NULL AND published_by IS NOT NULL)),
  CONSTRAINT ck_payslips_superseded CHECK (status <> 'superseded' OR superseded_at IS NOT NULL)
);
CREATE UNIQUE INDEX uq_payslips_open ON payslips (run_id, user_id) WHERE status IN ('draft','processed');
CREATE UNIQUE INDEX uq_payslips_live ON payslips (run_id, user_id) WHERE status = 'published';
CREATE INDEX ix_payslips_user_status ON payslips (user_id, status);
CREATE INDEX ix_payslips_run_status ON payslips (run_id, status);
CREATE INDEX ix_payslips_structure_id ON payslips (structure_id);
CREATE INDEX ix_payslips_supersedes_id ON payslips (supersedes_id);
CREATE INDEX ix_payslips_processed_by ON payslips (processed_by);
CREATE INDEX ix_payslips_published_by ON payslips (published_by);
CREATE INDEX ix_payslips_created_by ON payslips (created_by);
CREATE INDEX ix_payslips_updated_by ON payslips (updated_by);
CREATE TRIGGER trg_payslips_updated_at BEFORE UPDATE ON payslips FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION payroll_runs_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'payroll run %% is %% and cannot be deleted', OLD.id, OLD.status USING ERRCODE = '23000';
    END IF;
    IF EXISTS (SELECT 1 FROM payslips WHERE run_id = OLD.id AND status IN ('published','superseded')) THEN
      RAISE EXCEPTION 'payroll run %% has published payslips and cannot be deleted', OLD.id USING ERRCODE = '23000';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.status = 'published' AND (to_jsonb(NEW) - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at') THEN
    RAISE EXCEPTION 'payroll run %% is published and locked', OLD.id USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payroll_runs_lock BEFORE UPDATE OR DELETE ON payroll_runs FOR EACH ROW EXECUTE FUNCTION payroll_runs_lock();

CREATE FUNCTION payroll_strip_enc(j jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
  SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb) FROM jsonb_each(j) WHERE right(key, 4) <> '_enc'
$$;

CREATE FUNCTION payslips_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'payslip %% is %% and cannot be deleted', OLD.id, OLD.status USING ERRCODE = '23000';
    END IF;
    RETURN OLD;
  END IF;
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  IF OLD.status = 'superseded' THEN
    RAISE EXCEPTION 'payslip %% is superseded and frozen', OLD.id USING ERRCODE = '23000';
  END IF;
  IF OLD.status = 'published' THEN
    IF NEW.status <> 'superseded' THEN
      RAISE EXCEPTION 'payslip %% is published; only the transition to superseded is allowed', OLD.id USING ERRCODE = '23000';
    END IF;
    NEW.superseded_at := COALESCE(NEW.superseded_at, now());
    IF (to_jsonb(NEW) - 'status' - 'superseded_at' - 'version' - 'updated_by' - 'updated_at')
       IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'superseded_at' - 'version' - 'updated_by' - 'updated_at') THEN
      RAISE EXCEPTION 'payslip %% is published and locked', OLD.id USING ERRCODE = '23000';
    END IF;
  ELSIF NEW.status = 'superseded' THEN
    RAISE EXCEPTION 'only a published payslip can be superseded' USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payslips_lock BEFORE UPDATE OR DELETE ON payslips FOR EACH ROW EXECUTE FUNCTION payslips_lock();

CREATE FUNCTION payslips_superseded_check() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM payslips WHERE supersedes_id = NEW.id AND status = 'published') THEN
    RAISE EXCEPTION 'payslip %% cannot be superseded without a published successor', NEW.id USING ERRCODE = '23000';
  END IF;
  RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER trg_payslips_superseded_check AFTER UPDATE ON payslips DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (OLD.status = 'published' AND NEW.status = 'superseded') EXECUTE FUNCTION payslips_superseded_check();

CREATE FUNCTION salary_structures_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM payslips WHERE structure_id = OLD.id AND status IN ('processed','published','superseded')) THEN
    IF TG_OP = 'DELETE' THEN
      RETURN OLD;
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'UPDATE' AND current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  RAISE EXCEPTION 'salary structure %% is used by a processed or published payslip and is locked', OLD.id USING ERRCODE = '23000';
END
$$;
CREATE TRIGGER trg_salary_structures_lock BEFORE UPDATE OR DELETE ON salary_structures FOR EACH ROW EXECUTE FUNCTION salary_structures_lock();

INSERT INTO permissions (code, module, description) VALUES ('payroll.view_own', 'payroll', 'View and download own payslips') ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (code, module, description) VALUES ('payroll.view_all', 'payroll', 'View payroll (read-only): runs, structures, payslips') ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (code, module, description) VALUES ('payroll.manage', 'payroll', 'Manage payroll: settings, structures, details, runs, drafts') ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (code, module, description) VALUES ('payroll.publish', 'payroll', 'Publish payroll runs and payslips') ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE p.code = 'payroll.view_own' ON CONFLICT DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name IN ('admin', 'hr') AND p.code IN ('payroll.view_all', 'payroll.manage', 'payroll.publish') ON CONFLICT DO NOTHING;

INSERT INTO settings (key, value) VALUES ('payroll.pay_days_basis', '"calendar"'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.lop_absent_days', 'true'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.lop_half_day_attendance', 'true'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.pf_employee_rate_pct', '12'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.pf_employer_rate_pct', '12'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.pf_wage_ceiling', '15000'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.esi_wage_threshold', '20000'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.esi_employee_rate_pct', '0.75'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.esi_employer_rate_pct', '3.25'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.pt_frequency', '"half_yearly"'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.pt_slabs', '[{"min_gross":"0.00","max_gross":"21000.00","amount":"0.00","months":[3,9]},{"min_gross":"21001.00","max_gross":"30000.00","amount":"135.00","months":[3,9]},{"min_gross":"30001.00","max_gross":"45000.00","amount":"315.00","months":[3,9]},{"min_gross":"45001.00","max_gross":"60000.00","amount":"690.00","months":[3,9]},{"min_gross":"60001.00","max_gross":"75000.00","amount":"1025.00","months":[3,9]},{"min_gross":"75001.00","max_gross":null,"amount":"1250.00","months":[3,9]}]'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.rounding', '"rupee"'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.show_employer_contributions', 'false'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.employer_share_from_employee', 'true'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.mask_pan', 'true'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.publish_requires_second_person', 'false'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.company_address', '""'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.payslip_footer', '"This is a computer-generated payslip and does not require a signature."'::jsonb) ON CONFLICT (key) DO NOTHING;
"""

DOWNGRADE = r"""
DELETE FROM settings WHERE key LIKE 'payroll.%%';
DELETE FROM permissions WHERE code IN ('payroll.view_own', 'payroll.view_all', 'payroll.manage', 'payroll.publish');

DROP TABLE IF EXISTS payslips;
DROP TABLE IF EXISTS payroll_runs;
DROP TABLE IF EXISTS employee_payroll_details;
DROP TABLE IF EXISTS salary_structures;
DROP FUNCTION IF EXISTS payslips_superseded_check();
DROP FUNCTION IF EXISTS salary_structures_lock();
DROP FUNCTION IF EXISTS payslips_lock();
DROP FUNCTION IF EXISTS payroll_strip_enc(jsonb);
DROP FUNCTION IF EXISTS payroll_runs_lock();
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
