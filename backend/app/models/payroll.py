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
        Index("ix_salary_structures_user_effective", "user_id", text("effective_from DESC")),
        Index("ix_salary_structures_created_by", "created_by"),
        Index("ix_salary_structures_updated_by", "updated_by"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = fk_uuid("users.id")
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    components: Mapped[dict] = mapped_column("components_enc", EncryptedJSON, nullable=False)
    pf_applicable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    pf_on_full_basic: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    esi_applicable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    pt_applicable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)
    updated_by: Mapped[uuid.UUID | None] = fk_uuid("users.id", nullable=True)


class EmployeePayrollDetails(TimestampMixin, Base):
    """1:1 with users (optional). Formats (PAN/IFSC/account/UAN/ESI IP) are validated by the service, not the DB."""
    __tablename__ = "employee_payroll_details"
    __table_args__ = (
        CheckConstraint("designation IS NULL OR char_length(designation) <= 60", name="ck_employee_payroll_details_designation"),
        CheckConstraint("payment_mode IN ('bank_transfer','cheque','cash')", name="ck_employee_payroll_details_payment_mode"),
        Index("ix_employee_payroll_details_updated_by", "updated_by"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    designation: Mapped[str | None] = mapped_column(Text)
    payment_mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'bank_transfer'"))
    include_in_payroll: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
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
        CheckConstraint("status <> 'published' OR (published_at IS NOT NULL AND published_by IS NOT NULL)",
                        name="ck_payroll_runs_published"),
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
        CheckConstraint("basis_days >= 0 AND employed_days >= 0 AND lop_auto >= 0 AND paid_days >= 0 AND paid_days <= basis_days",
                        name="ck_payslips_days"),
        CheckConstraint("lop_override IS NULL OR (lop_override >= 0 AND lop_override * 2 = trunc(lop_override * 2))",
                        name="ck_payslips_lop_override"),
        CheckConstraint("lop_override IS NULL OR lop_override_reason_enc IS NOT NULL", name="ck_payslips_lop_override_reason"),
        CheckConstraint("status = 'draft' OR result_enc IS NOT NULL", name="ck_payslips_result_when_processed"),
        CheckConstraint("revision = 1 OR revision_reason_enc IS NOT NULL", name="ck_payslips_revision_reason"),
        CheckConstraint("version >= 1", name="ck_payslips_version"),
        CheckConstraint("status NOT IN ('published','superseded') OR (published_at IS NOT NULL AND published_by IS NOT NULL)",
                        name="ck_payslips_published"),
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
