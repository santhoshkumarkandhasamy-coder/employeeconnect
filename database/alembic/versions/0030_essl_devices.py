"""eSSL / ZK attendance devices: devices, device users, raw punches, outcomes, sync history, attendance source columns (plan 18, step 1)

Revision ID: 0030
Revises: 0029

Plan: docs/plans/18-essl-devices.md section 3; contract: docs/api-contract.md section 21.

Added
* devices                 the office machines (ip inet, port, transport, comm key ENCRYPTED, pinned serial, clock status, failure counters).
                          UNIQUE (ip, port) and UNIQUE (lower(name)). Delete is RESTRICTed by punches / users / attendance (disable instead).
* device_syncs            history and job status of schedule / manual / reconcile / test runs. device_id NULL for an unsaved test.
                          Deleting a device cascades its sync history (history only, no evidence).
* device_users            one row per user slot seen on a device. name_enc and card_enc ENCRYPTED. The device PIN is never stored. NO
                          fingerprint / face / palm template column exists anywhere (a test guards the names). CHECK: user_id IS NOT NULL
                          iff match_method IN ('auto','manual'). DELETE stays granted to app_user (the service clears a device's user list).
* device_punches          raw, append-only. UNIQUE (device_id, device_user_id, device_time) is the idempotency key (raw device clock).
                          Trigger device_punches_block_mutation(): UPDATE and TRUNCATE always raise; DELETE only for rows whose punch_at is older
                          than 90 days (retention job, DELETE stays granted to app_user). One narrow UPDATE is let through: the foreign-key
                          action ON DELETE SET NULL on sync_id (the sync-history retention job deletes old device_syncs rows while the punches
                          live 3 years). That UPDATE runs inside the RI trigger (pg_trigger_depth() > 1) and may change nothing but sync_id -> NULL.
                          REVOKE TRUNCATE from app_user. Fk to devices is RESTRICT.
* device_punch_outcomes   mutable result of the latest recompute, one row per punch (fk ON DELETE CASCADE: purging a punch purges its outcome).
* attendance              check_in_source (NOT NULL default 'app') / check_out_source, check_in_device_id / check_out_device_id (fk devices
                          RESTRICT), app_check_in_at / app_check_out_at (the phone punch times, so a recompute is repeatable).
                          Backfill: non-manual rows app_* = check_in_at / check_out_at; manual rows are 'manual' (check_out_source only with a
                          check-out); real app punches on manual rows keep app_* NULL (unknown).
* permissions             device.view, device.map (admin, hr), device.manage (admin).
* settings                the 15 device.* keys of contract section 21.6 (defaults only; the service validates the ranges).

Downgrade REFUSES while any device_punches row or any attendance row sourced from a device exists (evidence must not be dropped silently).
Otherwise it drops the attendance columns, the tables, the trigger function, the permissions (role_permissions cascade) and the settings.
"""

from migration_sql import run

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_SETTINGS = (
    ("device.sync_interval_minutes", "5"),
    ("device.sync_window_start", '"05:00"'),
    ("device.sync_window_end", '"23:30"'),
    ("device.off_window_interval_minutes", "60"),
    ("device.apply_window_days", "7"),
    ("device.checkout_min_gap_minutes", "5"),
    ("device.drift_tolerance_s", "120"),
    ("device.drift_invalid_s", "43200"),
    ("device.future_tolerance_s", "300"),
    ("device.match_prefix", '""'),
    ("device.apply_on_on_duty_days", "false"),
    ("device.use_punch_state", "false"),
    ("device.push_on_punch", "false"),
    ("device.punch_retention_days", "1095"),
    ("device.sync_history_retention_days", "90"),
)

_PERMISSIONS = (
    ("device.view", "View attendance devices, unmatched device users, punch log and sync history", "('admin', 'hr')"),
    ("device.map", "Map or ignore device users and run Sync now", "('admin', 'hr')"),
    ("device.manage", "Add, edit, enable and delete attendance devices, comm key, test connection", "('admin')"),
)

UPGRADE = (
    r"""
-- ---------------------------------------------------------------- devices
CREATE TABLE devices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  ip inet NOT NULL,
  port integer NOT NULL DEFAULT 4370,
  transport text NOT NULL DEFAULT 'tcp',
  comm_key_enc bytea,
  enabled boolean NOT NULL DEFAULT true,
  location_label text,
  timezone text,
  clock_correction text NOT NULL DEFAULT 'auto',
  ingest_from date NOT NULL,
  serial_number text,
  model text,
  firmware text,
  platform text,
  last_clock_offset_s integer,
  clock_status text,
  last_log_count integer,
  last_user_count integer,
  last_users_read_at timestamptz,
  last_sync_at timestamptz,
  last_success_at timestamptz,
  last_error_code text,
  last_error_at timestamptz,
  consecutive_failures integer NOT NULL DEFAULT 0,
  circuit_open_until timestamptz,
  last_alert_at timestamptz,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_devices_ip_port UNIQUE (ip, port),
  CONSTRAINT ck_devices_name CHECK (char_length(name) BETWEEN 1 AND 60),
  CONSTRAINT ck_devices_port CHECK (port BETWEEN 1 AND 65535),
  CONSTRAINT ck_devices_transport CHECK (transport IN ('tcp','udp')),
  CONSTRAINT ck_devices_location_label CHECK (location_label IS NULL OR char_length(location_label) <= 80),
  CONSTRAINT ck_devices_clock_correction CHECK (clock_correction IN ('auto','off')),
  CONSTRAINT ck_devices_clock_status CHECK (clock_status IS NULL OR clock_status IN ('ok','drift','invalid')),
  CONSTRAINT ck_devices_failures CHECK (consecutive_failures >= 0),
  CONSTRAINT ck_devices_counts CHECK ((last_log_count IS NULL OR last_log_count >= 0) AND (last_user_count IS NULL OR last_user_count >= 0))
);
CREATE UNIQUE INDEX uq_devices_name_lower ON devices (lower(name));
CREATE INDEX ix_devices_created_by ON devices (created_by);
CREATE INDEX ix_devices_enabled ON devices (enabled);
CREATE TRIGGER trg_devices_updated_at BEFORE UPDATE ON devices FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- sync history
CREATE TABLE device_syncs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id uuid REFERENCES devices(id) ON DELETE CASCADE,
  kind text NOT NULL,
  requested_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  status text NOT NULL DEFAULT 'queued',
  error_code text,
  queued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  records_on_device integer,
  users_on_device integer,
  fetched integer,
  inserted integer,
  quarantined integer,
  days_recomputed integer,
  clock_offset_s integer,
  result jsonb,
  CONSTRAINT ck_device_syncs_kind CHECK (kind IN ('schedule','manual','reconcile','test')),
  CONSTRAINT ck_device_syncs_status CHECK (status IN ('queued','running','ok','failed','skipped')),
  CONSTRAINT ck_device_syncs_counts CHECK (
    (records_on_device IS NULL OR records_on_device >= 0) AND (users_on_device IS NULL OR users_on_device >= 0)
    AND (fetched IS NULL OR fetched >= 0) AND (inserted IS NULL OR inserted >= 0)
    AND (quarantined IS NULL OR quarantined >= 0) AND (days_recomputed IS NULL OR days_recomputed >= 0)),
  CONSTRAINT ck_device_syncs_result CHECK (result IS NULL OR kind = 'test')
);
CREATE INDEX ix_device_syncs_device_queued ON device_syncs (device_id, queued_at DESC);
CREATE INDEX ix_device_syncs_requested_by ON device_syncs (requested_by);
CREATE INDEX ix_device_syncs_open ON device_syncs (status) WHERE status IN ('queued','running');
CREATE INDEX ix_device_syncs_queued_at ON device_syncs (queued_at);

-- ---------------------------------------------------------------- device users
CREATE TABLE device_users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id uuid NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
  device_uid integer,
  device_user_id text NOT NULL,
  name_enc bytea,
  card_enc bytea,
  is_device_admin boolean NOT NULL DEFAULT false,
  user_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  match_method text NOT NULL DEFAULT 'unmatched',
  matched_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  matched_at timestamptz,
  present_on_device boolean NOT NULL DEFAULT true,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_device_users_device_user UNIQUE (device_id, device_user_id),
  CONSTRAINT ck_device_users_user_id CHECK (char_length(device_user_id) BETWEEN 1 AND 24),
  CONSTRAINT ck_device_users_match_method CHECK (match_method IN ('auto','manual','unmatched','ignored')),
  CONSTRAINT ck_device_users_mapped CHECK ((user_id IS NOT NULL) = (match_method IN ('auto','manual')))
);
CREATE INDEX ix_device_users_user_id ON device_users (user_id);
CREATE INDEX ix_device_users_matched_by ON device_users (matched_by);
CREATE INDEX ix_device_users_unmatched ON device_users (device_id, device_user_id) WHERE match_method = 'unmatched';
CREATE TRIGGER trg_device_users_updated_at BEFORE UPDATE ON device_users FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- raw punches (append-only)
CREATE TABLE device_punches (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  device_id uuid NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
  device_user_id text NOT NULL,
  device_uid integer,
  device_time timestamp NOT NULL,
  punch_at timestamptz NOT NULL,
  offset_applied_s integer NOT NULL DEFAULT 0,
  state smallint,
  verify_mode smallint,
  quarantine text,
  sync_id uuid REFERENCES device_syncs(id) ON DELETE SET NULL,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_device_punches_device_user_time UNIQUE (device_id, device_user_id, device_time),
  CONSTRAINT ck_device_punches_user_id CHECK (char_length(device_user_id) BETWEEN 1 AND 24),
  CONSTRAINT ck_device_punches_quarantine CHECK (quarantine IS NULL OR quarantine IN ('clock_invalid','future'))
);
CREATE INDEX ix_device_punches_user_punch_at ON device_punches (device_user_id, punch_at);
CREATE INDEX ix_device_punches_punch_at ON device_punches (punch_at);
CREATE INDEX ix_device_punches_sync_id ON device_punches (sync_id) WHERE sync_id IS NOT NULL;

CREATE FUNCTION device_punches_block_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.punch_at < now() - interval '90 days' THEN
      RETURN OLD;  -- retention job only; younger rows are evidence
    END IF;
    RAISE EXCEPTION 'device_punches is append-only: rows younger than 90 days cannot be deleted' USING ERRCODE = 'insufficient_privilege', DETAIL = TG_OP;
  ELSIF TG_OP = 'UPDATE' THEN
    -- the only allowed change: the foreign-key action ON DELETE SET NULL on sync_id (runs inside the RI trigger, so the depth is above 1)
    IF pg_trigger_depth() > 1 AND NEW.sync_id IS NULL AND OLD.sync_id IS NOT NULL
       AND (to_jsonb(NEW) - 'sync_id') = (to_jsonb(OLD) - 'sync_id') THEN
      RETURN NEW;
    END IF;
  END IF;
  RAISE EXCEPTION 'device_punches is append-only' USING ERRCODE = 'insufficient_privilege', DETAIL = TG_OP;
END
$$;
CREATE TRIGGER device_punches_append_only BEFORE UPDATE OR DELETE ON device_punches
  FOR EACH ROW EXECUTE FUNCTION device_punches_block_mutation();
CREATE TRIGGER device_punches_no_truncate BEFORE TRUNCATE ON device_punches
  FOR EACH STATEMENT EXECUTE FUNCTION device_punches_block_mutation();

-- ---------------------------------------------------------------- outcomes of the latest recompute
CREATE TABLE device_punch_outcomes (
  punch_id bigint PRIMARY KEY REFERENCES device_punches(id) ON DELETE CASCADE,
  user_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  work_date date,
  outcome text NOT NULL,
  reason text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_device_punch_outcomes_outcome CHECK (outcome IN ('check_in','check_out','extra','unmatched','ignored','quarantined','not_applied')),
  CONSTRAINT ck_device_punch_outcomes_reason CHECK (reason IS NULL OR reason IN ('on_leave','on_duty','no_shift','inactive_user','manual_entry','outside_window','payroll_locked','mapping_ignored','clock_invalid','future','duplicate'))
);
CREATE INDEX ix_device_punch_outcomes_user_work_date ON device_punch_outcomes (user_id, work_date);
CREATE INDEX ix_device_punch_outcomes_work_date ON device_punch_outcomes (work_date);
CREATE INDEX ix_device_punch_outcomes_outcome ON device_punch_outcomes (outcome);
CREATE TRIGGER trg_device_punch_outcomes_updated_at BEFORE UPDATE ON device_punch_outcomes FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------- attendance columns
ALTER TABLE attendance ADD COLUMN check_in_source text NOT NULL DEFAULT 'app';
ALTER TABLE attendance ADD COLUMN check_out_source text;
ALTER TABLE attendance ADD COLUMN check_in_device_id uuid REFERENCES devices(id) ON DELETE RESTRICT;
ALTER TABLE attendance ADD COLUMN check_out_device_id uuid REFERENCES devices(id) ON DELETE RESTRICT;
ALTER TABLE attendance ADD COLUMN app_check_in_at timestamptz;
ALTER TABLE attendance ADD COLUMN app_check_out_at timestamptz;

UPDATE attendance SET check_in_source = 'manual', check_out_source = CASE WHEN check_out_at IS NOT NULL THEN 'manual' END WHERE manual;
UPDATE attendance SET check_out_source = CASE WHEN check_out_at IS NOT NULL THEN 'app' END, app_check_in_at = check_in_at, app_check_out_at = check_out_at WHERE NOT manual;

ALTER TABLE attendance ADD CONSTRAINT ck_attendance_check_in_source CHECK (check_in_source IN ('app','device','manual'));
ALTER TABLE attendance ADD CONSTRAINT ck_attendance_check_out_source CHECK (check_out_source IS NULL OR check_out_source IN ('app','device','manual'));
ALTER TABLE attendance ADD CONSTRAINT ck_attendance_check_in_device CHECK (check_in_device_id IS NULL OR check_in_source = 'device');
ALTER TABLE attendance ADD CONSTRAINT ck_attendance_check_out_device CHECK (check_out_device_id IS NULL OR check_out_source = 'device');
ALTER TABLE attendance ADD CONSTRAINT ck_attendance_check_out_source_set CHECK (check_out_source IS NULL OR check_out_at IS NOT NULL);
ALTER TABLE attendance ADD CONSTRAINT ck_attendance_app_times CHECK (app_check_in_at IS NULL OR app_check_out_at IS NULL OR app_check_out_at > app_check_in_at);
CREATE INDEX ix_attendance_check_in_device_id ON attendance (check_in_device_id) WHERE check_in_device_id IS NOT NULL;
CREATE INDEX ix_attendance_check_out_device_id ON attendance (check_out_device_id) WHERE check_out_device_id IS NOT NULL;

-- ---------------------------------------------------------------- permissions and settings
"""
    + "\n".join(
        f"INSERT INTO permissions (code, module, description) VALUES ('{c}', 'device', '{d}') ON CONFLICT (code) DO NOTHING;\n"
        f"INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name IN {roles} AND p.code = '{c}' ON CONFLICT DO NOTHING;"
        for c, d, roles in _PERMISSIONS
    )
    + "\n"
    + "\n".join(f"INSERT INTO settings (key, value) VALUES ('{k}', '{v}'::jsonb) ON CONFLICT (key) DO NOTHING;" for k, v in _SETTINGS)
    + r"""

DO $$
BEGIN
  REVOKE TRUNCATE ON device_punches FROM app_user;  -- DELETE stays granted: the retention job; the trigger refuses rows younger than 90 days
  REVOKE TRUNCATE ON device_users FROM app_user;    -- DELETE stays granted: the service clears a device's user list
  REVOKE TRUNCATE ON devices FROM app_user;
  REVOKE TRUNCATE ON device_syncs FROM app_user;
  REVOKE TRUNCATE ON device_punch_outcomes FROM app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'revoke on the 0030 tables skipped';
END
$$;
"""
)

DOWNGRADE = (
    r"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM device_punches) THEN
    RAISE EXCEPTION 'cannot downgrade 0030: device punches exist (raw attendance evidence must not be dropped silently)';
  END IF;
  IF EXISTS (SELECT 1 FROM attendance WHERE check_in_source = 'device' OR check_out_source = 'device') THEN
    RAISE EXCEPTION 'cannot downgrade 0030: attendance rows sourced from a device exist';
  END IF;
END
$$;

DELETE FROM settings WHERE key IN ("""
    + ", ".join(f"'{k}'" for k, _ in _SETTINGS)
    + r""");
DELETE FROM permissions WHERE code IN ("""
    + ", ".join(f"'{c}'" for c, _, _ in _PERMISSIONS)
    + r""");

DROP INDEX IF EXISTS ix_attendance_check_out_device_id;
DROP INDEX IF EXISTS ix_attendance_check_in_device_id;
ALTER TABLE attendance DROP CONSTRAINT ck_attendance_app_times;
ALTER TABLE attendance DROP CONSTRAINT ck_attendance_check_out_source_set;
ALTER TABLE attendance DROP CONSTRAINT ck_attendance_check_out_device;
ALTER TABLE attendance DROP CONSTRAINT ck_attendance_check_in_device;
ALTER TABLE attendance DROP CONSTRAINT ck_attendance_check_out_source;
ALTER TABLE attendance DROP CONSTRAINT ck_attendance_check_in_source;
ALTER TABLE attendance DROP COLUMN app_check_out_at;
ALTER TABLE attendance DROP COLUMN app_check_in_at;
ALTER TABLE attendance DROP COLUMN check_out_device_id;
ALTER TABLE attendance DROP COLUMN check_in_device_id;
ALTER TABLE attendance DROP COLUMN check_out_source;
ALTER TABLE attendance DROP COLUMN check_in_source;

DROP TABLE IF EXISTS device_punch_outcomes;
DROP TABLE IF EXISTS device_punches;
DROP FUNCTION IF EXISTS device_punches_block_mutation();
DROP TABLE IF EXISTS device_users;
DROP TABLE IF EXISTS device_syncs;
DROP TABLE IF EXISTS devices;
"""
)


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
