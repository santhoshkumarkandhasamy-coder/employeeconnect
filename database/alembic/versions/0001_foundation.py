"""foundation: extensions, roles, users, auth tables, audit log, settings

Revision ID: 0001
Revises:
"""
from migration_sql import run

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;

-- Application runtime role: DML only. Devops sets LOGIN + password (ALTER ROLE app_user LOGIN PASSWORD '...').
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    CREATE ROLE app_user NOLOGIN;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  RAISE NOTICE 'cannot create role app_user; create it manually before migrating';
END
$$;

DO $$
BEGIN
  GRANT USAGE ON SCHEMA public TO app_user;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'grants to app_user skipped';
END
$$;

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END
$$;

CREATE TABLE roles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  description text,
  is_system boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_roles_name UNIQUE (name)
);

CREATE TABLE permissions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL,
  module text NOT NULL,
  description text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_permissions_code UNIQUE (code)
);

CREATE TABLE role_permissions (
  role_id uuid NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  permission_id uuid NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
  CONSTRAINT pk_role_permissions PRIMARY KEY (role_id, permission_id)
);
CREATE INDEX ix_role_permissions_permission_id ON role_permissions (permission_id);

CREATE TABLE departments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name citext NOT NULL,
  code text NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_departments_name UNIQUE (name),
  CONSTRAINT uq_departments_code UNIQUE (code)
);

CREATE TABLE shifts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  start_time time NOT NULL,
  end_time time NOT NULL,
  grace_minutes integer NOT NULL DEFAULT 10,
  working_days smallint[] NOT NULL DEFAULT '{1,2,3,4,5}',
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_shifts_name UNIQUE (name),
  CONSTRAINT ck_shifts_grace_minutes CHECK (grace_minutes BETWEEN 0 AND 120),
  CONSTRAINT ck_shifts_start_ne_end CHECK (start_time <> end_time),
  CONSTRAINT ck_shifts_working_days CHECK (working_days <@ ARRAY[1,2,3,4,5,6,7]::smallint[])
);

CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id citext NOT NULL,
  password_hash text NOT NULL,
  full_name text NOT NULL,
  official_email citext,
  role_id uuid NOT NULL REFERENCES roles(id) ON DELETE RESTRICT,
  department_id uuid REFERENCES departments(id) ON DELETE RESTRICT,
  shift_id uuid REFERENCES shifts(id) ON DELETE RESTRICT,
  manager_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  status text NOT NULL DEFAULT 'active',
  must_change_password boolean NOT NULL DEFAULT true,
  failed_login_count integer NOT NULL DEFAULT 0,
  locked_until timestamptz,
  last_failed_login_at timestamptz,
  last_login_at timestamptz,
  password_changed_at timestamptz,
  join_date date,
  deleted_at timestamptz,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_users_employee_id UNIQUE (employee_id),
  CONSTRAINT uq_users_official_email UNIQUE (official_email),
  CONSTRAINT ck_users_status CHECK (status IN ('active','inactive')),
  CONSTRAINT ck_users_manager_not_self CHECK (manager_id IS NULL OR manager_id <> id),
  CONSTRAINT ck_users_failed_login_count CHECK (failed_login_count >= 0),
  CONSTRAINT ck_users_employee_id_canonical CHECK (employee_id::text = upper(btrim(employee_id::text)) AND employee_id::text <> '')
);
CREATE INDEX ix_users_role_id ON users (role_id);
CREATE INDEX ix_users_department_id ON users (department_id);
CREATE INDEX ix_users_shift_id ON users (shift_id);
CREATE INDEX ix_users_manager_id ON users (manager_id);
CREATE INDEX ix_users_status ON users (status);

CREATE TABLE files (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  purpose text NOT NULL,
  storage_path text NOT NULL,
  content_type text NOT NULL,
  size_bytes integer NOT NULL,
  sha256 text,
  is_encrypted boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_files_purpose CHECK (purpose IN ('selfie','leave_attachment','announcement_image','announcement_pdf','profile_photo')),
  CONSTRAINT ck_files_size_bytes CHECK (size_bytes >= 0)
);
CREATE INDEX ix_files_owner_id ON files (owner_id);

CREATE TABLE employee_profiles (
  user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  photo_file_id uuid REFERENCES files(id) ON DELETE SET NULL,
  dob_enc bytea,
  wedding_anniversary_enc bytea,
  personal_email_enc bytea,
  phone_enc bytea,
  emergency_name_enc bytea,
  emergency_relationship text,
  emergency_phone_enc bytea,
  blood_group text,
  address_enc bytea,
  home_location_enc bytea,
  home_radius_m integer NOT NULL DEFAULT 150,
  location_consent_at timestamptz,
  location_consent_version text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_employee_profiles_blood_group CHECK (blood_group IS NULL OR blood_group IN ('A+','A-','B+','B-','AB+','AB-','O+','O-')),
  CONSTRAINT ck_employee_profiles_home_radius CHECK (home_radius_m BETWEEN 50 AND 1000)
);
CREATE INDEX ix_employee_profiles_photo_file_id ON employee_profiles (photo_file_id);

CREATE TABLE refresh_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  family_id uuid NOT NULL,
  token_hash char(64) NOT NULL,
  device_id text,
  device_name text,
  ip inet,
  user_agent text,
  issued_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  used_at timestamptz,
  revoked_at timestamptz,
  replaced_by uuid REFERENCES refresh_tokens(id) ON DELETE SET NULL,
  CONSTRAINT uq_refresh_tokens_token_hash UNIQUE (token_hash),
  CONSTRAINT ck_refresh_tokens_expiry CHECK (expires_at > issued_at)
);
CREATE INDEX ix_refresh_tokens_user_id ON refresh_tokens (user_id);
CREATE INDEX ix_refresh_tokens_family_id ON refresh_tokens (family_id);
CREATE INDEX ix_refresh_tokens_expires_at ON refresh_tokens (expires_at);
CREATE INDEX ix_refresh_tokens_replaced_by ON refresh_tokens (replaced_by);

CREATE TABLE device_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  fcm_token text NOT NULL,
  platform text NOT NULL,
  device_id text,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_device_tokens_fcm_token UNIQUE (fcm_token),
  CONSTRAINT ck_device_tokens_platform CHECK (platform IN ('android','ios'))
);
CREATE INDEX ix_device_tokens_user_id ON device_tokens (user_id);

CREATE TABLE password_reset_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  status text NOT NULL DEFAULT 'open',
  otp_hash text,
  otp_expires_at timestamptz,
  otp_attempts integer NOT NULL DEFAULT 0,
  requested_at timestamptz NOT NULL DEFAULT now(),
  handled_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  handled_at timestamptz,
  CONSTRAINT ck_password_reset_requests_status CHECK (status IN ('open','handled','expired')),
  CONSTRAINT ck_password_reset_requests_attempts CHECK (otp_attempts >= 0)
);
CREATE UNIQUE INDEX uq_password_reset_requests_one_open ON password_reset_requests (user_id) WHERE status = 'open';
CREATE INDEX ix_password_reset_requests_status ON password_reset_requests (status);
CREATE INDEX ix_password_reset_requests_handled_by ON password_reset_requests (handled_by);

CREATE TABLE audit_logs (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  actor_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  actor_employee_id text,
  action text NOT NULL,
  entity_type text,
  entity_id text,
  before jsonb,
  after jsonb,
  ip inet,
  user_agent text,
  request_id text,
  outcome text NOT NULL DEFAULT 'success',
  row_hmac text,
  CONSTRAINT ck_audit_logs_outcome CHECK (outcome IN ('success','failure'))
);
CREATE INDEX ix_audit_logs_ts ON audit_logs (ts);
CREATE INDEX ix_audit_logs_actor_id ON audit_logs (actor_id);
CREATE INDEX ix_audit_logs_entity ON audit_logs (entity_type, entity_id);
CREATE INDEX ix_audit_logs_action ON audit_logs (action);

-- Append-only: block UPDATE/DELETE/TRUNCATE for every role (including the owner).
CREATE FUNCTION audit_logs_block_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_logs is append-only' USING ERRCODE = 'insufficient_privilege', DETAIL = TG_OP;
END
$$;
CREATE TRIGGER trg_audit_logs_no_update_delete BEFORE UPDATE OR DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION audit_logs_block_mutation();
CREATE TRIGGER trg_audit_logs_no_truncate BEFORE TRUNCATE ON audit_logs
  FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_block_mutation();

DO $$
BEGIN
  REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM app_user;
EXCEPTION WHEN undefined_object OR insufficient_privilege THEN
  RAISE NOTICE 'revoke on audit_logs skipped';
END
$$;

CREATE TABLE settings (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_settings_updated_by ON settings (updated_by);

CREATE TRIGGER trg_roles_updated_at BEFORE UPDATE ON roles FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_departments_updated_at BEFORE UPDATE ON departments FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_shifts_updated_at BEFORE UPDATE ON shifts FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_employee_profiles_updated_at BEFORE UPDATE ON employee_profiles FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_settings_updated_at BEFORE UPDATE ON settings FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS settings;
DROP TABLE IF EXISTS audit_logs;
DROP FUNCTION IF EXISTS audit_logs_block_mutation();
DROP TABLE IF EXISTS password_reset_requests;
DROP TABLE IF EXISTS device_tokens;
DROP TABLE IF EXISTS refresh_tokens;
DROP TABLE IF EXISTS employee_profiles;
DROP TABLE IF EXISTS files;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS shifts;
DROP TABLE IF EXISTS departments;
DROP TABLE IF EXISTS role_permissions;
DROP TABLE IF EXISTS permissions;
DROP TABLE IF EXISTS roles;
DROP FUNCTION IF EXISTS set_updated_at();
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    # Extensions and the app_user role are intentionally left in place (cluster-level, shared).
    run(DOWNGRADE)
