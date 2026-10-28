from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import CITEXT, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import EncryptedString

from .base import Base, TimestampMixin, fk_uuid, uuid_pk


class ClientLocation(TimestampMixin, Base):
    """A client site set by the admin. On an approved on-duty day the employee can punch only inside its radius."""
    __tablename__ = "client_locations"
    __table_args__ = (
        UniqueConstraint("name", name="uq_client_locations_name"),
        CheckConstraint("lat BETWEEN -90 AND 90", name="ck_client_locations_lat"),
        CheckConstraint("lng BETWEEN -180 AND 180", name="ck_client_locations_lng"),
        CheckConstraint("radius_m BETWEEN 20 AND 5000", name="ck_client_locations_radius"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(CITEXT, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    radius_m: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("200"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class OnDutyRequest(TimestampMixin, Base):
    """Full working days at a client location. No balance: an approved day is worked (punched at the client site)."""
    __tablename__ = "on_duty_requests"
    __table_args__ = (
        CheckConstraint("to_date >= from_date", name="ck_on_duty_requests_dates"),
        CheckConstraint("days > 0", name="ck_on_duty_requests_days"),
        CheckConstraint("status IN ('pending','approved','rejected','cancelled')", name="ck_on_duty_requests_status"),
        ExcludeConstraint(
            ("user_id", "="), (text("daterange(from_date, to_date, '[]')"), "&&"),
            using="gist", where=text("status IN ('pending','approved')"), name="ex_on_duty_requests_no_overlap"),
        Index("ix_on_duty_requests_status", "status"),
        Index("ix_on_duty_requests_user_from_date", "user_id", "from_date"),
        Index("ix_on_duty_requests_client_location_id", "client_location_id"),
        Index("ix_on_duty_requests_approver_id", "approver_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    client_location_id: Mapped[uuid.UUID] = fk_uuid("client_locations.id")
    from_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    to_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)  # working days, computed at submit time
    # Encrypted at rest like leave/permission text. NULL only after the retention job purged it.
    reason: Mapped[str | None] = mapped_column("reason_enc", EncryptedString)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    approver_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    decision_comment: Mapped[str | None] = mapped_column("decision_comment_enc", EncryptedString)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
