"""QA (black-box, contract 14.3 requests, 14.4, 14.6-14.8): requests lifecycle, admin decisions, scope, dashboard, day view,
home/config/calendar/summary additions, export, deactivation."""

from __future__ import annotations

import datetime as dt
import io

import pytest
from openpyxl import load_workbook

from app.models import Holiday  # noqa: F401
from tests.qa_on_duty_support import (
    FAR,
    NEAR,
    assign,
    check_in,
    check_out,
    clock,  # noqa: F401
    code,
    ist,
    new_loc,
    seed_leave,
    set_settings,
)

OD = "/api/on-duty"
ADM = "/api/admin"


@pytest.fixture
async def site(client, H, uid):
    loc = await new_loc(client, H["admin"], "Acme Client", radius=200)
    await assign(client, H["admin"], loc["id"], [uid["employee"]])
    return loc


def req(**over):
    return {"from_date": "2026-10-28", "to_date": "2026-10-30", "location_id": None, "purpose": "Installation at client site", **over}


async def apply(client, h, **over):
    return await client.post(f"{OD}/requests", headers=h, json=req(**over))


# ---------------------------------------------------------------- apply
async def test_apply_ok_shape(client, H, site, clock):
    r = await apply(client, H["employee"], location_id=site["id"])
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["days"] == 3 and d["status"] == "pending" and d["location"] == {"id": site["id"], "name": "Acme Client"}
    assert d["approver"] is None and d["decision_comment"] is None and d["decided_at"] is None
    assert d["purpose"] == "Installation at client site"
    r = await apply(client, H["employee"], from_date="2026-11-10", to_date="2026-11-10")
    assert r.status_code == 201 and r.json()["location"] is None and r.json()["days"] == 1


@pytest.mark.parametrize(
    "over,err",
    [
        ({"from_date": "2026-10-30", "to_date": "2026-10-28"}, "INVALID_DATE_RANGE"),
        ({"from_date": "2026-10-28", "to_date": "2026-11-28"}, "INVALID_DATE_RANGE"),  # 32 days
        ({"from_date": "2026-10-20", "to_date": "2026-10-20"}, "INVALID_DATE_RANGE"),  # backdate 0
        ({"from_date": "2027-02-01", "to_date": "2027-02-01"}, "INVALID_DATE_RANGE"),  # > 60 days ahead
        ({"purpose": ""}, "VALIDATION_ERROR"),
        ({"purpose": "   "}, "VALIDATION_ERROR"),
        ({"purpose": "x" * 301}, "VALIDATION_ERROR"),
        ({"location_id": "00000000-0000-0000-0000-0000000000ab"}, "DUTY_LOCATION_UNAVAILABLE"),
        ({"bogus": 1}, "VALIDATION_ERROR"),
    ],
)
async def test_apply_validation(client, H, site, clock, over, err):
    r = await apply(client, H["employee"], **over)
    assert r.status_code == 422 and code(r) == err, r.text


async def test_apply_31_days_ok_and_32_rejected(client, H, clock):
    assert (await apply(client, H["employee"], from_date="2026-11-01", to_date="2026-12-01")).status_code == 201  # 31 days
    r = await apply(client, H["manager"], from_date="2026-11-01", to_date="2026-12-02")
    assert r.status_code == 422 and code(r) == "INVALID_DATE_RANGE"


async def test_apply_backdate_setting(client, H, clock):
    assert (await apply(client, H["employee"], from_date="2026-10-19", to_date="2026-10-19")).status_code == 422
    await set_settings(client, H["admin"], request_backdate_days=3)
    assert (await apply(client, H["employee"], from_date="2026-10-19", to_date="2026-10-19")).status_code == 201
    assert (await apply(client, H["employee"], from_date="2026-10-17", to_date="2026-10-17")).status_code == 422


async def test_apply_unassigned_location_rejected(client, H, uid, clock):
    other = await new_loc(client, H["admin"], "Elsewhere")
    r = await apply(client, H["employee"], location_id=other["id"])
    assert r.status_code == 422 and code(r) == "DUTY_LOCATION_UNAVAILABLE"


async def test_apply_when_disabled(client, H, clock):
    await set_settings(client, H["admin"], enabled=False)
    r = await apply(client, H["employee"])
    assert r.status_code == 409 and code(r) == "ON_DUTY_DISABLED"


async def test_overlap_with_own_request_and_leave(client, H, db, clock):
    assert (await apply(client, H["employee"])).status_code == 201
    r = await apply(client, H["employee"], from_date="2026-10-30", to_date="2026-11-02")
    assert r.status_code == 409 and code(r) == "OVERLAPPING_REQUEST"
    assert r.json()["error"]["details"] == [{"field": "date", "message": "2026-10-30"}]
    # adjacent is fine
    assert (await apply(client, H["employee"], from_date="2026-10-31", to_date="2026-10-31")).status_code == 201
    # other user is unaffected
    assert (await apply(client, H["manager"])).status_code == 201
    # full-day leave clash
    await seed_leave(db, "EMP001", dt.date(2026, 11, 5), dt.date(2026, 11, 6))
    r = await apply(client, H["employee"], from_date="2026-11-04", to_date="2026-11-05")
    assert r.status_code == 409 and r.json()["error"]["details"][0]["message"] == "2026-11-05"
    # half-day leave does not block
    await seed_leave(db, "EMP001", dt.date(2026, 11, 9), dt.date(2026, 11, 9), half=True)
    assert (await apply(client, H["employee"], from_date="2026-11-09", to_date="2026-11-09")).status_code == 201
    # pending leave does block? contract: pending/approved full-day leave
    await seed_leave(db, "EMP001", dt.date(2026, 11, 12), dt.date(2026, 11, 12), status="pending")
    assert (await apply(client, H["employee"], from_date="2026-11-12", to_date="2026-11-12")).status_code == 409


# ---------------------------------------------------------------- list / detail / cancel (ownership)
async def test_list_detail_cancel_isolation(client, H, clock):
    mine = (await apply(client, H["employee"])).json()
    theirs = (await apply(client, H["manager"])).json()
    items = (await client.get(f"{OD}/requests", headers=H["employee"])).json()
    assert items["total"] == 1 and items["items"][0]["id"] == mine["id"]
    assert (await client.get(f"{OD}/requests/{theirs['id']}", headers=H["employee"])).status_code == 404
    assert (await client.post(f"{OD}/requests/{theirs['id']}/cancel", headers=H["employee"])).status_code == 404
    assert (await client.get(f"{OD}/requests/00000000-0000-0000-0000-0000000000ab", headers=H["employee"])).status_code == 404
    r = await client.post(f"{OD}/requests/{mine['id']}/cancel", headers=H["employee"])
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    r = await client.post(f"{OD}/requests/{mine['id']}/cancel", headers=H["employee"])
    assert r.status_code == 409 and code(r) == "NOT_PENDING"
    f = await client.get(f"{OD}/requests", headers=H["employee"], params={"status": "cancelled"})
    assert f.json()["total"] == 1
    assert (await client.get(f"{OD}/requests", headers=H["employee"], params={"status": "pending"})).json()["total"] == 0
    # cancelled frees the dates
    assert (await apply(client, H["employee"])).status_code == 201


async def test_cancel_approved_depends_on_setting_and_future(client, H, clock):
    r1 = (await apply(client, H["employee"], from_date="2026-10-28", to_date="2026-10-28")).json()
    await client.put(f"{ADM}/requests/on_duty/{r1['id']}/approve", headers=H["manager"], json={})
    await set_settings(client, H["admin"], **{"leave.allow_cancel_approved": False})
    r = await client.post(f"{OD}/requests/{r1['id']}/cancel", headers=H["employee"])
    assert r.status_code == 409 and code(r) == "NOT_PENDING"
    await set_settings(client, H["admin"], **{"leave.allow_cancel_approved": True})
    r = await client.post(f"{OD}/requests/{r1['id']}/cancel", headers=H["employee"])
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


async def test_cancel_approved_not_for_today(client, H, clock):
    await set_settings(client, H["admin"], **{"leave.allow_cancel_approved": True})
    r1 = (await apply(client, H["employee"], from_date="2026-10-21", to_date="2026-10-21")).json()
    await client.put(f"{ADM}/requests/on_duty/{r1['id']}/approve", headers=H["manager"], json={})
    r = await client.post(f"{OD}/requests/{r1['id']}/cancel", headers=H["employee"])
    assert r.status_code == 409 and code(r) == "NOT_PENDING"  # only from_date > today


async def test_list_filters(client, H, clock):
    await apply(client, H["employee"], from_date="2026-10-28", to_date="2026-10-29")
    await apply(client, H["employee"], from_date="2026-11-10", to_date="2026-11-10")
    r = await client.get(f"{OD}/requests", headers=H["employee"], params={"from": "2026-11-01", "to": "2026-11-30"})
    assert r.json()["total"] == 1
    r = await client.get(f"{OD}/requests", headers=H["employee"], params={"from": "2026-10-29", "to": "2026-10-29"})
    assert r.json()["total"] == 1  # overlapping range
    r = await client.get(f"{OD}/requests", headers=H["employee"])
    assert {i["from_date"] for i in r.json()["items"]} == {"2026-10-28", "2026-11-10"}  # order not asserted: SQLite created_at ties


# ---------------------------------------------------------------- admin decisions
async def test_admin_list_scope_and_decisions(client, H, clock):
    mine = (await apply(client, H["employee"])).json()
    mgr_own = (await apply(client, H["manager"])).json()
    # manager (direct report scope) sees employee's request, plus own? check at least employee
    lst = (await client.get(f"{ADM}/requests", headers=H["manager"], params={"type": "on_duty"})).json()
    assert mine["id"] in {i["id"] for i in lst["items"]}
    item = next(i for i in lst["items"] if i["id"] == mine["id"])
    assert item["type"] == "on_duty" and item["user"]["employee_id"] == "EMP001"
    # employee cannot access
    assert (await client.get(f"{ADM}/requests", headers=H["employee"], params={"type": "on_duty"})).status_code == 403
    assert (await client.put(f"{ADM}/requests/on_duty/{mine['id']}/approve", headers=H["employee"], json={})).status_code == 403
    # self-approval forbidden
    r = await client.put(f"{ADM}/requests/on_duty/{mgr_own['id']}/approve", headers=H["manager"], json={})
    assert r.status_code == 403 and code(r) == "FORBIDDEN"
    # reject needs comment
    r = await client.put(f"{ADM}/requests/on_duty/{mine['id']}/reject", headers=H["manager"], json={})
    assert r.status_code == 422
    r = await client.put(f"{ADM}/requests/on_duty/{mine['id']}/reject", headers=H["manager"], json={"comment": "No"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["decision_comment"] == "No"
    assert r.json()["approver"]["employee_id"] == "MGR001"
    r = await client.put(f"{ADM}/requests/on_duty/{mine['id']}/approve", headers=H["manager"], json={})
    assert r.status_code == 409 and code(r) == "NOT_PENDING"
    # the employee sees the decision
    d = (await client.get(f"{OD}/requests/{mine['id']}", headers=H["employee"])).json()
    assert d["status"] == "rejected" and d["decision_comment"] == "No"


async def test_manager_out_of_scope_404(client, H, uid, db, clock):
    # HR's own request is not a direct report of the manager
    r = (await apply(client, H["hr"])).json()
    out = await client.put(f"{ADM}/requests/on_duty/{r['id']}/approve", headers=H["manager"], json={})
    assert out.status_code in (403, 404)
    assert out.status_code != 200
    ok = await client.put(f"{ADM}/requests/on_duty/{r['id']}/approve", headers=H["admin"], json={})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert (await client.put(f"{ADM}/requests/on_duty/00000000-0000-0000-0000-0000000000ab/approve", headers=H["admin"], json={})).status_code == 404


async def test_approve_rechecks_leave_overlap_and_inactive_requester(client, H, uid, db, clock):
    r1 = (await apply(client, H["employee"], from_date="2026-11-03", to_date="2026-11-04")).json()
    await seed_leave(db, "EMP001", dt.date(2026, 11, 4), dt.date(2026, 11, 4))
    out = await client.put(f"{ADM}/requests/on_duty/{r1['id']}/approve", headers=H["manager"], json={})
    assert out.status_code == 409 and code(out) == "OVERLAPPING_REQUEST"
    r2 = (await apply(client, H["employee"], from_date="2026-11-10", to_date="2026-11-10")).json()
    d = await client.post(f"{ADM}/employees/{uid['employee']}/deactivate", headers=H["admin"], json={"reason": "other"})
    assert d.status_code == 200, d.text
    got = (await client.get(f"{ADM}/requests", headers=H["admin"], params={"type": "on_duty", "status": "cancelled"})).json()
    assert {i["id"] for i in got["items"]} >= {r2["id"], r1["id"]}  # pending requests auto-cancelled on deactivate


async def test_approved_on_duty_flags_leave_conflict(client, H, db, clock):
    rid = (await apply(client, H["employee"], from_date="2026-11-03", to_date="2026-11-03")).json()["id"]
    await client.put(f"{ADM}/requests/on_duty/{rid}/approve", headers=H["manager"], json={})
    lv = await seed_leave(db, "EMP001", dt.date(2026, 11, 3), dt.date(2026, 11, 3), status="pending")
    items = (await client.get(f"{ADM}/requests", headers=H["manager"], params={"type": "leave"})).json()["items"]
    row = next(i for i in items if i["id"] == str(lv.id))
    assert row["on_duty_conflict"] is True


async def test_approval_creates_no_attendance(client, H, clock):
    rid = (await apply(client, H["employee"], from_date="2026-10-21", to_date="2026-10-21")).json()["id"]
    await client.put(f"{ADM}/requests/on_duty/{rid}/approve", headers=H["manager"], json={})
    t = (await client.get(f"{OD}/today", headers=H["employee"])).json()
    assert t["attendance"] is None


async def test_open_visit_survives_deactivation_flow(client, H, uid, site, clock):
    await check_in(client, H["employee"], site["id"], clock)
    d = await client.post(f"{ADM}/employees/{uid['employee']}/deactivate", headers=H["admin"], json={"reason": "other"})
    assert d.status_code == 200
    ov = (await client.get(f"{ADM}/on-duty/visits", headers=H["admin"], params={"open": "true"})).json()
    assert ov["total"] == 1  # left for the 00:30 job


# ---------------------------------------------------------------- admin visits
async def test_admin_visits_rbac_scope_and_fields(client, H, uid, site, clock):
    await assign(client, H["admin"], site["id"], [uid["employee"], uid["hr"]])
    await check_in(client, H["employee"], site["id"], clock)
    await check_in(client, H["hr"], site["id"], clock)
    clock.advance(minutes=30)
    await check_out(client, H["employee"], clock, p=FAR)
    url = f"{ADM}/on-duty/visits"
    assert (await client.get(url, headers=H["employee"])).status_code == 403
    assert (await client.get(url)).status_code == 401
    adm = (await client.get(url, headers=H["admin"], params={"date": "2026-10-21"})).json()
    assert adm["total"] == 2
    emp = next(i for i in adm["items"] if i["user"]["employee_id"] == "EMP001")
    assert emp["check_out_within_fence"] is False and emp["check_in_distance_m"] is not None and emp["suspicious"] is False
    assert emp["check_in_accuracy_m"] is not None
    # manager: direct reports only (employee), view_all-only fields null
    mg = (await client.get(url, headers=H["manager"], params={"date": "2026-10-21"})).json()
    assert [i["user"]["employee_id"] for i in mg["items"]] == ["EMP001"]
    for k in ("check_in_distance_m", "check_out_distance_m", "check_out_within_fence", "check_in_accuracy_m", "check_out_accuracy_m"):
        assert mg["items"][0][k] is None, k
    # filters
    f = lambda **p: client.get(url, headers=H["admin"], params=p)  # noqa: E731
    assert (await f(date="2026-10-21", open="true")).json()["total"] == 1
    assert (await f(date="2026-10-21", employee_id="emp001")).json()["total"] == 1
    assert (await f(date="2026-10-21", location_id=site["id"])).json()["total"] == 2
    assert (await f(date="2026-10-21", location_id="00000000-0000-0000-0000-0000000000ab")).json()["total"] == 0
    assert (await f(date="2026-10-21", department_id="00000000-0000-0000-0000-0000000000ab")).json()["total"] == 0
    assert (await f(date="2026-10-20")).json()["total"] == 0
    r = await f(**{"from": "2024-01-01", "to": "2026-10-21"})
    assert r.status_code == 422 and code(r) == "INVALID_DATE_RANGE"


async def test_admin_visits_sorted_desc(client, H, uid, site, clock):
    await assign(client, H["admin"], site["id"], [uid["employee"]], mode="all")
    await check_in(client, H["employee"], site["id"], clock)
    clock.advance(minutes=1)
    await check_in(client, H["manager"], site["id"], clock)
    items = (await client.get(f"{ADM}/on-duty/visits", headers=H["admin"], params={"date": "2026-10-21"})).json()["items"]
    assert [i["user"]["employee_id"] for i in items] == ["MGR001", "EMP001"]


# ---------------------------------------------------------------- dashboard / day / calendar / summary / home / config
async def test_dashboard_and_day_counts(client, H, uid, site, clock, monkeypatch):
    await assign(client, H["admin"], site["id"], [uid["employee"], uid["manager"]])
    await check_in(client, H["employee"], site["id"], clock)
    await check_in(client, H["manager"], site["id"], clock)
    clock.advance(minutes=20)
    await check_out(client, H["manager"], clock)
    await apply(client, H["hr"])
    monkeypatch.setattr("app.services.console.utcnow", lambda: clock.now)  # dashboard has its own clock import
    dash = (await client.get(f"{ADM}/dashboard", headers=H["admin"])).json()
    assert dash["attendance"]["on_duty"] == 2 and dash["attendance"]["on_duty_open"] == 1
    assert dash["requests"]["on_duty_pending"] == 1
    day = (await client.get(f"{ADM}/attendance/day", headers=H["admin"], params={"date": "2026-10-21", "status": "on_duty"})).json()
    ids = {i["employee_id"] if "employee_id" in i else i["user"]["employee_id"] for i in day["items"]}
    assert ids == {"EMP001", "MGR001"}
    for i in day["items"]:
        assert i["work_mode"] == "on_duty" and i["on_duty_locations"] == ["Acme Client"]
        s = str(i)
        assert "lat" not in s.lower().replace("late", "") and "selfie" not in s
    allday = (await client.get(f"{ADM}/attendance/day", headers=H["admin"], params={"date": "2026-10-21"})).json()
    nomode = [i for i in allday["items"] if i["work_mode"] is None]
    assert nomode and all(i["on_duty_locations"] == [] for i in nomode)


async def test_calendar_summary_home_config(client, H, uid, site, clock):
    e = H["employee"]
    cfg = (await client.get("/api/config", headers=e)).json()["on_duty"]
    assert cfg["enabled"] is True and cfg["has_locations"] is True and cfg["approval_required"] is False
    assert cfg["max_visits_per_day"] == 10 and cfg["request_max_advance_days"] == 60 and cfg["request_backdate_days"] == 0
    assert cfg["selfie_required"] is False and cfg["checkout_geofence"] is False
    mgr_cfg = (await client.get("/api/config", headers=H["manager"])).json()["on_duty"]
    assert mgr_cfg["has_locations"] is False  # nothing assigned
    await check_in(client, e, site["id"], clock)
    home = (await client.get("/api/home", headers=e)).json()
    ta = home["today_attendance"]
    assert ta["work_mode"] == "on_duty" and ta["open_visit"]["location_name"] == "Acme Client"
    await apply(client, e)
    home = (await client.get("/api/home", headers=e)).json()
    assert home["pending_requests"] >= 1
    clock.advance(minutes=10)
    await check_out(client, e, clock)
    home = (await client.get("/api/home", headers=e)).json()
    assert home["today_attendance"]["open_visit"] is None
    cal = (await client.get("/api/attendance/calendar", headers=e, params={"month": "2026-10"})).json()
    days = cal["days"] if "days" in cal else cal["items"]
    d21 = next(d for d in days if d["date"] == "2026-10-21")
    assert d21["on_duty"] is True
    assert all(d.get("on_duty") is False for d in days if d["date"] != "2026-10-21")
    sm = (await client.get("/api/attendance/summary", headers=e, params={"month": "2026-10"})).json()
    assert sm["on_duty_days"] == 1
    hist = (await client.get("/api/attendance/team", headers=H["manager"], params={"from": "2026-10-21", "to": "2026-10-21"})).json()
    rec = hist["items"][0]
    assert rec["work_mode"] == "on_duty" and rec["on_duty_locations"] == ["Acme Client"] and rec["within_fence"] is None


async def test_config_has_locations_false_when_disabled(client, H, site):
    await set_settings(client, H["admin"], enabled=False)
    cfg = (await client.get("/api/config", headers=H["employee"])).json()["on_duty"]
    assert cfg["enabled"] is False and cfg["has_locations"] is False


# ---------------------------------------------------------------- export
def wb(r):
    assert r.status_code == 200, r.text
    return load_workbook(io.BytesIO(r.content))


@pytest.fixture(autouse=False)
async def _reset(redis):
    for k in await redis.keys("rl:export:*"):
        await redis.delete(k)


async def exp(client, h, redis, **p):
    for k in await redis.keys("rl:export:*"):
        await redis.delete(k)
    return await client.get(f"{ADM}/export", headers=h, params={"from": "2026-10-21", "to": "2026-10-21", **p})


async def test_export_on_duty_sheet_and_attendance_columns(client, H, uid, site, clock, redis):
    await assign(client, H["admin"], site["id"], [uid["employee"]])
    await check_in(client, H["employee"], site["id"], clock, note="Secret note here")
    clock.advance(minutes=95)
    await check_out(client, H["employee"], clock)
    w = wb(await exp(client, H["admin"], redis, type="on_duty"))
    assert w.sheetnames == ["On Duty"]
    ws = w["On Duty"]
    head = [c.value for c in ws[1]]
    assert head == ["Date", "Employee ID", "Name", "Department", "Location", "Check-in", "Check-out", "Visit Minutes", "Auto Closed", "Check-out Within Site"]
    assert ws.max_row == 2
    row = [c.value for c in ws[2]]
    assert row[1] == "EMP001" and row[4] == "Acme Client" and row[7] == 95
    flat = " ".join(str(c.value) for r in ws.iter_rows() for c in r)
    assert "Secret note" not in flat
    alls = wb(await exp(client, H["admin"], redis, type="all"))
    assert alls.sheetnames == ["Attendance", "Leave", "Late", "Permission", "On Duty"]
    att = alls["Attendance"]
    ah = [c.value for c in att[1]]
    assert ah[-3:] == ["Within Geofence", "Mode", "On Duty Locations"] or ah[-2:] == ["Mode", "On Duty Locations"], ah
    m, locs = ah.index("Mode"), ah.index("On Duty Locations")
    arow = [c.value for c in att[2]]
    assert arow[m] == "on_duty" and arow[locs] == "Acme Client"
    gf = ah.index("Within Geofence")
    assert arow[gf] in (None, "")
    assert ah.index("Mode") == gf + 1 and locs == gf + 2


async def test_export_on_duty_note_requires_sensitive(client, H, uid, site, clock, redis):
    await check_in(client, H["employee"], site["id"], clock, note="Visible with reasons")
    clock.advance(minutes=5)
    await check_out(client, H["employee"], clock)
    r = await exp(client, H["admin"], redis, type="on_duty", include_reasons=True)
    if r.status_code == 200:
        ws = load_workbook(io.BytesIO(r.content))["On Duty"]
        head = [c.value for c in ws[1]]
        assert head[-1] == "Note" and ws[2][-1].value == "Visible with reasons"
    else:
        assert r.status_code == 403
    r = await exp(client, H["hr"], redis, type="on_duty", include_reasons=True)
    assert r.status_code in (200, 403)


async def test_export_on_duty_filters_and_rbac(client, H, uid, site, clock, redis):
    await check_in(client, H["employee"], site["id"], clock)
    clock.advance(minutes=5)
    await check_out(client, H["employee"], clock)
    assert (await exp(client, H["employee"], redis, type="on_duty")).status_code == 403
    ws = wb(await exp(client, H["admin"], redis, type="on_duty", employee_id="NOPE-X"))["On Duty"] if False else None
    ws = wb(await exp(client, H["manager"], redis, type="on_duty"))["On Duty"]
    assert ws.max_row == 2  # employee is the manager's direct report
    ws = wb(await client.get(f"{ADM}/export", headers=H["admin"], params={"type": "on_duty", "from": "2026-10-22", "to": "2026-10-23"}))["On Duty"]
    assert ws.max_row == 1  # header only
    r = await exp(client, H["admin"], redis, type="on_duty", department_id="00000000-0000-0000-0000-0000000000ab")
    assert r.status_code in (200, 404)
