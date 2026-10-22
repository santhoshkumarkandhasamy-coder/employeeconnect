"""Auth/security review 01 fixes: proxy trust, off-loop hashing, access-token invalidation, refresh edge cases,
permission-based admin guards, soft delete, reset-request queue, audit redaction, break-glass CLI."""

import datetime as dt
import ipaddress
import threading

import pytest
from sqlalchemy import select

from app.core import net, security
from app.core.audit import redact, safe_eid
from app.core.config import get_settings
from app.models import AuditLog, PasswordResetRequest, RefreshToken, User

from .conftest import PASSWORD, bearer, do_login, login_tokens

BAD = "Wrong!Pass1x"


# ------------------------------------------------------------------ H1 proxy trust
def _nets(*c):
    return [ipaddress.ip_network(x) for x in c]


def test_resolve_ip_ignores_forwarded_headers_from_untrusted_peer():
    assert net.resolve_client_ip("203.0.113.9", "1.2.3.4", "5.6.7.8", _nets("10.0.0.0/8")) == "203.0.113.9"
    assert net.resolve_client_ip("203.0.113.9", "1.2.3.4", None, []) == "203.0.113.9"


def test_resolve_ip_trusted_proxy_uses_real_ip_then_xff_rightmost_untrusted_and_validates():
    t = _nets("10.0.0.0/8")
    assert net.resolve_client_ip("10.0.0.2", "198.51.100.7", None, t) == "198.51.100.7"
    assert net.resolve_client_ip("10.0.0.2", None, "9.9.9.9, 198.51.100.7, 10.0.0.5", t) == "198.51.100.7"
    assert net.resolve_client_ip("10.0.0.2", "not-an-ip", None, t) == "10.0.0.2"
    assert net.resolve_client_ip("10.0.0.2", None, "garbage", t) == "10.0.0.2"


def test_rate_limit_key_collapses_ipv6_to_64():
    assert net.rate_limit_key("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == net.rate_limit_key("2001:db8:1:2::1")
    assert net.rate_limit_key("203.0.113.9") == "203.0.113.9"


async def test_spoofed_x_real_ip_cannot_bypass_ip_rate_limit_without_trusted_proxy(client):
    for i in range(20):
        r = await client.post(
            "/api/auth/login", json={"employee_id": f"X{i}", "password": BAD}, headers={"X-Real-IP": f"198.51.100.{i}"}
        )
        assert r.status_code == 401
    r = await client.post(
        "/api/auth/login", json={"employee_id": "X99", "password": BAD}, headers={"X-Real-IP": "198.51.100.200"}
    )
    assert r.status_code == 429


async def test_trusted_proxy_header_is_honoured_when_peer_is_in_trusted_proxies(client, monkeypatch, db):
    monkeypatch.setattr(get_settings(), "trusted_proxies", "127.0.0.0/8")
    await client.post("/api/auth/login", json={"employee_id": "EMP001", "password": BAD}, headers={"X-Real-IP": "198.51.100.7"})
    ip = (await db.execute(select(AuditLog.ip).where(AuditLog.action == "auth.login_failed"))).scalar_one()
    assert str(ip) == "198.51.100.7"


def test_invalid_trusted_proxies_rejected_at_startup():
    from app.core.config import Settings

    with pytest.raises(ValueError):
        Settings(trusted_proxies="not-a-cidr", database_url="sqlite+aiosqlite://", jwt_secret="x" * 40, data_encryption_keys="k")


async def test_request_id_is_validated(client):
    r = await client.get("/healthz", headers={"X-Request-ID": "evil\r\nvalue with spaces"})
    assert r.headers["x-request-id"] != "evil\r\nvalue with spaces" and len(r.headers["x-request-id"]) == 32
    ok = await client.get("/healthz", headers={"X-Request-ID": "abc-12345678"})
    assert ok.headers["x-request-id"] == "abc-12345678"


# ------------------------------------------------------------------ M2 hashing off the event loop
async def test_argon2_runs_in_worker_thread(monkeypatch):
    seen = []
    real = security.verify_password

    def spy(h, p):
        seen.append(threading.get_ident())
        return real(h, p)

    monkeypatch.setattr(security, "verify_password", spy)
    h = security.hash_password("Abc!defg1")
    assert await security.averify_password(h, "Abc!defg1") is True
    await security.averify_dummy("x")
    assert seen and all(t != threading.get_ident() for t in seen)


# ------------------------------------------------------------------ M3 access token invalidation
async def test_access_token_dies_on_logout(client):
    t = await login_tokens(client, "EMP001")
    assert (await client.get("/api/auth/me", headers=bearer(t))).status_code == 200
    await client.post("/api/auth/logout", headers=bearer(t), json={"refresh_token": t["refresh_token"]})
    r = await client.get("/api/auth/me", headers=bearer(t))
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED"


async def test_logout_all_kills_access_tokens_of_other_devices_and_new_login_works(client):
    a, b = await login_tokens(client, "EMP001"), await login_tokens(client, "EMP001")
    assert (await client.post("/api/auth/logout-all", headers=bearer(a))).status_code == 204
    assert (await client.get("/api/auth/me", headers=bearer(b))).status_code == 401
    fresh = await login_tokens(client, "EMP001")  # minted in the same second: must be accepted
    assert (await client.get("/api/auth/me", headers=bearer(fresh))).status_code == 200


async def test_password_change_invalidates_old_access_tokens_but_new_pair_works(client):
    old = await login_tokens(client, "EMP001")
    other = await login_tokens(client, "EMP001")
    r = await client.post(
        "/api/auth/change-password", headers=bearer(old), json={"current_password": PASSWORD, "new_password": "N3w!Valid-Pass99"}
    )
    assert r.status_code == 200
    new = r.json()
    assert (await client.get("/api/auth/me", headers=bearer(new))).status_code == 200
    assert (await client.get("/api/auth/me", headers=bearer(other))).status_code == 401
    assert (await client.get("/api/auth/me", headers=bearer(old))).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": other["refresh_token"]})).json()["error"][
        "code"
    ] == "SESSION_REVOKED"


async def test_admin_reset_password_invalidates_access_token(client, seeded):
    emp = await login_tokens(client, "EMP001")
    admin = await login_tokens(client, "ADM001")
    await client.post(f"/api/admin/employees/{seeded['users']['employee']}/reset-password", headers=bearer(admin))
    assert (await client.get("/api/auth/me", headers=bearer(emp))).status_code == 401
    assert (await client.get("/api/auth/me", headers=bearer(admin))).status_code == 200  # actor unaffected


# ------------------------------------------------------------------ M4 / B1
async def test_wrong_current_password_counts_once_and_is_not_401(client, db):
    t = await login_tokens(client, "EMP001")
    r = await client.post(
        "/api/auth/change-password", headers=bearer(t), json={"current_password": BAD, "new_password": "N3w!Valid-Pass99"}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "CURRENT_PASSWORD_INVALID"
    await db.rollback()
    u = (
        (await db.execute(select(User).where(User.employee_id == "EMP001").execution_options(populate_existing=True)))
        .unique()
        .scalar_one()
    )
    assert u.failed_login_count == 1


# ------------------------------------------------------------------ M1 lockout does not enumerate
async def test_locked_account_indistinguishable_from_unknown_for_wrong_password(client):
    for _ in range(5):
        await do_login(client, "EMP001", BAD)
    locked = await do_login(client, "EMP001", BAD)
    unknown = await do_login(client, "NOSUCH1", BAD)
    assert locked.status_code == unknown.status_code == 401
    assert locked.json()["error"]["code"] == unknown.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert locked.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_locked_path_performs_a_real_verification(client, monkeypatch):
    for _ in range(5):
        await do_login(client, "EMP001", BAD)
    calls = []
    real = security.verify_password
    monkeypatch.setattr(security, "verify_password", lambda h, p: calls.append(1) or real(h, p))
    await do_login(client, "EMP001", BAD)
    assert calls  # timing parity with the other branches


# ------------------------------------------------------------------ M5 refresh
async def _refresh(client, tok):
    return await client.post("/api/auth/refresh", json={"refresh_token": tok})


async def test_lost_response_replay_within_grace_succeeds_and_chain_continues(client):
    t = await login_tokens(client, "EMP001")
    first = (await _refresh(client, t["refresh_token"])).json()  # response "lost"
    retry = await _refresh(client, t["refresh_token"])  # client retries with the old token, within the grace window
    assert retry.status_code == 200
    again = await _refresh(client, retry.json()["refresh_token"])
    assert again.status_code == 200
    # the token whose response was lost is now superseded and unusable
    assert (await _refresh(client, first["refresh_token"])).status_code == 401


async def test_replay_of_predecessor_after_successor_was_used_is_reuse(client, db):
    t = await login_tokens(client, "EMP001")
    n = (await _refresh(client, t["refresh_token"])).json()
    assert (await _refresh(client, n["refresh_token"])).status_code == 200  # successor consumed -> grace not applicable
    r = await _refresh(client, t["refresh_token"])
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_REUSED"


async def test_logout_then_refresh_is_session_revoked_not_reuse(client, db):
    t = await login_tokens(client, "EMP001")
    await client.post("/api/auth/logout", headers=bearer(t), json={"refresh_token": t["refresh_token"]})
    r = await _refresh(client, t["refresh_token"])
    assert r.status_code == 401 and r.json()["error"]["code"] == "SESSION_REVOKED"
    assert "auth.refresh_reuse" not in [a for (a,) in (await db.execute(select(AuditLog.action))).all()]


async def test_absolute_session_lifetime_is_not_extended_by_rotation(client, db):
    t = await login_tokens(client, "EMP001")
    await db.rollback()
    row = (await db.execute(select(RefreshToken))).scalar_one()
    row.family_started_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=get_settings().session_max_days - 1)
    await db.commit()
    n = (await _refresh(client, t["refresh_token"])).json()
    await db.rollback()
    rows = (await db.execute(select(RefreshToken).order_by(RefreshToken.issued_at))).scalars().all()
    child = rows[-1]
    exp = child.expires_at if child.expires_at.tzinfo else child.expires_at.replace(tzinfo=dt.UTC)
    assert exp - dt.datetime.now(dt.UTC) < dt.timedelta(days=1, minutes=1)  # capped far below refresh_token_days
    for r in rows:
        r.family_started_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=get_settings().session_max_days + 1)
    await db.commit()
    r = await _refresh(client, n["refresh_token"])
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_EXPIRED"


# ------------------------------------------------------------------ M8 permission based guards
def _body(seeded, role, eid, **over):
    return {
        "employee_id": eid,
        "full_name": "Some Person",
        "official_email": f"{eid.lower()}@corp-mail.com",
        "role_id": str(seeded["roles"][role]),
        "department_id": str(seeded["dept"]),
        "shift_id": str(seeded["shift"]),
        **over,
    }


@pytest.mark.parametrize("role", ["hr", "manager", "admin"])
async def test_hr_cannot_create_elevated_accounts(client, seeded, role):
    hr = await login_tokens(client, "HR001")
    r = await client.post("/api/admin/employees", headers=bearer(hr), json=_body(seeded, role, "NEW001"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


async def test_hr_can_create_plain_employee_and_admin_can_create_hr(client, seeded):
    hr = await login_tokens(client, "HR001")
    assert (
        await client.post("/api/admin/employees", headers=bearer(hr), json=_body(seeded, "employee", "NEW001"))
    ).status_code == 201
    admin = await login_tokens(client, "ADM001")
    assert (
        await client.post("/api/admin/employees", headers=bearer(admin), json=_body(seeded, "hr", "NEW002"))
    ).status_code == 201


async def test_hr_cannot_promote_or_reset_or_edit_managers_and_hr(client, seeded):
    hr = await login_tokens(client, "HR001")
    emp_id = seeded["users"]["employee"]
    promote = await client.put(
        f"/api/admin/employees/{emp_id}",
        headers=bearer(hr),
        json={"full_name": "E", "role_id": str(seeded["roles"]["manager"]), "department_id": str(seeded["dept"])},
    )
    assert promote.status_code == 403
    for target in ("manager", "hr"):
        uid = seeded["users"][target]
        assert (await client.post(f"/api/admin/employees/{uid}/reset-password", headers=bearer(hr))).status_code == 403
        assert (
            await client.put(
                f"/api/admin/employees/{uid}",
                headers=bearer(hr),
                json={"full_name": "X", "role_id": str(seeded["roles"][target])},
            )
        ).status_code == 403
    # a plain employee can still be reset by HR
    assert (await client.post(f"/api/admin/employees/{emp_id}/reset-password", headers=bearer(hr))).status_code == 200
    # review H1: unlock / activate / deactivate of an elevated account needs role.assign too
    for action in ("unlock", "deactivate", "activate"):
        r = await client.post(f"/api/admin/employees/{seeded['users']['manager']}/{action}", headers=bearer(hr))
        assert r.status_code == 403, action


async def test_second_admin_can_be_deleted_while_another_remains(client, seeded):
    admin = await login_tokens(client, "ADM001")
    r2 = await client.post(
        "/api/admin/employees", headers=bearer(admin), json=_body(seeded, "admin", "ADM002", current_password=PASSWORD)
    )
    assert (await client.delete(f"/api/admin/employees/{r2.json()['employee']['id']}", headers=bearer(admin))).status_code == 204


# ------------------------------------------------------------------ B9
async def test_delete_employee_soft_deletes_revokes_and_reserves_id(client, seeded, db):
    emp = await login_tokens(client, "EMP001")
    admin = await login_tokens(client, "ADM001")
    uid = seeded["users"]["employee"]
    assert (await client.delete(f"/api/admin/employees/{uid}", headers=bearer(admin))).status_code == 204
    assert (await client.get(f"/api/admin/employees/{uid}", headers=bearer(admin))).status_code == 404
    assert (await client.get("/api/auth/me", headers=bearer(emp))).status_code in (401, 403)
    assert (await _refresh(client, emp["refresh_token"])).status_code == 403  # ACCOUNT_INACTIVE: app wipes + locks
    assert (await do_login(client, "EMP001")).json()["error"]["code"] == "ACCOUNT_INACTIVE"
    again = await client.post("/api/admin/employees", headers=bearer(admin), json=_body(seeded, "employee", "EMP001"))
    assert again.status_code == 409  # employee_id never reused
    await db.rollback()
    u = (
        (await db.execute(select(User).where(User.employee_id == "EMP001").execution_options(populate_existing=True)))
        .unique()
        .scalar_one()
    )
    assert u.deleted_at is not None and u.status == "inactive"
    assert "employee.delete" in [a for (a,) in (await db.execute(select(AuditLog.action))).all()]


async def test_delete_requires_permission_and_blocks_self_and_last_admin(client, seeded):
    hr = await login_tokens(client, "HR001")
    assert (await client.delete(f"/api/admin/employees/{seeded['users']['employee']}", headers=bearer(hr))).status_code == 403
    admin = await login_tokens(client, "ADM001")
    assert (await client.delete(f"/api/admin/employees/{seeded['users']['admin']}", headers=bearer(admin))).status_code == 403
    assert (
        await client.delete("/api/admin/employees/00000000-0000-0000-0000-000000000001", headers=bearer(admin))
    ).status_code == 404


async def test_last_admin_cannot_be_deleted_by_another_admin_holder(client, seeded, db):
    from app.services import employees as svc

    admin_u = (await db.execute(select(User).where(User.employee_id == "ADM001"))).unique().scalar_one()
    with pytest.raises(Exception) as e:
        await svc._guard_last_admin(db, admin_u)
    assert e.value.code == "LAST_ADMIN"


async def test_password_reset_request_queue(client, seeded, db):
    await client.post("/api/auth/forgot-password", json={"employee_id": "EMP001"})
    await client.post("/api/auth/forgot-password", json={"employee_id": "MGR001"})
    hr = await login_tokens(client, "HR001")
    r = await client.get("/api/admin/password-reset-requests?status=open", headers=bearer(hr))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and {i["user"]["employee_id"] for i in body["items"]} == {"EMP001", "MGR001"}
    assert set(body["items"][0]) == {"id", "status", "requested_at", "handled_at", "handled_by", "user"}
    emp = await login_tokens(client, "EMP001")
    assert (await client.get("/api/admin/password-reset-requests", headers=bearer(emp))).status_code == 403
    await client.post(f"/api/admin/employees/{seeded['users']['employee']}/reset-password", headers=bearer(hr))
    assert (await client.get("/api/admin/password-reset-requests?status=open", headers=bearer(hr))).json()["total"] == 1
    assert (await client.get("/api/admin/password-reset-requests?status=handled", headers=bearer(hr))).json()["total"] == 1
    assert (await client.get("/api/admin/password-reset-requests?status=bogus", headers=bearer(hr))).status_code == 422
    assert (await db.execute(select(PasswordResetRequest))).scalars().all()


# ------------------------------------------------------------------ M9 audit
def test_redact_is_recursive_and_covers_pii():
    out = redact(
        {
            "a": {"password": "x", "ok": 1},
            "items": [{"phone": "+91"}, {"n": 1}],
            "personal_email": "a@b.c",
            "dob": "2000-01-01",
        }
    )
    assert out == {
        "a": {"password": "***", "ok": 1},
        "items": [{"phone": "***"}, {"n": 1}],
        "personal_email": "***",
        "dob": "***",
    }


def test_safe_eid_hides_password_typed_into_id_field():
    assert safe_eid(" emp001 ") == "EMP001"
    h = safe_eid("My Secret P@ss!")
    assert h.startswith("sha256:") and "Secret" not in h


async def test_unknown_login_does_not_store_typed_text_in_audit(client, db):
    await client.post("/api/auth/login", json={"employee_id": "hunter2 my password!", "password": BAD})
    ids = [i for (i,) in (await db.execute(select(AuditLog.actor_employee_id))).all()]
    assert ids and all("password" not in (i or "") for i in ids)


# ------------------------------------------------------------------ misc
async def test_config_inactivity_timeout_is_clamped(client, db):
    from app.models import Setting

    db.add(Setting(key="auth.inactivity_timeout_minutes", value=100000))
    await db.commit()
    t = await login_tokens(client, "EMP001")
    assert (await client.get("/api/config", headers=bearer(t))).json()["auth"]["inactivity_timeout_minutes"] == 30


async def test_list_search_escapes_like_wildcards(client):
    admin = await login_tokens(client, "ADM001")
    r = await client.get("/api/admin/employees?q=%25", headers=bearer(admin))
    assert r.json()["total"] == 0


# ------------------------------------------------------------------ break-glass CLI
async def test_cli_unlock_reset_and_create_admin(client, db, sessionmaker_, seeded):
    from scripts import create_admin as cli

    for _ in range(5):
        await do_login(client, "ADM001", BAD)
    assert (await do_login(client, "ADM001")).status_code == 423
    async with sessionmaker_() as s:
        await cli.unlock(s, "adm001")
    assert (await do_login(client, "ADM001")).status_code == 200
    async with sessionmaker_() as s:
        temp = await cli.reset_password(s, "ADM001")
    assert security.validate_password(temp, "ADM001", "Test Admin") == []
    assert (await do_login(client, "ADM001", PASSWORD)).status_code == 401
    assert (await do_login(client, "ADM001", temp)).json()["must_change_password"] is True
    async with sessionmaker_() as s:
        u = await cli.create_admin(s, "root01", "Root User", "Gr8!Complex-Pw#7")
        assert u.employee_id == "ROOT01"
        with pytest.raises(cli.CliError):
            await cli.create_admin(s, "root02", "Weak", "weak")
        with pytest.raises(cli.CliError):
            await cli.create_admin(s, "ROOT01", "Dup", "Gr8!Complex-Pw#7")
    assert (await do_login(client, "ROOT01", "Gr8!Complex-Pw#7")).json()["user"]["role"] == "admin"
