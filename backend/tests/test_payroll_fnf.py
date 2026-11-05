"""Plan 16 work package 4b: full and final settlement (maker-checker, orchestration, settlement on publish, employee view).

"Today" is 2026-10-21 (office tz Asia/Kolkata). The employee EMP001 (basic 20000, HRA 5000) leaves on 2025-06-20 after resigning on
2025-05-25 (served 27 of 30 notice days, shortfall 3); the settlement month is the past month 2025-06 (PERIOD).
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal as D

import pytest
from sqlalchemy import select, update

from app.core import security
from app.models import (
    AuditLog,
    EmployeePayrollDetails,
    EmployeeProfile,
    FnfSettlement,
    LeaveBalance,
    LeaveEncashment,
    LeaveType,
    PayrollItemEvent,
    PayrollLoan,
    PayrollLoanInstallment,
    PayrollOneTimeItem,
    User,
)
from app.services import notifications
from tests.conftest import PASSWORD, bearer
from tests.payroll_fixtures import PERIOD, new_run, process_and_publish, publish_run, row_for

FNF = "/api/admin/payroll/fnf"
EMP = "/api/admin/payroll/employees"
RUNS = "/api/admin/payroll/runs"
TODAY = dt.datetime(2026, 10, 21, 4, 30, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from app.services import admin_security
    from app.services import payroll_common as pc

    async def no_limit(*a, **k):
        return None

    async def today(session):
        return TODAY.date()

    monkeypatch.setattr(pc, "limit", no_limit)
    monkeypatch.setattr(admin_security, "REAUTH_PER_MINUTE", 10_000)
    monkeypatch.setattr(admin_security, "REAUTH_PER_DAY", 10_000)
    monkeypatch.setattr("app.services.leave.utcnow", lambda: TODAY)
    monkeypatch.setattr("app.services.payroll_runs._today", today)


def err(r):
    return r.json()["error"]["code"]


def create_body(**kw):
    body = {
        "resignation_date": "2025-05-25", "last_working_day": "2025-06-20", "exit_reason": "resignation",
        "notice_period_days": 30, "settlement_period": PERIOD, "remarks": None,
    }  # fmt: skip
    body.update(kw)
    return body


async def mk(client, hdr, user_id, expect=201, **kw):
    r = await client.post(f"{EMP}/{user_id}/fnf", json=create_body(**kw), headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def put(client, hdr, f, expect=200, **kw):
    r = await client.put(f"{FNF}/{f['id']}", json={"expected_version": f["version"], **kw}, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def act(client, hdr, f, action, expect=200, **body):
    body.setdefault("expected_version", f["version"])
    if action in ("approve", "revoke"):
        body.setdefault("current_password", PASSWORD)
    if action == "reject":
        body.setdefault("comment", "Please check the notice")
    if action == "cancel":
        body = {"reason": "Employee stays"}
    r = await client.post(f"{FNF}/{f['id']}/{action}", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def get(client, hdr, fid):
    r = await client.get(f"{FNF}/{fid}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


async def submitted(client, payroll, uid, expect_put=None, **put_kw):
    f = await mk(client, payroll["hr"], uid["employee"])
    if put_kw:
        f = await put(client, payroll["hr"], f, **put_kw)
    return await act(client, payroll["hr"], f, "submit")


async def items_of(db, user_id):
    return list((await db.execute(select(PayrollOneTimeItem).where(PayrollOneTimeItem.user_id == uuid.UUID(user_id)))).scalars())


@pytest.fixture
async def types(db):
    el = LeaveType(
        code="EL",
        name="Earned Leave",
        annual_quota=12,
        is_paid=True,
        encashable=True,
        encash_max_days_per_year=6,
        encash_min_balance=2,
    )
    db.add(el)
    await db.commit()
    return {"EL": str(el.id)}


# ================================================================================================ pure arithmetic
def test_notice_arithmetic():
    from app.services import payroll_fnf_calc as fc

    n = fc.notice_for("resignation", dt.date(2025, 6, 10), dt.date(2025, 6, 20), 30, waived=False, override_days=None)
    assert (n.served, n.computed, n.shortfall) == (11, 19, 19)
    assert (
        fc.notice_for("resignation", dt.date(2025, 6, 10), dt.date(2025, 6, 20), 30, waived=True, override_days=None).shortfall
        == 0
    )
    assert (
        fc.notice_for("resignation", dt.date(2025, 6, 10), dt.date(2025, 6, 20), 30, waived=False, override_days=5).shortfall == 5
    )
    assert (
        fc.notice_for("termination", dt.date(2025, 6, 10), dt.date(2025, 6, 20), 30, waived=False, override_days=None).shortfall
        == 0
    )
    assert fc.notice_for("resignation", None, dt.date(2025, 6, 20), 30, waived=False, override_days=None).shortfall == 0
    assert (
        fc.notice_for("resignation", dt.date(2025, 5, 1), dt.date(2025, 6, 20), 30, waived=False, override_days=None).shortfall
        == 0
    )
    assert fc.daily_rate(D("25000"), 30) == D("833.33")
    assert fc.notice_amount(19, D("833.33")) == D("15833.27")
    lo, hi = fc.settlement_window(dt.date(2025, 11, 20))
    assert (lo, hi) == (dt.date(2025, 11, 1), dt.date(2026, 2, 1))


def test_notice_recovery_is_reserved_system_code():
    from app.services import payroll_calc as calc

    assert "NOTICE_RECOVERY" in calc.RESERVED_CODES and "NOTICE_RECOVERY" in calc.SYSTEM_ONLY_CODES
    assert "NOTICE_RECOVERY" in calc.DEDUCTION_ADJUSTMENTS


def test_settlement_only_days_and_window(seeded):
    from app.services import payroll_days as pd

    u = User(employee_id="X", full_name="X", join_date=dt.date(2024, 1, 1))
    tz = dt.UTC
    w = pd.employment_window(u, dt.date(2025, 6, 1), dt.date(2025, 6, 30), tz, dt.date(2025, 6, 20))
    assert w == (dt.date(2025, 6, 1), dt.date(2025, 6, 20))
    assert pd.employment_window(u, dt.date(2025, 7, 1), dt.date(2025, 7, 31), tz, dt.date(2025, 6, 20)) is None


# ================================================================================================ create / edit
async def test_create_sets_lwd_and_notice(client, payroll, uid, db):
    f = await mk(client, payroll["hr"], uid["employee"])
    assert f["status"] == "draft" and f["version"] == 1 and f["settlement_period"] == PERIOD and f["run_status"] == "none"
    n = f["notice"]
    assert (n["required_days"], n["served_days"], n["shortfall_days"], n["computed_shortfall_days"]) == (30, 27, 3, 3)
    assert (n["basis"], n["divisor"], n["daily_rate"], n["amount"]) == ("gross", 30, "833.33", "2499.99")
    det = (
        await db.execute(select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id == uuid.UUID(uid["employee"])))
    ).scalar_one()
    assert det.last_working_day == dt.date(2025, 6, 20)
    d = await client.get(f"{EMP}/{uid['employee']}/details", headers=payroll["hr"])
    assert d.json()["last_working_day"] == "2025-06-20" and d.json()["notice_period_days"] is None
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.fnf.create"))).scalars().all()
    assert (
        len(rows) == 1
        and "amount" not in str(rows[0].after)
        and rows[0].after["counts"] == {"items": 0, "encashments": 0, "loans": 0}
    )


async def test_create_rules(client, payroll, uid):
    h = payroll["hr"]
    await mk(client, payroll["admin"], uid["admin"], expect=403)  # own record
    await mk(client, h, uid["employee"], expect=422, last_working_day="2027-06-20", settlement_period="2027-06")  # > today + 180
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json=create_body(resignation_date="2025-06-25"), headers=h)
    assert r.status_code == 422 and err(r) == "INVALID_DATE"
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json=create_body(settlement_period="2025-05"), headers=h)
    assert err(r) == "INVALID_PERIOD"
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json=create_body(settlement_period="2025-10"), headers=h)
    assert err(r) == "INVALID_PERIOD"  # more than three months after the last working day
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json=create_body(unknown=1), headers=h)
    assert r.status_code == 422
    await mk(client, h, uid["employee"])
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json=create_body(), headers=h)
    assert r.status_code == 409 and err(r) == "FNF_EXISTS"
    r = await client.post(f"{EMP}/{uid['employee']}/fnf", json=create_body(), headers=payroll["manager"])
    assert r.status_code == 403  # payroll.manage required
    r = await client.post(f"{EMP}/{uuid.uuid4()}/fnf", json=create_body(), headers=h)
    assert r.status_code == 404


async def test_notice_default_from_details_and_setting(client, payroll, uid):
    r = await client.put(f"{EMP}/{uid['employee']}/details", json={"notice_period_days": 45}, headers=payroll["hr"])
    assert r.status_code == 200 and r.json()["notice_period_days"] == 45  # no step-up when it is the only key
    f = await mk(client, payroll["hr"], uid["employee"], notice_period_days=None)
    assert f["notice"]["required_days"] == 45
    r = await client.put(f"{EMP}/{uid['employee']}/details", json={"last_working_day": "2025-01-01"}, headers=payroll["hr"])
    assert r.status_code == 422  # read-only


async def test_edit_replaces_lists_and_checks_version(client, payroll, uid, types):
    h = payroll["hr"]
    f = await mk(client, h, uid["employee"])
    f2 = await put(client, h, f, items=[{"code": "OTHER_EARNING", "label": "Pro-rata bonus", "amount": "3000.00"}], remarks="ok")
    assert f2["version"] == 2 and f2["items"][0]["label"] == "Pro-rata bonus" and f2["remarks"] == "ok"
    r = await client.put(f"{FNF}/{f['id']}", json={"expected_version": 1, "remarks": "x"}, headers=h)
    assert r.status_code == 409 and err(r) == "VERSION_CONFLICT"
    f3 = await put(client, h, f2, notice={"waived": True, "override": None})
    assert f3["notice"]["shortfall_days"] == 0 and f3["notice"]["waived"] is True and f3["notice"]["amount"] == "0.00"
    f4 = await put(client, h, f3, notice={"waived": False, "override": {"days": 5, "reason": "Handover completed early"}})
    assert f4["notice"]["shortfall_days"] == 5 and f4["notice"]["computed_shortfall_days"] == 3
    r = await client.put(
        f"{FNF}/{f4['id']}",
        json={
            "expected_version": f4["version"],
            "notice": {"waived": False, "override": {"days": 31, "reason": "Too many days"}},
        },
        headers=h,
    )
    assert r.status_code == 422
    r = await client.put(
        f"{FNF}/{f4['id']}",
        json={"expected_version": f4["version"], "items": [{"code": "NOTICE_RECOVERY", "label": "x", "amount": "1.00"}]},
        headers=h,
    )
    assert r.status_code == 422  # system-only code
    r = await client.put(f"{FNF}/{f4['id']}", json={"expected_version": f4["version"]}, headers=h)
    assert r.status_code == 422  # nothing to change
    r = await client.put(
        f"{FNF}/{f4['id']}", json={"expected_version": f4["version"], "last_working_day": "2025-06-25"}, headers=payroll["admin"]
    )
    assert r.status_code == 200  # payroll.manage holder; the LWD and details follow
    ev = (await client.get(f"{EMP}/{uid['employee']}/details", headers=h)).json()
    assert ev["last_working_day"] == "2025-06-25"


async def test_encashment_line_validation(client, payroll, uid, types):
    h = payroll["hr"]
    f = await mk(client, h, uid["employee"])
    line = {"leave_type_id": types["EL"], "year": 2025, "days": 4.0}
    f = await put(client, h, f, encashments=[line])
    e = f["encashments"][0]
    assert e["leave_type"]["code"] == "EL" and e["days"] == 4.0 and e["available_days"] == 6.0
    assert e["daily_rate"] == "666.67" and e["amount"] == "2666.68"  # basic 20000 / encashment divisor 30
    r = await client.put(
        f"{FNF}/{f['id']}", json={"expected_version": f["version"], "encashments": [{**line, "days": 7.0}]}, headers=h
    )
    assert (
        r.status_code == 409 and err(r) == "INSUFFICIENT_BALANCE" and r.json()["error"]["details"][0]["field"] == "encashments[0]"
    )
    r = await client.put(
        f"{FNF}/{f['id']}", json={"expected_version": f["version"], "encashments": [{**line, "year": 2020}]}, headers=h
    )
    assert r.status_code == 422


# ================================================================================================ lifecycle and maker-checker
async def test_submit_withdraw_cancel(client, payroll, uid, db):
    h = payroll["hr"]
    f = await mk(client, h, uid["employee"])
    s = await act(client, h, f, "submit")
    assert s["status"] == "submitted" and s["submitted_by"]["employee_id"] == "HR001"
    r = await client.put(f"{FNF}/{f['id']}", json={"expected_version": s["version"], "remarks": "x"}, headers=h)
    assert r.status_code == 409 and err(r) == "NOT_DRAFT"
    r = await client.post(f"{FNF}/{f['id']}/submit", json={"expected_version": s["version"]}, headers=h)
    assert err(r) == "NOT_DRAFT"
    w = await act(client, h, s, "withdraw")
    assert w["status"] == "draft" and w["submitted_by"] is None
    r = await client.post(f"{FNF}/{f['id']}/withdraw", json={"expected_version": w["version"]}, headers=h)
    assert err(r) == "NOT_SUBMITTED"
    c = await act(client, h, w, "cancel")
    assert c["status"] == "cancelled"
    det = (
        await db.execute(select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id == uuid.UUID(uid["employee"])))
    ).scalar_one()
    assert det.last_working_day is None  # cancel clears it
    await mk(client, h, uid["employee"])  # a new one may follow


async def test_checker_rules(client, payroll, uid, db, seeded):
    h, a = payroll["hr"], payroll["admin"]
    f = await submitted(client, payroll, uid)
    # the maker (HR001, also holds payroll.approve) can neither approve nor reject
    r = await client.post(
        f"{FNF}/{f['id']}/approve", json={"expected_version": f["version"], "current_password": PASSWORD}, headers=h
    )
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    r = await client.post(f"{FNF}/{f['id']}/reject", json={"expected_version": f["version"], "comment": "No good"}, headers=h)
    assert err(r) == "SECOND_PERSON_REQUIRED"
    # a manager has no payroll.approve at all
    r = await client.post(
        f"{FNF}/{f['id']}/approve",
        json={"expected_version": f["version"], "current_password": PASSWORD},
        headers=payroll["manager"],
    )
    assert r.status_code == 403 and err(r) == "FORBIDDEN"
    # the employee: own record, audited
    r = await client.post(
        f"{FNF}/{f['id']}/approve",
        json={"expected_version": f["version"], "current_password": PASSWORD},
        headers=payroll["employee"],
    )
    assert r.status_code == 403
    denied = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.denied"))).scalars().all()
    assert denied == [] or all("amount" not in str(x.after) for x in denied)
    # a step-up failure comes before any state check
    r = await client.post(
        f"{FNF}/{f['id']}/approve", json={"expected_version": 99, "current_password": "wrong-password"}, headers=a
    )
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    r = await client.post(f"{FNF}/{f['id']}/approve", json={"expected_version": f["version"]}, headers=a)
    assert r.status_code == 422  # password required
    # bound to the version the checker reviewed
    r = await client.post(
        f"{FNF}/{f['id']}/approve", json={"expected_version": f["version"] + 1, "current_password": PASSWORD}, headers=a
    )
    assert r.status_code == 409 and err(r) == "VERSION_CONFLICT"
    ok = await act(client, a, f, "approve", comment="Checked")
    assert ok["status"] == "approved" and ok["decided_by"]["employee_id"] == "ADM001" and ok["summary"]["net_estimate"]
    r = await client.post(
        f"{FNF}/{f['id']}/approve", json={"expected_version": ok["version"], "current_password": PASSWORD}, headers=a
    )
    assert err(r) == "NOT_SUBMITTED"


async def test_every_contributor_is_excluded(client, payroll, uid, db, seeded):
    """A second HR person who edited the draft, or submitted and withdrew it, can never be its checker."""
    hr2 = await _make_user(db, seeded, "hr", "HR002")
    h2 = await _login(client, "HR002")
    h = payroll["hr"]
    f = await mk(client, h, uid["employee"])
    f = await put(client, h2, f, remarks="edited by HR002")
    s = await act(client, h, f, "submit")
    r = await client.post(
        f"{FNF}/{s['id']}/approve", json={"expected_version": s["version"], "current_password": PASSWORD}, headers=h2
    )
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"  # last editor
    w = await act(client, h, s, "withdraw")
    s2 = await act(client, h2, w, "submit")
    r = await client.post(
        f"{FNF}/{s2['id']}/approve", json={"expected_version": s2["version"], "current_password": PASSWORD}, headers=h
    )
    assert err(r) == "SECOND_PERSON_REQUIRED"  # creator
    w2 = await act(client, h2, s2, "withdraw")
    assert w2["status"] == "draft"
    s3 = await act(client, payroll["admin"], w2, "submit")  # admin submits; h2 stays in the history
    r = await client.post(
        f"{FNF}/{s3['id']}/approve", json={"expected_version": s3["version"], "current_password": PASSWORD}, headers=h2
    )
    assert err(r) == "SECOND_PERSON_REQUIRED"
    row = (await db.execute(select(FnfSettlement))).scalars().one()
    assert set(row.contributors) >= {str(seeded["users"]["hr"]), str(hr2.id), str(seeded["users"]["admin"])}


async def test_reject_returns_to_draft_and_binds_the_checker(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    f = await submitted(client, payroll, uid)
    r = await act(client, a, f, "reject", comment="Notice looks wrong")
    assert r["status"] == "draft" and r["decision_comment"] == "Notice looks wrong" and r["decided_by"]["employee_id"] == "ADM001"
    # the checker of the rejected round cannot become the maker of the next one
    r2 = await client.put(f"{FNF}/{f['id']}", json={"expected_version": r["version"], "remarks": "fix"}, headers=a)
    assert r2.status_code == 403 and err(r2) == "SECOND_PERSON_REQUIRED"
    f2 = await put(client, h, r, remarks="fixed")
    s = await act(client, h, f2, "submit")
    assert s["decided_by"] is None  # a new round
    ok = await act(client, payroll["admin"], s, "approve")
    assert ok["status"] == "approved"


async def test_rejecting_requires_a_comment(client, payroll, uid):
    f = await submitted(client, payroll, uid)
    r = await client.post(f"{FNF}/{f['id']}/reject", json={"expected_version": f["version"]}, headers=payroll["admin"])
    assert r.status_code == 422


async def test_loans_must_be_resolved_before_submit(client, payroll, uid):
    h = payroll["hr"]
    loan = await _mk_loan(client, h, uid["employee"])
    f = await mk(client, h, uid["employee"])
    r = await client.post(f"{FNF}/{f['id']}/submit", json={"expected_version": f["version"]}, headers=h)
    assert r.status_code == 422 and err(r) == "LOAN_UNRESOLVED"
    g = await get(client, h, f["id"])
    assert g["loans"][0]["mode"] is None and g["loans"][0]["outstanding"] == "30000.00"
    f = await put(client, h, f, loans=[{"loan_id": loan["id"], "mode": "payroll"}])
    await act(client, h, f, "submit")
    r = await client.put(
        f"{FNF}/{f['id']}", json={"expected_version": 1, "loans": [{"loan_id": str(uuid.uuid4()), "mode": "payroll"}]}, headers=h
    )
    assert r.status_code in (409, 422)


# ================================================================================================ approve orchestration
async def test_approve_writes_tagged_rows(client, payroll, uid, db, types):
    h, a = payroll["hr"], payroll["admin"]
    loan = await _mk_loan(client, h, uid["employee"], principal="3000.00", emi="1000.00")
    f = await mk(client, h, uid["employee"])
    f = await put(
        client, h, f,
        encashments=[{"leave_type_id": types["EL"], "year": 2025, "days": 4.0}],
        loans=[{"loan_id": loan["id"], "mode": "payroll"}],
        items=[{"code": "OTHER_EARNING", "label": "Pro-rata bonus", "amount": "3000.00"}],
    )  # fmt: skip
    f = await act(client, h, f, "submit")
    ok = await act(client, a, f, "approve")
    assert ok["status"] == "approved"
    its = await items_of(db, uid["employee"])
    notice = next(i for i in its if i.code == "NOTICE_RECOVERY")
    assert notice.fnf_id == uuid.UUID(f["id"]) and notice.amount == "2499.99" and notice.period.isoformat() == "2025-06-01"
    assert notice.label == "Notice period shortfall (3 d)" and notice.status == "pending"
    bonus = next(i for i in its if i.code == "OTHER_EARNING")
    assert bonus.fnf_id == notice.fnf_id and bonus.amount == "3000.00"
    enc = (await db.execute(select(LeaveEncashment))).scalars().one()
    assert enc.source == "fnf" and enc.fnf_id == notice.fnf_id and enc.amount == "2666.68"
    bal = (await db.execute(select(LeaveBalance).where(LeaveBalance.leave_type_id == enc.leave_type_id))).scalars().one()
    assert D(str(bal.encashed)) == D("4.0")
    inst = (await db.execute(select(PayrollLoanInstallment).where(PayrollLoanInstallment.fnf_id.is_not(None)))).scalars().one()
    assert inst.kind == "foreclosure" and inst.amount == "3000.00" and inst.period.isoformat() == "2025-06-01"
    assert ok["notice"]["item_id"] == str(notice.id) and ok["notice"]["amount"] == "2499.99"
    assert ok["items"][0]["item_id"] == str(bonus.id) and ok["encashments"][0]["encashment_id"] == str(enc.id)
    assert ok["loans"][0]["installment_id"] == str(inst.id) and ok["loans"][0]["status"] == "pending"
    ev = (await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.item_type == "fnf"))).scalars().all()
    assert {e.action for e in ev} >= {"create", "submit", "approve"}
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.fnf.approve"))).scalars().one()
    assert (
        logs.after["maker_id"] and logs.after["checker_id"] and logs.after["counts"] == {"items": 1, "encashments": 1, "loans": 1}
    )
    assert "2499" not in str(logs.after) and "Pro-rata" not in str(logs.after)


async def test_negative_net_blocks_approve(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    loan = await _mk_loan(client, h, uid["employee"], principal="90000.00", emi="10000.00")
    f = await mk(client, h, uid["employee"])
    f = await put(client, h, f, loans=[{"loan_id": loan["id"], "mode": "payroll"}])
    f = await act(client, h, f, "submit")
    r = await client.post(
        f"{FNF}/{f['id']}/approve", json={"expected_version": f["version"], "current_password": PASSWORD}, headers=a
    )
    assert r.status_code == 409 and err(r) == "FNF_NEGATIVE"
    assert await items_of(db, uid["employee"]) == []  # the whole approval rolled back
    assert (await db.execute(select(PayrollLoanInstallment).where(PayrollLoanInstallment.fnf_id.is_not(None)))).first() is None
    assert (await get(client, h, f["id"]))["status"] == "submitted"
    # recording the loan as paid outside lets it through
    w = await act(client, h, await get(client, h, f["id"]), "withdraw")
    w = await put(
        client, h, w, loans=[{"loan_id": loan["id"], "mode": "outside", "paid_on": "2025-06-18", "reference": "UTR123"}]
    )
    s = await act(client, h, w, "submit")
    ok = await act(client, a, s, "approve")
    assert (
        ok["loans"][0]["mode"] == "outside" and ok["loans"][0]["status"] == "closed" and ok["loans"][0]["reference"] == "UTR123"
    )
    lo = (await db.execute(select(PayrollLoan))).scalars().one()
    assert lo.status == "closed" and lo.close_mode == "foreclosed_outside"


async def test_approve_period_locked_when_month_published_without_revision(client, payroll, uid):
    h, a = payroll["hr"], payroll["admin"]
    run = await new_run(client, h)
    await process_and_publish(client, h, run["id"])
    f = await submitted(client, payroll, uid)
    r = await client.post(
        f"{FNF}/{f['id']}/approve", json={"expected_version": f["version"], "current_password": PASSWORD}, headers=a
    )
    assert r.status_code == 409 and err(r) == "PERIOD_LOCKED"
    # HR revises the payslip first: then the approval goes through into the open revision
    rv = await client.post(
        f"{RUNS}/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Final settlement"}, headers=h
    )
    assert rv.status_code in (200, 201), rv.text
    ok = await act(client, a, f, "approve")
    assert ok["status"] == "approved"


async def test_open_items_are_moved(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    # a bonus in a month after the last working day will never be paid by a normal run
    r = await client.post(
        "/api/admin/payroll/items",
        json={"user_id": uid["employee"], "period": "2025-08", "code": "BONUS", "label": "Late bonus", "amount": "500.00"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    f = await mk(client, h, uid["employee"])
    g = await get(client, h, f["id"])
    assert g["move_open_items"] is True and len(g["open_items"]) == 1 and g["open_items"][0]["action"] == "move"
    s = await act(client, h, f, "submit")
    ok = await act(client, a, s, "approve")
    assert ok["open_items"][0]["period"] == "2025-08"
    moved = next(i for i in await items_of(db, uid["employee"]) if i.code == "BONUS")
    assert moved.period.isoformat() == "2025-06-01" and moved.fnf_id is None


# ================================================================================================ revoke
async def test_revoke_restores_everything(client, payroll, uid, db, types):
    h, a = payroll["hr"], payroll["admin"]
    loan = await _mk_loan(client, h, uid["employee"], principal="3000.00", emi="1000.00", start="2025-06")
    f = await mk(client, h, uid["employee"])
    f = await put(client, h, f, encashments=[{"leave_type_id": types["EL"], "year": 2025, "days": 4.0}],
                  loans=[{"loan_id": loan["id"], "mode": "payroll"}],
                  items=[{"code": "BONUS", "label": "Farewell bonus", "amount": "9000.00"}])  # fmt: skip
    ok = await act(client, a, await act(client, h, f, "submit"), "approve")
    r = await client.post(
        f"{FNF}/{f['id']}/revoke",
        json={"expected_version": ok["version"], "reason": "Mistake found", "current_password": "wrong"},
        headers=a,
    )
    assert err(r) == "REAUTH_FAILED"
    r = await client.post(
        f"{FNF}/{f['id']}/revoke",
        json={"expected_version": ok["version"], "reason": "Mistake found", "current_password": PASSWORD},
        headers=payroll["employee"],
    )
    assert r.status_code == 403
    rv = await act(client, a, ok, "revoke", reason="Mistake found")
    assert rv["status"] == "draft" and rv["summary"] is None
    its = await items_of(db, uid["employee"])
    assert all(i.status == "cancelled" for i in its) and len(its) == 2
    enc = (await db.execute(select(LeaveEncashment))).scalars().one()
    assert enc.status == "cancelled"
    bal = (await db.execute(select(LeaveBalance).where(LeaveBalance.leave_type_id == enc.leave_type_id))).scalars().one()
    assert D(str(bal.encashed)) == D("0.0")
    insts = (
        (await db.execute(select(PayrollLoanInstallment).order_by(PayrollLoanInstallment.period, PayrollLoanInstallment.seq)))
        .scalars()
        .all()
    )
    live = [i for i in insts if i.status == "pending"]
    assert [i.amount for i in live] == ["1000.00"] * 3 and live[0].period.isoformat() == "2025-06-01"  # EMI schedule is back
    assert all(i.fnf_id is None for i in live)
    ev = (
        (
            await db.execute(
                select(PayrollItemEvent).where(PayrollItemEvent.item_type == "fnf", PayrollItemEvent.action == "revoke")
            )
        )
        .scalars()
        .one()
    )
    assert ev.actor_id == uuid.UUID(uid["admin"]) and ev.detail["reason"] == "Mistake found"
    # a second approval writes NEW rows; the cancelled ones stay
    f2 = await act(client, h, rv, "submit")
    ok2 = await act(client, a, f2, "approve")
    assert ok2["status"] == "approved"
    assert len(await items_of(db, uid["employee"])) == 4


async def test_revoke_refused_while_processed(client, payroll, uid):
    h, a = payroll["hr"], payroll["admin"]
    ok = await act(client, a, await submitted(client, payroll, uid), "approve")
    run = await new_run(client, h)
    r = await client.post(f"{RUNS}/{run['id']}/process", headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(
        f"{FNF}/{ok['id']}/revoke",
        json={"expected_version": ok["version"], "reason": "Mistake found", "current_password": PASSWORD},
        headers=a,
    )
    assert r.status_code == 409 and err(r) == "FNF_NOT_REVOCABLE"
    r = await client.post(f"{FNF}/{ok['id']}/cancel", json={"reason": "Employee stays"}, headers=h)
    assert r.status_code == 409  # approved: revoke first


# ================================================================================================ payslips, settlement on publish
async def test_final_payslip_and_settled_on_publish(client, payroll, uid, db, types, monkeypatch):
    h, a = payroll["hr"], payroll["admin"]
    pushes = []

    async def fake_notify(session, user_ids, **kw):
        pushes.append((list(user_ids), kw))
        return 0

    monkeypatch.setattr(notifications, "notify", fake_notify)
    f = await mk(client, h, uid["employee"])
    f = await put(client, h, f, items=[{"code": "OTHER_EARNING", "label": "Pro-rata bonus", "amount": "3000.00"}])
    await act(client, a, await act(client, h, f, "submit"), "approve")
    run = await new_run(client, h)
    ps = await row_for(client, h, run["id"], "EMP001")
    detail = (await client.get(f"/api/admin/payroll/payslips/{ps['id']}", headers=h)).json()
    assert detail["final_settlement"] == {"fnf_id": f["id"], "status": "approved"} and detail["is_final"] is True
    assert detail["days"]["employed_days"] == 20.0 and "EXIT_IN_MONTH" in detail["warnings"]
    codes = [x["code"] for x in detail["deductions"]]
    assert "NOTICE_RECOVERY" in codes
    assert any(x["code"] == "OTHER_EARNING" for x in detail["earnings"])
    # no payment on the employee's side before publish
    me = await client.get("/api/me/fnf", headers=payroll["employee"])
    assert me.json()["fnf"]["status"] == "in_progress" and me.json()["fnf"]["final_payslip_id"] is None
    st = await client.get("/api/me/fnf/statement", headers=payroll["employee"])
    assert st.status_code == 404
    draft_pdf = await client.get(f"{FNF}/{f['id']}/statement", headers=h)
    assert (
        draft_pdf.status_code == 200
        and draft_pdf.headers["content-type"] == "application/pdf"
        and draft_pdf.content[:4] == b"%PDF"
    )
    assert (
        draft_pdf.headers["cache-control"] == "no-store" and "fnf-EMP001-2025-06.pdf" in draft_pdf.headers["content-disposition"]
    )
    await process_and_publish(client, h, run["id"])
    g = await get(client, h, f["id"])
    assert g["status"] == "settled" and g["final_payslip"]["id"] == ps["id"] and g["final_payslip"]["status"] == "published"
    me = await client.get("/api/me/fnf", headers=payroll["employee"])
    assert me.json() == {
        "fnf": {
            "id": f["id"],
            "status": "settled",
            "last_working_day": "2025-06-20",
            "settlement_period": PERIOD,
            "final_payslip_id": ps["id"],
        }
    }
    lst = (await client.get("/api/payslips", params={"year": 2025}, headers=payroll["employee"])).json()["items"]
    assert lst[0]["is_final"] is True
    pay = (await client.get(f"/api/payslips/{ps['id']}", headers=payroll["employee"])).json()
    assert pay["is_final"] is True
    pdf = await client.get("/api/me/fnf/statement", headers=payroll["employee"])
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    assert any(kw.get("type") == "fnf_settled" and "amount" not in str(kw).lower() for _, kw in pushes)
    settled = [kw for _, kw in pushes if kw.get("type") == "fnf_settled"]
    assert settled[0]["body"] == "Your final settlement statement is available."
    items = await items_of(db, uid["employee"])
    assert all(i.status == "paid" for i in items)
    ev = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.fnf.settle"))).scalars().all()
    assert len(ev) == 1
    # statement audit rows
    assert (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.fnf.view_own"))).scalars().all()
    # other people's settlement statement is a 404
    r = await client.get("/api/me/fnf/statement", headers=payroll["manager"])
    assert r.status_code == 404


async def test_revision_publish_moves_final_payslip(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    f = await submitted(client, payroll, uid)
    await act(client, a, f, "approve")
    run = await new_run(client, h)
    await process_and_publish(client, h, run["id"])
    first = await get(client, h, f["id"])
    rv = await client.post(f"{RUNS}/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Correction"}, headers=h)
    assert rv.status_code in (200, 201), rv.text
    new_id = rv.json()["id"]
    assert (await get(client, h, f["id"]))["status"] == "settled"  # the open revision changes nothing
    r = await client.post(f"/api/admin/payroll/payslips/{new_id}/process", headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/admin/payroll/payslips/{new_id}/publish", json={"current_password": PASSWORD}, headers=a)
    assert r.status_code == 200, r.text
    after = await get(client, h, f["id"])
    assert after["final_payslip"]["id"] == new_id != first["final_payslip"]["id"]


async def test_settlement_only_payslip_in_a_later_month(client, payroll, uid):
    h, a = payroll["hr"], payroll["admin"]
    f = await mk(client, h, uid["employee"], settlement_period="2025-08")
    f = await put(client, h, f, items=[{"code": "BONUS", "label": "Retention bonus", "amount": "4000.00"}])
    await act(client, a, await act(client, h, f, "submit"), "approve")
    june = await new_run(client, h, "2025-06")
    p6 = await row_for(client, h, june["id"], "EMP001")
    d6 = (await client.get(f"/api/admin/payroll/payslips/{p6['id']}", headers=h)).json()
    assert d6["days"]["employed_days"] == 20.0
    july = await new_run(client, h, "2025-07")
    assert not [
        x
        for x in (await client.get(f"{RUNS}/{july['id']}/payslips", headers=h)).json()["items"]
        if x["user"]["employee_id"] == "EMP001"
    ]
    aug = await new_run(client, h, "2025-08")
    ps = await row_for(client, h, aug["id"], "EMP001")
    d = (await client.get(f"/api/admin/payroll/payslips/{ps['id']}", headers=h)).json()
    assert d["days"]["employed_days"] == 0.0 and d["days"]["paid_days"] == 0.0
    assert "FNF_SETTLEMENT_ONLY" in d["warnings"] and "ZERO_PAID_DAYS" not in d["warnings"]
    assert [x["code"] for x in d["earnings"] if x["amount"] != "0.00"] == ["BONUS"] and d["totals"]["gross_earnings"] == "4000.00"
    assert d["final_settlement"]["status"] == "approved"
    # the manager (not leaving) is unaffected
    assert (await row_for(client, h, aug["id"], "MGR001"))["user"]["employee_id"] == "MGR001"


async def test_publish_refuses_a_payslip_without_the_fnf_lines(client, payroll, uid):
    """Approve after the payslip was processed: the processed payslip does not carry the lines; publishing it must not settle."""
    h, a = payroll["hr"], payroll["admin"]
    run = await new_run(client, h)
    r = await client.post(f"{RUNS}/{run['id']}/process", headers=h)
    assert r.status_code == 200
    f = await submitted(client, payroll, uid)
    await act(client, a, f, "approve")
    r = await publish_run(client, a, run["id"])
    assert r.status_code == 409 and err(r) == "FNF_NOT_INCLUDED"
    assert (await get(client, h, f["id"]))["status"] == "approved"


# ================================================================================================ FNF_MANAGED
async def test_fnf_rows_are_managed(client, payroll, uid, db, types):
    h, a = payroll["hr"], payroll["admin"]
    loan = await _mk_loan(client, h, uid["employee"], principal="3000.00", emi="1000.00")
    f = await mk(client, h, uid["employee"])
    f = await put(client, h, f, encashments=[{"leave_type_id": types["EL"], "year": 2025, "days": 2.0}],
                  loans=[{"loan_id": loan["id"], "mode": "payroll"}])  # fmt: skip
    await act(client, a, await act(client, h, f, "submit"), "approve")
    its = await items_of(db, uid["employee"])
    nid = next(str(i.id) for i in its if i.code == "NOTICE_RECOVERY")
    r = await client.put(f"/api/admin/payroll/items/{nid}", json={"expected_version": 1, "amount": "1.00"}, headers=h)
    assert r.status_code == 409 and err(r) == "FNF_MANAGED"
    r = await client.post(f"/api/admin/payroll/items/{nid}/cancel", json={"reason": "Not needed"}, headers=h)
    assert err(r) == "FNF_MANAGED"
    enc = (await db.execute(select(LeaveEncashment))).scalars().one()
    r = await client.post(f"/api/admin/payroll/encashments/{enc.id}/cancel", json={"reason": "Not needed"}, headers=h)
    assert err(r) == "FNF_MANAGED"
    lo = (await client.get(f"/api/admin/payroll/loans/{loan['id']}", headers=h)).json()
    for action, body in (
        ("cancel", {"reason": "Mistake"}),
        ("foreclose", {"mode": "payroll", "period": PERIOD, "reason": "Mistake"}),
    ):
        r = await client.post(
            f"/api/admin/payroll/loans/{loan['id']}/{action}",
            json={**body, "expected_version": lo["version"], "current_password": PASSWORD},
            headers=h,
        )
        assert r.status_code == 409 and err(r) == "FNF_MANAGED", r.text
    listed = (await client.get("/api/admin/payroll/items", params={"user_id": uid["employee"]}, headers=h)).json()["items"]
    assert any(i["fnf_id"] == f["id"] for i in listed)


async def test_encashment_source_fnf_is_refused(client, payroll, uid, types):
    r = await client.post(
        "/api/admin/payroll/encashments",
        json={"user_id": uid["employee"], "leave_type_id": types["EL"], "year": 2025, "days": 1.0, "period": PERIOD,
              "source": "fnf", "reason": "Exit encashment", "current_password": PASSWORD},
        headers=payroll["hr"],
    )  # fmt: skip
    assert r.status_code == 409 and err(r) == "FNF_MANAGED"


async def test_notice_recovery_refused_in_manual_inputs(client, payroll, uid):
    h = payroll["hr"]
    r = await client.post(
        "/api/admin/payroll/items",
        json={"user_id": uid["employee"], "period": PERIOD, "code": "NOTICE_RECOVERY", "label": "x", "amount": "1.00"},
        headers=h,
    )
    assert r.status_code == 422
    r = await client.post(
        f"{EMP}/{uid['employee']}/structures",
        json={
            "effective_from": "2025-03-01",
            "basic": "1000",
            "hra": "0",
            "allowances": [{"code": "NOTICE_RECOVERY", "name": "x", "amount": "1"}],
            "fixed_deductions": [],
            "tds_monthly": "0",
            "pf_applicable": False,
            "pf_on_full_basic": False,
            "esi_applicable": False,
            "pt_applicable": False,
        },
        headers=h,
    )
    assert r.status_code == 422


# ================================================================================================ activation, dashboard, statutory, reads
async def test_activation_rules(client, payroll, uid, db):
    h, a = payroll["hr"], payroll["admin"]
    f = await mk(client, h, uid["employee"])
    r = await client.post(f"/api/admin/employees/{uid['employee']}/deactivate", json={"reason": "resigned"}, headers=a)
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/admin/employees/{uid['employee']}/activate", headers=a)
    assert r.status_code == 409 and err(r) == "FNF_OPEN"
    ok = await act(client, a, await act(client, h, f, "submit"), "approve")
    r = await client.post(f"/api/admin/employees/{uid['employee']}/activate", headers=a)
    assert err(r) == "FNF_OPEN"  # approved is still open
    run = await new_run(client, h)
    await process_and_publish(client, h, run["id"])
    assert (await get(client, h, ok["id"]))["status"] == "settled"
    r = await client.post(f"/api/admin/employees/{uid['employee']}/activate", headers=a)
    assert r.status_code == 200, r.text
    det = (
        await db.execute(select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id == uuid.UUID(uid["employee"])))
    ).scalar_one()
    await db.refresh(det)
    assert det.last_working_day is None  # cleared on reactivation


async def test_dashboard_alerts_and_leavers(client, payroll, uid):
    h, a = payroll["hr"], payroll["admin"]
    f = await submitted(client, payroll, uid)
    d = (await client.get("/api/admin/payroll/dashboard", params={"period": PERIOD}, headers=h)).json()
    assert "FNF_PENDING_APPROVAL" in d["calendar"]["alerts"]
    assert {"employee_id": "EMP001", "name": "Test Employee", "date": "2025-06-20"} in [
        {**x, "name": "Test Employee"} for x in d["leavers"]
    ] or any(x["employee_id"] == "EMP001" and x["date"] == "2025-06-20" for x in d["leavers"])
    await act(client, a, f, "approve")
    d = (await client.get("/api/admin/payroll/dashboard", params={"period": PERIOD}, headers=h)).json()
    assert "FNF_PENDING_APPROVAL" not in d["calendar"]["alerts"]


async def test_exit_without_fnf_alert(client, payroll, uid):
    h, a = payroll["hr"], payroll["admin"]
    r = await client.post(f"/api/admin/employees/{uid['employee']}/deactivate", json={"reason": "resigned"}, headers=a)
    assert r.status_code == 200
    d = (await client.get("/api/admin/payroll/dashboard", params={"period": PERIOD}, headers=h)).json()
    assert "EXIT_WITHOUT_FNF" in d["calendar"]["alerts"]
    await mk(client, h, uid["employee"])
    d = (await client.get("/api/admin/payroll/dashboard", params={"period": PERIOD}, headers=h)).json()
    assert "EXIT_WITHOUT_FNF" not in d["calendar"]["alerts"]


async def test_esi_last_working_day_uses_lwd(client, payroll, uid, db):
    from app.services import payroll_statutory as st

    h, a = payroll["hr"], payroll["admin"]
    await client.put(
        f"{EMP}/{uid['employee']}/details", json={"esi_ip_number": "1234567890", "current_password": PASSWORD}, headers=h
    )
    f = await submitted(client, payroll, uid)
    await act(client, a, f, "approve")
    run = await new_run(client, h)
    await process_and_publish(client, h, run["id"])
    # a gross of 25000 is above the ESI threshold, so use a template read through the module for the reconciliation only
    assert st._esi_wage  # the function exists; the LWD column logic is covered in the unit test below


async def test_esi_lwd_column_unit():
    from app.services.payroll_statutory import _Row

    det = EmployeePayrollDetails(
        user_id=uuid.uuid4(), last_working_day=dt.date(2025, 6, 20), esi_ip_number="1", uan="1", pan="ABCDE1234F"
    )
    ps = type("P", (), {"employee_snapshot": {}, "result": {}})()
    assert _Row(ps, None, None, det).ident["lwd"] == dt.date(2025, 6, 20)


async def test_list_permissions_and_filters(client, payroll, uid):
    h = payroll["hr"]
    f = await mk(client, h, uid["employee"])
    r = await client.get(FNF, headers=payroll["employee"])
    assert r.status_code == 403
    r = await client.get(FNF, params={"status": "draft"}, headers=payroll["manager"])
    assert r.status_code in (200, 403)
    r = await client.get(FNF, params={"status": "draft", "q": "EMP001"}, headers=h)
    body = r.json()
    assert r.status_code == 200 and body["total"] == 1 and body["items"][0]["id"] == f["id"]
    assert set(body["items"][0]) == {
        "id",
        "user",
        "status",
        "last_working_day",
        "settlement_period",
        "exit_reason",
        "run_status",
        "warnings",
        "created_at",
    }
    r = await client.get(f"{FNF}/{uuid.uuid4()}", headers=h)
    assert r.status_code == 404
    pv = await client.get(f"{FNF}/{f['id']}/preview", headers=h)
    assert pv.status_code == 200
    j = pv.json()
    assert j["estimate"] is True and j["source"] == "structure" and j["days"]["employed_days"] == 20.0
    assert any(x["code"] == "NOTICE_RECOVERY" for x in j["deductions"])
    assert "FNF_SETTLEMENT_ONLY" not in j["warnings"]


async def test_no_amounts_in_audit(client, payroll, uid, db, types):
    h, a = payroll["hr"], payroll["admin"]
    f = await mk(client, h, uid["employee"])
    f = await put(client, h, f, items=[{"code": "BONUS", "label": "SecretLabel", "amount": "7777.00"}], remarks="SecretRemark",
                  encashments=[{"leave_type_id": types["EL"], "year": 2025, "days": 2.0}])  # fmt: skip
    ok = await act(client, a, await act(client, h, f, "submit"), "approve", comment="SecretComment")
    await act(client, a, ok, "revoke", reason="SecretReason")
    rows = (await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.fnf.%")))).scalars().all()
    blob = " ".join(str(r.after) + str(r.before) for r in rows)
    for needle in ("7777", "SecretLabel", "SecretRemark", "SecretComment", "SecretReason", "2499"):
        assert needle not in blob


async def test_money_ceiling(client, payroll, uid):
    h = payroll["hr"]
    f = await mk(client, h, uid["employee"])
    r = await client.put(
        f"{FNF}/{f['id']}",
        json={"expected_version": 1, "items": [{"code": "BONUS", "label": "Huge", "amount": "10000000.01"}]},
        headers=h,
    )
    assert r.status_code == 422


# ================================================================================================ helpers (users, loans)
async def _make_user(db, seeded, role, eid):
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


async def _login(client, eid):
    r = await client.post("/api/auth/login", json={"employee_id": eid, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return bearer(r.json())


async def _mk_loan(client, hdr, user_id, *, principal="30000.00", emi="10000.00", start=PERIOD):
    r = await client.post(
        "/api/admin/payroll/loans",
        json={"user_id": user_id, "kind": "loan", "principal": principal, "emi": emi, "start_period": start,
              "reason": "Medical emergency", "current_password": PASSWORD},
        headers=hdr,
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()


_ = update


async def test_increment_after_the_last_working_day_is_refused(client, payroll, uid):
    from tests.test_payroll_increments import first, pct_body

    h = payroll["hr"]
    await mk(client, h, uid["employee"])
    r = await client.post(f"{EMP}/{uid['employee']}/increments", json=pct_body(user_eff=first(2)), headers=h)
    assert r.status_code == 422 and err(r) == "AFTER_EXIT"
