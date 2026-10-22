from __future__ import annotations

from pydantic import BaseModel


class IdName(BaseModel):
    id: str
    name: str
