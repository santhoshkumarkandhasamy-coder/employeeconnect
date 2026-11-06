"""Payroll tax data (migration 0027, plan docs/plans/17-payroll-phase5.md work package 5b): tax year rules, employee declarations,
proofs and uploaded Form 16 documents.

Tax slabs, rates and limits live ONLY in `TaxYearRule.rules` (data, not secret), verified by a second person. The database repeats the
maker-checker rule (ck_tax_year_rules_verifier) and guards the lifecycles with triggers (tax_year_rules_lock, tax_declarations_lock,
tax_proofs_lock, form16_documents_lock); the SQLite test harness knows none of them.
Declarations, claimed / verified amounts and unlock reasons are application-encrypted (`*_enc`); the python attribute drops the suffix.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import EncryptedJSON, EncryptedString
from .base import Base, TimestampMixin, fk_uuid, uuid_pk

_FY = "~ '^[0-9]{4}-[0-9]{2}$'"


class TaxYearRule(TimestampMixin, Base):
    """Versioned tax rules of one financial year. draft -> submitted -> verified -> superseded; the verifier is never a party
    (ck_tax_year_rules_verifier). `rules` is plain JSON (not secret); `windows` (declaration / proof dates) stays editable on every status.
    created_by / last_edited_by are NULL only for the migration seed."""

    __tablename__ = "tax_year_rules"
    __table_args__ = (
        UniqueConstraint("fy", "version", name="uq_tax_year_rules_fy_version"),
        CheckConstraint(f"fy {_FY}", name="ck_tax_year_rules_fy"),
        CheckConstraint("version >= 1", name="ck_tax_year_rules_version"),
        CheckConstraint("status IN ('draft','submitted','verified','superseded')", name="ck_tax_year_rules_status"),
        CheckConstraint("jsonb_typeof(rules) = 'object'", name="ck_tax_year_rules_rules"),
        CheckConstraint("jsonb_typeof(windows) = 'object'", name="ck_tax_year_rules_windows"),
        CheckConstraint("rules_sha256 ~ '^[0-9a-f]{64}$'", name="ck_tax_year_rules_sha256"),
        CheckConstraint("attestation IS NULL OR char_length(attestation) BETWEEN 20 AND 500", name="ck_tax_year_rules_attestation"),
        CheckConstraint("(submitted_by IS NULL) = (submitted_at IS NULL)", name="ck_tax_year_rules_submitted_pair"),
        CheckConstraint("(verified_by IS NULL) = (verified_at IS NULL)", name="ck_tax_year_rules_verified_pair"),
        CheckConstraint("(status IN ('verified','superseded')) = (verified_by IS NOT NULL)", name="ck_tax_year_rules_verified_status"),
        CheckConstraint("status NOT IN ('verified','superseded') OR attestation IS NOT NULL", name="ck_tax_year_rules_attested"),
        CheckConstraint(
            "status NOT IN ('submitted','verified','superseded') OR submitted_by IS NOT NULL", name="ck_tax_year_rules_submitted_status"
        ),
        CheckConstraint(
            "(created_by IS NULL OR created_by::text = ANY (contributors)) AND (last_edited_by IS NULL OR last_edited_by::text = ANY (contributors)) "
            "AND (submitted_by IS NULL OR submitted_by::text = ANY (contributors))",
            name="ck_tax_year_rules_contributors",
        ),
        CheckConstraint(
            "verified_by IS NULL OR (verified_by IS DISTINCT FROM created_by AND verified_by IS DISTINCT FROM last_edited_by "
            "AND verified_by IS DISTINCT FROM submitted_by AND verified_by::text <> ALL (contributors))",
            name="ck_tax_year_rules_verifier",
        ),
        CheckConstraint("version_row >= 1", name="ck_tax_year_rules_version_row"),
        Index("uq_tax_year_rules_verified", "fy", unique=True, postgresql_where=text("status = 'verified'")),
        Index("uq_tax_year_rules_open", "fy", unique=True, postgresql_where=text("status IN ('draft','submitted')")),
        Index("ix_tax_year_rules_status", "fy", "status"),
        Index("ix_tax_year_rules_created_by", "created_by"),
        Index("ix_tax_year_rules_last_edited_by", "last_edited_by"),
        Index("ix_tax_year_rules_submitted_by", "submitted_by"),
        Index("ix_tax_year_rules_verified_by", "verified_by"),
        Index("ix_tax_year_rules_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    fy: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    rules: Mapped[dict] = mapped_column(JSONB, nullable=False)
    rules_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    attestation: Mapped[str | None] = mapped_column(Text)
    windows: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    last_edited_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    contributors: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    submitted_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    verified_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    version_row: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))  # optimistic lock
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class TaxDeclaration(TimestampMixin, Base):
    """One declaration per (employee, FY). `draft` / `regime_draft` is what the employee edits; `submitted`, `regime`,
    `submitted_version` and `submitted_at` are frozen at submit and change only together (T3: a draft never feeds tax)."""

    __tablename__ = "tax_declarations"
    __table_args__ = (
        UniqueConstraint("user_id", "fy", name="uq_tax_declarations_user_fy"),
        CheckConstraint(f"fy {_FY}", name="ck_tax_declarations_fy"),
        CheckConstraint("regime_draft IS NULL OR regime_draft IN ('old','new')", name="ck_tax_declarations_regime_draft"),
        CheckConstraint("regime IS NULL OR regime IN ('old','new')", name="ck_tax_declarations_regime"),
        CheckConstraint("(submitted_enc IS NULL) = (submitted_version IS NULL)", name="ck_tax_declarations_submitted"),
        CheckConstraint("(submitted_enc IS NULL) = (submitted_at IS NULL)", name="ck_tax_declarations_submitted_at"),
        CheckConstraint("submitted_enc IS NULL OR regime IS NOT NULL", name="ck_tax_declarations_submitted_regime"),
        CheckConstraint("submitted_version IS NULL OR submitted_version >= 1", name="ck_tax_declarations_submitted_version"),
        CheckConstraint("(unlocked_until IS NULL) = (unlock_reason_enc IS NULL)", name="ck_tax_declarations_unlock"),
        CheckConstraint(
            "unlocked_until IS NULL OR unlocked_until BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'", name="ck_tax_declarations_unlocked_until"
        ),
        CheckConstraint("entered_by IS NULL OR entered_by <> user_id", name="ck_tax_declarations_entered_by"),
        CheckConstraint("version >= 1", name="ck_tax_declarations_version"),
        Index("ix_tax_declarations_fy", "fy", "user_id"),
        Index("ix_tax_declarations_entered_by", "entered_by"),
        Index("ix_tax_declarations_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    fy: Mapped[str] = mapped_column(Text, nullable=False)
    regime_draft: Mapped[str | None] = mapped_column(Text)
    draft: Mapped[dict | None] = mapped_column("draft_enc", EncryptedJSON)
    regime: Mapped[str | None] = mapped_column(Text)
    submitted: Mapped[dict | None] = mapped_column("submitted_enc", EncryptedJSON)
    submitted_version: Mapped[int | None] = mapped_column(Integer)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    entered_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)  # HR on behalf; set once
    unlocked_until: Mapped[dt.date | None] = mapped_column(Date)
    unlock_reason: Mapped[str | None] = mapped_column("unlock_reason_enc", EncryptedString)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class TaxProof(TimestampMixin, Base):
    """Proof of one declaration item. submitted -> verified | rejected | cancelled. `verified_by` is the decider (also for a rejection);
    it is never the employee nor the person who entered the declaration on behalf of the employee."""

    __tablename__ = "tax_proofs"
    __table_args__ = (
        CheckConstraint(f"fy {_FY}", name="ck_tax_proofs_fy"),
        CheckConstraint("char_length(item_id) BETWEEN 1 AND 64", name="ck_tax_proofs_item_id"),
        CheckConstraint("section ~ '^[A-Z0-9_]{2,16}$'", name="ck_tax_proofs_section"),
        CheckConstraint("cardinality(file_ids) BETWEEN 1 AND 10 AND array_position(file_ids, NULL::uuid) IS NULL", name="ck_tax_proofs_files"),
        CheckConstraint("status IN ('submitted','verified','rejected','cancelled')", name="ck_tax_proofs_status"),
        CheckConstraint("(verified_by IS NULL) = (verified_at IS NULL)", name="ck_tax_proofs_decided_pair"),
        CheckConstraint("(status IN ('verified','rejected')) = (verified_by IS NOT NULL)", name="ck_tax_proofs_decided_status"),
        CheckConstraint("(status = 'verified') = (verified_enc IS NOT NULL)", name="ck_tax_proofs_verified_enc"),
        CheckConstraint("status <> 'rejected' OR comment_enc IS NOT NULL", name="ck_tax_proofs_rejected_comment"),
        CheckConstraint("verified_by IS NULL OR verified_by <> user_id", name="ck_tax_proofs_verifier"),
        CheckConstraint("version >= 1", name="ck_tax_proofs_version"),
        Index("uq_tax_proofs_live_item", "declaration_id", "item_id", unique=True, postgresql_where=text("status IN ('submitted','verified')")),
        Index("ix_tax_proofs_declaration_id", "declaration_id"),
        Index("ix_tax_proofs_user_fy_status", "user_id", "fy", "status"),
        Index("ix_tax_proofs_status_created", "status", "created_at"),
        Index("ix_tax_proofs_verified_by", "verified_by"),
        Index("ix_tax_proofs_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    declaration_id: Mapped[uuid.UUID] = fk_uuid("tax_declarations.id")
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    fy: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str] = mapped_column(Text, nullable=False)
    claimed: Mapped[dict] = mapped_column("claimed_enc", EncryptedJSON, nullable=False)
    file_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)  # `files` rows, purpose tax_proof
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'submitted'"))
    verified: Mapped[dict | None] = mapped_column("verified_enc", EncryptedJSON)
    verified_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column("comment_enc", EncryptedString)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class Form16Document(TimestampMixin, Base):
    """An uploaded Form 16 / Part A / Part B / Form 12BA PDF (a `files` row, purpose form16). uploaded -> published | withdrawn,
    published -> withdrawn. `second_person` is fixed at upload: when true, `published_by` differs from `uploaded_by`."""

    __tablename__ = "form16_documents"
    __table_args__ = (
        UniqueConstraint("file_id", name="uq_form16_documents_file"),
        CheckConstraint(f"fy {_FY}", name="ck_form16_documents_fy"),
        CheckConstraint("kind IN ('form16','part_a','part_b','form12ba')", name="ck_form16_documents_kind"),
        CheckConstraint("status IN ('uploaded','published','withdrawn')", name="ck_form16_documents_status"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_form16_documents_sha256"),
        CheckConstraint("(published_by IS NULL) = (published_at IS NULL)", name="ck_form16_documents_published_pair"),
        CheckConstraint(
            "(status <> 'uploaded' OR published_by IS NULL) AND (status <> 'published' OR published_by IS NOT NULL)",
            name="ck_form16_documents_published_status",
        ),
        CheckConstraint("(withdrawn_by IS NULL) = (withdrawn_at IS NULL)", name="ck_form16_documents_withdrawn_pair"),
        CheckConstraint("(status = 'withdrawn') = (withdrawn_by IS NOT NULL)", name="ck_form16_documents_withdrawn_status"),
        CheckConstraint("status <> 'withdrawn' OR withdraw_reason_enc IS NOT NULL", name="ck_form16_documents_withdrawn_reason"),
        CheckConstraint("NOT second_person OR published_by IS NULL OR published_by <> uploaded_by", name="ck_form16_documents_second_person"),
        CheckConstraint("published_by IS NULL OR published_by <> user_id", name="ck_form16_documents_not_own"),
        CheckConstraint("version >= 1", name="ck_form16_documents_version"),
        Index("uq_form16_documents_live", "user_id", "fy", "kind", unique=True, postgresql_where=text("status IN ('uploaded','published')")),
        Index("ix_form16_documents_user_fy", "user_id", "fy"),
        Index("ix_form16_documents_fy_kind_status", "fy", "kind", "status"),
        Index("ix_form16_documents_upload_batch_id", "upload_batch_id"),
        Index("ix_form16_documents_uploaded_by", "uploaded_by"),
        Index("ix_form16_documents_published_by", "published_by"),
        Index("ix_form16_documents_withdrawn_by", "withdrawn_by"),
        Index("ix_form16_documents_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    fy: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    file_id: Mapped[uuid.UUID] = fk_uuid("files.id")
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    upload_batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'uploaded'"))
    uploaded_by: Mapped[uuid.UUID] = fk_uuid("users.id")
    second_person: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    published_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    withdrawn_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    withdraw_reason: Mapped[str | None] = mapped_column("withdraw_reason_enc", EncryptedString)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
