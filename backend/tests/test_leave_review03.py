"""Regression tests for docs/reviews/03-leave-security.md (M1-M4, L1-L10). 2026-10-21 is a Wednesday (office tz IST).
SQLite harness: the EXCLUDE constraints and real FOR UPDATE row locks are NOT exercised (the services' pre-checks are)."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import LargeBinary, select, type_coerce

from app.core import security
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.redis import get_redis
from app.models import (
    AuditLog,
    File,
    Holiday,
    LeaveBalance,
    LeaveRequest,
    LeaveType,
    PermissionRequest,
    Setting,
    User,
)
from app.services import key_rotation
from app.services import leave as lv
from app.services.notifications import OUTBOX
from tests.conftest import PASSWORD, USERS, bearer, login_tokens

TODAY = dt.datetime(2026, 10, 21, 4, 30, tzinfo=dt.UTC)  # 10:00 IST
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


@pytest.fixture(autouse=True)
def clock(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.leave.utcnow", lambda: TODAY)
    OUTBOX.clear()
    st = get_settings()
    monkeypatch.setattr(st, "leave_write_rate_per_min", 1000)
    monkeypatch.setattr(st, "leave_write_rate_per_day", 100000)
    monkeypatch.setattr(st, "leave_decide_rate_per_min", 1000)
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(st, "file_storage_dir", str(root))


@pytest.fixture
async def types(db):
    cl = LeaveType(code="CL", name="Casual Leave", annual_quota=12, is_paid=True, allow_half_day=True)
    sl = LeaveType(code="SL", name="Sick Leave", annual_quota=6, allow_half_day=False, requires_attachment_after_days=2)
    mc = LeaveType(code="MC", name="Capped", annual_quota=20, max_consecutive_days=3)
    db.add_all([cl, sl, mc])
    await db.commit()
    return {"CL": cl.id, "SL": sl.id, "MC": mc.id}


@pytest.fixture
async def H(tokens_for):
    cache: dict = {}

    async def _h(role):
        if role not in cache:
            cache[role] = bearer(await tokens_for(role))
        return cache[role]

    return _h


def body(tid, f, t=None, **kw):
    return {"leave_type_id": str(tid), "from_date": f, "to_date": t or f, "reason": "personal", **kw}


async def apply(client, h, tid, f, t=None, **kw):
    return await client.post("/api/leave/requests", headers=h, json=body(tid, f, t, **kw))


def perm(date="2026-10-22", f="10:00", t="11:00"):
    return {"date": date, "from_time": f, "to_time": t, "reason": "errand"}


def code(r):
    return r.json()["error"]["code"]


async def upload_pdf(client, h):
    r = await client.post(
        "/api/files", headers=h, data={"purpose": "leave_attachment"}, files={"file": ("c.pdf", PDF, "application/pdf")}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def uid(db, role):
    return (await db.execute(select(User.id).where(User.employee_id == USERS[role]))).scalar_one()


@pytest.fixture
async def mgr2(client, db, seeded):
    u = User(
        employee_id="MGR002",
        password_hash=security.hash_password(PASSWORD),
        full_name="Other Manager",
        official_email="m2@corp-mail.com",
        role_id=seeded["roles"]["manager"],
        department_id=seeded["dept"],
        shift_id=seeded["shift"],
        must_change_password=False,
        status="active",
    )
    db.add(u)
    await db.commit()
    return bearer(await login_tokens(client, "MGR002")), u.id


async def raw(db, col, rid):
    return (await db.execute(select(type_coerce(col, LargeBinary)).where(LeaveRequest.id == rid))).scalar_one_or_none()


# ================================================================== M1 encryption + retention
async def test_reason_and_comment_encrypted_at_rest(client, H, types, db):
    emp, mgr = await H("employee"), await H("manager")
    r = await client.post(
        "/api/leave/requests", headers=emp, json=body(types["CL"], "2026-11-02", reason="chemotherapy follow-up")
    )
    rid = r.json()["id"]
    p = await client.post("/api/permission/requests", headers=emp, json={**perm(), "reason": "pregnancy scan"})
    pid = p.json()["id"]
    await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={"comment": "get well soon"})
    await client.put(f"/api/admin/requests/permission/{pid}/reject", headers=mgr, json={"comment": "bring a note"})
    leave_raw = (await db.execute(select(type_coerce(LeaveRequest.reason, LargeBinary)))).scalar_one()
    assert leave_raw is not None and b"chemotherapy" not in bytes(leave_raw)
    assert b"get well" not in bytes(
        (await db.execute(select(type_coerce(LeaveRequest.decision_comment, LargeBinary)))).scalar_one()
    )
    praw = (await db.execute(select(type_coerce(PermissionRequest.reason, LargeBinary)))).scalar_one()
    assert b"pregnancy" not in bytes(praw)
    got = (await client.get(f"/api/leave/requests/{rid}", headers=emp)).json()
    assert got["reason"] == "chemotherapy follow-up" and got["decision_comment"] == "get well soon"
    assert (await client.get(f"/api/permission/requests/{pid}", headers=emp)).json()["decision_comment"] == "bring a note"


def test_key_rotation_discovers_the_new_columns():
    cols = {(t.name, c.name) for t, c in key_rotation.encrypted_columns()}
    for t in ("leave_requests", "permission_requests"):
        assert (t, "reason_enc") in cols and (t, "decision_comment_enc") in cols


async def _closed_request(db, user_id, type_id, d, status="approved", file_id=None):
    r = LeaveRequest(
        user_id=user_id,
        leave_type_id=type_id,
        from_date=d,
        to_date=d,
        days=1,
        reason="old reason",
        decision_comment="old comment",
        attachment_file_id=file_id,
        status=status,
    )
    db.add(r)
    await db.flush()
    return r


async def test_retention_off_by_default_then_purges_closed_requests(db, types, seeded):
    emp = seeded["users"]["employee"]
    f_old = File(
        owner_id=emp, purpose="leave_attachment", storage_path="aa/old.bin", content_type="application/pdf", size_bytes=1
    )
    f_shared = File(
        owner_id=emp, purpose="leave_attachment", storage_path="aa/sh.bin", content_type="application/pdf", size_bytes=1
    )
    db.add_all([f_old, f_shared])
    await db.flush()
    old = await _closed_request(db, emp, types["CL"], dt.date(2026, 3, 2), file_id=f_old.id)
    old2 = await _closed_request(db, emp, types["CL"], dt.date(2026, 3, 3), "rejected", file_id=f_shared.id)
    recent = await _closed_request(db, emp, types["CL"], dt.date(2026, 10, 14), file_id=f_shared.id)
    pending = await _closed_request(db, emp, types["CL"], dt.date(2026, 3, 4), "pending")
    perm_old = PermissionRequest(
        user_id=emp,
        request_date=dt.date(2026, 3, 2),
        from_time=dt.time(10),
        to_time=dt.time(11),
        minutes=60,
        reason="x",
        decision_comment="y",
        status="approved",
    )
    db.add(perm_old)
    await db.commit()
    ids = {k: v.id for k, v in {"old": old, "old2": old2, "recent": recent, "pending": pending, "perm": perm_old}.items()}
    fids = (f_old.id, f_shared.id)

    assert await lv.purge_old_text(db) == {"leave": 0, "permission": 0, "on_duty": 0, "files": 0}  # default: off
    assert (await db.get(LeaveRequest, ids["old"])).reason == "old reason"

    db.add(Setting(key="leave.reason_retention_days", value=30))
    await db.commit()
    out = await lv.purge_old_text(db)
    assert out == {"leave": 2, "permission": 1, "on_duty": 0, "files": 1}  # f_shared is still used by the recent request
    db.expire_all()
    for rid in (ids["old"], ids["old2"]):
        r = await db.get(LeaveRequest, rid)
        assert r.reason is None and r.decision_comment is None and r.attachment_file_id is None
        assert await raw(db, LeaveRequest.reason, rid) is None
    assert (await db.get(LeaveRequest, ids["recent"])).reason == "old reason"
    assert (await db.get(LeaveRequest, ids["pending"])).reason == "old reason"  # never purge open requests
    assert (await db.get(PermissionRequest, ids["perm"])).reason is None
    assert await db.get(File, fids[0]) is None and await db.get(File, fids[1]) is not None
    assert await lv.purge_old_text(db) == {"leave": 0, "permission": 0, "on_duty": 0, "files": 0}  # idempotent


async def test_purged_reason_renders_as_empty_string(client, H, types, db, seeded):
    emp = await H("employee")
    r = await _closed_request(db, seeded["users"]["employee"], types["CL"], dt.date(2026, 3, 2))
    r.reason = None
    await db.commit()
    assert (await client.get(f"/api/leave/requests/{r.id}", headers=emp)).json()["reason"] == ""


# ================================================================== M2 split bypass
async def test_split_requests_cannot_dodge_attachment_rule(client, H, types, db, seeded):
    emp = await H("employee")
    assert (await apply(client, emp, types["SL"], "2026-11-02", "2026-11-03")).status_code == 201  # Mon-Tue, 2 days
    r = await apply(client, emp, types["SL"], "2026-11-04", "2026-11-05")  # Wed-Thu, adjacent: block = 4 days
    assert r.status_code == 422 and code(r) == "ATTACHMENT_REQUIRED"
    # weekend gap still counts as touching: Fri + Mon, then Tue pushes the block over the limit
    assert (await apply(client, emp, types["SL"], "2026-11-06")).status_code == 201  # Fri: not adjacent to Tue (Wed/Thu free)
    assert (await apply(client, emp, types["SL"], "2026-11-09")).status_code == 201  # Mon: block Fri+Mon = 2
    r = await apply(client, emp, types["SL"], "2026-11-10")  # Tue: block Fri+Mon+Tue = 3 > 2
    assert code(r) == "ATTACHMENT_REQUIRED"
    f = await upload_pdf(client, emp)
    assert (await apply(client, emp, types["SL"], "2026-11-10", attachment_file_id=f["id"])).status_code == 201
    # any request in the block with an attachment satisfies the rule for its neighbours
    assert (await apply(client, emp, types["SL"], "2026-11-11")).status_code == 201


async def test_split_requests_cannot_dodge_max_consecutive(client, H, types):
    emp = await H("employee")
    assert (await apply(client, emp, types["MC"], "2026-11-02", "2026-11-03")).status_code == 201  # 2 <= 3
    r = await apply(client, emp, types["MC"], "2026-11-04", "2026-11-05")  # block spans 4 calendar days
    assert r.status_code == 422 and code(r) == "INVALID_DATE_RANGE"
    assert "adjacent" in r.json()["error"]["message"]
    assert (await apply(client, emp, types["MC"], "2026-11-04")).status_code == 201  # block of 3 is fine


async def test_holiday_gap_merges_and_other_types_do_not(client, H, types, db):
    emp = await H("employee")
    db.add(Holiday(date=dt.date(2026, 11, 4), name="Festival", is_active=True))
    await db.commit()
    assert (await apply(client, emp, types["SL"], "2026-11-02", "2026-11-03")).status_code == 201
    r = await apply(client, emp, types["SL"], "2026-11-05", "2026-11-06")  # Wed is a holiday: still one block of 4 days
    assert code(r) == "ATTACHMENT_REQUIRED"
    # a different leave type in between never merges
    assert (await apply(client, emp, types["CL"], "2026-11-16", "2026-11-17")).status_code == 201
    assert (await apply(client, emp, types["SL"], "2026-11-18", "2026-11-19")).status_code == 201


async def test_cancelled_neighbours_do_not_count(client, H, types):
    emp = await H("employee")
    first = (await apply(client, emp, types["SL"], "2026-11-02", "2026-11-03")).json()["id"]
    await client.post(f"/api/leave/requests/{first}/cancel", headers=emp)
    assert (await apply(client, emp, types["SL"], "2026-11-04", "2026-11-05")).status_code == 201


# ================================================================== M3 rate limits + fan-out
async def test_apply_and_cancel_are_rate_limited_per_user(client, H, types, monkeypatch):
    monkeypatch.setattr(get_settings(), "leave_write_rate_per_min", 3)
    emp, hr = await H("employee"), await H("hr")
    ids = []
    for d in ("2026-11-02", "2026-11-03"):
        ids.append((await apply(client, emp, types["CL"], d)).json()["id"])
    assert (await client.post(f"/api/leave/requests/{ids[0]}/cancel", headers=emp)).status_code == 200  # 3rd
    r = await apply(client, emp, types["CL"], "2026-11-04")
    assert r.status_code == 429 and code(r) == "RATE_LIMITED" and "retry-after" in r.headers
    r = await client.post("/api/permission/requests", headers=emp, json=perm())  # shared budget, both kinds
    assert r.status_code == 429
    assert (await apply(client, hr, types["CL"], "2026-11-02")).status_code == 201  # per user, not global


async def test_decide_is_rate_limited(client, H, types, monkeypatch):
    emp, mgr = await H("employee"), await H("manager")
    ids = [(await apply(client, emp, types["CL"], d)).json()["id"] for d in ("2026-11-02", "2026-11-03", "2026-11-04")]
    monkeypatch.setattr(get_settings(), "leave_decide_rate_per_min", 2)
    for rid in ids[:2]:
        assert (await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={})).status_code == 200
    r = await client.put(f"/api/admin/requests/leave/{ids[2]}/reject", headers=mgr, json={"comment": "no"})
    assert r.status_code == 429 and code(r) == "RATE_LIMITED"


async def test_write_limits_fail_closed_when_redis_is_down(app, client, H, types):
    emp = await H("employee")

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    async def _broken():
        return Broken()

    app.dependency_overrides[get_redis] = _broken
    r = await apply(client, emp, types["CL"], "2026-11-02")
    assert r.status_code == 503 and code(r) == "TEMPORARILY_UNAVAILABLE"
    r = await client.post("/api/permission/requests", headers=emp, json=perm())
    assert r.status_code == 503


async def test_no_manager_fanout_is_coalesced(client, H, types, db):
    hr = await H("hr")  # no manager: fan-out to approve_all holders (admin)
    await apply(client, hr, types["CL"], "2026-11-02")
    assert len(OUTBOX) == 1
    await apply(client, hr, types["CL"], "2026-11-03")
    await client.post("/api/permission/requests", headers=hr, json=perm())
    assert len(OUTBOX) == 1  # same requester -> same approvers within 10 minutes: no further push


# ================================================================== M4 attachment access
async def test_attachment_access_is_bound_to_a_visible_request(client, H, types, db, seeded, mgr2):
    emp, mgr, hr = await H("employee"), await H("manager"), await H("hr")
    m2h, _ = mgr2
    f = await upload_pdf(client, emp)
    for h in (mgr, hr, m2h):  # not attached to any request yet: owner only
        assert (await client.get(f["url"], headers=h)).status_code == 404
    assert (await client.get(f["url"], headers=emp)).status_code == 200
    r = await apply(client, emp, types["SL"], "2026-11-02", "2026-11-04", attachment_file_id=f["id"])
    assert r.status_code == 201 and r.json()["attachment_url"] == f["url"]
    assert r.json()["attachment_content_type"] == "application/pdf"
    assert (await client.get(f["url"], headers=mgr)).status_code == 200  # direct manager
    assert (await client.get(f["url"], headers=hr)).status_code == 200
    assert (await client.get(f["url"], headers=m2h)).status_code == 404  # another team's manager
    items = (await client.get("/api/admin/requests?type=leave", headers=mgr)).json()["items"]
    assert items[0]["attachment_url"] == f["url"] and items[0]["attachment_content_type"] == "application/pdf"
    denied = (await db.execute(select(AuditLog).where(AuditLog.action == "file.access_denied"))).scalars().all()
    assert len(denied) == 4


async def test_cannot_attach_someone_elses_file(client, H, types):
    emp, mgr = await H("employee"), await H("manager")
    theirs = await upload_pdf(client, mgr)
    r = await apply(client, emp, types["SL"], "2026-11-02", "2026-11-04", attachment_file_id=theirs["id"])
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"
    assert (await client.get(theirs["url"], headers=emp)).status_code == 404


async def test_manager_loses_attachment_access_when_no_longer_the_manager(client, H, types, db, seeded):
    emp, mgr, hr = await H("employee"), await H("manager"), await H("hr")
    f = await upload_pdf(client, emp)
    await apply(client, emp, types["SL"], "2026-11-02", "2026-11-04", attachment_file_id=f["id"])
    u = await db.get(User, seeded["users"]["employee"])
    u.manager_id = seeded["users"]["hr"]
    await db.commit()
    assert (await client.get(f["url"], headers=mgr)).status_code == 404
    assert (await client.get(f["url"], headers=hr)).status_code == 200


# ================================================================== L1 horizon / far-future
async def test_far_future_dates_are_a_clean_422(client, H, types, db):
    emp = await H("employee")
    for d in ("2201-01-05", "9999-12-30", "1999-12-30"):
        r = await apply(client, emp, types["CL"], d)
        assert r.status_code == 422 and code(r) == "INVALID_DATE_RANGE", d
    assert code(await apply(client, emp, types["CL"], "2027-12-01")) == "INVALID_DATE_RANGE"  # > 365 days ahead
    assert (await apply(client, emp, types["CL"], "2027-10-20")).status_code == 201  # inside the horizon
    db.add(Setting(key="leave.max_advance_days", value=0))  # 0 = no horizon; the year range still applies
    await db.commit()
    assert (await apply(client, emp, types["CL"], "2030-05-06")).status_code == 201
    assert code(await apply(client, emp, types["CL"], "2201-01-05")) == "INVALID_DATE_RANGE"


async def test_get_balance_never_500s_on_a_bad_year(db, types, seeded):
    u = await db.get(User, seeded["users"]["employee"])
    lt = await db.get(LeaveType, types["CL"])
    with pytest.raises(AppError) as e:
        await lv.get_balance(db, u, lt, 2201)
    assert e.value.code == "INVALID_DATE_RANGE"


# ================================================================== L2 inactive requesters
async def test_deactivate_auto_cancels_pending_requests(client, H, types, db, seeded):
    emp, adm = await H("employee"), await H("admin")
    lid = (await apply(client, emp, types["CL"], "2026-11-02", "2026-11-03")).json()["id"]
    pid = (await client.post("/api/permission/requests", headers=emp, json=perm())).json()["id"]
    done = (await apply(client, emp, types["CL"], "2026-11-09")).json()["id"]
    mgr = await H("manager")
    await client.put(f"/api/admin/requests/leave/{done}/approve", headers=mgr, json={})
    eid = seeded["users"]["employee"]
    assert (await client.post(f"/api/admin/employees/{eid}/deactivate", headers=adm)).status_code == 200
    db.expire_all()
    assert (await db.get(LeaveRequest, __import__("uuid").UUID(lid))).status == "cancelled"
    assert (await db.get(PermissionRequest, __import__("uuid").UUID(pid))).status == "cancelled"
    assert (await db.get(LeaveRequest, __import__("uuid").UUID(done))).status == "approved"  # decided ones are kept
    b = (await db.execute(select(LeaveBalance).where(LeaveBalance.user_id == eid, LeaveBalance.year == 2026))).scalars().all()
    cl = next(x for x in b if x.leave_type_id == types["CL"])
    assert (float(cl.pending), float(cl.used)) == (0.0, 1.0)
    actions = {a.action for a in (await db.execute(select(AuditLog))).scalars()}
    assert {"leave.auto_cancel", "permission.auto_cancel", "employee.deactivate"} <= actions


async def test_approving_an_inactive_employees_request_is_409_but_reject_cleans_up(client, H, types, db, seeded):
    emp, mgr = await H("employee"), await H("manager")
    a = (await apply(client, emp, types["CL"], "2026-11-02")).json()["id"]
    b = (await apply(client, emp, types["CL"], "2026-11-03")).json()["id"]
    u = await db.get(User, seeded["users"]["employee"])
    u.status = "inactive"  # legacy pending row that predates auto-cancel
    await db.commit()
    r = await client.put(f"/api/admin/requests/leave/{a}/approve", headers=mgr, json={})
    assert r.status_code == 409 and code(r) == "REQUESTER_INACTIVE"
    r = await client.put(f"/api/admin/requests/leave/{b}/reject", headers=mgr, json={"comment": "left"})
    assert r.status_code == 200
    refused = (await db.execute(select(AuditLog).where(AuditLog.action == "request.decide_refused"))).scalars().all()
    assert len(refused) == 1 and refused[0].after["code"] == "REQUESTER_INACTIVE" and refused[0].outcome == "failure"


async def test_delete_employee_auto_cancels_too(client, H, types, db, seeded):
    emp, adm = await H("employee"), await H("admin")
    lid = (await apply(client, emp, types["CL"], "2026-11-02")).json()["id"]
    assert (await client.delete(f"/api/admin/employees/{seeded['users']['employee']}", headers=adm)).status_code == 204
    db.expire_all()
    assert (await db.get(LeaveRequest, __import__("uuid").UUID(lid))).status == "cancelled"


# ================================================================== L3 / L4 denied attempts, 404 before 403
async def test_denied_decisions_are_audited_and_do_not_leak_existence(client, H, types, db, mgr2):
    emp, hr = await H("employee"), await H("hr")
    m2h, _ = mgr2
    rid = (await apply(client, emp, types["CL"], "2026-11-02")).json()["id"]
    hr_req = (await apply(client, hr, types["CL"], "2026-11-02")).json()["id"]
    r = await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=m2h, json={})
    assert r.status_code == 404 and code(r) == "NOT_FOUND"  # same answer as an unknown id
    unknown = await client.put("/api/admin/requests/leave/00000000-0000-0000-0000-000000000000/approve", headers=m2h, json={})
    assert unknown.status_code == 404 and unknown.json()["error"]["message"] == r.json()["error"]["message"]
    assert (await client.put(f"/api/admin/requests/leave/{hr_req}/reject", headers=hr, json={"comment": "x"})).status_code == 403
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "request.decide_denied"))).scalars().all()
    assert sorted(r.after["why"] for r in rows) == ["scope", "self"]
    assert all(r.outcome == "failure" and r.entity_id for r in rows)
    st = (await db.get(LeaveRequest, __import__("uuid").UUID(rid))).status
    assert st == "pending"


async def test_refused_approval_not_pending_is_audited(client, H, types, db):
    emp, mgr = await H("employee"), await H("manager")
    rid = (await apply(client, emp, types["CL"], "2026-11-02")).json()["id"]
    await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={})
    assert code(await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={})) == "NOT_PENDING"
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "request.decide_refused"))).scalars().all()
    assert [r.after["code"] for r in rows] == ["NOT_PENDING"]


async def test_cancel_of_foreign_or_unknown_id_is_404(client, H, types):
    emp, hr = await H("employee"), await H("hr")
    rid = (await apply(client, emp, types["CL"], "2026-11-02")).json()["id"]
    for kind in ("leave", "permission"):
        zero = "00000000-0000-0000-0000-000000000000"
        assert (await client.post(f"/api/{kind}/requests/{zero}/cancel", headers=hr)).status_code == 404
    assert (await client.post(f"/api/leave/requests/{rid}/cancel", headers=hr)).status_code == 404


# ================================================================== L5 prorata on join_date change
async def _put_employee(client, adm, db, seeded, **over):
    u = await db.get(User, seeded["users"]["employee"])
    data = {
        "full_name": u.full_name,
        "official_email": u.official_email,
        "department_id": str(u.department_id),
        "shift_id": str(u.shift_id),
        "role_id": str(u.role_id),
        "manager_id": str(u.manager_id),
        "join_date": None,
        **over,
    }
    r = await client.put(f"/api/admin/employees/{u.id}", headers=adm, json=data)
    assert r.status_code == 200, r.text
    return r


async def test_join_date_change_recomputes_unused_quota(client, H, types, db, seeded):
    emp, adm = await H("employee"), await H("admin")
    b = await client.get("/api/leave/balance", headers=emp)
    assert next(x for x in b.json()["leave"] if x["code"] == "CL")["quota"] == 12.0  # no join_date: full year
    await _put_employee(client, adm, db, seeded, join_date="2026-07-01")
    b = await client.get("/api/leave/balance", headers=emp)
    assert next(x for x in b.json()["leave"] if x["code"] == "CL")["quota"] == 6.0
    audit = (await db.execute(select(AuditLog).where(AuditLog.action == "leave.quota_recompute"))).scalars().all()
    assert len(audit) == 1 and {x["type"] for x in audit[0].after["recomputed"]} >= {"CL", "SL"}
    # once days are used or reserved the row is left alone and flagged for HR
    assert (await apply(client, emp, types["CL"], "2026-11-02")).status_code == 201
    await _put_employee(client, adm, db, seeded, join_date="2026-01-01")
    b = await client.get("/api/leave/balance", headers=emp)
    cl = next(x for x in b.json()["leave"] if x["code"] == "CL")
    sl = next(x for x in b.json()["leave"] if x["code"] == "SL")
    assert cl["quota"] == 6.0 and sl["quota"] == 6.0
    last = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "leave.quota_recompute").order_by(AuditLog.id.desc())))
        .scalars()
        .first()
    )
    assert [x["type"] for x in last.after["needs_review"]] == ["CL"]


# ================================================================== L6 permission interplay
async def test_permission_today_cannot_start_in_the_past(client, H):
    emp = await H("employee")
    P = "/api/permission/requests"
    # "now" is 10:00 IST on Wed 2026-10-21; grace is 5 minutes
    assert code(await client.post(P, headers=emp, json=perm(date="2026-10-21", f="09:40", t="10:30"))) == "INVALID_TIME_RANGE"
    assert (await client.post(P, headers=emp, json=perm(date="2026-10-21", f="09:56", t="10:30"))).status_code == 201


async def test_permission_blocked_by_pending_leave_and_half_day_sessions(client, H, types):
    emp = await H("employee")
    P = "/api/permission/requests"
    await apply(client, emp, types["CL"], "2026-10-22")  # PENDING full-day leave
    r = await client.post(P, headers=emp, json=perm())
    assert r.status_code == 409 and code(r) == "OVERLAPPING_REQUEST"
    # shift 09:30-18:30, midpoint 14:00. First-half leave blocks the morning only.
    await apply(client, emp, types["CL"], "2026-10-26", half_day=True, half_day_session="first")
    assert code(await client.post(P, headers=emp, json=perm(date="2026-10-26", f="10:00", t="11:00"))) == "OVERLAPPING_REQUEST"
    assert (await client.post(P, headers=emp, json=perm(date="2026-10-26", f="15:00", t="16:00"))).status_code == 201


async def test_leave_approval_blocked_by_approved_permission(client, H, types):
    emp, mgr = await H("employee"), await H("manager")
    p = (await client.post("/api/permission/requests", headers=emp, json=perm(date="2026-10-27", f="10:00", t="11:00"))).json()
    leave_id = (await apply(client, emp, types["CL"], "2026-10-27")).json()["id"]  # filed while the permission is only pending
    assert (await client.put(f"/api/admin/requests/permission/{p['id']}/approve", headers=mgr, json={})).status_code == 200
    r = await client.put(f"/api/admin/requests/leave/{leave_id}/approve", headers=mgr, json={})
    assert r.status_code == 409 and code(r) == "OVERLAPPING_REQUEST"
    # a second-half half-day leave does not touch a morning permission
    half = (await apply(client, emp, types["CL"], "2026-10-28", half_day=True, half_day_session="second")).json()["id"]
    pm = (await client.post("/api/permission/requests", headers=emp, json=perm(date="2026-10-28", f="10:00", t="11:00"))).json()
    await client.put(f"/api/admin/requests/permission/{pm['id']}/approve", headers=mgr, json={})
    assert (await client.put(f"/api/admin/requests/leave/{half}/approve", headers=mgr, json={})).status_code == 200


async def test_overnight_shift_permission_windows(client, H, db, seeded):
    from app.models import Shift

    shift = await db.get(Shift, seeded["shift"])
    shift.start_time, shift.end_time = dt.time(22, 0), dt.time(6, 0)
    shift.working_days = [1, 2, 3, 4, 5, 6, 7]
    await db.commit()
    emp = await H("employee")
    P = "/api/permission/requests"
    assert (await client.post(P, headers=emp, json=perm(f="23:00", t="23:45"))).status_code == 201  # evening part
    assert (await client.post(P, headers=emp, json=perm(f="02:00", t="03:00"))).status_code == 201  # after midnight
    assert code(await client.post(P, headers=emp, json=perm(f="12:00", t="13:00"))) == "INVALID_TIME_RANGE"  # off shift
    assert code(await client.post(P, headers=emp, json=perm(f="05:00", t="07:00"))) == "INVALID_TIME_RANGE"  # past 06:00


# ================================================================== L7 approver who can act
async def test_manager_without_approve_permission_is_not_the_only_recipient(client, H, types, db, seeded):
    emp = await H("employee")
    plain = User(
        employee_id="EMP002",
        password_hash=security.hash_password(PASSWORD),
        full_name="Plain Colleague",
        official_email="e2@corp-mail.com",
        role_id=seeded["roles"]["employee"],
        department_id=seeded["dept"],
        shift_id=seeded["shift"],
        must_change_password=False,
        status="active",
    )
    db.add(plain)
    await db.flush()
    u = await db.get(User, seeded["users"]["employee"])
    u.manager_id = plain.id
    await db.commit()
    await apply(client, emp, types["CL"], "2026-11-02")
    recipients = set(OUTBOX[-1]["user_ids"])
    assert str(plain.id) not in recipients
    assert recipients == {str(seeded["users"]["admin"]), str(seeded["users"]["hr"])}


# ================================================================== L8 balance year range
async def test_balance_only_persists_prev_current_next_year(client, H, types, db):
    emp = await H("employee")
    r = await client.get("/api/leave/balance", headers=emp, params={"year": 2100})
    assert r.status_code == 200 and {x["code"]: x["quota"] for x in r.json()["leave"]}["CL"] == 12.0
    assert (await db.execute(select(LeaveBalance).where(LeaveBalance.year == 2100))).first() is None
    assert (await client.get("/api/leave/balance", headers=emp, params={"year": 2201})).status_code == 422
    assert (await client.get("/api/leave/balance", headers=emp, params={"year": 2027})).status_code == 200
    assert (await db.execute(select(LeaveBalance).where(LeaveBalance.year == 2027))).first() is not None
    assert (await client.get("/api/leave/balance", headers=emp, params={"year": 2020})).status_code == 200
    assert (await db.execute(select(LeaveBalance).where(LeaveBalance.year == 2020))).first() is None


# ================================================================== L9 rollover batches
async def test_rollover_isolates_a_failing_user(db, types, seeded, monkeypatch):
    real = lv.ensure_balances
    bad = seeded["users"]["hr"]

    async def flaky(session, user, year):
        if user.id == bad:
            raise RuntimeError("bad row")
        return await real(session, user, year)

    monkeypatch.setattr(lv, "ensure_balances", flaky)
    assert await lv.rollover_balances(db, 2027) == 3 * 3  # three users x three types; one user failed
    rows = (await db.execute(select(LeaveBalance.user_id).where(LeaveBalance.year == 2027))).scalars().all()
    assert bad not in rows and len(rows) == 9
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "leave.rollover"))).scalars().one()
    assert a.after == {"year": 2027, "created": 9, "failed_users": 1} and a.outcome == "failure"
    monkeypatch.setattr(lv, "ensure_balances", real)
    assert await lv.rollover_balances(db, 2027) == 3  # a re-run only fills the gap


# ================================================================== L10 misc coverage
async def test_unknown_leave_type_is_404(client, H):
    r = await apply(client, await H("employee"), "00000000-0000-0000-0000-000000000000", "2026-11-02")
    assert r.status_code == 404 and code(r) == "NOT_FOUND"


async def test_push_payload_never_carries_reason_comment_or_dates(client, H, types):
    emp, mgr = await H("employee"), await H("manager")
    secret = "hiv-test-secret-reason"
    rid = (await apply(client, emp, types["CL"], "2026-11-02", reason=secret)).json()["id"]
    pushed = OUTBOX[-1]
    assert set(pushed["payload"]) == {"type", "id", "title", "body"}
    assert pushed["payload"]["type"] == "new_request" and pushed["payload"]["id"] == rid
    OUTBOX.clear()
    await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={"comment": "comment-secret-xyz"})
    dec = OUTBOX[-1]["payload"]
    assert set(dec) == {"type", "id", "title", "body"} and dec["type"] == "leave_decision"
    blob = json.dumps(list(OUTBOX))
    assert secret not in blob and "comment-secret-xyz" not in blob and "2026-11" not in blob


async def test_approve_and_cancel_exclude_each_other_in_both_orders(client, H, types):
    """Two orders, sequentially: whichever lands first wins, the other gets 409 NOT_PENDING, balance stays consistent.
    (True interleaving needs PostgreSQL row locks; see the gather test below for the best SQLite can do.)"""
    emp, mgr = await H("employee"), await H("manager")
    a = (await apply(client, emp, types["CL"], "2026-11-02", "2026-11-03")).json()["id"]
    assert (await client.put(f"/api/admin/requests/leave/{a}/approve", headers=mgr, json={})).status_code == 200
    assert code(await client.post(f"/api/leave/requests/{a}/cancel", headers=emp)) == "NOT_PENDING"
    b = (await apply(client, emp, types["CL"], "2026-11-09", "2026-11-10")).json()["id"]
    assert (await client.post(f"/api/leave/requests/{b}/cancel", headers=emp)).status_code == 200
    assert code(await client.put(f"/api/admin/requests/leave/{b}/approve", headers=mgr, json={})) == "NOT_PENDING"
    bal = (await client.get("/api/leave/balance", headers=emp)).json()["leave"]
    cl = next(x for x in bal if x["code"] == "CL")
    assert (cl["used"], cl["pending"], cl["remaining"]) == (2.0, 0.0, 10.0)


async def test_contract_note_permission_cancel_follows_allow_cancel_approved(client, H, db):
    emp, mgr = await H("employee"), await H("manager")
    p = (await client.post("/api/permission/requests", headers=emp, json=perm())).json()["id"]
    await client.put(f"/api/admin/requests/permission/{p}/approve", headers=mgr, json={})
    assert code(await client.post(f"/api/permission/requests/{p}/cancel", headers=emp)) == "NOT_PENDING"
    db.add(Setting(key="leave.allow_cancel_approved", value=True))
    await db.commit()
    assert (await client.post(f"/api/permission/requests/{p}/cancel", headers=emp)).status_code == 200


async def test_leave_settings_are_editable_and_validated(client, H):
    adm = await H("admin")
    r = await client.put(
        "/api/admin/settings", headers=adm, json={"settings": {"leave.reason_retention_days": 730, "leave.max_advance_days": 400}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["leave.reason_retention_days"] == 730
    bad = await client.put("/api/admin/settings", headers=adm, json={"settings": {"leave.reason_retention_days": -1}})
    assert bad.status_code == 422
