"""Full and final settlement with maker-checker (plan 16 work package 4b)

Revision ID: 0026
Revises: 0025

Plan: docs/plans/16-payroll-phase4.md section 2.2; contract: docs/api-contract.md section 18 (18.0, 18.1 [4b], 18.8 to 18.12).

Added
* fnf_settlements            one exit settlement per employee: draft -> submitted -> approved -> settled (+ cancelled; reject and revoke
                             both go back to draft). The plan (override reason, encashment lines, loan modes, other items, move_open_items),
                             the approval summary, the remarks, the decision comment and the cancel reason are application-encrypted.
                             MAKER-CHECKER IS ENFORCED BY THE DATABASE TOO: created_by <> user_id, and whenever `decided_by` is set it
                             differs from the employee, the creator, the last editor and the submitter and is not in `contributors`
                             (a never shrinking list of everybody who created, edited, submitted or withdrew the record). The rule holds
                             for every status, so a rejected or revoked draft keeps its checker bound. A live (draft / submitted /
                             approved) F&F per employee is unique, and so is an approved / settled one per employee and settlement month
                             (uq_fnf_settlements_period).
* fnf_settlements_lock       trigger (INSERT / UPDATE / DELETE): an insert must be a draft; user_id, created_by and created_at never
                             change; contributors never shrink; only the lifecycle transitions of the contract exist
                             (draft>submitted|cancelled, submitted>draft|approved|cancelled, approved>draft|settled);
                             draft>submitted changes nothing but the submit / decision columns and `summary_enc` (the quote snapshot the
                             checker reviews, review M2); submitted>submitted and approved>approved change
                             nothing (the bait-and-switch window between submit and approve is closed in the database);
                             submitted>approved changes only the decision columns and `summary_enc`; approved>settled only
                             `final_payslip_id`; a settled row may move only `final_payslip_id` (revision carry; it stays set); a cancelled
                             row is immutable. `version` and updated_* may always change. Key rotation passes (`app.key_rotation = 'on'` and
                             only *_enc columns changed, updated_at unchanged). Only a draft can be deleted (app_user has no DELETE anyway).
                             No FK of the table is ON DELETE SET NULL, so no pg_trigger_depth() escape is needed.
* employee_payroll_details   `last_working_day` (written only by the F&F service) and `notice_period_days` (0..365).
* payroll_one_time_items     `fnf_id` (RESTRICT, indexed), code CHECK gains NOTICE_RECOVERY, which is system-only: it needs `fnf_id`.
                             A row created by an F&F is never an imported row (`fnf_id` and `batch_id` exclude each other).
* leave_encashments          `fnf_id` (RESTRICT, indexed); a tagged row must have source `fnf` (the reverse is NOT enforced: the 17.5
                             API could already create untagged rows with source `fnf` before this migration).
* payroll_loan_installments  `fnf_id` (RESTRICT, indexed); a tagged row must be a `foreclosure`.
* fnf_id is INSERT-only on the three source tables (trigger *_fnf_id_immutable): an UPDATE can neither set (NULL -> value), change
  nor clear it. A second trigger (*_fnf_user_match) requires a tagged row's user_id to equal fnf_settlements.user_id (INSERT, and UPDATE of
  fnf_id / user_id).
* payroll_item_events        nothing to do: 0025 already allows item_type `fnf` and the actions submit / withdraw / settle / revoke / remove.
* settings payroll.notice_period_days, payroll.notice_recovery_basis, payroll.notice_recovery_divisor (also defaults in code).
* REVOKE DELETE, TRUNCATE FROM app_user on fnf_settlements (cancelling is a status, never a delete).

Downgrade REFUSES (clear exception) while an F&F is approved / settled or a row tagged by an F&F is not cancelled: those rows carry money that
was or will be paid, and a downgrade must never silently delete it (revoke or cancel the F&F first). Otherwise it deletes the cancelled
NOTICE_RECOVERY items, drops the tagged columns, the details columns, the table and the settings, and restores the one-time item code CHECK.
Draft / submitted / cancelled F&F plans are lost. History events (payroll_item_events) are append-only and stay.
"""

from migration_sql import run

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_SETTINGS = ("payroll.notice_period_days", "payroll.notice_recovery_basis", "payroll.notice_recovery_divisor")

UPGRADE = r"""
CREATE TABLE fnf_settlements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  resignation_date date,
  last_working_day date NOT NULL,
  exit_reason text NOT NULL,
  settlement_period date NOT NULL,
  notice_required_days smallint NOT NULL,
  notice_waived boolean NOT NULL DEFAULT false,
  shortfall_days smallint NOT NULL DEFAULT 0,
  plan_enc bytea NOT NULL,
  summary_enc bytea,
  remarks_enc bytea,
  status text NOT NULL DEFAULT 'draft',
  submitted_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  submitted_at timestamptz,
  decided_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  decided_at timestamptz,
  decision_comment_enc bytea,
  cancel_reason_enc bytea,
  final_payslip_id uuid REFERENCES payslips(id) ON DELETE RESTRICT,
  version integer NOT NULL DEFAULT 1,
  created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  last_edited_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  contributors text[] NOT NULL DEFAULT '{}',
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_fnf_settlements_exit_reason CHECK (exit_reason IN ('resignation','termination','retirement','death','contract_end','absconding','other')),
  CONSTRAINT ck_fnf_settlements_status CHECK (status IN ('draft','submitted','approved','settled','cancelled')),
  CONSTRAINT ck_fnf_settlements_lwd_year CHECK (last_working_day BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_fnf_settlements_resignation CHECK (resignation_date IS NULL OR resignation_date <= last_working_day),
  CONSTRAINT ck_fnf_settlements_period_first CHECK (extract(day from settlement_period) = 1),
  CONSTRAINT ck_fnf_settlements_period_window CHECK (settlement_period >= make_date(extract(year from last_working_day)::int, extract(month from last_working_day)::int, 1) AND settlement_period <= (make_date(extract(year from last_working_day)::int, extract(month from last_working_day)::int, 1) + INTERVAL '3 months')::date),
  CONSTRAINT ck_fnf_settlements_notice_required CHECK (notice_required_days BETWEEN 0 AND 365),
  CONSTRAINT ck_fnf_settlements_shortfall CHECK (shortfall_days BETWEEN 0 AND 365 AND shortfall_days <= notice_required_days),
  CONSTRAINT ck_fnf_settlements_shortfall_waived CHECK (NOT notice_waived OR shortfall_days = 0),
  CONSTRAINT ck_fnf_settlements_shortfall_reason CHECK (exit_reason = 'resignation' OR shortfall_days = 0),
  CONSTRAINT ck_fnf_settlements_maker_not_employee CHECK (created_by <> user_id),
  CONSTRAINT ck_fnf_settlements_submitted CHECK ((submitted_by IS NULL) = (submitted_at IS NULL)),
  CONSTRAINT ck_fnf_settlements_decided_pair CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
  CONSTRAINT ck_fnf_settlements_live_submitted CHECK (status NOT IN ('submitted','approved','settled') OR submitted_by IS NOT NULL),
  CONSTRAINT ck_fnf_settlements_decided_status CHECK (status NOT IN ('approved','settled') OR decided_by IS NOT NULL),
  CONSTRAINT ck_fnf_settlements_summary CHECK (status NOT IN ('approved','settled') OR summary_enc IS NOT NULL),
  CONSTRAINT ck_fnf_settlements_cancelled CHECK (status <> 'cancelled' OR cancel_reason_enc IS NOT NULL),
  CONSTRAINT ck_fnf_settlements_settled CHECK ((status = 'settled') = (final_payslip_id IS NOT NULL)),
  CONSTRAINT ck_fnf_settlements_checker CHECK (decided_by IS NULL OR (decided_by <> user_id AND decided_by IS DISTINCT FROM created_by AND decided_by IS DISTINCT FROM last_edited_by AND decided_by IS DISTINCT FROM submitted_by)),
  CONSTRAINT ck_fnf_settlements_checker_history CHECK (decided_by IS NULL OR decided_by::text <> ALL (contributors)),
  CONSTRAINT ck_fnf_settlements_version CHECK (version >= 1)
);
CREATE UNIQUE INDEX uq_fnf_settlements_live ON fnf_settlements (user_id) WHERE status IN ('draft','submitted','approved');
CREATE UNIQUE INDEX uq_fnf_settlements_period ON fnf_settlements (user_id, settlement_period) WHERE status IN ('approved','settled');
CREATE INDEX ix_fnf_settlements_status_created ON fnf_settlements (status, created_at);
CREATE INDEX ix_fnf_settlements_user_created ON fnf_settlements (user_id, created_at);
CREATE INDEX ix_fnf_settlements_period_status ON fnf_settlements (settlement_period, status);
CREATE INDEX ix_fnf_settlements_final_payslip_id ON fnf_settlements (final_payslip_id);
CREATE INDEX ix_fnf_settlements_submitted_by ON fnf_settlements (submitted_by);
CREATE INDEX ix_fnf_settlements_decided_by ON fnf_settlements (decided_by);
CREATE INDEX ix_fnf_settlements_created_by ON fnf_settlements (created_by);
CREATE INDEX ix_fnf_settlements_last_edited_by ON fnf_settlements (last_edited_by);
CREATE INDEX ix_fnf_settlements_updated_by ON fnf_settlements (updated_by);

CREATE FUNCTION fnf_settlements_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  j_new jsonb;
  j_old jsonb;
  allowed text[] := ARRAY['version','updated_by','updated_at'];
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'draft' THEN
      RAISE EXCEPTION 'a final settlement must be created as draft' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'final settlement %% is %% and cannot be deleted', OLD.id, OLD.status USING ERRCODE = '23000';
    END IF;
    RETURN OLD;
  END IF;
  IF NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'final settlement %%: employee, creator and creation time never change', OLD.id USING ERRCODE = '23000';
  END IF;
  IF NOT (NEW.contributors @> OLD.contributors) THEN
    RAISE EXCEPTION 'final settlement %%: contributors can only grow', OLD.id USING ERRCODE = '23000';
  END IF;
  -- app.key_rotation is a plain session setting: rotation runs as the application role, so a role check is not possible
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  IF NEW.status <> OLD.status AND (OLD.status || '>' || NEW.status) NOT IN
     ('draft>submitted','draft>cancelled','submitted>draft','submitted>approved','submitted>cancelled','approved>draft','approved>settled') THEN
    RAISE EXCEPTION 'final settlement %%: %% to %% is not a valid transition', OLD.id, OLD.status, NEW.status USING ERRCODE = '23000';
  END IF;
  IF OLD.status = 'cancelled' THEN
    NULL;  -- immutable (version / updated_* only)
  ELSIF OLD.status = 'settled' THEN
    allowed := allowed || ARRAY['final_payslip_id'];  -- revision carry
  ELSIF OLD.status = 'submitted' AND NEW.status = 'submitted' THEN
    NULL;  -- nothing but version / updated_*
  ELSIF OLD.status = 'submitted' AND NEW.status = 'approved' THEN
    allowed := allowed || ARRAY['status','decided_by','decided_at','decision_comment_enc','summary_enc'];
  ELSIF OLD.status = 'approved' AND NEW.status = 'approved' THEN
    NULL;
  ELSIF OLD.status = 'approved' AND NEW.status = 'settled' THEN
    allowed := allowed || ARRAY['status','final_payslip_id'];
  ELSIF OLD.status = 'draft' AND NEW.status = 'submitted' THEN
    allowed := allowed || ARRAY['status','submitted_by','submitted_at','contributors','decided_by','decided_at','decision_comment_enc','summary_enc'];
  ELSE
    RETURN NEW;  -- draft edits, cancel, reject / withdraw, revoke: free
  END IF;
  j_new := to_jsonb(NEW) - allowed;
  j_old := to_jsonb(OLD) - allowed;
  IF j_new IS DISTINCT FROM j_old THEN
    RAISE EXCEPTION 'final settlement %% is %% and locked', OLD.id, OLD.status USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_fnf_settlements_lock BEFORE INSERT OR UPDATE OR DELETE ON fnf_settlements FOR EACH ROW EXECUTE FUNCTION fnf_settlements_lock();
CREATE TRIGGER trg_fnf_settlements_updated_at BEFORE UPDATE ON fnf_settlements FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE employee_payroll_details ADD COLUMN last_working_day date;
ALTER TABLE employee_payroll_details ADD COLUMN notice_period_days smallint;
ALTER TABLE employee_payroll_details ADD CONSTRAINT ck_employee_payroll_details_lwd_year CHECK (last_working_day IS NULL OR last_working_day BETWEEN DATE '2000-01-01' AND DATE '2200-12-31');
ALTER TABLE employee_payroll_details ADD CONSTRAINT ck_employee_payroll_details_notice_days CHECK (notice_period_days IS NULL OR notice_period_days BETWEEN 0 AND 365);

CREATE FUNCTION payroll_fnf_id_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.fnf_id IS DISTINCT FROM OLD.fnf_id THEN
    RAISE EXCEPTION 'payroll item %% : fnf_id is set on INSERT only and never changes', OLD.id USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;

CREATE FUNCTION payroll_fnf_user_match() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.fnf_id IS NOT NULL AND NEW.user_id IS DISTINCT FROM (SELECT f.user_id FROM fnf_settlements f WHERE f.id = NEW.fnf_id) THEN
    RAISE EXCEPTION 'payroll item %% : a row tagged by a final settlement must belong to the settlement employee', NEW.id USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;

ALTER TABLE payroll_one_time_items ADD COLUMN fnf_id uuid REFERENCES fnf_settlements(id) ON DELETE RESTRICT;
ALTER TABLE payroll_one_time_items DROP CONSTRAINT ck_payroll_one_time_items_code;
ALTER TABLE payroll_one_time_items ADD CONSTRAINT ck_payroll_one_time_items_code CHECK (code IN ('BONUS','INCENTIVE','OTHER_EARNING','OTHER_DEDUCTION','NOTICE_RECOVERY'));
ALTER TABLE payroll_one_time_items ADD CONSTRAINT ck_payroll_one_time_items_notice_system CHECK (code <> 'NOTICE_RECOVERY' OR fnf_id IS NOT NULL);
ALTER TABLE payroll_one_time_items ADD CONSTRAINT ck_payroll_one_time_items_fnf_batch CHECK (fnf_id IS NULL OR batch_id IS NULL);
CREATE INDEX ix_payroll_one_time_items_fnf_id ON payroll_one_time_items (fnf_id);
CREATE TRIGGER trg_payroll_one_time_items_fnf_id_immutable BEFORE UPDATE OF fnf_id ON payroll_one_time_items FOR EACH ROW EXECUTE FUNCTION payroll_fnf_id_immutable();
CREATE TRIGGER trg_payroll_one_time_items_fnf_user_match BEFORE INSERT OR UPDATE OF fnf_id, user_id ON payroll_one_time_items FOR EACH ROW EXECUTE FUNCTION payroll_fnf_user_match();

ALTER TABLE leave_encashments ADD COLUMN fnf_id uuid REFERENCES fnf_settlements(id) ON DELETE RESTRICT;
ALTER TABLE leave_encashments ADD CONSTRAINT ck_leave_encashments_fnf_source CHECK (fnf_id IS NULL OR source = 'fnf');
CREATE INDEX ix_leave_encashments_fnf_id ON leave_encashments (fnf_id);
CREATE TRIGGER trg_leave_encashments_fnf_id_immutable BEFORE UPDATE OF fnf_id ON leave_encashments FOR EACH ROW EXECUTE FUNCTION payroll_fnf_id_immutable();
CREATE TRIGGER trg_leave_encashments_fnf_user_match BEFORE INSERT OR UPDATE OF fnf_id, user_id ON leave_encashments FOR EACH ROW EXECUTE FUNCTION payroll_fnf_user_match();

ALTER TABLE payroll_loan_installments ADD COLUMN fnf_id uuid REFERENCES fnf_settlements(id) ON DELETE RESTRICT;
ALTER TABLE payroll_loan_installments ADD CONSTRAINT ck_payroll_loan_installments_fnf_kind CHECK (fnf_id IS NULL OR kind = 'foreclosure');
CREATE INDEX ix_payroll_loan_installments_fnf_id ON payroll_loan_installments (fnf_id);
CREATE TRIGGER trg_payroll_loan_installments_fnf_id_immutable BEFORE UPDATE OF fnf_id ON payroll_loan_installments FOR EACH ROW EXECUTE FUNCTION payroll_fnf_id_immutable();
CREATE TRIGGER trg_payroll_loan_installments_fnf_user_match BEFORE INSERT OR UPDATE OF fnf_id, user_id ON payroll_loan_installments FOR EACH ROW EXECUTE FUNCTION payroll_fnf_user_match();

INSERT INTO settings (key, value) VALUES ('payroll.notice_period_days', '30'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.notice_recovery_basis', '"gross"'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.notice_recovery_divisor', '30'::jsonb) ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  REVOKE DELETE, TRUNCATE ON fnf_settlements FROM app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'revoke on fnf_settlements skipped';
END
$$;
"""

DOWNGRADE = r"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM fnf_settlements WHERE status IN ('approved','settled'))
     OR EXISTS (SELECT 1 FROM payroll_one_time_items WHERE fnf_id IS NOT NULL AND status <> 'cancelled')
     OR EXISTS (SELECT 1 FROM leave_encashments WHERE fnf_id IS NOT NULL AND status <> 'cancelled')
     OR EXISTS (SELECT 1 FROM payroll_loan_installments WHERE fnf_id IS NOT NULL AND status NOT IN ('cancelled','skipped')) THEN
    RAISE EXCEPTION 'cannot downgrade 0026: approved / settled final settlements or live rows created by one exist (revoke or cancel them first)';
  END IF;
END
$$;

DELETE FROM settings WHERE key IN ('payroll.notice_period_days', 'payroll.notice_recovery_basis', 'payroll.notice_recovery_divisor');

DROP TRIGGER IF EXISTS trg_payroll_loan_installments_fnf_user_match ON payroll_loan_installments;
DROP TRIGGER IF EXISTS trg_payroll_loan_installments_fnf_id_immutable ON payroll_loan_installments;
DROP INDEX IF EXISTS ix_payroll_loan_installments_fnf_id;
ALTER TABLE payroll_loan_installments DROP CONSTRAINT IF EXISTS ck_payroll_loan_installments_fnf_kind;
ALTER TABLE payroll_loan_installments DROP COLUMN IF EXISTS fnf_id;

DROP TRIGGER IF EXISTS trg_leave_encashments_fnf_user_match ON leave_encashments;
DROP TRIGGER IF EXISTS trg_leave_encashments_fnf_id_immutable ON leave_encashments;
DROP INDEX IF EXISTS ix_leave_encashments_fnf_id;
ALTER TABLE leave_encashments DROP CONSTRAINT IF EXISTS ck_leave_encashments_fnf_source;
ALTER TABLE leave_encashments DROP COLUMN IF EXISTS fnf_id;

DROP TRIGGER IF EXISTS trg_payroll_one_time_items_fnf_user_match ON payroll_one_time_items;
DROP TRIGGER IF EXISTS trg_payroll_one_time_items_fnf_id_immutable ON payroll_one_time_items;
DROP INDEX IF EXISTS ix_payroll_one_time_items_fnf_id;
ALTER TABLE payroll_one_time_items DROP CONSTRAINT IF EXISTS ck_payroll_one_time_items_fnf_batch;
ALTER TABLE payroll_one_time_items DROP CONSTRAINT IF EXISTS ck_payroll_one_time_items_notice_system;
DELETE FROM payroll_one_time_items WHERE code = 'NOTICE_RECOVERY';
ALTER TABLE payroll_one_time_items DROP CONSTRAINT ck_payroll_one_time_items_code;
ALTER TABLE payroll_one_time_items ADD CONSTRAINT ck_payroll_one_time_items_code CHECK (code IN ('BONUS','INCENTIVE','OTHER_EARNING','OTHER_DEDUCTION'));
ALTER TABLE payroll_one_time_items DROP COLUMN IF EXISTS fnf_id;
DROP FUNCTION IF EXISTS payroll_fnf_user_match();
DROP FUNCTION IF EXISTS payroll_fnf_id_immutable();

ALTER TABLE employee_payroll_details DROP CONSTRAINT IF EXISTS ck_employee_payroll_details_notice_days;
ALTER TABLE employee_payroll_details DROP CONSTRAINT IF EXISTS ck_employee_payroll_details_lwd_year;
ALTER TABLE employee_payroll_details DROP COLUMN IF EXISTS notice_period_days;
ALTER TABLE employee_payroll_details DROP COLUMN IF EXISTS last_working_day;

DROP TABLE IF EXISTS fnf_settlements;
DROP FUNCTION IF EXISTS fnf_settlements_lock();
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
