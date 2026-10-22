"""auth hardening: users.token_valid_after, refresh_tokens.family_started_at

Revision ID: 0007
Revises: 0006
"""
from migration_sql import run

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UPGRADE = r"""
ALTER TABLE users ADD COLUMN token_valid_after timestamptz;
ALTER TABLE refresh_tokens ADD COLUMN family_started_at timestamptz;
UPDATE refresh_tokens rt SET family_started_at = COALESCE((SELECT min(f.issued_at) FROM refresh_tokens f WHERE f.family_id = rt.family_id), rt.issued_at);
ALTER TABLE refresh_tokens ALTER COLUMN family_started_at SET DEFAULT now();
ALTER TABLE refresh_tokens ALTER COLUMN family_started_at SET NOT NULL;
"""

DOWNGRADE = r"""
ALTER TABLE refresh_tokens DROP COLUMN family_started_at;
ALTER TABLE users DROP COLUMN token_valid_after;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
