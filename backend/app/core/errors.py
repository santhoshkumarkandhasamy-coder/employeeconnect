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
    "OUTSIDE_CLIENT_LOCATION": 422,
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
    "PAYROLL_RUN_EXISTS": 409,
    "PAYROLL_LOCKED": 409,
    "PAYROLL_BLOCKED": 409,
    "NOTHING_TO_PUBLISH": 409,
    "STRUCTURE_EXISTS": 409,
    "STRUCTURE_IN_USE": 409,
    "VERSION_CONFLICT": 409,
    "INVALID_PERIOD": 422,
    "SECOND_PERSON_REQUIRED": 403,
    # plan 15 / contract 17.10
    "PERIOD_LOCKED": 409,
    "ITEM_LOCKED": 409,
    "TOO_MANY_ITEMS": 409,
    "COMPONENTS_CHANGED": 409,
    "IMPORT_DUPLICATE": 409,
    "IMPORT_FILE_CHANGED": 409,
    "IMPORT_NOT_CHECKED": 409,
    "LOAN_NOT_ACTIVE": 409,
    "LOAN_HAS_RECOVERIES": 409,
    "LOAN_LIMIT_REACHED": 409,
    "ARREARS_STALE": 409,
    "NOT_PROPOSED": 409,
    "DUPLICATE_BILL": 409,
    "CLAIM_LIMIT_REACHED": 409,
    "OVERTIME_DISABLED": 409,
    "IMPORT_INVALID": 422,
    "NOT_ENCASHABLE": 422,
    "INVALID_CATEGORY": 422,
    "AMOUNT_ABOVE_LIMIT": 422,
    "BILL_REQUIRED": 422,
    "STRUCTURE_MISSING": 422,
    # plan 16 / contract 18.12 (work package 4a)
    "APPROVAL_REQUIRED": 409,
    "INCREMENT_EXISTS": 409,
    "INCREMENT_STALE": 409,
    "LATER_STRUCTURE_EXISTS": 409,
    "NOT_SUBMITTED": 409,
    "NOT_DRAFT": 409,
    "INVALID_PERCENT": 422,
    "AFTER_EXIT": 422,
    "NO_CHANGE": 422,
    "TOO_MANY_ROWS": 422,
    # plan 16 / contract 18.12 (work package 4b)
    "FNF_EXISTS": 409,
    "FNF_OPEN": 409,
    "FNF_NOT_REVOCABLE": 409,
    "FNF_NEGATIVE": 409,
    "FNF_MANAGED": 409,
    "FNF_NOT_INCLUDED": 409,
    "FNF_STALE_QUOTE": 409,
    "LOAN_UNRESOLVED": 422,
    # plan 17 / contract 19.13 (work package 5a)
    "PUBLISH_REQUEST_REQUIRED": 409,
    "PUBLISH_REQUEST_EXISTS": 409,
    "PUBLISH_STALE": 409,
    "MAKER_CHECKER_OFF": 409,
    "BATCH_STALE": 409,
    "NOT_APPROVED": 409,
    "BATCH_CLOSED": 409,
    "NOTHING_TO_PAY": 422,
    # plan 17 / contract 19.13 (work package 5b)
    "DRAFT_EXISTS": 409,
    "RULES_CHANGED": 409,
    "TAX_RULES_NOT_VERIFIED": 409,
    "WINDOW_CLOSED": 409,
    "REGIME_LOCKED": 409,
    "TDS_ENGINE_OFF": 409,
    "INVALID_SECTION": 422,
    "LANDLORD_PAN_REQUIRED": 422,
    "ITEM_NOT_SUBMITTED": 422,
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
