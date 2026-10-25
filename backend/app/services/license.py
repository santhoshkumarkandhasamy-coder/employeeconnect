"""Per-user licensing (plan 11 s.5). A license is a vendor-signed token `ECL1.<b64url payload>.<b64url Ed25519 signature>`.

* The server always re-verifies the stored token (signature, deployment id); the parsed `license` columns are only a
  display copy and are never trusted. The seat limit cannot be changed through settings, env or the API except by
  installing a validly signed token.
* Seats used = active, non-deleted users (admins included). Seat-consuming paths take the lock FIRST
  (`lock_for_seats`: license row, or the company row when none exists) and only then touch any users rows, so they can never
  deadlock with the LAST_ADMIN row locks.
* No license installed (or an invalid stored token) = UNLICENSED_MAX_USERS seats, never expires.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import license_keys
from ..core.audit import audit
from ..core.clock import as_utc, iso_z, utcnow
from ..core.errors import AppError
from ..models import Company, License, Permission, Role, User
from . import notifications

log = logging.getLogger("app.license")

UNLICENSED_MAX_USERS = 50  # code constant (decision): not an env value, not a DB setting
MAX_TOKEN_LEN = 4096
PREFIX = "ECL1"

# Writes that stay possible when an EXPIRED license has on_expiry=read_only (offboarding and licence renewal).
_READ_ONLY_EXEMPT = (
    ("DELETE", re.compile(r"^/api/admin/employees/[^/]+$")),
    ("POST", re.compile(r"^/api/admin/employees/[^/]+/(deactivate|revoke-sessions)$")),
    ("DELETE", re.compile(r"^/api/admin/sessions/[^/]+$")),
    ("PUT", re.compile(r"^/api/admin/license$")),
    ("POST", re.compile(r"^/api/admin/payroll/runs/[^/]+/register$")),  # an export, i.e. a read (contract 13.0)
    ("*", re.compile(r"^/api/auth/")),  # login/refresh/logout/change-password keep working
    ("*", re.compile(r"^/api/me/device-tokens")),
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class LicenseError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Verified:
    license_id: str
    licensed_to: str
    deployment_id: str
    max_users: int
    issued_at: dt.datetime
    expires_at: dt.datetime | None
    grace_days: int
    on_expiry: str


@dataclass(frozen=True)
class State:
    status: str  # valid | grace | expired | unlicensed | invalid
    max_users: int
    on_expiry: str
    verified: Verified | None
    grace_until: dt.datetime | None

    @property
    def expires_at(self) -> dt.datetime | None:
        return self.verified.expires_at if self.verified else None


# ---------------------------------------------------------------- token verification
def _b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _ts(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise LicenseError(f"{field} must be a timestamp")
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LicenseError(f"{field} is not a valid timestamp") from None
    return as_utc(d if d.tzinfo else d.replace(tzinfo=dt.UTC))  # type: ignore[return-value]


def _int(value: Any, field: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise LicenseError(f"{field} must be a whole number between {lo} and {hi}")
    return value


_verify_cache: dict[tuple[str, str], Verified] = {}


def verify_token(token: str, deployment_id: str) -> Verified:
    """Signature first; the payload is parsed only after it verified. Raises LicenseError."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LEN:
        raise LicenseError("license key is malformed")
    token = token.strip()
    key = (hashlib.sha256(token.encode()).hexdigest(), deployment_id)
    hit = _verify_cache.get(key)
    if hit is not None:
        return hit
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise LicenseError("license key is malformed")
    try:
        payload_raw, sig = _b64d(parts[1]), _b64d(parts[2])
    except (binascii.Error, ValueError):
        raise LicenseError("license key is malformed") from None
    signed = f"{PREFIX}.{parts[1]}".encode()
    ok = False
    for raw_key in license_keys.VENDOR_PUBLIC_KEYS:
        try:
            Ed25519PublicKey.from_public_bytes(_b64d(raw_key)).verify(sig, signed)
            ok = True
            break
        except (InvalidSignature, ValueError, binascii.Error):
            continue
    if not ok:
        raise LicenseError("license signature is not valid")
    try:
        data = json.loads(payload_raw)
    except ValueError:
        raise LicenseError("license payload is not valid JSON") from None
    if not isinstance(data, dict):
        raise LicenseError("license payload is malformed")
    lic_id, to = data.get("license_id"), data.get("licensed_to")
    if not isinstance(lic_id, str) or not 1 <= len(lic_id) <= 64 or not isinstance(to, str) or not 1 <= len(to) <= 200:
        raise LicenseError("license_id / licensed_to are missing or too long")
    dep = data.get("deployment_id")
    if not isinstance(dep, str):
        raise LicenseError("deployment_id is missing")
    try:
        dep_norm = str(uuid.UUID(dep))
    except ValueError:
        raise LicenseError("deployment_id is not valid") from None
    if dep_norm != str(uuid.UUID(deployment_id)):
        raise LicenseError("this license was issued for a different installation")
    on_expiry = data.get("on_expiry", "block_new_users")
    if on_expiry not in ("block_new_users", "read_only"):
        raise LicenseError("on_expiry is not valid")
    issued = _ts(data.get("issued_at"), "issued_at")
    expires = _ts(data["expires_at"], "expires_at") if data.get("expires_at") is not None else None
    if expires is not None and expires <= issued:
        raise LicenseError("expires_at must be after issued_at")
    v = Verified(
        license_id=lic_id,
        licensed_to=to,
        deployment_id=dep_norm,
        max_users=_int(data.get("max_users"), "max_users", 1, 100000),
        issued_at=issued,
        expires_at=expires,
        grace_days=_int(data.get("grace_days", 14), "grace_days", 0, 90),
        on_expiry=on_expiry,
    )
    if len(_verify_cache) > 64:
        _verify_cache.clear()
    _verify_cache[key] = v
    return v


# ---------------------------------------------------------------- state
async def get_company(session: AsyncSession, *, lock: bool = False) -> Company:
    """The singleton row (created on the fly for hand-built databases; migration 0017 normally inserts it)."""
    q = select(Company).where(Company.id == 1)
    if lock:
        q = q.with_for_update()
    row = (await session.execute(q)).scalar_one_or_none()
    if row is None:
        try:
            async with session.begin_nested():
                session.add(Company(id=1))
                await session.flush()
        except IntegrityError:
            pass  # another request inserted it first
        row = (await session.execute(q.execution_options(populate_existing=True))).scalar_one()
    return row


def compute_state(row: License | None, deployment_id: str, now: dt.datetime | None = None) -> State:
    now = now or utcnow()
    if row is None:
        return State("unlicensed", UNLICENSED_MAX_USERS, "block_new_users", None, None)
    try:
        v = verify_token(row.token, deployment_id)
    except LicenseError as exc:
        log.error("stored license is invalid (%s); treating the installation as unlicensed", exc.message)
        return State("invalid", UNLICENSED_MAX_USERS, "block_new_users", None, None)
    if v.expires_at is None:
        return State("valid", v.max_users, v.on_expiry, v, None)
    grace_until = v.expires_at + dt.timedelta(days=v.grace_days)
    if now < v.expires_at:
        status = "valid"
    elif now < grace_until:
        status = "grace"
    else:
        status = "expired"
    return State(status, v.max_users, v.on_expiry, v, grace_until)


async def load_state(session: AsyncSession) -> State:
    row = (await session.execute(select(License).where(License.id == 1))).scalar_one_or_none()
    if row is None:
        return compute_state(None, "")
    company = await get_company(session)
    return compute_state(row, str(company.deployment_id))


async def active_users(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count()).select_from(User).where(User.status == "active", User.deleted_at.is_(None)))
    ).scalar_one()


async def lock_for_seats(session: AsyncSession) -> None:
    """MUST be called before any users row is locked in the same transaction (deadlock rule)."""
    row = (await session.execute(select(License).where(License.id == 1).with_for_update())).scalar_one_or_none()
    if row is None:
        await get_company(session, lock=True)


async def check_seat(session: AsyncSession, actor: Any = None) -> None:
    """Call after `lock_for_seats`. Refuses (and audits) when no seat is free or the license is expired."""
    state = await load_state(session)
    used = await active_users(session)
    err: AppError | None = None
    if state.status == "expired":
        err = AppError("LICENSE_EXPIRED", "The license has expired. Install a new license to add or reactivate users.")
    elif used >= state.max_users:
        err = AppError(
            "USER_LIMIT_REACHED",
            "The user limit of the license has been reached.",
            details=[
                {"field": "max_users", "message": str(state.max_users)},
                {"field": "active_users", "message": str(used)},
            ],
        )
    if err is None:
        return
    audit(
        session,
        action="license.seat_refused",
        actor=actor,
        actor_employee_id=None if actor is not None else "cli",
        entity_type="license",
        outcome="failure",
        after={"code": err.code, "max_users": state.max_users, "active_users": used, "status": state.status},
    )
    await session.commit()
    raise err


async def enforce_not_read_only(session: AsyncSession, method: str, path: str) -> None:
    """Expired license with signed on_expiry=read_only: every write except offboarding/renewal/auth answers 403."""
    if method.upper() in _SAFE_METHODS:
        return
    for m, rx in _READ_ONLY_EXEMPT:
        if (m == "*" or m == method.upper()) and rx.match(path):
            return
    row = (await session.execute(select(License).where(License.id == 1))).scalar_one_or_none()
    if row is None:
        return
    state = compute_state(row, str((await get_company(session)).deployment_id))
    if state.status == "expired" and state.on_expiry == "read_only":
        raise AppError("LICENSE_EXPIRED", "The license has expired; the system is read-only until a new license is installed.")


# ---------------------------------------------------------------- output / install
async def license_out(session: AsyncSession) -> dict[str, Any]:
    company = await get_company(session)
    row = (await session.execute(select(License).where(License.id == 1))).scalar_one_or_none()
    state = compute_state(row, str(company.deployment_id))
    used = await active_users(session)
    v = state.verified
    return {
        "status": state.status,
        "license_id": v.license_id if v else None,
        "licensed_to": v.licensed_to if v else None,
        "max_users": state.max_users,
        "active_users": used,
        "seats_available": max(0, state.max_users - used),
        "issued_at": iso_z(v.issued_at) if v else None,
        "expires_at": iso_z(v.expires_at) if v and v.expires_at else None,
        "grace_until": iso_z(state.grace_until) if state.grace_until else None,
        "on_expiry": state.on_expiry,
        "deployment_id": str(company.deployment_id),
    }


def _install_audit(session: AsyncSession, actor: Any, outcome: str, after: dict, entity_id: str | None = None) -> None:
    audit(
        session,
        action="license.install",
        actor=actor,
        actor_employee_id=None if actor else "cli",
        entity_type="license",
        entity_id=entity_id,
        outcome=outcome,
        after=after,
    )


async def install_token(session: AsyncSession, token: str, *, actor: Any = None) -> dict[str, Any]:
    """Verify and store a license (PUT /admin/license and the CLI; actor None = CLI). Commits."""
    await lock_for_seats(session)
    company = await get_company(session)
    try:
        new = verify_token(token, str(company.deployment_id))
    except LicenseError as exc:
        _install_audit(session, actor, "failure", {"reason": exc.message})
        await session.commit()
        raise AppError("LICENSE_INVALID", f"The license key was refused: {exc.message}.") from None
    row = (await session.execute(select(License).where(License.id == 1))).scalar_one_or_none()
    if row is not None:
        try:
            old: Verified | None = verify_token(row.token, str(company.deployment_id))
        except LicenseError:
            old = None  # a broken stored license may always be replaced
        if old is not None and new.issued_at < old.issued_at:
            _install_audit(session, actor, "failure", {"reason": "outdated", "license_id": new.license_id})
            await session.commit()
            raise AppError("LICENSE_OUTDATED", "This license is older than the one already installed.")
    else:
        row = License(id=1)
        session.add(row)
    row.token = token.strip()
    row.license_id, row.licensed_to, row.max_users = new.license_id, new.licensed_to, new.max_users
    row.issued_at, row.expires_at, row.grace_days, row.on_expiry = new.issued_at, new.expires_at, new.grace_days, new.on_expiry
    row.installed_at = utcnow()
    row.installed_by = getattr(actor, "id", None)
    _install_audit(
        session,
        actor,
        "success",
        {
            "license_id": new.license_id,
            "max_users": new.max_users,
            "expires_at": iso_z(new.expires_at) if new.expires_at else None,
            "on_expiry": new.on_expiry,
        },
        entity_id=new.license_id,
    )
    await session.commit()
    return await license_out(session)


# ---------------------------------------------------------------- daily alert (worker)
async def holders_of(session: AsyncSession, code: str) -> list[uuid.UUID]:
    q = (
        select(User.id)
        .join(Role, Role.id == User.role_id)
        .where(User.status == "active", User.deleted_at.is_(None), Role.permissions.any(Permission.code == code))
    )
    return list((await session.execute(q)).unique().scalars())


async def alert_expiry(session: AsyncSession) -> int:
    """Daily: grace / expired licenses push an admin_alert to the license.manage holders. Returns device targets."""
    state = await load_state(session)
    if state.status not in ("grace", "expired") or state.verified is None:
        return 0
    if state.status == "grace":
        title = "License expiring"
        body = f"The license expired on {state.expires_at:%Y-%m-%d}; it keeps working until {state.grace_until:%Y-%m-%d}."
    else:
        title = "License expired"
        body = "The license has expired. Install a new license to add users."
    ids = await holders_of(session, "license.manage")
    return await notifications.notify(
        session, ids, type="admin_alert", entity_id=state.verified.license_id, title=title, body=body, high_priority=True
    )
