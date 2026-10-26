"""On Duty tables (migration 0019, plan docs/plans/13-on-duty.md section 2).

Visit coordinates, the visit note, the request purpose and the decision comment are application-encrypted (`*_enc` columns,
python attribute without the suffix). Duty location coordinates are business data and stay in clear.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import EncryptedJSON, EncryptedString
from .base import Base, TimestampMixin, fk_uuid, uuid_pk


class DutyLocation(TimestampMixin, Base):
    __tablename__ = "duty_locations"
    __table_args__ = (
        CheckConstraint("char_length(btrim(name)) BETWEEN 2 AND 80", name="ck_duty_locations_name"),
        CheckConstraint("address IS NULL OR char_length(address) <= 300", name="ck_duty_locations_address"),
        CheckConstraint("lat BETWEEN -90 AND 90", name="ck_duty_locations_lat"),
        CheckConstraint("lng BETWEEN -180 AND 180", name="ck_duty_locations_lng"),
        CheckConstraint("radius_m BETWEEN 10 AND 2000", name="ck_duty_locations_radius"),
        CheckConstraint("assign_mode IN ('all','selected')", name="ck_duty_locations_assign_mode"),
        Index("uq_duty_locations_name", text("lower(btrim(name))"), unique=True),
        Index("ix_duty_locations_active", "is_active"),
        Index("ix_duty_locations_created_by", "created_by"),
        Index("ix_duty_locations_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float] = mapped_column(Float, nullable=False)  # double precision
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    radius_m: Mapped[int] = mapped_column(Integer, nullable=False)
    assign_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'selected'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class DutyLocationAssignment(Base):
    """Ignored while the location's assign_mode is 'all' (rows are kept so switching back restores the list)."""
    __tablename__ = "duty_location_assignments"
    __table_args__ = (
        Index("ix_duty_location_assignments_user", "user_id"),
        Index("ix_duty_location_assignments_created_by", "created_by"),
    )

    location_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("duty_locations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", ondelete="SET NULL", nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OnDutyRequest(TimestampMixin, Base):
    __tablename__ = "on_duty_requests"
    __table_args__ = (
        CheckConstraint("to_date >= from_date", name="ck_on_duty_requests_dates"),
        CheckConstraint("to_date - from_date <= 30", name="ck_on_duty_requests_span"),
        CheckConstraint("status IN ('pending','approved','rejected','cancelled')", name="ck_on_duty_requests_status"),
        Index("ix_on_duty_requests_user_from", "user_id", "from_date"),
        Index("ix_on_duty_requests_status", "status"),
        Index("ix_on_duty_requests_dates", "from_date", "to_date"),
        Index("ix_on_duty_requests_location_id", "location_id"),
        Index("ix_on_duty_requests_approver_id", "approver_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    from_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    to_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    location_id: Mapped[uuid.UUID | None] = fk_uuid("duty_locations.id", nullable=True)  # NULL = any assigned location
    purpose: Mapped[str] = mapped_column("purpose_enc", EncryptedString, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    approver_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    decision_comment: Mapped[str | None] = mapped_column("decision_comment_enc", EncryptedString)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class OnDutyVisit(TimestampMixin, Base):
    __tablename__ = "on_duty_visits"
    __table_args__ = (
        CheckConstraint("check_out_at IS NULL OR check_out_at > check_in_at", name="ck_on_duty_visits_checkout_after_checkin"),
        CheckConstraint("check_in_distance_m IS NULL OR check_in_distance_m >= 0", name="ck_on_duty_visits_in_distance"),
        CheckConstraint("check_out_distance_m IS NULL OR check_out_distance_m >= 0", name="ck_on_duty_visits_out_distance"),
        Index("ix_on_duty_visits_user_date", "user_id", "work_date"),
        Index("ix_on_duty_visits_work_date", "work_date"),
        Index("ix_on_duty_visits_location_id", "location_id"),
        Index("ix_on_duty_visits_attendance_id", "attendance_id"),
        Index("ix_on_duty_visits_request_id", "request_id"),
        # race backstop: one open visit per user (an auto-closed visit keeps check_out_at NULL but no longer counts as open)
        Index(
            "uq_on_duty_visits_one_open",
            "user_id",
            unique=True,
            postgresql_where=text("check_out_at IS NULL AND auto_closed = false"),
        ),
        Index(
            "uq_on_duty_visits_check_in_selfie",
            "check_in_selfie_file_id",
            unique=True,
            postgresql_where=text("check_in_selfie_file_id IS NOT NULL"),
        ),
        Index(
            "uq_on_duty_visits_check_out_selfie",
            "check_out_selfie_file_id",
            unique=True,
            postgresql_where=text("check_out_selfie_file_id IS NOT NULL"),
        ),
        Index("ix_on_duty_visits_suspicious", "work_date", postgresql_where=text("suspicious")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    attendance_id: Mapped[uuid.UUID] = fk_uuid("attendance.id", ondelete="CASCADE")
    work_date: Mapped[dt.date] = mapped_column(Date, nullable=False)  # office timezone
    location_id: Mapped[uuid.UUID] = fk_uuid("duty_locations.id")
    location_name: Mapped[str] = mapped_column(Text, nullable=False)  # snapshot
    request_id: Mapped[uuid.UUID | None] = fk_uuid("on_duty_requests.id", ondelete="SET NULL", nullable=True)
    check_in_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    check_in_loc: Mapped[dict | None] = mapped_column("check_in_loc_enc", EncryptedJSON, deferred=True)
    check_in_distance_m: Mapped[int | None] = mapped_column(Integer)
    check_in_accuracy_m: Mapped[float | None] = mapped_column(Float)
    check_in_clock_skew_s: Mapped[int | None] = mapped_column(Integer)
    check_in_mock_location: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    check_in_selfie_file_id: Mapped[uuid.UUID | None] = fk_uuid("files.id", ondelete="SET NULL", nullable=True)
    check_out_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    check_out_loc: Mapped[dict | None] = mapped_column("check_out_loc_enc", EncryptedJSON, deferred=True)
    check_out_distance_m: Mapped[int | None] = mapped_column(Integer)
    check_out_within_fence: Mapped[bool | None] = mapped_column(Boolean)
    check_out_accuracy_m: Mapped[float | None] = mapped_column(Float)
    check_out_mock_location: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    check_out_selfie_file_id: Mapped[uuid.UUID | None] = fk_uuid("files.id", ondelete="SET NULL", nullable=True)
    note: Mapped[str | None] = mapped_column("note_enc", EncryptedString)
    auto_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    suspicious: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
