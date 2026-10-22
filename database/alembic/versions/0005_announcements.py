"""announcements, announcement_departments, announcement_reads

Revision ID: 0005
Revises: 0004
"""
from migration_sql import run

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UPGRADE = r"""
CREATE TABLE announcements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title varchar(150) NOT NULL,
  body text NOT NULL,
  priority text NOT NULL DEFAULT 'normal',
  image_file_id uuid REFERENCES files(id) ON DELETE SET NULL,
  pdf_file_id uuid REFERENCES files(id) ON DELETE SET NULL,
  target_all boolean NOT NULL DEFAULT true,
  published_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  created_by uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_announcements_priority CHECK (priority IN ('normal','important','urgent')),
  CONSTRAINT ck_announcements_expiry CHECK (expires_at IS NULL OR expires_at > published_at),
  CONSTRAINT ck_announcements_title CHECK (length(btrim(title)) > 0)
);
CREATE INDEX ix_announcements_published_at ON announcements (published_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX ix_announcements_created_by ON announcements (created_by);
CREATE INDEX ix_announcements_image_file_id ON announcements (image_file_id);
CREATE INDEX ix_announcements_pdf_file_id ON announcements (pdf_file_id);

CREATE TABLE announcement_departments (
  announcement_id uuid NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
  department_id uuid NOT NULL REFERENCES departments(id) ON DELETE RESTRICT,
  CONSTRAINT pk_announcement_departments PRIMARY KEY (announcement_id, department_id)
);
CREATE INDEX ix_announcement_departments_department_id ON announcement_departments (department_id);

CREATE TABLE announcement_reads (
  announcement_id uuid NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  read_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_announcement_reads PRIMARY KEY (announcement_id, user_id)
);
CREATE INDEX ix_announcement_reads_user_id ON announcement_reads (user_id);

CREATE TRIGGER trg_announcements_updated_at BEFORE UPDATE ON announcements FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS announcement_reads;
DROP TABLE IF EXISTS announcement_departments;
DROP TABLE IF EXISTS announcements;
"""


def upgrade() -> None:
    run(UPGRADE)


def downgrade() -> None:
    run(DOWNGRADE)
