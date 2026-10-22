"""attendance anti-spoofing evidence + one-use selfies

Revision ID: 0008
Revises: 0007

* attendance.check_in_accuracy_m / check_in_clock_skew_s / check_in_mock_location / check_out_mock_location / suspicious
* a selfie file may back only one punch: partial UNIQUE indexes on the two selfie columns. Existing duplicates are
  resolved first by keeping the earliest punch's reference and NULLing the later ones (the file rows are untouched).
"""
from migration_sql import run

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

UPGRADE = r"""
ALTER TABLE attendance ADD COLUMN check_in_accuracy_m real;
ALTER TABLE attendance ADD COLUMN check_in_clock_skew_s integer;
ALTER TABLE attendance ADD COLUMN check_in_mock_location boolean NOT NULL DEFAULT false;
ALTER TABLE attendance ADD COLUMN check_out_mock_location boolean NOT NULL DEFAULT false;
ALTER TABLE attendance ADD COLUMN suspicious boolean NOT NULL DEFAULT false;
UPDATE attendance a SET check_in_selfie_file_id = NULL WHERE a.check_in_selfie_file_id IS NOT NULL AND EXISTS (SELECT 1 FROM attendance b WHERE b.check_in_selfie_file_id = a.check_in_selfie_file_id AND (b.check_in_at, b.id) < (a.check_in_at, a.id));
UPDATE attendance a SET check_out_selfie_file_id = NULL WHERE a.check_out_selfie_file_id IS NOT NULL AND EXISTS (SELECT 1 FROM attendance b WHERE b.check_out_selfie_file_id = a.check_out_selfie_file_id AND (b.check_in_at, b.id) < (a.check_in_at, a.id));
CREATE UNIQUE INDEX uq_attendance_check_in_selfie ON attendance (check_in_selfie_file_id) WHERE check_in_selfie_file_id IS NOT NULL;
CREATE UNIQUE INDEX uq_attendance_check_out_selfie ON attendance (check_out_selfie_file_id) WHERE check_out_selfie_file_id IS NOT NULL;
CREATE INDEX ix_attendance_suspicious ON attendance (work_date) WHERE suspicious;
"""

DOWNGRADE = r"""
DROP INDEX ix_attendance_suspicious;
DROP INDEX uq_attendance_check_out_selfie;
DROP INDEX uq_attendance_check_in_selfie;
ALTER TABLE attendance DROP COLUMN suspicious;
ALTER TABLE attendance DROP COLUMN check_out_mock_location;
ALTER TABLE attendance DROP COLUMN check_in_mock_location;
ALTER TABLE attendance DROP COLUMN check_in_clock_skew_s;
ALTER TABLE attendance DROP COLUMN check_in_accuracy_m;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
