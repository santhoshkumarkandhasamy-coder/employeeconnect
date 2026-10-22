"""Remove the trips / Safe Reach Home / SOS / live-location feature

Revision ID: 0016
Revises: 0015

DESTRUCTIVE and irreversible for data: every trip, trip location point and SOS alert is deleted. Take a backup first.

Dropped
* tables sos_alerts, trip_locations, trips (0006, 0010, 0012, 0013) with their indexes, constraints and trigger
* function purge_old_trip_data(integer, integer) (0010, 0012)
* permissions trip.view_all and trip.manage_sos; role_permissions rows go with them (ON DELETE CASCADE)
* settings rows trip.location_interval_s, trip.buffer_minutes, trip.offline_alert_minutes, trip.retention_days
* employee_profiles columns home_location_enc, home_radius_m (+ ck_employee_profiles_home_radius), location_consent_at,
  location_consent_version. They existed only for trips (home snapshot for a trip, consent to track).

Kept on purpose
* office geofence settings (office.*) and attendance location / mock-location columns: attendance uses them
* emergency contact columns of employee_profiles: part of the personal profile, not only used by SOS
* audit_logs rows written by earlier trip actions (history is never rewritten)

Downgrade recreates the EMPTY structures (tables, columns, function, permissions granted to the admin role, default settings);
the deleted trip / SOS / home location / consent data cannot be restored.
"""

from migration_sql import run

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

UPGRADE = r"""
DROP TABLE IF EXISTS sos_alerts;
DROP TABLE IF EXISTS trip_locations;
DROP TABLE IF EXISTS trips;
DROP FUNCTION IF EXISTS purge_old_trip_data(integer, integer);
DROP FUNCTION IF EXISTS purge_old_trip_data(integer);
DELETE FROM permissions WHERE code IN ('trip.view_all', 'trip.manage_sos');
DELETE FROM settings WHERE key IN ('trip.location_interval_s', 'trip.buffer_minutes', 'trip.offline_alert_minutes', 'trip.retention_days');
ALTER TABLE employee_profiles DROP CONSTRAINT IF EXISTS ck_employee_profiles_home_radius;
ALTER TABLE employee_profiles DROP COLUMN IF EXISTS home_location_enc;
ALTER TABLE employee_profiles DROP COLUMN IF EXISTS home_radius_m;
ALTER TABLE employee_profiles DROP COLUMN IF EXISTS location_consent_at;
ALTER TABLE employee_profiles DROP COLUMN IF EXISTS location_consent_version;
"""

DOWNGRADE = r"""
ALTER TABLE employee_profiles ADD COLUMN home_location_enc bytea;
ALTER TABLE employee_profiles ADD COLUMN home_radius_m integer NOT NULL DEFAULT 150;
ALTER TABLE employee_profiles ADD COLUMN location_consent_at timestamptz;
ALTER TABLE employee_profiles ADD COLUMN location_consent_version text;
ALTER TABLE employee_profiles ADD CONSTRAINT ck_employee_profiles_home_radius CHECK (home_radius_m BETWEEN 50 AND 1000);

CREATE TABLE trips (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  status text NOT NULL DEFAULT 'active',
  end_reason text,
  started_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz,
  eta_minutes integer NOT NULL,
  expected_arrival_at timestamptz NOT NULL,
  start_loc_enc bytea,
  home_loc_enc bytea,
  home_radius_m integer NOT NULL DEFAULT 150,
  last_loc_enc bytea,
  last_location_at timestamptz,
  last_battery_pct smallint,
  alert_state text NOT NULL DEFAULT 'none',
  overdue_alerted_at timestamptz,
  offline_alerted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_received_at timestamptz,
  mock_location_seen boolean NOT NULL DEFAULT false,
  CONSTRAINT ck_trips_status CHECK (status IN ('active','completed','cancelled')),
  CONSTRAINT ck_trips_end_reason CHECK (end_reason IS NULL OR end_reason IN ('reached_home','auto_geofence','cancelled','admin_closed','expired','account_deactivated')),
  CONSTRAINT ck_trips_eta CHECK (eta_minutes BETWEEN 5 AND 480),
  CONSTRAINT ck_trips_alert_state CHECK (alert_state IN ('none','overdue','offline','sos')),
  CONSTRAINT ck_trips_home_radius CHECK (home_radius_m BETWEEN 50 AND 1000),
  CONSTRAINT ck_trips_battery CHECK (last_battery_pct IS NULL OR last_battery_pct BETWEEN 0 AND 100),
  CONSTRAINT ck_trips_ended_consistency CHECK (
    (status = 'active' AND ended_at IS NULL AND end_reason IS NULL) OR
    (status <> 'active' AND ended_at IS NOT NULL AND end_reason IS NOT NULL)),
  CONSTRAINT ck_trips_ended_after_start CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE UNIQUE INDEX uq_trips_one_active_per_user ON trips (user_id) WHERE status = 'active';
CREATE INDEX ix_trips_status ON trips (status);
CREATE INDEX ix_trips_started_at ON trips (started_at);
CREATE INDEX ix_trips_user_started ON trips (user_id, started_at DESC);

CREATE TABLE trip_locations (
  id bigserial PRIMARY KEY,
  trip_id uuid NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  loc_enc bytea NOT NULL,
  accuracy_m real,
  recorded_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  battery_pct smallint,
  is_mock boolean NOT NULL DEFAULT false,
  CONSTRAINT ck_trip_locations_battery CHECK (battery_pct IS NULL OR battery_pct BETWEEN 0 AND 100),
  CONSTRAINT ck_trip_locations_accuracy CHECK (accuracy_m IS NULL OR accuracy_m >= 0)
);
CREATE UNIQUE INDEX uq_trip_locations_trip_recorded ON trip_locations (trip_id, recorded_at);

CREATE TABLE sos_alerts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  trip_id uuid REFERENCES trips(id) ON DELETE SET NULL,
  loc_enc bytea,
  message_enc bytea,
  status text NOT NULL DEFAULT 'open',
  acknowledged_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  acknowledged_at timestamptz,
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  repeat_count integer NOT NULL DEFAULT 0,
  last_alert_at timestamptz,
  contact_notified boolean NOT NULL DEFAULT false,
  CONSTRAINT ck_sos_alerts_status CHECK (status IN ('open','acknowledged','resolved')),
  CONSTRAINT ck_sos_alerts_repeat_count CHECK (repeat_count >= 0)
);
CREATE INDEX ix_sos_alerts_status ON sos_alerts (status);
CREATE INDEX ix_sos_alerts_user_id ON sos_alerts (user_id);
CREATE INDEX ix_sos_alerts_trip_id ON sos_alerts (trip_id);
CREATE INDEX ix_sos_alerts_acknowledged_by ON sos_alerts (acknowledged_by);

CREATE TRIGGER trg_trips_updated_at BEFORE UPDATE ON trips FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION purge_old_trip_data(retention_days integer, batch_size integer DEFAULT 10000)
RETURNS TABLE (locations_deleted bigint, trips_scrubbed bigint, sos_scrubbed bigint)
LANGUAGE plpgsql AS $$
DECLARE
  cutoff timestamptz := now() - make_interval(days => retention_days);
  n_loc bigint := 0;
  n_trip bigint := 0;
  n_sos bigint := 0;
BEGIN
  IF retention_days IS NULL OR retention_days <= 0 OR batch_size IS NULL OR batch_size <= 0 THEN
    RETURN QUERY SELECT 0::bigint, 0::bigint, 0::bigint;
    RETURN;
  END IF;
  DELETE FROM trip_locations WHERE id IN (
    SELECT tl.id FROM trip_locations tl JOIN trips t ON t.id = tl.trip_id
      WHERE t.status <> 'active' AND t.started_at < cutoff LIMIT batch_size);
  GET DIAGNOSTICS n_loc = ROW_COUNT;
  UPDATE trips SET start_loc_enc = NULL, home_loc_enc = NULL, last_loc_enc = NULL WHERE id IN (
    SELECT id FROM trips
      WHERE status <> 'active' AND started_at < cutoff
        AND (start_loc_enc IS NOT NULL OR home_loc_enc IS NOT NULL OR last_loc_enc IS NOT NULL)
      LIMIT batch_size);
  GET DIAGNOSTICS n_trip = ROW_COUNT;
  UPDATE sos_alerts SET loc_enc = NULL, message_enc = NULL WHERE id IN (
    SELECT id FROM sos_alerts
      WHERE created_at < cutoff AND (loc_enc IS NOT NULL OR message_enc IS NOT NULL)
      LIMIT batch_size);
  GET DIAGNOSTICS n_sos = ROW_COUNT;
  RETURN QUERY SELECT n_loc, n_trip, n_sos;
END
$$;

INSERT INTO permissions (code, module, description) VALUES ('trip.view_all', 'trip', 'View live trips and trip history') ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (code, module, description) VALUES ('trip.manage_sos', 'trip', 'Acknowledge/resolve SOS alerts') ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name = 'admin' AND p.code IN ('trip.view_all', 'trip.manage_sos') ON CONFLICT DO NOTHING;
INSERT INTO settings (key, value) VALUES ('trip.location_interval_s', '30'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('trip.buffer_minutes', '15'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('trip.offline_alert_minutes', '5'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('trip.retention_days', '90'::jsonb) ON CONFLICT (key) DO NOTHING;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
