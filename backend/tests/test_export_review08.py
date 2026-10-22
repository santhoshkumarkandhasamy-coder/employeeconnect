"""Review 08 backend fixes: M2 (export.sensitive), M3 (daily quota), L1 (audited refusals), L3 (team window), L4 (semaphore /
counts under the timeout / pending cap), L5 (EXPORT_TEMP_DIR in prod, safe sweep)."""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import os
import time

import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.models import AuditLog, Role, User
from app.services import export as export_svc
from app.services import xlsx
from app.services.xlsx import SheetSpec
from tests.conftest import bearer
from tests.test_export_full import (
    _REDIS,
    P,
    _redis_handle,  # noqa: F401 - autouse fixture of the helper module (fills _REDIS)
    book,
    get,
    ids,
    login_as,
    make_user,
    role_with,
    seed_all,
)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(export_svc, "today", lambda: dt.date(2026, 10, 22))


@pytest.fixture
async def H(tokens_for):
    cache: dict = {}

    async def _h(role):
        if role not in cache:
            cache[role] = bearer(await tokens_for(role))
        return cache[role]

    return _h


def failures(rows):
    return [lg for lg in rows if lg.outcome == "failure"]


async def audit_rows(db):
    return (await db.execute(select(AuditLog).where(AuditLog.action == "export.run").order_by(AuditLog.id))).scalars().all()


# ---------------------------------------------------------------- M2
async def test_include_reasons_needs_export_sensitive_and_is_audited(client, H, db, seeded):
    await seed_all(db, seeded)
    role = await role_with(db, "NOSENS", ["export.all", "export.team", "request.view_all"])
    await make_user(db, seeded, "UNOSENS", role.id)
    await db.commit()
    for h in (await H("manager"), await login_as(client, "UNOSENS")):
        r = await get(client, h, type="leave", include_reasons="true")
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
        assert (await get(client, h, type="leave")).status_code == 200  # the same caller is fine without the flag
    assert (await get(client, await H("hr"), type="late", include_reasons="true")).status_code == 200
    assert (await get(client, await H("admin"), type="leave", include_reasons="true")).status_code == 200
    rows = await audit_rows(db)
    ok = {(lg.actor_employee_id, lg.after["type"]): lg.after["include_reasons"] for lg in rows if lg.outcome == "success"}
    assert ok[("HR001", "late")] is False  # no reason columns on that sheet: the audit row does not claim otherwise
    assert ok[("ADM001", "leave")] is True and ok[("UNOSENS", "leave")] is False and ok[("MGR001", "leave")] is False
    denied = failures(rows)
    assert len(denied) == 2 and all(lg.after["result"] == "denied" and lg.after["include_reasons"] is True for lg in denied)


async def test_default_workbook_has_no_reason_text_even_for_hr(client, H, db, seeded):
    await seed_all(db, seeded)
    wb = book(await get(client, await H("hr"), type="all"))
    blob = repr([[c.value for c in row] for ws in wb.worksheets for row in ws.iter_rows()])
    assert "family event" not in blob and "Reason" not in blob and "Comment" not in blob


async def test_export_sensitive_is_held_by_hr_and_admin_only(seeded, db):
    for name, expected in (("hr", True), ("admin", True), ("manager", False), ("employee", False)):
        role = (await db.execute(select(Role).where(Role.name == name))).scalar_one()
        assert ("export.sensitive" in {p.code for p in role.permissions}) is expected, name


def test_migration_0015_and_schema_sql_grant_export_sensitive():
    root = os.path.join(os.path.dirname(__file__), "..", "..", "database")
    path = os.path.join(root, "alembic", "versions", "0015_export_sensitive_permission.py")
    import sys

    sys.path.insert(0, root)
    try:
        spec = importlib.util.spec_from_file_location("mig0015", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(root)
    assert (mod.revision, mod.down_revision) == ("0015", "0014")
    assert "export.sensitive" in mod.UPGRADE and "'admin', 'hr'" in mod.UPGRADE and "export.sensitive" in mod.DOWNGRADE
    sql = open(os.path.join(root, "schema.sql"), encoding="utf-8").read()
    assert "export.sensitive" in sql and "version_num='0015'" in sql


# ---------------------------------------------------------------- L1
async def test_denied_and_failed_attempts_are_audited(client, H, db, seeded):
    await seed_all(db, seeded)
    h = await H("manager")
    await get(client, h, type="leave", employee_id="ADM001")  # out of scope
    await get(client, h, type="food")  # type permission
    r = await client.get(
        "/api/admin/export", headers=await H("hr"), params={"type": "all", "from": "2026-01-01", "to": "2027-06-01"}
    )
    assert r.status_code == 422
    for _ in range(6):
        r = await get(client, await H("hr"), reset_limit=False, type="late")
    assert r.status_code == 429
    rows = failures(await audit_rows(db))
    got = {(lg.after["code"], lg.after["result"]) for lg in rows}
    assert {("FORBIDDEN", "denied"), ("RANGE_TOO_LARGE", "failed"), ("RATE_LIMITED", "failed")} <= got
    scope_row = next(lg for lg in rows if lg.after.get("employee_id") == "ADM001")
    assert scope_row.actor_employee_id == "MGR001" and scope_row.after["type"] == "leave"
    assert scope_row.after["from"] == P["from"] and scope_row.after["status"] == 403


async def test_audit_does_not_store_a_typed_password_as_employee_id(client, H, db, seeded):
    r = await get(client, await H("hr"), type="late", employee_id="not an id!!")
    assert r.status_code == 404
    row = failures(await audit_rows(db))[0]
    assert row.after["employee_id"].startswith("sha256:") and "not an id" not in repr(row.after)


# ---------------------------------------------------------------- M3
async def test_daily_export_count_quota_and_refund(client, H, db, seeded, monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "export_daily_max_exports", 3)
    monkeypatch.setattr(get_settings(), "export_daily_warn_exports", 2)
    caplog.set_level("WARNING", logger="app.export")
    h = await H("hr")
    for _ in range(3):
        assert (await get(client, h, type="late")).status_code == 200
    assert "exports today" in caplog.text
    r = await get(client, h, type="late")
    assert r.status_code == 429 and r.json()["error"]["code"] == "EXPORT_QUOTA_EXCEEDED" and int(r.headers["Retry-After"]) > 0
    assert (await get(client, await H("manager"), type="late")).status_code == 200  # per user
    monkeypatch.setattr(get_settings(), "export_daily_max_exports", 4)
    bad = await client.get("/api/admin/export", headers=h, params={"type": "all", "from": "2026-01-01", "to": "2027-06-01"})
    assert bad.status_code == 422  # refused: the count is given back
    assert (await get(client, h, type="late")).status_code == 200
    assert (await get(client, h, type="late")).status_code == 429
    last = [lg for lg in await audit_rows(db) if lg.outcome == "success" and lg.actor_employee_id == "HR001"][-1]
    assert last.after["daily_exports"] == 4


async def test_daily_row_quota_and_large_export_warning(client, H, db, seeded, monkeypatch, caplog):
    await seed_all(db, seeded)
    monkeypatch.setattr(get_settings(), "export_daily_max_rows", 1000)
    monkeypatch.setattr(get_settings(), "export_warn_rows", 4)
    monkeypatch.setattr(get_settings(), "export_daily_warn_rows", 4)
    caplog.set_level("WARNING", logger="app.export")
    h = await H("hr")
    assert (await get(client, h, type="attendance")).status_code == 200  # 4 rows
    assert "large export" in caplog.text and "rows today" in caplog.text
    key = (await _REDIS[0].keys("quota:export:*:rows"))[0]
    await _REDIS[0].set(key, 998)  # the next 4 rows would pass the ceiling: refused, rows given back, nothing delivered
    r = await get(client, h, type="attendance")
    assert r.status_code == 429 and r.json()["error"]["code"] == "EXPORT_QUOTA_EXCEEDED"
    assert int(await _REDIS[0].get(key)) == 998
    await _REDIS[0].set(key, 1000)  # already used up: refused before building
    assert (await get(client, h, type="attendance")).status_code == 429


async def test_quota_fails_closed_when_redis_breaks(monkeypatch):
    from app.services import export_quota

    class Broken:
        async def get(self, *a):
            raise ConnectionError

    import uuid

    with pytest.raises(AppError) as e:
        await export_quota.reserve(Broken(), uuid.uuid4())
    assert e.value.status == 503


# ---------------------------------------------------------------- L3
async def test_team_scope_clamps_from_and_excludes_deleted_users(client, H, db, seeded, monkeypatch):
    await seed_all(db, seeded)
    h = await H("manager")
    wide = {"from": "2026-10-01", "to": "2026-12-31"}
    monkeypatch.setattr(export_svc, "today", lambda: dt.date(2027, 10, 24))  # the seeded day is 12 months + 1 day back
    assert book(await get(client, h, type="attendance", **wide))["Attendance"].max_row == 1  # clamped away
    assert book(await get(client, await H("hr"), type="attendance", **wide))["Attendance"].max_row == 5  # export.all: never
    monkeypatch.setattr(export_svc, "today", lambda: dt.date(2027, 10, 23))  # exactly 12 months: still inside
    assert ids(book(await get(client, h, type="attendance", **wide))["Attendance"]) == ["EMP001"]
    monkeypatch.setattr(export_svc, "today", lambda: dt.date(2028, 10, 23))
    monkeypatch.setattr(get_settings(), "export_team_window_months", 0)  # setting: 0 switches the clamp off
    assert ids(book(await get(client, h, type="attendance", **wide))["Attendance"]) == ["EMP001"]
    monkeypatch.setattr(get_settings(), "export_team_window_months", 12)
    monkeypatch.setattr(export_svc, "today", lambda: dt.date(2026, 10, 22))
    emp = await db.get(User, seeded["users"]["employee"])
    emp.deleted_at = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    await db.commit()
    assert book(await get(client, h, type="attendance"))["Attendance"].max_row == 1  # a soft-deleted report is not exported
    assert (await get(client, h, type="attendance", employee_id="EMP001")).status_code == 403
    row = [lg for lg in await audit_rows(db) if lg.outcome == "success" and lg.actor_employee_id == "MGR001"][-1]
    assert row.after["effective_from"] == "2026-10-23" and row.after["from"] == "2026-10-23"


def test_months_back_handles_month_ends():
    f = export_svc._months_back
    assert f(dt.date(2026, 10, 22), 12) == dt.date(2025, 10, 22)
    assert f(dt.date(2026, 3, 31), 1) == dt.date(2026, 2, 28)
    assert f(dt.date(2026, 1, 15), 2) == dt.date(2025, 11, 15)
    assert f(dt.date(2024, 2, 29), 12) == dt.date(2023, 2, 28)


# ---------------------------------------------------------------- L4
def _spec(n=3, count=None, title="T"):
    async def cnt():
        return n

    async def rows():
        for i in range(n):
            yield (i, f"n{i}", dt.date(2026, 1, 1))

    return SheetSpec(title, ["A", "B", "C"], count or cnt, rows, 1000)


async def test_semaphore_is_taken_before_the_count_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "export_temp_dir", str(tmp_path))
    monkeypatch.setattr(get_settings(), "export_max_concurrency", 1)
    monkeypatch.setattr(xlsx, "SLOT_WAIT_S", 0.05)
    started = {"n": 0}
    gate = asyncio.Event()

    async def slow_count():
        started["n"] += 1
        await gate.wait()
        return 1

    first = asyncio.create_task(xlsx.build_workbook([_spec(count=slow_count)]))
    await asyncio.sleep(0.05)
    with pytest.raises(AppError) as e:  # waits for the slot; its count query must NOT run meanwhile
        await xlsx.build_workbook([_spec(count=slow_count)])
    assert e.value.status == 503 and started["n"] == 1
    gate.set()
    (await first).close()
    assert xlsx.pending_downloads() == 0


async def test_slow_count_is_covered_by_the_timeout_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "export_temp_dir", str(tmp_path))
    monkeypatch.setattr(get_settings(), "export_timeout_s", 0.2)

    async def hang():
        await asyncio.sleep(5)
        return 1

    t0 = time.monotonic()
    with pytest.raises(OverflowError):
        await xlsx.build_workbook([_spec(count=hang)])
    assert time.monotonic() - t0 < 2
    assert xlsx.pending_downloads() == 0 and list(tmp_path.iterdir()) == []


async def test_undrained_downloads_are_capped_and_released_on_close(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "export_temp_dir", str(tmp_path))
    monkeypatch.setattr(get_settings(), "export_max_pending_downloads", 2)
    a = await xlsx.build_workbook([_spec()])
    b = await xlsx.build_workbook([_spec()])
    with pytest.raises(AppError) as e:
        await xlsx.build_workbook([_spec()])
    assert e.value.status == 503 and e.value.headers["Retry-After"]
    a.close()
    c = await xlsx.build_workbook([_spec()])  # a slot is free again
    for built in (b, c):
        built.close()
    assert xlsx.pending_downloads() == 0
    # a refused build (over the row cap) does not leak its slot either
    with pytest.raises(OverflowError):
        await xlsx.build_workbook([_spec(n=5000)])
    assert xlsx.pending_downloads() == 0


async def test_abandoned_slots_are_reclaimed_after_the_max_age(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "export_temp_dir", str(tmp_path))
    monkeypatch.setattr(get_settings(), "export_max_pending_downloads", 1)
    monkeypatch.setattr(xlsx, "PENDING_MAX_AGE_S", 0.05)
    a = await xlsx.build_workbook([_spec()])  # never closed (client vanished)
    await asyncio.sleep(0.1)
    b = await xlsx.build_workbook([_spec()])
    a.close()
    b.close()


# ---------------------------------------------------------------- L5
_BASE = {
    "DATABASE_URL": "sqlite+aiosqlite://",
    "JWT_SECRET": "x" * 40,
    "DATA_ENCRYPTION_KEYS": "k",
}


def _settings(monkeypatch, **extra):
    for k in ("EXPORT_TEMP_DIR", "CORS_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in {**_BASE, **extra}.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_export_temp_dir_is_required_in_prod(monkeypatch):
    with pytest.raises(ValueError, match="EXPORT_TEMP_DIR"):
        _settings(monkeypatch, APP_ENV="prod", JWT_SECRET="q9" * 24)
    with pytest.raises(ValueError, match="EXPORT_TEMP_DIR"):
        _settings(monkeypatch, APP_ENV="prod", JWT_SECRET="q9" * 24, EXPORT_TEMP_DIR="relative/dir")
    s = _settings(monkeypatch, APP_ENV="prod", JWT_SECRET="q9" * 24, EXPORT_TEMP_DIR="/exports")
    assert s.export_temp_dir == "/exports"
    assert _settings(monkeypatch, APP_ENV="test").export_temp_dir is None  # only prod insists


def _old(path, age=7200):
    os.utime(path, (time.time() - age, time.time() - age))


def test_sweep_only_removes_safe_directories(tmp_path):
    me = os.getpid()
    mk = lambda name: (tmp_path / name).mkdir() or tmp_path / name  # noqa: E731
    mine_old = mk(f"{xlsx.DIR_PREFIX}{me}-old")
    mine_fresh = mk(f"{xlsx.DIR_PREFIX}{me}-fresh")
    mine_active = mk(f"{xlsx.DIR_PREFIX}{me}-active")
    legacy_old = mk(f"{xlsx.DIR_PREFIX}legacy")
    dead = mk(f"{xlsx.DIR_PREFIX}999999999-dead")  # no such process
    other = mk("unrelated")
    for d in (mine_old, mine_active, legacy_old, other):
        _old(d)
    xlsx._register(mine_active)
    try:
        removed = xlsx.sweep_stale(str(tmp_path))
    finally:
        xlsx._unregister(mine_active)
    assert removed == (3 if os.name != "nt" else 2)  # nt: PID liveness is unknown, so the "dead" dir is kept
    assert not mine_old.exists() and not legacy_old.exists()
    assert mine_fresh.exists() and mine_active.exists() and other.exists()  # live / unrelated: untouched
    if os.name != "nt":
        assert not dead.exists()


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks / POSIX PIDs")
def test_sweep_ignores_symlinks_and_live_foreign_pids(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    link = tmp_path / f"{xlsx.DIR_PREFIX}legacy-link"
    link.symlink_to(victim)
    _old(victim)
    foreign = tmp_path / f"{xlsx.DIR_PREFIX}1-live"  # PID 1 is alive and is not us
    foreign.mkdir()
    _old(foreign)  # 2 h old is not enough for a live foreign pid (needs a day)
    assert xlsx.sweep_stale(str(tmp_path)) == 0
    assert victim.exists() and foreign.exists()


async def test_build_directory_is_private_and_named_after_the_pid(tmp_path, monkeypatch):
    base = tmp_path / "exp"
    monkeypatch.setattr(get_settings(), "export_temp_dir", str(base))
    built = await xlsx.build_workbook([_spec()])
    (d,) = list(base.iterdir())
    assert d.name.startswith(f"{xlsx.DIR_PREFIX}{os.getpid()}-")
    if os.name != "nt":
        assert (d.stat().st_mode & 0o777) == 0o700 and (base.stat().st_mode & 0o777) == 0o700
    built.close()
    assert list(base.iterdir()) == []
