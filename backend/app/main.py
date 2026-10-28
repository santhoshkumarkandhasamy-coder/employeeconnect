"""FastAPI app factory. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from .core.config import get_settings
from .core.context import RequestContext, set_request_context
from .core.crypto import validate_keys
from .core.db import dispose_engine
from .core.errors import install_error_handlers
from .core.net import resolve_client_ip
from .core.redis import close_redis
from .routers import (
    admin_console,
    admin_employees,
    admin_export,
    admin_masters,
    admin_roles,
    admin_settings,
    announcements,
    attendance,
    auth,
    auth_web,
    company,
    files,
    food,
    home,
    leave,
    payroll,
    profile,
    tasks,
)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        s = get_settings()
        ip = resolve_client_ip(
            request.client.host if request.client else None,
            request.headers.get("x-real-ip"),
            request.headers.get("x-forwarded-for"),
            s.trusted_proxy_networks,
        )
        rid = request.headers.get("x-request-id", "")
        if not _REQUEST_ID.match(rid):  # never store attacker-chosen free text in logs/audit
            rid = uuid.uuid4().hex
        set_request_context(RequestContext(request_id=rid, ip=ip, user_agent=request.headers.get("user-agent")))
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        if "cache-control" not in response.headers:  # routes that are meant to be cached (company logo) set their own
            response.headers["Cache-Control"] = "no-store"
        return response


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    logging.basicConfig(level=s.log_level)
    validate_keys()  # fail fast on missing/invalid DATA_ENCRYPTION_KEYS
    app = FastAPI(
        title="Employee Connect API",
        version="1.0",
        lifespan=lifespan,
        docs_url=None if s.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if s.is_prod else "/openapi.json",
    )
    if s.cors_origin_list:
        app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():  # unauthenticated liveness probe (no data)
        return {"status": "ok"}

    app.include_router(auth.router, prefix="/api")
    app.include_router(auth.public_router, prefix="/api")
    app.include_router(auth_web.router, prefix="/api")
    app.include_router(company.public_router, prefix="/api")
    app.include_router(company.router, prefix="/api")
    app.include_router(admin_employees.router, prefix="/api")
    app.include_router(admin_console.router, prefix="/api")
    app.include_router(attendance.router, prefix="/api")
    app.include_router(files.router, prefix="/api")
    app.include_router(leave.router, prefix="/api")
    app.include_router(food.router, prefix="/api")
    app.include_router(announcements.router, prefix="/api")
    app.include_router(profile.router, prefix="/api")
    app.include_router(home.router, prefix="/api")
    app.include_router(admin_settings.router, prefix="/api")
    app.include_router(admin_export.router, prefix="/api")
    app.include_router(admin_masters.router, prefix="/api")
    app.include_router(admin_roles.router, prefix="/api")
    app.include_router(payroll.employee_router, prefix="/api")
    app.include_router(payroll.admin_router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    return app
