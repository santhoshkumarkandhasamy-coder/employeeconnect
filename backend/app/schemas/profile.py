from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictBool, field_validator, model_validator

from ..core.text import clean_text

_PHONE = re.compile(r"^\+?[0-9]{8,15}$")
BloodGroup = Literal["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]


def normalise_phone(v: str) -> str:
    compact = re.sub(r"[ \-().]", "", v.strip())
    if not _PHONE.match(compact):
        raise ValueError("must be a phone number with 8-15 digits, optionally starting with +")
    return compact


class EmergencyContactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    relationship: str | None = Field(default=None, max_length=50)
    phone: str = Field(max_length=32)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = clean_text(v)
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("relationship")
    @classmethod
    def _rel(cls, v: str | None) -> str | None:
        return (clean_text(v) or None) if v is not None else None

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalise_phone(v)


PERSONAL_KEYS = (
    "dob", "wedding_anniversary", "personal_email", "phone", "emergency_contact", "blood_group", "address",
)  # fmt: skip


class PersonalFields(BaseModel):
    """The personal fields shared by the self-service edit and the admin employee edit. Only the keys that are sent are
    changed; an explicit null clears the field. Unknown keys are rejected (extra=forbid -> 422)."""

    model_config = ConfigDict(extra="forbid")
    dob: dt.date | None = None
    wedding_anniversary: dt.date | None = None
    personal_email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    emergency_contact: EmergencyContactIn | None = None
    blood_group: BloodGroup | None = None
    address: str | None = Field(default=None, max_length=300)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return normalise_phone(v) if v is not None else None

    @field_validator("address")
    @classmethod
    def _address(cls, v: str | None) -> str | None:
        return (clean_text(v, multiline=True) or None) if v is not None else None

    @field_validator("dob")
    @classmethod
    def _dob(cls, v: dt.date | None) -> dt.date | None:
        if v is None:
            return v
        today = dt.date.today()
        age = today.year - v.year - ((today.month, today.day) < (v.month, v.day))
        if v >= today or not 16 <= age <= 100:
            raise ValueError("must be a past date giving an age between 16 and 100")
        return v

    @field_validator("wedding_anniversary")
    @classmethod
    def _anniv(cls, v: dt.date | None) -> dt.date | None:
        if v is not None and (v >= dt.date.today() or v.year < 1925):
            raise ValueError("must be a past date")
        return v


class ProfileUpdate(PersonalFields):
    """Self-service edit (`PUT /me/profile`): the personal fields plus the employee's own `show_celebrations` preference.
    full_name / official_email / department / role are not accepted."""

    show_celebrations: StrictBool | None = None

    @model_validator(mode="after")
    def _not_null_flags(self) -> ProfileUpdate:
        if "show_celebrations" in self.model_fields_set and self.show_celebrations is None:
            raise ValueError("show_celebrations cannot be null")
        return self
