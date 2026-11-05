"""Plan 16 work package 4b: security and checklist behaviour of the full and final settlement endpoints."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models import AuditLog, FnfSettlement
from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, new_run, process_and_publish
from tests.test_payroll_fnf import (  # noqa: F401  (_env is an autouse fixture)
    EMP,
    FNF,
    _env,
    act,
    create_body,
    err,
    get,
    mk,
    put,
    submitted,
)


async def test_endpoints_need_their_permission(client, payroll, uid):
    f = await mk(client, payroll["hr"], uid["employee"])
    emp = payroll["employee"]
    for method, url, body in (
        ("GET", FNF, None),
        ("GET", f"{FNF}/{f['id']}", None),
        ("GET", f"{FNF}/{f['id']}/preview", None),
        ("GET", f"{FNF}/{f['id']}/statement", None),
        ("POST", f"{EMP}/{uid['employee']}/fnf", create_body()),
        ("PUT", f"{FNF}/{f['id']}", {"expected_version": 1, "remarks": "x"}),
        ("POST", f"{FNF}/{f['id']}/submit", {"expected_version": 1}),
        ("POST", f"{FNF}/{f['id']}/approve", {"expected_version": 1, "current_password": PASSWORD}),
        ("POST", f"{FNF}/{f['id']}/revoke", {"expected_version": 1, "reason": "No reason", "current_password": PASSWORD}),
    ):
        r = await client.request(method, url, json=body, headers=emp)
        assert r.status_code == 403, (method, url, r.status_code)
        r = await client.request(method, url, json=body)
        assert r.status_code == 401, (method, url)
    r = await client.get(f"{FNF}/{f['id']}", headers=payroll["manager"])
    assert r.status_code == 403  # payroll.view_all is HR / admin only


async def test_employee_probes_are_404_and_audited(client, payroll, uid, db):
    r = await client.get("/api/me/fnf/statement", headers=payroll["employee"])
    assert r.status_code == 404
    denied = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.denied"))).scalars().all()
    assert denied and denied[-1].after["status"] == 404
    r = await client.get("/api/me/fnf", headers=payroll["employee"])
    assert r.json() == {"fnf": None}
    f = await mk(client, payroll["hr"], uid["employee"])
    assert (await client.get("/api/me/fnf", headers=payroll["employee"])).json() == {"fnf": None}  # a draft is invisible
    await act(client, payroll["hr"], f, "submit")
    me = (await client.get("/api/me/fnf", headers=payroll["employee"])).json()["fnf"]
    assert me["status"] == "in_progress" and me["final_payslip_id"] is None
    assert set(me) == {"id", "status", "last_working_day", "settlement_period", "final_payslip_id"}
    assert (await client.get("/api/me/fnf/statement", headers=payroll["employee"])).status_code == 404  # not settled


async def test_writes_fail_closed_without_redis(client, payroll, uid, monkeypatch):
    from app.core.redis import get_redis

    monkeypatch.undo()  # drop the autouse no-op limiter

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    client._transport.app.dependency_overrides[get_redis] = lambda: Broken()
    fid = uuid.uuid4()
    for method, url, body in (
        ("POST", f"{EMP}/{uid['employee']}/fnf", create_body()),
        ("PUT", f"{FNF}/{fid}", {"expected_version": 1, "remarks": "x"}),
        ("POST", f"{FNF}/{fid}/submit", {"expected_version": 1}),
        ("POST", f"{FNF}/{fid}/approve", {"expected_version": 1, "current_password": PASSWORD}),
        ("POST", f"{FNF}/{fid}/reject", {"expected_version": 1, "comment": "No good"}),
        ("POST", f"{FNF}/{fid}/revoke", {"expected_version": 1, "reason": "No reason", "current_password": PASSWORD}),
        ("GET", f"{FNF}/{fid}/statement", None),
    ):
        r = await client.request(method, url, json=body, headers=payroll["hr"])
        assert r.status_code == 503 and err(r) == "TEMPORARILY_UNAVAILABLE", (url, r.text)


async def test_unknown_ids_and_unknown_fields(client, payroll, uid):
    h = payroll["hr"]
    for method, url, body in (
        ("GET", f"{FNF}/{uuid.uuid4()}", None),
        ("GET", f"{FNF}/{uuid.uuid4()}/preview", None),
        ("PUT", f"{FNF}/{uuid.uuid4()}", {"expected_version": 1, "remarks": "x"}),
        ("POST", f"{FNF}/{uuid.uuid4()}/submit", {"expected_version": 1}),
        ("POST", f"{FNF}/{uuid.uuid4()}/cancel", {"reason": "Not needed"}),
    ):
        r = await client.request(method, url, json=body, headers=h)
        assert r.status_code == 404, (url, r.status_code)
    f = await mk(client, h, uid["employee"])
    r = await client.put(f"{FNF}/{f['id']}", json={"expected_version": 1, "status": "approved"}, headers=h)
    assert r.status_code == 422
    r = await client.put(f"{FNF}/{f['id']}", json={"expected_version": 1, "remarks": None, "items": None}, headers=h)
    assert r.status_code == 422  # a null list is not an empty list


async def test_step_up_is_required_for_approve_and_revoke(client, payroll, uid):
    f = await submitted(client, payroll, uid)
    r = await client.post(f"{FNF}/{f['id']}/approve", json={"expected_version": f["version"]}, headers=payroll["admin"])
    assert r.status_code == 422
    ok = await act(client, payroll["admin"], f, "approve")
    r = await client.post(
        f"{FNF}/{f['id']}/revoke", json={"expected_version": ok["version"], "reason": "Mistake found"}, headers=payroll["admin"]
    )
    assert r.status_code == 422


async def test_rejected_round_keeps_the_checker(client, payroll, uid, db):
    f = await submitted(client, payroll, uid)
    rj = await act(client, payroll["admin"], f, "reject")
    row = (await db.execute(select(FnfSettlement))).scalar_one()
    await db.refresh(row)
    assert str(row.decided_by) == uid["admin"] and row.status == "draft"
    c = await client.post(f"{FNF}/{rj['id']}/cancel", json={"reason": "Employee stays"}, headers=payroll["admin"])
    assert c.status_code == 200  # cancelling does not touch the editor columns


async def test_checklist_warnings(client, payroll, uid):
    h = payroll["hr"]
    # July was published before the exit was recorded: the employee was paid for a month after the last working day
    july = await new_run(client, h, "2025-07")
    await process_and_publish(client, h, july["id"])
    june = await new_run(client, h, PERIOD)
    await process_and_publish(client, h, june["id"])
    f = await mk(client, h, uid["employee"])
    g = await get(client, h, f["id"])
    assert "PAID_AFTER_EXIT" in g["warnings"] and "ACCOUNT_STILL_ACTIVE" in g["warnings"]
    assert g["run_status"] == "published"
    pv = (await client.get(f"{FNF}/{f['id']}/preview", headers=h)).json()
    assert pv["estimate"] is True and pv["source"] == "structure"


async def test_negative_estimate_is_a_warning(client, payroll, uid):
    h = payroll["hr"]
    r = await client.post(
        "/api/admin/payroll/loans",
        json={"user_id": uid["employee"], "kind": "loan", "principal": "90000.00", "emi": "10000.00", "start_period": PERIOD,
              "reason": "Medical emergency", "current_password": PASSWORD},
        headers=h,
    )  # fmt: skip
    assert r.status_code == 201
    f = await mk(client, h, uid["employee"])
    f = await put(client, h, f, loans=[{"loan_id": r.json()["id"], "mode": "payroll"}])
    g = await get(client, h, f["id"])
    assert "NEGATIVE_NET_ESTIMATE" in g["warnings"]
