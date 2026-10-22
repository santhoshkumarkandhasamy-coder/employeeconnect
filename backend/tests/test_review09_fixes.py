"""Review 09 fixes: M1 lockout DoS, L1 console logout, L2 forgot-password push, L3 PUBLIC_ORIGIN, L4 CLI temp
password expiry, L10 console token on mobile change-password."""

from __future__ import annotations

import datetime as dt
import time

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models import DeviceToken, User
from app.services import auth as auth_svc
from app.services.notifications import OUTBOX
from tests.conftest import PASSWORD, bearer, do_login, login_tokens

BAD = "Wrong!Pass1x"
ORIGIN = "https://hr.example.com"
WEB = {"Origin": ORIGIN, "X-EC-Client": "console"}
OWNER_IP = "198.51.100.7"
ATTACKER_IP = "203.0.113.9"


@pytest.fixture(autouse=True)
def proxy_and_origin(monkeypatch):
    monkeypatch.setattr(get_settings(), "trusted_proxies", "127.0.0.0/8")  # X-Real-IP simulates client addresses
    monkeypatch.setattr(get_settings(), "public_origin", ORIGIN)


def at(ip: str) -> dict:
    return {"X-Real-IP": ip}


async def login_from(client, ip: str, eid: str = "EMP001", pw: str = PASSWORD):
    return await client.post("/api/auth/login", json={"employee_id": eid, "password": pw}, headers=at(ip))


# ---------------------------------------------------------------- M1
async def test_outsider_cannot_lock_owner_out_of_their_known_network(client):
    assert (await login_from(client, OWNER_IP)).status_code == 200  # owner has signed in from this IP before
    for _ in range(5):
        assert (await login_from(client, ATTACKER_IP, pw=BAD)).status_code == 401
    assert (await login_from(client, ATTACKER_IP)).status_code == 423  # the lock holds for the unknown IP
    assert (await login_from(client, "192.0.2.55")).status_code == 423  # ... and for any other unknown IP
    assert (await login_from(client, OWNER_IP)).status_code == 200  # but not for the owner's own network


async def test_known_ip_is_still_brute_force_limited(client):
    assert (await login_from(client, OWNER_IP)).status_code == 200
    for _ in range(5):  # guesses from the known IP itself burn its per-account budget
        assert (await login_from(client, OWNER_IP, pw=BAD)).status_code == 401
    assert (await login_from(client, OWNER_IP)).status_code == 423  # no bypass once the budget is used up


async def test_lockout_without_prior_sign_in_is_unchanged(client):
    for _ in range(5):
        await login_from(client, ATTACKER_IP, pw=BAD)
    assert (await login_from(client, OWNER_IP)).status_code == 423  # never signed in from there: still locked


async def test_privileged_lockout_alerts_admins(client, seeded, db):
    db.add(DeviceToken(user_id=seeded["users"]["admin"], fcm_token="adm-" + "z" * 40, platform="android"))
    await db.commit()
    OUTBOX.clear()
    for _ in range(5):
        await login_from(client, ATTACKER_IP, eid="ADM001", pw=BAD)
    alerts = [m for m in OUTBOX if m["payload"]["type"] == "admin_alert"]
    assert len(alerts) == 1 and "ADM001" in alerts[0]["payload"]["body"]
    OUTBOX.clear()
    for _ in range(5):  # a plain employee lockout raises no alert
        await login_from(client, ATTACKER_IP, eid="EMP001", pw=BAD)
    assert [m for m in OUTBOX if m["payload"]["type"] == "admin_alert"] == []


# ---------------------------------------------------------------- L1
async def web_session(client, eid="ADM001"):
    r = await client.post("/api/auth/web/login", headers=WEB, json={"employee_id": eid, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"], r.cookies.get("__Secure-ec_rt")


async def test_web_logout_invalidates_access_token_but_not_other_sessions(client):
    tok1, rt1 = await web_session(client)
    tok2, _rt2 = await web_session(client)
    h1, h2 = {"Authorization": f"Bearer {tok1}"}, {"Authorization": f"Bearer {tok2}"}
    assert (await client.get("/api/auth/me", headers=h1)).status_code == 200
    r = await client.post("/api/auth/web/logout", headers={**WEB, "Cookie": f"__Secure-ec_rt={rt1}"})
    assert r.status_code == 204
    assert (await client.get("/api/auth/me", headers=h1)).status_code == 401  # dead immediately, not after 15 min
    assert (await client.get("/api/auth/me", headers=h2)).status_code == 200  # the other console session lives on
    mobile = await login_tokens(client, "ADM001")
    assert (await client.get("/api/auth/me", headers=bearer(mobile))).status_code == 200


# ---------------------------------------------------------------- L2
async def test_forgot_password_service_does_not_push_inline(redis, db, seeded):
    admin_dev = DeviceToken(user_id=seeded["users"]["admin"], fcm_token="adm-" + "q" * 40, platform="android")
    db.add(admin_dev)
    await db.commit()
    OUTBOX.clear()
    requester = await auth_svc.forgot_password(db, redis, employee_id="EMP001")
    assert requester is not None and requester.employee_id == "EMP001"
    assert len(OUTBOX) == 0  # the push is the router's background task, outside the timed window
    assert await auth_svc.forgot_password(db, redis, employee_id="NOBODY") is None


async def test_forgot_password_endpoint_still_notifies_after_response(client, seeded, db):
    db.add(DeviceToken(user_id=seeded["users"]["admin"], fcm_token="adm-" + "w" * 40, platform="android"))
    await db.commit()
    OUTBOX.clear()
    assert (await client.post("/api/auth/forgot-password", json={"employee_id": "EMP001"})).status_code == 202
    assert [m for m in OUTBOX if m["payload"]["type"] == "reset_request"]


# ---------------------------------------------------------------- L3
def origin_errors(origin: str, env: str) -> bool:
    try:
        Settings(_env_file=None, app_env=env, public_origin=origin)  # type: ignore[call-arg]
    except ValidationError as exc:
        return any(e["loc"] == ("public_origin",) for e in exc.errors())
    return False


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost.attacker.tld",
        "http://localhost:8080.evil.com",
        "http://hr.company.com",
        "https://hr.company.com/path",
        "https://user@hr.company.com",
        "ftp://hr.company.com",
        "https://",
        "https://hr.company.com?x=1",
    ],
)
def test_public_origin_rejected_in_prod(origin):
    assert origin_errors(origin, "prod")


@pytest.mark.parametrize(
    ("origin", "env"),
    [
        ("https://hr.company.com", "prod"),
        ("https://hr.company.com:8443/", "prod"),
        ("http://localhost", "prod"),
        ("http://127.0.0.1:8080", "prod"),
        ("http://hr.lan", "dev"),
        ("http://localhost.test", "test"),
    ],
)
def test_public_origin_accepted(origin, env):
    assert not origin_errors(origin, env)


# ---------------------------------------------------------------- L4
async def test_cli_reset_sets_temp_password_expiry(db, sessionmaker_, seeded):
    from scripts import create_admin as cli

    async with sessionmaker_() as s:
        await cli.reset_password(s, "EMP001")
    await db.rollback()
    u = (
        (await db.execute(select(User).where(User.employee_id == "EMP001").execution_options(populate_existing=True)))
        .unique()
        .scalar_one()
    )
    assert u.must_change_password and u.temp_password_expires_at is not None
    left = u.temp_password_expires_at.replace(tzinfo=u.temp_password_expires_at.tzinfo or dt.UTC)
    assert 0 < left.timestamp() - time.time() <= get_settings().temp_password_hours * 3600 + 5


# ---------------------------------------------------------------- L10
async def test_console_token_cannot_use_mobile_change_password(client):
    tok, _ = await web_session(client)
    r = await client.post(
        "/api/auth/change-password",
        headers={"Authorization": f"Bearer {tok}"},
        json={"current_password": PASSWORD, "new_password": "Brand!New-Pass-42x"},
    )
    assert r.status_code == 403 and "refresh_token" not in r.text
    r = await client.post(  # a mobile token still works on the mobile endpoint
        "/api/auth/change-password",
        headers=bearer(await login_tokens(client, "EMP001")),
        json={"current_password": PASSWORD, "new_password": "Brand!New-Pass-42x"},
    )
    assert r.status_code == 200 and r.json()["refresh_token"]
    assert (await do_login(client, "EMP001", "Brand!New-Pass-42x")).status_code == 200
