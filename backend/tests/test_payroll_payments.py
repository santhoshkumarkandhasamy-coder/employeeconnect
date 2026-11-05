"""Plan 17 work package 5a: company bank accounts, payment batches, the bank file and confirmation (contract 19.4 / 19.5).

"Today" is 2026-10-21 (office tz Asia/Kolkata); the payroll month is 2025-06. HR001 is the maker, ADM001 the checker. EMP001 (basic 20000,
HRA 5000, net 21400.00) and MGR001 (net 36400.00) are paid. The database CHECKs and triggers are PostgreSQL-only (migration 0026 tests).
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.models import (
    AuditLog,
    CompanyBankAccount,
    EmployeePayrollDetails,
    PayrollItemEvent,
    PayrollPaymentBatch,
    PayrollPaymentLine,
    User,
)
from app.services import notifications
from tests.conftest import PASSWORD
from tests.payroll_controls_helpers import (
    ACCOUNTS,
    ADMIN,
    BATCHES,
    DEBIT,
    OTHER_BANK,
    RUNS,
    approved_batch,
    batch_act,
    err,
    extra_user,
    new_account,
    new_batch,
    published_run,
    quiet,  # noqa: F401  (fixture)
    set_bank,
)
from tests.payroll_fixtures import new_run, process_and_publish, row_for, set_setting

pytestmark = pytest.mark.usefixtures("quiet")

HEADER = "Sr No,Payment Type,Debit Account No,Beneficiary Name,Beneficiary Account No,IFSC,Amount,Value Date,Narration,Reference"


async def setup(client, payroll, uid, **kw):
    run = await published_run(client, payroll, uid, **kw)
    acct = await new_account(client, payroll["hr"])
    return run, acct


# ================================================================================================ company bank accounts
async def test_bank_account_create_is_masked_stepped_up_and_alerts_admins(client, payroll, db):
    hr = payroll["hr"]
    body = {**DEBIT, "is_default": True}
    r = await client.post(ACCOUNTS, json=body, headers=hr)
    assert r.status_code == 422  # step-up password missing
    r = await client.post(ACCOUNTS, json={**body, "current_password": "Wrong!Passw0rd#1"}, headers=hr)
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    for bad in ({"account": "12345"}, {"ifsc": "HDFC1234567"}, {"label": ""}, {"bank_name": ""}, {"extra": 1}):
        r = await client.post(ACCOUNTS, json={**body, **bad, "current_password": PASSWORD}, headers=hr)
        assert r.status_code == 422, bad
    notifications.OUTBOX.clear()
    a = await new_account(client, hr)
    assert a["account"] == "XXXXXXXX1012" and a["ifsc"] == "ICIC0000123" and a["is_default"] is True and a["active"] is True
    assert a["created_by"]["employee_id"] == "HR001" and set(a) == {
        "id",
        "label",
        "bank_name",
        "account",
        "ifsc",
        "is_default",
        "active",
        "created_at",
        "created_by",
    }
    alerts = [o for o in notifications.OUTBOX if o["payload"]["type"] == "admin_alert"]
    assert (
        len(alerts) == 1 and alerts[0]["payload"]["body"] == "HDFC salary account" and "987654321012" not in json.dumps(alerts[0])
    )
    listing = (await client.get(ACCOUNTS, headers=payroll["admin"])).json()["items"]
    assert [i["account"] for i in listing] == ["XXXXXXXX1012"]
    row = (await db.execute(select(CompanyBankAccount))).scalar_one()
    assert row.account == "987654321012"  # stored (encrypted), never returned
    audit_text = json.dumps(
        [a.after for a in (await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.bank_account.%")))).scalars()]
    )
    assert "987654321012" not in audit_text and "ICIC0000123" not in audit_text and "HDFC salary" not in audit_text


async def test_bank_account_update_rules(client, payroll, db):
    hr = payroll["hr"]
    a = await new_account(client, hr)
    b = await new_account(client, hr, label="Second", account="111122223333", is_default=True)
    ids = {i["label"]: i for i in (await client.get(ACCOUNTS, headers=hr)).json()["items"]}
    assert ids["HDFC salary account"]["is_default"] is False and ids["Second"]["is_default"] is True  # one default
    for bad in ({"account": "123456789"}, {"ifsc": "ICIC0000123"}, {"bank_name": "x"}, {}, {"label": None}):
        assert (await client.put(f"{ACCOUNTS}/{a['id']}", json=bad, headers=hr)).status_code == 422, bad
    r = await client.put(f"{ACCOUNTS}/{a['id']}", json={"label": "Main account", "is_default": True}, headers=hr)
    assert r.status_code == 200 and r.json()["label"] == "Main account" and r.json()["is_default"] is True
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"active": False}, headers=hr)
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["is_default"] is False
    r = await client.put(f"{ACCOUNTS}/{b['id']}", json={"is_default": True}, headers=hr)
    assert r.status_code == 422  # an inactive account is never the default
    assert (await client.put(f"{ACCOUNTS}/{uuid.uuid4()}", json={"label": "x"}, headers=hr)).status_code == 404
    acts = [a_.action for a_ in (await db.execute(select(AuditLog))).scalars() if a_.action.startswith("payroll.bank_account")]
    assert acts.count("payroll.bank_account.update") == 2 and acts.count("payroll.bank_account.create") == 2


async def test_bank_account_in_use_cannot_be_deactivated(client, payroll, uid):
    run, acct = await setup(client, payroll, uid)
    b = await new_batch(client, payroll["hr"], run["id"])
    r = await client.put(f"{ACCOUNTS}/{acct['id']}", json={"active": False}, headers=payroll["hr"])
    assert r.status_code == 409 and err(r) == "IN_USE"
    await batch_act(client, payroll["hr"], b, "cancel")  # a cancelled batch frees the account
    assert (await client.put(f"{ACCOUNTS}/{acct['id']}", json={"active": False}, headers=payroll["hr"])).status_code == 200


async def test_bank_account_permissions(client, payroll):
    for method, url, body in (("GET", ACCOUNTS, None), ("POST", ACCOUNTS, {**DEBIT, "current_password": PASSWORD}),
                              ("PUT", f"{ACCOUNTS}/{uuid.uuid4()}", {"label": "x"})):  # fmt: skip
        for who in ("employee", "manager"):
            assert (await client.request(method, url, json=body, headers=payroll[who])).status_code == 403
        assert (await client.request(method, url, json=body)).status_code == 401


# ================================================================================================ details -> bank_changed marker
async def test_bank_detail_changes_set_the_marker_other_changes_do_not(client, payroll, db, uid):
    hr = payroll["hr"]
    emp = uuid.UUID(uid["employee"])
    det = lambda: db.get(EmployeePayrollDetails, emp)  # noqa: E731
    r = await client.put(
        f"{ADMIN}/employees/{emp}/details", json={"designation": "Analyst", "current_password": PASSWORD}, headers=hr
    )
    assert r.status_code == 200 and r.json()["bank_changed_at"] is None
    d = await det()
    assert d.bank_changed_by is None and d.bank_changed_at is None
    await set_bank(client, hr, emp)
    db.expire_all()
    d = await det()
    assert d.bank_changed_by == uuid.UUID(uid["hr"]) and d.bank_changed_at is not None
    first = d.bank_changed_at
    r = await client.put(
        f"{ADMIN}/employees/{emp}/details", json={"pan": "ABCDE1234G", "current_password": PASSWORD}, headers=payroll["admin"]
    )
    assert r.status_code == 200
    db.expire_all()
    assert (await det()).bank_changed_by == uuid.UUID(uid["hr"])  # a PAN change is not a bank change
    r = await client.put(
        f"{ADMIN}/employees/{emp}/details",
        json={"bank_ifsc": "ICIC0000456", "current_password": PASSWORD},
        headers=payroll["admin"],
    )
    assert r.status_code == 200 and r.json()["bank_changed_at"]
    db.expire_all()
    d = await det()
    assert d.bank_changed_by == uuid.UUID(uid["admin"]) and d.bank_changed_at >= first
    r = await client.put(
        f"{ADMIN}/employees/{emp}/details", json={"payment_mode": "cash", "current_password": PASSWORD}, headers=hr
    )
    db.expire_all()
    assert (await det()).bank_changed_by == uuid.UUID(uid["hr"])  # the payment mode counts too
    r = await client.put(
        f"{ADMIN}/employees/{emp}/details", json={"bank_ifsc": "ICIC0000456", "current_password": PASSWORD}, headers=hr
    )
    db.expire_all()
    assert (await det()).bank_changed_by == uuid.UUID(uid["hr"])  # unchanged value: the marker did not move to anybody else


# ================================================================================================ batch creation
async def test_create_batch_shape_lines_and_masking(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    b = await new_batch(client, payroll["hr"], run["id"], debit_account_id=acct["id"])
    assert (
        b["reference"] == "PAY-2025-06-01" and b["status"] == "draft" and b["format"] == "generic_neft_csv" and b["version"] == 1
    )
    assert b["run"] == {"id": run["id"], "period": "2025-06"} and b["value_date"] == "2026-10-31"
    assert b["debit_account"] == {"id": acct["id"], "label": "HDFC salary account", "account": "XXXXXXXX1012"}
    assert b["counts"] == {"lines": 2, "excluded": 0, "exported": 0, "paid": 0, "failed": 0} and b["total"] == "57800.00"
    assert [ln["seq"] for ln in b["lines"]] == [1, 2] and [ln["user"]["employee_id"] for ln in b["lines"]] == ["EMP001", "MGR001"]
    ln = b["lines"][0]
    assert ln["amount"] == "21400.00" and ln["beneficiary_name"] == "Test Employee" and ln["account"] == "XXXXXXXX9012"
    assert ln["ifsc"] == "HDFC0001234" and ln["account_source"] == "snapshot" and ln["current"] is None and ln["flags"] == []
    assert ln["status"] == "pending" and ln["reference"] is None and ln["paid_on"] is None and ln["payslip"]["revision"] == 1
    assert (
        b["blockers"] == []
        and b["file_sha256"] is None
        and b["created_by"]["employee_id"] == "HR001"
        and b["submitted_by"] is None
    )
    assert "123456789012" not in json.dumps(b)
    row = (await db.execute(select(PayrollPaymentBatch))).scalar_one()
    assert row.contributors == [uid["hr"]] and row.last_edited_by == uuid.UUID(uid["hr"]) and row.lines_mac is None
    page = (await client.get(BATCHES, params={"run_id": run["id"]}, headers=payroll["admin"])).json()
    assert page["total"] == 1 and "lines" not in page["items"][0] and "blockers" not in page["items"][0]
    assert (await client.get(BATCHES, params={"status": "approved"}, headers=payroll["admin"])).json()["total"] == 0
    assert (await client.get(BATCHES, params={"status": "bogus"}, headers=payroll["admin"])).status_code == 422
    two = await new_batch(client, payroll["hr"], run["id"], expect=422)  # everything is already in a live line
    assert err_of(two) == "NOTHING_TO_PAY"


def err_of(body: dict) -> str:
    return body["error"]["code"]


async def test_scope_value_date_default_account_and_errors(client, payroll, uid, monkeypatch):
    run = await published_run(client, payroll, uid)
    hr = payroll["hr"]
    r = await client.post(f"{RUNS}/{run['id']}/payment-batches", json={"value_date": "2026-10-31"}, headers=hr)
    assert r.status_code == 422  # no default company bank account yet
    acct = await new_account(client, hr, is_default=False)
    r = await client.post(f"{RUNS}/{run['id']}/payment-batches", json={"value_date": "2026-10-31"}, headers=hr)
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "debit_account_id"
    for day, code in (("2026-10-20", "INVALID_DATE"), ("2026-11-21", "INVALID_DATE")):
        r = await client.post(
            f"{RUNS}/{run['id']}/payment-batches", json={"value_date": day, "debit_account_id": acct["id"]}, headers=hr
        )
        assert r.status_code == 422 and err(r) == code, day
    for day in ("2026-10-21", "2026-11-20"):  # both ends of today..today+30 are fine
        r = await client.post(
            f"{RUNS}/{run['id']}/payment-batches",
            json={"value_date": day, "debit_account_id": acct["id"], "user_ids": [uid["employee"]]},
            headers=hr,
        )
        assert r.status_code == 201, (day, r.text)
        await client.post(f"{BATCHES}/{r.json()['id']}/cancel", json={"reason": "just a test"}, headers=hr)
    r = await client.post(
        f"{RUNS}/{run['id']}/payment-batches",
        json={"value_date": "2026-10-31", "debit_account_id": acct["id"], "user_ids": [uid["admin"]]},
        headers=hr,
    )
    assert r.status_code == 422  # no published payslip for that employee
    r = await client.post(
        f"{RUNS}/{run['id']}/payment-batches",
        json={"value_date": "2026-10-31", "debit_account_id": str(uuid.uuid4())},
        headers=hr,
    )
    assert r.status_code == 422
    r = await client.post(
        f"{RUNS}/{run['id']}/payment-batches",
        json={"value_date": "2026-10-31", "debit_account_id": acct["id"], "x": 1},
        headers=hr,
    )
    assert r.status_code == 422
    from app.services import payroll_payments as pp

    monkeypatch.setattr(pp, "MAX_LINES", 1)
    r = await client.post(
        f"{RUNS}/{run['id']}/payment-batches", json={"value_date": "2026-10-31", "debit_account_id": acct["id"]}, headers=hr
    )
    assert r.status_code == 422 and err(r) == "TOO_MANY_ROWS"
    assert (
        await client.post(f"{RUNS}/{uuid.uuid4()}/payment-batches", json={"value_date": "2026-10-31"}, headers=hr)
    ).status_code == 404


async def test_batches_need_a_published_run(client, payroll, uid):
    acct = await new_account(client, payroll["hr"])
    await set_setting(client, payroll["hr"], bank_change_alert_days=0)
    run = await new_run(client, payroll["hr"])
    r = await client.post(
        f"{RUNS}/{run['id']}/payment-batches",
        json={"value_date": "2026-10-31", "debit_account_id": acct["id"]},
        headers=payroll["hr"],
    )
    assert r.status_code == 409 and err(r) == "PAYROLL_LOCKED"


async def test_blockers_are_listed_not_paid(client, payroll, uid, db):
    hr = payroll["hr"]
    await set_setting(client, hr, bank_change_alert_days=0)
    await set_bank(client, hr, uid["employee"], payment_mode="cash")
    await set_bank(client, hr, uid["manager"])
    mgr = await db.get(User, uuid.UUID(uid["manager"]))
    mgr.full_name = "=+-@"  # nothing bank-safe is left of this name
    await db.commit()
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    acct = await new_account(client, hr)
    r = await client.post(
        f"{RUNS}/{run['id']}/payment-batches", json={"value_date": "2026-10-31", "debit_account_id": acct["id"]}, headers=hr
    )
    assert r.status_code == 422 and err(r) == "NOTHING_TO_PAY"
    assert {d["field"]: d["message"] for d in r.json()["error"]["details"]} == {"EMP001": "NOT_BANK_TRANSFER", "MGR001": "NAME"}


async def test_blockers_missing_bank_details_and_invalid_formats(client, payroll, uid, db):
    hr = payroll["hr"]
    await set_setting(client, hr, bank_change_alert_days=0)
    await set_bank(client, hr, uid["employee"])
    await set_bank(client, hr, uid["manager"])
    emp = await db.get(EmployeePayrollDetails, uuid.UUID(uid["employee"]))
    emp.bank_account = "123"  # too short: the details API would refuse it, old data may not
    await db.commit()
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    acct = await new_account(client, hr)
    b = await new_batch(client, hr, run["id"], debit_account_id=acct["id"])
    assert [ln["user"]["employee_id"] for ln in b["lines"]] == ["MGR001"]
    assert [(x["user"]["employee_id"], x["reason"]) for x in b["blockers"]] == [("EMP001", "INVALID_ACCOUNT")]
    got = (await client.get(f"{BATCHES}/{b['id']}", headers=hr)).json()
    assert [x["reason"] for x in got["blockers"]] == ["INVALID_ACCOUNT"]  # a draft batch lists the live blockers


async def test_missing_bank_details_blocker(client, payroll, uid):
    hr = payroll["hr"]
    run = await published_run(client, payroll, uid, banks=("manager",))
    acct = await new_account(client, hr)
    b = await new_batch(client, hr, run["id"], debit_account_id=acct["id"])
    assert [(x["user"]["employee_id"], x["reason"]) for x in b["blockers"]] == [("EMP001", "NO_BANK_DETAILS")]


# ================================================================================================ lifecycle + file
async def test_end_to_end_file_confirm_and_second_batch(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    hr, admin = payroll["hr"], payroll["admin"]
    b = await new_batch(client, hr, run["id"])
    s = await batch_act(client, hr, b, "submit")
    assert s["status"] == "submitted" and s["submitted_by"]["employee_id"] == "HR001" and s["version"] == 2
    assert (await db.execute(select(PayrollPaymentBatch.lines_mac))).scalar_one() is not None
    r = await client.post(f"{BATCHES}/{b['id']}/file", json={"current_password": PASSWORD}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_APPROVED"
    a = await batch_act(client, admin, s, "approve")
    assert a["status"] == "approved" and a["decided_by"]["employee_id"] == "ADM001" and a["total"] == "57800.00"
    notifications.OUTBOX.clear()
    r = await batch_act(client, hr, a, "file")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "us-ascii" in r.headers["content-type"]
    assert r.headers["content-disposition"] == 'attachment; filename="bank-PAY-2025-06-01.csv"'
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    expected = (
        HEADER + "\r\n"
        "1,NEFT,987654321012,TEST EMPLOYEE,123456789012,HDFC0001234,21400.00,31/10/2026,SALARY JUN 2025,PAY-2025-06-01-0001\r\n"
        "2,NEFT,987654321012,TEST MANAGER,123456789012,HDFC0001234,36400.00,31/10/2026,SALARY JUN 2025,PAY-2025-06-01-0002\r\n"
    ).encode()  # fmt: skip
    assert r.content == expected
    import hashlib

    got = (await client.get(f"{BATCHES}/{b['id']}", headers=hr)).json()
    assert (
        got["status"] == "exported"
        and got["file_sha256"] == hashlib.sha256(expected).hexdigest()
        and got["exported_by"]["employee_id"] == "HR001"
    )
    assert [ln["status"] for ln in got["lines"]] == ["exported", "exported"] and got["counts"]["exported"] == 2
    again = await batch_act(client, admin, got, "file")  # any manage holder, byte-identical
    assert again.content == expected
    alerts = [o["payload"] for o in notifications.OUTBOX if o["payload"]["type"] == "admin_alert"]
    assert [a_["body"] for a_ in alerts] == ["PAY-2025-06-01: 2 lines", "PAY-2025-06-01: 2 lines"] and alerts[0][
        "title"
    ] == "Bank file downloaded"
    # confirm: all paid with a shared reference
    c = await batch_act(client, hr, got, "confirm", paid_on="2026-10-21", all_paid=True, reference="UTR 9988/77")
    assert c["status"] == "closed" and c["counts"]["paid"] == 2 and c["lines"][0]["reference"] == "XXXXXXX8/77"
    assert [ln["paid_on"] for ln in c["lines"]] == ["2026-10-21", "2026-10-21"]
    assert (await client.get(f"{BATCHES}/{b['id']}", headers=hr)).json()["status"] == "closed"
    third = await batch_act(client, admin, c, "file")  # closed batches can still be re-downloaded
    assert third.content == expected
    await new_batch(client, hr, run["id"], expect=422)  # ALREADY_PAID for both
    audit = list((await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.payment_batch.%")))).scalars())
    assert [a_.action.split(".")[-1] for a_ in audit if a_.action.split(".")[-1] not in ("view", "list")] == [
        "create",
        "submit",
        "approve",
        "export",
        "download",
        "confirm",
        "download",
    ]
    exp = next(a_ for a_ in audit if a_.action.endswith("export"))
    assert (
        exp.after["file_sha256"] == got["file_sha256"] and exp.after["reference"] == "PAY-2025-06-01" and exp.after["lines"] == 2
    )
    ev = [e.action for e in (await db.execute(select(PayrollItemEvent))).scalars() if e.item_type == "payment_batch"]
    assert ev == ["create", "submit", "approve", "export", "confirm"]


async def test_nothing_sensitive_in_audit_events_pushes_or_errors(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    notifications.OUTBOX.clear()
    b = await approved_batch(client, payroll, uid, run)
    await batch_act(client, payroll["hr"], b, "file")
    r = await client.post(f"{BATCHES}/{b['id']}/file", json={"current_password": "Wrong!Passw0rd#1"}, headers=payroll["hr"])
    assert r.status_code == 403
    secrets = (
        "123456789012",
        "987654321012",
        "HDFC0001234",
        "ICIC0000123",
        "Test Employee",
        "21400",
        "36400",
        "57800",
        "TEST EMPLOYEE",
    )
    blob = json.dumps([[a.before, a.after] for a in (await db.execute(select(AuditLog))).scalars()], default=str)
    blob += json.dumps([o["payload"] for o in notifications.OUTBOX], default=str)
    for e in (await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.item_type == "payment_batch"))).scalars():
        blob += json.dumps([e.detail, e.amount], default=str)
    for s in secrets:
        assert s not in blob, s
    assert any(o["payload"].get("kind") == "payment_batch" for o in notifications.OUTBOX)


async def test_csv_cleans_names_and_refuses_unsafe_fields():
    from app.services import payroll_payment_file as pf

    assert pf.bank_safe_name("  José  O'Brien-Smith Jr. ") == "JOSE OBRIENSMITH JR."
    assert pf.bank_safe_name("=cmd|' /C calc'!A0") == "CMD C CALCA0"
    for evil in ("=1+1", "+SUM(A1)", "-2", "@x", "\t=1", '"quoted",comma', "line\nbreak", "...", "கார்த்திக்"):
        n = pf.bank_safe_name(evil)
        assert n == "" or (n[0] not in "=+-@" and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789. " for c in n)), evil
    assert pf.bank_safe_name("A" * 60) == "A" * 35 and pf.bank_safe_name("") == "" and pf.bank_safe_name(None) == ""
    import datetime as dt

    line = pf.FileLine(1, "Asha K", "123456789012", "HDFC0001234", "100.00", True)
    data = pf.render(reference="PAY-2026-10-01", payment_type="NEFT", debit_account="987654321012", value_date=dt.date(2026, 10, 31),
                     period=dt.date(2026, 10, 1), lines=[line])  # fmt: skip
    assert data.decode().splitlines()[1].endswith("31/10/2026,SALARY REV OCT 2026,PAY-2026-10-01-0001") and data.endswith(b"\r\n")
    assert (
        pf.render(
            reference="PAY-2026-10-01",
            payment_type="NEFT",
            debit_account="987654321012",
            value_date=dt.date(2026, 10, 31),
            period=dt.date(2026, 10, 1),
            lines=[line],
        )
        == data
    )  # deterministic
    for bad in (pf.FileLine(1, "=", "123456789012", "HDFC0001234", "1.00", False), pf.FileLine(1, "A", "12", "HDFC0001234", "1.00", False),
                pf.FileLine(1, "A", "123456789012", "bad", "1.00", False), pf.FileLine(1, "A", "123456789012", "HDFC0001234", "1", False),
                pf.FileLine(1, "A", "123456789012", "HDFC0001234", "1.00,5", False)):  # fmt: skip
        with pytest.raises(ValueError):
            pf.render(reference="PAY-2026-10-01", payment_type="NEFT", debit_account="987654321012", value_date=dt.date(2026, 10, 31),
                      period=dt.date(2026, 10, 1), lines=[bad])  # fmt: skip
    with pytest.raises(ValueError):
        pf.render(reference="PAY-2026-10-01", payment_type="NEFT,X", debit_account="987654321012", value_date=dt.date(2026, 10, 31),
                  period=dt.date(2026, 10, 1), lines=[line])  # fmt: skip


async def test_confirm_rules_partial_and_failed_lines_return_to_the_pool(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    hr = payroll["hr"]
    b = await approved_batch(client, payroll, uid, run)
    exported = (await batch_act(client, hr, b, "file")).content
    assert exported
    cur = (await client.get(f"{BATCHES}/{b['id']}", headers=hr)).json()
    l1, l2 = cur["lines"]
    base = {"current_password": PASSWORD}
    for bad in (
        {"paid_on": "2026-10-22", "all_paid": True},
        {"paid_on": "2026-10-20", "all_paid": True},
    ):  # future / before the download
        r = await client.post(f"{BATCHES}/{b['id']}/confirm", json={**base, **bad}, headers=hr)
        assert r.status_code == 422 and err(r) == "INVALID_DATE", bad
    for bad in ({"paid_on": "2026-10-21"}, {"paid_on": "2026-10-21", "all_paid": False, "lines": []}, {"paid_on": "2026-10-21", "all_paid": True, "reference": "bad,ref"},
                {"paid_on": "2026-10-21", "lines": [{"line_id": l1["id"], "status": "maybe"}]}):  # fmt: skip
        assert (await client.post(f"{BATCHES}/{b['id']}/confirm", json={**base, **bad}, headers=hr)).status_code == 422, bad
    r = await client.post(
        f"{BATCHES}/{b['id']}/confirm",
        json={**base, "paid_on": "2026-10-21", "lines": [{"line_id": str(uuid.uuid4()), "status": "paid"}]},
        headers=hr,
    )
    assert r.status_code == 422
    r = await client.post(f"{BATCHES}/{b['id']}/confirm", json={"paid_on": "2026-10-21", "all_paid": True}, headers=hr)
    assert r.status_code == 422  # step-up
    part = await batch_act(
        client, hr, cur, "confirm", paid_on="2026-10-21", lines=[{"line_id": l1["id"], "status": "paid", "reference": "UTR100"}]
    )
    assert (
        part["status"] == "exported"
        and [ln["status"] for ln in part["lines"]] == ["paid", "exported"]
        and part["lines"][0]["reference"] == "XXR100"
    )
    r = await client.post(
        f"{BATCHES}/{b['id']}/confirm",
        json={**base, "paid_on": "2026-10-21", "lines": [{"line_id": l1["id"], "status": "failed"}]},
        headers=hr,
    )
    assert r.status_code == 409  # already final
    done = await batch_act(
        client,
        hr,
        part,
        "confirm",
        paid_on="2026-10-21",
        lines=[{"line_id": l2["id"], "status": "failed", "reference": "RET55"}],
        all_paid=False,
    )
    assert done["status"] == "closed" and done["counts"] == {"lines": 2, "excluded": 0, "exported": 0, "paid": 1, "failed": 1}
    assert done["lines"][1]["status"] == "failed" and done["lines"][1]["paid_on"] is None
    r = await client.post(f"{BATCHES}/{b['id']}/confirm", json={**base, "paid_on": "2026-10-21", "all_paid": True}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_APPROVED"  # closed
    again = await new_batch(client, hr, run["id"])  # the failed line frees its payslip
    assert [ln["user"]["employee_id"] for ln in again["lines"]] == ["MGR001"] and again["lines"][0]["amount"] == "36400.00"
    assert [(x["user"]["employee_id"], x["reason"]) for x in again["blockers"]] == [("EMP001", "ALREADY_PAID")]
    assert again["reference"] == "PAY-2025-06-02"


# ================================================================================================ maker-checker
async def test_maker_checker_rules_and_step_up_first(client, payroll, uid, db, seeded):
    run, acct = await setup(client, payroll, uid)
    hr, admin = payroll["hr"], payroll["admin"]
    b = await new_batch(client, hr, run["id"])
    s = await batch_act(client, hr, b, "submit")
    r = await client.post(f"{BATCHES}/{b['id']}/approve", json={"expected_version": 2}, headers=admin)
    assert r.status_code == 422  # step-up password is required
    r = await client.post(
        f"{BATCHES}/{b['id']}/approve", json={"expected_version": 2, "current_password": "Wrong!Passw0rd#1"}, headers=hr
    )
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"  # before the maker check
    await batch_act(client, hr, s, "approve", 403)  # a party: SECOND_PERSON_REQUIRED
    r = await client.post(f"{BATCHES}/{b['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=hr)
    assert err(r) == "SECOND_PERSON_REQUIRED"
    r = await client.post(f"{BATCHES}/{b['id']}/reject", json={"expected_version": 2, "comment": "no"}, headers=admin)
    assert r.status_code == 422  # 3-200 characters
    await batch_act(client, admin, s, "approve", 409, expected_version=9)  # VERSION_CONFLICT
    a = await batch_act(client, admin, s, "approve")
    assert a["status"] == "approved"
    await batch_act(client, admin, s, "approve", 409)  # not submitted any more
    r = await client.post(f"{BATCHES}/{b['id']}/withdraw", json={"expected_version": 3}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_SUBMITTED"
    r = await client.put(f"{BATCHES}/{b['id']}", json={"expected_version": 3, "value_date": "2026-10-30"}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_DRAFT"
    ev = [e.action for e in (await db.execute(select(PayrollItemEvent))).scalars() if e.item_type == "payment_batch"]
    assert ev == ["create", "submit", "approve"]


async def test_a_line_that_pays_the_checker_blocks_the_whole_batch(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    me, admin3 = await extra_user(client, db, seeded, "ADM003", on_payroll=True)
    r = await client.post(f"{ADMIN}/employees/{me.id}/structures", json={
        "effective_from": "2025-01-01", "basic": "20000", "hra": "0", "allowances": [], "fixed_deductions": [], "tds_monthly": "0",
        "pf_applicable": True, "pf_on_full_basic": False, "esi_applicable": True, "pt_applicable": True}, headers=hr)  # fmt: skip
    assert r.status_code == 201, r.text
    await set_bank(client, hr, str(me.id))
    run, acct = await setup(client, payroll, uid)
    b = await new_batch(client, hr, run["id"])
    assert len(b["lines"]) == 3
    s = await batch_act(client, hr, b, "submit")
    body = await batch_act(client, admin3, s, "approve", 403)
    assert err_of(body) == "FORBIDDEN"
    denied = [a for a in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.denied"))).scalars()]
    assert denied and denied[-1].after["reason"] == "self"
    assert (await batch_act(client, admin, s, "approve"))["status"] == "approved"


async def test_checker_who_changed_a_line_employees_bank_recently_is_refused(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    run, acct = await setup(client, payroll, uid, alert_days=30)
    await set_bank(client, admin, uid["employee"], **OTHER_BANK)  # ADM001 changes EMP001's account after the publish
    b = await new_batch(client, hr, run["id"])
    ln = b["lines"][0]
    assert set(ln["flags"]) >= {"BANK_DETAILS_CHANGED", "RECENT_BANK_CHANGE"} and ln["current"] == {
        "account": "XXXXXXXX7777",
        "ifsc": "ICIC0000456",
    }
    assert ln["account"] == "XXXXXXXX9012" and ln["account_source"] == "snapshot"
    s = await batch_act(client, hr, b, "submit")
    body = await batch_act(client, admin, s, "approve", 403)
    assert err_of(body) == "SECOND_PERSON_REQUIRED"
    _, admin2 = await extra_user(client, db, seeded, "ADM002")
    assert (await batch_act(client, admin2, s, "approve"))["status"] == "approved"


async def test_reject_returns_to_draft_and_the_decider_cannot_become_a_maker(client, payroll, uid, db, seeded):
    run, acct = await setup(client, payroll, uid)
    hr, admin = payroll["hr"], payroll["admin"]
    b = await new_batch(client, hr, run["id"])
    s = await batch_act(client, hr, b, "submit")
    await batch_act(client, hr, s, "reject", 403)  # a party cannot reject
    rj = await batch_act(client, admin, s, "reject", comment="Totals look wrong")
    assert (
        rj["status"] == "draft" and rj["decided_by"]["employee_id"] == "ADM001" and rj["decision_comment"] == "Totals look wrong"
    )
    row = (await db.execute(select(PayrollPaymentBatch))).scalar_one()
    assert row.lines_mac is None and row.status == "draft"
    for body, path, method in (
        ({"expected_version": 3, "value_date": "2026-10-30"}, "", "put"),
        ({"expected_version": 3}, "/submit", "post"),
    ):
        r = await getattr(client, method)(f"{BATCHES}/{b['id']}{path}", json=body, headers=admin)
        assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED", path  # the decider never becomes a maker
    again = await batch_act(client, hr, rj, "submit")
    assert again["status"] == "submitted" and again["decided_by"] is None and again["decision_comment"] is None  # a new round
    assert (await batch_act(client, admin, again, "approve"))["status"] == "approved"
    notes = [e.action for e in (await db.execute(select(PayrollItemEvent))).scalars() if e.item_type == "payment_batch"]
    assert notes == ["create", "submit", "reject", "submit", "approve"]


async def test_withdraw_makes_the_withdrawer_a_contributor(client, payroll, uid, db, seeded):
    run, acct = await setup(client, payroll, uid)
    hr, admin = payroll["hr"], payroll["admin"]
    b = await new_batch(client, hr, run["id"])
    s = await batch_act(client, hr, b, "submit")
    other, other_h = await extra_user(client, db, seeded, "ADM002")
    w = await batch_act(client, other_h, s, "withdraw")
    assert w["status"] == "draft"
    row = (await db.execute(select(PayrollPaymentBatch))).scalar_one()
    assert str(other.id) in row.contributors and row.lines_mac is None
    s2 = await batch_act(client, hr, w, "submit")
    await batch_act(client, other_h, s2, "approve", 403)  # a contributor can never approve
    assert (await batch_act(client, admin, s2, "approve"))["status"] == "approved"


# ================================================================================================ staleness (BATCH_STALE)
async def test_bank_details_changed_after_submit_is_batch_stale_and_refresh_replaces_the_line(client, payroll, uid, db, seeded):
    run, acct = await setup(client, payroll, uid)
    hr, admin = payroll["hr"], payroll["admin"]
    b = await new_batch(client, hr, run["id"])
    s = await batch_act(client, hr, b, "submit")
    await set_bank(client, hr, uid["employee"], **OTHER_BANK)
    body = await batch_act(client, admin, s, "approve", 409)
    assert err_of(body) == "BATCH_STALE" and body["error"]["details"] == [
        {"field": "lines[1]", "message": "BANK_DETAILS_CHANGED"}
    ]
    assert (await client.get(f"{BATCHES}/{b['id']}", headers=hr)).json()["status"] == "submitted"  # nothing was approved
    r = await client.put(f"{BATCHES}/{b['id']}", json={"expected_version": 2}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_DRAFT"
    w = await batch_act(client, hr, s, "withdraw")
    stale_submit = await client.post(f"{BATCHES}/{b['id']}/submit", json={"expected_version": w["version"]}, headers=hr)
    assert stale_submit.status_code == 409 and err(stale_submit) == "BATCH_STALE"  # a stale draft cannot be submitted either
    r = await client.put(f"{BATCHES}/{b['id']}", json={"expected_version": w["version"]}, headers=hr)  # an empty edit refreshes
    assert r.status_code == 200, r.text
    ref = r.json()
    lines = ref["lines"]
    assert [(ln["seq"], ln["status"]) for ln in lines] == [(1, "excluded"), (2, "pending"), (3, "pending")]
    new = lines[2]
    assert new["user"]["employee_id"] == "EMP001" and new["account"] == "XXXXXXXX9012" and "BANK_DETAILS_CHANGED" in new["flags"]
    assert new["current"]["account"] == "XXXXXXXX7777" and ref["counts"]["lines"] == 2 and ref["counts"]["excluded"] == 1
    s2 = await batch_act(client, hr, ref, "submit")
    assert (await batch_act(client, admin, s2, "approve"))["status"] == "approved"


async def test_a_revised_payslip_makes_the_batch_stale(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    hr, admin = payroll["hr"], payroll["admin"]
    b = await new_batch(client, hr, run["id"])
    s = await batch_act(client, hr, b, "submit")
    row = await row_for(client, hr, run["id"], "EMP001")
    rev = await client.post(f"{RUNS}/{run['id']}/employees/{row['user']['id']}/revise", json={"reason": "fix"}, headers=hr)
    assert (await client.post(f"{ADMIN}/payslips/{rev.json()['id']}/process", headers=hr)).status_code == 200
    assert (
        await client.post(f"{ADMIN}/payslips/{rev.json()['id']}/publish", json={"current_password": PASSWORD}, headers=admin)
    ).status_code == 200
    body = await batch_act(client, admin, s, "approve", 409)
    assert err_of(body) == "BATCH_STALE" and body["error"]["details"] == [{"field": "lines[1]", "message": "PAYSLIP_SUPERSEDED"}]


async def test_a_tampered_line_amount_is_caught_by_the_mac(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    b = await new_batch(client, payroll["hr"], run["id"])
    s = await batch_act(client, payroll["hr"], b, "submit")
    line = (await db.execute(select(PayrollPaymentLine).where(PayrollPaymentLine.seq == 1))).scalar_one()
    ben = json.loads(json.dumps(line.beneficiary))
    ben["name"] = "Somebody Else"  # the account holder was swapped behind the maker's back
    line.beneficiary = ben
    await db.commit()
    body = await batch_act(client, payroll["admin"], s, "approve", 409)
    assert err_of(body) == "BATCH_STALE"


async def test_stale_draft_line_is_refreshed_by_a_new_line(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    hr = payroll["hr"]
    b = await new_batch(client, hr, run["id"])
    await set_bank(client, hr, uid["manager"], **OTHER_BANK)
    r = await client.put(
        f"{BATCHES}/{b['id']}", json={"expected_version": 1, "exclude_line_ids": [b["lines"][0]["id"]]}, headers=hr
    )
    assert r.status_code == 200
    got = r.json()
    assert [(ln["seq"], ln["status"]) for ln in got["lines"]] == [(1, "excluded"), (2, "excluded"), (3, "pending")]
    assert got["version"] == 2 and got["counts"]["lines"] == 1
    inc = await client.put(
        f"{BATCHES}/{b['id']}", json={"expected_version": 2, "include_line_ids": [got["lines"][0]["id"]]}, headers=hr
    )
    assert inc.status_code == 200 and [ln["status"] for ln in inc.json()["lines"]] == [
        "pending",
        "excluded",
        "pending",
    ]  # the unchanged line is back
    bad = await client.put(
        f"{BATCHES}/{b['id']}", json={"expected_version": 3, "include_line_ids": [str(uuid.uuid4())]}, headers=hr
    )
    assert bad.status_code == 422
    assert (
        await client.put(f"{BATCHES}/{b['id']}", json={"expected_version": 1, "value_date": "2026-10-30"}, headers=hr)
    ).status_code == 409


async def test_account_source_choice_changes_the_file(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    hr, admin = payroll["hr"], payroll["admin"]
    await set_bank(client, hr, uid["employee"], **OTHER_BANK)
    b = await new_batch(client, hr, run["id"])
    l1, l2 = b["lines"]
    for line_id, src in ((l2["id"], "current"), (str(uuid.uuid4()), "current")):  # only a flagged line has a current account
        r = await client.put(
            f"{BATCHES}/{b['id']}",
            json={"expected_version": 1, "account_sources": [{"line_id": line_id, "source": src}]},
            headers=hr,
        )
        assert r.status_code == 422, (line_id, r.text)
    r = await client.put(
        f"{BATCHES}/{b['id']}",
        json={"expected_version": 1, "account_sources": [{"line_id": l1["id"], "source": "current"}]},
        headers=hr,
    )
    assert r.status_code == 200 and r.json()["lines"][0]["account_source"] == "current"
    s = await batch_act(client, hr, r.json(), "submit")
    a = await batch_act(client, admin, s, "approve")
    csv = (await batch_act(client, hr, a, "file")).content.decode()
    assert "TEST EMPLOYEE,555566667777,ICIC0000456,21400.00" in csv and "TEST MANAGER,123456789012,HDFC0001234" in csv
    assert "123456789012,HDFC0001234,21400.00" not in csv


async def test_current_details_unusable_cannot_be_selected(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    hr = payroll["hr"]
    emp = await db.get(EmployeePayrollDetails, uuid.UUID(uid["employee"]))
    emp.bank_account = "12345"  # a malformed current account (old data)
    await db.commit()
    b = await new_batch(client, hr, run["id"])
    assert "BANK_DETAILS_CHANGED" in b["lines"][0]["flags"]
    r = await client.put(
        f"{BATCHES}/{b['id']}",
        json={"expected_version": 1, "account_sources": [{"line_id": b["lines"][0]["id"], "source": "current"}]},
        headers=hr,
    )
    assert r.status_code == 422


# ================================================================================================ revisions and cancellation
async def revise_and_publish(client, payroll, run, emp, **put):
    hr, admin = payroll["hr"], payroll["admin"]
    row = await row_for(client, hr, run["id"], emp)
    rev = await client.post(f"{RUNS}/{run['id']}/employees/{row['user']['id']}/revise", json={"reason": "fix"}, headers=hr)
    assert rev.status_code == 201, rev.text
    pid = rev.json()["id"]
    r = await client.put(f"{ADMIN}/payslips/{pid}", json={"expected_version": rev.json()["version"], **put}, headers=hr)
    assert r.status_code == 200, r.text
    assert (await client.post(f"{ADMIN}/payslips/{pid}/process", headers=hr)).status_code == 200
    assert (
        await client.post(f"{ADMIN}/payslips/{pid}/publish", json={"current_password": PASSWORD}, headers=admin)
    ).status_code == 200
    return pid


async def test_revision_pays_only_the_difference_and_a_decrease_is_a_recovery(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    hr = payroll["hr"]
    b = await approved_batch(client, payroll, uid, run)
    await batch_act(client, hr, b, "file")
    cur = (await client.get(f"{BATCHES}/{b['id']}", headers=hr)).json()
    await batch_act(client, hr, cur, "confirm", paid_on="2026-10-21", all_paid=True)
    await revise_and_publish(
        client, payroll, run, "EMP001", adjustments=[{"code": "BONUS", "label": "Late bonus", "amount": "1000"}]
    )
    delta = await new_batch(client, hr, run["id"])
    assert [ln["user"]["employee_id"] for ln in delta["lines"]] == ["EMP001"] and delta["lines"][0]["amount"] == "1000.00"
    assert "REVISION_DELTA" in delta["lines"][0]["flags"] and delta["lines"][0]["payslip"]["revision"] == 2
    assert [(x["user"]["employee_id"], x["reason"]) for x in delta["blockers"]] == [("MGR001", "ALREADY_PAID")]
    s = await batch_act(client, hr, delta, "submit")
    a = await batch_act(client, payroll["admin"], s, "approve")
    csv = (await batch_act(client, hr, a, "file")).content.decode()
    assert "1000.00,31/10/2026,SALARY REV JUN 2025,PAY-2025-06-02-0001" in csv and csv.count("\r\n") == 2
    # a second revision that pays LESS than what was already paid is a recovery, not a payment
    cur2 = (await client.get(f"{BATCHES}/{a['id']}", headers=hr)).json()
    await batch_act(client, hr, cur2, "confirm", paid_on="2026-10-21", all_paid=True)
    await revise_and_publish(
        client, payroll, run, "EMP001", adjustments=[{"code": "OTHER_DEDUCTION", "label": "Advance", "amount": "5000"}]
    )
    r = await client.post(f"{RUNS}/{run['id']}/payment-batches", json={"value_date": "2026-10-31"}, headers=hr)
    assert r.status_code == 422
    assert {d["field"]: d["message"] for d in r.json()["error"]["details"]}["EMP001"] == "RECOVERY"


async def test_pending_line_of_an_earlier_copy_blocks_the_revision(client, payroll, uid):
    run, acct = await setup(client, payroll, uid)
    hr = payroll["hr"]
    b = await new_batch(client, hr, run["id"], user_ids=[uid["employee"]])
    await revise_and_publish(
        client, payroll, run, "EMP001", adjustments=[{"code": "BONUS", "label": "Late bonus", "amount": "1000"}]
    )
    nxt = await new_batch(client, hr, run["id"], user_ids=[uid["manager"]])
    assert [ln["user"]["employee_id"] for ln in nxt["lines"]] == ["MGR001"]
    r = await client.post(
        f"{RUNS}/{run['id']}/payment-batches", json={"value_date": "2026-10-31", "user_ids": [uid["employee"]]}, headers=hr
    )
    assert r.status_code == 422 and r.json()["error"]["details"] == [{"field": "EMP001", "message": "IN_OTHER_BATCH"}]
    refreshed = await client.put(
        f"{BATCHES}/{b['id']}", json={"expected_version": 1}, headers=hr
    )  # the stale first line is replaced
    assert [(ln["seq"], ln["status"], ln["payslip"]["revision"]) for ln in refreshed.json()["lines"]] == [
        (1, "excluded", 1),
        (2, "pending", 2),
    ]


async def test_cancel_frees_the_slot_and_excludes_every_line(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    hr, admin = payroll["hr"], payroll["admin"]
    b = await approved_batch(client, payroll, uid, run)
    r = await client.post(f"{BATCHES}/{b['id']}/cancel", json={"reason": "no"}, headers=hr)
    assert r.status_code == 422
    c = await batch_act(client, hr, b, "cancel")
    assert c["status"] == "cancelled" and {ln["status"] for ln in c["lines"]} == {"excluded"} and c["counts"]["lines"] == 0
    await batch_act(client, hr, c, "cancel", 409)
    r = await client.post(f"{BATCHES}/{b['id']}/file", json={"current_password": PASSWORD}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_APPROVED"
    fresh = await new_batch(client, hr, run["id"])
    assert fresh["reference"] == "PAY-2025-06-02" and len(fresh["lines"]) == 2
    # a checker who cancels does not become a contributor
    s = await batch_act(client, hr, fresh, "submit")
    ap = await batch_act(client, admin, s, "approve")
    cc = await batch_act(client, admin, ap, "cancel")
    assert cc["status"] == "cancelled"
    row = (await db.execute(select(PayrollPaymentBatch).where(PayrollPaymentBatch.reference == "PAY-2025-06-02"))).scalar_one()
    assert str(row.decided_by) not in row.contributors


async def test_exported_batch_cannot_be_cancelled(client, payroll, uid):
    run, acct = await setup(client, payroll, uid)
    b = await approved_batch(client, payroll, uid, run)
    await batch_act(client, payroll["hr"], b, "file")
    cur = (await client.get(f"{BATCHES}/{b['id']}", headers=payroll["hr"])).json()
    await batch_act(client, payroll["hr"], cur, "cancel", 409)


async def test_tampered_stored_lines_cannot_be_exported_or_redownloaded(client, payroll, uid, db):
    run, acct = await setup(client, payroll, uid)
    b = await approved_batch(client, payroll, uid, run)
    line = (await db.execute(select(PayrollPaymentLine).where(PayrollPaymentLine.seq == 2))).scalar_one()
    line.amount = "99999.00"
    await db.commit()
    r = await batch_act(client, payroll["hr"], b, "file", 409)
    assert err(r) == "BATCH_STALE"
    line.amount = "36400.00"
    await db.commit()
    await batch_act(client, payroll["hr"], b, "file")
    line = (await db.execute(select(PayrollPaymentLine).where(PayrollPaymentLine.seq == 2))).scalar_one()
    line.amount = "1.00"
    await db.commit()
    r = await batch_act(client, payroll["hr"], b, "file", 409)  # the stored lines no longer reproduce the exported file
    assert err(r) == "CONFLICT"


# ================================================================================================ access
async def test_payment_endpoints_need_their_permission(client, payroll, uid):
    run, acct = await setup(client, payroll, uid)
    b = await new_batch(client, payroll["hr"], run["id"])
    bid = b["id"]
    cases = (
        ("POST", f"{RUNS}/{run['id']}/payment-batches", {"value_date": "2026-10-31"}),
        ("GET", BATCHES, None), ("GET", f"{BATCHES}/{bid}", None),
        ("PUT", f"{BATCHES}/{bid}", {"expected_version": 1}), ("POST", f"{BATCHES}/{bid}/submit", {"expected_version": 1}),
        ("POST", f"{BATCHES}/{bid}/withdraw", {"expected_version": 1}),
        ("POST", f"{BATCHES}/{bid}/approve", {"expected_version": 1, "current_password": PASSWORD}),
        ("POST", f"{BATCHES}/{bid}/reject", {"expected_version": 1, "comment": "No good"}),
        ("POST", f"{BATCHES}/{bid}/cancel", {"reason": "No good"}),
        ("POST", f"{BATCHES}/{bid}/file", {"current_password": PASSWORD}),
        ("POST", f"{BATCHES}/{bid}/confirm", {"paid_on": "2026-10-21", "all_paid": True, "current_password": PASSWORD}),
    )  # fmt: skip
    for method, url, body in cases:
        for who in ("employee", "manager"):
            assert (await client.request(method, url, json=body, headers=payroll[who])).status_code == 403, (method, url)
        assert (await client.request(method, url, json=body)).status_code == 401, (method, url)
    for method, url, body in cases[2:]:
        r = await client.request(method, url.replace(bid, str(uuid.uuid4())), json=body, headers=payroll["admin"])
        assert r.status_code == 404, (method, url, r.status_code)


async def test_payment_writes_fail_closed_without_redis(client, payroll, uid, monkeypatch):
    from app.core.redis import get_redis

    monkeypatch.undo()

    class Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    client._transport.app.dependency_overrides[get_redis] = lambda: Broken()
    bid = uuid.uuid4()
    for method, url, body in (
        ("POST", f"{RUNS}/{uuid.uuid4()}/payment-batches", {"value_date": "2026-10-31"}),
        ("PUT", f"{BATCHES}/{bid}", {"expected_version": 1}),
        ("POST", f"{BATCHES}/{bid}/submit", {"expected_version": 1}),
        ("POST", f"{BATCHES}/{bid}/approve", {"expected_version": 1, "current_password": PASSWORD}),
        ("POST", f"{BATCHES}/{bid}/file", {"current_password": PASSWORD}),
        ("POST", f"{BATCHES}/{bid}/confirm", {"paid_on": "2026-10-21", "all_paid": True, "current_password": PASSWORD}),
        ("POST", ACCOUNTS, {**DEBIT, "current_password": PASSWORD}),
    ):
        r = await client.request(method, url, json=body, headers=payroll["hr"])
        assert r.status_code == 503 and err(r) == "TEMPORARILY_UNAVAILABLE", url


async def test_file_download_is_rate_limited_per_day(client, payroll, uid, monkeypatch):
    from app.core import rate_limit

    monkeypatch.undo()
    seen = []

    async def spy(redis, rules, **kw):
        seen.append((rules, kw))

    monkeypatch.setattr(rate_limit, "enforce", spy)
    r = await client.post(f"{BATCHES}/{uuid.uuid4()}/file", json={"current_password": PASSWORD}, headers=payroll["hr"])
    assert r.status_code == 404
    rules, kw = seen[0]
    assert [(n, w) for _, n, w in rules] == [(10, 60), (30, 86400)] and "bank_file" in rules[0][0] and kw == {"fail_closed": True}


async def test_dashboard_alerts_for_payments_and_publish(client, payroll, uid):
    run, acct = await setup(client, payroll, uid)
    hr = payroll["hr"]
    dash = (await client.get(f"{ADMIN}/dashboard", params={"period": "2025-06"}, headers=hr)).json()
    assert "PAYMENT_NOT_EXPORTED" in dash["calendar"]["alerts"] and "PAYMENT_FAILED_LINES" not in dash["calendar"]["alerts"]
    b = await approved_batch(client, payroll, uid, run)
    await batch_act(client, hr, b, "file")
    cur = (await client.get(f"{BATCHES}/{b['id']}", headers=hr)).json()
    dash = (await client.get(f"{ADMIN}/dashboard", params={"period": "2025-06"}, headers=hr)).json()
    assert "PAYMENT_NOT_EXPORTED" not in dash["calendar"]["alerts"]
    await batch_act(
        client,
        hr,
        cur,
        "confirm",
        paid_on="2026-10-21",
        lines=[{"line_id": cur["lines"][0]["id"], "status": "failed"}],
        all_paid=True,
    )
    dash = (await client.get(f"{ADMIN}/dashboard", params={"period": "2025-06"}, headers=hr)).json()
    assert "PAYMENT_FAILED_LINES" in dash["calendar"]["alerts"]


# ================================================================================================ key rotation covers the new columns
def test_key_rotation_discovers_every_new_encrypted_column():
    from app.services import key_rotation

    found = {(t.name, c.name) for t, c in key_rotation.encrypted_columns()}
    for pair in (
        ("payroll_publish_requests", "snapshot_enc"), ("payroll_publish_requests", "comment_enc"),
        ("payroll_publish_requests", "decision_comment_enc"), ("company_bank_accounts", "account_enc"),
        ("company_bank_accounts", "ifsc_enc"), ("company_bank_accounts", "bank_name_enc"), ("payroll_payment_batches", "decision_comment_enc"),
        ("payroll_payment_batches", "cancel_reason_enc"), ("payroll_payment_lines", "amount_enc"),
        ("payroll_payment_lines", "beneficiary_enc"), ("payroll_payment_lines", "reference_enc"),
    ):  # fmt: skip
        assert pair in found, pair


async def test_rotation_keeps_the_batch_valid(client, payroll, uid, db):
    """The MACs cover plaintext values, so re-encrypting the columns never invalidates an approved batch."""
    from app.services import key_rotation

    run, acct = await setup(client, payroll, uid)
    b = await approved_batch(client, payroll, uid, run)
    stats = await key_rotation.rotate_all(db, dry_run=True)
    assert stats["undecryptable"] == 0
    data = await batch_act(client, payroll["hr"], b, "file")
    assert data.status_code == 200


async def test_batch_step_up_is_checked_before_the_state(client, payroll, uid):
    run, _ = await setup(client, payroll, uid)
    b = await new_batch(client, payroll["hr"], run["id"])  # still a draft
    for action, body in (
        ("approve", {"expected_version": 1}),
        ("file", {}),
        ("confirm", {"paid_on": "2026-10-21", "all_paid": True}),
    ):
        r = await client.post(
            f"{BATCHES}/{b['id']}/{action}", json={**body, "current_password": "Wrong!Passw0rd#1"}, headers=payroll["admin"]
        )
        assert r.status_code == 403 and err(r) == "REAUTH_FAILED", action
        r = await client.post(f"{BATCHES}/{b['id']}/{action}", json=body, headers=payroll["admin"])
        assert r.status_code == 422, action  # a missing password is a validation error, not a state error


def test_database_refusals_map_to_clean_errors_never_500():
    from sqlalchemy.exc import IntegrityError

    from app.services import payroll_payments as pp

    class Orig(Exception):
        def __init__(self, state: str):
            super().__init__("db error")
            self.sqlstate = state

    def exc(state: str, msg: str) -> IntegrityError:
        err_ = Orig(state)
        err_.args = (msg,)
        return IntegrityError("stmt", {}, err_)

    cases = (
        ("23514", 'violates check constraint "ck_payroll_payment_batches_checker_history"', "SECOND_PERSON_REQUIRED"),
        ("23000", "payment batch x: the approver recently changed the bank details of a paid employee", "SECOND_PERSON_REQUIRED"),
        ("23000", "payment batch x: the approver is paid by a line of the batch", "FORBIDDEN"),
        ("23505", 'unique constraint "uq_payroll_payment_lines_live"', "CONFLICT"),
        ("23000", "payment line x is exported and its batch is exported: locked", "CONFLICT"),
    )
    for state, msg, code in cases:
        assert pp._map_db(exc(state, msg)).code == code, msg
