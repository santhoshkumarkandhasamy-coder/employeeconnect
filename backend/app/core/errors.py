"""Single error type and handlers producing the contract error format:
{"error": {"code", "message", "details": [{"field","message"}], "request_id"}}."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .context import get_request_context
from .crypto import DataDecryptionError

log = logging.getLogger("app.errors")

STATUS_CODES = {
    "VALIDATION_ERROR": 422,
    "BAD_REQUEST": 400,
    "UNAUTHORIZED": 401,
    "INVALID_CREDENTIALS": 401,
    "TOKEN_EXPIRED": 401,
    "TOKEN_REUSED": 401,
    "SESSION_REVOKED": 401,
    "CURRENT_PASSWORD_INVALID": 400,
    "PASSWORD_REUSED": 422,
    "LAST_ADMIN": 409,
    "IN_USE": 409,
    "FORBIDDEN": 403,
    "PASSWORD_CHANGE_REQUIRED": 403,
    "ACCOUNT_INACTIVE": 403,
    "TEMP_PASSWORD_EXPIRED": 403,
    "REAUTH_FAILED": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "ACCOUNT_LOCKED": 423,
    "RATE_LIMITED": 429,
    "ALREADY_CHECKED_IN": 409,
    "ALREADY_CHECKED_OUT": 409,
    "NOT_CHECKED_IN": 409,
    "ON_LEAVE": 409,
    "OUTSIDE_GEOFENCE": 422,
    "SELFIE_REQUIRED": 422,
    "LOCATION_ACCURACY_LOW": 422,
    "NO_SHIFT_ASSIGNED": 422,
    "FILE_TOO_LARGE": 422,
    "FILE_TYPE_NOT_ALLOWED": 422,
    "INSUFFICIENT_BALANCE": 409,
    "OVERLAPPING_REQUEST": 409,
    "PERMISSION_LIMIT_EXCEEDED": 409,
    "NOT_PENDING": 409,
    "REQUESTER_INACTIVE": 409,
    "QUOTA_BELOW_USED": 409,
    "INVALID_DATE_RANGE": 422,
    "INVALID_TIME_RANGE": 422,
    "ZERO_WORKING_DAYS": 422,
    "ATTACHMENT_REQUIRED": 422,
    "HALF_DAY_NOT_ALLOWED": 422,
    "LEAVE_TYPE_INACTIVE": 422,
    "LOCATION_FIX_STALE": 422,
    "MOCK_LOCATION_DETECTED": 422,
    "SELFIE_STALE": 422,
    "SELFIE_REUSED": 422,
    "FOOD_CUTOFF_PASSED": 409,
    "INVALID_DATE": 422,
    "RANGE_TOO_LARGE": 422,
    "EXPORT_QUOTA_EXCEEDED": 429,
    "USER_LIMIT_REACHED": 409,
    "LICENSE_EXPIRED": 403,
    "LICENSE_INVALID": 422,
    "LICENSE_OUTDATED": 409,
    "DATA_UNREADABLE": 503,
    "FILE_UNAVAILABLE": 503,
    "TEMPORARILY_UNAVAILABLE": 503,
    "INTERNAL_ERROR": 500,
}


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or STATUS_CODES.get(code, 400)
        self.details = details or []
        self.headers = headers or {}


def error_response(
    status: int, code: str, message: str, details: list | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or [], "request_id": get_request_context().request_id}}
    return JSONResponse(status_code=status, content=body, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return error_response(exc.status, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        details = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
            details.append({"field": ".".join(loc), "message": err.get("msg", "invalid")})
        return error_response(422, "VALIDATION_ERROR", "Request validation failed", details)

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException):
        code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND"}.get(exc.status_code, "BAD_REQUEST")
        return error_response(exc.status_code, code, str(exc.detail), headers=getattr(exc, "headers", None))

    @app.exception_handler(DataDecryptionError)
    async def _undecryptable(_: Request, exc: DataDecryptionError):
        log.error("data decryption failed (check DATA_ENCRYPTION_KEYS / run scripts.rotate_keys --verify-only)")
        return error_response(503, "DATA_UNREADABLE", "Data is temporarily unavailable")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("unhandled error")  # stack trace stays in server logs only
        return error_response(500, "INTERNAL_ERROR", "Internal server error")
