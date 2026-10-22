"""Authentication service: login, refresh rotation with reuse detection, logout, password change,
forgot-password, device tokens, client config. Routers stay thin; all rules live here."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import net, rate_limit, security
from ..core.audit import audit, safe_eid
from ..core.clock import as_utc, iso_z, utcnow
from ..core.config import get_settings
from ..core.context import get_request_context
from ..core.errors import AppError
from ..core.rbac import Principal, revoke_session_access
from ..core.text import clean_text
from ..models import DeviceToken, PasswordResetRequest, RefreshToken, Setting, User
from . import admin_security
from . import company as company_svc

GENERIC_CREDENTIALS = "Invalid employee ID or password"


def canonical_employee_id(value: str) -> str:
    return value.strip().upper()


def _locked_error(locked_until: dt.datetime) -> AppError:
    iso = iso_z(locked_until)
    return AppError(
        "ACCOUNT_LOCKED",
        "Account is locked. Try again later or contact an administrator.",
        details=[{"field": "locked_until", "message": iso}],
    )


def auth_user_payload(user: User) -> dict[str, Any]:
    return {
        "id": str(user.id),
        "employee_id": user.employee_id,
        "full_name": user.full_name,
        "role": user.role.name,
        "permissions": sorted(p.code for p in user.role.permissions),
    }


# ------------------------------------------------------------------ token issuing
def bump_token_epoch(user: User) -> None:
    """Invalidate every access token issued so far (iat < token_valid_after). Whole-second value in the NEXT second,
    because JWT iat has 1 s resolution; tokens minted afterwards get iat >= this (see create_access_token)."""
    user.token_valid_after = utcnow().replace(microsecond=0) + dt.timedelta(seconds=1)


async def issue_tokens(
    session: AsyncSession,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    family_started_at: dt.datetime | None = None,
    device_id: str | None = None,
    device_name: str | None = None,
    client_type: str = "mobile",
    platform: str | None = None,
    app_version: str | None = None,
) -> tuple[dict[str, Any], RefreshToken]:
    s = get_settings()
    ctx = get_request_context()
    now = utcnow()
    started = as_utc(family_started_at) or now
    raw = security.new_refresh_token()
    expires_at = min(now + dt.timedelta(days=s.refresh_token_days), started + dt.timedelta(days=s.session_max_days))
    if client_type == "web":  # console sessions have their own, much shorter absolute lifetime
        max_hours, _ = await console_limits(session)
        expires_at = min(expires_at, started + dt.timedelta(hours=max_hours))
    row = RefreshToken(
        id=uuid.uuid4(),
        user_id=user.id,
        family_id=family_id or uuid.uuid4(),
        token_hash=security.hash_refresh_token(raw),
        device_id=device_id,
        device_name=device_name,
        client_type=client_type,
        platform=platform if client_type == "mobile" else "web",
        app_version=app_version,
        ip=ctx.ip if _valid_ip(ctx.ip) else None,
        user_agent=(ctx.user_agent or "")[:300] or None,
        issued_at=now,
        family_started_at=started,
        expires_at=expires_at,
    )
    session.add(row)
    access, expires_in = security.create_access_token(
        user.id,
        user.role.name,
        user.must_change_password,
        as_utc(user.token_valid_after),
        sid=row.family_id.hex if client_type == "web" else None,  # lets a console logout kill just this session
    )
    await session.flush()
    return (
        {
            "access_token": access,
            "refresh_token": raw,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "must_change_password": user.must_change_password,
        },
        row,
    )


def clean_label(value: str | None, limit: int) -> str | None:
    """device_name / app_version are user supplied: control characters and markup go, length is capped, and clients must
    still render them as plain text."""
    if value is None:
        return None
    return clean_text(value)[:limit] or None


async def console_limits(session: AsyncSession) -> tuple[int, int]:
    """(console.session_max_hours, console.inactivity_timeout_minutes), clamped to their validated ranges."""
    rows = {
        r.key: r.value
        for r in (
            await session.execute(
                select(Setting).where(Setting.key.in_(["console.session_max_hours", "console.inactivity_timeout_minutes"]))
            )
        ).scalars()
    }
    return (
        _clamp_int(rows.get("console.session_max_hours"), 12, 1, 24),
        _clamp_int(rows.get("console.inactivity_timeout_minutes"), 15, 5, 60),
    )


def _valid_ip(ip: str | None) -> bool:
    from ..core.audit import _ip

    return _ip(ip) is not None


async def revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow())
    )


# ------------------------------------------------------------------ failure / lockout
def _fail_key(user_id: uuid.UUID, bucket: str) -> str:
    return f"lf:{user_id}:{bucket}"


async def _count_ip_failure(redis: Redis | None, user: User, ip: str | None) -> None:
    """Failed passwords per (account, client IP bucket); see _trusted_ip_bypass."""
    bucket = net.rate_limit_key(ip) if _valid_ip(ip) else None
    if redis is None or not bucket:
        return
    await rate_limit.gate_fail(redis, _fail_key(user.id, bucket), get_settings().lockout_minutes * 60)


async def _trusted_ip_bypass(session: AsyncSession, redis: Redis, user: User, ip: str | None) -> bool:
    """Lockout-DoS defence (review M1). An account lock stops every client EXCEPT one coming from an IP this account
    already signed in from within the last 30 days, and only while that IP has not itself burned the failure budget
    (LOCKOUT_THRESHOLD wrong passwords per account+IP within the lockout window). So an outsider cannot lock a user out
    of their own network, but brute force stays capped at LOCKOUT_THRESHOLD guesses per lockout window from any one IP
    and the lock still stops every unknown IP. Redis down = no bypass (fail closed)."""
    bucket = net.rate_limit_key(ip) if _valid_ip(ip) else None
    if not bucket:
        return False
    try:
        raw = await redis.get(_fail_key(user.id, bucket))
        if raw is not None and int(raw) >= get_settings().lockout_threshold:
            return False
    except Exception:  # noqa: BLE001
        return False
    since = utcnow() - dt.timedelta(days=30)
    known = await session.execute(
        select(RefreshToken.id)
        .where(RefreshToken.user_id == user.id, RefreshToken.ip == ip, RefreshToken.issued_at >= since)
        .limit(1)
    )
    return known.first() is not None


async def _register_failure(session: AsyncSession, user: User, reason: str) -> bool:
    """Count a failed password check and commit. Returns True when this attempt locked the account.
    The caller decides what to raise (login never reveals the lock; change-password may)."""
    s = get_settings()
    now = utcnow()
    user.failed_login_count += 1
    user.last_failed_login_at = now
    audit(
        session,
        action="auth.login_failed",
        actor=user,
        entity_type="user",
        entity_id=user.id,
        outcome="failure",
        after={"reason": reason, "failed_login_count": user.failed_login_count},
    )
    locked = False
    if user.failed_login_count >= s.lockout_threshold:
        user.locked_until = now + dt.timedelta(minutes=s.lockout_minutes)
        audit(
            session,
            action="auth.account_locked",
            actor=user,
            entity_type="user",
            entity_id=user.id,
            outcome="failure",
            after={"locked_until": iso_z(user.locked_until)},
        )
        locked = True
    privileged = locked and "console.access" in {p.code for p in user.role.permissions}
    await session.commit()
    if privileged and user.failed_login_count == s.lockout_threshold:  # once per lock, not on every extension
        await admin_security.notify_admins(
            session,
            entity_id=user.id,
            title="Privileged account locked",
            body=f"{user.full_name} ({user.employee_id}) was locked after repeated failed sign-ins.",
        )
    return locked


# ------------------------------------------------------------------ login
async def login(
    session: AsyncSession,
    redis: Redis,
    *,
    employee_id: str,
    password: str,
    device_id: str | None = None,
    device_name: str | None = None,
    app_version: str | None = None,
    platform: str | None = None,
    web: bool = False,
) -> dict[str, Any]:
    """Anti-enumeration: 423 ACCOUNT_LOCKED (with the countdown) and ACCOUNT_INACTIVE are only revealed after the
    CORRECT password. Wrong password -> identical 401 for unknown, locked, and ordinary failures, with the same
    amount of hashing work in every branch."""
    s = get_settings()
    ctx = get_request_context()
    eid = canonical_employee_id(employee_id)
    w = s.login_rate_window_seconds
    checks = [(f"rl:login:eid:{eid.lower()}", s.login_rate_eid, w)]
    ip_key = net.rate_limit_key(ctx.ip)
    if ip_key:
        checks.append((f"rl:login:ip:{ip_key}", s.login_rate_ip, w))
    await rate_limit.enforce(redis, checks)

    user = (
        (await session.execute(select(User).where(User.employee_id == eid).with_for_update(of=User)))
        .unique()
        .scalar_one_or_none()
    )
    now = utcnow()
    if user is None:
        await security.averify_dummy(password)  # timing equalisation
        audit(
            session,
            action="auth.login_failed",
            actor_employee_id=safe_eid(employee_id),
            outcome="failure",
            after={"reason": "unknown_user"},
        )
        await session.commit()
        raise AppError("INVALID_CREDENTIALS", GENERIC_CREDENTIALS)

    locked_until = as_utc(user.locked_until)
    lock_active = bool(locked_until and locked_until > now)
    if lock_active and await _trusted_ip_bypass(session, redis, user, ctx.ip):
        lock_active = False  # the owner's own network is not locked out by outsiders' guesses (review M1)
    if lock_active and locked_until:
        password_ok = await security.averify_password(user.password_hash, password)  # same cost as other branches
        audit(
            session,
            action="auth.login_failed",
            actor=user,
            entity_type="user",
            entity_id=user.id,
            outcome="failure",
            after={"reason": "locked"},
        )
        await session.commit()
        if password_ok:
            raise _locked_error(locked_until)
        raise AppError("INVALID_CREDENTIALS", GENERIC_CREDENTIALS)  # not counted, not distinguishable from unknown id
    if locked_until and locked_until <= now:  # lock expired: start fresh
        user.failed_login_count = 0
        user.locked_until = None

    if not await security.averify_password(user.password_hash, password):
        await _count_ip_failure(redis, user, ctx.ip)
        await _register_failure(session, user, "bad_password")
        raise AppError("INVALID_CREDENTIALS", GENERIC_CREDENTIALS)

    if user.status != "active" or user.deleted_at is not None:  # only revealed after a correct password
        audit(
            session,
            action="auth.login_failed",
            actor=user,
            entity_type="user",
            entity_id=user.id,
            outcome="failure",
            after={"reason": "inactive"},
        )
        await session.commit()
        raise AppError("ACCOUNT_INACTIVE", "Account is inactive. Contact an administrator.")

    expires = as_utc(user.temp_password_expires_at)
    if user.must_change_password and expires is not None and expires <= now:
        # an admin-issued temporary password that was never used in time (review M7); revealed only after the CORRECT
        # password, like ACCOUNT_INACTIVE. Not a failed attempt: it does not count towards the lockout.
        audit(
            session,
            action="auth.login_failed",
            actor=user,
            entity_type="user",
            entity_id=user.id,
            outcome="failure",
            after={"reason": "temp_password_expired"},
        )
        await session.commit()
        raise AppError("TEMP_PASSWORD_EXPIRED", "Your temporary password has expired. Ask an administrator for a new one.")

    if web and "console.access" not in {p.code for p in user.role.permissions}:  # only revealed after the CORRECT password
        audit(
            session,
            action="auth.login_failed",
            actor=user,
            entity_type="user",
            entity_id=user.id,
            outcome="failure",
            after={"reason": "no_console_access"},
        )
        await session.commit()
        raise AppError("FORBIDDEN", "You do not have access to the admin console")

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    if security.needs_rehash(user.password_hash):
        user.password_hash = await security.ahash_password(password)
    tokens, _ = await issue_tokens(
        session,
        user,
        device_id=clean_label(device_id, 128),
        device_name=clean_label(device_name, 128),
        client_type="web" if web else "mobile",
        platform=platform,
        app_version=clean_label(app_version, 32),
    )
    audit(
        session,
        action="auth.login_success",
        actor=user,
        entity_type="user",
        entity_id=user.id,
        after={"client": "web"} if web else None,
    )
    await session.commit()
    return {**tokens, "user": auth_user_payload(user)}


# ------------------------------------------------------------------ refresh (rotation + reuse detection)
async def refresh(session: AsyncSession, *, refresh_token: str, web: bool = False) -> dict[str, Any]:
    """Rotation with:
    * SESSION_REVOKED (401) for tokens revoked by logout / password change / reset (no theft alarm);
    * ACCOUNT_INACTIVE (403) for any token of a deactivated / deleted user (the app wipes and locks);
    * TOKEN_REUSED (401, family revoked, audited) only for a rotated token replayed OUTSIDE the grace window;
    * a short grace window: re-presenting the immediate predecessor (lost response) while its successor is unused
      issues a fresh child instead of killing the session;
    * an absolute family lifetime (SESSION_MAX_DAYS) that rotation cannot extend."""
    s = get_settings()
    now = utcnow()
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == security.hash_refresh_token(refresh_token)).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("UNAUTHORIZED", "Invalid refresh token")
    if row.client_type != ("web" if web else "mobile"):  # a console cookie is no mobile token and vice versa
        raise AppError("UNAUTHORIZED", "Invalid refresh token")
    user = (await session.execute(select(User).where(User.id == row.user_id))).unique().scalar_one()

    if user.status != "active" or user.deleted_at is not None:
        # offboarded (deactivated / deleted): 403 ACCOUNT_INACTIVE instead of SESSION_REVOKED so the app wipes + locks.
        # Safe to reveal: the caller proves possession of this user's refresh token.
        if row.revoked_at is None:
            await revoke_family(session, row.family_id)
            await session.commit()
        raise AppError("ACCOUNT_INACTIVE", "Account is inactive")
    if row.revoked_at is not None:
        raise AppError("SESSION_REVOKED", "Session ended. Please sign in again.")

    parent = row
    grace = False
    if row.used_at is not None:
        successor = None
        if row.replaced_by is not None:
            successor = (
                await session.execute(select(RefreshToken).where(RefreshToken.id == row.replaced_by).with_for_update())
            ).scalar_one_or_none()
        within = (now - as_utc(row.used_at)).total_seconds() <= s.refresh_grace_seconds  # type: ignore[operator]
        if within and successor is not None and successor.used_at is None and successor.revoked_at is None:
            parent, grace = successor, True
        else:
            await revoke_family(session, row.family_id)
            audit(
                session,
                action="auth.refresh_reuse",
                actor=user,
                entity_type="user",
                entity_id=user.id,
                outcome="failure",
                after={"family_id": str(row.family_id)},
            )
            await session.commit()
            raise AppError("TOKEN_REUSED", "Refresh token was already used. Please sign in again.")
    if as_utc(row.expires_at) <= now:  # type: ignore[operator]
        raise AppError("TOKEN_EXPIRED", "Refresh token expired")
    if as_utc(row.family_started_at) + dt.timedelta(days=s.session_max_days) <= now:  # type: ignore[operator]
        await revoke_family(session, row.family_id)
        await session.commit()
        raise AppError("TOKEN_EXPIRED", "Session reached its maximum lifetime. Please sign in again.")
    if web:
        if "console.access" not in {p.code for p in user.role.permissions}:
            await revoke_family(session, row.family_id)
            await session.commit()
            raise AppError("FORBIDDEN", "You do not have access to the admin console")
        _, idle_min = await console_limits(session)
        # the client refreshes when its access token expires, so the server-side idle backstop starts after that
        idle_limit = dt.timedelta(minutes=idle_min + s.access_token_minutes)
        if now - as_utc(row.issued_at) > idle_limit:  # type: ignore[operator]
            await revoke_family(session, row.family_id)
            await session.commit()
            raise AppError("TOKEN_EXPIRED", "Session timed out due to inactivity")

    tokens, new_row = await issue_tokens(
        session,
        user,
        family_id=row.family_id,
        family_started_at=row.family_started_at,
        device_id=row.device_id,
        device_name=row.device_name,
        client_type=row.client_type,
        platform=row.platform,
        app_version=row.app_version,
    )
    parent.used_at = now
    parent.replaced_by = new_row.id
    if grace:  # `row` stays as it was; the unused successor is consumed instead
        audit(
            session,
            action="auth.refresh_grace",
            actor=user,
            entity_type="user",
            entity_id=user.id,
            after={"family_id": str(row.family_id)},
        )
    await session.commit()
    return tokens


# ------------------------------------------------------------------ logout
async def logout(session: AsyncSession, principal: Principal, *, refresh_token: str) -> None:
    row = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == security.hash_refresh_token(refresh_token), RefreshToken.user_id == principal.id
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await revoke_family(session, row.family_id)
    bump_token_epoch(principal.user)  # the presented access token (and any other still-live one) dies now
    audit(session, action="auth.logout", actor=principal, entity_type="user", entity_id=principal.id)
    await session.commit()


async def logout_all(session: AsyncSession, principal: Principal) -> None:
    await revoke_all_for_user(session, principal.id)
    bump_token_epoch(principal.user)
    audit(session, action="auth.logout_all", actor=principal, entity_type="user", entity_id=principal.id)
    await session.commit()


# ------------------------------------------------------------------ change password
async def change_password(
    session: AsyncSession,
    principal: Principal,
    *,
    current_password: str,
    new_password: str,
    web: bool = False,
    redis: Redis | None = None,
) -> dict[str, Any]:
    """A wrong current password is 400 CURRENT_PASSWORD_INVALID (never 401: clients refresh+replay on 401, which
    used to double-count the failure). It counts ONCE toward lockout; the attempt that locks returns 423."""
    user = (await session.execute(select(User).where(User.id == principal.id).with_for_update(of=User))).unique().scalar_one()
    locked_until = as_utc(user.locked_until)
    if locked_until and locked_until > utcnow():
        raise _locked_error(locked_until)
    if not await security.averify_password(user.password_hash, current_password):
        await _count_ip_failure(redis, user, get_request_context().ip)
        if await _register_failure(session, user, "bad_current_password"):
            raise _locked_error(user.locked_until)  # type: ignore[arg-type]
        raise AppError("CURRENT_PASSWORD_INVALID", "Current password is incorrect")
    if await security.averify_password(user.password_hash, new_password):
        raise AppError("PASSWORD_REUSED", "New password must differ from the current one", status=422)
    problems = security.validate_password(new_password, user.employee_id, user.full_name)
    if problems:
        raise AppError(
            "VALIDATION_ERROR",
            "Password does not meet the policy",
            status=422,
            details=[{"field": "new_password", "message": p} for p in problems],
        )
    now = utcnow()
    user.password_hash = await security.ahash_password(new_password)
    user.password_changed_at = now
    user.must_change_password = False
    user.temp_password_expires_at = None
    user.failed_login_count = 0
    user.locked_until = None
    await revoke_all_for_user(session, user.id)
    bump_token_epoch(user)
    tokens, _ = await issue_tokens(session, user, client_type="web" if web else "mobile")
    audit(session, action="auth.password_changed", actor=user, entity_type="user", entity_id=user.id)
    await session.commit()
    return {**tokens, "user": auth_user_payload(user)}


# ------------------------------------------------------------------ forgot password (Admin-reset v1)
async def forgot_password(session: AsyncSession, redis: Redis, *, employee_id: str) -> User | None:
    """Always succeeds from the caller's view. Creates one open request for Admin/HR when applicable.
    The call always takes at least FORGOT_MIN_LATENCY_MS so response time does not reveal whether the id exists.
    Returns the requester when admins still have to be notified: the router sends that push AFTER the response
    (background task), so push latency cannot reveal whether the id exists (review L2)."""
    started = time.monotonic()
    try:
        return await _forgot_password(session, redis, employee_id=employee_id)
    finally:
        remaining = get_settings().forgot_min_latency_ms / 1000 - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)


async def _forgot_password(session: AsyncSession, redis: Redis, *, employee_id: str) -> User | None:
    s = get_settings()
    ctx = get_request_context()
    eid = canonical_employee_id(employee_id)
    try:
        await rate_limit.enforce(
            redis,
            [
                (f"rl:forgot:eid:{eid.lower()}", s.forgot_rate_eid, 3600),
                (f"rl:forgot:eidd:{eid.lower()}", s.forgot_rate_eid_day, 86400),
                (f"rl:forgot:ip:{net.rate_limit_key(ctx.ip) or 'unknown'}", s.forgot_rate_ip, 3600),
            ],
        )
    except AppError:
        return None  # identical response; request silently dropped
    user = (await session.execute(select(User).where(User.employee_id == eid))).unique().scalar_one_or_none()
    if user is None or user.status != "active" or user.deleted_at is not None:
        return None
    open_req = (
        await session.execute(
            select(PasswordResetRequest.id).where(PasswordResetRequest.user_id == user.id, PasswordResetRequest.status == "open")
        )
    ).first()
    if open_req is None:
        session.add(PasswordResetRequest(id=uuid.uuid4(), user_id=user.id, status="open"))
        audit(session, action="auth.forgot_password", actor=user, entity_type="user", entity_id=user.id)
        try:
            await session.commit()
        except IntegrityError:  # two simultaneous requests: the other one created the open request
            await session.rollback()
            return None
        return user
    return None


# ------------------------------------------------------------------ device tokens
async def register_device_token(
    session: AsyncSession, principal: Principal, *, fcm_token: str, platform: str, device_id: str | None
) -> None:
    row = (await session.execute(select(DeviceToken).where(DeviceToken.fcm_token == fcm_token))).scalar_one_or_none()
    if row is None:
        session.add(
            DeviceToken(id=uuid.uuid4(), user_id=principal.id, fcm_token=fcm_token, platform=platform, device_id=device_id)
        )
    else:  # token moved to another user/device: re-assign
        row.user_id, row.platform, row.device_id, row.last_seen_at = principal.id, platform, device_id, utcnow()
    await session.commit()


async def delete_device_token(session: AsyncSession, principal: Principal, *, fcm_token: str) -> None:
    await session.execute(delete(DeviceToken).where(DeviceToken.fcm_token == fcm_token, DeviceToken.user_id == principal.id))
    await session.commit()


# ------------------------------------------------------------------ client config
_CONFIG_DEFAULTS = {
    "food.cutoff_mode": "previous_day",
    "food.cutoff_time": "18:00",
    "office.lat": None,
    "office.lng": None,
    "office.radius_m": 150,
    "office.timezone": "Asia/Kolkata",
    "attendance.selfie_required": False,
    "auth.inactivity_timeout_minutes": 5,
    "permission.monthly_count_limit": 3,
    "permission.monthly_minutes_limit": 240,
    "permission.max_minutes_per_request": 120,
    "auth.offline_cache_max_hours": 72,
}


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _offline_hours(value: Any) -> int:
    """0 = off, otherwise 12..720 (a bad stored value falls back to the default, never to 'off')."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 72
    return 0 if v == 0 else max(12, min(720, v))


def _clamp_minutes(value: Any, default: int = 5, lo: int = 1, hi: int = 30) -> int:
    """A typo in the settings table must not disable the inactivity lock."""
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


async def client_config(session: AsyncSession) -> dict[str, Any]:
    rows = {r.key: r.value for r in (await session.execute(select(Setting))).scalars()}
    v = {k: rows.get(k, d) for k, d in _CONFIG_DEFAULTS.items()}
    return {
        "food": {"cutoff_mode": v["food.cutoff_mode"], "cutoff_time": v["food.cutoff_time"]},
        "office": {
            "lat": v["office.lat"],
            "lng": v["office.lng"],
            "radius_m": v["office.radius_m"],
            "timezone": v["office.timezone"],
        },
        "attendance": {"selfie_required": v["attendance.selfie_required"]},
        "auth": {
            "inactivity_timeout_minutes": _clamp_minutes(v["auth.inactivity_timeout_minutes"]),
            "offline_cache_max_hours": _offline_hours(v["auth.offline_cache_max_hours"]),
        },
        "company": await company_svc.public_block(session),
        "permission": {
            "monthly_count_limit": v["permission.monthly_count_limit"],
            "monthly_minutes_limit": v["permission.monthly_minutes_limit"],
            "max_minutes_per_request": v["permission.max_minutes_per_request"],
        },
    }


async def logout_web(session: AsyncSession, redis: Redis, *, refresh_token: str | None) -> None:
    """Console logout: authenticated by the refresh cookie alone. Revokes that family only (other devices of the same
    user keep working, so the access-token epoch is NOT bumped) and denylists the session's access tokens (`sid`
    claim) in Redis for their remaining lifetime (review L1)."""
    if not refresh_token:
        return
    row = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == security.hash_refresh_token(refresh_token), RefreshToken.client_type == "web"
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    await revoke_family(session, row.family_id)
    s = get_settings()
    await revoke_session_access(redis, row.family_id.hex, s.access_token_minutes * 60 + s.jwt_leeway_seconds + 5)
    audit(
        session,
        action="auth.logout",
        actor_employee_id=None,
        entity_type="user",
        entity_id=row.user_id,
        after={"client": "web"},
    )
    await session.commit()
