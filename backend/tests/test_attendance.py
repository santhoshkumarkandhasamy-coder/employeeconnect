"""Attendance + late (plans 02 / 04). 2026-10-21 is a Wednesday; office timezone Asia/Kolkata = UTC+5:30."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import undefer

from app.models import (
    Attendance,
    File,
    Holiday,
    LateRecord,
    LeaveRequest,
    LeaveType,
    PermissionRequest,
    Setting,
    User,
)
from app.services import attendance as att_svc
from tests.conftest import USERS, bearer

OFFICE = {"lat": 12.9716, "lng": 77.5946}
NEAR = {"lat": 12.9716 + 0.0009, "lng": 77.5946, "accuracy_m": 10}  # ~100 m north
FAR = {"lat": 12.9716 + 0.0020, "lng": 77.5946, "accuracy_m": 10}  # ~222 m north
WED = dt.date(2026, 10, 21)


def ist(y, mo, d, h, mi, s=0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).astimezone(dt.UTC)


@pytest.fixture
def clock(monkeypatch):
    def _set(when: dt.datetime):
        monkeypatch.setattr("app.services.attendance.utcnow", lambda: when)
        monkeypatch.setattr("app.services.late.utcnow", lambda: when)

    _set(ist(2026, 10, 21, 9, 0))
    return _set


@pytest.fixture
async def emp(tokens_for):
    return bearer(await tokens_for("employee"))


@pytest.fixture
async def setting(db):
    async def _set(key, value):
        row = await db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value
        await db.commit()

    return _set


async def uid(db, role) -> uuid.UUID:
    return (await db.execute(select(User.id).where(User.employee_id == USERS[role]))).scalar_one()


def _fix() -> dict:
    """A fresh GPS fix relative to the (patched) server clock."""
    return {"fix_time": att_svc.utcnow().isoformat()}


async def check_in(client, h, body=None, **kw):
    return await client.post("/api/attendance/check-in", json={**NEAR, **_fix(), **(body or {})}, headers=h, **kw)


async def check_out(client, h, body=None):
    return await client.post("/api/attendance/check-out", json={**NEAR, **_fix(), **(body or {})}, headers=h)


# ------------------------------------------------------------------ geo-fence
async def test_checkin_inside_fence(client, emp, clock, db):
    r = await check_in(client, emp)
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["status"] == "present" and b["minutes_late"] == 0 and b["within_fence"] is True
    assert b["distance_m"] is None  # exact distance is not exposed to the employee
    assert b["work_date"] == "2026-10-21" and b["check_in_at"].endswith("Z")
    assert b["shift"] == {"start": "09:30", "end": "18:30", "grace_minutes": 10}


async def test_checkin_outside_fence(client, emp, clock):
    r = await check_in(client, emp, FAR)
    assert r.status_code == 422
    e = r.json()["error"]
    assert e["code"] == "OUTSIDE_GEOFENCE"
    assert {d["field"] for d in e["details"]} == {"within_fence"}  # no distance_m / radius_m (trilateration oracle)
    assert "distance_m" not in r.text and "radius_m" not in r.text
    assert (await client.get("/api/attendance/today", headers=emp)).json() == {"record": None, "on_duty": None}


async def test_fence_radius_boundary_from_settings(client, emp, clock, setting):
    await setting("office.radius_m", 100)  # point is ~100.07 m away
    assert (await check_in(client, emp)).status_code == 422
    await setting("office.radius_m", 101)
    assert (await check_in(client, emp)).status_code == 201


async def test_low_accuracy_rejected(client, emp, clock):
    r = await check_in(client, emp, {"accuracy_m": 100.5})
    assert r.status_code == 422 and r.json()["error"]["code"] == "LOCATION_ACCURACY_LOW"
    assert (await check_in(client, emp, {"accuracy_m": 100})).status_code == 201


async def test_invalid_coordinates_422(client, emp, clock):
    r = await check_in(client, emp, {"lat": 91})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_location_is_encrypted_at_rest(client, emp, clock, db):
    await check_in(client, emp)
    raw = (await db.execute(text("SELECT check_in_loc_enc FROM attendance"))).scalar_one()
    assert isinstance(raw, bytes) and b"12.97" not in raw and raw.startswith(b"gAAAA")
    rec = (await db.execute(select(Attendance).options(undefer(Attendance.check_in_loc)))).scalar_one()
    assert abs(rec.check_in_loc["lat"] - NEAR["lat"]) < 1e-9


# ------------------------------------------------------------------ double check-in / check-out
async def test_double_checkin_409_with_existing_id(client, emp, clock):
    first = (await check_in(client, emp)).json()
    r = await check_in(client, emp)
    assert r.status_code == 409
    e = r.json()["error"]
    assert e["code"] == "ALREADY_CHECKED_IN"
    assert e["details"] == [{"field": "record_id", "message": first["id"]}]


async def test_checkout_flow(client, emp, clock):
    assert (await check_out(client, emp)).json()["error"]["code"] == "NOT_CHECKED_IN"
    await check_in(client, emp)
    clock(ist(2026, 10, 21, 18, 30))
    r = await check_out(client, emp)
    assert r.status_code == 200
    b = r.json()
    assert b["worked_minutes"] == 570 and b["status"] == "present" and b["check_out_at"].endswith("Z")
    r = await check_out(client, emp)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ALREADY_CHECKED_OUT"
    assert (await client.get("/api/attendance/today", headers=emp)).json()["check_out_at"] is not None


async def test_half_day_on_short_work(client, emp, clock):
    await check_in(client, emp)  # 09:00
    clock(ist(2026, 10, 21, 12, 59))  # 239 min
    b = (await check_out(client, emp)).json()
    assert b["worked_minutes"] == 239 and b["status"] == "half_day"


async def test_full_threshold_not_half_day(client, emp, clock):
    await check_in(client, emp)
    clock(ist(2026, 10, 21, 13, 0))  # exactly 240
    assert (await check_out(client, emp)).json()["status"] == "present"


async def test_checkout_geofence_only_when_enabled(client, emp, clock, setting):
    await check_in(client, emp)
    clock(ist(2026, 10, 21, 18, 0))
    await setting("attendance.checkout_geofence", True)
    r = await check_out(client, emp, FAR)
    assert r.status_code == 422 and r.json()["error"]["code"] == "OUTSIDE_GEOFENCE"
    await setting("attendance.checkout_geofence", False)
    assert (await check_out(client, emp, FAR)).status_code == 200


async def test_late_checkin_then_short_day_keeps_late_record(client, emp, clock, db):
    clock(ist(2026, 10, 21, 10, 30))
    assert (await check_in(client, emp)).json()["status"] == "late"
    clock(ist(2026, 10, 21, 11, 0))
    assert (await check_out(client, emp)).json()["status"] == "half_day"
    assert len((await db.execute(select(LateRecord))).scalars().all()) == 1


# ------------------------------------------------------------------ late / grace boundaries
@pytest.mark.parametrize(
    "hms,late",
    [((9, 30, 0), 0), ((9, 40, 0), 0), ((9, 40, 1), 10), ((9, 41, 0), 11), ((10, 30, 0), 60), ((8, 0, 0), 0)],
)
async def test_grace_boundaries(client, emp, clock, db, hms, late):
    clock(ist(2026, 10, 21, *hms))
    b = (await check_in(client, emp)).json()
    assert b["minutes_late"] == late and b["status"] == ("late" if late else "present")
    rows = (await db.execute(select(LateRecord))).scalars().all()
    if late:
        r = rows[0]
        assert (r.minutes_late, r.grace_minutes, r.shift_start, r.work_date) == (late, 10, dt.time(9, 30), WED)
    else:
        assert rows == []


async def test_late_snapshot_survives_grace_change(client, emp, clock, db):
    clock(ist(2026, 10, 21, 10, 0))
    await check_in(client, emp)
    from app.models import Shift

    shift = (await db.execute(select(Shift))).scalars().first()
    shift.grace_minutes = 45
    await db.commit()
    items = (await client.get("/api/late", headers=emp)).json()["items"]
    assert items[0]["grace_minutes"] == 10 and items[0]["minutes_late"] == 30 and items[0]["shift_start"] == "09:30"


async def test_holiday_checkin_never_late(client, emp, clock, db):
    db.add(Holiday(date=WED, name="Diwali"))
    await db.commit()
    clock(ist(2026, 10, 21, 11, 0))
    b = (await check_in(client, emp)).json()
    assert b["status"] == "present" and b["minutes_late"] == 0
    assert (await db.execute(select(LateRecord))).scalars().all() == []


async def test_weekly_off_checkin_never_late(client, emp, clock):
    clock(ist(2026, 10, 24, 11, 0))  # Saturday
    b = (await check_in(client, emp)).json()
    assert b["status"] == "present" and b["work_date"] == "2026-10-24"


async def test_permission_extends_limit(client, emp, clock, db):
    db.add(
        PermissionRequest(
            user_id=await uid(db, "employee"),
            request_date=WED,
            from_time=dt.time(9, 30),
            to_time=dt.time(11, 0),
            minutes=90,
            reason="bank",
            status="approved",
        )
    )
    await db.commit()
    clock(ist(2026, 10, 21, 11, 10))  # to_time + grace
    assert (await check_in(client, emp)).json()["status"] == "present"


async def test_permission_extends_but_not_past_limit(client, emp, clock, db):
    db.add(
        PermissionRequest(
            user_id=await uid(db, "employee"),
            request_date=WED,
            from_time=dt.time(9, 30),
            to_time=dt.time(11, 0),
            minutes=90,
            reason="bank",
            status="approved",
        )
    )
    await db.commit()
    clock(ist(2026, 10, 21, 11, 11))
    b = (await check_in(client, emp)).json()
    assert b["status"] == "late" and b["minutes_late"] == 101  # counted from shift start


async def test_pending_permission_does_not_help(client, emp, clock, db):
    db.add(
        PermissionRequest(
            user_id=await uid(db, "employee"),
            request_date=WED,
            from_time=dt.time(9, 30),
            to_time=dt.time(11, 0),
            minutes=90,
            reason="x",
            status="pending",
        )
    )
    await db.commit()
    clock(ist(2026, 10, 21, 10, 0))
    assert (await check_in(client, emp)).json()["status"] == "late"


# ------------------------------------------------------------------ leave
async def _leave(db, role, d_from, d_to, half=False, session=None):
    lt = LeaveType(code="CL", name="Casual Leave", annual_quota=12)
    db.add(lt)
    await db.flush()
    db.add(
        LeaveRequest(
            user_id=await uid(db, role),
            leave_type_id=lt.id,
            from_date=d_from,
            to_date=d_to,
            half_day=half,
            half_day_session=session,
            days=0.5 if half else 1,
            reason="r",
            status="approved",
        )
    )
    await db.commit()


async def test_checkin_on_leave_409(client, emp, clock, db):
    await _leave(db, "employee", WED, WED)
    r = await check_in(client, emp)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ON_LEAVE"


async def test_half_day_leave_allows_checkin_and_is_not_late_in_morning(client, emp, clock, db):
    await _leave(db, "employee", WED, WED, half=True, session="first")
    clock(ist(2026, 10, 21, 13, 0))
    b = (await check_in(client, emp)).json()
    assert b["status"] == "present"


async def test_no_shift_assigned(client, emp, clock, db):
    u = (await db.execute(select(User).where(User.employee_id == "EMP001"))).unique().scalar_one()
    u.shift_id = None
    await db.commit()
    r = await check_in(client, emp)
    assert r.status_code == 422 and r.json()["error"]["code"] == "NO_SHIFT_ASSIGNED"


# ------------------------------------------------------------------ timezone
async def test_work_date_uses_office_timezone(client, emp, clock):
    clock(dt.datetime(2026, 10, 20, 20, 0, tzinfo=dt.UTC))  # 01:30 IST on the 21st (before shift: not late)
    b = (await check_in(client, emp)).json()
    assert b["work_date"] == "2026-10-21" and b["check_in_at"] == "2026-10-20T20:00:00Z"


async def test_timezone_setting_changes_work_date_and_lateness(client, emp, clock, setting):
    await setting("office.timezone", "America/New_York")
    clock(dt.datetime(2026, 10, 21, 13, 45, tzinfo=dt.UTC))  # 09:45 EDT -> late by 15
    b = (await check_in(client, emp)).json()
    assert b["work_date"] == "2026-10-21" and b["minutes_late"] == 15


async def test_invalid_timezone_setting_falls_back(client, emp, clock, setting):
    await setting("office.timezone", "Not/AZone")
    assert (await check_in(client, emp)).status_code == 201


# ------------------------------------------------------------------ selfie
async def _file(db, role, purpose="selfie", age_s: int = 30) -> uuid.UUID:
    f = File(
        owner_id=await uid(db, role),
        purpose=purpose,
        storage_path="x",
        content_type="image/jpeg",
        size_bytes=1,
        created_at=att_svc.utcnow() - dt.timedelta(seconds=age_s),
    )
    db.add(f)
    await db.commit()
    return f.id


async def test_selfie_required_setting(client, emp, clock, setting, db):
    await setting("attendance.selfie_required", True)
    r = await check_in(client, emp)
    assert r.status_code == 422 and r.json()["error"]["code"] == "SELFIE_REQUIRED"
    fid = await _file(db, "employee")
    r = await check_in(client, emp, {"selfie_file_id": str(fid)})
    assert r.status_code == 201 and r.json()["selfie_url"] == f"/api/files/{fid}"


async def test_selfie_must_be_own_and_selfie_purpose(client, emp, clock, db):
    other = await _file(db, "manager")
    wrong = await _file(db, "employee", "leave_attachment")
    for fid in (other, wrong, uuid.uuid4()):
        r = await check_in(client, emp, {"selfie_file_id": str(fid)})
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------------ history / calendar / summary
async def _seed(db, role, day, status="present", minutes_late=0, worked=540, auto=False):
    u = await uid(db, role)
    a = Attendance(
        user_id=u,
        work_date=day,
        check_in_at=ist(day.year, day.month, day.day, 9, 30 + minutes_late if minutes_late < 30 else 9),
        check_out_at=None if worked is None else ist(day.year, day.month, day.day, 18, 30),
        status=status,
        minutes_late=minutes_late,
        worked_minutes=worked,
        auto_closed=auto,
    )
    db.add(a)
    await db.flush()
    if minutes_late:
        db.add(
            LateRecord(
                user_id=u,
                attendance_id=a.id,
                work_date=day,
                shift_start=dt.time(9, 30),
                grace_minutes=10,
                check_in_at=a.check_in_at,
                minutes_late=minutes_late,
            )
        )
    await db.commit()
    return a


async def test_history_paginated_and_filtered(client, emp, clock, db):
    for d in (1, 2, 5, 6, 7):
        await _seed(db, "employee", dt.date(2026, 10, d))
    r = (await client.get("/api/attendance?page_size=2&page=1", headers=emp)).json()
    assert r["total"] == 5 and [i["work_date"] for i in r["items"]] == ["2026-10-07", "2026-10-06"]
    r = (await client.get("/api/attendance?from=2026-10-02&to=2026-10-05", headers=emp)).json()
    assert [i["work_date"] for i in r["items"]] == ["2026-10-05", "2026-10-02"]
    assert (await client.get("/api/attendance?from=2026-10-05&to=2026-10-02", headers=emp)).status_code == 422


async def test_calendar_statuses(client, emp, clock, db):
    # today = Wed 2026-10-21; record day 1 present, day 2 late, day 5 half_day; day 6 holiday; day 7 leave; day 8 absent
    await _seed(db, "employee", dt.date(2026, 10, 1))
    await _seed(db, "employee", dt.date(2026, 10, 2), "late", 25)
    await _seed(db, "employee", dt.date(2026, 10, 5), "half_day", worked=120)
    db.add(Holiday(date=dt.date(2026, 10, 6), name="Fest"))
    await db.commit()
    await _leave(db, "employee", dt.date(2026, 10, 7), dt.date(2026, 10, 7))
    days = {d["date"]: d for d in (await client.get("/api/attendance/calendar?month=2026-10", headers=emp)).json()["days"]}
    assert len(days) == 31
    assert days["2026-10-01"]["status"] == "present"
    assert days["2026-10-02"]["status"] == "late"
    assert days["2026-10-03"]["status"] == "weekly_off" and days["2026-10-04"]["status"] == "weekly_off"
    assert days["2026-10-05"]["status"] == "half_day"
    assert days["2026-10-06"] == {
        "date": "2026-10-06",
        "status": "holiday",
        "holiday_name": "Fest",
        "leave_type": None,
        "on_duty": None,
    }
    assert days["2026-10-07"]["status"] == "on_leave" and days["2026-10-07"]["leave_type"] == "Casual Leave"
    assert days["2026-10-08"]["status"] == "absent"
    assert days["2026-10-21"]["status"] == "future" and days["2026-10-22"]["status"] == "future"


async def test_calendar_holiday_beats_leave_and_bad_month(client, emp, clock, db):
    db.add(Holiday(date=dt.date(2026, 10, 7), name="Fest"))
    await db.commit()
    await _leave(db, "employee", dt.date(2026, 10, 7), dt.date(2026, 10, 7))
    days = {d["date"]: d for d in (await client.get("/api/attendance/calendar?month=2026-10", headers=emp)).json()["days"]}
    assert days["2026-10-07"]["status"] == "holiday"
    for bad in ("2026-13", "202610", "abc"):
        r = await client.get(f"/api/attendance/calendar?month={bad}", headers=emp)
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_summary_counts(client, emp, clock, db):
    await _seed(db, "employee", dt.date(2026, 10, 1), worked=540)
    await _seed(db, "employee", dt.date(2026, 10, 2), "late", 25, worked=500)
    await _seed(db, "employee", dt.date(2026, 10, 5), "half_day", worked=120)
    await _leave(db, "employee", dt.date(2026, 10, 7), dt.date(2026, 10, 7))
    r = (await client.get("/api/attendance/summary?month=2026-10", headers=emp)).json()
    # working days 1..20 Oct (Mon-Fri) = 14; recorded 3, leave 1 -> absent 10
    assert r == {
        "month": "2026-10", "present": 1, "late": 1, "absent": 10, "on_leave": 1, "half_day": 1, "total_worked_minutes": 1160,
    }  # fmt: skip


async def test_summary_future_month_has_no_absences(client, emp, clock):
    r = (await client.get("/api/attendance/summary?month=2026-12", headers=emp)).json()
    assert r["absent"] == 0 and r["present"] == 0


async def test_today_returns_record(client, emp, clock):
    await check_in(client, emp)
    assert (await client.get("/api/attendance/today", headers=emp)).json()["status"] == "present"


# ------------------------------------------------------------------ RBAC scope
async def test_team_requires_permission(client, emp):
    assert (await client.get("/api/attendance/team", headers=emp)).status_code == 403
    assert (await client.get("/api/late/team", headers=emp)).status_code == 403
    assert (await client.get("/api/attendance")).status_code == 401


async def test_team_scope_manager_vs_hr(client, tokens_for, clock, db):
    d = dt.date(2026, 10, 20)
    await _seed(db, "employee", d, "late", 20)
    await _seed(db, "manager", d)
    await _seed(db, "hr", d)
    mgr = bearer(await tokens_for("manager"))
    r = (await client.get("/api/attendance/team?date=2026-10-20", headers=mgr)).json()
    assert [i["user"]["employee_id"] for i in r["items"]] == ["EMP001"]  # direct reports only
    assert r["items"][0]["user"]["department"] == "General" and r["total"] == 1
    for role in ("hr", "admin"):
        h = bearer(await tokens_for(role))
        r = (await client.get("/api/attendance/team?date=2026-10-20", headers=h)).json()
        assert {i["user"]["employee_id"] for i in r["items"]} == {"EMP001", "MGR001", "HR001"}
    # filters
    hr = bearer(await tokens_for("hr"))
    r = (await client.get("/api/attendance/team?from=2026-10-01&to=2026-10-31&employee_id=emp001", headers=hr)).json()
    assert r["total"] == 1
    r = (await client.get(f"/api/attendance/team?department_id={uuid.uuid4()}", headers=hr)).json()
    assert r["total"] == 0
    # manager cannot widen scope by filtering on someone else
    r = (await client.get("/api/attendance/team?employee_id=HR001", headers=mgr)).json()
    assert r["total"] == 0


async def test_employee_only_sees_own_history(client, tokens_for, clock, db):
    await _seed(db, "employee", dt.date(2026, 10, 20))
    await _seed(db, "manager", dt.date(2026, 10, 19))
    r = (await client.get("/api/attendance", headers=bearer(await tokens_for("employee")))).json()
    assert r["total"] == 1
    r = (await client.get("/api/attendance", headers=bearer(await tokens_for("manager")))).json()
    assert r["total"] == 1 and r["items"][0]["work_date"] == "2026-10-19"


# ------------------------------------------------------------------ late endpoints
async def test_late_list_and_summary_math(client, emp, clock, db):
    await _seed(db, "employee", dt.date(2026, 10, 1), "late", 20)
    await _seed(db, "employee", dt.date(2026, 10, 2), "late", 25)
    await _seed(db, "employee", dt.date(2026, 10, 5), "late", 20)
    s = (await client.get("/api/late/summary?month=2026-10", headers=emp)).json()
    assert s["late_count"] == 3 and s["total_minutes_late"] == 65 and s["average_minutes_late"] == 21.7
    assert s["days"] == [
        {"date": "2026-10-01", "minutes_late": 20},
        {"date": "2026-10-02", "minutes_late": 25},
        {"date": "2026-10-05", "minutes_late": 20},
    ]
    items = (await client.get("/api/late?from=2026-10-02", headers=emp)).json()
    assert items["total"] == 2 and items["items"][0]["work_date"] == "2026-10-05"
    assert set(items["items"][0]) == {"id", "work_date", "shift_start", "grace_minutes", "check_in_at", "minutes_late"}


async def test_late_summary_empty_and_month_boundaries(client, emp, clock, db):
    r = (await client.get("/api/late/summary?month=2026-10", headers=emp)).json()
    assert r == {"month": "2026-10", "late_count": 0, "total_minutes_late": 0, "average_minutes_late": 0, "days": []}
    await _seed(db, "employee", dt.date(2026, 9, 30), "late", 20)
    await _seed(db, "employee", dt.date(2026, 10, 31), "late", 30)
    await _seed(db, "employee", dt.date(2026, 11, 1), "late", 40)
    oct_ = (await client.get("/api/late/summary?month=2026-10", headers=emp)).json()
    assert [d["date"] for d in oct_["days"]] == ["2026-10-31"]
    sep = (await client.get("/api/late/summary?month=2026-09", headers=emp)).json()
    assert sep["late_count"] == 1 and sep["total_minutes_late"] == 20


async def test_late_via_checkin_appears_in_summary(client, emp, clock):
    clock(ist(2026, 10, 21, 10, 0))
    await check_in(client, emp)
    s = (await client.get("/api/late/summary", headers=emp)).json()  # default month = current office month
    assert s["month"] == "2026-10" and s["late_count"] == 1 and s["total_minutes_late"] == 30


async def test_late_team_scope(client, tokens_for, clock, db):
    d = dt.date(2026, 10, 20)
    await _seed(db, "employee", d, "late", 20)
    await _seed(db, "hr", d, "late", 30)
    mgr = bearer(await tokens_for("manager"))
    r = (await client.get("/api/late/team", headers=mgr)).json()
    assert [i["user"]["employee_id"] for i in r["items"]] == ["EMP001"] and r["items"][0]["minutes_late"] == 20
    hr = bearer(await tokens_for("hr"))
    r = (await client.get("/api/late/team?from=2026-10-01&to=2026-10-31", headers=hr)).json()
    assert {i["user"]["employee_id"] for i in r["items"]} == {"EMP001", "HR001"}
    assert (await client.get("/api/late/team?from=2026-10-31&to=2026-10-01", headers=hr)).status_code == 422


# ------------------------------------------------------------------ holidays, rate limit, auto-close
async def test_holidays_read(client, emp, db):
    db.add_all([Holiday(date=dt.date(2026, 1, 26), name="Republic Day"), Holiday(date=dt.date(2025, 12, 25), name="Xmas")])
    await db.commit()
    for path in ("/api/holidays", "/api/admin/holidays"):  # the /admin alias is deprecated but still served
        r = (await client.get(f"{path}?year=2026", headers=emp)).json()
        assert [i["name"] for i in r["items"]] == ["Republic Day"]


async def test_checkin_rate_limited(client, emp, clock):
    codes = [(await check_in(client, emp)).status_code for _ in range(11)]
    assert codes[0] == 201 and codes[1:10] == [409] * 9 and codes[10] == 429


async def test_auto_close_job(db, seeded, clock):
    old = await _seed(db, "employee", dt.date(2026, 10, 20), worked=None)  # no check-out, yesterday
    done = await _seed(db, "manager", dt.date(2026, 10, 20))
    today_open = await _seed(db, "hr", WED, worked=None)
    clock(ist(2026, 10, 21, 0, 30))
    # WED 00:30 IST: yesterday's open record is closed; "today" (the 21st) is untouched
    assert await att_svc.auto_close_open_records(db) == 1
    await db.refresh(old)
    await db.refresh(done)
    await db.refresh(today_open)
    assert old.auto_closed and old.check_out_at is None and old.worked_minutes is None
    assert not done.auto_closed and not today_open.auto_closed
    assert await att_svc.auto_close_open_records(db) == 0  # idempotent
    audit_n = (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action='attendance.auto_close'"))).scalar_one()
    assert audit_n == 1


# ------------------------------------------------------------------ security review 02: H1 fix freshness / accuracy / mock
async def _audit_rows(db, action="attendance.punch_rejected"):
    from app.models import AuditLog

    return (await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()


async def test_accuracy_is_mandatory(client, emp, clock, db):
    body = {k: v for k, v in NEAR.items() if k != "accuracy_m"}
    r = await client.post("/api/attendance/check-in", json={**body, **_fix()}, headers=emp)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["details"] == [{"field": "accuracy_m", "message": "required"}]
    r = await client.post("/api/attendance/check-in", json={**body, **_fix(), "accuracy_m": None}, headers=emp)
    assert r.status_code == 422
    assert len(await _audit_rows(db)) == 2


async def test_fix_time_is_mandatory(client, emp, clock):
    r = await client.post("/api/attendance/check-in", json=NEAR, headers=emp)
    assert r.status_code == 422 and r.json()["error"]["details"] == [{"field": "fix_time", "message": "required"}]


async def test_stale_and_future_fix_rejected(client, emp, clock, db):
    now = att_svc.utcnow()
    for delta in (-61, 31):
        r = await check_in(client, emp, {"fix_time": (now + dt.timedelta(seconds=delta)).isoformat()})
        assert r.status_code == 422 and r.json()["error"]["code"] == "LOCATION_FIX_STALE", delta
    # boundaries are accepted, and a client with a wrong clock cannot help itself: only server time counts
    assert (await check_in(client, emp, {"fix_time": (now - dt.timedelta(seconds=59)).isoformat()})).status_code == 201
    assert (await _audit_rows(db))[0].after == {"kind": "check_in", "reason": "LOCATION_FIX_STALE"}


async def test_fix_window_is_configurable(client, emp, clock, setting):
    await setting("attendance.max_fix_age_s", 300)
    old = (att_svc.utcnow() - dt.timedelta(seconds=200)).isoformat()
    assert (await check_in(client, emp, {"fix_time": old})).status_code == 201


async def test_client_time_cannot_override_server_time(client, emp, clock):
    far = (att_svc.utcnow() - dt.timedelta(days=3)).isoformat()
    b = (await check_in(client, emp, {"client_time": far})).json()
    assert b["check_in_at"] == "2026-10-21T03:30:00Z"  # 09:00 IST, from the server clock


async def test_mock_location_rejected_by_default_and_audited(client, emp, clock, db):
    r = await check_in(client, emp, {"is_mock_location": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "MOCK_LOCATION_DETECTED"
    assert (await client.get("/api/attendance/today", headers=emp)).json() == {"record": None, "on_duty": None}
    assert (await _audit_rows(db))[0].after["reason"] == "MOCK_LOCATION_DETECTED"
    assert (await check_in(client, emp, {"is_mock_location": False})).status_code == 201


async def test_mock_location_flagged_when_rejection_disabled(client, emp, clock, db, setting):
    await setting("attendance.reject_mock_location", False)
    assert (await check_in(client, emp, {"is_mock_location": True, "accuracy_m": 12.5})).status_code == 201
    rec = (await db.execute(select(Attendance))).scalar_one()
    assert rec.suspicious is True and rec.check_in_mock_location is True and rec.check_in_accuracy_m == 12.5
    assert len(await _audit_rows(db, "attendance.suspicious_punch")) == 1
    clock(ist(2026, 10, 21, 18, 0))
    assert (await check_out(client, emp, {"is_mock_location": True})).status_code == 200
    await db.refresh(rec)
    assert rec.check_out_mock_location is True


async def test_clean_punch_is_not_suspicious_and_stores_evidence(client, emp, clock, db):
    skew = (att_svc.utcnow() - dt.timedelta(seconds=7)).isoformat()
    await check_in(client, emp, {"client_time": skew})
    rec = (await db.execute(select(Attendance))).scalar_one()
    assert rec.suspicious is False and rec.check_in_mock_location is False
    assert rec.check_in_accuracy_m == 10 and rec.check_in_clock_skew_s == 7


async def test_checkout_requires_fix_only_with_geofence(client, emp, clock, setting):
    await check_in(client, emp)
    clock(ist(2026, 10, 21, 18, 0))
    await setting("attendance.checkout_geofence", True)
    bare = {k: v for k, v in NEAR.items() if k != "accuracy_m"}
    r = await client.post("/api/attendance/check-out", json=bare, headers=emp)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    stale = (att_svc.utcnow() - dt.timedelta(minutes=10)).isoformat()
    r = await check_out(client, emp, {"fix_time": stale})
    assert r.json()["error"]["code"] == "LOCATION_FIX_STALE"
    await setting("attendance.checkout_geofence", False)
    assert (await client.post("/api/attendance/check-out", json=bare, headers=emp)).status_code == 200


async def test_rejected_punches_audited_without_coordinates(client, emp, clock, db):
    await check_in(client, emp, FAR)
    await check_in(client, emp)
    await check_in(client, emp)  # ALREADY_CHECKED_IN
    rows = await _audit_rows(db)
    assert [r.after["reason"] for r in rows] == ["OUTSIDE_GEOFENCE", "ALREADY_CHECKED_IN"]
    assert all(r.outcome == "failure" and "12.97" not in str(r.after) for r in rows)


async def test_failed_punch_cooldown(client, emp, clock, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "punch_fail_limit", 3)
    codes = [(await check_in(client, emp, FAR)).status_code for _ in range(4)]
    assert codes == [422, 422, 422, 429]
    assert (await check_in(client, emp)).status_code == 429  # even a valid punch waits out the cool-down


async def test_success_resets_failure_counter(client, emp, clock, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "punch_fail_limit", 3)
    await check_in(client, emp, FAR)
    await check_in(client, emp, FAR)
    assert (await check_in(client, emp)).status_code == 201
    clock(ist(2026, 10, 21, 17, 0))
    r = await check_out(client, emp, FAR)  # geofence off for check-out: ok, counter was reset
    assert r.status_code == 200


# ------------------------------------------------------------------ M1: distance only for HR/admin
async def test_distance_only_exposed_to_hr_and_admin(client, emp, tokens_for, clock):
    await check_in(client, emp)
    assert (await client.get("/api/attendance/today", headers=emp)).json()["distance_m"] is None
    assert (await client.get("/api/attendance", headers=emp)).json()["items"][0]["distance_m"] is None
    mgr = bearer(await tokens_for("manager"))
    hr = bearer(await tokens_for("hr"))
    m = (await client.get("/api/attendance/team?date=2026-10-21", headers=mgr)).json()["items"][0]
    h = (await client.get("/api/attendance/team?date=2026-10-21", headers=hr)).json()["items"][0]
    assert m["distance_m"] is None and 95 <= h["distance_m"] <= 105


# ------------------------------------------------------------------ M2: selfie freshness + single use
async def test_selfie_must_be_fresh(client, emp, clock, db):
    old = await _file(db, "employee", age_s=601)
    r = await check_in(client, emp, {"selfie_file_id": str(old)})
    assert r.status_code == 422 and r.json()["error"]["code"] == "SELFIE_STALE"
    future = await _file(db, "employee", age_s=-120)
    assert (await check_in(client, emp, {"selfie_file_id": str(future)})).json()["error"]["code"] == "SELFIE_STALE"
    ok = await _file(db, "employee", age_s=599)
    assert (await check_in(client, emp, {"selfie_file_id": str(ok)})).status_code == 201


async def test_selfie_cannot_be_replayed_next_day_or_at_checkout(client, emp, clock, db):
    fid = await _file(db, "employee")
    assert (await check_in(client, emp, {"selfie_file_id": str(fid)})).status_code == 201
    clock(ist(2026, 10, 21, 9, 5))
    r = await check_out(client, emp, {"selfie_file_id": str(fid)})  # same file for the other punch
    assert r.status_code == 422 and r.json()["error"]["code"] == "SELFIE_REUSED"
    clock(ist(2026, 10, 22, 9, 0))  # next day: replay is refused (stale or already used)
    r = await check_in(client, emp, {"selfie_file_id": str(fid)})
    assert r.status_code == 422 and r.json()["error"]["code"] in ("SELFIE_REUSED", "SELFIE_STALE")
    clock(ist(2026, 10, 21, 9, 6))
    fresh = await _file(db, "employee", age_s=10)
    assert (await check_out(client, emp, {"selfie_file_id": str(fresh)})).status_code == 200


async def test_selfie_reuse_by_other_user_still_rejected(client, tokens_for, clock, db):
    fid = await _file(db, "employee")
    other = bearer(await tokens_for("manager"))
    r = await check_in(client, other, {"selfie_file_id": str(fid)})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"  # not the owner


# ------------------------------------------------------------------ M6: DST-safe lateness
async def _late(db, tz_name, day, start, local_h, local_m, fold=0, grace=10):
    from zoneinfo import ZoneInfo

    from app.models import Shift

    user = (await db.execute(select(User).where(User.employee_id == "EMP001"))).unique().scalar_one()
    shift = Shift(name="x", start_time=start, end_time=dt.time(23, 0), grace_minutes=grace)
    local = dt.datetime(day.year, day.month, day.day, local_h, local_m, tzinfo=ZoneInfo(tz_name), fold=fold)
    # go through UTC like the service does so fold/offset are exact
    local = local.astimezone(dt.UTC).astimezone(ZoneInfo(tz_name))
    return await att_svc.minutes_late_for(db, user, shift, day, local)


async def test_late_minutes_fall_back_day_repeated_hour(db, seeded):
    day = dt.date(2026, 11, 1)  # America/New_York ends DST: 01:00-02:00 happens twice
    assert await _late(db, "America/New_York", day, dt.time(0, 30), 1, 45, fold=0) == 75  # EDT
    assert await _late(db, "America/New_York", day, dt.time(0, 30), 1, 15, fold=1) == 105  # EST, 1 h later in real time
    assert await _late(db, "America/New_York", day, dt.time(0, 30), 0, 40, fold=0) == 0  # inside grace


async def test_late_minutes_spring_forward_day_missing_hour(db, seeded):
    day = dt.date(2026, 3, 8)  # America/New_York: 02:00 -> 03:00
    # start 02:30 does not exist; fold=0 reads it with the pre-transition offset (= 03:30 EDT). 03:45 EDT is 15 min after.
    assert await _late(db, "America/New_York", day, dt.time(2, 30), 3, 45) == 15
    # a shift after the gap: 09:30 start, 09:55 arrival -> 25 regardless of the skipped hour
    assert await _late(db, "America/New_York", day, dt.time(9, 30), 9, 55) == 25
    assert await _late(db, "Europe/London", dt.date(2026, 3, 29), dt.time(6, 0), 7, 30) == 90


# ------------------------------------------------------------------ M6: night shift
async def _night_shift(db):
    from app.models import Shift

    shift = (await db.execute(select(Shift))).scalars().first()
    shift.start_time, shift.end_time, shift.working_days = dt.time(22, 0), dt.time(6, 0), [1, 2, 3, 4, 5, 6, 7]
    await db.commit()


async def test_night_shift_checkout_after_midnight(client, emp, clock, db):
    await _night_shift(db)
    clock(ist(2026, 10, 21, 21, 55))
    assert (await check_in(client, emp)).json()["work_date"] == "2026-10-21"
    clock(ist(2026, 10, 22, 6, 5))  # next calendar day
    t = (await client.get("/api/attendance/today", headers=emp)).json()
    assert t["work_date"] == "2026-10-21" and t["check_out_at"] is None  # still the open night record
    r = await check_out(client, emp)
    assert r.status_code == 200 and r.json()["work_date"] == "2026-10-21" and r.json()["worked_minutes"] == 490


async def test_night_shift_not_auto_closed_until_window_passes(client, emp, clock, db):
    await _night_shift(db)
    clock(ist(2026, 10, 21, 21, 55))
    await check_in(client, emp)
    assert await att_svc.auto_close_open_records(db, ist(2026, 10, 22, 0, 30)) == 0  # shift still running
    assert await att_svc.auto_close_open_records(db, ist(2026, 10, 22, 10, 30)) == 1  # 06:00 end + 4 h buffer has passed


async def test_day_shift_checkout_next_day_is_not_checked_in(client, emp, clock):
    await check_in(client, emp)
    clock(ist(2026, 10, 22, 0, 10))
    r = await check_out(client, emp)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_CHECKED_IN"


# ------------------------------------------------------------------ M7: retention + deferred location columns
async def test_location_columns_not_loaded_on_list_queries(client, emp, clock, db, monkeypatch):
    await check_in(client, emp)
    calls = []
    from app.core import crypto

    real = crypto.decrypt_bytes
    monkeypatch.setattr(crypto, "decrypt_bytes", lambda t: calls.append(1) or real(t))
    assert (await client.get("/api/attendance", headers=emp)).status_code == 200
    mgr_calls = len(calls)
    assert mgr_calls == 0
    assert Attendance.check_in_loc.property.deferred is True and Attendance.check_out_loc.property.deferred is True


async def test_retention_purges_old_locations_and_selfies(db, seeded, clock, setting, tmp_path, monkeypatch):
    from app.core.config import get_settings

    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(root))
    (root / "ab").mkdir()
    blob = root / "ab" / "old.bin"
    blob.write_bytes(b"x")
    u = await uid(db, "employee")
    f_old = File(owner_id=u, purpose="selfie", storage_path="ab/old.bin", content_type="image/jpeg", size_bytes=1)
    f_new = File(owner_id=u, purpose="selfie", storage_path="ab/new.bin", content_type="image/jpeg", size_bytes=1)
    db.add_all([f_old, f_new])
    await db.flush()
    old = await _seed(db, "employee", dt.date(2026, 7, 1))
    new = await _seed(db, "manager", dt.date(2026, 10, 15))
    for rec, f in ((old, f_old), (new, f_new)):
        rec.check_in_loc = {"lat": 1.0, "lng": 2.0}
        rec.check_out_loc = {"lat": 1.0, "lng": 2.0}
        rec.check_in_selfie_file_id = f.id
    await db.commit()
    out = await att_svc.purge_pii(db)  # today = 2026-10-21, retention 90 d -> cutoff 2026-07-23
    assert out == {"locations": 1, "selfies": 1}
    await db.refresh(old)
    await db.refresh(new, ["check_in_loc", "check_in_selfie_file_id"])
    assert old.check_in_selfie_file_id is None and not blob.exists() and await db.get(File, f_old.id) is None
    assert new.check_in_loc == {"lat": 1.0, "lng": 2.0} and new.check_in_selfie_file_id == f_new.id
    raw = (
        await db.execute(text("SELECT check_in_loc_enc, check_out_loc_enc FROM attendance WHERE work_date='2026-07-01'"))
    ).one()
    assert raw == (None, None)
    assert len(await _audit_rows(db, "attendance.retention_purge")) == 1
    assert await att_svc.purge_pii(db) == {"locations": 0, "selfies": 0}  # idempotent


async def test_retention_disabled_with_zero(db, seeded, clock, setting):
    await setting("attendance.location_retention_days", 0)
    await setting("attendance.selfie_retention_days", 0)
    rec = await _seed(db, "employee", dt.date(2025, 1, 6))
    rec.check_in_loc = {"lat": 1.0, "lng": 2.0}
    await db.commit()
    assert await att_svc.purge_pii(db) == {"locations": 0, "selfies": 0}


# ------------------------------------------------------------------ L8: holidays
async def test_holiday_writes_need_manage_permission_and_are_audited(client, emp, tokens_for, db):
    body = {"date": "2026-12-25", "name": "Christmas"}
    mgr = bearer(await tokens_for("manager"))
    assert (await client.post("/api/holidays", json=body, headers=emp)).status_code == 403
    assert (await client.post("/api/holidays", json=body, headers=mgr)).status_code == 403
    hr = bearer(await tokens_for("hr"))
    r = await client.post("/api/holidays", json=body, headers=hr)
    assert r.status_code == 201 and r.json()["name"] == "Christmas"
    hid = r.json()["id"]
    assert (await client.post("/api/holidays", json=body, headers=hr)).status_code == 409  # one per date
    assert (await client.patch(f"/api/holidays/{hid}", json={"name": "Xmas"}, headers=emp)).status_code == 403
    assert (await client.patch(f"/api/holidays/{hid}", json={"name": "Xmas"}, headers=hr)).json()["name"] == "Xmas"
    assert (await client.get("/api/holidays?year=2026", headers=emp)).json()["items"][0]["name"] == "Xmas"
    assert (await client.delete(f"/api/holidays/{hid}", headers=emp)).status_code == 403
    assert (await client.delete(f"/api/holidays/{hid}", headers=hr)).status_code == 204
    assert (await client.delete(f"/api/holidays/{hid}", headers=hr)).status_code == 404
    actions = [r.action for r in await _audit_rows(db, "holiday.create")] + [
        r.action for r in await _audit_rows(db, "holiday.update")
    ]
    assert actions == ["holiday.create", "holiday.update"] and len(await _audit_rows(db, "holiday.delete")) == 1


# ------------------------------------------------------------------ L9: range cap
async def test_team_range_is_capped(client, tokens_for, clock):
    hr = bearer(await tokens_for("hr"))
    ok = await client.get("/api/attendance/team?from=2025-10-21&to=2026-10-21", headers=hr)  # 366 days inclusive
    assert ok.status_code == 200
    too_long = await client.get("/api/attendance/team?from=2025-10-20&to=2026-10-21", headers=hr)
    assert too_long.status_code == 422 and too_long.json()["error"]["code"] == "INVALID_DATE_RANGE"
    assert (await client.get("/api/late/team?from=2020-01-01&to=2026-10-21", headers=hr)).status_code == 422


async def test_team_without_bounds_defaults_to_last_year(client, tokens_for, clock, db):
    await _seed(db, "employee", dt.date(2026, 10, 20))
    await _seed(db, "employee", dt.date(2024, 1, 8))  # older than the default window
    hr = bearer(await tokens_for("hr"))
    assert (await client.get("/api/attendance/team", headers=hr)).json()["total"] == 1
    assert (await client.get("/api/attendance/team?date=2024-01-08", headers=hr)).json()["total"] == 1  # explicit day is fine


def test_cap_range_unit():
    d = dt.date(2026, 10, 21)
    assert att_svc.cap_range(None, None, d) == (d - dt.timedelta(days=365), d)
    assert att_svc.cap_range(dt.date(2026, 1, 1), None, d)[1] == dt.date(2026, 1, 1) + dt.timedelta(days=365)
    assert att_svc.cap_range(None, dt.date(2026, 1, 1), d)[0] == dt.date(2026, 1, 1) - dt.timedelta(days=365)


def test_timezone_setting_validation():
    att_svc.validate_office_setting("office.timezone", "Europe/Berlin")
    with pytest.raises(att_svc.AppError):
        att_svc.validate_office_setting("office.timezone", "Mars/Olympus")
