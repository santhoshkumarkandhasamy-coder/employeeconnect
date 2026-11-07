"""Request bodies of contract section 21 (attendance devices). Extra keys are refused (422)."""

from __future__ import annotations

import datetime as dt
import ipaddress
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.text import clean_text


def _ipv4(v: str) -> str:
    try:
        return str(ipaddress.IPv4Address(v.strip()))
    except ValueError:
        raise ValueError("must be an IPv4 address like 192.168.0.201") from None


class DeviceIn(BaseModel):
    """POST and PUT /admin/devices. On PUT `comm_key` omitted = unchanged, null = clear, number = set."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    ip: str = Field(max_length=15)
    port: int = Field(4370, ge=1, le=65535)
    transport: Literal["tcp", "udp"] = "tcp"
    comm_key: int | None = Field(None, ge=0, le=999999)
    enabled: bool = True
    location_label: str | None = Field(None, max_length=80)
    timezone: str | None = Field(None, max_length=64)
    clock_correction: Literal["auto", "off"] = "auto"
    ingest_from: dt.date | None = None

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        return _ipv4(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = clean_text(v)
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("location_label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return (clean_text(v) or None) if v is not None else None


class DeviceTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ip: str = Field(max_length=15)
    port: int = Field(4370, ge=1, le=65535)
    transport: Literal["tcp", "udp"] = "tcp"
    comm_key: int | None = Field(None, ge=0, le=999999)

    @field_validator("ip")
    @classmethod
    def _ip(cls, v: str) -> str:
        return _ipv4(v)


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: uuid.UUID | None
    apply_to_all_devices: bool = False  # review L9: one slot unless the caller asks for every device
    reason: str = Field(min_length=3, max_length=300)  # review M3: audited with the previous employee

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = clean_text(v)
        if len(v) < 3:
            raise ValueError("give a reason (at least 3 characters)")
        return v


class UseDeviceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = clean_text(v)
        if len(v) < 3:
            raise ValueError("give a reason (at least 3 characters)")
        return v
