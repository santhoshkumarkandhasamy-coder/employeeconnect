"""Export hardening: permission export.sensitive (docs/reviews/08-export-security.md, M2)

Revision ID: 0015
Revises: 0014

Adds the permission `export.sensitive` (needed for GET /admin/export?include_reasons=true, which adds the decrypted Reason /
Decision Comment columns to the Leave and Permission sheets) and grants it to the `admin` and `hr` roles. Idempotent. Custom
roles never get it automatically. Data only, no schema change.

Downgrade removes the permission (role_permissions rows go with it, ON DELETE CASCADE).
"""

from migration_sql import run

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

UPGRADE = r"""
INSERT INTO permissions (code, module, description) VALUES ('export.sensitive', 'export', 'Include leave/permission reasons and decision comments in exports') ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name IN ('admin', 'hr') AND p.code = 'export.sensitive' ON CONFLICT DO NOTHING;
"""

DOWNGRADE = r"""
DELETE FROM permissions WHERE code = 'export.sensitive';
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
