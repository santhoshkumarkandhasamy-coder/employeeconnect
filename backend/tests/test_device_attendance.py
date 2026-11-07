"""From punches to attendance (plan 18 section 5): derive_day matrix, phone and manual integration, use-device, mapping changes.
2026-10-21 is a Wednesday (today), office tz Asia/Kolkata, shift 09:30-18:30 with 10 minutes grace."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from app.models import (
    Attendance,
    AuditLog,
    ClientLocation,
    DevicePunchOutcome,
    DeviceUser,
    Holiday,
    LateRecord,
    LeaveRequest,
    LeaveType,
    OnDutyRequest,
    PayrollRun,
    PermissionRequest,
    Setting,
    Shift,
    User,
)
from app.services import device_attendance as da
from app.services import device_sync
from tests.conftest import bearer
from tests.device_helpers import NOW, TODAY, add_device, add_device_user, add_punch, local, to_utc

pytestmark = pytest.mark.usefixtures("seeded", "clk")
OFFICE = {"lat": 12.9716, "lng": 77.5946, "accuracy_m": 10}


@pytest.fixture
async def dev(db):
    return await add_device(db, None, ip="192.168.0.201", ingest_from=TODAY - dt.timedelta(days=60))


@pytest.fixture
async def mapped(db, dev):
    await add_device_user(db, dev, "EMP001", user="employee")
    return dev


@pytest.fixture
async def H(tokens_for):
    cache: dict = {}

    async def _h(role):
        if role not in cache:
            cache[role] = bearer(await tokens_for(role))
        return cache[role]

    return _h


async def derive(db, seeded, day=TODAY, **kw):
    res = await da.derive_day(db, seeded["users"]["employee"], day, **kw)
    await db.commit()
    return res


async def rec_of(sm, seeded, day=TODAY, user="employee"):
    async with sm() as s:
        return (
            await s.execute(select(Attendance).where(Attendance.user_id == seeded["users"][user], Attendance.work_date == day))
        ).scalar_one_or_none()


async def outcomes(sm, day=TODAY):
    async with sm() as s:
        rows = (await s.execute(select(DevicePunchOutcome).order_by(DevicePunchOutcome.punch_id))).scalars().all()
        return [(o.outcome, o.reason) for o in rows if o.work_date == day or o.work_date is None]


def at(rec_dt):
    return rec_dt.replace(tzinfo=dt.UTC)


# ---------------------------------------------------------------- device only
async def test_device_only_in_and_out(db, mapped, seeded, sessionmaker_):
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:58"))
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:59"))  # inside the 5 minute gap: duplicate
    await add_punch(db, mapped, "EMP001", local(TODAY, "09:10"))
    res = await derive(db, seeded)
    assert res.action == "created"
    a = await rec_of(sessionmaker_, seeded)
    assert (a.check_in_source, a.check_out_source, a.check_in_device_id, a.check_out_device_id) == (
        "device",
        "device",
        mapped.id,
        mapped.id,
    )
    assert (a.status, a.minutes_late, a.worked_minutes) == ("half_day", 0, 12)  # below the half-day threshold
    assert at(a.check_in_at) == to_utc(local(TODAY, "08:58")) and at(a.check_out_at) == to_utc(local(TODAY, "09:10"))
    assert a.check_in_within_fence is True and a.check_in_distance_m is None and a.app_check_in_at is None
    assert sorted(await outcomes(sessionmaker_), key=str) == [
        ("check_in", None),
        ("check_out", None),
        ("not_applied", "duplicate"),
    ]


async def test_full_day_on_time_and_late_variants(db, mapped, seeded, sessionmaker_):
    yesterday = TODAY - dt.timedelta(days=1)
    await add_punch(db, mapped, "EMP001", local(yesterday, "09:00"))
    await add_punch(db, mapped, "EMP001", local(yesterday, "12:00"))
    await add_punch(db, mapped, "EMP001", local(yesterday, "18:35"))
    await derive(db, seeded, yesterday)
    a = await rec_of(sessionmaker_, seeded, yesterday)
    assert (a.status, a.minutes_late, a.worked_minutes) == ("present", 0, 575)
    assert at(a.check_out_at) == to_utc(local(yesterday, "18:35"))
    assert sorted(o for o, _ in await outcomes(sessionmaker_, yesterday)) == ["check_in", "check_out", "extra"]
    d2 = TODAY - dt.timedelta(days=2)
    for hm, late in (("09:40", 0), ("09:41", 11)):
        await add_punch(db, mapped, "EMP001", local(d2, hm))
        await add_punch(db, mapped, "EMP001", local(d2, "18:30"))
        await derive(db, seeded, d2)
        a = await rec_of(sessionmaker_, seeded, d2)
        assert a.minutes_late == late, hm
        async with sessionmaker_() as s:
            lr = (await s.execute(select(LateRecord).where(LateRecord.attendance_id == a.id))).scalar_one_or_none()
            assert (lr is not None) == bool(late)
        # reset the day for the next variant
        async with sessionmaker_() as s:
            from sqlalchemy import delete as sa_delete

            await s.execute(sa_delete(LateRecord))
            await s.execute(sa_delete(DevicePunchOutcome))
            await s.execute(sa_delete(Attendance))
            from app.models import DevicePunch

            await s.execute(sa_delete(DevicePunch).where(DevicePunch.punch_at >= to_utc(local(d2, "00:00"))))
            await s.commit()


async def test_late_device_check_in_builds_the_late_record(db, mapped, seeded, sessionmaker_):
    await add_punch(db, mapped, "EMP001", local(TODAY, "09:50"))
    await derive(db, seeded)
    a = await rec_of(sessionmaker_, seeded)
    assert (a.status, a.minutes_late) == ("late", 20)
    async with sessionmaker_() as s:
        lr = (await s.execute(select(LateRecord))).scalar_one()
        assert (lr.minutes_late, lr.grace_minutes, at(lr.check_in_at)) == (20, 10, to_utc(local(TODAY, "09:50")))


async def test_derive_is_idempotent(db, mapped, seeded, sessionmaker_):
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:58"))
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:59", 59))
    first = await derive(db, seeded)
    snap = await rec_of(sessionmaker_, seeded)
    again = await derive(db, seeded)
    assert first.action == "created" and again.action == "unchanged"
    snap2 = await rec_of(sessionmaker_, seeded)
    assert (snap.id, snap.check_in_at, snap.check_out_at, snap.status) == (
        snap2.id,
        snap2.check_in_at,
        snap2.check_out_at,
        snap2.status,
    )
    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(Attendance)) == 1


# ---------------------------------------------------------------- phone + device
async def _app_row(db, seeded, shift_id, in_hm, out_hm=None, day=TODAY):
    a = Attendance(
        user_id=seeded["users"]["employee"], work_date=day, shift_id=shift_id, check_in_at=to_utc(local(day, in_hm)),
        app_check_in_at=to_utc(local(day, in_hm)), check_in_within_fence=True, check_in_distance_m=12, check_in_source="app",
        check_out_at=to_utc(local(day, out_hm)) if out_hm else None, app_check_out_at=to_utc(local(day, out_hm)) if out_hm else None,
        check_out_source="app" if out_hm else None, status="late", minutes_late=15,
    )  # fmt: skip
    db.add(a)
    await db.commit()
    return a


async def test_app_only_row_is_left_alone(db, mapped, seeded, sessionmaker_):
    await _app_row(db, seeded, seeded["shift"], "09:45", "18:30")
    res = await derive(db, seeded)
    assert res.action in ("unchanged", "updated")
    a = await rec_of(sessionmaker_, seeded)
    assert (a.check_in_source, a.check_out_source) == ("app", "app") and at(a.check_in_at) == to_utc(local(TODAY, "09:45"))
    assert a.minutes_late == 15 and a.status == "late"


async def test_earlier_device_check_in_beats_a_late_phone_check_in(db, mapped, seeded, sessionmaker_):
    await _app_row(db, seeded, seeded["shift"], "09:45")
    async with sessionmaker_() as s:
        s.add(LateRecord(user_id=seeded["users"]["employee"], attendance_id=(await rec_of(sessionmaker_, seeded)).id, work_date=TODAY,
                         shift_start=dt.time(9, 30), grace_minutes=10, check_in_at=to_utc(local(TODAY, "09:45")), minutes_late=15))  # fmt: skip
        await s.commit()
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:55"))
    await derive(db, seeded)
    a = await rec_of(sessionmaker_, seeded)
    assert (a.check_in_source, a.minutes_late, a.status) == ("device", 0, "present")
    assert at(a.app_check_in_at) == to_utc(local(TODAY, "09:45"))  # the phone time is kept as evidence
    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(LateRecord)) == 0
    # the mapping goes away: the row falls back to the phone punch
    async with sessionmaker_() as s:
        du = (await s.execute(select(DeviceUser))).scalar_one()
        du.user_id, du.match_method = None, "ignored"
        await s.commit()
    await da.recompute_device_users(db, [(mapped.id, "EMP001")])
    a = await rec_of(sessionmaker_, seeded)
    assert (a.check_in_source, a.minutes_late) == ("app", 15) and at(a.check_in_at) == to_utc(local(TODAY, "09:45"))
    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(LateRecord)) == 1


async def test_tie_keeps_the_phone_as_source_and_close_device_punch_is_a_duplicate(db, mapped, seeded, sessionmaker_):
    await _app_row(db, seeded, seeded["shift"], "09:00")
    await add_punch(db, mapped, "EMP001", local(TODAY, "09:00"))
    await add_punch(db, mapped, "EMP001", local(TODAY, "09:03"))
    await derive(db, seeded)
    a = await rec_of(sessionmaker_, seeded)
    assert (a.check_in_source, a.check_out_at) == ("app", None)
    assert [x for x in await outcomes(sessionmaker_)] == [("not_applied", "duplicate")] * 2


async def test_later_of_phone_and_device_check_out_wins(db, mapped, seeded, sessionmaker_):
    await _app_row(db, seeded, seeded["shift"], "09:00", "18:40")
    await add_punch(db, mapped, "EMP001", local(TODAY, "18:35"))
    await derive(db, seeded)
    a = await rec_of(sessionmaker_, seeded)
    assert a.check_out_source == "app" and at(a.check_out_at) == to_utc(local(TODAY, "18:40"))
    assert await outcomes(sessionmaker_) == [("extra", None)]
    await add_punch(db, mapped, "EMP001", local(TODAY, "19:10"))
    await derive(db, seeded)
    a = await rec_of(sessionmaker_, seeded)
    assert (a.check_out_source, a.check_out_device_id) == ("device", mapped.id) and at(a.app_check_out_at) == to_utc(
        local(TODAY, "18:40")
    )


async def test_single_punch_auto_closed_then_late_check_out(db, mapped, seeded, sessionmaker_):
    y = TODAY - dt.timedelta(days=1)
    await add_punch(db, mapped, "EMP001", local(y, "09:00"))
    await derive(db, seeded, y)
    async with sessionmaker_() as s:
        a = (await s.execute(select(Attendance))).scalar_one()
        a.auto_closed = True
        await s.commit()
    assert (await rec_of(sessionmaker_, seeded, y)).check_out_at is None
    await add_punch(db, mapped, "EMP001", local(y, "19:00"))
    await derive(db, seeded, y)
    a = await rec_of(sessionmaker_, seeded, y)
    assert a.check_out_source == "device" and a.auto_closed is False and a.worked_minutes == 600


async def test_night_shift_punches_across_midnight_belong_to_the_start_date(db, mapped, seeded, sessionmaker_):
    async with sessionmaker_() as s:
        night = Shift(name="Night", start_time=dt.time(22, 0), end_time=dt.time(6, 0), grace_minutes=10)
        s.add(night)
        await s.flush()
        u = await s.get(User, seeded["users"]["employee"])
        u.shift_id = night.id
        await s.commit()
    d = TODAY - dt.timedelta(days=1)
    await add_punch(db, mapped, "EMP001", local(d, "21:55"))
    await add_punch(db, mapped, "EMP001", local(TODAY, "06:10"))
    await db.commit()
    await derive(db, seeded, d)
    await derive(db, seeded, TODAY)
    a = await rec_of(sessionmaker_, seeded, d)
    assert (a.minutes_late, a.worked_minutes) == (0, 495) and at(a.check_out_at) == to_utc(local(TODAY, "06:10"))
    assert await rec_of(sessionmaker_, seeded, TODAY) is None
    assert da.punch_work_date(to_utc(local(TODAY, "10:01")), None, __import__("zoneinfo").ZoneInfo("Asia/Kolkata")) == TODAY


async def test_weekly_off_and_holiday_are_never_late(db, mapped, seeded, sessionmaker_):
    sat = dt.date(2026, 10, 17)
    mon = dt.date(2026, 10, 19)
    async with sessionmaker_() as s:
        s.add(Holiday(date=mon, name="Festival"))
        await s.commit()
    for d in (sat, mon):
        await add_punch(db, mapped, "EMP001", local(d, "11:00"))
        await derive(db, seeded, d)
        a = await rec_of(sessionmaker_, seeded, d)
        assert (a.minutes_late, a.status) == (0, "present"), d


async def test_permission_and_first_half_leave_move_the_late_limit(db, mapped, seeded, sessionmaker_):
    emp = seeded["users"]["employee"]
    d1, d2 = TODAY - dt.timedelta(days=1), TODAY - dt.timedelta(days=2)
    async with sessionmaker_() as s:
        lt = LeaveType(code="CL", name="Casual", annual_quota=12)
        s.add(lt)
        await s.flush()
        s.add(
            PermissionRequest(
                user_id=emp, request_date=d1, from_time=dt.time(9, 0), to_time=dt.time(10, 30), minutes=90, status="approved"
            )
        )
        s.add(LeaveRequest(user_id=emp, leave_type_id=lt.id, from_date=d2, to_date=d2, half_day=True, half_day_session="first", days=0.5, status="approved"))  # fmt: skip
        await s.commit()
    await add_punch(db, mapped, "EMP001", local(d1, "10:35"))  # inside 10:30 + 10 min grace
    await derive(db, seeded, d1)
    assert (await rec_of(sessionmaker_, seeded, d1)).minutes_late == 0
    await add_punch(db, mapped, "EMP001", local(d2, "11:30"))  # morning is on leave
    await derive(db, seeded, d2)
    assert (await rec_of(sessionmaker_, seeded, d2)).minutes_late == 0


# ---------------------------------------------------------------- not applied
async def test_full_day_leave_blocks_the_punch(db, mapped, seeded, sessionmaker_):
    async with sessionmaker_() as s:
        lt = LeaveType(code="CL", name="Casual", annual_quota=12)
        s.add(lt)
        await s.flush()
        s.add(
            LeaveRequest(
                user_id=seeded["users"]["employee"],
                leave_type_id=lt.id,
                from_date=TODAY,
                to_date=TODAY,
                days=1,
                status="approved",
            )
        )
        await s.commit()
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:55"))
    res = await derive(db, seeded)
    assert (res.action, res.reason) == ("not_applied", "on_leave") and await rec_of(sessionmaker_, seeded) is None
    assert await outcomes(sessionmaker_) == [("not_applied", "on_leave")]


@pytest.mark.parametrize("apply_on_duty", [False, True])
async def test_on_duty_day_depends_on_the_setting(db, mapped, seeded, sessionmaker_, apply_on_duty):
    async with sessionmaker_() as s:
        site = ClientLocation(name="Acme", lat=13.0, lng=80.0)
        s.add(site)
        await s.flush()
        s.add(
            OnDutyRequest(
                user_id=seeded["users"]["employee"],
                client_location_id=site.id,
                from_date=TODAY,
                to_date=TODAY,
                days=1,
                status="approved",
            )
        )
        if apply_on_duty:
            s.add(Setting(key="device.apply_on_on_duty_days", value=True))
        await s.commit()
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:55"))
    res = await derive(db, seeded)
    if apply_on_duty:
        assert res.action == "created" and (await rec_of(sessionmaker_, seeded)) is not None
    else:
        assert (res.action, res.reason) == ("not_applied", "on_duty") and await rec_of(sessionmaker_, seeded) is None


async def test_outside_window_inactive_user_no_shift_payroll_locked(db, mapped, seeded, sessionmaker_):
    old = TODAY - dt.timedelta(days=10)
    await add_punch(db, mapped, "EMP001", local(old, "09:00"))
    res = await derive(db, seeded, old)
    assert (res.action, res.reason) == ("not_applied", "outside_window") and await rec_of(sessionmaker_, seeded, old) is None
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:55"))
    async with sessionmaker_() as s:
        s.add(PayrollRun(period=dt.date(2026, 10, 1), status="processed", attendance_cutoff=dt.date(2026, 10, 31), config={}))
        await s.commit()
    assert (await derive(db, seeded)).reason == "payroll_locked"
    async with sessionmaker_() as s:
        (await s.execute(select(PayrollRun))).scalar_one().status = "draft"
        u = await s.get(User, seeded["users"]["employee"])
        u.status = "inactive"
        await s.commit()
    assert (await derive(db, seeded)).reason == "inactive_user"
    async with sessionmaker_() as s:
        u = await s.get(User, seeded["users"]["employee"])
        u.status, u.shift_id = "active", None
        await s.commit()
    assert (await derive(db, seeded)).reason == "no_shift"
    assert await rec_of(sessionmaker_, seeded) is None


async def test_quarantined_punches_never_count(db, mapped, seeded, sessionmaker_):
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:55"), quarantine="future")
    res = await derive(db, seeded)
    assert res.action == "none" and await rec_of(sessionmaker_, seeded) is None
    assert await outcomes(sessionmaker_) == [("quarantined", "future")]


# ---------------------------------------------------------------- concurrency
async def test_concurrent_phone_check_in_is_retried_once(db, mapped, seeded, sessionmaker_, monkeypatch):
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:55"))
    await db.commit()
    real = da._day_punches
    fired = {"n": 0}

    async def racing(session, *a, **k):
        if not fired["n"]:  # the phone wins the race after derive looked for the row
            fired["n"] += 1
            session.add(Attendance(user_id=seeded["users"]["employee"], work_date=TODAY, shift_id=seeded["shift"],
                                   check_in_at=to_utc(local(TODAY, "08:57")), app_check_in_at=to_utc(local(TODAY, "08:57")), check_in_source="app"))  # fmt: skip
            await session.flush()
        return await real(session, *a, **k)

    monkeypatch.setattr(da, "_day_punches", racing)
    res = await derive(db, seeded)
    a = await rec_of(sessionmaker_, seeded)
    assert (
        res.action in ("updated", "unchanged")
        and a.check_in_source == "device"
        and at(a.app_check_in_at) == to_utc(local(TODAY, "08:57"))
    )
    async with sessionmaker_() as s:
        assert await s.scalar(select(func.count()).select_from(Attendance)) == 1


# ---------------------------------------------------------------- phone flow (writes sources and app times)
def _punch_body(clk):
    return {**OFFICE, "fix_time": clk.now.isoformat()}


async def test_phone_check_in_and_out_record_source_and_app_times(client, H, seeded, sessionmaker_, clk):
    r = await client.post("/api/attendance/check-in", headers=await H("employee"), json=_punch_body(clk))
    assert r.status_code == 201, r.text
    assert (r.json()["check_in_source"], r.json()["check_out_source"], r.json()["check_in_device"]) == ("app", None, None)
    a = await rec_of(sessionmaker_, seeded)
    assert a.check_in_source == "app" and at(a.app_check_in_at) == NOW and a.app_check_out_at is None
    clk.set(NOW + dt.timedelta(hours=9))
    r = await client.post("/api/attendance/check-out", headers=await H("employee"), json=_punch_body(clk))
    assert r.status_code == 200 and r.json()["check_out_source"] == "app"
    a = await rec_of(sessionmaker_, seeded)
    assert (a.check_out_source, at(a.app_check_out_at)) == ("app", NOW + dt.timedelta(hours=9)) and a.check_out_device_id is None


async def test_phone_check_in_on_a_device_day_is_refused_but_check_out_works(client, H, db, mapped, seeded, sessionmaker_, clk):
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:50"))
    await derive(db, seeded)
    r = await client.post("/api/attendance/check-in", headers=await H("employee"), json=_punch_body(clk))
    assert (r.status_code, r.json()["error"]["code"]) == (409, "ALREADY_CHECKED_IN")
    clk.set(NOW + dt.timedelta(hours=9))  # 18:00 IST
    r = await client.post("/api/attendance/check-out", headers=await H("employee"), json=_punch_body(clk))
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["check_in_source"], body["check_out_source"], body["within_fence"], body["distance_m"], body["selfie_url"]) == (
        "device",
        "app",
        True,
        None,
        None,
    )
    assert body["check_in_device"] == {"id": str(mapped.id), "name": mapped.name}
    a = await rec_of(sessionmaker_, seeded)
    assert a.app_check_in_at is None and at(a.app_check_out_at) == clk.now
    await add_punch(db, mapped, "EMP001", local(TODAY, "18:20"))  # a later device punch can still move the check-out later
    await derive(db, seeded)
    a = await rec_of(sessionmaker_, seeded)
    assert a.check_out_source == "device" and at(a.check_out_at) == to_utc(local(TODAY, "18:20"))


# ---------------------------------------------------------------- manual attendance
async def test_manual_entry_wins_then_use_device_takes_over(client, H, db, mapped, seeded, sessionmaker_):
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:50"))
    await add_punch(db, mapped, "EMP001", local(TODAY, "18:20"))
    await db.commit()
    emp = seeded["users"]["employee"]
    r = await client.put(
        f"/api/admin/attendance/{emp}/{TODAY}",
        headers=await H("hr"),
        json={"check_in": "08:00", "check_out": "08:40", "reason": "Forgot to punch"},
    )
    assert r.status_code == 200, r.text
    rec = r.json()["record"]
    assert (rec["manual"], rec["check_in_source"], rec["check_out_source"], rec["check_in_device"]) == (
        True,
        "manual",
        "manual",
        None,
    )
    a = await rec_of(sessionmaker_, seeded)
    assert (a.check_in_source, a.check_out_source, a.app_check_in_at) == ("manual", "manual", None)
    await derive(db, seeded)  # the nightly recompute changes nothing on a manual day
    a = await rec_of(sessionmaker_, seeded)
    assert at(a.check_in_at) == to_utc(local(TODAY, "08:00"))
    assert {o for o, _ in await outcomes(sessionmaker_)} == {"not_applied"} and {
        r_ for _, r_ in await outcomes(sessionmaker_)
    } == {"manual_entry"}
    got = await client.get(f"/api/admin/attendance/{emp}/{TODAY}", headers=await H("hr"))
    assert [(p["device_name"], p["outcome"]) for p in got.json()["device_punches"]] == [(mapped.name, "not_applied")] * 2
    assert (
        await client.delete(f"/api/admin/attendance/{emp}/{TODAY}", headers=await H("hr"))
    ).status_code == 409  # device punches exist
    r = await client.post(
        f"/api/admin/attendance/{emp}/{TODAY}/use-device", headers=await H("hr"), json={"reason": "Machine is right"}
    )
    assert r.status_code == 200, r.text
    rec = r.json()["record"]
    assert (rec["manual"], rec["check_in_source"], rec["check_out_source"]) == (False, "device", "device")
    assert rec["check_in_at"] == "2026-10-21T03:20:00Z" and rec["check_out_at"] == "2026-10-21T12:50:00Z"
    assert sorted(p["outcome"] for p in r.json()["device_punches"]) == ["check_in", "check_out"]
    async with sessionmaker_() as s:
        ev = (await s.execute(select(AuditLog).where(AuditLog.action == "attendance.device_use"))).scalar_one()
        assert ev.after["reason_length"] == 16 and "reason" not in ev.after


async def test_use_device_rules(client, H, db, mapped, seeded, sessionmaker_):
    emp = seeded["users"]["employee"]
    url = f"/api/admin/attendance/{emp}/{TODAY}/use-device"
    hr = await H("hr")
    assert (await client.post(url, headers=hr, json={"reason": "no punches here"})).status_code == 409
    me = seeded["users"]["hr"]
    assert (
        await client.post(f"/api/admin/attendance/{me}/{TODAY}/use-device", headers=hr, json={"reason": "my own day"})
    ).status_code == 403
    assert (await client.post(url, headers=await H("employee"), json={"reason": "I want it"})).status_code == 403
    assert (await client.post(url, headers=hr, json={"reason": "x"})).status_code == 422
    assert (await client.post(url, headers=hr, json={"reason": "okay okay", "extra": 1})).status_code == 422
    # a payroll-locked month refuses and leaves the manual entry untouched
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:50"))
    await db.commit()
    await client.put(f"/api/admin/attendance/{emp}/{TODAY}", headers=hr, json={"check_in": "08:00", "reason": "Entered by HR"})
    async with sessionmaker_() as s:
        s.add(
            PayrollRun(
                period=dt.date(2026, 10, 1),
                status="published",
                attendance_cutoff=dt.date(2026, 10, 31),
                config={},
                published_at=NOW,
                published_by=me,
            )
        )
        await s.commit()
    r = await client.post(url, headers=hr, json={"reason": "Machine is right"})
    assert r.status_code == 409
    assert (await rec_of(sessionmaker_, seeded)).manual is True


# ---------------------------------------------------------------- mapping changes
async def test_ignoring_a_device_user_unapplies_a_device_only_day(client, H, db, mapped, seeded, sessionmaker_, redis):
    await add_punch(db, mapped, "EMP001", local(TODAY, "08:50"))
    await db.commit()
    await derive(db, seeded)
    assert await rec_of(sessionmaker_, seeded) is not None
    async with sessionmaker_() as s:
        du_id = (await s.execute(select(DeviceUser.id))).scalar_one()
    r = await client.put(  # ignoring a MATCHED user needs device.manage (review M3): HR is refused, the admin may
        f"/api/admin/device-users/{du_id}/mapping", headers=await H("hr"), json={"user_id": None, "reason": "left the company"}
    )
    assert r.status_code == 403
    r = await client.put(
        f"/api/admin/device-users/{du_id}/mapping", headers=await H("admin"), json={"user_id": None, "reason": "left the company"}
    )
    assert r.status_code == 200 and r.json()["recompute_queued"] is True
    assert r.json()["items"][0]["status"] == "ignored"
    await device_sync.process_queue(sessionmaker_, redis)
    assert await rec_of(sessionmaker_, seeded) is None
    assert await outcomes(sessionmaker_) == [("ignored", "mapping_ignored")]
    async with sessionmaker_() as s:
        assert "attendance.device_unapply" in (await s.execute(select(AuditLog.action))).scalars().all()
    # mapping it back to the employee brings the day back
    r = await client.put(
        f"/api/admin/device-users/{du_id}/mapping", headers=await H("hr"),
        json={"user_id": str(seeded["users"]["employee"]), "reason": "badge re-issued"},
    )
    assert r.json()["items"][0]["match_method"] == "manual"
    await device_sync.process_queue(sessionmaker_, redis)
    assert (await rec_of(sessionmaker_, seeded)).check_in_source == "device"


async def test_reset_to_auto_matches_by_employee_id_again(client, H, db, mapped, seeded, sessionmaker_, redis):
    async with sessionmaker_() as s:
        du = (await s.execute(select(DeviceUser))).scalar_one()
        du.user_id, du.match_method = None, "ignored"
        await s.commit()
        du_id = du.id
    r = await client.post(f"/api/admin/device-users/{du_id}/auto", headers=await H("hr"))
    assert r.status_code == 200 and r.json()["items"][0]["match_method"] == "auto"
    async with sessionmaker_() as s:
        assert {a for a in (await s.execute(select(AuditLog.action))).scalars()} >= {"device_user.auto"}
