"""Plan 10 (+ plan 13 On Duty): attendance / leave / late / permission / on duty / all export - scope, permission gates, injection, limits, audit."""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.core import security
from app.core.config import get_settings
from app.core.crypto import encrypt_bytes
from app.models import (
    Attendance,
    AuditLog,
    ClientLocation,
    LateRecord,
    LeaveRequest,
    LeaveType,
    OnDutyRequest,
    Permission,
    PermissionRequest,
    Role,
    RolePermission,
    User,
)
from app.services import export as export_svc
from tests.conftest import PASSWORD, USERS, bearer, do_login

DAY = dt.date(2026, 10, 23)
P = {"from": "2026-10-23", "to": "2026-10-23"}
ROLES4 = ("employee", "manager", "admin", "hr")
SHEET_NAMES = ["Attendance", "Leave", "Late", "Permission", "On Duty"]


@pytest.fixture
async def H(tokens_for):
    cache: dict = {}

    async def _h(role):
        if role not in cache:
            cache[role] = bearer(await tokens_for(role))
        return cache[role]

    return _h


async def _leave_type(db) -> uuid.UUID:
    lt = (await db.execute(select(LeaveType))).scalars().first()
    if lt is None:
        lt = LeaveType(code="CL", name="Casual Leave")
        db.add(lt)
        await db.flush()
    return lt.id


async def _client_location(db) -> uuid.UUID:
    loc = (await db.execute(select(ClientLocation))).scalars().first()
    if loc is None:
        loc = ClientLocation(name="Acme Site", lat=13.0827, lng=80.2707, radius_m=200)
        db.add(loc)
        await db.flush()
    return loc.id


async def seed_for_user(db, user_id, shift_id, *, reason="family event", check_in=None):
    """One attendance + late + leave + permission + on-duty row for DAY."""
    check_in = check_in or dt.datetime(2026, 10, 23, 4, 30, tzinfo=dt.UTC)  # 10:00 IST
    att = Attendance(
        user_id=user_id,
        work_date=DAY,
        shift_id=shift_id,
        check_in_at=check_in,
        check_in_within_fence=True,
        check_out_at=check_in + dt.timedelta(hours=9),
        status="late",
        minutes_late=20,
        worked_minutes=540,
    )
    db.add(att)
    await db.flush()
    db.add(
        LateRecord(
            user_id=user_id,
            attendance_id=att.id,
            work_date=DAY,
            shift_start=dt.time(9, 30),
            grace_minutes=10,
            check_in_at=check_in,
            minutes_late=20,
        )
    )
    db.add(
        LeaveRequest(
            user_id=user_id,
            leave_type_id=await _leave_type(db),
            from_date=DAY - dt.timedelta(days=3),
            to_date=DAY + dt.timedelta(days=1),  # overlaps the range, starts before it
            days=5,
            reason=reason,
            status="approved",
            decision_comment="ok",
        )
    )
    db.add(
        PermissionRequest(
            user_id=user_id,
            request_date=DAY,
            from_time=dt.time(16, 0),
            to_time=dt.time(17, 0),
            minutes=60,
            reason=reason,
            status="pending",
        )
    )
    db.add(
        OnDutyRequest(
            user_id=user_id,
            client_location_id=await _client_location(db),
            from_date=DAY,  # rejected, so it may share DAY with the approved leave above
            to_date=DAY,
            days=1,
            reason=reason,
            status="rejected",
        )
    )


async def seed_all(db, seeded, roles=ROLES4):
    for r in roles:
        await seed_for_user(db, seeded["users"][r], seeded["shift"])
    await db.commit()


def book(r):
    assert r.status_code == 200, r.text
    return load_workbook(io.BytesIO(r.content))


def ids(ws, col=None):
    """Employee ID column of a sheet (the column named 'Employee ID')."""
    head = [c.value for c in ws[1]]
    i = head.index("Employee ID")
    return [row[i].value for row in ws.iter_rows(min_row=2)]


_REDIS: list = []


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    """The team-scope window (L3) is relative to today; pin it so the fixed DAY stays inside it."""
    monkeypatch.setattr(export_svc, "today", lambda: dt.date(2026, 10, 22))


@pytest.fixture(autouse=True)
def _redis_handle(redis):
    _REDIS[:] = [redis]
    yield
    _REDIS.clear()


async def get(client, h, *, reset_limit=True, **params):
    """GET /admin/export; clears the per-user rate-limit counters first so test volume does not trip 5/min."""
    if reset_limit:
        for k in await _REDIS[0].keys("rl:export:*"):
            await _REDIS[0].delete(k)
    return await client.get("/api/admin/export", headers=h, params={**P, **params})


async def make_user(db, seeded, employee_id, role_id, *, manager_id=None, name=None):
    u = User(
        employee_id=employee_id,
        password_hash=security.hash_password(PASSWORD),
        full_name=name or employee_id.title(),
        role_id=role_id,
        department_id=seeded["dept"],
        shift_id=seeded["shift"],
        must_change_password=False,
        status="active",
        manager_id=manager_id,
    )
    db.add(u)
    await db.flush()
    return u


async def role_with(db, name, codes):
    role = Role(name=name, description=name, is_system=False)
    db.add(role)
    await db.flush()
    for c in codes:
        p = (await db.execute(select(Permission).where(Permission.code == c))).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=p.id))
    await db.flush()
    return role


async def login_as(client, employee_id):
    r = await do_login(client, employee_id)
    assert r.status_code == 200, r.text
    return bearer(r.json())


# ---------------------------------------------------------------- workbook shape
async def test_all_has_four_sheets_with_headers_and_rows_for_hr(client, H, db, seeded):
    await seed_all(db, seeded)
    r = await get(client, await H("hr"), type="all")
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert r.headers["content-disposition"] == 'attachment; filename="employee-connect-all-2026-10-23_2026-10-23.xlsx"'
    assert r.headers["cache-control"] == "no-store"
    wb = book(r)
    assert wb.sheetnames == SHEET_NAMES  # no Food
    for ws in wb.worksheets:
        assert ws.max_row == 5, ws.title  # header + 4 employees
        assert ws.freeze_panes == "A2"
        assert ws["A1"].font.bold
    assert [c.value for c in wb["Attendance"][1]] == [
        "Date", "Employee ID", "Name", "Department", "Shift", "Check-in", "Check-out", "Status",
        "Minutes Late", "Worked Hours", "Within Geofence", "Client Location", "Manual Entry",
    ]  # fmt: skip
    assert [c.value for c in wb["Late"][1]] == [
        "Date", "Employee ID", "Name", "Department", "Shift Start", "Check-in", "Minutes Late",
    ]  # fmt: skip
    # Reason / Decision Comment are not part of the default workbook (review 08 M2)
    assert [c.value for c in wb["Leave"][1]] == [
        "Request ID", "Employee ID", "Name", "Department", "Leave Type", "From", "To", "Days", "Half Day", "Status",
        "Approver", "Applied On", "Decided On",
    ]  # fmt: skip
    assert [c.value for c in wb["Permission"][1]] == [
        "Request ID", "Employee ID", "Name", "Department", "Date", "From", "To", "Minutes", "Status", "Approver",
    ]  # fmt: skip
    blob = repr([[c.value for c in row] for ws in wb.worksheets for row in ws.iter_rows()])
    assert "family event" not in blob
    wb = book(await get(client, await H("hr"), type="all", include_reasons="true"))
    assert wb["Leave"]["K1"].value == "Reason" and wb["Leave"]["M1"].value == "Decision Comment"
    assert wb["Permission"]["J1"].value == "Reason" and wb["Permission"]["L1"].value == "Comment"
    assert wb["Leave"]["K2"].value == "family event"


async def test_each_single_type_returns_only_its_sheet(client, H, db, seeded):
    await seed_all(db, seeded)
    for t, title in (("attendance", "Attendance"), ("leave", "Leave"), ("late", "Late"), ("permission", "Permission")):
        wb = book(await get(client, await H("hr"), type=t))
        assert wb.sheetnames == [title]
        assert wb[title].max_row == 5


async def test_cell_values_and_office_timezone(client, H, db, seeded):
    # 20:00Z on the 23rd is 01:30 IST on the 24th, but work_date stays the 23rd
    await seed_for_user(
        db, seeded["users"]["employee"], seeded["shift"], check_in=dt.datetime(2026, 10, 23, 20, 0, tzinfo=dt.UTC)
    )
    await db.commit()
    h = await H("hr")
    ws = book(await get(client, h, type="attendance"))["Attendance"]
    row = [c.value for c in ws[2]]
    assert row[0].date() == DAY and row[1] == "EMP001" and row[4] == "General Shift"
    assert row[5] == dt.datetime(2026, 10, 24, 1, 30)  # naive office wall-clock time
    assert row[7] == "late" and row[8] == 20 and row[9] == 9.0 and row[10] == "Yes"
    assert ws["F2"].number_format == "yyyy-mm-dd hh:mm"
    leave = book(await get(client, h, type="leave", include_reasons="true"))["Leave"]  # overlapping request is included
    lrow = [c.value for c in leave[2]]
    assert lrow[10] == "family event" and lrow[7] == 5 and lrow[8] == "No" and lrow[9] == "approved" and lrow[12] == "ok"
    perm = book(await get(client, h, type="permission"))["Permission"]
    assert [c.value for c in perm[2]][7] == 60


async def test_filters_department_employee_and_range(client, H, db, seeded):
    await seed_all(db, seeded)
    h = await H("hr")
    assert ids(book(await get(client, h, type="attendance", employee_id="emp001"))["Attendance"]) == ["EMP001"]
    other_dept = None
    from app.models import Department

    other_dept = Department(name="Other", code="OTH")
    db.add(other_dept)
    await db.commit()
    assert book(await get(client, h, type="all", department_id=str(other_dept.id)))["Leave"].max_row == 1  # header only
    r = await client.get("/api/admin/export", headers=h, params={"type": "attendance", "from": "2026-10-24", "to": "2026-10-25"})
    assert book(r)["Attendance"].max_row == 1
    # a leave request that overlaps the window is included even though it starts before it
    r = await client.get("/api/admin/export", headers=h, params={"type": "leave", "from": "2026-10-24", "to": "2026-10-26"})
    assert book(r)["Leave"].max_row == 5
    r = await client.get("/api/admin/export", headers=h, params={"type": "leave", "from": "2026-10-25", "to": "2026-10-26"})
    assert book(r)["Leave"].max_row == 1


# ---------------------------------------------------------------- scope
async def test_manager_sees_only_direct_reports_no_cross_manager_leak(client, H, db, seeded):
    roles = seeded["roles"]
    mgr2 = await make_user(db, seeded, "MGR002", roles["manager"], name="Second Manager")
    other = await make_user(db, seeded, "EMP002", roles["employee"], manager_id=mgr2.id, name="Other Team")
    await db.commit()
    await seed_all(db, seeded)
    await seed_for_user(db, other.id, seeded["shift"])
    await seed_for_user(db, mgr2.id, seeded["shift"])
    await db.commit()

    h = await H("manager")
    wb = book(await get(client, h, type="all"))
    for ws in wb.worksheets:
        assert ids(ws) == ["EMP001"], ws.title  # not EMP002, not the manager himself, not other roles
    blob = repr([[c.value for c in row] for ws in wb.worksheets for row in ws.iter_rows()])
    assert "Other Team" not in blob and "MGR002" not in blob
    # the second manager sees only his own report
    h2 = await login_as(client, "MGR002")
    for ws in book(await get(client, h2, type="all")).worksheets:
        assert ids(ws) == ["EMP002"], ws.title
    # HR sees everybody (6 users with data)
    assert book(await get(client, await H("hr"), type="attendance"))["Attendance"].max_row == 7


async def test_manager_cannot_reach_outside_team_via_filters(client, H, db, seeded):
    await seed_all(db, seeded)
    h = await H("manager")
    for t in ("all", "attendance", "leave", "late", "permission"):
        assert (await get(client, h, type=t, employee_id="ADM001")).status_code == 403, t
        assert (await get(client, h, type=t, employee_id="MGR001")).status_code == 403, t  # himself is not a report
        assert (await get(client, h, type=t, employee_id="EMP001")).status_code == 200, t
    # unknown ids look exactly like out-of-scope ones for a team-scoped caller (review 08 L2)
    unknown = await get(client, h, type="all", employee_id="NOPE")
    known = await get(client, h, type="all", employee_id="ADM001")
    assert unknown.status_code == known.status_code == 403
    assert unknown.json()["error"]["code"] == known.json()["error"]["code"] == "FORBIDDEN"
    assert unknown.json()["error"]["message"] == known.json()["error"]["message"]
    assert (await get(client, await H("hr"), type="all", employee_id="NOPE")).status_code == 404  # export.all keeps 404
    # a department filter can only narrow the team scope
    r = await get(client, h, type="attendance", department_id=str(seeded["dept"]))
    assert ids(book(r)["Attendance"]) == ["EMP001"]


async def test_manager_with_no_reports_gets_header_only(client, H, db, seeded):
    await seed_all(db, seeded)
    roles = seeded["roles"]
    await make_user(db, seeded, "MGR003", roles["manager"])
    await db.commit()
    h = await login_as(client, "MGR003")
    for ws in book(await get(client, h, type="all")).worksheets:
        assert ws.max_row == 1


# ---------------------------------------------------------------- permission gates
async def test_permission_gates(client, H, db, seeded):
    await seed_all(db, seeded)
    assert (await get(client, await H("employee"), type="all")).status_code == 403
    assert (await get(client, await H("manager"), type="food")).status_code == 403
    assert (await client.get("/api/admin/export", params={**P, "type": "all"})).status_code == 401

    roles_codes = {
        "ATT": ["export.team", "attendance.view_team"],
        "REQ": ["export.team", "request.view_team"],
        "NOEXPORT": ["attendance.view_all", "request.view_all", "late.view_all"],
    }
    for name, codes in roles_codes.items():
        role = await role_with(db, name, codes)
        await make_user(db, seeded, f"U{name}", role.id, manager_id=None)
    await db.commit()

    h = await login_as(client, "UATT")
    assert (await get(client, h, type="attendance")).status_code == 200
    for t in ("leave", "permission", "late", "all", "food"):
        assert (await get(client, h, type=t)).status_code == 403, t
    h = await login_as(client, "UREQ")
    assert (await get(client, h, type="leave")).status_code == 200
    assert (await get(client, h, type="permission")).status_code == 200
    assert (await get(client, h, type="attendance")).status_code == 403
    assert (await get(client, h, type="all")).status_code == 403
    h = await login_as(client, "UNOEXPORT")
    assert (await get(client, h, type="attendance")).status_code == 403  # view perms alone are not enough


async def test_export_all_without_view_all_is_still_team_scoped(client, H, db, seeded):
    await seed_all(db, seeded)
    role = await role_with(db, "WIDE", ["export.all", "export.team", "attendance.view_team"])
    boss = await make_user(db, seeded, "UWIDE", role.id)
    rep = await make_user(db, seeded, "EMP009", seeded["roles"]["employee"], manager_id=boss.id)
    await seed_for_user(db, rep.id, seeded["shift"])
    await db.commit()
    h = await login_as(client, "UWIDE")
    assert ids(book(await get(client, h, type="attendance"))["Attendance"]) == ["EMP009"]
    assert (await get(client, h, type="attendance", employee_id="EMP001")).status_code == 403


# ---------------------------------------------------------------- injection / encoding / data minimisation
async def test_formula_injection_and_illegal_chars(client, H, db, seeded):
    evil_name = '=HYPERLINK("http://x","y")'
    u = await db.get(User, seeded["users"]["employee"])
    u.full_name = evil_name
    await seed_for_user(db, u.id, seeded["shift"], reason="@SUM(1)\x00\x01 ok￾")
    lr = (await db.execute(select(LeaveRequest))).scalars().one()
    lr.decision_comment = "-2+3"
    pr = (await db.execute(select(PermissionRequest))).scalars().one()
    pr.reason = "x" * 40_000
    await db.commit()
    h = await H("hr")
    for t, col in (("attendance", "C"), ("leave", "C"), ("late", "C"), ("permission", "C")):
        ws = book(await get(client, h, type=t)).worksheets[0]
        cell = ws[f"{col}2"]
        assert cell.data_type == "s" and cell.value == "'" + evil_name, t
    leave = book(await get(client, h, type="leave", include_reasons="true"))["Leave"]
    assert leave["K2"].value == "'@SUM(1) ok" and leave["K2"].data_type == "s"
    assert leave["M2"].value == "'-2+3"
    perm = book(await get(client, h, type="permission", include_reasons="true"))["Permission"]
    assert len(perm["J2"].value) == 32_000


async def test_undecryptable_and_purged_reasons_do_not_break_the_export(client, H, db, seeded):
    await seed_all(db, seeded, roles=("employee", "manager"))
    from sqlalchemy import text

    q = text("UPDATE leave_requests SET reason_enc = :v WHERE user_id = :u")  # raw bytes, bypassing the encrypting type
    await db.execute(q, {"v": b"not a fernet token", "u": seeded["users"]["employee"].hex})
    await db.execute(q, {"v": None, "u": seeded["users"]["manager"].hex})
    await db.commit()
    ws = book(await get(client, await H("hr"), type="leave", include_reasons="true"))["Leave"]
    by_id = {row[1].value: row[10].value for row in ws.iter_rows(min_row=2)}
    assert by_id == {"EMP001": "[unreadable]", "MGR001": None}  # empty string reads back as None
    assert encrypt_bytes(b"x")  # keys configured


async def test_no_sensitive_columns_or_values(client, H, db, seeded):
    u = await db.get(User, seeded["users"]["employee"])
    await seed_for_user(db, u.id, seeded["shift"])
    att = (await db.execute(select(Attendance))).scalars().one()
    att.check_in_loc = {"lat": 12.34567, "lng": 77.65432}
    att.check_in_mock_location = True
    att.suspicious = True
    await db.commit()
    wb = book(await get(client, await H("hr"), type="all"))
    heads = {c.value.lower() for ws in wb.worksheets for c in ws[1]}
    assert not heads & {"latitude", "longitude", "lat", "lng", "email", "phone", "address", "dob", "suspicious", "distance"}
    blob = repr([[c.value for c in row] for ws in wb.worksheets for row in ws.iter_rows()])
    for secret in ("12.34567", "77.65432", "corp-mail.com", "password", "argon2"):
        assert secret not in blob


# ---------------------------------------------------------------- limits
async def test_range_and_date_validation(client, H, db, seeded):
    h = await H("hr")
    r = await client.get("/api/admin/export", headers=h, params={"type": "all", "from": "2026-01-01", "to": "2027-01-02"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "RANGE_TOO_LARGE"  # 366 days + 1
    r = await client.get("/api/admin/export", headers=h, params={"type": "all", "from": "2026-01-01", "to": "2027-01-01"})
    assert r.status_code == 200  # exactly 366 days
    r = await client.get("/api/admin/export", headers=h, params={"type": "all", "from": "2026-10-02", "to": "2026-10-01"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert (await client.get("/api/admin/export", headers=h, params={"type": "all", "from": "2026-10-02"})).status_code == 422
    assert (await client.get("/api/admin/export", headers=h, params={"type": "bogus", **P})).status_code == 422
    r = await client.get("/api/admin/export", headers=h, params={"type": "all", "from": "0001-01-01", "to": "0001-01-02"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE"


async def test_row_cap_applies_per_sheet_for_every_type(client, H, db, seeded, monkeypatch):
    await seed_all(db, seeded)
    monkeypatch.setattr(get_settings(), "export_max_rows", 3)
    h = await H("hr")
    for t in ("all", "attendance", "leave", "late", "permission"):
        r = await get(client, h, type=t)
        assert r.status_code == 422 and r.json()["error"]["code"] == "RANGE_TOO_LARGE", t
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "export.run"))).scalars().all()
    assert len(logs) == 5 and all(lg.outcome == "failure" and lg.after["code"] == "RANGE_TOO_LARGE" for lg in logs)
    # the manager's team (1 person) fits under the same cap
    assert (await get(client, await H("manager"), type="all")).status_code == 200


async def test_rate_limit_5_per_minute_per_user(client, H, db, seeded):
    h = await H("hr")
    for _ in range(5):
        assert (await get(client, h, reset_limit=False, type="late")).status_code == 200
    r = await get(client, h, reset_limit=False, type="late")
    assert r.status_code == 429
    assert (await get(client, await H("manager"), reset_limit=False, type="late")).status_code == 200  # other users unaffected


# ---------------------------------------------------------------- audit
async def test_audit_records_filters_and_counts_never_data(client, H, db, seeded):
    u = await db.get(User, seeded["users"]["employee"])
    u.full_name = "Secret Person"
    await seed_all(db, seeded)
    await db.commit()
    r = await get(client, await H("hr"), type="all", department_id=str(seeded["dept"]), employee_id="EMP001")
    assert r.status_code == 200
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "export.run"))).scalars().one()
    assert log.actor_employee_id == "HR001"
    a = log.after
    assert a["type"] == "all" and a["from"] == "2026-10-23" and a["to"] == "2026-10-23" and a["scope"] == "all"
    assert a["employee_id"] == "EMP001" and a["department_id"] == str(seeded["dept"])
    assert a["rows"] == {"Attendance": 1, "Leave": 1, "Late": 1, "Permission": 1, "On Duty": 1} and a["total_rows"] == 5
    blob = repr(a) + repr(log.before)
    assert "Secret Person" not in blob and "family event" not in blob
    assert a["include_reasons"] is False and a["daily_exports"] == 1 and a["daily_rows"] == 5
    # manager export is audited as team scope; the employee never reaches the handler (dependency 403, not audited here)
    await get(client, await H("manager"), type="leave")
    await get(client, await H("employee"), type="leave")
    logs = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "export.run", AuditLog.outcome == "success"))).scalars().all()
    )
    assert sorted(lg.after["scope"] for lg in logs) == ["all", "team"]


def test_registry_covers_every_type_with_permissions():
    assert {"attendance", "leave", "late", "permission", "on_duty", "food"} == set(export_svc.SHEETS)
    assert set(export_svc.ALL_TYPES) == {"attendance", "leave", "late", "permission", "on_duty"}
    assert USERS  # fixture import sanity
