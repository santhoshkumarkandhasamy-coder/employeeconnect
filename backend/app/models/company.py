from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, SmallInteger, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, fk_uuid


class Company(TimestampMixin, Base):
    """Singleton (id = 1), created by migration 0017. 'Configured' = setup_completed_at IS NOT NULL."""
    __tablename__ = "company"
    __table_args__ = (
        UniqueConstraint("deployment_id", name="uq_company_deployment_id"),
        CheckConstraint("id = 1", name="ck_company_singleton"),
        CheckConstraint("name IS NULL OR char_length(btrim(name)) BETWEEN 2 AND 100", name="ck_company_name"),
        CheckConstraint("logo_version >= 0", name="ck_company_logo_version"),
        CheckConstraint("setup_completed_at IS NULL OR name IS NOT NULL", name="ck_company_setup_has_name"),
        Index("ix_company_logo_file_id", "logo_file_id"),
        Index("ix_company_created_by", "created_by"),
        Index("ix_company_updated_by", "updated_by"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("1"))
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()"))
    name: Mapped[str | None] = mapped_column(Text)
    logo_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL"), nullable=True)
    logo_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    setup_completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class License(Base):
    """Singleton (id = 1). Parsed columns are a display copy; the server always re-verifies `token`. No row = unlicensed
    (built-in default 50 seats, no expiry, admins count; code constant, not stored). expires_at NULL = never expires.

    Seat lock: SELECT ... FOR UPDATE on this row (company row when absent) BEFORE locking any users rows.
    """
    __tablename__ = "license"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_license_singleton"),
        CheckConstraint("char_length(token) <= 4096", name="ck_license_token_len"),
        CheckConstraint("max_users BETWEEN 1 AND 100000", name="ck_license_max_users"),
        CheckConstraint("expires_at IS NULL OR expires_at > issued_at", name="ck_license_expiry"),
        CheckConstraint("grace_days BETWEEN 0 AND 90", name="ck_license_grace_days"),
        CheckConstraint("on_expiry IN ('block_new_users','read_only')", name="ck_license_on_expiry"),
        Index("ix_license_installed_by", "installed_by"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("1"))
    token: Mapped[str] = mapped_column(Text, nullable=False)
    license_id: Mapped[str] = mapped_column(Text, nullable=False)
    licensed_to: Mapped[str] = mapped_column(Text, nullable=False)
    max_users: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    grace_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("14"))
    on_expiry: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'block_new_users'"))
    installed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    installed_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
