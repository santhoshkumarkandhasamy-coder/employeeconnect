"""Publish maker-checker, bank payment batches, payslip delivery (plan 17 work package 5a)

Revision ID: 0026
Revises: 0025

Plan: docs/plans/17-payroll-phase5.md section 2.1; contract: docs/api-contract.md section 19 (19.0 to 19.5, 19.10).

Added
* payroll_runs.contributors / payslips.contributors   text[] NOT NULL DEFAULT '{}': user ids (text) of everybody who created, edited, processed,
                             refreshed, revised or reopened the row. Backfilled from created_by / processed_by (runs) and created_by /
                             updated_by / processed_by (payslips). payroll_contributors_guard(): a value can only GROW (never shrink),
                             and the contributors of a payslip that is published or superseded are frozen. (payslips_lock already
                             refuses every other column change of a published row, so the 0018 function is NOT replaced.)
* payroll_publish_requests   maker-checker for publish. status submitted -> approved | rejected | withdrawn | stale. The bound snapshot
                             (snapshot_enc: {payslip_id, version, result_mac} set, counts, totals, processed_at) and its HMAC are
                             write-once. MAKER-CHECKER IS ENFORCED BY THE DATABASE: whenever `decided_by` is set it differs from the requester,
                             is not in the append-only `makers` list, and `makers` contains the requester (ck_..._checker, _checker_history,
                             _requester_in_makers). payroll_publish_requests_checker_guard() repeats the part the database can see: the
                             decider is not in the contributors of the run (scope run), of the processed non-held payslips of the run
                             (scope run) or of the payslip (scope payslip), and is not the employee of a scope-payslip request.
                             payroll_publish_requests_lock(): inserts are `submitted`, only submitted -> final transitions exist, a final row
                             is immutable, `makers` only grows, key rotation passes. Partial uniques: ONE open request per run (scope run)
                             and per payslip (scope payslip). Tagged-row trigger: the payslip belongs to the run.
* payslip_deliveries         push-only delivery tracking, one row per published payslip. first_viewed_at / first_downloaded_at / pushed_at are
                             write-once, counters and last_reminded_at never go back, user_id must equal the payslip's user.
* company_bank_accounts      debit accounts of the company. account / IFSC / bank name are encrypted and IMMUTABLE (create a new account to
                             change; key rotation passes). One active default (partial unique).
* employee_payroll_details   bank_changed_by / bank_changed_at. employee_payroll_details_bank_guard(): the marker can never be cleared or moved
                             back, and a change of payment_mode / bank name / account / IFSC (outside key rotation) must come with a fresh marker.
* payroll_payment_batches    draft -> submitted -> approved -> exported -> closed (+ cancelled; withdraw and reject go back to draft).
                             `format` and `payment_type` are fixed at creation, so a later download is byte-identical even if the settings
                             change. lines_mac is set at submit. Maker-checker enforced by the database too: decided_by differs from the
                             creator, last editor and submitter and is not in `contributors`; contributors must contain creator, last editor
                             and submitter. The lock trigger allows only the lifecycle transitions and, per transition, only its own columns;
                             at approve it also refuses a decider who is the employee of a live line or who changed the bank details of a line
                             employee within payroll.bank_change_alert_days. A deferred constraint trigger checks that a cancelled / exported /
                             closed batch has consistent line statuses at commit.
* payroll_payment_lines      one beneficiary per line. Partial unique (payslip_id) WHERE status IN (pending, exported, paid): one live line per
                             payslip. (batch_id, seq) unique. Insert only into a draft batch, only for a published, non-held payslip of the batch's
                             run with the same user (tagged-row trigger). batch/payslip/user/seq never change. While the batch is draft the
                             amount, beneficiary, flags, account_source and pending/excluded status may change; afterwards only
                             pending>exported (batch approved / exported), exported>paid|failed with reference_enc / paid_on (batch exported /
                             closed) and pending>excluded (batch cancelled).
* payroll_item_events        item_type gains `publish_request`, `payment_batch`; action gains `export`, `confirm`, `expire`, `stale`.
* settings payroll.publish_request_ttl_hours, payroll.bank_file_requires_second_person, payroll.bank_file_format, payroll.bank_payment_type,
  payroll.bank_change_alert_days (also defaults in code). No new permission: payroll.approve / payroll.publish / payroll.manage exist.
* REVOKE DELETE, TRUNCATE FROM app_user on all five new tables. No foreign key of the new tables is ON DELETE SET NULL, so no
  pg_trigger_depth() escape is needed.

Downgrade REFUSES while a publish request is submitted / approved or a payment batch is anything but draft / cancelled (their evidence and
the money they describe must never be dropped silently). Otherwise it drops the five tables, the columns, triggers, functions and settings,
DELETES the publish_request / payment_batch history events (append-only trigger disabled for that statement) and restores the two
payroll_item_events CHECKs. Draft batches and all payslip delivery data are lost.
"""

from migration_sql import run

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_SETTINGS = (
    "payroll.publish_request_ttl_hours",
    "payroll.bank_file_requires_second_person",
    "payroll.bank_file_format",
    "payroll.bank_payment_type",
    "payroll.bank_change_alert_days",
)

_EVENT_TYPES_OLD = "'one_time','loan','loan_installment','arrears','encashment','reimbursement','overtime','increment','fnf'"
_EVENT_TYPES_NEW = _EVENT_TYPES_OLD + ",'publish_request','payment_batch'"
_EVENT_ACTIONS_OLD = (
    "'create','update','cancel','skip','reschedule','foreclose','close','propose','accept','dismiss','verify','approve','reject',"
    "'process','reopen','pay','carry','submit','withdraw','settle','revoke','remove'"
)
_EVENT_ACTIONS_NEW = _EVENT_ACTIONS_OLD + ",'export','confirm','expire','stale'"

UPGRADE = (
    r"""
-- ---------------------------------------------------------------- contributors on runs and payslips
ALTER TABLE payroll_runs ADD COLUMN contributors text[] NOT NULL DEFAULT '{}';
ALTER TABLE payslips ADD COLUMN contributors text[] NOT NULL DEFAULT '{}';

-- backfill: published / superseded rows are locked, so the lock and updated_at triggers are switched off for these two statements
ALTER TABLE payroll_runs DISABLE TRIGGER trg_payroll_runs_lock;
ALTER TABLE payroll_runs DISABLE TRIGGER trg_payroll_runs_updated_at;
UPDATE payroll_runs SET contributors = ARRAY(SELECT DISTINCT v FROM unnest(ARRAY[created_by::text, processed_by::text]) AS v WHERE v IS NOT NULL ORDER BY v);
ALTER TABLE payroll_runs ENABLE TRIGGER trg_payroll_runs_updated_at;
ALTER TABLE payroll_runs ENABLE TRIGGER trg_payroll_runs_lock;
ALTER TABLE payslips DISABLE TRIGGER trg_payslips_lock;
ALTER TABLE payslips DISABLE TRIGGER trg_payslips_updated_at;
UPDATE payslips SET contributors = ARRAY(SELECT DISTINCT v FROM unnest(ARRAY[created_by::text, updated_by::text, processed_by::text]) AS v WHERE v IS NOT NULL ORDER BY v);
ALTER TABLE payslips ENABLE TRIGGER trg_payslips_updated_at;
ALTER TABLE payslips ENABLE TRIGGER trg_payslips_lock;

CREATE FUNCTION payroll_contributors_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.contributors IS NOT DISTINCT FROM OLD.contributors THEN
    RETURN NEW;
  END IF;
  IF NOT (NEW.contributors @> OLD.contributors) THEN
    RAISE EXCEPTION '%% %%: contributors can only grow', TG_TABLE_NAME, OLD.id USING ERRCODE = '23000';
  END IF;
  IF TG_TABLE_NAME = 'payslips' AND OLD.status NOT IN ('draft','processed') THEN
    RAISE EXCEPTION 'payslip %% is %% and its contributors are frozen', OLD.id, OLD.status USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payroll_runs_contributors BEFORE UPDATE OF contributors ON payroll_runs FOR EACH ROW EXECUTE FUNCTION payroll_contributors_guard();
CREATE TRIGGER trg_payslips_contributors BEFORE UPDATE OF contributors ON payslips FOR EACH ROW EXECUTE FUNCTION payroll_contributors_guard();

-- ---------------------------------------------------------------- company (debit) bank accounts
CREATE TABLE company_bank_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  label text NOT NULL,
  bank_name_enc bytea NOT NULL,
  account_enc bytea NOT NULL,
  ifsc_enc bytea NOT NULL,
  is_default boolean NOT NULL DEFAULT false,
  active boolean NOT NULL DEFAULT true,
  created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_company_bank_accounts_label CHECK (char_length(btrim(label)) BETWEEN 1 AND 40)
);
CREATE UNIQUE INDEX uq_company_bank_accounts_default ON company_bank_accounts (is_default) WHERE is_default AND active;
CREATE INDEX ix_company_bank_accounts_created_by ON company_bank_accounts (created_by);
CREATE INDEX ix_company_bank_accounts_updated_by ON company_bank_accounts (updated_by);

CREATE FUNCTION company_bank_accounts_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'company bank account %%: creator and creation time never change', OLD.id USING ERRCODE = '23000';
  END IF;
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  IF NEW.account_enc IS DISTINCT FROM OLD.account_enc OR NEW.ifsc_enc IS DISTINCT FROM OLD.ifsc_enc OR NEW.bank_name_enc IS DISTINCT FROM OLD.bank_name_enc THEN
    RAISE EXCEPTION 'company bank account %%: bank name, account number and IFSC are immutable (create a new account)', OLD.id USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_company_bank_accounts_lock BEFORE UPDATE ON company_bank_accounts FOR EACH ROW EXECUTE FUNCTION company_bank_accounts_lock();
CREATE TRIGGER trg_company_bank_accounts_updated_at BEFORE UPDATE ON company_bank_accounts FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- bank change marker on employee details
ALTER TABLE employee_payroll_details ADD COLUMN bank_changed_by uuid REFERENCES users(id) ON DELETE RESTRICT;
ALTER TABLE employee_payroll_details ADD COLUMN bank_changed_at timestamptz;
ALTER TABLE employee_payroll_details ADD CONSTRAINT ck_employee_payroll_details_bank_changed CHECK ((bank_changed_by IS NULL) = (bank_changed_at IS NULL));
CREATE INDEX ix_employee_payroll_details_bank_changed_by ON employee_payroll_details (bank_changed_by);

CREATE FUNCTION employee_payroll_details_bank_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.bank_changed_at IS NOT NULL AND (NEW.bank_changed_at IS NULL OR NEW.bank_changed_by IS NULL OR NEW.bank_changed_at < OLD.bank_changed_at) THEN
    RAISE EXCEPTION 'employee payroll details %%: the bank change marker can never be cleared or moved back', OLD.user_id USING ERRCODE = '23000';
  END IF;
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  IF (NEW.payment_mode IS DISTINCT FROM OLD.payment_mode OR NEW.bank_name_enc IS DISTINCT FROM OLD.bank_name_enc
      OR NEW.bank_account_enc IS DISTINCT FROM OLD.bank_account_enc OR NEW.bank_ifsc_enc IS DISTINCT FROM OLD.bank_ifsc_enc)
     AND (NEW.bank_changed_at IS NULL OR NEW.bank_changed_by IS NULL OR NEW.bank_changed_at < now() - INTERVAL '5 minutes') THEN
    RAISE EXCEPTION 'employee payroll details %%: a bank detail change must set bank_changed_by / bank_changed_at', OLD.user_id USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_employee_payroll_details_bank_guard BEFORE UPDATE ON employee_payroll_details FOR EACH ROW EXECUTE FUNCTION employee_payroll_details_bank_guard();

-- ---------------------------------------------------------------- publish requests
CREATE TABLE payroll_publish_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES payroll_runs(id) ON DELETE RESTRICT,
  scope text NOT NULL,
  payslip_id uuid REFERENCES payslips(id) ON DELETE RESTRICT,
  status text NOT NULL DEFAULT 'submitted',
  snapshot_enc bytea NOT NULL,
  snapshot_mac text NOT NULL,
  stale_reason text,
  comment_enc bytea,
  requested_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  requested_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  makers text[] NOT NULL DEFAULT '{}',
  decided_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  decided_at timestamptz,
  decision_comment_enc bytea,
  withdrawn_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  withdrawn_at timestamptz,
  version integer NOT NULL DEFAULT 1,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_payroll_publish_requests_scope CHECK (scope IN ('run','payslip')),
  CONSTRAINT ck_payroll_publish_requests_scope_payslip CHECK ((scope = 'payslip') = (payslip_id IS NOT NULL)),
  CONSTRAINT ck_payroll_publish_requests_status CHECK (status IN ('submitted','approved','rejected','withdrawn','stale')),
  CONSTRAINT ck_payroll_publish_requests_stale_reason CHECK (stale_reason IS NULL OR stale_reason IN ('RUN_REOPENED','RUN_REFRESHED','PAYSLIP_CHANGED','EXPIRED')),
  CONSTRAINT ck_payroll_publish_requests_stale_pair CHECK ((status = 'stale') = (stale_reason IS NOT NULL)),
  CONSTRAINT ck_payroll_publish_requests_mac CHECK (snapshot_mac ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_payroll_publish_requests_expiry CHECK (expires_at > requested_at),
  CONSTRAINT ck_payroll_publish_requests_decided_pair CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
  CONSTRAINT ck_payroll_publish_requests_decided_status CHECK ((status IN ('approved','rejected')) = (decided_by IS NOT NULL)),
  CONSTRAINT ck_payroll_publish_requests_withdrawn CHECK ((status = 'withdrawn') = (withdrawn_by IS NOT NULL) AND (withdrawn_by IS NULL) = (withdrawn_at IS NULL)),
  CONSTRAINT ck_payroll_publish_requests_rejected_comment CHECK (status <> 'rejected' OR decision_comment_enc IS NOT NULL),
  CONSTRAINT ck_payroll_publish_requests_checker CHECK (decided_by IS NULL OR decided_by <> requested_by),
  CONSTRAINT ck_payroll_publish_requests_checker_history CHECK (decided_by IS NULL OR decided_by::text <> ALL (makers)),
  CONSTRAINT ck_payroll_publish_requests_requester_in_makers CHECK (decided_by IS NULL OR requested_by::text = ANY (makers)),
  CONSTRAINT ck_payroll_publish_requests_version CHECK (version >= 1)
);
CREATE UNIQUE INDEX uq_payroll_publish_requests_open_run ON payroll_publish_requests (run_id) WHERE scope = 'run' AND status = 'submitted';
CREATE UNIQUE INDEX uq_payroll_publish_requests_open_payslip ON payroll_publish_requests (payslip_id) WHERE scope = 'payslip' AND status = 'submitted';
CREATE INDEX ix_payroll_publish_requests_run_status ON payroll_publish_requests (run_id, status);
CREATE INDEX ix_payroll_publish_requests_status_requested ON payroll_publish_requests (status, requested_at);
CREATE INDEX ix_payroll_publish_requests_payslip_id ON payroll_publish_requests (payslip_id);
CREATE INDEX ix_payroll_publish_requests_requested_by ON payroll_publish_requests (requested_by);
CREATE INDEX ix_payroll_publish_requests_decided_by ON payroll_publish_requests (decided_by);
CREATE INDEX ix_payroll_publish_requests_withdrawn_by ON payroll_publish_requests (withdrawn_by);
CREATE INDEX ix_payroll_publish_requests_updated_by ON payroll_publish_requests (updated_by);

CREATE FUNCTION payroll_publish_requests_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  j_new jsonb;
  j_old jsonb;
  allowed text[] := ARRAY['version','updated_by','updated_at'];
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'submitted' OR NEW.decided_by IS NOT NULL OR NEW.withdrawn_by IS NOT NULL OR NEW.stale_reason IS NOT NULL OR cardinality(NEW.makers) <> 0 THEN
      RAISE EXCEPTION 'a publish request must be created as a plain submitted request' USING ERRCODE = '23000';
    END IF;
    IF NEW.scope = 'run' AND NOT EXISTS (SELECT 1 FROM payroll_runs r WHERE r.id = NEW.run_id AND r.status = 'processed') THEN
      RAISE EXCEPTION 'publish request: the run must be processed' USING ERRCODE = '23000';
    END IF;
    IF NEW.scope = 'payslip' AND NOT EXISTS (SELECT 1 FROM payroll_runs r JOIN payslips p ON p.run_id = r.id WHERE r.id = NEW.run_id AND r.status = 'published' AND p.id = NEW.payslip_id AND p.status = 'processed') THEN
      RAISE EXCEPTION 'publish request: the payslip must be processed in a published run' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'publish request %% cannot be deleted', OLD.id USING ERRCODE = '23000';
  END IF;
  IF NOT (NEW.makers @> OLD.makers) THEN
    RAISE EXCEPTION 'publish request %%: makers can only grow', OLD.id USING ERRCODE = '23000';
  END IF;
  -- app.key_rotation is a plain session setting: rotation runs as the application role, so a role check is not possible
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  IF NEW.status <> OLD.status AND (OLD.status || '>' || NEW.status) NOT IN ('submitted>approved','submitted>rejected','submitted>withdrawn','submitted>stale') THEN
    RAISE EXCEPTION 'publish request %%: %% to %% is not a valid transition', OLD.id, OLD.status, NEW.status USING ERRCODE = '23000';
  END IF;
  IF OLD.status = 'submitted' AND NEW.status IN ('approved','rejected') THEN
    allowed := allowed || ARRAY['status','decided_by','decided_at','decision_comment_enc','makers'];
  ELSIF OLD.status = 'submitted' AND NEW.status = 'withdrawn' THEN
    allowed := allowed || ARRAY['status','withdrawn_by','withdrawn_at'];
  ELSIF OLD.status = 'submitted' AND NEW.status = 'stale' THEN
    allowed := allowed || ARRAY['status','stale_reason'];
  END IF;
  j_new := to_jsonb(NEW) - allowed;
  j_old := to_jsonb(OLD) - allowed;
  IF j_new IS DISTINCT FROM j_old THEN
    RAISE EXCEPTION 'publish request %% is %% and locked', OLD.id, OLD.status USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payroll_publish_requests_lock BEFORE INSERT OR UPDATE OR DELETE ON payroll_publish_requests FOR EACH ROW EXECUTE FUNCTION payroll_publish_requests_lock();
CREATE TRIGGER trg_payroll_publish_requests_updated_at BEFORE UPDATE ON payroll_publish_requests FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION payroll_publish_requests_tagged() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.payslip_id IS NOT NULL AND NEW.run_id IS DISTINCT FROM (SELECT p.run_id FROM payslips p WHERE p.id = NEW.payslip_id) THEN
    RAISE EXCEPTION 'publish request %%: the payslip does not belong to the run', NEW.id USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payroll_publish_requests_tagged BEFORE INSERT OR UPDATE OF run_id, payslip_id ON payroll_publish_requests FOR EACH ROW EXECUTE FUNCTION payroll_publish_requests_tagged();

CREATE FUNCTION payroll_publish_requests_checker_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.decided_by IS NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.scope = 'run' THEN
    IF EXISTS (SELECT 1 FROM payroll_runs r WHERE r.id = NEW.run_id AND NEW.decided_by::text = ANY (r.contributors)) THEN
      RAISE EXCEPTION 'publish request %%: the decider contributed to the run', NEW.id USING ERRCODE = '23000';
    END IF;
    IF EXISTS (SELECT 1 FROM payslips p WHERE p.run_id = NEW.run_id AND p.status IN ('processed','published') AND NOT p.hold AND NEW.decided_by::text = ANY (p.contributors)) THEN
      RAISE EXCEPTION 'publish request %%: the decider contributed to a payslip of the run', NEW.id USING ERRCODE = '23000';
    END IF;
  ELSE
    IF EXISTS (SELECT 1 FROM payslips p WHERE p.id = NEW.payslip_id AND (p.user_id = NEW.decided_by OR NEW.decided_by::text = ANY (p.contributors))) THEN
      RAISE EXCEPTION 'publish request %%: the decider is the employee or contributed to the payslip', NEW.id USING ERRCODE = '23000';
    END IF;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payroll_publish_requests_checker BEFORE INSERT OR UPDATE OF decided_by ON payroll_publish_requests FOR EACH ROW EXECUTE FUNCTION payroll_publish_requests_checker_guard();

-- ---------------------------------------------------------------- payslip delivery
CREATE TABLE payslip_deliveries (
  payslip_id uuid PRIMARY KEY REFERENCES payslips(id) ON DELETE RESTRICT,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  pushed_at timestamptz,
  push_count smallint NOT NULL DEFAULT 0,
  reminders smallint NOT NULL DEFAULT 0,
  last_reminded_at timestamptz,
  first_viewed_at timestamptz,
  first_downloaded_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_payslip_deliveries_push_count CHECK (push_count BETWEEN 0 AND 10),
  CONSTRAINT ck_payslip_deliveries_reminders CHECK (reminders BETWEEN 0 AND 3),
  CONSTRAINT ck_payslip_deliveries_reminded CHECK ((reminders = 0) = (last_reminded_at IS NULL))
);
CREATE INDEX ix_payslip_deliveries_user_id ON payslip_deliveries (user_id);
CREATE INDEX ix_payslip_deliveries_viewed ON payslip_deliveries (first_viewed_at);

CREATE FUNCTION payslip_deliveries_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.user_id IS DISTINCT FROM (SELECT p.user_id FROM payslips p WHERE p.id = NEW.payslip_id) THEN
      RAISE EXCEPTION 'payslip delivery %%: the user must be the payslip employee', NEW.payslip_id USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.payslip_id IS DISTINCT FROM OLD.payslip_id OR NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'payslip delivery %%: payslip, employee and creation time never change', OLD.payslip_id USING ERRCODE = '23000';
  END IF;
  IF (OLD.pushed_at IS NOT NULL AND NEW.pushed_at IS DISTINCT FROM OLD.pushed_at)
     OR (OLD.first_viewed_at IS NOT NULL AND NEW.first_viewed_at IS DISTINCT FROM OLD.first_viewed_at)
     OR (OLD.first_downloaded_at IS NOT NULL AND NEW.first_downloaded_at IS DISTINCT FROM OLD.first_downloaded_at) THEN
    RAISE EXCEPTION 'payslip delivery %%: pushed / first viewed / first downloaded are write-once', OLD.payslip_id USING ERRCODE = '23000';
  END IF;
  IF NEW.push_count < OLD.push_count OR NEW.reminders < OLD.reminders
     OR (OLD.last_reminded_at IS NOT NULL AND (NEW.last_reminded_at IS NULL OR NEW.last_reminded_at < OLD.last_reminded_at)) THEN
    RAISE EXCEPTION 'payslip delivery %%: counters and the reminder time never go back', OLD.payslip_id USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payslip_deliveries_lock BEFORE INSERT OR UPDATE ON payslip_deliveries FOR EACH ROW EXECUTE FUNCTION payslip_deliveries_lock();
CREATE TRIGGER trg_payslip_deliveries_updated_at BEFORE UPDATE ON payslip_deliveries FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- payment batches
CREATE TABLE payroll_payment_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES payroll_runs(id) ON DELETE RESTRICT,
  reference text NOT NULL,
  debit_account_id uuid NOT NULL REFERENCES company_bank_accounts(id) ON DELETE RESTRICT,
  value_date date NOT NULL,
  format text NOT NULL DEFAULT 'generic_neft_csv',
  payment_type text NOT NULL DEFAULT 'NEFT',
  status text NOT NULL DEFAULT 'draft',
  lines_mac text,
  file_sha256 text,
  exported_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  exported_at timestamptz,
  created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  last_edited_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  contributors text[] NOT NULL DEFAULT '{}',
  submitted_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  submitted_at timestamptz,
  decided_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  decided_at timestamptz,
  decision_comment_enc bytea,
  cancel_reason_enc bytea,
  version integer NOT NULL DEFAULT 1,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_payroll_payment_batches_reference UNIQUE (reference),
  CONSTRAINT ck_payroll_payment_batches_reference CHECK (reference ~ '^PAY-[0-9]{4}-[0-9]{2}-[0-9]{2,4}$'),
  CONSTRAINT ck_payroll_payment_batches_format CHECK (format IN ('generic_neft_csv')),
  CONSTRAINT ck_payroll_payment_batches_payment_type CHECK (payment_type IN ('NEFT','RTGS','IMPS')),
  CONSTRAINT ck_payroll_payment_batches_status CHECK (status IN ('draft','submitted','approved','exported','closed','cancelled')),
  CONSTRAINT ck_payroll_payment_batches_value_date CHECK (value_date BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_payroll_payment_batches_sha256 CHECK (file_sha256 IS NULL OR file_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_payroll_payment_batches_lines_mac CHECK (lines_mac IS NULL OR lines_mac ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_payroll_payment_batches_draft_mac CHECK (status <> 'draft' OR lines_mac IS NULL),
  CONSTRAINT ck_payroll_payment_batches_live_mac CHECK (status NOT IN ('submitted','approved','exported','closed') OR lines_mac IS NOT NULL),
  CONSTRAINT ck_payroll_payment_batches_submitted CHECK ((submitted_by IS NULL) = (submitted_at IS NULL)),
  CONSTRAINT ck_payroll_payment_batches_live_submitted CHECK (status NOT IN ('submitted','approved','exported','closed') OR submitted_by IS NOT NULL),
  CONSTRAINT ck_payroll_payment_batches_decided_pair CHECK ((decided_by IS NULL) = (decided_at IS NULL)),
  CONSTRAINT ck_payroll_payment_batches_decided_status CHECK (status NOT IN ('approved','exported','closed') OR decided_by IS NOT NULL),
  CONSTRAINT ck_payroll_payment_batches_exported_pair CHECK ((exported_by IS NULL) = (exported_at IS NULL)),
  CONSTRAINT ck_payroll_payment_batches_exported CHECK ((status IN ('exported','closed')) = (exported_by IS NOT NULL)),
  CONSTRAINT ck_payroll_payment_batches_file CHECK ((status IN ('exported','closed')) = (file_sha256 IS NOT NULL)),
  CONSTRAINT ck_payroll_payment_batches_cancelled CHECK (status <> 'cancelled' OR cancel_reason_enc IS NOT NULL),
  CONSTRAINT ck_payroll_payment_batches_contributors CHECK (created_by::text = ANY (contributors) AND last_edited_by::text = ANY (contributors) AND (submitted_by IS NULL OR submitted_by::text = ANY (contributors))),
  CONSTRAINT ck_payroll_payment_batches_checker CHECK (decided_by IS NULL OR (decided_by IS DISTINCT FROM created_by AND decided_by IS DISTINCT FROM last_edited_by AND decided_by IS DISTINCT FROM submitted_by)),
  CONSTRAINT ck_payroll_payment_batches_checker_history CHECK (decided_by IS NULL OR decided_by::text <> ALL (contributors)),
  CONSTRAINT ck_payroll_payment_batches_version CHECK (version >= 1)
);
CREATE INDEX ix_payroll_payment_batches_run_status ON payroll_payment_batches (run_id, status);
CREATE INDEX ix_payroll_payment_batches_status_created ON payroll_payment_batches (status, created_at);
CREATE INDEX ix_payroll_payment_batches_debit_account_id ON payroll_payment_batches (debit_account_id);
CREATE INDEX ix_payroll_payment_batches_created_by ON payroll_payment_batches (created_by);
CREATE INDEX ix_payroll_payment_batches_last_edited_by ON payroll_payment_batches (last_edited_by);
CREATE INDEX ix_payroll_payment_batches_submitted_by ON payroll_payment_batches (submitted_by);
CREATE INDEX ix_payroll_payment_batches_decided_by ON payroll_payment_batches (decided_by);
CREATE INDEX ix_payroll_payment_batches_exported_by ON payroll_payment_batches (exported_by);
CREATE INDEX ix_payroll_payment_batches_updated_by ON payroll_payment_batches (updated_by);

CREATE TABLE payroll_payment_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id uuid NOT NULL REFERENCES payroll_payment_batches(id) ON DELETE RESTRICT,
  payslip_id uuid NOT NULL REFERENCES payslips(id) ON DELETE RESTRICT,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  seq integer NOT NULL,
  amount_enc bytea NOT NULL,
  beneficiary_enc bytea NOT NULL,
  account_source text NOT NULL DEFAULT 'snapshot',
  flags text[] NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'pending',
  reference_enc bytea,
  paid_on date,
  version integer NOT NULL DEFAULT 1,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_payroll_payment_lines_batch_seq UNIQUE (batch_id, seq),
  CONSTRAINT ck_payroll_payment_lines_seq CHECK (seq BETWEEN 1 AND 9999),
  CONSTRAINT ck_payroll_payment_lines_account_source CHECK (account_source IN ('snapshot','current')),
  CONSTRAINT ck_payroll_payment_lines_flags CHECK (flags <@ ARRAY['BANK_DETAILS_CHANGED','RECENT_BANK_CHANGE','REVISION_DELTA','NO_PAN']::text[]),
  CONSTRAINT ck_payroll_payment_lines_source_flag CHECK (account_source = 'snapshot' OR 'BANK_DETAILS_CHANGED' = ANY (flags)),
  CONSTRAINT ck_payroll_payment_lines_status CHECK (status IN ('pending','excluded','exported','paid','failed')),
  CONSTRAINT ck_payroll_payment_lines_confirmation CHECK ((reference_enc IS NULL AND paid_on IS NULL) OR status IN ('paid','failed')),
  CONSTRAINT ck_payroll_payment_lines_paid_on CHECK (status <> 'paid' OR paid_on IS NOT NULL),
  CONSTRAINT ck_payroll_payment_lines_paid_year CHECK (paid_on IS NULL OR paid_on BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_payroll_payment_lines_version CHECK (version >= 1)
);
CREATE UNIQUE INDEX uq_payroll_payment_lines_live ON payroll_payment_lines (payslip_id) WHERE status IN ('pending','exported','paid');
CREATE INDEX ix_payroll_payment_lines_payslip_id ON payroll_payment_lines (payslip_id);
CREATE INDEX ix_payroll_payment_lines_user_status ON payroll_payment_lines (user_id, status);
CREATE INDEX ix_payroll_payment_lines_batch_status ON payroll_payment_lines (batch_id, status);
CREATE INDEX ix_payroll_payment_lines_updated_by ON payroll_payment_lines (updated_by);

CREATE FUNCTION payroll_payment_batches_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  j_new jsonb;
  j_old jsonb;
  allowed text[] := ARRAY['version','updated_by','updated_at'];
  alert_days integer;
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'draft' THEN
      RAISE EXCEPTION 'a payment batch must be created as draft' USING ERRCODE = '23000';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM payroll_runs r WHERE r.id = NEW.run_id AND r.status = 'published') THEN
      RAISE EXCEPTION 'payment batch %%: the run must be published', NEW.id USING ERRCODE = '23000';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM company_bank_accounts a WHERE a.id = NEW.debit_account_id AND a.active) THEN
      RAISE EXCEPTION 'payment batch %%: the debit account must be active', NEW.id USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'payment batch %% is %% and cannot be deleted', OLD.id, OLD.status USING ERRCODE = '23000';
    END IF;
    RETURN OLD;
  END IF;
  IF NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.reference IS DISTINCT FROM OLD.reference OR NEW.created_by IS DISTINCT FROM OLD.created_by
     OR NEW.created_at IS DISTINCT FROM OLD.created_at OR NEW.format IS DISTINCT FROM OLD.format OR NEW.payment_type IS DISTINCT FROM OLD.payment_type THEN
    RAISE EXCEPTION 'payment batch %%: run, reference, creator, format and payment type never change', OLD.id USING ERRCODE = '23000';
  END IF;
  IF NOT (NEW.contributors @> OLD.contributors) THEN
    RAISE EXCEPTION 'payment batch %%: contributors can only grow', OLD.id USING ERRCODE = '23000';
  END IF;
  -- app.key_rotation is a plain session setting: rotation runs as the application role, so a role check is not possible
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  IF NEW.status <> OLD.status AND (OLD.status || '>' || NEW.status) NOT IN
     ('draft>submitted','draft>cancelled','submitted>draft','submitted>approved','submitted>cancelled','approved>exported','approved>cancelled','exported>closed') THEN
    RAISE EXCEPTION 'payment batch %%: %% to %% is not a valid transition', OLD.id, OLD.status, NEW.status USING ERRCODE = '23000';
  END IF;
  IF OLD.status IN ('closed','cancelled') THEN
    NULL;  -- immutable (version / updated_* only)
  ELSIF OLD.status = 'draft' AND NEW.status = 'draft' THEN
    allowed := allowed || ARRAY['value_date','debit_account_id','last_edited_by','contributors'];
  ELSIF OLD.status = 'draft' AND NEW.status = 'submitted' THEN
    IF NEW.decided_by IS NOT NULL THEN
      RAISE EXCEPTION 'payment batch %%: submitting clears an earlier decision', OLD.id USING ERRCODE = '23000';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM payroll_payment_lines l WHERE l.batch_id = OLD.id AND l.status = 'pending') THEN
      RAISE EXCEPTION 'payment batch %%: there is no pending line to submit', OLD.id USING ERRCODE = '23000';
    END IF;
    allowed := allowed || ARRAY['status','submitted_by','submitted_at','lines_mac','contributors','decided_by','decided_at','decision_comment_enc'];
  ELSIF OLD.status = 'submitted' AND NEW.status = 'draft' THEN
    allowed := allowed || ARRAY['status','lines_mac','contributors','decided_by','decided_at','decision_comment_enc'];
  ELSIF OLD.status = 'submitted' AND NEW.status = 'approved' THEN
    SELECT COALESCE((SELECT (s.value #>> '{}')::integer FROM settings s WHERE s.key = 'payroll.bank_change_alert_days'), 30) INTO alert_days;
    IF EXISTS (SELECT 1 FROM payroll_payment_lines l WHERE l.batch_id = OLD.id AND l.status <> 'excluded' AND l.user_id = NEW.decided_by) THEN
      RAISE EXCEPTION 'payment batch %%: the approver is paid by a line of the batch', OLD.id USING ERRCODE = '23000';
    END IF;
    IF EXISTS (SELECT 1 FROM payroll_payment_lines l JOIN employee_payroll_details d ON d.user_id = l.user_id
               WHERE l.batch_id = OLD.id AND l.status <> 'excluded' AND d.bank_changed_by = NEW.decided_by
                 AND d.bank_changed_at > now() - make_interval(days => alert_days)) THEN
      RAISE EXCEPTION 'payment batch %%: the approver recently changed the bank details of a paid employee', OLD.id USING ERRCODE = '23000';
    END IF;
    allowed := allowed || ARRAY['status','decided_by','decided_at','decision_comment_enc'];
  ELSIF NEW.status = 'cancelled' THEN
    allowed := allowed || ARRAY['status','cancel_reason_enc','contributors'];
  ELSIF OLD.status = 'approved' AND NEW.status = 'exported' THEN
    allowed := allowed || ARRAY['status','exported_by','exported_at','file_sha256'];
  ELSIF OLD.status = 'exported' AND NEW.status = 'closed' THEN
    allowed := allowed || ARRAY['status'];
  END IF;  -- submitted>submitted, approved>approved, exported>exported: nothing but version / updated_*
  IF NEW.debit_account_id IS DISTINCT FROM OLD.debit_account_id
     AND NOT EXISTS (SELECT 1 FROM company_bank_accounts a WHERE a.id = NEW.debit_account_id AND a.active) THEN
    RAISE EXCEPTION 'payment batch %%: the debit account must be active', OLD.id USING ERRCODE = '23000';
  END IF;
  j_new := to_jsonb(NEW) - allowed;
  j_old := to_jsonb(OLD) - allowed;
  IF j_new IS DISTINCT FROM j_old THEN
    RAISE EXCEPTION 'payment batch %% is %% and locked', OLD.id, OLD.status USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payroll_payment_batches_lock BEFORE INSERT OR UPDATE OR DELETE ON payroll_payment_batches FOR EACH ROW EXECUTE FUNCTION payroll_payment_batches_lock();
CREATE TRIGGER trg_payroll_payment_batches_updated_at BEFORE UPDATE ON payroll_payment_batches FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION payroll_payment_batches_consistency() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.status IN ('cancelled','exported','closed') AND EXISTS (SELECT 1 FROM payroll_payment_lines l WHERE l.batch_id = NEW.id AND l.status = 'pending') THEN
    RAISE EXCEPTION 'payment batch %% is %% but has pending lines', NEW.id, NEW.status USING ERRCODE = '23000';
  END IF;
  IF NEW.status = 'closed' AND EXISTS (SELECT 1 FROM payroll_payment_lines l WHERE l.batch_id = NEW.id AND l.status = 'exported') THEN
    RAISE EXCEPTION 'payment batch %% is closed but has unconfirmed lines', NEW.id USING ERRCODE = '23000';
  END IF;
  IF NEW.status IN ('draft','submitted','approved') AND EXISTS (SELECT 1 FROM payroll_payment_lines l WHERE l.batch_id = NEW.id AND l.status IN ('exported','paid','failed')) THEN
    RAISE EXCEPTION 'payment batch %% is %% but has exported lines', NEW.id, NEW.status USING ERRCODE = '23000';
  END IF;
  RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER trg_payroll_payment_batches_consistency AFTER INSERT OR UPDATE ON payroll_payment_batches DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION payroll_payment_batches_consistency();

CREATE FUNCTION payroll_payment_lines_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  b_status text;
  b_run uuid;
  ps record;
  j_new jsonb;
  j_old jsonb;
  allowed text[] := ARRAY['version','updated_by','updated_at'];
BEGIN
  IF TG_OP = 'INSERT' THEN
    SELECT b.status, b.run_id INTO b_status, b_run FROM payroll_payment_batches b WHERE b.id = NEW.batch_id;
    IF b_status IS DISTINCT FROM 'draft' THEN
      RAISE EXCEPTION 'payment line: lines can only be added to a draft batch' USING ERRCODE = '23000';
    END IF;
    IF NEW.status NOT IN ('pending','excluded') THEN
      RAISE EXCEPTION 'payment line: a new line is pending or excluded' USING ERRCODE = '23000';
    END IF;
    SELECT p.run_id, p.user_id, p.status, p.hold INTO ps FROM payslips p WHERE p.id = NEW.payslip_id;
    IF NOT FOUND OR ps.run_id IS DISTINCT FROM b_run THEN
      RAISE EXCEPTION 'payment line: the payslip does not belong to the run of the batch' USING ERRCODE = '23000';
    END IF;
    IF ps.user_id IS DISTINCT FROM NEW.user_id THEN
      RAISE EXCEPTION 'payment line: the line employee must be the payslip employee' USING ERRCODE = '23000';
    END IF;
    IF ps.status <> 'published' OR ps.hold THEN
      RAISE EXCEPTION 'payment line: only a published, non-held payslip can be paid' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
  END IF;
  SELECT b.status INTO b_status FROM payroll_payment_batches b WHERE b.id = OLD.batch_id;
  IF TG_OP = 'DELETE' THEN
    IF b_status IS DISTINCT FROM 'draft' THEN
      RAISE EXCEPTION 'payment line: lines of a non-draft batch cannot be deleted' USING ERRCODE = '23000';
    END IF;
    RETURN OLD;
  END IF;
  IF NEW.batch_id IS DISTINCT FROM OLD.batch_id OR NEW.payslip_id IS DISTINCT FROM OLD.payslip_id OR NEW.user_id IS DISTINCT FROM OLD.user_id
     OR NEW.seq IS DISTINCT FROM OLD.seq OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'payment line %%: batch, payslip, employee and sequence never change', OLD.id USING ERRCODE = '23000';
  END IF;
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  IF NEW.status <> OLD.status AND (OLD.status || '>' || NEW.status) NOT IN ('pending>excluded','excluded>pending','pending>exported','exported>paid','exported>failed') THEN
    RAISE EXCEPTION 'payment line %%: %% to %% is not a valid transition', OLD.id, OLD.status, NEW.status USING ERRCODE = '23000';
  END IF;
  IF b_status = 'draft' THEN
    IF NEW.status NOT IN ('pending','excluded') THEN
      RAISE EXCEPTION 'payment line %%: a draft batch has pending or excluded lines only', OLD.id USING ERRCODE = '23000';
    END IF;
    allowed := allowed || ARRAY['status','amount_enc','beneficiary_enc','flags','account_source'];
  ELSIF b_status = 'cancelled' AND OLD.status = 'pending' AND NEW.status = 'excluded' THEN
    allowed := allowed || ARRAY['status'];
  ELSIF b_status IN ('approved','exported') AND OLD.status = 'pending' AND NEW.status = 'exported' THEN
    allowed := allowed || ARRAY['status'];
  ELSIF b_status IN ('exported','closed') AND OLD.status = 'exported' AND NEW.status IN ('paid','failed') THEN
    allowed := allowed || ARRAY['status','reference_enc','paid_on'];
  END IF;
  j_new := to_jsonb(NEW) - allowed;
  j_old := to_jsonb(OLD) - allowed;
  IF j_new IS DISTINCT FROM j_old THEN
    RAISE EXCEPTION 'payment line %% is %% and its batch is %%: locked', OLD.id, OLD.status, b_status USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payroll_payment_lines_lock BEFORE INSERT OR UPDATE OR DELETE ON payroll_payment_lines FOR EACH ROW EXECUTE FUNCTION payroll_payment_lines_lock();
CREATE TRIGGER trg_payroll_payment_lines_updated_at BEFORE UPDATE ON payroll_payment_lines FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- money history events
ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_item_type;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_item_type CHECK (item_type IN ("""
    + _EVENT_TYPES_NEW
    + r"""));
ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_action;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_action CHECK (action IN ("""
    + _EVENT_ACTIONS_NEW
    + r"""));

-- ---------------------------------------------------------------- settings
INSERT INTO settings (key, value) VALUES ('payroll.publish_request_ttl_hours', '72'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.bank_file_requires_second_person', 'true'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.bank_file_format', '"generic_neft_csv"'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.bank_payment_type', '"NEFT"'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.bank_change_alert_days', '30'::jsonb) ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  REVOKE DELETE, TRUNCATE ON payroll_publish_requests FROM app_user;
  REVOKE DELETE, TRUNCATE ON payslip_deliveries FROM app_user;
  REVOKE DELETE, TRUNCATE ON company_bank_accounts FROM app_user;
  REVOKE DELETE, TRUNCATE ON payroll_payment_batches FROM app_user;
  REVOKE DELETE, TRUNCATE ON payroll_payment_lines FROM app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'revoke on the 0026 tables skipped';
END
$$;
"""
)

DOWNGRADE = (
    r"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM payroll_publish_requests WHERE status IN ('submitted','approved'))
     OR EXISTS (SELECT 1 FROM payroll_payment_batches WHERE status NOT IN ('draft','cancelled')) THEN
    RAISE EXCEPTION 'cannot downgrade 0026: submitted / approved publish requests or submitted / approved / exported / closed payment batches exist (cancel or withdraw them first)';
  END IF;
END
$$;

DELETE FROM settings WHERE key IN ('payroll.publish_request_ttl_hours', 'payroll.bank_file_requires_second_person', 'payroll.bank_file_format', 'payroll.bank_payment_type', 'payroll.bank_change_alert_days');

ALTER TABLE payroll_item_events DISABLE TRIGGER trg_payroll_item_events_no_update_delete;
DELETE FROM payroll_item_events WHERE item_type IN ('publish_request','payment_batch') OR action IN ('export','confirm','expire','stale');
ALTER TABLE payroll_item_events ENABLE TRIGGER trg_payroll_item_events_no_update_delete;
ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_action;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_action CHECK (action IN ("""
    + _EVENT_ACTIONS_OLD
    + r"""));
ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_item_type;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_item_type CHECK (item_type IN ("""
    + _EVENT_TYPES_OLD
    + r"""));

DROP TABLE IF EXISTS payroll_payment_lines;
DROP TABLE IF EXISTS payroll_payment_batches;
DROP FUNCTION IF EXISTS payroll_payment_lines_lock();
DROP FUNCTION IF EXISTS payroll_payment_batches_consistency();
DROP FUNCTION IF EXISTS payroll_payment_batches_lock();
DROP TABLE IF EXISTS payslip_deliveries;
DROP FUNCTION IF EXISTS payslip_deliveries_lock();
DROP TABLE IF EXISTS payroll_publish_requests;
DROP FUNCTION IF EXISTS payroll_publish_requests_checker_guard();
DROP FUNCTION IF EXISTS payroll_publish_requests_tagged();
DROP FUNCTION IF EXISTS payroll_publish_requests_lock();
DROP TABLE IF EXISTS company_bank_accounts;
DROP FUNCTION IF EXISTS company_bank_accounts_lock();

DROP TRIGGER IF EXISTS trg_employee_payroll_details_bank_guard ON employee_payroll_details;
DROP FUNCTION IF EXISTS employee_payroll_details_bank_guard();
DROP INDEX IF EXISTS ix_employee_payroll_details_bank_changed_by;
ALTER TABLE employee_payroll_details DROP CONSTRAINT IF EXISTS ck_employee_payroll_details_bank_changed;
ALTER TABLE employee_payroll_details DROP COLUMN IF EXISTS bank_changed_at;
ALTER TABLE employee_payroll_details DROP COLUMN IF EXISTS bank_changed_by;

DROP TRIGGER IF EXISTS trg_payslips_contributors ON payslips;
DROP TRIGGER IF EXISTS trg_payroll_runs_contributors ON payroll_runs;
DROP FUNCTION IF EXISTS payroll_contributors_guard();
ALTER TABLE payslips DROP COLUMN IF EXISTS contributors;
ALTER TABLE payroll_runs DROP COLUMN IF EXISTS contributors;
"""
)


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
