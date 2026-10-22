"""Plan 11 s.5: vendor-signed licenses, seat limit (50 unlicensed, admins count, no expiry by default), grace/expiry modes."""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from app.core import license_keys
from app.models import AuditLog, License, User
from app.services import license as lic
from scripts import create_admin as cli
from tests.conftest import PASSWORD, bearer, do_login, login_tokens
from tests.test_hardening import _body


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def vendor(monkeypatch):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(license_keys, "VENDOR_PUBLIC_KEYS", [b64(pub)])
    lic._verify_cache.clear()
    yield key
    lic._verify_cache.clear()


def mint(key, dep, *, now=None, **over) -> str:
    now = now or dt.datetime.now(dt.UTC)
    payload = {
        "license_id": "L-2026-0042",
        "licensed_to": "Acme Pvt Ltd",
        "deployment_id": dep,
        "max_users": 6,
        "issued_at": (now - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": None,
        "grace_days": 14,
        "on_expiry": "block_new_users",
        **over,
    }
    body = b64(json.dumps(payload).encode())
    return f"ECL1.{body}.{b64(key.sign(f'ECL1.{body}'.encode()))}"


async def dep_id(db) -> str:
    return str((await lic.get_company(db)).deployment_id)


@pytest.fixture
async def admin(tokens_for):
    return bearer(await tokens_for("admin"))


async def install(client, admin, token, pw=PASSWORD):
    return await client.put("/api/admin/license", headers=admin, json={"license_key": token, "current_password": pw})


async def make_user(client, admin, seeded, eid, role="employee"):
    return await client.post("/api/admin/employees", headers=admin, json=_body(seeded, role, eid))


# ------------------------------------------------------------------ unlicensed default
def test_unlicensed_default_is_fifty_and_never_expires():
    assert lic.UNLICENSED_MAX_USERS == 50
    st = lic.compute_state(None, "")
    assert st.status == "unlicensed" and st.max_users == 50 and st.grace_until is None and st.expires_at is None


async def test_get_license_unlicensed(client, admin, db):
    r = await client.get("/api/admin/license", headers=admin)
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "unlicensed" and b["max_users"] == 50 and b["active_users"] == 4 and b["seats_available"] == 46
    assert b["expires_at"] is None and b["grace_until"] is None and b["on_expiry"] == "block_new_users"
    assert b["deployment_id"] == await dep_id(db) and b["license_id"] is None
    assert (await client.get("/api/admin/license", headers=bearer(await login_tokens(client, "HR001")))).status_code == 403


async def test_seat_limit_unlicensed_counts_admins_and_deactivate_frees_a_seat(client, admin, seeded, db, monkeypatch):
    monkeypatch.setattr(lic, "UNLICENSED_MAX_USERS", 5)  # 4 seeded users (admin included) -> one free seat
    r = await make_user(client, admin, seeded, "EMP100")
    assert r.status_code == 201
    full = await make_user(client, admin, seeded, "EMP101")
    assert full.status_code == 409
    e = full.json()["error"]
    assert e["code"] == "USER_LIMIT_REACHED"
    assert e["details"] == [{"field": "max_users", "message": "5"}, {"field": "active_users", "message": "5"}]
    row = (await db.execute(select(AuditLog).where(AuditLog.action == "license.seat_refused"))).scalar_one()
    assert row.outcome == "failure" and row.after["code"] == "USER_LIMIT_REACHED"
    assert (await db.execute(select(User).where(User.employee_id == "EMP101"))).first() is None
    # deactivate frees the seat immediately; activate at the limit is refused
    new_id = r.json()["employee"]["id"]
    assert (await client.post(f"/api/admin/employees/{new_id}/deactivate", headers=admin)).status_code == 200
    assert (await make_user(client, admin, seeded, "EMP101")).status_code == 201
    r = await client.post(f"/api/admin/employees/{new_id}/activate", headers=admin)
    assert r.status_code == 409 and r.json()["error"]["code"] == "USER_LIMIT_REACHED"
    # an admin account takes a seat like anybody else
    assert (await client.get("/api/admin/license", headers=admin)).json()["seats_available"] == 0


async def test_deleted_users_free_their_seat(client, admin, seeded, monkeypatch):
    monkeypatch.setattr(lic, "UNLICENSED_MAX_USERS", 4)
    assert (await make_user(client, admin, seeded, "EMP100")).status_code == 409
    assert (await client.delete(f"/api/admin/employees/{seeded['users']['manager']}", headers=admin)).status_code == 204
    assert (await make_user(client, admin, seeded, "EMP100")).status_code == 201


# ------------------------------------------------------------------ install
async def test_install_valid_license(client, admin, vendor, db):
    tok = mint(vendor, await dep_id(db), max_users=5)
    r = await install(client, admin, tok)
    assert r.status_code == 200, r.text
    b = r.json()
    assert (
        b["status"] == "valid" and b["max_users"] == 5 and b["license_id"] == "L-2026-0042" and b["licensed_to"] == "Acme Pvt Ltd"
    )
    assert b["seats_available"] == 1 and b["expires_at"] is None
    row = (await db.execute(select(License))).scalar_one()
    assert row.max_users == 5 and row.token == tok and row.installed_by is not None
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "license.install"))).scalar_one()
    assert (
        a.after["license_id"] == "L-2026-0042" and a.after["max_users"] == 5 and tok not in json.dumps(a.after)
    )  # never the token
    assert (await client.get("/api/admin/license", headers=admin)).json()["max_users"] == 5


async def test_install_rejections(client, admin, vendor, db, redis):
    dep = await dep_id(db)
    good = mint(vendor, dep)
    head, payload, sig = good.split(".")
    other_key = Ed25519PrivateKey.generate()
    forged_payload = b64(json.dumps({**json.loads(base64.urlsafe_b64decode(payload + "==")), "max_users": 99999}).encode())
    cases = {
        "garbage": "not-a-license",
        "wrong prefix": "ECL2." + payload + "." + sig,
        "edited payload": f"{head}.{forged_payload}.{sig}",
        "other vendor key": mint(other_key, dep),
        "other deployment": mint(vendor, "7a1b2c3d-0000-4000-8000-000000000000"),
        "bad on_expiry": mint(vendor, dep, on_expiry="free_for_all"),
        "zero users": mint(vendor, dep, max_users=0),
        "expiry before issue": mint(vendor, dep, expires_at="2000-01-01T00:00:00Z"),
        "oversize": "ECL1." + "A" * 5000 + ".AAAA",
    }
    for name, tok in cases.items():
        await redis.flushall()  # the 5/min step-up limit is tested separately
        r = await install(client, admin, tok)
        assert r.status_code in (422,), (name, r.status_code, r.text)
        assert r.json()["error"]["code"] in ("LICENSE_INVALID", "VALIDATION_ERROR"), name
    assert (await db.execute(select(License))).first() is None
    assert (await client.get("/api/admin/license", headers=admin)).json()["max_users"] == 50  # nothing changed


async def test_install_needs_step_up_and_permission(client, admin, vendor, db):
    tok = mint(vendor, await dep_id(db))
    r = await install(client, admin, tok, pw="Wr0ng!Passw0rd#9")
    assert r.status_code == 403 and r.json()["error"]["code"] == "REAUTH_FAILED"
    r = await client.put("/api/admin/license", headers=admin, json={"license_key": tok})
    assert r.status_code == 422  # current_password is always required
    hr = bearer(await login_tokens(client, "HR001"))
    assert (await install(client, hr, tok)).status_code == 403
    assert (await db.execute(select(License))).first() is None
    codes = [(await install(client, admin, "x" * 20, pw="Wr0ng!Passw0rd#9")).status_code for _ in range(6)]
    assert 429 in codes  # 5/min


async def test_older_license_is_refused_newer_replaces(client, admin, vendor, db):
    dep = await dep_id(db)
    now = dt.datetime.now(dt.UTC)
    big = mint(vendor, dep, max_users=20, issued_at=(now - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    small = mint(vendor, dep, max_users=5, issued_at=(now - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert (await install(client, admin, small)).status_code == 200
    r = await install(client, admin, big)  # an old, larger license cannot be re-installed after a downgrade
    assert r.status_code == 409 and r.json()["error"]["code"] == "LICENSE_OUTDATED"
    assert (await client.get("/api/admin/license", headers=admin)).json()["max_users"] == 5
    newer = mint(vendor, dep, max_users=30, issued_at=(now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert (await install(client, admin, newer)).json()["max_users"] == 30
    assert (await install(client, admin, newer)).status_code == 200  # re-installing the same token is fine


async def test_downgrade_below_active_users_blocks_new_but_deactivates_nobody(client, admin, vendor, seeded, db):
    await install(client, admin, mint(vendor, await dep_id(db), max_users=2))
    b = (await client.get("/api/admin/license", headers=admin)).json()
    assert b["max_users"] == 2 and b["active_users"] == 4 and b["seats_available"] == 0
    assert (await make_user(client, admin, seeded, "EMP100")).json()["error"]["code"] == "USER_LIMIT_REACHED"
    assert (await do_login(client, "EMP001")).status_code == 200  # nobody was deactivated


async def test_invalid_stored_token_behaves_unlicensed(client, admin, vendor, db, caplog):
    await install(client, admin, mint(vendor, await dep_id(db), max_users=500))
    row = (await db.execute(select(License))).scalar_one()
    row.token = row.token[:-4] + "AAAA"  # tampered after install; also edit the display copy to look big
    row.max_users = 100000
    await db.commit()
    with caplog.at_level("ERROR", logger="app.license"):
        b = (await client.get("/api/admin/license", headers=admin)).json()
    assert b["status"] == "invalid" and b["max_users"] == 50
    assert any("invalid" in r.message for r in caplog.records)
    # a DB copy to another deployment also invalidates
    row.token = mint(vendor, "7a1b2c3d-0000-4000-8000-000000000000", max_users=500)
    await db.commit()
    assert (await client.get("/api/admin/license", headers=admin)).json()["status"] == "invalid"


async def test_no_public_key_means_nothing_installs(client, admin, db, monkeypatch):
    monkeypatch.setattr(license_keys, "VENDOR_PUBLIC_KEYS", [])
    key = Ed25519PrivateKey.generate()
    assert (await install(client, admin, mint(key, await dep_id(db)))).status_code == 422


# ------------------------------------------------------------------ expiry
def test_state_valid_grace_expired_boundaries(vendor):
    dep = "11111111-1111-4111-8111-111111111111"
    now = dt.datetime(2026, 10, 22, tzinfo=dt.UTC)
    exp = now - dt.timedelta(days=1)

    def state(**kw):
        row = License(token=mint(vendor, dep, now=now - dt.timedelta(days=100), **kw))
        return lic.compute_state(row, dep, now)

    assert state(expires_at=(now + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")).status == "valid"
    assert state(expires_at=exp.strftime("%Y-%m-%dT%H:%M:%SZ"), grace_days=14).status == "grace"
    s = state(expires_at=(now - dt.timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ"), grace_days=14)
    assert s.status == "expired" and s.on_expiry == "block_new_users"
    assert state().status == "valid"  # no expires_at = never


async def _expired(client, admin, vendor, db, on_expiry):
    now = dt.datetime.now(dt.UTC)
    tok = mint(
        vendor, await dep_id(db), max_users=50, on_expiry=on_expiry, grace_days=0,
        issued_at=(now - dt.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )  # fmt: skip
    # install bypassing the endpoint (an expired license is still installable via the CLI path, tested separately)
    r = await install(client, admin, tok)
    assert r.status_code == 200 and r.json()["status"] == "expired"


async def test_grace_blocks_nothing(client, admin, vendor, seeded, db):
    now = dt.datetime.now(dt.UTC)
    tok = mint(
        vendor, await dep_id(db), grace_days=14, max_users=50, issued_at=(now - dt.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )  # fmt: skip
    b = (await install(client, admin, tok)).json()
    assert b["status"] == "grace" and b["grace_until"]
    assert (await make_user(client, admin, seeded, "EMP100")).status_code == 201


async def test_expired_block_new_users_only_refuses_seat_consuming_actions(client, admin, vendor, seeded, db):
    await _expired(client, admin, vendor, db, "block_new_users")
    r = await make_user(client, admin, seeded, "EMP100")
    assert r.status_code == 403 and r.json()["error"]["code"] == "LICENSE_EXPIRED"
    emp_id = seeded["users"]["employee"]
    await client.post(f"/api/admin/employees/{emp_id}/deactivate", headers=admin)
    r = await client.post(f"/api/admin/employees/{emp_id}/activate", headers=admin)
    assert r.status_code == 403 and r.json()["error"]["code"] == "LICENSE_EXPIRED"
    # everything else keeps working: employees use the app, admins edit other data
    emp = bearer(await login_tokens(client, "MGR001"))
    assert (await client.get("/api/auth/me", headers=emp)).status_code == 200
    assert (
        await client.post("/api/admin/departments", headers=admin, json={"name": "Ops", "code": "OPS", "is_active": True})
    ).status_code == 201
    assert "license.seat_refused" in [a for (a,) in (await db.execute(select(AuditLog.action))).all()]


async def test_expired_read_only_blocks_writes_but_not_reads_auth_offboarding(client, admin, vendor, seeded, db):
    emp_t = await login_tokens(client, "EMP001")
    await _expired(client, admin, vendor, db, "read_only")
    expired = lambda r: r.status_code == 403 and r.json()["error"]["code"] == "LICENSE_EXPIRED"  # noqa: E731
    assert expired(
        await client.post("/api/admin/departments", headers=admin, json={"name": "Ops", "code": "OPS", "is_active": True})
    )
    assert expired(await make_user(client, admin, seeded, "EMP100"))
    assert expired(await client.put("/api/admin/settings", headers=admin, json={"settings": {"food.max_advance_days": 7}}))
    assert expired(await client.post("/api/attendance/check-in", headers=bearer(emp_t), json={}))  # employee write endpoints too
    # reads, exports, login/refresh/change-password/logout keep working
    assert (await client.get("/api/admin/employees", headers=admin)).status_code == 200
    assert (await client.get("/api/admin/license", headers=admin)).status_code == 200
    assert (await client.get("/api/auth/me", headers=bearer(emp_t))).status_code == 200
    assert (await client.post("/api/auth/refresh", json={"refresh_token": emp_t["refresh_token"]})).status_code == 200
    assert (await do_login(client, "HR001")).status_code == 200
    ch = await client.post(
        "/api/auth/change-password",
        headers=bearer(emp_t),
        json={"current_password": PASSWORD, "new_password": "N3w!Valid-Pass99"},
    )
    assert ch.status_code in (200, 401)  # (old access token may already be rotated out; the point is it is not LICENSE_EXPIRED)
    assert ch.json().get("error", {}).get("code") != "LICENSE_EXPIRED"
    ex = await client.get("/api/admin/export?type=attendance&from=2026-10-01&to=2026-10-31", headers=admin)
    assert ex.status_code == 200
    # offboarding and renewal stay possible
    emp_id = seeded["users"]["employee"]
    assert (await client.post(f"/api/admin/employees/{emp_id}/revoke-sessions", headers=admin)).status_code == 200
    assert (await client.post(f"/api/admin/employees/{emp_id}/deactivate", headers=admin)).status_code == 200
    assert (await client.delete(f"/api/admin/employees/{seeded['users']['manager']}", headers=admin)).status_code == 204
    now = dt.datetime.now(dt.UTC)
    renewal = mint(vendor, await dep_id(db), max_users=50, issued_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert (await install(client, admin, renewal)).json()["status"] == "valid"
    assert (
        await client.post("/api/admin/departments", headers=admin, json={"name": "Ops", "code": "OPS", "is_active": True})
    ).status_code == 201


async def test_expiry_alert_job_notifies_license_managers(db, client, admin, vendor, seeded):
    from app.models import DeviceToken
    from app.services.notifications import OUTBOX

    assert await lic.alert_expiry(db) == 0  # unlicensed: nothing to say
    await _expired(client, admin, vendor, db, "block_new_users")
    db.add(DeviceToken(user_id=seeded["users"]["admin"], fcm_token="z" * 40, platform="android"))
    await db.commit()
    OUTBOX.clear()
    assert await lic.alert_expiry(db) == 1
    assert OUTBOX[-1]["payload"]["type"] == "admin_alert" and OUTBOX[-1]["user_ids"] == [str(seeded["users"]["admin"])]


# ------------------------------------------------------------------ CLI
async def test_cli_create_admin_respects_seats_and_sets_company(db, seeded, vendor, monkeypatch):
    u = await cli.create_admin(db, "root1", "Root One", "Str0ng!Passw0rd#7", None, "Initech Ltd")
    assert u.employee_id == "ROOT1"
    from app.services import company as company_svc

    assert (await company_svc.public_info(db))["name"] == "Initech Ltd"
    monkeypatch.setattr(lic, "UNLICENSED_MAX_USERS", 5)
    with pytest.raises(cli.CliError, match="USER_LIMIT_REACHED"):
        await cli.create_admin(db, "root2", "Root Two", "Str0ng!Passw0rd#7")
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "license.seat_refused"))).scalar_one()
    assert a.actor_employee_id == "cli"


async def test_cli_license_install_and_show(db, vendor, tmp_path):
    dep = await dep_id(db)
    f = tmp_path / "license.txt"
    f.write_text(mint(vendor, dep, max_users=7, expires_at=None) + "\n")
    info = await cli.license_install(db, str(f))
    assert info["status"] == "valid" and info["max_users"] == 7
    row = (await db.execute(select(License))).scalar_one()
    assert row.installed_by is None  # CLI
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "license.install"))).scalar_one()
    assert a.actor_employee_id == "cli"
    f.write_text("ECL1.bad.bad")
    with pytest.raises(cli.CliError, match="LICENSE_INVALID"):
        await cli.license_install(db, str(f))
    with pytest.raises(cli.CliError, match="cannot read"):
        await cli.license_install(db, str(tmp_path / "missing.txt"))


# ------------------------------------------------------------------ vendor tools
def test_issue_license_tool_round_trip(tmp_path, vendor, monkeypatch):
    """tools/license/issue_license.py output verifies with the public key (public key only inside the app)."""
    import importlib.util
    import sys
    from pathlib import Path

    tools = Path(__file__).resolve().parents[2] / "tools" / "license"
    spec = importlib.util.spec_from_file_location("issue_license", tools / "issue_license.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    keyfile = tmp_path / "k.pem"
    keyfile.write_bytes(
        vendor.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    out = tmp_path / "lic.txt"
    dep = "22222222-2222-4222-8222-222222222222"
    monkeypatch.setattr(
        sys, "argv",
        ["issue_license.py", "--key", str(keyfile), "--deployment-id", dep, "--license-id", "L-1", "--licensed-to", "X",
         "--max-users", "25", "--expires", "2099-01-01", "--on-expiry", "read_only", "--out", str(out)],
    )  # fmt: skip
    assert mod.main() == 0
    v = lic.verify_token(out.read_text().strip(), dep)
    assert v.max_users == 25 and v.on_expiry == "read_only" and v.expires_at.year == 2099 and v.grace_days == 14
