"""Files module: upload validation, authorisation per purpose, EXIF strip, encryption at rest, traversal, orphans."""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Announcement, AuditLog, EmployeeProfile, File, User
from app.services import files as svc
from tests.conftest import USERS, bearer

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(root))
    return root


def jpeg(size=(40, 30), exif=True) -> bytes:
    im = Image.new("RGB", size, (200, 10, 10))
    out = io.BytesIO()
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "SecretCamCo"  # Make
        im.save(out, "JPEG", exif=ex)
    else:
        im.save(out, "JPEG")
    return out.getvalue()


def png(size=(10, 10)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, (0, 0, 255, 128)).save(out, "PNG")
    return out.getvalue()


class _Resp:
    """POST /me/profile/photo answered like POST /files (status 201, id/url/content_type/size_bytes)."""

    def __init__(self, status_code, payload):
        self.status_code, self._payload = status_code, payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


async def up(client, headers, purpose, data, name="x.jpg", ctype="image/jpeg"):
    if purpose == "profile_photo":  # not accepted by POST /files any more (review 07 L4)
        r = await client.post("/api/me/profile/photo", headers=headers, files={"file": (name, data, ctype)})
        if r.status_code != 200:
            return r
        url = r.json()["photo_url"]
        got = await client.get(url, headers=headers)
        return _Resp(201, {"id": url.rsplit("/", 1)[1], "url": url, "content_type": got.headers["content-type"],
                           "size_bytes": len(got.content)})  # fmt: skip
    return await client.post("/api/files", headers=headers, data={"purpose": purpose}, files={"file": (name, data, ctype)})


@pytest.fixture
async def H(tokens_for):
    return {r: bearer(await tokens_for(r)) for r in USERS}


async def uid(db, role):
    return (await db.execute(select(User.id).where(User.employee_id == USERS[role]))).scalar_one()


# ------------------------------------------------------------------ auth / upload
async def test_requires_auth(client):
    r = await client.post("/api/files", data={"purpose": "selfie"}, files={"file": ("a.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 401
    assert (await client.get(f"/api/files/{uuid.uuid4()}")).status_code == 401


async def test_upload_and_download_roundtrip(client, H, storage):
    r = await up(client, H["employee"], "profile_photo", jpeg())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["url"] == f"/api/files/{body['id']}" and body["content_type"] == "image/jpeg"
    g = await client.get(body["url"], headers=H["employee"])
    assert g.status_code == 200
    assert g.headers["content-type"] == "image/jpeg"
    assert g.headers["x-content-type-options"] == "nosniff"
    assert g.headers["content-disposition"].startswith("inline;") and body["id"] in g.headers["content-disposition"]
    assert len(g.content) == body["size_bytes"]
    stored = list(storage.rglob("*.bin"))
    assert len(stored) == 1 and "x.jpg" not in str(stored[0])  # random server-side name


async def test_exif_is_stripped(client, H):
    assert b"SecretCamCo" in jpeg()
    r = await up(client, H["employee"], "profile_photo", jpeg())
    data = (await client.get(r.json()["url"], headers=H["employee"])).content
    assert b"SecretCamCo" not in data
    with Image.open(io.BytesIO(data)) as im:
        assert not im.getexif() and im.size == (40, 30)


async def test_trailing_payload_dropped(client, H):
    r = await up(client, H["employee"], "profile_photo", jpeg(exif=False) + b"<?php evil ?>")
    data = (await client.get(r.json()["url"], headers=H["employee"])).content
    assert b"<?php" not in data


async def test_png_accepted(client, H):
    r = await up(client, H["employee"], "profile_photo", png(), "p.png", "image/png")
    assert r.status_code == 201 and r.json()["content_type"] == "image/png"


async def test_client_mime_header_ignored(client, H):
    r = await up(client, H["employee"], "profile_photo", PDF, "a.jpg", "image/jpeg")
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"
    r = await up(client, H["admin"], "announcement_pdf", jpeg(), "a.pdf", "application/pdf")
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"


@pytest.mark.parametrize(
    "data", [b"GIF89a....", b"<html><script>1</script></html>", b"MZ\x90\x00", b"", b"\xff\xd8\xffnot really a jpeg"]
)
async def test_bad_content_rejected(client, H, data):
    r = await up(client, H["employee"], "selfie", data)
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"


async def test_oversize_rejected(client, H):
    big = jpeg(exif=False) + b"\0" * (5 * 1024 * 1024)
    r = await up(client, H["employee"], "selfie", big)
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TOO_LARGE"
    ok_pdf = PDF + b"\0" * (9 * 1024 * 1024)  # pdf limit is 10 MB
    assert (await up(client, H["employee"], "leave_attachment", ok_pdf, "a.pdf", "application/pdf")).status_code == 201
    r = await up(client, H["employee"], "leave_attachment", PDF + b"\0" * (10 * 1024 * 1024), "a.pdf", "application/pdf")
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"


async def test_global_cap_lowers_limits(client, H, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    r = await up(client, H["employee"], "leave_attachment", PDF + b"\0" * (2 * 1024 * 1024), "a.pdf", "application/pdf")
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"


async def test_unknown_purpose_and_missing_fields(client, H):
    r = await up(client, H["employee"], "avatar", jpeg())
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = await client.post("/api/files", headers=H["employee"], data={"purpose": "selfie"})
    assert r.status_code == 422


async def test_upload_rate_limit(client, H):
    codes = [(await up(client, H["employee"], "profile_photo", png(), "p.png", "image/png")).status_code for _ in range(22)]
    assert codes[:20] == [201] * 20 and 429 in codes[20:]


async def test_decompression_bomb_rejected(client, H):
    out = io.BytesIO()
    Image.new("1", (9000, 9000)).save(out, "PNG")  # 81 Mpx > 40 Mpx limit, tiny on the wire
    r = await up(client, H["employee"], "profile_photo", out.getvalue(), "b.png", "image/png")
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"


# ------------------------------------------------------------------ RBAC per purpose
@pytest.mark.parametrize("role,status", [("admin", 201), ("hr", 201), ("manager", 403), ("employee", 403)])
@pytest.mark.parametrize(
    "purpose,data,ct", [("announcement_image", jpeg(), "image/jpeg"), ("announcement_pdf", PDF, "application/pdf")]
)
async def test_announcement_upload_needs_permission(client, H, role, status, purpose, data, ct):
    assert (await up(client, H[role], purpose, data, "f", ct)).status_code == status


async def test_admin_upload_is_audited(client, H, db):
    await up(client, H["admin"], "announcement_pdf", PDF, "a.pdf", "application/pdf")
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "file.upload"))).scalars().all()
    assert len(rows) == 1 and rows[0].outcome == "success"


async def _selfie(client, H, role="employee"):
    return (await up(client, H[role], "selfie", jpeg())).json()["url"]


async def test_selfie_access_matrix(client, H, db):
    url = await _selfie(client, H)  # owner EMP001, whose manager is MGR001
    for role in ("employee", "manager", "hr", "admin"):
        assert (await client.get(url, headers=H[role])).status_code == 200, role
    murl = await _selfie(client, H, "manager")  # the manager's own selfie
    assert (await client.get(murl, headers=H["employee"])).status_code == 404
    assert (await client.get(murl, headers=H["hr"])).status_code == 200
    emp = await db.get(User, await uid(db, "employee"))
    emp.manager_id = None  # manager no longer has this employee in scope
    await db.commit()
    assert (await client.get(url, headers=H["manager"])).status_code == 404
    assert (await client.get(url, headers=H["employee"])).status_code == 200


async def test_cross_user_view_audited(client, H, db):
    url = await _selfie(client, H)
    await client.get(url, headers=H["employee"])  # owner: no audit row
    await client.get(url, headers=H["hr"])
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "file.view_other"))).scalars().all()
    assert len(rows) == 1


async def test_leave_attachment_matrix(client, H, db):
    r = await up(client, H["employee"], "leave_attachment", PDF, "m.pdf", "application/pdf")
    url = r.json()["url"]
    g = await client.get(url, headers=H["employee"])
    assert g.headers["content-type"] == "application/pdf" and g.headers["content-disposition"].startswith("attachment;")
    assert g.content == PDF
    # never submitted with a request: owner only (review 03 M4)
    assert (await client.get(url, headers=H["hr"])).status_code == 404
    assert (await client.get(url, headers=H["manager"])).status_code == 404
    # attached to the owner's request: visible to the people who can see that request
    from app.models import LeaveRequest, LeaveType

    lt = LeaveType(code="CL", name="Casual", annual_quota=12)
    db.add(lt)
    await db.flush()
    db.add(
        LeaveRequest(
            user_id=await uid(db, "employee"),
            leave_type_id=lt.id,
            from_date=dt.date(2026, 11, 2),
            to_date=dt.date(2026, 11, 2),
            days=1,
            reason="r",
            attachment_file_id=uuid.UUID(url.rsplit("/", 1)[1]),
            status="pending",
        )
    )
    await db.commit()
    assert (await client.get(url, headers=H["hr"])).status_code == 200
    assert (await client.get(url, headers=H["manager"])).status_code == 200  # direct report
    other = (await up(client, H["manager"], "leave_attachment", PDF, "m.pdf", "application/pdf")).json()["url"]
    assert (await client.get(other, headers=H["employee"])).status_code == 404


async def test_profile_photo_visible_to_all_authed(client, H):
    url = (await up(client, H["employee"], "profile_photo", jpeg())).json()["url"]
    for role in USERS:
        assert (await client.get(url, headers=H[role])).status_code == 200


async def test_announcement_file_visibility(client, H, db):
    url = (await up(client, H["admin"], "announcement_image", jpeg())).json()["url"]
    assert (await client.get(url, headers=H["employee"])).status_code == 404  # not attached to anything yet
    assert (await client.get(url, headers=H["hr"])).status_code == 200  # announcement.manage
    fid = uuid.UUID(url.rsplit("/", 1)[1])
    db.add(
        Announcement(
            title="t",
            body="b",
            image_file_id=fid,
            target_all=True,
            created_by=await uid(db, "admin"),
            published_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
        )
    )
    await db.commit()
    assert (await client.get(url, headers=H["employee"])).status_code == 200


async def test_unknown_and_malformed_ids(client, H):
    assert (await client.get(f"/api/files/{uuid.uuid4()}", headers=H["admin"])).status_code == 404
    assert (await client.get("/api/files/not-a-uuid", headers=H["admin"])).status_code == 422


# ------------------------------------------------------------------ storage safety
async def test_path_traversal_blocked(client, H, db, storage):
    secret = storage.parent / "outside.txt"
    secret.write_bytes(PDF)
    owner = await uid(db, "employee")
    recs = [
        File(owner_id=owner, purpose="leave_attachment", storage_path=p, content_type="application/pdf", size_bytes=len(PDF))
        for p in ("../outside.txt", str(secret), "..", "")
    ]
    db.add_all(recs)
    await db.commit()
    for rec in recs:
        assert (await client.get(f"/api/files/{rec.id}", headers=H["employee"])).status_code == 404


async def test_filename_and_purpose_cannot_steer_path(client, H, storage):
    r = await up(client, H["employee"], "profile_photo", jpeg(), "../../evil.jpg")
    assert r.status_code == 201
    assert all(p.is_relative_to(storage) for p in storage.rglob("*"))
    assert not (storage.parent / "evil.jpg").exists()


# ------------------------------------------------------------------ encryption at rest
async def test_selfie_encrypted_roundtrip(client, H, db, storage):
    r = await up(client, H["employee"], "selfie", jpeg())
    rec = await db.get(File, uuid.UUID(r.json()["id"]))
    assert rec.is_encrypted is True
    raw = (storage / rec.storage_path).read_bytes()
    assert not raw.startswith(b"\xff\xd8")  # ciphertext, not a JPEG
    g = await client.get(r.json()["url"], headers=H["employee"])
    assert g.content.startswith(b"\xff\xd8\xff") and g.headers["content-type"] == "image/jpeg"
    assert rec.size_bytes == len(g.content)


async def test_profile_photo_not_encrypted(client, H, db, storage):
    r = await up(client, H["employee"], "profile_photo", jpeg())
    rec = await db.get(File, uuid.UUID(r.json()["id"]))
    assert rec.is_encrypted is False and (storage / rec.storage_path).read_bytes().startswith(b"\xff\xd8")


async def test_missing_blob_is_404(client, H, db, storage):
    r = await up(client, H["employee"], "profile_photo", jpeg())
    rec = await db.get(File, uuid.UUID(r.json()["id"]))
    (storage / rec.storage_path).unlink()
    assert (await client.get(r.json()["url"], headers=H["employee"])).status_code == 404


# ------------------------------------------------------------------ resolved URLs
async def test_profile_photo_url_in_user_summary(client, H, db):
    from app.services.attendance import user_summary

    r = await up(client, H["employee"], "profile_photo", jpeg())
    prof = await db.get(EmployeeProfile, await uid(db, "employee"))
    prof.photo_file_id = uuid.UUID(r.json()["id"])
    await db.commit()
    photos = await svc.photo_file_ids(db, [prof.user_id])
    assert photos == {prof.user_id: prof.photo_file_id}
    u = await db.get(User, prof.user_id)
    assert user_summary(u, photos[u.id])["photo_url"] == r.json()["url"]
    assert user_summary(u)["photo_url"] is None


async def test_team_attendance_has_photo_url(client, H, db):
    r = await up(client, H["employee"], "profile_photo", jpeg())
    prof = await db.get(EmployeeProfile, await uid(db, "employee"))
    prof.photo_file_id = uuid.UUID(r.json()["id"])
    await db.commit()
    from app.models import Attendance, Shift

    shift = (await db.execute(select(Shift))).scalars().first()
    db.add(
        Attendance(
            user_id=prof.user_id,
            work_date=dt.date(2026, 10, 21),
            shift_id=shift.id,
            check_in_at=dt.datetime(2026, 10, 21, 4, 0, tzinfo=dt.UTC),
            status="present",
        )
    )
    await db.commit()
    t = await client.get("/api/attendance/team?date=2026-10-21", headers=H["hr"])
    assert t.status_code == 200, t.text
    assert t.json()["items"][0]["user"]["photo_url"] == r.json()["url"]


# ------------------------------------------------------------------ orphan cleanup
async def test_cleanup_orphans(client, H, db, storage):
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)
    kept = (await up(client, H["employee"], "profile_photo", jpeg())).json()["id"]
    orphan_old = (await up(client, H["employee"], "selfie", jpeg())).json()["id"]
    orphan_new = (await up(client, H["employee"], "selfie", jpeg())).json()["id"]
    for fid in (kept, orphan_old):
        (await db.get(File, uuid.UUID(fid))).created_at = old
    prof = await db.get(EmployeeProfile, await uid(db, "employee"))
    prof.photo_file_id = uuid.UUID(kept)
    await db.commit()
    path_old = storage / (await db.get(File, uuid.UUID(orphan_old))).storage_path
    assert await svc.cleanup_orphans(db, older_than_hours=24) == 1
    assert not path_old.exists()
    ids = set((await db.execute(select(File.id))).scalars())
    assert ids == {uuid.UUID(kept), uuid.UUID(orphan_new)}


# ------------------------------------------------------------------ security review 02 (M4, M5, L2, L5, L7)
async def test_pixel_cap_checked_before_decode(client, H, monkeypatch):
    monkeypatch.setattr(svc, "MAX_IMAGE_PIXELS", 1000)
    r = await up(client, H["employee"], "profile_photo", jpeg((40, 30)))  # 1200 px > 1000
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"
    assert (await up(client, H["employee"], "profile_photo", jpeg((30, 30)))).status_code == 201  # 900 px


def test_default_pixel_cap_is_lower_than_before():
    assert svc.MAX_IMAGE_PIXELS <= 16_000_000 < 40_000_000
    out = io.BytesIO()
    Image.new("1", (4100, 4100)).save(out, "PNG")  # 16.8 Mpx, tiny on the wire
    with pytest.raises(svc.AppError):
        svc._reencode(out.getvalue(), "image/png")


def test_reencode_concurrency_is_bounded(monkeypatch):
    import threading
    import time

    monkeypatch.setattr(get_settings(), "image_concurrency", 2)
    svc._IMAGE_SLOTS = None
    active = peak = 0
    lock = threading.Lock()

    def fake(data, ctype, max_side=None):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return b"x"

    monkeypatch.setattr(svc, "_reencode_unbounded", fake)
    threads = [threading.Thread(target=svc._reencode, args=(b"", "image/png")) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak == 2


@pytest.mark.parametrize(
    "evil",
    [
        b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile", b"/J#61vaScript",
        b"/OpenAction", b"/AA", b"/SubmitForm", b"/GoToR", b"/ImportData", b"/XFA", b"/Open#41ction",
    ],
)  # fmt: skip
async def test_pdf_with_active_content_rejected(client, H, evil):
    pdf = b"%PDF-1.4\n1 0 obj<< " + evil + b" (x) >>endobj\n%%EOF"
    r = await up(client, H["employee"], "leave_attachment", pdf, "a.pdf", "application/pdf")
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"
    assert (await up(client, H["employee"], "leave_attachment", PDF, "a.pdf", "application/pdf")).status_code == 201


async def test_storage_permissions_are_private(client, H, db, storage):
    import os
    import stat

    r = await up(client, H["employee"], "selfie", jpeg())
    rec = await db.get(File, uuid.UUID(r.json()["id"]))
    p = storage / rec.storage_path
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


async def test_undecryptable_blob_is_clean_503_not_500(client, H, db, storage, monkeypatch):
    r = await up(client, H["employee"], "selfie", jpeg())
    from cryptography.fernet import Fernet

    from app.core import crypto

    monkeypatch.setenv("DATA_ENCRYPTION_KEYS", Fernet.generate_key().decode())  # "forgot" the old key
    crypto.reset_crypto_cache()
    try:
        g = await client.get(r.json()["url"], headers=H["employee"])
    finally:
        monkeypatch.undo()
        crypto.reset_crypto_cache()
    assert g.status_code == 503 and g.json()["error"]["code"] == "FILE_UNAVAILABLE"
    assert "Traceback" not in g.text and "InvalidToken" not in g.text


async def test_denied_file_read_is_audited(client, H, db):
    url = await _selfie(client, H, "manager")
    assert (await client.get(url, headers=H["employee"])).status_code == 404
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "file.access_denied"))).scalars().all()
    assert len(rows) == 1 and rows[0].outcome == "failure" and rows[0].after == {"purpose": "selfie"}
    await client.get(f"/api/files/{uuid.uuid4()}", headers=H["employee"])  # unknown id: not audited (random ids, noise)
    assert len((await db.execute(select(AuditLog).where(AuditLog.action == "file.access_denied"))).scalars().all()) == 1


async def test_upload_limiter_fails_closed_without_redis(client, H, app):
    from app.core.redis import get_redis

    class Broken:
        def pipeline(self):
            raise ConnectionError("down")

    async def _broken():
        return Broken()

    app.dependency_overrides[get_redis] = _broken
    r = await up(client, H["employee"], "profile_photo", png(), "p.png", "image/png")
    assert r.status_code == 503 and r.json()["error"]["code"] == "TEMPORARILY_UNAVAILABLE"
