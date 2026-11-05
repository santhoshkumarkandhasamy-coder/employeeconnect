"""Salary increments with maker-checker, increment batches, structure provenance (plan 16 work package 4a)

Revision ID: 0024
Revises: 0023

Plan: docs/plans/16-payroll-phase4.md section 2.1; contract: docs/api-contract.md section 18 (18.4 to 18.7, 18.11).

Added
* salary_increment_batches  one bulk hike (kind `percent`) or one committed structure import (kind `import`, step 2 of 4a).
                            Percent and reason are application-encrypted. An imported file (sha256) can be committed once.
* salary_increments         one proposed salary change per employee and month. draft -> submitted -> approved | rejected
                            (+ cancelled). The proposed structure (encrypted JSON), the percent, the gross figures, the reason
                            and the decision comment are application-encrypted. MAKER-CHECKER IS ENFORCED BY THE DATABASE TOO:
                            created_by <> user_id, and an approved row needs decided_by that differs from the employee, the
                            creator and the submitter. A live (draft / submitted) increment per employee and month is unique.
* salary_increments_lock    trigger: an approved / rejected / cancelled row is immutable except the FK `ON DELETE SET NULL` of
                            structure_id / base_structure_id / replaces_structure_id (and updated_*); key rotation passes
                            (`app.key_rotation = 'on'` and only *_enc columns changed, updated_at unchanged). Only a draft
                            row can be deleted at all (and app_user has no DELETE privilege anyway).
* salary_structures         `source` (manual | increment | import), `increment_id` (circular FK, RESTRICT) and
                            `import_batch_id`. The existing salary_structures_lock already compares every column.
* payroll_item_events       item_type gains `increment`, `fnf`; action gains `submit`, `withdraw`, `settle`, `revoke`, `remove`
                            (the two CHECKs are recreated; 0025 uses the F&F values).
* permission `payroll.approve` (module payroll) granted to the admin and hr roles.
* settings payroll.structure_changes_need_approval (default true), payroll.split_*, payroll.increment_*,
  payroll.ctc_include_gratuity, payroll.gratuity_* (also defaults in code).
* REVOKE DELETE, TRUNCATE FROM app_user on both new tables (cancelling is a status, never a delete).

Downgrade drops both tables (ALL increment and batch data is lost), the two salary_structures columns, the permission and the
settings, restores the two payroll_item_events CHECKs and DELETES the increment / fnf history events (the append-only trigger is
disabled for that statement; the rows describe tables that no longer exist). It does not touch structures that were created by
an increment (they stay as plain structures).
"""

from migration_sql import run

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_EVENT_TYPES_NEW = "'one_time','loan','loan_installment','arrears','encashment','reimbursement','overtime','increment','fnf'"
_EVENT_TYPES_OLD = "'one_time','loan','loan_installment','arrears','encashment','reimbursement','overtime'"
_EVENT_ACTIONS_OLD = "'create','update','cancel','skip','reschedule','foreclose','close','propose','accept','dismiss','verify','approve','reject','process','reopen','pay','carry'"
_EVENT_ACTIONS_NEW = _EVENT_ACTIONS_OLD + ",'submit','withdraw','settle','revoke','remove'"

UPGRADE = (
    r"""
CREATE TABLE salary_increment_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text NOT NULL,
  effective_from date,
  percent_enc bytea,
  method text,
  file_sha256 text,
  rows integer NOT NULL,
  reason_enc bytea NOT NULL,
  created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  cancelled_at timestamptz,
  cancelled_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_salary_increment_batches_kind CHECK (kind IN ('percent','import')),
  CONSTRAINT ck_salary_increment_batches_method CHECK (method IS NULL OR method IN ('proportional','split_from_gross')),
  CONSTRAINT ck_salary_increment_batches_effective_first CHECK (effective_from IS NULL OR extract(day from effective_from) = 1),
  CONSTRAINT ck_salary_increment_batches_effective_year CHECK (effective_from IS NULL OR effective_from BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_salary_increment_batches_sha256 CHECK (file_sha256 IS NULL OR file_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_salary_increment_batches_import_sha CHECK ((kind = 'import') = (file_sha256 IS NOT NULL)),
  CONSTRAINT ck_salary_increment_batches_percent_fields CHECK (kind <> 'percent' OR (effective_from IS NOT NULL AND percent_enc IS NOT NULL AND method IS NOT NULL)),
  CONSTRAINT ck_salary_increment_batches_rows CHECK (rows BETWEEN 1 AND 500),
  CONSTRAINT ck_salary_increment_batches_cancelled CHECK ((cancelled_at IS NULL) = (cancelled_by IS NULL))
);
CREATE UNIQUE INDEX uq_salary_increment_batches_import_sha ON salary_increment_batches (file_sha256) WHERE kind = 'import';
CREATE INDEX ix_salary_increment_batches_created ON salary_increment_batches (kind, created_at);
CREATE INDEX ix_salary_increment_batches_created_by ON salary_increment_batches (created_by);
CREATE INDEX ix_salary_increment_batches_cancelled_by ON salary_increment_batches (cancelled_by);

CREATE TABLE salary_increments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  batch_id uuid REFERENCES salary_increment_batches(id) ON DELETE RESTRICT,
  kind text NOT NULL,
  effective_from date NOT NULL,
  method text,
  percent_enc bytea,
  base_structure_id uuid REFERENCES salary_structures(id) ON DELETE SET NULL,
  base_updated_at timestamptz,
  replaces_structure_id uuid REFERENCES salary_structures(id) ON DELETE SET NULL,
  proposed_enc bytea,
  old_gross_enc bytea,
  new_gross_enc bytea,
  warnings text[] NOT NULL DEFAULT '{}',
  reason_enc bytea NOT NULL,
  status text NOT NULL DEFAULT 'draft',
  submitted_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  submitted_at timestamptz,
  decided_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  decided_at timestamptz,
  decision_comment_enc bytea,
  structure_id uuid REFERENCES salary_structures(id) ON DELETE SET NULL,
  version integer NOT NULL DEFAULT 1,
  created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_salary_increments_kind CHECK (kind IN ('percent','amount','structure','remove','import')),
  CONSTRAINT ck_salary_increments_method CHECK (method IS NULL OR method IN ('proportional','split_from_gross')),
  CONSTRAINT ck_salary_increments_effective_first CHECK (extract(day from effective_from) = 1),
  CONSTRAINT ck_salary_increments_effective_year CHECK (effective_from BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_salary_increments_status CHECK (status IN ('draft','submitted','approved','rejected','cancelled')),
  CONSTRAINT ck_salary_increments_warnings CHECK (warnings <@ ARRAY['BACKDATED','TDS_REVIEW','DECREASE','LARGE_CHANGE','ESI_THRESHOLD_CROSSED','PF_CEILING_CROSSED','NO_CHANGE']::text[]),
  CONSTRAINT ck_salary_increments_remove CHECK ((kind = 'remove') = (proposed_enc IS NULL)),
  CONSTRAINT ck_salary_increments_percent_fields CHECK (kind <> 'percent' OR (percent_enc IS NOT NULL AND method IS NOT NULL)),
  CONSTRAINT ck_salary_increments_maker_not_employee CHECK (created_by <> user_id),
  CONSTRAINT ck_salary_increments_submitted CHECK ((submitted_by IS NULL) = (submitted_at IS NULL)),
  CONSTRAINT ck_salary_increments_decided_pair CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
  CONSTRAINT ck_salary_increments_decided_status CHECK (status NOT IN ('approved','rejected') OR decided_by IS NOT NULL),
  CONSTRAINT ck_salary_increments_live_submitted CHECK (status NOT IN ('submitted','approved') OR submitted_by IS NOT NULL),
  CONSTRAINT ck_salary_increments_rejected_comment CHECK (status <> 'rejected' OR decision_comment_enc IS NOT NULL),
  CONSTRAINT ck_salary_increments_checker CHECK (status <> 'approved' OR (decided_by IS NOT NULL AND decided_by <> user_id AND decided_by IS DISTINCT FROM created_by AND decided_by IS DISTINCT FROM submitted_by)),
  CONSTRAINT ck_salary_increments_version CHECK (version >= 1)
);
CREATE UNIQUE INDEX uq_salary_increments_live ON salary_increments (user_id, effective_from) WHERE status IN ('draft','submitted');
CREATE INDEX ix_salary_increments_status_created ON salary_increments (status, created_at);
CREATE INDEX ix_salary_increments_batch_id ON salary_increments (batch_id);
CREATE INDEX ix_salary_increments_user_created ON salary_increments (user_id, created_at);
CREATE INDEX ix_salary_increments_base_structure_id ON salary_increments (base_structure_id);
CREATE INDEX ix_salary_increments_replaces_structure_id ON salary_increments (replaces_structure_id);
CREATE INDEX ix_salary_increments_structure_id ON salary_increments (structure_id);
CREATE INDEX ix_salary_increments_submitted_by ON salary_increments (submitted_by);
CREATE INDEX ix_salary_increments_decided_by ON salary_increments (decided_by);
CREATE INDEX ix_salary_increments_created_by ON salary_increments (created_by);
CREATE INDEX ix_salary_increments_updated_by ON salary_increments (updated_by);

CREATE FUNCTION salary_increments_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  j_new jsonb;
  j_old jsonb;
  col text;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'salary increment %% is %% and cannot be deleted', OLD.id, OLD.status USING ERRCODE = '23000';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.status NOT IN ('approved','rejected','cancelled') THEN
    RETURN NEW;
  END IF;
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  j_new := to_jsonb(NEW) - 'updated_by' - 'updated_at';
  j_old := to_jsonb(OLD) - 'updated_by' - 'updated_at';
  FOREACH col IN ARRAY ARRAY['structure_id','base_structure_id','replaces_structure_id'] LOOP
    IF (to_jsonb(NEW) -> col) = 'null'::jsonb THEN
      j_new := j_new - col;  -- ON DELETE SET NULL of a structure
      j_old := j_old - col;
    END IF;
  END LOOP;
  IF j_new IS DISTINCT FROM j_old THEN
    RAISE EXCEPTION 'salary increment %% is %% and locked', OLD.id, OLD.status USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_salary_increments_lock BEFORE UPDATE OR DELETE ON salary_increments FOR EACH ROW EXECUTE FUNCTION salary_increments_lock();
CREATE TRIGGER trg_salary_increments_updated_at BEFORE UPDATE ON salary_increments FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE salary_structures ADD COLUMN source text NOT NULL DEFAULT 'manual';
ALTER TABLE salary_structures ADD COLUMN increment_id uuid REFERENCES salary_increments(id) ON DELETE RESTRICT;
ALTER TABLE salary_structures ADD COLUMN import_batch_id uuid REFERENCES salary_increment_batches(id) ON DELETE RESTRICT;
ALTER TABLE salary_structures ADD CONSTRAINT ck_salary_structures_source CHECK (source IN ('manual','increment','import'));
ALTER TABLE salary_structures ADD CONSTRAINT ck_salary_structures_increment CHECK ((source = 'increment') = (increment_id IS NOT NULL));
CREATE INDEX ix_salary_structures_increment_id ON salary_structures (increment_id);
CREATE INDEX ix_salary_structures_import_batch_id ON salary_structures (import_batch_id);

ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_item_type;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_item_type CHECK (item_type IN ("""
    + _EVENT_TYPES_NEW
    + r"""));
ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_action;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_action CHECK (action IN ("""
    + _EVENT_ACTIONS_NEW
    + r"""));

INSERT INTO permissions (code, module, description) VALUES ('payroll.approve', 'payroll', 'Approve or reject salary increments (maker-checker)') ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name IN ('admin', 'hr') AND p.code = 'payroll.approve' ON CONFLICT DO NOTHING;

INSERT INTO settings (key, value) VALUES ('payroll.structure_changes_need_approval', 'true'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.split_basic_pct', '50'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.split_hra_pct_of_basic', '40'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.split_allowance_code', '"SPECIAL"'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.split_allowance_name', '"Special allowance"'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.increment_max_pct', '50'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.increment_backdate_months', '12'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.ctc_include_gratuity', 'true'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.gratuity_min_years', '5'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.gratuity_cap', '"2000000.00"'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.gratuity_wage_basis', '"basic"'::jsonb) ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  REVOKE DELETE, TRUNCATE ON salary_increment_batches FROM app_user;
  REVOKE DELETE, TRUNCATE ON salary_increments FROM app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'revoke on salary_increment_batches / salary_increments skipped';
END
$$;
"""
)

DOWNGRADE = (
    r"""
DELETE FROM settings WHERE key IN ('payroll.structure_changes_need_approval', 'payroll.split_basic_pct', 'payroll.split_hra_pct_of_basic', 'payroll.split_allowance_code', 'payroll.split_allowance_name', 'payroll.increment_max_pct', 'payroll.increment_backdate_months', 'payroll.ctc_include_gratuity', 'payroll.gratuity_min_years', 'payroll.gratuity_cap', 'payroll.gratuity_wage_basis');
DELETE FROM permissions WHERE code = 'payroll.approve';

ALTER TABLE payroll_item_events DISABLE TRIGGER trg_payroll_item_events_no_update_delete;
DELETE FROM payroll_item_events WHERE item_type IN ('increment','fnf') OR action IN ('submit','withdraw','settle','revoke','remove');
ALTER TABLE payroll_item_events ENABLE TRIGGER trg_payroll_item_events_no_update_delete;
ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_action;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_action CHECK (action IN ("""
    + _EVENT_ACTIONS_OLD
    + r"""));
ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_item_type;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_item_type CHECK (item_type IN ("""
    + _EVENT_TYPES_OLD
    + r"""));

DROP INDEX IF EXISTS ix_salary_structures_import_batch_id;
DROP INDEX IF EXISTS ix_salary_structures_increment_id;
ALTER TABLE salary_structures DROP CONSTRAINT IF EXISTS ck_salary_structures_increment;
ALTER TABLE salary_structures DROP CONSTRAINT IF EXISTS ck_salary_structures_source;
ALTER TABLE salary_structures DROP COLUMN IF EXISTS import_batch_id;
ALTER TABLE salary_structures DROP COLUMN IF EXISTS increment_id;
ALTER TABLE salary_structures DROP COLUMN IF EXISTS source;

DROP TABLE IF EXISTS salary_increments;
DROP TABLE IF EXISTS salary_increment_batches;
DROP FUNCTION IF EXISTS salary_increments_lock();
"""
)


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
