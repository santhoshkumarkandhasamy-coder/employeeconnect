"""food_enrollments

Revision ID: 0004
Revises: 0003
"""
from migration_sql import run

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE food_enrollments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  date date NOT NULL,
  need_food boolean NOT NULL,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_food_enrollments_user_date UNIQUE (user_id, date)
);
CREATE INDEX ix_food_enrollments_date ON food_enrollments (date);
CREATE INDEX ix_food_enrollments_date_need ON food_enrollments (date) WHERE need_food;
CREATE INDEX ix_food_enrollments_updated_by ON food_enrollments (updated_by);

CREATE TRIGGER trg_food_enrollments_updated_at BEFORE UPDATE ON food_enrollments FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS food_enrollments;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
