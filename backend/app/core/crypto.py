"""Application-level encryption for sensitive columns (Fernet / MultiFernet).

Created by the database-engineer because the SQLAlchemy models import the column
types below. The backend-developer owns this file from now on; keep the public
names (`EncryptedString`, `EncryptedJSON`, `encrypt_bytes`, `decrypt_bytes`) stable.

DATA_ENCRYPTION_KEYS = comma separated Fernet keys, NEWEST FIRST (rotation: prepend a
new key, run a re-encrypt script, then drop the old key). Keys are read lazily so that
importing the models (Alembic, tests that do not touch encrypted columns) needs no env.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy.types import LargeBinary, TypeDecorator


class DataDecryptionError(Exception):
    """A stored token cannot be decrypted with any configured key (key dropped/rotated wrongly, or data tampered with)."""


@lru_cache(maxsize=1)
def _fernets() -> tuple[Fernet, ...]:
    raw = os.environ.get("DATA_ENCRYPTION_KEYS", "")
    if not raw:  # .env-file configuration is visible through Settings, not os.environ
        from .config import get_settings

        raw = get_settings().data_encryption_keys
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("DATA_ENCRYPTION_KEYS is not set (comma separated Fernet keys, newest first)")
    return tuple(Fernet(k.encode()) for k in keys)


def _fernet() -> MultiFernet:
    return MultiFernet(list(_fernets()))


def fernet_keys() -> tuple[Fernet, ...]:
    """Configured keys, newest (primary) first. Used by the key-rotation tooling."""
    return _fernets()


def validate_keys() -> None:
    """Called at startup so a missing/malformed key stops the app instead of 500ing on first write."""
    _fernets()


def reset_crypto_cache() -> None:
    """For tests / key rotation: forget cached keys."""
    _fernets.cache_clear()


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    try:
        return _fernet().decrypt(bytes(token))
    except InvalidToken as exc:
        raise DataDecryptionError("stored value cannot be decrypted with the configured keys") from exc


class EncryptedString(TypeDecorator):
    """str <-> bytea (Fernet token). None stays NULL. Not filterable/sortable in SQL."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> bytes | None:
        if value is None:
            return None
        return encrypt_bytes(value.encode("utf-8"))

    def process_result_value(self, value: bytes | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return decrypt_bytes(value).decode("utf-8")


class EncryptedJSON(TypeDecorator):
    """JSON-serialisable value (e.g. {"lat":..,"lng":..}) <-> bytea (Fernet token)."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> bytes | None:
        if value is None:
            return None
        return encrypt_bytes(json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def process_result_value(self, value: bytes | None, dialect: Any) -> Any:
        if value is None:
            return None
        return json.loads(decrypt_bytes(value).decode("utf-8"))
