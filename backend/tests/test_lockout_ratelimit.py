import datetime as dt

from sqlalchemy import select

from app.models import AuditLog, User

from .conftest import PASSWORD, bearer, do_login, login_tokens

BAD = "Wrong!Pass1x"


async def user(db, eid):
    await db.rollback()  # drop any stale snapshot
    return (
        (await db.execute(select(User).where(User.employee_id == eid).execution_options(populate_existing=True)))
        .unique()
        .scalar_one()
    )


async def test_lockout_on_fifth_failure(client, db):
    for i in range(4):
        r = await do_login(client, "EMP001", BAD)
        assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS", i
    r = await do_login(client, "EMP001", BAD)  # 5th locks, but a wrong password never reveals the lock (no enumeration)
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    r = await do_login(client, "EMP001", PASSWORD)  # the correct password reveals it, with the countdown
    assert r.status_code == 423
    err = r.json()["error"]
    assert err["code"] == "ACCOUNT_LOCKED" and err["details"][0]["field"] == "locked_until"
    assert err["details"][0]["message"].endswith("Z")
    u = await user(db, "EMP001")
    assert u.failed_login_count == 5 and u.locked_until is not None
    acts = [a for (a,) in (await db.execute(select(AuditLog.action))).all()]
    assert acts.count("auth.login_failed") == 6 and "auth.account_locked" in acts


async def test_locked_account_rejects_even_correct_password_without_incrementing(client, db):
    for _ in range(5):
        await do_login(client, "EMP001", BAD)
    r = await do_login(client, "EMP001", PASSWORD)
    assert r.status_code == 423
    assert (await user(db, "EMP001")).failed_login_count == 5


async def test_lockout_cannot_be_bypassed_with_case_or_whitespace_variants(client):
    for variant in ["emp001", " EMP001 ", "Emp001", "eMp001", "EMP001"]:
        await do_login(client, variant, BAD)
    assert (await do_login(client, "emp001", PASSWORD)).status_code == 423
    assert (await do_login(client, "emp001", BAD)).status_code == 401


async def test_auto_unlock_after_lock_expires(client, db):
    for _ in range(5):
        await do_login(client, "EMP001", BAD)
    u = await user(db, "EMP001")
    u.locked_until = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    await db.commit()
    r = await do_login(client, "EMP001", PASSWORD)
    assert r.status_code == 200
    u = await user(db, "EMP001")
    assert u.failed_login_count == 0 and u.locked_until is None


async def test_failed_attempt_after_expiry_starts_counter_fresh(client, db):
    for _ in range(5):
        await do_login(client, "EMP001", BAD)
    u = await user(db, "EMP001")
    u.locked_until = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    await db.commit()
    assert (await do_login(client, "EMP001", BAD)).status_code == 401
    assert (await user(db, "EMP001")).failed_login_count == 1


async def test_success_resets_counter(client, db):
    for _ in range(3):
        await do_login(client, "EMP001", BAD)
    assert (await do_login(client, "EMP001")).status_code == 200
    assert (await user(db, "EMP001")).failed_login_count == 0


async def test_admin_unlock_clears_lock_immediately(client, db):
    for _ in range(5):
        await do_login(client, "EMP001", BAD)
    admin = await login_tokens(client, "ADM001")
    uid = str((await user(db, "EMP001")).id)
    r = await client.post(f"/api/admin/employees/{uid}/unlock", headers=bearer(admin))
    assert r.status_code == 200 and r.json()["is_locked"] is False
    assert (await do_login(client, "EMP001")).status_code == 200
    assert "employee.unlock" in [a for (a,) in (await db.execute(select(AuditLog.action))).all()]


async def test_login_rate_limit_per_employee_id_returns_429_with_retry_after(client):
    # LOGIN_RATE_EID default 10 per window; use unknown ids so lockout does not interfere
    codes = [(await do_login(client, "GHOST1", BAD)).status_code for _ in range(10)]
    assert set(codes) == {401}
    r = await do_login(client, "GHOST1", BAD)
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    assert int(r.headers["retry-after"]) > 0
    assert (await do_login(client, "GHOST2", BAD)).status_code == 401  # other ids unaffected


async def test_login_rate_limit_per_ip(client):
    # 20 attempts per IP per window across different ids
    for i in range(20):
        assert (await do_login(client, f"X{i}", BAD)).status_code == 401
    assert (await do_login(client, "X99", BAD)).status_code == 429


async def test_redis_down_fails_open_and_db_lockout_still_works(app, client, db):
    from app.core.redis import get_redis

    class DeadRedis:
        def pipeline(self):
            raise ConnectionError("redis down")

    async def _dead():
        return DeadRedis()

    app.dependency_overrides[get_redis] = _dead
    assert (await do_login(client, "EMP001")).status_code == 200  # login works
    for _ in range(5):
        await do_login(client, "HR001", BAD)
    assert (await do_login(client, "HR001")).status_code == 423  # DB lockout intact (correct password reveals it)
