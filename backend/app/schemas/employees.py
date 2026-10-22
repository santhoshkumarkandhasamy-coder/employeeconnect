from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from ..core.text import clean_text
from .common import IdName
from .profile import PersonalFields, normalise_phone


def _full_name(v: str) -> str:
    v = clean_text(v)
    if not v:
        raise ValueError("must not be empty")
    return v


class _ReauthMixin(BaseModel):
    """`current_password` is the ACTING admin's own password (step-up). It is only required when the request touches an
    admin-capable role (role.assign / role.manage); the server decides, so it is optional in the schema. A SecretStr
    never shows up in reprs, and it is never logged or audited."""

    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=128)


class EmployeeCreate(_ReauthMixin):
    model_config = ConfigDict(extra="forbid")
    employee_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")
    full_name: str = Field(min_length=1, max_length=150)
    official_email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    department_id: uuid.UUID | None = None
    shift_id: uuid.UUID | None = None
    role_id: uuid.UUID
    manager_id: uuid.UUID | None = None
    join_date: dt.date | None = None

    _fn = field_validator("full_name")(_full_name)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return normalise_phone(v) if v is not None else None


class EmployeeUpdate(PersonalFields, _ReauthMixin):
    """PUT = full replace of the org fields (name, email, department, shift, role, manager, join_date).
    The personal fields inherited from PersonalFields (phone, dob, wedding_anniversary, personal_email, emergency_contact,
    blood_group, address) are optional: only the keys that were sent change, an explicit null clears.
    Unknown keys (also `show_celebrations`, which is the employee's own preference) are rejected with 422."""

    full_name: str = Field(min_length=1, max_length=150)
    official_email: EmailStr | None = None
    department_id: uuid.UUID | None = None
    shift_id: uuid.UUID | None = None
    role_id: uuid.UUID
    manager_id: uuid.UUID | None = None
    join_date: dt.date | None = None

    _fn = field_validator("full_name")(_full_name)


class ResetPasswordIn(_ReauthMixin):
    model_config = ConfigDict(extra="forbid")


class DeactivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Literal["resigned", "terminated", "contract_end", "absconded", "other"] = "other"


class ManagerSummary(BaseModel):
    id: str
    employee_id: str
    full_name: str


class EmployeeOut(BaseModel):
    """Never contains password_hash or other secrets."""

    id: str
    employee_id: str
    full_name: str
    official_email: str | None
    department: IdName | None
    shift: IdName | None
    role: IdName
    manager: ManagerSummary | None
    status: str
    # Managers (team scope only) get a reduced shape without these three keys (review L5)
    is_locked: bool | None = None
    locked_until: str | None = None
    join_date: str | None
    last_login_at: str | None = None
    deactivated_at: str | None = None
    deactivation_reason: str | None = None


class EmployeeDetail(EmployeeOut):
    """GET/PUT detail. `profile` (personal data) is only filled for holders of `employee.view_sensitive`, else null."""

    profile: dict | None = None


class EmployeeCreated(BaseModel):
    employee: EmployeeOut
    temporary_password: str


class TemporaryPassword(BaseModel):
    temporary_password: str
