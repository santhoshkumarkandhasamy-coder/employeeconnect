"""Request bodies of plan 17 work package 5b (contract 19.6 to 19.8): tax year rules, investment declarations / proofs, Form 16.
Responses are plain dicts built by the services. Unknown fields are 422 and validators never echo values."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ..core.text import clean_text
from .payroll import _reason

__all__ = [
    "RulesUpdate",
    "RulesVersionIn",
    "RulesVerify",
    "RulesReject",
    "WindowsIn",
    "DeclarationSave",
    "DeclarationSubmit",
    "DeclarationSaveOnBehalf",
    "DeclarationSubmitOnBehalf",
    "DeclarationUnlock",
    "ProofCreate",
    "ProofVerify",
    "ProofReject",
    "DeclarationsReportIn",
    "Form16Document",
    "Form16Publish",
    "Form16Withdraw",
]

FY_RE = re.compile(r"^\d{4}-\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def check_fy(v: str) -> str:
    if not FY_RE.match(v):
        raise ValueError("must look like 2026-27")
    start = int(v[:4])
    if not 2000 <= start <= 2200 or int(v[5:]) != (start + 1) % 100:
        raise ValueError("must be a financial year such as 2026-27")
    return v


# ---------------------------------------------------------------------------------------------- rules (19.6)
class RulesVersionIn(_Strict):
    version_row: int = Field(ge=1)


class RulesUpdate(_Strict):
    version_row: int = Field(ge=1)
    rules: dict[str, Any]
    note: str | None = Field(default=None, max_length=300)

    @field_validator("note")
    @classmethod
    def _n(cls, v: str | None) -> str | None:
        return clean_text(v) if v is not None else None


class RulesVerify(_Strict):
    version_row: int = Field(ge=1)
    rules_sha256: str
    attestation: str
    current_password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("rules_sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not SHA_RE.match(v):
            raise ValueError("must be 64 lower-case hex characters")
        return v

    @field_validator("attestation")
    @classmethod
    def _att(cls, v: str) -> str:
        v = clean_text(v)
        if not 20 <= len(v) <= 500:
            raise ValueError("must be 20-500 characters")
        return v


class RulesReject(_Strict):
    version_row: int = Field(ge=1)
    comment: str

    _c = field_validator("comment")(_reason)


class WindowsIn(_Strict):
    declaration_opens_on: dt.date | None = None
    declaration_closes_on: dt.date | None = None
    proof_opens_on: dt.date | None = None
    proof_closes_on: dt.date | None = None


# ---------------------------------------------------------------------------------------------- declarations (19.7)
class DeclarationItemIn(_Strict):
    id: str | None = Field(default=None, max_length=32)
    section: str = Field(max_length=16)
    amount: str | None = Field(default=None, max_length=20)
    details: dict[str, Any] = Field(default_factory=dict)


class DeclarationSave(_Strict):
    fy: str
    expected_version: int = Field(ge=0)
    regime: str | None
    items: list[DeclarationItemIn] = Field(max_length=40)

    _fy = field_validator("fy")(check_fy)

    @field_validator("regime")
    @classmethod
    def _r(cls, v: str | None) -> str | None:
        if v is not None and v not in ("old", "new"):
            raise ValueError("must be old or new")
        return v


class DeclarationSubmit(_Strict):
    fy: str
    expected_version: int = Field(ge=1)

    _fy = field_validator("fy")(check_fy)


class DeclarationSaveOnBehalf(DeclarationSave):
    current_password: SecretStr = Field(min_length=1, max_length=128)


class DeclarationSubmitOnBehalf(DeclarationSubmit):
    current_password: SecretStr = Field(min_length=1, max_length=128)


class DeclarationUnlock(_Strict):
    fy: str
    until: dt.date
    reason: str

    _fy = field_validator("fy")(check_fy)
    _r = field_validator("reason")(_reason)


class ProofCreate(_Strict):
    fy: str
    item_id: str = Field(min_length=1, max_length=64)
    claimed: str = Field(max_length=20)
    file_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)

    _fy = field_validator("fy")(check_fy)


def _opt_comment(v: str | None) -> str | None:
    return _reason(v) if v is not None else None


class ProofVerify(_Strict):
    expected_version: int = Field(ge=1)
    verified: str = Field(max_length=20)
    comment: str | None = None

    _c = field_validator("comment")(_opt_comment)


class ProofReject(_Strict):
    expected_version: int = Field(ge=1)
    comment: str

    _c = field_validator("comment")(_reason)


class DeclarationsReportIn(_Strict):
    include_identifiers: bool = False
    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=128)


# ---------------------------------------------------------------------------------------------- Form 16 (19.8)
class Form16Document(_Strict):
    id: uuid.UUID
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not SHA_RE.match(v):
            raise ValueError("must be 64 lower-case hex characters")
        return v


class Form16Publish(_Strict):
    fy: str
    documents: list[Form16Document] = Field(min_length=1, max_length=500)
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _fy = field_validator("fy")(check_fy)


class Form16Withdraw(_Strict):
    reason: str
    current_password: SecretStr = Field(min_length=1, max_length=128)

    _r = field_validator("reason")(_reason)
