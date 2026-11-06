"""Statutory challan event log + tighter privileges (security review of payroll statutory: M2, L5)

Revision ID: 0022
Revises: 0021

Added
* statutory_challan_events  APPEND-ONLY history of every paid / unpaid change of a statutory_challans row. Each event stores the
                            state AFTER the change (paid_on, reference_enc, amount_enc, status) and the state BEFORE it
                            (prev_*), so values that are overwritten or cleared (mark unpaid, re-pay with another amount) are
                            never lost. reference and amount are application-encrypted (bytea; model type EncryptedString).
                            Protection: REVOKE UPDATE, DELETE, TRUNCATE FROM app_user (as for audit_logs) plus a trigger that
                            blocks DELETE / TRUNCATE for every role and UPDATE for every role except a key rotation
                            (`app.key_rotation = 'on'` and only `*_enc` columns changed, see payroll_strip_enc from 0018).
                            app_user keeps column-level UPDATE on the four encrypted columns only, so key rotation still works.
* statutory_challans        REVOKE DELETE, TRUNCATE FROM app_user (a challan record is never deleted, only marked unpaid).

Downgrade drops the event table and restores the two privileges.
"""

from migration_sql import run

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE statutory_challan_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  challan_id uuid NOT NULL REFERENCES statutory_challans(id) ON DELETE RESTRICT,
  type text NOT NULL,
  period date NOT NULL,
  action text NOT NULL,
  status text NOT NULL,
  paid_on date,
  reference_enc bytea,
  amount_enc bytea,
  prev_status text,
  prev_paid_on date,
  prev_reference_enc bytea,
  prev_amount_enc bytea,
  actor_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_statutory_challan_events_type CHECK (type IN ('pf','esi','pt','tds')),
  CONSTRAINT ck_statutory_challan_events_action CHECK (action IN ('paid','unpaid')),
  CONSTRAINT ck_statutory_challan_events_status CHECK (status IN ('unpaid','paid')),
  CONSTRAINT ck_statutory_challan_events_prev_status CHECK (prev_status IS NULL OR prev_status IN ('unpaid','paid'))
);
CREATE INDEX ix_statutory_challan_events_challan ON statutory_challan_events (challan_id, created_at);
CREATE INDEX ix_statutory_challan_events_actor_id ON statutory_challan_events (actor_id);
CREATE INDEX ix_statutory_challan_events_period ON statutory_challan_events (period, type);

CREATE FUNCTION statutory_challan_events_lock() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND current_setting('app.key_rotation', true) = 'on'
     AND payroll_strip_enc(to_jsonb(NEW)) IS NOT DISTINCT FROM payroll_strip_enc(to_jsonb(OLD)) THEN
    RETURN NEW;  -- key rotation: only *_enc columns changed
  END IF;
  RAISE EXCEPTION 'statutory_challan_events is append-only' USING ERRCODE = 'insufficient_privilege', DETAIL = TG_OP;
END
$$;
CREATE TRIGGER trg_statutory_challan_events_no_update_delete BEFORE UPDATE OR DELETE ON statutory_challan_events
  FOR EACH ROW EXECUTE FUNCTION statutory_challan_events_lock();
CREATE TRIGGER trg_statutory_challan_events_no_truncate BEFORE TRUNCATE ON statutory_challan_events
  FOR EACH STATEMENT EXECUTE FUNCTION statutory_challan_events_lock();

DO $$
BEGIN
  REVOKE UPDATE, DELETE, TRUNCATE ON statutory_challan_events FROM app_user;
  GRANT UPDATE (reference_enc, amount_enc, prev_reference_enc, prev_amount_enc) ON statutory_challan_events TO app_user;
  REVOKE DELETE, TRUNCATE ON statutory_challans FROM app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'revoke on statutory tables skipped';
END
$$;
"""

DOWNGRADE = r"""
DO $$
BEGIN
  GRANT DELETE, TRUNCATE ON statutory_challans TO app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'grant on statutory_challans skipped';
END
$$;
DROP TABLE IF EXISTS statutory_challan_events;
DROP FUNCTION IF EXISTS statutory_challan_events_lock();
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
