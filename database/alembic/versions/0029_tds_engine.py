"""TDS engine: tax computation snapshots, TDS challan details, engine settings (plan 17 work package 5c)

Revision ID: 0029
Revises: 0028

Plan: docs/plans/17-payroll-phase5.md sections 2.3 and 3 (TDS engine); decisions T6, T8, T9; contract: docs/api-contract.md section 19.9, 19.10.

Added
* payroll_tax_computations  ONE row per draft / processed payslip (UNIQUE payslip_id): the rules used (rules_id + rules_version), the regime,
                            the submitted declaration version (NULL = none), a digest of the verified proofs used, the mode the run snapshotted
                            ('suggest' | 'apply'), the encrypted computation (annual figures and steps) and the encrypted suggested TDS,
                            `applied` (the engine value was written to the payslip TDS line; only in mode apply) and an optional encrypted
                            override reason (only in mode apply). payroll_tax_computations_lock():
                              - user_id equals the payslip's user and fy equals the financial year (April to March) of the run's period;
                              - rules_id points to a VERIFIED or SUPERSEDED rules row of the same FY whose version equals rules_version
                                (a draft or submitted rules document can never feed a payslip);
                              - the row can only be written (INSERT / UPDATE) while the payslip is `draft`, and can only be deleted while the
                                payslip is `draft` or already gone (the draft payslip itself is being deleted: ON DELETE CASCADE). Once the
                                payslip is processed (or published / superseded) the computation is FROZEN: write it BEFORE the payslip
                                is moved to `processed`, in the same transaction. A reopened payslip (back to draft) can be recomputed;
                              - payslip_id, user_id, fy and created_at never change;
                              - KEY ROTATION passes: with `app.key_rotation = 'on'` an UPDATE that changes only `*_enc` columns is accepted
                                at any payslip status (payroll_strip_enc, as for every payroll table). This is the SAME escape every payroll
                                table since 0018 has (a plain session setting; rotation runs as app_user, so a role check is impossible); it is
                                not widened here. What it could rewrite (the encrypted computation) is covered by the publish MAC
                                (payroll_tds.attach_macs hashes the decrypted computation body and the override), so tampering makes a
                                publish request stale;
                              - the payslip row is read FOR SHARE (a concurrent status change waits until this statement commits).
                            Fk payslip_id is ON DELETE CASCADE (a draft payslip / run can still be deleted); every other fk is RESTRICT.
                            REVOKE TRUNCATE FROM app_user; DELETE stays granted (the service deletes the row of a draft payslip when the engine no longer
                            applies; the trigger refuses it for every other status).
* statutory_challans        bsr_code_enc, challan_serial_enc (encrypted), deposited_on date: TDS challan details for the 24Q Challans sheet.
                            CHECKs: only a PAID TDS challan carries them (so marking a challan unpaid must clear them, as it already clears
                            paid_on / reference_enc); bsr_code_enc and challan_serial_enc are both set or both NULL; deposited_on in 2000..2200.
* statutory_challan_events  the same three columns (state after) and prev_bsr_code_enc, prev_challan_serial_enc, prev_deposited_on (state
                            before), same CHECKs (TDS rows only, pairs). app_user keeps column-level UPDATE on the four encrypted columns
                            for key rotation (as for reference_enc / amount_enc since 0022).
* settings                  payroll.tds_engine_enabled (false), payroll.tds_engine_mode ("suggest"), payroll.tds_one_time_in_month (true),
                            payroll.tds_proofs_from_month (1), payroll.tds_include_other_income (false).

Downgrade REFUSES while ANY payroll_tax_computations row exists or any challan / challan event carries the new TDS details (evidence must
not be dropped silently). Otherwise it drops the table, trigger function, columns, CHECKs and the five settings.
"""

from migration_sql import run

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_SETTINGS = (
    ("payroll.tds_engine_enabled", "false"),
    ("payroll.tds_engine_mode", '"suggest"'),
    ("payroll.tds_one_time_in_month", "true"),
    ("payroll.tds_proofs_from_month", "1"),
    ("payroll.tds_include_other_income", "false"),
)

UPGRADE = (
    r"""
-- ---------------------------------------------------------------- tax computation snapshots
CREATE TABLE payroll_tax_computations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  payslip_id uuid NOT NULL REFERENCES payslips(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  fy text NOT NULL,
  rules_id uuid NOT NULL REFERENCES tax_year_rules(id) ON DELETE RESTRICT,
  rules_version integer NOT NULL,
  regime text NOT NULL,
  declaration_version integer,
  proofs_digest text NOT NULL,
  mode text NOT NULL,
  computation_enc bytea NOT NULL,
  suggested_enc bytea NOT NULL,
  applied boolean NOT NULL DEFAULT false,
  override_reason_enc bytea,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_payroll_tax_computations_payslip UNIQUE (payslip_id),
  CONSTRAINT ck_payroll_tax_computations_fy CHECK (fy ~ '^[0-9]{4}-[0-9]{2}$'),
  CONSTRAINT ck_payroll_tax_computations_rules_version CHECK (rules_version >= 1),
  CONSTRAINT ck_payroll_tax_computations_regime CHECK (regime IN ('old','new')),
  CONSTRAINT ck_payroll_tax_computations_declaration_version CHECK (declaration_version IS NULL OR declaration_version >= 1),
  CONSTRAINT ck_payroll_tax_computations_proofs_digest CHECK (char_length(proofs_digest) BETWEEN 1 AND 128),
  CONSTRAINT ck_payroll_tax_computations_mode CHECK (mode IN ('suggest','apply')),
  CONSTRAINT ck_payroll_tax_computations_applied CHECK (NOT applied OR mode = 'apply'),
  CONSTRAINT ck_payroll_tax_computations_override CHECK (override_reason_enc IS NULL OR mode = 'apply')
);
CREATE INDEX ix_payroll_tax_computations_user_fy ON payroll_tax_computations (user_id, fy);
CREATE INDEX ix_payroll_tax_computations_rules_id ON payroll_tax_computations (rules_id);
CREATE INDEX ix_payroll_tax_computations_created_by ON payroll_tax_computations (created_by);
CREATE INDEX ix_payroll_tax_computations_updated_by ON payroll_tax_computations (updated_by);

CREATE FUNCTION payroll_tax_computations_lock() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  slip record;
  rl record;
  y integer;
BEGIN
  IF TG_OP = 'DELETE' THEN
    SELECT p.status INTO slip FROM payslips p WHERE p.id = OLD.payslip_id FOR SHARE OF p;
    IF FOUND AND slip.status <> 'draft' THEN
      RAISE EXCEPTION 'tax computation %% belongs to a %% payslip and cannot be deleted', OLD.id, slip.status USING ERRCODE = '23000';
    END IF;
    RETURN OLD;  -- draft payslip, or the payslip itself is being deleted (cascade)
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.payslip_id IS DISTINCT FROM OLD.payslip_id OR NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.fy IS DISTINCT FROM OLD.fy
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
      RAISE EXCEPTION 'tax computation %%: payslip, employee, FY and creation time never change', OLD.id USING ERRCODE = '23000';
    END IF;
    -- app.key_rotation is a plain session setting: rotation runs as the application role, so a role check is not possible
    IF current_setting('app.key_rotation', true) = 'on'
       AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
      RETURN NEW;  -- key rotation: only *_enc columns changed
    END IF;
  END IF;
  SELECT p.status, p.user_id, r.period INTO slip FROM payslips p JOIN payroll_runs r ON r.id = p.run_id WHERE p.id = NEW.payslip_id FOR SHARE OF p;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'tax computation: the payslip does not exist' USING ERRCODE = '23000';
  END IF;
  IF slip.status <> 'draft' THEN
    RAISE EXCEPTION 'tax computation of payslip %% is frozen (the payslip is %%)', NEW.payslip_id, slip.status USING ERRCODE = '23000';
  END IF;
  IF slip.user_id IS DISTINCT FROM NEW.user_id THEN
    RAISE EXCEPTION 'tax computation: the employee must equal the payslip''s' USING ERRCODE = '23000';
  END IF;
  y := extract(year FROM slip.period)::integer - CASE WHEN extract(month FROM slip.period) < 4 THEN 1 ELSE 0 END;
  IF NEW.fy IS DISTINCT FROM (y::text || '-' || lpad(((y + 1) %% 100)::text, 2, '0')) THEN
    RAISE EXCEPTION 'tax computation: FY %% is not the financial year of the payslip period', NEW.fy USING ERRCODE = '23000';
  END IF;
  SELECT t.fy, t.version, t.status INTO rl FROM tax_year_rules t WHERE t.id = NEW.rules_id;
  IF NOT FOUND OR rl.fy IS DISTINCT FROM NEW.fy OR rl.version IS DISTINCT FROM NEW.rules_version OR rl.status NOT IN ('verified','superseded') THEN
    RAISE EXCEPTION 'tax computation: the rules must be a verified (or superseded) version of the same FY, with the stated version' USING ERRCODE = '23000';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER trg_payroll_tax_computations_lock BEFORE INSERT OR UPDATE OR DELETE ON payroll_tax_computations FOR EACH ROW EXECUTE FUNCTION payroll_tax_computations_lock();
CREATE TRIGGER trg_payroll_tax_computations_updated_at BEFORE UPDATE ON payroll_tax_computations FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- TDS challan details
ALTER TABLE statutory_challans ADD COLUMN bsr_code_enc bytea;
ALTER TABLE statutory_challans ADD COLUMN challan_serial_enc bytea;
ALTER TABLE statutory_challans ADD COLUMN deposited_on date;
ALTER TABLE statutory_challans ADD CONSTRAINT ck_statutory_challans_tds_fields CHECK ((type = 'tds' AND status = 'paid') OR (bsr_code_enc IS NULL AND challan_serial_enc IS NULL AND deposited_on IS NULL));
ALTER TABLE statutory_challans ADD CONSTRAINT ck_statutory_challans_bsr_pair CHECK ((bsr_code_enc IS NULL) = (challan_serial_enc IS NULL));
ALTER TABLE statutory_challans ADD CONSTRAINT ck_statutory_challans_deposited_on CHECK (deposited_on IS NULL OR deposited_on BETWEEN DATE '2000-01-01' AND DATE '2200-12-31');

ALTER TABLE statutory_challan_events ADD COLUMN bsr_code_enc bytea;
ALTER TABLE statutory_challan_events ADD COLUMN challan_serial_enc bytea;
ALTER TABLE statutory_challan_events ADD COLUMN deposited_on date;
ALTER TABLE statutory_challan_events ADD COLUMN prev_bsr_code_enc bytea;
ALTER TABLE statutory_challan_events ADD COLUMN prev_challan_serial_enc bytea;
ALTER TABLE statutory_challan_events ADD COLUMN prev_deposited_on date;
ALTER TABLE statutory_challan_events ADD CONSTRAINT ck_statutory_challan_events_tds_fields CHECK (type = 'tds' OR (bsr_code_enc IS NULL AND challan_serial_enc IS NULL AND deposited_on IS NULL AND prev_bsr_code_enc IS NULL AND prev_challan_serial_enc IS NULL AND prev_deposited_on IS NULL));
ALTER TABLE statutory_challan_events ADD CONSTRAINT ck_statutory_challan_events_bsr_pair CHECK ((bsr_code_enc IS NULL) = (challan_serial_enc IS NULL) AND (prev_bsr_code_enc IS NULL) = (prev_challan_serial_enc IS NULL));

-- ---------------------------------------------------------------- settings
"""
    + "\n".join(f"INSERT INTO settings (key, value) VALUES ('{k}', '{v}'::jsonb) ON CONFLICT (key) DO NOTHING;" for k, v in _SETTINGS)
    + r"""

DO $$
BEGIN
  REVOKE TRUNCATE ON payroll_tax_computations FROM app_user;
  GRANT DELETE ON payroll_tax_computations TO app_user;  -- the service deletes the row of a draft payslip; the trigger refuses every other status
  GRANT UPDATE (bsr_code_enc, challan_serial_enc, prev_bsr_code_enc, prev_challan_serial_enc) ON statutory_challan_events TO app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'revoke / grant on the 0029 objects skipped';
END
$$;
"""
)

DOWNGRADE = (
    r"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM payroll_tax_computations) THEN
    RAISE EXCEPTION 'cannot downgrade 0029: tax computations exist (their evidence must not be dropped silently)';
  END IF;
  IF EXISTS (SELECT 1 FROM statutory_challans WHERE bsr_code_enc IS NOT NULL OR challan_serial_enc IS NOT NULL OR deposited_on IS NOT NULL)
     OR EXISTS (SELECT 1 FROM statutory_challan_events WHERE bsr_code_enc IS NOT NULL OR challan_serial_enc IS NOT NULL OR deposited_on IS NOT NULL
                OR prev_bsr_code_enc IS NOT NULL OR prev_challan_serial_enc IS NOT NULL OR prev_deposited_on IS NOT NULL) THEN
    RAISE EXCEPTION 'cannot downgrade 0029: TDS challan details (BSR code, serial, deposit date) exist';
  END IF;
END
$$;

DELETE FROM settings WHERE key IN ("""
    + ", ".join(f"'{k}'" for k, _ in _SETTINGS)
    + r""");

ALTER TABLE statutory_challan_events DROP CONSTRAINT ck_statutory_challan_events_bsr_pair;
ALTER TABLE statutory_challan_events DROP CONSTRAINT ck_statutory_challan_events_tds_fields;
ALTER TABLE statutory_challan_events DROP COLUMN prev_deposited_on;
ALTER TABLE statutory_challan_events DROP COLUMN prev_challan_serial_enc;
ALTER TABLE statutory_challan_events DROP COLUMN prev_bsr_code_enc;
ALTER TABLE statutory_challan_events DROP COLUMN deposited_on;
ALTER TABLE statutory_challan_events DROP COLUMN challan_serial_enc;
ALTER TABLE statutory_challan_events DROP COLUMN bsr_code_enc;

ALTER TABLE statutory_challans DROP CONSTRAINT ck_statutory_challans_deposited_on;
ALTER TABLE statutory_challans DROP CONSTRAINT ck_statutory_challans_bsr_pair;
ALTER TABLE statutory_challans DROP CONSTRAINT ck_statutory_challans_tds_fields;
ALTER TABLE statutory_challans DROP COLUMN deposited_on;
ALTER TABLE statutory_challans DROP COLUMN challan_serial_enc;
ALTER TABLE statutory_challans DROP COLUMN bsr_code_enc;

DROP TABLE IF EXISTS payroll_tax_computations;
DROP FUNCTION IF EXISTS payroll_tax_computations_lock();
"""
)


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
