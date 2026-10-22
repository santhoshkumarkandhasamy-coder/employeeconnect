from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import IdName

# Passwords are never trimmed. Upper bound guards the hasher against DoS.
_PW = Field(min_length=1, max_length=128)


class LoginRequest(BaseModel):
    employee_id: str = Field(min_length=1, max_length=64)
    password: str = _PW
    device_id: str | None = Field(default=None, max_length=128)
    device_name: str | None = Field(default=None, max_length=128)
    app_version: str | None = Field(default=None, max_length=32)
    platform: Literal["android", "ios"] | None = None


class WebLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: str = Field(min_length=1, max_length=64)
    password: str = _PW


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = _PW
    new_password: str = _PW


class ForgotPasswordRequest(BaseModel):
    employee_id: str = Field(min_length=1, max_length=64)


class AuthUser(BaseModel):
    id: str
    employee_id: str
    full_name: str
    role: str
    permissions: list[str]


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    must_change_password: bool


class LoginResponse(TokenResponse):
    user: AuthUser


class MeResponse(AuthUser):
    must_change_password: bool
    department: IdName | None = None
    company: dict[str, Any] | None = None


class DeviceTokenRequest(BaseModel):
    fcm_token: str = Field(min_length=10, max_length=4096)
    platform: str = Field(pattern="^(android|ios)$")
    device_id: str | None = Field(default=None, max_length=128)
