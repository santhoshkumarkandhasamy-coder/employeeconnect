"""Audit-log service. Rows are written in the caller's transaction (so they roll back with the change);
for failures that raise afterwards, the caller commits before raising."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog
from .context import get_request_context

_SENSITIVE = (
    "password", "token", "hash", "secret", "otp",
    "phone", "personal_email", "address", "dob", "birth", "anniversary", "emergency", "blood", "location",
)  # fmt: skip
_EID = re.compile(r"^[A-Z0-9._-]{1,32}$")


def redact(data: Any) -> Any:
    """Recursively mask sensitive keys (nested dicts / lists included)."""
    if isinstance(data, dict):
        return {k: ("***" if any(s in str(k).lower() for s in _SENSITIVE) else redact(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [redact(v) for v in data]
    return data


def safe_eid(value: str) -> str:
    """Typed-in ids for unknown users: keep only well-formed ids; anything else (often a password typed into the
    wrong field) is replaced by a short hash so it is never stored in clear."""
    v = value.strip().upper()
    return v if _EID.match(v) else "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]


def _ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def audit(
    session: AsyncSession,
    *,
    action: str,
    actor: Any = None,
    actor_employee_id: str | None = None,
    entity_type: str | None = None,
    entity_id: Any = None,
    before: dict | None = None,
    after: dict | None = None,
    outcome: str = "success",
) -> None:
    """`actor` is anything with `.id` and `.employee_id` (User / Principal) or None."""
    ctx = get_request_context()
    session.add(
        AuditLog(
            actor_id=getattr(actor, "id", None),
            actor_employee_id=actor_employee_id or getattr(actor, "employee_id", None),
            action=action,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            before=redact(before),
            after=redact(after),
            ip=_ip(ctx.ip),
            user_agent=(ctx.user_agent or "")[:300] or None,
            request_id=ctx.request_id,
            outcome=outcome,
        )
    )
