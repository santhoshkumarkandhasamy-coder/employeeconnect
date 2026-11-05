"""Payroll tables (migration 0018, plan docs/plans/12-payroll.md section 2).

Every money / bank / identity value is application-encrypted (`*_enc` columns). The python attribute drops the suffix.
Published data is locked by DB triggers (payroll_runs_lock / payslips_lock): the ORM never needs to guard it, but a
write against a published row raises an IntegrityError-class error from PostgreSQL.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import EncryptedJSON, EncryptedString
from .base import Base, TimestampMixin, fk_uuid, uuid_pk


class SalaryStructure(TimestampMixin, Base):
    """Effective-dated (always the 1st of a month). components: {basic, hra, allowances[], fixed_deductions[], tds_monthly}."""

    __tablename__ = "salary_structures"
    __table_args__ = (
        UniqueConstraint("user_id", "effective_from", name="uq_salary_structures_user_effective"),
        CheckConstraint("extract(day from effective_from) = 1", name="ck_salary_structures_first_of_month"),
        CheckConstraint("effective_from BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'", name="ck_salary_structures_year"),
        CheckConstraint("source IN ('manual','increment','import')", name="ck_salary_structures_source"),
        CheckConstraint("(source = 'increment') = (increment_id IS NOT NULL)", name="ck_salary_structures_increment"),
        Index("ix_salary_structures_user_effective", "user_id", text("effective_from DESC")),
        Index("ix_salary_structures_created_by", "created_by"),
        Index("ix_salary_structures_updated_by", "updated_by"),
        Index("ix_salary_structures_increment_id", "increment_id"),
        Index("ix_salary_structures_import_batch_id", "import_batch_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    components: Mapped[dict] = mapped_column("components_enc", EncryptedJSON, nullable=False)
    pf_applicable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    pf_on_full_basic: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    esi_applicable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    pt_applicable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'manual'"))  # migration 0024
    # circular with salary_increments.structure_id: use_alter keeps create_all (test harness) able to order the two tables
    increment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("salary_increments.id", ondelete="RESTRICT", use_alter=True, name="fk_salary_structures_increment_id"),
    )
    import_batch_id: Mapped[uuid.UUID | None] = fk_uuid("salary_increment_batches.id", nullable=True)
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class SalaryIncrementBatch(Base):
    """One bulk hike (`percent`) or one committed structure import (`import`). Migration 0024."""

    __tablename__ = "salary_increment_batches"
    __table_args__ = (
        CheckConstraint("kind IN ('percent','import')", name="ck_salary_increment_batches_kind"),
        CheckConstraint("method IS NULL OR method IN ('proportional','split_from_gross')", name="ck_salary_increment_batches_method"),
        CheckConstraint("effective_from IS NULL OR extract(day from effective_from) = 1", name="ck_salary_increment_batches_effective_first"),
        CheckConstraint(
            "effective_from IS NULL OR effective_from BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'",
            name="ck_salary_increment_batches_effective_year",
        ),
        CheckConstraint("file_sha256 IS NULL OR file_sha256 ~ '^[0-9a-f]{64}$'", name="ck_salary_increment_batches_sha256"),
        CheckConstraint("(kind = 'import') = (file_sha256 IS NOT NULL)", name="ck_salary_increment_batches_import_sha"),
        CheckConstraint(
            "kind <> 'percent' OR (effective_from IS NOT NULL AND percent_enc IS NOT NULL AND method IS NOT NULL)",
            name="ck_salary_increment_batches_percent_fields",
        ),
        CheckConstraint("rows BETWEEN 1 AND 500", name="ck_salary_increment_batches_rows"),
        CheckConstraint("(cancelled_at IS NULL) = (cancelled_by IS NULL)", name="ck_salary_increment_batches_cancelled"),
        Index("uq_salary_increment_batches_import_sha", "file_sha256", unique=True, postgresql_where=text("kind = 'import'")),
        Index("ix_salary_increment_batches_created", "kind", "created_at"),
        Index("ix_salary_increment_batches_created_by", "created_by"),
        Index("ix_salary_increment_batches_cancelled_by", "cancelled_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[dt.date | None] = mapped_column(Date)
    percent: Mapped[str | None] = mapped_column("percent_enc", EncryptedString)
    method: Mapped[str | None] = mapped_column(Text)
    file_sha256: Mapped[str | None] = mapped_column(Text)
    rows: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column("reason_enc", EncryptedString, nullable=False)
    created_by: Mapped[uuid.UUID] = fk_uuid("users.id")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class SalaryIncrement(TimestampMixin, Base):
    """A proposed salary change (maker-checker). `proposed` = StructureIn without effective_from (NULL for `remove`).
    Migration 0024; the DB repeats the maker-checker rule in ck_salary_increments_checker."""

    __tablename__ = "salary_increments"
    __table_args__ = (
        CheckConstraint("kind IN ('percent','amount','structure','remove','import')", name="ck_salary_increments_kind"),
        CheckConstraint("method IS NULL OR method IN ('proportional','split_from_gross')", name="ck_salary_increments_method"),
        CheckConstraint("extract(day from effective_from) = 1", name="ck_salary_increments_effective_first"),
        CheckConstraint("effective_from BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'", name="ck_salary_increments_effective_year"),
        CheckConstraint("status IN ('draft','submitted','approved','rejected','cancelled')", name="ck_salary_increments_status"),
        CheckConstraint(
            "warnings <@ ARRAY['BACKDATED','TDS_REVIEW','DECREASE','LARGE_CHANGE','ESI_THRESHOLD_CROSSED','PF_CEILING_CROSSED',"
            "'NO_CHANGE']::text[]",
            name="ck_salary_increments_warnings",
        ),
        CheckConstraint("(kind = 'remove') = (proposed_enc IS NULL)", name="ck_salary_increments_remove"),
        CheckConstraint("kind <> 'percent' OR (percent_enc IS NOT NULL AND method IS NOT NULL)", name="ck_salary_increments_percent_fields"),
        CheckConstraint("created_by <> user_id", name="ck_salary_increments_maker_not_employee"),
        CheckConstraint("(submitted_by IS NULL) = (submitted_at IS NULL)", name="ck_salary_increments_submitted"),
        CheckConstraint("(decided_by IS NULL) = (decided_at IS NULL)", name="ck_salary_increments_decided_pair"),
        CheckConstraint("status NOT IN ('approved','rejected') OR decided_by IS NOT NULL", name="ck_salary_increments_decided_status"),
        CheckConstraint("status NOT IN ('submitted','approved') OR submitted_by IS NOT NULL", name="ck_salary_increments_live_submitted"),
        CheckConstraint("status <> 'rejected' OR decision_comment_enc IS NOT NULL", name="ck_salary_increments_rejected_comment"),
        CheckConstraint(
            "status NOT IN ('approved','rejected') OR (decided_by IS NOT NULL AND decided_by <> user_id "
            "AND decided_by IS DISTINCT FROM created_by AND decided_by IS DISTINCT FROM last_edited_by "
            "AND decided_by IS DISTINCT FROM submitted_by)",
            name="ck_salary_increments_checker",
        ),
        CheckConstraint(
            "status NOT IN ('approved','rejected') OR decided_by::text <> ALL (contributors)",
            name="ck_salary_increments_checker_history",
        ),
        CheckConstraint("version >= 1", name="ck_salary_increments_version"),
        Index(
            "uq_salary_increments_live", "user_id", "effective_from", unique=True,
            postgresql_where=text("status IN ('draft','submitted')"),
        ),  # fmt: skip
        Index("ix_salary_increments_status_created", "status", "created_at"),
        Index("ix_salary_increments_batch_id", "batch_id"),
        Index("ix_salary_increments_user_created", "user_id", "created_at"),
        Index("ix_salary_increments_base_structure_id", "base_structure_id"),
        Index("ix_salary_increments_replaces_structure_id", "replaces_structure_id"),
        Index("ix_salary_increments_structure_id", "structure_id"),
        Index("ix_salary_increments_submitted_by", "submitted_by"),
        Index("ix_salary_increments_decided_by", "decided_by"),
        Index("ix_salary_increments_created_by", "created_by"),
        Index("ix_salary_increments_last_edited_by", "last_edited_by"),
        Index("ix_salary_increments_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    batch_id: Mapped[uuid.UUID | None] = fk_uuid("salary_increment_batches.id", nullable=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    method: Mapped[str | None] = mapped_column(Text)
    percent: Mapped[str | None] = mapped_column("percent_enc", EncryptedString)
    base_structure_id: Mapped[uuid.UUID | None] = fk_uuid("salary_structures.id", ondelete="SET NULL", nullable=True)
    base_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    replaces_structure_id: Mapped[uuid.UUID | None] = fk_uuid("salary_structures.id", ondelete="SET NULL", nullable=True)
    proposed: Mapped[dict | None] = mapped_column("proposed_enc", EncryptedJSON)
    old_gross: Mapped[str | None] = mapped_column("old_gross_enc", EncryptedString)
    new_gross: Mapped[str | None] = mapped_column("new_gross_enc", EncryptedString)
    warnings: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    reason: Mapped[str] = mapped_column("reason_enc", EncryptedString, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    submitted_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column("decision_comment_enc", EncryptedString)
    structure_id: Mapped[uuid.UUID | None] = fk_uuid("salary_structures.id", ondelete="SET NULL", nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID] = fk_uuid("users.id")
    # last actor who changed the proposal (create / edit); never touched by submit, approve or reject
    last_edited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        default=lambda ctx: ctx.get_current_parameters()["created_by"],
    )  # fmt: skip
    # user ids (text) of everybody who created, edited, submitted or withdrew it; append-only, survives a withdraw
    contributors: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class EmployeePayrollDetails(TimestampMixin, Base):
    """1:1 with users (optional). Formats (PAN/IFSC/account/UAN/ESI IP) are validated by the service, not the DB."""

    __tablename__ = "employee_payroll_details"
    __table_args__ = (
        CheckConstraint("designation IS NULL OR char_length(designation) <= 60", name="ck_employee_payroll_details_designation"),
        CheckConstraint("payment_mode IN ('bank_transfer','cheque','cash')", name="ck_employee_payroll_details_payment_mode"),
        CheckConstraint(
            "last_working_day IS NULL OR last_working_day BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'",
            name="ck_employee_payroll_details_lwd_year",
        ),
        CheckConstraint(
            "notice_period_days IS NULL OR notice_period_days BETWEEN 0 AND 365", name="ck_employee_payroll_details_notice_days"
        ),
        Index("ix_employee_payroll_details_updated_by", "updated_by"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    designation: Mapped[str | None] = mapped_column(Text)
    payment_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'bank_transfer'"))
    include_in_payroll: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    overtime_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))  # migration 0023
    last_working_day: Mapped[dt.date | None] = mapped_column(Date)  # migration 0025; written only by the F&F service
    notice_period_days: Mapped[int | None] = mapped_column(SmallInteger)  # migration 0025
    bank_name: Mapped[str | None] = mapped_column("bank_name_enc", EncryptedString)
    bank_account: Mapped[str | None] = mapped_column("bank_account_enc", EncryptedString)
    bank_ifsc: Mapped[str | None] = mapped_column("bank_ifsc_enc", EncryptedString)
    pan: Mapped[str | None] = mapped_column("pan_enc", EncryptedString)
    uan: Mapped[str | None] = mapped_column("uan_enc", EncryptedString)
    esi_ip_number: Mapped[str | None] = mapped_column("esi_ip_number_enc", EncryptedString)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class PayrollRun(TimestampMixin, Base):
    """One run per month. status draft -> processed -> published (published = locked by trigger).
    config = snapshot of the payroll.* settings (rates only, non-personal)."""

    __tablename__ = "payroll_runs"
    __table_args__ = (
        UniqueConstraint("period", name="uq_payroll_runs_period"),
        CheckConstraint("extract(day from period) = 1", name="ck_payroll_runs_period_first"),
        CheckConstraint("status IN ('draft','processed','published')", name="ck_payroll_runs_status"),
        CheckConstraint(
            "status <> 'published' OR (published_at IS NOT NULL AND published_by IS NOT NULL)", name="ck_payroll_runs_published"
        ),
        CheckConstraint("company_name IS NULL OR char_length(company_name) <= 100", name="ck_payroll_runs_company_name"),
        Index("ix_payroll_runs_status", "status"),
        Index("ix_payroll_runs_created_by", "created_by"),
        Index("ix_payroll_runs_processed_by", "processed_by"),
        Index("ix_payroll_runs_published_by", "published_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    pay_date: Mapped[dt.date | None] = mapped_column(Date)
    attendance_cutoff: Mapped[dt.date] = mapped_column(Date, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    company_name: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    processed_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    published_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Payslip(TimestampMixin, Base):
    """One row per (run, employee, revision). Published rows only ever change to 'superseded' (trigger).
    Encrypted: identity (pan/uan/esi_ip_number/bank_*), inputs (tds, pt_override, adjustments, note), result
    (earnings/deductions/employer/gross/total_deductions/net), lop_override_reason, revision_reason."""

    __tablename__ = "payslips"
    __table_args__ = (
        UniqueConstraint("run_id", "user_id", "revision", name="uq_payslips_run_user_revision"),
        CheckConstraint("revision BETWEEN 1 AND 99", name="ck_payslips_revision"),
        CheckConstraint("status IN ('draft','processed','published','superseded')", name="ck_payslips_status"),
        CheckConstraint(
            "basis_days >= 0 AND employed_days >= 0 AND lop_auto >= 0 AND paid_days >= 0 AND paid_days <= basis_days",
            name="ck_payslips_days",
        ),
        CheckConstraint(
            "lop_override IS NULL OR (lop_override >= 0 AND lop_override * 2 = trunc(lop_override * 2))",
            name="ck_payslips_lop_override",
        ),
        CheckConstraint("lop_override IS NULL OR lop_override_reason_enc IS NOT NULL", name="ck_payslips_lop_override_reason"),
        CheckConstraint("status = 'draft' OR result_enc IS NOT NULL", name="ck_payslips_result_when_processed"),
        CheckConstraint("revision = 1 OR revision_reason_enc IS NOT NULL", name="ck_payslips_revision_reason"),
        CheckConstraint("version >= 1", name="ck_payslips_version"),
        CheckConstraint(
            "status NOT IN ('published','superseded') OR (published_at IS NOT NULL AND published_by IS NOT NULL)",
            name="ck_payslips_published",
        ),
        CheckConstraint("status <> 'superseded' OR superseded_at IS NOT NULL", name="ck_payslips_superseded"),
        Index("uq_payslips_open", "run_id", "user_id", unique=True, postgresql_where=text("status IN ('draft','processed')")),
        Index("uq_payslips_live", "run_id", "user_id", unique=True, postgresql_where=text("status = 'published'")),
        Index("ix_payslips_user_status", "user_id", "status"),
        Index("ix_payslips_run_status", "run_id", "status"),
        Index("ix_payslips_structure_id", "structure_id"),
        Index("ix_payslips_supersedes_id", "supersedes_id"),
        Index("ix_payslips_processed_by", "processed_by"),
        Index("ix_payslips_published_by", "published_by"),
        Index("ix_payslips_created_by", "created_by"),
        Index("ix_payslips_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = fk_uuid("payroll_runs.id")
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    revision: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    hold: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    structure_id: Mapped[uuid.UUID | None] = fk_uuid("salary_structures.id", nullable=True)
    employee_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    identity: Mapped[dict | None] = mapped_column("identity_enc", EncryptedJSON)
    basis_days: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    employed_days: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    lop_auto: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False, server_default=text("0"))
    lop_override: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    lop_override_reason: Mapped[str | None] = mapped_column("lop_override_reason_enc", EncryptedString)
    paid_days: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    lop_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False)
    inputs: Mapped[dict] = mapped_column("inputs_enc", EncryptedJSON, nullable=False)
    result: Mapped[dict | None] = mapped_column("result_enc", EncryptedJSON)
    warnings: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    revision_reason: Mapped[str | None] = mapped_column("revision_reason_enc", EncryptedString)
    supersedes_id: Mapped[uuid.UUID | None] = fk_uuid("payslips.id", nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    processed_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    superseded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


_LWD_MONTH = "make_date(extract(year from last_working_day)::int, extract(month from last_working_day)::int, 1)"


class FnfSettlement(TimestampMixin, Base):
    """Full and final settlement of one employee (maker-checker). Migration 0025; the DB repeats the maker-checker rule in
    ck_fnf_settlements_checker / _checker_history and guards the lifecycle in fnf_settlements_lock.
    `plan` = encrypted JSON (override reason, encashment lines, loan modes, other items, move_open_items); `summary` = approval snapshot."""

    __tablename__ = "fnf_settlements"
    __table_args__ = (
        CheckConstraint(
            "exit_reason IN ('resignation','termination','retirement','death','contract_end','absconding','other')",
            name="ck_fnf_settlements_exit_reason",
        ),
        CheckConstraint("status IN ('draft','submitted','approved','settled','cancelled')", name="ck_fnf_settlements_status"),
        CheckConstraint("last_working_day BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'", name="ck_fnf_settlements_lwd_year"),
        CheckConstraint("resignation_date IS NULL OR resignation_date <= last_working_day", name="ck_fnf_settlements_resignation"),
        CheckConstraint("extract(day from settlement_period) = 1", name="ck_fnf_settlements_period_first"),
        CheckConstraint(
            f"settlement_period >= {_LWD_MONTH} AND settlement_period <= ({_LWD_MONTH} + INTERVAL '3 months')::date",
            name="ck_fnf_settlements_period_window",
        ),
        CheckConstraint("notice_required_days BETWEEN 0 AND 365", name="ck_fnf_settlements_notice_required"),
        CheckConstraint("shortfall_days BETWEEN 0 AND 365 AND shortfall_days <= notice_required_days", name="ck_fnf_settlements_shortfall"),
        CheckConstraint("NOT notice_waived OR shortfall_days = 0", name="ck_fnf_settlements_shortfall_waived"),
        CheckConstraint("exit_reason = 'resignation' OR shortfall_days = 0", name="ck_fnf_settlements_shortfall_reason"),
        CheckConstraint("created_by <> user_id", name="ck_fnf_settlements_maker_not_employee"),
        CheckConstraint("(submitted_by IS NULL) = (submitted_at IS NULL)", name="ck_fnf_settlements_submitted"),
        CheckConstraint("(decided_by IS NULL) = (decided_at IS NULL)", name="ck_fnf_settlements_decided_pair"),
        CheckConstraint("status NOT IN ('submitted','approved','settled') OR submitted_by IS NOT NULL", name="ck_fnf_settlements_live_submitted"),
        CheckConstraint("status NOT IN ('approved','settled') OR decided_by IS NOT NULL", name="ck_fnf_settlements_decided_status"),
        CheckConstraint("status NOT IN ('approved','settled') OR summary_enc IS NOT NULL", name="ck_fnf_settlements_summary"),
        CheckConstraint("status <> 'cancelled' OR cancel_reason_enc IS NOT NULL", name="ck_fnf_settlements_cancelled"),
        CheckConstraint("(status = 'settled') = (final_payslip_id IS NOT NULL)", name="ck_fnf_settlements_settled"),
        CheckConstraint(
            "decided_by IS NULL OR (decided_by <> user_id AND decided_by IS DISTINCT FROM created_by "
            "AND decided_by IS DISTINCT FROM last_edited_by AND decided_by IS DISTINCT FROM submitted_by)",
            name="ck_fnf_settlements_checker",
        ),
        CheckConstraint("decided_by IS NULL OR decided_by::text <> ALL (contributors)", name="ck_fnf_settlements_checker_history"),
        CheckConstraint("version >= 1", name="ck_fnf_settlements_version"),
        Index(
            "uq_fnf_settlements_live", "user_id", unique=True,
            postgresql_where=text("status IN ('draft','submitted','approved')"),
        ),  # fmt: skip
        Index("ix_fnf_settlements_status_created", "status", "created_at"),
        Index("ix_fnf_settlements_user_created", "user_id", "created_at"),
        Index("ix_fnf_settlements_period_status", "settlement_period", "status"),
        Index("ix_fnf_settlements_final_payslip_id", "final_payslip_id"),
        Index("ix_fnf_settlements_submitted_by", "submitted_by"),
        Index("ix_fnf_settlements_decided_by", "decided_by"),
        Index("ix_fnf_settlements_created_by", "created_by"),
        Index("ix_fnf_settlements_last_edited_by", "last_edited_by"),
        Index("ix_fnf_settlements_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    resignation_date: Mapped[dt.date | None] = mapped_column(Date)
    last_working_day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    exit_reason: Mapped[str] = mapped_column(Text, nullable=False)
    settlement_period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    notice_required_days: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    notice_waived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    shortfall_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    plan: Mapped[dict] = mapped_column("plan_enc", EncryptedJSON, nullable=False)
    summary: Mapped[dict | None] = mapped_column("summary_enc", EncryptedJSON)
    remarks: Mapped[str | None] = mapped_column("remarks_enc", EncryptedString)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    submitted_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_comment: Mapped[str | None] = mapped_column("decision_comment_enc", EncryptedString)
    cancel_reason: Mapped[str | None] = mapped_column("cancel_reason_enc", EncryptedString)
    final_payslip_id: Mapped[uuid.UUID | None] = fk_uuid("payslips.id", nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_by: Mapped[uuid.UUID] = fk_uuid("users.id")
    # last actor who changed the plan (create / edit); never touched by submit, approve, reject or revoke
    last_edited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        default=lambda ctx: ctx.get_current_parameters()["created_by"],
    )  # fmt: skip
    # user ids (text) of everybody who created, edited, submitted or withdrew it; append-only (the trigger refuses a shrink)
    contributors: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class StatutoryChallan(TimestampMixin, Base):
    """Paid / unpaid tracking of one statutory due (migration 0020). Amount and bank reference are encrypted."""

    __tablename__ = "statutory_challans"
    __table_args__ = (
        UniqueConstraint("type", "period", name="uq_statutory_challans_type_period"),
        CheckConstraint("type IN ('pf','esi','pt','tds')", name="ck_statutory_challans_type"),
        CheckConstraint("status IN ('unpaid','paid')", name="ck_statutory_challans_status"),
        CheckConstraint("extract(day from period) = 1", name="ck_statutory_challans_period_first"),
        CheckConstraint("period BETWEEN DATE '2000-01-01' AND DATE '2200-12-31'", name="ck_statutory_challans_period_year"),
        CheckConstraint(
            "(status = 'paid' AND paid_on IS NOT NULL) OR (status = 'unpaid' AND paid_on IS NULL AND reference_enc IS NULL)",
            name="ck_statutory_challans_paid",
        ),
        Index("ix_statutory_challans_period", "period"),
        Index("ix_statutory_challans_created_by", "created_by"),
        Index("ix_statutory_challans_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    type: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    amount: Mapped[str] = mapped_column("amount_enc", EncryptedString, nullable=False)
    paid_on: Mapped[dt.date | None] = mapped_column(Date)
    reference: Mapped[str | None] = mapped_column("reference_enc", EncryptedString)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'unpaid'"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class StatutoryChallanEvent(Base):
    """APPEND-ONLY history of every paid / unpaid change of a challan (migration 0021). `*` = state after the change,
    `prev_*` = the state that was overwritten or cleared. Never updated or deleted by the application (privileges revoked)."""

    __tablename__ = "statutory_challan_events"
    __table_args__ = (
        CheckConstraint("type IN ('pf','esi','pt','tds')", name="ck_statutory_challan_events_type"),
        CheckConstraint("action IN ('paid','unpaid')", name="ck_statutory_challan_events_action"),
        CheckConstraint("status IN ('unpaid','paid')", name="ck_statutory_challan_events_status"),
        CheckConstraint(
            "prev_status IS NULL OR prev_status IN ('unpaid','paid')", name="ck_statutory_challan_events_prev_status"
        ),
        Index("ix_statutory_challan_events_challan", "challan_id", "created_at"),
        Index("ix_statutory_challan_events_actor_id", "actor_id"),
        Index("ix_statutory_challan_events_period", "period", "type"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    challan_id: Mapped[uuid.UUID] = fk_uuid("statutory_challans.id")
    type: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[dt.date] = mapped_column(Date, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    paid_on: Mapped[dt.date | None] = mapped_column(Date)
    reference: Mapped[str | None] = mapped_column("reference_enc", EncryptedString)
    amount: Mapped[str | None] = mapped_column("amount_enc", EncryptedString)
    prev_status: Mapped[str | None] = mapped_column(Text)
    prev_paid_on: Mapped[dt.date | None] = mapped_column(Date)
    prev_reference: Mapped[str | None] = mapped_column("prev_reference_enc", EncryptedString)
    prev_amount: Mapped[str | None] = mapped_column("prev_amount_enc", EncryptedString)
    actor_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
