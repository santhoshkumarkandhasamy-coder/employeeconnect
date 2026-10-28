from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import EncryptedString

from .base import Base, TimestampMixin, fk_uuid, uuid_pk


class Task(TimestampMixin, Base):
    """A dated task with an optional reminder (plan 14). `user_id` is the assignee; `created_by` assigned it
    (the same person for a personal task). The reminder job pushes once at `remind_at` and stamps `reminded_at`."""
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("remind_at IS NULL OR remind_at <= due_at", name="ck_tasks_remind_before_due"),
        Index("ix_tasks_user_due", "user_id", "due_at"),
        Index("ix_tasks_created_by", "created_by"),
        Index("ix_tasks_reminder_due", "remind_at",
              postgresql_where=text("reminded_at IS NULL AND done_at IS NULL AND remind_at IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    created_by: Mapped[uuid.UUID] = fk_uuid("users.id")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column("notes_enc", EncryptedString)  # private text, encrypted at rest
    due_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    remind_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reminded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
