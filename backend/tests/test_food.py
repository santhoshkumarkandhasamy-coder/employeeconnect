"""Food enrollment (plan 05). Clock: 2026-10-21 (Wednesday) 10:00 IST = 04:30Z. Office tz Asia/Kolkata (+05:30, no DST).
Default cut-off: previous day 18:00 => tomorrow (Thu 22nd) closes at 2026-10-21 12:30Z.
SQLite: upsert uses the sqlite dialect; true multi-connection races are not exercised (StaticPool = one connection)."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.core.errors import AppError
from app.models import AuditLog, Department, FoodEnrollment, Holiday, LeaveRequest, LeaveType, Setting, User
from app.services import food as fs
from app.services.xlsx import sanitize_cell
from tests.conftest import USERS, bearer

NOW = dt.datetime(2026, 10, 21, 4, 30, tzinfo=dt.UTC)
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def clock(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr("app.services.food.utcnow", lambda: state["now"])
    return state


@pytest.fixture
async def H(tokens_for):
    cache: dict = {}

    async def _h(role):
        if role not in cache:
            cache[role] = bearer(await tokens_for(role))
        return cache[role]

    return _h


async def put(client, h, date, need=True):
    return await client.put("/api/food/enrollment", headers=h, json={"date": date, "need_food": need})


async def set_setting(db, key, value):
    row = await db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    await db.commit()


# ---------------------------------------------------------------- pure calculator
def cfg(mode="previous_day", t=dt.time(18, 0), tz="Asia/Kolkata", adv=14):
    return fs.FoodConfig(mode=mode, cutoff_time=t, max_advance_days=adv, tz=ZoneInfo(tz))


def test_cutoff_boundary_inclusive_exclusive():
    c = cfg()
    day = dt.date(2026, 10, 22)
    cutoff = fs.compute_cutoff(day, c.mode, c.cutoff_time, c.tz)
    assert cutoff == dt.datetime(2026, 10, 21, 12, 30, tzinfo=dt.UTC)
    assert fs.can_change(day, cutoff - dt.timedelta(seconds=1), c)
    assert not fs.can_change(day, cutoff, c)  # exactly at the boundary: locked
    assert not fs.can_change(day, cutoff + dt.timedelta(seconds=1), c)


def test_same_day_mode_and_past_and_horizon():
    c = cfg("same_day", dt.time(9, 0))
    today = dt.date(2026, 10, 21)  # 09:00 IST = 03:30Z
    assert fs.compute_cutoff(today, c.mode, c.cutoff_time, c.tz) == dt.datetime(2026, 10, 21, 3, 30, tzinfo=dt.UTC)
    assert not fs.can_change(today, NOW, c)  # 10:00 IST is after 09:00
    assert not fs.can_change(dt.date(2026, 10, 20), NOW, c)  # past
    assert not fs.can_change(dt.date(2026, 11, 5), NOW, c)  # beyond 14 days (15 ahead)
    assert fs.can_change(dt.date(2026, 11, 4), NOW, c)  # exactly 14 ahead


def test_cutoff_is_dst_safe_in_new_york():
    c = cfg("previous_day", dt.time(18, 0), "America/New_York")
    # Meal on Sun 2026-11-01 (DST ends 02:00 that day): cut-off Sat 31 Oct 18:00 EDT (UTC-4) = 22:00Z
    assert fs.compute_cutoff(dt.date(2026, 11, 1), c.mode, c.cutoff_time, c.tz) == dt.datetime(2026, 10, 31, 22, 0, tzinfo=dt.UTC)
    # Meal Mon 2 Nov: cut-off Sun 1 Nov 18:00 EST (UTC-5) = 23:00Z (wall clock preserved, not shifted by an hour)
    assert fs.compute_cutoff(dt.date(2026, 11, 2), c.mode, c.cutoff_time, c.tz) == dt.datetime(2026, 11, 1, 23, 0, tzinfo=dt.UTC)
    # "today" follows the office tz, not UTC: 2026-11-01 03:30Z is still Oct 31 in New York
    assert fs.office_today(dt.datetime(2026, 11, 1, 3, 30, tzinfo=dt.UTC), c.tz) == dt.date(2026, 10, 31)


def test_parse_and_validate_settings():
    assert fs.parse_hhmm("18:00") == dt.time(18, 0)
    for bad in ("24:00", "6pm", "18:60", None, 1800):
        assert fs.parse_hhmm(bad) is None
    with pytest.raises(AppError):
        fs.validate_food_setting("food.cutoff_mode", "weekly")
    with pytest.raises(AppError):
        fs.validate_food_setting("food.max_advance_days", True)


# ---------------------------------------------------------------- employee API
async def test_put_future_date_and_unset_default(client, H, clock, db):
    h = await H("employee")
    r = await client.get("/api/food/my-enrollments", headers=h, params={"from": "2026-10-22", "to": "2026-10-23"})
    assert r.status_code == 200
    assert [i["need_food"] for i in r.json()["items"]] == [None, None]  # unanswered, not coerced to false

    r = await put(client, h, "2026-10-23", True)  # Friday
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"date": "2026-10-23", "need_food": True, "can_change": True, "cutoff_at": "2026-10-22T12:30:00Z"}
    assert "user_id" not in body
    r = await put(client, h, "2026-10-23", False)  # toggle
    assert r.json()["need_food"] is False
    assert (await db.execute(select(func.count()).select_from(FoodEnrollment))).scalar_one() == 1


async def test_cutoff_enforced_at_boundary(client, H, clock):
    h = await H("employee")
    clock["now"] = dt.datetime(2026, 10, 21, 12, 29, 59, tzinfo=dt.UTC)
    assert (await put(client, h, "2026-10-22")).status_code == 200
    clock["now"] = dt.datetime(2026, 10, 21, 12, 30, 0, tzinfo=dt.UTC)  # exactly cut-off
    r = await put(client, h, "2026-10-22", False)
    assert r.status_code == 409 and r.json()["error"]["code"] == "FOOD_CUTOFF_PASSED"
    got = await client.get("/api/food/my-enrollments", headers=h, params={"from": "2026-10-22", "to": "2026-10-22"})
    assert got.json()["items"][0]["need_food"] is True  # stored row untouched
    assert got.json()["items"][0]["can_change"] is False


async def test_today_locked_in_previous_day_mode(client, H, clock):
    r = await put(client, await H("employee"), "2026-10-21")
    assert r.status_code == 409 and r.json()["error"]["code"] == "FOOD_CUTOFF_PASSED"


async def test_same_day_mode_via_setting(client, H, clock, db):
    await set_setting(db, "food.cutoff_mode", "same_day")
    await set_setting(db, "food.cutoff_time", "11:00")  # 11:00 IST = 05:30Z; now is 04:30Z
    h = await H("employee")
    r = await put(client, h, "2026-10-21")
    assert r.status_code == 200 and r.json()["cutoff_at"] == "2026-10-21T05:30:00Z"
    clock["now"] = dt.datetime(2026, 10, 21, 5, 30, tzinfo=dt.UTC)
    assert (await put(client, h, "2026-10-21", False)).status_code == 409


async def test_setting_tightened_locks_tomorrow_but_keeps_rows(client, H, clock, db):
    h = await H("employee")
    assert (await put(client, h, "2026-10-22")).status_code == 200
    await set_setting(db, "food.cutoff_time", "09:00")  # tomorrow's cut-off becomes today 09:00 IST: already passed
    assert (await put(client, h, "2026-10-22", False)).status_code == 409
    row = (await db.execute(select(FoodEnrollment))).scalar_one()
    assert row.need_food is True


async def test_invalid_dates(client, H, clock, db):
    h = await H("employee")
    for d in ("2026-10-20", "2026-10-01"):  # past
        r = await put(client, h, d)
        assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE", d
    r = await put(client, h, "2026-11-05")  # 15 days ahead
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE"
    assert (await put(client, h, "2026-11-04")).status_code == 200  # 14 days ahead (Wed)
    r = await put(client, h, "2026-10-24")  # Saturday = weekly off
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE"
    db.add(Holiday(date=dt.date(2026, 10, 27), name="Festival"))
    await db.commit()
    r = await put(client, h, "2026-10-27")
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE"
    items = (await client.get("/api/food/my-enrollments", headers=h, params={"from": "2026-10-24", "to": "2026-10-27"})).json()[
        "items"
    ]
    assert [(i["is_weekly_off"], i["is_holiday"], i["can_change"]) for i in items] == [
        (True, False, False),
        (True, False, False),
        (False, False, True),
        (False, True, False),
    ]


async def test_body_validation_yes_no_only(client, H, clock):
    h = await H("employee")
    for payload in (
        {"date": "2026-10-23", "need_food": "yes"},
        {"date": "2026-10-23", "need_food": 1},
        {"date": "2026-10-23", "need_food": None},
        {"date": "2026-10-23"},
        {"date": "2026-10-23", "need_food": True, "meal_type": "veg"},
        {"date": "2026-10-23", "need_food": True, "user_id": "x"},
    ):
        r = await client.put("/api/food/enrollment", headers=h, json=payload)
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR", payload


async def test_bulk_partial_success(client, H, clock):
    h = await H("employee")
    items = [
        {"date": "2026-10-20", "need_food": True},  # past
        {"date": "2026-10-21", "need_food": True},  # cut-off passed
        {"date": "2026-10-22", "need_food": True},  # ok
        {"date": "2026-10-24", "need_food": True},  # weekly off
        {"date": "2026-10-26", "need_food": False},  # ok
    ]
    r = await client.put("/api/food/enrollments", headers=h, json={"items": items})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [i["date"] for i in out["items"]] == ["2026-10-22", "2026-10-26"]
    assert out["failed"] == [
        {"date": "2026-10-20", "code": "INVALID_DATE"},
        {"date": "2026-10-21", "code": "FOOD_CUTOFF_PASSED"},
        {"date": "2026-10-24", "code": "INVALID_DATE"},
    ]
    mine = (await client.get("/api/food/my-enrollments", headers=h, params={"from": "2026-10-22", "to": "2026-10-26"})).json()[
        "items"
    ]
    assert [i["need_food"] for i in mine] == [True, None, None, None, False]


async def test_bulk_limits(client, H, clock):
    h = await H("employee")
    r = await client.put("/api/food/enrollments", headers=h, json={"items": []})
    assert r.status_code == 422
    dup = {"items": [{"date": "2026-10-22", "need_food": True}, {"date": "2026-10-22", "need_food": False}]}
    assert (await client.put("/api/food/enrollments", headers=h, json=dup)).status_code == 422
    many = [{"date": (dt.date(2026, 10, 22) + dt.timedelta(days=i)).isoformat(), "need_food": True} for i in range(32)]
    assert (await client.put("/api/food/enrollments", headers=h, json={"items": many})).status_code == 422


async def test_my_enrollments_range_rules(client, H, clock):
    h = await H("employee")
    r = await client.get("/api/food/my-enrollments", headers=h)  # default today..today+13
    items = r.json()["items"]
    assert len(items) == 14 and items[0]["date"] == "2026-10-21"
    r = await client.get("/api/food/my-enrollments", headers=h, params={"from": "2026-10-10", "to": "2026-10-01"})
    assert r.status_code == 422
    r = await client.get("/api/food/my-enrollments", headers=h, params={"from": "2026-01-01", "to": "2026-12-31"})
    assert r.status_code == 422


async def test_idempotent_upsert_under_concurrency(client, H, clock, db):
    h = await H("employee")
    results = await asyncio.gather(*[put(client, h, "2026-10-23", i % 2 == 0) for i in range(8)])
    assert all(r.status_code == 200 for r in results)
    rows = (await db.execute(select(FoodEnrollment))).scalars().all()
    assert len(rows) == 1  # UNIQUE(user_id,date): never duplicated
    again = await put(client, h, "2026-10-23", True)  # same value twice is a no-op success
    again2 = await put(client, h, "2026-10-23", True)
    assert again.json() == again2.json()


async def test_employee_only_touches_own_rows(client, H, clock, db, seeded):
    await put(client, await H("employee"), "2026-10-23", True)
    await put(client, await H("manager"), "2026-10-23", False)
    rows = {r.user_id: r.need_food for r in (await db.execute(select(FoodEnrollment))).scalars()}
    assert rows == {seeded["users"]["employee"]: True, seeded["users"]["manager"]: False}
    mine = (
        await client.get(
            "/api/food/my-enrollments", headers=await H("manager"), params={"from": "2026-10-23", "to": "2026-10-23"}
        )
    ).json()
    assert mine["items"][0]["need_food"] is False


async def test_rate_limit_on_writes(client, H, clock):
    h = await H("employee")
    codes = [(await put(client, h, "2026-10-23", i % 2 == 0)).status_code for i in range(32)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


async def test_unauthenticated(client):
    assert (await client.put("/api/food/enrollment", json={"date": "2026-10-23", "need_food": True})).status_code == 401
    assert (await client.get("/api/admin/food/daily")).status_code == 401


# ---------------------------------------------------------------- admin
async def _seed_people(db, seeded):
    """EMP001 yes, MGR001 no, HR001 unanswered, ADM001 yes; extra inactive user with yes (must be excluded)."""
    day = dt.date(2026, 10, 23)
    u = seeded["users"]
    db.add_all(
        [
            FoodEnrollment(user_id=u["employee"], date=day, need_food=True),
            FoodEnrollment(user_id=u["manager"], date=day, need_food=False),
            FoodEnrollment(user_id=u["admin"], date=day, need_food=True),
        ]
    )
    await db.commit()


async def test_admin_daily_counts_list_and_scope(client, H, clock, db, seeded):
    await _seed_people(db, seeded)
    for role in ("employee", "manager"):
        r = await client.get("/api/admin/food/daily", headers=await H(role), params={"date": "2026-10-23"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", role
    for role in ("admin", "hr"):
        r = await client.get("/api/admin/food/daily", headers=await H(role), params={"date": "2026-10-23"})
        assert r.status_code == 200, r.text
    d = r.json()
    assert (d["count_yes"], d["count_no"], d["count_unanswered"]) == (2, 1, 1)
    assert d["cutoff_at"] == "2026-10-22T12:30:00Z"
    assert sorted(e["user"]["employee_id"] for e in d["employees"]) == ["ADM001", "EMP001"]
    assert all(e["need_food"] is True and e["on_leave"] is False for e in d["employees"])
    assert d["by_department"] == [{"department": "General", "count_yes": 2, "count_no": 1, "count_unanswered": 1}]

    r = await client.get("/api/admin/food/daily", headers=await H("hr"), params={"date": "2026-10-23", "include": "all"})
    by = {e["user"]["employee_id"]: e["need_food"] for e in r.json()["employees"]}
    assert by == {"ADM001": True, "EMP001": True, "MGR001": False, "HR001": None}


async def test_admin_daily_department_filter_inactive_and_on_leave(client, H, clock, db, seeded):
    await _seed_people(db, seeded)
    other = Department(name="Ops", code="OPS")
    db.add(other)
    await db.flush()
    emp = await db.get(User, seeded["users"]["employee"])
    emp.department_id = other.id
    mgr = await db.get(User, seeded["users"]["manager"])
    mgr.status = "inactive"  # excluded from counts
    lt = LeaveType(code="CL", name="Casual", annual_quota=12)
    db.add(lt)
    await db.flush()
    db.add(
        LeaveRequest(
            user_id=emp.id,
            leave_type_id=lt.id,
            from_date=dt.date(2026, 10, 23),
            to_date=dt.date(2026, 10, 23),
            days=1,
            reason="x",
            status="approved",
        )
    )
    await db.commit()
    h = await H("hr")
    r = await client.get("/api/admin/food/daily", headers=h, params={"date": "2026-10-23", "department_id": str(other.id)})
    d = r.json()
    assert (d["count_yes"], d["count_no"], d["count_unanswered"]) == (1, 0, 0)
    assert d["employees"][0]["user"]["employee_id"] == "EMP001" and d["employees"][0]["on_leave"] is True
    r = await client.get("/api/admin/food/daily", headers=h, params={"date": "2026-10-23"})
    assert (r.json()["count_yes"], r.json()["count_no"], r.json()["count_unanswered"]) == (2, 0, 1)  # inactive MGR gone
    r = await client.get("/api/admin/food/daily", headers=h, params={"department_id": "nope"})
    assert r.status_code == 422


# ---------------------------------------------------------------- settings
async def test_settings_rbac_validation_and_audit(client, H, clock, db):
    for role in ("employee", "hr", "manager"):
        assert (await client.get("/api/admin/settings", headers=await H(role))).status_code == 403
        r = await client.put("/api/admin/settings", headers=await H(role), json={"settings": {"food.cutoff_time": "17:00"}})
        assert r.status_code == 403
    a = await H("admin")
    got = (await client.get("/api/admin/settings", headers=a)).json()["settings"]
    assert got["food.cutoff_mode"] == "previous_day" and got["food.cutoff_time"] == "18:00"
    for bad in (
        {"food.cutoff_time": "25:00"},
        {"food.cutoff_mode": "whenever"},
        {"food.max_advance_days": 0},
        {"food.max_advance_days": 500},
        {"food.max_advance_days": True},
        {"food.unknown": 1},
        {"office.timezone": "Mars/Base"},
    ):
        r = await client.put("/api/admin/settings", headers=a, json={"settings": bad})
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR", bad
    r = await client.put(
        "/api/admin/settings", headers=a, json={"settings": {"food.cutoff_time": "17:30", "food.cutoff_mode": "same_day"}}
    )
    assert r.status_code == 200 and r.json()["settings"]["food.cutoff_time"] == "17:30"
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "settings.update"))).scalars().all()
    assert len(logs) == 1
    assert logs[0].before is None
    assert sorted(logs[0].after["changes"], key=lambda c: c["key"]) == [
        {"key": "food.cutoff_mode", "before": "previous_day", "after": "same_day"},
        {"key": "food.cutoff_time", "before": "18:00", "after": "17:30"},
    ]
    # applies to future evaluations: same-day 17:30 IST = 12:00Z, so today is still open at 04:30Z
    assert (await put(client, await H("employee"), "2026-10-21")).json()["cutoff_at"] == "2026-10-21T12:00:00Z"


# ---------------------------------------------------------------- export
def sheet_rows(resp):
    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb["Food"]
    return wb, [[c.value for c in row] for row in ws.iter_rows()]


async def test_export_food_content_and_headers(client, H, clock, db, seeded):
    await _seed_people(db, seeded)
    r = await client.get(
        "/api/admin/export", headers=await H("hr"), params={"type": "food", "from": "2026-10-22", "to": "2026-10-24"}
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert r.headers["content-disposition"] == 'attachment; filename="employee-connect-food-2026-10-22_2026-10-24.xlsx"'
    wb, rows = sheet_rows(r)
    assert wb.sheetnames == ["Food"]
    assert rows[0] == ["Date", "Employee ID", "Name", "Department", "Need Food"]
    assert len(rows) == 4
    assert [(x[1], x[4]) for x in rows[1:]] == [("ADM001", "Yes"), ("EMP001", "Yes"), ("MGR001", "No")]
    ws = wb["Food"]
    assert ws["A1"].font.bold and ws.freeze_panes == "A2"
    assert ws.column_dimensions["C"].width >= len("Test Employee")
    audit_rows = (await db.execute(select(AuditLog).where(AuditLog.action == "export.run"))).scalars().all()
    assert len(audit_rows) == 1 and audit_rows[0].after["type"] == "food"


async def test_export_filters_empty_and_validation(client, H, clock, db, seeded):
    await _seed_people(db, seeded)
    h = await H("admin")
    base = {"type": "food", "from": "2026-10-23", "to": "2026-10-23"}
    r = await client.get("/api/admin/export", headers=h, params={**base, "employee_id": "emp001"})
    _, rows = sheet_rows(r)
    assert [x[1] for x in rows[1:]] == ["EMP001"]
    r = await client.get("/api/admin/export", headers=h, params={**base, "department_id": "00000000-0000-0000-0000-000000000000"})
    assert r.status_code == 404  # unknown department id
    r = await client.get("/api/admin/export", headers=h, params={**base, "employee_id": "NOPE999"})
    assert r.status_code == 404
    r = await client.get(
        "/api/admin/export", headers=h, params={**base, "department_id": str(seeded["dept"]), "from": "2026-10-24"}
    )
    assert r.status_code == 422
    r = await client.get("/api/admin/export", headers=h, params={**base, "from": "2025-01-01", "to": "2026-10-23"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "RANGE_TOO_LARGE"
    r = await client.get("/api/admin/export", headers=h, params={"type": "food"})
    assert r.status_code == 422


async def test_export_rbac_and_team_scope(client, H, clock, db, seeded):
    await _seed_people(db, seeded)
    p = {"type": "food", "from": "2026-10-23", "to": "2026-10-23"}
    assert (await client.get("/api/admin/export", headers=await H("employee"), params=p)).status_code == 403
    # per-type permission: food needs food.view_all, which a manager does not hold (team scope is tested per builder)
    r = await client.get("/api/admin/export", headers=await H("manager"), params=p)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    r = await client.get("/api/admin/export", headers=await H("hr"), params=p)
    assert r.status_code == 200


async def test_export_formula_injection_neutralised(client, H, clock, db, seeded):
    emp = await db.get(User, seeded["users"]["employee"])
    emp.full_name = '=HYPERLINK("http://evil","x")'
    mgr = await db.get(User, seeded["users"]["manager"])
    mgr.full_name = "@SUM(1+1)"
    dept = await db.get(Department, seeded["dept"])
    dept.name = "+cmd|' /C calc'!A0"
    await db.commit()
    await _seed_people(db, seeded)
    r = await client.get(
        "/api/admin/export", headers=await H("admin"), params={"type": "food", "from": "2026-10-23", "to": "2026-10-23"}
    )
    wb = load_workbook(io.BytesIO(r.content))
    ws = wb["Food"]
    names = {ws.cell(row=i, column=3).value for i in range(2, ws.max_row + 1)}
    assert '\'=HYPERLINK("http://evil","x")' in names and "'@SUM(1+1)" in names
    for row in ws.iter_rows(min_row=2):
        for c in row:
            assert c.data_type != "f", c.coordinate  # nothing is stored as a formula
    assert ws["D2"].value.startswith("'+")


def test_sanitize_cell_unit():
    assert sanitize_cell("=1+1") == "'=1+1"
    assert sanitize_cell("-5") == "'-5"
    assert sanitize_cell("\t=x") == "'\t=x"
    assert sanitize_cell("hello") == "hello"
    assert sanitize_cell("a\x00b") == "ab"
    assert sanitize_cell(5) == 5 and sanitize_cell(None) is None


async def test_audit_not_written_for_employee_puts(client, H, clock, db):
    await put(client, await H("employee"), "2026-10-23")
    assert (await db.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action.like("food%")))).scalar_one() == 0


def test_users_constant():  # keeps the import used and documents the seeded actors
    assert USERS["employee"] == "EMP001"
