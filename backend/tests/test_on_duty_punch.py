"""On Duty visit check-in / check-out and the office interplay (plan 13 sections 5-6).
SQLite: Postgres-only partial indexes are dropped by the harness; the one-open-visit race is exercised by creating the same
partial unique index by hand (SQLite supports partial indexes) where it matters."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import undefer

from app.core.config import get_settings
from app.models import Attendance, AuditLog, File, Holiday, LateRecord, OnDutyVisit
from app.services import attendance as att_svc
from app.services import on_duty as od_svc
from tests.conftest import bearer
from tests.on_duty_helpers import (
    FAR,
    NEAR,
    SITE,
    SUN,
    WED,
    body_in,
    fix,
    ist,
    make_file,
    make_leave,
    make_loc,
    office_in,
    office_out,
    set_setting,
    user_id,
    visit_in,
    visit_out,
)


@pytest.fixture
def clock(monkeypatch):
    def _set(when: dt.datetime):
        monkeypatch.setattr("app.services.attendance.utcnow", lambda: when)
        monkeypatch.setattr("app.services.late.utcnow", lambda: when)

    _set(ist(2026, 10, 21, 11, 0))
    return _set


@pytest.fixture
async def emp(tokens_for):
    return bearer(await tokens_for("employee"))


@pytest.fixture
async def loc(db):
    return await make_loc(db)


def code(r) -> str:
    return r.json()["error"]["code"]


async def attendance_row(db, role="employee", day=WED) -> Attendance:
    uid = await user_id(db, role)
    return (
        await db.execute(
            select(Attendance)
            .where(Attendance.user_id == uid, Attendance.work_date == day)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# ------------------------------------------------------------------ happy path + shape
async def test_check_in_creates_on_duty_day_and_visit(client, emp, clock, loc, db):
    r = await visit_in(client, emp, loc, note="  Quarterly <b>review</b>  ")
    assert r.status_code == 201, r.text
    b = r.json()
    v, a = b["visit"], b["attendance"]
    assert v["open"] is True and v["check_out_at"] is None and v["visit_minutes"] is None and v["auto_closed"] is False
    assert v["location"] == {"id": str(loc.id), "name": "Acme Client"} and v["note"] == "Quarterly review"
    assert v["work_date"] == "2026-10-21" and v["check_in_at"] == "2026-10-21T05:30:00Z" and v["request_id"] is None
    assert a["work_mode"] == "on_duty" and a["status"] == "present" and a["minutes_late"] == 0
    assert a["within_fence"] is None and a["distance_m"] is None  # no office check-in on this day
    assert a["on_duty_locations"] == ["Acme Client"] and a["check_out_source"] is None
    assert "radius_m" not in r.text and "distance" not in v
    row = await attendance_row(db)
    assert row.check_in_distance_m is None and row.check_in_within_fence is None and row.work_mode == "on_duty"


async def test_visit_data_encrypted_at_rest_and_not_in_audit(client, emp, clock, loc, db):
    await visit_in(client, emp, loc, note="secret note")
    raw = (await db.execute(text("SELECT check_in_loc_enc, note_enc FROM on_duty_visits"))).one()
    assert all(isinstance(x, bytes) and x.startswith(b"gAAAA") for x in raw)
    assert b"13.00" not in raw[0] and b"secret" not in raw[1]
    v = (await db.execute(select(OnDutyVisit).options(undefer(OnDutyVisit.check_in_loc)))).scalar_one()
    assert abs(v.check_in_loc["lat"] - NEAR["lat"]) < 1e-9 and v.check_in_distance_m == 100
    audits = (await db.execute(select(AuditLog))).scalars().all()
    blob = repr([(a.before, a.after) for a in audits])
    assert "13.0009" not in blob and "secret note" not in blob


async def test_missing_required_fields_and_unknown_fields_422(client, emp, clock, loc):
    for missing in ("location_id", "lat", "lng", "accuracy_m", "fix_time"):
        b = body_in(loc)
        del b[missing]
        r = await client.post("/api/on-duty/check-in", json=b, headers=emp)
        assert r.status_code == 422 and code(r) == "VALIDATION_ERROR", missing
        assert missing in {d["field"] for d in r.json()["error"]["details"]}
    r = await client.post("/api/on-duty/check-in", json={**body_in(loc), "evil": 1}, headers=emp)
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"
    r = await client.post("/api/on-duty/check-in", json={**body_in(loc), "lat": 91}, headers=emp)
    assert r.status_code == 422
    r = await client.post("/api/on-duty/check-in", json={**body_in(loc), "note": "x" * 201}, headers=emp)
    assert r.status_code == 422
    r = await client.post("/api/on-duty/check-in", json={**body_in(loc), "note": "  <i> </i> "}, headers=emp)
    assert r.status_code == 201 and r.json()["visit"]["note"] is None  # blank after cleaning -> null


async def test_auth_required_and_no_permission_needed(client, loc):
    assert (await client.post("/api/on-duty/check-in", json={})).status_code == 401
    assert (await client.get("/api/on-duty/today")).status_code == 401


# ------------------------------------------------------------------ check-in order of checks
async def test_disabled_and_no_shift(client, emp, clock, loc, db):
    await set_setting(db, "on_duty.enabled", False)
    r = await visit_in(client, emp, loc)
    assert r.status_code == 409 and code(r) == "ON_DUTY_DISABLED"
    await set_setting(db, "on_duty.enabled", True)
    from app.models import User

    (await db.get(User, await user_id(db, "employee"))).shift_id = None
    await db.commit()
    r = await visit_in(client, emp, loc)
    assert r.status_code == 422 and code(r) == "NO_SHIFT_ASSIGNED"


async def test_unknown_inactive_unassigned_locations_look_identical(client, emp, clock, db):
    inactive = await make_loc(db, "Inactive", active=False)
    unassigned = await make_loc(db, "Unassigned", assign=("manager",))
    import uuid

    class Ghost:
        id = uuid.uuid4()

    answers = []
    for target in (inactive, unassigned, Ghost):
        r = await visit_in(client, emp, target)
        assert r.status_code == 422 and code(r) == "DUTY_LOCATION_UNAVAILABLE"
        e = r.json()["error"]
        answers.append((e["message"], e["details"]))
    assert len(set(map(repr, answers))) == 1  # no enumeration: same message and details for all three
    assert (await db.execute(select(OnDutyVisit))).first() is None
    # review 11 L4: audited (reason only, no coordinates) but NOT a punch failure (not in PUNCH_FAIL_CODES, counter untouched)
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "attendance.punch_rejected"))).scalars().all()
    assert len(rows) == 3 and all(a.after == {"kind": "on_duty_check_in", "reason": "DUTY_LOCATION_UNAVAILABLE"} for a in rows)


async def test_assign_mode_all_and_switching(client, emp, clock, db):
    site = await make_loc(db, "Everyone", mode="all", assign=())
    assert (await visit_in(client, emp, site)).status_code == 201
    await visit_out(client, emp)
    site.assign_mode = "selected"  # no explicit rows -> nobody
    await db.commit()
    r = await visit_in(client, emp, site)
    assert code(r) == "DUTY_LOCATION_UNAVAILABLE"


async def test_full_day_leave_blocks_half_day_allows(client, emp, clock, loc, db):
    await make_leave(db, "employee", WED, WED, half=True)
    assert (await visit_in(client, emp, loc)).status_code == 201
    await visit_out(client, emp)
    await make_leave(db, "employee", WED, WED)
    r = await visit_in(client, emp, loc)
    assert r.status_code == 409 and code(r) == "ON_LEAVE"


async def test_second_check_in_while_open_is_visit_open(client, emp, clock, loc, db):
    first = (await visit_in(client, emp, loc)).json()["visit"]
    r = await visit_in(client, emp, loc)
    assert r.status_code == 409 and code(r) == "VISIT_OPEN"
    assert r.json()["error"]["details"] == [{"field": "visit_id", "message": first["id"]}]
    other = await make_loc(db, "Branch")
    assert code(await visit_in(client, emp, other)) == "VISIT_OPEN"  # one open visit at a time, any location


async def test_double_tap_race_hits_unique_index_and_answers_visit_open(client, emp, clock, loc, db, monkeypatch):
    await db.execute(
        text("CREATE UNIQUE INDEX uq_one_open ON on_duty_visits (user_id) WHERE check_out_at IS NULL AND auto_closed = 0")
    )
    await db.commit()
    first = (await visit_in(client, emp, loc)).json()["visit"]
    real = od_svc._open_visit
    calls = {"n": 0}

    async def blind_first(session, uid, *, lock=False):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(session, uid, lock=lock)  # the pre-check loses the race

    monkeypatch.setattr(od_svc, "_open_visit", blind_first)
    r = await visit_in(client, emp, loc)
    assert r.status_code == 409 and code(r) == "VISIT_OPEN"
    assert r.json()["error"]["details"] == [{"field": "visit_id", "message": first["id"]}]
    assert len((await db.execute(select(OnDutyVisit))).scalars().all()) == 1


async def test_max_visits_per_day(client, emp, clock, loc, db):
    await set_setting(db, "on_duty.max_visits_per_day", 2)
    for i in range(2):
        clock(ist(2026, 10, 21, 10 + i * 2, 0))
        assert (await visit_in(client, emp, loc)).status_code == 201
        clock(ist(2026, 10, 21, 11 + i * 2, 0))
        assert (await visit_out(client, emp)).status_code == 200
    clock(ist(2026, 10, 21, 15, 0))
    r = await visit_in(client, emp, loc)
    assert r.status_code == 409 and code(r) == "MAX_VISITS_REACHED"
    t = (await client.get("/api/on-duty/today", headers=emp)).json()
    assert t["visits_remaining"] == 0 and t["blocked_reason"] == "MAX_VISITS_REACHED" and len(t["visits"]) == 2


# ------------------------------------------------------------------ approval mode
async def _request(db, role, d_from, d_to, loc=None, status="approved"):
    from app.models import OnDutyRequest

    r = OnDutyRequest(
        user_id=await user_id(db, role),
        from_date=d_from,
        to_date=d_to,
        location_id=loc.id if loc else None,
        purpose="p",
        status=status,
    )
    db.add(r)
    await db.commit()
    return r


async def test_approval_required_matrix(client, emp, clock, loc, db):
    await set_setting(db, "on_duty.approval_required", True)
    other = await make_loc(db, "Other Site")
    r = await visit_in(client, emp, loc)
    assert r.status_code == 409 and code(r) == "ON_DUTY_NOT_APPROVED"
    t = (await client.get("/api/on-duty/today", headers=emp)).json()
    assert t["can_check_in"] is False and t["blocked_reason"] == "ON_DUTY_NOT_APPROVED" and t["approval"]["required"] is True
    await _request(db, "employee", WED, WED, loc, status="pending")  # pending is not enough
    await _request(db, "employee", WED - dt.timedelta(days=3), WED - dt.timedelta(days=1), loc)  # other days
    await _request(db, "employee", WED, WED, other)  # approved for another location
    assert code(await visit_in(client, emp, loc)) == "ON_DUTY_NOT_APPROVED"
    t = (await client.get("/api/on-duty/today", headers=emp)).json()
    assert (
        t["approval"]["covered"] is True
        and t["approval"]["location_ids"] == [str(other.id)]
        and not t["approval"]["any_location"]
    )
    assert t["can_check_in"] is True  # advisory: covered for at least one assigned location? other is not assigned -> see below
    # exact request for this location
    req = await _request(db, "employee", WED, WED + dt.timedelta(days=2), loc)
    r = await visit_in(client, emp, loc)
    assert r.status_code == 201 and r.json()["visit"]["request_id"] == str(req.id)


async def test_approval_any_location_request_and_off_mode_ignores_requests(client, emp, clock, loc, db):
    await set_setting(db, "on_duty.approval_required", True)
    req = await _request(db, "employee", WED, WED, None)  # any assigned location
    t = (await client.get("/api/on-duty/today", headers=emp)).json()
    assert t["approval"] == {"required": True, "covered": True, "location_ids": [], "any_location": True}
    r = await visit_in(client, emp, loc)
    assert r.status_code == 201 and r.json()["visit"]["request_id"] == str(req.id)
    clock(ist(2026, 10, 21, 12, 0))
    await visit_out(client, emp)
    await set_setting(db, "on_duty.approval_required", False)
    await db.execute(text("DELETE FROM on_duty_requests"))
    await db.commit()
    assert (await visit_in(client, emp, loc)).status_code == 201  # no approval needed


# ------------------------------------------------------------------ fence + fix rules
async def test_outside_location_is_422_without_distance_and_audited(client, emp, clock, loc, db):
    r = await visit_in(client, emp, loc, **FAR)
    assert r.status_code == 422 and code(r) == "OUTSIDE_DUTY_LOCATION"
    e = r.json()["error"]
    assert e["details"] == [{"field": "within_fence", "message": "false"}]
    assert "distance" not in r.text.lower().replace("within_fence", "") and "radius" not in r.text
    assert (await db.execute(select(OnDutyVisit))).first() is None and (await db.execute(select(Attendance))).first() is None
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "attendance.punch_rejected"))).scalars().one()
    assert a.after == {"kind": "on_duty_check_in", "reason": "OUTSIDE_DUTY_LOCATION"} and a.outcome == "failure"


async def test_radius_is_per_location_boundary(client, emp, clock, db):
    tight = await make_loc(db, "Tight", radius=100)  # NEAR is ~100.07 m away
    assert code(await visit_in(client, emp, tight)) == "OUTSIDE_DUTY_LOCATION"
    tight.radius_m = 101
    await db.commit()
    assert (await visit_in(client, emp, tight)).status_code == 201


async def test_radius_smaller_than_accuracy_is_allowed(client, emp, clock, db):
    tiny = await make_loc(db, "Tiny", radius=10)
    r = await visit_in(client, emp, tiny, lat=SITE["lat"], lng=SITE["lng"], accuracy_m=80)
    assert r.status_code == 201


async def test_overlapping_locations_server_checks_only_the_chosen_one(client, emp, clock, db):
    a = await make_loc(db, "Site A", radius=50)
    b = await make_loc(db, "Site B", radius=200)
    assert code(await visit_in(client, emp, a)) == "OUTSIDE_DUTY_LOCATION"  # ~100 m from A, within B
    assert (await visit_in(client, emp, b)).status_code == 201


async def test_accuracy_stale_fix_and_mock_rules(client, emp, clock, loc, db):
    assert code(await visit_in(client, emp, loc, accuracy_m=100.5)) == "LOCATION_ACCURACY_LOW"
    stale = (att_svc.utcnow() - dt.timedelta(seconds=120)).isoformat()
    assert code(await visit_in(client, emp, loc, fix_time=stale)) == "LOCATION_FIX_STALE"
    future = (att_svc.utcnow() + dt.timedelta(seconds=120)).isoformat()
    assert code(await visit_in(client, emp, loc, fix_time=future)) == "LOCATION_FIX_STALE"
    r = await visit_in(client, emp, loc, is_mock_location=True)
    assert r.status_code == 422 and code(r) == "MOCK_LOCATION_DETECTED"
    reasons = [
        a.after["reason"]
        for a in (await db.execute(select(AuditLog).where(AuditLog.action == "attendance.punch_rejected"))).scalars()
    ]
    assert reasons == ["LOCATION_ACCURACY_LOW", "LOCATION_FIX_STALE", "LOCATION_FIX_STALE", "MOCK_LOCATION_DETECTED"]
    assert (await visit_in(client, emp, loc, accuracy_m=100)).status_code == 201


async def test_mock_accepted_when_rejection_off_flags_visit_and_audits(client, emp, clock, loc, db):
    await set_setting(db, "attendance.reject_mock_location", False)
    r = await visit_in(client, emp, loc, is_mock_location=True)
    assert r.status_code == 201
    v = (await db.execute(select(OnDutyVisit))).scalar_one()
    assert v.suspicious is True and v.check_in_mock_location is True
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "on_duty.suspicious_punch"))).scalars().one()
    assert a.after == {"kind": "check_in", "reason": "mock_flag"}


async def test_failure_counter_is_shared_with_office_and_success_resets(client, emp, clock, loc, db, redis):
    assert get_settings().punch_fail_limit == 5
    # 3 office failures + 2 on-duty failures exhaust the single per-user budget
    for _ in range(3):
        r = await client.post("/api/attendance/check-in", json={"lat": 1.0, "lng": 1.0, "accuracy_m": 10, **fix()}, headers=emp)
        assert code(r) == "OUTSIDE_GEOFENCE"
    for _ in range(2):
        assert code(await visit_in(client, emp, loc, **FAR)) == "OUTSIDE_DUTY_LOCATION"
    r = await visit_in(client, emp, loc)
    assert r.status_code == 429 and code(r) == "RATE_LIMITED" and "retry-after" in r.headers
    r = await client.post("/api/attendance/check-in", json={"lat": 1.0, "lng": 1.0, "accuracy_m": 10, **fix()}, headers=emp)
    assert r.status_code == 429
    await redis.flushall()
    assert (await visit_in(client, emp, loc, **FAR)).status_code == 422
    assert (await visit_in(client, emp, loc)).status_code == 201
    assert await redis.get(f"rl:punchfail:{await user_id(db, 'employee')}") is None  # success reset it


async def test_pre_fence_refusals_do_not_count_as_punch_failures(client, emp, clock, loc, db, redis):
    await set_setting(db, "on_duty.approval_required", True)
    for _ in range(6):
        assert code(await visit_in(client, emp, loc)) == "ON_DUTY_NOT_APPROVED"
    assert await redis.get(f"rl:punchfail:{await user_id(db, 'employee')}") is None


async def test_punch_limiter_is_shared_10_per_minute(client, emp, clock, loc, db):
    await set_setting(db, "on_duty.approval_required", True)  # cheap refusals still hit the 10/min limiter
    codes = []
    for i in range(12):
        r = await (visit_in(client, emp, loc) if i % 2 else office_in(client, emp, {"lat": 12.9716}))
        codes.append(r.status_code)
    assert 429 in codes[10:] and 429 not in codes[:10]


# ------------------------------------------------------------------ selfies
async def test_selfie_required_stale_reused_across_office_and_visit(client, emp, clock, loc, db):
    await set_setting(db, "on_duty.selfie_required", True)
    assert code(await visit_in(client, emp, loc)) == "SELFIE_REQUIRED"
    old = await make_file(db, age_s=700)
    assert code(await visit_in(client, emp, loc, selfie_file_id=str(old))) == "SELFIE_STALE"
    foreign = await make_file(db, role="manager")
    assert code(await visit_in(client, emp, loc, selfie_file_id=str(foreign))) == "VALIDATION_ERROR"
    wrong = await make_file(db, purpose="profile_photo")
    assert code(await visit_in(client, emp, loc, selfie_file_id=str(wrong))) == "VALIDATION_ERROR"
    ok = await make_file(db)
    r = await visit_in(client, emp, loc, selfie_file_id=str(ok))
    assert r.status_code == 201 and r.json()["visit"]["check_in_selfie_url"] == f"/api/files/{ok}"
    # single use: not as the check-out selfie of the same visit, not for an office punch
    assert code(await visit_out(client, emp, selfie_file_id=str(ok))) == "SELFIE_REUSED"
    assert code(await office_out(client, emp, {"selfie_file_id": str(ok)})) in {"VISIT_OPEN", "SELFIE_REUSED"}
    clock(ist(2026, 10, 21, 12, 0))
    fresh = await make_file(db)
    r = await visit_out(client, emp, selfie_file_id=str(fresh))  # optional at check-out, accepted when fresh
    assert r.status_code == 200 and r.json()["visit"]["check_out_selfie_url"] == f"/api/files/{fresh}"
    await set_setting(db, "on_duty.selfie_required", False)
    assert code(await visit_in(client, emp, loc, selfie_file_id=str(fresh))) == "SELFIE_REUSED"


async def test_office_selfie_cannot_be_replayed_for_a_visit(client, emp, clock, loc, db):
    fid = await make_file(db)
    assert (await office_in(client, emp, {"selfie_file_id": str(fid)})).status_code == 201
    assert code(await visit_in(client, emp, loc, selfie_file_id=str(fid))) == "SELFIE_REUSED"


# ------------------------------------------------------------------ late rule
async def test_day_starting_with_visit_is_not_late_by_default(client, emp, clock, loc, db):
    clock(ist(2026, 10, 21, 14, 0))  # shift starts 09:30 + 10 min grace
    r = await visit_in(client, emp, loc)
    assert r.json()["attendance"]["status"] == "present" and r.json()["attendance"]["minutes_late"] == 0
    assert (await db.execute(select(LateRecord))).first() is None


async def test_apply_late_rule_marks_first_visit_late(client, emp, clock, loc, db):
    await set_setting(db, "on_duty.apply_late_rule", True)
    clock(ist(2026, 10, 21, 10, 0))
    r = await visit_in(client, emp, loc)
    a = r.json()["attendance"]
    assert a["status"] == "late" and a["minutes_late"] == 30
    lr = (await db.execute(select(LateRecord))).scalar_one()
    assert lr.minutes_late == 30 and lr.attendance_id is not None
    await visit_out(client, emp)
    clock(ist(2026, 10, 21, 12, 0))
    await visit_in(client, emp, loc)  # a later visit never adds a second late record
    assert len((await db.execute(select(LateRecord))).scalars().all()) == 1


async def test_apply_late_rule_within_grace_and_on_holiday_not_late(client, emp, clock, loc, db):
    await set_setting(db, "on_duty.apply_late_rule", True)
    clock(ist(2026, 10, 21, 9, 40))  # exactly start + grace
    assert (await visit_in(client, emp, loc)).json()["attendance"]["status"] == "present"
    clock(ist(2026, 10, 21, 9, 50))
    await visit_out(client, emp)
    db.add(Holiday(date=dt.date(2026, 10, 22), name="Fest"))
    await db.commit()
    clock(ist(2026, 10, 22, 15, 0))
    assert (await visit_in(client, emp, loc)).json()["attendance"]["minutes_late"] == 0  # holiday: never late
    clock(ist(2026, 10, 25, 15, 0))
    r = await visit_in(client, emp, loc)  # the holiday visit was never closed
    assert code(r) == "VISIT_OPEN"
    await visit_out(client, emp)
    clock(ist(2026, 10, 25, 16, 0))
    assert (await visit_in(client, emp, loc)).json()["attendance"]["minutes_late"] == 0  # weekly off: never late


async def test_office_late_mark_is_kept_when_visits_follow(client, emp, clock, loc, db):
    clock(ist(2026, 10, 21, 10, 0))
    r = await office_in(client, emp)
    assert r.status_code == 201 and r.json()["status"] == "late"
    clock(ist(2026, 10, 21, 11, 0))
    r = await visit_in(client, emp, loc)
    a = r.json()["attendance"]
    assert a["work_mode"] == "mixed" and a["status"] == "late" and a["minutes_late"] == 30
    assert a["within_fence"] is True  # the office check-in fields are untouched
    clock(ist(2026, 10, 21, 17, 0))
    r = await visit_out(client, emp)
    assert r.json()["attendance"]["status"] == "late"  # still late after the closing punch
    assert len((await db.execute(select(LateRecord))).scalars().all()) == 1


# ------------------------------------------------------------------ check-out
async def test_check_out_flow_and_attendance_update(client, emp, clock, loc, db):
    assert code(await visit_out(client, emp)) == "NO_OPEN_VISIT"
    clock(ist(2026, 10, 21, 10, 0))
    vid = (await visit_in(client, emp, loc, note="in")).json()["visit"]["id"]
    clock(ist(2026, 10, 21, 15, 30))
    r = await visit_out(client, emp, visit_id=vid, note="done")
    assert r.status_code == 200, r.text
    v, a = r.json()["visit"], r.json()["attendance"]
    assert v["open"] is False and v["visit_minutes"] == 330 and v["note"] == "done"
    assert a["worked_minutes"] == 330 and a["status"] == "present" and a["check_out_source"] == "on_duty"
    assert a["check_out_at"] == "2026-10-21T10:00:00Z" and a["work_mode"] == "on_duty"
    assert code(await visit_out(client, emp)) == "NO_OPEN_VISIT"
    # check-in/out note: omitted note keeps the old one
    clock(ist(2026, 10, 21, 16, 0))
    await visit_in(client, emp, loc, note="keep")
    clock(ist(2026, 10, 21, 16, 30))
    assert (await visit_out(client, emp)).json()["visit"]["note"] == "keep"


async def test_check_out_requires_fix_fields(client, emp, clock, loc):
    await visit_in(client, emp, loc)
    for missing in ("lat", "lng", "accuracy_m", "fix_time"):
        b = {**NEAR, **fix()}
        del b[missing]
        r = await client.post("/api/on-duty/check-out", json=b, headers=emp)
        assert r.status_code == 422 and code(r) == "VALIDATION_ERROR", missing
    r = await client.post("/api/on-duty/check-out", json={**NEAR, **fix(), "bogus": 1}, headers=emp)
    assert r.status_code == 422


async def test_check_out_visit_id_must_match_open_visit(client, emp, clock, loc):
    import uuid

    await visit_in(client, emp, loc)
    clock(ist(2026, 10, 21, 12, 0))
    r = await visit_out(client, emp, visit_id=str(uuid.uuid4()))
    assert r.status_code == 404 and code(r) == "NOT_FOUND"
    assert (await visit_out(client, emp)).status_code == 200


async def test_check_out_fix_rules_audited_with_check_out_kind(client, emp, clock, loc, db):
    await visit_in(client, emp, loc)
    clock(ist(2026, 10, 21, 12, 0))
    assert code(await visit_out(client, emp, accuracy_m=500)) == "LOCATION_ACCURACY_LOW"
    assert code(await visit_out(client, emp, is_mock_location=True)) == "MOCK_LOCATION_DETECTED"
    stale = (att_svc.utcnow() - dt.timedelta(seconds=300)).isoformat()
    assert code(await visit_out(client, emp, fix_time=stale)) == "LOCATION_FIX_STALE"
    kinds = {
        a.after["kind"]
        for a in (await db.execute(select(AuditLog).where(AuditLog.action == "attendance.punch_rejected"))).scalars()
    }
    assert kinds == {"on_duty_check_out"}
    v = (await db.execute(select(OnDutyVisit))).scalar_one()
    assert v.check_out_at is None  # the visit is still open


async def test_check_out_outside_site_is_allowed_and_flagged(client, emp, clock, loc, db, tokens_for):
    await visit_in(client, emp, loc)
    clock(ist(2026, 10, 21, 12, 0))
    r = await visit_out(client, emp, **FAR)
    assert r.status_code == 200
    assert "check_out_within_fence" not in r.text  # employees never see the flag
    v = (await db.execute(select(OnDutyVisit))).scalar_one()
    assert v.check_out_within_fence is False and v.check_out_distance_m == 334
    hr = bearer(await tokens_for("hr"))
    item = (await client.get("/api/admin/on-duty/visits?date=2026-10-21", headers=hr)).json()["items"][0]
    assert item["check_out_within_fence"] is False and item["check_out_distance_m"] == 334 and item["check_in_distance_m"] == 100


async def test_check_out_geofence_setting_refuses_outside(client, emp, clock, loc, db):
    await set_setting(db, "on_duty.checkout_geofence", True)
    await visit_in(client, emp, loc)
    clock(ist(2026, 10, 21, 12, 0))
    r = await visit_out(client, emp, **FAR)
    assert r.status_code == 422 and code(r) == "OUTSIDE_DUTY_LOCATION"
    assert r.json()["error"]["details"] == [{"field": "within_fence", "message": "false"}]
    assert (await db.execute(select(OnDutyVisit))).scalar_one().check_out_at is None
    assert (await visit_out(client, emp)).status_code == 200  # inside: fine


async def test_check_out_works_after_deactivation_unassignment_and_disable(client, emp, clock, loc, db):
    await visit_in(client, emp, loc)
    loc.is_active = False
    loc.assign_mode = "selected"
    await set_setting(db, "on_duty.enabled", False)
    from sqlalchemy import delete

    from app.models import DutyLocationAssignment

    await db.execute(delete(DutyLocationAssignment))
    await db.commit()
    clock(ist(2026, 10, 21, 12, 0))
    r = await visit_out(client, emp)
    assert r.status_code == 200 and r.json()["visit"]["open"] is False


async def test_check_out_measures_against_current_site_coordinates(client, emp, clock, loc, db):
    await set_setting(db, "on_duty.checkout_geofence", True)
    await visit_in(client, emp, loc)
    loc.lat = 14.0  # the site was moved; past visits keep their stored data, the fence is the current row
    await db.commit()
    clock(ist(2026, 10, 21, 12, 0))
    assert code(await visit_out(client, emp)) == "OUTSIDE_DUTY_LOCATION"


async def test_suspicious_checkout_flags_visit(client, emp, clock, loc, db):
    await set_setting(db, "attendance.reject_mock_location", False)
    await visit_in(client, emp, loc)
    clock(ist(2026, 10, 21, 12, 0))
    await visit_out(client, emp, is_mock_location=True)
    v = (await db.execute(select(OnDutyVisit))).scalar_one()
    assert v.suspicious is True and v.check_out_mock_location is True
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "on_duty.suspicious_punch"))).scalars().all()
    mock_rows = [a for a in rows if a.after["reason"] == "mock_flag"]
    assert len(mock_rows) == 1 and mock_rows[0].after["kind"] == "check_out"


# ------------------------------------------------------------------ day model: half day, span, multiple visits
async def test_half_day_then_lifted_by_later_visit(client, emp, clock, loc, db):
    clock(ist(2026, 10, 21, 10, 0))
    await visit_in(client, emp, loc)
    clock(ist(2026, 10, 21, 11, 0))
    a = (await visit_out(client, emp)).json()["attendance"]
    assert a["status"] == "half_day" and a["worked_minutes"] == 60
    clock(ist(2026, 10, 21, 12, 0))
    await visit_in(client, emp, loc)
    clock(ist(2026, 10, 21, 15, 0))
    a = (await visit_out(client, emp)).json()["attendance"]
    assert a["status"] == "present" and a["worked_minutes"] == 300  # span 10:00-15:00 incl. travel between sites
    assert a["on_duty_locations"] == ["Acme Client"]  # distinct names
    assert len((await db.execute(select(OnDutyVisit))).scalars().all()) == 2
    assert len((await db.execute(select(Attendance))).scalars().all()) == 1  # one row per day


async def test_distinct_locations_in_first_visit_order(client, emp, clock, db):
    a = await make_loc(db, "Zeta Site")
    b = await make_loc(db, "Alpha Site")
    clock(ist(2026, 10, 21, 10, 0))
    await visit_in(client, emp, a)
    clock(ist(2026, 10, 21, 11, 0))
    await visit_out(client, emp)
    clock(ist(2026, 10, 21, 12, 0))
    await visit_in(client, emp, b)
    clock(ist(2026, 10, 21, 13, 0))
    r = await visit_out(client, emp)
    assert r.json()["attendance"]["on_duty_locations"] == ["Zeta Site", "Alpha Site"]


async def test_day_closes_from_last_visit_and_job_does_not_flag(client, emp, clock, loc, db):
    clock(ist(2026, 10, 21, 9, 30))
    assert (await office_in(client, emp)).status_code == 201
    clock(ist(2026, 10, 21, 11, 0))
    await visit_in(client, emp, loc)
    clock(ist(2026, 10, 21, 15, 0))
    a = (await visit_out(client, emp)).json()["attendance"]
    assert a["check_out_at"] == "2026-10-21T09:30:00Z" and a["worked_minutes"] == 330 and a["work_mode"] == "mixed"
    n = await att_svc.auto_close_open_records(db, now=ist(2026, 10, 22, 0, 30))
    assert n == 0  # no office check-out needed: the visit closed the day
    assert (await attendance_row(db)).auto_closed is False


# ------------------------------------------------------------------ office interplay
async def test_office_check_in_after_visit_is_already_checked_in(client, emp, clock, loc):
    first = (await visit_in(client, emp, loc)).json()["attendance"]
    r = await office_in(client, emp)
    assert r.status_code == 409 and code(r) == "ALREADY_CHECKED_IN"
    assert r.json()["error"]["details"] == [{"field": "record_id", "message": first["id"]}]


async def test_office_check_out_refused_while_visit_open(client, emp, clock, loc, db):
    await office_in(client, emp)
    vid = (await visit_in(client, emp, loc)).json()["visit"]["id"]
    r = await office_out(client, emp)
    assert r.status_code == 409 and code(r) == "VISIT_OPEN"
    assert r.json()["error"]["details"] == [{"field": "visit_id", "message": vid}]
    # "checked first, not counted": no punch_rejected audit and no failure counter
    assert not (await db.execute(select(AuditLog).where(AuditLog.action == "attendance.punch_rejected"))).first()
    clock(ist(2026, 10, 21, 12, 0))
    await visit_out(client, emp)
    assert (await office_out(client, emp)).status_code == 200


async def test_office_check_out_semantics_already_checked_out_only_for_office(client, emp, clock, loc, db):
    clock(ist(2026, 10, 21, 10, 0))
    await visit_in(client, emp, loc)
    clock(ist(2026, 10, 21, 12, 0))
    await visit_out(client, emp)
    row = await attendance_row(db)
    assert row.check_out_source == "on_duty"
    clock(ist(2026, 10, 21, 18, 0))
    r = await office_out(client, emp)  # "End day" after a visit-set check-out is allowed
    assert r.status_code == 200
    b = r.json()
    assert b["check_out_source"] == "office" and b["work_mode"] == "mixed" and b["worked_minutes"] == 480
    assert b["check_out_at"] == "2026-10-21T12:30:00Z" and b["on_duty_locations"] == ["Acme Client"]
    r = await office_out(client, emp)
    assert r.status_code == 409 and code(r) == "ALREADY_CHECKED_OUT"


async def test_office_check_out_without_row_is_not_checked_in(client, emp, clock):
    assert code(await office_out(client, emp)) == "NOT_CHECKED_IN"


async def test_visit_after_office_check_out_extends_the_day(client, emp, clock, loc, db):
    clock(ist(2026, 10, 21, 9, 30))
    await office_in(client, emp)
    clock(ist(2026, 10, 21, 12, 0))
    r = await office_out(client, emp)
    assert r.json()["status"] == "half_day" and r.json()["worked_minutes"] == 150
    clock(ist(2026, 10, 21, 13, 0))
    assert (await visit_in(client, emp, loc)).json()["attendance"]["work_mode"] == "mixed"
    clock(ist(2026, 10, 21, 16, 0))
    a = (await visit_out(client, emp)).json()["attendance"]
    assert a["check_out_at"] == "2026-10-21T10:30:00Z" and a["check_out_source"] == "on_duty"
    assert a["worked_minutes"] == 390 and a["status"] == "present"  # lifted from half_day
    # the office check-out is now available again (source is on_duty) and not blocked
    clock(ist(2026, 10, 21, 17, 0))
    assert (await office_out(client, emp)).status_code == 200


async def test_later_office_check_out_is_never_overwritten_by_a_visit(client, emp, clock, loc, db):
    clock(ist(2026, 10, 21, 9, 30))
    await office_in(client, emp)
    clock(ist(2026, 10, 21, 10, 0))
    await visit_in(client, emp, loc)
    row = await attendance_row(db)
    row.check_out_at = ist(2026, 10, 21, 18, 30)  # a later office check-out (e.g. clock correction)
    row.check_out_source = "office"
    await db.commit()
    clock(ist(2026, 10, 21, 12, 0))
    a = (await visit_out(client, emp)).json()["attendance"]
    assert a["check_out_source"] == "office" and a["check_out_at"] == "2026-10-21T13:00:00Z"
    assert a["worked_minutes"] == 540


async def test_office_check_in_unchanged_regression_shape(client, emp, clock):
    r = await office_in(client, emp)
    b = r.json()
    assert r.status_code == 201 and b["work_mode"] == "office" and b["on_duty_locations"] == [] and b["within_fence"] is True


# ------------------------------------------------------------------ auto close + retention jobs
async def test_job_flags_open_visit_of_past_day_but_keeps_check_out_null(client, emp, clock, loc, db):
    await visit_in(client, emp, loc)
    n = await att_svc.auto_close_open_records(db, now=ist(2026, 10, 22, 0, 30))
    assert n == 1  # the attendance row of the day has no check-out either
    v = (await db.execute(select(OnDutyVisit).execution_options(populate_existing=True))).scalar_one()
    assert v.auto_closed is True and v.check_out_at is None
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "on_duty.auto_close"))).scalars().one()
    assert a.after["count"] == 1
    assert await att_svc.auto_close_open_records(db, now=ist(2026, 10, 22, 0, 31)) == 0  # idempotent
    # an auto-closed visit is not "open": check-out says NO_OPEN_VISIT, a new visit may start (next day)
    clock(ist(2026, 10, 22, 10, 0))
    assert code(await visit_out(client, emp)) == "NO_OPEN_VISIT"
    r = await visit_in(client, emp, loc)
    assert r.status_code == 201
    out = (await client.get("/api/on-duty/visits?from=2026-10-21&to=2026-10-22", headers=emp)).json()["items"]
    old = [x for x in out if x["work_date"] == "2026-10-21"][0]
    assert old["auto_closed"] is True and old["open"] is False and old["visit_minutes"] is None and old["check_out_at"] is None


async def test_job_does_not_flag_today_open_visit(client, emp, clock, loc, db):
    await visit_in(client, emp, loc)
    await att_svc.auto_close_open_records(db, now=ist(2026, 10, 21, 23, 59))
    v = (await db.execute(select(OnDutyVisit).execution_options(populate_existing=True))).scalar_one()
    assert v.auto_closed is False


async def test_retention_purges_visit_locations_and_selfies(client, emp, clock, loc, db):
    fid_in = await make_file(db)
    await visit_in(client, emp, loc, selfie_file_id=str(fid_in))
    clock(ist(2026, 10, 21, 12, 0))
    fid_out = await make_file(db)
    await visit_out(client, emp, selfie_file_id=str(fid_out))
    now = ist(2026, 10, 21, 12, 0) + dt.timedelta(days=100)  # retention default 90 days
    out = await att_svc.purge_pii(db, now=now)
    assert out["locations"] >= 1 and out["selfies"] == 2
    raw = (
        await db.execute(
            text(
                "SELECT check_in_loc_enc, check_out_loc_enc, check_in_selfie_file_id, check_out_selfie_file_id FROM on_duty_visits"
            )
        )
    ).one()
    assert tuple(raw) == (None, None, None, None)
    assert (await db.execute(select(File))).scalars().all() == []
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "on_duty.retention_purge"))).scalars().one()
    assert a.after == {"loc_rows": 1, "selfie_files": 2}
    again = await att_svc.purge_pii(db, now=now)
    assert again == {"locations": 0, "selfies": 0}  # idempotent


async def test_orphan_cleanup_keeps_visit_selfies(client, emp, clock, loc, db):
    from app.services import files as files_svc

    fid = await make_file(db)
    assert (await visit_in(client, emp, loc, selfie_file_id=str(fid))).status_code == 201
    # the file is older than the orphan grace period, but a visit references it
    (await db.get(File, fid)).created_at = att_svc.utcnow() - dt.timedelta(days=3)
    await db.commit()
    removed = await files_svc.cleanup_orphans(db, older_than_hours=24)
    assert removed == 0 and await db.get(File, fid) is not None


# ------------------------------------------------------------------ today / locations / history / calendar / home / config
async def test_locations_endpoint_sorted_active_assigned_without_radius(client, emp, clock, db):
    await make_loc(db, "Zulu")
    await make_loc(db, "alpha")
    await make_loc(db, "Hidden", active=False)
    await make_loc(db, "Not Mine", assign=("manager",))
    await make_loc(db, "Everyone", mode="all", assign=())
    r = await client.get("/api/on-duty/locations", headers=emp)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["name"] for i in items] == ["alpha", "Everyone", "Zulu"]
    assert set(items[0]) == {"id", "name", "address", "lat", "lng"} and "radius" not in r.text
    await set_setting(db, "on_duty.enabled", False)
    assert (await client.get("/api/on-duty/locations", headers=emp)).json() == {"items": []}  # not an error


async def test_today_blocked_reasons_in_order(client, emp, clock, loc, db):
    t = (await client.get("/api/on-duty/today", headers=emp)).json()
    assert t["can_check_in"] is True and t["blocked_reason"] is None and t["visits_remaining"] == 10 and t["attendance"] is None
    assert t["open_visit"] is None and t["visits"] == [] and t["enabled"] is True and t["work_date"] == "2026-10-21"
    await make_leave(db, "employee", WED, WED)
    assert (await client.get("/api/on-duty/today", headers=emp)).json()["blocked_reason"] == "ON_LEAVE"
    await set_setting(db, "on_duty.enabled", False)
    t = (await client.get("/api/on-duty/today", headers=emp)).json()
    assert t["blocked_reason"] == "ON_DUTY_DISABLED" and t["enabled"] is False  # disabled wins over leave
    await set_setting(db, "on_duty.enabled", True)
    loc.is_active = False
    await db.commit()
    assert (await client.get("/api/on-duty/today", headers=emp)).json()["blocked_reason"] == "NO_LOCATIONS"


async def test_today_shows_open_visit_attendance_and_timeline(client, emp, clock, loc):
    await visit_in(client, emp, loc)
    t = (await client.get("/api/on-duty/today", headers=emp)).json()
    assert t["blocked_reason"] == "VISIT_OPEN" and t["can_check_in"] is False
    assert t["open_visit"]["open"] is True and [v["id"] for v in t["visits"]] == [t["open_visit"]["id"]]
    assert t["attendance"]["work_mode"] == "on_duty" and t["visits_remaining"] == 9


async def test_visits_history_pagination_range_and_privacy(client, emp, clock, loc, tokens_for, db):
    for i in range(3):
        clock(ist(2026, 10, 21, 10 + i * 2, 0))
        await visit_in(client, emp, loc)
        clock(ist(2026, 10, 21, 11 + i * 2, 0))
        await visit_out(client, emp)
    r = (await client.get("/api/on-duty/visits?page_size=2", headers=emp)).json()
    assert (
        r["total"] == 3 and len(r["items"]) == 2 and r["items"][0]["check_in_at"] > r["items"][1]["check_in_at"]
    )  # newest first
    assert (await client.get("/api/on-duty/visits?page=2&page_size=2", headers=emp)).json()["items"].__len__() == 1
    assert code(await client.get("/api/on-duty/visits?from=2026-10-10&to=2026-10-01", headers=emp)) == "INVALID_DATE_RANGE"
    assert code(await client.get("/api/on-duty/visits?from=2026-01-01&to=2026-10-21", headers=emp)) == "INVALID_DATE_RANGE"
    r = await client.get("/api/on-duty/visits?from=garbage", headers=emp)
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"
    assert (await client.get("/api/on-duty/visits?from=2026-10-22&to=2026-10-30", headers=emp)).json()["total"] == 0
    mgr = bearer(await tokens_for("manager"))
    assert (await client.get("/api/on-duty/visits", headers=mgr)).json()["total"] == 0  # only the caller's own visits


async def test_calendar_summary_history_and_team_carry_on_duty(client, emp, clock, loc, tokens_for):
    await visit_in(client, emp, loc)
    cal = {d["date"]: d for d in (await client.get("/api/attendance/calendar?month=2026-10", headers=emp)).json()["days"]}
    assert cal["2026-10-21"]["on_duty"] is True and cal["2026-10-20"]["on_duty"] is False
    s = (await client.get("/api/attendance/summary?month=2026-10", headers=emp)).json()
    assert s["on_duty_days"] == 1 and s["present"] + s["late"] + s["half_day"] >= 1  # not disjoint from present/late/half_day
    h = (await client.get("/api/attendance", headers=emp)).json()["items"][0]
    assert h["work_mode"] == "on_duty" and h["on_duty_locations"] == ["Acme Client"] and h["within_fence"] is None
    t = (await client.get("/api/attendance/today", headers=emp)).json()
    assert t["work_mode"] == "on_duty"
    mgr = bearer(await tokens_for("manager"))
    team = (await client.get("/api/attendance/team?date=2026-10-21", headers=mgr)).json()["items"][0]
    assert team["work_mode"] == "on_duty" and team["on_duty_locations"] == ["Acme Client"]


async def test_home_and_config(client, emp, clock, loc, db):
    c = (await client.get("/api/config", headers=emp)).json()["on_duty"]
    assert c == {
        "enabled": True, "has_locations": True, "approval_required": False, "selfie_required": False, "checkout_geofence": False,
        "max_visits_per_day": 10, "request_backdate_days": 0, "request_max_advance_days": 60,
    }  # fmt: skip
    loc.is_active = False
    await db.commit()
    assert (await client.get("/api/config", headers=emp)).json()["on_duty"]["has_locations"] is False
    loc.is_active = True
    await db.commit()
    h = (await client.get("/api/home", headers=emp)).json()["today_attendance"]
    assert h["open_visit"] is None and h["work_mode"] is None
    vid = (await visit_in(client, emp, loc)).json()["visit"]
    h = (await client.get("/api/home", headers=emp)).json()["today_attendance"]
    assert h["work_mode"] == "on_duty" and h["open_visit"] == {
        "id": vid["id"], "location_name": "Acme Client", "check_in_at": vid["check_in_at"],
    }  # fmt: skip
    await set_setting(db, "on_duty.enabled", False)
    assert (await client.get("/api/config", headers=emp)).json()["on_duty"]["has_locations"] is False


async def test_weekly_off_visit_allowed(client, emp, clock, loc):
    clock(ist(2026, 10, 25, 11, 0))
    assert SUN.isoweekday() == 7
    r = await visit_in(client, emp, loc)
    assert r.status_code == 201 and r.json()["attendance"]["minutes_late"] == 0


async def test_bearer_user_cannot_touch_other_users_visit(client, emp, clock, loc, tokens_for):
    await visit_in(client, emp, loc)
    mgr = bearer(await tokens_for("manager"))
    assert code(await visit_out(client, mgr)) == "NO_OPEN_VISIT"  # the manager has no open visit; employee's is untouched


async def test_visit_open_across_midnight_closes_the_day_it_started_on(client, emp, clock, loc, db):
    clock(ist(2026, 10, 21, 23, 50))
    vid = (await visit_in(client, emp, loc)).json()["visit"]["id"]
    clock(ist(2026, 10, 22, 0, 10))  # before the 00:30 job ran: the visit is still open
    t = (await client.get("/api/on-duty/today", headers=emp)).json()
    assert (
        t["open_visit"]["id"] == vid and t["visits"] == [] and t["blocked_reason"] == "VISIT_OPEN"
    )  # open_visit is not date-bound
    r = await visit_out(client, emp, visit_id=vid)
    assert r.status_code == 200
    a = r.json()["attendance"]
    assert a["work_date"] == "2026-10-21" and a["worked_minutes"] == 20 and a["status"] == "half_day"
    assert r.json()["visit"]["work_date"] == "2026-10-21"
