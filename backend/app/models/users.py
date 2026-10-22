from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.crypto import EncryptedString
from .access import Role
from .base import Base, CreatedMixin, TimestampMixin, fk_uuid, uuid_pk
from .org import Department, Shift


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("employee_id", name="uq_users_employee_id"),
        UniqueConstraint("official_email", name="uq_users_official_email"),
        CheckConstraint("status IN ('active','inactive')", name="ck_users_status"),
        CheckConstraint("manager_id IS NULL OR manager_id <> id", name="ck_users_manager_not_self"),
        CheckConstraint("failed_login_count >= 0", name="ck_users_failed_login_count"),
        CheckConstraint("employee_id::text = upper(btrim(employee_id::text)) AND employee_id::text <> ''",
                        name="ck_users_employee_id_canonical"),
        Index("ix_users_role_id", "role_id"),
        Index("ix_users_department_id", "department_id"),
        Index("ix_users_shift_id", "shift_id"),
        Index("ix_users_manager_id", "manager_id"),
        Index("ix_users_status", "status"),
        Index("ix_users_deactivated_by", "deactivated_by"),
        CheckConstraint("deactivation_reason IS NULL OR deactivation_reason IN "
                        "('resigned','terminated','contract_end','absconded','other')", name="ck_users_deactivation_reason"),
        CheckConstraint("status = 'inactive' OR (deactivated_at IS NULL AND deactivation_reason IS NULL)",
                        name="ck_users_deactivation_only_inactive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    employee_id: Mapped[str] = mapped_column(CITEXT, nullable=False)  # canonical upper case, never reused
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    official_email: Mapped[str | None] = mapped_column(CITEXT)
    role_id: Mapped[uuid.UUID] = fk_uuid("roles.id")
    department_id: Mapped[uuid.UUID | None] = fk_uuid("departments.id", nullable=True)
    shift_id: Mapped[uuid.UUID | None] = fk_uuid("shifts.id", nullable=True)
    manager_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when an admin issues a temporary password (create / reset); login refuses it afterwards. NULL = no expiry.
    temp_password_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Access tokens with iat < token_valid_after are rejected (logout, logout-all, password change, admin reset).
    token_valid_after: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    join_date: Mapped[dt.date | None] = mapped_column(Date)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    # Offboarding: set by deactivate, cleared by activate (contract s.12.1).
    deactivated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    deactivation_reason: Mapped[str | None] = mapped_column(Text)

    role: Mapped[Role] = relationship(lazy="joined")
    department: Mapped[Department | None] = relationship(lazy="joined")
    shift: Mapped[Shift | None] = relationship(lazy="joined")
    manager: Mapped["User | None"] = relationship(foreign_keys=[manager_id], remote_side="User.id")
    profile: Mapped["EmployeeProfile | None"] = relationship(back_populates="user", uselist=False)


class File(CreatedMixin, Base):
    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint("purpose IN ('selfie','leave_attachment','announcement_image','announcement_pdf','profile_photo','company_logo')",
                        name="ck_files_purpose"),
        CheckConstraint("size_bytes >= 0", name="ck_files_size_bytes"),
        Index("ix_files_owner_id", "owner_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    owner_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str | None] = mapped_column(Text)
    is_encrypted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class EmployeeProfile(TimestampMixin, Base):
    """Sensitive columns are Fernet-encrypted (app.core.crypto); not SQL-filterable.

    Python attribute names drop the _enc suffix; DB column names keep it.
    """
    __tablename__ = "employee_profiles"
    __table_args__ = (
        Index("ix_employee_profiles_photo_file_id", "photo_file_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    photo_file_id: Mapped[uuid.UUID | None] = fk_uuid("files.id", ondelete="SET NULL", nullable=True)
    dob: Mapped[str | None] = mapped_column("dob_enc", EncryptedString)  # ISO date string
    wedding_anniversary: Mapped[str | None] = mapped_column("wedding_anniversary_enc", EncryptedString)
    personal_email: Mapped[str | None] = mapped_column("personal_email_enc", EncryptedString)
    phone: Mapped[str | None] = mapped_column("phone_enc", EncryptedString)
    emergency_name: Mapped[str | None] = mapped_column("emergency_name_enc", EncryptedString)
    emergency_relationship: Mapped[str | None] = mapped_column("emergency_relationship_enc", EncryptedString)
    emergency_phone: Mapped[str | None] = mapped_column("emergency_phone_enc", EncryptedString)
    blood_group: Mapped[str | None] = mapped_column("blood_group_enc", EncryptedString)  # value set validated in the app
    address: Mapped[str | None] = mapped_column("address_enc", EncryptedString)
    show_celebrations: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))  # opt-in (review 07 M2)

    user: Mapped[User] = relationship(back_populates="profile")
