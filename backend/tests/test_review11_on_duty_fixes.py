"""Review 11 fixes for On Duty (docs/reviews/11-on-duty-security.md): M2 (self-assignment, admin alerts, radius cap for
'all'), M1 (flag-only spoofing heuristics), L1 (selfie lock), L2 (retention), L4 (audited probing refusals)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.models import AuditLog, DutyLocationAssignment, File, OnDutyVisit
from app.services import attendance as att_svc
from app.services import punch
from app.services import settings as settings_svc
from app.services.notifications import OUTBOX
from tests.conftest import bearer
from tests.on_duty_helpers import (
    FAR,
    SITE,
    ist,
    make_file,
    make_loc,
    office_in,
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
    return {"name": "Review Site", "address": "1 Test Rd", "lat": 13.0067, "lng": 80.2206, "radius_m": 200, **over}


def alerts_for(loc_id) -> list[dict]:
    return [o["payload"] for o in OUTBOX if o["payload"]["type"] == "admin_alert" and o["payload"]["id"] == str(loc_id)]


async def visits(db) -> list[OnDutyVisit]:
    return list(
        (
            await db.execute(select(OnDutyVisit).order_by(OnDutyVisit.check_in_at).execution_options(populate_existing=True))
        ).scalars()
    )


async def signals(db) -> list[dict]:
    rows = (
        await db.execute(select(AuditLog).where(AuditLog.action == "on_duty.suspicious_punch").order_by(AuditLog.id))
    ).scalars()
    return [a.after for a in rows]


# ------------------------------------------------------------------ M2
async def test_hr_cannot_assign_itself_but_admin_can(client, H, db):
    loc = await make_loc(db, assign=())
    hr_id, adm_id, emp_id = [str(await user_id(db, r)) for r in ("hr", "admin", "employee")]
    url = f"/api/admin/duty-locations/{loc.id}/assignments"
    r = await client.put(url, headers=await H("hr"), json={"assign_mode": "selected", "user_ids": [emp_id, hr_id]})
    assert r.status_code == 403 and code(r) == "FORBIDDEN", r.text
    assert (await db.execute(select(DutyLocationAssignment))).first() is None  # nothing was written, not even the other user
    # HR may still assign other people
    r = await client.put(url, headers=await H("hr"), json={"assign_mode": "selected", "user_ids": [emp_id]})
    assert r.status_code == 200 and len(r.json()["items"]) == 1
    # an admin (role.assign) may add itself; the audit row carries self_assigned and the admins are alerted
    OUTBOX.clear()
    r = await client.put(url, headers=await H("admin"), json={"assign_mode": "selected", "user_ids": [emp_id, adm_id]})
    assert r.status_code == 200
    a = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "duty_location.assignments").order_by(AuditLog.id)))
        .scalars()
        .all()
    )
    assert a[0].after["self_assigned"] is False and a[-1].after["self_assigned"] is True
    assert any("assigned themselves" in p["body"] for p in alerts_for(loc.id))


async def test_hr_cannot_assign_itself_through_a_department(client, H, db, seeded):
    loc = await make_loc(db, assign=())
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments",
        headers=await H("hr"),
        json={"assign_mode": "selected", "user_ids": [], "add_department_ids": [str(seeded["dept"])]},
    )
    assert r.status_code == 403 and code(r) == "FORBIDDEN"


async def test_all_mode_radius_cap(client, H, db):
    hr = await H("hr")
    r = await client.post("/api/admin/duty-locations", headers=hr, json=loc_body(assign_mode="all", radius_m=301))
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR" and r.json()["error"]["details"][0]["field"] == "radius_m"
    r = await client.post("/api/admin/duty-locations", headers=hr, json=loc_body(assign_mode="all", radius_m=300))
    assert r.status_code == 201
    lid = r.json()["id"]
    # a big radius is fine while the location is 'selected', but cannot be switched to 'all' or edited into it
    big = await client.post("/api/admin/duty-locations", headers=hr, json=loc_body(name="Big Site", radius_m=2000))
    assert big.status_code == 201
    bid = big.json()["id"]
    r = await client.put(f"/api/admin/duty-locations/{bid}/assignments", headers=hr, json={"assign_mode": "all", "user_ids": []})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "radius_m"
    r = await client.put(f"/api/admin/duty-locations/{lid}", headers=hr, json=loc_body(assign_mode="all", radius_m=900))
    assert r.status_code == 422
    # the cap is a validated setting
    await set_setting(db, "on_duty.all_mode_max_radius_m", 1000)
    r = await client.put(f"/api/admin/duty-locations/{lid}", headers=hr, json=loc_body(assign_mode="all", radius_m=900))
    assert r.status_code == 200
    for bad in (5, 2001, 10.5, "x", True):
        with pytest.raises(Exception):  # noqa: B017,PT011 - AppError
            settings_svc.validate("on_duty.all_mode_max_radius_m", bad)
    assert settings_svc.validate("on_duty.all_mode_max_radius_m", 450) == 450


async def test_all_mode_switch_is_alerted_and_flagged(client, H, db):
    loc = await make_loc(db, assign=(), radius=250)
    OUTBOX.clear()
    r = await client.put(
        f"/api/admin/duty-locations/{loc.id}/assignments", headers=await H("hr"), json={"assign_mode": "all", "user_ids": []}
    )
    assert r.status_code == 200
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "duty_location.assignments"))).scalars().one()
    assert a.after["self_assigned"] is True  # 'all' includes the actor
    assert any("to all employees" in p["body"] for p in alerts_for(loc.id))


async def test_admins_alerted_on_create_and_geo_change_only(client, H, db):
    hr = await H("hr")
    OUTBOX.clear()
    r = await client.post("/api/admin/duty-locations", headers=hr, json=loc_body())
    lid = r.json()["id"]
    created = alerts_for(lid)
    assert len(created) == 1 and "created a duty location" in created[0]["body"]
    assert "13.0067" not in repr(created) and "80.2206" not in repr(created)  # names only, never coordinates
    admin_id = str(await user_id(db, "admin"))
    assert [o["user_ids"] for o in OUTBOX if o["payload"]["id"] == lid] == [[admin_id]]  # role.assign holders only
    OUTBOX.clear()
    r = await client.put(f"/api/admin/duty-locations/{lid}", headers=hr, json=loc_body(address="New address"))
    assert r.status_code == 200 and alerts_for(lid) == []  # nothing geographic changed
    for over in ({"radius_m": 250}, {"lat": 13.01}, {"lng": 80.23}):
        OUTBOX.clear()
        r = await client.put(f"/api/admin/duty-locations/{lid}", headers=hr, json=loc_body(**over))
        assert r.status_code == 200
        assert len(alerts_for(lid)) == 1 and "changed the coordinates or radius" in alerts_for(lid)[0]["body"]
    upd = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "duty_location.update").order_by(AuditLog.id)))
        .scalars()
        .all()
    )
    assert [a.after["geo_changed"] for a in upd] == [False, True, True, True]


# ------------------------------------------------------------------ M1 (flag only, never reject)
@pytest.fixture
async def emp(tokens_for):
    return bearer(await tokens_for("employee"))


@pytest.fixture
async def site(db):
    return await make_loc(db)


async def test_honest_visit_is_not_flagged_and_employee_never_sees_the_flag(client, emp, clock, site, db):
    r = await visit_in(client, emp, site)
    assert r.status_code == 201
    clock(ist(2026, 10, 21, 12, 0))
    r2 = await visit_out(client, emp, lat=FAR["lat"], lng=FAR["lng"] + 0.0001, accuracy_m=12)
    assert r2.status_code == 200
    assert [v.suspicious for v in await visits(db)] == [False]
    assert await signals(db) == []
    hidden = {
        "suspicious",
        "radius_m",
        "check_in_distance_m",
        "check_out_distance_m",
        "check_in_accuracy_m",
        "check_out_accuracy_m",
    }
    hidden |= {"check_out_within_fence", "check_in_mock_location"}
    today = (await client.get("/api/on-duty/today", headers=emp)).json()
    hist = (await client.get("/api/on-duty/visits", headers=emp)).json()
    shown = [r.json()["visit"], r2.json()["visit"], today["visits"][0], hist["items"][0]]
    assert all(not hidden & set(v) for v in shown)


async def test_centre_with_zero_accuracy_is_flagged_not_rejected(client, emp, clock, site, db):
    r = await visit_in(client, emp, site, lat=SITE["lat"], lng=SITE["lng"], accuracy_m=0)
    assert r.status_code == 201, r.text
    assert [v.suspicious for v in await visits(db)] == [True]
    assert (await signals(db))[0] == {"kind": "check_in", "reason": "heuristic", "signals": ["centre_with_static_accuracy"]}


async def test_centre_with_repeated_accuracy_is_flagged(client, emp, clock, site, db):
    assert (await visit_in(client, emp, site, accuracy_m=8.5)).status_code == 201  # ~100 m from the centre: fine
    clock(ist(2026, 10, 21, 11, 30))
    assert (await visit_out(client, emp, lat=FAR["lat"], lng=FAR["lng"], accuracy_m=9)).status_code == 200
    clock(ist(2026, 10, 21, 12, 0))
    r = await visit_in(client, emp, site, lat=SITE["lat"], lng=SITE["lng"], accuracy_m=8.5)  # dead centre, same accuracy again
    assert r.status_code == 201
    first, second = await visits(db)
    assert first.suspicious is False and second.suspicious is True


async def test_centre_with_a_fresh_accuracy_value_is_not_flagged(client, emp, clock, site, db):
    r = await visit_in(client, emp, site, lat=SITE["lat"], lng=SITE["lng"], accuracy_m=6.3)
    assert r.status_code == 201 and [v.suspicious for v in await visits(db)] == [False]


async def test_repeated_coordinates_across_punches_are_flagged(client, emp, clock, site, db):
    assert (await visit_in(client, emp, site)).status_code == 201
    clock(ist(2026, 10, 21, 11, 30))
    assert (await visit_out(client, emp)).status_code == 200  # identical lat/lng to the check-in
    first = (await visits(db))[0]
    assert first.suspicious is True
    clock(ist(2026, 10, 21, 12, 0))
    assert (await visit_in(client, emp, site)).status_code == 201  # and again on a later visit
    assert [s["signals"] for s in await signals(db)] == [["repeated_coordinates"], ["repeated_coordinates"]]
    assert "13.0009" not in repr(await signals(db))  # reasons only, no coordinates in the audit rows


async def test_impossible_travel_from_an_office_punch_is_flagged(client, emp, clock, site, db):
    assert (await office_in(client, emp)).status_code in (200, 201)  # Bengaluru, 11:00
    clock(ist(2026, 10, 21, 11, 5))
    r = await visit_in(client, emp, site)  # ~290 km away five minutes later
    assert r.status_code == 201, r.text
    assert (await visits(db))[0].suspicious is True
    assert (await signals(db))[0]["signals"] == ["impossible_travel"]


async def test_impossible_travel_between_two_visits_is_flagged(client, emp, clock, db):
    a = await make_loc(db, "Site A")
    b = await make_loc(db, "Site B", lat=14.5, lng=80.0)  # ~165 km north of A
    assert (await visit_in(client, emp, a)).status_code == 201
    clock(ist(2026, 10, 21, 11, 10))
    assert (await visit_out(client, emp, lat=13.0011)).status_code == 200
    clock(ist(2026, 10, 21, 11, 20))
    r = await visit_in(client, emp, b, lat=14.5004, lng=80.0001)
    assert r.status_code == 201
    v = await visits(db)
    assert v[0].suspicious is False and v[1].suspicious is True
    # the same trip with enough time is plausible
    clock(ist(2026, 10, 21, 11, 30))
    await visit_out(client, emp, lat=14.5008, lng=80.0002)
    clock(ist(2026, 10, 21, 18, 0))
    assert (await visit_in(client, emp, a, lat=13.0012, lng=80.0003)).status_code == 201
    assert (await visits(db))[2].suspicious is False


# ------------------------------------------------------------------ L1
class _Recorder:
    """Wraps a session and keeps every statement, so the SELECT ... FOR UPDATE on the file row can be asserted (SQLite, the test
    database, silently drops FOR UPDATE when compiling, so the SQL text cannot be used)."""

    def __init__(self, session):
        self._s = session
        self.stmts: list = []

    async def execute(self, stmt, *a, **k):
        self.stmts.append(stmt)
        return await self._s.execute(stmt, *a, **k)

    async def get(self, *a, **k):  # must not be used for the selfie any more
        raise AssertionError("session.get would read the file row without a lock")


async def test_validate_selfie_locks_the_file_row(db, clock, seeded):
    fid = await make_file(db)
    uid = await user_id(db, "employee")
    rec = _Recorder(db)
    cfg = await att_svc.load_settings(db)
    out = await punch.validate_selfie(rec, SimpleNamespace(id=uid), fid, True, cfg, att_svc.utcnow())
    assert out == fid
    first = rec.stmts[0]
    assert first._for_update_arg is not None  # SELECT ... FOR UPDATE on files
    assert "files" in str(first)


async def test_selfie_still_single_use_across_office_and_visit(client, emp, clock, site, db):
    fid = await make_file(db)
    assert (await office_in(client, emp, {"selfie_file_id": str(fid)})).status_code in (200, 201)
    r = await visit_in(client, emp, site, selfie_file_id=str(fid))
    assert r.status_code == 422 and code(r) == "SELFIE_REUSED"
    assert (await db.get(File, fid)) is not None


# ------------------------------------------------------------------ L2
async def test_retention_clears_note_distance_and_accuracy_with_the_coordinates(client, emp, clock, site, db):
    assert (await visit_in(client, emp, site, note="private errand")).status_code == 201
    clock(ist(2026, 10, 21, 12, 0))
    assert (await visit_out(client, emp, lat=FAR["lat"], lng=FAR["lng"], note="left early")).status_code == 200
    q = text(
        "SELECT check_in_loc_enc, check_out_loc_enc, note_enc, check_in_distance_m, check_out_distance_m, check_in_accuracy_m, "
        "check_out_accuracy_m, check_out_within_fence FROM on_duty_visits"
    )
    before = (await db.execute(q)).one()
    assert all(x is not None for x in before)
    await set_setting(db, "attendance.location_retention_days", 30)
    # inside the window: untouched
    await att_svc.purge_pii(db, now=ist(2026, 11, 10, 3, 0))
    assert tuple((await db.execute(q)).one()) == tuple(before)
    # past the cutoff: everything but the within-fence verdict goes
    out = await att_svc.purge_pii(db, now=ist(2026, 12, 1, 3, 0))
    assert out["locations"] >= 1
    after = (await db.execute(q)).one()
    assert after[:7] == (None,) * 7 and after[7] is not None
    # idempotent
    again = await att_svc.purge_pii(db, now=ist(2026, 12, 1, 3, 0))
    assert again["locations"] == 0


# ------------------------------------------------------------------ L4
async def test_visit_id_mismatch_is_audited_without_coordinates(client, emp, clock, site, db):
    import uuid

    assert (await visit_in(client, emp, site)).status_code == 201
    clock(ist(2026, 10, 21, 12, 0))
    r = await visit_out(client, emp, visit_id=str(uuid.uuid4()))
    assert r.status_code == 404
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "attendance.punch_rejected"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].after == {"kind": "on_duty_check_out", "reason": "VISIT_ID_MISMATCH"} and rows[0].outcome == "failure"
    assert "13.0" not in repr(rows[0].after)
    assert (await visit_out(client, emp)).status_code == 200  # the open visit is untouched and not counted as a failed punch


async def test_unavailable_location_is_audited_and_not_counted(client, emp, clock, db):
    import uuid

    class Ghost:
        id = uuid.uuid4()

    for _ in range(7):  # more than the 5 counted failures: still never locked out
        r = await visit_in(client, emp, Ghost)
        assert r.status_code == 422 and code(r) == "DUTY_LOCATION_UNAVAILABLE"
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "attendance.punch_rejected"))).scalars().all()
    assert len(rows) == 7 and all(a.after == {"kind": "on_duty_check_in", "reason": "DUTY_LOCATION_UNAVAILABLE"} for a in rows)
    assert "location_id" not in repr([a.after for a in rows]) and "13.0" not in repr([a.after for a in rows])
    site = await make_loc(db, "Real Site")
    assert (await visit_in(client, emp, site)).status_code == 201  # no cool-down was triggered
