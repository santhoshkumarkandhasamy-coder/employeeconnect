"""Password hashing (Argon2id), password policy, JWT access tokens, refresh-token helpers."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from functools import lru_cache

import anyio
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .clock import utcnow
from .common_passwords import COMMON_PASSWORDS
from .config import get_settings
from .errors import AppError

MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128


# ------------------------------------------------------------------ hashing
@lru_cache(maxsize=1)
def _hasher() -> PasswordHasher:
    s = get_settings()
    return PasswordHasher(time_cost=s.argon2_time_cost, memory_cost=s.argon2_memory_kib, parallelism=s.argon2_parallelism)


def reset_hasher_cache() -> None:
    _hasher.cache_clear()
    _dummy_hash.cache_clear()
    _limiter_state.clear()


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher().verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher().check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher().hash(secrets.token_urlsafe(16))


def verify_dummy(password: str) -> None:
    """Spend the same time as a real verification (unknown employee_id)."""
    verify_password(_dummy_hash(), password)


# Async wrappers: Argon2 is ~100-250 ms of CPU and 64 MiB; running it on the event loop would stall every
# request (incl. /healthz). Hashing runs in worker threads (argon2-cffi releases the GIL) behind a bounded limiter.
_limiter_state: dict[int, anyio.CapacityLimiter] = {}


def _limiter() -> anyio.CapacityLimiter:
    key = id(asyncio.get_running_loop())  # one limiter per event loop (tests create many loops)
    lim = _limiter_state.get(key)
    if lim is None:
        _limiter_state.clear()
        lim = _limiter_state[key] = anyio.CapacityLimiter(max(1, get_settings().argon2_max_threads))
    return lim


async def ahash_password(password: str) -> str:
    return await anyio.to_thread.run_sync(hash_password, password, limiter=_limiter())


async def averify_password(password_hash: str, password: str) -> bool:
    return await anyio.to_thread.run_sync(verify_password, password_hash, password, limiter=_limiter())


async def averify_dummy(password: str) -> None:
    await anyio.to_thread.run_sync(verify_dummy, password, limiter=_limiter())


# ------------------------------------------------------------------ policy
def validate_password(password: str, employee_id: str = "", full_name: str = "") -> list[str]:
    """Return the list of failed rules (empty = ok). Length counted in characters; no trimming."""
    problems: list[str] = []
    if not MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN:
        problems.append(f"must be {MIN_PASSWORD_LEN}-{MAX_PASSWORD_LEN} characters long")
    if not any(c.isupper() for c in password):
        problems.append("must contain an upper-case letter")
    if not any(c.islower() for c in password):
        problems.append("must contain a lower-case letter")
    if not any(c.isdigit() for c in password):
        problems.append("must contain a digit")
    if not any(not c.isalnum() for c in password):
        problems.append("must contain a symbol")
    low = password.lower()
    if employee_id and employee_id.lower() in low:
        problems.append("must not contain your employee id")
    if any(len(tok) >= 3 and tok in low for tok in re.split(r"\s+", full_name.lower()) if tok):
        problems.append("must not contain a part of your name")
    letters_only = re.sub(r"[^a-z]", "", low)
    if low in COMMON_PASSWORDS or (letters_only and letters_only in COMMON_PASSWORDS):
        problems.append("is too common")
    return problems


_UPPER, _LOWER, _DIGIT, _SYMBOL = "ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnpqrstuvwxyz", "23456789", "!@#$%^&*-_+="


def generate_temp_password(employee_id: str = "", full_name: str = "", length: int = 12) -> str:
    allchars = _UPPER + _LOWER + _DIGIT + _SYMBOL
    while True:
        chars = [secrets.choice(_UPPER), secrets.choice(_LOWER), secrets.choice(_DIGIT), secrets.choice(_SYMBOL)]
        chars += [secrets.choice(allchars) for _ in range(length - 4)]
        secrets.SystemRandom().shuffle(chars)
        candidate = "".join(chars)
        if not validate_password(candidate, employee_id, full_name):
            return candidate


# ------------------------------------------------------------------ refresh tokens
def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------ JWT
@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    jti: str
    role: str
    pwd: bool
    iat: int = 0
    sid: str | None = None  # console (web) sessions only: the refresh-token family id


JWT_ISSUER = "employeeconnect"
JWT_AUDIENCE = "employeeconnect-api"


def create_access_token(
    user_id: uuid.UUID,
    role: str,
    must_change_password: bool,
    not_before: dt.datetime | None = None,
    sid: str | None = None,
) -> tuple[str, int]:
    """`not_before` = the user's token_valid_after: iat is never earlier than it, so tokens minted right after an
    invalidation (same second) are not rejected by the `iat < token_valid_after` check."""
    s = get_settings()
    now = utcnow()
    expires = s.access_token_minutes * 60
    iat = int(now.timestamp())
    if not_before is not None:
        iat = max(iat, int(not_before.timestamp()))
    claims = {
        "sub": str(user_id),
        "jti": uuid.uuid4().hex,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "iat": iat,
        "exp": iat + expires,
        "role": role,
        "pwd": must_change_password,
    }
    if sid:
        claims["sid"] = sid
    return jwt.encode(claims, s.jwt_secret, algorithm=s.jwt_algorithm), expires


def decode_access_token(token: str) -> AccessClaims:
    s = get_settings()
    try:
        data = jwt.decode(
            token,
            s.jwt_secret,
            algorithms=[s.jwt_algorithm],
            leeway=s.jwt_leeway_seconds,
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["exp", "sub", "iat"]},
        )
        return AccessClaims(
            uuid.UUID(data["sub"]),
            data.get("jti", ""),
            data.get("role", ""),
            bool(data.get("pwd")),
            int(data["iat"]),
            str(data["sid"]) if data.get("sid") else None,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AppError("TOKEN_EXPIRED", "Access token expired") from exc
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise AppError("UNAUTHORIZED", "Invalid access token") from exc
