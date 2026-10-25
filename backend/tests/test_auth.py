import pytest
from sqlalchemy import select

from app.models import AuditLog, User

from .conftest import PASSWORD, USERS, bearer, do_login, login_tokens


async def actions(db):
    return [a for (a,) in (await db.execute(select(AuditLog.action).order_by(AuditLog.id))).all()]


async def test_login_success_shape_and_audit(client, db):
    r = await do_login(client, "emp001", device_id="d1", device_name="Pixel")  # case-insensitive id
    assert r.status_code == 200
    b = r.json()
    assert b["token_type"] == "Bearer" and b["expires_in"] == 900 and b["must_change_password"] is False
    assert b["user"]["employee_id"] == "EMP001" and b["user"]["role"] == "employee"
    assert b["user"]["permissions"] == ["payroll.view_own"]  # employees only hold own-payslip access
    assert "password_hash" not in r.text
    assert "auth.login_success" in await actions(db)
    row = (await db.execute(select(AuditLog).where(AuditLog.action == "auth.login_success"))).scalar_one()
    assert row.request_id and row.actor_employee_id == "EMP001"


async def test_login_wrong_password_and_unknown_id_are_indistinguishable(client):
    bad_pw = await do_login(client, "EMP001", "Wrong!Pass1x")
    unknown = await do_login(client, "NOPE999", "Wrong!Pass1x")
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json()["error"]["code"] == unknown.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert bad_pw.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_error_format_has_request_id_and_details(client):
    r = await client.post("/api/auth/login", json={"employee_id": ""})
    err = r.json()["error"]
    assert r.status_code == 422 and err["code"] == "VALIDATION_ERROR" and err["request_id"]
    assert {d["field"] for d in err["details"]} >= {"employee_id", "password"}


async def test_inactive_user_reveals_status_only_with_correct_password(client, db):
    u = (await db.execute(select(User).where(User.employee_id == "EMP001"))).unique().scalar_one()
    u.status = "inactive"
    await db.commit()
    assert (await do_login(client, "EMP001", "Wrong!Pass1x")).json()["error"]["code"] == "INVALID_CREDENTIALS"
    r = await do_login(client, "EMP001")
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCOUNT_INACTIVE"


async def test_me_and_missing_or_bad_token(client):
    assert (await client.get("/api/auth/me")).json()["error"]["code"] == "UNAUTHORIZED"
    assert (await client.get("/api/auth/me", headers={"Authorization": "Bearer junk"})).status_code == 401
    t = await login_tokens(client, USERS["hr"])
    me = (await client.get("/api/auth/me", headers=bearer(t))).json()
    assert me["employee_id"] == "HR001" and me["role"] == "hr" and "employee.create" in me["permissions"]
    assert me["department"]["name"] == "General" and me["must_change_password"] is False


async def test_deactivated_user_blocked_with_still_valid_access_token(client, db):
    t = await login_tokens(client, "EMP001")
    assert (await client.get("/api/auth/me", headers=bearer(t))).status_code == 200
    u = (await db.execute(select(User).where(User.employee_id == "EMP001"))).unique().scalar_one()
    u.status = "inactive"
    await db.commit()
    r = await client.get("/api/auth/me", headers=bearer(t))
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCOUNT_INACTIVE"


# ---------------------------------------------------------------- forced password change
@pytest.fixture
async def forced(client, db):
    u = (await db.execute(select(User).where(User.employee_id == "EMP001"))).unique().scalar_one()
    u.must_change_password = True
    await db.commit()
    return await login_tokens(client, "EMP001")


async def test_forced_change_gate_blocks_everything_but_allowed_endpoints(client, forced):
    assert forced["must_change_password"] is True
    h = bearer(forced)
    for method, path in [
        ("get", "/api/config"),
        ("get", "/api/admin/departments"),
        ("post", "/api/auth/logout-all"),
        ("get", "/api/admin/employees"),
    ]:
        r = await getattr(client, method)(path, headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED", path
    assert (await client.get("/api/auth/me", headers=h)).status_code == 200
    r = await client.post("/api/auth/logout", headers=h, json={"refresh_token": forced["refresh_token"]})
    assert r.status_code == 204


async def test_change_password_success_clears_flag_and_revokes_old_sessions(client, forced, db):
    other = await login_tokens(client, "EMP001")  # second device
    new_pw = "N3w!Valid-Pass99"
    r = await client.post(
        "/api/auth/change-password", headers=bearer(forced), json={"current_password": PASSWORD, "new_password": new_pw}
    )
    assert r.status_code == 200
    b = r.json()
    assert b["must_change_password"] is False and b["refresh_token"] != forced["refresh_token"]
    assert (await client.get("/api/config", headers=bearer(b))).status_code == 200
    for old in (forced, other):  # every old refresh token is dead
        rr = await client.post("/api/auth/refresh", json={"refresh_token": old["refresh_token"]})
        assert rr.status_code == 401
    assert (await do_login(client, "EMP001", PASSWORD)).status_code == 401
    assert (await do_login(client, "EMP001", new_pw)).status_code == 200
    assert "auth.password_changed" in await actions(db)


@pytest.mark.parametrize(
    "new_pw,status,code",
    [
        (PASSWORD, 422, "PASSWORD_REUSED"),
        ("weak", 422, "VALIDATION_ERROR"),
        ("Password1!", 422, "VALIDATION_ERROR"),
        ("Xx!EMP001-aaa1", 422, "VALIDATION_ERROR"),
    ],
)
async def test_change_password_rejections(client, forced, new_pw, status, code):
    r = await client.post(
        "/api/auth/change-password", headers=bearer(forced), json={"current_password": PASSWORD, "new_password": new_pw}
    )
    assert r.status_code == status and r.json()["error"]["code"] == code
    if code == "VALIDATION_ERROR":
        assert r.json()["error"]["details"]


async def test_change_password_wrong_current_counts_toward_lockout(client, forced):
    h = bearer(forced)
    for i in range(5):
        r = await client.post(
            "/api/auth/change-password", headers=h, json={"current_password": "Wrong!Pass1x", "new_password": "N3w!Valid-Pass99"}
        )
        # distinct 400 (never 401, which clients refresh+replay); the attempt that locks returns 423
        assert r.status_code == (423 if i == 4 else 400)
        if i < 4:
            assert r.json()["error"]["code"] == "CURRENT_PASSWORD_INVALID"
    assert (await do_login(client, "EMP001", "Wrong!Pass1x")).status_code == 401  # lock not revealed without the password
    assert (await do_login(client, "EMP001")).status_code == 423


async def test_forgot_password_always_202_and_creates_single_request(client, db):
    from app.models import PasswordResetRequest

    for eid in ("EMP001", "EMP001", "NOBODY"):
        r = await client.post("/api/auth/forgot-password", json={"employee_id": eid})
        assert r.status_code == 202 and r.json() == {"message": "If the account exists, a request was sent."}
    assert len((await db.execute(select(PasswordResetRequest))).scalars().all()) == 1


async def test_healthz_and_security_headers(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff" and r.headers["x-request-id"]
    assert (await client.get("/api/nope")).json()["error"]["code"] == "NOT_FOUND"
