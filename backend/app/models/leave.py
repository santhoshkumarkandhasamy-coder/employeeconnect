from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Index, Integer, Numeric, Text, Time, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import EncryptedString

from .base import Base, TimestampMixin, fk_uuid, uuid_pk

_REQ_STATUS = "status IN ('pending','approved','rejected','cancelled')"


class LeaveType(TimestampMixin, Base):
    __tablename__ = "leave_types"
    __table_args__ = (
        UniqueConstraint("code", name="uq_leave_types_code"),
        UniqueConstraint("name", name="uq_leave_types_name"),
        CheckConstraint("annual_quota >= 0", name="ck_leave_types_annual_quota"),
        CheckConstraint("requires_attachment_after_days IS NULL OR requires_attachment_after_days >= 0",
                        name="ck_leave_types_attachment_days"),
        CheckConstraint("max_consecutive_days IS NULL OR max_consecutive_days > 0", name="ck_leave_types_max_consecutive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    annual_quota: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False, server_default=text("0"))
    is_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    allow_half_day: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    requires_attachment_after_days: Mapped[int | None] = mapped_column(Integer)
    max_consecutive_days: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class LeaveBalance(TimestampMixin, Base):
    """remaining = quota - used - pending. DB guarantees used, pending >= 0 and used + pending <= quota."""
    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint("user_id", "leave_type_id", "year", name="uq_leave_balances_user_type_year"),
        CheckConstraint("year BETWEEN 2000 AND 2200", name="ck_leave_balances_year"),
        CheckConstraint("quota >= 0 AND used >= 0 AND pending >= 0", name="ck_leave_balances_non_negative"),
        CheckConstraint("used + pending <= quota", name="ck_leave_balances_not_overdrawn"),
        Index("ix_leave_balances_leave_type_id", "leave_type_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    leave_type_id: Mapped[uuid.UUID] = fk_uuid("leave_types.id")
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    quota: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False, server_default=text("0"))
    used: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False, server_default=text("0"))
    pending: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False, server_default=text("0"))


class LeaveRequest(TimestampMixin, Base):
    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint("to_date >= from_date", name="ck_leave_requests_dates"),
        CheckConstraint("NOT half_day OR from_date = to_date", name="ck_leave_requests_half_day_single_date"),
        CheckConstraint("(half_day AND half_day_session IN ('first','second')) OR (NOT half_day AND half_day_session IS NULL)",
                        name="ck_leave_requests_half_day_session"),
        CheckConstraint("days > 0", name="ck_leave_requests_days"),
        CheckConstraint(_REQ_STATUS, name="ck_leave_requests_status"),
        ExcludeConstraint(
            ("user_id", "="), (text("daterange(from_date, to_date, '[]')"), "&&"),
            using="gist", where=text("status IN ('pending','approved')"), name="ex_leave_requests_no_overlap"),
        Index("ix_leave_requests_status", "status"),
        Index("ix_leave_requests_user_from_date", "user_id", "from_date"),
        Index("ix_leave_requests_leave_type_id", "leave_type_id"),
        Index("ix_leave_requests_approver_id", "approver_id"),
        Index("ix_leave_requests_attachment_file_id", "attachment_file_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    leave_type_id: Mapped[uuid.UUID] = fk_uuid("leave_types.id")
    from_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    to_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    half_day: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    half_day_session: Mapped[str | None] = mapped_column(Text)
    days: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    # Encrypted at rest (review 03 M1). NULL only after the retention job purged it.
    reason: Mapped[str | None] = mapped_column("reason_enc", EncryptedString)
    attachment_file_id: Mapped[uuid.UUID | None] = fk_uuid("files.id", ondelete="SET NULL", nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    approver_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    decision_comment: Mapped[str | None] = mapped_column("decision_comment_enc", EncryptedString)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PermissionRequest(TimestampMixin, Base):
    __tablename__ = "permission_requests"
    __table_args__ = (
        CheckConstraint("to_time > from_time", name="ck_permission_requests_times"),
        CheckConstraint("minutes > 0", name="ck_permission_requests_minutes"),
        CheckConstraint(_REQ_STATUS, name="ck_permission_requests_status"),
        ExcludeConstraint(
            ("user_id", "="),
            (text("tsrange((request_date + from_time), (request_date + to_time))"), "&&"),
            using="gist", where=text("status IN ('pending','approved')"), name="ex_permission_requests_no_overlap"),
        Index("ix_permission_requests_status", "status"),
        Index("ix_permission_requests_user_date", "user_id", "request_date"),
        Index("ix_permission_requests_approver_id", "approver_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    request_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    from_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    to_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # Encrypted at rest (review 03 M1). NULL only after the retention job purged it.
    reason: Mapped[str | None] = mapped_column("reason_enc", EncryptedString)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    approver_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    decision_comment: Mapped[str | None] = mapped_column("decision_comment_enc", EncryptedString)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
