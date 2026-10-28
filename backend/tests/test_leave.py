"""Leave + permission (plan 03). 2026-10-21 is a Wednesday (office tz Asia/Kolkata).
SQLite: the EXCLUDE constraints and real FOR UPDATE row locks are NOT exercised here (the services' pre-checks are)."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import AuditLog, File, Holiday, LeaveBalance, LeaveType, Setting, User
from app.services import leave as lv
from app.services.notifications import OUTBOX
from tests.conftest import USERS, bearer

TODAY = dt.datetime(2026, 10, 21, 4, 30, tzinfo=dt.UTC)  # 10:00 IST
ZERO_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("app.services.leave.utcnow", lambda: TODAY)
    OUTBOX.clear()
    st = get_settings()  # the write limits have dedicated tests; everything else must not trip them
    monkeypatch.setattr(st, "leave_write_rate_per_min", 1000)
    monkeypatch.setattr(st, "leave_write_rate_per_day", 100000)
    monkeypatch.setattr(st, "leave_decide_rate_per_min", 1000)


@pytest.fixture
async def types(db):
    cl = LeaveType(code="CL", name="Casual Leave", annual_quota=12, is_paid=True, allow_half_day=True)
    sl = LeaveType(code="SL", name="Sick Leave", annual_quota=6, allow_half_day=False, requires_attachment_after_days=2)
    lop = LeaveType(code="LOP", name="Loss of Pay", annual_quota=0, is_paid=False, allow_half_day=True)
    db.add_all([cl, sl, lop])
    await db.commit()
    return {"CL": cl.id, "SL": sl.id, "LOP": lop.id}


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


def perm(date="2026-10-22", f="10:00", t="11:00"):
    return {"date": date, "from_time": f, "to_time": t, "reason": "errand"}


async def bal(client, h, code="CL", year=None):
    r = await client.get("/api/leave/balance", headers=h, params={"year": year} if year else None)
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["leave"] if x["code"] == code), r.json()


async def apply(client, h, tid, f, t=None, **kw):
    return await client.post("/api/leave/requests", headers=h, json=body(tid, f, t, **kw))


def code(r):
    return r.json()["error"]["code"]


# ------------------------------------------------------------------ balances
async def test_types_and_lazy_balance(client, H, types):
    h = await H("employee")
    assert {t["code"] for t in (await client.get("/api/leave/types", headers=h)).json()["items"]} == {"CL", "SL", "LOP"}
    b, full = await bal(client, h)
    assert (b["quota"], b["used"], b["pending"], b["remaining"]) == (12.0, 0.0, 0.0, 12.0)
    assert (await bal(client, h, "LOP"))[0]["quota"] == 365.0
    assert full["permission"]["limit_count"] == 3 and full["permission"]["remaining_minutes"] == 240


def test_pro_rata_quota():
    lt = LeaveType(code="X", name="X", annual_quota=12, is_paid=True)
    u = User(join_date=dt.date(2026, 7, 1))
    assert lv.initial_quota(lt, u, 2026) == 6.0  # 184/365*12 = 6.05 -> 6.0
    assert lv.initial_quota(lt, u, 2027) == 12.0
    assert lv.initial_quota(lt, User(join_date=dt.date(2026, 1, 1)), 2026) == 12.0
    assert lv.initial_quota(lt, User(join_date=dt.date(2026, 12, 31)), 2026) == 0.0  # 0.03 -> 0
    assert lv.initial_quota(lt, User(join_date=dt.date(2026, 10, 1)), 2026) == 3.0  # 92/365*12 = 3.02 -> 3.0


# ------------------------------------------------------------------ apply
async def test_apply_working_days_and_pending(client, H, types, db):
    h = await H("employee")
    db.add(Holiday(date=dt.date(2026, 11, 4), name="Festival", is_active=True))
    await db.commit()
    r = await apply(client, h, types["CL"], "2026-11-02", "2026-11-06")  # Mon-Fri minus holiday Wed
    assert r.status_code == 201, r.text
    assert r.json()["days"] == 4.0 and r.json()["status"] == "pending" and r.json()["leave_type"]["code"] == "CL"
    b, _ = await bal(client, h)
    assert (b["pending"], b["remaining"]) == (4.0, 8.0)
    mgr = (await db.execute(select(User.id).where(User.employee_id == USERS["manager"]))).scalar_one()
    assert OUTBOX[-1]["payload"]["type"] == "new_request" and OUTBOX[-1]["user_ids"] == [str(mgr)]


async def test_weekend_only_zero_days(client, H, types):
    r = await apply(client, await H("employee"), types["CL"], "2026-11-07", "2026-11-08")
    assert r.status_code == 422 and code(r) == "ZERO_WORKING_DAYS"


async def test_insufficient_balance_counts_pending_and_never_negative(client, H, types, db):
    h = await H("employee")
    assert (await apply(client, h, types["CL"], "2026-11-02", "2026-11-10")).status_code == 201  # 7 days pending
    assert (await apply(client, h, types["CL"], "2026-11-16", "2026-11-20")).status_code == 201  # +5 = 12 exactly
    r = await apply(client, h, types["CL"], "2026-11-23")  # 13 > 12
    assert r.status_code == 409 and code(r) == "INSUFFICIENT_BALANCE"
    b, _ = await bal(client, h)
    assert b["remaining"] == 0.0 and b["pending"] == 12.0
    rows = (await db.execute(select(LeaveBalance))).scalars().all()
    assert all(float(x.quota) >= float(x.used) + float(x.pending) >= 0 for x in rows)


async def test_overlap_rejected_until_cancelled(client, H, types):
    h = await H("employee")
    first = await apply(client, h, types["CL"], "2026-11-02", "2026-11-04")
    assert first.status_code == 201
    for f, t in (("2026-11-04", "2026-11-05"), ("2026-11-01", "2026-11-09"), ("2026-11-03", "2026-11-03")):
        r = await apply(client, h, types["CL"], f, t)
        assert r.status_code == 409 and code(r) == "OVERLAPPING_REQUEST"
    assert (await client.post(f"/api/leave/requests/{first.json()['id']}/cancel", headers=h)).status_code == 200
    assert (await apply(client, h, types["CL"], "2026-11-03")).status_code == 201


async def test_half_day(client, H, types):
    h = await H("employee")
    r = await apply(client, h, types["CL"], "2026-11-03", half_day=True, half_day_session="first")
    assert r.status_code == 201 and r.json()["days"] == 0.5 and r.json()["half_day_session"] == "first"
    assert (await bal(client, h))[0]["pending"] == 0.5
    r = await apply(client, h, types["SL"], "2026-11-09", half_day=True, half_day_session="second")
    assert code(r) == "HALF_DAY_NOT_ALLOWED"
    r = await apply(client, h, types["CL"], "2026-11-09", "2026-11-10", half_day=True, half_day_session="second")
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"
    assert code(await apply(client, h, types["CL"], "2026-11-09", half_day=True)) == "VALIDATION_ERROR"  # no session
    r = await apply(client, h, types["CL"], "2026-11-07", half_day=True, half_day_session="first")  # Saturday
    assert code(r) == "ZERO_WORKING_DAYS"


async def test_year_boundary_and_backdate(client, H, types):
    h = await H("employee")
    r = await apply(client, h, types["CL"], "2026-12-30", "2027-01-05")
    assert r.status_code == 422 and code(r) == "INVALID_DATE_RANGE"
    assert (await apply(client, h, types["CL"], "2026-12-30", "2026-12-31")).status_code == 201
    assert (await apply(client, h, types["CL"], "2027-01-01", "2027-01-05")).status_code == 201
    b26, _ = await bal(client, h, year=2026)
    b27, _ = await bal(client, h, year=2027)
    assert b26["pending"] == 2.0 and b27["pending"] == 3.0 and b27["quota"] == 12.0  # no carry-forward
    assert code(await apply(client, h, types["CL"], "2026-10-13")) == "INVALID_DATE_RANGE"  # 8 days back
    assert (await apply(client, h, types["CL"], "2026-10-14")).status_code == 201  # exactly 7 days back
    assert code(await apply(client, h, types["CL"], "2026-11-12", "2026-11-11")) == "INVALID_DATE_RANGE"


async def test_attachment_rules(client, H, types, db, seeded):
    h = await H("employee")
    r = await apply(client, h, types["SL"], "2026-11-02", "2026-11-04")
    assert r.status_code == 422 and code(r) == "ATTACHMENT_REQUIRED"
    assert (await apply(client, h, types["SL"], "2026-11-02", "2026-11-03")).status_code == 201  # 2 days: not required
    emp, mgr = seeded["users"]["employee"], seeded["users"]["manager"]
    mine = File(owner_id=emp, purpose="leave_attachment", storage_path="aa/x.bin", content_type="application/pdf", size_bytes=1)
    other = File(owner_id=mgr, purpose="leave_attachment", storage_path="aa/y.bin", content_type="application/pdf", size_bytes=1)
    selfie = File(owner_id=emp, purpose="selfie", storage_path="aa/z.bin", content_type="image/png", size_bytes=1)
    db.add_all([mine, other, selfie])
    await db.commit()
    for f in (other, selfie):
        r = await apply(client, h, types["SL"], "2026-11-09", "2026-11-11", attachment_file_id=str(f.id))
        assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"
    r = await apply(client, h, types["SL"], "2026-11-09", "2026-11-11", attachment_file_id=str(mine.id))
    assert r.status_code == 201 and r.json()["attachment_url"] == f"/api/files/{mine.id}"


# ------------------------------------------------------------------ approval flow
async def test_approve_moves_pending_to_used_with_audit_and_notify(client, H, types, db):
    emp, mgr = await H("employee"), await H("manager")
    rid = (await apply(client, emp, types["CL"], "2026-11-02", "2026-11-03")).json()["id"]
    OUTBOX.clear()
    r = await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={"comment": "ok"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "approved" and j["decision_comment"] == "ok" and j["approver"]["employee_id"] == "MGR001"
    assert j["type"] == "leave" and j["user"]["employee_id"] == "EMP001"
    b, _ = await bal(client, emp)
    assert (b["used"], b["pending"], b["remaining"]) == (2.0, 0.0, 10.0)
    assert OUTBOX[-1]["payload"]["type"] == "leave_decision"
    audit = (await db.execute(select(AuditLog).where(AuditLog.action == "request.approve"))).scalars().all()
    assert len(audit) == 1 and audit[0].actor_employee_id == "MGR001"
    again = await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={})
    assert again.status_code == 409 and code(again) == "NOT_PENDING"  # the "second approver" loses
    c = await client.post(f"/api/leave/requests/{rid}/cancel", headers=emp)  # approved leave: no cancel in v1
    assert c.status_code == 409 and code(c) == "NOT_PENDING"
    assert (await bal(client, emp))[0]["used"] == 2.0


async def test_reject_requires_comment_and_restores(client, H, types):
    emp, mgr = await H("employee"), await H("manager")
    rid = (await apply(client, emp, types["CL"], "2026-11-02", "2026-11-03")).json()["id"]
    for bad in ({}, {"comment": "  "}):
        r = await client.put(f"/api/admin/requests/leave/{rid}/reject", headers=mgr, json=bad)
        assert r.status_code == 422
    r = await client.put(f"/api/admin/requests/leave/{rid}/reject", headers=mgr, json={"comment": "busy week"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    b, _ = await bal(client, emp)
    assert (b["used"], b["pending"], b["remaining"]) == (0.0, 0.0, 12.0)
    assert (await apply(client, emp, types["CL"], "2026-11-02", "2026-11-03")).status_code == 201  # dates free again


async def test_cancel_pending_restores_and_owner_only(client, H, types):
    emp, hr = await H("employee"), await H("hr")
    rid = (await apply(client, emp, types["CL"], "2026-11-02", "2026-11-03")).json()["id"]
    assert (await client.post(f"/api/leave/requests/{rid}/cancel", headers=hr)).status_code == 404
    assert (await client.get(f"/api/leave/requests/{rid}", headers=hr)).status_code == 404  # IDOR -> 404
    r = await client.post(f"/api/leave/requests/{rid}/cancel", headers=emp)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert (await bal(client, emp))[0]["remaining"] == 12.0
    assert code(await client.post(f"/api/leave/requests/{rid}/cancel", headers=emp)) == "NOT_PENDING"


async def test_cancel_approved_behind_setting_restores_used(client, H, types, db):
    emp, mgr = await H("employee"), await H("manager")
    rid = (await apply(client, emp, types["CL"], "2026-11-02", "2026-11-03")).json()["id"]
    await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={})
    db.add(Setting(key="leave.allow_cancel_approved", value=True))
    await db.commit()
    assert (await client.post(f"/api/leave/requests/{rid}/cancel", headers=emp)).status_code == 200
    b, _ = await bal(client, emp)
    assert (b["used"], b["remaining"]) == (0.0, 12.0)


async def test_rbac_scope(client, H, types):
    emp, mgr, hr, adm = await H("employee"), await H("manager"), await H("hr"), await H("admin")
    assert (await client.get("/api/admin/requests", headers=emp)).status_code == 403
    emp_id = (await apply(client, emp, types["CL"], "2026-11-02")).json()["id"]
    assert (await client.put(f"/api/admin/requests/leave/{emp_id}/approve", headers=emp, json={})).status_code == 403
    hr_id = (await apply(client, hr, types["CL"], "2026-11-02")).json()["id"]  # HR has no manager
    items = (await client.get("/api/admin/requests?type=leave", headers=mgr)).json()["items"]
    assert [i["id"] for i in items] == [emp_id]  # manager: direct reports only
    assert (
        await client.put(f"/api/admin/requests/leave/{hr_id}/approve", headers=mgr, json={})
    ).status_code == 404  # out of scope
    assert (await client.put(f"/api/admin/requests/leave/{hr_id}/approve", headers=hr, json={})).status_code == 403  # self
    assert len((await client.get("/api/admin/requests?type=leave", headers=hr)).json()["items"]) == 2
    flt = await client.get("/api/admin/requests?type=leave&status=pending&employee_id=emp001", headers=adm)
    assert [i["id"] for i in flt.json()["items"]] == [emp_id]
    assert (await client.put(f"/api/admin/requests/leave/{hr_id}/approve", headers=adm, json={})).status_code == 200
    assert (await client.put(f"/api/admin/requests/leave/{emp_id}/approve", headers=hr, json={})).status_code == 200
    assert (await client.put(f"/api/admin/requests/leave/{ZERO_ID}/approve", headers=adm, json={})).status_code == 404
    assert (await client.put(f"/api/admin/requests/bogus/{emp_id}/approve", headers=adm, json={})).status_code == 422


async def test_no_manager_notifies_all_approvers(client, H, types, db):
    await apply(client, await H("hr"), types["CL"], "2026-11-02")
    adm = (await db.execute(select(User.id).where(User.employee_id == USERS["admin"]))).scalar_one()
    assert OUTBOX[-1]["user_ids"] == [str(adm)]  # requester excluded; admin holds request.approve_all


async def test_deactivated_type_still_approvable(client, H, types, db):
    emp, mgr = await H("employee"), await H("manager")
    rid = (await apply(client, emp, types["CL"], "2026-11-02")).json()["id"]
    lt = await db.get(LeaveType, types["CL"])
    lt.is_active = False
    await db.commit()
    assert (await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={})).status_code == 200
    assert code(await apply(client, emp, types["CL"], "2026-11-09")) == "LEAVE_TYPE_INACTIVE"


async def test_my_requests_list_filters(client, H, types):
    emp = await H("employee")
    a = (await apply(client, emp, types["CL"], "2026-11-02")).json()["id"]
    await apply(client, emp, types["CL"], "2026-11-09")
    await client.post(f"/api/leave/requests/{a}/cancel", headers=emp)
    r = await client.get("/api/leave/requests", headers=emp, params={"status": "pending"})
    assert r.json()["total"] == 1 and r.json()["items"][0]["from_date"] == "2026-11-09"
    r = await client.get("/api/leave/requests", headers=emp, params={"from": "2026-11-01", "to": "2026-11-05"})
    assert [i["id"] for i in r.json()["items"]] == [a]
    assert (await client.get("/api/leave/requests", headers=emp, params={"status": "x"})).status_code == 422


async def test_rapid_submits_never_overdraw(client, H, types):
    """True concurrency is not testable here: the SQLite harness shares ONE connection between all sessions, so
    interleaved transactions corrupt each other. Row locks / EXCLUDE need PostgreSQL. This checks the serial path."""
    h = await H("employee")
    res = [await apply(client, h, types["CL"], f"2026-11-{d:02d}", f"2026-11-{d + 4:02d}") for d in (2, 9, 16, 23)]
    assert [r.status_code for r in res] == [201, 201, 409, 409]
    b, _ = await bal(client, h)
    assert b["pending"] == 10.0 and b["remaining"] == 2.0


# ------------------------------------------------------------------ permission
async def test_permission_apply_balance_and_flow(client, H):
    emp, mgr = await H("employee"), await H("manager")
    r = await client.post("/api/permission/requests", headers=emp, json=perm(f="16:00", t="17:30"))
    assert r.status_code == 201, r.text
    p = r.json()
    assert (p["date"], p["from_time"], p["to_time"], p["minutes"], p["status"]) == ("2026-10-22", "16:00", "17:30", 90, "pending")
    pm = (await client.get("/api/leave/balance", headers=emp)).json()["permission"]
    assert (pm["used_count"], pm["pending_count"], pm["remaining_count"], pm["remaining_minutes"]) == (0, 1, 2, 150)
    a = await client.put(f"/api/admin/requests/permission/{p['id']}/approve", headers=mgr, json={})
    assert a.status_code == 200 and a.json()["type"] == "permission"
    pm = (await client.get("/api/leave/balance", headers=emp)).json()["permission"]
    assert (pm["used_count"], pm["used_minutes"], pm["pending_count"]) == (1, 90, 0)
    assert OUTBOX[-1]["payload"]["type"] == "permission_decision"
    assert code(await client.post(f"/api/permission/requests/{p['id']}/cancel", headers=emp)) == "NOT_PENDING"
    assert (await client.get(f"/api/permission/requests/{p['id']}", headers=emp)).status_code == 200
    assert (await client.get(f"/api/permission/requests/{p['id']}", headers=mgr)).status_code == 404


async def test_permission_limits_and_validation(client, H):
    emp = await H("employee")
    P = "/api/permission/requests"
    assert code(await client.post(P, headers=emp, json=perm(f="11:00", t="10:00"))) == "INVALID_TIME_RANGE"
    assert code(await client.post(P, headers=emp, json=perm(f="09:00", t="10:00"))) == "INVALID_TIME_RANGE"  # before shift
    assert code(await client.post(P, headers=emp, json=perm(f="10:00", t="12:30"))) == "INVALID_TIME_RANGE"  # > 120
    assert code(await client.post(P, headers=emp, json=perm(date="2026-10-20"))) == "INVALID_TIME_RANGE"  # past
    assert code(await client.post(P, headers=emp, json=perm(date="2026-10-24"))) == "INVALID_TIME_RANGE"  # Saturday
    first = await client.post(P, headers=emp, json=perm(f="10:00", t="11:00"))
    assert first.status_code == 201
    r = await client.post(P, headers=emp, json=perm(f="10:30", t="11:30"))
    assert r.status_code == 409 and code(r) == "OVERLAPPING_REQUEST"
    assert (await client.post(P, headers=emp, json=perm(f="12:00", t="13:00"))).status_code == 201
    assert (await client.post(P, headers=emp, json=perm(f="14:00", t="15:00"))).status_code == 201
    r = await client.post(P, headers=emp, json=perm(f="16:00", t="17:00"))  # 4th in the month
    assert r.status_code == 409 and code(r) == "PERMISSION_LIMIT_EXCEEDED" and r.json()["error"]["details"][0]["field"] == "count"
    # November has its own allowance; minutes limit: 3 x 90 = 270 > 240
    assert (await client.post(P, headers=emp, json=perm(date="2026-11-02", f="10:00", t="11:30"))).status_code == 201
    assert (await client.post(P, headers=emp, json=perm(date="2026-11-03", f="10:00", t="11:30"))).status_code == 201
    r = await client.post(P, headers=emp, json=perm(date="2026-11-04", f="10:00", t="11:30"))
    assert code(r) == "PERMISSION_LIMIT_EXCEEDED" and r.json()["error"]["details"][0]["field"] == "minutes"
    rid = first.json()["id"]  # a cancelled request frees the quota
    assert (await client.post(f"{P}/{rid}/cancel", headers=emp)).status_code == 200
    assert (await client.post(P, headers=emp, json=perm(f="16:00", t="17:00"))).status_code == 201
    assert (await client.get(P, headers=emp, params={"status": "cancelled"})).json()["total"] == 1


async def test_permission_blocked_by_approved_leave(client, H, types):
    emp, mgr = await H("employee"), await H("manager")
    rid = (await apply(client, emp, types["CL"], "2026-10-22")).json()["id"]
    await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={})
    r = await client.post("/api/permission/requests", headers=emp, json=perm())
    assert r.status_code == 409 and code(r) == "OVERLAPPING_REQUEST"


# ------------------------------------------------------------------ jobs / infra
async def test_rollover_job_idempotent(db, types, seeded):
    assert await lv.rollover_balances(db, 2027) == 4 * 3  # 4 users x 3 active types
    assert await lv.rollover_balances(db, 2027) == 0
    assert len((await db.execute(select(LeaveBalance).where(LeaveBalance.year == 2027))).scalars().all()) == 12


async def test_pending_request_follows_current_manager(client, H, types, db, seeded):
    emp, hr, mgr = await H("employee"), await H("hr"), await H("manager")
    rid = (await apply(client, emp, types["CL"], "2026-11-02")).json()["id"]
    u = await db.get(User, seeded["users"]["employee"])
    u.manager_id = seeded["users"]["hr"]
    await db.commit()
    assert (await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={})).status_code == 404
    assert (await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=hr, json={})).status_code == 200


def test_scheduler_builds_jobs():
    from app.jobs.scheduler import build_scheduler

    s = build_scheduler("Asia/Kolkata")
    assert {j.id for j in s.get_jobs()} == {
        "attendance.auto_close_open_records",
        "files.cleanup_orphans",
        "attendance.retention",
        "leave.year_rollover",
        "leave.retention",
        "announcements.retention",
        "license.expiry_alert",
        "worker.heartbeat",
        "tasks.reminders",
    }
    trig = str(s.get_job("attendance.auto_close_open_records").trigger)
    assert "hour='0'" in trig and "minute='30'" in trig


def test_settings_optional_blank(monkeypatch):
    from app.core.config import Settings

    base = {"DATABASE_URL": "sqlite+aiosqlite://", "JWT_SECRET": "x" * 40, "DATA_ENCRYPTION_KEYS": "k", "APP_ENV": "test"}
    for k, v in {**base, "FCM_CREDENTIALS_FILE": "", "SMTP_HOST": "", "SMTP_PORT": "", "AUDIT_HMAC_KEY": ""}.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.fcm_credentials_file is None and s.smtp_host is None and s.smtp_port == 587 and s.audit_hmac_key is None
    monkeypatch.setenv("AUDIT_HMAC_KEY", "short")
    with pytest.raises(ValueError):
        Settings()
    monkeypatch.setenv("AUDIT_HMAC_KEY", "a" * 32)
    monkeypatch.setenv("SMTP_HOST", "mail")
    with pytest.raises(ValueError):  # SMTP_FROM missing
        Settings()
