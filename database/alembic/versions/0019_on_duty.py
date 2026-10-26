"""On Duty: client-site / branch visits (duty locations, assignments, requests, visits) + attendance.work_mode / check_out_source

Revision ID: 0019
Revises: 0018

Plan: docs/plans/13-on-duty.md section 2 (rules in section 5).

Added
* duty_locations              admin-managed sites (lat/lng/radius in clear: business data, not personal data)
* duty_location_assignments   (location, user) list; ignored while assign_mode = 'all' but kept so switching back restores it
* on_duty_requests            planning / approval requests; purpose_enc and decision_comment_enc are encrypted
* on_duty_visits              one row per visit (check-in/out); locations and note encrypted; at most one OPEN visit per user
* attendance.work_mode        'office' | 'on_duty' | 'mixed' (existing rows are 'office')
* attendance.check_out_source 'office' | 'on_duty'; NULL exactly when check_out_at is NULL (existing closed rows -> 'office')
* permission duty_location.manage (admin, hr)
* settings on_duty.* defaults

Encrypted columns use the same bytea storage as the rest of the schema (the model types EncryptedString / EncryptedJSON).
Downgrade drops the four tables (ALL on-duty data is lost), the attendance columns, the permission and the settings.
"""

from migration_sql import run

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE duty_locations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  address text,
  lat double precision NOT NULL,
  lng double precision NOT NULL,
  radius_m integer NOT NULL,
  assign_mode text NOT NULL DEFAULT 'selected',
  is_active boolean NOT NULL DEFAULT true,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_duty_locations_name CHECK (char_length(btrim(name)) BETWEEN 2 AND 80),
  CONSTRAINT ck_duty_locations_address CHECK (address IS NULL OR char_length(address) <= 300),
  CONSTRAINT ck_duty_locations_lat CHECK (lat BETWEEN -90 AND 90),
  CONSTRAINT ck_duty_locations_lng CHECK (lng BETWEEN -180 AND 180),
  CONSTRAINT ck_duty_locations_radius CHECK (radius_m BETWEEN 10 AND 2000),
  CONSTRAINT ck_duty_locations_assign_mode CHECK (assign_mode IN ('all','selected'))
);
CREATE UNIQUE INDEX uq_duty_locations_name ON duty_locations (lower(btrim(name)));
CREATE INDEX ix_duty_locations_active ON duty_locations (is_active);
CREATE INDEX ix_duty_locations_created_by ON duty_locations (created_by);
CREATE INDEX ix_duty_locations_updated_by ON duty_locations (updated_by);
CREATE TRIGGER trg_duty_locations_updated_at BEFORE UPDATE ON duty_locations FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE duty_location_assignments (
  location_id uuid NOT NULL REFERENCES duty_locations(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_duty_location_assignments PRIMARY KEY (location_id, user_id)
);
CREATE INDEX ix_duty_location_assignments_user ON duty_location_assignments (user_id);
CREATE INDEX ix_duty_location_assignments_created_by ON duty_location_assignments (created_by);

CREATE TABLE on_duty_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  from_date date NOT NULL,
  to_date date NOT NULL,
  location_id uuid REFERENCES duty_locations(id) ON DELETE RESTRICT,
  purpose_enc bytea NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  approver_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  decision_comment_enc bytea,
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_on_duty_requests_dates CHECK (to_date >= from_date),
  CONSTRAINT ck_on_duty_requests_span CHECK (to_date - from_date <= 30),
  CONSTRAINT ck_on_duty_requests_status CHECK (status IN ('pending','approved','rejected','cancelled'))
);
CREATE INDEX ix_on_duty_requests_user_from ON on_duty_requests (user_id, from_date);
CREATE INDEX ix_on_duty_requests_status ON on_duty_requests (status);
CREATE INDEX ix_on_duty_requests_dates ON on_duty_requests (from_date, to_date);
CREATE INDEX ix_on_duty_requests_location_id ON on_duty_requests (location_id);
CREATE INDEX ix_on_duty_requests_approver_id ON on_duty_requests (approver_id);
CREATE TRIGGER trg_on_duty_requests_updated_at BEFORE UPDATE ON on_duty_requests FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE on_duty_visits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  attendance_id uuid NOT NULL REFERENCES attendance(id) ON DELETE CASCADE,
  work_date date NOT NULL,
  location_id uuid NOT NULL REFERENCES duty_locations(id) ON DELETE RESTRICT,
  location_name text NOT NULL,
  request_id uuid REFERENCES on_duty_requests(id) ON DELETE SET NULL,
  check_in_at timestamptz NOT NULL,
  check_in_loc_enc bytea,
  check_in_distance_m integer,
  check_in_accuracy_m real,
  check_in_clock_skew_s integer,
  check_in_mock_location boolean NOT NULL DEFAULT false,
  check_in_selfie_file_id uuid REFERENCES files(id) ON DELETE SET NULL,
  check_out_at timestamptz,
  check_out_loc_enc bytea,
  check_out_distance_m integer,
  check_out_within_fence boolean,
  check_out_accuracy_m real,
  check_out_mock_location boolean NOT NULL DEFAULT false,
  check_out_selfie_file_id uuid REFERENCES files(id) ON DELETE SET NULL,
  note_enc bytea,
  auto_closed boolean NOT NULL DEFAULT false,
  suspicious boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_on_duty_visits_checkout_after_checkin CHECK (check_out_at IS NULL OR check_out_at > check_in_at),
  CONSTRAINT ck_on_duty_visits_in_distance CHECK (check_in_distance_m IS NULL OR check_in_distance_m >= 0),
  CONSTRAINT ck_on_duty_visits_out_distance CHECK (check_out_distance_m IS NULL OR check_out_distance_m >= 0)
);
CREATE INDEX ix_on_duty_visits_user_date ON on_duty_visits (user_id, work_date);
CREATE INDEX ix_on_duty_visits_work_date ON on_duty_visits (work_date);
CREATE INDEX ix_on_duty_visits_location_id ON on_duty_visits (location_id);
CREATE INDEX ix_on_duty_visits_attendance_id ON on_duty_visits (attendance_id);
CREATE INDEX ix_on_duty_visits_request_id ON on_duty_visits (request_id);
CREATE UNIQUE INDEX uq_on_duty_visits_one_open ON on_duty_visits (user_id) WHERE check_out_at IS NULL AND auto_closed = false;
CREATE UNIQUE INDEX uq_on_duty_visits_check_in_selfie ON on_duty_visits (check_in_selfie_file_id) WHERE check_in_selfie_file_id IS NOT NULL;
CREATE UNIQUE INDEX uq_on_duty_visits_check_out_selfie ON on_duty_visits (check_out_selfie_file_id) WHERE check_out_selfie_file_id IS NOT NULL;
CREATE INDEX ix_on_duty_visits_suspicious ON on_duty_visits (work_date) WHERE suspicious;
CREATE TRIGGER trg_on_duty_visits_updated_at BEFORE UPDATE ON on_duty_visits FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE attendance ADD COLUMN work_mode text NOT NULL DEFAULT 'office';
ALTER TABLE attendance ADD COLUMN check_out_source text;
UPDATE attendance SET check_out_source = 'office' WHERE check_out_at IS NOT NULL;
ALTER TABLE attendance ADD CONSTRAINT ck_attendance_work_mode CHECK (work_mode IN ('office','on_duty','mixed'));
ALTER TABLE attendance ADD CONSTRAINT ck_attendance_check_out_source CHECK (check_out_source IN ('office','on_duty'));
ALTER TABLE attendance ADD CONSTRAINT ck_attendance_check_out_source_pair CHECK ((check_out_at IS NULL) = (check_out_source IS NULL));
CREATE INDEX ix_attendance_work_mode ON attendance (work_date) WHERE work_mode <> 'office';

INSERT INTO permissions (code, module, description) VALUES ('duty_location.manage', 'duty_location', 'Manage on-duty locations and assignments') ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name IN ('admin', 'hr') AND p.code = 'duty_location.manage' ON CONFLICT DO NOTHING;

INSERT INTO settings (key, value) VALUES ('on_duty.enabled', 'true'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('on_duty.approval_required', 'false'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('on_duty.apply_late_rule', 'false'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('on_duty.checkout_geofence', 'false'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('on_duty.selfie_required', 'false'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('on_duty.max_visits_per_day', '10'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('on_duty.request_backdate_days', '0'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('on_duty.request_max_advance_days', '60'::jsonb) ON CONFLICT (key) DO NOTHING;
"""

DOWNGRADE = r"""
DELETE FROM settings WHERE key LIKE 'on_duty.%%';
DELETE FROM permissions WHERE code = 'duty_location.manage';

DROP INDEX IF EXISTS ix_attendance_work_mode;
ALTER TABLE attendance DROP CONSTRAINT IF EXISTS ck_attendance_check_out_source_pair;
ALTER TABLE attendance DROP CONSTRAINT IF EXISTS ck_attendance_check_out_source;
ALTER TABLE attendance DROP CONSTRAINT IF EXISTS ck_attendance_work_mode;
ALTER TABLE attendance DROP COLUMN IF EXISTS check_out_source;
ALTER TABLE attendance DROP COLUMN IF EXISTS work_mode;

DROP TABLE IF EXISTS on_duty_visits;
DROP TABLE IF EXISTS on_duty_requests;
DROP TABLE IF EXISTS duty_location_assignments;
DROP TABLE IF EXISTS duty_locations;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
