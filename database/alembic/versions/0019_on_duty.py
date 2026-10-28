"""On duty: client locations (admin geo coordinates), on-duty requests, attendance.client_location_id, permission

Revision ID: 0019
Revises: 0018

Plan: docs/plans/13-on-duty.md; contract: docs/api-contract.md section 14.

Added
* client_locations         client sites set by the admin: name (unique, case-insensitive), address, lat/lng, radius_m (20-5000)
* on_duty_requests         full working days at one client location; same approval flow and statuses as leave. No balance.
                           EXCLUDE: one user cannot have two pending/approved on-duty requests on the same day.
                           reason / decision comment are encrypted (reason_enc / decision_comment_enc), like leave.
* attendance.client_location_id  the client site a punch was checked against (NULL = normal office punch)
* permission client_location.manage (admin, hr)

Downgrade drops both tables and the attendance column (all on-duty data is lost) and the permission.
"""

from migration_sql import run

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE client_locations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name citext NOT NULL,
  address text,
  lat double precision NOT NULL,
  lng double precision NOT NULL,
  radius_m integer NOT NULL DEFAULT 200,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_client_locations_name UNIQUE (name),
  CONSTRAINT ck_client_locations_lat CHECK (lat BETWEEN -90 AND 90),
  CONSTRAINT ck_client_locations_lng CHECK (lng BETWEEN -180 AND 180),
  CONSTRAINT ck_client_locations_radius CHECK (radius_m BETWEEN 20 AND 5000)
);
CREATE TRIGGER trg_client_locations_updated_at BEFORE UPDATE ON client_locations FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE on_duty_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  client_location_id uuid NOT NULL REFERENCES client_locations(id) ON DELETE RESTRICT,
  from_date date NOT NULL,
  to_date date NOT NULL,
  days integer NOT NULL,
  reason_enc bytea,
  status text NOT NULL DEFAULT 'pending',
  approver_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  decision_comment_enc bytea,
  decided_at timestamptz,
  cancelled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_on_duty_requests_dates CHECK (to_date >= from_date),
  CONSTRAINT ck_on_duty_requests_days CHECK (days > 0),
  CONSTRAINT ck_on_duty_requests_status CHECK (status IN ('pending','approved','rejected','cancelled')),
  CONSTRAINT ex_on_duty_requests_no_overlap EXCLUDE USING gist (
    user_id WITH =, daterange(from_date, to_date, '[]') WITH &&
  ) WHERE (status IN ('pending','approved'))
);
CREATE INDEX ix_on_duty_requests_status ON on_duty_requests (status);
CREATE INDEX ix_on_duty_requests_user_from_date ON on_duty_requests (user_id, from_date);
CREATE INDEX ix_on_duty_requests_client_location_id ON on_duty_requests (client_location_id);
CREATE INDEX ix_on_duty_requests_approver_id ON on_duty_requests (approver_id);
CREATE TRIGGER trg_on_duty_requests_updated_at BEFORE UPDATE ON on_duty_requests FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE attendance ADD COLUMN client_location_id uuid REFERENCES client_locations(id) ON DELETE RESTRICT;
CREATE INDEX ix_attendance_client_location_id ON attendance (client_location_id);

INSERT INTO permissions (code, module, description) VALUES ('client_location.manage', 'client_location', 'Manage client locations for on duty (geo coordinates)') ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name IN ('admin', 'hr') AND p.code = 'client_location.manage' ON CONFLICT DO NOTHING;
"""

DOWNGRADE = r"""
DELETE FROM permissions WHERE code = 'client_location.manage';
DROP INDEX IF EXISTS ix_attendance_client_location_id;
ALTER TABLE attendance DROP COLUMN IF EXISTS client_location_id;
DROP TABLE IF EXISTS on_duty_requests;
DROP TABLE IF EXISTS client_locations;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
