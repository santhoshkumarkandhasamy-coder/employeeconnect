"""Trips security hardening (docs/reviews/05-trips-security.md)

Revision ID: 0012
Revises: 0011

* trips.last_received_at        server receive time of the last location batch (offline detection no longer trusts client clocks)
* trips.mock_location_seen      true once any point of the trip was flagged as a mock location
* trip_locations.is_mock        per-point mock-location flag sent by the device
* sos_alerts.message            plaintext column DROPPED, replaced by message_enc (Fernet, like loc_enc). Existing plaintext
                                messages cannot be encrypted in SQL and are discarded (free text can contain a location).
* sos_alerts.repeat_count / last_alert_at / contact_notified   repeat SOS of one trip are merged into one row
* purge_old_trip_data(days, batch_size): now works in batches (call it in a loop, committing between calls, until every
  count is below batch_size) and also nulls sos_alerts.message_enc. The one-argument version is dropped.
"""
from migration_sql import run

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

UPGRADE = r"""
ALTER TABLE trips ADD COLUMN last_received_at timestamptz;
ALTER TABLE trips ADD COLUMN mock_location_seen boolean NOT NULL DEFAULT false;
ALTER TABLE trip_locations ADD COLUMN is_mock boolean NOT NULL DEFAULT false;
ALTER TABLE sos_alerts DROP COLUMN message;
ALTER TABLE sos_alerts ADD COLUMN message_enc bytea;
ALTER TABLE sos_alerts ADD COLUMN repeat_count integer NOT NULL DEFAULT 0;
ALTER TABLE sos_alerts ADD COLUMN last_alert_at timestamptz;
ALTER TABLE sos_alerts ADD COLUMN contact_notified boolean NOT NULL DEFAULT false;
ALTER TABLE sos_alerts ADD CONSTRAINT ck_sos_alerts_repeat_count CHECK (repeat_count >= 0);

DROP FUNCTION IF EXISTS purge_old_trip_data(integer);

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
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS purge_old_trip_data(integer, integer);

CREATE FUNCTION purge_old_trip_data(retention_days integer)
RETURNS TABLE (locations_deleted bigint, trips_scrubbed bigint, sos_scrubbed bigint)
LANGUAGE plpgsql AS $$
DECLARE
  cutoff timestamptz := now() - make_interval(days => retention_days);
  n_loc bigint := 0;
  n_trip bigint := 0;
  n_sos bigint := 0;
BEGIN
  IF retention_days IS NULL OR retention_days <= 0 THEN
    RETURN QUERY SELECT 0::bigint, 0::bigint, 0::bigint;
    RETURN;
  END IF;
  DELETE FROM trip_locations tl USING trips t
    WHERE tl.trip_id = t.id AND t.status <> 'active' AND t.started_at < cutoff;
  GET DIAGNOSTICS n_loc = ROW_COUNT;
  UPDATE trips SET start_loc_enc = NULL, home_loc_enc = NULL, last_loc_enc = NULL
    WHERE status <> 'active' AND started_at < cutoff
      AND (start_loc_enc IS NOT NULL OR home_loc_enc IS NOT NULL OR last_loc_enc IS NOT NULL);
  GET DIAGNOSTICS n_trip = ROW_COUNT;
  UPDATE sos_alerts SET loc_enc = NULL WHERE created_at < cutoff AND loc_enc IS NOT NULL;
  GET DIAGNOSTICS n_sos = ROW_COUNT;
  RETURN QUERY SELECT n_loc, n_trip, n_sos;
END
$$;

ALTER TABLE sos_alerts DROP CONSTRAINT IF EXISTS ck_sos_alerts_repeat_count;
ALTER TABLE sos_alerts DROP COLUMN IF EXISTS contact_notified;
ALTER TABLE sos_alerts DROP COLUMN IF EXISTS last_alert_at;
ALTER TABLE sos_alerts DROP COLUMN IF EXISTS repeat_count;
ALTER TABLE sos_alerts DROP COLUMN IF EXISTS message_enc;
ALTER TABLE sos_alerts ADD COLUMN message text;
ALTER TABLE trip_locations DROP COLUMN IF EXISTS is_mock;
ALTER TABLE trips DROP COLUMN IF EXISTS mock_location_seen;
ALTER TABLE trips DROP COLUMN IF EXISTS last_received_at;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
