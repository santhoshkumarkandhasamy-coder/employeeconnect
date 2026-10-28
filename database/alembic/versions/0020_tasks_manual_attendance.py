"""Task reminders (calendar) and manual attendance by HR/Admin

Revision ID: 0020
Revises: 0019

Plan: docs/plans/14-tasks-manual-attendance.md; contract: docs/api-contract.md section 15.

Added
* tasks                     dated tasks with an optional reminder; user_id = assignee, created_by = who created/assigned it.
                            notes are encrypted (notes_enc). The reminder job pushes once (reminded_at).
* attendance.manual / manual_by / manual_reason_enc   manual entry or correction by HR/Admin (reason encrypted)
* permissions task.assign_team (manager, hr, admin), task.assign_all (hr, admin), attendance.manual (hr, admin)

Downgrade drops the tasks table (all tasks are lost), the attendance columns and the permissions.
"""

from migration_sql import run

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  title text NOT NULL,
  notes_enc bytea,
  due_at timestamptz NOT NULL,
  remind_at timestamptz,
  reminded_at timestamptz,
  done_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_tasks_remind_before_due CHECK (remind_at IS NULL OR remind_at <= due_at),
  CONSTRAINT ck_tasks_title CHECK (char_length(title) BETWEEN 1 AND 150)
);
CREATE INDEX ix_tasks_user_due ON tasks (user_id, due_at);
CREATE INDEX ix_tasks_created_by ON tasks (created_by);
CREATE INDEX ix_tasks_reminder_due ON tasks (remind_at) WHERE reminded_at IS NULL AND done_at IS NULL AND remind_at IS NOT NULL;
CREATE TRIGGER trg_tasks_updated_at BEFORE UPDATE ON tasks FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE attendance ADD COLUMN manual boolean NOT NULL DEFAULT false;
ALTER TABLE attendance ADD COLUMN manual_by uuid REFERENCES users(id) ON DELETE RESTRICT;
ALTER TABLE attendance ADD COLUMN manual_reason_enc bytea;
CREATE INDEX ix_attendance_manual ON attendance (work_date) WHERE manual;

INSERT INTO permissions (code, module, description) VALUES ('task.assign_team', 'task', 'Assign tasks to direct reports') ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (code, module, description) VALUES ('task.assign_all', 'task', 'Assign tasks to any employee') ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (code, module, description) VALUES ('attendance.manual', 'attendance', 'Enter or correct attendance manually') ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name IN ('admin', 'hr', 'manager') AND p.code = 'task.assign_team' ON CONFLICT DO NOTHING;
INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p WHERE r.name IN ('admin', 'hr') AND p.code IN ('task.assign_all', 'attendance.manual') ON CONFLICT DO NOTHING;
"""

DOWNGRADE = r"""
DELETE FROM permissions WHERE code IN ('task.assign_team', 'task.assign_all', 'attendance.manual');
DROP INDEX IF EXISTS ix_attendance_manual;
ALTER TABLE attendance DROP COLUMN IF EXISTS manual_reason_enc;
ALTER TABLE attendance DROP COLUMN IF EXISTS manual_by;
ALTER TABLE attendance DROP COLUMN IF EXISTS manual;
DROP TABLE IF EXISTS tasks;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
