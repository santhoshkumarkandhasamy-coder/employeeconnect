"""Request bodies for the payroll endpoints (contract section 13). Responses are plain dicts built by the services.

Money is a JSON string (never a number) with at most 2 decimals; validators never echo the submitted value."""

from __future__ import annotations

import datetime as dt
import re
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


class StructureIn(_Strict):
    effective_from: dt.date
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
    def _checks(self) -> StructureIn:
        if self.basic <= 0:
            raise ValueError("basic must be greater than 0")
        codes = [a.code for a in self.allowances] + [d.code for d in self.fixed_deductions]
        if len(set(codes)) != len(codes):
            raise ValueError("allowance and deduction codes must be unique")
        return self


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
        for name in ("payment_mode", "include_in_payroll"):
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
