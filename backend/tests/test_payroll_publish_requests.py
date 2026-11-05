"""Plan 17 work package 5a: publish requests (maker-checker for publish), contributors, publish-check, staleness (contract 19.2).

HR001 is the maker everywhere (`payroll` fixture: HR created the structures); ADM001 is a clean checker; more people are added with
`extra_user`. Everything runs on SQLite: row locks and the database triggers / CHECKs are PostgreSQL-only (migration 0026 tests).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.models import AuditLog, PayrollItemEvent, PayrollPublishRequest, PayrollRun, Payslip, PayslipDelivery
from app.services import notifications
from app.services import payroll_publish_requests as prs
from tests.conftest import PASSWORD
from tests.payroll_controls_helpers import (
    ADMIN,
    PAYSLIPS,
    REQUESTS,
    RUNS,
    decide,
    enable_mc,
    err,
    extra_user,
    process,
    quiet,  # noqa: F401  (fixture)
    request_payslip,
    request_run,
)
from tests.payroll_fixtures import new_run, process_and_publish, row_for

pytestmark = pytest.mark.usefixtures("quiet")


async def processed_run(client, payroll):
    """MC on, a run created and processed by HR (the maker)."""
    await enable_mc(client, payroll["admin"])
    run = await new_run(client, payroll["hr"])
    await process(client, payroll["hr"], run["id"])
    return run


# ================================================================================================ gate + lifecycle
async def test_direct_publish_is_refused_while_maker_checker_applies(client, payroll):
    run = await processed_run(client, payroll)
    for who in ("hr", "admin"):
        r = await client.post(f"{RUNS}/{run['id']}/publish", json={"current_password": PASSWORD}, headers=payroll[who])
        assert r.status_code == 409 and err(r) == "PUBLISH_REQUEST_REQUIRED", who
    assert (await client.get(f"{RUNS}/{run['id']}", headers=payroll["hr"])).json()["status"] == "processed"


async def test_request_endpoints_answer_maker_checker_off_when_the_rule_is_off(client, payroll):
    run = await new_run(client, payroll["hr"])
    await process(client, payroll["hr"], run["id"])
    r = await client.post(f"{RUNS}/{run['id']}/publish-requests", json={}, headers=payroll["hr"])
    assert r.status_code == 409 and err(r) == "MAKER_CHECKER_OFF"
    check = (await client.get(f"{RUNS}/{run['id']}/publish-check", headers=payroll["admin"])).json()
    assert check["maker_checker"] is False and check["can_request"] is False and check["can_approve"] is False
    assert check["can_publish_directly"] is True and check["open_request"] is None
    r = await client.post(f"{RUNS}/{run['id']}/publish", json={"current_password": PASSWORD}, headers=payroll["admin"])
    assert r.status_code == 200  # today's behaviour is unchanged


async def test_request_then_approve_publishes_the_exact_set(client, payroll, db, uid):
    run = await processed_run(client, payroll)
    hr, admin = payroll["hr"], payroll["admin"]
    req = await request_run(client, hr, run["id"], comment="October payroll")
    assert req["scope"] == "run" and req["status"] == "submitted" and req["stale_reason"] is None and req["version"] == 1
    assert req["run"] == {"id": run["id"], "period": "2025-06", "status": "processed"} and req["payslip"] is None
    assert req["snapshot"]["payslips"] == 2 and req["snapshot"]["held"] == 0 and req["snapshot"]["warnings"] >= 0
    assert set(req["snapshot"]["totals"]) == {"gross_earnings", "total_deductions", "net_pay", "employer_cost"}
    assert req["comment"] == "October payroll" and req["requested_by"]["employee_id"] == "HR001"
    assert req["makers"] is None and req["decided_by"] is None and req["expires_at"]
    assert (await client.get(f"{RUNS}/{run['id']}", headers=hr)).json()["publish_request"]["id"] == req["id"]
    # the requester can never approve; the step-up password is checked first
    r = await client.post(
        f"{REQUESTS}/{req['id']}/approve", json={"expected_version": 1, "current_password": "Wrong!Passw0rd#1"}, headers=hr
    )
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    await decide(client, hr, req, "approve", 403)
    out = await decide(client, admin, req, "approve")
    assert out["request"]["status"] == "approved" and out["request"]["decided_by"]["employee_id"] == "ADM001"
    assert out["run"]["status"] == "published" and out["run"]["publish_request"]["status"] == "approved"
    assert [m["employee_id"] for m in out["request"]["makers"]] == ["HR001"]
    db.expire_all()
    assert {p.status for p in (await db.execute(select(Payslip))).scalars()} == {"published"}
    assert len((await db.execute(select(PayslipDelivery))).scalars().all()) == 2  # delivery rows are created by the publish
    pub = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.run.publish"))).scalar_one()
    assert pub.after["request_id"] == req["id"] and pub.after["payslips"] == 2
    ap = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.publish_request.approve"))).scalar_one()
    assert ap.after["maker_id"] == uid["hr"] and ap.after["checker_id"] == uid["admin"] and ap.after["scope"] == "run"
    ev = [
        (e.item_type, e.action)
        for e in (await db.execute(select(PayrollItemEvent))).scalars()
        if e.item_type == "publish_request"
    ]
    assert ev == [("publish_request", "create"), ("publish_request", "approve")]
    # once decided, the request cannot be decided again
    await decide(client, admin, out["request"], "approve", 409)


async def test_one_open_request_and_nothing_to_publish(client, payroll):
    run = await processed_run(client, payroll)
    await request_run(client, payroll["hr"], run["id"])
    r = await client.post(f"{RUNS}/{run['id']}/publish-requests", json={}, headers=payroll["hr"])
    assert r.status_code == 409 and err(r) == "PUBLISH_REQUEST_EXISTS"
    draft = await new_run(client, payroll["hr"], "2025-05")
    r = await client.post(f"{RUNS}/{draft['id']}/publish-requests", json={}, headers=payroll["hr"])
    assert r.status_code == 409 and err(r) == "CONFLICT"  # a draft run cannot be requested
    for p in (await client.get(f"{RUNS}/{run['id']}/payslips", headers=payroll["hr"])).json()["items"]:
        assert p["status"] == "processed"


async def test_withdraw_and_reject(client, payroll, db):
    run = await processed_run(client, payroll)
    hr, admin = payroll["hr"], payroll["admin"]
    req = await request_run(client, hr, run["id"])
    w = await decide(client, hr, req, "withdraw")
    assert w["status"] == "withdrawn"
    await decide(client, hr, w, "withdraw", 409)  # NOT_SUBMITTED
    again = await request_run(client, hr, run["id"])  # a withdrawn request frees the slot
    r = await client.post(f"{REQUESTS}/{again['id']}/reject", json={"expected_version": 1}, headers=admin)
    assert r.status_code == 422  # a comment is required
    r = await client.post(f"{REQUESTS}/{again['id']}/reject", json={"expected_version": 1, "comment": "x"}, headers=admin)
    assert r.status_code == 422
    await decide(client, hr, again, "reject", 403)  # the requester is a maker: only somebody else decides
    rej = await decide(client, admin, again, "reject", comment="Totals look too high")
    assert rej["status"] == "rejected" and rej["decision_comment"] == "Totals look too high" and rej["makers"]
    assert (await client.get(f"{RUNS}/{run['id']}", headers=hr)).json()["status"] == "processed"  # unchanged
    db.expire_all()
    for a in (await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.publish_request.%")))).scalars():
        assert "Totals look" not in json.dumps(a.after)  # comments never reach the audit log


async def test_version_binding_and_unknown_ids(client, payroll):
    run = await processed_run(client, payroll)
    req = await request_run(client, payroll["hr"], run["id"])
    await decide(client, payroll["admin"], req, "approve", 409, expected_version=7)  # VERSION_CONFLICT
    for method, url in (("GET", f"{REQUESTS}/{uuid.uuid4()}"), ("POST", f"{REQUESTS}/{uuid.uuid4()}/withdraw")):
        r = await client.request(method, url, json={"expected_version": 1} if method == "POST" else None, headers=payroll["hr"])
        assert r.status_code == 404
    r = await client.post(f"{REQUESTS}/{req['id']}/approve", json={"expected_version": 1}, headers=payroll["admin"])
    assert r.status_code == 422  # step-up password missing
    r = await client.post(f"{REQUESTS}/{req['id']}/withdraw", json={"expected_version": 1, "x": 1}, headers=payroll["hr"])
    assert r.status_code == 422  # unknown field
    assert (await client.post(f"{RUNS}/{uuid.uuid4()}/publish-requests", json={}, headers=payroll["hr"])).status_code == 404


async def test_permission_matrix(client, payroll):
    run = await processed_run(client, payroll)
    req = await request_run(client, payroll["hr"], run["id"])
    emp, mgr = payroll["employee"], payroll["manager"]
    for method, url, body in (
        ("GET", f"{RUNS}/{run['id']}/publish-check", None),
        ("GET", f"{PAYSLIPS}/{uuid.uuid4()}/publish-check", None),
        ("POST", f"{RUNS}/{run['id']}/publish-requests", {}),
        ("POST", f"{PAYSLIPS}/{uuid.uuid4()}/publish-requests", {}),
        ("GET", REQUESTS, None),
        ("GET", f"{REQUESTS}/{req['id']}", None),
        ("POST", f"{REQUESTS}/{req['id']}/withdraw", {"expected_version": 1}),
        ("POST", f"{REQUESTS}/{req['id']}/approve", {"expected_version": 1, "current_password": PASSWORD}),
        ("POST", f"{REQUESTS}/{req['id']}/reject", {"expected_version": 1, "comment": "No good"}),
    ):
        for who in (emp, mgr):
            r = await client.request(method, url, json=body, headers=who)
            assert r.status_code == 403, (method, url, r.status_code)
        r = await client.request(method, url, json=body)
        assert r.status_code == 401, (method, url)


async def test_requests_fail_closed_without_redis(client, payroll, monkeypatch):
    from app.core.redis import get_redis
    from app.services import payroll_common as pc

    monkeypatch.undo()  # drop the no-op limiter of the `quiet` fixture

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    client._transport.app.dependency_overrides[get_redis] = lambda: Broken()
    rid = uuid.uuid4()
    for method, url, body in (
        ("POST", f"{RUNS}/{rid}/publish-requests", {}),
        ("POST", f"{REQUESTS}/{rid}/withdraw", {"expected_version": 1}),
        ("POST", f"{REQUESTS}/{rid}/approve", {"expected_version": 1, "current_password": PASSWORD}),
        ("POST", f"{REQUESTS}/{rid}/reject", {"expected_version": 1, "comment": "No good"}),
        ("POST", f"{RUNS}/{rid}/delivery/remind", {}),
    ):
        r = await client.request(method, url, json=body, headers=payroll["hr"])
        assert r.status_code == 503 and err(r) == "TEMPORARILY_UNAVAILABLE", url
    del pc


# ================================================================================================ maker set (G1 / G2)
async def test_payslip_editor_is_a_maker_even_when_somebody_else_processed(client, payroll, db, seeded):
    """G1: A edits a payslip, B processes the run (B becomes `updated_by`): A still cannot approve what A typed."""
    hr, admin = payroll["hr"], payroll["admin"]
    await enable_mc(client, admin)
    run = await new_run(client, hr)
    row = await row_for(client, hr, run["id"], "EMP001")
    r = await client.put(f"{PAYSLIPS}/{row['id']}", json={"expected_version": 1, "note": "typed by admin"}, headers=admin)
    assert r.status_code == 200, r.text
    await process(client, hr, run["id"])
    req = await request_run(client, hr, run["id"])
    out = await decide(client, admin, req, "approve", 403)  # edited the payslip
    assert err_code(out) == "SECOND_PERSON_REQUIRED"
    _, admin2 = await extra_user(client, db, seeded, "ADM002")
    check = (await client.get(f"{RUNS}/{run['id']}/publish-check", headers=admin)).json()
    assert "EDITED_PAYSLIP" in check["me"]["reasons"] and check["can_approve"] is False
    assert {m["user"]["employee_id"] for m in check["makers"]} >= {"HR001", "ADM001"}
    done = await decide(client, admin2, req, "approve")
    assert done["request"]["status"] == "approved"
    assert {m["employee_id"] for m in done["request"]["makers"]} == {"HR001", "ADM001"}


def err_code(body: dict) -> str:
    return body["error"]["code"]


async def test_component_creator_is_a_maker(client, payroll, db, seeded, uid):
    """G2: the person who created the bonus in the payslip cannot approve the publish, even without touching run or payslip."""
    hr, admin = payroll["hr"], payroll["admin"]
    await enable_mc(client, admin)
    r = await client.post(
        "/api/admin/payroll/items",
        json={"user_id": uid["employee"], "period": "2025-06", "code": "BONUS", "label": "Diwali", "amount": "5000"},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    run = await new_run(client, hr)
    await process(client, hr, run["id"])
    req = await request_run(client, hr, run["id"])
    body = await decide(client, admin, req, "approve", 403)
    assert err_code(body) == "SECOND_PERSON_REQUIRED"
    check = (await client.get(f"{RUNS}/{run['id']}/publish-check", headers=admin)).json()
    assert check["me"]["reasons"] == ["CREATED_COMPONENT"] and check["can_approve"] is False
    _, admin2 = await extra_user(client, db, seeded, "ADM002")
    assert (await decide(client, admin2, req, "approve"))["request"]["status"] == "approved"


async def test_structure_creator_is_a_maker_but_the_increment_checker_is_not(client, payroll, db, seeded, uid):
    hr, admin = payroll["hr"], payroll["admin"]
    _, admin2 = await extra_user(client, db, seeded, "ADM002")
    # a manual structure created by ADM002 for the manager (a later, higher effective date wins for the period)
    r = await client.post(
        f"{ADMIN}/employees/{uid['manager']}/structures",
        json={"effective_from": "2025-02-01", "basic": "31000", "hra": "0", "allowances": [], "fixed_deductions": [],
              "tds_monthly": "0", "pf_applicable": True, "pf_on_full_basic": False, "esi_applicable": True, "pt_applicable": True},
        headers=admin2,
    )  # fmt: skip
    assert r.status_code == 201, r.text
    await enable_mc(client, admin)
    run = await new_run(client, hr)
    await process(client, hr, run["id"])
    req = await request_run(client, hr, run["id"])
    check = (await client.get(f"{RUNS}/{run['id']}/publish-check", headers=admin2)).json()
    assert "CREATED_STRUCTURE" in check["me"]["reasons"] and check["can_approve"] is False
    await decide(client, admin2, req, "approve", 403)
    ok = (await client.get(f"{RUNS}/{run['id']}/publish-check", headers=admin)).json()
    assert ok["can_approve"] is True and ok["me"]["reasons"] == []
    assert (await decide(client, admin, req, "approve"))["request"]["status"] == "approved"


async def test_fnf_rows_count_their_finance_makers_not_the_fnf_checker(client, payroll, db, seeded, uid):
    """A row written by an F&F approval belongs to the F&F makers (creator / editor / submitter / contributors), not to its checker."""
    from app.models import FnfSettlement, PayrollOneTimeItem

    hr, admin = payroll["hr"], payroll["admin"]
    await enable_mc(client, admin)
    run = await new_run(client, hr)
    admin2_user, admin2 = await extra_user(client, db, seeded, "ADM002")
    maker_id = uuid.UUID(uid["manager"])
    fnf = FnfSettlement(
        user_id=uuid.UUID(uid["employee"]), last_working_day=dt.date(2025, 6, 20), exit_reason="resignation",
        settlement_period=dt.date(2025, 6, 1), notice_required_days=30, plan={"items": [], "encashments": [], "loans": []},
        status="approved", created_by=maker_id, last_edited_by=maker_id, submitted_by=maker_id, submitted_at=dt.datetime.now(dt.UTC),
        decided_by=admin2_user.id, decided_at=dt.datetime.now(dt.UTC), contributors=[str(maker_id)], summary={"created": {}},
    )  # fmt: skip
    db.add(fnf)
    await db.flush()
    item = PayrollOneTimeItem(
        user_id=uuid.UUID(uid["employee"]), period=dt.date(2025, 6, 1), code="OTHER_EARNING", label="F&F", amount="100.00",
        status="pending", fnf_id=fnf.id, created_by=admin2_user.id, updated_by=admin2_user.id,
    )  # fmt: skip
    db.add(item)
    await db.commit()
    await client.post(f"{RUNS}/{run['id']}/refresh", headers=hr)
    await process(client, hr, run["id"])
    req = await request_run(client, hr, run["id"])
    p = await db.get(PayrollPublishRequest, uuid.UUID(req["id"]))
    run_row = await db.get(PayrollRun, uuid.UUID(run["id"]))
    payslips = list((await db.execute(select(Payslip).where(Payslip.run_id == run_row.id))).scalars())
    makers = await prs.compute_makers(db, run_row, payslips, scope="run", requester_id=p.requested_by, lock=False)
    assert "FNF_MAKER" in makers[str(maker_id)]
    assert "CREATED_COMPONENT" not in makers.get(str(admin2_user.id), set())  # the F&F checker is not a maker
    assert (await decide(client, admin2, req, "approve"))["request"]["status"] == "approved"


async def test_run_contributors_are_tracked_for_every_action(client, payroll, db, uid):
    hr, admin = payroll["hr"], payroll["admin"]
    run = await new_run(client, hr)
    rid = uuid.UUID(run["id"])
    row = await db.get(PayrollRun, rid)
    assert row.contributors == [uid["hr"]]
    await client.post(f"{RUNS}/{run['id']}/refresh", headers=admin)
    await process(client, admin, run["id"])
    await client.post(f"{RUNS}/{run['id']}/reopen", headers=payroll["hr"])
    db.expire_all()
    row = await db.get(PayrollRun, rid)
    assert set(row.contributors) == {uid["hr"], uid["admin"]}
    slips = list((await db.execute(select(Payslip).where(Payslip.run_id == rid))).scalars())
    assert slips and all(
        set(p.contributors) == {uid["hr"], uid["admin"]} for p in slips
    )  # created, refreshed, processed, reopened
    pid = slips[0].id
    mgr_edit = await client.put(f"{PAYSLIPS}/{pid}", json={"expected_version": slips[0].version, "note": "n"}, headers=admin)
    assert mgr_edit.status_code == 200


async def test_contributors_survive_a_published_run_and_revisions(client, payroll, db, uid):
    hr, admin = payroll["hr"], payroll["admin"]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    row = await row_for(client, hr, run["id"], "EMP001")
    rev = await client.post(f"{RUNS}/{run['id']}/employees/{row['user']['id']}/revise", json={"reason": "fix"}, headers=admin)
    assert rev.status_code == 201, rev.text
    pid = uuid.UUID(rev.json()["id"])
    p = await db.get(Payslip, pid)
    assert p.contributors == [uid["admin"]]  # a revision starts its own history
    assert (await client.post(f"{PAYSLIPS}/{pid}/process", headers=hr)).status_code == 200
    db.expire_all()
    p = await db.get(Payslip, pid)
    assert set(p.contributors) == {uid["admin"], uid["hr"]}


# ================================================================================================ scope payslip
async def revised_processed(client, payroll, db, seeded, emp="EMP001"):
    hr, admin = payroll["hr"], payroll["admin"]
    await enable_mc(client, admin)
    run = await new_run(client, hr)
    await process(client, hr, run["id"])
    req = await request_run(client, hr, run["id"])
    _, admin2 = await extra_user(client, db, seeded, "ADM002")
    await decide(client, admin2, req, "approve")
    row = await row_for(client, hr, run["id"], emp)
    rev = await client.post(f"{RUNS}/{run['id']}/employees/{row['user']['id']}/revise", json={"reason": "fix"}, headers=hr)
    assert rev.status_code == 201, rev.text
    pid = rev.json()["id"]
    r = await client.post(f"{PAYSLIPS}/{pid}/process", headers=hr)
    assert r.status_code == 200, r.text
    return run, pid, admin2


async def test_payslip_scope_request_and_direct_publish_gate(client, payroll, db, seeded):
    run, pid, admin2 = await revised_processed(client, payroll, db, seeded)
    hr, admin = payroll["hr"], payroll["admin"]
    r = await client.post(f"{PAYSLIPS}/{pid}/publish", json={"current_password": PASSWORD}, headers=admin)
    assert r.status_code == 409 and err(r) == "PUBLISH_REQUEST_REQUIRED"
    req = await request_payslip(client, hr, pid)
    assert req["scope"] == "payslip" and req["payslip"]["id"] == pid and req["payslip"]["revision"] == 2
    assert req["payslip"]["user"]["employee_id"] == "EMP001" and req["snapshot"]["payslips"] == 1
    got = (await client.get(f"{PAYSLIPS}/{pid}", headers=hr)).json()
    assert got["publish_request"]["id"] == req["id"] and got["publish_request"]["status"] == "submitted"
    await decide(client, hr, req, "approve", 403)
    out = await decide(client, admin, req, "approve")
    assert out["request"]["status"] == "approved" and out["payslip"]["status"] == "published" and out["payslip"]["revision"] == 2
    assert "run" not in out
    old = await db.get(
        Payslip,
        (
            await db.execute(
                select(Payslip.id).where(Payslip.revision == 1, Payslip.user_id == uuid.UUID(out["payslip"]["user"]["id"]))
            )
        ).scalar_one(),
    )
    await db.refresh(old)
    assert old.status == "superseded"


async def test_the_employee_of_a_payslip_request_cannot_decide_it(client, payroll, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    me, admin3 = await extra_user(client, db, seeded, "ADM003", on_payroll=True)  # a publisher who is also on the payroll
    r = await client.post(f"{ADMIN}/employees/{me.id}/structures", json={
        "effective_from": "2025-01-01", "basic": "20000", "hra": "0", "allowances": [], "fixed_deductions": [], "tds_monthly": "0",
        "pf_applicable": True, "pf_on_full_basic": False, "esi_applicable": True, "pt_applicable": True}, headers=hr)  # fmt: skip
    assert r.status_code == 201, r.text
    await enable_mc(client, admin)
    run = await new_run(client, hr)
    await process(client, hr, run["id"])
    req = await request_run(client, hr, run["id"])
    _, admin2 = await extra_user(client, db, seeded, "ADM002")
    await decide(client, admin2, req, "approve")
    row = await row_for(client, hr, run["id"], "ADM003")
    rev = await client.post(f"{RUNS}/{run['id']}/employees/{row['user']['id']}/revise", json={"reason": "fix"}, headers=hr)
    await client.post(f"{PAYSLIPS}/{rev.json()['id']}/process", headers=hr)
    preq = await request_payslip(client, hr, rev.json()["id"])
    body = await decide(client, admin3, preq, "approve", 403)
    assert err_code(body) == "FORBIDDEN"  # the employee: not SECOND_PERSON_REQUIRED
    denied = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.denied"))).scalars().all()
    assert denied and denied[-1].after["reason"] == "self"
    check = (await client.get(f"{PAYSLIPS}/{rev.json()['id']}/publish-check", headers=admin3)).json()
    assert "OWN_PAYSLIP" in check["me"]["reasons"] and check["can_approve"] is False and check["can_request"] is False
    r = await client.post(f"{PAYSLIPS}/{rev.json()['id']}/publish-requests", json={}, headers=admin3)
    assert r.status_code == 403  # nobody requests their own payroll
    assert (await decide(client, admin, preq, "approve"))["request"]["status"] == "approved"


# ================================================================================================ staleness
async def test_reopen_marks_the_request_stale_and_approve_is_refused(client, payroll, db):
    run = await processed_run(client, payroll)
    hr, admin = payroll["hr"], payroll["admin"]
    req = await request_run(client, hr, run["id"])
    await client.post(f"{RUNS}/{run['id']}/reopen", headers=hr)
    got = (await client.get(f"{REQUESTS}/{req['id']}", headers=hr)).json()
    assert got["status"] == "stale" and got["stale_reason"] == "RUN_REOPENED"
    body = await decide(client, admin, got, "approve", 409)
    assert err_code(body) == "NOT_SUBMITTED"
    await process(client, hr, run["id"])
    new = await request_run(client, hr, run["id"])  # the slot is free again
    assert new["id"] != req["id"]
    db.expire_all()
    ev = [e.action for e in (await db.execute(select(PayrollItemEvent))).scalars() if e.item_type == "publish_request"]
    assert ev == ["create", "stale", "create"]


async def test_a_changed_payslip_is_publish_stale_and_marks_the_request(client, payroll, db):
    run = await processed_run(client, payroll)
    req = await request_run(client, payroll["hr"], run["id"])
    ps = (await db.execute(select(Payslip).where(Payslip.run_id == uuid.UUID(run["id"])))).scalars().first()
    res = dict(ps.result)
    res["net"] = "1.00"  # somebody changed the processed result behind the request
    ps.result = res
    await db.commit()
    body = await decide(client, payroll["admin"], req, "approve", 409)
    assert err_code(body) == "PUBLISH_STALE"
    db.expire_all()
    row = await db.get(PayrollPublishRequest, uuid.UUID(req["id"]))
    assert row.status == "stale" and row.stale_reason == "PAYSLIP_CHANGED"
    assert (await db.get(PayrollRun, uuid.UUID(run["id"]))).status == "processed"  # nothing was published


async def _expire(db, req) -> None:
    """Move the request into the past (the CHECK needs expires_at > requested_at)."""
    row = await db.get(PayrollPublishRequest, uuid.UUID(req["id"]))
    now = dt.datetime.now(dt.UTC)
    row.requested_at, row.expires_at = now - dt.timedelta(hours=3), now - dt.timedelta(seconds=5)
    await db.commit()


async def test_an_expired_request_is_stale_expired(client, payroll, db):
    run = await processed_run(client, payroll)
    req = await request_run(client, payroll["hr"], run["id"])
    await _expire(db, req)
    body = await decide(client, payroll["admin"], req, "approve", 409)
    assert err_code(body) == "PUBLISH_STALE"
    db.expire_all()
    row = await db.get(PayrollPublishRequest, uuid.UUID(req["id"]))
    assert row.status == "stale" and row.stale_reason == "EXPIRED"
    ev = [e.action for e in (await db.execute(select(PayrollItemEvent))).scalars() if e.item_type == "publish_request"]
    assert ev == ["create", "expire"]


async def test_lazy_expiry_on_read_and_ttl_setting(client, payroll, db):
    await client.put(f"{ADMIN}/settings", json={"settings": {"payroll.publish_request_ttl_hours": 1}}, headers=payroll["hr"])
    run = await processed_run(client, payroll)
    req = await request_run(client, payroll["hr"], run["id"])
    row = await db.get(PayrollPublishRequest, uuid.UUID(req["id"]))
    assert 0 < (row.expires_at.replace(tzinfo=dt.UTC) - row.requested_at.replace(tzinfo=dt.UTC)).total_seconds() <= 3600
    await _expire(db, req)
    listing = (await client.get(REQUESTS, params={"status": "stale"}, headers=payroll["hr"])).json()
    assert [i["id"] for i in listing["items"]] == [req["id"]] and listing["items"][0]["stale_reason"] == "EXPIRED"
    assert await prs.expire_overdue(db) == 0  # nothing left to expire


async def test_components_changed_rolls_everything_back_and_keeps_the_request_open(client, payroll, db, monkeypatch):
    run = await processed_run(client, payroll)
    req = await request_run(client, payroll["hr"], run["id"])
    from app.services import payroll_runs as runs

    async def boom(*a, **k):
        raise AppError("COMPONENTS_CHANGED", "changed")

    monkeypatch.setattr(runs.comps, "mark_paid", boom)
    body = await decide(client, payroll["admin"], req, "approve", 409)
    assert err_code(body) == "COMPONENTS_CHANGED"
    db.expire_all()
    row = await db.get(PayrollPublishRequest, uuid.UUID(req["id"]))
    assert row.status == "submitted" and row.decided_by is None and row.version == 1
    assert (await db.get(PayrollRun, uuid.UUID(run["id"]))).status == "processed"


async def test_a_tampered_snapshot_is_detected(client, payroll, db):
    run = await processed_run(client, payroll)
    req = await request_run(client, payroll["hr"], run["id"])
    row = await db.get(PayrollPublishRequest, uuid.UUID(req["id"]))
    snap = json.loads(json.dumps(row.snapshot))
    snap["payslips"] = snap["payslips"][:1]
    row.snapshot = snap
    await db.commit()
    body = await decide(client, payroll["admin"], req, "approve", 409)
    assert err_code(body) == "PUBLISH_STALE"


# ================================================================================================ delete guards
async def test_delete_run_and_payslip_with_requests_is_a_clean_409(client, payroll, db, seeded):
    run = await processed_run(client, payroll)
    hr = payroll["hr"]
    await request_run(client, hr, run["id"])
    await client.post(f"{RUNS}/{run['id']}/reopen", headers=hr)  # the request is stale, the run is draft again
    r = await client.delete(f"{RUNS}/{run['id']}", headers=hr)
    assert r.status_code == 409 and err(r) == "CONFLICT"
    slips = (await client.get(f"{RUNS}/{run['id']}/payslips", headers=hr)).json()["items"]
    assert (
        await client.delete(f"{PAYSLIPS}/{slips[0]['id']}", headers=hr)
    ).status_code == 204  # run-scope requests do not pin payslips


async def test_delete_payslip_with_a_payslip_request_is_a_clean_409(client, payroll, db, seeded):
    run, pid, _ = await revised_processed(client, payroll, db, seeded)
    hr = payroll["hr"]
    await request_payslip(client, hr, pid)
    assert (await client.post(f"{PAYSLIPS}/{pid}/reopen", headers=hr)).status_code == 200  # the request becomes stale
    r = await client.delete(f"{PAYSLIPS}/{pid}", headers=hr)
    assert r.status_code == 409 and err(r) == "CONFLICT"
    assert run["id"]


# ================================================================================================ publish-check + lists
async def test_publish_check_shapes_and_roles(client, payroll, db, seeded):
    run = await processed_run(client, payroll)
    hr, admin = payroll["hr"], payroll["admin"]
    c = (await client.get(f"{RUNS}/{run['id']}/publish-check", headers=hr)).json()
    assert set(c) == {
        "maker_checker",
        "can_request",
        "can_publish_directly",
        "can_approve",
        "open_request",
        "me",
        "makers",
        "publishable",
    }
    assert (
        c["maker_checker"] is True
        and c["can_request"] is True
        and c["can_approve"] is False
        and c["can_publish_directly"] is False
    )
    assert c["publishable"] == {"payslips": 2, "held": 0} and c["me"]["reasons"] == [
        "PROCESSED_RUN",
        "EDITED_PAYSLIP",
        "CREATED_STRUCTURE",
    ]
    req = await request_run(client, hr, run["id"])
    c = (await client.get(f"{RUNS}/{run['id']}/publish-check", headers=hr)).json()
    assert (
        c["open_request"] == {"id": req["id"], "status": "submitted"}
        and c["can_request"] is False
        and "REQUESTED" in c["me"]["reasons"]
    )
    a = (await client.get(f"{RUNS}/{run['id']}/publish-check", headers=admin)).json()
    assert a["can_approve"] is True and a["me"]["reasons"] == []
    assert [m["user"]["employee_id"] for m in a["makers"]] == ["HR001"]
    assert set(a["makers"][0]) == {"user", "reasons"}
    assert (await client.get(f"{PAYSLIPS}/{uuid.uuid4()}/publish-check", headers=hr)).status_code == 404
    assert (await client.get(f"{RUNS}/{uuid.uuid4()}/publish-check", headers=hr)).status_code == 404


async def test_list_and_detail_shapes_and_audit(client, payroll, db, uid):
    run = await processed_run(client, payroll)
    hr, admin = payroll["hr"], payroll["admin"]
    req = await request_run(client, hr, run["id"])
    page = (await client.get(REQUESTS, headers=hr)).json()
    assert (
        page["total"] == 1 and page["page"] == 1 and "totals" not in page["items"][0]["snapshot"] and "makers" in page["items"][0]
    )
    assert page["items"][0]["makers"] is None
    assert (await client.get(REQUESTS, params={"status": "approved"}, headers=hr)).json()["total"] == 0
    assert (await client.get(REQUESTS, params={"run_id": run["id"]}, headers=hr)).json()["total"] == 1
    assert (await client.get(REQUESTS, params={"status": "bogus"}, headers=hr)).status_code == 422
    one = (await client.get(f"{REQUESTS}/{req['id']}", headers=hr)).json()
    assert "totals" in one["snapshot"]
    acts = [
        a.action
        for a in (await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars()
        if a.action.startswith("payroll.publish_request")
    ]
    assert (
        "payroll.publish_request.list" in acts
        and "payroll.publish_request.view" in acts
        and "payroll.publish_request.create" in acts
    )
    lst = next(a for a in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.publish_request.list"))).scalars())
    assert lst.after == {"count": 1}
    del admin, uid


async def test_pushes_carry_no_amounts_and_go_to_the_right_people(client, payroll, db, seeded, uid):
    _, admin2 = await extra_user(client, db, seeded, "ADM002")
    run = await processed_run(client, payroll)
    notifications.OUTBOX.clear()
    req = await request_run(client, payroll["hr"], run["id"])
    sent = [o for o in notifications.OUTBOX if o["payload"].get("kind") == "publish"]
    assert len(sent) == 1 and uid["hr"] not in sent[0]["user_ids"] and uid["admin"] in sent[0]["user_ids"]
    assert (
        sent[0]["payload"]["body"] == "Test Hr asked to publish payroll for June 2025"
        and sent[0]["payload"]["type"] == "payroll_approval"
    )
    notifications.OUTBOX.clear()
    await decide(client, admin2, req, "approve")
    mine = [o for o in notifications.OUTBOX if o["payload"].get("kind") == "publish"]
    assert [o["user_ids"] for o in mine] == [[uid["hr"]]] and mine[0]["payload"]["title"] == "Publish request approved"
    assert mine[0]["payload"]["body"] == "June 2025"
    for o in notifications.OUTBOX:
        assert not any(ch.isdigit() for ch in o["payload"]["body"].replace("2025", "")), o["payload"]  # no amounts anywhere


async def test_payslip_level_reopen_marks_every_open_request_of_the_run_stale(client, payroll, db, seeded):
    run, pid, admin2 = await revised_processed(client, payroll, db, seeded)
    hr = payroll["hr"]
    mgr_row = await row_for(client, hr, run["id"], "MGR001")
    rev = await client.post(f"{RUNS}/{run['id']}/employees/{mgr_row['user']['id']}/revise", json={"reason": "fix"}, headers=hr)
    pid2 = rev.json()["id"]
    assert (await client.post(f"{PAYSLIPS}/{pid2}/process", headers=hr)).status_code == 200
    a = await request_payslip(client, hr, pid)
    b = await request_payslip(client, hr, pid2)
    assert (await client.post(f"{PAYSLIPS}/{pid}/reopen", headers=hr)).status_code == 200
    for req in (a, b):
        got = (await client.get(f"{REQUESTS}/{req['id']}", headers=hr)).json()
        assert got["status"] == "stale" and got["stale_reason"] == "PAYSLIP_CHANGED"
    await decide(client, payroll["admin"], b, "approve", 409)  # NOT_SUBMITTED: ask again
    assert (await client.get(f"{PAYSLIPS}/{pid2}", headers=hr)).json()["publish_request"]["status"] == "stale"


async def test_refresh_marks_an_open_request_stale(client, payroll, db):
    run = await processed_run(client, payroll)
    req = await request_run(client, payroll["hr"], run["id"])
    row = await db.get(PayrollRun, uuid.UUID(run["id"]))
    row.status = "draft"  # behind the service's back: the refresh hook is defensive
    await db.commit()
    assert (await client.post(f"{RUNS}/{run['id']}/refresh", headers=payroll["hr"])).status_code == 200
    got = (await client.get(f"{REQUESTS}/{req['id']}", headers=payroll["hr"])).json()
    assert got["status"] == "stale" and got["stale_reason"] == "RUN_REFRESHED"


async def test_dashboard_alert_for_an_open_request(client, payroll):
    run = await processed_run(client, payroll)
    assert (
        "PUBLISH_PENDING_APPROVAL"
        not in (await client.get(f"{ADMIN}/dashboard", params={"period": "2025-06"}, headers=payroll["hr"])).json()["calendar"][
            "alerts"
        ]
    )
    await request_run(client, payroll["hr"], run["id"])
    assert (
        "PUBLISH_PENDING_APPROVAL"
        in (await client.get(f"{ADMIN}/dashboard", params={"period": "2025-06"}, headers=payroll["hr"])).json()["calendar"][
            "alerts"
        ]
    )


def test_result_mac_is_a_keyed_hash_over_everything_reviewed():
    from types import SimpleNamespace

    base = dict(id=uuid.uuid4(), version=3, revision=1, hold=False, result={"net": "100.00"}, inputs={"tds": None},
                identity={"bank_account": "1"}, employee_snapshot={"full_name": "A"}, basis_days=30, employed_days=30, paid_days=30, structure_id=None)  # fmt: skip
    mac = prs.result_mac(SimpleNamespace(**base))
    assert len(mac) == 64 and mac == prs.result_mac(SimpleNamespace(**base))
    for field, value in (("result", {"net": "101.00"}), ("inputs", {"tds": "5.00"}), ("identity", {"bank_account": "2"}),
                         ("version", 4), ("hold", True), ("employee_snapshot", {"full_name": "B"})):  # fmt: skip
        assert prs.result_mac(SimpleNamespace(**{**base, field: value})) != mac, field
    from hashlib import sha256

    assert mac != sha256(b"{}").hexdigest()  # keyed: not a plain hash of the content


async def test_step_up_is_checked_before_the_state_of_the_request(client, payroll):
    run = await processed_run(client, payroll)
    req = await request_run(client, payroll["hr"], run["id"])
    w = await decide(client, payroll["hr"], req, "withdraw")
    r = await client.post(
        f"{REQUESTS}/{w['id']}/approve",
        json={"expected_version": 1, "current_password": "Wrong!Passw0rd#1"},
        headers=payroll["admin"],
    )
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"  # not NOT_SUBMITTED
    await decide(client, payroll["admin"], w, "approve", 409)


def test_database_refusals_map_to_clean_errors_never_500():
    from sqlalchemy.exc import IntegrityError

    class Orig(Exception):
        def __init__(self, state: str, msg: str):
            super().__init__(msg)
            self.sqlstate = state

    def exc(state: str, msg: str) -> IntegrityError:
        return IntegrityError("stmt", {}, Orig(state, msg))

    cases = (
        ("23514", 'violates check constraint "ck_payroll_publish_requests_checker_history"', "SECOND_PERSON_REQUIRED"),
        ("23514", 'violates check constraint "ck_payroll_publish_requests_requester_in_makers"', "SECOND_PERSON_REQUIRED"),
        ("23000", "publish request x: the decider contributed to the run", "SECOND_PERSON_REQUIRED"),
        (
            "23505",
            'duplicate key value violates unique constraint "uq_payroll_publish_requests_open_run"',
            "PUBLISH_REQUEST_EXISTS",
        ),
        ("23000", "publish request x is submitted and locked", "PAYROLL_LOCKED"),
    )
    for state, msg, code in cases:
        assert prs._map_db(exc(state, msg)).code == code, msg
