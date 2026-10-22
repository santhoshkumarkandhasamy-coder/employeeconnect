from __future__ import annotations

import uuid
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.text import clean_text


class AnnouncementIn(BaseModel):
    """Create and PUT (full replace of the editable fields; omitted image/pdf are cleared)."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=150)
    body: str = Field(min_length=1, max_length=5000)
    priority: Literal["normal", "important", "urgent"] = "normal"
    image_file_id: uuid.UUID | None = None
    pdf_file_id: uuid.UUID | None = None
    target_all: bool = True
    department_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    expires_at: AwareDatetime | None = None
    renotify: bool = False  # PUT only: send the push again

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = clean_text(v)
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        v = clean_text(v, multiline=True)
        if not v:
            raise ValueError("must not be empty")
        return v

    @model_validator(mode="after")
    def _targets(self) -> AnnouncementIn:
        self.department_ids = list(dict.fromkeys(self.department_ids))
        if self.target_all:
            self.department_ids = []
        elif not self.department_ids:
            raise ValueError("department_ids needs at least one department when target_all is false")
        return self
