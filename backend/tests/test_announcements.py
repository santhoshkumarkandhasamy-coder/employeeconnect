"""Announcements: visibility matrix, read idempotency, unread counts, admin RBAC, stats, push payload, audit."""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Announcement, AnnouncementRead, AuditLog, Department, User
from app.services import notifications
from tests.conftest import USERS, bearer


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(root))


@pytest.fixture
async def H(tokens_for):
    return {r: bearer(await tokens_for(r)) for r in USERS}


@pytest.fixture
async def ops(db, seeded):
    """A second department; the manager moves into it (employee stays in General)."""
    d = Department(name="Ops", code="OPS")
    db.add(d)
    await db.flush()
    mgr = await db.get(User, seeded["users"]["manager"])
    mgr.department_id = d.id
    await db.commit()
    return d.id


def body(**kw):
    return {"title": "Office closed", "body": "Plain text body", "priority": "normal", "target_all": True, **kw}


async def create(client, H, **kw):
    r = await client.post("/api/admin/announcements", headers=H["hr"], json=body(**kw))
    assert r.status_code == 201, r.text
    return r.json()


async def test_target_all_visible_and_shape(client, H):
    a = await create(client, H)
    assert a["read_count"] == 0 and a["target_count"] == 4 and a["status"] == "published"
    r = await client.get("/api/announcements", headers=H["employee"])
    assert r.status_code == 200
    j = r.json()
    assert (j["page"], j["page_size"], j["total"]) == (1, 20, 1)
    item = j["items"][0]
    assert item["id"] == a["id"] and item["is_read"] is False and item["created_by"]["employee_id"] == "HR001"
    assert {"image_url", "pdf_url", "published_at", "expires_at", "updated_by", "updated_at"} <= item.keys()
    assert "target_all" not in item and "department_ids" not in item  # targeting is admin-only (review 07 L1)
    assert item["updated_by"] is None
    assert "read_count" not in item  # employees never see stats


async def test_department_visibility_matrix(client, H, ops, seeded):
    a = await create(client, H, target_all=False, department_ids=[str(ops)])
    # manager is in Ops, employee in General
    assert (await client.get("/api/announcements", headers=H["manager"])).json()["total"] == 1
    assert (await client.get("/api/announcements", headers=H["employee"])).json()["total"] == 0
    assert (await client.get(f"/api/announcements/{a['id']}", headers=H["manager"])).status_code == 200
    # untargeted -> 404 (not 403) for detail and read
    assert (await client.get(f"/api/announcements/{a['id']}", headers=H["employee"])).status_code == 404
    assert (await client.post(f"/api/announcements/{a['id']}/read", headers=H["employee"])).status_code == 404
    assert (await client.get("/api/announcements/unread-count", headers=H["employee"])).json() == {"unread_count": 0}
    assert a["target_count"] == 1


async def test_user_without_department_sees_only_target_all(client, H, db, seeded):
    u = await db.get(User, seeded["users"]["employee"])
    u.department_id = None
    await db.commit()
    await create(client, H)
    await create(client, H, target_all=False, department_ids=[str(seeded["dept"])])
    assert (await client.get("/api/announcements", headers=H["employee"])).json()["total"] == 1


async def test_expired_and_deleted_hidden_from_employees_but_expired_listed_for_admin(client, H, db):
    a = await create(client, H, expires_at=(dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat())
    row = await db.get(Announcement, uuid.UUID(a["id"]))
    row.published_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    await db.commit()
    assert (await client.get("/api/announcements", headers=H["employee"])).json()["total"] == 0
    assert (await client.get(f"/api/announcements/{a['id']}", headers=H["employee"])).status_code == 404
    adm = (await client.get("/api/admin/announcements", headers=H["hr"])).json()
    assert adm["total"] == 1 and adm["items"][0]["status"] == "expired"
    assert (await client.delete(f"/api/admin/announcements/{a['id']}", headers=H["hr"])).status_code == 204
    assert (await client.get("/api/admin/announcements", headers=H["hr"])).json()["total"] == 0


async def test_read_is_idempotent_and_unread_count_and_filter(client, H):
    a = await create(client, H)
    b = await create(client, H, title="Second")
    uc = lambda: client.get("/api/announcements/unread-count", headers=H["employee"])  # noqa: E731
    assert (await uc()).json() == {"unread_count": 2}
    for _ in range(3):
        assert (await client.post(f"/api/announcements/{a['id']}/read", headers=H["employee"])).status_code == 204
    assert (await uc()).json() == {"unread_count": 1}
    unread = (await client.get("/api/announcements?unread=true", headers=H["employee"])).json()
    assert [i["id"] for i in unread["items"]] == [b["id"]]
    read = (await client.get("/api/announcements?unread=false", headers=H["employee"])).json()
    assert [i["id"] for i in read["items"]] == [a["id"]] and read["items"][0]["is_read"] is True
    # detail alone does not mark as read
    await client.get(f"/api/announcements/{b['id']}", headers=H["employee"])
    assert (await uc()).json() == {"unread_count": 1}


async def test_admin_stats_and_reads_list(client, H):
    a = await create(client, H)
    await client.post(f"/api/announcements/{a['id']}/read", headers=H["employee"])
    await client.post(f"/api/announcements/{a['id']}/read", headers=H["manager"])
    r = await client.get(f"/api/admin/announcements/{a['id']}/reads", headers=H["hr"])
    j = r.json()
    assert j["read_count"] == 2 and j["target_count"] == 4 and j["total"] == 2
    assert {i["user"]["employee_id"] for i in j["items"]} == {"EMP001", "MGR001"}
    assert all(i["read_at"].endswith("Z") for i in j["items"])
    lst = (await client.get("/api/admin/announcements", headers=H["hr"])).json()["items"][0]
    assert (lst["read_count"], lst["target_count"]) == (2, 4)


async def test_changing_targets_keeps_reads(client, H, ops, db):
    a = await create(client, H)
    await client.post(f"/api/announcements/{a['id']}/read", headers=H["employee"])
    r = await client.put(
        f"/api/admin/announcements/{a['id']}", headers=H["hr"], json=body(target_all=False, department_ids=[str(ops)])
    )
    assert r.status_code == 200 and r.json()["department_ids"] == [str(ops)]
    assert len((await db.execute(select(AnnouncementRead))).scalars().all()) == 1
    # employee no longer targeted: hidden, and no longer counted
    assert (await client.get("/api/announcements", headers=H["employee"])).json()["total"] == 0
    assert (await client.get(f"/api/admin/announcements/{a['id']}/reads", headers=H["hr"])).json()["read_count"] == 0


async def test_rbac_admin_endpoints(client, H):
    a = await create(client, H)
    for who in ("employee", "manager"):
        assert (await client.get("/api/admin/announcements", headers=H[who])).status_code == 403
        assert (await client.post("/api/admin/announcements", headers=H[who], json=body())).status_code == 403
        assert (await client.put(f"/api/admin/announcements/{a['id']}", headers=H[who], json=body())).status_code == 403
        assert (await client.delete(f"/api/admin/announcements/{a['id']}", headers=H[who])).status_code == 403
        assert (await client.get(f"/api/admin/announcements/{a['id']}/reads", headers=H[who])).status_code == 403
    assert (await client.get("/api/announcements")).status_code == 401
    assert (await client.post("/api/admin/announcements", headers=H["admin"], json=body())).status_code == 201


async def test_validation(client, H):
    post = lambda **kw: client.post("/api/admin/announcements", headers=H["hr"], json=body(**kw))  # noqa: E731
    assert (await post(target_all=False, department_ids=[])).status_code == 422
    assert (await post(target_all=False, department_ids=[str(uuid.uuid4())])).status_code == 422
    assert (await post(title="x" * 151)).status_code == 422
    assert (await post(body="x" * 5001)).status_code == 422
    assert (await post(priority="loud")).status_code == 422
    assert (await post(expires_at=(dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat())).status_code == 422
    assert (await post(expires_at="2999-01-01T00:00:00")).status_code == 422  # naive datetime
    assert (await post(image_file_id=str(uuid.uuid4()))).status_code == 422
    assert (await client.post("/api/admin/announcements", headers=H["hr"], json={**body(), "evil": 1})).status_code == 422


async def test_markup_stripped_from_text(client, H):
    a = await create(client, H, title="<b>Hi</b> all", body="Line<script>alert(1)</script> one\n\n\n\nTwo")
    assert a["title"] == "Hi all" and a["body"] == "Line one\n\nTwo"


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (20, 20), (1, 2, 3)).save(out, "JPEG")
    return out.getvalue()


async def test_attachments_and_file_access_follow_targeting(client, H, ops):
    up = await client.post(
        "/api/files", headers=H["hr"], data={"purpose": "announcement_image"}, files={"file": ("a.jpg", _jpeg(), "image/jpeg")}
    )
    fid = up.json()["id"]
    a = await create(client, H, image_file_id=fid, target_all=False, department_ids=[str(ops)])
    assert a["image_url"] == f"/api/files/{fid}" and a["pdf_url"] is None
    assert (await client.get(f"/api/files/{fid}", headers=H["manager"])).status_code == 200
    assert (await client.get(f"/api/files/{fid}", headers=H["employee"])).status_code == 404
    # a file with another purpose cannot be attached as an announcement image
    ph = await client.post(
        "/api/files", headers=H["hr"], data={"purpose": "selfie"}, files={"file": ("a.jpg", _jpeg(), "image/jpeg")}
    )
    r = await client.post("/api/admin/announcements", headers=H["hr"], json=body(image_file_id=ph.json()["id"]))
    assert r.status_code == 422


async def test_push_on_create_targets_only_audience_with_payload(client, H, ops, seeded):
    notifications.OUTBOX.clear()
    await create(client, H, title="Fire drill", priority="urgent", target_all=False, department_ids=[str(ops)])
    assert len(notifications.OUTBOX) == 1
    entry = notifications.OUTBOX[0]
    assert entry["payload"]["type"] == "announcement" and entry["payload"]["title"] == "Fire drill"
    assert entry["user_ids"] == [str(seeded["users"]["manager"])]  # only the Ops member


async def test_update_pushes_only_on_renotify(client, H):
    a = await create(client, H)
    notifications.OUTBOX.clear()
    await client.put(f"/api/admin/announcements/{a['id']}", headers=H["hr"], json=body(title="Edited"))
    assert not notifications.OUTBOX
    await client.put(f"/api/admin/announcements/{a['id']}", headers=H["hr"], json=body(title="Edited", renotify=True))
    assert len(notifications.OUTBOX) == 1


async def test_push_skips_announcement_deleted_in_flight(client, H, db, sessionmaker_):
    from app.services import announcements as svc

    a = await create(client, H)
    await client.delete(f"/api/admin/announcements/{a['id']}", headers=H["hr"])
    notifications.OUTBOX.clear()
    await svc.push(sessionmaker_, uuid.UUID(a["id"]))
    assert not notifications.OUTBOX


async def test_admin_actions_are_audited_without_body_text(client, H, db):
    a = await create(client, H, body="secret-ish text")
    await client.put(f"/api/admin/announcements/{a['id']}", headers=H["hr"], json=body(title="T2"))
    await client.delete(f"/api/admin/announcements/{a['id']}", headers=H["hr"])
    rows = (await db.execute(select(AuditLog).where(AuditLog.entity_type == "announcement"))).scalars().all()
    assert sorted(r.action for r in rows) == ["announcement.create", "announcement.delete", "announcement.update"]
    assert all("secret-ish" not in str(r.after) + str(r.before) for r in rows)


async def test_pagination_orders_newest_first(client, H):
    ids = [(await create(client, H, title=f"T{i}"))["id"] for i in range(3)]
    j = (await client.get("/api/announcements?page=1&page_size=2", headers=H["employee"])).json()
    assert j["total"] == 3 and len(j["items"]) == 2
    j2 = (await client.get("/api/announcements?page=2&page_size=2", headers=H["employee"])).json()
    assert len(j2["items"]) == 1
    assert {i["id"] for i in j["items"] + j2["items"]} == set(ids)
