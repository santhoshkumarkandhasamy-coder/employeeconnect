from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.text import clean_text


class PunchRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    # accuracy_m / fix_time are mandatory for check-in (and for check-out when the check-out geofence is on). They are
    # optional in the schema so the service can answer with a precise VALIDATION_ERROR and an audit row.
    accuracy_m: float | None = Field(None, ge=0, le=100_000)
    fix_time: dt.datetime | None = None  # platform timestamp of the GPS fix (UTC ISO-8601); must be fresh
    selfie_file_id: uuid.UUID | None = None
    client_time: dt.datetime | None = None  # diagnostics only; server time is authoritative
    is_mock_location: bool | None = None  # client-reported; stored, flagged, optionally rejected (never trusted as "clean")


class ManualAttendanceIn(BaseModel):
    """HR/Admin manual entry or correction for one employee and date (office-local wall-clock times)."""

    model_config = ConfigDict(extra="forbid")
    check_in: dt.time
    check_out: dt.time | None = None
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = clean_text(v)
        if len(v) < 3:
            raise ValueError("give a reason (at least 3 characters)")
        return v


class HolidayIn(BaseModel):
    date: dt.date
    name: str = Field(min_length=1, max_length=120)


class HolidayPatch(BaseModel):
    date: dt.date | None = None
    name: str | None = Field(None, min_length=1, max_length=120)
    is_active: bool | None = None
