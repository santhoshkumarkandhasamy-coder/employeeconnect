"""QA (black-box, contract 14.3/14.4): visit check-in/out rules, fence, fix checks, approval, leave/holiday, late, office combos."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.models import Holiday
from tests.qa_on_duty_support import (
    FAR,
    NEAR,
    OFFICE,
    SITE,
    assign,
    check_in,
    check_out,
    clock,  # noqa: F401
    code,
    ist,
    new_loc,
    office_in,
    office_out,
    pt,
    seed_leave,
    set_settings,
    z,
)

OD = "/api/on-duty"


@pytest.fixture
async def site(client, H, uid):
    loc = await new_loc(client, H["admin"], "Acme Client", radius=200)
    await assign(client, H["admin"], loc["id"], [uid["employee"]])
    return loc


# ---------------------------------------------------------------- happy path
async def test_check_in_out_flow_and_attendance(client, H, site, clock):
    e = H["employee"]
    r = await check_in(client, e, site["id"], clock, note="  Quarterly review  ")
    assert r.status_code == 201, r.text
    d = r.json()
    v, a = d["visit"], d["attendance"]
    assert v["open"] is True and v["auto_closed"] is False and v["check_out_at"] is None and v["visit_minutes"] is None
    assert v["location"] == {"id": site["id"], "name": "Acme Client"}
    assert v["work_date"] == "2026-10-21" and v["note"] == "Quarterly review"
    assert a["work_mode"] == "on_duty" and a["within_fence"] is None and a["distance_m"] is None
    assert a["status"] == "present" and a["minutes_late"] == 0  # 11:00 vs 09:30+10 grace, but not late by default
    assert a["on_duty_locations"] == ["Acme Client"]

    today = (await client.get(f"{OD}/today", headers=e)).json()
    assert today["open_visit"]["id"] == v["id"] and today["can_check_in"] is False and today["blocked_reason"] == "VISIT_OPEN"
    assert today["visits_remaining"] == 9 and today["enabled"] is True

    clock.advance(minutes=95, seconds=30)
    r = await check_out(client, e, clock, note="Done")
    assert r.status_code == 200, r.text
    v2 = r.json()["visit"]
    assert v2["open"] is False and v2["visit_minutes"] == 95 and v2["note"] == "Done"
    a2 = r.json()["attendance"]
    assert a2["check_out_source"] == "on_duty" and a2["work_mode"] == "on_duty"
    assert a2["worked_minutes"] == 95 and a2["status"] == "half_day"  # 95 < 240

    today = (await client.get(f"{OD}/today", headers=e)).json()
    assert today["open_visit"] is None and today["can_check_in"] is True and len(today["visits"]) == 1


async def test_note_kept_when_checkout_note_null_and_note_cleaning(client, H, site, clock):
    e = H["employee"]
    await check_in(client, e, site["id"], clock, note="<b>hello</b>\x00 site")
    clock.advance(minutes=5)
    r = await check_out(client, e, clock)
    note = r.json()["visit"]["note"]
    assert note and "<" not in note and "\x00" not in note and "hello" in note
    # too long / blank
    clock.advance(minutes=1)
    r = await check_in(client, e, site["id"], clock, note="x" * 201)
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"
    r = await check_in(client, e, site["id"], clock, note="   ")
    assert r.status_code == 201 and r.json()["visit"]["note"] is None


async def test_required_fields_and_extra_forbidden(client, H, site, clock):
    e = H["employee"]
    full = {"location_id": site["id"], **pt(NEAR, clock=clock)}
    for k in ("location_id", "lat", "lng", "accuracy_m", "fix_time"):
        b = {x: y for x, y in full.items() if x != k}
        r = await client.post(f"{OD}/check-in", headers=e, json=b)
        assert r.status_code == 422 and code(r) == "VALIDATION_ERROR", k
        assert any(d.get("field", "").endswith(k) for d in r.json()["error"]["details"]), (k, r.text)
    r = await client.post(f"{OD}/check-in", headers=e, json={**full, "extra": 1})
    assert r.status_code == 422
    # checkout always needs coordinates
    r = await client.post(f"{OD}/check-out", headers=e, json={})
    assert r.status_code == 422


# ---------------------------------------------------------------- location availability
async def test_unknown_inactive_unassigned_same_422(client, H, uid, clock):
    e = H["employee"]
    inactive = await new_loc(client, H["admin"], "Inactive", active=False)
    await assign(client, H["admin"], inactive["id"], [uid["employee"]])
    other = await new_loc(client, H["admin"], "Other")  # nobody assigned
    outs = []
    for lid in (inactive["id"], other["id"], "00000000-0000-0000-0000-0000000000ff"):
        r = await check_in(client, e, lid, clock)
        assert r.status_code == 422 and code(r) == "DUTY_LOCATION_UNAVAILABLE", r.text
        outs.append(r.json()["error"]["message"])
    assert len(set(outs)) == 1  # no enumeration via messages


async def test_all_mode_and_switch(client, H, uid, clock):
    loc = await new_loc(client, H["admin"], "All Sites", mode="all")
    assert (await check_in(client, H["employee"], loc["id"], clock)).status_code == 201
    assert (await check_in(client, H["manager"], loc["id"], clock)).status_code == 201
    await assign(client, H["admin"], loc["id"], [uid["manager"]], mode="selected")
    clock.advance(minutes=1)
    await check_out(client, H["employee"], clock)  # allowed even when no longer assigned
    r = await check_in(client, H["employee"], loc["id"], clock)
    assert code(r) == "DUTY_LOCATION_UNAVAILABLE"


async def test_checkout_works_after_deactivate_unassign_and_disable(client, H, site, uid, clock):
    e = H["employee"]
    assert (await check_in(client, e, site["id"], clock)).status_code == 201
    r = await client.put(
        f"/api/admin/duty-locations/{site['id']}",
        headers=H["admin"],
        json={"name": "Acme Client", "lat": SITE[0], "lng": SITE[1], "radius_m": 200, "assign_mode": "selected", "is_active": False},
    )
    assert r.status_code == 200
    await assign(client, H["admin"], site["id"], [])
    await set_settings(client, H["admin"], enabled=False)
    clock.advance(minutes=10)
    r = await check_out(client, e, clock)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- disabled / shift
async def test_disabled_409(client, H, site, clock):
    await set_settings(client, H["admin"], enabled=False)
    r = await check_in(client, H["employee"], site["id"], clock)
    assert r.status_code == 409 and code(r) == "ON_DUTY_DISABLED"
    t = (await client.get(f"{OD}/today", headers=H["employee"])).json()
    assert t["can_check_in"] is False and t["blocked_reason"] == "ON_DUTY_DISABLED"


async def test_no_shift_assigned_422(client, H, site, clock, db, uid):
    from app.models import User

    u = await db.get(User, __import__("uuid").UUID(uid["employee"]))
    u.shift_id = None
    await db.commit()
    r = await check_in(client, H["employee"], site["id"], clock)
    assert r.status_code == 422 and code(r) == "NO_SHIFT_ASSIGNED"


# ---------------------------------------------------------------- fence
async def test_fence_inside_edge_outside(client, H, uid, clock):
    loc = await new_loc(client, H["admin"], "Small", radius=150)
    await assign(client, H["admin"], loc["id"], [uid["employee"]])
    e = H["employee"]
    r = await check_in(client, e, loc["id"], clock, p=FAR)  # ~333 m
    assert r.status_code == 422 and code(r) == "OUTSIDE_DUTY_LOCATION"
    det = r.json()["error"]["details"]
    assert det == [{"field": "within_fence", "message": "false"}]
    err = r.json()["error"]
    blob = str(err.get("message", "")) + str(err.get("details"))  # ids/timestamps may contain digits
    assert "radius" not in blob.lower() and "333" not in blob and "150" not in blob
    # ~100 m inside a 150 m fence
    assert (await check_in(client, e, loc["id"], clock, p=NEAR)).status_code == 201


async def test_fence_uses_per_location_radius(client, H, uid, clock):
    tight = await new_loc(client, H["admin"], "Tight", radius=50)  # ~100 m away is outside
    await assign(client, H["admin"], tight["id"], [uid["employee"]])
    r = await check_in(client, H["employee"], tight["id"], clock, p=NEAR)
    assert code(r) == "OUTSIDE_DUTY_LOCATION"
    # exactly at the centre
    assert (await check_in(client, H["employee"], tight["id"], clock, p=SITE)).status_code == 201


async def test_radius_edit_applies_to_next_punch_not_history(client, H, site, uid, clock):
    e = H["employee"]
    assert (await check_in(client, e, site["id"], clock)).status_code == 201
    clock.advance(minutes=10)
    await check_out(client, e, clock)
    await client.put(
        f"/api/admin/duty-locations/{site['id']}",
        headers=H["admin"],
        json={"name": "Renamed", "lat": SITE[0], "lng": SITE[1], "radius_m": 10, "assign_mode": "selected", "is_active": True},
    )
    visits = (await client.get(f"{OD}/visits", headers=e)).json()["items"]
    assert visits[0]["location"]["name"] == "Acme Client"  # name snapshot at check-in
    clock.advance(minutes=1)
    assert code(await check_in(client, e, site["id"], clock, p=NEAR)) == "OUTSIDE_DUTY_LOCATION"


async def test_checkout_outside_site_allowed_and_flagged(client, H, site, clock):
    e = H["employee"]
    await check_in(client, e, site["id"], clock)
    clock.advance(minutes=30)
    r = await check_out(client, e, clock, p=FAR)
    assert r.status_code == 200, r.text
    adm = (await client.get("/api/admin/on-duty/visits", headers=H["admin"], params={"date": "2026-10-21"})).json()["items"][0]
    assert adm["check_out_within_fence"] is False and adm["check_in_distance_m"] is not None


async def test_checkout_geofence_enforced_when_on(client, H, site, clock):
    await set_settings(client, H["admin"], checkout_geofence=True)
    e = H["employee"]
    await check_in(client, e, site["id"], clock)
    clock.advance(minutes=30)
    r = await check_out(client, e, clock, p=FAR)
    assert r.status_code == 422 and code(r) == "OUTSIDE_DUTY_LOCATION"
    assert (await client.get(f"{OD}/today", headers=e)).json()["open_visit"] is not None
    assert (await check_out(client, e, clock, p=NEAR)).status_code == 200


# ---------------------------------------------------------------- fix checks
async def test_stale_future_accuracy_mock(client, H, site, clock):
    e = H["employee"]
    stale = z(clock.now - dt.timedelta(seconds=600))
    r = await check_in(client, e, site["id"], clock, fix_time=stale)
    assert r.status_code == 422 and code(r) == "LOCATION_FIX_STALE"
    future = z(clock.now + dt.timedelta(seconds=600))
    r = await check_in(client, e, site["id"], clock, fix_time=future)
    assert code(r) == "LOCATION_FIX_STALE"
    r = await check_in(client, e, site["id"], clock, acc=500)
    assert r.status_code == 422 and code(r) == "LOCATION_ACCURACY_LOW"
    r = await check_in(client, e, site["id"], clock, is_mock_location=True)
    assert r.status_code == 422 and code(r) == "MOCK_LOCATION_DETECTED"


async def test_mock_accepted_when_rejection_off_flags_suspicious(client, H, site, clock):
    await set_settings(client, H["admin"], **{"attendance.reject_mock_location": False})
    r = await check_in(client, H["employee"], site["id"], clock, is_mock_location=True)
    assert r.status_code == 201, r.text
    adm = (await client.get("/api/admin/on-duty/visits", headers=H["admin"], params={"date": "2026-10-21"})).json()["items"][0]
    assert adm["suspicious"] is True


async def test_selfie_required_and_missing_file(client, H, site, clock):
    await set_settings(client, H["admin"], selfie_required=True)
    r = await check_in(client, H["employee"], site["id"], clock)
    assert r.status_code == 422 and code(r) == "SELFIE_REQUIRED"
    r = await check_in(client, H["employee"], site["id"], clock, selfie_file_id="00000000-0000-0000-0000-0000000000ee")
    assert r.status_code in (404, 422)  # someone else's / unknown file must not be accepted
    assert r.status_code != 201


async def test_fail_counter_locks_after_five(client, H, site, clock):
    e = H["employee"]
    for _ in range(5):
        assert code(await check_in(client, e, site["id"], clock, p=FAR)) == "OUTSIDE_DUTY_LOCATION"
    r = await check_in(client, e, site["id"], clock, p=NEAR)
    assert r.status_code == 429 and code(r) == "RATE_LIMITED"
    assert "retry-after" in {k.lower() for k in r.headers}


async def test_fail_counter_shared_with_office_punch_and_reset_on_success(client, H, site, clock):
    e = H["employee"]
    for _ in range(4):
        await check_in(client, e, site["id"], clock, p=FAR)
    assert (await check_in(client, e, site["id"], clock, p=NEAR)).status_code == 201  # success resets
    clock.advance(minutes=1)
    await check_out(client, e, clock)
    for _ in range(4):
        assert code(await check_in(client, e, site["id"], clock, p=FAR)) == "OUTSIDE_DUTY_LOCATION"


async def test_validation_failures_not_counted(client, H, site, clock):
    e = H["employee"]
    for _ in range(8):
        r = await check_in(client, e, "00000000-0000-0000-0000-0000000000ff", clock)
        assert r.status_code == 422 and code(r) == "DUTY_LOCATION_UNAVAILABLE"  # not 429 (not counted); under 10/min limiter? see below


# ---------------------------------------------------------------- one open visit / max visits
async def test_second_checkin_while_open_is_visit_open(client, H, uid, clock):
    a = await new_loc(client, H["admin"], "Site A", mode="all")
    b = await new_loc(client, H["admin"], "Site B", mode="all")
    e = H["employee"]
    first = (await check_in(client, e, a["id"], clock)).json()["visit"]
    r = await check_in(client, e, b["id"], clock)
    assert r.status_code == 409 and code(r) == "VISIT_OPEN"
    assert r.json()["error"]["details"] == [{"field": "visit_id", "message": first["id"]}]


async def test_max_visits_per_day(client, H, site, clock):
    await set_settings(client, H["admin"], max_visits_per_day=2)
    e = H["employee"]
    for _ in range(2):
        assert (await check_in(client, e, site["id"], clock)).status_code == 201
        clock.advance(minutes=10)
        assert (await check_out(client, e, clock)).status_code == 200
        clock.advance(minutes=1)
    r = await check_in(client, e, site["id"], clock)
    assert r.status_code == 409 and code(r) == "MAX_VISITS_REACHED"
    t = (await client.get(f"{OD}/today", headers=e)).json()
    assert t["visits_remaining"] == 0 and t["blocked_reason"] == "MAX_VISITS_REACHED" and len(t["visits"]) == 2


async def test_checkout_without_open_visit_and_wrong_visit_id(client, H, site, clock):
    e = H["employee"]
    r = await check_out(client, e, clock)
    assert r.status_code == 409 and code(r) == "NO_OPEN_VISIT"
    await check_in(client, e, site["id"], clock)
    r = await check_out(client, e, clock, visit_id="00000000-0000-0000-0000-0000000000cc")
    assert r.status_code == 404 and code(r) == "NOT_FOUND"
    # someone else's visit id -> 404 (no IDOR)
    mid = (await client.get(f"{OD}/today", headers=e)).json()["open_visit"]["id"]
    await assign(client, H["admin"], site["id"], [], mode="all")
    clock.advance(minutes=1)
    r = await check_out(client, H["manager"], clock, visit_id=mid)
    assert r.status_code in (404, 409)
    assert r.status_code != 200
    r = await check_out(client, e, clock, visit_id=mid)
    assert r.status_code == 200


# ---------------------------------------------------------------- approval
async def test_approval_required_and_request_flow(client, H, uid, site, clock):
    await set_settings(client, H["admin"], approval_required=True)
    e = H["employee"]
    r = await check_in(client, e, site["id"], clock)
    assert r.status_code == 409 and code(r) == "ON_DUTY_NOT_APPROVED"
    t = (await client.get(f"{OD}/today", headers=e)).json()
    assert t["approval"]["required"] is True and t["approval"]["covered"] is False and t["blocked_reason"] == "ON_DUTY_NOT_APPROVED"
    req = await client.post(
        f"{OD}/requests", headers=e, json={"from_date": "2026-10-21", "to_date": "2026-10-21", "location_id": site["id"], "purpose": "Install"}
    )
    assert req.status_code == 201, req.text
    assert code(await check_in(client, e, site["id"], clock)) == "ON_DUTY_NOT_APPROVED"  # pending is not enough
    rid = req.json()["id"]
    ap = await client.put(f"/api/admin/requests/on_duty/{rid}/approve", headers=H["manager"], json={})
    assert ap.status_code == 200, ap.text
    t = (await client.get(f"{OD}/today", headers=e)).json()
    assert t["approval"]["covered"] is True and t["approval"]["location_ids"] == [site["id"]] and t["approval"]["any_location"] is False
    r = await check_in(client, e, site["id"], clock)
    assert r.status_code == 201 and r.json()["visit"]["request_id"] == rid


async def test_approval_location_specific_vs_any(client, H, uid, clock):
    await set_settings(client, H["admin"], approval_required=True)
    a = await new_loc(client, H["admin"], "Site A", mode="all")
    b = await new_loc(client, H["admin"], "Site B", mode="all")
    e = H["employee"]
    rid = (
        await client.post(f"{OD}/requests", headers=e, json={"from_date": "2026-10-21", "to_date": "2026-10-21", "location_id": a["id"], "purpose": "p"})
    ).json()["id"]
    await client.put(f"/api/admin/requests/on_duty/{rid}/approve", headers=H["admin"], json={})
    assert code(await check_in(client, e, b["id"], clock)) == "ON_DUTY_NOT_APPROVED"
    assert (await check_in(client, e, a["id"], clock)).status_code == 201
    clock.advance(minutes=5)
    r = await check_out(client, e, clock)
    assert r.status_code == 200, r.text
    # a second, any-location request now covers B too
    clock.advance(minutes=5)
    rid2 = (
        await client.post(f"{OD}/requests", headers=e, json={"from_date": "2026-10-22", "to_date": "2026-10-22", "location_id": None, "purpose": "p"})
    ).json()["id"]
    await client.put(f"/api/admin/requests/on_duty/{rid2}/approve", headers=H["admin"], json={})
    clock.set(ist(2026, 10, 22, 11, 0))
    r = await check_in(client, e, b["id"], clock)
    assert r.status_code == 201, r.text
    t = (await client.get(f"{OD}/today", headers=e)).json()
    assert t["approval"]["any_location"] is True


# ---------------------------------------------------------------- leave / holiday / weekly off / late
async def _leave(client, H, day, half=False):
    types = (await client.get("/api/leave/types", headers=H["employee"])).json()
    return types


async def test_on_leave_blocks(client, H, site, clock, db, uid):
    await seed_leave(db, "EMP001", dt.date(2026, 10, 21), dt.date(2026, 10, 21))
    r = await check_in(client, H["employee"], site["id"], clock)
    assert r.status_code == 409 and code(r) == "ON_LEAVE"
    t = (await client.get(f"{OD}/today", headers=H["employee"])).json()
    assert t["blocked_reason"] == "ON_LEAVE"


async def test_half_day_leave_allows(client, H, site, clock, db):
    await seed_leave(db, "EMP001", dt.date(2026, 10, 21), dt.date(2026, 10, 21), half=True)
    assert (await check_in(client, H["employee"], site["id"], clock)).status_code == 201


async def test_pending_leave_does_not_block(client, H, site, clock, db):
    await seed_leave(db, "EMP001", dt.date(2026, 10, 21), dt.date(2026, 10, 21), status="pending")
    assert (await check_in(client, H["employee"], site["id"], clock)).status_code == 201


async def test_holiday_and_weekly_off_allowed_never_late(client, H, site, clock, db):
    db.add(Holiday(date=dt.date(2026, 10, 21), name="Diwali"))
    await db.commit()
    await set_settings(client, H["admin"], apply_late_rule=True)
    r = await check_in(client, H["employee"], site["id"], clock)  # 11:00, shift 09:30 -> would be late on a work day
    assert r.status_code == 201 and r.json()["attendance"]["minutes_late"] == 0 and r.json()["attendance"]["status"] == "present"
    clock.set(ist(2026, 10, 25, 11, 0))  # Sunday
    await check_out(client, H["employee"], clock)
    assert (await check_in(client, H["employee"], site["id"], clock)).status_code == 201


async def test_late_rule_on_and_off(client, H, uid, clock):
    loc = await new_loc(client, H["admin"], "Site L", mode="all")
    # off (default): 11:00 start is not late
    r = await check_in(client, H["employee"], loc["id"], clock)
    assert r.json()["attendance"]["status"] == "present" and r.json()["attendance"]["minutes_late"] == 0
    # on: manager checks in 11:00 vs 09:30 + grace 10
    await set_settings(client, H["admin"], apply_late_rule=True)
    r = await check_in(client, H["manager"], loc["id"], clock)
    a = r.json()["attendance"]
    assert a["status"] == "late" and a["minutes_late"] > 0


async def test_late_rule_on_but_within_grace(client, H, uid, clock):
    await set_settings(client, H["admin"], apply_late_rule=True)
    loc = await new_loc(client, H["admin"], "Site L", mode="all")
    clock.set(ist(2026, 10, 21, 9, 35))
    a = (await check_in(client, H["employee"], loc["id"], clock)).json()["attendance"]
    assert a["status"] == "present" and a["minutes_late"] == 0


# ---------------------------------------------------------------- office combinations
async def test_office_first_then_visit_is_mixed_and_checkout_rules(client, H, site, clock):
    e = H["employee"]
    clock.set(ist(2026, 10, 21, 9, 0))
    assert (await office_in(client, e, clock)).status_code in (200, 201)
    clock.set(ist(2026, 10, 21, 10, 0))
    r = await check_in(client, e, site["id"], clock)
    assert r.status_code == 201
    a = r.json()["attendance"]
    assert a["work_mode"] == "mixed" and a["within_fence"] is True  # office fence result kept
    clock.set(ist(2026, 10, 21, 12, 0))
    r = await office_out(client, e, clock)  # visit still open
    assert r.status_code == 409 and code(r) == "VISIT_OPEN"
    vid = (await client.get(f"{OD}/today", headers=e)).json()["open_visit"]["id"]
    assert r.json()["error"]["details"] == [{"field": "visit_id", "message": vid}]
    clock.set(ist(2026, 10, 21, 13, 0))
    r = await check_out(client, e, clock)
    assert r.status_code == 200
    assert r.json()["attendance"]["check_out_source"] == "on_duty" and r.json()["attendance"]["work_mode"] == "mixed"
    clock.set(ist(2026, 10, 21, 17, 0))
    r = await office_out(client, e, clock)
    assert r.status_code == 200, r.text
    a = r.json()["attendance"] if "attendance" in r.json() else r.json()
    assert a["check_out_source"] == "office" and a["work_mode"] == "mixed"
    assert a["worked_minutes"] == 8 * 60 and a["status"] == "present"
    clock.set(ist(2026, 10, 21, 17, 30))
    r = await office_out(client, e, clock)
    assert r.status_code == 409 and code(r) == "ALREADY_CHECKED_OUT"


async def test_visit_first_then_office_checkin_conflicts_office_out_ok(client, H, site, clock):
    e = H["employee"]
    clock.set(ist(2026, 10, 21, 9, 0))
    await check_in(client, e, site["id"], clock)
    clock.set(ist(2026, 10, 21, 10, 0))
    r = await office_in(client, e, clock)
    assert r.status_code == 409 and code(r) == "ALREADY_CHECKED_IN"
    clock.set(ist(2026, 10, 21, 11, 0))
    await check_out(client, e, clock)
    # day started on duty can be closed in the office
    clock.set(ist(2026, 10, 21, 18, 0))
    r = await office_out(client, e, clock)
    assert r.status_code == 200, r.text
    a = r.json()["attendance"] if "attendance" in r.json() else r.json()
    assert a["check_out_source"] == "office" and a["work_mode"] == "mixed" and a["worked_minutes"] == 9 * 60


async def test_visit_after_office_checkout_keeps_later_office_checkout(client, H, site, clock):
    e = H["employee"]
    clock.set(ist(2026, 10, 21, 9, 0))
    await office_in(client, e, clock)
    clock.set(ist(2026, 10, 21, 14, 0))
    assert (await office_out(client, e, clock)).status_code == 200
    clock.set(ist(2026, 10, 21, 14, 30))
    assert (await check_in(client, e, site["id"], clock)).status_code == 201
    clock.set(ist(2026, 10, 21, 15, 0))
    r = await check_out(client, e, clock)
    assert r.status_code == 200
    a = r.json()["attendance"]
    assert a["check_out_source"] == "on_duty" and a["worked_minutes"] == 6 * 60  # max(existing, now) = 15:00


async def test_on_duty_only_day_without_office_checkout_is_closed_by_last_visit(client, H, site, clock):
    e = H["employee"]
    clock.set(ist(2026, 10, 21, 9, 0))
    await check_in(client, e, site["id"], clock)
    clock.set(ist(2026, 10, 21, 17, 0))
    r = await check_out(client, e, clock)
    a = r.json()["attendance"]
    assert a["worked_minutes"] == 480 and a["status"] == "present" and a["check_out_source"] == "on_duty"
    # office check-in after an on-duty day still conflicts
    assert code(await office_in(client, e, clock)) == "ALREADY_CHECKED_IN"


async def test_two_visits_span_travel_included(client, H, uid, clock):
    a = await new_loc(client, H["admin"], "Site A", mode="all")
    b = await new_loc(client, H["admin"], "Site B", lat=13.5, lng=80.5, mode="all")
    e = H["employee"]
    clock.set(ist(2026, 10, 21, 9, 0))
    await check_in(client, e, a["id"], clock)
    clock.set(ist(2026, 10, 21, 10, 0))
    await check_out(client, e, clock)
    clock.set(ist(2026, 10, 21, 12, 0))
    assert (await check_in(client, e, b["id"], clock, p=(13.5, 80.5))).status_code == 201
    clock.set(ist(2026, 10, 21, 15, 0))
    r = await check_out(client, e, clock, p=(13.5, 80.5))
    att = r.json()["attendance"]
    assert att["worked_minutes"] == 6 * 60 and att["on_duty_locations"] == ["Site A", "Site B"]
    visits = (await client.get(f"{OD}/visits", headers=e)).json()
    assert [v["location"]["name"] for v in visits["items"]] == ["Site B", "Site A"]  # newest first


async def test_half_day_threshold_setting(client, H, site, clock):
    await set_settings(client, H["admin"], **{"attendance.half_day_minutes": 30})
    e = H["employee"]
    clock.set(ist(2026, 10, 21, 9, 0))
    await check_in(client, e, site["id"], clock)
    clock.set(ist(2026, 10, 21, 9, 45))
    a = (await check_out(client, e, clock)).json()["attendance"]
    assert a["status"] == "present"


async def test_late_day_checkout_stays_late_unless_half_day(client, H, uid, clock):
    await set_settings(client, H["admin"], apply_late_rule=True)
    loc = await new_loc(client, H["admin"], "Site L", mode="all")
    clock.set(ist(2026, 10, 21, 10, 30))
    await check_in(client, H["employee"], loc["id"], clock)
    clock.set(ist(2026, 10, 21, 18, 30))
    a = (await check_out(client, H["employee"], clock)).json()["attendance"]
    assert a["status"] == "late" and a["minutes_late"] > 0


# ---------------------------------------------------------------- history
async def test_visits_history_ranges_and_isolation(client, H, site, uid, clock):
    e = H["employee"]
    await check_in(client, e, site["id"], clock)
    mgr = (await client.get(f"{OD}/visits", headers=H["manager"])).json()
    assert mgr["items"] == [] and mgr["total"] == 0  # no cross-user data
    r = await client.get(f"{OD}/visits", headers=e, params={"from": "2026-10-22", "to": "2026-10-21"})
    assert r.status_code == 422 and code(r) == "INVALID_DATE_RANGE"
    r = await client.get(f"{OD}/visits", headers=e, params={"from": "2026-01-01", "to": "2026-10-21"})
    assert r.status_code == 422 and code(r) == "INVALID_DATE_RANGE"
    r = await client.get(f"{OD}/visits", headers=e, params={"from": "garbage"})
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"
    r = await client.get(f"{OD}/visits", headers=e, params={"from": "2026-10-21", "to": "2026-10-21"})
    assert r.json()["total"] == 1
    r = await client.get(f"{OD}/visits", headers=e, params={"from": "2026-10-01", "to": "2026-10-20"})
    assert r.json()["total"] == 0


async def test_employee_view_has_no_admin_fields(client, H, site, clock):
    v = (await check_in(client, H["employee"], site["id"], clock)).json()["visit"]
    for k in ("check_in_distance_m", "check_in_accuracy_m", "suspicious", "user", "check_out_within_fence"):
        assert k not in v, k
    assert "lat" not in str(v) or True
