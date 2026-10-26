"""On Duty admin side: duty locations + assignments (`duty_location.manage`), admin visits, dashboard / day view, settings,
export (plan 13 sections 3, 5 and 8)."""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.models import (
    AuditLog,
    Department,
    DutyLocationAssignment,
    OnDutyRequest,
    OnDutyVisit,
    Setting,
    User,
)
from app.services import settings as settings_svc
from tests.conftest import PASSWORD, bearer, login_tokens
from tests.on_duty_helpers import WED, body_in, ist, make_loc, office_in, set_setting, user_id, visit_in, visit_out
from tests.test_plan11_license import _expired, admin, vendor  # noqa: F401  (fixtures + helper reused for the read_only test)


@pytest.fixture
def clock(monkeypatch):
    def _set(when: dt.datetime):
        monkeypatch.setattr("app.services.attendance.utcnow", lambda: when)
        monkeypatch.setattr("app.services.late.utcnow", lambda: when)
        monkeypatch.setattr("app.services.console.utcnow", lambda: when)

    _set(ist(2026, 10, 21, 11, 0))
    return _set


@pytest.fixture
async def H(tokens_for):
    cache: dict = {}

    async def _h(role):
        if role not in cache:
            cache[role] = bearer(await tokens_for(role))
        return cache[role]

    return _h


def code(r) -> str:
    return r.json()["error"]["code"]


def loc_body(**over):
    return {"name": "Acme Client - Guindy", "address": "12 Mount Rd", "lat": 13.0067, "lng": 80.2206, "radius_m": 200, **over}


# ------------------------------------------------------------------ permission
async def test_permission_matrix(client, H, db):
    loc = await make_loc(db)
    paths = [
        ("get", "/api/admin/duty-locations"),
        ("post", "/api/admin/duty-locations"),
        ("get", f"/api/admin/duty-locations/{loc.id}"),
        ("put", f"/api/admin/duty-locations/{loc.id}"),
        ("delete", f"/api/admin/duty-locations/{loc.id}"),
        ("get", f"/api/admin/duty-locations/{loc.id}/assignments"),
        ("put", f"/api/admin/duty-locations/{loc.id}/assignments"),
        ("get", f"/api/admin/employees/{uuid.uuid4()}/duty-locations"),
    ]
    for method, path in paths:
        r = await getattr(client, method)(path)
        assert r.status_code == 401, (method, path)
        for role in ("employee", "manager"):  # neither holds duty_location.manage
            r = await getattr(client, method)(path, headers=await H(role), **({"json": {}} if method in ("post", "put") else {}))
            assert r.status_code == 403 and code(r) == "FORBIDDEN", (method, path, role)
    assert (await client.get("/api/admin/duty-locations", headers=await H("hr"))).status_code == 200
    assert (await client.get("/api/admin/duty-locations", headers=await H("admin"))).status_code == 200


# ------------------------------------------------------------------ CRUD
async def test_create_get_update_delete_with_audit(client, H, db):
    hr = await H("hr")
    r = await client.post("/api/admin/duty-locations", headers=hr, json=loc_body(name="  Acme   Client  "))
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["name"] == "Acme Client" and b["assign_mode"] == "selected" and b["is_active"] is True and b["radius_m"] == 200
    assert b["assigned_count"] == 0 and b["in_use"] is False and b["created_by"]["employee_id"] == "HR001"
    assert b["updated_by"]["employee_id"] == "HR001" and b["created_at"].endswith("Z")
    lid = b["id"]
    assert (await client.get(f"/api/admin/duty-locations/{lid}", headers=hr)).json()["id"] == lid
    r = await client.put(
        f"/api/admin/duty-locations/{lid}",
        headers=await H("admin"),
        json=loc_body(name="Acme HQ", radius_m=300, is_active=False, address=None),
    )
    assert (
        r.status_code == 200 and r.json()["name"] == "Acme HQ" and r.json()["is_active"] is False and r.json()["address"] is None
    )
    assert r.json()["updated_by"]["employee_id"] == "ADM001" and r.json()["created_by"]["employee_id"] == "HR001"
    assert (await client.delete(f"/api/admin/duty-locations/{lid}", headers=hr)).status_code == 204
    assert (await client.get(f"/api/admin/duty-locations/{lid}", headers=hr)).status_code == 404
    acts = [
        a
        for a in (
            await db.execute(select(AuditLog).where(AuditLog.entity_type == "duty_location").order_by(AuditLog.id))
        ).scalars()
    ]
    assert [a.action for a in acts] == ["duty_location.create", "duty_location.update", "duty_location.delete"]
    assert acts[0].after["lat"] == 13.0067 and acts[0].actor_employee_id == "HR001"  # coordinates are business data
    assert acts[1].before["name"] == "Acme Client" and acts[1].after["name"] == "Acme HQ"


async def test_validation(client, H):
    hr = await H("hr")
    bad = [
        {"name": "x"}, {"name": "x" * 81}, {"name": "   "}, {"name": "<b></b>"}, {"address": "a" * 301},
        {"lat": 90.1}, {"lat": -91}, {"lng": 180.1}, {"lng": -181}, {"radius_m": 9}, {"radius_m": 2001}, {"radius_m": 150.5},
        {"assign_mode": "everyone"}, {"is_active": "yes"}, {"surprise": 1},
    ]  # fmt: skip
    for over in bad:
        r = await client.post("/api/admin/duty-locations", headers=hr, json=loc_body(**over))
        assert r.status_code == 422 and code(r) == "VALIDATION_ERROR", over
    for missing in ("name", "lat", "lng", "radius_m"):
        b = loc_body()
        del b[missing]
        assert (await client.post("/api/admin/duty-locations", headers=hr, json=b)).status_code == 422, missing
    ok = [{"radius_m": 10}, {"radius_m": 2000, "name": "Big"}, {"lat": 90, "name": "C1"}, {"lng": -180, "name": "C2"}]
    for i, over in enumerate(ok):
        over.setdefault("name", f"Edge {i}")
        assert (await client.post("/api/admin/duty-locations", headers=hr, json=loc_body(**over))).status_code == 201, over
    r = await client.put(f"/api/admin/duty-locations/{uuid.uuid4()}", headers=hr, json=loc_body())
    assert r.status_code == 404
    assert (await client.get("/api/admin/duty-locations/not-a-uuid", headers=hr)).status_code == 422


async def test_duplicate_name_case_insensitive_409(client, H):
    hr = await H("hr")
    first = (await client.post("/api/admin/duty-locations", headers=hr, json=loc_body(name="Branch One"))).json()
    r = await client.post("/api/admin/duty-locations", headers=hr, json=loc_body(name=" BRANCH one "))
    assert r.status_code == 409 and code(r) == "CONFLICT" and r.json()["error"]["details"][0]["field"] == "name"
    other = (await client.post("/api/admin/duty-locations", headers=hr, json=loc_body(name="Branch Two"))).json()
    r = await client.put(f"/api/admin/duty-locations/{other['id']}", headers=hr, json=loc_body(name="branch one"))
    assert r.status_code == 409
    assert (
        await client.put(f"/api/admin/duty-locations/{first['id']}", headers=hr, json=loc_body(name="BRANCH ONE"))
    ).status_code == 200


async def test_list_filters_search_pagination_and_counts(client, H, db):
    hr = await H("hr")
    await make_loc(db, "Alpha Works", assign=("employee", "manager"))
    await make_loc(db, "beta_site", mode="all", assign=())
    await make_loc(db, "Gamma", active=False)
    r = (await client.get("/api/admin/duty-locations", headers=hr)).json()
    assert [i["name"] for i in r["items"]] == ["Alpha Works", "beta_site", "Gamma"] and r["total"] == 3
    by = {i["name"]: i for i in r["items"]}
    assert by["Alpha Works"]["assigned_count"] == 2
    assert by["beta_site"]["assigned_count"] == 4  # all active employees
    assert by["Gamma"]["assigned_count"] == 1 and by["Gamma"]["is_active"] is False
    assert [i["name"] for i in (await client.get("/api/admin/duty-locations?q=mount", headers=hr)).json()["items"]] == [
        "Alpha Works",
        "beta_site",
        "Gamma",
    ]  # the address matches too
    assert [i["name"] for i in (await client.get("/api/admin/duty-locations?q=beta_", headers=hr)).json()["items"]] == [
        "beta_site"
    ]
    assert (await client.get("/api/admin/duty-locations?q=%25", headers=hr)).json()["total"] == 0  # wildcard is escaped
    assert (await client.get("/api/admin/duty-locations?is_active=false", headers=hr)).json()["total"] == 1
    assert (await client.get("/api/admin/duty-locations?assign_mode=all", headers=hr)).json()["total"] == 1
    assert (await client.get("/api/admin/duty-locations?assign_mode=bogus", headers=hr)).status_code == 422
    page = (await client.get("/api/admin/duty-locations?page=2&page_size=2", headers=hr)).json()
    assert len(page["items"]) == 1 and page["total"] == 3 and page["page_size"] == 2


async def test_delete_in_use_is_409_and_deactivate_works(client, H, db, clock):
    hr = await H("hr")
    used_by_visit = await make_loc(db, "Visited")
    used_by_request = await make_loc(db, "Requested")
    free = await make_loc(db, "Free")
    emp = await H("employee")
    assert (await visit_in(client, emp, used_by_visit)).status_code == 201
    db.add(
        OnDutyRequest(
            user_id=await user_id(db, "employee"), from_date=WED, to_date=WED, location_id=used_by_request.id, purpose="p"
        )
    )
    await db.commit()
    for loc in (used_by_visit, used_by_request):
        r = await client.delete(f"/api/admin/duty-locations/{loc.id}", headers=hr)
        assert r.status_code == 409 and code(r) == "IN_USE"
        assert (await client.get(f"/api/admin/duty-locations/{loc.id}", headers=hr)).json()["in_use"] is True
    assert (await client.delete(f"/api/admin/duty-locations/{free.id}", headers=hr)).status_code == 204
    r = await client.put(
        f"/api/admin/duty-locations/{used_by_visit.id}", headers=hr, json=loc_body(name="Visited", is_active=False)
    )
    assert r.status_code == 200 and r.json()["is_active"] is False
    # editing coordinates never rewrites past visits
    await client.put(
        f"/api/admin/duty-locations/{used_by_visit.id}", headers=hr, json=loc_body(name="Visited Renamed", lat=1.0, lng=2.0)
    )
    v = (await db.execute(select(OnDutyVisit).execution_options(populate_existing=True))).scalar_one()
    assert v.location_name == "Visited"  # snapshot
    items = (await client.get("/api/on-duty/visits", headers=emp)).json()["items"]
    assert items[0]["location"]["name"] == "Visited"


async def test_deleting_location_removes_assignments(client, H, db):
    hr = await H("hr")
    loc = await make_loc(db, assign=("employee", "manager"))
    assert (await client.delete(f"/api/admin/duty-locations/{loc.id}", headers=hr)).status_code == 204
    assert (await db.execute(select(DutyLocationAssignment))).first() is None


# ------------------------------------------------------------------ assignments
async def test_assignments_get_put_full_replace_and_audit(client, H, db):
    hr = await H("hr")
    loc = await make_loc(db, assign=("employee",))
    r = (await client.get(f"/api/admin/duty-locations/{loc.id}/assignments", headers=hr)).json()
    assert r["location_id"] == str(loc.id) and r["assign_mode"] == "selected"
    assert [i["user"]["employee_id"] for i in r["items"]] == ["EMP001"] and r["items"][0]["assigned_at"].endswith("Z")
    mgr_id, adm_id, emp_id = str(await user_id(db, "manager")), str(await user_id(db, "admin")), str(await user_id(db, "employee"))
    r = await client.put(  # the admin may include itself (review 11 M2: HR may not, see test_qa_review11_on_duty_fixes)
        f"/api/admin/duty-locations/{loc.id}/assignments",
        headers=await H("admin"),
        json={"assign_mode": "selected", "user_ids": [mgr_id, adm_id]},
    )
    assert r.status_code == 200, r.text
    assert [i["user"]["employee_id"] for i in r.json()["items"]] == [
        "ADM001",
        "MGR001",
    ]  # replaced: EMP001 is gone, sorted by employee id
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "duty_location.assignments"))).scalars().one()
    assert (
        a.after["assign_mode"] == "selected"
        and sorted(a.after["added"]) == sorted([mgr_id, adm_id])
        and a.after["removed"] == [emp_id]
    )
    assert "Test " not in repr(a.after)  # ids only, never names
    # clearing the list is allowed
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments", headers=hr, json={"assign_mode": "selected", "user_ids": []}
    )
    assert r.status_code == 200 and r.json()["items"] == []


async def test_assignments_all_mode_keeps_rows_and_switching_back_restores(client, H, db):
    hr = await H("hr")
    loc = await make_loc(db, assign=("employee",))
    emp_id = str(await user_id(db, "employee"))
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments", headers=hr, json={"assign_mode": "all", "user_ids": [emp_id]}
    )
    assert r.json()["assign_mode"] == "all" and len(r.json()["items"]) == 1
    assert (await client.get(f"/api/admin/duty-locations/{loc.id}", headers=hr)).json()["assigned_count"] == 4
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments", headers=hr, json={"assign_mode": "selected", "user_ids": [emp_id]}
    )
    assert [i["user"]["employee_id"] for i in r.json()["items"]] == ["EMP001"]
    emp = await H("employee")
    assert len((await client.get("/api/on-duty/locations", headers=emp)).json()["items"]) == 1
    # employees not on the list lose it
    assert (await client.get("/api/on-duty/locations", headers=await H("manager"))).json()["items"] == []


async def test_assignments_department_expansion_and_validation(client, H, db, seeded):
    hr = await H("hr")
    loc = await make_loc(db, assign=())
    emp_id = str(await user_id(db, "employee"))
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments",
        headers=hr,
        json={"assign_mode": "selected", "user_ids": [emp_id], "add_department_ids": [str(seeded["dept"])]},
    )
    assert r.status_code == 200
    assert len(r.json()["items"]) == 4  # the department's four active members merged with the explicit id (no duplicates)
    # later joiners are not added automatically (no department link stored)
    dept2 = Department(name="Ops", code="OPS")
    db.add(dept2)
    await db.commit()
    newbie = User(
        employee_id="NEW001",
        password_hash="x",
        full_name="New Person",
        role_id=seeded["roles"]["employee"],
        department_id=seeded["dept"],
        status="active",
    )
    db.add(newbie)
    await db.commit()
    assert len((await client.get(f"/api/admin/duty-locations/{loc.id}/assignments", headers=hr)).json()["items"]) == 4
    # errors
    ghost, ghost2 = str(uuid.uuid4()), str(uuid.uuid4())
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments",
        headers=hr,
        json={"assign_mode": "selected", "user_ids": [emp_id, ghost]},
    )
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"
    assert r.json()["error"]["details"] == [{"field": "user_ids", "message": ghost}]
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments",
        headers=hr,
        json={"assign_mode": "selected", "user_ids": [], "add_department_ids": [ghost2]},
    )
    assert r.status_code == 422 and r.json()["error"]["details"] == [{"field": "add_department_ids", "message": ghost2}]
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments",
        headers=hr,
        json={"assign_mode": "selected", "user_ids": [emp_id, emp_id]},
    )
    assert r.status_code == 422
    assert (
        len((await client.get(f"/api/admin/duty-locations/{loc.id}/assignments", headers=hr)).json()["items"]) == 4
    )  # unchanged after errors
    for bad in (
        {"user_ids": []},  # assign_mode required
        {"assign_mode": "selected"},  # user_ids required
        {"assign_mode": "selected", "user_ids": [], "extra": 1},
        {"assign_mode": "selected", "user_ids": [str(uuid.uuid4()) for _ in range(5001)]},
        {"assign_mode": "selected", "user_ids": [], "add_department_ids": [str(uuid.uuid4()) for _ in range(51)]},
    ):
        r = await client.put(f"/api/admin/duty-locations/{loc.id}/assignments", headers=hr, json=bad)
        assert r.status_code == 422, list(bad)
    assert (
        await client.put(
            f"/api/admin/duty-locations/{uuid.uuid4()}/assignments", headers=hr, json={"assign_mode": "all", "user_ids": []}
        )
    ).status_code == 404


async def test_assignments_reject_inactive_users_and_listing_hides_them(client, H, db):
    hr = await H("hr")
    loc = await make_loc(db, assign=("employee", "manager"))
    mgr = await db.get(User, await user_id(db, "manager"))
    mgr.status = "inactive"
    await db.commit()
    got = (await client.get(f"/api/admin/duty-locations/{loc.id}/assignments", headers=hr)).json()["items"]
    assert [i["user"]["employee_id"] for i in got] == ["EMP001"]  # inactive users are hidden
    assert (await client.get(f"/api/admin/duty-locations/{loc.id}", headers=hr)).json()["assigned_count"] == 1
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments",
        headers=hr,
        json={"assign_mode": "selected", "user_ids": [str(mgr.id)]},
    )
    assert r.status_code == 422
    # a replace removes the rows of previously assigned inactive users
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments",
        headers=hr,
        json={"assign_mode": "selected", "user_ids": [str(await user_id(db, "employee"))]},
    )
    assert r.status_code == 200
    assert len((await db.execute(select(DutyLocationAssignment))).scalars().all()) == 1


async def test_assignments_put_rate_limit_10_per_minute(client, H, db):
    hr = await H("hr")
    loc = await make_loc(db, assign=())
    codes = [
        (
            await client.put(
                f"/api/admin/duty-locations/{loc.id}/assignments", headers=hr, json={"assign_mode": "selected", "user_ids": []}
            )
        ).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [200] * 10 and codes[10] == 429


async def test_location_write_rate_limit_30_per_minute(client, H):
    hr = await H("hr")
    codes = [
        (await client.post("/api/admin/duty-locations", headers=hr, json=loc_body(name=f"Site {i}"))).status_code
        for i in range(31)
    ]
    assert codes[:30] == [201] * 30 and codes[30] == 429


async def test_employee_duty_locations_view(client, H, db):
    hr = await H("hr")
    a = await make_loc(db, "Selected", assign=("employee",))
    await make_loc(db, "Everyone", mode="all", assign=())
    await make_loc(db, "Off", active=False, assign=("employee",))
    await make_loc(db, "Not Theirs", assign=("manager",))
    eid = await user_id(db, "employee")
    r = (await client.get(f"/api/admin/employees/{eid}/duty-locations", headers=hr)).json()
    assert [(i["name"], i["is_active"], i["assign_mode"]) for i in r["items"]] == [
        ("Everyone", True, "all"), ("Off", False, "selected"), ("Selected", True, "selected"),
    ]  # fmt: skip
    assert set(r["items"][0]) == {"id", "name", "is_active", "assign_mode"} and a.id
    assert (await client.get(f"/api/admin/employees/{uuid.uuid4()}/duty-locations", headers=hr)).status_code == 404
    emp = await db.get(User, eid)
    from app.core.clock import utcnow

    emp.deleted_at = utcnow()
    await db.commit()
    assert (await client.get(f"/api/admin/employees/{eid}/duty-locations", headers=hr)).status_code == 404


# ------------------------------------------------------------------ admin visits
async def _two_days_of_visits(client, H, db, clock):
    emp = await H("employee")
    a = await make_loc(db, "Site A")
    b = await make_loc(db, "Site B", assign=("employee", "hr"))
    clock(ist(2026, 10, 20, 10, 0))
    await visit_in(client, emp, a)
    clock(ist(2026, 10, 20, 12, 0))
    await visit_out(client, emp, lat=13.003)  # outside: flagged
    clock(ist(2026, 10, 21, 10, 0))
    await visit_in(client, emp, b)
    clock(ist(2026, 10, 21, 10, 30))
    hr = await H("hr")
    await visit_in(client, hr, b)
    return a, b


async def test_admin_visits_scope_fields_and_filters(client, H, db, clock):
    a, b = await _two_days_of_visits(client, H, db, clock)
    hr, mgr, emp = await H("hr"), await H("manager"), await H("employee")
    r = (await client.get("/api/admin/on-duty/visits?from=2026-10-20&to=2026-10-21", headers=hr)).json()
    assert r["total"] == 3 and [i["user"]["employee_id"] for i in r["items"]] == ["HR001", "EMP001", "EMP001"]  # check_in_at desc
    full = [i for i in r["items"] if i["location"]["name"] == "Site A"][0]
    assert full["check_out_within_fence"] is False and full["check_out_distance_m"] == 334 and full["check_in_distance_m"] == 100
    assert full["check_in_accuracy_m"] == 10 and full["check_out_accuracy_m"] == 10 and full["suspicious"] is False
    assert full["visit_minutes"] == 120 and full["open"] is False and full["user"]["full_name"] == "Test Employee"
    # manager: only direct reports, and the view_all-only fields are null
    rm = (await client.get("/api/admin/on-duty/visits?from=2026-10-20&to=2026-10-21", headers=mgr)).json()
    assert rm["total"] == 2 and {i["user"]["employee_id"] for i in rm["items"]} == {"EMP001"}
    for i in rm["items"]:
        for k in (
            "check_in_distance_m",
            "check_out_distance_m",
            "check_out_within_fence",
            "check_in_accuracy_m",
            "check_out_accuracy_m",
            "suspicious",
        ):
            assert i[k] is None, k
    assert (await client.get("/api/admin/on-duty/visits", headers=emp)).status_code == 403
    # filters
    q = "/api/admin/on-duty/visits"
    assert (await client.get(f"{q}?date=2026-10-20", headers=hr)).json()["total"] == 1
    assert (await client.get(f"{q}?date=2026-10-20&from=2026-01-01&to=2026-01-02", headers=hr)).json()[
        "total"
    ] == 1  # date overrides
    assert (await client.get(f"{q}?date=2026-10-21&location_id={b.id}", headers=hr)).json()["total"] == 2
    assert (await client.get(f"{q}?date=2026-10-21&location_id={a.id}", headers=hr)).json()["total"] == 0
    assert (await client.get(f"{q}?date=2026-10-21&location_id={uuid.uuid4()}", headers=hr)).json()[
        "total"
    ] == 0  # unknown -> empty, not 404
    assert (await client.get(f"{q}?date=2026-10-21&department_id={uuid.uuid4()}", headers=hr)).json()["total"] == 0
    assert (await client.get(f"{q}?date=2026-10-21&employee_id=emp001", headers=hr)).json()["total"] == 1
    assert (await client.get(f"{q}?date=2026-10-21&open=true", headers=hr)).json()["total"] == 2
    assert (await client.get(f"{q}?date=2026-10-20&open=true", headers=hr)).json()["total"] == 0
    assert (await client.get(f"{q}?from=2026-10-21&to=2026-10-21&page_size=1&page=2", headers=hr)).json()["items"][0]["user"][
        "employee_id"
    ] == "EMP001"
    assert code(await client.get(f"{q}?from=2025-01-01&to=2026-10-21", headers=hr)) == "INVALID_DATE_RANGE"  # > 366 days
    assert (await client.get(f"{q}?from=2026-10-22&to=2026-10-21", headers=hr)).status_code == 422
    # defaults are bounded (no unbounded scan) and still answer
    assert (await client.get(q, headers=hr)).status_code == 200


async def test_admin_visits_rate_limit_60_per_minute(client, H):
    hr = await H("hr")
    codes = [(await client.get("/api/admin/on-duty/visits?date=2026-10-21", headers=hr)).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


async def test_visit_selfie_urls_and_suspicious_visible_to_view_all_only(client, H, db, clock):
    from app.models import File
    from tests.on_duty_helpers import make_file

    loc = await make_loc(db)
    emp = await H("employee")
    fid = await make_file(db)
    await set_setting(db, "attendance.reject_mock_location", False)
    await visit_in(client, emp, loc, selfie_file_id=str(fid), is_mock_location=True)
    hr, mgr = await H("hr"), await H("manager")
    item = (await client.get("/api/admin/on-duty/visits?date=2026-10-21", headers=hr)).json()["items"][0]
    assert item["suspicious"] is True and item["check_in_selfie_url"] == f"/api/files/{fid}"
    assert (await client.get("/api/admin/on-duty/visits?date=2026-10-21", headers=mgr)).json()["items"][0]["suspicious"] is None
    assert await db.get(File, fid) is not None


# ------------------------------------------------------------------ dashboard + day view (console)
async def test_dashboard_and_day_view_on_duty(client, H, db, clock):
    loc = await make_loc(db, "Zone", assign=("employee", "hr"))
    emp, hr = await H("employee"), await H("hr")
    await visit_in(client, emp, loc)  # EMP001: open visit, on_duty
    clock(ist(2026, 10, 21, 10, 0))
    assert (await office_in(client, hr)).status_code == 201  # HR001: office only
    adm = bearer((await client.post("/api/auth/login", json={"employee_id": "ADM001", "password": PASSWORD})).json())
    d = (await client.get("/api/admin/dashboard", headers=adm)).json()
    assert d["attendance"]["on_duty"] == 1 and d["attendance"]["on_duty_open"] == 1 and d["attendance"]["checked_in"] == 2
    assert d["requests"]["on_duty_pending"] == 0
    db.add(OnDutyRequest(user_id=await user_id(db, "employee"), from_date=WED, to_date=WED, purpose="p"))
    await db.commit()
    assert (await client.get("/api/admin/dashboard", headers=adm)).json()["requests"]["on_duty_pending"] == 1
    day = (await client.get("/api/admin/attendance/day?date=2026-10-21&page_size=100", headers=hr)).json()["items"]
    by = {i["user"]["employee_id"]: i for i in day}
    assert by["EMP001"]["work_mode"] == "on_duty" and by["EMP001"]["on_duty_locations"] == ["Zone"]
    assert by["HR001"]["work_mode"] == "office" and by["HR001"]["on_duty_locations"] == []
    assert by["MGR001"]["work_mode"] is None and by["MGR001"]["on_duty_locations"] == []  # no row
    assert not {"lat", "lng", "distance_m", "selfie_url", "accuracy_m"} & set(by["EMP001"])
    only = (await client.get("/api/admin/attendance/day?date=2026-10-21&status=on_duty", headers=hr)).json()
    assert [i["user"]["employee_id"] for i in only["items"]] == ["EMP001"] and only["total"] == 1
    assert (await client.get("/api/admin/attendance/day?date=2026-10-21&status=bogus", headers=hr)).status_code == 422
    # mixed counts as on duty too
    clock(ist(2026, 10, 21, 11, 0))
    await visit_in(client, hr, loc, lat=13.0)
    only = (await client.get("/api/admin/attendance/day?date=2026-10-21&status=on_duty", headers=hr)).json()
    assert {i["user"]["employee_id"] for i in only["items"]} == {"EMP001", "HR001"}


# ------------------------------------------------------------------ settings
async def test_settings_keys_and_validation(client, H, db):
    adm = await H("admin")
    cur = (await client.get("/api/admin/settings", headers=adm)).json()["settings"]
    assert {k: cur[k] for k in cur if k.startswith("on_duty.")} == {
        "on_duty.enabled": True, "on_duty.approval_required": False, "on_duty.apply_late_rule": False, "on_duty.checkout_geofence": False,
        "on_duty.selfie_required": False, "on_duty.max_visits_per_day": 10, "on_duty.request_backdate_days": 0,
        "on_duty.request_max_advance_days": 60,
    }  # fmt: skip
    ok = {
        "on_duty.approval_required": True,
        "on_duty.max_visits_per_day": 50,
        "on_duty.request_backdate_days": 30,
        "on_duty.request_max_advance_days": 365,
    }
    r = await client.put("/api/admin/settings", headers=adm, json={"settings": ok})
    assert r.status_code == 200 and r.json()["settings"]["on_duty.max_visits_per_day"] == 50
    bad = [
        ("on_duty.enabled", 1), ("on_duty.enabled", "true"), ("on_duty.approval_required", None), ("on_duty.max_visits_per_day", 0),
        ("on_duty.max_visits_per_day", 51), ("on_duty.max_visits_per_day", 2.5), ("on_duty.max_visits_per_day", True),
        ("on_duty.request_backdate_days", -1), ("on_duty.request_backdate_days", 31), ("on_duty.request_max_advance_days", 0),
        ("on_duty.request_max_advance_days", 366), ("on_duty.unknown", 1),
    ]  # fmt: skip
    for k, v in bad:
        r = await client.put("/api/admin/settings", headers=adm, json={"settings": {k: v}})
        assert r.status_code == 422 and code(r) == "VALIDATION_ERROR", (k, v)
    r = await client.put("/api/admin/settings", headers=adm, json={"settings": {"on_duty.max_visits_per_day": 5.0}})
    assert r.status_code == 200 and r.json()["settings"]["on_duty.max_visits_per_day"] == 5  # integral float normalised
    assert (await client.put("/api/admin/settings", headers=await H("hr"), json={"settings": ok})).status_code == 403
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "settings.update"))).scalars().first()
    assert log is not None


def test_every_known_setting_has_a_spec():
    assert set(settings_svc.KNOWN) <= set(settings_svc.SPECS)
    assert {k for k in settings_svc.KNOWN if k.startswith("on_duty.")} == {
        k for k in settings_svc.SPECS if k.startswith("on_duty.")
    }


async def test_settings_row_is_picked_up_live(client, H, db):
    await set_setting(db, "on_duty.max_visits_per_day", 1)
    assert isinstance(await db.get(Setting, "on_duty.max_visits_per_day"), Setting)
    loc = await make_loc(db)
    emp = await H("employee")
    from tests.on_duty_helpers import fix  # noqa: F401

    t = (await client.get("/api/on-duty/today", headers=emp)).json()
    assert t["visits_remaining"] == 1 and body_in(loc)["location_id"] == str(loc.id)


# ------------------------------------------------------------------ export
def _sheet(content: bytes, name: str) -> list[dict]:
    ws = load_workbook(io.BytesIO(content))[name]
    rows = list(ws.iter_rows(values_only=True))
    return [dict(zip(rows[0], r, strict=True)) for r in rows[1:]]


async def test_export_on_duty_sheet_columns_privacy_and_attendance_columns(client, H, db, clock, redis):
    loc = await make_loc(db, "Export Site")
    emp, hr, mgr = await H("employee"), await H("hr"), await H("manager")
    clock(ist(2026, 10, 21, 10, 0))
    await visit_in(client, emp, loc, note="client call")
    clock(ist(2026, 10, 21, 12, 15))
    await visit_out(client, emp, lat=13.003)  # outside the site: "Check-out Within Site" = No
    clock(ist(2026, 10, 21, 13, 0))
    await visit_in(client, emp, loc)
    q = {"from": "2026-10-21", "to": "2026-10-21", "type": "on_duty"}
    r = await client.get("/api/admin/export", headers=hr, params=q)
    assert r.status_code == 200, r.text
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["On Duty"]
    ws = wb["On Duty"]
    assert [c.value for c in ws[1]] == [
        "Date", "Employee ID", "Name", "Department", "Location", "Check-in", "Check-out", "Visit Minutes", "Auto Closed",
        "Check-out Within Site",
    ]  # fmt: skip
    assert ws.freeze_panes == "A2" and ws["A1"].font.bold
    rows = _sheet(r.content, "On Duty")
    assert len(rows) == 2
    first, second = rows
    assert first["Employee ID"] == "EMP001" and first["Location"] == "Export Site" and first["Visit Minutes"] == 135
    assert first["Check-out Within Site"] == "No" and first["Auto Closed"] == "No"
    assert second["Check-out"] is None and second["Visit Minutes"] is None and second["Check-out Within Site"] in (None, "")
    blob = repr(rows)
    assert "client call" not in blob and "13.0" not in blob  # no note, no coordinates, no distances
    # note only with include_reasons=true (needs export.sensitive)
    r = await client.get("/api/admin/export", headers=hr, params={**q, "include_reasons": "true"})
    assert [c.value for c in load_workbook(io.BytesIO(r.content))["On Duty"][1]][-1] == "Note"
    assert _sheet(r.content, "On Duty")[0]["Note"] == "client call"
    await redis.flushall()
    assert (await client.get("/api/admin/export", headers=mgr, params={**q, "include_reasons": "true"})).status_code == 403
    # manager: own team only; employee: refused
    r = await client.get("/api/admin/export", headers=mgr, params=q)
    assert r.status_code == 200 and {x["Employee ID"] for x in _sheet(r.content, "On Duty")} == {"EMP001"}
    assert (await client.get("/api/admin/export", headers=emp, params=q)).status_code == 403
    # type=all contains the sheet, and Attendance has Mode + On Duty Locations after Within Geofence
    await redis.flushall()
    r = await client.get("/api/admin/export", headers=hr, params={**q, "type": "all"})
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Attendance", "Leave", "Late", "Permission", "On Duty"]
    heads = [c.value for c in wb["Attendance"][1]]
    assert heads[-3:] == ["Within Geofence", "Mode", "On Duty Locations"]
    att = _sheet(r.content, "Attendance")[0]
    assert att["Mode"] == "on_duty" and att["On Duty Locations"] == "Export Site" and att["Within Geofence"] in (None, "")


async def test_export_filters_for_on_duty(client, H, db, clock, redis):
    loc = await make_loc(db, "F Site", assign=("employee", "hr"))
    emp, hr = await H("employee"), await H("hr")
    clock(ist(2026, 10, 21, 10, 0))
    await visit_in(client, emp, loc)
    await visit_in(client, hr, loc)
    base = {"from": "2026-10-21", "to": "2026-10-21", "type": "on_duty"}
    r = await client.get("/api/admin/export", headers=hr, params={**base, "employee_id": "emp001"})
    assert [x["Employee ID"] for x in _sheet(r.content, "On Duty")] == ["EMP001"]
    await redis.flushall()
    r = await client.get("/api/admin/export", headers=hr, params={**base, "from": "2026-10-22", "to": "2026-10-22"})
    assert _sheet(r.content, "On Duty") == []
    await redis.flushall()
    r = await client.get("/api/admin/export", headers=hr, params={**base, "department_id": str(uuid.uuid4())})
    assert r.status_code == 404


# ------------------------------------------------------------------ license read_only


async def test_license_read_only_refuses_visit_punches_and_request_writes(client, admin, vendor, db):  # noqa: F811
    emp = bearer(await login_tokens(client, "EMP001"))
    loc = await make_loc(db)
    await _expired(client, admin, vendor, db, "read_only")
    refused = lambda r: r.status_code == 403 and r.json()["error"]["code"] == "LICENSE_EXPIRED"  # noqa: E731
    assert refused(await client.post("/api/on-duty/check-in", headers=emp, json=body_in(loc)))
    assert refused(await client.post("/api/on-duty/check-out", headers=emp, json={}))
    assert refused(
        await client.post(
            "/api/on-duty/requests", headers=emp, json={"from_date": "2026-12-01", "to_date": "2026-12-01", "purpose": "x"}
        )
    )
    assert refused(await client.post(f"/api/on-duty/requests/{uuid.uuid4()}/cancel", headers=emp))
    assert refused(await client.post("/api/admin/duty-locations", headers=admin, json=loc_body(name="Blocked")))
    assert (await client.get("/api/on-duty/today", headers=emp)).status_code == 200  # reads keep working
    assert (await client.get("/api/on-duty/locations", headers=emp)).status_code == 200
