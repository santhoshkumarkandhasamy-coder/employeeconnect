from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CHAR, BigInteger, CheckConstraint, DateTime, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedMixin, fk_uuid, uuid_pk


class RefreshToken(Base):
    """Only the SHA-256 hex digest of the token is stored."""
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        CheckConstraint("expires_at > issued_at", name="ck_refresh_tokens_expiry"),
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_family_id", "family_id"),
        Index("ix_refresh_tokens_expires_at", "expires_at"),
        Index("ix_refresh_tokens_replaced_by", "replaced_by"),
        CheckConstraint("client_type IN ('mobile','web')", name="ck_refresh_tokens_client_type"),
        CheckConstraint("app_version IS NULL OR char_length(app_version) <= 32", name="ck_refresh_tokens_app_version"),
        CheckConstraint("platform IS NULL OR platform IN ('android','ios','web')", name="ck_refresh_tokens_platform"),
        Index("ix_refresh_tokens_live", "user_id", "family_id",
              postgresql_where=text("revoked_at IS NULL AND replaced_by IS NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id", ondelete="CASCADE")
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    device_id: Mapped[str | None] = mapped_column(Text)
    device_name: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # When the first token of this family was issued; rotation never extends a session past this + SESSION_MAX_DAYS.
    family_started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[uuid.UUID | None] = fk_uuid("refresh_tokens.id", ondelete="SET NULL", nullable=True)
    client_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'mobile'"))
    app_version: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(Text)


class DeviceToken(CreatedMixin, Base):
    __tablename__ = "device_tokens"
    __table_args__ = (
        UniqueConstraint("fcm_token", name="uq_device_tokens_fcm_token"),
        CheckConstraint("platform IN ('android','ios')", name="ck_device_tokens_platform"),
        Index("ix_device_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id", ondelete="CASCADE")
    fcm_token: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PasswordResetRequest(Base):
    __tablename__ = "password_reset_requests"
    __table_args__ = (
        CheckConstraint("status IN ('open','handled','expired')", name="ck_password_reset_requests_status"),
        Index("uq_password_reset_requests_one_open", "user_id", unique=True, postgresql_where=text("status = 'open'")),
        Index("ix_password_reset_requests_status", "status"),
        Index("ix_password_reset_requests_handled_by", "handled_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    handled_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    handled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only. DB trigger rejects UPDATE/DELETE/TRUNCATE; app_user has INSERT/SELECT only.

    Never call session.merge/update/delete on this model.
    """
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("outcome IN ('success','failure')", name="ck_audit_logs_outcome"),
        Index("ix_audit_logs_ts", "ts"),
        Index("ix_audit_logs_actor_id", "actor_id"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_action", "action"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    actor_employee_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'success'"))
    row_hmac: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = (Index("ix_settings_updated_by", "updated_by"),)

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[object] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
