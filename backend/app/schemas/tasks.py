from __future__ import annotations

import uuid

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from ..core.text import clean_text


class TaskIn(BaseModel):
    """Create and PUT (full replace). `user_id` (create only) assigns the task to someone else."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=150)
    notes: str | None = Field(None, max_length=2000)
    due_at: AwareDatetime
    remind_minutes_before: int | None = Field(None, ge=0, le=10080)  # null = no reminder, 0 = at the due time
    user_id: uuid.UUID | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = clean_text(v)
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("notes")
    @classmethod
    def _notes(cls, v: str | None) -> str | None:
        v = clean_text(v, multiline=True) if v is not None else None
        return v or None


class TaskDone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    done: bool
