"""Plan 16 work package 4a: salary increments (maker-checker), bulk hikes, salary history, arrears bulk accept, the F2 gate."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core import security
from app.models import (
    AuditLog,
    EmployeeProfile,
    PayrollItemEvent,
    Permission,
    Role,
    RolePermission,
    SalaryIncrement,
    SalaryStructure,
    User,
)
from app.services import notifications
from app.services import payroll_components as comps
from tests.conftest import PASSWORD, bearer
from tests.payroll_fixtures import PERIOD, add_structure, new_run, process_and_publish, structure_body

INC = "/api/admin/payroll/increments"
EMP = "/api/admin/payroll/employees"
TZ = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _relaxed_limits(monkeypatch):
    from app.services import admin_security
    from app.services import payroll_common as pc

    async def no_limit(*a, **k):
        return None

    monkeypatch.setattr(pc, "limit", no_limit)
    monkeypatch.setattr(admin_security, "REAUTH_PER_MINUTE", 10_000)
    monkeypatch.setattr(admin_security, "REAUTH_PER_DAY", 10_000)


def err(r):
    return r.json()["error"]["code"]


def first(n: int = 0) -> str:
    """First day of the month `n` months from the office-timezone current month."""
    return comps.month_add(dt.datetime.now(TZ).date().replace(day=1), n).isoformat()


def pct_body(user_eff=None, percent="10.00", method="proportional", reason="Annual increment", **extra):
    return {
        "kind": "percent",
        "effective_from": user_eff or first(2),
        "percent": percent,
        "method": method,
        "reason": reason,
        **extra,
    }


async def make(client, hdr, user_id, body=None, expect=201):
    r = await client.post(f"{EMP}/{user_id}/increments", json=body or pct_body(), headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def submit(client, hdr, inc, expect=200):
    r = await client.post(f"{INC}/{inc['id']}/submit", json={"expected_version": inc["version"]}, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def approve(client, hdr, inc, expect=200, **kw):
    body = {"expected_version": inc["version"], "comment": None, "current_password": PASSWORD}
    body.update(kw)
    r = await client.post(f"{INC}/{inc['id']}/approve", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def expected_of(client, hdr, bid):
    """The `expected` list of a batch approval: id and current version of every submitted row (what the checker reviewed)."""
    rows = (await client.get(INC, params={"batch_id": bid, "status": "submitted"}, headers=hdr)).json()["items"]
    return [{"id": r["id"], "version": r["version"]} for r in rows]


async def ready(client, payroll, uid, body=None, who="employee"):
    """HR drafts and submits an increment; returns the submitted Increment."""
    inc = await make(client, payroll["hr"], uid[who], body)
    return await submit(client, payroll["hr"], inc)


async def structures(client, hdr, user_id):
    r = await client.get(f"{EMP}/{user_id}/structures", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def make_user(db, seeded, role, eid):
    u = User(
        employee_id=eid, password_hash=security.hash_password(PASSWORD), full_name=f"Test {eid}",
        official_email=f"{eid.lower()}@corp-mail.com", role_id=seeded["roles"][role], department_id=seeded["dept"],
        shift_id=seeded["shift"], must_change_password=False, status="active",
    )  # fmt: skip
    db.add(u)
    await db.flush()
    db.add(EmployeeProfile(user_id=u.id))
    await db.commit()
    return u


async def login(client, eid):
    r = await client.post("/api/auth/login", json={"employee_id": eid, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return bearer(r.json())


# ================================================================================================ create / read
async def test_percent_increment_proportional_draft(client, payroll, uid):
    inc = await make(client, payroll["hr"], uid["employee"])
    assert inc["status"] == "draft" and inc["version"] == 1 and inc["kind"] == "percent" and inc["batch_id"] is None
    assert inc["percent"] == "10.00" and inc["method"] == "proportional" and inc["reason"] == "Annual increment"
    assert inc["change"] == {"old_gross": "25000.00", "new_gross": "27500.00", "amount": "2500.00", "percent": "10.00"}
    p = inc["proposed"]
    assert (p["basic"], p["hra"], p["monthly_gross"]) == ("22000.00", "5500.00", "27500.00")
    assert inc["diff"] == [
        {"code": "BASIC", "old": "20000.00", "new": "22000.00"},
        {"code": "HRA", "old": "5000.00", "new": "5500.00"},
    ]
    assert inc["base_structure"]["effective_from"] == "2025-01-01" and inc["base_structure"]["monthly_gross"] == "25000.00"
    assert inc["warnings"] == [] and inc["run_status"] == "none" and inc["structure_id"] is None
    assert inc["user"]["employee_id"] == "EMP001" and inc["created_by"]["employee_id"] == "HR001"
    assert inc["submitted_by"] is None and inc["decided_by"] is None and inc["decision_comment"] is None
    # nothing changes on the structures until an approval
    assert len(await structures(client, payroll["hr"], uid["employee"])) == 1


async def test_amount_increment_split_from_gross_and_large_change_warning(client, payroll, uid):
    body = {
        "kind": "amount",
        "effective_from": first(1),
        "monthly_gross": "40000.00",
        "method": "split_from_gross",
        "reason": "Promotion",
    }
    inc = await make(client, payroll["hr"], uid["employee"], body)
    p = inc["proposed"]
    assert (p["basic"], p["hra"]) == ("20000.00", "8000.00")
    assert p["allowances"] == [
        {"code": "SPECIAL", "name": "Special allowance", "amount": "12000.00", "prorate": True, "esi_wage": True}
    ]
    assert inc["warnings"] == ["LARGE_CHANGE"] and inc["change"]["percent"] == "60.00"


async def test_split_rule_comes_from_the_settings(client, payroll, uid):
    r = await client.put(
        "/api/admin/payroll/settings",
        json={"settings": {"payroll.split_basic_pct": 60, "payroll.split_hra_pct_of_basic": 50, "payroll.split_allowance_code": "FLEXI",
                           "payroll.split_allowance_name": "Flexi pay"}},
        headers=payroll["hr"],
    )  # fmt: skip
    assert r.status_code == 200, r.text
    body = {
        "kind": "amount",
        "effective_from": first(1),
        "monthly_gross": "30000.00",
        "method": "split_from_gross",
        "reason": "Rule check",
    }
    p = (await make(client, payroll["hr"], uid["employee"], body))["proposed"]
    assert (p["basic"], p["hra"]) == ("18000.00", "9000.00") and p["allowances"][0]["code"] == "FLEXI"


async def test_structure_kind_and_no_change_cannot_be_submitted(client, payroll, uid):
    s = structure_body(basic="20000", hra="5000")
    s.pop("effective_from")
    same = await make(
        client,
        payroll["hr"],
        uid["employee"],
        {"kind": "structure", "effective_from": first(2), "structure": s, "reason": "Re-confirmed"},
    )
    assert "NO_CHANGE" in same["warnings"]
    r = await client.post(f"{INC}/{same['id']}/submit", json={"expected_version": 1}, headers=payroll["hr"])
    assert r.status_code == 422 and err(r) == "NO_CHANGE"
    s2 = dict(s, basic="26000")
    inc = await make(
        client,
        payroll["hr"],
        uid["manager"],
        {"kind": "structure", "effective_from": first(2), "structure": s2, "reason": "Re-structured"},
    )
    assert inc["proposed"]["basic"] == "26000.00" and "NO_CHANGE" not in inc["warnings"]


@pytest.mark.parametrize(
    "mutate,code,status",
    [
        (lambda b: b.update(percent="0.00"), "INVALID_PERCENT", 422),
        (lambda b: b.update(percent="50.01"), "INVALID_PERCENT", 422),
        (lambda b: b.update(effective_from=first(2)[:8] + "15"), "INVALID_PERIOD", 422),
        (lambda b: b.update(effective_from=first(13)), "INVALID_PERIOD", 422),
        (lambda b: b.update(effective_from=first(-13)), "INVALID_PERIOD", 422),
        (lambda b: b.update(monthly_gross="40000.00"), "VALIDATION_ERROR", 422),  # not a percent field
        (lambda b: b.update(extra_field=1), "VALIDATION_ERROR", 422),
        (lambda b: b.update(method="other"), "VALIDATION_ERROR", 422),
        (lambda b: b.update(reason="x"), "VALIDATION_ERROR", 422),
        (lambda b: b.update(kind="import"), "VALIDATION_ERROR", 422),  # created only by the structure import
        (lambda b: b.pop("method"), "VALIDATION_ERROR", 422),
    ],
)
async def test_create_validation(client, payroll, uid, mutate, code, status):
    body = pct_body()
    mutate(body)
    r = await client.post(f"{EMP}/{uid['employee']}/increments", json=body, headers=payroll["hr"])
    assert r.status_code == status and err(r) == code, r.text


async def test_state_errors_on_create(client, payroll, uid, db):
    hr, admin = payroll["hr"], payroll["admin"]
    # no structure in force: HR001 has none (the admin acts, so this is not the self rule)
    r = await client.post(f"{EMP}/{uid['hr']}/increments", json=pct_body(), headers=admin)
    assert r.status_code == 422 and err(r) == "STRUCTURE_MISSING"
    await make(client, hr, uid["employee"])
    r = await client.post(f"{EMP}/{uid['employee']}/increments", json=pct_body(), headers=hr)
    assert r.status_code == 409 and err(r) == "INCREMENT_EXISTS"
    # a structure on the same date, and a later one
    await add_structure(client, hr, uid["manager"], basic="31000", hra="10000", eff=first(3))
    r = await client.post(f"{EMP}/{uid['manager']}/increments", json=pct_body(first(3)), headers=hr)
    assert r.status_code == 409 and err(r) == "STRUCTURE_EXISTS"
    r = await client.post(f"{EMP}/{uid['manager']}/increments", json=pct_body(first(2)), headers=hr)
    assert r.status_code == 409 and err(r) == "LATER_STRUCTURE_EXISTS"
    # unknown employee
    r = await client.post(f"{EMP}/{uuid.uuid4()}/increments", json=pct_body(), headers=hr)
    assert r.status_code == 404
    # the employee left before that month
    u = await db.get(User, uuid.UUID(uid["employee"]))
    u.status, u.deactivated_at = "inactive", dt.datetime.now(dt.UTC) - dt.timedelta(days=120)
    await db.commit()
    r = await client.post(f"{EMP}/{uid['employee']}/increments", json=pct_body(first(4)), headers=hr)
    assert r.status_code == 422 and err(r) == "AFTER_EXIT"


async def test_self_rule_is_audited_and_permissions(client, payroll, uid, db):
    hr, admin = payroll["hr"], payroll["admin"]
    await add_structure(client, admin, uid["hr"], basic="25000", hra="5000")
    r = await client.post(f"{EMP}/{uid['hr']}/increments", json=pct_body(), headers=hr)
    assert r.status_code == 403 and err(r) == "FORBIDDEN"
    denied = (
        (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "payroll.denied", AuditLog.entity_type == "salary_increment")
            )
        )
        .scalars()
        .all()
    )
    assert len(denied) == 1 and denied[0].outcome == "failure"
    emp = await login(client, "EMP001")
    mgr = await login(client, "MGR001")
    for h in (emp, mgr):
        assert (await client.get(INC, headers=h)).status_code == 403
        assert (await client.get(f"{INC}/batches", headers=h)).status_code == 403
        assert (await client.post(f"{EMP}/{uid['manager']}/increments", json=pct_body(), headers=h)).status_code == 403
        assert (
            await client.post(
                f"{INC}/{uuid.uuid4()}/approve", json={"expected_version": 1, "current_password": PASSWORD}, headers=h
            )
        ).status_code == 403
    me = (await client.get("/api/auth/me", headers=hr)).json()["permissions"]
    assert "payroll.approve" in me
    assert "payroll.approve" in (await client.get("/api/auth/me", headers=admin)).json()["permissions"]
    assert "payroll.approve" not in (await client.get("/api/auth/me", headers=mgr)).json()["permissions"]


async def test_payroll_approve_is_a_separate_permission(client, payroll, uid, db, seeded):
    """A maker role with payroll.manage but WITHOUT payroll.approve can draft and submit but never decide."""
    role = Role(name="payroll_maker", description="maker only", is_system=False)
    db.add(role)
    await db.flush()
    for code in ("payroll.view_own", "payroll.view_all", "payroll.manage"):
        pid = (await db.execute(select(Permission.id).where(Permission.code == code))).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=pid))
    await db.commit()
    await make_user(db, seeded, "employee", "MK001")
    u = (await db.execute(select(User).where(User.employee_id == "MK001"))).unique().scalar_one()
    u.role_id = role.id
    await db.commit()
    maker = await login(client, "MK001")
    inc = await make(client, maker, uid["employee"])
    inc = await submit(client, maker, inc)
    r = await client.post(f"{INC}/{inc['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=maker)
    assert r.status_code == 403 and err(r) == "FORBIDDEN"
    r = await client.post(f"{INC}/{inc['id']}/reject", json={"expected_version": 2, "comment": "no way"}, headers=maker)
    assert r.status_code == 403


# ================================================================================================ lifecycle
async def test_edit_submit_withdraw_cancel(client, payroll, uid):
    hr = payroll["hr"]
    inc = await make(client, hr, uid["employee"])
    r = await client.put(
        f"{INC}/{inc['id']}", json={**pct_body(percent="12.00", reason="Revised"), "expected_version": 1}, headers=hr
    )
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["version"] == 2 and edited["percent"] == "12.00" and edited["change"]["new_gross"] == "28000.00"
    stale = await client.put(f"{INC}/{inc['id']}", json={**pct_body(), "expected_version": 1}, headers=hr)
    assert stale.status_code == 409 and err(stale) == "VERSION_CONFLICT"
    kind = await client.put(
        f"{INC}/{inc['id']}",
        json={
            "kind": "amount",
            "effective_from": first(2),
            "monthly_gross": "30000.00",
            "method": "proportional",
            "reason": "Other",
            "expected_version": 2,
        },
        headers=hr,
    )
    assert kind.status_code == 422  # the kind cannot change
    sub = await submit(client, hr, edited)
    assert sub["status"] == "submitted" and sub["submitted_by"]["employee_id"] == "HR001" and sub["version"] == 3
    r = await client.put(f"{INC}/{inc['id']}", json={**pct_body(), "expected_version": 3}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_DRAFT"
    r = await client.post(f"{INC}/{inc['id']}/submit", json={"expected_version": 3}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_DRAFT"
    r = await client.post(f"{INC}/{inc['id']}/withdraw", json={"expected_version": 2}, headers=hr)
    assert r.status_code == 409 and err(r) == "VERSION_CONFLICT"
    r = await client.post(f"{INC}/{inc['id']}/withdraw", json={"expected_version": 3}, headers=hr)
    assert r.status_code == 200 and r.json()["status"] == "draft" and r.json()["submitted_by"] is None
    r = await client.post(f"{INC}/{inc['id']}/withdraw", json={"expected_version": 4}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_SUBMITTED"
    r = await client.post(f"{INC}/{inc['id']}/cancel", json={"reason": "Not needed"}, headers=hr)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    r = await client.post(f"{INC}/{inc['id']}/cancel", json={"reason": "Again please"}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_DRAFT"
    # a cancelled increment frees the month
    await make(client, hr, uid["employee"])


async def test_approve_creates_the_structure_with_provenance(client, payroll, uid, db):
    inc = await ready(client, payroll, uid)
    done = await approve(client, payroll["admin"], inc, comment="Approved for FY")
    assert (
        done["status"] == "approved"
        and done["decided_by"]["employee_id"] == "ADM001"
        and done["decision_comment"] == "Approved for FY"
    )
    assert done["structure_id"] and done["arrears_proposed"] is None and done["version"] == 3
    items = await structures(client, payroll["hr"], uid["employee"])
    assert [s["effective_from"] for s in items] == [first(2), "2025-01-01"]
    new = items[0]
    assert new["id"] == done["structure_id"] and new["source"] == "increment" and new["increment_id"] == inc["id"]
    assert (new["basic"], new["hra"], new["monthly_gross"]) == ("22000.00", "5500.00", "27500.00")
    assert new["created_by"]["employee_id"] == "ADM001" and items[1]["source"] == "manual"
    row = await db.get(SalaryStructure, uuid.UUID(new["id"]))
    assert str(row.increment_id) == inc["id"]


async def test_maker_checker_rules(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    inc = await make(client, hr, uid["employee"])
    # a draft is not decided
    r = await client.post(f"{INC}/{inc['id']}/approve", json={"expected_version": 1, "current_password": PASSWORD}, headers=admin)
    assert r.status_code == 409 and err(r) == "NOT_SUBMITTED"
    inc = await submit(client, hr, inc)
    # the creator / submitter cannot decide
    r = await client.post(f"{INC}/{inc['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=hr)
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    r = await client.post(f"{INC}/{inc['id']}/reject", json={"expected_version": 2, "comment": "Mine to cancel"}, headers=hr)
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    # step-up: missing -> 422, wrong -> 403 (verified before the state / version checks)
    r = await client.post(f"{INC}/{inc['id']}/approve", json={"expected_version": 2}, headers=admin)
    assert r.status_code == 422 and err(r) == "VALIDATION_ERROR"
    r = await client.post(
        f"{INC}/{inc['id']}/approve", json={"expected_version": 99, "current_password": "Wrong!Pass#12345"}, headers=admin
    )
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    r = await client.post(
        f"{INC}/{inc['id']}/approve", json={"expected_version": 99, "current_password": PASSWORD}, headers=admin
    )
    assert r.status_code == 409 and err(r) == "VERSION_CONFLICT"
    # the submitter is a different person from the maker: neither may decide, a third one can
    admin_made = await make(client, admin, uid["manager"])
    admin_made = await submit(client, hr, admin_made)  # maker = admin, submitter = hr
    for h in (admin, hr):
        r = await client.post(
            f"{INC}/{admin_made['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=h
        )
        assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    await make_user(db, seeded, "hr", "HR002")
    third = await login(client, "HR002")
    done = await approve(client, third, admin_made)
    assert done["status"] == "approved" and done["decided_by"]["employee_id"] == "HR002"
    # the employee is never the checker
    await add_structure(client, hr, uid["admin"], basic="25000", hra="5000")
    for_admin = await submit(client, hr, await make(client, hr, uid["admin"]))
    r = await client.post(
        f"{INC}/{for_admin['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=admin
    )
    assert r.status_code == 403 and err(r) == "FORBIDDEN"


async def test_reject_is_final_and_needs_a_comment(client, payroll, uid):
    inc = await ready(client, payroll, uid)
    admin = payroll["admin"]
    r = await client.post(f"{INC}/{inc['id']}/reject", json={"expected_version": 2}, headers=admin)
    assert r.status_code == 422
    r = await client.post(f"{INC}/{inc['id']}/reject", json={"expected_version": 2, "comment": "Budget frozen"}, headers=admin)
    assert r.status_code == 200
    out = r.json()
    assert (
        out["status"] == "rejected"
        and out["decision_comment"] == "Budget frozen"
        and out["decided_by"]["employee_id"] == "ADM001"
    )
    await approve(client, admin, out, expect=409)
    assert (
        err(await client.post(f"{INC}/{inc['id']}/withdraw", json={"expected_version": 3}, headers=payroll["hr"]))
        == "NOT_SUBMITTED"
    )
    await make(client, payroll["hr"], uid["employee"])  # a new draft is needed, and allowed
    assert len(await structures(client, payroll["hr"], uid["employee"])) == 1


async def test_replace_and_remove(client, payroll, uid):
    hr, admin = payroll["hr"], payroll["admin"]
    old = (await structures(client, hr, uid["employee"]))[0]
    s = structure_body(basic="26000", hra="0")
    s.pop("effective_from")
    rep = await make(
        client,
        hr,
        uid["employee"],
        {
            "kind": "structure",
            "effective_from": "2025-01-01",
            "structure": s,
            "replaces_structure_id": old["id"],
            "reason": "Correction",
        },
    )
    assert rep["replaces_structure_id"] == old["id"] and rep["base_structure"]["id"] == old["id"]
    done = await approve(client, admin, await submit(client, hr, rep))
    assert done["structure_id"] == old["id"]  # replaced in place: payslips of drafts keep pointing at it
    items = await structures(client, hr, uid["employee"])
    assert len(items) == 1 and items[0]["basic"] == "26000.00" and items[0]["source"] == "increment"
    # a wrong target / a mismatching date
    r = await client.post(
        f"{EMP}/{uid['employee']}/increments",
        json={"kind": "remove", "replaces_structure_id": str(uuid.uuid4()), "reason": "Entered in error"},
        headers=hr,
    )
    assert r.status_code == 404
    r = await client.post(
        f"{EMP}/{uid['employee']}/increments",
        json={
            "kind": "structure",
            "effective_from": first(1),
            "structure": s,
            "replaces_structure_id": old["id"],
            "reason": "Wrong date",
        },
        headers=hr,
    )
    assert r.status_code == 422
    # remove a later structure
    later = await add_structure(client, hr, uid["employee"], basic="30000", hra="0", eff=first(3))
    rm = await make(
        client, hr, uid["employee"], {"kind": "remove", "replaces_structure_id": later["id"], "reason": "Entered in error"}
    )
    assert rm["kind"] == "remove" and rm["proposed"] is None and rm["effective_from"] == first(3) and rm["diff"] == []
    r = await client.post(f"{INC}/{rm['id']}/submit", json={"expected_version": 1}, headers=hr)
    assert r.status_code == 200
    gone = await approve(client, admin, r.json())
    assert gone["status"] == "approved" and gone["structure_id"] is None
    assert [s["effective_from"] for s in await structures(client, hr, uid["employee"])] == ["2025-01-01"]


async def test_replace_or_remove_of_a_structure_in_use_is_refused(client, payroll, uid):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    old = (await structures(client, hr, uid["employee"]))[0]
    assert old["in_use"] is True
    r = await client.post(
        f"{EMP}/{uid['employee']}/increments",
        json={"kind": "remove", "replaces_structure_id": old["id"], "reason": "Entered in error"},
        headers=hr,
    )
    assert r.status_code == 409 and err(r) == "STRUCTURE_IN_USE"


# ================================================================================================ stale / overridden
async def test_stale_base_blocks_the_approval(client, payroll, uid, db):
    inc = await ready(client, payroll, uid)
    base = inc["base_structure"]["id"]
    await db.execute(
        update(SalaryStructure)
        .where(SalaryStructure.id == uuid.UUID(base))
        .values(updated_at=dt.datetime(2031, 1, 1, tzinfo=dt.UTC))
    )
    await db.commit()
    r = await client.post(
        f"{INC}/{inc['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=payroll["admin"]
    )
    assert r.status_code == 409 and err(r) == "INCREMENT_STALE"
    assert (await client.get(f"{INC}/{inc['id']}", headers=payroll["hr"])).json()["status"] == "submitted"


async def test_base_deleted_or_structure_in_between_is_stale(client, payroll, uid):
    hr, admin = payroll["hr"], payroll["admin"]
    inc = await ready(client, payroll, uid, pct_body(first(3)))
    await add_structure(client, hr, uid["employee"], basic="23000", hra="0", eff=first(1))  # between base and the effective date
    r = await client.post(f"{INC}/{inc['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=admin)
    assert r.status_code == 409 and err(r) == "INCREMENT_STALE"
    inc2 = await ready(client, payroll, uid, pct_body(first(5)), who="manager")
    mgr_base = (await structures(client, hr, uid["manager"]))[0]["id"]
    d = await client.delete(f"/api/admin/payroll/structures/{mgr_base}", headers=hr)
    assert d.status_code == 204
    r = await client.post(
        f"{INC}/{inc2['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=admin
    )
    assert r.status_code == 409 and err(r) in {"INCREMENT_STALE"}


async def test_same_date_and_later_structure_at_approval(client, payroll, uid):
    hr, admin = payroll["hr"], payroll["admin"]
    same = await ready(client, payroll, uid, pct_body(first(2)))
    await add_structure(client, hr, uid["employee"], basic="24000", hra="0", eff=first(2))
    r = await client.post(
        f"{INC}/{same['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=admin
    )
    assert r.status_code == 409 and err(r) == "STRUCTURE_EXISTS"
    later = await ready(client, payroll, uid, pct_body(first(2)), who="manager")
    await add_structure(client, hr, uid["manager"], basic="32000", hra="0", eff=first(4))
    r = await client.post(
        f"{INC}/{later['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=admin
    )
    assert r.status_code == 409 and err(r) == "LATER_STRUCTURE_EXISTS"


async def test_after_exit_at_approval(client, payroll, uid, db):
    inc = await ready(client, payroll, uid, pct_body(first(3)))
    u = await db.get(User, uuid.UUID(uid["employee"]))
    u.status, u.deactivated_at = "inactive", dt.datetime.now(dt.UTC) - dt.timedelta(days=120)
    await db.commit()
    r = await client.post(
        f"{INC}/{inc['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=payroll["admin"]
    )
    assert r.status_code == 422 and err(r) == "AFTER_EXIT"


# ================================================================================================ backdated -> arrears
async def test_backdated_increment_proposes_arrears_and_bulk_accept(client, payroll, uid):
    hr, admin = payroll["hr"], payroll["admin"]
    await client.put("/api/admin/payroll/settings", json={"settings": {"payroll.increment_backdate_months": 24}}, headers=hr)
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    body = pct_body("2025-06-01")
    emp = await make(client, hr, uid["employee"], body)
    assert emp["warnings"] == ["BACKDATED"] and emp["run_status"] == "published"
    mgr = await make(client, hr, uid["manager"], body)
    out = await approve(client, admin, await submit(client, hr, emp))
    assert out["arrears_proposed"] is True and out["structure_id"]
    await approve(client, admin, await submit(client, hr, mgr))
    props = (await client.get("/api/admin/payroll/arrears", params={"status": "proposed"}, headers=hr)).json()["items"]
    assert {p["user"]["employee_id"] for p in props} == {"EMP001", "MGR001"}
    emp_p = next(p for p in props if p["user"]["employee_id"] == "EMP001")
    assert emp_p["amount"] == "2500.00" and emp_p["from_period"] == PERIOD == emp_p["to_period"]
    assert emp_p["trigger_structure"]["id"] == out["structure_id"]

    url = "/api/admin/payroll/arrears/bulk-accept"
    items = [{"id": p["id"], "expected_version": p["version"]} for p in props]
    # step-up first, shape rules
    assert (await client.post(url, json={"items": items, "period": "2025-07"}, headers=hr)).status_code == 422
    r = await client.post(url, json={"items": items, "period": "2025-07", "current_password": "Wrong!Pass#12345"}, headers=hr)
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    assert (
        await client.post(url, json={"items": items + items[:1], "period": "2025-07", "current_password": PASSWORD}, headers=hr)
    ).status_code == 422
    assert (
        await client.post(url, json={"items": [], "period": "2025-07", "current_password": PASSWORD}, headers=hr)
    ).status_code == 422
    many = [{"id": str(uuid.uuid4()), "expected_version": 1} for _ in range(201)]
    assert (
        await client.post(url, json={"items": many, "period": "2025-07", "current_password": PASSWORD}, headers=hr)
    ).status_code == 422
    # a published month without an open revision refuses the rows, nothing is changed
    r = await client.post(url, json={"items": items, "period": PERIOD, "current_password": PASSWORD}, headers=hr)
    assert r.status_code == 200 and r.json()["accepted"] == 0
    assert {s["reason"] for s in r.json()["skipped"]} == {"PERIOD_LOCKED"}
    # partial: a stale version, an unknown id; the third is accepted
    bad = [{"id": items[0]["id"], "expected_version": 99}, {"id": str(uuid.uuid4()), "expected_version": 1}, items[1]]
    r = await client.post(url, json={"items": bad, "period": "2025-07", "current_password": PASSWORD}, headers=hr)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1
    assert sorted(s["reason"] for s in r.json()["skipped"]) == ["ARREARS_STALE", "NOT_FOUND"]
    r = await client.post(url, json={"items": items, "period": "2025-07", "current_password": PASSWORD}, headers=hr)
    assert r.json()["accepted"] == 1 and [s["reason"] for s in r.json()["skipped"]] == ["NOT_PROPOSED"]
    pending = (await client.get("/api/admin/payroll/arrears", params={"status": "pending"}, headers=hr)).json()["items"]
    assert len(pending) == 2 and all(p["period"] == "2025-07" for p in pending)
    # employees cannot use it
    assert (
        await client.post(
            url, json={"items": items, "period": "2025-07", "current_password": PASSWORD}, headers=await login(client, "EMP001")
        )
    ).status_code == 403


# ================================================================================================ bulk hike
def bulk_body(**kw):
    body = {
        "effective_from": first(2),
        "percent": "8.00",
        "method": "proportional",
        "reason": "Annual increment FY",
        "scope": {"department_ids": None, "user_ids": None, "exclude_user_ids": []},
        "overrides": [],
    }
    body.update(kw)
    return body


async def test_bulk_hike_scope_skips_overrides_and_totals(client, payroll, uid):
    hr = payroll["hr"]
    r = await client.post(f"{INC}/bulk", json=bulk_body(overrides=[{"user_id": uid["employee"], "percent": "12.00"}]), headers=hr)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == 2 and out["skipped"] == []  # HR / admin have no structure: not part of "all with a structure"
    b = out["batch"]
    assert (
        b["kind"] == "percent"
        and b["rows"] == 2
        and b["percent"] == "8.00"
        and b["counts"]["draft"] == 2
        and b["effective_from"] == first(2)
    )
    # EMP001 25000 -> +12% = 28000, MGR001 40000 -> +8% = 43200
    assert b["totals"] == {
        "old_gross": "65000.00",
        "new_gross": "71200.00",
        "delta_monthly": "6200.00",
        "delta_annual": "74400.00",
    }
    rows = (await client.get(INC, params={"batch_id": b["id"]}, headers=hr)).json()
    assert rows["total"] == 2 and all(
        "proposed" not in i and "diff" not in i and "decision_comment" not in i for i in rows["items"]
    )
    by_emp = {i["user"]["employee_id"]: i for i in rows["items"]}
    assert (
        by_emp["EMP001"]["percent"] == "12.00"
        and by_emp["EMP001"]["change"]["new_gross"] == "28000.00"
        and by_emp["EMP001"]["batch_id"] == b["id"]
    )
    assert (await client.get(f"{INC}/batches/{b['id']}", headers=hr)).json()["id"] == b["id"]
    lst = (await client.get(f"{INC}/batches", params={"kind": "percent"}, headers=hr)).json()
    assert lst["total"] == 1 and lst["items"][0]["id"] == b["id"]
    assert (await client.get(f"{INC}/batches", params={"kind": "import"}, headers=hr)).json()["total"] == 0
    # a row may be overridden while draft but keeps the month of its batch
    row = by_emp["MGR001"]
    full = (await client.get(f"{INC}/{row['id']}", headers=hr)).json()
    r = await client.put(
        f"{INC}/{row['id']}",
        json={**pct_body(user_eff=first(2), percent="9.00"), "expected_version": full["version"]},
        headers=hr,
    )
    assert r.status_code == 200 and r.json()["change"]["new_gross"] == "43600.00"
    r = await client.put(
        f"{INC}/{row['id']}",
        json={**pct_body(user_eff=first(3), percent="9.00"), "expected_version": full["version"] + 1},
        headers=hr,
    )
    assert r.status_code == 422


async def test_bulk_hike_skip_reasons_and_limits(client, payroll, uid, monkeypatch, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    await add_structure(client, admin, uid["hr"], basic="25000", hra="5000")
    emp2 = await make_user(db, seeded, "employee", "EMP002")  # eligible, but no structure yet
    await make(client, hr, uid["manager"])  # a live increment for that month already exists
    scope = {
        "department_ids": None,
        "user_ids": [uid["hr"], uid["employee"], uid["admin"], uid["manager"], str(emp2.id)],
        "exclude_user_ids": [],
    }
    r = await client.post(f"{INC}/bulk", json=bulk_body(scope=scope), headers=hr)
    assert r.status_code == 201, r.text
    reasons = {s["user"]["employee_id"]: s["reason"] for s in r.json()["skipped"]}
    assert reasons == {"HR001": "SELF", "ADM001": "NOT_ELIGIBLE", "EMP002": "NO_STRUCTURE", "MGR001": "INCREMENT_EXISTS"}
    assert r.json()["created"] == 1
    scope2 = {"department_ids": None, "user_ids": None, "exclude_user_ids": [uid["employee"]]}
    r = await client.post(f"{INC}/bulk", json=bulk_body(scope=scope2, effective_from=first(3)), headers=hr)
    assert (
        r.status_code == 201 and r.json()["skipped"] == [] and r.json()["created"] == 1
    )  # HR (excluded from payroll) is not in "all"
    # an override must belong to the scope; its percent is capped
    r = await client.post(
        f"{INC}/bulk",
        json=bulk_body(effective_from=first(4), overrides=[{"user_id": uid["admin"], "percent": "5.00"}]),
        headers=hr,
    )
    assert r.status_code == 422
    r = await client.post(
        f"{INC}/bulk",
        json=bulk_body(effective_from=first(4), overrides=[{"user_id": uid["employee"], "percent": "55.00"}]),
        headers=hr,
    )
    assert r.status_code == 422 and err(r) == "INVALID_PERCENT"
    assert err(await client.post(f"{INC}/bulk", json=bulk_body(percent="0.00"), headers=hr)) == "INVALID_PERCENT"
    # nothing qualifies
    none_scope = {"department_ids": None, "user_ids": [uid["admin"]], "exclude_user_ids": []}
    r = await client.post(f"{INC}/bulk", json=bulk_body(scope=none_scope, effective_from=first(6)), headers=hr)
    assert r.status_code == 422
    from app.services import payroll_increments

    monkeypatch.setattr(payroll_increments, "MAX_BATCH_ROWS", 1)
    r = await client.post(f"{INC}/bulk", json=bulk_body(effective_from=first(7)), headers=hr)
    assert r.status_code == 422 and err(r) == "TOO_MANY_ROWS"


async def test_bulk_submit_approve_partial_failure_reject_cancel(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    b = (await client.post(f"{INC}/bulk", json=bulk_body(), headers=hr)).json()["batch"]
    bid = b["id"]
    rows = (await client.get(INC, params={"batch_id": bid}, headers=hr)).json()["items"]
    # nothing is decided before the batch is submitted
    r = await client.post(f"{INC}/batches/{bid}/approve", json={"expected": [], "current_password": PASSWORD}, headers=admin)
    assert r.status_code == 200 and r.json() == {"approved": 0, "failed": [], "arrears_proposed": 0}
    r = await client.post(f"{INC}/batches/{bid}/submit", json={"x": 1}, headers=hr)
    assert r.status_code == 422
    r = await client.post(f"{INC}/batches/{bid}/submit", json={}, headers=hr)
    assert r.status_code == 200 and r.json() == {"submitted": 2, "skipped": []}
    assert (await client.get(f"{INC}/batches/{bid}", headers=hr)).json()["counts"]["submitted"] == 2
    # the batch maker counts as maker of every row (no submitted row is approved, each is reported)
    exp = await expected_of(client, hr, bid)
    r = await client.post(f"{INC}/batches/{bid}/approve", json={"expected": exp, "current_password": PASSWORD}, headers=hr)
    assert r.status_code == 200 and r.json()["approved"] == 0
    assert {f["reason"] for f in r.json()["failed"]} == {"SECOND_PERSON_REQUIRED"}
    # step-up
    assert (await client.post(f"{INC}/batches/{bid}/approve", json={"expected": exp}, headers=admin)).status_code == 422
    assert (
        err(
            await client.post(
                f"{INC}/batches/{bid}/approve", json={"expected": exp, "current_password": "Wrong!Pass#12345"}, headers=admin
            )
        )
        == "REAUTH_FAILED"
    )
    # one row goes stale: it stays submitted and is reported, the other is approved
    emp_row = next(i for i in rows if i["user"]["employee_id"] == "EMP001")
    await db.execute(
        update(SalaryStructure)
        .where(SalaryStructure.id == uuid.UUID(emp_row["base_structure"]["id"]))
        .values(updated_at=dt.datetime(2031, 1, 1, tzinfo=dt.UTC))
    )
    await db.commit()
    r = await client.post(
        f"{INC}/batches/{bid}/approve", json={"expected": exp, "comment": None, "current_password": PASSWORD}, headers=admin
    )
    assert r.status_code == 200, r.text
    assert r.json()["approved"] == 1 and r.json()["failed"] == [{"id": emp_row["id"], "reason": "INCREMENT_STALE"}]
    counts = (await client.get(f"{INC}/batches/{bid}", headers=hr)).json()["counts"]
    assert counts["approved"] == 1 and counts["submitted"] == 1
    mgr_items = await structures(client, hr, uid["manager"])
    assert mgr_items[0]["source"] == "increment" and mgr_items[0]["monthly_gross"] == "43200.00"
    # reject the rest (comment required); a maker cannot reject their own batch
    assert (await client.post(f"{INC}/batches/{bid}/reject", json={}, headers=admin)).status_code == 422
    r = await client.post(f"{INC}/batches/{bid}/reject", json={"comment": "Redo it"}, headers=hr)
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    r = await client.post(f"{INC}/batches/{bid}/reject", json={"comment": "Redo it"}, headers=admin)
    assert r.status_code == 200 and r.json() == {"rejected": 1}
    # cancel: the approved row cannot be cancelled
    r = await client.post(f"{INC}/batches/{bid}/cancel", json={"reason": "Closing the batch"}, headers=hr)
    assert r.status_code == 200 and r.json() == {"cancelled": 0, "not_cancelled": 2}


async def test_bulk_cancel_and_unknown_batch(client, payroll, uid):
    hr = payroll["hr"]
    bid = (await client.post(f"{INC}/bulk", json=bulk_body(), headers=hr)).json()["batch"]["id"]
    r = await client.post(f"{INC}/batches/{bid}/cancel", json={"reason": "Wrong percent"}, headers=hr)
    assert r.status_code == 200 and r.json() == {"cancelled": 2, "not_cancelled": 0}
    b = (await client.get(f"{INC}/batches/{bid}", headers=hr)).json()
    assert b["counts"]["cancelled"] == 2 and b["cancelled_at"] and b["totals"]["old_gross"] == "0.00"
    r = await client.post(f"{INC}/batches/{bid}/submit", json={}, headers=hr)
    assert r.status_code == 409
    assert (await client.get(f"{INC}/batches/{uuid.uuid4()}", headers=hr)).status_code == 404
    assert (
        await client.post(
            f"{INC}/batches/{uuid.uuid4()}/approve", json={"expected": [], "current_password": PASSWORD}, headers=payroll["admin"]
        )
    ).status_code == 404


async def test_bulk_submit_skips_no_change_rows(client, payroll, uid, db):
    hr = payroll["hr"]
    bid = (await client.post(f"{INC}/bulk", json=bulk_body(), headers=hr)).json()["batch"]["id"]
    row = (await db.execute(select(SalaryIncrement).where(SalaryIncrement.batch_id == uuid.UUID(bid)).limit(1))).scalars().first()
    row.warnings = ["NO_CHANGE"]
    await db.commit()
    r = await client.post(f"{INC}/batches/{bid}/submit", json={}, headers=hr)
    assert r.json()["submitted"] == 1 and r.json()["skipped"] == [{"id": str(row.id), "reason": "NO_CHANGE"}]


# ================================================================================================ history, list, filters
async def test_salary_history_and_list_filters(client, payroll, uid):
    hr, admin = payroll["hr"], payroll["admin"]
    done = await approve(client, admin, await ready(client, payroll, uid))
    live = await make(client, hr, uid["employee"], pct_body(first(5), percent="5.00"))
    h = (await client.get(f"{EMP}/{uid['employee']}/salary-history", headers=hr)).json()["items"]
    assert [i["type"] for i in h] == ["increment", "structure", "structure"]
    assert h[0]["increment_id"] == live["id"] and h[0]["status"] == "draft" and h[0]["new_gross"] == "28875.00"
    cur, first_s = h[1], h[2]
    assert cur["source"] == "increment" and cur["increment_id"] == done["id"] and cur["change_percent"] == "10.00"
    assert cur["approved_by"]["employee_id"] == "ADM001" and cur["effective_to"] is None
    assert first_s["source"] == "manual" and first_s["change_percent"] is None and first_s["approved_by"] is None
    assert first_s["effective_to"] == (dt.date.fromisoformat(first(2)) - dt.timedelta(days=1)).isoformat()
    # filters and the list shape
    by_status = (await client.get(INC, params={"status": "draft"}, headers=hr)).json()
    assert by_status["total"] == 1 and by_status["items"][0]["id"] == live["id"]
    assert (await client.get(INC, params={"user_id": uid["manager"]}, headers=hr)).json()["total"] == 0
    assert (await client.get(INC, params={"effective_from": first(5)[:7]}, headers=hr)).json()["total"] == 1
    assert (await client.get(INC, params={"effective_from": "2026-13"}, headers=hr)).status_code == 422
    assert (await client.get(INC, params={"status": "bogus"}, headers=hr)).status_code == 422
    assert (await client.get(f"{INC}/{uuid.uuid4()}", headers=hr)).status_code == 404
    assert (await client.get(f"{EMP}/{uuid.uuid4()}/salary-history", headers=hr)).status_code == 404


# ================================================================================================ audit, events, push, DB checks
async def test_audit_events_and_push_never_carry_amounts(client, payroll, uid, db):
    hr, admin = payroll["hr"], payroll["admin"]
    notifications.OUTBOX.clear()
    inc = await make(client, hr, uid["employee"], pct_body(reason="Secret reason text"))
    inc = await submit(client, hr, inc)
    done = await approve(client, admin, inc, comment="Secret comment text")
    rows = (await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.increment.%")))).scalars().all()
    assert {r.action for r in rows} >= {"payroll.increment.create", "payroll.increment.submit", "payroll.increment.approve"}
    dump = json.dumps([[r.before, r.after] for r in rows])
    for secret in ("27500.00", "22000.00", "5500.00", "10.00", "Secret reason", "Secret comment", "25000.00"):
        assert secret not in dump, secret
    ap = next(r for r in rows if r.action == "payroll.increment.approve")
    assert (
        ap.after["maker_id"] == uid["hr"]
        and ap.after["checker_id"] == uid["admin"]
        and ap.after["structure_id"] == done["structure_id"]
    )
    assert ap.after["kind"] == "percent" and ap.after["effective_from"] == first(2)
    # money history (encrypted at rest; read through the ORM)
    ev = (await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.item_type == "increment"))).scalars().all()
    assert {e.action for e in ev} == {"create", "submit", "approve"}
    last = next(e for e in ev if e.action == "approve")
    assert last.amount == "27500.00" and last.prev_amount == "25000.00" and last.period.isoformat() == first(2)
    assert next(e for e in ev if e.action == "create").detail == {"reason": "Secret reason text"}
    # push: approvers (not the maker) on submit, the maker on approve; names only
    pushes = [p for p in notifications.OUTBOX if p["payload"]["type"] == "payroll_approval"]
    assert len(pushes) == 2
    sub, dec = pushes
    assert (
        sub["user_ids"] == [uid["admin"]]
        and sub["payload"]["title"] == "Approval needed"
        and sub["payload"]["kind"] == "increment"
    )
    assert (
        dec["user_ids"] == [uid["hr"]]
        and dec["payload"]["title"] == "Increment approved"
        and dec["payload"]["body"] == "Test Employee"
    )
    assert sub["payload"]["body"] == "Test Hr submitted 1 increment" and "27500" not in json.dumps(pushes)


async def test_columns_are_encrypted_at_rest(client, payroll, uid, db):
    inc = await make(client, payroll["hr"], uid["employee"], pct_body(reason="Plain text reason"))
    from sqlalchemy import text

    raw = (
        await db.execute(
            text(
                "SELECT percent_enc, proposed_enc, old_gross_enc, new_gross_enc, reason_enc FROM salary_increments WHERE id = :i"
            ),
            {"i": inc["id"].replace("-", "")},
        )
    ).one()
    for blob in raw:
        assert isinstance(blob, bytes) and b"Plain text" not in blob and b"22000" not in blob and b"10.00" not in blob


async def test_database_checks_repeat_the_maker_checker_rule(db, uid):
    base = {"user_id": uuid.UUID(uid["employee"]), "kind": "percent", "effective_from": dt.date(2030, 1, 1), "method": "proportional",
            "percent": "5.00", "proposed": {"basic": "1.00"}, "reason": "Check", "created_by": uuid.UUID(uid["hr"]),
            "last_edited_by": uuid.UUID(uid["hr"])}  # fmt: skip
    # maker = employee
    db.add(SalaryIncrement(**{**base, "created_by": uuid.UUID(uid["employee"]), "last_edited_by": uuid.UUID(uid["employee"])}))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    # approved by the creator / the submitter / the employee
    for decider in (uid["hr"], uid["employee"]):
        db.add(SalaryIncrement(**base, status="approved", submitted_by=uuid.UUID(uid["manager"]), submitted_at=dt.datetime.now(dt.UTC),
                               decided_by=uuid.UUID(decider), decided_at=dt.datetime.now(dt.UTC)))  # fmt: skip
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
    db.add(SalaryIncrement(**base, status="approved", submitted_by=uuid.UUID(uid["hr"]), submitted_at=dt.datetime.now(dt.UTC),
                           decided_by=uuid.UUID(uid["manager"]), decided_at=dt.datetime.now(dt.UTC)))  # fmt: skip
    await db.commit()  # a third person is fine
    # approved without a submitter is refused, a rejection needs a comment, remove needs no proposal
    db.add(
        SalaryIncrement(
            **{**base, "effective_from": dt.date(2030, 2, 1)},
            status="approved",
            decided_by=uuid.UUID(uid["manager"]),
            decided_at=dt.datetime.now(dt.UTC),
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    db.add(
        SalaryIncrement(**{**base, "effective_from": dt.date(2030, 3, 1), "kind": "remove", "method": None, "percent": None})
    )  # proposed still set
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_database_check_covers_rejected_and_the_last_editor(db, uid):
    """H1 / L3: the checker rule also holds for a rejected row and for the last editor."""
    base = {"user_id": uuid.UUID(uid["employee"]), "kind": "percent", "effective_from": dt.date(2030, 1, 1), "method": "proportional",
            "percent": "5.00", "proposed": {"basic": "1.00"}, "reason": "Check", "created_by": uuid.UUID(uid["hr"]),
            "last_edited_by": uuid.UUID(uid["manager"]), "submitted_by": uuid.UUID(uid["hr"]),
            "submitted_at": dt.datetime.now(dt.UTC), "decided_at": dt.datetime.now(dt.UTC),
            "decision_comment": "No"}  # fmt: skip
    for status, decider in (("approved", uid["manager"]), ("rejected", uid["manager"]), ("rejected", uid["hr"]),
                            ("rejected", uid["employee"])):  # fmt: skip
        db.add(SalaryIncrement(**base, status=status, decided_by=uuid.UUID(decider)))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
    db.add(SalaryIncrement(**base, status="rejected", decided_by=uuid.UUID(uid["admin"])))
    await db.commit()  # a person who took no part may reject


# ================================================================================================ fail closed
async def test_writes_fail_closed_without_redis(client, payroll, uid, monkeypatch):
    """The limiter is real in this test: a Redis outage refuses the write (503) instead of letting it through."""
    from app.core.redis import get_redis

    monkeypatch.undo()  # drop the autouse no-op limiter

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    client._transport.app.dependency_overrides[get_redis] = lambda: Broken()
    for method, url, body in (
        ("POST", f"{EMP}/{uid['employee']}/increments", pct_body()),
        ("POST", f"{INC}/bulk", bulk_body()),
        (
            "POST",
            "/api/admin/payroll/arrears/bulk-accept",
            {"items": [{"id": str(uuid.uuid4()), "expected_version": 1}], "period": "2025-07", "current_password": PASSWORD},
        ),
        ("POST", f"{INC}/{uuid.uuid4()}/approve", {"expected_version": 1, "current_password": PASSWORD}),
    ):
        r = await client.request(method, url, json=body, headers=payroll["hr"])
        assert r.status_code == 503 and err(r) == "TEMPORARILY_UNAVAILABLE", (url, r.text)


# ================================================================================================ F2: the bypass is closed
@pytest.mark.approval_on
async def test_direct_structure_endpoints_are_closed_after_the_first_structure(client, payroll, uid):
    hr, admin = payroll["hr"], payroll["admin"]
    s = (await client.get("/api/admin/payroll/settings", headers=hr)).json()["settings"]
    assert s["payroll.structure_changes_need_approval"] is True
    first_s = (await structures(client, hr, uid["employee"]))[0]  # onboarding: the first structure went in directly
    assert first_s["source"] == "manual" and first_s["increment_id"] is None
    r = await client.post(f"{EMP}/{uid['employee']}/structures", json=structure_body(eff="2025-07-01"), headers=hr)
    assert r.status_code == 409 and err(r) == "APPROVAL_REQUIRED"
    # the only manual structure may still be corrected or removed (nothing is in use)
    r = await client.put(f"/api/admin/payroll/structures/{first_s['id']}", json=structure_body(basic="21000"), headers=hr)
    assert r.status_code == 200 and r.json()["basic"] == "21000.00"
    # an increment structure can only be changed by another increment
    inc = await submit(client, hr, await make(client, hr, uid["employee"]))
    done = await approve(client, admin, inc)
    for sid in (first_s["id"], done["structure_id"]):
        r = await client.put(f"/api/admin/payroll/structures/{sid}", json=structure_body(basic="22000"), headers=hr)
        assert r.status_code == 409 and err(r) == "APPROVAL_REQUIRED"
        r = await client.delete(f"/api/admin/payroll/structures/{sid}", headers=hr)
        assert r.status_code == 409 and err(r) == "APPROVAL_REQUIRED"
    # a first structure that is already in use also needs an approval
    mgr_first = (await structures(client, hr, uid["manager"]))[0]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    r = await client.put(f"/api/admin/payroll/structures/{mgr_first['id']}", json=structure_body(basic="31000"), headers=hr)
    assert r.status_code == 409 and err(r) == "APPROVAL_REQUIRED"


@pytest.mark.approval_on
async def test_only_a_role_assign_holder_with_password_can_switch_the_rule_off(client, payroll, uid, db):
    hr, admin = payroll["hr"], payroll["admin"]
    key = "payroll.structure_changes_need_approval"
    url = "/api/admin/payroll/settings"
    r = await client.put(url, json={"settings": {key: False}, "current_password": PASSWORD}, headers=hr)
    assert r.status_code == 403 and err(r) == "FORBIDDEN"  # HR does not hold role.assign
    r = await client.put(url, json={"settings": {key: False}}, headers=admin)
    assert r.status_code == 422
    r = await client.put(url, json={"settings": {key: False}, "current_password": "Wrong!Pass#12345"}, headers=admin)
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    assert (
        await client.post(f"{EMP}/{uid['employee']}/structures", json=structure_body(eff="2025-07-01"), headers=hr)
    ).status_code == 409
    notifications.OUTBOX.clear()
    r = await client.put(url, json={"settings": {key: False}, "current_password": PASSWORD}, headers=admin)
    assert r.status_code == 200 and r.json()["settings"][key] is False
    alerts = [p for p in notifications.OUTBOX if p["payload"]["type"] == "admin_alert"]
    assert len(alerts) == 1 and uid["admin"] in alerts[0]["user_ids"]
    # off = today's behaviour
    assert (
        await client.post(f"{EMP}/{uid['employee']}/structures", json=structure_body(eff="2025-07-01"), headers=hr)
    ).status_code == 201
    # an unchanged value needs neither (no change)
    r = await client.put(url, json={"settings": {key: False}}, headers=hr)
    assert r.status_code == 200
    # switching it back on is guarded the same way
    r = await client.put(url, json={"settings": {key: True}}, headers=hr)
    assert r.status_code == 403
    audit = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.settings.update"))).scalars().all()
    assert any(c["key"] == key for a in audit for c in a.after["changes"])


async def test_new_settings_validate(client, payroll, uid):
    hr = payroll["hr"]
    url = "/api/admin/payroll/settings"
    for bad in (
        {"payroll.split_basic_pct": 0},
        {"payroll.split_basic_pct": 101},
        {"payroll.split_hra_pct_of_basic": -1},
        {"payroll.split_allowance_code": "BASIC"},
        {"payroll.split_allowance_code": "bad code"},
        {"payroll.split_allowance_name": ""},
        {"payroll.increment_max_pct": 0},
        {"payroll.increment_backdate_months": 25},
        {"payroll.gratuity_min_years": 11},
        {"payroll.gratuity_cap": 2000000},
        {"payroll.gratuity_cap": "0.00"},
        {"payroll.gratuity_wage_basis": "gross"},
        {"payroll.ctc_include_gratuity": "yes"},
    ):
        r = await client.put(url, json={"settings": bad}, headers=hr)
        assert r.status_code == 422, bad
    ok = {
        "payroll.gratuity_cap": "1500000",
        "payroll.gratuity_wage_basis": "basic_or_half_gross",
        "payroll.ctc_include_gratuity": False,
        "payroll.increment_max_pct": 30,
    }
    r = await client.put(url, json={"settings": ok}, headers=hr)
    assert r.status_code == 200
    s = r.json()["settings"]
    assert (
        s["payroll.gratuity_cap"] == "1500000.00" and s["payroll.increment_max_pct"] == 30 and s["payroll.split_basic_pct"] == 50
    )
    pct = await client.post(f"{EMP}/{uid['employee']}/increments", json=pct_body(percent="31.00"), headers=payroll["admin"])
    assert pct.status_code == 422 and err(pct) == "INVALID_PERCENT"


async def test_increment_columns_follow_key_rotation(client, payroll, uid, db, monkeypatch):
    """The new *_enc columns are discovered from the models and re-encrypted under the newest key (plan 16 security notes)."""
    import os

    from cryptography.fernet import Fernet
    from sqlalchemy import select as sa_select

    from app.core import crypto
    from app.services import key_rotation

    inc = await make(client, payroll["hr"], uid["employee"], pct_body(reason="Rotate me"))
    names = {(t.name, c.name) for t, c in key_rotation.encrypted_columns()}
    for col in ("percent_enc", "proposed_enc", "old_gross_enc", "new_gross_enc", "reason_enc", "decision_comment_enc"):
        assert ("salary_increments", col) in names, col
    assert ("salary_increment_batches", "reason_enc") in names and ("salary_increment_batches", "percent_enc") in names
    old = os.environ["DATA_ENCRYPTION_KEYS"]
    new = Fernet.generate_key().decode()
    try:
        monkeypatch.setenv("DATA_ENCRYPTION_KEYS", f"{new},{old}")
        crypto.reset_crypto_cache()
        stats = await key_rotation.rotate_all(db)
        assert stats["undecryptable"] == 0 and stats["rotated"] > 0
        monkeypatch.setenv("DATA_ENCRYPTION_KEYS", new)  # the old key is no longer needed
        crypto.reset_crypto_cache()
        await db.rollback()
        q = sa_select(SalaryIncrement).where(SalaryIncrement.id == uuid.UUID(inc["id"])).execution_options(populate_existing=True)
        row = (await db.execute(q)).scalar_one()
        assert row.reason == "Rotate me" and row.proposed["basic"] == "22000.00" and row.old_gross == "25000.00"
        assert (await key_rotation.rotate_all(db))["rotated"] == 0  # idempotent
    finally:
        monkeypatch.setenv("DATA_ENCRYPTION_KEYS", old)
        crypto.reset_crypto_cache()
