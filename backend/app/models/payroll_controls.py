"""Payroll controls (migration 0026, plan docs/plans/17-payroll-phase5.md work package 5a): publish requests (maker-checker for
publish), payslip delivery tracking, company debit accounts, payment batches and their lines.

The database repeats every maker-checker rule (CHECK constraints) and guards the lifecycles with triggers
(payroll_publish_requests_lock / _checker_guard / _tagged, payroll_payment_batches_lock / _consistency, payroll_payment_lines_lock,
payslip_deliveries_lock, company_bank_accounts_lock); the SQLite test harness knows none of them.
Every money / account / comment value is application-encrypted (`*_enc`); the python attribute drops the suffix.
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
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import EncryptedJSON, EncryptedString
from .base import Base, TimestampMixin, fk_uuid, uuid_pk


class PayrollPublishRequest(TimestampMixin, Base):
    """A maker asks a different person to publish a processed run (scope run) or one processed payslip of a published run (scope payslip).
    `snapshot` = encrypted JSON {payslips: [{payslip_id, version, result_mac}], counts, totals, processed_at}; with `snapshot_mac`
    it is write-once. `makers` is filled when the request is decided (approve / reject) and only grows."""

    __tablename__ = "payroll_publish_requests"
    __table_args__ = (
        CheckConstraint("scope IN ('run','payslip')", name="ck_payroll_publish_requests_scope"),
        CheckConstraint("(scope = 'payslip') = (payslip_id IS NOT NULL)", name="ck_payroll_publish_requests_scope_payslip"),
        CheckConstraint("status IN ('submitted','approved','rejected','withdrawn','stale')", name="ck_payroll_publish_requests_status"),
        CheckConstraint(
            "stale_reason IS NULL OR stale_reason IN ('RUN_REOPENED','RUN_REFRESHED','PAYSLIP_CHANGED','EXPIRED')",
            name="ck_payroll_publish_requests_stale_reason",
        ),
        CheckConstraint("(status = 'stale') = (stale_reason IS NOT NULL)", name="ck_payroll_publish_requests_stale_pair"),
        CheckConstraint("snapshot_mac ~ '^[0-9a-f]{64}$'", name="ck_payroll_publish_requests_mac"),
        CheckConstraint("expires_at > requested_at", name="ck_payroll_publish_requests_expiry"),
        CheckConstraint("(decided_by IS NULL) = (decided_at IS NULL)", name="ck_payroll_publish_requests_decided_pair"),
        CheckConstraint("(status IN ('approved','rejected')) = (decided_by IS NOT NULL)", name="ck_payroll_publish_requests_decided_status"),
        CheckConstraint(
            "(status = 'withdrawn') = (withdrawn_by IS NOT NULL) AND (withdrawn_by IS NULL) = (withdrawn_at IS NULL)",
            name="ck_payroll_publish_requests_withdrawn",
        ),
        CheckConstraint("status <> 'rejected' OR decision_comment_enc IS NOT NULL", name="ck_payroll_publish_requests_rejected_comment"),
        CheckConstraint("decided_by IS NULL OR decided_by <> requested_by", name="ck_payroll_publish_requests_checker"),
        CheckConstraint("decided_by IS NULL OR decided_by::text <> ALL (makers)", name="ck_payroll_publish_requests_checker_history"),
        CheckConstraint(
            "decided_by IS NULL OR requested_by::text = ANY (makers)", name="ck_payroll_publish_requests_requester_in_makers"
        ),
        CheckConstraint("version >= 1", name="ck_payroll_publish_requests_version"),
        Index(
            "uq_payroll_publish_requests_open_run", "run_id", unique=True,
            postgresql_where=text("scope = 'run' AND status = 'submitted'"),
        ),  # fmt: skip
        Index(
            "uq_payroll_publish_requests_open_payslip", "payslip_id", unique=True,
            postgresql_where=text("scope = 'payslip' AND status = 'submitted'"),
        ),  # fmt: skip
        Index("ix_payroll_publish_requests_run_status", "run_id", "status"),
        Index("ix_payroll_publish_requests_status_requested", "status", "requested_at"),
        Index("ix_payroll_publish_requests_payslip_id", "payslip_id"),
        Index("ix_payroll_publish_requests_requested_by", "requested_by"),
        Index("ix_payroll_publish_requests_decided_by", "decided_by"),
        Index("ix_payroll_publish_requests_withdrawn_by", "withdrawn_by"),
        Index("ix_payroll_publish_requests_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = fk_uuid("payroll_runs.id")
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    payslip_id: Mapped[uuid.UUID | None] = fk_uuid("payslips.id", nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'submitted'"))
    snapshot: Mapped[dict] = mapped_column("snapshot_enc", EncryptedJSON, nullable=False)
    snapshot_mac: Mapped[str] = mapped_column(Text, nullable=False)
    stale_reason: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column("comment_enc", EncryptedString)
    requested_by: Mapped[uuid.UUID] = fk_uuid("users.id")
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    makers: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    decided_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column("decision_comment_enc", EncryptedString)
    withdrawn_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    withdrawn_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class PayslipDelivery(TimestampMixin, Base):
    """Push-only delivery tracking, one row per published payslip. pushed_at / first_viewed_at / first_downloaded_at are write-once."""

    __tablename__ = "payslip_deliveries"
    __table_args__ = (
        CheckConstraint("push_count BETWEEN 0 AND 10", name="ck_payslip_deliveries_push_count"),
        CheckConstraint("reminders BETWEEN 0 AND 3", name="ck_payslip_deliveries_reminders"),
        CheckConstraint("(reminders = 0) = (last_reminded_at IS NULL)", name="ck_payslip_deliveries_reminded"),
        Index("ix_payslip_deliveries_user_id", "user_id"),
        Index("ix_payslip_deliveries_viewed", "first_viewed_at"),
    )

    payslip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payslips.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    pushed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    push_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    reminders: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    last_reminded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    first_viewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    first_downloaded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class CompanyBankAccount(TimestampMixin, Base):
    """Company debit account. Bank name, account number and IFSC are encrypted and immutable (a trigger refuses a change)."""

    __tablename__ = "company_bank_accounts"
    __table_args__ = (
        CheckConstraint("char_length(btrim(label)) BETWEEN 1 AND 40", name="ck_company_bank_accounts_label"),
        Index("uq_company_bank_accounts_default", "is_default", unique=True, postgresql_where=text("is_default AND active")),
        Index("ix_company_bank_accounts_created_by", "created_by"),
        Index("ix_company_bank_accounts_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    label: Mapped[str] = mapped_column(Text, nullable=False)
    bank_name: Mapped[str] = mapped_column("bank_name_enc", EncryptedString, nullable=False)
    account: Mapped[str] = mapped_column("account_enc", EncryptedString, nullable=False)
    ifsc: Mapped[str] = mapped_column("ifsc_enc", EncryptedString, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID] = fk_uuid("users.id")
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class PayrollPaymentBatch(TimestampMixin, Base):
    """One bank payment batch of a published run. draft -> submitted -> approved -> exported -> closed (+ cancelled).
    Maker-checker is repeated by ck_payroll_payment_batches_checker / _checker_history / _contributors and by payroll_payment_batches_lock.
    `format` and `payment_type` are copied from the settings at creation so a later download is byte-identical."""

    __tablename__ = "payroll_payment_batches"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_payroll_payment_batches_reference"),
        CheckConstraint("reference ~ '^PAY-[0-9]{4}-[0-9]{2}-[0-9]{2,4}$'", name="ck_payroll_payment_batches_reference"),
        CheckConstraint("format IN ('generic_neft_csv')", name="ck_payroll_payment_batches_format"),
        CheckConstraint("payment_type IN ('NEFT','RTGS','IMPS')", name="ck_payroll_payment_batches_payment_type"),
        CheckConstraint(
            "status IN ('draft','submitted','approved','exported','closed','cancelled')", name="ck_payroll_payment_batches_status"
        ),
        CheckConstraint("value_date BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'", name="ck_payroll_payment_batches_value_date"),
        CheckConstraint("file_sha256 IS NULL OR file_sha256 ~ '^[0-9a-f]{64}$'", name="ck_payroll_payment_batches_sha256"),
        CheckConstraint("lines_mac IS NULL OR lines_mac ~ '^[0-9a-f]{64}$'", name="ck_payroll_payment_batches_lines_mac"),
        CheckConstraint("status <> 'draft' OR lines_mac IS NULL", name="ck_payroll_payment_batches_draft_mac"),
        CheckConstraint(
            "status NOT IN ('submitted','approved','exported','closed') OR lines_mac IS NOT NULL", name="ck_payroll_payment_batches_live_mac"
        ),
        CheckConstraint("(submitted_by IS NULL) = (submitted_at IS NULL)", name="ck_payroll_payment_batches_submitted"),
        CheckConstraint(
            "status NOT IN ('submitted','approved','exported','closed') OR submitted_by IS NOT NULL",
            name="ck_payroll_payment_batches_live_submitted",
        ),
        CheckConstraint("(decided_by IS NULL) = (decided_at IS NULL)", name="ck_payroll_payment_batches_decided_pair"),
        CheckConstraint(
            "status NOT IN ('approved','exported','closed') OR decided_by IS NOT NULL", name="ck_payroll_payment_batches_decided_status"
        ),
        CheckConstraint("(exported_by IS NULL) = (exported_at IS NULL)", name="ck_payroll_payment_batches_exported_pair"),
        CheckConstraint("(status IN ('exported','closed')) = (exported_by IS NOT NULL)", name="ck_payroll_payment_batches_exported"),
        CheckConstraint("(status IN ('exported','closed')) = (file_sha256 IS NOT NULL)", name="ck_payroll_payment_batches_file"),
        CheckConstraint("status <> 'cancelled' OR cancel_reason_enc IS NOT NULL", name="ck_payroll_payment_batches_cancelled"),
        CheckConstraint(
            "created_by::text = ANY (contributors) AND last_edited_by::text = ANY (contributors) "
            "AND (submitted_by IS NULL OR submitted_by::text = ANY (contributors))",
            name="ck_payroll_payment_batches_contributors",
        ),
        CheckConstraint(
            "decided_by IS NULL OR (decided_by IS DISTINCT FROM created_by AND decided_by IS DISTINCT FROM last_edited_by "
            "AND decided_by IS DISTINCT FROM submitted_by)",
            name="ck_payroll_payment_batches_checker",
        ),
        CheckConstraint("decided_by IS NULL OR decided_by::text <> ALL (contributors)", name="ck_payroll_payment_batches_checker_history"),
        CheckConstraint("version >= 1", name="ck_payroll_payment_batches_version"),
        Index("ix_payroll_payment_batches_run_status", "run_id", "status"),
        Index("ix_payroll_payment_batches_status_created", "status", "created_at"),
        Index("ix_payroll_payment_batches_debit_account_id", "debit_account_id"),
        Index("ix_payroll_payment_batches_created_by", "created_by"),
        Index("ix_payroll_payment_batches_last_edited_by", "last_edited_by"),
        Index("ix_payroll_payment_batches_submitted_by", "submitted_by"),
        Index("ix_payroll_payment_batches_decided_by", "decided_by"),
        Index("ix_payroll_payment_batches_exported_by", "exported_by"),
        Index("ix_payroll_payment_batches_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = fk_uuid("payroll_runs.id")
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    debit_account_id: Mapped[uuid.UUID] = fk_uuid("company_bank_accounts.id")
    value_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'generic_neft_csv'"))
    payment_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'NEFT'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    lines_mac: Mapped[str | None] = mapped_column(Text)
    file_sha256: Mapped[str | None] = mapped_column(Text)
    exported_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    exported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = fk_uuid("users.id")
    last_edited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        default=lambda ctx: ctx.get_current_parameters()["created_by"],
    )  # fmt: skip
    # user ids (text) of everybody who created, edited, submitted or withdrew it; append-only. Must contain created_by,
    # last_edited_by and submitted_by (ck_payroll_payment_batches_contributors).
    contributors: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    submitted_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column("decision_comment_enc", EncryptedString)
    cancel_reason: Mapped[str | None] = mapped_column("cancel_reason_enc", EncryptedString)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class PayrollPaymentLine(TimestampMixin, Base):
    """One beneficiary of a batch. `amount` is a 2-decimal string; `beneficiary` = encrypted JSON holding the snapshot account fields
    (and, on a BANK_DETAILS_CHANGED line, the current ones too); `account_source` says which of them the file uses.
    A line is created only in a draft batch; to refresh a stale line exclude it and add a new one (the old slot is freed)."""

    __tablename__ = "payroll_payment_lines"
    __table_args__ = (
        UniqueConstraint("batch_id", "seq", name="uq_payroll_payment_lines_batch_seq"),
        CheckConstraint("seq BETWEEN 1 AND 9999", name="ck_payroll_payment_lines_seq"),
        CheckConstraint("account_source IN ('snapshot','current')", name="ck_payroll_payment_lines_account_source"),
        CheckConstraint(
            "flags <@ ARRAY['BANK_DETAILS_CHANGED','RECENT_BANK_CHANGE','REVISION_DELTA','NO_PAN']::text[]", name="ck_payroll_payment_lines_flags"
        ),
        CheckConstraint(
            "account_source = 'snapshot' OR 'BANK_DETAILS_CHANGED' = ANY (flags)", name="ck_payroll_payment_lines_source_flag"
        ),
        CheckConstraint("status IN ('pending','excluded','exported','paid','failed')", name="ck_payroll_payment_lines_status"),
        CheckConstraint(
            "(reference_enc IS NULL AND paid_on IS NULL) OR status IN ('paid','failed')", name="ck_payroll_payment_lines_confirmation"
        ),
        CheckConstraint("status <> 'paid' OR paid_on IS NOT NULL", name="ck_payroll_payment_lines_paid_on"),
        CheckConstraint("paid_on IS NULL OR paid_on BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'", name="ck_payroll_payment_lines_paid_year"),
        CheckConstraint("version >= 1", name="ck_payroll_payment_lines_version"),
        Index(
            "uq_payroll_payment_lines_live", "payslip_id", unique=True,
            postgresql_where=text("status IN ('pending','exported','paid')"),
        ),  # fmt: skip
        Index("ix_payroll_payment_lines_payslip_id", "payslip_id"),
        Index("ix_payroll_payment_lines_user_status", "user_id", "status"),
        Index("ix_payroll_payment_lines_batch_status", "batch_id", "status"),
        Index("ix_payroll_payment_lines_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    batch_id: Mapped[uuid.UUID] = fk_uuid("payroll_payment_batches.id")
    payslip_id: Mapped[uuid.UUID] = fk_uuid("payslips.id")
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[str] = mapped_column("amount_enc", EncryptedString, nullable=False)
    beneficiary: Mapped[dict] = mapped_column("beneficiary_enc", EncryptedJSON, nullable=False)
    account_source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'snapshot'"))
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    reference: Mapped[str | None] = mapped_column("reference_enc", EncryptedString)
    paid_on: Mapped[dt.date | None] = mapped_column(Date)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
