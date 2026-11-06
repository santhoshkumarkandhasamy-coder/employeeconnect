"""Statutory challans (plan 14 phase 2): paid / unpaid tracking of PF, ESI, PT and TDS dues per month

Revision ID: 0021
Revises: 0020

Added
* statutory_challans  one row per (type, period): type pf|esi|pt|tds, period = first of month, status unpaid|paid.
                      amount_enc and reference_enc are application-encrypted (bytea; model types EncryptedString).
                      A paid row must carry paid_on; an unpaid row carries neither paid_on nor reference.

Downgrade drops the table (ALL challan history is lost).
"""

from migration_sql import run

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE statutory_challans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  type text NOT NULL,
  period date NOT NULL,
  amount_enc bytea NOT NULL,
  paid_on date,
  reference_enc bytea,
  status text NOT NULL DEFAULT 'unpaid',
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_statutory_challans_type_period UNIQUE (type, period),
  CONSTRAINT ck_statutory_challans_type CHECK (type IN ('pf','esi','pt','tds')),
  CONSTRAINT ck_statutory_challans_status CHECK (status IN ('unpaid','paid')),
  CONSTRAINT ck_statutory_challans_period_first CHECK (extract(day from period) = 1),
  CONSTRAINT ck_statutory_challans_period_year CHECK (period BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'),
  CONSTRAINT ck_statutory_challans_paid CHECK (
    (status = 'paid' AND paid_on IS NOT NULL) OR (status = 'unpaid' AND paid_on IS NULL AND reference_enc IS NULL)
  )
);
CREATE INDEX ix_statutory_challans_period ON statutory_challans (period);
CREATE INDEX ix_statutory_challans_created_by ON statutory_challans (created_by);
CREATE INDEX ix_statutory_challans_updated_by ON statutory_challans (updated_by);
CREATE TRIGGER trg_statutory_challans_updated_at BEFORE UPDATE ON statutory_challans FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS statutory_challans;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
