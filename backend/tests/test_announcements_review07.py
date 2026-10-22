"""Review 07 hardening: push abuse limits, edited-by + containment, concurrency, expiry rule, rate limits, retention."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import pytest
from sqlalchemy import event, select

from app.core.config import get_settings
from app.core.redis import get_redis
from app.models import Announcement, AuditLog, File
from app.services import announcements as svc
from app.services import files as files_svc
from app.services import notifications
from tests import test_announcements as _base
from tests.conftest import USERS, bearer
from tests.test_announcements import _jpeg, body, create

storage = _base.storage  # autouse: private file storage dir
ops = _base.ops  # second department fixture


@pytest.fixture
async def H(tokens_for):
    return {r: bearer(await tokens_for(r)) for r in USERS}


async def put(client, H, who, a, **kw):
    return await client.put(f"/api/admin/announcements/{a['id']}", headers=H[who], json=body(**kw))


async def test_renotify_cooldown_per_announcement(client, H):
    a = await create(client, H)
    other = await create(client, H, title="Other")
    notifications.OUTBOX.clear()
    assert (await put(client, H, "hr", a, renotify=True)).status_code == 200
    r = await put(client, H, "hr", a, title="again", renotify=True)
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED" and int(r.headers["retry-after"]) > 0
    assert len(notifications.OUTBOX) == 1
    # the refused PUT changed nothing; edits WITHOUT renotify and other announcements are unaffected
    assert (await put(client, H, "hr", a, title="quiet")).status_code == 200
    assert (await put(client, H, "hr", other, renotify=True)).status_code == 200


async def test_daily_push_budget_per_admin(client, H, monkeypatch):
    monkeypatch.setattr(get_settings(), "announcement_push_budget_per_day", 3)
    first = await create(client, H)  # 1
    assert (await put(client, H, "hr", first, renotify=True)).status_code == 200  # 2
    await create(client, H)  # 3
    r = await client.post("/api/admin/announcements", headers=H["hr"], json=body())
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    # another admin has their own budget; edits that send no push are not counted
    assert (await client.post("/api/admin/announcements", headers=H["admin"], json=body())).status_code == 201
    assert (await put(client, H, "hr", first, title="quiet")).status_code == 200


async def test_push_gate_fails_closed_without_redis(client, H, app):
    class Broken:
        def pipeline(self):
            raise ConnectionError("down")

        async def ttl(self, key):
            raise ConnectionError("down")

    async def _broken():
        return Broken()

    app.dependency_overrides[get_redis] = _broken
    r = await client.post("/api/admin/announcements", headers=H["hr"], json=body())
    assert r.status_code == 503 and r.json()["error"]["code"] == "TEMPORARILY_UNAVAILABLE"


async def test_high_priority_push_only_once(client, H, redis):
    notifications.OUTBOX.clear()
    a = await create(client, H, priority="urgent")
    assert notifications.OUTBOX[-1]["high_priority"] is True
    assert (await put(client, H, "hr", a, priority="urgent", renotify=True)).status_code == 200
    assert notifications.OUTBOX[-1]["high_priority"] is False  # later re-sends are normal pushes
    # a normal announcement raised to urgent gets its one high-priority push
    b = await create(client, H, title="B")
    assert (await put(client, H, "hr", b, title="B", priority="urgent", renotify=True)).status_code == 200
    assert notifications.OUTBOX[-1]["high_priority"] is True


async def test_audit_has_renotify_and_body_hash_but_no_text(client, H, db):
    a = await create(client, H, body="first version")
    await put(client, H, "hr", a, body="second version", renotify=True)
    rows = {r.action: r for r in (await db.execute(select(AuditLog).where(AuditLog.entity_type == "announcement"))).scalars()}
    upd = rows["announcement.update"]
    assert upd.after["renotify"] is True
    assert upd.before["body_sha256"] == hashlib.sha256(b"first version").hexdigest() and upd.before["body_len"] == 13
    assert upd.after["body_sha256"] == hashlib.sha256(b"second version").hexdigest()
    assert "first version" not in str(upd.before) and "second version" not in str(upd.after)


async def test_edited_by_is_shown_and_version_increments(client, H):
    a = await create(client, H)
    assert a["version"] == 1 and a["updated_by"] is None
    r = await put(client, H, "admin", a, title="Admin edit")
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["updated_by"]["employee_id"] == "ADM001"
    item = (await client.get(f"/api/announcements/{a['id']}", headers=H["employee"])).json()
    assert item["created_by"]["employee_id"] == "HR001" and item["updated_by"]["employee_id"] == "ADM001"
    assert item["updated_at"].endswith("Z")


async def test_lower_privileged_editor_cannot_touch_admin_announcement(client, H, db):
    a = (await client.post("/api/admin/announcements", headers=H["admin"], json=body())).json()
    assert (await put(client, H, "hr", a, title="Rewritten")).status_code == 403
    assert (await client.delete(f"/api/admin/announcements/{a['id']}", headers=H["hr"])).status_code == 403
    row = await db.get(Announcement, uuid.UUID(a["id"]))
    await db.refresh(row)
    assert row.title == "Office closed" and row.deleted_at is None and row.updated_by is None
    # the other direction is fine, and HR can edit HR's own
    mine = await create(client, H)
    assert (await put(client, H, "admin", mine, title="by admin")).status_code == 200
    assert (await put(client, H, "hr", mine, title="by hr")).status_code == 200


async def test_if_match_optimistic_concurrency(client, H):
    a = await create(client, H)

    def put_if(v, **kw):
        return client.put(f"/api/admin/announcements/{a['id']}", headers={**H["hr"], "If-Match": v}, json=body(**kw))

    assert (await put_if('"1"', title="one")).json()["version"] == 2
    r = await put_if('"1"', title="stale")
    assert r.status_code == 409 and r.json()["error"]["code"] == "CONFLICT"
    assert (await put_if('"2"', title="two")).status_code == 200
    assert (await put_if("abc", title="x")).status_code == 422
    assert (await put_if("*", title="any")).status_code == 200
    adm = (await client.get("/api/admin/announcements", headers=H["hr"])).json()["items"][0]
    assert adm["title"] == "any" and adm["version"] == 4


async def test_expired_item_needs_explicit_new_expiry(client, H, db):
    a = await create(client, H, expires_at=(dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat())
    row = await db.get(Announcement, uuid.UUID(a["id"]))
    row.published_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    await db.commit()
    assert (await put(client, H, "hr", a, title="typo fix")).status_code == 422  # would silently republish it
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    assert (await put(client, H, "hr", a, expires_at=past)).status_code == 422
    ok = await put(client, H, "hr", a, expires_at=(dt.datetime.now(dt.UTC) + dt.timedelta(days=2)).isoformat())
    assert ok.status_code == 200 and ok.json()["status"] == "published"
    assert (await client.get(f"/api/announcements/{a['id']}", headers=H["employee"])).status_code == 200


async def test_employee_view_hides_targeting_admin_view_keeps_it(client, H, ops):
    a = await create(client, H, target_all=False, department_ids=[str(ops)])
    assert a["target_all"] is False and a["department_ids"] == [str(ops)]
    for item in (
        (await client.get(f"/api/announcements/{a['id']}", headers=H["manager"])).json(),
        (await client.get("/api/announcements", headers=H["manager"])).json()["items"][0],
    ):
        assert "target_all" not in item and "department_ids" not in item
    adm = (await client.get("/api/admin/announcements", headers=H["hr"])).json()["items"][0]
    assert adm["target_all"] is False and adm["department_ids"] == [str(ops)]


async def test_admin_list_stats_are_grouped_not_per_row(client, H, ops, db):
    everyone = await create(client, H, title="All")
    ops_only = await create(client, H, title="Ops", target_all=False, department_ids=[str(ops)])
    await client.post(f"/api/announcements/{everyone['id']}/read", headers=H["employee"])
    await client.post(f"/api/announcements/{everyone['id']}/read", headers=H["manager"])
    await client.post(f"/api/announcements/{ops_only['id']}/read", headers=H["manager"])

    def run_list(n_extra):
        counter = []
        sync = db.bind.sync_engine

        def tick(*_a):
            counter.append(1)

        event.listen(sync, "before_cursor_execute", tick)
        return counter, sync, tick

    counter, sync, tick = run_list(0)
    try:
        items = {i["title"]: i for i in (await client.get("/api/admin/announcements", headers=H["hr"])).json()["items"]}
    finally:
        event.remove(sync, "before_cursor_execute", tick)
    assert (items["All"]["read_count"], items["All"]["target_count"]) == (2, 4)
    assert (items["Ops"]["read_count"], items["Ops"]["target_count"]) == (1, 1)
    two_rows = len(counter)
    for i in range(3):
        await create(client, H, title=f"More{i}")
    counter, sync, tick = run_list(0)
    try:
        assert len((await client.get("/api/admin/announcements", headers=H["hr"])).json()["items"]) == 5
    finally:
        event.remove(sync, "before_cursor_execute", tick)
    assert len(counter) == two_rows  # same number of queries for 5 rows as for 2


async def test_read_and_admin_endpoints_are_rate_limited(client, H):
    a = await create(client, H)
    codes = [(await client.get("/api/admin/announcements", headers=H["hr"])).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30 and 429 in codes[30:]
    codes = [(await client.get(f"/api/admin/announcements/{a['id']}/reads", headers=H["admin"])).status_code for _ in range(32)]
    assert 429 in codes
    ids = [(await create(client, H, title=f"D{i}"))["id"] for i in range(11)]
    codes = [(await client.delete(f"/api/admin/announcements/{i}", headers=H["admin"])).status_code for i in ids]
    assert codes[:10] == [204] * 10 and codes[10] == 429
    codes = [(await client.get("/api/announcements/unread-count", headers=H["employee"])).status_code for _ in range(122)]
    assert codes[:120] == [200] * 120 and 429 in codes[120:]


async def test_deleted_announcement_files_are_unlinked_after_retention(client, H, db, sessionmaker_):
    up = await client.post(
        "/api/files", headers=H["hr"], data={"purpose": "announcement_image"}, files={"file": ("a.jpg", _jpeg(), "image/jpeg")}
    )
    fid = uuid.UUID(up.json()["id"])
    a = await create(client, H, image_file_id=str(fid))
    keep = await create(client, H, title="kept")
    await client.delete(f"/api/admin/announcements/{a['id']}", headers=H["hr"])
    async with sessionmaker_() as s:
        assert await svc.purge_deleted_files(s, days=30) == 0  # deleted just now
    row = await db.get(Announcement, uuid.UUID(a["id"]))
    row.deleted_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=31)
    await db.commit()
    async with sessionmaker_() as s:
        assert await svc.purge_deleted_files(s, days=0) == 0  # 0 = off
        assert await svc.purge_deleted_files(s, days=30) == 1
        assert await svc.purge_deleted_files(s, days=30) == 0
    await db.refresh(row)
    assert row.image_file_id is None
    assert (await db.get(Announcement, uuid.UUID(keep["id"]))).deleted_at is None
    # the orphan sweep now removes the blob
    (await db.get(File, fid)).created_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)
    await db.commit()
    assert await files_svc.cleanup_orphans(db) == 1


def test_nginx_has_a_profile_photo_location_with_6m_limit():
    from pathlib import Path

    conf = (Path(__file__).resolve().parents[2] / "deploy" / "nginx.conf").read_text(encoding="utf-8")
    block = conf[conf.index("location = /api/me/profile/photo") :]
    block = block[: block.index("proxy_pass")]
    for needle in ("client_max_body_size 6m", "limit_req zone=upload", "limit_conn uploadsrv", "limit_except POST"):
        assert needle in block
    assert conf.index("location = /api/me/profile/photo") < conf.index("location /api/ {")  # exact match, before the catch-all
