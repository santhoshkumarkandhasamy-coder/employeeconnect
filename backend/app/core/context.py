"""Request-scoped context (ip, user agent, request id) for audit logging."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass
class RequestContext:
    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None


_ctx: contextvars.ContextVar[RequestContext] = contextvars.ContextVar("request_ctx", default=None)  # type: ignore[arg-type]


def set_request_context(ctx: RequestContext) -> None:
    _ctx.set(ctx)


def get_request_context() -> RequestContext:
    return _ctx.get() or RequestContext()
