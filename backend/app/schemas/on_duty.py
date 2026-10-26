"""On Duty request bodies (plan 13). Unknown fields are refused (422 VALIDATION_ERROR) on every body."""

from __future__ import annotations

import datetime as dt
import math
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from ..core.text import clean_text

NOTE_MAX = 200
PURPOSE_MAX = 300


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _clean_note(v: str | None) -> str | None:
    if v is None:
        return None
    v = clean_text(v)
    if not v:
        return None
    if len(v) > NOTE_MAX:
        raise ValueError(f"must be at most {NOTE_MAX} characters")
    return v


class DutyCheckIn(_Strict):
    location_id: uuid.UUID
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(ge=0, le=100_000)
    fix_time: dt.datetime
    selfie_file_id: uuid.UUID | None = None
    client_time: dt.datetime | None = None  # diagnostics only
    is_mock_location: bool | None = None
    note: str | None = Field(None, max_length=2000)

    _n = field_validator("note")(_clean_note)


class DutyCheckOut(_Strict):
    visit_id: uuid.UUID | None = None
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(ge=0, le=100_000)
    fix_time: dt.datetime
    selfie_file_id: uuid.UUID | None = None
    client_time: dt.datetime | None = None
    is_mock_location: bool | None = None
    note: str | None = Field(None, max_length=2000)

    _n = field_validator("note")(_clean_note)


class OnDutyApply(_Strict):
    from_date: dt.date
    to_date: dt.date
    location_id: uuid.UUID | None = None
    purpose: str = Field(max_length=2000)

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, v: str) -> str:
        v = clean_text(v)
        if not v:
            raise ValueError("must not be blank")
        if len(v) > PURPOSE_MAX:
            raise ValueError(f"must be at most {PURPOSE_MAX} characters")
        return v


def _finite(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("must be a finite number")
    return v


class DutyLocationIn(_Strict):
    name: str = Field(max_length=500)
    address: str | None = Field(None, max_length=2000)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    radius_m: int = Field(ge=10, le=2000)
    assign_mode: Literal["all", "selected"] = "selected"
    is_active: StrictBool = True

    _f = field_validator("lat", "lng")(_finite)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = clean_text(v)
        if not 2 <= len(v) <= 80:
            raise ValueError("must be 2-80 characters")
        return v

    @field_validator("address")
    @classmethod
    def _address(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = clean_text(v)
        if len(v) > 300:
            raise ValueError("must be at most 300 characters")
        return v or None


class AssignmentsIn(_Strict):
    assign_mode: Literal["all", "selected"]
    user_ids: list[uuid.UUID] = Field(max_length=5000)
    add_department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
