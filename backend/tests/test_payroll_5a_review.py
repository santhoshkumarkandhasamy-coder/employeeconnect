"""Plan 17 work package 5a, security review fixes (H1, H2, M1 to M3, L2 to L4, L7).

"Today" is 2026-10-21 (see `quiet`); the payroll month is 2025-06. HR001 is the maker, ADM001 the checker. Database CHECKs and triggers
(the other half of these fixes) are PostgreSQL-only: they are probed in the migration 0027 notes and cross-checked in
test_migration_0027.py.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import event, select

from app.models import AuditLog, EmployeeBankChange, PayrollPaymentLine, PayrollPublishRequest, PayrollRun, Setting
from app.services import notifications
from app.services import payroll_publish_requests as prs
from tests.conftest import PASSWORD
from tests.payroll_controls_helpers import (
    ACCOUNTS,
    ADMIN,
    BATCHES,
    OTHER_BANK,
    RUNS,
    approved_batch,
    batch_act,
    enable_mc,
    err,
    extra_user,
    new_account,
    new_batch,
    published_run,
    quiet,  # noqa: F401  (fixture)
    recent_bank_changes,
    request_run,
    set_bank,
)
from tests.payroll_fixtures import add_structure, new_run, process_and_publish
from tests.test_payroll_payments import revise_and_publish

pytestmark = pytest.mark.usefixtures("quiet")
PAID_ON = "2026-10-21"


async def prep(client, payroll, uid, **kw):
    run = await published_run(client, payroll, uid, **kw)
    return run, await new_account(client, payroll["hr"])


async def exported(client, payroll, uid, run, **over):
    """HR creates and submits, ADM001 approves, HR downloads the file: the batch is `exported`. Returns its detail."""
    b = await approved_batch(client, payroll, uid, run, **over)
    await batch_act(client, payroll["hr"], b, "file")
    return (await client.get(f"{BATCHES}/{b['id']}", headers=payroll["hr"])).json()


# ================================================================================================ H1
async def test_h1_the_window_floor_applies_even_if_a_lower_value_is_stored(client, payroll, uid, db):
    hr, admin = payroll["hr"], payroll["admin"]
    run, acct = await prep(client, payroll, uid, alert_days=30)
    with recent_bank_changes():
        await set_bank(client, admin, uid["employee"], **OTHER_BANK)  # ADM001 changes EMP001's account after the publish
    row = await db.get(Setting, "payroll.bank_change_alert_days")
    if row is None:
        db.add(Setting(key="payroll.bank_change_alert_days", value=0))
    else:
        row.value = 0  # old / hand-edited data: the validation would refuse it
    await db.commit()
    b = await new_batch(client, hr, run["id"])
    emp = next(ln for ln in b["lines"] if ln["user"]["employee_id"] == "EMP001")
    assert "RECENT_BANK_CHANGE" in emp["flags"]  # the flag uses max(setting, 7)
    s = await batch_act(client, hr, b, "submit")
    r = await batch_act(client, admin, s, "approve", 403)
    assert r["error"]["code"] == "SECOND_PERSON_REQUIRED"


# ================================================================================================ M1
async def test_m1_every_bank_change_in_the_history_counts_not_only_the_latest(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    run, acct = await prep(client, payroll, uid, alert_days=30)
    with recent_bank_changes():
        await set_bank(client, admin, uid["employee"], **OTHER_BANK)  # ADM001 changes EMP001's account ...
        await set_bank(
            client, hr, uid["employee"], bank_account="999988887777"
        )  # ... and HR001 changes it again: HR is the latest
    db.expire_all()
    history = list(
        (await db.execute(select(EmployeeBankChange).where(EmployeeBankChange.user_id == uuid.UUID(uid["employee"])))).scalars()
    )
    assert sorted(str(h.changed_by) for h in history).count(uid["admin"]) == 1 and len(history) == 3  # HR (publish), ADM001, HR
    b = await new_batch(client, hr, run["id"])
    s = await batch_act(client, hr, b, "submit")
    body = await batch_act(client, admin, s, "approve", 403)
    assert body["error"]["code"] == "SECOND_PERSON_REQUIRED"  # the latest marker is HR's, but ADM001's earlier change counts too
    _, clean = await extra_user(client, db, seeded, "ADM005")
    assert (await batch_act(client, clean, s, "approve"))["status"] == "approved"


async def test_m1_a_repeated_change_with_the_same_stamp_is_one_history_row(client, payroll, uid, db):
    await set_bank(client, payroll["hr"], uid["employee"])
    n = len((await db.execute(select(EmployeeBankChange))).scalars().all())
    await set_bank(client, payroll["hr"], uid["employee"])  # nothing changed: no marker, no history row
    assert len((await db.execute(select(EmployeeBankChange))).scalars().all()) == n == 1


# ================================================================================================ H2
async def test_h2_confirm_applies_the_self_rule_per_line(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    me, adm3 = await extra_user(client, db, seeded, "ADM003", on_payroll=True)
    me_id = me.id
    await add_structure(client, hr, str(me_id), basic="20000", hra="0")
    await set_bank(client, hr, str(me_id))
    run, acct = await prep(client, payroll, uid)
    cur = await exported(client, payroll, uid, run)
    assert cur["counts"]["exported"] == 3
    mine = next(ln for ln in cur["lines"] if ln["user"]["employee_id"] == "ADM003")
    other = next(ln for ln in cur["lines"] if ln["user"]["employee_id"] == "EMP001")
    for body in ({"all_paid": True}, {"lines": [{"line_id": mine["id"], "status": "failed"}]},
                 {"lines": [{"line_id": mine["id"], "status": "paid"}]}):  # fmt: skip
        r = await batch_act(client, adm3, cur, "confirm", 403, paid_on=PAID_ON, **body)
        assert r["error"]["code"] == "FORBIDDEN"
    denied = list((await db.execute(select(AuditLog).where(AuditLog.action == "payroll.denied"))).scalars())
    assert len(denied) == 3 and all(d.after["reason"] == "self" for d in denied)
    assert (await client.get(f"{BATCHES}/{cur['id']}", headers=hr)).json()["counts"]["exported"] == 3  # nothing changed
    # he can confirm somebody else's line, and his own is confirmed by somebody else
    done = await batch_act(client, adm3, cur, "confirm", paid_on=PAID_ON, lines=[{"line_id": other["id"], "status": "failed"}])
    assert done["counts"]["failed"] == 1
    db.expire_all()
    failed = (await db.execute(select(PayrollPaymentLine).where(PayrollPaymentLine.status == "failed"))).scalar_one()
    assert failed.failed_by == me_id  # who marked the line failed is recorded
    paid = await batch_act(client, admin, done, "confirm", paid_on=PAID_ON, all_paid=True)
    assert paid["status"] == "closed" and paid["counts"] == {"lines": 3, "excluded": 0, "exported": 0, "paid": 2, "failed": 1}
    db.expire_all()
    others = (await db.execute(select(PayrollPaymentLine).where(PayrollPaymentLine.status == "paid"))).scalars().all()
    assert all(ln.failed_by is None for ln in others)


async def test_h2_whoever_failed_a_payment_cannot_prepare_submit_or_approve_the_re_batch(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    run, acct = await prep(client, payroll, uid)
    _, failer = await extra_user(client, db, seeded, "ADM004")
    cur = await exported(client, payroll, uid, run)
    mgr = next(ln for ln in cur["lines"] if ln["user"]["employee_id"] == "MGR001")
    done = await batch_act(
        client, failer, cur, "confirm", paid_on=PAID_ON, lines=[{"line_id": mgr["id"], "status": "failed"}], all_paid=True
    )
    assert done["status"] == "closed"
    # ADM004 cannot prepare the re-batch of the payment he failed: it is a blocker for him
    nope = await new_batch(client, failer, run["id"], expect=422)
    assert {d["field"]: d["message"] for d in nope["error"]["details"]} == {"EMP001": "ALREADY_PAID", "MGR001": "FAILED_BY_YOU"}
    again = await new_batch(client, hr, run["id"])
    assert [ln["flags"] for ln in again["lines"]] == [["PREVIOUSLY_FAILED"]] and again["lines"][0]["amount"] == "36400.00"
    created = [a for a in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.payment_batch.create"))).scalars()]
    assert created[0].after["flags_count"] == 0 and created[-1].after["flags_count"] == 1  # the new flag is counted
    r = await client.put(f"{BATCHES}/{again['id']}", json={"expected_version": again["version"]}, headers=failer)
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    r = await client.post(f"{BATCHES}/{again['id']}/submit", json={"expected_version": again["version"]}, headers=failer)
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    s = await batch_act(client, hr, again, "submit")
    body = await batch_act(client, failer, s, "approve", 403)
    assert body["error"]["code"] == "SECOND_PERSON_REQUIRED"
    assert (await batch_act(client, admin, s, "approve"))["status"] == "approved"


# ================================================================================================ M3
async def test_m3_a_failed_ancestor_line_after_a_paid_delta_is_an_underpaid_chain(client, payroll, uid, db):
    hr, admin = payroll["hr"], payroll["admin"]
    run, acct = await prep(client, payroll, uid)
    first = await exported(client, payroll, uid, run)  # EMP001 and MGR001 exported, not confirmed yet
    await revise_and_publish(
        client, payroll, run, "EMP001", adjustments=[{"code": "BONUS", "label": "Late bonus", "amount": "1000"}]
    )
    delta = await new_batch(client, hr, run["id"], user_ids=[uid["employee"]])  # the exported line counts as paid
    assert delta["lines"][0]["amount"] == "1000.00"
    s = await batch_act(client, hr, delta, "submit")
    a = await batch_act(client, admin, s, "approve")
    await batch_act(client, hr, a, "file")
    emp = next(ln for ln in first["lines"] if ln["user"]["employee_id"] == "EMP001")
    done = await batch_act(
        client, admin, first, "confirm", paid_on=PAID_ON, lines=[{"line_id": emp["id"], "status": "failed"}], all_paid=True
    )
    assert done["counts"]["failed"] == 1
    dash = (await client.get(f"{ADMIN}/dashboard", params={"period": "2025-06"}, headers=hr)).json()
    assert "PAYMENT_UNDERPAID" in dash["calendar"]["alerts"] and "PAYMENT_FAILED_LINES" in dash["calendar"]["alerts"]
    r = await client.post(f"{RUNS}/{run['id']}/payment-batches", json={"value_date": "2026-10-31"}, headers=hr)
    assert r.status_code == 422 and err(r) == "NOTHING_TO_PAY"
    assert {d["field"]: d["message"] for d in r.json()["error"]["details"]}["EMP001"] == "UNDERPAID_CHAIN"


async def test_m3_no_underpaid_alert_in_the_normal_cases(client, payroll, uid):
    run, acct = await prep(client, payroll, uid)
    cur = await exported(client, payroll, uid, run)
    await revise_and_publish(
        client, payroll, run, "EMP001", adjustments=[{"code": "BONUS", "label": "Late bonus", "amount": "1000"}]
    )
    await batch_act(client, payroll["admin"], cur, "confirm", paid_on=PAID_ON, all_paid=True)
    delta = await new_batch(client, payroll["hr"], run["id"])
    a = await batch_act(client, payroll["admin"], await batch_act(client, payroll["hr"], delta, "submit"), "approve")
    await batch_act(client, payroll["hr"], a, "file")
    dash = (await client.get(f"{ADMIN}/dashboard", params={"period": "2025-06"}, headers=payroll["hr"])).json()
    assert "PAYMENT_UNDERPAID" not in dash["calendar"]["alerts"] and "PAYMENT_FAILED_LINES" not in dash["calendar"]["alerts"]


# ================================================================================================ M2
async def test_m2_arrears_proposer_and_trigger_structure_creator_are_makers(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    adm2, adm2_h = await extra_user(client, db, seeded, "ADM002")
    adm3, adm3_h = await extra_user(client, db, seeded, "ADM003")
    # ADM002's structure reaches the paid month: it proposes the arrears (event `propose` by ADM002) and is the trigger structure
    await add_structure(client, adm2_h, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    items = (await client.get(f"{ADMIN}/arrears", headers=hr)).json()["items"]
    assert len(items) == 1
    a = items[0]
    r = await client.post(
        f"{ADMIN}/arrears/{a['id']}/accept",
        json={"period": "2025-07", "expected_version": a["version"], "current_password": PASSWORD}, headers=hr,
    )  # fmt: skip
    assert r.status_code == 200, r.text
    # a later structure by ADM003 is the one the July payslip uses (it does not reach a paid month: no new proposal)
    await add_structure(client, adm3_h, uid["employee"], basic="25000", hra="5000", eff="2025-07-01")
    await enable_mc(client, admin)
    run2 = await new_run(client, hr, "2025-07")
    assert (await client.post(f"{RUNS}/{run2['id']}/process", headers=hr)).status_code == 200
    check2 = (await client.get(f"{RUNS}/{run2['id']}/publish-check", headers=adm2_h)).json()
    assert set(check2["me"]["reasons"]) == {"CREATED_COMPONENT", "CREATED_STRUCTURE"}  # proposer + trigger structure creator
    check3 = (await client.get(f"{RUNS}/{run2['id']}/publish-check", headers=adm3_h)).json()
    assert set(check3["me"]["reasons"]) == {"CREATED_STRUCTURE"}  # the structure of the payslip itself
    req = await request_run(client, hr, run2["id"])
    r = await client.post(
        f"/api/admin/payroll/publish-requests/{req['id']}/approve",
        json={"expected_version": req["version"], "current_password": PASSWORD}, headers=adm2_h,
    )  # fmt: skip
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    assert req["status"] == "submitted"


async def test_m2_expire_overdue_locks_the_run_before_the_request_rows(client, payroll, uid, db):
    hr = payroll["hr"]
    await enable_mc(client, payroll["admin"])
    run = await new_run(client, hr)
    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    req = await request_run(client, hr, run["id"])
    row = await db.get(PayrollPublishRequest, uuid.UUID(req["id"]))
    row.expires_at = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    row.requested_at = dt.datetime(2019, 12, 1, tzinfo=dt.UTC)
    await db.commit()
    locked: list[str] = []

    @event.listens_for(db.sync_session, "do_orm_execute")
    def _watch(state):
        stmt = state.statement
        if getattr(stmt, "_for_update_arg", None) is not None:
            locked.append(stmt.get_final_froms()[0].name)

    assert await prs.expire_overdue(db) == 1
    assert locked[:2] == [PayrollRun.__tablename__, PayrollPublishRequest.__tablename__]  # run first, request rows last


# ================================================================================================ L3
async def test_l3_reactivating_and_default_changes_need_step_up_and_alert_admins(client, payroll, db):
    hr = payroll["hr"]
    a = await new_account(client, hr)
    b = await new_account(client, hr, label="Second", account="111122223333", is_default=False)
    notifications.OUTBOX.clear()
    alerts = lambda: [o["payload"] for o in notifications.OUTBOX if o["payload"]["type"] == "admin_alert"]  # noqa: E731
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"label": "Backup"}, headers=hr)
    assert r.status_code == 200 and alerts() == []  # a label needs no step-up
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"active": False}, headers=hr)
    assert r.status_code == 200 and alerts() == []  # deactivating needs none either
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"active": True}, headers=hr)
    assert r.status_code == 422  # reactivating: the password is missing
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"active": True, "current_password": "Wrong!Passw0rd#1"}, headers=hr)
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    listed = {i["id"]: i for i in (await client.get(ACCOUNTS, headers=hr)).json()["items"]}
    assert listed[b["id"]]["active"] is False  # the refused attempts changed nothing
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"active": True, "current_password": PASSWORD}, headers=hr)
    assert r.status_code == 200 and r.json()["active"] is True
    assert len(alerts()) == 1
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"is_default": True}, headers=hr)
    assert r.status_code == 422  # a default change needs the password
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"is_default": True, "current_password": PASSWORD}, headers=hr)
    assert r.status_code == 200 and r.json()["is_default"] is True and len(alerts()) == 2
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"is_default": True}, headers=hr)
    assert r.status_code == 200 and len(alerts()) == 2  # no change: no step-up, no alert
    assert all("Backup" not in str(x) and "Second" not in str(x) and a["label"] not in str(x) for x in alerts())  # fixed text
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"current_password": PASSWORD}, headers=hr)
    assert r.status_code == 422  # the password alone is not an update
    audit = [
        x.after for x in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.bank_account.update"))).scalars()
    ]
    assert all("current_password" not in str(x) for x in audit)


# ================================================================================================ L4
async def test_l4_approve_and_the_first_download_recheck_the_value_date_and_the_debit_account(
    client, payroll, uid, db, monkeypatch
):
    hr, admin = payroll["hr"], payroll["admin"]
    run, acct = await prep(client, payroll, uid)
    b = await new_batch(client, hr, run["id"])
    s = await batch_act(client, hr, b, "submit")

    now = dt.date(2026, 10, 21)
    later_or_now = [dt.date(2026, 11, 5)]  # after the value date 2026-10-31

    async def clock(session):
        return later_or_now[0]

    monkeypatch.setattr("app.services.payroll_runs._today", clock)
    body = await batch_act(client, admin, s, "approve", 422)
    assert body["error"]["code"] == "INVALID_DATE"
    later_or_now[0] = now
    from app.models import CompanyBankAccount

    row = await db.get(CompanyBankAccount, uuid.UUID(acct["id"]))
    row.active = False  # deactivation is refused while a batch uses the account: this is the belt and braces check
    await db.commit()
    body = await batch_act(client, admin, s, "approve", 422)
    assert body["error"]["code"] == "VALIDATION_ERROR" and body["error"]["details"][0]["field"] == "debit_account_id"
    row.active = True
    await db.commit()
    a = await batch_act(client, admin, s, "approve")
    # approved, then the date passes before anybody downloaded the file
    later_or_now[0] = dt.date(2026, 11, 5)
    r = await batch_act(client, hr, a, "file", 422)
    assert r.json()["error"]["code"] == "INVALID_DATE"
    later_or_now[0] = now
    row.active = False
    await db.commit()
    r = await batch_act(client, hr, a, "file", 422)
    assert r.json()["error"]["details"][0]["field"] == "debit_account_id"
    row.active = True
    await db.commit()
    assert (await batch_act(client, hr, a, "file")).status_code == 200
    assert (await client.get(f"{BATCHES}/{b['id']}", headers=hr)).json()["status"] == "exported"
    # once exported the file can be fetched again whatever the date (L7) as long as the batch is not closed
    later_or_now[0] = dt.date(2026, 11, 5)
    assert (await batch_act(client, hr, a, "file")).status_code == 200
