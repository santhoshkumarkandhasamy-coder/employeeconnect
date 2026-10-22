"""trips, trip_locations, sos_alerts

Revision ID: 0006
Revises: 0005
"""
from migration_sql import run

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

UPGRADE = r"""
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
  CONSTRAINT ck_trips_status CHECK (status IN ('active','completed','cancelled')),
  CONSTRAINT ck_trips_end_reason CHECK (end_reason IS NULL OR end_reason IN ('reached_home','auto_geofence','cancelled','admin_closed','expired')),
  CONSTRAINT ck_trips_eta CHECK (eta_minutes BETWEEN 5 AND 480),
  CONSTRAINT ck_trips_alert_state CHECK (alert_state IN ('none','overdue','offline','sos')),
  CONSTRAINT ck_trips_home_radius CHECK (home_radius_m BETWEEN 50 AND 1000),
  CONSTRAINT ck_trips_battery CHECK (last_battery_pct IS NULL OR last_battery_pct BETWEEN 0 AND 100),
  CONSTRAINT ck_trips_ended_consistency CHECK (
    (status = 'active' AND ended_at IS NULL AND end_reason IS NULL) OR
    (status <> 'active' AND ended_at IS NOT NULL AND end_reason IS NOT NULL)),
  CONSTRAINT ck_trips_ended_after_start CHECK (ended_at IS NULL OR ended_at >= started_at)
);
-- one active trip per user, enforced by the database
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
  CONSTRAINT ck_trip_locations_battery CHECK (battery_pct IS NULL OR battery_pct BETWEEN 0 AND 100),
  CONSTRAINT ck_trip_locations_accuracy CHECK (accuracy_m IS NULL OR accuracy_m >= 0)
);
CREATE INDEX ix_trip_locations_trip_recorded ON trip_locations (trip_id, recorded_at);

CREATE TABLE sos_alerts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  trip_id uuid REFERENCES trips(id) ON DELETE SET NULL,
  loc_enc bytea,
  message text,
  status text NOT NULL DEFAULT 'open',
  acknowledged_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  acknowledged_at timestamptz,
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_sos_alerts_status CHECK (status IN ('open','acknowledged','resolved'))
);
CREATE INDEX ix_sos_alerts_status ON sos_alerts (status);
CREATE INDEX ix_sos_alerts_user_id ON sos_alerts (user_id);
CREATE INDEX ix_sos_alerts_trip_id ON sos_alerts (trip_id);
CREATE INDEX ix_sos_alerts_acknowledged_by ON sos_alerts (acknowledged_by);

CREATE TRIGGER trg_trips_updated_at BEFORE UPDATE ON trips FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS sos_alerts;
DROP TABLE IF EXISTS trip_locations;
DROP TABLE IF EXISTS trips;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
