"""Tax year rules, declarations, proofs and Form 16 documents (plan 17 work package 5b)

Revision ID: 0027
Revises: 0026

Plan: docs/plans/17-payroll-phase5.md sections 2.2 and 7 (5b); contract: docs/api-contract.md section 19 (19.6 to 19.8, 19.10).

Added
* tax_year_rules          per-FY, versioned tax rules held as DATA (jsonb, not secret). draft -> submitted -> verified -> superseded.
                          MAKER-CHECKER IS ENFORCED BY THE DATABASE: `verified_by` differs from the creator, the last editor and the
                          submitter and is not in the append-only `contributors` list (ck_tax_year_rules_verifier); a verified / superseded row
                          needs submitter, verifier and an attestation of 20..500 characters. tax_year_rules_lock(): inserts are plain drafts,
                          only draft>submitted, submitted>draft (withdraw / reject), submitted>verified and verified>superseded exist,
                          a draft can only change its rules / sha / editor, verified and superseded rows are immutable except `windows`
                          (declaration / proof dates are not tax rules), a delete is refused unless the row is a draft, and a rules document whose
                          rules.note still starts with UNVERIFIED cannot be verified. Unique (fy, version); ONE verified row per FY
                          (partial unique) and ONE open (draft / submitted) row per FY (partial unique). The python code holds no slab, rate or
                          limit: the engine reads the verified row only.
                          Seed: FY 2026-27 version 1 `draft`, rules.note = "UNVERIFIED seed ...", created_by NULL (system). `windows` is seeded
                          empty ({}), i.e. windows closed until HR sets them. rules_sha256 = sha256 of the canonical JSON
                          json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=False).
* tax_declarations        one row per (user, FY). `draft_enc` / `regime_draft` are what the employee is editing; `submitted_enc` +
                          `submitted_version` + `regime` + `submitted_at` are FROZEN at submit and only change together, with a strictly higher
                          submitted_version (T3: a draft never feeds tax). `entered_by` = HR on behalf; it is set once and never changes.
                          Rows are never deleted. Key rotation passes (only *_enc columns change).
* tax_proofs              proof of one declaration item. submitted -> verified | rejected | cancelled. `verified_by` (the decider, also for a
                          rejection) differs from the employee and from the declaration's `entered_by` (trigger). user / FY equal the
                          declaration's; every file in `file_ids` (1..10, no duplicates, no NULLs; the 5 default is the setting
                          payroll.tax_proof_max_files) is a `tax_proof` file owned by the employee or by whoever entered the declaration.
                          While `submitted` only claimed_enc / file_ids may change; final rows are immutable. One live (submitted / verified)
                          proof per declaration item (partial unique).
* form16_documents       uploaded Form 16 / Part A / Part B / Form 12BA PDFs (`files`, purpose form16, RESTRICT: never purged while referenced).
                          uploaded -> published | withdrawn, published -> withdrawn. `second_person` is fixed at upload (setting
                          payroll.form16_requires_second_person): when true, published_by differs from uploaded_by; published_by is never the
                          employee. One live (uploaded / published) document per (user, FY, kind) (partial unique); one document per file.
* files.purpose           ck_files_purpose accepts 'tax_proof' and 'form16'.
* payroll_item_events     item_type gains `tax_rules`, `tax_declaration`, `tax_proof`, `form16`; action gains `unlock`
                          (`verify` and `withdraw` already exist).
* settings                payroll.form16_requires_second_person (true), payroll.tax_proof_max_files (5). The 5c settings are not seeded here.
* REVOKE DELETE, TRUNCATE FROM app_user on the four tables. No foreign key is ON DELETE SET NULL, so no pg_trigger_depth() escape is needed.

Downgrade REFUSES while ANY declaration, proof or Form 16 document exists, or any tax_year_rules row other than the untouched seed
(FY 2026-27, version 1, draft, created_by and last_edited_by NULL). Otherwise it drops the tables, triggers and functions, deletes the (then
orphaned) tax_proof / form16 files rows and the new history events (append-only trigger disabled for that statement), restores the CHECKs
and removes the two settings.
"""

import hashlib
import json

from migration_sql import run

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_SETTINGS = ("payroll.form16_requires_second_person", "payroll.tax_proof_max_files")

_FILE_PURPOSES_OLD = "'selfie','leave_attachment','announcement_image','announcement_pdf','profile_photo','company_logo','reimbursement_bill'"
_FILE_PURPOSES_NEW = _FILE_PURPOSES_OLD + ",'tax_proof','form16'"
_EVENT_TYPES_OLD = (
    "'one_time','loan','loan_installment','arrears','encashment','reimbursement','overtime','increment','fnf','publish_request','payment_batch'"
)
_EVENT_TYPES_NEW = _EVENT_TYPES_OLD + ",'tax_rules','tax_declaration','tax_proof','form16'"
_EVENT_ACTIONS_OLD = (
    "'create','update','cancel','skip','reschedule','foreclose','close','propose','accept','dismiss','verify','approve','reject',"
    "'process','reopen','pay','carry','submit','withdraw','settle','revoke','remove','export','confirm','expire','stale'"
)
_EVENT_ACTIONS_NEW = _EVENT_ACTIONS_OLD + ",'unlock'"

# UNVERIFIED seed (plan 17 Q-L1): FY 2025-26 values as known when written, labelled for the Income-tax Act, 2025. Nothing uses it until a
# second person verifies it; the code contains no tax constant.
_SEED_RULES = {
    "note": "UNVERIFIED seed copied from FY 2025-26; verify against the Finance Act 2026 and the Income-tax Act, 2025",
    "labels": {"act": "Income-tax Act, 2025", "tds_section": "Section 192", "form16": "Form 16", "return": "Form 24Q"},
    "default_regime": "new",
    "regimes": {
        "new": {
            "slabs": [
                {"from": "0.00", "to": "400000.00", "rate_pct": "0"},
                {"from": "400000.00", "to": "800000.00", "rate_pct": "5"},
                {"from": "800000.00", "to": "1200000.00", "rate_pct": "10"},
                {"from": "1200000.00", "to": "1600000.00", "rate_pct": "15"},
                {"from": "1600000.00", "to": "2000000.00", "rate_pct": "20"},
                {"from": "2000000.00", "to": "2400000.00", "rate_pct": "25"},
                {"from": "2400000.00", "to": None, "rate_pct": "30"},
            ],
            "standard_deduction": "75000.00",
            "rebate": {"income_limit": "1200000.00", "max": "60000.00", "marginal_relief": True},
            "surcharge": [
                {"above": "5000000.00", "rate_pct": "10"},
                {"above": "10000000.00", "rate_pct": "15"},
                {"above": "20000000.00", "rate_pct": "25"},
            ],
            "surcharge_marginal_relief": True,
            "pt_deductible": False,
            "hra_exemption": False,
            "allowed_sections": [],
        },
        "old": {
            "slabs": [
                {"from": "0.00", "to": "250000.00", "rate_pct": "0"},
                {"from": "250000.00", "to": "500000.00", "rate_pct": "5"},
                {"from": "500000.00", "to": "1000000.00", "rate_pct": "20"},
                {"from": "1000000.00", "to": None, "rate_pct": "30"},
            ],
            "standard_deduction": "50000.00",
            "rebate": {"income_limit": "500000.00", "max": "12500.00", "marginal_relief": False},
            "surcharge": [
                {"above": "5000000.00", "rate_pct": "10"},
                {"above": "10000000.00", "rate_pct": "15"},
                {"above": "20000000.00", "rate_pct": "25"},
                {"above": "50000000.00", "rate_pct": "37"},
            ],
            "surcharge_marginal_relief": True,
            "pt_deductible": True,
            "hra_exemption": True,
            "allowed_sections": ["80C", "80CCC", "80CCD1", "80CCD1B", "80D_SELF", "80D_PARENTS", "80E", "80G", "80TTA", "24B", "HRA"],
        },
    },
    "cess_pct": "4",
    "no_pan_min_rate_pct": "20",
    "round_income_to": 10,
    "round_tax_to": 10,
    "loan_perquisite_threshold": "20000.00",
    "sections": [
        {"code": "80C", "name": "Section 80C (PF, PPF, ELSS, life insurance, tuition fees, home loan principal)", "limit": None,
         "group": "80C_GROUP", "proof_required": True, "fields": ["description"]},
        {"code": "80CCD1B", "name": "NPS (additional)", "limit": "50000.00", "group": None, "proof_required": True, "fields": ["description"]},
        {"code": "80D_SELF", "name": "Health insurance - self and family", "limit": "25000.00", "senior_limit": "50000.00", "group": None,
         "proof_required": True, "fields": ["description", "senior"]},
        {"code": "24B", "name": "Home loan interest (self-occupied)", "limit": "200000.00", "group": None, "proof_required": True,
         "fields": ["lender_name", "lender_pan"]},
        {"code": "HRA", "name": "House rent", "limit": None, "group": None, "proof_required": True,
         "fields": ["monthly_rent", "from_month", "to_month", "landlord_name", "landlord_pan", "city", "metro"], "landlord_pan_above": "100000.00"},
        {"code": "PREV_EMPLOYER", "name": "Previous employer this year (Form 12B)", "limit": None, "group": None, "proof_required": True,
         "fields": ["income", "tds", "pt", "pf"]},
        {"code": "OTHER_INCOME", "name": "Other income", "limit": None, "group": None, "proof_required": False, "fields": ["description"]},
    ],
    "group_limits": [{"group": "80C_GROUP", "limit": "150000.00"}],
    "hra": {"metro_pct": "50", "non_metro_pct": "40", "rent_minus_basic_pct": "10", "metro_cities": ["Delhi", "Mumbai", "Kolkata", "Chennai"]},
    "component_treatment": {"default": "taxable", "REIMBURSEMENT": "exempt", "LEAVE_ENCASH": "taxable"},
}
SEED_FY = "2026-27"
SEED_RULES_JSON = json.dumps(_SEED_RULES, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
SEED_RULES_SHA256 = hashlib.sha256(SEED_RULES_JSON.encode("utf-8")).hexdigest()
assert "'" not in SEED_RULES_JSON and "%" not in SEED_RULES_JSON  # safe inside a SQL literal and the DDL layer

UPGRADE = (
    r"""
-- ---------------------------------------------------------------- tax year rules
CREATE TABLE tax_year_rules (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  fy text NOT NULL,
  version integer NOT NULL,
  status text NOT NULL DEFAULT 'draft',
  rules jsonb NOT NULL,
  rules_sha256 text NOT NULL,
  attestation text,
  windows jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  last_edited_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  contributors text[] NOT NULL DEFAULT '{}',
  submitted_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  submitted_at timestamptz,
  verified_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  verified_at timestamptz,
  version_row integer NOT NULL DEFAULT 1,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_tax_year_rules_fy_version UNIQUE (fy, version),
  CONSTRAINT ck_tax_year_rules_fy CHECK (fy ~ '^[0-9]{4}-[0-9]{2}$'),
  CONSTRAINT ck_tax_year_rules_version CHECK (version >= 1),
  CONSTRAINT ck_tax_year_rules_status CHECK (status IN ('draft','submitted','verified','superseded')),
  CONSTRAINT ck_tax_year_rules_rules CHECK (jsonb_typeof(rules) = 'object'),
  CONSTRAINT ck_tax_year_rules_windows CHECK (jsonb_typeof(windows) = 'object'),
  CONSTRAINT ck_tax_year_rules_sha256 CHECK (rules_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_tax_year_rules_attestation CHECK (attestation IS NULL OR char_length(attestation) BETWEEN 20 AND 500),
  CONSTRAINT ck_tax_year_rules_submitted_pair CHECK ((submitted_by IS NULL) = (submitted_at IS NULL)),
  CONSTRAINT ck_tax_year_rules_verified_pair CHECK ((verified_by IS NULL) = (verified_at IS NULL)),
  CONSTRAINT ck_tax_year_rules_verified_status CHECK ((status IN ('verified','superseded')) = (verified_by IS NOT NULL)),
  CONSTRAINT ck_tax_year_rules_attested CHECK (status NOT IN ('verified','superseded') OR attestation IS NOT NULL),
  CONSTRAINT ck_tax_year_rules_submitted_status CHECK (status NOT IN ('submitted','verified','superseded') OR submitted_by IS NOT NULL),
  CONSTRAINT ck_tax_year_rules_contributors CHECK ((created_by IS NULL OR created_by::text = ANY (contributors)) AND (last_edited_by IS NULL OR last_edited_by::text = ANY (contributors)) AND (submitted_by IS NULL OR submitted_by::text = ANY (contributors))),
  CONSTRAINT ck_tax_year_rules_verifier CHECK (verified_by IS NULL OR (verified_by IS DISTINCT FROM created_by AND verified_by IS DISTINCT FROM last_edited_by AND verified_by IS DISTINCT FROM submitted_by AND verified_by::text <> ALL (contributors))),
  CONSTRAINT ck_tax_year_rules_version_row CHECK (version_row >= 1)
);
CREATE UNIQUE INDEX uq_tax_year_rules_verified ON tax_year_rules (fy) WHERE status = 'verified';
CREATE UNIQUE INDEX uq_tax_year_rules_open ON tax_year_rules (fy) WHERE status IN ('draft','submitted');
CREATE INDEX ix_tax_year_rules_status ON tax_year_rules (fy, status);
CREATE INDEX ix_tax_year_rules_created_by ON tax_year_rules (created_by);
CREATE INDEX ix_tax_year_rules_last_edited_by ON tax_year_rules (last_edited_by);
CREATE INDEX ix_tax_year_rules_submitted_by ON tax_year_rules (submitted_by);
CREATE INDEX ix_tax_year_rules_verified_by ON tax_year_rules (verified_by);
CREATE INDEX ix_tax_year_rules_updated_by ON tax_year_rules (updated_by);

CREATE FUNCTION tax_year_rules_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  j_new jsonb;
  j_old jsonb;
  allowed text[] := ARRAY['windows','version_row','updated_by','updated_at'];
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'draft' OR NEW.submitted_by IS NOT NULL OR NEW.verified_by IS NOT NULL OR NEW.attestation IS NOT NULL THEN
      RAISE EXCEPTION 'tax rules must be created as a plain draft' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'tax rules %% (%% v%%) are %% and cannot be deleted', OLD.id, OLD.fy, OLD.version, OLD.status USING ERRCODE = '23000';
    END IF;
    RETURN OLD;
  END IF;
  IF NEW.fy IS DISTINCT FROM OLD.fy OR NEW.version IS DISTINCT FROM OLD.version OR NEW.created_by IS DISTINCT FROM OLD.created_by
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'tax rules %%: FY, version, creator and creation time never change', OLD.id USING ERRCODE = '23000';
  END IF;
  IF NOT (NEW.contributors @> OLD.contributors) THEN
    RAISE EXCEPTION 'tax rules %%: contributors can only grow', OLD.id USING ERRCODE = '23000';
  END IF;
  IF NEW.status <> OLD.status AND (OLD.status || '>' || NEW.status) NOT IN ('draft>submitted','submitted>draft','submitted>verified','verified>superseded') THEN
    RAISE EXCEPTION 'tax rules %%: %% to %% is not a valid transition', OLD.id, OLD.status, NEW.status USING ERRCODE = '23000';
  END IF;
  IF OLD.status = 'draft' AND NEW.status = 'draft' THEN
    allowed := allowed || ARRAY['rules','rules_sha256','last_edited_by','contributors'];
  ELSIF OLD.status = 'draft' AND NEW.status = 'submitted' THEN
    allowed := allowed || ARRAY['status','submitted_by','submitted_at','contributors'];
  ELSIF OLD.status = 'submitted' AND NEW.status = 'draft' THEN
    allowed := allowed || ARRAY['status','submitted_by','submitted_at','contributors'];
  ELSIF OLD.status = 'submitted' AND NEW.status = 'verified' THEN
    IF COALESCE(NEW.rules #>> '{note}', '') ~* '^\s*UNVERIFIED' THEN
      RAISE EXCEPTION 'tax rules %%: the UNVERIFIED seed note must be removed before the rules can be verified', OLD.id USING ERRCODE = '23000';
    END IF;
    allowed := allowed || ARRAY['status','verified_by','verified_at','attestation'];
  ELSIF OLD.status = 'verified' AND NEW.status = 'superseded' THEN
    allowed := allowed || ARRAY['status'];
  END IF;  -- submitted>submitted, verified>verified, superseded>superseded: windows / version_row / updated_* only
  j_new := to_jsonb(NEW) - allowed;
  j_old := to_jsonb(OLD) - allowed;
  IF j_new IS DISTINCT FROM j_old THEN
    RAISE EXCEPTION 'tax rules %% (%% v%%) are %% and locked', OLD.id, OLD.fy, OLD.version, OLD.status USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_tax_year_rules_lock BEFORE INSERT OR UPDATE OR DELETE ON tax_year_rules FOR EACH ROW EXECUTE FUNCTION tax_year_rules_lock();
CREATE TRIGGER trg_tax_year_rules_updated_at BEFORE UPDATE ON tax_year_rules FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO tax_year_rules (fy, version, status, rules, rules_sha256) VALUES ('"""
    + SEED_FY
    + r"""', 1, 'draft', '"""
    + SEED_RULES_JSON
    + r"""'::jsonb, '"""
    + SEED_RULES_SHA256
    + r"""') ON CONFLICT (fy, version) DO NOTHING;

-- ---------------------------------------------------------------- tax declarations
CREATE TABLE tax_declarations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  fy text NOT NULL,
  regime_draft text,
  draft_enc bytea,
  regime text,
  submitted_enc bytea,
  submitted_version integer,
  submitted_at timestamptz,
  entered_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  unlocked_until date,
  unlock_reason_enc bytea,
  version integer NOT NULL DEFAULT 1,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_tax_declarations_user_fy UNIQUE (user_id, fy),
  CONSTRAINT ck_tax_declarations_fy CHECK (fy ~ '^[0-9]{4}-[0-9]{2}$'),
  CONSTRAINT ck_tax_declarations_regime_draft CHECK (regime_draft IS NULL OR regime_draft IN ('old','new')),
  CONSTRAINT ck_tax_declarations_regime CHECK (regime IS NULL OR regime IN ('old','new')),
  CONSTRAINT ck_tax_declarations_submitted CHECK ((submitted_enc IS NULL) = (submitted_version IS NULL)),
  CONSTRAINT ck_tax_declarations_submitted_at CHECK ((submitted_enc IS NULL) = (submitted_at IS NULL)),
  CONSTRAINT ck_tax_declarations_submitted_regime CHECK (submitted_enc IS NULL OR regime IS NOT NULL),
  CONSTRAINT ck_tax_declarations_submitted_version CHECK (submitted_version IS NULL OR submitted_version >= 1),
  CONSTRAINT ck_tax_declarations_unlock CHECK ((unlocked_until IS NULL) = (unlock_reason_enc IS NULL)),
  CONSTRAINT ck_tax_declarations_unlocked_until CHECK (unlocked_until IS NULL OR unlocked_until BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_tax_declarations_entered_by CHECK (entered_by IS NULL OR entered_by <> user_id),
  CONSTRAINT ck_tax_declarations_version CHECK (version >= 1)
);
CREATE INDEX ix_tax_declarations_fy ON tax_declarations (fy, user_id);
CREATE INDEX ix_tax_declarations_entered_by ON tax_declarations (entered_by);
CREATE INDEX ix_tax_declarations_updated_by ON tax_declarations (updated_by);

CREATE FUNCTION tax_declarations_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.submitted_enc IS NOT NULL OR NEW.unlocked_until IS NOT NULL OR NEW.regime IS NOT NULL THEN
      RAISE EXCEPTION 'a tax declaration must be created as a draft' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'tax declaration %% cannot be deleted', OLD.id USING ERRCODE = '23000';
  END IF;
  IF NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.fy IS DISTINCT FROM OLD.fy OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'tax declaration %%: employee, FY and creation time never change', OLD.id USING ERRCODE = '23000';
  END IF;
  IF OLD.entered_by IS NOT NULL AND NEW.entered_by IS DISTINCT FROM OLD.entered_by THEN
    RAISE EXCEPTION 'tax declaration %%: entered_by is set once', OLD.id USING ERRCODE = '23000';
  END IF;
  -- app.key_rotation is a plain session setting: rotation runs as the application role, so a role check is not possible
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  IF NEW.entered_by IS NOT NULL AND NEW.entered_by IS DISTINCT FROM OLD.entered_by
     AND EXISTS (SELECT 1 FROM tax_proofs p WHERE p.declaration_id = OLD.id AND p.verified_by = NEW.entered_by) THEN
    RAISE EXCEPTION 'tax declaration %%: the person entering it on behalf verified a proof of it', OLD.id USING ERRCODE = '23000';
  END IF;
  -- the submitted part (T3) changes only by resubmitting: new ciphertext AND a strictly higher submitted_version
  IF NEW.submitted_enc IS DISTINCT FROM OLD.submitted_enc THEN
    IF NEW.submitted_enc IS NULL OR NEW.submitted_version IS NULL OR NEW.submitted_version <= COALESCE(OLD.submitted_version, 0) THEN
      RAISE EXCEPTION 'tax declaration %%: a resubmission needs a higher submitted_version', OLD.id USING ERRCODE = '23000';
    END IF;
  ELSIF NEW.submitted_version IS DISTINCT FROM OLD.submitted_version OR NEW.regime IS DISTINCT FROM OLD.regime
        OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at THEN
    RAISE EXCEPTION 'tax declaration %%: the submitted regime and version change only by resubmitting', OLD.id USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_tax_declarations_lock BEFORE INSERT OR UPDATE OR DELETE ON tax_declarations FOR EACH ROW EXECUTE FUNCTION tax_declarations_lock();
CREATE TRIGGER trg_tax_declarations_updated_at BEFORE UPDATE ON tax_declarations FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- tax proofs
CREATE TABLE tax_proofs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  declaration_id uuid NOT NULL REFERENCES tax_declarations(id) ON DELETE RESTRICT,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  fy text NOT NULL,
  item_id text NOT NULL,
  section text NOT NULL,
  claimed_enc bytea NOT NULL,
  file_ids uuid[] NOT NULL,
  status text NOT NULL DEFAULT 'submitted',
  verified_enc bytea,
  verified_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  verified_at timestamptz,
  comment_enc bytea,
  version integer NOT NULL DEFAULT 1,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_tax_proofs_fy CHECK (fy ~ '^[0-9]{4}-[0-9]{2}$'),
  CONSTRAINT ck_tax_proofs_item_id CHECK (char_length(item_id) BETWEEN 1 AND 64),
  CONSTRAINT ck_tax_proofs_section CHECK (section ~ '^[A-Z0-9_]{2,16}$'),
  CONSTRAINT ck_tax_proofs_files CHECK (cardinality(file_ids) BETWEEN 1 AND 10 AND array_position(file_ids, NULL::uuid) IS NULL),
  CONSTRAINT ck_tax_proofs_status CHECK (status IN ('submitted','verified','rejected','cancelled')),
  CONSTRAINT ck_tax_proofs_decided_pair CHECK ((verified_by IS NULL) = (verified_at IS NULL)),
  CONSTRAINT ck_tax_proofs_decided_status CHECK ((status IN ('verified','rejected')) = (verified_by IS NOT NULL)),
  CONSTRAINT ck_tax_proofs_verified_enc CHECK ((status = 'verified') = (verified_enc IS NOT NULL)),
  CONSTRAINT ck_tax_proofs_rejected_comment CHECK (status <> 'rejected' OR comment_enc IS NOT NULL),
  CONSTRAINT ck_tax_proofs_verifier CHECK (verified_by IS NULL OR verified_by <> user_id),
  CONSTRAINT ck_tax_proofs_version CHECK (version >= 1)
);
CREATE UNIQUE INDEX uq_tax_proofs_live_item ON tax_proofs (declaration_id, item_id) WHERE status IN ('submitted','verified');
CREATE INDEX ix_tax_proofs_declaration_id ON tax_proofs (declaration_id);
CREATE INDEX ix_tax_proofs_user_fy_status ON tax_proofs (user_id, fy, status);
CREATE INDEX ix_tax_proofs_status_created ON tax_proofs (status, created_at);
CREATE INDEX ix_tax_proofs_verified_by ON tax_proofs (verified_by);
CREATE INDEX ix_tax_proofs_updated_by ON tax_proofs (updated_by);

CREATE FUNCTION tax_proofs_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  decl record;
  j_new jsonb;
  j_old jsonb;
  allowed text[] := ARRAY['version','updated_by','updated_at'];
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'tax proof %% cannot be deleted', OLD.id USING ERRCODE = '23000';
  END IF;
  SELECT d.user_id, d.fy, d.entered_by INTO decl FROM tax_declarations d WHERE d.id = NEW.declaration_id;
  IF NOT FOUND OR decl.user_id IS DISTINCT FROM NEW.user_id OR decl.fy IS DISTINCT FROM NEW.fy THEN
    RAISE EXCEPTION 'tax proof: employee and FY must equal the declaration''s' USING ERRCODE = '23000';
  END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'submitted' OR NEW.verified_by IS NOT NULL THEN
      RAISE EXCEPTION 'a tax proof must be created as submitted' USING ERRCODE = '23000';
    END IF;
  ELSE
    IF NEW.declaration_id IS DISTINCT FROM OLD.declaration_id OR NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.fy IS DISTINCT FROM OLD.fy
       OR NEW.item_id IS DISTINCT FROM OLD.item_id OR NEW.section IS DISTINCT FROM OLD.section OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
      RAISE EXCEPTION 'tax proof %%: declaration, employee, FY, item, section and creation time never change', OLD.id USING ERRCODE = '23000';
    END IF;
    IF current_setting('app.key_rotation', true) = 'on'
       AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
      RETURN NEW;  -- key rotation: only *_enc columns changed
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status || '>' || NEW.status) NOT IN ('submitted>verified','submitted>rejected','submitted>cancelled') THEN
      RAISE EXCEPTION 'tax proof %%: %% to %% is not a valid transition', OLD.id, OLD.status, NEW.status USING ERRCODE = '23000';
    END IF;
    IF OLD.status = 'submitted' AND NEW.status = 'submitted' THEN
      allowed := allowed || ARRAY['claimed_enc','file_ids'];
    ELSIF OLD.status = 'submitted' AND NEW.status = 'verified' THEN
      allowed := allowed || ARRAY['status','verified_enc','verified_by','verified_at','comment_enc'];
    ELSIF OLD.status = 'submitted' AND NEW.status = 'rejected' THEN
      allowed := allowed || ARRAY['status','verified_by','verified_at','comment_enc'];
    ELSIF OLD.status = 'submitted' AND NEW.status = 'cancelled' THEN
      allowed := allowed || ARRAY['status'];
    END IF;  -- verified / rejected / cancelled: nothing but version / updated_*
    j_new := to_jsonb(NEW) - allowed;
    j_old := to_jsonb(OLD) - allowed;
    IF j_new IS DISTINCT FROM j_old THEN
      RAISE EXCEPTION 'tax proof %% is %% and locked', OLD.id, OLD.status USING ERRCODE = '23000';
    END IF;
  END IF;
  IF NEW.verified_by IS NOT NULL AND NEW.verified_by IS NOT DISTINCT FROM decl.entered_by THEN
    RAISE EXCEPTION 'tax proof %%: the decider entered the declaration on behalf of the employee', NEW.id USING ERRCODE = '23000';
  END IF;
  IF TG_OP = 'INSERT' OR NEW.file_ids IS DISTINCT FROM OLD.file_ids THEN
    IF (SELECT count(*) FROM files f WHERE f.id = ANY (NEW.file_ids) AND f.purpose = 'tax_proof'
          AND (f.owner_id = NEW.user_id OR f.owner_id IS NOT DISTINCT FROM decl.entered_by)) <> cardinality(NEW.file_ids) THEN
      RAISE EXCEPTION 'tax proof: every file must be a distinct tax_proof file of the employee (or of the person entering the declaration)' USING ERRCODE = '23000';
    END IF;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_tax_proofs_lock BEFORE INSERT OR UPDATE OR DELETE ON tax_proofs FOR EACH ROW EXECUTE FUNCTION tax_proofs_lock();
CREATE TRIGGER trg_tax_proofs_updated_at BEFORE UPDATE ON tax_proofs FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- Form 16 documents
CREATE TABLE form16_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  fy text NOT NULL,
  kind text NOT NULL,
  file_id uuid NOT NULL REFERENCES files(id) ON DELETE RESTRICT,
  sha256 text NOT NULL,
  upload_batch_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'uploaded',
  uploaded_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  second_person boolean NOT NULL DEFAULT true,
  published_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  published_at timestamptz,
  withdrawn_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  withdrawn_at timestamptz,
  withdraw_reason_enc bytea,
  version integer NOT NULL DEFAULT 1,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_form16_documents_file UNIQUE (file_id),
  CONSTRAINT ck_form16_documents_fy CHECK (fy ~ '^[0-9]{4}-[0-9]{2}$'),
  CONSTRAINT ck_form16_documents_kind CHECK (kind IN ('form16','part_a','part_b','form12ba')),
  CONSTRAINT ck_form16_documents_status CHECK (status IN ('uploaded','published','withdrawn')),
  CONSTRAINT ck_form16_documents_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_form16_documents_published_pair CHECK ((published_by IS NULL) = (published_at IS NULL)),
  CONSTRAINT ck_form16_documents_published_status CHECK ((status <> 'uploaded' OR published_by IS NULL) AND (status <> 'published' OR published_by IS NOT NULL)),
  CONSTRAINT ck_form16_documents_withdrawn_pair CHECK ((withdrawn_by IS NULL) = (withdrawn_at IS NULL)),
  CONSTRAINT ck_form16_documents_withdrawn_status CHECK ((status = 'withdrawn') = (withdrawn_by IS NOT NULL)),
  CONSTRAINT ck_form16_documents_withdrawn_reason CHECK (status <> 'withdrawn' OR withdraw_reason_enc IS NOT NULL),
  CONSTRAINT ck_form16_documents_second_person CHECK (NOT second_person OR published_by IS NULL OR published_by <> uploaded_by),
  CONSTRAINT ck_form16_documents_not_own CHECK (published_by IS NULL OR published_by <> user_id),
  CONSTRAINT ck_form16_documents_version CHECK (version >= 1)
);
CREATE UNIQUE INDEX uq_form16_documents_live ON form16_documents (user_id, fy, kind) WHERE status IN ('uploaded','published');
CREATE INDEX ix_form16_documents_user_fy ON form16_documents (user_id, fy);
CREATE INDEX ix_form16_documents_fy_kind_status ON form16_documents (fy, kind, status);
CREATE INDEX ix_form16_documents_upload_batch_id ON form16_documents (upload_batch_id);
CREATE INDEX ix_form16_documents_uploaded_by ON form16_documents (uploaded_by);
CREATE INDEX ix_form16_documents_published_by ON form16_documents (published_by);
CREATE INDEX ix_form16_documents_withdrawn_by ON form16_documents (withdrawn_by);
CREATE INDEX ix_form16_documents_updated_by ON form16_documents (updated_by);

CREATE FUNCTION form16_documents_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  j_new jsonb;
  j_old jsonb;
  allowed text[] := ARRAY['version','updated_by','updated_at'];
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'uploaded' OR NEW.published_by IS NOT NULL OR NEW.withdrawn_by IS NOT NULL THEN
      RAISE EXCEPTION 'a Form 16 document must be created as uploaded' USING ERRCODE = '23000';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM files f WHERE f.id = NEW.file_id AND f.purpose = 'form16') THEN
      RAISE EXCEPTION 'Form 16 document: the file must have purpose form16' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
  END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Form 16 document %% cannot be deleted', OLD.id USING ERRCODE = '23000';
  END IF;
  IF NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.fy IS DISTINCT FROM OLD.fy OR NEW.kind IS DISTINCT FROM OLD.kind
     OR NEW.file_id IS DISTINCT FROM OLD.file_id OR NEW.sha256 IS DISTINCT FROM OLD.sha256 OR NEW.upload_batch_id IS DISTINCT FROM OLD.upload_batch_id
     OR NEW.uploaded_by IS DISTINCT FROM OLD.uploaded_by OR NEW.second_person IS DISTINCT FROM OLD.second_person
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'Form 16 document %%: employee, FY, kind, file, hash, batch, uploader and second-person flag never change', OLD.id USING ERRCODE = '23000';
  END IF;
  IF current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  IF NEW.status <> OLD.status AND (OLD.status || '>' || NEW.status) NOT IN ('uploaded>published','uploaded>withdrawn','published>withdrawn') THEN
    RAISE EXCEPTION 'Form 16 document %%: %% to %% is not a valid transition', OLD.id, OLD.status, NEW.status USING ERRCODE = '23000';
  END IF;
  IF OLD.status = 'uploaded' AND NEW.status = 'published' THEN
    allowed := allowed || ARRAY['status','published_by','published_at'];
  ELSIF NEW.status = 'withdrawn' AND OLD.status IN ('uploaded','published') THEN
    allowed := allowed || ARRAY['status','withdrawn_by','withdrawn_at','withdraw_reason_enc'];
  END IF;  -- published>published, withdrawn>withdrawn, uploaded>uploaded: nothing but version / updated_*
  j_new := to_jsonb(NEW) - allowed;
  j_old := to_jsonb(OLD) - allowed;
  IF j_new IS DISTINCT FROM j_old THEN
    RAISE EXCEPTION 'Form 16 document %% is %% and locked', OLD.id, OLD.status USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_form16_documents_lock BEFORE INSERT OR UPDATE OR DELETE ON form16_documents FOR EACH ROW EXECUTE FUNCTION form16_documents_lock();
CREATE TRIGGER trg_form16_documents_updated_at BEFORE UPDATE ON form16_documents FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- file purposes, money history events
ALTER TABLE files DROP CONSTRAINT ck_files_purpose;
ALTER TABLE files ADD CONSTRAINT ck_files_purpose CHECK (purpose IN ("""
    + _FILE_PURPOSES_NEW
    + r"""));

ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_item_type;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_item_type CHECK (item_type IN ("""
    + _EVENT_TYPES_NEW
    + r"""));
ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_action;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_action CHECK (action IN ("""
    + _EVENT_ACTIONS_NEW
    + r"""));

-- ---------------------------------------------------------------- settings
INSERT INTO settings (key, value) VALUES ('payroll.form16_requires_second_person', 'true'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('payroll.tax_proof_max_files', '5'::jsonb) ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  REVOKE DELETE, TRUNCATE ON tax_year_rules FROM app_user;
  REVOKE DELETE, TRUNCATE ON tax_declarations FROM app_user;
  REVOKE DELETE, TRUNCATE ON tax_proofs FROM app_user;
  REVOKE DELETE, TRUNCATE ON form16_documents FROM app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'revoke on the 0027 tables skipped';
END
$$;
"""
)

DOWNGRADE = (
    r"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM tax_declarations) OR EXISTS (SELECT 1 FROM tax_proofs) OR EXISTS (SELECT 1 FROM form16_documents) THEN
    RAISE EXCEPTION 'cannot downgrade 0027: tax declarations, proofs or Form 16 documents exist (their evidence must not be dropped silently)';
  END IF;
  IF EXISTS (SELECT 1 FROM tax_year_rules WHERE NOT (fy = '"""
    + SEED_FY
    + r"""' AND version = 1 AND status = 'draft' AND created_by IS NULL AND last_edited_by IS NULL)) THEN
    RAISE EXCEPTION 'cannot downgrade 0027: tax year rules other than the untouched seed exist';
  END IF;
END
$$;

DELETE FROM settings WHERE key IN ('payroll.form16_requires_second_person', 'payroll.tax_proof_max_files');

ALTER TABLE payroll_item_events DISABLE TRIGGER trg_payroll_item_events_no_update_delete;
DELETE FROM payroll_item_events WHERE item_type IN ('tax_rules','tax_declaration','tax_proof','form16') OR action = 'unlock';
ALTER TABLE payroll_item_events ENABLE TRIGGER trg_payroll_item_events_no_update_delete;
ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_action;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_action CHECK (action IN ("""
    + _EVENT_ACTIONS_OLD
    + r"""));
ALTER TABLE payroll_item_events DROP CONSTRAINT ck_payroll_item_events_item_type;
ALTER TABLE payroll_item_events ADD CONSTRAINT ck_payroll_item_events_item_type CHECK (item_type IN ("""
    + _EVENT_TYPES_OLD
    + r"""));

DROP TABLE IF EXISTS form16_documents;
DROP FUNCTION IF EXISTS form16_documents_lock();
DROP TABLE IF EXISTS tax_proofs;
DROP FUNCTION IF EXISTS tax_proofs_lock();
DROP TABLE IF EXISTS tax_declarations;
DROP FUNCTION IF EXISTS tax_declarations_lock();
DROP TABLE IF EXISTS tax_year_rules;
DROP FUNCTION IF EXISTS tax_year_rules_lock();

DELETE FROM files WHERE purpose IN ('tax_proof','form16');
ALTER TABLE files DROP CONSTRAINT ck_files_purpose;
ALTER TABLE files ADD CONSTRAINT ck_files_purpose CHECK (purpose IN ("""
    + _FILE_PURPOSES_OLD
    + r"""));
"""
)


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
