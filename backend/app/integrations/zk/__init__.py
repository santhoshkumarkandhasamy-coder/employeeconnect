"""Minimal READ-ONLY client for eSSL / ZKTeco attendance machines (plan 18 section 6). Stdlib only.

Only the commands in `protocol.ALLOWED_COMMANDS` can ever leave the socket; templates (fingerprint / face) are never requested.
"""

from .client import ZKClient
from .errors import (
    DeviceAuthFailed,
    DeviceBusy,
    DeviceError,
    DeviceIdentityChanged,
    DeviceProtocolError,
    DeviceTimeout,
    DeviceUnreachable,
    ForbiddenCommand,
)
from .records import ZKPunch, ZKSizes, ZKUser

__all__ = [
    "DeviceAuthFailed",
    "DeviceBusy",
    "DeviceError",
    "DeviceIdentityChanged",
    "DeviceProtocolError",
    "DeviceTimeout",
    "DeviceUnreachable",
    "ForbiddenCommand",
    "ZKClient",
    "ZKPunch",
    "ZKSizes",
    "ZKUser",
]
