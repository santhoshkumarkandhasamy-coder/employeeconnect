"""attendance, late_records, holidays

Revision ID: 0002
Revises: 0001
"""
from migration_sql import run

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE holidays (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  date date NOT NULL,
  name text NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_holidays_date UNIQUE (date)
);

CREATE TABLE attendance (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  work_date date NOT NULL,
  shift_id uuid REFERENCES shifts(id) ON DELETE RESTRICT,
  check_in_at timestamptz NOT NULL,
  check_in_loc_enc bytea,
  check_in_distance_m integer,
  check_in_within_fence boolean,
  check_in_selfie_file_id uuid REFERENCES files(id) ON DELETE SET NULL,
  check_out_at timestamptz,
  check_out_loc_enc bytea,
  check_out_selfie_file_id uuid REFERENCES files(id) ON DELETE SET NULL,
  status text NOT NULL DEFAULT 'present',
  minutes_late integer NOT NULL DEFAULT 0,
  worked_minutes integer,
  auto_closed boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_attendance_user_work_date UNIQUE (user_id, work_date),
  CONSTRAINT ck_attendance_status CHECK (status IN ('present','late','half_day')),
  CONSTRAINT ck_attendance_checkout_after_checkin CHECK (check_out_at IS NULL OR check_out_at > check_in_at),
  CONSTRAINT ck_attendance_minutes_late CHECK (minutes_late >= 0),
  CONSTRAINT ck_attendance_worked_minutes CHECK (worked_minutes IS NULL OR worked_minutes >= 0),
  CONSTRAINT ck_attendance_distance CHECK (check_in_distance_m IS NULL OR check_in_distance_m >= 0)
);
CREATE INDEX ix_attendance_work_date ON attendance (work_date);
CREATE INDEX ix_attendance_status ON attendance (status);
CREATE INDEX ix_attendance_shift_id ON attendance (shift_id);
-- open (not yet checked-out) rows, used by the auto-close job
CREATE INDEX ix_attendance_open ON attendance (work_date) WHERE check_out_at IS NULL;

CREATE TABLE late_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  attendance_id uuid NOT NULL REFERENCES attendance(id) ON DELETE CASCADE,
  work_date date NOT NULL,
  shift_start time NOT NULL,
  grace_minutes integer NOT NULL,
  check_in_at timestamptz NOT NULL,
  minutes_late integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_late_records_attendance_id UNIQUE (attendance_id),
  CONSTRAINT uq_late_records_user_work_date UNIQUE (user_id, work_date),
  CONSTRAINT ck_late_records_minutes_late CHECK (minutes_late > 0),
  CONSTRAINT ck_late_records_grace CHECK (grace_minutes >= 0)
);
CREATE INDEX ix_late_records_work_date ON late_records (work_date);

CREATE TRIGGER trg_holidays_updated_at BEFORE UPDATE ON holidays FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_attendance_updated_at BEFORE UPDATE ON attendance FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS late_records;
DROP TABLE IF EXISTS attendance;
DROP TABLE IF EXISTS holidays;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
