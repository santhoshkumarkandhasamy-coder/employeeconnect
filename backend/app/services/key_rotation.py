"""Re-encrypt stored data under the newest DATA_ENCRYPTION_KEYS entry.

Procedure: prepend the new key to DATA_ENCRYPTION_KEYS (keep the old ones), deploy, run
`python -m scripts.rotate_keys`, then `python -m scripts.rotate_keys --verify-only` (must report 0 failed), and only
then drop the old key. The run is idempotent and resumable: tokens already under the primary key are skipped.
Covers every `EncryptedString`/`EncryptedJSON` column (discovered from the models) and every encrypted file blob.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import LargeBinary, select, text, type_coerce, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.crypto import EncryptedJSON, EncryptedString, fernet_keys
from ..core.errors import AppError
from ..models import Base, File
from . import files as files_svc

log = logging.getLogger("app.keyrotation")


def encrypted_columns() -> list[tuple[Any, Any]]:
    """[(table, column)] for every application-encrypted column."""
    out = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, (EncryptedString, EncryptedJSON)):
                out.append((table, col))
    return out


async def _allow_rotation(session: AsyncSession) -> None:
    """PostgreSQL only: lets the payroll lock triggers (migration 0018) accept an UPDATE that changes nothing but `*_enc`
    columns. `SET LOCAL` lasts for the current transaction only, so it is issued again after every commit. Other columns are
    still refused by the triggers."""
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(text("SET LOCAL app.key_rotation = 'on'"))


def _is_primary(primary: Fernet, token: bytes) -> bool:
    try:
        primary.decrypt(bytes(token))
        return True
    except InvalidToken:
        return False


async def rotate_all(session: AsyncSession, *, dry_run: bool = False, batch: int = 500) -> dict[str, int]:
    keys = fernet_keys()
    primary, multi = keys[0], MultiFernet(list(keys))
    stats = {"rotated": 0, "already_current": 0, "undecryptable": 0, "missing_blobs": 0}

    for table, col in encrypted_columns():
        pk = list(table.primary_key.columns)
        raw_col = type_coerce(col, LargeBinary)  # bypass the TypeDecorator: we want the stored bytes
        offset = 0
        while True:
            q = select(*pk, raw_col).where(col.is_not(None)).order_by(*pk).limit(batch).offset(offset)
            rows = (await session.execute(q)).all()
            if not rows:
                break
            offset += len(rows)
            if not dry_run:
                await _allow_rotation(session)
            for row in rows:
                *pk_vals, token = row
                try:
                    if _is_primary(primary, token):
                        stats["already_current"] += 1
                        continue
                    new = multi.rotate(bytes(token))
                except InvalidToken:
                    stats["undecryptable"] += 1
                    log.error("undecryptable value in %s.%s (pk=%s)", table.name, col.name, pk_vals)
                    continue
                stats["rotated"] += 1
                if not dry_run:
                    cond = [c == v for c, v in zip(pk, pk_vals, strict=True)]
                    await session.execute(update(table).where(*cond).values({col.name: type_coerce(new, LargeBinary)}))
            if not dry_run:
                await session.commit()

    offset = 0
    while True:
        q = select(File).where(File.is_encrypted.is_(True)).order_by(File.id).limit(batch).offset(offset)
        files = (await session.execute(q)).scalars().all()
        if not files:
            break
        offset += len(files)
        for f in files:
            try:
                path = files_svc._resolve(f.storage_path)
                raw = await asyncio.to_thread(path.read_bytes)
            except (FileNotFoundError, AppError):
                stats["missing_blobs"] += 1
                log.error("encrypted file %s has no readable blob", f.id)
                continue
            try:
                if _is_primary(primary, raw):
                    stats["already_current"] += 1
                    continue
                new = multi.rotate(raw)
            except InvalidToken:
                stats["undecryptable"] += 1
                log.error("undecryptable file blob %s", f.id)
                continue
            stats["rotated"] += 1
            if not dry_run:
                await asyncio.to_thread(files_svc._write_atomic, path, new)  # atomic replace: never a torn blob
    return stats


async def verify_all(session: AsyncSession, batch: int = 500) -> dict[str, int]:
    """Everything must decrypt with the PRIMARY key alone (i.e. the old keys can be dropped)."""
    primary = fernet_keys()[0]
    stats = {"ok": 0, "failed": 0}
    for table, col in encrypted_columns():
        pk = list(table.primary_key.columns)
        offset = 0
        while True:
            q = select(type_coerce(col, LargeBinary)).where(col.is_not(None)).order_by(*pk).limit(batch).offset(offset)
            rows = (await session.execute(q)).scalars().all()
            if not rows:
                break
            offset += len(rows)
            for token in rows:
                stats["ok" if _is_primary(primary, token) else "failed"] += 1
    files = (await session.execute(select(File).where(File.is_encrypted.is_(True)))).scalars().all()
    for f in files:
        try:
            raw = await asyncio.to_thread(files_svc._resolve(f.storage_path).read_bytes)
        except (FileNotFoundError, AppError):
            stats["failed"] += 1
            continue
        stats["ok" if _is_primary(primary, raw) else "failed"] += 1
    return stats
