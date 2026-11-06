from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _clean(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("must not be blank")
    return v


class LeaveApply(BaseModel):
    leave_type_id: uuid.UUID
    from_date: dt.date
    to_date: dt.date
    half_day: bool = False
    half_day_session: Literal["first", "second"] | None = None
    reason: str = Field(max_length=500)
    attachment_file_id: uuid.UUID | None = None

    _r = field_validator("reason")(_clean)

    @model_validator(mode="after")
    def _half(self) -> LeaveApply:
        if self.half_day:
            if self.half_day_session is None:
                raise ValueError("half_day_session is required when half_day is true")
            if self.from_date != self.to_date:
                raise ValueError("half_day requires from_date == to_date")
        elif self.half_day_session is not None:
            raise ValueError("half_day_session is only allowed when half_day is true")
        return self


class PermissionApply(BaseModel):
    date: dt.date
    from_time: dt.time
    to_time: dt.time
    reason: str = Field(max_length=500)

    _r = field_validator("reason")(_clean)


class ApproveBody(BaseModel):
    comment: str | None = Field(None, max_length=500)


class RejectBody(BaseModel):
    comment: str = Field(max_length=500)

    _c = field_validator("comment")(_clean)


class OnDutyApply(BaseModel):
    client_location_id: uuid.UUID
    from_date: dt.date
    to_date: dt.date
    reason: str = Field(max_length=500)

    _r = field_validator("reason")(_clean)

    @model_validator(mode="after")
    def _dates(self) -> OnDutyApply:
        if self.to_date < self.from_date:
            raise ValueError("to_date must be on or after from_date")
        return self
