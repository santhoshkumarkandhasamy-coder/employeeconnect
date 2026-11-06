from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, Index, Integer, Text, Time, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import EncryptedJSON
from .base import Base, CreatedMixin, TimestampMixin, fk_uuid, uuid_pk


class Holiday(TimestampMixin, Base):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("date", name="uq_holidays_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Attendance(TimestampMixin, Base):
    __tablename__ = "attendance"
    __table_args__ = (
        UniqueConstraint("user_id", "work_date", name="uq_attendance_user_work_date"),
        CheckConstraint("status IN ('present','late','half_day')", name="ck_attendance_status"),
        CheckConstraint("check_out_at IS NULL OR check_out_at > check_in_at", name="ck_attendance_checkout_after_checkin"),
        CheckConstraint("minutes_late >= 0", name="ck_attendance_minutes_late"),
        CheckConstraint("worked_minutes IS NULL OR worked_minutes >= 0", name="ck_attendance_worked_minutes"),
        CheckConstraint("check_in_distance_m IS NULL OR check_in_distance_m >= 0", name="ck_attendance_distance"),
        Index("ix_attendance_work_date", "work_date"),
        Index("ix_attendance_status", "status"),
        Index("ix_attendance_shift_id", "shift_id"),
        Index("ix_attendance_open", "work_date", postgresql_where=text("check_out_at IS NULL")),
        Index("ix_attendance_suspicious", "work_date", postgresql_where=text("suspicious")),
        # a selfie file can back at most one punch (anti-replay); cross in/out reuse is checked in the service
        Index(
            "uq_attendance_check_in_selfie",
            "check_in_selfie_file_id",
            unique=True,
            postgresql_where=text("check_in_selfie_file_id IS NOT NULL"),
        ),
        Index(
            "uq_attendance_check_out_selfie",
            "check_out_selfie_file_id",
            unique=True,
            postgresql_where=text("check_out_selfie_file_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    work_date: Mapped[dt.date] = mapped_column(Date, nullable=False)  # office timezone
    shift_id: Mapped[uuid.UUID | None] = fk_uuid("shifts.id", nullable=True)  # snapshot
    check_in_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    check_in_loc: Mapped[dict | None] = mapped_column("check_in_loc_enc", EncryptedJSON, deferred=True)
    check_in_distance_m: Mapped[int | None] = mapped_column(Integer)
    check_in_within_fence: Mapped[bool | None] = mapped_column(Boolean)
    check_in_selfie_file_id: Mapped[uuid.UUID | None] = fk_uuid("files.id", ondelete="SET NULL", nullable=True)
    check_out_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    check_out_loc: Mapped[dict | None] = mapped_column("check_out_loc_enc", EncryptedJSON, deferred=True)
    check_out_selfie_file_id: Mapped[uuid.UUID | None] = fk_uuid("files.id", ondelete="SET NULL", nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'present'"))
    minutes_late: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    worked_minutes: Mapped[int | None] = mapped_column(Integer)
    auto_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # anti-spoofing evidence (plan: security review 02 H1). Never returned to employees.
    check_in_accuracy_m: Mapped[float | None] = mapped_column(Float)
    check_in_clock_skew_s: Mapped[int | None] = mapped_column(Integer)  # server now - client_time, diagnostics only
    check_in_mock_location: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    check_out_mock_location: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    suspicious: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class LateRecord(CreatedMixin, Base):
    __tablename__ = "late_records"
    __table_args__ = (
        UniqueConstraint("attendance_id", name="uq_late_records_attendance_id"),
        UniqueConstraint("user_id", "work_date", name="uq_late_records_user_work_date"),
        CheckConstraint("minutes_late > 0", name="ck_late_records_minutes_late"),
        CheckConstraint("grace_minutes >= 0", name="ck_late_records_grace"),
        Index("ix_late_records_work_date", "work_date"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    attendance_id: Mapped[uuid.UUID] = fk_uuid("attendance.id", ondelete="CASCADE")
    work_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    shift_start: Mapped[dt.time] = mapped_column(Time, nullable=False)
    grace_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    check_in_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    minutes_late: Mapped[int] = mapped_column(Integer, nullable=False)
