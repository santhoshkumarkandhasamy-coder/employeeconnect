"""On Duty requests: apply / list / cancel / approve / reject / auto-cancel / retention (plan 13 section 5 "Requests").
2026-10-21 (Wed) 11:00 IST is "now"."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Attendance, AuditLog, OnDutyRequest, User
from app.services import on_duty as od_svc
from app.services.notifications import OUTBOX
from tests.conftest import bearer
from tests.on_duty_helpers import WED, ist, make_leave, make_loc, set_setting, user_id, visit_in


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = ist(2026, 10, 21, 11, 0)
    monkeypatch.setattr("app.services.attendance.utcnow", lambda: now)
    monkeypatch.setattr("app.services.late.utcnow", lambda: now)
    monkeypatch.setattr("app.services.leave.utcnow", lambda: now)
    OUTBOX.clear()
    st = get_settings()
    monkeypatch.setattr(st, "leave_write_rate_per_min", 1000)
    monkeypatch.setattr(st, "leave_write_rate_per_day", 100000)
    monkeypatch.setattr(st, "leave_decide_rate_per_min", 1000)
    return now


@pytest.fixture
async def H(tokens_for):
    cache: dict = {}

    async def _h(role):
        if role not in cache:
            cache[role] = bearer(await tokens_for(role))
        return cache[role]

    return _h


@pytest.fixture
async def loc(db):
    return await make_loc(db)


def d(offset: int) -> str:
    return (WED + dt.timedelta(days=offset)).isoformat()


def req(f=0, t=None, **kw):
    return {
        "from_date": d(f),
        "to_date": d(f if t is None else t),
        "location_id": None,
        "purpose": "Installation at client site",
        **kw,
    }


async def apply(client, h, **kw):
    return await client.post("/api/on-duty/requests", json=req(**kw), headers=h)


def code(r) -> str:
    return r.json()["error"]["code"]


# ------------------------------------------------------------------ apply
async def test_apply_shape_audit_and_push(client, H, loc, db):
    r = await apply(client, await H("employee"), f=2, t=4, location_id=str(loc.id))
    assert r.status_code == 201, r.text
    b = r.json()
    assert set(b) == {
        "id", "from_date", "to_date", "days", "location", "purpose", "status", "approver", "decision_comment", "decided_at", "created_at",
    }  # fmt: skip
    assert b["days"] == 3 and b["status"] == "pending" and b["location"] == {"id": str(loc.id), "name": "Acme Client"}
    assert b["purpose"] == "Installation at client site" and b["approver"] is None
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "on_duty.apply"))).scalars().one()
    assert a.entity_type == "on_duty_request" and a.entity_id == b["id"] and "Installation" not in repr(a.after)
    pushed = [m for m in OUTBOX if m["payload"]["type"] == "new_request"]
    assert len(pushed) == 1
    p = pushed[0]["payload"]
    assert p == {
        "type": "new_request",
        "request_type": "on_duty",
        "id": b["id"],
        "title": "New on duty request",
        "body": "Test Employee (EMP001)",
    }
    assert pushed[0]["user_ids"] == [str(await user_id(db, "manager"))]  # routed to the manager like leave
    raw = (await db.execute(__import__("sqlalchemy").text("SELECT purpose_enc FROM on_duty_requests"))).scalar_one()
    assert isinstance(raw, bytes) and b"Installation" not in raw


async def test_any_location_and_single_day(client, H):
    b = (await apply(client, await H("employee"))).json()
    assert b["location"] is None and b["days"] == 1


async def test_disabled_blocks_apply(client, H, db):
    await set_setting(db, "on_duty.enabled", False)
    r = await apply(client, await H("employee"))
    assert r.status_code == 409 and code(r) == "ON_DUTY_DISABLED"


async def test_date_rules(client, H, db):
    h = await H("employee")
    assert code(await apply(client, h, f=3, t=2)) == "INVALID_DATE_RANGE"
    assert code(await apply(client, h, f=0, t=31)) == "INVALID_DATE_RANGE"  # 32 calendar days
    assert (await apply(client, h, f=0, t=30)).status_code == 201  # 31 days is the maximum
    assert code(await apply(client, h, f=-1)) == "INVALID_DATE_RANGE"  # backdate default 0
    assert code(await apply(client, h, f=61, t=61)) == "INVALID_DATE_RANGE"  # advance default 60 days


async def test_backdate_and_advance_boundaries(client, H, db):
    h = await H("employee")
    await set_setting(db, "on_duty.request_backdate_days", 2)
    await set_setting(db, "on_duty.request_max_advance_days", 10)
    assert (await apply(client, h, f=-2, t=-2)).status_code == 201
    assert (await apply(client, h, f=10, t=10)).status_code == 201
    assert code(await apply(client, h, f=11, t=11)) == "INVALID_DATE_RANGE"
    assert code(await apply(client, h, f=-3, t=-3)) == "INVALID_DATE_RANGE"


async def test_location_must_be_assigned_and_active(client, H, loc, db):
    h = await H("employee")
    for target in (
        str(uuid.uuid4()),
        str((await make_loc(db, "Off", active=False)).id),
        str((await make_loc(db, "Not Mine", assign=("manager",))).id),
    ):
        r = await apply(client, h, location_id=target)
        assert r.status_code == 422 and code(r) == "DUTY_LOCATION_UNAVAILABLE"
    everyone = await make_loc(db, "All", mode="all", assign=())
    assert (await apply(client, h, f=7, location_id=str(everyone.id))).status_code == 201


async def test_purpose_validation_and_cleaning(client, H):
    h = await H("employee")
    for bad in ("", "   ", "<b></b>", "x" * 301):
        r = await apply(client, h, purpose=bad)
        assert r.status_code == 422 and code(r) == "VALIDATION_ERROR", bad
    r = await apply(client, h, purpose="  Site <script>alert(1)</script>visit\n today ")
    assert r.status_code == 201 and r.json()["purpose"] == "Site visit today"
    assert (await apply(client, h, f=5, purpose="x" * 300)).status_code == 201
    r = await client.post("/api/on-duty/requests", json={**req(f=9), "extra": 1}, headers=h)
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"
    r = await client.post("/api/on-duty/requests", json={"from_date": d(0)}, headers=h)
    assert r.status_code == 422


async def test_overlap_with_own_requests_reports_first_clashing_date(client, H):
    h = await H("employee")
    assert (await apply(client, h, f=2, t=4)).status_code == 201
    r = await apply(client, h, f=4, t=6)
    assert r.status_code == 409 and code(r) == "OVERLAPPING_REQUEST"
    assert r.json()["error"]["details"] == [{"field": "date", "message": d(4)}]
    r = await apply(client, h, f=0, t=3)
    assert r.json()["error"]["details"] == [{"field": "date", "message": d(2)}]
    assert (await apply(client, h, f=5, t=6)).status_code == 201  # adjacent is fine
    # other employees are not affected
    assert (await apply(client, await H("manager"), f=2, t=4)).status_code == 201


async def test_cancelled_and_rejected_requests_do_not_block(client, H, db):
    h = await H("employee")
    rid = (await apply(client, h, f=2)).json()["id"]
    assert (await client.post(f"/api/on-duty/requests/{rid}/cancel", headers=h)).status_code == 200
    assert (await apply(client, h, f=2)).status_code == 201


async def test_overlap_with_full_day_leave_only(client, H, db):
    h = await H("employee")
    await make_leave(db, "employee", WED + dt.timedelta(days=3), WED + dt.timedelta(days=4), status="pending")
    r = await apply(client, h, f=2, t=5)
    assert r.status_code == 409 and code(r) == "OVERLAPPING_REQUEST"
    assert r.json()["error"]["details"] == [{"field": "date", "message": d(3)}]
    await make_leave(db, "employee", WED + dt.timedelta(days=10), WED + dt.timedelta(days=10), status="approved")
    assert code(await apply(client, h, f=9, t=11)) == "OVERLAPPING_REQUEST"
    await make_leave(db, "employee", WED + dt.timedelta(days=20), WED + dt.timedelta(days=20), half=True)
    assert (await apply(client, h, f=20, t=20)).status_code == 201  # half-day leave does not block
    await make_leave(db, "employee", WED + dt.timedelta(days=15), WED + dt.timedelta(days=15), status="rejected")
    assert (await apply(client, h, f=15, t=15)).status_code == 201


async def test_apply_shares_the_leave_write_limiter(client, H, monkeypatch):
    h = await H("employee")
    monkeypatch.setattr(get_settings(), "leave_write_rate_per_min", 2)
    assert (await apply(client, h, f=1)).status_code == 201
    assert (await apply(client, h, f=3)).status_code == 201
    r = await apply(client, h, f=5)
    assert r.status_code == 429 and code(r) == "RATE_LIMITED"


# ------------------------------------------------------------------ own requests
async def test_list_get_and_ownership(client, H, db):
    h = await H("employee")
    ids = [(await apply(client, h, f=i * 2)).json()["id"] for i in range(3)]
    rid = ids[0]
    cancelled = (await client.post(f"/api/on-duty/requests/{ids[1]}/cancel", headers=h)).json()
    assert cancelled["status"] == "cancelled"
    page = (await client.get("/api/on-duty/requests?page_size=2", headers=h)).json()
    assert page["total"] == 3 and len(page["items"]) == 2 and page["page_size"] == 2
    assert [i["id"] for i in (await client.get("/api/on-duty/requests?status=cancelled", headers=h)).json()["items"]] == [ids[1]]
    ranged = (await client.get(f"/api/on-duty/requests?from={d(3)}&to={d(5)}", headers=h)).json()  # overlapping range
    assert [i["id"] for i in ranged["items"]] == [ids[2]]
    assert (await client.get("/api/on-duty/requests?status=bogus", headers=h)).status_code == 422
    assert (await client.get(f"/api/on-duty/requests/{rid}", headers=h)).json()["id"] == rid
    other = await H("manager")
    assert (await client.get(f"/api/on-duty/requests/{rid}", headers=other)).status_code == 404  # not the owner
    assert (await client.post(f"/api/on-duty/requests/{rid}/cancel", headers=other)).status_code == 404
    assert (await client.get(f"/api/on-duty/requests/{uuid.uuid4()}", headers=h)).status_code == 404
    assert (await client.get("/api/on-duty/requests", headers=other)).json()["total"] == 0


async def test_cancel_rules(client, H, db):
    h = await H("employee")
    adm = await H("hr")
    pend = (await apply(client, h, f=2)).json()["id"]
    appr = (await apply(client, h, f=5)).json()["id"]
    assert (await client.put(f"/api/admin/requests/on_duty/{appr}/approve", headers=adm)).status_code == 200
    rej = (await apply(client, h, f=8)).json()["id"]
    assert (await client.put(f"/api/admin/requests/on_duty/{rej}/reject", headers=adm, json={"comment": "no"})).status_code == 200
    r = await client.post(f"/api/on-duty/requests/{appr}/cancel", headers=h)
    assert r.status_code == 409 and code(r) == "NOT_PENDING"  # leave.allow_cancel_approved is off
    assert code(await client.post(f"/api/on-duty/requests/{rej}/cancel", headers=h)) == "NOT_PENDING"
    await set_setting(db, "leave.allow_cancel_approved", True)
    assert (await client.post(f"/api/on-duty/requests/{appr}/cancel", headers=h)).json()["status"] == "cancelled"
    started = (await apply(client, h, f=0)).json()["id"]  # starts today: not "before it starts"
    assert (await client.put(f"/api/admin/requests/on_duty/{started}/approve", headers=adm)).status_code == 200
    assert code(await client.post(f"/api/on-duty/requests/{started}/cancel", headers=h)) == "NOT_PENDING"
    assert (await client.post(f"/api/on-duty/requests/{pend}/cancel", headers=h)).status_code == 200
    assert code(await client.post(f"/api/on-duty/requests/{pend}/cancel", headers=h)) == "NOT_PENDING"
    actions = {a.action for a in (await db.execute(select(AuditLog))).scalars()}
    assert "on_duty.cancel" in actions


# ------------------------------------------------------------------ admin list + decisions
async def test_admin_list_scope_filters_and_shape(client, H, db):
    emp, mgr, hr = await H("employee"), await H("manager"), await H("hr")
    mine = (await apply(client, emp, f=2)).json()
    await apply(client, mgr, f=3)  # the manager's own request is not in the manager's team
    r = (await client.get("/api/admin/requests?type=on_duty", headers=mgr)).json()
    assert [i["id"] for i in r["items"]] == [mine["id"]]
    assert r["items"][0]["type"] == "on_duty" and r["items"][0]["user"]["employee_id"] == "EMP001"
    assert (await client.get("/api/admin/requests?type=on_duty", headers=hr)).json()["total"] == 2
    assert (await client.get("/api/admin/requests?type=on_duty&employee_id=emp001", headers=hr)).json()["total"] == 1
    assert (await client.get("/api/admin/requests?type=on_duty&status=approved", headers=hr)).json()["total"] == 0
    assert (await client.get(f"/api/admin/requests?type=on_duty&from={d(3)}&to={d(3)}", headers=hr)).json()["total"] == 1
    assert (await client.get("/api/admin/requests?type=on_duty", headers=emp)).status_code == 403
    assert (await client.get("/api/admin/requests?type=bogus", headers=hr)).status_code == 422


async def test_approve_and_reject_flow_push_and_no_attendance(client, H, db):
    emp, mgr = await H("employee"), await H("manager")
    a = (await apply(client, emp, f=2, t=4)).json()["id"]
    OUTBOX.clear()
    r = await client.put(f"/api/admin/requests/on_duty/{a}/approve", headers=mgr, json={"comment": " ok "})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "approved" and b["decision_comment"] == "ok" and b["approver"]["employee_id"] == "MGR001"
    assert b["type"] == "on_duty" and b["user"]["employee_id"] == "EMP001" and b["decided_at"]
    msg = [m for m in OUTBOX if m["payload"]["type"] == "on_duty_decision"]
    assert len(msg) == 1 and msg[0]["payload"] == {
        "type": "on_duty_decision", "id": a, "title": "On duty request approved", "body": "23 Oct - 25 Oct 2026",
    }  # fmt: skip
    assert msg[0]["user_ids"] == [str(await user_id(db, "employee"))]
    assert (await db.execute(select(Attendance))).first() is None  # approval creates no attendance
    # reject
    c = (await apply(client, emp, f=10)).json()["id"]
    OUTBOX.clear()
    assert (
        await client.put(f"/api/admin/requests/on_duty/{c}/reject", headers=mgr, json={})
    ).status_code == 422  # comment required
    assert (await client.put(f"/api/admin/requests/on_duty/{c}/reject", headers=mgr, json={"comment": "  "})).status_code == 422
    r = await client.put(f"/api/admin/requests/on_duty/{c}/reject", headers=mgr, json={"comment": "not needed"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["decision_comment"] == "not needed"
    assert [m["payload"]["title"] for m in OUTBOX if m["payload"]["type"] == "on_duty_decision"] == ["On duty request rejected"]
    actions = [(x.action, x.entity_type) for x in (await db.execute(select(AuditLog))).scalars()]
    assert ("request.approve", "on_duty_request") in actions and ("request.reject", "on_duty_request") in actions


async def test_decision_guards(client, H, db):
    emp, mgr, hr, adm = await H("employee"), await H("manager"), await H("hr"), await H("admin")
    rid = (await apply(client, emp, f=2)).json()["id"]
    assert (await client.put(f"/api/admin/requests/on_duty/{rid}/approve", headers=emp)).status_code == 403  # no permission
    own = (await apply(client, hr, f=2)).json()["id"]
    r = await client.put(f"/api/admin/requests/on_duty/{own}/approve", headers=hr)
    assert r.status_code == 403 and code(r) == "FORBIDDEN"  # self-approval
    assert (await client.put(f"/api/admin/requests/on_duty/{own}/approve", headers=mgr)).status_code == 404  # out of scope
    assert (await client.put(f"/api/admin/requests/on_duty/{uuid.uuid4()}/approve", headers=hr)).status_code == 404
    assert (await client.put(f"/api/admin/requests/on_duty/{own}/approve", headers=adm)).status_code == 200
    assert (await client.put(f"/api/admin/requests/on_duty/{rid}/approve", headers=hr)).status_code == 200
    r = await client.put(f"/api/admin/requests/on_duty/{rid}/approve", headers=hr)
    assert r.status_code == 409 and code(r) == "NOT_PENDING"
    assert code(await client.put(f"/api/admin/requests/on_duty/{rid}/reject", headers=hr, json={"comment": "x"})) == "NOT_PENDING"
    denied = [a.after for a in (await db.execute(select(AuditLog).where(AuditLog.action == "request.decide_denied"))).scalars()]
    assert {x["why"] for x in denied} == {"self", "scope"}
    refused = [
        a.after["code"] for a in (await db.execute(select(AuditLog).where(AuditLog.action == "request.decide_refused"))).scalars()
    ]
    assert refused.count("NOT_PENDING") == 2


async def test_approve_inactive_requester_and_leave_overlap_rechecked(client, H, db):
    emp, hr = await H("employee"), await H("hr")
    rid = (await apply(client, emp, f=2, t=4)).json()["id"]
    await make_leave(db, "employee", WED + dt.timedelta(days=3), WED + dt.timedelta(days=3), status="approved")
    r = await client.put(f"/api/admin/requests/on_duty/{rid}/approve", headers=hr)
    assert r.status_code == 409 and code(r) == "OVERLAPPING_REQUEST"
    assert r.json()["error"]["details"] == [{"field": "date", "message": d(3)}]
    row = await db.get(OnDutyRequest, uuid.UUID(rid))
    await db.refresh(row)
    assert row.status == "pending"
    # rejecting is still possible
    assert (
        await client.put(f"/api/admin/requests/on_duty/{rid}/reject", headers=hr, json={"comment": "leave"})
    ).status_code == 200
    rid2 = (await apply(client, emp, f=10)).json()["id"]
    u = await db.get(User, await user_id(db, "employee"))
    u.status = "inactive"
    await db.commit()
    r = await client.put(f"/api/admin/requests/on_duty/{rid2}/approve", headers=hr)
    assert r.status_code == 409 and code(r) == "REQUESTER_INACTIVE"


async def test_leave_items_flag_on_duty_conflict(client, H, db):
    emp, hr = await H("employee"), await H("hr")
    rid = (await apply(client, emp, f=3, t=3)).json()["id"]
    await client.put(f"/api/admin/requests/on_duty/{rid}/approve", headers=hr)
    await make_leave(db, "employee", WED + dt.timedelta(days=3), WED + dt.timedelta(days=3), status="pending")
    await make_leave(db, "employee", WED + dt.timedelta(days=9), WED + dt.timedelta(days=9), status="pending")
    items = (await client.get("/api/admin/requests?type=leave&status=pending", headers=hr)).json()["items"]
    flags = {i["from_date"]: i["on_duty_conflict"] for i in items}
    assert flags == {d(3): True, d(9): False}
    # permission items are untouched
    assert all(
        "on_duty_conflict" not in i for i in (await client.get("/api/admin/requests?type=permission", headers=hr)).json()["items"]
    )


async def test_leave_wins_over_approved_on_duty_at_check_in(client, H, loc, db, clock):
    emp, hr = await H("employee"), await H("hr")
    rid = (await apply(client, emp, f=0)).json()["id"]
    await client.put(f"/api/admin/requests/on_duty/{rid}/approve", headers=hr)
    await make_leave(db, "employee", WED, WED)
    r = await visit_in(client, emp, loc)
    assert r.status_code == 409 and code(r) == "ON_LEAVE"


# ------------------------------------------------------------------ deactivation, retention, home
async def test_deactivate_auto_cancels_pending_and_leaves_open_visit(client, H, loc, db):
    emp, adm = await H("employee"), await H("admin")
    pend = (await apply(client, emp, f=2)).json()["id"]
    appr = (await apply(client, emp, f=6)).json()["id"]
    await client.put(f"/api/admin/requests/on_duty/{appr}/approve", headers=await H("hr"))
    assert (await visit_in(client, emp, loc)).status_code == 201
    eid = await user_id(db, "employee")
    assert (await client.post(f"/api/admin/employees/{eid}/deactivate", headers=adm)).status_code == 200
    db.expire_all()
    assert (await db.get(OnDutyRequest, uuid.UUID(pend))).status == "cancelled"
    assert (await db.get(OnDutyRequest, uuid.UUID(appr))).status == "approved"  # only PENDING requests are auto-cancelled
    audits = (await db.execute(select(AuditLog).where(AuditLog.action == "on_duty.auto_cancel"))).scalars().all()
    assert len(audits) == 1 and audits[0].after["reason"] == "user_deactivated" and audits[0].entity_id == pend
    from app.models import OnDutyVisit

    v = (await db.execute(select(OnDutyVisit))).scalar_one()
    assert v.check_out_at is None and v.auto_closed is False  # left for the 00:30 job


async def test_delete_employee_auto_cancels_pending(client, H, db):
    emp, adm = await H("employee"), await H("admin")
    pend = (await apply(client, emp, f=2)).json()["id"]
    eid = await user_id(db, "employee")
    assert (await client.delete(f"/api/admin/employees/{eid}", headers=adm)).status_code == 204
    db.expire_all()
    assert (await db.get(OnDutyRequest, uuid.UUID(pend))).status == "cancelled"
    assert (await db.execute(select(AuditLog).where(AuditLog.action == "on_duty.auto_cancel"))).scalars().one().after[
        "reason"
    ] == "user_deleted"


async def test_retention_blanks_purpose_and_comment_only_for_old_decided_requests(client, H, db):
    emp, hr = await H("employee"), await H("hr")
    old = OnDutyRequest(
        user_id=await user_id(db, "employee"), from_date=dt.date(2026, 1, 5), to_date=dt.date(2026, 1, 6), purpose="old purpose",
        status="approved", decision_comment="old comment",
    )  # fmt: skip
    old_pending = OnDutyRequest(
        user_id=await user_id(db, "employee"),
        from_date=dt.date(2026, 1, 8),
        to_date=dt.date(2026, 1, 8),
        purpose="keep",
        status="pending",
    )
    db.add_all([old, old_pending])
    await db.commit()
    old_id, pending_id = old.id, old_pending.id
    recent = (await apply(client, emp, f=2)).json()["id"]
    await client.put(f"/api/admin/requests/on_duty/{recent}/approve", headers=hr, json={"comment": "fine"})
    assert await od_svc.purge_old_text(db) == 0  # leave.reason_retention_days default 0 = keep forever
    await set_setting(db, "leave.reason_retention_days", 30)
    assert await od_svc.purge_old_text(db) == 1
    db.expire_all()
    got = await db.get(OnDutyRequest, old_id)
    assert got.purpose == "" and got.decision_comment is None
    assert (await db.get(OnDutyRequest, pending_id)).purpose == "keep"
    r = (await client.get(f"/api/on-duty/requests/{recent}", headers=emp)).json()
    assert r["purpose"] == "Installation at client site" and r["decision_comment"] == "fine"
    assert await od_svc.purge_old_text(db) == 0  # idempotent


async def test_home_pending_requests_counts_on_duty(client, H):
    emp = await H("employee")
    await apply(client, emp, f=2)
    await apply(client, emp, f=5)
    assert (await client.get("/api/home", headers=emp)).json()["pending_requests"] == 2
