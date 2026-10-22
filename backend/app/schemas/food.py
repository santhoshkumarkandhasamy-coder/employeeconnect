from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, StrictBool, field_validator

from ..services.food import MAX_BULK


class EnrollmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: dt.date
    need_food: StrictBool  # Yes/No only; "yes", 1, null are rejected


class BulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[EnrollmentIn]

    @field_validator("items")
    @classmethod
    def _items(cls, v: list[EnrollmentIn]) -> list[EnrollmentIn]:
        if not 1 <= len(v) <= MAX_BULK:
            raise ValueError(f"must contain 1..{MAX_BULK} items")
        if len({i.date for i in v}) != len(v):
            raise ValueError("duplicate dates")
        return v


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: dict[str, bool | int | float | str]

    @field_validator("settings")
    @classmethod
    def _settings(cls, v: dict) -> dict:
        if not 1 <= len(v) <= 50:
            raise ValueError("must contain 1..50 settings")
        return v
