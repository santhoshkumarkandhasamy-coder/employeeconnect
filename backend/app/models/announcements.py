from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, fk_uuid, uuid_pk


class Announcement(TimestampMixin, Base):
    __tablename__ = "announcements"
    __table_args__ = (
        CheckConstraint("priority IN ('normal','important','urgent')", name="ck_announcements_priority"),
        CheckConstraint("expires_at IS NULL OR expires_at > published_at", name="ck_announcements_expiry"),
        CheckConstraint("length(btrim(title)) > 0", name="ck_announcements_title"),
        Index("ix_announcements_published_at", text("published_at DESC"), postgresql_where=text("deleted_at IS NULL")),
        Index("ix_announcements_created_by", "created_by"),
        Index("ix_announcements_updated_by", "updated_by"),
        Index("ix_announcements_image_file_id", "image_file_id"),
        Index("ix_announcements_pdf_file_id", "pdf_file_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'normal'"))
    image_file_id: Mapped[uuid.UUID | None] = fk_uuid("files.id", ondelete="SET NULL", nullable=True)
    pdf_file_id: Mapped[uuid.UUID | None] = fk_uuid("files.id", ondelete="SET NULL", nullable=True)
    target_all: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    published_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = fk_uuid("users.id")
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)  # last editor; NULL = never edited
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"), default=1)
    high_priority_pushed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), default=False)


class AnnouncementDepartment(Base):
    __tablename__ = "announcement_departments"
    __table_args__ = (Index("ix_announcement_departments_department_id", "department_id"),)

    announcement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("announcements.id", ondelete="CASCADE"), primary_key=True)
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="RESTRICT"), primary_key=True)


class AnnouncementRead(Base):
    __tablename__ = "announcement_reads"
    __table_args__ = (Index("ix_announcement_reads_user_id", "user_id"),)

    announcement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("announcements.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    read_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
