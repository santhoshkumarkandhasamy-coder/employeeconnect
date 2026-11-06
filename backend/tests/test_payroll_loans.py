"""Plan 15 work package 3a, step 2: loans and advances (schedule, skip, reschedule, foreclose, cancel, engine integration).

Runs on SQLite; row locks (`FOR UPDATE`), the nextval() reference and the DB triggers are PostgreSQL-only.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from sqlalchemy import LargeBinary, select, type_coerce

from app.models import AuditLog, PayrollItemEvent, PayrollLoan
from app.services import payroll_loans as loans_svc
from app.services.notifications import OUTBOX
from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, new_run, process_and_publish, publish_run, row_for, set_setting

LOANS = "/api/admin/payroll/loans"
RUNS = "/api/admin/payroll/runs"


async def mk_loan(client, hdr, user_id, *, kind="loan", principal="30000.00", emi="10000.00", start=PERIOD, expect=201, **kw):
    body = {
        "user_id": user_id, "kind": kind, "principal": principal, "emi": emi, "start_period": start,
        "reason": "Medical emergency", "current_password": PASSWORD,
    }  # fmt: skip
    body.update(kw)
    r = await client.post(LOANS, json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def act(client, hdr, loan, action, expect=200, **body):
    body.setdefault("expected_version", loan["version"] if isinstance(loan, dict) else 1)
    lid = loan["id"] if isinstance(loan, dict) else loan
    if action != "skip" and "current_password" not in body:
        body["current_password"] = PASSWORD
    body.setdefault("reason", "Because HR said so")
    r = await client.post(f"{LOANS}/{lid}/{action}", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def get_loan(client, hdr, lid):
    r = await client.get(f"{LOANS}/{lid}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


async def payslip(client, hdr, run_id, emp="EMP001"):
    row = await row_for(client, hdr, run_id, emp)
    r = await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


def periods(loan, *statuses):
    return [(i["period"], i["amount"]) for i in loan["installments"] if not statuses or i["status"] in statuses]


def err(r):
    return r.json()["error"]["code"]


@pytest.fixture(autouse=True)
def _relaxed_limits(request, monkeypatch):
    """The 10/min action limiter and the 5/min step-up limiter are covered by their own tests; these scenarios do more."""
    OUTBOX.clear()
    if "rate_limited" in request.node.name:
        return
    from app.services import admin_security
    from app.services import payroll_common as pc

    async def no_limit(*a, **k):
        return None

    monkeypatch.setattr(pc, "limit", no_limit)
    monkeypatch.setattr(admin_security, "REAUTH_PER_MINUTE", 10_000)
    monkeypatch.setattr(admin_security, "REAUTH_PER_DAY", 10_000)


# ================================================================================================ pure schedule
def test_schedule_is_fixed_emi_with_the_remainder_last():
    s = loans_svc.make_schedule(D("25000.00"), D("10000.00"), dt.date(2025, 11, 1))
    assert s == [
        (dt.date(2025, 11, 1), D("10000.00")), (dt.date(2025, 12, 1), D("10000.00")), (dt.date(2026, 1, 1), D("5000.00"))
    ]  # fmt: skip
    assert loans_svc.installment_count(D("25000.00"), D("10000.00")) == 3
    assert loans_svc.make_schedule(D("100.00"), D("100.00"), dt.date(2025, 1, 1)) == [(dt.date(2025, 1, 1), D("100.00"))]
    total = sum(a for _, a in loans_svc.make_schedule(D("10000.01"), D("3333.33"), dt.date(2025, 1, 1)))
    assert total == D("10000.01")


# ================================================================================================ create
async def test_create_generates_schedule_reference_and_push(client, payroll, uid, db):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"], principal="25000.00")
    assert loan["reference"] == "L-0001" and loan["kind"] == "loan" and loan["status"] == "active"
    assert (loan["principal"], loan["emi"], loan["recovered"], loan["in_process"], loan["outstanding"]) == (
        "25000.00", "10000.00", "0.00", "0.00", "25000.00"
    )  # fmt: skip
    assert periods(loan) == [("2025-06", "10000.00"), ("2025-07", "10000.00"), ("2025-08", "5000.00")]
    assert [i["seq"] for i in loan["installments"]] == [1, 2, 3]
    assert loan["user"]["employee_id"] == "EMP001" and loan["reason"] == "Medical emergency" and loan["version"] == 1
    second = await mk_loan(client, hr, uid["manager"], kind="advance", principal="5000.00", emi="5000.00")
    assert second["reference"] == "L-0002"
    push = [m for m in OUTBOX if m["payload"]["type"] == "loan_created"]
    assert len(push) == 2 and push[0]["payload"]["id"] == loan["id"]
    assert "25000" not in str(push[0]["payload"]) and "10000" not in str(push[0]["payload"])  # never amounts
    # money is stored encrypted: the raw column is not the plain amount
    raw = (await db.execute(select(type_coerce(PayrollLoan.principal, LargeBinary)))).scalars().first()
    assert b"25000" not in bytes(raw)


async def test_create_validation_and_step_up(client, payroll, uid):
    hr = payroll["hr"]
    emp = uid["employee"]
    await mk_loan(client, hr, emp, principal="0.50", emi="0.50", expect=422)  # below 1.00
    await mk_loan(client, hr, emp, principal="2000000.00", emi="100000.00", expect=422)  # above 1,000,000
    await mk_loan(client, hr, emp, principal="1000.00", emi="2000.00", expect=422)  # emi > principal
    await mk_loan(client, hr, emp, principal="1000.00", emi="0.00", expect=422)
    r = await client.post(
        LOANS, json={"user_id": emp, "kind": "loan", "principal": "100000.00", "emi": "1000.00", "start_period": PERIOD,
                     "reason": "Too long", "current_password": PASSWORD}, headers=hr,
    )  # fmt: skip
    assert r.status_code == 422 and "60" in r.text  # 100 months > the 60 month tenure limit
    await mk_loan(client, hr, emp, principal="1000.00", emi="500.00", start="2030-01", expect=422)  # > 12 months ahead
    r = await client.post(LOANS, json={"user_id": emp, "kind": "loan", "principal": "1000.00", "emi": "500.00",
                                       "start_period": PERIOD, "reason": "No password"}, headers=hr)  # fmt: skip
    assert r.status_code == 422
    await mk_loan(client, hr, emp, principal="1000.00", emi="500.00", current_password="wrong-password", expect=403)
    await mk_loan(client, hr, emp, principal="1000.00", emi="500.00", reason="x", expect=422)
    await mk_loan(client, hr, emp, principal="1000.00", emi="500.00", surprise=1, expect=422)
    await mk_loan(client, hr, emp, principal=1000, emi="500.00", expect=422)  # money is a string


async def test_tenure_limit_is_a_setting_and_active_limit_is_enforced(client, payroll, uid):
    hr = payroll["hr"]
    await set_setting(client, payroll["admin"], loan_max_tenure_months=2, loan_max_active=2)
    await mk_loan(client, hr, uid["employee"], principal="30000.00", emi="10000.00", expect=422)  # 3 months > 2
    await mk_loan(client, hr, uid["employee"], principal="20000.00", emi="10000.00")
    await mk_loan(client, hr, uid["employee"], principal="2000.00", emi="1000.00")
    r = await client.post(
        LOANS, json={"user_id": uid["employee"], "kind": "advance", "principal": "100.00", "emi": "100.00",
                     "start_period": PERIOD, "reason": "One too many", "current_password": PASSWORD}, headers=hr,
    )  # fmt: skip
    assert r.status_code == 409 and err(r) == "LOAN_LIMIT_REACHED"


async def test_self_rule_permissions_and_unknown_ids(client, payroll, uid):
    hr = payroll["hr"]
    # HR001 cannot give HR001 a loan (403, audited as payroll.denied)
    await mk_loan(client, hr, uid["hr"], expect=403)
    emp = payroll["employee"]
    for method, path in (("GET", LOANS), ("POST", LOANS), ("GET", f"{LOANS}/{uid['employee']}")):
        r = await client.request(method, path, headers=emp, json={} if method == "POST" else None)
        assert r.status_code == 403, (method, path)
    r = await client.get(f"{LOANS}/00000000-0000-0000-0000-000000000001", headers=hr)
    assert r.status_code == 404
    await mk_loan(client, hr, "00000000-0000-0000-0000-000000000001", expect=404)
    await mk_loan(client, payroll["admin"], uid["hr"], expect=422)  # HR001 is excluded from payroll by the fixture


async def test_period_must_be_open(client, payroll, uid):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    await mk_loan(client, hr, uid["employee"], start=PERIOD, expect=409)  # published, no open revision
    ok = await mk_loan(client, hr, uid["employee"], start="2025-07")
    assert ok["installments"][0]["flags"] == []


# ================================================================================================ engine integration
async def test_installments_flow_through_create_process_reopen_publish(client, payroll, uid, db):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"], principal="25000.00")
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    comp = p["inputs"]["components"]
    assert [(c["code"], c["label"], c["amount"]) for c in comp] == [("LOAN_EMI", "Loan L-0001 (1/3)", "10000.00")]
    assert comp[0]["sources"][0]["type"] == "loan_installment"
    ded = next(d for d in p["deductions"] if d["code"] == "LOAN_EMI")
    assert ded["amount"] == "10000.00"

    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    l2 = await get_loan(client, hr, loan["id"])
    assert l2["installments"][0]["status"] == "processing" and l2["installments"][0]["payslip_id"] == p["id"]
    assert (l2["recovered"], l2["in_process"], l2["outstanding"]) == ("0.00", "10000.00", "25000.00")
    # a processed installment is locked
    await act(client, hr, l2, "skip", 409, installment_id=l2["installments"][0]["id"])

    assert (await client.post(f"{RUNS}/{run['id']}/reopen", headers=hr)).status_code == 200
    l3 = await get_loan(client, hr, loan["id"])
    assert l3["installments"][0]["status"] == "pending" and l3["installments"][0]["payslip_id"] is None

    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    assert (await publish_run(client, hr, run["id"])).status_code == 200
    l4 = await get_loan(client, hr, loan["id"])
    assert l4["installments"][0]["status"] == "paid"
    assert (l4["recovered"], l4["in_process"], l4["outstanding"], l4["status"]) == ("10000.00", "0.00", "15000.00", "active")
    db.expire_all()
    ev = [
        (e.item_type, e.action)
        for e in (await db.execute(select(PayrollItemEvent))).scalars()
        if e.item_type == "loan_installment"
    ]
    for want in ("create", "process", "reopen", "pay"):
        assert ("loan_installment", want) in ev


async def test_advance_uses_advance_recovery_and_loan_closes_when_recovered(client, payroll, uid):
    hr = payroll["hr"]
    adv = await mk_loan(client, hr, uid["employee"], kind="advance", principal="4000.00", emi="4000.00")
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    assert [(c["code"], c["label"]) for c in p["inputs"]["components"]] == [("ADVANCE_RECOVERY", "Advance L-0001 (1/1)")]
    await process_and_publish(client, hr, run["id"])
    done = await get_loan(client, hr, adv["id"])
    assert (done["status"], done["close_mode"], done["outstanding"], done["recovered"]) == (
        "closed",
        "recovered",
        "0.00",
        "4000.00",
    )
    assert done["closed_at"] is not None and done["version"] == 2


async def test_held_payslip_keeps_its_installment_pending(client, payroll, uid):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"])
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    r = await client.put(
        f"/api/admin/payroll/payslips/{p['id']}", json={"expected_version": p["version"], "hold": True}, headers=hr
    )
    assert r.status_code == 200, r.text
    await process_and_publish(client, hr, run["id"])
    assert (await get_loan(client, hr, loan["id"]))["installments"][0]["status"] == "pending"


async def test_revision_carries_a_paid_installment(client, payroll, uid):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"], principal="20000.00")
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    published = await payslip(client, hr, run["id"])
    r = await client.post(f"{RUNS}/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Fix attendance"}, headers=hr)
    assert r.status_code == 201, r.text
    rev = r.json()
    assert [(c["code"], c["amount"]) for c in rev["inputs"]["components"]] == [("LOAN_EMI", "10000.00")]  # carried paid row
    pr = await client.post(f"/api/admin/payroll/payslips/{rev['id']}/process", headers=hr)
    assert pr.status_code == 200, pr.text
    assert (
        await client.post(f"/api/admin/payroll/payslips/{rev['id']}/publish", json={"current_password": PASSWORD}, headers=hr)
    ).status_code == 200
    after = await get_loan(client, hr, loan["id"])
    inst = after["installments"][0]
    assert inst["status"] == "paid" and inst["payslip_id"] == rev["id"] != published["id"]  # follows the new revision
    assert after["recovered"] == "10000.00"


async def test_warning_for_manual_overlap_with_a_loan_line(client, payroll, uid):
    hr = payroll["hr"]
    await mk_loan(client, hr, uid["employee"])
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    r = await client.put(
        f"/api/admin/payroll/payslips/{p['id']}",
        json={"expected_version": p["version"], "adjustments": [{"code": "LOAN_EMI", "label": "manual", "amount": "100"}]},
        headers=hr,
    )
    assert r.status_code == 200 and "MANUAL_COMPONENT_OVERLAP" in r.json()["warnings"]


# ================================================================================================ skip
async def test_skip_defers_one_installment_to_the_end(client, payroll, uid):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"], principal="25000.00")
    first = loan["installments"][0]
    after = await act(client, hr, loan, "skip", installment_id=first["id"])
    assert after["version"] == 2
    by_status = {i["seq"]: (i["status"], i["period"], i["amount"]) for i in after["installments"]}
    assert by_status == {
        1: ("skipped", "2025-06", "10000.00"), 2: ("pending", "2025-07", "10000.00"),
        3: ("pending", "2025-08", "5000.00"), 4: ("pending", "2025-09", "10000.00"),
    }  # fmt: skip
    assert after["outstanding"] == "25000.00"
    # stale version / not pending / unknown installment / not active
    await act(client, hr, loan, "skip", 409, installment_id=after["installments"][1]["id"])  # expected_version 1 is stale
    await act(client, hr, after, "skip", 409, installment_id=first["id"])  # already skipped
    await act(client, hr, after, "skip", 404, installment_id="00000000-0000-0000-0000-000000000001")
    await act(client, hr, after, "cancel")
    r = await client.post(f"{LOANS}/{loan['id']}/skip", headers=hr,
                          json={"installment_id": first["id"], "reason": "again", "expected_version": 3})  # fmt: skip
    assert r.status_code == 409 and err(r) == "LOAN_NOT_ACTIVE"


async def test_skip_reschedule_and_foreclose_never_exhaust_installment_numbers(client, payroll, uid):
    """Migration 0024: numbers are unique among LIVE installments only, so a loan can be skipped / rescheduled any number of times."""
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"], principal="30000.00", emi="10000.00")
    cur = loan
    for _ in range(
        22
    ):  # about 4 numbers per round used up the old table-wide numbering; the drift stays inside the 12 month horizon
        first = next(i for i in cur["installments"] if i["status"] == "pending")
        cur = await act(client, hr, cur, "skip", installment_id=first["id"])
        cur = await act(
            client,
            hr,
            cur,
            "reschedule",
            emi="10000.00",
            from_period=min(i["period"] for i in cur["installments"] if i["status"] == "pending"),
        )
    live = [i["seq"] for i in cur["installments"] if i["status"] in ("pending", "processing", "paid")]
    assert live == [1, 2, 3]  # numbers are reused, not exhausted
    assert max(i["seq"] for i in cur["installments"]) <= 4 and len(cur["installments"]) > 80
    cur = await act(client, hr, cur, "foreclose", mode="payroll", period="2025-12")
    assert [i["kind"] for i in cur["installments"] if i["status"] == "pending"] == ["foreclosure"]
    assert max(i["seq"] for i in cur["installments"] if i["status"] == "pending") == 1
    # tenure is limited on the LIVE installments (create), not on the numbers ever used
    await set_setting(client, payroll["admin"], loan_max_tenure_months=2)
    await mk_loan(client, hr, uid["manager"], principal="30000.00", emi="10000.00", expect=422)


async def test_skipping_the_last_installment_moves_it_one_month_on(client, payroll, uid):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"], principal="10000.00", emi="10000.00")
    after = await act(client, hr, loan, "skip", installment_id=loan["installments"][0]["id"])
    assert periods(after, "pending") == [("2025-07", "10000.00")]


# ================================================================================================ reschedule
async def test_reschedule_regenerates_pending_installments_from_a_month(client, payroll, uid):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"], principal="30000.00", emi="10000.00")
    run = await new_run(client, hr)
    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200  # June is processing
    cur = await get_loan(client, hr, loan["id"])
    after = await act(client, hr, cur, "reschedule", emi="15000.00", from_period="2025-07")
    assert after["emi"] == "15000.00" and after["version"] == cur["version"] + 1
    live = [(i["period"], i["amount"], i["status"]) for i in after["installments"] if i["status"] != "cancelled"]
    assert live == [("2025-06", "10000.00", "processing"), ("2025-07", "15000.00", "pending"), ("2025-08", "5000.00", "pending")]
    assert [i["status"] for i in after["installments"]].count("cancelled") == 2
    assert after["outstanding"] == "30000.00"
    # the first new month must follow the processing one; remaining must be positive; emi bounded
    await act(client, hr, after, "reschedule", 422, emi="1000.00", from_period="2025-06")
    await act(client, hr, after, "reschedule", 422, emi="30000.00", from_period="2025-07")  # more than is left
    await act(client, hr, after, "reschedule", 403, emi="1000.00", from_period="2025-07", current_password="nope-nope")


# ================================================================================================ foreclose
async def test_foreclose_via_payroll_adds_one_installment_and_closes_when_paid(client, payroll, uid):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"], principal="30000.00", emi="10000.00")
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])  # June deducted
    cur = await get_loan(client, hr, loan["id"])
    assert cur["recovered"] == "10000.00"
    after = await act(client, hr, cur, "foreclose", mode="payroll", period="2025-07")
    live = [(i["period"], i["amount"], i["kind"], i["status"]) for i in after["installments"] if i["status"] != "cancelled"]
    assert live == [("2025-06", "10000.00", "emi", "paid"), ("2025-07", "20000.00", "foreclosure", "pending")]
    assert after["status"] == "active"
    run2 = await new_run(client, hr, "2025-07")
    p = await payslip(client, hr, run2["id"])
    assert [(c["code"], c["label"], c["amount"]) for c in p["inputs"]["components"]] == [
        ("LOAN_EMI", "Loan L-0001 foreclosure", "20000.00")
    ]  # fmt: skip
    # processed -> a second foreclose is refused until the run is reopened
    assert (await client.post(f"{RUNS}/{run2['id']}/process", headers=hr)).status_code == 200
    cur2 = await get_loan(client, hr, loan["id"])
    await act(client, hr, cur2, "foreclose", 409, mode="payroll", period="2025-08")
    await act(client, hr, cur2, "cancel", 409)  # LOAN_HAS_RECOVERIES
    assert (await publish_run(client, hr, run2["id"])).status_code == 200
    done = await get_loan(client, hr, loan["id"])
    assert (done["status"], done["close_mode"], done["outstanding"], done["recovered"]) == (
        "closed", "foreclosed_payroll", "0.00", "30000.00"
    )  # fmt: skip


async def test_foreclose_period_cannot_reuse_a_deducted_month(client, payroll, uid):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"])
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    cur = await get_loan(client, hr, loan["id"])
    await act(client, hr, cur, "foreclose", 422, mode="payroll", period=PERIOD)  # that month was already deducted
    await act(client, hr, cur, "foreclose", 409, mode="payroll", period="2025-06", expected_version=99)  # stale version
    await act(client, hr, cur, "foreclose", 422, mode="payroll")  # missing period (schema)


async def test_foreclose_outside_closes_the_loan_and_keeps_the_reference_out_of_the_audit(client, payroll, uid, db):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"], principal="30000.00")
    after = await act(client, hr, loan, "foreclose", mode="outside", paid_on="2025-06-20", reference="UTR-9988-SECRET")
    assert (after["status"], after["close_mode"], after["outstanding"]) == ("closed", "foreclosed_outside", "0.00")
    assert {i["status"] for i in after["installments"]} == {"cancelled"}
    db.expire_all()
    ev = [e for e in (await db.execute(select(PayrollItemEvent))).scalars() if e.item_type == "loan" and e.action == "foreclose"]
    assert len(ev) == 1 and ev[0].detail["reference"] == "UTR-9988-SECRET" and ev[0].detail["paid_on"] == "2025-06-20"
    logs = (await db.execute(select(AuditLog))).scalars().all()
    blob = " ".join(str(a.after) + str(a.before) for a in logs)
    assert "UTR-9988" not in blob
    await act(client, hr, after, "foreclose", 409, mode="outside", paid_on="2025-06-21", reference="UTR-2", expected_version=2)
    # future date, bad reference characters, wrong field mix
    other = await mk_loan(client, hr, uid["manager"])
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    await act(client, hr, other, "foreclose", 422, mode="outside", paid_on=tomorrow, reference="UTR-1")
    await act(client, hr, other, "foreclose", 422, mode="outside", paid_on="2025-06-20", reference="<script>")
    await act(client, hr, other, "foreclose", 422, mode="outside", paid_on="2025-06-20")
    await act(client, hr, other, "foreclose", 422, mode="payroll", period="2025-07", paid_on="2025-06-20")


# ================================================================================================ cancel
async def test_cancel_before_any_recovery(client, payroll, uid):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"])
    after = await act(client, hr, loan, "cancel")
    assert (after["status"], after["close_mode"], after["outstanding"]) == ("cancelled", "cancelled", "0.00")
    assert {i["status"] for i in after["installments"]} == {"cancelled"}
    # a cancelled loan no longer reaches payslips
    run = await new_run(client, hr)
    assert (await payslip(client, hr, run["id"]))["inputs"]["components"] == []


async def test_cancel_refused_with_recoveries_and_needs_password_and_version(client, payroll, uid):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"])
    r = await client.post(f"{LOANS}/{loan['id']}/cancel", json={"reason": "no pw", "expected_version": 1}, headers=hr)
    assert r.status_code == 422
    await act(client, hr, loan, "cancel", 403, current_password="not-the-password")
    await act(client, hr, loan, "cancel", 409, expected_version=7)
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    cur = await get_loan(client, hr, loan["id"])
    r = await client.post(f"{LOANS}/{loan['id']}/cancel", headers=hr,
                          json={"reason": "late", "expected_version": cur["version"], "current_password": PASSWORD})  # fmt: skip
    assert r.status_code == 409 and err(r) == "LOAN_HAS_RECOVERIES"


# ================================================================================================ lists / audit
async def test_admin_list_filters_and_next_installment(client, payroll, uid):
    hr = payroll["hr"]
    a = await mk_loan(client, hr, uid["employee"], principal="20000.00")
    b = await mk_loan(client, hr, uid["manager"], kind="advance", principal="3000.00", emi="3000.00")
    r = await client.get(LOANS, headers=hr)
    assert r.status_code == 200 and r.json()["total"] == 2
    item = next(i for i in r.json()["items"] if i["id"] == a["id"])
    assert item["next_installment"] == {"period": "2025-06", "amount": "10000.00"}
    assert "installments" not in item and "reason" not in item
    r = await client.get(LOANS, params={"kind": "advance"}, headers=hr)
    assert [i["id"] for i in r.json()["items"]] == [b["id"]]
    r = await client.get(LOANS, params={"user_id": uid["employee"], "status": "active"}, headers=hr)
    assert [i["id"] for i in r.json()["items"]] == [a["id"]]
    assert (await client.get(LOANS, params={"status": "bogus"}, headers=hr)).status_code == 422


async def test_audit_has_no_amounts_or_reasons(client, payroll, uid, db):
    hr = payroll["hr"]
    loan = await mk_loan(client, hr, uid["employee"], principal="25000.00", reason="Private surgery for family member")
    await get_loan(client, hr, loan["id"])
    await act(client, hr, loan, "skip", installment_id=loan["installments"][0]["id"], reason="Secret skip reason")
    db.expire_all()
    rows = [a for a in (await db.execute(select(AuditLog))).scalars() if a.action.startswith("payroll.loan")]
    assert {a.action for a in rows} >= {"payroll.loan.create", "payroll.loan.view", "payroll.loan.skip"}
    blob = " ".join(f"{a.before}{a.after}" for a in rows)
    for secret in ("25000", "10000", "Private surgery", "Secret skip"):
        assert secret not in blob
    created = next(a for a in rows if a.action == "payroll.loan.create")
    assert created.after["reference"] == "L-0001" and created.after["kind"] == "loan" and created.after["start_period"] == PERIOD


# ================================================================================================ employee view
async def test_employee_sees_only_own_loans_with_friendly_statuses(client, payroll, uid, db):
    hr = payroll["hr"]
    emp = payroll["employee"]
    mine = await mk_loan(client, hr, uid["employee"], principal="25000.00", reason="Private reason")
    theirs = await mk_loan(client, hr, uid["manager"], principal="9000.00", emi="3000.00")
    await act(client, hr, mine, "skip", installment_id=mine["installments"][0]["id"])
    r = await client.get("/api/me/loans", headers=emp)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["id"] for i in items] == [mine["id"]]
    assert items[0]["outstanding"] == "25000.00" and items[0]["next_installment"] == {"period": "2025-07", "amount": "10000.00"}
    r = await client.get(f"/api/me/loans/{mine['id']}", headers=emp)
    assert r.status_code == 200
    body = r.json()
    assert [i["status"] for i in body["installments"]] == ["deferred", "scheduled", "scheduled", "scheduled"]
    flat = str(body)
    assert "Private reason" not in flat and "payslip_id" not in flat and "created_by" not in flat and "reason" not in body
    # someone else's loan is a 404 and the probe is audited
    r = await client.get(f"/api/me/loans/{theirs['id']}", headers=emp)
    assert r.status_code == 404
    db.expire_all()
    actions = [(a.action, a.outcome) for a in (await db.execute(select(AuditLog))).scalars()]
    assert ("payroll.denied", "failure") in actions and ("payroll.loan.view_own", "success") in actions
    assert (await client.get("/api/me/loans/not-a-uuid", headers=emp)).status_code == 422


async def test_me_loans_requires_login(client):
    assert (await client.get("/api/me/loans")).status_code == 401


async def test_loan_columns_are_covered_by_key_rotation():
    from app.services.key_rotation import encrypted_columns

    cols = {(t.name, c.name) for t, c in encrypted_columns()}
    for want in (
        ("payroll_loans", "principal_enc"), ("payroll_loans", "emi_enc"), ("payroll_loans", "reason_enc"),
        ("payroll_loan_installments", "amount_enc"), ("payroll_arrears", "breakdown_enc"), ("payroll_arrears", "amount_enc"),
        ("leave_encashments", "amount_enc"), ("leave_encashments", "daily_rate_enc"),
    ):  # fmt: skip
        assert want in cols


async def test_loan_actions_are_rate_limited_and_fail_closed(client, payroll, uid, monkeypatch):
    hr = payroll["hr"]
    body = {"reason": "Rate limit check", "expected_version": 1, "current_password": PASSWORD}
    ghost = "00000000-0000-0000-0000-000000000009"
    codes = [(await client.post(f"{LOANS}/{ghost}/cancel", json=body, headers=hr)).status_code for _ in range(11)]
    assert codes[:10] == [404] * 10 and codes[10] == 429
    # a Redis outage refuses the write instead of letting it through (503 TEMPORARILY_UNAVAILABLE)
    from app.core.redis import get_redis

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    client._transport.app.dependency_overrides[get_redis] = lambda: Broken()
    r = await client.post(f"{LOANS}/{ghost}/cancel", json=body, headers=hr)
    assert r.status_code == 503 and err(r) == "TEMPORARILY_UNAVAILABLE"
