"""Company registration, licensing, offboarding columns, session metadata, admin-reset-only passwords

Revision ID: 0017
Revises: 0016

Plan: docs/plans/11-remote-console-offboarding-company.md section 7; contract: docs/api-contract.md section 12.

Added
* company (singleton, id = 1): name, logo, logo_version, setup_completed_at, deployment_id (random uuid, generated here)
* license (singleton, id = 1): display copy of the vendor-signed token (the server re-verifies the token). No row = unlicensed.
* files.ck_files_purpose accepts 'company_logo'
* users.deactivated_at / deactivated_by / deactivation_reason (+ checks); inactive rows are backfilled (reason 'other')
* refresh_tokens.client_type / app_version / platform + partial index ix_refresh_tokens_live
* permissions company.manage, console.access, session.manage, system.view, license.manage (+ role grants)
* settings console.session_max_hours, console.inactivity_timeout_minutes, auth.offline_cache_max_hours

Dropped
* password_reset_requests.otp_hash / otp_expires_at / otp_attempts and ck_password_reset_requests_attempts (admin reset only)

Unlicensed default (decision): 50 seats, never expires, admins count as seats. This is a code constant in the backend
(UNLICENSED_MAX_USERS), deliberately NOT a DB setting; a signed key may raise it later (license.expires_at NULL = never expires).

Seat-lock order (deadlock rule): always lock the license row first (company row when there is no license), then any users rows.

Downgrade restores the structures. The dropped OTP data (all expired/unused) and company/license rows are lost;
files with purpose 'company_logo' are deleted (rows only) so the old purpose check can be restored.
"""

from migration_sql import run

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE company (
  id smallint PRIMARY KEY DEFAULT 1,
  deployment_id uuid NOT NULL DEFAULT gen_random_uuid(),
  name text,
  logo_file_id uuid REFERENCES files(id) ON DELETE SET NULL,
  logo_version integer NOT NULL DEFAULT 0,
  setup_completed_at timestamptz,
  created_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  updated_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_company_singleton CHECK (id = 1),
  CONSTRAINT ck_company_name CHECK (name IS NULL OR char_length(btrim(name)) BETWEEN 2 AND 100),
  CONSTRAINT ck_company_logo_version CHECK (logo_version >= 0),
  CONSTRAINT ck_company_setup_has_name CHECK (setup_completed_at IS NULL OR name IS NOT NULL),
  CONSTRAINT uq_company_deployment_id UNIQUE (deployment_id)
);
CREATE INDEX ix_company_logo_file_id ON company (logo_file_id);
CREATE INDEX ix_company_created_by ON company (created_by);
CREATE INDEX ix_company_updated_by ON company (updated_by);
CREATE TRIGGER trg_company_updated_at BEFORE UPDATE ON company FOR EACH ROW EXECUTE FUNCTION set_updated_at();
INSERT INTO company (id) VALUES (1);

CREATE TABLE license (
  id smallint PRIMARY KEY DEFAULT 1,
  token text NOT NULL,
  license_id text NOT NULL,
  licensed_to text NOT NULL,
  max_users integer NOT NULL,
  issued_at timestamptz NOT NULL,
  expires_at timestamptz,
  grace_days integer NOT NULL DEFAULT 14,
  on_expiry text NOT NULL DEFAULT 'block_new_users',
  installed_at timestamptz NOT NULL DEFAULT now(),
  installed_by uuid REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT ck_license_singleton CHECK (id = 1),
  CONSTRAINT ck_license_token_len CHECK (char_length(token) <= 4096),
  CONSTRAINT ck_license_max_users CHECK (max_users BETWEEN 1 AND 100000),
  CONSTRAINT ck_license_expiry CHECK (expires_at IS NULL OR expires_at > issued_at),
  CONSTRAINT ck_license_grace_days CHECK (grace_days BETWEEN 0 AND 90),
  CONSTRAINT ck_license_on_expiry CHECK (on_expiry IN ('block_new_users','read_only'))
);
CREATE INDEX ix_license_installed_by ON license (installed_by);

ALTER TABLE files DROP CONSTRAINT ck_files_purpose;
ALTER TABLE files ADD CONSTRAINT ck_files_purpose CHECK (purpose IN ('selfie','leave_attachment','announcement_image','announcement_pdf','profile_photo','company_logo'));

ALTER TABLE password_reset_requests DROP CONSTRAINT IF EXISTS ck_password_reset_requests_attempts;
ALTER TABLE password_reset_requests DROP COLUMN IF EXISTS otp_hash;
ALTER TABLE password_reset_requests DROP COLUMN IF EXISTS otp_expires_at;
ALTER TABLE password_reset_requests DROP COLUMN IF EXISTS otp_attempts;

ALTER TABLE users ADD COLUMN deactivated_at timestamptz;
ALTER TABLE users ADD COLUMN deactivated_by uuid REFERENCES users(id) ON DELETE RESTRICT;
ALTER TABLE users ADD COLUMN deactivation_reason text;
UPDATE users SET deactivated_at = COALESCE(deleted_at, updated_at), deactivation_reason = 'other' WHERE status = 'inactive';
ALTER TABLE users ADD CONSTRAINT ck_users_deactivation_reason CHECK (deactivation_reason IS NULL OR deactivation_reason IN ('resigned','terminated','contract_end','absconded','other'));
ALTER TABLE users ADD CONSTRAINT ck_users_deactivation_only_inactive CHECK (status = 'inactive' OR (deactivated_at IS NULL AND deactivation_reason IS NULL));
CREATE INDEX ix_users_deactivated_by ON users (deactivated_by);

ALTER TABLE refresh_tokens ADD COLUMN client_type text NOT NULL DEFAULT 'mobile';
ALTER TABLE refresh_tokens ADD COLUMN app_version text;
ALTER TABLE refresh_tokens ADD COLUMN platform text;
ALTER TABLE refresh_tokens ADD CONSTRAINT ck_refresh_tokens_client_type CHECK (client_type IN ('mobile','web'));
ALTER TABLE refresh_tokens ADD CONSTRAINT ck_refresh_tokens_app_version CHECK (app_version IS NULL OR char_length(app_version) <= 32);
ALTER TABLE refresh_tokens ADD CONSTRAINT ck_refresh_tokens_platform CHECK (platform IS NULL OR platform IN ('android','ios','web'));
CREATE INDEX ix_refresh_tokens_live ON refresh_tokens (user_id, family_id) WHERE revoked_at IS NULL AND replaced_by IS NULL;

INSERT INTO permissions (code, module, description) VALUES ('company.manage', 'company', 'Manage company name and logo') ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (code, module, description) VALUES ('console.access', 'console', 'Sign in to the admin web console') ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (code, module, description) VALUES ('session.manage', 'session', 'View and revoke login sessions and devices') ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (code, module, description) VALUES ('system.view', 'system', 'View system health') ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (code, module, description) VALUES ('license.manage', 'license', 'View and install the license') ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name = 'admin' AND p.code IN ('company.manage', 'console.access', 'session.manage', 'system.view', 'license.manage') ON CONFLICT DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name = 'hr' AND p.code IN ('session.manage') ON CONFLICT DO NOTHING;
INSERT INTO settings (key, value) VALUES ('console.session_max_hours', '12'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('console.inactivity_timeout_minutes', '15'::jsonb) ON CONFLICT (key) DO NOTHING;
INSERT INTO settings (key, value) VALUES ('auth.offline_cache_max_hours', '72'::jsonb) ON CONFLICT (key) DO NOTHING;
"""

DOWNGRADE = r"""
DELETE FROM settings WHERE key IN ('console.session_max_hours', 'console.inactivity_timeout_minutes', 'auth.offline_cache_max_hours');
DELETE FROM permissions WHERE code IN ('company.manage', 'console.access', 'session.manage', 'system.view', 'license.manage');

DROP INDEX IF EXISTS ix_refresh_tokens_live;
ALTER TABLE refresh_tokens DROP CONSTRAINT IF EXISTS ck_refresh_tokens_platform;
ALTER TABLE refresh_tokens DROP CONSTRAINT IF EXISTS ck_refresh_tokens_app_version;
ALTER TABLE refresh_tokens DROP CONSTRAINT IF EXISTS ck_refresh_tokens_client_type;
ALTER TABLE refresh_tokens DROP COLUMN IF EXISTS platform;
ALTER TABLE refresh_tokens DROP COLUMN IF EXISTS app_version;
ALTER TABLE refresh_tokens DROP COLUMN IF EXISTS client_type;

DROP INDEX IF EXISTS ix_users_deactivated_by;
ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_deactivation_only_inactive;
ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_deactivation_reason;
ALTER TABLE users DROP COLUMN IF EXISTS deactivation_reason;
ALTER TABLE users DROP COLUMN IF EXISTS deactivated_by;
ALTER TABLE users DROP COLUMN IF EXISTS deactivated_at;

ALTER TABLE password_reset_requests ADD COLUMN otp_hash text;
ALTER TABLE password_reset_requests ADD COLUMN otp_expires_at timestamptz;
ALTER TABLE password_reset_requests ADD COLUMN otp_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE password_reset_requests ADD CONSTRAINT ck_password_reset_requests_attempts CHECK (otp_attempts >= 0);

DROP TABLE IF EXISTS license;
DROP TABLE IF EXISTS company;
DELETE FROM files WHERE purpose = 'company_logo';
ALTER TABLE files DROP CONSTRAINT ck_files_purpose;
ALTER TABLE files ADD CONSTRAINT ck_files_purpose CHECK (purpose IN ('selfie','leave_attachment','announcement_image','announcement_pdf','profile_photo'));
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
