"""eSSL / ZK attendance devices (plan 18, migration 0030). Read-only integration: no biometric data is stored anywhere."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import EncryptedString
from .base import Base, TimestampMixin, fk_uuid, uuid_pk


class Device(TimestampMixin, Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("ip", "port", name="uq_devices_ip_port"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 60", name="ck_devices_name"),
        CheckConstraint("port BETWEEN 1 AND 65535", name="ck_devices_port"),
        CheckConstraint("transport IN ('tcp','udp')", name="ck_devices_transport"),
        CheckConstraint("location_label IS NULL OR char_length(location_label) <= 80", name="ck_devices_location_label"),
        CheckConstraint("clock_correction IN ('auto','off')", name="ck_devices_clock_correction"),
        CheckConstraint("clock_status IS NULL OR clock_status IN ('ok','drift','invalid')", name="ck_devices_clock_status"),
        CheckConstraint("consecutive_failures >= 0", name="ck_devices_failures"),
        CheckConstraint(
            "(last_log_count IS NULL OR last_log_count >= 0) AND (last_user_count IS NULL OR last_user_count >= 0)",
            name="ck_devices_counts",
        ),
        Index("uq_devices_name_lower", text("lower(name)"), unique=True),
        Index("ix_devices_created_by", "created_by"),
        Index("ix_devices_enabled", "enabled"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ip: Mapped[str] = mapped_column(INET, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("4370"))
    transport: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'tcp'"))
    comm_key: Mapped[str | None] = mapped_column("comm_key_enc", EncryptedString)  # numeric 0..999999, Fernet
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    location_label: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str | None] = mapped_column(Text)  # IANA; NULL = office timezone
    clock_correction: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'auto'"))
    ingest_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    serial_number: Mapped[str | None] = mapped_column(Text)  # pinned identity
    model: Mapped[str | None] = mapped_column(Text)
    firmware: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(Text)
    last_clock_offset_s: Mapped[int | None] = mapped_column(Integer)
    clock_status: Mapped[str | None] = mapped_column(Text)
    last_log_count: Mapped[int | None] = mapped_column(Integer)
    last_user_count: Mapped[int | None] = mapped_column(Integer)
    last_users_read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    circuit_open_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_alert_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class DeviceSync(Base):
    """History and job status of a sync / test run. device_id NULL = test of an unsaved device."""
    __tablename__ = "device_syncs"
    __table_args__ = (
        CheckConstraint("kind IN ('schedule','manual','reconcile','test')", name="ck_device_syncs_kind"),
        CheckConstraint("status IN ('queued','running','ok','failed','skipped')", name="ck_device_syncs_status"),
        CheckConstraint(
            "(records_on_device IS NULL OR records_on_device >= 0) AND (users_on_device IS NULL OR users_on_device >= 0)"
            " AND (fetched IS NULL OR fetched >= 0) AND (inserted IS NULL OR inserted >= 0)"
            " AND (quarantined IS NULL OR quarantined >= 0) AND (days_recomputed IS NULL OR days_recomputed >= 0)",
            name="ck_device_syncs_counts",
        ),
        CheckConstraint("result IS NULL OR kind = 'test'", name="ck_device_syncs_result"),
        Index("ix_device_syncs_device_queued", "device_id", text("queued_at DESC")),
        Index("ix_device_syncs_requested_by", "requested_by"),
        Index("ix_device_syncs_open", "status", postgresql_where=text("status IN ('queued','running')")),
        Index("ix_device_syncs_queued_at", "queued_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    device_id: Mapped[uuid.UUID | None] = fk_uuid("devices.id", ondelete="CASCADE", nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    error_code: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    records_on_device: Mapped[int | None] = mapped_column(Integer)
    users_on_device: Mapped[int | None] = mapped_column(Integer)
    fetched: Mapped[int | None] = mapped_column(Integer)
    inserted: Mapped[int | None] = mapped_column(Integer)
    quarantined: Mapped[int | None] = mapped_column(Integer)
    days_recomputed: Mapped[int | None] = mapped_column(Integer)
    clock_offset_s: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[dict | None] = mapped_column(JSONB)  # test only: serial, model, firmware, platform, device_time, counts


class DeviceUser(TimestampMixin, Base):
    """One user slot seen on a device. The device PIN and any template are never stored."""
    __tablename__ = "device_users"
    __table_args__ = (
        UniqueConstraint("device_id", "device_user_id", name="uq_device_users_device_user"),
        CheckConstraint("char_length(device_user_id) BETWEEN 1 AND 24", name="ck_device_users_user_id"),
        CheckConstraint("match_method IN ('auto','manual','unmatched','ignored')", name="ck_device_users_match_method"),
        CheckConstraint("(user_id IS NOT NULL) = (match_method IN ('auto','manual'))", name="ck_device_users_mapped"),
        Index("ix_device_users_user_id", "user_id"),
        Index("ix_device_users_matched_by", "matched_by"),
        Index("ix_device_users_unmatched", "device_id", "device_user_id", postgresql_where=text("match_method = 'unmatched'")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    device_id: Mapped[uuid.UUID] = fk_uuid("devices.id")
    device_uid: Mapped[int | None] = mapped_column(Integer)
    device_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column("name_enc", EncryptedString)
    card: Mapped[str | None] = mapped_column("card_enc", EncryptedString)
    is_device_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    user_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    match_method: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'unmatched'"))
    matched_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    matched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    present_on_device: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DevicePunch(Base):
    """Raw punch, append-only (trigger). UPDATE/TRUNCATE never; DELETE only after 90 days (retention job)."""
    __tablename__ = "device_punches"
    __table_args__ = (
        UniqueConstraint("device_id", "device_user_id", "device_time", name="uq_device_punches_device_user_time"),
        CheckConstraint("char_length(device_user_id) BETWEEN 1 AND 24", name="ck_device_punches_user_id"),
        CheckConstraint("quarantine IS NULL OR quarantine IN ('clock_invalid','future')", name="ck_device_punches_quarantine"),
        Index("ix_device_punches_user_punch_at", "device_user_id", "punch_at"),
        Index("ix_device_punches_punch_at", "punch_at"),
        Index("ix_device_punches_sync_id", "sync_id", postgresql_where=text("sync_id IS NOT NULL")),
    )

    # GENERATED ALWAYS AS IDENTITY in PostgreSQL
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID] = fk_uuid("devices.id")
    device_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    device_uid: Mapped[int | None] = mapped_column(Integer)
    device_time: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False), nullable=False)  # raw device clock, naive
    punch_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # corrected, UTC
    offset_applied_s: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    state: Mapped[int | None] = mapped_column(SmallInteger)
    verify_mode: Mapped[int | None] = mapped_column(SmallInteger)
    quarantine: Mapped[str | None] = mapped_column(Text)
    sync_id: Mapped[uuid.UUID | None] = fk_uuid("device_syncs.id", ondelete="SET NULL", nullable=True)
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DevicePunchOutcome(Base):
    """Mutable result of the latest recompute for one punch."""
    __tablename__ = "device_punch_outcomes"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('check_in','check_out','extra','unmatched','ignored','quarantined','not_applied')",
            name="ck_device_punch_outcomes_outcome",
        ),
        CheckConstraint(
            "reason IS NULL OR reason IN ('on_leave','on_duty','no_shift','inactive_user','manual_entry','outside_window',"
            "'payroll_locked','mapping_ignored','clock_invalid','future','duplicate')",
            name="ck_device_punch_outcomes_reason",
        ),
        Index("ix_device_punch_outcomes_user_work_date", "user_id", "work_date"),
        Index("ix_device_punch_outcomes_work_date", "work_date"),
        Index("ix_device_punch_outcomes_outcome", "outcome"),
    )

    punch_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("device_punches.id", ondelete="CASCADE"), primary_key=True, autoincrement=False)
    user_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    work_date: Mapped[dt.date | None] = mapped_column(Date)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
