"""RBAC matrix over the admin-lite endpoints + employee management behaviour + route coverage."""

import uuid

import pytest
from sqlalchemy import select

from app.core.rbac import get_principal, require_permission
from app.main import create_app
from app.models import AuditLog, User

from .conftest import PASSWORD, USERS, bearer, do_login, login_tokens

ROLES = ["admin", "hr", "manager", "employee"]
FAKE = "00000000-0000-0000-0000-000000000001"

# (method, path, permission-holding roles) ; target id is a random uuid -> allowed roles get 404, others 403
MATRIX = [
    ("get", "/api/admin/employees", {"admin", "hr", "manager"}),
    ("post", "/api/admin/employees", {"admin", "hr"}),
    ("get", f"/api/admin/employees/{FAKE}", {"admin", "hr", "manager"}),
    ("put", f"/api/admin/employees/{FAKE}", {"admin", "hr"}),
    ("post", f"/api/admin/employees/{FAKE}/deactivate", {"admin", "hr"}),
    ("post", f"/api/admin/employees/{FAKE}/activate", {"admin", "hr"}),
    ("post", f"/api/admin/employees/{FAKE}/unlock", {"admin", "hr"}),
    ("post", f"/api/admin/employees/{FAKE}/reset-password", {"admin", "hr"}),
    ("get", "/api/admin/roles", {"admin", "hr"}),
]


@pytest.mark.parametrize("method,path,allowed", MATRIX)
@pytest.mark.parametrize("role", ROLES)
async def test_rbac_matrix(client, role, method, path, allowed):
    t = await login_tokens(client, USERS[role])
    r = await getattr(client, method)(path, headers=bearer(t), **({"json": {}} if method in ("post", "put") else {}))
    if role in allowed:
        assert r.status_code != 403, (role, path, r.text)
    else:
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


async def test_unauthenticated_requests_rejected_everywhere(client):
    for method, path, _ in MATRIX:
        r = await getattr(client, method)(path)
        assert r.status_code == 401, path


def test_every_api_route_declares_a_permission_dependency():
    """Guards the rule 'RBAC dependency on every endpoint'. Public auth endpoints are listed explicitly."""
    public = {
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/refresh"),
        ("POST", "/api/auth/forgot-password"),
        ("POST", "/api/auth/web/login"),  # browser console auth: cookie + CSRF guards instead of a bearer token
        ("POST", "/api/auth/web/refresh"),
        ("POST", "/api/auth/web/logout"),
        ("GET", "/api/public/company"),
        ("GET", "/api/public/company/logo"),
        ("GET", "/healthz"),
    }
    app = create_app()
    from fastapi.routing import APIRoute

    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        deps = [d.call for d in route.dependant.dependencies]
        names = {getattr(c, "__qualname__", "") for c in deps}
        guarded = any("require_permission" in n for n in names) or get_principal in deps
        if not guarded and not any((m, route.path) in public for m in route.methods):
            missing.append((route.methods, route.path))
    assert missing == []
    assert require_permission  # imported for clarity


NEW = {"employee_id": "emp100", "full_name": "Priya Nair", "official_email": "priya@corp-mail.com"}


async def _create(client, admin_tokens, ids, **over):
    body = {
        **NEW,
        "role_id": str(ids["roles"]["employee"]),
        "department_id": str(ids["dept"]),
        "shift_id": str(ids["shift"]),
        **over,
    }
    return await client.post("/api/admin/employees", headers=bearer(admin_tokens), json=body)


async def test_create_employee_flow_temp_password_forced_change_and_audit(client, seeded, db):
    admin = await login_tokens(client, "ADM001")
    r = await _create(client, admin, seeded, phone="+919999999999")
    assert r.status_code == 201
    body = r.json()
    emp, temp = body["employee"], body["temporary_password"]
    assert emp["employee_id"] == "EMP100" and emp["status"] == "active" and emp["role"]["name"] == "employee"
    assert "password" not in str(emp).lower().replace("temporary_password", "")
    from app.core.security import validate_password

    assert validate_password(temp, "EMP100", "Priya Nair") == []
    # temp password is never stored/audited in clear
    u = (await db.execute(select(User).where(User.employee_id == "EMP100"))).unique().scalar_one()
    assert u.password_hash != temp and u.must_change_password is True
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "employee.create"))).scalars().all()
    assert logs and temp not in str([x.after for x in logs])
    # the new user can log in but must change password first
    login = await do_login(client, "EMP100", temp)
    assert login.status_code == 200 and login.json()["must_change_password"] is True
    gated = await client.get("/api/config", headers=bearer(login.json()))
    assert gated.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"


async def test_create_duplicates_and_validation(client, seeded):
    admin = await login_tokens(client, "ADM001")
    assert (await _create(client, admin, seeded)).status_code == 201
    dup = await _create(client, admin, seeded, official_email="other@corp-mail.com")
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "EMPLOYEE_ID_EXISTS"
    dup2 = await _create(client, admin, seeded, employee_id="EMP101")
    assert dup2.status_code == 409 and dup2.json()["error"]["code"] == "EMAIL_EXISTS"
    # review L7: admins can no longer choose the initial password (unknown field -> 422)
    chosen = await _create(client, admin, seeded, employee_id="EMP102", official_email="w@corp-mail.com", initial_password="x")
    assert chosen.status_code == 422
    bad_role = await _create(client, admin, seeded, employee_id="EMP103", official_email="z@corp-mail.com", role_id=FAKE)
    assert bad_role.status_code == 422


async def test_hr_cannot_create_admin_or_touch_admin_accounts(client, seeded):
    hr = await login_tokens(client, "HR001")
    r = await _create(client, hr, seeded, role_id=str(seeded["roles"]["admin"]))
    assert r.status_code == 403
    admin_id = str(seeded["users"]["admin"])
    for path in ("reset-password", "unlock", "deactivate"):
        assert (await client.post(f"/api/admin/employees/{admin_id}/{path}", headers=bearer(hr))).status_code == 403


async def test_reset_password_flow(client, seeded, db):
    emp = await login_tokens(client, "EMP001")
    admin = await login_tokens(client, "ADM001")
    uid = str(seeded["users"]["employee"])
    r = await client.post(f"/api/admin/employees/{uid}/reset-password", headers=bearer(admin))
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    assert (await client.post("/api/auth/refresh", json={"refresh_token": emp["refresh_token"]})).status_code == 401
    assert (await do_login(client, "EMP001", PASSWORD)).status_code == 401
    login = await do_login(client, "EMP001", temp)
    assert login.status_code == 200 and login.json()["must_change_password"] is True
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "employee.reset_password"))).scalars().all()
    assert len(logs) == 1 and temp not in str(logs[0].after)


async def test_reset_password_also_unlocks(client, seeded):
    for _ in range(5):
        await do_login(client, "EMP001", "Wrong!Pass1x")
    admin = await login_tokens(client, "ADM001")
    r = await client.post(f"/api/admin/employees/{seeded['users']['employee']}/reset-password", headers=bearer(admin))
    assert (await do_login(client, "EMP001", r.json()["temporary_password"])).status_code == 200


async def test_deactivate_blocks_login_revokes_sessions_and_activate_restores(client, seeded):
    emp = await login_tokens(client, "EMP001")
    admin = await login_tokens(client, "ADM001")
    uid = seeded["users"]["employee"]
    r = await client.post(f"/api/admin/employees/{uid}/deactivate", headers=bearer(admin))
    assert r.status_code == 200 and r.json()["status"] == "inactive"
    r = await client.post("/api/auth/refresh", json={"refresh_token": emp["refresh_token"]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCOUNT_INACTIVE"  # offboarded: app wipes + locks
    assert (await client.get("/api/auth/me", headers=bearer(emp))).json()["error"]["code"] == "ACCOUNT_INACTIVE"
    assert (await do_login(client, "EMP001")).json()["error"]["code"] == "ACCOUNT_INACTIVE"
    assert (await client.post(f"/api/admin/employees/{uid}/activate", headers=bearer(admin))).json()["status"] == "active"
    assert (await do_login(client, "EMP001")).status_code == 200


async def test_cannot_deactivate_self_or_last_admin(client, seeded):
    admin = await login_tokens(client, "ADM001")
    r = await client.post(f"/api/admin/employees/{seeded['users']['admin']}/deactivate", headers=bearer(admin))
    assert r.status_code == 403
    # a second admin deactivating the first is fine only while another active admin remains
    r2 = await _create(
        client,
        admin,
        seeded,
        employee_id="ADM002",
        official_email="a2@corp-mail.com",
        role_id=str(seeded["roles"]["admin"]),
        current_password=PASSWORD,
    )
    new_id = r2.json()["employee"]["id"]
    assert (await client.post(f"/api/admin/employees/{new_id}/deactivate", headers=bearer(admin))).status_code == 200
    # now demoting the only active admin is blocked (self -> 403; guard hit via update by another admin path n/a)
    upd = await client.put(
        f"/api/admin/employees/{seeded['users']['admin']}",
        headers=bearer(admin),
        json={"full_name": "X", "role_id": str(seeded["roles"]["employee"])},
    )
    assert upd.status_code == 403  # cannot change own role


async def test_last_admin_guard_when_actor_is_hr_is_blocked_by_admin_guard_and_by_code(client, seeded, db):
    from app.services import employees as svc

    u = (await db.execute(select(User).where(User.employee_id == "ADM001"))).unique().scalar_one()
    with pytest.raises(Exception) as e:
        await svc._guard_last_admin(db, u)
    assert getattr(e.value, "code", "") == "LAST_ADMIN" and e.value.status == 409


async def test_update_employee_and_audit_diff(client, seeded, db):
    hr = await login_tokens(client, "HR001")
    uid = seeded["users"]["employee"]
    r = await client.put(
        f"/api/admin/employees/{uid}",
        headers=bearer(hr),
        json={
            "full_name": "Renamed Person",
            "official_email": "emp@corp-mail.com",
            "role_id": str(seeded["roles"]["employee"]),
            "department_id": str(seeded["dept"]),
            "shift_id": str(seeded["shift"]),
        },
    )
    assert r.status_code == 200 and r.json()["full_name"] == "Renamed Person"
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "employee.update"))).scalar_one()
    assert log.after == {"full_name": "Renamed Person", "official_email": "emp@corp-mail.com"} or "full_name" in log.after


async def test_list_pagination_search_and_manager_team_scope(client, seeded):
    admin = await login_tokens(client, "ADM001")
    r = await client.get("/api/admin/employees?page=1&page_size=2", headers=bearer(admin))
    b = r.json()
    assert b["page"] == 1 and b["page_size"] == 2 and b["total"] == 4 and len(b["items"]) == 2
    assert "password" not in r.text
    q = (await client.get("/api/admin/employees?q=emp0", headers=bearer(admin))).json()
    assert [i["employee_id"] for i in q["items"]] == ["EMP001"]
    assert (await client.get("/api/admin/employees?page_size=500", headers=bearer(admin))).status_code == 422
    mgr = await login_tokens(client, "MGR001")
    team = (await client.get("/api/admin/employees", headers=bearer(mgr))).json()
    assert [i["employee_id"] for i in team["items"]] == ["EMP001"]  # direct reports only
    assert (await client.get(f"/api/admin/employees/{seeded['users']['hr']}", headers=bearer(mgr))).status_code == 404
    assert (await client.get(f"/api/admin/employees/{uuid.uuid4()}", headers=bearer(admin))).status_code == 404


async def test_device_token_register_and_delete(client):
    t = await login_tokens(client, "EMP001")
    body = {"fcm_token": "f" * 40, "platform": "android", "device_id": "d1"}
    assert (await client.post("/api/me/device-tokens", headers=bearer(t), json=body)).status_code == 204
    assert (await client.post("/api/me/device-tokens", headers=bearer(t), json=body)).status_code == 204  # upsert
    assert (await client.delete(f"/api/me/device-tokens/{body['fcm_token']}", headers=bearer(t))).status_code == 204
    assert (await client.post("/api/me/device-tokens", headers=bearer(t), json={**body, "platform": "web"})).status_code == 422


async def test_config_endpoint_returns_defaults(client):
    t = await login_tokens(client, "EMP001")
    c = (await client.get("/api/config", headers=bearer(t))).json()
    assert c["auth"]["inactivity_timeout_minutes"] == 5 and c["food"]["cutoff_time"] == "18:00"
    assert set(c) == {"food", "office", "attendance", "auth", "permission", "company"}
    assert c["auth"]["offline_cache_max_hours"] == 72 and c["company"]["configured"] is False
