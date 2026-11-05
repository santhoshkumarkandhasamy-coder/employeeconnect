"""Payroll components (migration 0022, plan docs/plans/15-payroll-phase3.md section 2.1): one-time items and bulk import batches,
loans and instalments, auto arrears, leave encashments and the append-only money history.

Every money value, label, reason and breakdown is application-encrypted (`*_enc`; the python attribute drops the suffix).
Shared payable lifecycle (`status`): pending -> processing (inside a processed payslip, `payslip_id` set) -> paid (payslip
published). `paid` rows are immutable (DB trigger payroll_item_paid_lock); nothing is ever deleted (cancel is a status).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import EncryptedJSON, EncryptedString
from .base import Base, TimestampMixin, fk_uuid, uuid_pk

PERIOD_BOUNDS = "BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'"
PAYSLIP_RULE = "(status IN ('processing','paid')) = (payslip_id IS NOT NULL)"


class PayrollItemEvent(Base):
    """APPEND-ONLY history of every create / edit / state change of a phase 3 source row (never updated or deleted)."""

    __tablename__ = "payroll_item_events"
    __table_args__ = (
        CheckConstraint(
            "item_type IN ('one_time','loan','loan_installment','arrears','encashment','reimbursement','overtime',"
            "'increment','fnf','publish_request','payment_batch')",
            name="ck_payroll_item_events_item_type",
        ),
        CheckConstraint(
            "action IN ('create','update','cancel','skip','reschedule','foreclose','close','propose','accept','dismiss',"
            "'verify','approve','reject','process','reopen','pay','carry','submit','withdraw','settle','revoke','remove',"
            "'export','confirm','expire','stale')",
            name="ck_payroll_item_events_action",
        ),
        CheckConstraint("period IS NULL OR extract(day from period) = 1", name="ck_payroll_item_events_period_first"),
        CheckConstraint(
            "prev_period IS NULL OR extract(day from prev_period) = 1", name="ck_payroll_item_events_prev_period_first"
        ),
        Index("ix_payroll_item_events_item", "item_type", "item_id", "created_at"),
        Index("ix_payroll_item_events_user", "user_id", "created_at"),
        Index("ix_payroll_item_events_actor_id", "actor_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    item_type: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # polymorphic: no foreign key
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    action: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[dt.date | None] = mapped_column(Date)
    prev_period: Mapped[dt.date | None] = mapped_column(Date)
    amount: Mapped[str | None] = mapped_column("amount_enc", EncryptedString)
    prev_amount: Mapped[str | None] = mapped_column("prev_amount_enc", EncryptedString)
    detail: Mapped[dict | None] = mapped_column("detail_enc", EncryptedJSON)  # reason, reference, minutes
    payslip_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # no foreign key
    actor_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)  # NULL = system
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PayrollImportBatch(Base):
    """One committed bulk upload. A file (sha256) can be imported once per period."""

    __tablename__ = "payroll_import_batches"
    __table_args__ = (
        UniqueConstraint("file_sha256", "period", name="uq_payroll_import_batches_file_period"),
        CheckConstraint("extract(day from period) = 1", name="ck_payroll_import_batches_period_first"),
        CheckConstraint(f"period {PERIOD_BOUNDS}", name="ck_payroll_import_batches_period_year"),
        CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="ck_payroll_import_batches_sha256"),
        CheckConstraint("rows BETWEEN 1 AND 500", name="ck_payroll_import_batches_rows"),
        CheckConstraint("(cancelled_at IS NULL) = (cancelled_by IS NULL)", name="ck_payroll_import_batches_cancelled"),
        Index("ix_payroll_import_batches_period", "period"),
        Index("ix_payroll_import_batches_created_by", "created_by"),
        Index("ix_payroll_import_batches_cancelled_by", "cancelled_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[uuid.UUID] = fk_uuid("users.id")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class PayrollOneTimeItem(TimestampMixin, Base):
    """Bonus / incentive / other earning / other deduction for one employee-month (amount is always > 0; checked by the service)."""

    __tablename__ = "payroll_one_time_items"
    __table_args__ = (
        CheckConstraint("extract(day from period) = 1", name="ck_payroll_one_time_items_period_first"),
        CheckConstraint(f"period {PERIOD_BOUNDS}", name="ck_payroll_one_time_items_period_year"),
        CheckConstraint(
            "code IN ('BONUS','INCENTIVE','OTHER_EARNING','OTHER_DEDUCTION','NOTICE_RECOVERY')", name="ck_payroll_one_time_items_code"
        ),
        # migration 0025: NOTICE_RECOVERY is system-only (written by the F&F approve); an F&F row is never an imported row
        CheckConstraint("code <> 'NOTICE_RECOVERY' OR fnf_id IS NOT NULL", name="ck_payroll_one_time_items_notice_system"),
        CheckConstraint("fnf_id IS NULL OR batch_id IS NULL", name="ck_payroll_one_time_items_fnf_batch"),
        CheckConstraint("status IN ('pending','processing','paid','cancelled')", name="ck_payroll_one_time_items_status"),
        CheckConstraint(PAYSLIP_RULE, name="ck_payroll_one_time_items_payslip"),
        CheckConstraint("status <> 'cancelled' OR cancel_reason_enc IS NOT NULL", name="ck_payroll_one_time_items_cancelled"),
        CheckConstraint("version >= 1", name="ck_payroll_one_time_items_version"),
        Index("ix_payroll_one_time_items_user_period", "user_id", "period", "status"),
        Index("ix_payroll_one_time_items_period", "period", "status"),
        Index("ix_payroll_one_time_items_batch_id", "batch_id"),
        Index("ix_payroll_one_time_items_payslip_id", "payslip_id"),
        Index("ix_payroll_one_time_items_fnf_id", "fnf_id"),
        Index("ix_payroll_one_time_items_created_by", "created_by"),
        Index("ix_payroll_one_time_items_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column("label_enc", EncryptedString, nullable=False)
    amount: Mapped[str] = mapped_column("amount_enc", EncryptedString, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    batch_id: Mapped[uuid.UUID | None] = fk_uuid("payroll_import_batches.id", nullable=True)
    fnf_id: Mapped[uuid.UUID | None] = fk_uuid("fnf_settlements.id", nullable=True)  # migration 0025; write-once (trigger)
    payslip_id: Mapped[uuid.UUID | None] = fk_uuid("payslips.id", nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column("cancel_reason_enc", EncryptedString)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class PayrollLoan(TimestampMixin, Base):
    """Interest-free loan or salary advance. `reference` (L-0001) comes from a sequence in PostgreSQL (column default, step 2
    of work package 3a generates it in the service for portability)."""

    __tablename__ = "payroll_loans"
    __table_args__ = (
        UniqueConstraint("reference", name="uq_payroll_loans_reference"),
        CheckConstraint("reference ~ '^L-[0-9]{4,}$'", name="ck_payroll_loans_reference"),
        CheckConstraint("kind IN ('loan','advance')", name="ck_payroll_loans_kind"),
        CheckConstraint("extract(day from start_period) = 1", name="ck_payroll_loans_start_first"),
        CheckConstraint(f"start_period {PERIOD_BOUNDS}", name="ck_payroll_loans_start_year"),
        CheckConstraint("status IN ('active','closed','cancelled')", name="ck_payroll_loans_status"),
        CheckConstraint(
            "close_mode IS NULL OR close_mode IN ('recovered','foreclosed_payroll','foreclosed_outside','cancelled')",
            name="ck_payroll_loans_close_mode",
        ),
        CheckConstraint(
            "(status = 'active' AND closed_at IS NULL AND close_mode IS NULL) "
            "OR (status = 'closed' AND closed_at IS NOT NULL AND close_mode IN ('recovered','foreclosed_payroll','foreclosed_outside')) "
            "OR (status = 'cancelled' AND closed_at IS NOT NULL AND close_mode = 'cancelled')",
            name="ck_payroll_loans_closed",
        ),
        CheckConstraint("version >= 1", name="ck_payroll_loans_version"),
        Index("ix_payroll_loans_user_status", "user_id", "status"),
        Index("ix_payroll_loans_created_by", "created_by"),
        Index("ix_payroll_loans_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    principal: Mapped[str] = mapped_column("principal_enc", EncryptedString, nullable=False)
    emi: Mapped[str] = mapped_column("emi_enc", EncryptedString, nullable=False)
    start_period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column("reason_enc", EncryptedString, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    close_mode: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class PayrollLoanInstallment(TimestampMixin, Base):
    __tablename__ = "payroll_loan_installments"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="ck_payroll_loan_installments_seq"),
        CheckConstraint("extract(day from period) = 1", name="ck_payroll_loan_installments_period_first"),
        CheckConstraint(f"period {PERIOD_BOUNDS}", name="ck_payroll_loan_installments_period_year"),
        CheckConstraint("kind IN ('emi','foreclosure')", name="ck_payroll_loan_installments_kind"),
        CheckConstraint(
            "status IN ('pending','processing','paid','skipped','cancelled')", name="ck_payroll_loan_installments_status"
        ),
        CheckConstraint(PAYSLIP_RULE, name="ck_payroll_loan_installments_payslip"),
        CheckConstraint("fnf_id IS NULL OR kind = 'foreclosure'", name="ck_payroll_loan_installments_fnf_kind"),
        CheckConstraint("version >= 1", name="ck_payroll_loan_installments_version"),
        Index(
            "uq_payroll_loan_installments_live",
            "loan_id",
            "period",
            unique=True,
            postgresql_where=text("status IN ('pending','processing','paid')"),
        ),  # fmt: skip
        Index(
            "uq_payroll_loan_installments_live_seq",
            "loan_id",
            "seq",
            unique=True,
            postgresql_where=text("status NOT IN ('cancelled','skipped')"),
        ),  # fmt: skip  (migration 0023: numbers are unique among live installments only)
        Index("ix_payroll_loan_installments_user_period", "user_id", "period", "status"),
        Index("ix_payroll_loan_installments_payslip_id", "payslip_id"),
        Index("ix_payroll_loan_installments_fnf_id", "fnf_id"),
        Index("ix_payroll_loan_installments_created_by", "created_by"),
        Index("ix_payroll_loan_installments_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    loan_id: Mapped[uuid.UUID] = fk_uuid("payroll_loans.id")
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")  # denormalised for the pull query
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    amount: Mapped[str] = mapped_column("amount_enc", EncryptedString, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'emi'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    payslip_id: Mapped[uuid.UUID | None] = fk_uuid("payslips.id", nullable=True)
    fnf_id: Mapped[uuid.UUID | None] = fk_uuid("fnf_settlements.id", nullable=True)  # migration 0025; write-once (trigger)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class PayrollArrears(TimestampMixin, Base):
    """Arrears (code ARREARS) or salary recovery (OTHER_DEDUCTION) after a structure change. `proposed` rows are recomputed in place."""

    __tablename__ = "payroll_arrears"
    __table_args__ = (
        CheckConstraint("period IS NULL OR extract(day from period) = 1", name="ck_payroll_arrears_period_first"),
        CheckConstraint(f"period IS NULL OR period {PERIOD_BOUNDS}", name="ck_payroll_arrears_period_year"),
        CheckConstraint("extract(day from from_period) = 1", name="ck_payroll_arrears_from_first"),
        CheckConstraint("extract(day from to_period) = 1", name="ck_payroll_arrears_to_first"),
        CheckConstraint("from_period <= to_period", name="ck_payroll_arrears_range"),
        CheckConstraint("code IN ('ARREARS','OTHER_DEDUCTION')", name="ck_payroll_arrears_code"),
        CheckConstraint("status IN ('proposed','pending','processing','paid','dismissed')", name="ck_payroll_arrears_status"),
        CheckConstraint("status IN ('proposed','dismissed') OR period IS NOT NULL", name="ck_payroll_arrears_period_set"),
        CheckConstraint(PAYSLIP_RULE, name="ck_payroll_arrears_payslip"),
        CheckConstraint("version >= 1", name="ck_payroll_arrears_version"),
        Index("uq_payroll_arrears_open_proposal", "user_id", unique=True, postgresql_where=text("status = 'proposed'")),
        Index("ix_payroll_arrears_user_period", "user_id", "period", "status"),
        Index("ix_payroll_arrears_status", "status"),
        Index("ix_payroll_arrears_trigger_structure_id", "trigger_structure_id"),
        Index("ix_payroll_arrears_payslip_id", "payslip_id"),
        Index("ix_payroll_arrears_created_by", "created_by"),
        Index("ix_payroll_arrears_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    period: Mapped[dt.date | None] = mapped_column(Date)
    from_period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    to_period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    trigger_structure_id: Mapped[uuid.UUID | None] = fk_uuid("salary_structures.id", ondelete="SET NULL", nullable=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[str] = mapped_column("amount_enc", EncryptedString, nullable=False)
    breakdown: Mapped[list] = mapped_column("breakdown_enc", EncryptedJSON, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'proposed'"))
    dismiss_reason: Mapped[str | None] = mapped_column("dismiss_reason_enc", EncryptedString)
    payslip_id: Mapped[uuid.UUID | None] = fk_uuid("payslips.id", nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class LeaveEncashment(TimestampMixin, Base):
    """Encashed leave days (reserved in leave_balances.encashed at creation); rate and amount are fixed at creation."""

    __tablename__ = "leave_encashments"
    __table_args__ = (
        CheckConstraint("year BETWEEN 2000 AND 2200", name="ck_leave_encashments_year"),
        CheckConstraint("days > 0 AND days * 2 = trunc(days * 2)", name="ck_leave_encashments_days"),
        CheckConstraint("extract(day from period) = 1", name="ck_leave_encashments_period_first"),
        CheckConstraint(f"period {PERIOD_BOUNDS}", name="ck_leave_encashments_period_year"),
        CheckConstraint("source IN ('request','fnf')", name="ck_leave_encashments_source"),
        CheckConstraint("status IN ('pending','processing','paid','cancelled')", name="ck_leave_encashments_status"),
        CheckConstraint(PAYSLIP_RULE, name="ck_leave_encashments_payslip"),
        CheckConstraint("fnf_id IS NULL OR source = 'fnf'", name="ck_leave_encashments_fnf_source"),
        CheckConstraint("version >= 1", name="ck_leave_encashments_version"),
        Index("ix_leave_encashments_user_year", "user_id", "year"),
        Index("ix_leave_encashments_user_period", "user_id", "period", "status"),
        Index("ix_leave_encashments_leave_type_id", "leave_type_id"),
        Index("ix_leave_encashments_payslip_id", "payslip_id"),
        Index("ix_leave_encashments_fnf_id", "fnf_id"),
        Index("ix_leave_encashments_created_by", "created_by"),
        Index("ix_leave_encashments_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    leave_type_id: Mapped[uuid.UUID] = fk_uuid("leave_types.id")
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    days: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    daily_rate: Mapped[str] = mapped_column("daily_rate_enc", EncryptedString, nullable=False)
    amount: Mapped[str] = mapped_column("amount_enc", EncryptedString, nullable=False)
    period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'request'"))
    reason: Mapped[str | None] = mapped_column("reason_enc", EncryptedString)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    payslip_id: Mapped[uuid.UUID | None] = fk_uuid("payslips.id", nullable=True)
    fnf_id: Mapped[uuid.UUID | None] = fk_uuid("fnf_settlements.id", nullable=True)  # migration 0025; write-once (trigger)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class ReimbursementClaim(TimestampMixin, Base):
    """Expense claim (migration 0023). Two-step approval: the reporting manager verifies (`manager_step`), HR approves the final
    amount and the payout month. `approved` is the payable state of the component engine; `period` is the payout month."""

    __tablename__ = "reimbursement_claims"
    __table_args__ = (
        CheckConstraint("category ~ '^[a-z][a-z0-9_]{1,19}$'", name="ck_reimbursement_claims_category"),
        CheckConstraint(
            "status IN ('submitted','verified','approved','processing','paid','rejected','cancelled')",
            name="ck_reimbursement_claims_status",
        ),
        CheckConstraint("manager_step IN ('pending','done','skipped')", name="ck_reimbursement_claims_manager_step"),
        CheckConstraint("period IS NULL OR extract(day from period) = 1", name="ck_reimbursement_claims_period_first"),
        CheckConstraint(f"period IS NULL OR period {PERIOD_BOUNDS}", name="ck_reimbursement_claims_period_year"),
        CheckConstraint(
            "status NOT IN ('approved','processing','paid') OR (period IS NOT NULL AND approved_amount_enc IS NOT NULL)",
            name="ck_reimbursement_claims_payable",
        ),
        CheckConstraint(PAYSLIP_RULE, name="ck_reimbursement_claims_payslip"),
        CheckConstraint("(verifier_id IS NULL) = (verified_at IS NULL)", name="ck_reimbursement_claims_verified"),
        CheckConstraint("(approver_id IS NULL) = (decided_at IS NULL)", name="ck_reimbursement_claims_decided"),
        CheckConstraint("status <> 'rejected' OR decision_comment_enc IS NOT NULL", name="ck_reimbursement_claims_rejected"),
        CheckConstraint("version >= 1", name="ck_reimbursement_claims_version"),
        Index(
            "uq_reimbursement_claims_live_bill",
            "bill_file_id",
            unique=True,
            postgresql_where=text("bill_file_id IS NOT NULL AND status NOT IN ('rejected','cancelled')"),
        ),  # fmt: skip
        Index("ix_reimbursement_claims_user_status", "user_id", "status"),
        Index("ix_reimbursement_claims_status_created", "status", "created_at"),
        Index("ix_reimbursement_claims_user_period", "user_id", "period", "status"),
        Index("ix_reimbursement_claims_verifier_id", "verifier_id"),
        Index("ix_reimbursement_claims_approver_id", "approver_id"),
        Index("ix_reimbursement_claims_payslip_id", "payslip_id"),
        Index("ix_reimbursement_claims_created_by", "created_by"),
        Index("ix_reimbursement_claims_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    category: Mapped[str] = mapped_column(Text, nullable=False)
    expense_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    amount: Mapped[str] = mapped_column("amount_enc", EncryptedString, nullable=False)  # claimed
    approved_amount: Mapped[str | None] = mapped_column("approved_amount_enc", EncryptedString)
    description: Mapped[str] = mapped_column("description_enc", EncryptedString, nullable=False)
    bill_file_id: Mapped[uuid.UUID | None] = fk_uuid("files.id", nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'submitted'"))
    manager_step: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    verifier_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approver_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column("decision_comment_enc", EncryptedString)
    period: Mapped[dt.date | None] = mapped_column(Date)  # payout month, set on approve
    payslip_id: Mapped[uuid.UUID | None] = fk_uuid("payslips.id", nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)

    @property
    def event_amount(self) -> str:
        """The amount the money history records: what is payable once approved, else what was claimed."""
        return self.approved_amount if self.approved_amount is not None else self.amount


class OvertimeDay(TimestampMixin, Base):
    """Overtime minutes of one employee-day derived from attendance and reviewed by HR (migration 0023). Minutes are not money
    (plaintext); `approved` is the payable state of the component engine."""

    __tablename__ = "overtime_days"
    __table_args__ = (
        UniqueConstraint("user_id", "work_date", name="uq_overtime_days_user_date"),
        CheckConstraint("period = work_date - (extract(day from work_date)::integer - 1)", name="ck_overtime_days_period"),
        CheckConstraint(f"period {PERIOD_BOUNDS}", name="ck_overtime_days_period_year"),
        CheckConstraint("computed_minutes BETWEEN 0 AND 1440", name="ck_overtime_days_computed"),
        CheckConstraint(
            "approved_minutes IS NULL OR (approved_minutes BETWEEN 0 AND 1440 AND approved_minutes <= computed_minutes)",
            name="ck_overtime_days_approved",
        ),
        CheckConstraint(
            "flags <@ ARRAY['AUTO_CLOSED','SUSPICIOUS','OFF_DAY','ATTENDANCE_CHANGED']::text[]", name="ck_overtime_days_flags"
        ),
        CheckConstraint("status IN ('computed','approved','rejected','processing','paid')", name="ck_overtime_days_status"),
        CheckConstraint("status IN ('computed','rejected') OR approved_minutes IS NOT NULL", name="ck_overtime_days_minutes_set"),
        CheckConstraint(PAYSLIP_RULE, name="ck_overtime_days_payslip"),
        CheckConstraint("(reviewer_id IS NULL) OR (reviewed_at IS NOT NULL)", name="ck_overtime_days_reviewed"),
        CheckConstraint("version >= 1", name="ck_overtime_days_version"),
        Index("ix_overtime_days_period_status", "period", "status"),
        Index("ix_overtime_days_user_period", "user_id", "period", "status"),
        Index("ix_overtime_days_attendance_id", "attendance_id"),
        Index("ix_overtime_days_reviewer_id", "reviewer_id"),
        Index("ix_overtime_days_payslip_id", "payslip_id"),
        Index("ix_overtime_days_created_by", "created_by"),
        Index("ix_overtime_days_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    work_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    period: Mapped[dt.date] = mapped_column(Date, nullable=False)  # first of the month of work_date
    attendance_id: Mapped[uuid.UUID | None] = fk_uuid("attendance.id", ondelete="SET NULL", nullable=True)
    computed_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_minutes: Mapped[int | None] = mapped_column(Integer)
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'computed'"))
    reviewer_id: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column("comment_enc", EncryptedString)
    payslip_id: Mapped[uuid.UUID | None] = fk_uuid("payslips.id", nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
