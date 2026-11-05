"""Request bodies of plan 17 work package 5a (contract 19.2 to 19.5): publish requests, delivery reminders, company bank accounts,
payment batches. Responses are plain dicts built by the services. Unknown fields are 422 and validators never echo values."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from ..core.text import clean_text
from .payroll import VersionIn, _label, _reason

__all__ = [
    "VersionIn",
    "PublishRequestCreate",
    "PublishRequestApprove",
    "PublishRequestReject",
    "DeliveryRemind",
    "BankAccountCreate",
    "BankAccountUpdate",
    "PaymentBatchCreate",
    "PaymentBatchUpdate",
    "PaymentBatchApprove",
    "PaymentBatchReject",
    "PaymentBatchCancel",
    "PaymentFileIn",
    "PaymentConfirm",
]

ACCOUNT_RE = re.compile(r"^\d{9,18}$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
BANK_REF_RE = re.compile(r"^[A-Za-z0-9 ._/\-]{1,40}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _opt_reason(v: str | None) -> str | None:
    return _reason(v) if v is not None else None


# ---------------------------------------------------------------------------------------------- publish requests (19.2)
class PublishRequestCreate(_Strict):
    comment: str | None = None

    _c = field_validator("comment")(_opt_reason)


class PublishRequestApprove(_Strict):
    expected_version: int = Field(ge=1)
    comment: str | None = None
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _c = field_validator("comment")(_opt_reason)


class PublishRequestReject(_Strict):
    expected_version: int = Field(ge=1)
    comment: str

    _c = field_validator("comment")(_reason)


# ---------------------------------------------------------------------------------------------- delivery (19.3)
class DeliveryRemind(_Strict):
    user_ids: list[uuid.UUID] | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------------------------- bank accounts (19.4)
class BankAccountCreate(_Strict):
    label: str
    bank_name: str
    account: str
    ifsc: str
    is_default: bool = False
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _l = field_validator("label")(_label)

    @field_validator("bank_name")
    @classmethod
    def _bank(cls, v: str) -> str:
        v = clean_text(v)
        if not 1 <= len(v) <= 60:
            raise ValueError("must be 1-60 characters")
        return v

    @field_validator("account")
    @classmethod
    def _account(cls, v: str) -> str:
        if not ACCOUNT_RE.match(v):
            raise ValueError("must be 9 to 18 digits")
        return v

    @field_validator("ifsc")
    @classmethod
    def _ifsc(cls, v: str) -> str:
        if not IFSC_RE.match(v):
            raise ValueError("must look like HDFC0001234")
        return v


class BankAccountUpdate(_Strict):
    label: str | None = None
    is_default: bool | None = None
    active: bool | None = None

    @field_validator("label")
    @classmethod
    def _l(cls, v: str | None) -> str | None:
        return _label(v) if v is not None else None

    @model_validator(mode="after")
    def _some(self) -> BankAccountUpdate:
        if not self.model_fields_set:
            raise ValueError("send at least one field")
        if any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError("fields cannot be null")
        return self


# ---------------------------------------------------------------------------------------------- payment batches (19.5)
class PaymentBatchCreate(_Strict):
    debit_account_id: uuid.UUID | None = None
    value_date: dt.date
    user_ids: list[uuid.UUID] | None = Field(default=None, max_length=1000)


class AccountSource(_Strict):
    line_id: uuid.UUID
    source: Literal["snapshot", "current"]


class PaymentBatchUpdate(_Strict):
    expected_version: int = Field(ge=1)
    value_date: dt.date | None = None
    debit_account_id: uuid.UUID | None = None
    exclude_line_ids: list[uuid.UUID] | None = Field(default=None, max_length=1000)
    include_line_ids: list[uuid.UUID] | None = Field(default=None, max_length=1000)
    account_sources: list[AccountSource] | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _no_nulls(self) -> PaymentBatchUpdate:
        if any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError("fields cannot be null")
        return self


class PaymentBatchApprove(_Strict):
    expected_version: int = Field(ge=1)
    comment: str | None = None
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _c = field_validator("comment")(_opt_reason)


class PaymentBatchReject(_Strict):
    expected_version: int = Field(ge=1)
    comment: str

    _c = field_validator("comment")(_reason)


class PaymentBatchCancel(_Strict):
    reason: str

    _r = field_validator("reason")(_reason)


class PaymentFileIn(_Strict):
    current_password: SecretStr = Field(min_length=1, max_length=128)


def _bank_ref(v: str | None) -> str | None:
    if v is None:
        return None
    if not BANK_REF_RE.match(v):
        raise ValueError("1-40 characters: letters, digits, space and . _ / -")
    return v


class ConfirmLine(_Strict):
    line_id: uuid.UUID
    status: Literal["paid", "failed"]
    reference: str | None = None

    _ref = field_validator("reference")(_bank_ref)


class PaymentConfirm(_Strict):
    paid_on: dt.date
    lines: list[ConfirmLine] | None = Field(default=None, max_length=1000)
    all_paid: bool = False
    reference: str | None = None
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _ref = field_validator("reference")(_bank_ref)

    @model_validator(mode="after")
    def _something(self) -> PaymentConfirm:
        if not self.all_paid and not self.lines:
            raise ValueError("send lines or all_paid")
        return self
