"""employee_profiles.show_celebrations (opt-out of the birthday/anniversary feed)

Revision ID: 0011
Revises: 0010
"""
from migration_sql import run

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

UPGRADE = r"""
ALTER TABLE employee_profiles ADD COLUMN show_celebrations boolean NOT NULL DEFAULT true;
"""

DOWNGRADE = r"""
ALTER TABLE employee_profiles DROP COLUMN IF EXISTS show_celebrations;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
