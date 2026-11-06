"""Plan 11: company registration, offboarding (ACCOUNT_INACTIVE), sessions, console auth, console data, passwords."""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import get_settings
from app.models import (
    Attendance,
    AuditLog,
    DeviceToken,
    File,
    PasswordResetRequest,
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    Setting,
    User,
)
from app.services import company as company_svc
from app.services.notifications import OUTBOX
from tests.conftest import PASSWORD, bearer, do_login, login_tokens

ORIGIN = "https://hr.example.com"
WEB = {"Origin": ORIGIN, "X-EC-Client": "console"}


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(root))
    monkeypatch.setattr(get_settings(), "public_origin", ORIGIN)
    return root


def png(size=(40, 30), mode="RGBA") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, (0, 0, 255, 255) if mode == "RGBA" else (0, 0, 255)).save(out, "PNG")
    return out.getvalue()


def jpeg(size=(900, 300)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 10, 10)).save(out, "JPEG")
    return out.getvalue()


async def actions(db) -> list[str]:
    return [a for (a,) in (await db.execute(select(AuditLog.action))).all()]


@pytest.fixture
async def admin(tokens_for):
    return bearer(await tokens_for("admin"))


@pytest.fixture
async def hr(tokens_for):
    return bearer(await tokens_for("hr"))


@pytest.fixture
async def emp(tokens_for):
    return bearer(await tokens_for("employee"))


# ------------------------------------------------------------------ company
async def test_public_company_before_and_after_setup(client, admin, emp, db):
    r = await client.get("/api/public/company")  # no auth
    assert r.status_code == 200
    assert r.json() == {
        "product": "employee-connect", "api_version": "1", "configured": False, "name": None, "logo_url": None, "logo_version": 0,
    }  # fmt: skip
    assert (await client.put("/api/admin/company", headers=emp, json={"name": "Acme"})).status_code == 403
    assert (await client.put("/api/admin/company", headers=admin, json={"name": "A"})).status_code == 422
    r = await client.put("/api/admin/company", headers=admin, json={"name": "  <b>Acme</b>  Pvt\tLtd "})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Acme Pvt Ltd" and body["configured"] is True and body["setup_completed_at"]
    assert body["deployment_id"] and body["updated_by"]["employee_id"] == "ADM001"
    pub = (await client.get("/api/public/company")).json()
    assert pub["configured"] is True and pub["name"] == "Acme Pvt Ltd"
    assert (await client.get("/api/admin/company", headers=admin)).json()["name"] == "Acme Pvt Ltd"
    assert (await client.get("/api/admin/company", headers=emp)).status_code == 403
    me = (await client.get("/api/auth/me", headers=emp)).json()
    assert me["company"]["name"] == "Acme Pvt Ltd" and me["company"]["configured"] is True
    assert (await client.get("/api/config", headers=emp)).json()["company"]["name"] == "Acme Pvt Ltd"
    assert "company.update" in await actions(db)
    assert (await client.put("/api/admin/company", headers=admin, json={"name": "Acme", "x": 1})).status_code == 422


async def test_logo_upload_reencode_cache_replace_delete(client, admin, emp, db, storage):
    up = lambda data, name="l.png", ct="image/png": client.post(  # noqa: E731
        "/api/admin/company/logo", headers=admin, files={"file": (name, data, ct)}
    )
    assert (await client.get("/api/public/company/logo")).status_code == 404
    assert (
        await client.post("/api/admin/company/logo", headers=emp, files={"file": ("l.png", png(), "image/png")})
    ).status_code == 403
    # JPEG 900x300 -> PNG, longest side 512
    r = await up(jpeg(), "l.jpg", "image/jpeg")
    assert r.status_code == 200, r.text
    assert r.json()["logo_version"] == 1 and r.json()["logo_url"] == "/api/public/company/logo?v=1"
    got = await client.get("/api/public/company/logo?v=1")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    assert "public" in got.headers["cache-control"] and got.headers["etag"]
    im = Image.open(io.BytesIO(got.content))
    assert im.format == "PNG" and max(im.size) == 512
    again = await client.get("/api/public/company/logo", headers={"If-None-Match": got.headers["etag"]})
    assert again.status_code == 304
    # replacing deletes the old file (row + blob)
    first_id = (await db.execute(select(File.id).where(File.purpose == "company_logo"))).scalar_one()
    r2 = await up(png((10, 10)))
    assert r2.status_code == 200 and r2.json()["logo_version"] == 2
    await db.rollback()
    ids = list((await db.execute(select(File.id).where(File.purpose == "company_logo"))).scalars())
    assert len(ids) == 1 and ids[0] != first_id
    assert len(list(storage.glob("*/*.bin"))) == 1
    # PNG with a trailing payload comes back clean
    poly = png((20, 20)) + b"<script>alert(1)</script>"
    assert (await up(poly)).status_code == 200
    assert b"<script>" not in (await client.get("/api/public/company/logo")).content
    # not allowed: svg, text, pdf, oversize
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = await up(svg, "l.svg", "image/svg+xml")
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"
    assert (await up(b"%PDF-1.4 x", "l.pdf", "application/pdf")).json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024 + 10)
    r = await up(big)
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TOO_LARGE"
    # POST /files must not accept the purpose
    r = await client.post(
        "/api/files", headers=admin, data={"purpose": "company_logo"}, files={"file": ("a.png", png(), "image/png")}
    )
    assert r.status_code == 422
    # delete
    assert (await client.delete("/api/admin/company/logo", headers=emp)).status_code == 403
    assert (await client.delete("/api/admin/company/logo", headers=admin)).status_code == 204
    assert (await client.get("/api/public/company/logo")).status_code == 404
    assert (await client.get("/api/public/company", headers={})).json()["logo_version"] == 4
    assert list(storage.glob("*/*.bin")) == []
    assert "company.logo_update" in await actions(db)


async def test_orphan_sweep_keeps_the_company_logo(client, admin, db):
    from app.services import files as files_svc

    await client.post("/api/admin/company/logo", headers=admin, files={"file": ("l.png", png(), "image/png")})
    f = (await db.execute(select(File).where(File.purpose == "company_logo"))).scalar_one()
    f.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    await db.commit()
    assert await files_svc.cleanup_orphans(db) == 0
    assert (await client.get("/api/public/company/logo")).status_code == 200


async def test_public_company_is_rate_limited(client):
    codes = [(await client.get("/api/public/company")).status_code for _ in range(62)]
    assert codes[:60] == [200] * 60 and 429 in codes[60:]


async def test_company_service_cli_path_and_name_rules(db):
    out = await company_svc.update_name(db, None, "Globex")
    assert out["configured"] is True and out["name"] == "Globex"
    from app.core.errors import AppError

    with pytest.raises(AppError):
        await company_svc.update_name(db, None, "<i></i>")


# ------------------------------------------------------------------ offboarding
async def test_deactivate_reason_and_access_codes(client, admin, seeded, db):
    emp_t = await login_tokens(client, "EMP001")
    emp = bearer(emp_t)
    uid = seeded["users"]["employee"]
    OUTBOX.clear()
    db.add(DeviceToken(user_id=uid, fcm_token="t" * 40, platform="android"))
    await db.commit()
    bad = await client.post(f"/api/admin/employees/{uid}/deactivate", headers=admin, json={"reason": "fired"})
    assert bad.status_code == 422
    r = await client.post(f"/api/admin/employees/{uid}/deactivate", headers=admin, json={"reason": "resigned"})
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "inactive" and out["deactivation_reason"] == "resigned" and out["deactivated_at"]
    # next access call and refresh answer 403 ACCOUNT_INACTIVE; login only after the correct password
    r = await client.get("/api/auth/me", headers=emp)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCOUNT_INACTIVE"
    r = await client.post("/api/auth/refresh", json={"refresh_token": emp_t["refresh_token"]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCOUNT_INACTIVE"
    assert (await do_login(client, "EMP001")).json()["error"]["code"] == "ACCOUNT_INACTIVE"
    assert (await do_login(client, "EMP001", "Wr0ng!Passw0rd#9")).json()["error"]["code"] == "INVALID_CREDENTIALS"
    # data-only push, tokens deleted
    msg = [m for m in OUTBOX if m["payload"]["type"] == "account_disabled"]
    assert msg and msg[-1]["payload"] == {"type": "account_disabled"} and len(msg[-1]["targets"]) == 1
    assert (await db.execute(select(DeviceToken))).first() is None
    row = (await db.execute(select(AuditLog).where(AuditLog.action == "employee.deactivate"))).scalar_one()
    assert row.after["reason"] == "resigned"
    # listing shows it; activate clears it
    items = (await client.get("/api/admin/employees?status=inactive", headers=admin)).json()["items"]
    assert items[0]["deactivation_reason"] == "resigned"
    r = await client.post(f"/api/admin/employees/{uid}/activate", headers=admin)
    assert r.status_code == 200 and r.json()["deactivated_at"] is None and r.json()["deactivation_reason"] is None


async def test_deactivate_default_reason_and_delete_sets_columns(client, admin, seeded, db):
    uid = seeded["users"]["employee"]
    r = await client.post(f"/api/admin/employees/{uid}/deactivate", headers=admin)  # no body
    assert r.status_code == 200 and r.json()["deactivation_reason"] == "other"
    mgr = seeded["users"]["manager"]
    assert (await client.delete(f"/api/admin/employees/{mgr}", headers=admin)).status_code == 204
    u = (await db.execute(select(User).where(User.id == mgr))).unique().scalar_one()
    assert u.deactivated_at is not None and u.deactivation_reason == "other" and u.deleted_at is not None


async def test_login_stores_session_metadata(client, db):
    r = await do_login(
        client, "EMP001", device_id="dev-1", device_name="Pixel\x07 <b>8</b>", app_version="1.4.0", platform="android"
    )
    assert r.status_code == 200
    row = (await db.execute(select(RefreshToken))).scalar_one()
    assert row.client_type == "mobile" and row.platform == "android" and row.app_version == "1.4.0"
    assert row.device_name == "Pixel 8"  # control characters and markup stripped
    assert (await do_login(client, "EMP001", platform="symbian")).status_code == 422
    t = r.json()
    n = (await client.post("/api/auth/refresh", json={"refresh_token": t["refresh_token"]})).json()
    assert n["access_token"]
    heads = (await db.execute(select(RefreshToken).where(RefreshToken.replaced_by.is_(None)))).scalars().all()
    assert {(h.platform, h.app_version) for h in heads} == {("android", "1.4.0")}  # carried over on rotation


# ------------------------------------------------------------------ sessions
async def test_sessions_list_scope_and_revoke(client, admin, hr, seeded, db):
    emp_t = await login_tokens(client, "EMP001")
    tablet = (await do_login(client, "EMP001", device_name="Tablet", platform="ios")).json()
    r = await client.get("/api/admin/sessions", headers=admin)
    assert r.status_code == 200 and r.json()["total"] >= 4
    item = next(i for i in r.json()["items"] if i["user"]["employee_id"] == "EMP001" and i["device_name"] == "Tablet")
    assert item["platform"] == "ios"
    assert set(item) == {
        "id", "user", "client_type", "platform", "device_name", "device_id", "app_version", "ip", "started_at", "last_seen_at", "expires_at",
    }  # fmt: skip
    assert "view" in " ".join(a for a in await actions(db) if a.startswith("session"))
    # HR sees plain employees but NOT admin / hr / manager sessions
    seen = {i["user"]["employee_id"] for i in (await client.get("/api/admin/sessions?page_size=100", headers=hr)).json()["items"]}
    assert "EMP001" in seen and "ADM001" not in seen and "MGR001" not in seen
    assert (await client.get("/api/admin/sessions?client_type=web", headers=admin)).json()["total"] == 0
    f = (await client.get("/api/admin/sessions?employee_id=emp001", headers=admin)).json()
    assert f["total"] == 2
    per = (await client.get(f"/api/admin/employees/{seeded['users']['employee']}/sessions", headers=admin)).json()
    assert per["total"] == 2
    admin_user = seeded["users"]["admin"]
    assert (await client.get(f"/api/admin/employees/{admin_user}/sessions", headers=hr)).status_code == 403
    # plain employee has no session.manage
    assert (await client.get("/api/admin/sessions", headers=bearer(emp_t))).status_code == 403
    # revoke ONE session: only that device ends
    r = await client.delete(f"/api/admin/sessions/{item['id']}", headers=hr)
    assert r.status_code == 204
    assert (await client.get("/api/auth/me", headers=bearer(tablet))).status_code == 401  # its access token is invalidated
    gone = await client.post("/api/auth/refresh", json={"refresh_token": tablet["refresh_token"]})
    assert gone.status_code == 401 and gone.json()["error"]["code"] == "SESSION_REVOKED"  # NOT ACCOUNT_INACTIVE
    other = await client.post("/api/auth/refresh", json={"refresh_token": emp_t["refresh_token"]})
    assert other.status_code == 200  # the other device just refreshes and carries on
    assert (await client.delete(f"/api/admin/sessions/{item['id']}", headers=admin)).status_code == 404  # no longer live
    # HR cannot touch an admin session
    admin_sessions = (await client.get("/api/admin/sessions?employee_id=ADM001", headers=admin)).json()["items"]
    assert (await client.delete(f"/api/admin/sessions/{admin_sessions[0]['id']}", headers=hr)).status_code == 403
    # revoke-sessions keeps the account active
    emp_id = seeded["users"]["employee"]
    await login_tokens(client, "EMP001")
    r = await client.post(f"/api/admin/employees/{emp_id}/revoke-sessions", headers=hr)
    assert r.status_code == 200 and r.json()["revoked"] >= 1
    assert (await do_login(client, "EMP001")).status_code == 200
    assert (await client.post(f"/api/admin/employees/{admin_user}/revoke-sessions", headers=hr)).status_code == 403
    assert (await client.post(f"/api/admin/employees/{uuid.uuid4()}/revoke-sessions", headers=hr)).status_code == 404
    acts = await actions(db)
    assert "session.revoke" in acts and "session.revoke_all" in acts


async def test_sessions_list_rate_limit(client, admin):
    codes = [(await client.get("/api/admin/sessions", headers=admin)).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30 and 429 in codes[30:]


# ------------------------------------------------------------------ console (web) auth
def _cookie(resp) -> str:
    raw = resp.headers["set-cookie"]
    return raw.split(";", 1)[0].split("=", 1)[1]


async def web_login(client, eid="ADM001", pw=PASSWORD, headers=WEB):
    return await client.post("/api/auth/web/login", headers=headers, json={"employee_id": eid, "password": pw})


async def test_web_csrf_guards(client, monkeypatch):
    assert (await web_login(client, headers={})).status_code == 403
    assert (await web_login(client, headers={"Origin": ORIGIN})).status_code == 403  # no X-EC-Client
    assert (await web_login(client, headers={"X-EC-Client": "console"})).status_code == 403  # no Origin
    assert (await web_login(client, headers={**WEB, "Origin": "https://evil.example"})).status_code == 403
    assert (await web_login(client, headers={**WEB, "Origin": ORIGIN + "/"})).status_code == 200  # trailing slash tolerated
    for path in ("refresh", "logout"):
        assert (
            await client.post(f"/api/auth/web/{path}", headers={"Origin": "https://evil.example", "X-EC-Client": "console"})
        ).status_code == 403
    monkeypatch.setattr(get_settings(), "public_origin", None)  # console disabled when PUBLIC_ORIGIN is unset
    assert (await web_login(client)).status_code == 403


async def test_web_login_cookie_refresh_logout(client, seeded, db):
    r = await web_login(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "refresh_token" not in body and body["access_token"] and body["user"]["employee_id"] == "ADM001"
    sc = r.headers["set-cookie"]
    assert sc.startswith("__Secure-ec_rt=")
    low = sc.lower()
    assert "httponly" in low and "secure" in low and "samesite=strict" in low and "path=/api/auth/web" in low
    assert "max-age=43200" in low  # console.session_max_hours default 12
    row = (await db.execute(select(RefreshToken))).scalar_one()
    assert row.client_type == "web" and row.platform == "web"
    assert (row.expires_at - row.family_started_at) <= dt.timedelta(hours=12, seconds=5)
    tok = _cookie(r)
    # the web session is listed
    assert (await client.get("/api/admin/sessions?client_type=web", headers=bearer(body))).json()["total"] == 1
    # refresh rotates the cookie
    r2 = await client.post("/api/auth/web/refresh", headers={**WEB, "Cookie": f"__Secure-ec_rt={tok}"})
    assert r2.status_code == 200 and "refresh_token" not in r2.json() and "user" not in r2.json()
    new = _cookie(r2)
    assert new != tok
    # no cookie -> 401; mobile endpoints refuse the web token and vice versa
    assert (await client.post("/api/auth/web/refresh", headers=WEB)).status_code == 401
    assert (await client.post("/api/auth/refresh", json={"refresh_token": new})).status_code == 401
    m = await login_tokens(client, "ADM001")
    bad = await client.post("/api/auth/web/refresh", headers={**WEB, "Cookie": f"__Secure-ec_rt={m['refresh_token']}"})
    assert bad.status_code == 401
    # logout clears the cookie and revokes the family
    lo = await client.post("/api/auth/web/logout", headers={**WEB, "Cookie": f"__Secure-ec_rt={new}"})
    assert lo.status_code == 204 and "max-age=0" in lo.headers["set-cookie"].lower()
    again = await client.post("/api/auth/web/refresh", headers={**WEB, "Cookie": f"__Secure-ec_rt={new}"})
    assert again.status_code == 401 and again.json()["error"]["code"] == "SESSION_REVOKED"
    assert "max-age=0" in again.headers["set-cookie"].lower()  # dead cookie dropped
    # the mobile session of the same user was not touched
    assert (await client.get("/api/auth/me", headers=bearer(m))).status_code == 200


async def test_web_login_requires_console_access_after_correct_password(client, db):
    # HR does not hold console.access (Admin only decision); employees neither
    r = await web_login(client, "HR001")
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    assert (await web_login(client, "HR001", "Wr0ng!Passw0rd#9")).status_code == 401  # not revealed without the password
    assert (await do_login(client, "HR001")).status_code == 200  # mobile login unaffected
    assert "auth.login_failed" in await actions(db)
    assert (await db.execute(select(RefreshToken).where(RefreshToken.client_type == "web"))).first() is None


async def test_web_lockout_is_shared_with_mobile_login(client):
    for _ in range(5):
        await web_login(client, "ADM001", "Wr0ng!Passw0rd#9")
    r = await web_login(client)
    assert r.status_code == 423 and r.json()["error"]["code"] == "ACCOUNT_LOCKED"


async def test_web_session_idle_and_absolute_limits(client, db):
    r = await web_login(client)
    tok = _cookie(r)
    row = (await db.execute(select(RefreshToken))).scalar_one()
    row.issued_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=15 + 15 + 2)  # idle > inactivity + access lifetime
    await db.commit()
    idle = await client.post("/api/auth/web/refresh", headers={**WEB, "Cookie": f"__Secure-ec_rt={tok}"})
    assert idle.status_code == 401 and idle.json()["error"]["code"] == "TOKEN_EXPIRED"
    # absolute: console.session_max_hours
    r = await web_login(client)
    tok = _cookie(r)
    row = (await db.execute(select(RefreshToken).where(RefreshToken.revoked_at.is_(None)))).scalar_one()
    row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    row.issued_at = row.expires_at - dt.timedelta(minutes=1)
    await db.commit()
    gone = await client.post("/api/auth/web/refresh", headers={**WEB, "Cookie": f"__Secure-ec_rt={tok}"})
    assert gone.status_code == 401 and gone.json()["error"]["code"] == "TOKEN_EXPIRED"


async def test_web_refresh_for_deactivated_user_is_account_inactive(client, seeded):
    r = await web_login(client)
    tok = _cookie(r)
    other = await login_tokens(client, "ADM001")
    # deactivate the web user with another admin: create one first
    from tests.test_hardening import _body

    c = await client.post(
        "/api/admin/employees",
        headers=bearer(other),
        json={**_body(seeded, "employee", "NEWADM"), "role_id": str(seeded["roles"]["admin"]), "current_password": PASSWORD},
    )
    assert c.status_code == 201, c.text
    nid = c.json()["employee"]["id"]
    pw = c.json()["temporary_password"]
    # first login with the temp password from the console, then deactivate
    w = await web_login(client, "NEWADM", pw)
    assert w.status_code == 200
    wt = _cookie(w)
    assert (await client.post(f"/api/admin/employees/{nid}/deactivate", headers=bearer(other))).status_code == 200
    out = await client.post("/api/auth/web/refresh", headers={**WEB, "Cookie": f"__Secure-ec_rt={wt}"})
    assert out.status_code == 403 and out.json()["error"]["code"] == "ACCOUNT_INACTIVE"
    assert tok  # the first admin session is unaffected
    assert (await client.post("/api/auth/web/refresh", headers={**WEB, "Cookie": f"__Secure-ec_rt={tok}"})).status_code == 200


async def test_web_change_password_sets_cookie_not_body(client):
    r = await web_login(client)
    h = {**bearer(r.json()), **WEB}
    new = "N3w!Valid-Pass99"
    ch = await client.post("/api/auth/web/change-password", headers=h, json={"current_password": PASSWORD, "new_password": new})
    assert ch.status_code == 200 and "refresh_token" not in ch.json() and ch.headers["set-cookie"].startswith("__Secure-ec_rt=")
    assert (
        await client.post(
            "/api/auth/web/change-password", headers=bearer(ch.json()), json={"current_password": new, "new_password": PASSWORD}
        )
    ).status_code == 403  # no CSRF headers


# ------------------------------------------------------------------ console data
async def test_dashboard_blocks_follow_permissions(client, admin, hr, seeded, db):
    assert (await client.get("/api/admin/dashboard", headers=hr)).status_code == 403  # HR: no console.access
    d = (await client.get("/api/admin/dashboard", headers=admin)).json()
    assert d["employees"] == {"active": 4, "inactive": 0}
    assert set(d["attendance"]) == {"checked_in", "checked_out", "late", "on_leave", "not_checked_in"}
    assert set(d["requests"]) == {"leave_pending", "permission_pending"}
    assert set(d["food"]) == {"date", "count_yes", "count_no", "count_unanswered", "tomorrow_yes"}
    assert d["announcements"] == {"active": 0} and d["password_reset_requests_open"] == 0
    assert d["sessions"]["mobile"] >= 1 and d["seats"] == {"used": 4, "total": 50, "status": "unlicensed"}
    # a custom role holding only console.access sees only the seats block
    role = Role(name="viewer", description="v", is_system=False)
    db.add(role)
    await db.flush()
    perm = (await db.execute(select(Permission).where(Permission.code == "console.access"))).scalar_one()
    db.add(RolePermission(role_id=role.id, permission_id=perm.id))
    u = User(employee_id="VIEW01", password_hash=(await db.get(User, seeded["users"]["admin"])).password_hash, full_name="Vi Ewer",
             role_id=role.id, status="active", must_change_password=False)  # fmt: skip
    db.add(u)
    await db.commit()
    v = (await client.get("/api/admin/dashboard", headers=bearer(await login_tokens(client, "VIEW01")))).json()
    assert v["seats"]["total"] == 50
    for k in ("employees", "attendance", "requests", "food", "announcements", "sessions", "password_reset_requests_open"):
        assert v[k] is None, k


async def test_attendance_day_statuses_and_scope(client, admin, hr, emp, seeded, db):
    day = dt.datetime.now(dt.UTC).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).date()
    now = dt.datetime.now(dt.UTC)
    db.add(Attendance(user_id=seeded["users"]["employee"], work_date=day, check_in_at=now, status="late", minutes_late=12))
    await db.commit()
    r = await client.get(f"/api/admin/attendance/day?date={day}&page_size=100", headers=hr)
    assert r.status_code == 200
    by = {i["user"]["employee_id"]: i for i in r.json()["items"]}
    assert set(by) == {"ADM001", "HR001", "MGR001", "EMP001"}
    assert by["EMP001"]["status"] == "late" and by["EMP001"]["minutes_late"] == 12 and by["EMP001"]["check_in_at"]
    assert set(by["EMP001"]) == {"user", "status", "check_in_at", "check_out_at", "minutes_late", "leave_type"}
    others = {by[k]["status"] for k in by if k != "EMP001"}
    assert others <= {"not_checked_in", "weekly_off", "holiday"}
    only_late = (await client.get(f"/api/admin/attendance/day?date={day}&status=late", headers=hr)).json()
    assert only_late["total"] == 1
    assert (await client.get(f"/api/admin/attendance/day?date={day}&status=checked_in", headers=hr)).json()["total"] == 1
    assert (await client.get(f"/api/admin/attendance/day?date={day}&q=emp0", headers=hr)).json()["total"] == 1
    assert (await client.get(f"/api/admin/attendance/day?date={day + dt.timedelta(days=1)}", headers=hr)).json()["error"][
        "code"
    ] == "INVALID_DATE"
    assert (await client.get(f"/api/admin/attendance/day?date={day - dt.timedelta(days=400)}", headers=hr)).status_code == 422
    assert (await client.get("/api/admin/attendance/day?status=bogus", headers=hr)).status_code == 422
    assert (await client.get("/api/admin/attendance/day", headers=emp)).status_code == 403
    # a manager only sees direct reports
    mg = bearer(await login_tokens(client, "MGR001"))
    rows = (await client.get(f"/api/admin/attendance/day?date={day}", headers=mg)).json()["items"]
    assert [i["user"]["employee_id"] for i in rows] == ["EMP001"]
    assert "coordinates" not in str(rows) and "selfie" not in str(rows)


async def test_system_health(client, admin, hr, redis, tmp_path, monkeypatch):
    assert (await client.get("/api/admin/system/health", headers=hr)).status_code == 403  # system.view is Admin only
    monkeypatch.setattr(get_settings(), "status_dir", str(tmp_path / "status"))
    monkeypatch.setattr(get_settings(), "tls_cert_file", None)
    h = (await client.get("/api/admin/system/health", headers=admin)).json()
    assert h["db"]["ok"] is True and h["db"]["latency_ms"] is not None and h["redis"] == {"ok": True}
    assert h["worker"]["ok"] is False and h["status"] == "degraded"  # no heartbeat, no backup status file
    assert h["push"] == {"configured": False} and h["tls"] == {"not_after": None, "days_left": None}
    assert h["storage"]["uploads_free_bytes"] is not None
    # worker alive + fresh backups = ok
    (tmp_path / "status").mkdir()
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "status" / "backup.json").write_text(f'{{"db_last_success_at":"{ts}","uploads_last_success_at":"{ts}"}}')
    await redis.set("ec:worker:heartbeat", ts)
    h = (await client.get("/api/admin/system/health", headers=admin)).json()
    assert h["worker"]["ok"] and h["backup"]["ok"] and h["status"] == "ok"
    # stale worker / old backup
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "status" / "backup.json").write_text(f'{{"db_last_success_at":"{old}","uploads_last_success_at":"{ts}"}}')
    assert (await client.get("/api/admin/system/health", headers=admin)).json()["backup"]["ok"] is False
    await redis.set("ec:worker:heartbeat", old)
    assert (await client.get("/api/admin/system/health", headers=admin)).json()["worker"]["ok"] is False


async def test_system_health_redis_down(client, admin, redis, monkeypatch):
    async def boom(*_a, **_k):
        raise ConnectionError("down")

    monkeypatch.setattr(redis, "ping", boom)
    h = (await client.get("/api/admin/system/health", headers=admin)).json()
    assert h["redis"] == {"ok": False} and h["status"] == "degraded"


async def test_heartbeat_job_writes_redis(redis, monkeypatch):
    from app.jobs import scheduler

    async def _r():
        return redis

    monkeypatch.setattr(scheduler, "get_redis", _r)
    await scheduler.job_heartbeat()
    assert await redis.get("ec:worker:heartbeat")


# ------------------------------------------------------------------ settings
async def test_new_settings_ranges_and_config(client, admin, emp):
    put = lambda k, v: client.put("/api/admin/settings", headers=admin, json={"settings": {k: v}})  # noqa: E731
    for k, bad in [("console.session_max_hours", 0), ("console.session_max_hours", 25), ("console.inactivity_timeout_minutes", 4),
                   ("console.inactivity_timeout_minutes", 61), ("auth.offline_cache_max_hours", 5), ("auth.offline_cache_max_hours", 721),
                   ("auth.offline_cache_max_hours", -1), ("auth.offline_cache_max_hours", True)]:  # fmt: skip
        r = await put(k, bad)
        assert r.status_code == 422, (k, bad)
    for k, ok in [("console.session_max_hours", 24), ("console.inactivity_timeout_minutes", 5), ("auth.offline_cache_max_hours", 0),
                  ("auth.offline_cache_max_hours", 12), ("auth.offline_cache_max_hours", 720)]:  # fmt: skip
        assert (await put(k, ok)).status_code == 200, (k, ok)
    await put("auth.offline_cache_max_hours", 48)
    assert (await client.get("/api/config", headers=emp)).json()["auth"]["offline_cache_max_hours"] == 48
    await put("auth.offline_cache_max_hours", 0)
    assert (await client.get("/api/config", headers=emp)).json()["auth"]["offline_cache_max_hours"] == 0
    row = (await client.get("/api/admin/settings", headers=admin)).json()["settings"]
    assert row["console.session_max_hours"] == 24


async def test_console_session_limit_uses_setting(client, db):
    db.add(Setting(key="console.session_max_hours", value=2))
    await db.commit()
    r = await web_login(client)
    assert "max-age=7200" in r.headers["set-cookie"].lower()
    row = (await db.execute(select(RefreshToken))).scalar_one()
    assert (row.expires_at - row.family_started_at) <= dt.timedelta(hours=2, seconds=5)


# ------------------------------------------------------------------ passwords
async def test_reset_password_route_is_gone_and_change_password_stays(client, emp):
    r = await client.post("/api/auth/reset-password", json={"employee_id": "EMP001", "otp": "123456", "new_password": "x"})
    assert r.status_code == 404
    ch = await client.post(
        "/api/auth/change-password", headers=emp, json={"current_password": PASSWORD, "new_password": "N3w!Valid-Pass99"}
    )
    assert ch.status_code == 200


async def test_forgot_password_per_id_day_limit_is_silent(client, admin, seeded, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "forgot_rate_eid", 100)  # isolate the daily limit
    monkeypatch.setattr(get_settings(), "forgot_rate_eid_day", 2)
    monkeypatch.setattr(get_settings(), "forgot_rate_ip", 100)
    uid = seeded["users"]["employee"]
    for i in range(3):
        r = await client.post("/api/auth/forgot-password", json={"employee_id": "EMP001"})
        assert r.status_code == 202 and r.json() == {"message": "If the account exists, a request was sent."}
        for req in (await db.execute(select(PasswordResetRequest).where(PasswordResetRequest.status == "open"))).scalars():
            req.status = "handled"
        await db.commit()
        n = len((await db.execute(select(PasswordResetRequest.id))).all())
        assert n == min(i + 1, 2)  # the 3rd request in a day is dropped silently (still 202)
    assert uid


async def test_forgot_password_hourly_id_limit(client, db):
    for _ in range(4):
        assert (await client.post("/api/auth/forgot-password", json={"employee_id": "EMP001"})).status_code == 202
        for req in (await db.execute(select(PasswordResetRequest).where(PasswordResetRequest.status == "open"))).scalars():
            req.status = "handled"
        await db.commit()
    assert len((await db.execute(select(PasswordResetRequest.id))).all()) == 3  # 3 per hour


async def test_reset_request_push_goes_to_eligible_admins_once_per_hour(client, seeded, db):
    for role in ("admin", "hr", "manager"):
        db.add(DeviceToken(user_id=seeded["users"][role], fcm_token=f"{role}-" + "x" * 40, platform="android"))
    await db.commit()
    OUTBOX.clear()
    assert (await client.post("/api/auth/forgot-password", json={"employee_id": "EMP001"})).status_code == 202
    msgs = [m for m in OUTBOX if m["payload"]["type"] == "reset_request"]
    assert len(msgs) == 1
    m = msgs[0]
    assert m["payload"]["id"] == str(seeded["users"]["employee"]) and "EMP001" in m["payload"]["body"]
    assert set(m["user_ids"]) == {
        str(seeded["users"]["admin"]),
        str(seeded["users"]["hr"]),
    }  # manager lacks employee.reset_password
    assert len(m["targets"]) == 2
    for req in (await db.execute(select(PasswordResetRequest))).scalars():
        req.status = "handled"
    await db.commit()
    OUTBOX.clear()
    await client.post("/api/auth/forgot-password", json={"employee_id": "EMP001"})
    assert [x for x in OUTBOX if x["payload"]["type"] == "reset_request"] == []  # max 1 per requester per hour


async def test_reset_request_for_elevated_requester_only_reaches_role_assign_holders(client, seeded, db):
    for role in ("admin", "hr"):
        db.add(DeviceToken(user_id=seeded["users"][role], fcm_token=f"{role}-" + "y" * 40, platform="android"))
    await db.commit()
    OUTBOX.clear()
    await client.post("/api/auth/forgot-password", json={"employee_id": "HR001"})
    m = [x for x in OUTBOX if x["payload"]["type"] == "reset_request"]
    assert len(m) == 1 and m[0]["user_ids"] == [str(seeded["users"]["admin"])]  # HR may not reset HR
