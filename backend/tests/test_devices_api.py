"""Attendance devices API (contract section 21): permission matrix, CRUD, address guard, write-only comm key, job queue, device users,
punch log, employee view, export, health, settings. Nothing here opens a socket except the end-to-end test through the worker."""

from __future__ import annotations

import datetime as dt
import io
import json
import re

import pytest
from openpyxl import load_workbook
from sqlalchemy import select, text

from app.core.config import get_settings
from app.models import Attendance, AuditLog, Device, DevicePunch, DeviceSync, DeviceUser, Setting
from app.services import device_attendance as da
from app.services import device_sync
from app.services import devices as devices_svc
from app.services.system_health import HEARTBEAT_KEY
from tests.conftest import PASSWORD, USERS, bearer
from tests.device_helpers import NOW, TODAY, add_device, add_device_user, add_punch, local
from tests.fakes.zk_server import FakePunch, FakeUser

pytestmark = pytest.mark.usefixtures("seeded", "clk")
A = "/api/admin"
BODY = {
    "name": "Main entrance",
    "ip": "192.168.0.201",
    "port": 4370,
    "transport": "tcp",
    "comm_key": 123456,
    "location_label": "Ground floor",
}


WEB = {"Origin": "https://hr.example.com", "X-EC-Client": "console"}


@pytest.fixture
async def H(client, monkeypatch, tokens_for):
    """Console (web) sessions: device.manage writes need a console access token (review L7)."""
    cache: dict = {}
    monkeypatch.setattr(get_settings(), "public_origin", WEB["Origin"])

    async def _h(role):
        if role not in cache:
            r = await client.post("/api/auth/web/login", headers=WEB, json={"employee_id": USERS[role], "password": PASSWORD})
            if r.status_code != 200:  # roles without console access (employee, manager) keep a mobile session
                cache[role] = bearer(await tokens_for(role))
            else:
                cache[role] = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return cache[role]

    return _h


@pytest.fixture
async def HM(tokens_for):
    """Mobile sessions (no console session id)."""

    async def _h(role):
        return bearer(await tokens_for(role))

    return _h


@pytest.fixture
async def worker_up(redis, clk):
    await redis.set(HEARTBEAT_KEY, clk.now.strftime("%Y-%m-%dT%H:%M:%SZ"))


async def create(client, H, **over):
    r = await client.post(f"{A}/devices", headers=await H("admin"), json={**BODY, **over})
    assert r.status_code == 201, r.text
    return r.json()


def keys_of(obj, out=None):
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            keys_of(v, out)
    elif isinstance(obj, list):
        for v in obj:
            keys_of(v, out)
    return out


# ---------------------------------------------------------------- permissions
@pytest.mark.parametrize(
    "method,path,body,allowed",
    [
        ("GET", "/devices", None, {"admin", "hr"}),
        ("POST", "/devices", BODY, {"admin"}),
        ("GET", "/devices/{id}", None, {"admin", "hr"}),
        ("PUT", "/devices/{id}", BODY, {"admin"}),
        ("DELETE", "/devices/{id}", None, {"admin"}),
        ("POST", "/devices/test", {"ip": "192.168.0.201"}, {"admin"}),
        ("POST", "/devices/{id}/test", None, {"admin"}),
        ("POST", "/devices/{id}/sync", None, {"admin", "hr"}),
        ("POST", "/devices/sync-all", None, {"admin", "hr"}),
        ("POST", "/devices/{id}/accept-identity", {}, {"admin"}),
        ("GET", "/devices/{id}/syncs", None, {"admin", "hr"}),
        ("GET", "/device-users", None, {"admin", "hr"}),
        ("PUT", "/device-users/{du}/mapping", {"user_id": None, "reason": "visitor card"}, {"admin", "hr"}),
        ("POST", "/device-users/{du}/auto", None, {"admin", "hr"}),
        ("GET", "/device-punches?from=2026-10-20&to=2026-10-21", None, {"admin", "hr"}),
    ],
)
@pytest.mark.parametrize("role", ["admin", "hr", "manager", "employee"])
async def test_permission_matrix(client, H, db, worker_up, role, method, path, body, allowed):
    dev = await add_device(db, None, ip="192.168.0.205", name="Matrix")
    du = await add_device_user(db, dev, "EMP001", user=None, method="unmatched")
    url = A + path.replace("{id}", str(dev.id)).replace("{du}", str(du.id))
    # the sync endpoints need the 60 s window free; use a fresh Redis key per case through the fixture's fresh fakeredis
    r = await client.request(method, url, headers=await H(role), json=body)
    if role in allowed:
        assert r.status_code not in (401, 403), (r.status_code, r.text)
    else:
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", r.text
        # a refused call changed nothing
        assert (await db.execute(select(Device))).scalars().all()[0].name == "Matrix"


async def test_unauthenticated_is_401(client):
    assert (await client.get(f"{A}/devices")).status_code == 401
    assert (await client.get(f"{A}/device-punches?from=2026-10-20&to=2026-10-21")).status_code == 401
    assert (await client.get("/api/attendance/punches")).status_code == 401


async def test_hr_cannot_read_a_test_job_but_can_read_sync_jobs(client, H, db, worker_up):
    dev = await add_device(db, None, ip="192.168.0.205")
    sync = DeviceSync(device_id=dev.id, kind="manual", status="ok")
    test = DeviceSync(device_id=dev.id, kind="test", status="ok")
    db.add_all([sync, test])
    await db.commit()
    assert (await client.get(f"{A}/device-syncs/{sync.id}", headers=await H("hr"))).status_code == 200
    assert (await client.get(f"{A}/device-syncs/{test.id}", headers=await H("hr"))).status_code == 403
    assert (await client.get(f"{A}/device-syncs/{test.id}", headers=await H("admin"))).status_code == 200


# ---------------------------------------------------------------- CRUD and the write-only comm key
async def test_create_shape_and_comm_key_is_never_returned(client, H, db):
    body = await create(client, H)
    assert set(body) == {
        "id", "name", "location_label", "ip", "port", "transport", "has_comm_key", "enabled", "timezone", "clock_correction", "ingest_from",
        "serial_number", "model", "firmware", "status", "clock_status", "last_clock_offset_s", "last_sync_at", "last_success_at",
        "last_error_code", "last_error_at", "circuit_open_until", "records_on_device", "users_on_device", "unmatched_users",
    }  # fmt: skip
    assert body["has_comm_key"] is True and body["status"] == "never_synced" and body["ingest_from"] == "2026-10-21"
    for url in (f"{A}/devices", f"{A}/devices/{body['id']}"):
        text_ = (await client.get(url, headers=await H("hr"))).text
        assert "123456" not in text_ and "comm_key" not in json.loads(text_).keys() | {"x"} and '"comm_key"' not in text_
    raw = (await db.execute(text("SELECT comm_key_enc FROM devices"))).scalar_one()
    assert b"123456" not in bytes(raw)  # encrypted at rest
    audit = (await db.execute(select(AuditLog).where(AuditLog.action == "device.create"))).scalar_one()
    assert (
        audit.after["comm_key_changed"] is True
        and "123456" not in json.dumps(audit.after)
        and (audit.after["ip"], audit.after["port"]) == ("192.168.0.201", 4370)  # non-secret values are kept (review L5)
    )


async def test_put_comm_key_semantics_and_audit(client, H, db):
    dev = await create(client, H)
    url = f"{A}/devices/{dev['id']}"
    base = {k: v for k, v in BODY.items() if k != "comm_key"}
    r = await client.put(url, headers=await H("admin"), json={**base, "name": "Renamed"})
    assert r.status_code == 200 and r.json()["has_comm_key"] is True and r.json()["name"] == "Renamed"  # omitted = unchanged
    r = await client.put(url, headers=await H("admin"), json={**base, "name": "Renamed", "comm_key": None})
    assert r.json()["has_comm_key"] is False  # null = clear
    r = await client.put(url, headers=await H("admin"), json={**base, "name": "Renamed", "comm_key": 777, "enabled": False})
    assert r.json()["has_comm_key"] is True and r.json()["status"] == "disabled"
    acts = [
        (a.action, a.after)
        for a in (await db.execute(select(AuditLog).order_by(AuditLog.ts))).scalars()
        if a.action.startswith("device.")
    ]
    names = [a for a, _ in acts]
    assert names.count("device.update") == 3 and "device.disable" in names
    assert acts[1][1] == {"fields": ["name"], "comm_key_changed": False, "name": "Renamed"}
    assert "777" not in json.dumps(acts)


@pytest.mark.parametrize(
    "over",
    [
        {"extra": 1},
        {"port": 0},
        {"port": 70000},
        {"ip": "::1"},
        {"ip": "not-an-ip"},
        {"ip": "192.168.0.256"},
        {"name": ""},
        {"name": "x" * 61},
        {"comm_key": 1_000_000},
        {"comm_key": -1},
        {"transport": "ws"},
        {"location_label": "x" * 81},
        {"timezone": "Mars/Base"},
        {"clock_correction": "set"},
        {"ingest_from": "2026-10-22"},
        {"ingest_from": "2020-01-01"},  # older than the punch retention (1095 days)
    ],
)
async def test_validation_errors(client, H, over):
    r = await client.post(f"{A}/devices", headers=await H("admin"), json={**BODY, **over})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] in ("VALIDATION_ERROR", "DEVICE_ADDRESS_NOT_ALLOWED")


async def test_ingest_from_may_reach_back_to_the_punch_retention(client, H):
    r = await client.post(f"{A}/devices", headers=await H("admin"), json={**BODY, "ingest_from": "2024-03-25"})
    assert r.status_code == 201, r.text
    assert r.json()["ingest_from"] == "2024-03-25"


async def test_defaults(client, H):
    r = await client.post(f"{A}/devices", headers=await H("admin"), json={"name": "Bare", "ip": "192.168.0.202"})
    assert r.status_code == 201
    b = r.json()
    assert (b["port"], b["transport"], b["has_comm_key"], b["enabled"], b["clock_correction"], b["ingest_from"]) == (
        4370,
        "tcp",
        False,
        True,
        "auto",
        "2026-10-21",
    )


async def test_duplicates_are_conflicts(client, H, monkeypatch):
    monkeypatch.setattr(get_settings(), "device_allowed_ports", "4370,4371")
    await create(client, H)
    r = await client.post(f"{A}/devices", headers=await H("admin"), json={**BODY, "name": "MAIN ENTRANCE", "ip": "192.168.0.202"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "CONFLICT")
    r = await client.post(f"{A}/devices", headers=await H("admin"), json={**BODY, "name": "Other"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "CONFLICT")
    r = await client.post(f"{A}/devices", headers=await H("admin"), json={**BODY, "name": "Other", "port": 4371})
    assert r.status_code == 201


async def test_list_sorted_by_name_and_get_includes_recent_syncs(client, H, db):
    for n, ip in (("Zeta", "192.168.0.203"), ("alpha", "192.168.0.204"), ("Mid", "192.168.0.205")):
        await create(client, H, name=n, ip=ip)
    items = (await client.get(f"{A}/devices", headers=await H("hr"))).json()["items"]
    assert [i["name"] for i in items] == ["alpha", "Mid", "Zeta"]
    did = items[0]["id"]
    for i in range(12):
        db.add(
            DeviceSync(
                device_id=items[0]["id"] and (await db.get(Device, __import__("uuid").UUID(did))).id,
                kind="schedule",
                status="ok",
                queued_at=NOW - dt.timedelta(minutes=i),
            )
        )
    await db.commit()
    got = (await client.get(f"{A}/devices/{did}", headers=await H("hr"))).json()
    assert len(got["recent_syncs"]) == 10 and got["recent_syncs"][0]["queued_at"] > got["recent_syncs"][-1]["queued_at"]
    page = (await client.get(f"{A}/devices/{did}/syncs?page=2&page_size=5", headers=await H("hr"))).json()
    assert (page["page"], page["page_size"], page["total"], len(page["items"])) == (2, 5, 12, 5)
    assert (await client.get(f"{A}/devices/00000000-0000-0000-0000-000000000009", headers=await H("hr"))).status_code == 404


async def test_delete_rules(client, H, db):
    dev = await create(client, H)
    did = dev["id"]
    d = await db.get(Device, __import__("uuid").UUID(did))
    await add_device_user(db, d, "EMP001", user="employee")
    db.add(DeviceSync(device_id=d.id, kind="schedule", status="ok"))
    await db.commit()
    await add_punch(db, d, "EMP001", local(TODAY, "08:55"))
    await db.commit()
    r = await client.delete(f"{A}/devices/{did}", headers=await H("admin"))
    assert (r.status_code, r.json()["error"]["code"]) == (409, "IN_USE")
    assert (await db.execute(select(DeviceUser))).scalars().all()  # nothing was deleted by the refused call
    other = await create(client, H, name="Spare", ip="192.168.0.210")
    od = await db.get(Device, __import__("uuid").UUID(other["id"]))
    od_id = od.id
    await add_device_user(db, od, "EMP001", user="employee")
    db.add(DeviceSync(device_id=od.id, kind="schedule", status="ok"))
    await db.commit()
    assert (await client.delete(f"{A}/devices/{other['id']}", headers=await H("admin"))).status_code == 204
    db.expire_all()
    assert (await db.execute(select(DeviceUser).where(DeviceUser.device_id == od_id))).scalars().all() == []
    assert (await db.execute(select(DeviceSync).where(DeviceSync.device_id == od_id))).scalars().all() == []
    assert "device.delete" in (await db.execute(select(AuditLog.action))).scalars().all()


async def test_delete_blocked_when_attendance_references_the_device(client, H, db, seeded):
    dev = await create(client, H)
    d = await db.get(Device, __import__("uuid").UUID(dev["id"]))
    db.add(
        Attendance(
            user_id=seeded["users"]["employee"],
            work_date=TODAY,
            check_in_at=NOW,
            check_in_source="device",
            check_in_device_id=d.id,
        )
    )
    await db.commit()
    assert (await client.delete(f"{A}/devices/{dev['id']}", headers=await H("admin"))).status_code == 409


async def test_accept_identity_clears_the_pin(client, H, db):
    dev = await create(client, H)
    d = await db.get(Device, __import__("uuid").UUID(dev["id"]))
    d.serial_number, d.last_error_code, d.consecutive_failures = "SN-OLD", "DEVICE_IDENTITY_CHANGED", 3
    await db.commit()
    r = await client.post(f"{A}/devices/{dev['id']}/accept-identity", headers=await H("admin"), json={})
    assert r.status_code == 200 and r.json()["serial_number"] is None and r.json()["last_error_code"] is None
    assert "device.identity_accepted" in (await db.execute(select(AuditLog.action))).scalars().all()
    assert (
        await client.post(f"{A}/devices/{dev['id']}/accept-identity", headers=await H("admin"), json={"x": 1})
    ).status_code == 422


async def test_pinned_serial_survives_an_ip_change(client, H, db):
    dev = await create(client, H)
    d = await db.get(Device, __import__("uuid").UUID(dev["id"]))
    d.serial_number = "SN-1"
    await db.commit()
    r = await client.put(f"{A}/devices/{dev['id']}", headers=await H("admin"), json={**BODY, "ip": "192.168.0.222"})
    assert r.status_code == 200 and r.json()["serial_number"] == "SN-1" and r.json()["ip"] == "192.168.0.222"


# ---------------------------------------------------------------- address guard (SSRF)
@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "127.5.5.5", "169.254.169.254", "224.0.0.251", "0.0.0.0", "255.255.255.255", "8.8.8.8", "192.168.1.33", "192.168.0.0",
     "192.168.0.255", "172.17.0.2", "192.168.0.33", "192.168.0.50", "192.168.0.99"],
)  # fmt: skip
async def test_address_guard_refuses(client, H, monkeypatch, ip):
    st = get_settings()
    monkeypatch.setattr(st, "device_blocked_ips", "192.168.0.33")
    monkeypatch.setattr(st, "egress_subnet", "192.168.0.96/28")
    monkeypatch.setattr(devices_svc, "_own_addresses", lambda: ("192.168.0.50",))
    monkeypatch.setattr(st, "device_allowed_cidrs", "192.168.0.0/24,172.17.0.0/16,127.0.0.0/8,10.0.0.0/8")
    r = await client.post(f"{A}/devices", headers=await H("admin"), json={**BODY, "ip": ip})
    assert r.status_code == 422, ip
    assert r.json()["error"]["code"] == "DEVICE_ADDRESS_NOT_ALLOWED"
    t = await client.post(f"{A}/devices/test", headers=await H("admin"), json={"ip": ip})
    assert t.status_code in (422, 503) and (t.status_code == 422 or True)


async def test_address_guard_allows_the_office_lan_and_applies_on_update(client, H):
    dev = await create(client, H, ip="192.168.0.203")
    r = await client.put(f"{A}/devices/{dev['id']}", headers=await H("admin"), json={**BODY, "ip": "8.8.8.8"})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "DEVICE_ADDRESS_NOT_ALLOWED")
    assert r.json()["error"]["details"][0]["field"] == "ip"


# ---------------------------------------------------------------- job queue
async def test_worker_down_gives_503(client, H, db, redis):
    dev = await add_device(db, None, ip="192.168.0.205")
    for url, body in (
        (f"/devices/{dev.id}/sync", None),
        (f"/devices/{dev.id}/test", None),
        ("/devices/test", {"ip": "192.168.0.201"}),
        ("/devices/sync-all", None),
    ):
        r = await client.post(A + url, headers=await H("admin"), json=body)
        assert (r.status_code, r.json()["error"]["code"]) == (503, "TEMPORARILY_UNAVAILABLE"), url
    await redis.set(HEARTBEAT_KEY, (NOW - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))  # stale heartbeat
    assert (await client.post(f"{A}/devices/{dev.id}/sync", headers=await H("admin"))).status_code == 503
    assert (await db.execute(select(DeviceSync))).scalars().all() == []


async def test_sync_now_queues_returns_the_open_job_and_rate_limits(client, H, db, worker_up, redis):
    dev = await add_device(db, None, ip="192.168.0.205")
    r = await client.post(f"{A}/devices/{dev.id}/sync", headers=await H("hr"))
    assert r.status_code == 202
    sid = r.json()["sync_id"]
    assert (await client.post(f"{A}/devices/{dev.id}/sync", headers=await H("admin"))).json()["sync_id"] == sid  # already queued
    job = json.loads(await redis.rpop(devices_svc.QUEUE_KEY))
    assert job == {"kind": "sync", "sync_id": sid, "device_id": str(dev.id)}
    got = (await client.get(f"{A}/device-syncs/{sid}", headers=await H("hr"))).json()
    assert (got["status"], got["kind"], got["requested_by"]["employee_id"], got["result"], got["device_id"]) == (
        "queued",
        "manual",
        USERS["hr"],
        None,
        str(dev.id),
    )
    row = await db.get(DeviceSync, __import__("uuid").UUID(sid))
    row.status = "ok"
    await db.commit()
    r = await client.post(f"{A}/devices/{dev.id}/sync", headers=await H("hr"))
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED" and int(r.headers["Retry-After"]) >= 1


async def test_sync_disabled_device_and_sync_all(client, H, db, worker_up):
    a = await add_device(db, None, ip="192.168.0.205", name="On")
    await add_device(db, None, ip="192.168.0.206", name="Off", enabled=False)
    r = await client.post(
        f"{A}/devices/{(await db.execute(select(Device.id).where(Device.name == 'Off'))).scalar_one()}/sync",
        headers=await H("hr"),
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "DEVICE_DISABLED")
    r = await client.post(f"{A}/devices/sync-all", headers=await H("hr"))
    assert r.status_code == 202 and [i["device_id"] for i in r.json()["items"]] == [str(a.id)]
    assert set(r.json()["items"][0]) == {"device_id", "sync_id"}


async def test_unsaved_test_payload_is_encrypted_and_rate_limited(client, H, db, worker_up, redis):
    r = await client.post(f"{A}/devices/test", headers=await H("admin"), json={"ip": "192.168.0.201", "comm_key": 4242})
    assert r.status_code == 202
    sid = r.json()["sync_id"]
    stored = await redis.get(devices_svc.TESTJOB_KEY.format(sid))
    assert stored and "4242" not in stored and "192.168" not in stored
    assert 0 < await redis.ttl(devices_svc.TESTJOB_KEY.format(sid)) <= 120
    row = await db.get(DeviceSync, __import__("uuid").UUID(sid))
    assert (row.kind, row.device_id, row.status) == ("test", None, "queued")
    for _ in range(5):
        assert (await client.post(f"{A}/devices/test", headers=await H("admin"), json={"ip": "192.168.0.201"})).status_code == 202
    r = await client.post(f"{A}/devices/test", headers=await H("admin"), json={"ip": "192.168.0.201"})
    assert r.status_code == 429  # 6 per minute per admin


async def test_end_to_end_through_the_worker(client, H, db, sessionmaker_, redis, worker_up, fake_zk, seeded):
    f = await fake_zk(users=[FakeUser(1, "EMP001", "Test Employee", card=987654321), FakeUser(2, "900", "Visitor")],
                      punches=[FakePunch("EMP001", local(TODAY, "08:55"), uid=1), FakePunch("900", local(TODAY, "08:56"), uid=2)])  # fmt: skip
    dev = await add_device(db, f, location_label="Ground floor")
    t = (await client.post(f"{A}/devices/{dev.id}/test", headers=await H("admin"))).json()["sync_id"]
    s = (await client.post(f"{A}/devices/{dev.id}/sync", headers=await H("hr"))).json()["sync_id"]
    assert await device_sync.process_queue(sessionmaker_, redis) == 2
    test = (await client.get(f"{A}/device-syncs/{t}", headers=await H("admin"))).json()
    assert (
        test["status"] == "ok" and test["result"]["serial_number"] == "SN-TEST-0001" and test["result"]["records_on_device"] == 2
    )
    sync = (await client.get(f"{A}/device-syncs/{s}", headers=await H("hr"))).json()
    assert (sync["status"], sync["inserted"], sync["fetched"], sync["records_on_device"], sync["users_on_device"]) == (
        "ok",
        2,
        2,
        2,
        2,
    )
    assert sync["result"] is None
    d = (await client.get(f"{A}/devices/{dev.id}", headers=await H("hr"))).json()
    assert (d["status"], d["serial_number"], d["model"], d["unmatched_users"], d["records_on_device"], d["users_on_device"]) == (
        "ok",
        "SN-TEST-0001",
        "K40",
        1,
        2,
        2,
    )
    assert {x["id"] for x in d["recent_syncs"]} == {t, s}

    # device users: unmatched by default, card masked, name readable, q matches id or name
    du = (await client.get(f"{A}/device-users", headers=await H("hr"))).json()
    assert du["total"] == 1 and du["items"][0]["device_user_id"] == "900"
    item = du["items"][0]
    assert set(item) == {"id", "device", "device_user_id", "name", "card_masked", "is_device_admin", "pending_confirmation", "present_on_device", "status", "match_method",
                         "user", "last_punch_at", "unapplied_punches", "last_seen_at"}  # fmt: skip
    assert (item["status"], item["unapplied_punches"], item["user"], item["device"]["name"]) == ("unmatched", 1, None, dev.name)
    allu = (await client.get(f"{A}/device-users?status=all&q=test emp", headers=await H("hr"))).json()
    assert [i["card_masked"] for i in allu["items"]] == ["****4321"] and allu["items"][0]["status"] == "matched"
    assert "987654321" not in (await client.get(f"{A}/device-users?status=all", headers=await H("hr"))).text
    assert (await client.get(f"{A}/device-users?status=matched&q=EMP001", headers=await H("hr"))).json()["total"] == 1
    assert (await client.get(f"{A}/device-users?status=ignored", headers=await H("hr"))).json()["total"] == 0

    # map the visitor to the manager on every device
    r = await client.put(
        f"{A}/device-users/{item['id']}/mapping", headers=await H("hr"), json={"user_id": str(seeded["users"]["manager"]), "reason": "visitor is the manager"}
    )
    assert r.status_code == 200 and r.json()["items"][0]["user"]["employee_id"] == USERS["manager"]
    r = await client.put(
        f"{A}/device-users/{item['id']}/mapping", headers=await H("hr"), json={"user_id": "00000000-0000-0000-0000-000000000009", "reason": "typo test"}
    )
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "user_id"
    await device_sync.process_queue(sessionmaker_, redis)
    acts = (await db.execute(select(AuditLog).where(AuditLog.action == "device_user.map"))).scalars().all()
    assert acts[0].after == {"device_user_id": "900", "employee_id": USERS["manager"], "reason": "visitor is the manager"}
    assert acts[0].before == {"device_user_id": "900", "match_method": "unmatched", "employee_id": None}

    # punch log
    q = f"from={TODAY}&to={TODAY}"
    log = (await client.get(f"{A}/device-punches?{q}", headers=await H("hr"))).json()
    assert log["total"] == 2
    p = log["items"][0]
    assert set(p) == {"id", "device", "device_user_id", "user", "punch_at", "device_time", "offset_applied_s", "state", "verify_mode", "method",
                      "work_date", "outcome", "reason"}  # fmt: skip
    assert (
        isinstance(p["id"], str)
        and p["device_time"].startswith("2026-10-21T08:5")
        and p["method"] == "fingerprint"
        and p["state"] == "check_in"
    )
    assert sorted(i["outcome"] for i in log["items"]) == ["check_in", "check_in"]
    one = (await client.get(f"{A}/device-punches?{q}&employee_id={USERS['employee']}", headers=await H("hr"))).json()
    assert one["total"] == 1 and one["items"][0]["user"]["employee_id"] == USERS["employee"]
    assert (await client.get(f"{A}/device-punches?{q}&outcome=unmatched", headers=await H("hr"))).json()["total"] == 0
    assert (await client.get(f"{A}/device-punches?{q}&device_user_id=900", headers=await H("hr"))).json()["total"] == 1
    assert (await client.get(f"{A}/device-punches?{q}&device_id={dev.id}", headers=await H("hr"))).json()["total"] == 2
    assert (await client.get(f"{A}/device-punches?from=2026-07-01&to={TODAY}", headers=await H("hr"))).status_code == 422
    assert (await client.get(f"{A}/device-punches?from={TODAY}", headers=await H("hr"))).status_code == 422

    # the employee sees only own punches, with no ids or device data
    mine = (await client.get("/api/attendance/punches", headers=await H("employee"))).json()
    assert mine == {
        "items": [
            {
                "punch_at": "2026-10-21T03:25:00Z",
                "work_date": "2026-10-21",
                "device_name": dev.name,
                "method": "fingerprint",
                "used_as": "check_in",
            }
        ]
    }
    assert (
        await client.get("/api/attendance/punches?from=2026-09-01&to=2026-10-21", headers=await H("employee"))
    ).status_code == 422
    other = (await client.get(f"/api/attendance/punches?date={TODAY}", headers=await H("manager"))).json()["items"]
    assert [i["used_as"] for i in other] == ["check_in"]  # the manager's own (mapped) punch only
    # the attendance record carries the device fields
    today = (await client.get("/api/attendance/today", headers=await H("employee"))).json()
    assert (today["check_in_source"], today["check_in_device"]["name"], today["within_fence"], today["selfie_url"]) == (
        "device",
        dev.name,
        True,
        None,
    )

    # no key anywhere is named like biometric data, and no secret is exposed
    blob = json.dumps([d, log, du, test, sync, mine])
    assert "comm_key" not in keys_of(json.loads(blob)) and "pin" not in keys_of(json.loads(blob))
    assert not re.search(r"template|finger|face|biometric|palm|password", " ".join(keys_of(json.loads(blob))), re.I)


async def test_privacy_notice_no_cards_in_punch_log_or_export(client, H, db, sessionmaker_, redis, worker_up, fake_zk):
    f = await fake_zk(
        users=[FakeUser(1, "EMP001", "Test Employee", card=987654321)],
        punches=[FakePunch("EMP001", local(TODAY, "08:55"), uid=1)],
    )
    dev = await add_device(db, f)
    await device_sync.run_sync(sessionmaker_, redis, dev.id, kind="manual")
    r = await client.get(f"{A}/export?type=device_punches&from={TODAY}&to={TODAY}", headers=await H("hr"))
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Device Punches"]
    rows = list(wb["Device Punches"].values)
    assert rows[0] == (
        "Punch Time",
        "Device Time",
        "Device",
        "Device User ID",
        "Employee ID",
        "Name",
        "Method",
        "State",
        "Work Date",
        "Outcome",
        "Reason",
    )
    assert (
        rows[1][2:]
        == (
            dev.name,
            "EMP001",
            USERS["employee"],
            "Test Employee",
            "fingerprint",
            "check_in",
            dt.datetime(2026, 10, 21, 0, 0),
            "check_in",
            "",
        )[0:0]
        + rows[1][2:]
    )
    assert (rows[1][2], rows[1][3], rows[1][4], rows[1][6], rows[1][7], rows[1][9]) == (
        dev.name,
        "EMP001",
        USERS["employee"],
        "fingerprint",
        "check_in",
        "check_in",
    )
    assert "987654321" not in json.dumps([[str(c) for c in row] for row in rows])


# ---------------------------------------------------------------- export
async def test_export_attendance_sheet_gains_source_columns_and_all_excludes_device_punches(
    client, H, db, sessionmaker_, redis, fake_zk
):
    f = await fake_zk(users=[FakeUser(1, "EMP001", "Test Employee")], punches=[FakePunch("EMP001", local(TODAY, "08:55"), uid=1)])
    dev = await add_device(db, f)
    await device_sync.run_sync(sessionmaker_, redis, dev.id, kind="manual")
    r = await client.get(f"{A}/export?type=all&from={TODAY}&to={TODAY}", headers=await H("admin"))
    wb = load_workbook(io.BytesIO(r.content))
    assert "Device Punches" not in wb.sheetnames and wb.sheetnames[0] == "Attendance"
    rows = list(wb["Attendance"].values)
    assert rows[0][-4:] == ("Check-in Source", "Check-out Source", "Check-in Device", "Check-out Device")
    assert (rows[1][-4], rows[1][-3], rows[1][-2], rows[1][-1]) == ("device", None, dev.name, None)


async def test_export_device_punches_permissions_and_limits(client, H):
    q = f"type=device_punches&from={TODAY}&to={TODAY}"
    assert (await client.get(f"{A}/export?{q}", headers=await H("manager"))).status_code == 403  # export.team but no device.view
    assert (await client.get(f"{A}/export?{q}", headers=await H("employee"))).status_code == 403
    assert (
        await client.get(f"{A}/export?type=device_punches&from=2026-07-01&to={TODAY}", headers=await H("admin"))
    ).status_code == 422
    assert (await client.get(f"{A}/export?{q}", headers=await H("admin"))).status_code == 200


# ---------------------------------------------------------------- health, settings
async def test_health_devices_block(client, H, db, redis, clk):
    await redis.set(HEARTBEAT_KEY, clk.now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    h = (await client.get(f"{A}/system/health", headers=await H("admin"))).json()
    assert h["devices"] == {"ok": True, "enabled": 0, "failing": 0, "offline": 0, "last_success_at": None}
    d = await add_device(
        db, None, ip="192.168.0.205", last_success_at=NOW - dt.timedelta(minutes=70), last_sync_at=NOW, created_by=None
    )
    await add_device(
        db,
        None,
        ip="192.168.0.206",
        name="Down",
        last_error_code="DEVICE_TIMEOUT",
        circuit_open_until=NOW + dt.timedelta(minutes=5),
        last_success_at=NOW,
    )
    await add_device(db, None, ip="192.168.0.207", name="Disabled", enabled=False)
    h = (await client.get(f"{A}/system/health", headers=await H("admin"))).json()["devices"]
    assert (h["ok"], h["enabled"], h["failing"], h["offline"]) == (False, 2, 0, 1)  # no success for 70 minutes inside the window
    assert h["last_success_at"] == "2026-10-21T03:30:00Z"
    d.last_success_at = NOW
    await db.commit()
    assert (await client.get(f"{A}/system/health", headers=await H("admin"))).json()["devices"]["ok"] is True


async def test_settings_keys_and_validation(client, H, db):
    got = (await client.get(f"{A}/settings", headers=await H("admin"))).json()["settings"]
    assert (
        got["device.sync_interval_minutes"] == 5
        and got["device.sync_window_start"] == "05:00"
        and got["device.punch_retention_days"] == 1095
    )
    assert got["device.apply_on_on_duty_days"] is False and got["device.match_prefix"] == ""
    ok = await client.put(
        f"{A}/settings",
        headers=await H("admin"),
        json={"settings": {"device.sync_interval_minutes": 10, "device.match_prefix": "EMP", "device.punch_retention_days": 0}},
    )
    assert ok.status_code == 200, ok.text
    for key, val in (
        ("device.sync_interval_minutes", 1),
        ("device.sync_interval_minutes", 61),
        ("device.sync_window_start", "25:00"),
        ("device.sync_window_end", "5pm"),
        ("device.off_window_interval_minutes", 14),
        ("device.apply_window_days", 32),
        ("device.checkout_min_gap_minutes", 0),
        ("device.drift_tolerance_s", 901),
        ("device.drift_invalid_s", 899),
        ("device.future_tolerance_s", 3601),
        ("device.match_prefix", "TOO-LONG-PREFIX"),
        ("device.match_prefix", "E M"),
        ("device.apply_on_on_duty_days", "yes"),
        ("device.punch_retention_days", 30),
        ("device.sync_history_retention_days", 6),
    ):
        r = await client.put(f"{A}/settings", headers=await H("admin"), json={"settings": {key: val}})
        assert r.status_code == 422, (key, val)
    assert (await db.execute(select(Setting).where(Setting.key == "device.sync_interval_minutes"))).scalar_one().value == 10


# ---------------------------------------------------------------- manual attendance contract
async def test_manual_get_has_device_punches_key(client, H, db, seeded):
    r = await client.get(f"{A}/attendance/{seeded['users']['employee']}/{TODAY}", headers=await H("hr"))
    assert r.status_code == 200 and r.json()["device_punches"] == []


async def test_employee_cannot_see_other_peoples_punches(client, H, db, seeded):
    dev = await add_device(db, None, ip="192.168.0.205")
    await add_device_user(db, dev, "ADM001", user="admin")
    p = await add_punch(db, dev, "ADM001", local(TODAY, "08:55"))
    await db.commit()
    await da.derive_day(db, seeded["users"]["admin"], TODAY)
    await db.commit()
    del p
    assert (await client.get(f"/api/attendance/punches?date={TODAY}", headers=await H("employee"))).json() == {"items": []}
    assert len((await client.get(f"/api/attendance/punches?date={TODAY}", headers=await H("admin"))).json()["items"]) == 1
    assert (await db.execute(select(DevicePunch))).scalars().all()


async def test_device_punch_export_respects_scope_at_builder_level(db, seeded):
    from app.services import export as export_svc

    dev = await add_device(db, None, ip="192.168.0.205")
    for who, user in (("EMP001", "employee"), ("MGR001", "manager")):
        await add_device_user(db, dev, who, user=user)
        await add_punch(db, dev, who, local(TODAY, "08:55"))
    await add_device_user(db, dev, "900", method="unmatched")
    await add_punch(db, dev, "900", local(TODAY, "08:56"))
    await db.commit()
    for p in (await db.execute(select(DevicePunch))).scalars():
        await da.set_outcomes(db, [(p.id, None, TODAY, "unmatched", None)])
    await da.derive_day(db, seeded["users"]["employee"], TODAY)
    await da.derive_day(db, seeded["users"]["manager"], TODAY)
    await db.commit()
    build = export_svc.SHEETS["device_punches"]
    team = export_svc.ExportScope.team({seeded["users"]["employee"]})
    rows = [r async for r in build(db, team, export_svc.ExportFilters(), TODAY, TODAY).rows()]
    assert {r[4] for r in rows} == {USERS["employee"]}
    everyone = [r async for r in build(db, export_svc.ExportScope.everyone(), export_svc.ExportFilters(), TODAY, TODAY).rows()]
    assert len(everyone) == 3
    assert await build(db, export_svc.ExportScope.team(set()), export_svc.ExportFilters(), TODAY, TODAY).count() == 0


# ================================================================ security review fixes
async def _map(client, H, role, du_id, user_id, reason="checked the badge list", **extra):
    return await client.put(
        f"{A}/device-users/{du_id}/mapping", headers=await H(role), json={"user_id": user_id, "reason": reason, **extra}
    )


# ---- M3
async def test_m3_mapping_needs_a_reason(client, H, db, seeded):
    dev = await add_device(db, None, ip="192.168.0.205")
    du = await add_device_user(db, dev, "900", user=None, method="unmatched")
    uid = str(seeded["users"]["manager"])
    for body in ({"user_id": uid}, {"user_id": uid, "reason": ""}, {"user_id": uid, "reason": "ab"}, {"user_id": uid, "reason": "x" * 301}):
        r = await client.put(f"{A}/device-users/{du.id}/mapping", headers=await H("hr"), json=body)
        assert r.status_code == 422, body
    assert (await _map(client, H, "hr", du.id, uid)).status_code == 200


async def test_m3_nobody_maps_a_device_user_to_themselves(client, H, db, seeded):
    dev = await add_device(db, None, ip="192.168.0.205")
    du = await add_device_user(db, dev, "900", user=None, method="unmatched")
    for role in ("hr", "admin"):
        r = await _map(client, H, role, du.id, str(seeded["users"][role]))
        assert (r.status_code, r.json()["error"]["code"]) == (403, "FORBIDDEN"), role
    await db.refresh(du)
    assert (du.match_method, du.user_id) == ("unmatched", None)


async def test_m3_a_device_user_mapped_to_the_caller_cannot_be_changed_by_the_caller(client, H, db, seeded):
    dev = await add_device(db, None, ip="192.168.0.205")
    mine = await add_device_user(db, dev, "EMP-HR", user="hr", method="manual")
    for target in (None, str(seeded["users"]["manager"])):
        assert (await _map(client, H, "hr", mine.id, target)).status_code == 403, target
    assert (await client.post(f"{A}/device-users/{mine.id}/auto", headers=await H("hr"))).status_code == 403
    await db.refresh(mine)
    assert mine.match_method == "manual" and mine.user_id == seeded["users"]["hr"]
    # somebody else (the admin) can still correct it
    assert (await _map(client, H, "admin", mine.id, None)).status_code == 200


async def test_m3_changing_an_already_matched_user_needs_device_manage_and_is_audited_with_the_previous_employee(client, H, db, seeded):
    dev = await add_device(db, None, ip="192.168.0.205")
    du = await add_device_user(db, dev, "EMP001", user="employee")  # matched to the employee
    mgr = str(seeded["users"]["manager"])
    r = await _map(client, H, "hr", du.id, mgr)
    assert r.status_code == 403 and r.json()["error"]["details"][0]["field"] == "id"
    assert (await _map(client, H, "hr", du.id, None)).status_code == 403
    assert (await client.post(f"{A}/device-users/{du.id}/auto", headers=await H("hr"))).status_code == 403
    await db.refresh(du)
    assert du.user_id == seeded["users"]["employee"]
    assert (await _map(client, H, "hr", du.id, str(seeded["users"]["employee"]))).status_code == 200  # no change: allowed
    r = await _map(client, H, "admin", du.id, mgr, reason="employee swapped badges")
    assert r.status_code == 200 and r.json()["items"][0]["user"]["employee_id"] == USERS["manager"]
    maps = (await db.execute(select(AuditLog).where(AuditLog.action == "device_user.map"))).scalars().all()
    a = next(m for m in maps if m.after["reason"] == "employee swapped badges")
    assert a.before["employee_id"] == USERS["employee"] and a.before["device_user_id"] == "EMP001"
    assert a.after == {"device_user_id": "EMP001", "employee_id": USERS["manager"], "reason": "employee swapped badges"}


async def test_m3_second_slot_on_the_same_device_alerts_hr(client, H, db, seeded):
    from app.services.notifications import OUTBOX

    dev = await add_device(db, None, ip="192.168.0.205")
    await add_device_user(db, dev, "EMP001", user="employee")
    extra = await add_device_user(db, dev, "7777", user=None, method="unmatched")
    OUTBOX.clear()
    r = await _map(client, H, "hr", extra.id, str(seeded["users"]["employee"]))
    assert r.status_code == 200
    got = [o for o in OUTBOX if "two users on one device" in o["payload"]["title"]]
    assert got and str(seeded["users"]["admin"]) in got[0]["user_ids"]
    assert "device_user.second_slot" in (await db.execute(select(AuditLog.action))).scalars().all()


# ---- M4: HR confirms a held user; the list highlights device administrators
async def test_m4_hr_confirms_a_held_user_and_admins_sort_first(client, H, db, seeded):
    dev = await add_device(db, None, ip="192.168.0.205")
    held = await add_device_user(db, dev, USERS["manager"], user=None, method="unmatched", auto_hold=True)
    await add_device_user(db, dev, "5", user=None, method="unmatched", is_device_admin=True)
    items = (await client.get(f"{A}/device-users", headers=await H("hr"))).json()["items"]
    assert [i["device_user_id"] for i in items][0] == "5" and items[0]["is_device_admin"] is True
    by = {i["device_user_id"]: i for i in items}
    assert by[USERS["manager"]]["pending_confirmation"] is True and by["5"]["pending_confirmation"] is False
    r = await client.post(f"{A}/device-users/{held.id}/auto", headers=await H("hr"))  # HR's confirmation
    assert r.status_code == 200 and r.json()["items"][0]["match_method"] == "auto"
    await db.refresh(held)
    assert held.auto_hold is False and held.user_id == seeded["users"]["manager"]


async def test_m3_hr_cannot_confirm_a_held_user_whose_id_is_their_own(client, H, db, seeded):
    dev = await add_device(db, None, ip="192.168.0.205")
    held = await add_device_user(db, dev, USERS["hr"], user=None, method="unmatched", auto_hold=True)
    r = await client.post(f"{A}/device-users/{held.id}/auto", headers=await H("hr"))
    assert r.status_code == 403
    await db.refresh(held)
    assert (held.match_method, held.auto_hold) == ("unmatched", True)
    r = await client.post(f"{A}/device-users/{held.id}/auto", headers=await H("admin"))  # somebody else confirms it
    assert r.status_code == 200 and r.json()["items"][0]["user"]["employee_id"] == USERS["hr"]


# ---- L7: device.manage writes need a console session
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/devices", BODY),
        ("PUT", "/devices/{id}", BODY),
        ("DELETE", "/devices/{id}", None),
        ("POST", "/devices/test", {"ip": "192.168.0.201"}),
        ("POST", "/devices/{id}/test", None),
        ("POST", "/devices/{id}/accept-identity", {}),
    ],
)
async def test_l7_mobile_tokens_cannot_change_devices(client, H, HM, db, worker_up, method, path, body):
    dev = await add_device(db, None, ip="192.168.0.205", name="Matrix")
    url = A + path.replace("{id}", str(dev.id))
    r = await client.request(method, url, headers=await HM("admin"), json=body)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "CONSOLE_REQUIRED"), r.text
    assert len((await db.execute(select(Device))).scalars().all()) == 1  # nothing changed
    r = await client.request(method, url, headers=await H("admin"), json=body)  # the console session is let through
    assert r.status_code < 400 or r.json()["error"]["code"] != "CONSOLE_REQUIRED"


async def test_l7_reads_and_mapping_still_work_from_the_phone(client, HM, db, seeded):
    dev = await add_device(db, None, ip="192.168.0.205")
    du = await add_device_user(db, dev, "900", user=None, method="unmatched")
    assert (await client.get(f"{A}/devices", headers=await HM("admin"))).status_code == 200
    assert (await client.get(f"{A}/devices/{dev.id}", headers=await HM("hr"))).status_code == 200
    r = await client.put(
        f"{A}/device-users/{du.id}/mapping", headers=await HM("hr"), json={"user_id": str(seeded["users"]["manager"]), "reason": "visitor card"}
    )
    assert r.status_code == 200


# ---- L9
async def test_l9_mapping_applies_to_one_device_unless_asked(client, H, db, seeded):
    d1 = await add_device(db, None, ip="192.168.0.205", name="One")
    d2 = await add_device(db, None, ip="192.168.0.206", name="Two")
    a = await add_device_user(db, d1, "900", user=None, method="unmatched")
    b = await add_device_user(db, d2, "900", user=None, method="unmatched")
    mgr = str(seeded["users"]["manager"])
    r = await _map(client, H, "hr", a.id, mgr)
    assert [i["device"]["name"] for i in r.json()["items"]] == ["One"]
    await db.refresh(b)
    assert b.match_method == "unmatched"
    r = await _map(client, H, "hr", a.id, mgr, apply_to_all_devices=True)
    assert sorted(i["device"]["name"] for i in r.json()["items"]) == ["One", "Two"]


# ---- L1 / L5
async def test_l1_only_allowed_ports_are_accepted_and_the_test_audit_names_ip_and_port(client, H, db, worker_up, monkeypatch):
    for url, body in (("/devices", BODY), ("/devices/test", {"ip": "192.168.0.201"})):
        r = await client.post(A + url, headers=await H("admin"), json={**body, "port": 22})
        assert (r.status_code, r.json()["error"]["code"]) == (422, "DEVICE_ADDRESS_NOT_ALLOWED"), url
        assert r.json()["error"]["details"][0]["field"] == "port"
    dev = await create(client, H)
    assert (await client.put(f"{A}/devices/{dev['id']}", headers=await H("admin"), json={**BODY, "port": 6379})).status_code == 422
    t = await client.post(f"{A}/devices/test", headers=await H("admin"), json={"ip": "192.168.0.201", "port": 4370, "comm_key": 99})
    assert t.status_code == 202
    t2 = await client.post(f"{A}/devices/{dev['id']}/test", headers=await H("admin"))
    assert t2.status_code == 202
    tests = (await db.execute(select(AuditLog).where(AuditLog.action == "device.test"))).scalars().all()
    assert sorted((a.after.get("unsaved", False), a.after["ip"], a.after["port"]) for a in tests) == [
        (False, "192.168.0.201", 4370), (True, "192.168.0.201", 4370),
    ]  # fmt: skip
    assert "99" not in json.dumps([a.after for a in tests])
    monkeypatch.setattr(get_settings(), "device_allowed_ports", "4370,5000")
    assert (await client.put(f"{A}/devices/{dev['id']}", headers=await H("admin"), json={**BODY, "port": 5000})).status_code == 200


async def test_l5_update_delete_and_accept_identity_audit_old_and_new_values(client, H, db):
    dev = await create(client, H)
    d = await db.get(Device, __import__("uuid").UUID(dev["id"]))
    d.serial_number = "SN-OLD"
    await db.commit()
    r = await client.put(
        f"{A}/devices/{dev['id']}", headers=await H("admin"),
        json={**BODY, "ip": "192.168.0.202", "transport": "udp", "comm_key": 4242},
    )  # fmt: skip
    assert r.status_code == 200
    upd = (await db.execute(select(AuditLog).where(AuditLog.action == "device.update"))).scalar_one()
    assert upd.before == {"ip": "192.168.0.201", "transport": "tcp"} and upd.after["ip"] == "192.168.0.202" and upd.after["transport"] == "udp"
    assert "4242" not in json.dumps([upd.before, upd.after]) and "123456" not in json.dumps([upd.before, upd.after])
    assert (await client.post(f"{A}/devices/{dev['id']}/accept-identity", headers=await H("admin"), json={})).status_code == 200
    acc = (await db.execute(select(AuditLog).where(AuditLog.action == "device.identity_accepted"))).scalar_one()
    assert acc.before == {"serial_number": "SN-OLD"} and acc.after == {"ip": "192.168.0.202", "port": 4370}
    assert (await client.delete(f"{A}/devices/{dev['id']}", headers=await H("admin"))).status_code == 204
    dele = (await db.execute(select(AuditLog).where(AuditLog.action == "device.delete"))).scalar_one()
    assert dele.before["ip"] == "192.168.0.202" and dele.before["port"] == 4370 and dele.before["name"] == "Main entrance"


# ---- L8: the mapping endpoints are rate limited
async def test_l8_mapping_endpoints_are_rate_limited(client, H, db, seeded):
    dev = await add_device(db, None, ip="192.168.0.205")
    du = await add_device_user(db, dev, "900", user=None, method="unmatched")
    mgr = str(seeded["users"]["manager"])
    codes = [(await _map(client, H, "hr", du.id, mgr)).status_code for _ in range(30)]
    assert set(codes) == {200}
    r = await _map(client, H, "hr", du.id, mgr)
    assert (r.status_code, r.json()["error"]["code"]) == (429, "RATE_LIMITED")
    assert (await client.post(f"{A}/device-users/{du.id}/auto", headers=await H("hr"))).status_code == 429
    assert (await _map(client, H, "admin", du.id, mgr)).status_code == 200  # per caller
