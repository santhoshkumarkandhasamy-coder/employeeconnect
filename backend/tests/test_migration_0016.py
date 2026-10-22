"""Migration 0016 (trip / SOS removal): content, schema.sql mirror, seed and the model metadata."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "database"


def _load():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location("mig0016", DB / "alembic" / "versions" / "0016_remove_trips_sos.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


def test_revision_chain_and_single_head():
    mod = _load()
    assert (mod.revision, mod.down_revision) == ("0016", "0015")
    revs = [
        re.search(r'^revision = "(\d+)"', p.read_text(encoding="utf-8"), re.M).group(1)
        for p in (DB / "alembic" / "versions").glob("0*.py")
    ]
    assert "0016" in revs and len(revs) == len(set(revs))


def test_upgrade_drops_every_trip_structure_and_nothing_else():
    up = _load().UPGRADE
    for stmt in (
        "DROP TABLE IF EXISTS sos_alerts",
        "DROP TABLE IF EXISTS trip_locations",
        "DROP TABLE IF EXISTS trips",
        "DROP FUNCTION IF EXISTS purge_old_trip_data(integer, integer)",
        "'trip.view_all', 'trip.manage_sos'",
        "'trip.location_interval_s', 'trip.buffer_minutes', 'trip.offline_alert_minutes', 'trip.retention_days'",
        "DROP COLUMN IF EXISTS home_location_enc",
        "DROP COLUMN IF EXISTS home_radius_m",
        "DROP COLUMN IF EXISTS location_consent_at",
        "DROP COLUMN IF EXISTS location_consent_version",
        "DROP CONSTRAINT IF EXISTS ck_employee_profiles_home_radius",
    ):
        assert stmt in up, stmt
    # children before parents; attendance geofence / emergency contact data are untouched
    assert (
        up.index("DROP TABLE IF EXISTS sos_alerts")
        < up.index("DROP TABLE IF EXISTS trip_locations")
        < up.index("DROP TABLE IF EXISTS trips")
    )
    assert "office." not in up and "attendance" not in up and "emergency" not in up


def test_downgrade_recreates_empty_structures():
    down = _load().DOWNGRADE
    for stmt in ("CREATE TABLE trips", "CREATE TABLE trip_locations", "CREATE TABLE sos_alerts", "purge_old_trip_data"):
        assert stmt in down
    assert "account_deactivated" in down and "home_location_enc" in down


def test_schema_sql_is_regenerated_to_0016():
    sql = (DB / "schema.sql").read_text(encoding="utf-8")
    assert "Running upgrade 0015 -> 0016" in sql and "version_num='0016'" in sql
    assert "DROP TABLE IF EXISTS trips" in sql and "DROP COLUMN IF EXISTS location_consent_version" in sql


def test_seed_has_no_trip_permissions_or_settings():
    seed = (DB / "seed.py").read_text(encoding="utf-8")
    assert "trip." not in seed and "sos" not in seed.lower().replace("positions", "")


def test_models_and_routes_have_no_trip_remnants():
    from app.main import create_app
    from app.models import Base, EmployeeProfile

    assert not {"trips", "trip_locations", "sos_alerts"} & set(Base.metadata.tables)
    cols = set(EmployeeProfile.__table__.columns.keys())
    assert not {"home_location_enc", "home_radius_m", "location_consent_at", "location_consent_version"} & cols
    paths = {getattr(r, "path", "") for r in create_app().routes}
    assert not [p for p in paths if "trip" in p or "sos" in p or "location-consent" in p]
