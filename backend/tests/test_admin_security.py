"""docs/reviews/06-admin-security.md fixes: H1, M1-M7, L1-L7 (SQLite + fakeredis only)."""

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit, security
from app.core.redis import get_redis
from app.models import AuditLog, DeviceToken, Permission, Role, RolePermission, User
from app.models.users import EmployeeProfile
from app.services import admin_security
from app.services.notifications import OUTBOX

from .conftest import PASSWORD, bearer, do_login, login_tokens

FAKE = "00000000-0000-0000-0000-000000000001"
ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------ helpers
async def _tok(client, eid):
    return bearer(await login_tokens(client, eid))


def _body(seeded, role="employee", eid="NEW001", **over):
    return {
        "employee_id": eid,
        "full_name": "Some Person",
        "official_email": f"{eid.lower()}@corp-mail.com",
        "role_id": str(seeded["roles"][role]),
        "department_id": str(seeded["dept"]),
        "shift_id": str(seeded["shift"]),
        **over,
    }


def _put(seeded, role="employee", **over):
    return {
        "full_name": "Test Employee",
        "official_email": "employee@corp-mail.com",
        "department_id": str(seeded["dept"]),
        "shift_id": str(seeded["shift"]),
        "role_id": str(seeded["roles"][role]),
        "manager_id": str(seeded["users"]["manager"]),
        **over,
    }


async def _custom_role(db, name, codes):
    role = Role(name=name, description=name, is_system=False)
    db.add(role)
    await db.flush()
    perms = (await db.execute(select(Permission).where(Permission.code.in_(codes)))).scalars().all()
    assert len(perms) == len(codes)
    for p in perms:
        db.add(RolePermission(role_id=role.id, permission_id=p.id))
    await db.commit()
    return role


async def _user(db, eid, role_id, *, manager_id=None, dept=None):
    u = User(
        employee_id=eid,
        password_hash=security.hash_password(PASSWORD),
        full_name=f"User {eid}",
        official_email=f"{eid.lower()}@corp-mail.com",
        role_id=role_id,
        department_id=dept,
        must_change_password=False,
        status="active",
        manager_id=manager_id,
    )
    db.add(u)
    await db.flush()
    db.add(EmployeeProfile(user_id=u.id))
    await db.commit()
    return u


class _BrokenRedis:
    def pipeline(self, *a, **k):
        raise ConnectionError("down")

    async def incr(self, *a, **k):
        raise ConnectionError("down")

    async def delete(self, *a, **k):
        raise ConnectionError("down")


async def _audit(db, action):
    await db.rollback()
    return (await db.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))).scalars().all()


def _err(r):
    return r.json()["error"]["code"]


# ------------------------------------------------------------------ H1
@pytest.mark.parametrize("target", ["manager", "hr", "admin"])
@pytest.mark.parametrize("action", ["activate", "deactivate", "unlock"])
async def test_h1_hr_cannot_activate_deactivate_unlock_elevated(client, seeded, target, action):
    hr = await _tok(client, "HR001")
    r = await client.post(f"/api/admin/employees/{seeded['users'][target]}/{action}", headers=hr)
    assert r.status_code == 403 and _err(r) == "FORBIDDEN"


async def test_h1_hr_still_manages_plain_employees_and_admin_manages_everyone(client, seeded):
    hr = await _tok(client, "HR001")
    admin = await _tok(client, "ADM001")
    emp = seeded["users"]["employee"]
    assert (await client.post(f"/api/admin/employees/{emp}/deactivate", headers=hr)).status_code == 200
    r = await client.post(f"/api/admin/employees/{emp}/activate", headers=hr)
    assert r.status_code == 200 and r.json()["status"] == "active"
    # deactivated by Admin, HR cannot bring the HR colleague back
    hr2 = await client.post(f"/api/admin/employees/{seeded['users']['manager']}/deactivate", headers=admin)
    assert hr2.status_code == 200
    assert (await client.post(f"/api/admin/employees/{seeded['users']['manager']}/activate", headers=hr)).status_code == 403
    assert (await client.post(f"/api/admin/employees/{seeded['users']['manager']}/activate", headers=admin)).status_code == 200


async def test_h1_reactivation_forces_password_change(client, seeded):
    admin = await _tok(client, "ADM001")
    emp = seeded["users"]["employee"]
    await client.post(f"/api/admin/employees/{emp}/deactivate", headers=admin)
    await client.post(f"/api/admin/employees/{emp}/activate", headers=admin)
    login = await do_login(client, "EMP001")
    assert login.status_code == 200 and login.json()["must_change_password"] is True


# ------------------------------------------------------------------ M1
async def test_m1_reset_elevated_needs_reauth(client, seeded, db):
    admin = await _tok(client, "ADM001")
    hr_id = seeded["users"]["hr"]
    url = f"/api/admin/employees/{hr_id}/reset-password"
    missing = await client.post(url, headers=admin)
    assert missing.status_code == 422 and _err(missing) == "VALIDATION_ERROR"
    assert missing.json()["error"]["details"][0]["field"] == "current_password"
    empty = await client.post(url, headers=admin, json={})
    assert empty.status_code == 422
    wrong = await client.post(url, headers=admin, json={"current_password": "Wrong!Passw0rd#9"})
    assert wrong.status_code == 403 and _err(wrong) == "REAUTH_FAILED"
    ok = await client.post(url, headers=admin, json={"current_password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["temporary_password"]
    # plain employees need no password
    assert (
        await client.post(f"/api/admin/employees/{seeded['users']['employee']}/reset-password", headers=admin)
    ).status_code == 200
    # the password never reaches the audit log; the failed attempt is recorded
    dump = str([(a.before, a.after) for a in (await db.execute(select(AuditLog))).scalars()])
    assert PASSWORD not in dump and "Wrong!Passw0rd#9" not in dump
    assert len(await _audit(db, "admin.reauth_failed")) == 1


async def test_m1_self_reset_blocked(client, seeded):
    admin = await _tok(client, "ADM001")
    r = await client.post(
        f"/api/admin/employees/{seeded['users']['admin']}/reset-password", headers=admin, json={"current_password": PASSWORD}
    )
    assert r.status_code == 403 and _err(r) == "FORBIDDEN"
    # HR resetting itself is blocked on the admin endpoint too
    hr = await _tok(client, "HR001")
    assert (await client.post(f"/api/admin/employees/{seeded['users']['hr']}/reset-password", headers=hr)).status_code == 403


async def test_m1_create_admin_capable_needs_reauth(client, seeded):
    admin = await _tok(client, "ADM001")
    r = await client.post("/api/admin/employees", headers=admin, json=_body(seeded, "admin", "ADM002"))
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "current_password"
    bad = await client.post(
        "/api/admin/employees", headers=admin, json=_body(seeded, "admin", "ADM002", current_password="Nope!Passw0rd#1")
    )
    assert bad.status_code == 403 and _err(bad) == "REAUTH_FAILED"
    ok = await client.post(
        "/api/admin/employees", headers=admin, json=_body(seeded, "admin", "ADM002", current_password=PASSWORD)
    )
    assert ok.status_code == 201 and "current_password" not in str(ok.json())
    # roles without role.assign / role.manage (hr, manager, employee) need no re-confirmation
    assert (await client.post("/api/admin/employees", headers=admin, json=_body(seeded, "hr", "HRX001"))).status_code == 201


async def test_m1_privilege_check_comes_before_reauth(client, seeded):
    hr = await _tok(client, "HR001")
    r = await client.post("/api/admin/employees", headers=hr, json=_body(seeded, "admin", "ADM002"))
    assert r.status_code == 403 and _err(r) == "FORBIDDEN"


async def test_m1_update_to_or_of_admin_capable_role_needs_reauth(client, seeded, db):
    admin = await _tok(client, "ADM001")
    emp = seeded["users"]["employee"]
    r = await client.put(f"/api/admin/employees/{emp}", headers=admin, json=_put(seeded, "admin"))
    assert r.status_code == 422
    ok = await client.put(f"/api/admin/employees/{emp}", headers=admin, json=_put(seeded, "admin", current_password=PASSWORD))
    assert ok.status_code == 200 and ok.json()["role"]["name"] == "admin"
    # editing an existing admin account (resulting role admin-capable) also needs it
    again = await client.put(f"/api/admin/employees/{emp}", headers=admin, json=_put(seeded, "admin"))
    assert again.status_code == 422
    # demoting needs nothing
    down = await client.put(f"/api/admin/employees/{emp}", headers=admin, json=_put(seeded, "employee"))
    assert down.status_code == 200
    dump = str([(a.before, a.after) for a in (await db.execute(select(AuditLog))).scalars()])
    assert PASSWORD not in dump


async def test_m1_set_role_permissions_needs_reauth(client, seeded):
    admin = await _tok(client, "ADM001")
    role = (await client.post("/api/admin/roles", headers=admin, json={"name": "auditor", "permission_codes": []})).json()
    url = f"/api/admin/roles/{role['id']}/permissions"
    assert (await client.put(url, headers=admin, json={"permission_codes": ["audit.view"]})).status_code == 422
    wrong = await client.put(url, headers=admin, json={"permission_codes": ["audit.view"], "current_password": "Nope!Passw0rd#1"})
    assert wrong.status_code == 403 and _err(wrong) == "REAUTH_FAILED"
    ok = await client.put(url, headers=admin, json={"permission_codes": ["audit.view"], "current_password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["permission_codes"] == ["audit.view"]


async def test_m1_reauth_rate_limit_5_per_minute_per_actor(client, seeded):
    admin = await _tok(client, "ADM001")
    url = f"/api/admin/employees/{seeded['users']['hr']}/reset-password"
    codes = []
    for _ in range(5):
        codes.append((await client.post(url, headers=admin, json={"current_password": "Nope!Passw0rd#1"})).status_code)
    assert codes == [403] * 5
    sixth = await client.post(url, headers=admin, json={"current_password": PASSWORD})
    assert sixth.status_code == 429 and _err(sixth) == "RATE_LIMITED"


async def test_m1_reauth_fails_closed_when_redis_is_down(client, app, seeded):
    admin = await _tok(client, "ADM001")
    app.dependency_overrides[get_redis] = lambda: _BrokenRedis()
    r = await client.post(
        f"/api/admin/employees/{seeded['users']['hr']}/reset-password", headers=admin, json={"current_password": PASSWORD}
    )
    assert r.status_code == 503 and _err(r) == "TEMPORARILY_UNAVAILABLE"


async def test_m1_admins_are_pushed_on_admin_account_events(client, seeded):
    admin = await _tok(client, "ADM001")
    OUTBOX.clear()
    r = await client.post("/api/admin/employees", headers=admin, json=_body(seeded, "admin", "ADM002", current_password=PASSWORD))
    assert r.status_code == 201
    msg = [m for m in OUTBOX if m["payload"]["type"] == "admin_alert"]
    assert len(msg) == 1 and str(seeded["users"]["admin"]) in msg[0]["user_ids"]
    assert "ADM002" in msg[0]["payload"]["body"] and PASSWORD not in str(msg) and r.json()["temporary_password"] not in str(msg)
    # reset of that admin account and promotion of a plain user
    new_id = r.json()["employee"]["id"]
    OUTBOX.clear()
    await client.post(f"/api/admin/employees/{new_id}/reset-password", headers=admin, json={"current_password": PASSWORD})
    assert [m for m in OUTBOX if m["payload"]["type"] == "admin_alert"]
    OUTBOX.clear()
    await client.put(
        f"/api/admin/employees/{seeded['users']['employee']}",
        headers=admin,
        json=_put(seeded, "admin", current_password=PASSWORD),
    )
    assert [m for m in OUTBOX if m["payload"]["type"] == "admin_alert"]
    # no alert for ordinary accounts
    OUTBOX.clear()
    await client.post("/api/admin/employees", headers=admin, json=_body(seeded, "employee", "EMP200"))
    assert not [m for m in OUTBOX if m["payload"]["type"] == "admin_alert"]


# ------------------------------------------------------------------ M2
async def test_m2_team_scoped_custom_role_cannot_act_on_others(client, seeded, db):
    role = await _custom_role(
        db,
        "teamlead",
        ["employee.view", "employee.deactivate", "employee.unlock", "employee.reset_password", "leave_balance.manage"],
    )
    lead = await _user(db, "LEAD01", role.id)
    mine = await _user(db, "EMP002", seeded["roles"]["employee"], manager_id=lead.id)
    h = await _tok(client, "LEAD01")
    other = seeded["users"]["employee"]
    for path in ("deactivate", "unlock", "reset-password", "activate"):
        assert (await client.post(f"/api/admin/employees/{other}/{path}", headers=h)).status_code == 404, path
    assert (await client.get(f"/api/admin/employees/{other}/leave-balances", headers=h)).status_code == 404
    put = await client.put(
        f"/api/admin/employees/{other}/leave-balances",
        headers=h,
        json={"year": 2030, "items": [{"leave_type_id": FAKE, "quota": 1}]},
    )
    assert put.status_code == 404
    assert (await client.get(f"/api/admin/employees/{mine.id}/leave-balances", headers=h)).status_code == 200
    # reset requests: only the direct reports (asked while EMP002 is still active; deactivated accounts get none)
    await client.post("/api/auth/forgot-password", json={"employee_id": "EMP001"})
    await client.post("/api/auth/forgot-password", json={"employee_id": "EMP002"})
    lst = (await client.get("/api/admin/password-reset-requests", headers=h)).json()
    assert lst["total"] == 1 and lst["items"][0]["user"]["employee_id"] == "EMP002"
    assert (await client.post(f"/api/admin/employees/{mine.id}/deactivate", headers=h)).status_code == 200


# ------------------------------------------------------------------ M3
async def test_m3_balance_edit_guards(client, seeded):
    hr = await _tok(client, "HR001")
    admin = await _tok(client, "ADM001")
    lt = (
        await client.post(
            "/api/admin/leave-types", headers=admin, json={"code": "cl", "name": "Casual", "annual_quota": 12, "is_paid": True}
        )
    ).json()["id"]
    body = {"year": 2030, "items": [{"leave_type_id": lt, "quota": 20}]}
    assert (
        await client.put(f"/api/admin/employees/{seeded['users']['hr']}/leave-balances", headers=hr, json=body)
    ).status_code == 403
    assert (
        await client.put(f"/api/admin/employees/{seeded['users']['manager']}/leave-balances", headers=hr, json=body)
    ).status_code == 403
    assert (
        await client.put(f"/api/admin/employees/{seeded['users']['admin']}/leave-balances", headers=hr, json=body)
    ).status_code == 403
    assert (
        await client.put(f"/api/admin/employees/{seeded['users']['admin']}/leave-balances", headers=admin, json=body)
    ).status_code == 403  # self
    assert (
        await client.put(f"/api/admin/employees/{seeded['users']['employee']}/leave-balances", headers=hr, json=body)
    ).status_code == 200
    assert (
        await client.put(f"/api/admin/employees/{seeded['users']['manager']}/leave-balances", headers=admin, json=body)
    ).status_code == 200


# ------------------------------------------------------------------ M4
async def test_m4_personal_fields_need_view_sensitive_and_employee_is_told(client, seeded, db):
    role = await _custom_role(db, "editor", ["employee.view", "employee.update"])
    await _user(db, "EDIT01", role.id)
    editor = await _tok(client, "EDIT01")
    emp = seeded["users"]["employee"]
    r = await client.put(f"/api/admin/employees/{emp}", headers=editor, json=_put(seeded, phone="+919999911111"))
    assert r.status_code == 403 and _err(r) == "FORBIDDEN"
    r = await client.put(f"/api/admin/employees/{emp}", headers=editor, json=_put(seeded, address=None))
    assert r.status_code == 403  # an explicit null is a change as well
    ok = await client.put(f"/api/admin/employees/{emp}", headers=editor, json=_put(seeded))
    assert ok.status_code == 200 and ok.json()["profile"] is None
    hr = await _tok(client, "HR001")
    OUTBOX.clear()
    r = await client.put(
        f"/api/admin/employees/{emp}", headers=hr, json=_put(seeded, phone="+919999911111", address="12 Secret Street")
    )
    assert r.status_code == 200
    msg = [m for m in OUTBOX if m["payload"]["type"] == "profile_changed"]
    assert len(msg) == 1 and msg[0]["user_ids"] == [str(emp)]
    body = msg[0]["payload"]["body"]
    assert "phone" in body and "address" in body and "9999911111" not in str(msg) and "Secret" not in str(msg)


# ------------------------------------------------------------------ M5
async def test_m5_profile_containment(client, seeded):
    hr = await _tok(client, "HR001")
    admin = await _tok(client, "ADM001")
    emp = seeded["users"]["employee"]
    put = await client.put(
        f"/api/admin/employees/{emp}",
        headers=admin,
        json=_put(seeded, phone="+919999911111"),
    )
    assert put.status_code == 200 and put.json()["profile"]["phone"] == "+919999911111"
    g = await client.get(f"/api/admin/employees/{emp}", headers=hr)
    prof = g.json()["profile"]
    assert prof["phone"] == "+919999911111" and "home_location" not in prof  # the field no longer exists
    # HR does not get the profile of an Admin (or any account beyond its own permissions)
    ga = await client.get(f"/api/admin/employees/{seeded['users']['admin']}", headers=hr)
    assert ga.status_code == 200 and ga.json()["profile"] is None
    # the PUT response applies the same rules
    pr = await client.put(f"/api/admin/employees/{emp}", headers=hr, json=_put(seeded))
    assert "home_location" not in pr.json()["profile"]


# ------------------------------------------------------------------ M6
async def _fill(redis, key, n, window=60):
    for _ in range(n):
        await rate_limit.hit(redis, key, 10_000, window)


async def test_m6_rate_limits(client, seeded, redis):
    admin = await _tok(client, "ADM001")
    aid = seeded["users"]["admin"]
    # apply-defaults: 2/min
    codes = [
        (await client.post("/api/admin/leave-balances/apply-defaults", headers=admin, json={"year": 2031})).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]
    # audit viewer: 30/min
    await _fill(redis, f"rl:admin:auditview:{aid}", 30)
    assert (await client.get("/api/admin/audit-logs", headers=admin)).status_code == 429
    # role writes: 10/min
    await _fill(redis, f"rl:admin:rolewrite:{aid}", 10)
    assert (await client.post("/api/admin/roles", headers=admin, json={"name": "abc"})).status_code == 429
    assert (await client.delete(f"/api/admin/roles/{FAKE}", headers=admin)).status_code == 429
    # create: 30/min
    await _fill(redis, f"rl:admin:create:{aid}", 30)
    assert (await client.post("/api/admin/employees", headers=admin, json=_body(seeded))).status_code == 429
    # employee detail with profile: 60/min
    await _fill(redis, f"rl:admin:detail:min:{aid}", 60)
    r = await client.get(f"/api/admin/employees/{seeded['users']['employee']}", headers=admin)
    assert r.status_code == 429 and _err(r) == "RATE_LIMITED"


async def test_m6_detail_with_profile_and_reset_fail_closed(client, app, seeded):
    admin = await _tok(client, "ADM001")
    hr = await _tok(client, "HR001")
    mgr = await _tok(client, "MGR001")
    app.dependency_overrides[get_redis] = lambda: _BrokenRedis()
    emp = seeded["users"]["employee"]
    r = await client.get(f"/api/admin/employees/{emp}", headers=admin)
    assert r.status_code == 503 and _err(r) == "TEMPORARILY_UNAVAILABLE"
    r = await client.post(f"/api/admin/employees/{emp}/reset-password", headers=hr)
    assert r.status_code == 503
    # a view without personal data does not touch the limiter
    assert (await client.get(f"/api/admin/employees/{emp}", headers=mgr)).status_code == 200


async def test_m6_many_sensitive_views_alert_the_admins(client, seeded, monkeypatch):
    monkeypatch.setattr(admin_security, "get_settings", lambda: SimpleNamespace(sensitive_view_alert_per_hour=2))
    hr = await _tok(client, "HR001")
    OUTBOX.clear()
    for _ in range(2):
        await client.get(f"/api/admin/employees/{seeded['users']['employee']}", headers=hr)
    assert not [m for m in OUTBOX if m["payload"]["type"] == "admin_alert"]
    await client.get(f"/api/admin/employees/{seeded['users']['employee']}", headers=hr)
    alerts = [m for m in OUTBOX if m["payload"]["type"] == "admin_alert"]
    assert len(alerts) == 1 and str(seeded["users"]["admin"]) in alerts[0]["user_ids"]
    await client.get(f"/api/admin/employees/{seeded['users']['employee']}", headers=hr)
    assert len([m for m in OUTBOX if m["payload"]["type"] == "admin_alert"]) == 1  # once per crossing


# ------------------------------------------------------------------ M7
async def _set_expiry(db, eid, when):
    await db.rollback()
    u = (
        (await db.execute(select(User).where(User.employee_id == eid).execution_options(populate_existing=True)))
        .unique()
        .scalar_one()
    )
    u.temp_password_expires_at = when
    await db.commit()


async def test_m7_temp_password_expires_after_72h(client, seeded, db):
    admin = await _tok(client, "ADM001")
    r = await client.post("/api/admin/employees", headers=admin, json=_body(seeded, "employee", "EMP300"))
    temp = r.json()["temporary_password"]
    await db.rollback()
    u = (await db.execute(select(User).where(User.employee_id == "EMP300"))).unique().scalar_one()
    left = u.temp_password_expires_at.replace(tzinfo=dt.UTC) - dt.datetime.now(dt.UTC)
    assert dt.timedelta(hours=71, minutes=55) < left <= dt.timedelta(hours=72)
    assert (await do_login(client, "EMP300", temp)).status_code == 200  # still valid
    await _set_expiry(db, "EMP300", dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
    expired = await do_login(client, "EMP300", temp)
    assert expired.status_code == 403 and _err(expired) == "TEMP_PASSWORD_EXPIRED"
    # a wrong password still looks like any other failure (no state leak); expiry is no failed attempt
    bad = await do_login(client, "EMP300", "Wrong!Passw0rd#1")
    assert bad.status_code == 401 and _err(bad) == "INVALID_CREDENTIALS"
    await db.rollback()
    u = (
        (await db.execute(select(User).where(User.employee_id == "EMP300").execution_options(populate_existing=True)))
        .unique()
        .scalar_one()
    )
    assert u.failed_login_count == 1
    assert any(a.after.get("reason") == "temp_password_expired" for a in await _audit(db, "auth.login_failed"))
    # an admin reset issues a fresh 72 h password
    uid = r.json()["employee"]["id"]
    new = (await client.post(f"/api/admin/employees/{uid}/reset-password", headers=admin)).json()["temporary_password"]
    assert (await do_login(client, "EMP300", new)).status_code == 200


async def test_m7_change_password_clears_the_deadline_and_normal_users_are_unaffected(client, seeded, db):
    admin = await _tok(client, "ADM001")
    r = await client.post("/api/admin/employees", headers=admin, json=_body(seeded, "employee", "EMP301"))
    t = (await do_login(client, "EMP301", r.json()["temporary_password"])).json()
    ch = await client.post(
        "/api/auth/change-password",
        headers=bearer(t),
        json={"current_password": r.json()["temporary_password"], "new_password": "N3w!Valid-Pass99"},
    )
    assert ch.status_code == 200
    await db.rollback()
    u = (
        (await db.execute(select(User).where(User.employee_id == "EMP301").execution_options(populate_existing=True)))
        .unique()
        .scalar_one()
    )
    assert u.temp_password_expires_at is None and u.must_change_password is False
    assert (await do_login(client, "EMP301", "N3w!Valid-Pass99")).status_code == 200
    # seeded accounts have no deadline
    assert (await do_login(client, "EMP001")).status_code == 200


# ------------------------------------------------------------------ L1
async def test_l1_unknown_fields_rejected_and_text_cleaned(client, seeded):
    admin = await _tok(client, "ADM001")
    emp = seeded["users"]["employee"]
    assert (await client.put(f"/api/admin/employees/{emp}", headers=admin, json=_put(seeded, nope=1))).status_code == 422
    assert (
        await client.put(f"/api/admin/employees/{emp}", headers=admin, json=_put(seeded, show_celebrations=False))
    ).status_code == 422
    assert (await client.post("/api/admin/employees", headers=admin, json=_body(seeded, nope=1))).status_code == 422
    assert (await client.post("/api/admin/employees", headers=admin, json=_body(seeded, full_name="   "))).status_code == 422
    r = await client.post(
        "/api/admin/employees", headers=admin, json=_body(seeded, "employee", "EMP310", full_name="<b>Bob</b>‮  Ray")
    )
    assert r.status_code == 201 and r.json()["employee"]["full_name"] == "Bob Ray"
    u = await client.put(f"/api/admin/employees/{emp}", headers=admin, json=_put(seeded, full_name="<script>x()</script>Al"))
    assert u.json()["full_name"] == "Al"
    # create phone is normalised / validated like the profile phone
    assert (
        await client.post("/api/admin/employees", headers=admin, json=_body(seeded, "employee", "EMP311", phone="abc"))
    ).status_code == 422
    ok = await client.post(
        "/api/admin/employees", headers=admin, json=_body(seeded, "employee", "EMP312", phone="+91 99999-11111")
    )
    g = await client.get(f"/api/admin/employees/{ok.json()['employee']['id']}", headers=admin)
    assert g.json()["profile"]["phone"] == "+919999911111"


async def test_l1_self_service_profile_still_accepts_show_celebrations(client):
    emp = await _tok(client, "EMP001")
    r = await client.put("/api/me/profile", headers=emp, json={"show_celebrations": False})
    assert r.status_code == 200 and r.json()["show_celebrations"] is False
    assert (await client.put("/api/me/profile", headers=emp, json={"bogus": 1})).status_code == 422


# ------------------------------------------------------------------ L2
async def test_l2_races_answer_409_not_500(client, seeded, monkeypatch):
    admin = await _tok(client, "ADM001")
    emp = seeded["users"]["employee"]
    real_flush = AsyncSession.flush

    async def boom(self, *a, **k):
        raise IntegrityError("x", {}, Exception("dup"))

    monkeypatch.setattr(AsyncSession, "flush", boom)
    r = await client.put(f"/api/admin/employees/{emp}", headers=admin, json=_put(seeded, full_name="Renamed"))
    assert r.status_code == 409 and _err(r) == "EMAIL_EXISTS"
    # first insert of a settings key raced with another admin and the row is not visible: clean 409
    s = await client.put("/api/admin/settings", headers=admin, json={"settings": {"office.radius_m": 300}})
    assert s.status_code == 409 and _err(s) == "CONFLICT"
    monkeypatch.setattr(AsyncSession, "flush", real_flush)
    role = (await client.post("/api/admin/roles", headers=admin, json={"name": "temp_role"})).json()

    async def fail_delete(self, *a, **k):
        raise IntegrityError("x", {}, Exception("fk"))

    monkeypatch.setattr(AsyncSession, "delete", fail_delete)
    d = await client.delete(f"/api/admin/roles/{role['id']}", headers=admin)
    assert d.status_code == 409 and _err(d) == "IN_USE"


# ------------------------------------------------------------------ L3
async def test_l3_audit_viewer_reads_are_audited(client, seeded, db):
    admin = await _tok(client, "ADM001")
    r = await client.get("/api/admin/audit-logs?action=auth&entity_type=user&actor=ADM001&page_size=5", headers=admin)
    assert r.status_code == 200 and "audit.view" not in {i["action"] for i in r.json()["items"]}  # not listed in its own result
    rows = await _audit(db, "audit.view")
    assert len(rows) == 1
    f = rows[0].after["filters"]
    assert f["action"] == "auth" and f["entity_type"] == "user" and f["actor"] == "ADM001" and f["page_size"] == 5
    assert rows[0].actor_id == seeded["users"]["admin"]
    again = await client.get("/api/admin/audit-logs?action=audit", headers=admin)
    assert again.json()["total"] == 1  # the first view is now visible


# ------------------------------------------------------------------ L4
async def test_l4_deactivate_and_delete_drop_device_tokens(client, seeded, db):
    admin = await _tok(client, "ADM001")
    for i, route in enumerate(("deactivate", "delete")):
        eid = f"TRP{i}"
        u = await _user(db, eid, seeded["roles"]["employee"])
        db.add(DeviceToken(user_id=u.id, fcm_token=f"tok-{i}", platform="android"))
        await db.commit()
        if route == "deactivate":
            assert (await client.post(f"/api/admin/employees/{u.id}/deactivate", headers=admin)).status_code == 200
        else:
            assert (await client.delete(f"/api/admin/employees/{u.id}", headers=admin)).status_code == 204
        await db.rollback()
        assert (await db.execute(select(DeviceToken).where(DeviceToken.user_id == u.id))).first() is None


# ------------------------------------------------------------------ L5
async def test_l5_team_scoped_viewer_gets_reduced_shape(client, seeded):
    mgr = await _tok(client, "MGR001")
    hr = await _tok(client, "HR001")
    emp = seeded["users"]["employee"]
    for url in (f"/api/admin/employees/{emp}", "/api/admin/employees"):
        r = await client.get(url, headers=mgr)
        item = r.json() if "items" not in r.json() else r.json()["items"][0]
        assert r.status_code == 200 and not {"last_login_at", "is_locked", "locked_until"} & set(item), url
        assert item["employee_id"] and item["role"]
    full = (await client.get(f"/api/admin/employees/{emp}", headers=hr)).json()
    assert {"last_login_at", "is_locked", "locked_until"} <= set(full)
    lst = (await client.get("/api/admin/employees", headers=hr)).json()["items"][0]
    assert {"last_login_at", "is_locked", "locked_until"} <= set(lst)


# ------------------------------------------------------------------ L6
async def test_l6_blood_group_and_emergency_relationship_encrypted_at_rest(client, seeded, db):
    emp = await _tok(client, "EMP001")
    r = await client.put(
        "/api/me/profile",
        headers=emp,
        json={"blood_group": "AB-", "emergency_contact": {"name": "Mum", "relationship": "mother", "phone": "+919888877777"}},
    )
    assert r.status_code == 200 and r.json()["blood_group"] == "AB-"
    assert r.json()["emergency_contact"]["relationship"] == "mother"
    raw = (
        await db.execute(
            text("SELECT blood_group_enc, emergency_relationship_enc FROM employee_profiles WHERE user_id = :u"),
            {"u": seeded["users"]["employee"].hex},
        )
    ).one()
    assert raw[0] and raw[1] and b"AB-" not in bytes(raw[0]) and b"mother" not in bytes(raw[1])
    assert bytes(raw[0]).startswith(b"gAAAA")  # Fernet token
    cols = {c.name for c in EmployeeProfile.__table__.columns}
    assert "blood_group" not in cols and "emergency_relationship" not in cols
    assert (await client.put("/api/me/profile", headers=emp, json={"blood_group": "Z+"})).status_code == 422  # app-level check


def _migration():
    sys.path.insert(0, str(ROOT / "database"))
    try:
        spec = importlib.util.spec_from_file_location(
            "mig0013", ROOT / "database" / "alembic" / "versions" / "0013_admin_security_hardening.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(ROOT / "database"))


def test_l6_migration_backfill_encrypts_existing_rows_and_is_safe_on_empty_tables():
    from app.core.crypto import decrypt_bytes

    mig = _migration()
    assert (mig.revision, mig.down_revision) == ("0013", "0012")
    eng = sa.create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(
            sa.text(
                "CREATE TABLE employee_profiles (user_id TEXT PRIMARY KEY, blood_group TEXT, emergency_relationship TEXT, "
                "blood_group_enc BLOB, emergency_relationship_enc BLOB)"
            )
        )
        mig._backfill(c)  # empty table: nothing to do, no error
        for i in range(1200):  # more than one batch
            c.execute(
                sa.text("INSERT INTO employee_profiles (user_id, blood_group, emergency_relationship) VALUES (:i, :b, :r)"),
                {"i": f"{i:05d}", "b": "O+" if i % 2 else None, "r": "mother" if i % 3 == 0 else None},
            )
        c.execute(sa.text("INSERT INTO employee_profiles (user_id) VALUES ('zzzzz')"))  # all-null row is left alone
        mig._backfill(c)
        rows = c.execute(
            sa.text(
                "SELECT user_id, blood_group, emergency_relationship, blood_group_enc, emergency_relationship_enc FROM employee_profiles"
            )
        ).all()
    for _uid, bg, rel, bge, rele in rows:
        assert (bg is None) == (bge is None) and (rel is None) == (rele is None)
        if bge is not None:
            assert decrypt_bytes(bytes(bge)).decode() == bg
        if rele is not None:
            assert decrypt_bytes(bytes(rele)).decode() == rel
    assert sum(1 for r in rows if r[3] is not None) == 600


# ------------------------------------------------------------------ L7
async def test_l7_initial_password_is_gone(client, seeded):
    admin = await _tok(client, "ADM001")
    r = await client.post("/api/admin/employees", headers=admin, json=_body(seeded, initial_password="Str0ng!Chosen#1"))
    assert r.status_code == 422
    ok = await client.post("/api/admin/employees", headers=admin, json=_body(seeded))
    assert ok.status_code == 201 and (await do_login(client, "NEW001", ok.json()["temporary_password"])).status_code == 200
    assert (await do_login(client, "NEW001", "Str0ng!Chosen#1")).status_code == 401
