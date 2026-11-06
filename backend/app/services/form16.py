"""Form 16 documents (plan 17 work package 5b, contract 19.8).

Form 16 is UPLOADED, never generated (T5): Part A comes from TRACES and the signed certificate from the employer / tax consultant. HR
uploads PDFs (`form16`, `part_a`, `part_b`, `form12ba`); each file is matched to an employee by the PAN or the Employee ID in its NAME.
The original file name contains the PAN, so it is used for matching only and is never stored, logged, audited or echoed (errors carry
the index in the upload). A dry run stores nothing but an HMAC binding (`actor | fy | kind | sorted file hashes`) in Redis; the commit
must present the manifest hash of the same files. Publishing to employees is maker-checker: `payroll.approve` + step-up, not the uploader
(setting `payroll.form16_requires_second_person`, fixed per document at upload), never the employee, bound to the listed `{id, sha256}`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import UploadFile
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import EmployeePayrollDetails, File, Form16Document, User
from ..schemas.payroll_tax import Form16Publish, Form16Withdraw
from . import admin_security, notifications
from . import files as files_svc
from . import payroll_common as pc
from . import payroll_settings as ps
from . import tax_rules as tr

log = logging.getLogger("app.payroll")
ENTITY = "form16"
KINDS = ("form16", "part_a", "part_b", "form12ba")
KIND_SLUG = {"form16": "form16", "part_a": "part-a", "part_b": "part-b", "form12ba": "form12ba"}
MAX_FILES = 20
MAX_FILE_BYTES = 2 * 1024 * 1024
DRY_RUN_TTL_S = 15 * 60
PUBLISH_MAX = 500
_PAN_TOKEN = re.compile(r"(?<![A-Z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Z0-9])")
_PDF_SUFFIX = re.compile(r"\.PDF$")
_YEAR_LIKE = re.compile(r"^(FY)?(19|20)\d{2}([-_/]?\d{2,4})?$")  # a year or a financial year is never an Employee ID
MIN_EID_LEN = 3


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise AppError(
            "VALIDATION_ERROR",
            "Unknown document kind",
            details=[{"field": "kind", "message": "must be one of: " + ", ".join(KINDS)}],
        )
    return kind


# ============================================================================================ matching
@dataclass
class Directory:
    """Everybody a file name can point to. PANs are decrypted once per request."""

    users: dict[uuid.UUID, User]
    pans: dict[str, set[uuid.UUID]] = field(default_factory=dict)
    eids: list[tuple[str, uuid.UUID]] = field(default_factory=list)


async def directory(session: AsyncSession) -> Directory:
    users = {u.id: u for u in (await session.execute(select(User))).unique().scalars()}
    d = Directory(users=users, eids=[(u.employee_id.upper(), u.id) for u in users.values() if u.employee_id])
    for det in (await session.execute(select(EmployeePayrollDetails))).scalars():
        pan = (det.pan or "").strip().upper()
        if pan:
            d.pans.setdefault(pan, set()).add(det.user_id)
    return d


def _has_token(stem: str, token: str) -> bool:
    start = stem.find(token)
    while start != -1:
        end = start + len(token)
        before_ok = start == 0 or not stem[start - 1].isalnum()
        after_ok = end == len(stem) or not stem[end].isalnum()
        if before_ok and after_ok:
            return True
        start = stem.find(token, start + 1)
    return False


def _stem(name: str) -> str:
    return _PDF_SUFFIX.sub("", name.strip().upper())


def has_pan_token(name: str | None) -> bool:
    return bool(name) and _PAN_TOKEN.search(_stem(name or "")) is not None


def match_name(name: str | None, d: Directory) -> set[uuid.UUID]:
    """Employees a file NAME points to. When the name holds a PAN-shaped token ONLY the PANs decide (Employee ID tokens are ignored, so a
    stray ID cannot redirect a document); otherwise an Employee ID token decides (delimited by anything that is not a letter or digit).
    Employee IDs shorter than 3 characters or shaped like a year / financial year never match."""
    if not name:
        return set()
    stem = _stem(name)
    tokens = _PAN_TOKEN.findall(stem)
    found: set[uuid.UUID] = set()
    if tokens:
        for tok in tokens:
            found |= d.pans.get(tok, set())
        return found
    for eid, uid in d.eids:
        if len(eid) >= MIN_EID_LEN and not _YEAR_LIKE.match(eid) and _has_token(stem, eid):
            found.add(uid)
    return found


@dataclass
class Parsed:
    index: int
    data: bytes | None = None
    sha: str | None = None
    user_id: uuid.UUID | None = None
    error: str | None = None


async def _read_upload(up: UploadFile) -> bytes | None:
    """The bytes, or None when the file is larger than the per-file limit (not kept)."""
    buf = bytearray()
    while chunk := await up.read(64 * 1024):
        buf += chunk
        if len(buf) > MAX_FILE_BYTES:
            return None
    return bytes(buf)


async def parse_uploads(session: AsyncSession, uploads: list[UploadFile], actor: Principal | None = None) -> list[Parsed]:
    if not 1 <= len(uploads) <= MAX_FILES:
        raise AppError("VALIDATION_ERROR", f"Send 1 to {MAX_FILES} PDF files",
                       details=[{"field": "files", "message": f"send 1 to {MAX_FILES} files"}])  # fmt: skip
    d = await directory(session)
    out: list[Parsed] = []
    for i, up in enumerate(uploads):
        p = Parsed(index=i)
        out.append(p)
        data = await _read_upload(up)
        if data is None:
            p.error = "FILE_TOO_LARGE"
            continue
        if not files_svc.pdf_ok(data):
            p.error = "NOT_PDF"
            continue
        p.data, p.sha = data, hashlib.sha256(data).hexdigest()
        hits = match_name(up.filename, d)
        if not hits:
            p.error = "UNKNOWN_PAN" if has_pan_token(up.filename) else "UNMATCHED"
        elif len(hits) > 1:
            p.error = "AMBIGUOUS"
        else:
            (p.user_id,) = hits
            if actor is not None and p.user_id == actor.id:
                p.error = "FORBIDDEN"  # nobody uploads their own Form 16
            elif d.users[p.user_id].deleted_at is not None:
                p.error = "DELETED_EMPLOYEE"
    seen: dict[uuid.UUID, list[Parsed]] = {}
    for p in out:
        if p.user_id is not None and p.error is None:
            seen.setdefault(p.user_id, []).append(p)
    for group in seen.values():
        if len(group) > 1:
            for p in group:
                p.error = "DUPLICATE_EMPLOYEE"
    return out


def manifest(fy: str, kind: str, parsed: list[Parsed]) -> str:
    """sha256 of `fy|kind|sorted (file hash:employee id) pairs`: the commit must reproduce the very mapping that was reviewed."""
    pairs = sorted(f"{p.sha}:{p.user_id or '-'}" for p in parsed if p.sha)
    return hashlib.sha256(f"{fy}|{kind}|{','.join(pairs)}".encode()).hexdigest()


def _binding_key(actor: Principal, fy: str, kind: str, man: str) -> str:
    mac = hmac.new(pc.import_binding_key(), f"form16|{actor.id}|{fy}|{kind}|{man}".encode(), hashlib.sha256).hexdigest()
    return f"payroll:form16:dry:{mac}"


def _unavailable() -> AppError:
    return AppError(
        "TEMPORARILY_UNAVAILABLE", "Service temporarily unavailable. Try again shortly.", headers={"Retry-After": "30"}
    )


# ============================================================================================ output
def document_out(doc: Form16Document, people: dict[uuid.UUID, User]) -> dict[str, Any]:
    return {
        "id": str(doc.id), "user": pc.user_out(people.get(doc.user_id)), "fy": doc.fy, "kind": doc.kind, "status": doc.status,
        "sha256": doc.sha256, "uploaded_by": pc.user_out(people.get(doc.uploaded_by)), "uploaded_at": pc.iso(doc.created_at),
        "published_by": pc.user_out(people.get(doc.published_by)) if doc.published_by else None,
        "published_at": pc.iso(doc.published_at), "withdrawn_at": pc.iso(doc.withdrawn_at),
    }  # fmt: skip


def _event(session: AsyncSession, doc: Form16Document, action: str, actor_id: uuid.UUID, **detail: Any) -> None:
    tr.event(session, "form16", doc.id, doc.user_id, doc.status, action, actor_id, detail or None)


def _audit(session: AsyncSession, actor: Principal, action: str, entity_id: Any, **after: Any) -> None:
    audit(session, action=action, actor=actor, entity_type=ENTITY, entity_id=entity_id, after=after)


async def _commit(session: AsyncSession) -> None:
    try:
        await pc.commit(session)
    except AppError:
        raise
    except IntegrityError as exc:  # pragma: no cover
        await session.rollback()
        raise pc.map_integrity(exc) from None


async def _live(
    session: AsyncSession, fy: str, kind: str, user_ids: list[uuid.UUID], *, lock: bool = False
) -> dict[uuid.UUID, Form16Document]:
    if not user_ids:
        return {}
    q = select(Form16Document).where(
        Form16Document.fy == fy, Form16Document.kind == kind, Form16Document.user_id.in_(user_ids),
        Form16Document.status.in_(("uploaded", "published")),
    )  # fmt: skip
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return {d.user_id: d for d in (await session.execute(q)).scalars()}


# ============================================================================================ upload
async def upload(
    session: AsyncSession, redis: Redis, actor: Principal, *, fy: str, kind: str, dry_run: bool, uploads: list[UploadFile],
    manifest_sha256: str | None, password: SecretStr | None,
) -> tuple[int, dict[str, Any]]:  # fmt: skip
    """(HTTP status, body): 200 for a dry run, 201 for a commit."""
    tr.check_fy(fy)
    check_kind(kind)
    if not dry_run:
        missing = [f for f, v in (("manifest_sha256", manifest_sha256), ("current_password", password)) if not v]
        if missing:
            raise AppError("VALIDATION_ERROR", "Request validation failed",
                           details=[{"field": f, "message": "is required to commit the upload"} for f in missing])  # fmt: skip
        if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256 or ""):
            raise AppError("VALIDATION_ERROR", "Request validation failed",
                           details=[{"field": "manifest_sha256", "message": "must be the 64 hex characters of the dry run"}])  # fmt: skip
        await admin_security.require_reauth(session, redis, actor, password, action="payroll.form16.commit")
    parsed = await parse_uploads(session, uploads, actor)
    man = manifest(fy, kind, parsed)
    users = {
        u.id: u
        for u in (await session.execute(select(User).where(User.id.in_({p.user_id for p in parsed if p.user_id}))))
        .unique()
        .scalars()
    }
    errors = [{"index": p.index, "message": p.error} for p in parsed if p.error]
    if dry_run:
        live = await _live(session, fy, kind, [p.user_id for p in parsed if p.user_id and not p.error])
        matched = [
            {"index": p.index, "user": pc.user_out(users[p.user_id]), "sha256": p.sha,
             "replaces_id": str(live[p.user_id].id) if p.user_id in live else None,
             "warnings": ["REPLACES"] if p.user_id in live else []}
            for p in parsed if p.user_id and not p.error
        ]  # fmt: skip
        if not errors:
            try:
                await redis.set(_binding_key(actor, fy, kind, man), "1", ex=DRY_RUN_TTL_S)
            except Exception:  # noqa: BLE001 - fail closed: no record, no commit
                log.error("Form 16 dry-run record unavailable; failing closed")
                raise _unavailable() from None
        _audit(session, actor, "payroll.form16.dry_run", None, fy=fy, kind=kind, files=len(parsed))
        await _commit(session)
        return 200, {"fy": fy, "kind": kind, "manifest_sha256": man, "files": len(parsed), "matched": matched, "errors": errors}
    if man != manifest_sha256:
        raise AppError("IMPORT_FILE_CHANGED", "The files differ from the ones you checked; run the dry run again")
    if errors:
        raise AppError("IMPORT_INVALID", "Some files could not be matched; fix them and run the dry run again",
                       details=[{"field": f"files[{e['index']}]", "message": e["message"]} for e in errors])  # fmt: skip
    key = _binding_key(actor, fy, kind, man)
    try:
        found = await redis.getdel(key)  # L6: consumed before any work, so two parallel commits cannot both run
    except Exception:  # noqa: BLE001
        log.error("Form 16 dry-run record unavailable; failing closed")
        raise _unavailable() from None
    if found is None:
        raise AppError("IMPORT_NOT_CHECKED", "Run the dry run for these files first (it is valid for 15 minutes)")
    batch_id = uuid.uuid4()
    stored: list[File] = []
    try:
        second = bool(await ps.current_value(session, ps.FORM16_SECOND_KEY))
        uids = [p.user_id for p in parsed if p.user_id]
        old = await _live(session, fy, kind, uids, lock=True)
        now = utcnow()
        for doc in old.values():
            doc.status, doc.withdrawn_by, doc.withdrawn_at, doc.withdraw_reason = (
                "withdrawn",
                actor.id,
                now,
                "Replaced by a newer upload",
            )
            doc.updated_by = actor.id
            doc.version += 1
        if old:
            await session.flush()  # frees the one-live-document-per-(employee, FY, kind) unique index
            for doc in old.values():
                _event(session, doc, "withdraw", actor.id, reason="replaced")
        created: list[Form16Document] = []
        for p in parsed:
            assert p.data is not None and p.user_id is not None  # noqa: S101 - errors were refused above
            f = await files_svc.store_blob(session, actor.id, "form16", p.data, files_svc.PDF)
            stored.append(f)
            doc = Form16Document(
                user_id=p.user_id, fy=fy, kind=kind, file_id=f.id, sha256=p.sha, upload_batch_id=batch_id, status="uploaded",
                uploaded_by=actor.id, second_person=second, version=1, updated_by=actor.id,
            )  # fmt: skip
            session.add(doc)
            created.append(doc)
        await session.flush()
        for doc in created:
            _event(session, doc, "create", actor.id)
        _audit(
            session,
            actor,
            "payroll.form16.commit",
            batch_id,
            fy=fy,
            kind=kind,
            files=len(parsed),
            created=len(created),
            replaced=len(old),
        )
        await _queue_ready_push(session, actor, len(created), batch_id)
        await _commit(session)
    except BaseException:
        for f in stored:
            await files_svc.delete_blob(f)
        try:
            await redis.set(key, "1", ex=DRY_RUN_TTL_S)  # the commit failed: the reviewed dry run stays usable
        except Exception:  # noqa: BLE001
            log.warning("Form 16 dry-run record could not be restored")
        raise
    return 201, {"upload_batch_id": str(batch_id), "created": len(created), "replaced": len(old)}


async def _queue_ready_push(session: AsyncSession, actor: Principal, n: int, batch_id: uuid.UUID) -> None:
    ids = [u for u in await tr.holders(session, "payroll.approve") if u != actor.id][:20]

    async def _send() -> None:
        await notifications.notify(
            session, ids, type="payroll_approval", entity_id=batch_id, title="Approval needed",
            body=f"{n} Form 16 documents ready to publish", extra={"kind": "form16"},
        )  # fmt: skip

    if ids:
        pc.after_commit(session, _send)


# ============================================================================================ admin reads
async def list_documents(
    session: AsyncSession, actor: Principal, *, fy: str | None, kind: str | None, status: str | None, q: str | None, page: PageParams
) -> dict[str, Any]:  # fmt: skip
    cond: list[Any] = []
    if fy:
        cond.append(Form16Document.fy == tr.check_fy(fy))
    if kind:
        cond.append(Form16Document.kind == check_kind(kind))
    if status:
        cond.append(Form16Document.status == status)
    if q:
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cond.append(
            Form16Document.user_id.in_(
                select(User.id).where(
                    func.lower(User.full_name).like(func.lower(like), escape="\\") | User.employee_id.ilike(like, escape="\\")
                )
            )
        )
    total = (await session.execute(select(func.count()).select_from(Form16Document).where(*cond))).scalar_one()
    rows = list(
        (await session.execute(select(Form16Document).where(*cond).order_by(Form16Document.created_at.desc(), Form16Document.id).offset(page.offset).limit(page.page_size)))
        .scalars()
    )  # fmt: skip
    ppl = await tr.people(session, {x for d in rows for x in (d.user_id, d.uploaded_by, d.published_by)})
    items = [document_out(d, ppl) for d in rows]
    await pc.audit_read(session, actor, "payroll.form16.list", ENTITY, count=len(items))
    return make_page(items, page, total)


def _filename(doc: Form16Document, employee_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "", employee_id) or "employee"
    return f"{KIND_SLUG[doc.kind]}-{safe}-{doc.fy}.pdf"


async def admin_file(session: AsyncSession, actor: Principal, doc_id: uuid.UUID) -> tuple[bytes, str]:
    doc = await session.get(Form16Document, doc_id)
    if doc is None:
        raise AppError("NOT_FOUND", "Document not found")
    user = await session.get(User, doc.user_id)
    f = await session.get(File, doc.file_id)
    if f is None:
        raise AppError("FILE_UNAVAILABLE", "The document file is not available")
    data = await files_svc.load_bytes(f)
    _audit(session, actor, "payroll.form16.download", doc.id, fy=doc.fy, kind=doc.kind)
    await _commit(session)
    return data, _filename(doc, user.employee_id if user else "")


# ============================================================================================ publish / withdraw
async def publish(session: AsyncSession, redis: Redis, actor: Principal, body: Form16Publish) -> dict[str, Any]:
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.form16.publish")
    wanted: dict[uuid.UUID, str] = {}
    for d in body.documents:
        wanted.setdefault(d.id, d.sha256)
    docs = {
        x.id: x
        for x in (
            await session.execute(
                select(Form16Document).where(Form16Document.id.in_(list(wanted))).with_for_update().execution_options(populate_existing=True)
            )
        ).scalars()
    }  # fmt: skip
    failed: list[dict[str, str]] = []
    done: list[Form16Document] = []
    now = utcnow()
    for doc_id, sha in wanted.items():
        doc = docs.get(doc_id)
        if doc is None or doc.status != "uploaded" or doc.fy != body.fy:
            failed.append({"id": str(doc_id), "reason": "NOT_UPLOADED"})
        elif doc.user_id == actor.id:
            failed.append({"id": str(doc_id), "reason": "FORBIDDEN"})
        elif doc.sha256 != sha:
            failed.append({"id": str(doc_id), "reason": "SHA_MISMATCH"})
        elif doc.second_person and doc.uploaded_by == actor.id:
            failed.append({"id": str(doc_id), "reason": "SECOND_PERSON_REQUIRED"})
        else:
            doc.status, doc.published_by, doc.published_at, doc.updated_by = "published", actor.id, now, actor.id
            doc.version += 1
            done.append(doc)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        text_ = str(getattr(exc, "orig", exc))
        if "ck_form16_documents_second_person" in text_ or "ck_form16_documents_not_own" in text_:
            raise AppError("SECOND_PERSON_REQUIRED", "A different person must publish these documents") from None
        raise pc.map_integrity(exc) from None
    for doc in done:
        _event(session, doc, "approve", actor.id, uploaded_by=str(doc.uploaded_by))
    _audit(session, actor, "payroll.form16.publish", None, fy=body.fy, published=len(done), failed=len(failed),
           checker_id=str(actor.id))  # fmt: skip
    first_by_user: dict[uuid.UUID, uuid.UUID] = {}
    for doc in done:
        first_by_user.setdefault(doc.user_id, doc.id)
    fy = body.fy

    async def _send() -> None:
        for uid, did in first_by_user.items():  # coalesced: one push per employee
            await notifications.notify(
                session, [uid], type="form16_available", entity_id=did, title="Form 16 available",
                body=f"Your Form 16 for FY {fy} is ready to download.",
            )  # fmt: skip

    if first_by_user:
        pc.after_commit(session, _send)
    await _commit(session)
    return {"published": len(done), "failed": failed}


async def withdraw(
    session: AsyncSession, redis: Redis, actor: Principal, doc_id: uuid.UUID, body: Form16Withdraw
) -> dict[str, Any]:
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.form16.withdraw")
    doc = (
        await session.execute(
            select(Form16Document).where(Form16Document.id == doc_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise AppError("NOT_FOUND", "Document not found")
    await pc.forbid_self(session, actor, doc.user_id, entity_type=ENTITY, entity_id=doc_id)
    if doc.status not in ("uploaded", "published"):
        raise AppError("CONFLICT", "The document has already been withdrawn")
    doc.status, doc.withdrawn_by, doc.withdrawn_at, doc.withdraw_reason, doc.updated_by = (
        "withdrawn",
        actor.id,
        utcnow(),
        body.reason,
        actor.id,
    )
    doc.version += 1
    await session.flush()
    _event(session, doc, "withdraw", actor.id, reason=body.reason)
    _audit(session, actor, "payroll.form16.withdraw", doc.id, fy=doc.fy, kind=doc.kind)
    await _commit(session)
    ppl = await tr.people(session, {doc.user_id, doc.uploaded_by, doc.published_by})
    return document_out(doc, ppl)


# ============================================================================================ employee
async def list_own(session: AsyncSession, actor: Principal) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Form16Document).where(Form16Document.user_id == actor.id, Form16Document.status == "published")
            .order_by(Form16Document.fy.desc(), Form16Document.kind, Form16Document.id)
        )
    ).scalars()  # fmt: skip
    return {"items": [{"id": str(d.id), "fy": d.fy, "kind": d.kind, "published_at": pc.iso(d.published_at)} for d in rows]}


async def own_file(session: AsyncSession, actor: Principal, doc_id: uuid.UUID) -> tuple[bytes, str]:
    doc = await session.get(Form16Document, doc_id)
    if doc is None or doc.user_id != actor.id or doc.status != "published":
        raise await pc.deny(session, actor, entity_type=ENTITY, entity_id=doc_id, reason="not_owner_or_unpublished", status=404)
    f = await session.get(File, doc.file_id)
    if f is None:
        raise AppError("FILE_UNAVAILABLE", "The document file is not available")
    data = await files_svc.load_bytes(f)
    _audit(session, actor, "payroll.form16.view_own", doc.id, fy=doc.fy, kind=doc.kind)
    await _commit(session)
    return data, _filename(doc, actor.employee_id)
