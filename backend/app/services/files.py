"""File storage: validated uploads, authorised reads, orphan cleanup.

* Type is decided by magic bytes, never by the client's Content-Type or filename.
* Images are decoded and re-encoded (drops EXIF/GPS/ICC/comments and any trailing payload).
* Stored under FILE_STORAGE_DIR/<2 hex>/<random hex>.bin; the client never influences the path.
* Selfies and leave attachments are Fernet-encrypted at rest (files.is_encrypted).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import os
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.config import get_settings
from ..core.crypto import DataDecryptionError, decrypt_bytes, encrypt_bytes
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import Announcement, AnnouncementDepartment, Attendance, Company, EmployeeProfile, File, LeaveRequest, User

log = logging.getLogger("app.files")

MB = 1024 * 1024
IMAGE_TYPES = {"image/jpeg", "image/png"}
PDF = "application/pdf"
EXT = {"image/jpeg": "jpg", "image/png": "png", PDF: "pdf"}

# purpose -> (allowed content types, max MB; always also capped by MAX_UPLOAD_MB, encrypt at rest, permission to upload)
PURPOSES: dict[str, tuple[frozenset[str], int, bool, str | None]] = {
    "selfie": (frozenset(IMAGE_TYPES), 5, True, None),
    "profile_photo": (frozenset(IMAGE_TYPES), 5, False, None),
    "leave_attachment": (frozenset(IMAGE_TYPES | {PDF}), 10, True, None),
    "announcement_image": (frozenset(IMAGE_TYPES), 5, False, "announcement.manage"),
    "announcement_pdf": (frozenset({PDF}), 10, False, "announcement.manage"),
    "company_logo": (frozenset(IMAGE_TYPES), 2, False, "company.manage"),  # only via POST /admin/company/logo
}
GENERIC_PURPOSES = frozenset(PURPOSES) - {"profile_photo", "company_logo"}  # accepted by POST /files
MAX_IMAGE_SIDE = 2048
MAX_IMAGE_PIXELS = 16_000_000  # decoded pixels; checked from the header BEFORE decoding (12 MP phones are ~12.2 Mpx)
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS  # Pillow's own guard (warns above, raises above 2x)
# Bounds concurrent decodes (each can hold tens of MB). A threading semaphore: the work runs in worker threads.
_IMAGE_SLOTS: threading.BoundedSemaphore | None = None
_IMAGE_SLOTS_N = 0
_IMAGE_SLOTS_LOCK = threading.Lock()
# PDF features that run code or smuggle files; refused outright (names can only be obfuscated with #xx escapes, also refused)
PDF_FORBIDDEN = (
    b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile", b"/RichMedia",
    b"/OpenAction", b"/SubmitForm", b"/GoToR", b"/ImportData", b"/XFA",
)  # fmt: skip
# `/AA` (additional actions) is a short name: refuse it only as a whole name, not as the prefix of e.g. /AAPL
_PDF_AA = re.compile(rb"/AA(?![A-Za-z0-9_.\-])")


def file_url(file_id: uuid.UUID | str | None) -> str | None:
    return f"/api/files/{file_id}" if file_id else None


# ---------------------------------------------------------------- storage primitives
def _root() -> Path:
    return Path(get_settings().file_storage_dir).resolve()


def _resolve(storage_path: str) -> Path:
    """Map a stored relative path to an absolute one, refusing anything that escapes the storage root."""
    root = _root()
    p = (root / storage_path).resolve()
    if not p.is_relative_to(root) or p == root:
        raise AppError("NOT_FOUND", "File not found")
    return p


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- validation
def sniff(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"%PDF-"):
        return PDF
    return None


def _slots() -> threading.BoundedSemaphore:
    global _IMAGE_SLOTS, _IMAGE_SLOTS_N
    n = max(1, get_settings().image_concurrency)
    with _IMAGE_SLOTS_LOCK:
        if _IMAGE_SLOTS is None or n != _IMAGE_SLOTS_N:
            _IMAGE_SLOTS, _IMAGE_SLOTS_N = threading.BoundedSemaphore(n), n
        return _IMAGE_SLOTS


def _reencode(data: bytes, ctype: str, max_side: int = MAX_IMAGE_SIDE, to_png: bool = False) -> bytes:
    """Decode fully and write a fresh file: no metadata, no appended data. Concurrency- and pixel-bounded.
    `to_png` writes PNG even for a JPEG source (company logo)."""
    with _slots():
        if to_png:
            return _reencode_unbounded(data, ctype, max_side, True)
        return _reencode_unbounded(data, ctype, max_side)


def _reencode_unbounded(data: bytes, ctype: str, max_side: int = MAX_IMAGE_SIDE, to_png: bool = False) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format != ("JPEG" if ctype == "image/jpeg" else "PNG"):
                raise ValueError("format mismatch")
            if im.width * im.height > MAX_IMAGE_PIXELS:  # header only so far: nothing big has been allocated yet
                raise ValueError("image too large")
            if ctype == "image/jpeg":
                im.draft("RGB", (max_side, max_side))  # let the decoder downscale while decoding
            im.load()
            im = ImageOps.exif_transpose(im)  # bake in the orientation before the EXIF is dropped
            im.thumbnail((max_side, max_side))
            out = io.BytesIO()
            if ctype == "image/jpeg" and not to_png:
                clean = im.convert("RGB")  # always a fresh image: no second full-size copy
                clean.info = {}
                clean.save(out, "JPEG", quality=85, optimize=True)
            else:
                clean = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")  # (also JPEG -> PNG)
                clean.info = {}
                clean.save(out, "PNG", optimize=True)
            return out.getvalue()
    except (
        UnidentifiedImageError,
        ValueError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        SyntaxError,
    ) as exc:
        raise AppError("FILE_TYPE_NOT_ALLOWED", "The file is not a valid image") from exc


_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")


def _pdf_is_safe(data: bytes) -> bool:
    """Best-effort screen on the raw bytes (decodes #xx name escapes). Names inside compressed object streams are not
    visible here, so this reduces, but does not remove, the risk; viewers should still treat the file as untrusted."""
    plain = _NAME_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), data)
    return not any(tok in plain for tok in PDF_FORBIDDEN) and _PDF_AA.search(plain) is None


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    buf = bytearray()
    while chunk := await upload.read(64 * 1024):
        buf += chunk
        if len(buf) > limit:
            raise AppError("FILE_TOO_LARGE", f"File exceeds the {limit // MB} MB limit")
    return bytes(buf)


# ---------------------------------------------------------------- upload
async def upload(
    session: AsyncSession, actor: Principal, purpose: str, upload_file: UploadFile, *, max_side: int = MAX_IMAGE_SIDE
) -> dict:
    spec = PURPOSES.get(purpose)
    if spec is None:
        raise AppError("VALIDATION_ERROR", "Unknown purpose", details=[{"field": "purpose", "message": "unsupported value"}])
    allowed, max_mb, encrypt, perm = spec
    if perm and not actor.has(perm):
        raise AppError("FORBIDDEN", "You do not have permission to perform this action")
    limit = min(max_mb, get_settings().max_upload_mb) * MB
    data = await _read_limited(upload_file, limit)
    ctype = sniff(data)
    if ctype is None or ctype not in allowed:
        raise AppError("FILE_TYPE_NOT_ALLOWED", "File type is not allowed for this purpose")
    if ctype in IMAGE_TYPES:
        data = await asyncio.to_thread(_reencode, data, ctype, max_side)
    elif ctype == PDF and not _pdf_is_safe(data):
        raise AppError("FILE_TYPE_NOT_ALLOWED", "PDFs with scripts, launch actions or embedded files are not allowed")

    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    name = secrets.token_hex(16)
    rel = f"{name[:2]}/{name}.bin"
    stored = encrypt_bytes(data) if encrypt else data
    path = _resolve(rel)
    await asyncio.to_thread(_write_atomic, path, stored)
    rec = File(
        owner_id=actor.id,
        purpose=purpose,
        storage_path=rel,
        content_type=ctype,
        size_bytes=len(data),
        sha256=digest,
        is_encrypted=encrypt,
    )
    session.add(rec)
    try:
        await session.flush()
        if perm:  # admin-side uploads are audited; employee selfies/attachments would be noise
            audit(
                session,
                action="file.upload",
                actor=actor,
                entity_type="file",
                entity_id=rec.id,
                after={"purpose": purpose, "content_type": ctype, "size_bytes": len(data)},
            )
        await session.commit()
    except Exception:
        await asyncio.to_thread(_unlink, path)
        raise
    return {"id": str(rec.id), "url": file_url(rec.id), "content_type": ctype, "size_bytes": len(data)}


async def store_company_logo(session: AsyncSession, actor: Principal, upload_file: UploadFile) -> File:
    """png/jpeg (magic bytes) <= 2 MB -> decoded and re-encoded to PNG, longest side <= 512 px, metadata stripped; stored
    unencrypted (it is public). Flushes the File row but does NOT commit: the caller links it to the company row and commits
    (and calls `delete_blob` if that fails)."""
    import hashlib

    limit = min(PURPOSES["company_logo"][1], get_settings().max_upload_mb) * MB
    data = await _read_limited(upload_file, limit)
    ctype = sniff(data)
    if ctype not in IMAGE_TYPES:  # SVG, PDF, anything else
        raise AppError("FILE_TYPE_NOT_ALLOWED", "The logo must be a PNG or JPEG image")
    png_bytes = await asyncio.to_thread(_reencode, data, ctype, 512, True)
    name = secrets.token_hex(16)
    rel = f"{name[:2]}/{name}.bin"
    path = _resolve(rel)
    await asyncio.to_thread(_write_atomic, path, png_bytes)
    rec = File(
        owner_id=actor.id,
        purpose="company_logo",
        storage_path=rel,
        content_type="image/png",
        size_bytes=len(png_bytes),
        sha256=hashlib.sha256(png_bytes).hexdigest(),
        is_encrypted=False,
    )
    session.add(rec)
    try:
        await session.flush()
    except Exception:
        await asyncio.to_thread(_unlink, path)
        raise
    return rec


async def read_plain(f: File) -> bytes:
    """Bytes of an unencrypted public file (company logo)."""
    try:
        return await asyncio.to_thread(_resolve(f.storage_path).read_bytes)
    except FileNotFoundError:
        raise AppError("NOT_FOUND", "File not found") from None


# ---------------------------------------------------------------- authorisation + read
async def _announcement_visible(session: AsyncSession, actor: Principal, f: File) -> bool:
    if actor.has("announcement.manage"):
        return True
    now = utcnow()
    q = select(Announcement).where(
        or_(Announcement.image_file_id == f.id, Announcement.pdf_file_id == f.id),
        Announcement.deleted_at.is_(None),
        Announcement.published_at <= now,
        or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
    )
    for a in (await session.execute(q)).scalars():
        if a.target_all:
            return True
        dept = actor.user.department_id
        if dept is not None:
            hit = await session.execute(
                select(AnnouncementDepartment.announcement_id).where(
                    AnnouncementDepartment.announcement_id == a.id, AnnouncementDepartment.department_id == dept
                )
            )
            if hit.first():
                return True
    return False


async def _leave_attachment_visible(session: AsyncSession, actor: Principal, f: File) -> bool:
    """Authorise through a REQUEST (review 03 M4): some leave request that carries this file must be one the actor may see
    (view_all / approve_all = any requester, view_team / approve_team = requester currently reports to the actor).
    Files not attached to any request (never submitted) are owner-only."""
    if actor.has("request.view_all") or actor.has("request.approve_all"):
        q = select(LeaveRequest.id).where(LeaveRequest.attachment_file_id == f.id).limit(1)
    elif actor.has("request.view_team") or actor.has("request.approve_team"):
        q = (
            select(LeaveRequest.id)
            .join(User, User.id == LeaveRequest.user_id)
            .where(LeaveRequest.attachment_file_id == f.id, User.manager_id == actor.id)
            .limit(1)
        )
    else:
        return False
    return (await session.execute(q)).first() is not None


@dataclass
class Access:
    file: File
    cross_user: bool  # someone other than the owner is reading a private file


async def authorize(session: AsyncSession, actor: Principal, file_id: uuid.UUID) -> Access:
    f = await session.get(File, file_id)
    if f is None:
        raise AppError("NOT_FOUND", "File not found")
    if f.owner_id == actor.id:
        return Access(f, False)
    ok = False
    if f.purpose == "profile_photo":
        # shown in directories / celebrations to every signed-in colleague, but only while it IS somebody's current photo
        ok = (
            await session.execute(
                select(EmployeeProfile.user_id)
                .join(User, User.id == EmployeeProfile.user_id)
                .where(EmployeeProfile.photo_file_id == f.id, User.status == "active", User.deleted_at.is_(None))
                .limit(1)
            )
        ).first() is not None
    elif f.purpose in ("announcement_image", "announcement_pdf"):
        ok = await _announcement_visible(session, actor, f)
    elif f.purpose == "leave_attachment":
        ok = await _leave_attachment_visible(session, actor, f)
    elif f.purpose == "selfie":
        if actor.has("attendance.view_all"):
            ok = True
        elif actor.has("attendance.view_team"):
            owner = await session.get(User, f.owner_id)
            ok = owner is not None and owner.manager_id == actor.id
    if not ok:
        # signal for id probing (e.g. a manager walking other teams' selfies); committed before the 404 is raised
        audit(
            session,
            action="file.access_denied",
            actor=actor,
            entity_type="file",
            entity_id=f.id,
            after={"purpose": f.purpose},
            outcome="failure",
        )
        await session.commit()
        raise AppError("NOT_FOUND", "File not found")  # 404, not 403: do not reveal that the id exists
    return Access(f, f.purpose in ("selfie", "leave_attachment"))


async def read(session: AsyncSession, actor: Principal, file_id: uuid.UUID) -> tuple[File, bytes]:
    acc = await authorize(session, actor, file_id)
    f = acc.file
    path = _resolve(f.storage_path)
    try:
        raw = await asyncio.to_thread(path.read_bytes)
    except FileNotFoundError:
        log.error("file row %s has no blob on disk", f.id)
        raise AppError("NOT_FOUND", "File not found") from None
    try:
        data = decrypt_bytes(raw) if f.is_encrypted else raw
    except DataDecryptionError:
        log.error("file %s (%s) cannot be decrypted with the configured keys", f.id, f.purpose)
        raise AppError("FILE_UNAVAILABLE", "File is temporarily unavailable") from None
    if acc.cross_user:
        audit(
            session,
            action="file.view_other",
            actor=actor,
            entity_type="file",
            entity_id=f.id,
            after={"purpose": f.purpose, "owner_id": str(f.owner_id)},
        )
        await session.commit()
    return f, data


async def delete_blob(f: File) -> None:
    """Remove the stored bytes of a file row (the caller deletes the row). Missing blobs are fine."""
    try:
        await asyncio.to_thread(_unlink, _resolve(f.storage_path))
    except AppError:
        log.error("file %s has an invalid storage path; blob not removed", f.id)


# ---------------------------------------------------------------- helpers for other modules
async def photo_file_ids(session: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """user_id -> profile photo file id, one query (for UserSummary.photo_url)."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(EmployeeProfile.user_id, EmployeeProfile.photo_file_id).where(
            EmployeeProfile.user_id.in_(set(user_ids)), EmployeeProfile.photo_file_id.is_not(None)
        )
    )
    return {u: p for u, p in rows.all()}


async def cleanup_orphans(session: AsyncSession, older_than_hours: int = 24, batch: int = 500) -> int:
    """Worker job: delete files nothing references (abandoned uploads) older than the grace period, plus stray
    *.tmp blobs. Returns the number of file rows removed."""
    cutoff = utcnow() - dt.timedelta(hours=older_than_hours)
    refs = [
        select(Attendance.id).where(
            or_(Attendance.check_in_selfie_file_id == File.id, Attendance.check_out_selfie_file_id == File.id)
        ),
        select(LeaveRequest.id).where(LeaveRequest.attachment_file_id == File.id),
        select(Announcement.id).where(or_(Announcement.image_file_id == File.id, Announcement.pdf_file_id == File.id)),
        select(EmployeeProfile.user_id).where(EmployeeProfile.photo_file_id == File.id),
        select(Company.id).where(Company.logo_file_id == File.id),
    ]
    q = select(File).where(File.created_at < cutoff, *[~r.exists() for r in refs]).limit(batch)
    removed = 0
    for f in (await session.execute(q)).scalars().all():
        try:
            await asyncio.to_thread(_unlink, _resolve(f.storage_path))
        except AppError:
            log.error("orphan file %s has an invalid storage path; row dropped only", f.id)
        await session.delete(f)
        removed += 1
    await session.commit()

    def _sweep_tmp() -> None:
        root = _root()
        if root.is_dir():
            limit = cutoff.timestamp()
            for p in root.glob("*/*.tmp"):
                if p.stat().st_mtime < limit:
                    _unlink(p)

    await asyncio.to_thread(_sweep_tmp)
    return removed
