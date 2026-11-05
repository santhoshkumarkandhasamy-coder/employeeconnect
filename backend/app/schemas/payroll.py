"""Request bodies for the payroll endpoints (contract section 13). Responses are plain dicts built by the services.

Money is a JSON string (never a number) with at most 2 decimals; validators never echo the submitted value."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, field_validator, model_validator

from ..core.text import clean_text
from ..services import payroll_calc as calc

PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _money(v: Any) -> Decimal:
    return calc.parse_money(v)


Money = Annotated[Decimal, BeforeValidator(_money)]  # a JSON string with <= 2 decimals; numbers are refused


def _label(v: str) -> str:
    v = clean_text(v)
    if not 1 <= len(v) <= 40:
        raise ValueError("must be 1-40 characters")
    return v


_FORMULA_START = ("=", "+", "-", "@", "＝", "＋", "－", "＠")


def _item_label(v: str) -> str:
    """A one-time item label: as `_label`, and the CLEANED text must not look like a spreadsheet formula (it ends up in
    xlsx exports; checking before cleaning would miss `<b>=1+1`, zero-width or control-character prefixes)."""
    v = _label(v)
    if v.startswith(_FORMULA_START):
        raise ValueError("must not start with = + - or @")
    return v


def _code(v: str) -> str:
    if not calc.CODE_RE.match(v):
        raise ValueError("must match ^[A-Z][A-Z0-9_]{1,15}$")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayrollSettingsIn(_Strict):
    settings: dict[str, Any] = Field(min_length=1, max_length=40)
    # the caller's own password; required only when `payroll.publish_requires_second_person` is being changed
    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=128)


class AllowanceIn(_Strict):
    code: str
    name: str
    amount: Money
    prorate: bool = True
    esi_wage: bool = True

    _c = field_validator("code")(_code)
    _n = field_validator("name")(_label)

    @field_validator("code")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in calc.RESERVED_CODES:
            raise ValueError("this code is reserved")
        return v


class FixedDeductionIn(_Strict):
    code: str
    name: str
    amount: Money

    _c = field_validator("code")(_code)
    _n = field_validator("name")(_label)

    @field_validator("code")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in calc.RESERVED_CODES:
            raise ValueError("this code is reserved")
        return v


class StructureBody(_Strict):
    """A structure without its effective date (also the `structure` of an increment)."""

    basic: Money
    hra: Money = Decimal("0.00")
    allowances: list[AllowanceIn] = Field(default_factory=list, max_length=15)
    fixed_deductions: list[FixedDeductionIn] = Field(default_factory=list, max_length=10)
    tds_monthly: Money = Decimal("0.00")
    pf_applicable: bool = True
    pf_on_full_basic: bool = False
    esi_applicable: bool = True
    pt_applicable: bool = True

    @model_validator(mode="after")
    def _checks(self) -> StructureBody:
        if self.basic <= 0:
            raise ValueError("basic must be greater than 0")
        codes = [a.code for a in self.allowances] + [d.code for d in self.fixed_deductions]
        if len(set(codes)) != len(codes):
            raise ValueError("allowance and deduction codes must be unique")
        return self


class StructureIn(StructureBody):
    effective_from: dt.date


_FORMATS = {
    "pan": re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$"),
    "bank_ifsc": re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$"),
    "bank_account": re.compile(r"^\d{9,18}$"),
    "uan": re.compile(r"^\d{12}$"),
    "esi_ip_number": re.compile(r"^\d{10}$"),
}


class DetailsIn(_Strict):
    """Only the keys that are sent change; `null` clears. `current_password` (your own) is always required."""

    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=128)
    designation: str | None = Field(default=None, max_length=60)
    payment_mode: Literal["bank_transfer", "cheque", "cash"] | None = None
    include_in_payroll: bool | None = None
    overtime_eligible: bool | None = None  # plan 15 3b; the only key that needs no current_password when sent alone
    bank_name: str | None = Field(default=None, max_length=60)
    bank_account: str | None = Field(default=None, max_length=40)
    bank_ifsc: str | None = Field(default=None, max_length=20)
    pan: str | None = Field(default=None, max_length=20)
    uan: str | None = Field(default=None, max_length=20)
    esi_ip_number: str | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _normalise(self) -> DetailsIn:
        for name in ("designation", "bank_name"):
            v = getattr(self, name)
            if v is not None:
                v = clean_text(v)
                setattr(self, name, v or None)
        for name, rx in _FORMATS.items():
            v = getattr(self, name)
            if v is None:
                continue
            v = re.sub(r"\s+", "", v).upper()
            if v == "":
                setattr(self, name, None)
                continue
            if not rx.match(v):
                raise ValueError(f"{name} has an invalid format")
            setattr(self, name, v)
        for name in ("payment_mode", "include_in_payroll", "overtime_eligible"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be cleared")
        return self


class RunCreate(_Strict):
    period: str
    pay_date: dt.date | None = None


class PublishIn(_Strict):
    current_password: SecretStr = Field(min_length=1, max_length=128)


class ReviseIn(_Strict):
    reason: str = Field(min_length=3, max_length=200)

    @field_validator("reason")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = clean_text(v)
        if not 3 <= len(v) <= 200:
            raise ValueError("must be 3-200 characters")
        return v


class RegisterIn(_Strict):
    include_bank: bool = False
    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=128)


class AdjustmentIn(_Strict):
    code: Literal[
        "BONUS", "ARREARS", "INCENTIVE", "REIMBURSEMENT", "OTHER_EARNING", "ADVANCE_RECOVERY", "LOAN_EMI", "OTHER_DEDUCTION"
    ]
    label: str
    amount: Money

    _l = field_validator("label")(_label)


class PayslipUpdate(_Strict):
    expected_version: int = Field(ge=1)
    lop_override: float | None = None
    lop_override_reason: str | None = Field(default=None, max_length=200)
    tds: Money | None = None
    pt_override: Money | None = None
    adjustments: list[AdjustmentIn] | None = Field(default=None, max_length=10)
    hold: bool | None = None
    note: str | None = Field(default=None, max_length=200)

    @field_validator("lop_override")
    @classmethod
    def _half_steps(cls, v: float | None) -> float | None:
        if v is None:
            return None
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            raise ValueError("must be a number") from None
        if not d.is_finite() or d < 0 or d > Decimal("99.5") or (d * 2) != (d * 2).to_integral_value():
            raise ValueError("must be 0 or more, in steps of 0.5")
        return v

    @field_validator("lop_override_reason", "note")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        return (clean_text(v) or None) if v is not None else None

    @model_validator(mode="after")
    def _nulls(self) -> PayslipUpdate:
        for name in ("adjustments", "hold"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        reason = self.lop_override_reason
        if (self.lop_override is not None and not reason) or (reason is not None and not 3 <= len(reason) <= 200):
            raise ValueError("lop_override_reason of 3-200 characters is required with lop_override")
        return self


class ChallanIn(_Strict):
    """Mark one statutory due paid or unpaid (plan 14 phase 2). `current_password` (step-up) is always required."""

    type: Literal["pf", "esi", "pt", "tds"]
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    status: Literal["paid", "unpaid"]
    paid_on: dt.date | None = None
    reference: str | None = Field(default=None, min_length=1, max_length=60, pattern=r"^[A-Za-z0-9 ._/\-]+$")
    amount: str | None = Field(default=None, max_length=14)
    current_password: SecretStr = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _check(self) -> ChallanIn:
        if self.status == "paid" and self.paid_on is None:
            raise ValueError("paid_on is required when status is paid")
        if self.status == "unpaid" and (self.paid_on is not None or self.reference is not None or self.amount is not None):
            raise ValueError("paid_on, reference and amount only apply to status paid")
        if self.amount is not None and _money(self.amount) <= 0:
            raise ValueError("amount must be greater than 0")
        return self


class StatutoryFileIn(_Strict):
    """Body of the statutory file downloads. `current_password` (step-up) is required for pf_ecr and esi, ignored for pt."""

    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=128)


# ============================================================================================ plan 15: pay components
ItemCode = Literal["BONUS", "INCENTIVE", "OTHER_EARNING", "OTHER_DEDUCTION"]
PeriodStr = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


def _reason(v: str) -> str:
    v = clean_text(v)
    if not 3 <= len(v) <= 200:
        raise ValueError("must be 3-200 characters")
    return v


def _positive(v: Decimal) -> Decimal:
    if v <= 0:
        raise ValueError("must be greater than 0 (use OTHER_DEDUCTION for a deduction)")
    return v


class OneTimeItemCreate(_Strict):
    user_id: uuid.UUID
    period: PeriodStr
    code: ItemCode
    label: str
    amount: Money

    _l = field_validator("label")(_item_label)
    _a = field_validator("amount")(_positive)


class OneTimeItemUpdate(_Strict):
    expected_version: int = Field(ge=1)
    period: PeriodStr | None = None
    label: str | None = None
    amount: Money | None = None

    @field_validator("label")
    @classmethod
    def _lab(cls, v: str | None) -> str | None:
        return _item_label(v) if v is not None else None

    @field_validator("amount")
    @classmethod
    def _amt(cls, v: Decimal | None) -> Decimal | None:
        return _positive(v) if v is not None else None

    @model_validator(mode="after")
    def _something(self) -> OneTimeItemUpdate:
        sent = self.model_fields_set - {"expected_version"}
        if not sent:
            raise ValueError("send at least one of period, label, amount")
        for name in sent:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class ReasonIn(_Strict):
    """Body of the cancel actions: a reason of 3-200 characters (stored encrypted, never audited)."""

    reason: str

    _r = field_validator("reason")(_reason)


# ---------------------------------------------------------------------------------------------- loans (3a step 2)
LoanKind = Literal["loan", "advance"]
_SAFE_REF = re.compile(r"^[A-Za-z0-9 ._/\-]{1,60}$")


class LoanCreate(_Strict):
    user_id: uuid.UUID
    kind: LoanKind
    principal: Money
    emi: Money
    start_period: PeriodStr
    reason: str
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _r = field_validator("reason")(_reason)


class LoanSkip(_Strict):
    installment_id: uuid.UUID
    reason: str
    expected_version: int = Field(ge=1)

    _r = field_validator("reason")(_reason)


class LoanReschedule(_Strict):
    emi: Money
    from_period: PeriodStr
    reason: str
    expected_version: int = Field(ge=1)
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _r = field_validator("reason")(_reason)


class LoanForeclose(_Strict):
    mode: Literal["payroll", "outside"]
    period: PeriodStr | None = None
    paid_on: dt.date | None = None
    reference: str | None = Field(default=None, min_length=1, max_length=60)
    reason: str
    expected_version: int = Field(ge=1)
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _r = field_validator("reason")(_reason)

    @model_validator(mode="after")
    def _by_mode(self) -> LoanForeclose:
        if self.mode == "payroll":
            if self.period is None:
                raise ValueError("period is required for mode payroll")
            if self.paid_on is not None or self.reference is not None:
                raise ValueError("paid_on and reference only apply to mode outside")
        else:
            if self.paid_on is None or self.reference is None:
                raise ValueError("paid_on and reference are required for mode outside")
            if self.period is not None:
                raise ValueError("period only applies to mode payroll")
            if not _SAFE_REF.match(self.reference):
                raise ValueError("reference may only contain letters, digits, space and . _ / -")
        return self


class LoanCancel(_Strict):
    reason: str
    expected_version: int = Field(ge=1)
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _r = field_validator("reason")(_reason)


# ---------------------------------------------------------------------------------------------- arrears / encashment
class ArrearsAccept(_Strict):
    period: PeriodStr
    expected_version: int = Field(ge=1)
    current_password: SecretStr = Field(min_length=1, max_length=128)


def _half_days(v: Decimal) -> Decimal:
    if v <= 0 or v > Decimal("999.9") or (v * 2) != (v * 2).to_integral_value():
        raise ValueError("must be greater than 0, in steps of 0.5")
    return v


class EncashmentCreate(_Strict):
    user_id: uuid.UUID
    leave_type_id: uuid.UUID
    year: int = Field(ge=2000, le=2200)
    days: Decimal = Field(allow_inf_nan=False, max_digits=4, decimal_places=1)
    period: PeriodStr
    source: Literal["request", "fnf"] = "request"
    reason: str
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _r = field_validator("reason")(_reason)
    _d = field_validator("days")(_half_days)


class OptionalReasonIn(_Strict):
    """Optional body of encashment cancel (kept in the encrypted money history only)."""

    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def _opt(cls, v: str | None) -> str | None:
        return _reason(v) if v is not None else None


# ============================================================================================ plan 15 work package 3b
def _opt_comment(v: str | None) -> str | None:
    return _reason(v) if v is not None else None


class ClaimCreate(_Strict):
    category: str = Field(pattern=r"^[a-z][a-z0-9_]{1,19}$")
    expense_date: dt.date
    amount: Money
    description: str
    bill_file_id: uuid.UUID | None = None

    @field_validator("amount")
    @classmethod
    def _amt(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("must be greater than 0")
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        v = clean_text(v)
        if not 3 <= len(v) <= 300:
            raise ValueError("must be 3-300 characters")
        return v


class ClaimVerify(_Strict):
    comment: str | None = None
    _c = field_validator("comment")(_opt_comment)


class ClaimApprove(_Strict):
    approved_amount: Money
    period: PeriodStr
    comment: str | None = None
    expected_version: int = Field(ge=1)

    _c = field_validator("comment")(_opt_comment)

    @field_validator("approved_amount")
    @classmethod
    def _amt(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("must be greater than 0")
        return v


class ClaimReject(_Strict):
    comment: str

    _c = field_validator("comment")(_reason)


class ClaimPayout(_Strict):
    period: PeriodStr
    expected_version: int = Field(ge=1)


class OvertimeUpdate(_Strict):
    status: Literal["approved", "rejected"]
    approved_minutes: int | None = Field(default=None, ge=0, le=1440)
    comment: str | None = None
    expected_version: int = Field(ge=1)

    _c = field_validator("comment")(_opt_comment)

    @model_validator(mode="after")
    def _by_status(self) -> OvertimeUpdate:
        if self.status == "rejected":
            if self.comment is None:
                raise ValueError("comment is required when rejecting")
            if self.approved_minutes is not None:
                raise ValueError("approved_minutes only applies to status approved")
        return self


class OvertimeBulk(_Strict):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    action: Literal["approve", "reject"]
    comment: str | None = None
    include_flagged: bool = False  # L5: also approve SUSPICIOUS / AUTO_CLOSED rows (ATTENDANCE_CHANGED rows never bulk)

    _c = field_validator("comment")(_opt_comment)

    @model_validator(mode="after")
    def _by_action(self) -> OvertimeBulk:
        if self.action == "reject" and self.comment is None:
            raise ValueError("comment is required when rejecting")
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("ids must be unique")
        return self


# ============================================================================================ plan 16 work package 4a
IncrementMethod = Literal["proportional", "split_from_gross"]
_INCREMENT_FIELDS = {
    "percent": {"kind", "effective_from", "percent", "method", "reason"},
    "amount": {"kind", "effective_from", "monthly_gross", "method", "reason"},
    "structure": {"kind", "effective_from", "structure", "replaces_structure_id", "reason"},
    "remove": {"kind", "replaces_structure_id", "reason"},
}
_INCREMENT_REQUIRED = {
    "percent": {"effective_from", "percent", "method", "reason"},
    "amount": {"effective_from", "monthly_gross", "method", "reason"},
    "structure": {"effective_from", "structure", "reason"},
    "remove": {"replaces_structure_id", "reason"},
}


class IncrementCreate(_Strict):
    """One body, four shapes (`kind`): fields that do not belong to the kind are refused (422)."""

    kind: Literal["percent", "amount", "structure", "remove"]
    effective_from: dt.date | None = None
    percent: Money | None = None
    monthly_gross: Money | None = None
    method: IncrementMethod | None = None
    structure: StructureBody | None = None
    replaces_structure_id: uuid.UUID | None = None
    reason: str

    _r = field_validator("reason")(_reason)

    @model_validator(mode="after")
    def _shape(self) -> IncrementCreate:
        sent = self.model_fields_set - {"expected_version"}
        extra = sent - _INCREMENT_FIELDS[self.kind]
        if extra:
            raise ValueError(f"not allowed for kind {self.kind}: {', '.join(sorted(extra))}")
        for name in _INCREMENT_REQUIRED[self.kind]:
            if getattr(self, name) is None:
                raise ValueError(f"{name} is required for kind {self.kind}")
        return self


class IncrementUpdate(IncrementCreate):
    expected_version: int = Field(ge=1)


class VersionIn(_Strict):
    expected_version: int = Field(ge=1)


class IncrementApprove(_Strict):
    expected_version: int = Field(ge=1)
    comment: str | None = None
    current_password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("comment")
    @classmethod
    def _c(cls, v: str | None) -> str | None:
        return _reason(v) if v is not None else None


class IncrementReject(_Strict):
    expected_version: int = Field(ge=1)
    comment: str

    _c = field_validator("comment")(_reason)


class EmptyIn(_Strict):
    """`{}` (a body is required, nothing else is accepted)."""


class BatchApprove(_Strict):
    comment: str | None = None
    current_password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("comment")
    @classmethod
    def _c(cls, v: str | None) -> str | None:
        return _reason(v) if v is not None else None


class BatchReject(_Strict):
    comment: str

    _c = field_validator("comment")(_reason)


class BulkScope(_Strict):
    department_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    user_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)
    exclude_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)


class BulkOverride(_Strict):
    user_id: uuid.UUID
    percent: Money


class BulkIncrementCreate(_Strict):
    effective_from: dt.date
    percent: Money
    method: IncrementMethod
    reason: str
    scope: BulkScope = Field(default_factory=BulkScope)
    overrides: list[BulkOverride] = Field(default_factory=list, max_length=500)

    _r = field_validator("reason")(_reason)

    @model_validator(mode="after")
    def _unique_overrides(self) -> BulkIncrementCreate:
        ids = [o.user_id for o in self.overrides]
        if len(set(ids)) != len(ids):
            raise ValueError("overrides must name each employee once")
        return self


class ArrearsBulkItem(_Strict):
    id: uuid.UUID
    expected_version: int = Field(ge=1)


class ArrearsBulkAccept(_Strict):
    items: list[ArrearsBulkItem] = Field(min_length=1, max_length=200)
    period: PeriodStr
    current_password: SecretStr = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _unique(self) -> ArrearsBulkAccept:
        ids = [i.id for i in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("items must be unique")
        return self


# ---------------------------------------------------------------------------------------------- CTC calculator (4a step 2)
_AMOUNT_RE = re.compile(r"^\d{1,9}(\.\d{1,2})?$")
_CALC_MAX_ANNUAL = Decimal("120000000.00")


def _calc_amount(v: Any) -> Decimal:
    """A JSON string with at most 2 decimals, 1.00 .. 120000000.00 (the monthly / annual limit is checked with the frequency)."""
    if not isinstance(v, str) or not _AMOUNT_RE.match(v.strip()):
        raise ValueError('must be an amount as a string such as "40000.00"')
    d = Decimal(v.strip())
    if d < 1 or d > _CALC_MAX_ANNUAL:
        raise ValueError("must be between 1.00 and 120000000.00")
    return calc.q2(d)


class CtcSplitIn(_Strict):
    basic_pct: int = Field(ge=1, le=100, strict=True)
    hra_pct_of_basic: int = Field(ge=0, le=100, strict=True)


class CtcCalculateIn(_Strict):
    mode: Literal["from_gross", "from_ctc"]
    amount: Annotated[Decimal, BeforeValidator(_calc_amount)]
    frequency: Literal["monthly", "annual"]
    pf_applicable: bool = True
    pf_on_full_basic: bool = False
    esi_applicable: bool = True
    pt_applicable: bool = True
    split: CtcSplitIn | None = None
    include_gratuity: bool | None = None
    employer_share_from_employee: bool | None = None

    @model_validator(mode="after")
    def _limits(self) -> CtcCalculateIn:
        if self.frequency == "monthly" and self.amount > Decimal("10000000.00"):
            raise ValueError("a monthly amount must be at most 10000000.00")
        return self
