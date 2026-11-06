"""Plan 11 QA gap-fill: dashboard figures with data, web-console edge cases, company/logo edges, license edges."""

from __future__ import annotations

import datetime as dt
import io
import zoneinfo
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import get_settings
from app.models import (
    Announcement,
    Attendance,
    FoodEnrollment,
    LeaveRequest,
    LeaveType,
    RefreshToken,
    User,
)
from tests.conftest import PASSWORD, bearer
from tests.test_plan11_company_offboarding import ORIGIN, WEB, _cookie, actions, png, web_login

WRONG = "Wr0ng!Passw0rd#9"
NEWPW = "N3w!Valid-Pass99"


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(root))
    monkeypatch.setattr(get_settings(), "public_origin", ORIGIN)


@pytest.fixture
async def admin(tokens_for):
    return bearer(await tokens_for("admin"))


def rt(cookie: str) -> dict:
    return {**WEB, "Cookie": f"__Secure-ec_rt={cookie}"}


# ------------------------------------------------------------------ dashboard with data
async def test_dashboard_counts_reflect_data(client, admin, seeded, db):
    day = dt.datetime.now(dt.UTC).astimezone(zoneinfo.ZoneInfo("Asia/Kolkata")).date()
    now = dt.datetime.now(dt.UTC)
    emp, mgr, hr = (seeded["users"][k] for k in ("employee", "manager", "hr"))
    db.add(
        Attendance(
            user_id=emp,
            work_date=day,
            check_in_at=now,
            check_out_at=now + dt.timedelta(minutes=1),
            status="present",
            minutes_late=0,
        )
    )
    db.add(Attendance(user_id=mgr, work_date=day, check_in_at=now, status="late", minutes_late=9))
    db.add(FoodEnrollment(user_id=emp, date=day, need_food=True))
    db.add(FoodEnrollment(user_id=mgr, date=day, need_food=False))
    db.add(FoodEnrollment(user_id=emp, date=day + dt.timedelta(days=1), need_food=True))
    lt = LeaveType(code="CL", name="Casual", annual_quota=Decimal("12"))
    db.add(lt)
    await db.flush()
    far = day + dt.timedelta(days=60)
    db.add(LeaveRequest(user_id=emp, leave_type_id=lt.id, from_date=far, to_date=far, days=Decimal("1"), status="pending"))
    adm = seeded["users"]["admin"]
    db.add(Announcement(title="Hi", body="b", created_by=adm))
    db.add(
        Announcement(
            title="Old", body="b", created_by=adm, published_at=now - dt.timedelta(days=5), expires_at=now - dt.timedelta(days=1)
        )
    )
    db.add(Announcement(title="Gone", body="b", created_by=adm, deleted_at=now))
    (await db.get(User, hr)).status = "inactive"
    await db.commit()
    await client.post("/api/auth/forgot-password", json={"employee_id": "EMP001"})
    assert (await web_login(client)).status_code == 200
    d = (await client.get("/api/admin/dashboard", headers=admin)).json()
    assert d["date"] == day.isoformat()
    assert d["employees"] == {"active": 3, "inactive": 1}
    a = d["attendance"]
    assert a["checked_in"] == 2 and a["checked_out"] == 1 and a["late"] == 1
    assert d["requests"] == {"leave_pending": 1, "permission_pending": 0}
    assert d["food"] == {"date": day.isoformat(), "count_yes": 1, "count_no": 1, "count_unanswered": 1, "tomorrow_yes": 1}
    assert d["announcements"] == {"active": 1}
    assert d["sessions"]["web"] == 1 and d["sessions"]["mobile"] >= 1
    assert d["password_reset_requests_open"] == 1
    assert d["seats"] == {"used": 3, "total": 50, "status": "unlicensed"}  # the deactivated HR user freed a seat


async def test_dashboard_requires_auth_and_is_rate_limited(client, admin):
    assert (await client.get("/api/admin/dashboard")).status_code == 401
    codes = [(await client.get("/api/admin/dashboard", headers=admin)).status_code for _ in range(62)]
    assert codes[:60] == [200] * 60 and 429 in codes[60:]


# ------------------------------------------------------------------ web console edges
async def test_web_login_unknown_field_and_bad_credentials(client):
    r = await client.post(
        "/api/auth/web/login", headers=WEB, json={"employee_id": "ADM001", "password": PASSWORD, "platform": "android"}
    )
    assert r.status_code == 422
    r = await web_login(client, "NOBODY1", WRONG)
    assert r.status_code == 401 and "set-cookie" not in r.headers
    r = await web_login(client, "ADM001", WRONG)
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_web_login_of_inactive_user_after_correct_password(client, seeded, db):
    (await db.get(User, seeded["users"]["admin"])).status = "inactive"
    await db.commit()
    r = await web_login(client)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCOUNT_INACTIVE" and "set-cookie" not in r.headers
    assert (await web_login(client, "ADM001", WRONG)).status_code == 401


async def test_web_login_forced_password_change_flow(client, seeded, db):
    (await db.get(User, seeded["users"]["admin"])).must_change_password = True
    await db.commit()
    r = await web_login(client)
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    h = {**bearer(r.json()), **WEB}
    assert (await client.get("/api/admin/dashboard", headers=h)).status_code == 403  # blocked until changed
    bad = await client.post("/api/auth/web/change-password", headers=h, json={"current_password": WRONG, "new_password": NEWPW})
    assert bad.status_code in (400, 401, 403, 422) and "set-cookie" not in bad.headers
    ok = await client.post("/api/auth/web/change-password", headers=h, json={"current_password": PASSWORD, "new_password": NEWPW})
    assert ok.status_code == 200 and ok.json()["must_change_password"] is False
    assert (await client.get("/api/admin/dashboard", headers=bearer(ok.json()))).status_code == 200
    assert (await client.post("/api/auth/web/refresh", headers=rt(_cookie(r)))).status_code == 401  # old cookie is dead
    assert (await client.post("/api/auth/web/refresh", headers=rt(_cookie(ok)))).status_code == 200


async def test_web_change_password_without_bearer_is_401(client):
    r = await client.post(
        "/api/auth/web/change-password", headers=WEB, json={"current_password": PASSWORD, "new_password": NEWPW}
    )
    assert r.status_code == 401


async def test_web_logout_without_cookie_and_admin_revoke_of_web_session(client, admin, db):
    assert (await client.post("/api/auth/web/logout", headers=WEB)).status_code == 204
    r = await web_login(client)
    tok = _cookie(r)
    sess = (await client.get("/api/admin/sessions?client_type=web", headers=admin)).json()["items"]
    assert len(sess) == 1 and sess[0]["client_type"] == "web" and sess[0]["platform"] == "web"
    assert (await client.delete(f"/api/admin/sessions/{sess[0]['id']}", headers=admin)).status_code == 204
    out = await client.post("/api/auth/web/refresh", headers=rt(tok))
    assert out.status_code == 401 and out.json()["error"]["code"] == "SESSION_REVOKED"
    assert (await client.get("/api/auth/me", headers=bearer(r.json()))).status_code == 401
    assert "session.revoke" in await actions(db)


async def test_web_refresh_reuse_of_rotated_cookie_revokes_family(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "refresh_grace_seconds", 0)  # outside the lost-response grace window
    first = _cookie(await web_login(client))
    second = _cookie(await client.post("/api/auth/web/refresh", headers=rt(first)))
    assert (await client.post("/api/auth/web/refresh", headers=rt(first))).status_code == 401  # replay
    assert (await client.post("/api/auth/web/refresh", headers=rt(second))).status_code == 401  # family is gone
    live = (await db.execute(select(RefreshToken).where(RefreshToken.revoked_at.is_(None)))).scalars().all()
    assert live == []


# ------------------------------------------------------------------ company edges
async def test_company_name_length_and_logo_edges(client, admin, tokens_for):
    put = lambda n: client.put("/api/admin/company", headers=admin, json={"name": n})  # noqa: E731
    assert (await put("ab")).status_code == 200
    assert (await put("x" * 100)).status_code == 200
    assert (await put("x" * 101)).status_code == 422
    assert (await put("<b></b>  ")).status_code == 422
    assert (await client.put("/api/admin/company", headers=admin, json={})).status_code == 422
    assert (await client.put("/api/admin/company", json={"name": "Acme"})).status_code == 401
    first = (await client.get("/api/admin/company", headers=admin)).json()["setup_completed_at"]
    await put("Second Name")
    assert (await client.get("/api/admin/company", headers=admin)).json()["setup_completed_at"] == first  # set once
    assert (await client.post("/api/admin/company/logo", headers=admin)).status_code == 422  # no file part
    v0 = (await client.get("/api/admin/company", headers=admin)).json()["logo_version"]
    assert (await client.delete("/api/admin/company/logo", headers=admin)).status_code == 204
    assert (await client.get("/api/admin/company", headers=admin)).json()["logo_version"] == v0  # no-op
    up = await client.post("/api/admin/company/logo", headers=admin, files={"file": ("l.png", png((40, 30)), "image/png")})
    assert up.status_code == 200
    got = await client.get("/api/public/company/logo")
    assert Image.open(io.BytesIO(got.content)).size == (40, 30)  # no upscaling
    assert (await client.get("/api/public/company/logo", headers={"If-None-Match": '"deadbeef"'})).status_code == 200
    me = (await client.get("/api/auth/me", headers=admin)).json()["company"]
    assert me["logo_url"] == "/api/public/company/logo?v=1" and me["logo_version"] == 1
    assert (await client.get("/api/public/company")).json()["logo_url"] == "/api/public/company/logo?v=1"
    corrupt = b"\x89PNG\r\n\x1a\n" + b"junk"
    bad = await client.post("/api/admin/company/logo", headers=admin, files={"file": ("l.png", corrupt, "image/png")})
    assert 400 <= bad.status_code < 500  # never a 500
    hr = bearer(await tokens_for("hr"))
    assert (await client.put("/api/admin/company", headers=hr, json={"name": "Acme"})).status_code == 403
    assert (await client.get("/api/admin/company", headers=hr)).status_code == 403


# ------------------------------------------------------------------ license / seat edges
async def test_seat_limit_exactly_fifty_unlicensed(client, admin, seeded, redis):
    from tests.test_plan11_license import make_user

    for i in range(46):  # 4 seeded + 46 = 50
        r = await make_user(client, admin, seeded, f"BULK{i:03d}")
        assert r.status_code == 201, (i, r.text)
        for k in await redis.keys("rl:admin:create:*"):  # stay under the 30/min create limit
            await redis.delete(k)
    full = await make_user(client, admin, seeded, "BULK999")
    assert full.status_code == 409
    assert full.json()["error"]["details"] == [
        {"field": "max_users", "message": "50"},
        {"field": "active_users", "message": "50"},
    ]
    lic = (await client.get("/api/admin/license", headers=admin)).json()
    assert lic["max_users"] == 50 and lic["seats_available"] == 0 and lic["expires_at"] is None and lic["status"] == "unlicensed"
    dash = (await client.get("/api/admin/dashboard", headers=admin)).json()
    assert dash["seats"] == {"used": 50, "total": 50, "status": "unlicensed"}


async def test_license_endpoints_need_auth_and_password(client, admin):
    assert (await client.get("/api/admin/license")).status_code == 401
    assert (await client.put("/api/admin/license", json={"license_key": "x", "current_password": PASSWORD})).status_code == 401
    r = await client.put("/api/admin/license", headers=admin, json={"license_key": "ECL1.abc.def"})  # no current_password
    assert r.status_code == 422
    r = await client.put("/api/admin/license", headers=admin, json={"license_key": "ECL1.abc.def", "current_password": WRONG})
    assert r.status_code == 403 and r.json()["error"]["code"] == "REAUTH_FAILED"
