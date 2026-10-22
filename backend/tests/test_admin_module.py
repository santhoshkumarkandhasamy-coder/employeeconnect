"""Plan 09 admin module: masters, roles/permissions, leave balances, audit viewer, employee edit of personal fields."""

import uuid

import pytest
from sqlalchemy import select

from app.models import AuditLog, LeaveBalance, LeaveType, Role

from .conftest import PASSWORD, bearer, do_login, login_tokens

ROLES = ["admin", "hr", "manager", "employee"]
FAKE = "00000000-0000-0000-0000-000000000001"
ALL = {"admin", "hr", "manager", "employee"}

MATRIX = [
    ("get", "/api/admin/departments", ALL),
    ("post", "/api/admin/departments", {"admin", "hr"}),
    ("put", f"/api/admin/departments/{FAKE}", {"admin", "hr"}),
    ("delete", f"/api/admin/departments/{FAKE}", {"admin", "hr"}),
    ("get", "/api/admin/shifts", ALL),
    ("post", "/api/admin/shifts", {"admin", "hr"}),
    ("delete", f"/api/admin/shifts/{FAKE}", {"admin", "hr"}),
    ("get", "/api/admin/leave-types", ALL),
    ("post", "/api/admin/leave-types", {"admin", "hr"}),
    ("delete", f"/api/admin/leave-types/{FAKE}", {"admin", "hr"}),
    ("get", f"/api/admin/employees/{FAKE}/leave-balances", {"admin", "hr"}),
    ("put", f"/api/admin/employees/{FAKE}/leave-balances", {"admin", "hr"}),
    ("post", "/api/admin/leave-balances/apply-defaults", {"admin", "hr"}),
    ("get", "/api/admin/roles", {"admin", "hr"}),
    ("get", f"/api/admin/roles/{FAKE}", {"admin", "hr"}),
    ("post", "/api/admin/roles", {"admin"}),
    ("put", f"/api/admin/roles/{FAKE}", {"admin"}),
    ("put", f"/api/admin/roles/{FAKE}/permissions", {"admin"}),
    ("delete", f"/api/admin/roles/{FAKE}", {"admin"}),
    ("get", "/api/admin/permissions", {"admin", "hr"}),
    ("get", "/api/admin/audit-logs", {"admin"}),
    ("get", "/api/admin/settings", {"admin"}),
]


@pytest.mark.parametrize("method,path,allowed", MATRIX)
@pytest.mark.parametrize("role", ROLES)
async def test_rbac_matrix(client, role, method, path, allowed):
    t = await login_tokens(client, {"admin": "ADM001", "hr": "HR001", "manager": "MGR001", "employee": "EMP001"}[role])
    r = await getattr(client, method)(path, headers=bearer(t), **({"json": {}} if method in ("post", "put") else {}))
    if role in allowed:
        assert r.status_code != 403, (role, path, r.text)
    else:
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"


async def _admin(client):
    return bearer(await login_tokens(client, "ADM001"))


async def _audit(db, action):
    return (await db.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))).scalars().all()


# ------------------------------------------------------------------ employees: personal fields, manager, role change
async def _put_body(seeded, **over):
    return {
        "full_name": "Test Employee",
        "official_email": "employee@corp-mail.com",
        "department_id": str(seeded["dept"]),
        "shift_id": str(seeded["shift"]),
        "role_id": str(seeded["roles"]["employee"]),
        "manager_id": str(seeded["users"]["manager"]),
        "join_date": "2024-01-15",
        **over,
    }


async def test_hr_edits_personal_fields_and_sensitive_view(client, seeded, db):
    hr = bearer(await login_tokens(client, "HR001"))
    uid = seeded["users"]["employee"]
    body = await _put_body(
        seeded,
        phone="+91 99999 11111",
        dob="1995-04-12",
        blood_group="O+",
        address="12 Secret Street",
        emergency_contact={"name": "Mom", "phone": "+919888877777", "relationship": "mother"},
    )
    r = await client.put(f"/api/admin/employees/{uid}", headers=hr, json=body)
    assert r.status_code == 200, r.text
    prof = r.json()["profile"]
    assert prof["phone"] == "+919999911111" and prof["dob"] == "1995-04-12" and prof["blood_group"] == "O+"
    assert prof["emergency_contact"]["name"] == "Mom"
    # omitted personal keys stay untouched, explicit null clears
    r2 = await client.put(f"/api/admin/employees/{uid}", headers=hr, json=await _put_body(seeded, address=None))
    p2 = r2.json()["profile"]
    assert p2["address"] is None and p2["dob"] == "1995-04-12" and p2["phone"] == "+919999911111"
    # audit: names only, never values
    rows = await _audit(db, "profile.update_admin")
    assert rows and rows[0].after == {"fields": ["dob", "phone", "blood_group", "address", "emergency_contact"]}
    dump = str([(x.before, x.after) for x in await _audit(db, "employee.update")] + [x.after for x in rows])
    for secret in ("Secret Street", "9999911111", "1995-04-12", "Mom"):
        assert secret not in dump
    # validation reuses the profile rules
    bad = await client.put(f"/api/admin/employees/{uid}", headers=hr, json=await _put_body(seeded, phone="abc"))
    assert bad.status_code == 422
    # manager sees the team member but no personal data
    mgr = bearer(await login_tokens(client, "MGR001"))
    g = await client.get(f"/api/admin/employees/{uid}", headers=mgr)
    assert g.status_code == 200 and g.json()["profile"] is None
    gh = await client.get(f"/api/admin/employees/{uid}", headers=hr)
    assert gh.json()["profile"]["phone"] == "+919999911111"
    assert await _audit(db, "employee.view_sensitive")


async def test_manager_rules_self_inactive_cycle(client, seeded):
    admin = await _admin(client)
    emp, mgr = seeded["users"]["employee"], seeded["users"]["manager"]
    r = await client.put(f"/api/admin/employees/{emp}", headers=admin, json=await _put_body(seeded, manager_id=str(emp)))
    assert r.status_code == 422
    # MGR001 reports to EMP001 -> cycle (EMP001 reports to MGR001)
    bad = await client.put(
        f"/api/admin/employees/{mgr}",
        headers=admin,
        json=await _put_body(seeded, full_name="Test Manager", official_email="manager@corp-mail.com",
                             role_id=str(seeded["roles"]["manager"]), manager_id=str(emp)),
    )  # fmt: skip
    assert bad.status_code == 422 and "cycle" in str(bad.json())
    # an inactive manager cannot be assigned
    await client.post(f"/api/admin/employees/{mgr}/deactivate", headers=admin)
    hr_user = seeded["users"]["hr"]
    r = await client.put(
        f"/api/admin/employees/{hr_user}",
        headers=admin,
        json=await _put_body(seeded, full_name="Test Hr", official_email="hr@corp-mail.com",
                             role_id=str(seeded["roles"]["hr"]), manager_id=str(mgr)),
    )  # fmt: skip
    assert r.status_code == 422


async def test_role_change_revokes_sessions_and_blocks_self(client, seeded, db):
    admin = await _admin(client)
    emp_tokens = await login_tokens(client, "EMP001")
    uid = seeded["users"]["employee"]
    r = await client.put(
        f"/api/admin/employees/{uid}", headers=admin, json=await _put_body(seeded, role_id=str(seeded["roles"]["manager"]))
    )
    assert r.status_code == 200 and r.json()["role"]["name"] == "manager"
    assert (await client.post("/api/auth/refresh", json={"refresh_token": emp_tokens["refresh_token"]})).status_code == 401
    assert (await client.get("/api/auth/me", headers=bearer(emp_tokens))).status_code == 401  # old access token dead too
    log = (await _audit(db, "employee.update"))[-1]
    assert log.after["sessions_revoked"] is True
    # an admin cannot change their own role
    me = seeded["users"]["admin"]
    s = await client.put(
        f"/api/admin/employees/{me}",
        headers=admin,
        json=await _put_body(seeded, full_name="Test Admin", official_email="admin@corp-mail.com",
                             role_id=str(seeded["roles"]["hr"]), manager_id=None),
    )  # fmt: skip
    assert s.status_code == 403


async def test_hr_cannot_promote_or_edit_admin(client, seeded):
    hr = bearer(await login_tokens(client, "HR001"))
    r = await client.put(
        f"/api/admin/employees/{seeded['users']['employee']}",
        headers=hr,
        json=await _put_body(seeded, role_id=str(seeded["roles"]["admin"])),
    )
    assert r.status_code == 403
    r = await client.put(
        f"/api/admin/employees/{seeded['users']['admin']}",
        headers=hr,
        json=await _put_body(seeded, role_id=str(seeded["roles"]["admin"])),
    )
    assert r.status_code == 403


async def test_reset_password_rate_limited(client, seeded):
    admin = await _admin(client)
    uid = seeded["users"]["employee"]
    codes = [(await client.post(f"/api/admin/employees/{uid}/reset-password", headers=admin)).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


# ------------------------------------------------------------------ departments / shifts
async def test_department_crud_in_use_and_audit(client, seeded, db):
    admin = await _admin(client)
    r = await client.post("/api/admin/departments", headers=admin, json={"name": "Finance", "code": "fin"})
    assert r.status_code == 201 and r.json()["code"] == "FIN" and r.json()["is_active"] is True
    did = r.json()["id"]
    dup = await client.post("/api/admin/departments", headers=admin, json={"name": "finance", "code": "F2"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "CONFLICT"
    u = await client.put(
        f"/api/admin/departments/{did}", headers=admin, json={"name": "Finance Ops", "code": "FIN", "is_active": False}
    )
    assert u.status_code == 200 and u.json()["is_active"] is False
    lst = await client.get("/api/admin/departments?q=ops", headers=admin)
    assert [d["name"] for d in lst.json()["items"]] == ["Finance Ops"] and lst.json()["total"] == 1
    assert (await client.delete(f"/api/admin/departments/{did}", headers=admin)).status_code == 204
    assert (await client.get(f"/api/admin/departments/{did}", headers=admin)).status_code == 404
    # General is used by the seeded users
    used = await client.delete(f"/api/admin/departments/{seeded['dept']}", headers=admin)
    assert used.status_code == 409 and used.json()["error"]["code"] == "IN_USE"
    actions = [x.action for x in await _audit(db, "department.create")] + [
        x.action for x in await _audit(db, "department.delete")
    ]
    assert actions == ["department.create", "department.delete"]
    upd = (await _audit(db, "department.update"))[0]
    assert upd.before["name"] == "Finance" and upd.after["name"] == "Finance Ops"


async def test_shift_crud_validation_and_in_use(client, seeded):
    admin = await _admin(client)
    body = {"name": "Night", "start_time": "22:00", "end_time": "06:00", "grace_minutes": 5, "working_days": [5, 1, 1, 2]}
    r = await client.post("/api/admin/shifts", headers=admin, json=body)
    assert r.status_code == 201, r.text
    assert r.json()["working_days"] == [1, 2, 5] and r.json()["start_time"] == "22:00"
    sid = r.json()["id"]
    for bad in (
        {**body, "name": "X", "working_days": [0]},
        {**body, "name": "X", "working_days": []},
        {**body, "name": "X", "grace_minutes": 500},
        {**body, "name": "X", "end_time": "22:00"},
    ):
        assert (await client.post("/api/admin/shifts", headers=admin, json=bad)).status_code == 422
    assert (await client.post("/api/admin/shifts", headers=admin, json=body)).status_code == 409
    u = await client.put(f"/api/admin/shifts/{sid}", headers=admin, json={**body, "grace_minutes": 15})
    assert u.json()["grace_minutes"] == 15
    assert (await client.delete(f"/api/admin/shifts/{sid}", headers=admin)).status_code == 204
    used = await client.delete(f"/api/admin/shifts/{seeded['shift']}", headers=admin)
    assert used.status_code == 409 and used.json()["error"]["code"] == "IN_USE"


# ------------------------------------------------------------------ leave types + balances
LT = {"code": "cl", "name": "Casual", "annual_quota": 12, "is_paid": True, "allow_half_day": True}


async def test_leave_type_crud_and_in_use(client, seeded, db):
    admin = await _admin(client)
    r = await client.post("/api/admin/leave-types", headers=admin, json=LT)
    assert r.status_code == 201 and r.json()["code"] == "CL" and r.json()["annual_quota"] == 12.0
    tid = r.json()["id"]
    assert (await client.post("/api/admin/leave-types", headers=admin, json={**LT, "name": "Other"})).status_code == 409
    assert (
        await client.post("/api/admin/leave-types", headers=admin, json={**LT, "code": "X", "name": "X", "annual_quota": -1})
    ).status_code == 422
    assert (
        await client.post("/api/admin/leave-types", headers=admin, json={**LT, "code": "Y", "name": "Y", "annual_quota": 1.25})
    ).status_code == 422
    u = await client.put(f"/api/admin/leave-types/{tid}", headers=admin, json={**LT, "annual_quota": 10.5, "is_active": False})
    assert u.json()["annual_quota"] == 10.5 and u.json()["is_active"] is False
    # a balance with used days makes it IN_USE
    db.add(LeaveBalance(user_id=seeded["users"]["employee"], leave_type_id=uuid.UUID(tid), year=2026, quota=5, used=1, pending=0))
    await db.commit()
    used = await client.delete(f"/api/admin/leave-types/{tid}", headers=admin)
    assert used.status_code == 409 and used.json()["error"]["code"] == "IN_USE"
    # another type with only an untouched row can be deleted
    t2 = (await client.post("/api/admin/leave-types", headers=admin, json={**LT, "code": "SL", "name": "Sick"})).json()["id"]
    db.add(LeaveBalance(user_id=seeded["users"]["employee"], leave_type_id=uuid.UUID(t2), year=2026, quota=5, used=0, pending=0))
    await db.commit()
    assert (await client.delete(f"/api/admin/leave-types/{t2}", headers=admin)).status_code == 204
    assert (await db.execute(select(LeaveType).where(LeaveType.id == uuid.UUID(t2)))).scalar_one_or_none() is None


async def test_leave_balances_get_put_and_apply_defaults(client, seeded, db):
    hr = bearer(await login_tokens(client, "HR001"))
    tid = (await client.post("/api/admin/leave-types", headers=hr, json=LT)).json()["id"]
    uid = seeded["users"]["employee"]
    g = await client.get(f"/api/admin/employees/{uid}/leave-balances?year=2030", headers=hr)
    assert g.status_code == 200 and g.json()["year"] == 2030
    item = g.json()["items"][0]
    assert item["leave_type"]["code"] == "CL" and item["used"] == 0.0 and item["remaining"] == item["quota"]
    assert (await db.execute(select(LeaveBalance))).first() is None  # GET creates nothing
    p = await client.put(
        f"/api/admin/employees/{uid}/leave-balances",
        headers=hr,
        json={"year": 2030, "items": [{"leave_type_id": tid, "quota": 20}]},
    )
    assert p.status_code == 200 and p.json()["items"][0]["quota"] == 20.0
    bal = (await db.execute(select(LeaveBalance))).scalars().one()
    bal.used = 5
    await db.commit()
    low = await client.put(
        f"/api/admin/employees/{uid}/leave-balances",
        headers=hr,
        json={"year": 2030, "items": [{"leave_type_id": tid, "quota": 4}]},
    )
    assert low.status_code == 409 and low.json()["error"]["code"] == "QUOTA_BELOW_USED"
    unk = await client.put(
        f"/api/admin/employees/{uid}/leave-balances",
        headers=hr,
        json={"year": 2030, "items": [{"leave_type_id": FAKE, "quota": 4}]},
    )
    assert unk.status_code == 422
    chg = (await _audit(db, "leave_balance.update"))[0]
    assert chg.after["changes"] == [{"leave_type": "CL", "before": 12.0, "after": 20.0}]
    # apply-defaults: idempotent
    first = await client.post("/api/admin/leave-balances/apply-defaults", headers=hr, json={"year": 2031})
    assert first.status_code == 200 and first.json()["created"] == 4 and first.json()["employees"] == 4
    second = await client.post("/api/admin/leave-balances/apply-defaults", headers=hr, json={"year": 2031})
    assert second.json()["created"] == 0


# ------------------------------------------------------------------ roles
async def test_roles_lifecycle_and_guardrails(client, seeded, db):
    admin = await _admin(client)
    cat = (await client.get("/api/admin/permissions", headers=admin)).json()["items"]
    assert {"code", "module", "description"} <= set(cat[0]) and any(c["code"] == "audit.view" for c in cat)
    r = await client.post(
        "/api/admin/roles",
        headers=admin,
        json={"name": "auditor", "description": "Reads logs", "permission_codes": ["audit.view"]},
    )
    assert r.status_code == 201 and r.json()["permission_codes"] == ["audit.view"] and r.json()["user_count"] == 0
    rid = r.json()["id"]
    assert (await client.post("/api/admin/roles", headers=admin, json={"name": "Auditor"})).status_code == 422  # pattern
    assert (await client.post("/api/admin/roles", headers=admin, json={"name": "auditor"})).status_code == 409
    bad = await client.put(
        f"/api/admin/roles/{rid}/permissions",
        headers=admin,
        json={"permission_codes": ["nope.nope"], "current_password": PASSWORD},
    )
    assert bad.status_code == 422
    p = await client.put(
        f"/api/admin/roles/{rid}/permissions",
        headers=admin,
        json={"permission_codes": ["audit.view", "role.view"], "current_password": PASSWORD},
    )
    assert p.status_code == 200 and p.json()["permission_codes"] == ["audit.view", "role.view"]
    log = (await _audit(db, "role.permissions"))[0]
    assert log.before == {"permission_codes": ["audit.view"]}
    assert log.after["added"] == ["role.view"] and log.after["removed"] == []
    u = await client.put(f"/api/admin/roles/{rid}", headers=admin, json={"name": "auditors", "description": None})
    assert u.status_code == 200 and u.json()["name"] == "auditors"
    # in use -> IN_USE; then reassign and delete
    uid = seeded["users"]["employee"]
    await client.put(f"/api/admin/employees/{uid}", headers=admin, json=await _put_body(seeded, role_id=rid))
    used = await client.delete(f"/api/admin/roles/{rid}", headers=admin)
    assert used.status_code == 409 and used.json()["error"]["code"] == "IN_USE"
    await client.put(f"/api/admin/employees/{uid}", headers=admin, json=await _put_body(seeded))
    assert (await client.delete(f"/api/admin/roles/{rid}", headers=admin)).status_code == 204
    assert (await client.get(f"/api/admin/roles/{rid}", headers=admin)).status_code == 404
    assert await _audit(db, "role.create") and await _audit(db, "role.delete")


async def test_system_and_admin_roles_protected(client, seeded):
    admin = await _admin(client)
    admin_role, hr_role = str(seeded["roles"]["admin"]), str(seeded["roles"]["hr"])
    assert (await client.delete(f"/api/admin/roles/{hr_role}", headers=admin)).status_code == 403
    assert (
        await client.put(
            f"/api/admin/roles/{admin_role}/permissions",
            headers=admin,
            json={"permission_codes": [], "current_password": PASSWORD},
        )
    ).status_code == 403
    assert (
        await client.put(f"/api/admin/roles/{admin_role}", headers=admin, json={"name": "admin", "description": "x"})
    ).status_code == 403
    assert (
        await client.put(f"/api/admin/roles/{hr_role}", headers=admin, json={"name": "hr2", "description": "x"})
    ).status_code == 403
    ok = await client.put(f"/api/admin/roles/{hr_role}", headers=admin, json={"name": "hr", "description": "People team"})
    assert ok.status_code == 200
    lst = (await client.get("/api/admin/roles", headers=admin)).json()["items"]
    assert {r["name"] for r in lst} == {"admin", "hr", "manager", "employee"}
    assert next(r for r in lst if r["name"] == "admin")["user_count"] == 1


# ------------------------------------------------------------------ audit viewer
async def test_audit_log_viewer_filters_and_masking(client, seeded, db):
    admin = await _admin(client)
    await client.post("/api/admin/departments", headers=admin, json={"name": "Ops", "code": "OPS"})
    await client.put(
        f"/api/admin/employees/{seeded['users']['employee']}", headers=admin, json=await _put_body(seeded, phone="+919999911111")
    )
    r = await client.get("/api/admin/audit-logs?action=department", headers=admin)
    assert r.status_code == 200 and r.json()["total"] == 1
    item = r.json()["items"][0]
    assert item["action"] == "department.create" and item["actor"]["employee_id"] == "ADM001" and item["outcome"] == "success"
    assert {"id", "ts", "actor", "action", "entity_type", "entity_id", "before", "after", "ip", "outcome"} <= set(item)
    assert (await client.get("/api/admin/audit-logs?action=depart", headers=admin)).json()["total"] == 0  # prefix ends at a dot
    by_actor = await client.get("/api/admin/audit-logs?actor=adm001&entity_type=user", headers=admin)
    assert by_actor.json()["total"] >= 1
    logins = await client.get("/api/admin/audit-logs?action=auth", headers=admin)
    assert logins.json()["total"] >= 1
    assert (await client.get("/api/admin/audit-logs?from=2999-01-01", headers=admin)).json()["total"] == 0
    assert (await client.get("/api/admin/audit-logs?from=2000-01-01&to=2999-01-01&page_size=2", headers=admin)).json()[
        "page_size"
    ] == 2
    assert "9999911111" not in (await client.get("/api/admin/audit-logs?page_size=100", headers=admin)).text
    # rows are stored by id descending
    ids = [i["id"] for i in (await client.get("/api/admin/audit-logs?page_size=100", headers=admin)).json()["items"]]
    assert ids == sorted(ids, reverse=True)
    assert await db.execute(select(Role))  # session still usable


async def test_audit_log_viewer_shows_unknown_user_login_failures(client):
    await do_login(client, "NOBODY1", "wrong-password-1")
    admin = await _admin(client)
    r = await client.get("/api/admin/audit-logs?actor=NOBODY1", headers=admin)
    assert r.status_code == 200
    for i in r.json()["items"]:
        assert i["actor"]["id"] is None and i["actor"]["employee_id"] == "NOBODY1"
