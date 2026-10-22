"""leave_types, leave_balances, leave_requests, permission_requests

Revision ID: 0003
Revises: 0002
"""
from migration_sql import run

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE leave_types (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code text NOT NULL,
  name text NOT NULL,
  annual_quota numeric(4,1) NOT NULL DEFAULT 0,
  is_paid boolean NOT NULL DEFAULT true,
  allow_half_day boolean NOT NULL DEFAULT true,
  requires_attachment_after_days integer,
  max_consecutive_days integer,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_leave_types_code UNIQUE (code),
  CONSTRAINT uq_leave_types_name UNIQUE (name),
  CONSTRAINT ck_leave_types_annual_quota CHECK (annual_quota >= 0),
  CONSTRAINT ck_leave_types_attachment_days CHECK (requires_attachment_after_days IS NULL OR requires_attachment_after_days >= 0),
  CONSTRAINT ck_leave_types_max_consecutive CHECK (max_consecutive_days IS NULL OR max_consecutive_days > 0)
);

CREATE TABLE leave_balances (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  leave_type_id uuid NOT NULL REFERENCES leave_types(id) ON DELETE RESTRICT,
  year integer NOT NULL,
  quota numeric(4,1) NOT NULL DEFAULT 0,
  used numeric(4,1) NOT NULL DEFAULT 0,
  pending numeric(4,1) NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_leave_balances_user_type_year UNIQUE (user_id, leave_type_id, year),
  CONSTRAINT ck_leave_balances_year CHECK (year BETWEEN 2000 AND 2200),
  CONSTRAINT ck_leave_balances_non_negative CHECK (quota >= 0 AND used >= 0 AND pending >= 0),
  CONSTRAINT ck_leave_balances_not_overdrawn CHECK (used + pending <= quota)
);
CREATE INDEX ix_leave_balances_leave_type_id ON leave_balances (leave_type_id);

CREATE TABLE leave_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  leave_type_id uuid NOT NULL REFERENCES leave_types(id) ON DELETE RESTRICT,
  from_date date NOT NULL,
  to_date date NOT NULL,
  half_day boolean NOT NULL DEFAULT false,
  half_day_session text,
  days numeric(4,1) NOT NULL,
  reason text NOT NULL,
  attachment_file_id uuid REFERENCES files(id) ON DELETE SET NULL,
  status text NOT NULL DEFAULT 'pending',
  approver_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  decision_comment text,
  decided_at timestamptz,
  cancelled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_leave_requests_dates CHECK (to_date >= from_date),
  CONSTRAINT ck_leave_requests_half_day_single_date CHECK (NOT half_day OR from_date = to_date),
  CONSTRAINT ck_leave_requests_half_day_session CHECK (
    (half_day AND half_day_session IN ('first','second')) OR (NOT half_day AND half_day_session IS NULL)),
  CONSTRAINT ck_leave_requests_days CHECK (days > 0),
  CONSTRAINT ck_leave_requests_status CHECK (status IN ('pending','approved','rejected','cancelled')),
  CONSTRAINT ex_leave_requests_no_overlap EXCLUDE USING gist (
    user_id WITH =, daterange(from_date, to_date, '[]') WITH &&
  ) WHERE (status IN ('pending','approved'))
);
CREATE INDEX ix_leave_requests_status ON leave_requests (status);
CREATE INDEX ix_leave_requests_user_from_date ON leave_requests (user_id, from_date);
CREATE INDEX ix_leave_requests_leave_type_id ON leave_requests (leave_type_id);
CREATE INDEX ix_leave_requests_approver_id ON leave_requests (approver_id);
CREATE INDEX ix_leave_requests_attachment_file_id ON leave_requests (attachment_file_id);

CREATE TABLE permission_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  request_date date NOT NULL,
  from_time time NOT NULL,
  to_time time NOT NULL,
  minutes integer NOT NULL,
  reason text NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  approver_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  decision_comment text,
  decided_at timestamptz,
  cancelled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_permission_requests_times CHECK (to_time > from_time),
  CONSTRAINT ck_permission_requests_minutes CHECK (minutes > 0),
  CONSTRAINT ck_permission_requests_status CHECK (status IN ('pending','approved','rejected','cancelled')),
  CONSTRAINT ex_permission_requests_no_overlap EXCLUDE USING gist (
    user_id WITH =, tsrange((request_date + from_time), (request_date + to_time)) WITH &&
  ) WHERE (status IN ('pending','approved'))
);
CREATE INDEX ix_permission_requests_status ON permission_requests (status);
CREATE INDEX ix_permission_requests_user_date ON permission_requests (user_id, request_date);
CREATE INDEX ix_permission_requests_approver_id ON permission_requests (approver_id);

CREATE TRIGGER trg_leave_types_updated_at BEFORE UPDATE ON leave_types FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_leave_balances_updated_at BEFORE UPDATE ON leave_balances FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_leave_requests_updated_at BEFORE UPDATE ON leave_requests FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_permission_requests_updated_at BEFORE UPDATE ON permission_requests FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS permission_requests;
DROP TABLE IF EXISTS leave_requests;
DROP TABLE IF EXISTS leave_balances;
DROP TABLE IF EXISTS leave_types;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
