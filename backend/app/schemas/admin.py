"""Request bodies of the admin masters (departments, shifts, leave types, roles, leave balances). PUT = full replace."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, field_validator, model_validator

from ..core.text import clean_text


def _name(v: str) -> str:
    v = clean_text(v)
    if not v:
        raise ValueError("must not be empty")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DepartmentIn(_Strict):
    name: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    is_active: StrictBool = True

    _n = field_validator("name")(_name)

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        return v.upper()


class ShiftIn(_Strict):
    name: str = Field(min_length=1, max_length=100)
    start_time: dt.time
    end_time: dt.time
    grace_minutes: int = Field(default=10, ge=0, le=120)
    working_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5], min_length=1, max_length=7)
    is_active: StrictBool = True

    _n = field_validator("name")(_name)

    @field_validator("working_days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d < 1 or d > 7 for d in v):
            raise ValueError("days must be 1 (Monday) to 7 (Sunday)")
        return sorted(set(v))

    @model_validator(mode="after")
    def _times(self) -> ShiftIn:
        if self.start_time == self.end_time:
            raise ValueError("start_time and end_time must differ")
        return self


class LeaveTypeIn(_Strict):
    code: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    annual_quota: Decimal = Field(ge=0, le=Decimal("999.9"), max_digits=4, decimal_places=1, allow_inf_nan=False)
    is_paid: StrictBool = True
    allow_half_day: StrictBool = True
    requires_attachment_after_days: int | None = Field(default=None, ge=0, le=366)
    max_consecutive_days: int | None = Field(default=None, ge=1, le=366)
    is_active: StrictBool = True

    _n = field_validator("name")(_name)

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        return v.upper()


class RoleIn(_Strict):
    name: str = Field(min_length=2, max_length=30, pattern=r"^[a-z][a-z0-9_]*$")
    description: str | None = Field(default=None, max_length=200)

    @field_validator("description")
    @classmethod
    def _d(cls, v: str | None) -> str | None:
        return (clean_text(v) or None) if v is not None else None


class RoleCreateIn(RoleIn):
    permission_codes: list[str] = Field(default_factory=list, max_length=200)


class RolePermissionsIn(_Strict):
    permission_codes: list[str] = Field(max_length=200)
    current_password: SecretStr = Field(min_length=1, max_length=128)  # the acting admin's own password (step-up)


class BalanceItemIn(_Strict):
    leave_type_id: uuid.UUID
    quota: Decimal = Field(ge=0, le=Decimal("999.9"), max_digits=4, decimal_places=1, allow_inf_nan=False)


class BalancesIn(_Strict):
    year: int = Field(ge=2000, le=2200)
    items: list[BalanceItemIn] = Field(min_length=1, max_length=50)


class ApplyDefaultsIn(_Strict):
    year: int = Field(ge=2000, le=2200)
