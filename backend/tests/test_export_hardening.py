"""Review 04 fixes: export temp files / threads / timeout / scope / permissions, settings table, sanitiser, dates, audit.
Clock: 2026-10-21 10:00 IST (04:30Z), like test_food."""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import io
import threading
import uuid

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import AppError
from app.models import AuditLog, FoodEnrollment, Setting
from app.services import export as export_svc
from app.services import food as fs
from app.services import settings as settings_svc
from app.services import xlsx
from app.services.xlsx import SheetSpec, sanitize_cell
from tests.conftest import bearer
from tests.test_export_full import seed_all

NOW = dt.datetime(2026, 10, 21, 4, 30, tzinfo=dt.UTC)
DAY = dt.date(2026, 10, 23)


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


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(export_svc, "today", lambda: dt.date(2026, 10, 22))  # keeps DAY inside the team window


@pytest.fixture
def tmp_export_dir(tmp_path, monkeypatch):
    d = tmp_path / "exports"
    monkeypatch.setattr(get_settings(), "export_temp_dir", str(d))
    return d


async def seed_food(db, seeded, roles=("employee", "manager", "admin", "hr")):
    for r in roles:
        db.add(FoodEnrollment(user_id=seeded["users"][r], date=DAY, need_food=True))
    await db.commit()


def spec(n: int, title="T", boom_after: int | None = None, max_rows: int = 1000) -> SheetSpec:
    async def count() -> int:
        return n

    async def rows():
        for i in range(n):
            if boom_after is not None and i >= boom_after:
                raise RuntimeError("db went away")
            yield (i, f"name {i}", dt.date(2026, 1, 1))

    return SheetSpec(title, ["A", "B", "C"], count, rows, max_rows)


def leftovers(d):
    return list(d.iterdir()) if d.exists() else []


# ---------------------------------------------------------------- M1: temp files
async def test_count_first_refuses_before_writing_anything(tmp_export_dir, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("builder must not be created when the count is over the cap")

    monkeypatch.setattr(xlsx, "_Builder", boom)
    with pytest.raises(OverflowError):
        await xlsx.build_workbook([spec(50, max_rows=10)])
    assert leftovers(tmp_export_dir) == []


async def test_failure_midway_removes_every_temp_file(tmp_export_dir):
    before = list(xlsx._opx_writer.ALL_TEMP_FILES)
    with pytest.raises(RuntimeError):
        await xlsx.build_workbook([spec(2500, boom_after=1700, max_rows=10_000)])
    assert leftovers(tmp_export_dir) == []
    assert xlsx._opx_writer.ALL_TEMP_FILES == before  # no stale registry entries either


async def test_overflow_during_write_cleans_up(tmp_export_dir):
    s = spec(5000, max_rows=10_000)
    s.max_rows = 1200  # the count said 5000 but a cap is enforced while writing too (rows grew between count and write)

    async def count():
        return 5

    s.count = count
    with pytest.raises(OverflowError):
        await xlsx.build_workbook([s])
    assert leftovers(tmp_export_dir) == []


async def test_success_leaves_nothing_after_close(tmp_export_dir):
    built = await xlsx.build_workbook([spec(1200, max_rows=10_000)])
    assert built.counts == {"T": 1200} and leftovers(tmp_export_dir)  # the spooled output lives until closed
    wb = load_workbook(io.BytesIO(built.file.read()))
    assert wb["T"].max_row == 1201
    built.close()
    built.close()  # idempotent
    assert leftovers(tmp_export_dir) == []


async def test_router_overflow_and_success_leave_no_files(client, H, clock, db, seeded, tmp_export_dir, monkeypatch):
    await seed_food(db, seeded)
    p = {"type": "food", "from": "2026-10-23", "to": "2026-10-23"}
    r = await client.get("/api/admin/export", headers=await H("hr"), params=p)
    assert r.status_code == 200 and leftovers(tmp_export_dir) == []
    monkeypatch.setattr(get_settings(), "export_max_rows", 2)
    r = await client.get("/api/admin/export", headers=await H("hr"), params=p)
    assert r.status_code == 422 and r.json()["error"]["code"] == "RANGE_TOO_LARGE"
    assert leftovers(tmp_export_dir) == []


async def test_stale_directories_are_swept(tmp_export_dir):
    tmp_export_dir.mkdir()
    old = tmp_export_dir / (xlsx.DIR_PREFIX + "old")
    old.mkdir()
    (old / "openpyxl.x").write_text("pii")
    import os
    import time

    os.utime(old, (time.time() - 7200, time.time() - 7200))
    fresh = tmp_export_dir / (xlsx.DIR_PREFIX + "fresh")
    fresh.mkdir()
    other = tmp_export_dir / "unrelated"
    other.mkdir()
    assert xlsx.sweep_stale(str(tmp_export_dir)) == 1
    assert not old.exists() and fresh.exists() and other.exists()


# ---------------------------------------------------------------- M2: thread, concurrency, timeout
async def test_workbook_is_written_in_a_worker_thread(tmp_export_dir, monkeypatch):
    seen: set[int] = set()
    orig = xlsx._Builder.append

    def spy(self, title, rows):
        seen.add(threading.get_ident())
        return orig(self, title, rows)

    monkeypatch.setattr(xlsx._Builder, "append", spy)
    (await xlsx.build_workbook([spec(30)])).close()
    assert seen and threading.get_ident() not in seen


async def test_concurrency_is_limited_to_two(tmp_export_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "export_max_concurrency", 2)
    active = {"now": 0, "max": 0}
    lock = threading.Lock()
    orig = xlsx._Builder.finish

    def slow_finish(self):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        import time

        time.sleep(0.15)
        try:
            return orig(self)
        finally:
            with lock:
                active["now"] -= 1

    monkeypatch.setattr(xlsx._Builder, "finish", slow_finish)
    monkeypatch.setattr(xlsx, "SLOT_WAIT_S", 10)
    built = await asyncio.gather(*[xlsx.build_workbook([spec(3)]) for _ in range(6)])
    for b in built:
        b.close()
    assert active["max"] == 2
    assert leftovers(tmp_export_dir) == []


async def test_busy_slots_answer_503(tmp_export_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "export_max_concurrency", 1)
    monkeypatch.setattr(xlsx, "SLOT_WAIT_S", 0.05)
    orig = xlsx._Builder.finish

    def slow(self):
        import time

        time.sleep(0.4)
        return orig(self)

    monkeypatch.setattr(xlsx._Builder, "finish", slow)
    first = asyncio.create_task(xlsx.build_workbook([spec(2)]))
    await asyncio.sleep(0.05)
    with pytest.raises(AppError) as e:
        await xlsx.build_workbook([spec(2)])
    assert e.value.status == 503 and e.value.code == "TEMPORARILY_UNAVAILABLE"
    (await first).close()


async def test_timeout_aborts_and_cleans_up(tmp_export_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "export_timeout_s", 0.2)
    orig = xlsx._Builder.append

    def slow(self, title, rows):
        import time

        time.sleep(0.5)
        return orig(self, title, rows)

    monkeypatch.setattr(xlsx._Builder, "append", slow)
    with pytest.raises(OverflowError):
        await xlsx.build_workbook([spec(1500)])
    assert leftovers(tmp_export_dir) == []


async def test_timeout_maps_to_range_too_large_via_router(client, H, clock, db, seeded, tmp_export_dir, monkeypatch):
    await seed_food(db, seeded)
    monkeypatch.setattr(get_settings(), "export_timeout_s", 0.05)
    orig = xlsx._Builder.start_sheet

    def slow(self, *a):
        import time

        time.sleep(0.3)
        return orig(self, *a)

    monkeypatch.setattr(xlsx._Builder, "start_sheet", slow)
    r = await client.get(
        "/api/admin/export", headers=await H("hr"), params={"type": "food", "from": "2026-10-23", "to": "2026-10-23"}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "RANGE_TOO_LARGE"
    assert leftovers(tmp_export_dir) == []


# ---------------------------------------------------------------- M3: scope + per-type permission
def test_every_registered_builder_requires_scope_and_has_a_permission_entry():
    assert set(export_svc.SHEETS) <= set(export_svc.TYPE_PERMISSIONS)
    for name, builder in export_svc.SHEETS.items():
        params = inspect.signature(builder).parameters
        assert "scope" in params and params["scope"].default is inspect.Parameter.empty, name
        assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), name  # no **filters to swallow it
        assert export_svc.TYPE_PERMISSIONS[name], name


async def test_every_registered_type_respects_team_scope_at_builder_level(db, seeded):
    """Add a type to SHEETS and this test runs it with a manager's scope over data for EVERYONE: out-of-team rows fail it."""
    await seed_food(db, seeded)
    await seed_all(db, seeded)
    team = await _team(db, seeded)
    assert team == {seeded["users"]["employee"]}
    scope = export_svc.ExportScope.team(team)
    for name, builder in export_svc.SHEETS.items():
        sp = builder(db, scope, export_svc.ExportFilters(), DAY, DAY)
        got = [r async for r in sp.rows()]
        assert got and {r[1] for r in got} == {"EMP001"}, name
        assert await sp.count() == len(got), name
        everyone = builder(db, export_svc.ExportScope.everyone(), export_svc.ExportFilters(), DAY, DAY)
        assert len([r async for r in everyone.rows()]) == 4, name
    empty = export_svc.ExportScope.team(set())  # a manager without reports sees nothing, not everything
    for name, builder in export_svc.SHEETS.items():
        assert await builder(db, empty, export_svc.ExportFilters(), DAY, DAY).count() == 0, name


async def _team(db, seeded):
    from app.models import User

    mgr = seeded["users"]["manager"]
    return set((await db.execute(select(User.id).where(User.manager_id == mgr))).scalars())


async def test_every_registered_type_as_manager_and_hr_over_http(client, H, clock, db, seeded, redis):
    await seed_food(db, seeded)
    await seed_all(db, seeded)
    for name in export_svc.SHEETS:
        await redis.flushall()  # the per-user export limit (5/min) is not under test here; there are more types than that
        p = {"type": name, "from": "2026-10-23", "to": "2026-10-23"}
        r = await client.get("/api/admin/export", headers=await H("hr"), params=p)
        assert r.status_code == 200, (name, r.text)
        ws = load_workbook(io.BytesIO(r.content)).worksheets[0]
        assert ws.max_row - 1 == 4, name  # HR sees all four seeded rows
        r = await client.get("/api/admin/export", headers=await H("manager"), params=p)
        needs = export_svc.TYPE_PERMISSIONS[name]
        mgr_has = any(
            code in {"attendance.view_team", "late.view_team", "request.view_team"} for code in needs
        )  # manager perms from the catalogue
        if mgr_has:
            assert r.status_code == 200, (name, r.text)
            ws = load_workbook(io.BytesIO(r.content)).worksheets[0]
            assert {row[1].value for row in ws.iter_rows(min_row=2)} <= {"EMP001"}, name
        else:
            assert r.status_code == 403, (name, r.status_code)
        assert (await client.get("/api/admin/export", headers=await H("employee"), params=p)).status_code == 403


async def test_unregistered_type_is_rejected_cleanly(client, H, clock, monkeypatch):
    monkeypatch.delitem(export_svc.SHEETS, "late")
    for t in ("all", "late"):
        r = await client.get(
            "/api/admin/export", headers=await H("hr"), params={"type": t, "from": "2026-10-23", "to": "2026-10-23"}
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR", t


async def test_team_user_cannot_filter_outside_team_and_unknown_ids_are_403(client, H, clock, db, seeded, monkeypatch):
    # give the manager a type they hold so the scope path is exercised (permission matrix is data, not code)
    monkeypatch.setitem(export_svc.TYPE_PERMISSIONS, "food", ("export.team",))
    await seed_food(db, seeded)
    p = {"type": "food", "from": "2026-10-23", "to": "2026-10-23"}
    h = await H("manager")
    r = await client.get("/api/admin/export", headers=h, params=p)
    assert r.status_code == 200
    ws = load_workbook(io.BytesIO(r.content)).worksheets[0]
    assert [row[1].value for row in ws.iter_rows(min_row=2)] == ["EMP001"]
    other = await client.get("/api/admin/export", headers=h, params={**p, "employee_id": "ADM001"})
    assert other.status_code == 403
    assert (await client.get("/api/admin/export", headers=h, params={**p, "employee_id": "EMP001"})).status_code == 200
    # review 08 L2: unknown ids are indistinguishable from out-of-scope ones for a team-scoped caller
    unknown_emp = await client.get("/api/admin/export", headers=h, params={**p, "employee_id": "ZZZ"})
    unknown_dept = await client.get("/api/admin/export", headers=h, params={**p, "department_id": str(uuid.uuid4())})
    assert unknown_emp.status_code == unknown_dept.status_code == other.status_code == 403
    assert unknown_emp.json()["error"]["message"] == other.json()["error"]["message"] == unknown_dept.json()["error"]["message"]


# ---------------------------------------------------------------- L6: row counts in audit
async def test_export_audit_has_row_counts(client, H, clock, db, seeded):
    await seed_food(db, seeded)
    r = await client.get(
        "/api/admin/export", headers=await H("hr"), params={"type": "food", "from": "2026-10-23", "to": "2026-10-23"}
    )
    assert r.status_code == 200
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "export.run"))).scalars().one()
    assert log.after["rows"] == {"Food": 4} and log.after["total_rows"] == 4 and log.after["scope"] == "all"


async def test_empty_result_is_header_only(client, H, clock, db, seeded):
    r = await client.get(
        "/api/admin/export",
        headers=await H("hr"),
        params={"type": "food", "from": "2026-10-23", "to": "2026-10-23", "department_id": str(seeded["dept"])},
    )
    ws = load_workbook(io.BytesIO(r.content))["Food"]
    assert ws.max_row == 1


# ---------------------------------------------------------------- M4 / M5: settings
async def put_settings(client, h, payload: dict | str):
    """A str payload is sent as the raw JSON body (to include NaN / Infinity literals)."""
    if isinstance(payload, str):
        return await client.put("/api/admin/settings", headers={**h, "Content-Type": "application/json"}, content=payload)
    return await client.put("/api/admin/settings", headers=h, json={"settings": payload})


def test_every_known_setting_has_a_valid_spec_and_its_default_passes():
    assert set(settings_svc.KNOWN) == set(settings_svc.SPECS)
    for k, default in settings_svc.KNOWN.items():
        assert settings_svc.validate(k, default) == default, k


@pytest.mark.parametrize(
    "bad",
    [
        {"office.radius_m": 1e9},
        {"office.radius_m": -5},
        {"office.radius_m": 150.5},
        {"office.radius_m": True},
        {"office.radius_m": "150"},
        {"office.lat": 91},
        {"office.lng": -181},
        {"attendance.location_retention_days": 0},
        {"attendance.location_retention_days": -1},
        {"attendance.selfie_retention_days": 0},
        {"attendance.max_fix_age_s": 100000},
        {"attendance.max_accuracy_m": 0.01},
        {"attendance.max_accuracy_m": 1},
        {"attendance.half_day_minutes": 0},
        {"permission.monthly_minutes_limit": -1},
        {"leave.max_advance_days": -1},
        {"attendance.reject_mock_location": "false"},
        {"attendance.reject_mock_location": 0},
        {"office.timezone": 5},
    ],
)
async def test_settings_out_of_range_rejected(client, H, bad):
    r = await put_settings(client, await H("admin"), bad)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR", bad


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
async def test_settings_nan_and_infinity_rejected_with_422_not_500(client, H, literal):
    for key in ("office.radius_m", "office.lat", "attendance.max_accuracy_m"):
        r = await put_settings(client, await H("admin"), '{"settings": {"' + key + '": ' + literal + "}}")
        assert r.status_code == 422, (key, literal, r.text)


async def test_settings_valid_values_are_normalised_and_stored(client, H, db):
    r = await put_settings(
        client, await H("admin"), {"office.radius_m": 200.0, "office.lat": 13, "attendance.max_accuracy_m": 15}
    )
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert s["office.radius_m"] == 200 and isinstance(s["office.radius_m"], int)
    assert s["office.lat"] == 13.0 and s["attendance.max_accuracy_m"] == 15
    assert (await db.get(Setting, "office.radius_m")).value == 200


async def test_settings_audit_is_key_before_after_list_without_masking(client, H, db):
    r = await put_settings(client, await H("admin"), {"attendance.location_retention_days": 30, "attendance.max_accuracy_m": 60})
    assert r.status_code == 200
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "settings.update"))).scalars().one()
    changes = {c["key"]: c for c in log.after["changes"]}
    assert changes["attendance.location_retention_days"] == {
        "key": "attendance.location_retention_days",
        "before": 90,
        "after": 30,
    }
    assert changes["attendance.max_accuracy_m"]["before"] == 100 and changes["attendance.max_accuracy_m"]["after"] == 60
    assert "***" not in str(log.after)


async def test_settings_noop_writes_no_audit(client, H, db):
    r = await put_settings(client, await H("admin"), {"attendance.location_retention_days": 90})
    assert r.status_code == 200
    assert (await db.execute(select(AuditLog).where(AuditLog.action == "settings.update"))).scalars().all() == []


# ---------------------------------------------------------------- L2
async def test_cutoff_checked_again_at_commit_time(client, H, db, monkeypatch):
    """The request starts one second before the cut-off (2026-10-21 12:30Z) and 'commits' after it."""
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return (
            dt.datetime(2026, 10, 21, 12, 29, 59, tzinfo=dt.UTC)
            if calls["n"] == 1
            else dt.datetime(2026, 10, 21, 12, 30, 1, tzinfo=dt.UTC)
        )

    monkeypatch.setattr("app.services.food.utcnow", fake)
    r = await client.put("/api/food/enrollment", headers=await H("employee"), json={"date": "2026-10-22", "need_food": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "FOOD_CUTOFF_PASSED"
    assert (await db.execute(select(FoodEnrollment))).scalars().all() == []  # rolled back, not committed


async def test_bulk_redoes_when_a_cutoff_passes_midway(client, H, db, monkeypatch):
    seq = iter(
        [dt.datetime(2026, 10, 21, 12, 29, 59, tzinfo=dt.UTC)]  # item 1 validation (attempt 1)
        + [dt.datetime(2026, 10, 21, 12, 30, 1, tzinfo=dt.UTC)] * 50  # everything after: cut-off passed
    )
    monkeypatch.setattr("app.services.food.utcnow", lambda: next(seq))
    r = await client.put(
        "/api/food/enrollments", headers=await H("employee"), json={"items": [{"date": "2026-10-22", "need_food": True}]}
    )
    assert r.status_code == 200
    assert r.json()["items"] == [] and r.json()["failed"] == [{"date": "2026-10-22", "code": "FOOD_CUTOFF_PASSED"}]
    assert (await db.execute(select(FoodEnrollment))).scalars().all() == []


def test_cutoff_time_in_dst_gap_is_detected():
    assert fs.cutoff_time_nonexistent("02:30", "America/New_York") is True
    assert fs.cutoff_time_nonexistent("18:00", "America/New_York") is False
    assert fs.cutoff_time_nonexistent("02:30", "Asia/Kolkata") is False


async def test_settings_reject_cutoff_inside_dst_gap(client, H):
    r = await put_settings(client, await H("admin"), {"office.timezone": "America/New_York", "food.cutoff_time": "02:30"})
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "food.cutoff_time"
    r = await put_settings(client, await H("admin"), {"office.timezone": "America/New_York", "food.cutoff_time": "18:00"})
    assert r.status_code == 200
    r = await put_settings(client, await H("admin"), {"food.cutoff_time": "02:15"})  # stored timezone is used too
    assert r.status_code == 422


# ---------------------------------------------------------------- L1: extreme dates
@pytest.mark.parametrize("d", ["0001-01-01", "9999-12-31", "1900-01-01", "2400-01-01"])
async def test_extreme_dates_are_clean_422(client, H, clock, d):
    h = await H("employee")
    for params in ({"from": d}, {"to": d}, {"from": d, "to": d}):
        r = await client.get("/api/food/my-enrollments", headers=h, params=params)
        assert r.status_code == 422 and r.json()["error"]["code"] in ("INVALID_DATE", "INVALID_DATE_RANGE"), (params, r.text)
    r = await client.get("/api/admin/food/daily", headers=await H("hr"), params={"date": d})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_DATE"
    r = await client.get("/api/admin/export", headers=await H("hr"), params={"type": "food", "from": d, "to": d})
    assert r.status_code == 422 and r.json()["error"]["code"] in ("INVALID_DATE", "RANGE_TOO_LARGE")
    r = await client.put("/api/food/enrollment", headers=h, json={"date": d, "need_food": True})
    assert r.status_code in (409, 422) and r.status_code != 500


def test_compute_cutoff_overflow_is_an_app_error():
    from datetime import time
    from zoneinfo import ZoneInfo

    with pytest.raises(AppError) as e:
        fs.compute_cutoff(dt.date.min, "previous_day", time(18, 0), ZoneInfo("Asia/Kolkata"))
    assert e.value.code == "INVALID_DATE"


# ---------------------------------------------------------------- L3: sanitiser
def test_sanitizer_strips_xml_illegal_and_truncates():
    assert sanitize_cell("a￾b￿c") == "abc"
    assert sanitize_cell("x\ud800y") == "xy"
    assert sanitize_cell("\x00\x1f=1+1") == "'=1+1"
    assert len(sanitize_cell("a" * 40_000)) == xlsx.MAX_CELL_CHARS
    assert len(sanitize_cell("=" + "a" * 40_000)) == xlsx.MAX_CELL_CHARS + 1
    assert sanitize_cell("tab\there") == "tab\there"


async def test_text_cells_are_strings_even_if_they_look_like_errors_or_formulas(tmp_export_dir):
    async def count():
        return 3

    async def rows():
        yield ("#N/A", "=SUM(A1)", "ok￾")
        yield ("#REF!", "-5", "a" * 40_000)
        yield (None, "", "x")

    built = await xlsx.build_workbook([SheetSpec("S", ["a", "b", "c"], count, rows, 10)])
    ws = load_workbook(io.BytesIO(built.file.read()))["S"]
    built.close()
    assert ws["A2"].value == "#N/A" and ws["A2"].data_type == "s"
    assert ws["B2"].data_type == "s" and ws["B2"].value == "'=SUM(A1)"
    assert ws["C2"].value == "ok"
    assert len(ws["C3"].value) == xlsx.MAX_CELL_CHARS


# ---------------------------------------------------------------- L5 / L7: daily list
async def test_daily_include_all_is_paginated_and_view_is_audited(client, H, clock, db, seeded):
    await seed_food(db, seeded, roles=("employee", "admin"))
    h = await H("hr")
    r = await client.get("/api/admin/food/daily", headers=h, params={"date": "2026-10-23", "include": "all", "page_size": 2})
    d = r.json()
    assert r.status_code == 200 and len(d["employees"]) == 2 and d["total"] == 4 and d["page"] == 1 and d["page_size"] == 2
    assert d["count_yes"] == 2 and d["count_unanswered"] == 2  # aggregates cover everyone, not just the page
    r2 = await client.get(
        "/api/admin/food/daily", headers=h, params={"date": "2026-10-23", "include": "all", "page_size": 2, "page": 2}
    )
    ids = {e["user"]["employee_id"] for e in d["employees"]} | {e["user"]["employee_id"] for e in r2.json()["employees"]}
    assert len(ids) == 4
    assert (await client.get("/api/admin/food/daily", headers=h, params={"page_size": 100000})).status_code == 422
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "food.daily.view"))).scalars().all()
    assert len(logs) == 2 and logs[0].after["include"] == "all" and logs[0].actor_employee_id == "HR001"
    # no audit for denied callers
    await client.get("/api/admin/food/daily", headers=await H("employee"))
    assert len((await db.execute(select(AuditLog).where(AuditLog.action == "food.daily.view"))).scalars().all()) == 2
