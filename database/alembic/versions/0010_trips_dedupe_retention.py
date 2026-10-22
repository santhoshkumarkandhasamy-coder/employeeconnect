"""trip_locations dedupe key + trip retention helper

Revision ID: 0010
Revises: 0009

* unique (trip_id, recorded_at) on trip_locations (replaces the plain ix_trip_locations_trip_recorded; the unique index
  also serves the (trip_id, recorded_at) range scans). Existing duplicates are removed first (keeps the lowest id).
  Ingest should use INSERT ... ON CONFLICT (trip_id, recorded_at) DO NOTHING.
* purge_old_trip_data(days): retention helper (docs/plans/08-trips.md). For trips that are NOT active and started more
  than `days` days ago it deletes their trip_locations and nulls the encrypted coordinates on the trip summary row
  (start/home/last). It also nulls sos_alerts.loc_enc older than `days`. The trip and SOS rows themselves are kept.
  Returns (locations_deleted, trips_scrubbed, sos_scrubbed). days <= 0 is a no-op (retention off).
"""
from migration_sql import run

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

UPGRADE = r"""
DELETE FROM trip_locations a USING trip_locations b
  WHERE a.trip_id = b.trip_id AND a.recorded_at = b.recorded_at AND a.id > b.id;
DROP INDEX ix_trip_locations_trip_recorded;
CREATE UNIQUE INDEX uq_trip_locations_trip_recorded ON trip_locations (trip_id, recorded_at);

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
"""

DOWNGRADE = r"""
DROP FUNCTION IF EXISTS purge_old_trip_data(integer);
DROP INDEX IF EXISTS uq_trip_locations_trip_recorded;
CREATE INDEX ix_trip_locations_trip_recorded ON trip_locations (trip_id, recorded_at);
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
