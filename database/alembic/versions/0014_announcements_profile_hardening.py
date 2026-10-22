"""Announcements / profile security hardening (docs/reviews/07-announcements-profile-security.md)

Revision ID: 0014
Revises: 0013

* employee_profiles.show_celebrations   (M2) default becomes false (opt-in). EXISTING ROWS ARE RESET TO false: nobody was
                                         ever asked, so nobody has consented to publishing their birthday/anniversary.
* announcements.updated_by              (M4) who last edited the row (NULL = never edited).
* announcements.version                 (L2) optimistic-concurrency counter, +1 on every PUT.
* announcements.high_priority_pushed    (M1) a high-priority push was already sent for this announcement (at most once).

Downgrade restores the old default (true) but cannot restore the previous per-row values.
"""
from migration_sql import run

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

UPGRADE = r"""
ALTER TABLE employee_profiles ALTER COLUMN show_celebrations SET DEFAULT false;
UPDATE employee_profiles SET show_celebrations = false WHERE show_celebrations;

ALTER TABLE announcements ADD COLUMN updated_by uuid REFERENCES users(id) ON DELETE RESTRICT;
ALTER TABLE announcements ADD COLUMN version integer NOT NULL DEFAULT 1;
ALTER TABLE announcements ADD COLUMN high_priority_pushed boolean NOT NULL DEFAULT false;
CREATE INDEX ix_announcements_updated_by ON announcements (updated_by);
"""

DOWNGRADE = r"""
DROP INDEX IF EXISTS ix_announcements_updated_by;
ALTER TABLE announcements DROP COLUMN IF EXISTS high_priority_pushed;
ALTER TABLE announcements DROP COLUMN IF EXISTS version;
ALTER TABLE announcements DROP COLUMN IF EXISTS updated_by;
ALTER TABLE employee_profiles ALTER COLUMN show_celebrations SET DEFAULT true;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
