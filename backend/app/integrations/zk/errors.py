"""Errors of the ZK client. `code` is the job-level error code stored in `devices.last_error_code` / `device_syncs.error_code`."""

from __future__ import annotations


class DeviceError(Exception):
    code = "DEVICE_PROTOCOL_ERROR"
    retryable = True

    def __init__(self, message: str = ""):
        super().__init__(message or self.code)


class DeviceUnreachable(DeviceError):
    code = "DEVICE_UNREACHABLE"


class DeviceTimeout(DeviceError):
    code = "DEVICE_TIMEOUT"


class DeviceAuthFailed(DeviceError):
    code = "DEVICE_AUTH_FAILED"
    retryable = False


class DeviceBusy(DeviceError):
    """Another client (for example eTimeTrackLite) holds the device's single connection."""

    code = "DEVICE_BUSY"
    retryable = False  # retried by the next tick, not within the run


class DeviceProtocolError(DeviceError):
    code = "DEVICE_PROTOCOL_ERROR"


class DeviceIdentityChanged(DeviceError):
    """A different machine (serial number) answers on the registered address. No data is read."""

    code = "DEVICE_IDENTITY_CHANGED"
    retryable = False


class ForbiddenCommand(Exception):
    """A command outside the read-only allow-list was about to be written. Raised BEFORE any byte is sent; a programming error,
    never a device problem."""
