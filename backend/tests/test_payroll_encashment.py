"""Plan 15 work package 3a, step 2: leave encashment (quote, create, cancel, leave balance effects, engine integration).

2026-10-21 is "today" (office tz Asia/Kolkata); encashment years are 2026 (current) and 2025 (previous).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.models import AuditLog, LeaveBalance, LeaveEncashment, LeaveType, PayrollItemEvent
from app.services import payroll_encashment as enc_svc
from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, new_run, process_and_publish, publish_run, row_for, set_setting

ENC = "/api/admin/payroll/encashments"
RUNS = "/api/admin/payroll/runs"
TODAY = dt.datetime(2026, 10, 21, 4, 30, tzinfo=dt.UTC)
YEAR = 2026


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
    monkeypatch.setattr("app.services.leave.utcnow", lambda: TODAY)
    monkeypatch.setattr("app.services.payroll_runs._today", today)
    from app.core.config import get_settings

    st = get_settings()
    monkeypatch.setattr(st, "leave_write_rate_per_min", 1000)
    monkeypatch.setattr(st, "leave_write_rate_per_day", 100000)


@pytest.fixture
async def types(db):
    el = LeaveType(
        code="EL", name="Earned Leave", annual_quota=12, is_paid=True, encashable=True, encash_max_days_per_year=6,
        encash_min_balance=2,
    )  # fmt: skip
    cl = LeaveType(code="CL", name="Casual Leave", annual_quota=12, is_paid=True)
    db.add_all([el, cl])
    await db.commit()
    return {"EL": str(el.id), "CL": str(cl.id)}


def err(r):
    return r.json()["error"]["code"]


async def mk(client, hdr, user_id, leave_type, *, days=4.0, year=YEAR, period=PERIOD, expect=201, **kw):
    body = {
        "user_id": user_id, "leave_type_id": leave_type, "year": year, "days": days, "period": period, "source": "request",
        "reason": "Year-end encashment", "current_password": PASSWORD,
    }  # fmt: skip
    body.update(kw)
    r = await client.post(ENC, json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def quote(client, hdr, user_id, leave_type, **params):
    q = {"leave_type_id": leave_type, "year": YEAR, "period": PERIOD, **params}
    return await client.get(f"/api/admin/payroll/employees/{user_id}/encashment-quote", params=q, headers=hdr)


async def leave_bal(client, hdr, code="EL"):
    r = await client.get("/api/leave/balance", headers=hdr, params={"year": YEAR})
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["leave"] if x["code"] == code)


async def admin_bal(client, hdr, user_id, code="EL"):
    r = await client.get(f"/api/admin/employees/{user_id}/leave-balances", headers=hdr, params={"year": YEAR})
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["items"] if x["leave_type"]["code"] == code)


async def payslip(client, hdr, run_id, emp="EMP001"):
    row = await row_for(client, hdr, run_id, emp)
    r = await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


# ================================================================================================ pure
def test_rate_and_amount_round_to_paise():
    assert enc_svc.daily_rate(D("20000"), 30) == D("666.67")
    assert enc_svc.amount_for(D("4.0"), D("666.67")) == D("2666.68")
    assert enc_svc.daily_rate(D("25000"), 26) == D("961.54")
    assert enc_svc.amount_for(D("0.5"), D("961.54")) == D("480.77")


# ================================================================================================ quote
async def test_quote_matches_the_contract_example(client, payroll, uid, types):
    hr = payroll["hr"]
    r = await quote(client, hr, uid["employee"], types["EL"], days=4)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "leave_type": {"id": types["EL"], "code": "EL", "name": "Earned Leave"}, "year": 2026, "available_days": 6.0, "days": 4.0,
        "daily_rate": "666.67", "amount": "2666.68", "basis": "basic", "divisor": 30, "period": "2025-06",
    }  # fmt: skip
    # days default to everything available; the quote writes nothing
    r = await quote(client, hr, uid["employee"], types["EL"])
    assert r.json()["days"] == 6.0 and r.json()["amount"] == "4000.02"
    assert (await leave_bal(client, payroll["employee"]))["encashed"] == 0.0


async def test_quote_errors(client, payroll, uid, types):
    hr = payroll["hr"]
    r = await quote(client, hr, uid["employee"], types["CL"])
    assert r.status_code == 422 and err(r) == "NOT_ENCASHABLE"
    r = await quote(client, hr, uid["employee"], types["EL"], days=7)
    assert r.status_code == 409 and err(r) == "INSUFFICIENT_BALANCE"
    assert r.json()["error"]["details"] == [{"field": "available_days", "message": "6.0"}]
    r = await quote(client, hr, uid["employee"], types["EL"], year=2024)
    assert r.status_code == 422
    r = await quote(client, hr, uid["employee"], types["EL"], days=1.25)
    assert r.status_code == 422
    r = await quote(client, hr, uid["employee"], types["EL"], period="2024-12")  # before the first structure
    assert r.status_code == 422 and err(r) == "STRUCTURE_MISSING"
    r = await quote(client, hr, "00000000-0000-0000-0000-000000000001", types["EL"])
    assert r.status_code == 404
    r = await quote(client, hr, uid["employee"], "00000000-0000-0000-0000-000000000001")
    assert r.status_code == 404
    r = await quote(client, payroll["employee"], uid["employee"], types["EL"])
    assert r.status_code == 403


async def test_gross_basis_and_divisor_are_settings(client, payroll, uid, types):
    hr = payroll["hr"]
    await set_setting(client, payroll["admin"], encashment_basis="gross", encashment_divisor=26)
    r = await quote(client, hr, uid["employee"], types["EL"], days=1)
    assert (r.json()["daily_rate"], r.json()["basis"], r.json()["divisor"]) == ("961.54", "gross", 26)  # 25000 / 26


# ================================================================================================ create + leave balance effects
async def test_create_reserves_days_in_the_leave_balance(client, payroll, uid, types, db):
    hr, emp = payroll["hr"], payroll["employee"]
    before = await leave_bal(client, emp)
    assert (before["quota"], before["encashed"], before["remaining"]) == (12.0, 0.0, 12.0)
    e = await mk(client, hr, uid["employee"], types["EL"], days=4.0)
    assert (e["days"], e["daily_rate"], e["amount"], e["status"], e["period"], e["source"], e["run_status"]) == (
        4.0, "666.67", "2666.68", "pending", "2025-06", "request", "none"
    )  # fmt: skip
    assert (
        e["leave_type"]["code"] == "EL" and e["user"]["employee_id"] == "EMP001" and e["version"] == 1 and e["payslip_id"] is None
    )
    after = await leave_bal(client, emp)
    assert (after["quota"], after["used"], after["pending"], after["encashed"], after["remaining"]) == (12.0, 0.0, 0.0, 4.0, 8.0)
    ab = await admin_bal(client, hr, uid["employee"])
    assert (ab["encashed"], ab["remaining"]) == (4.0, 8.0)
    # the DB row
    db.expire_all()
    bal = (await db.execute(select(LeaveBalance).where(LeaveBalance.year == YEAR))).scalars().all()
    assert any(float(b.encashed) == 4.0 for b in bal)
    # leave requests see the reservation: 9 days no longer fit, 8 do
    r = await client.post("/api/leave/requests", headers=emp, json={
        "leave_type_id": types["EL"], "from_date": "2026-11-02", "to_date": "2026-11-12", "reason": "trip"})  # fmt: skip
    assert r.status_code == 409 and err(r) == "INSUFFICIENT_BALANCE"  # 9 working days > 8 remaining
    r = await client.post("/api/leave/requests", headers=emp, json={
        "leave_type_id": types["EL"], "from_date": "2026-11-02", "to_date": "2026-11-11", "reason": "trip"})  # fmt: skip
    assert r.status_code == 201, r.text  # 8 working days
    after = await leave_bal(client, emp)
    assert (after["pending"], after["encashed"], after["remaining"]) == (8.0, 4.0, 0.0)
    # approving moves pending -> used and keeps the encashed reservation
    mgr = payroll["manager"]
    rid = r.json()["id"]
    ok = await client.put(f"/api/admin/requests/leave/{rid}/approve", headers=mgr, json={})
    assert ok.status_code == 200, ok.text
    done = await leave_bal(client, emp)
    assert (done["used"], done["pending"], done["encashed"], done["remaining"]) == (8.0, 0.0, 4.0, 0.0)


async def test_pending_leave_reduces_what_can_be_encashed(client, payroll, uid, types):
    hr, emp = payroll["hr"], payroll["employee"]
    r = await client.post("/api/leave/requests", headers=emp, json={
        "leave_type_id": types["EL"], "from_date": "2026-11-02", "to_date": "2026-11-06", "reason": "trip"})  # fmt: skip
    assert r.status_code == 201, r.text  # 5 days pending: 12 - 5 - 2 (minimum balance) = 5 available
    q = await quote(client, hr, uid["employee"], types["EL"])
    assert q.json()["available_days"] == 5.0
    e = await mk(client, hr, uid["employee"], types["EL"], days=6, expect=409)
    assert e["error"]["details"] == [{"field": "available_days", "message": "5.0"}]


async def test_yearly_cap_and_minimum_balance(client, payroll, uid, types):
    hr = payroll["hr"]
    await mk(client, hr, uid["employee"], types["EL"], days=4)
    e = await mk(client, hr, uid["employee"], types["EL"], days=3, expect=409)  # cap 6 - 4 encashed = 2 left
    assert e["error"]["details"][0]["message"] == "2.0"
    await mk(client, hr, uid["employee"], types["EL"], days=2.5, expect=409)
    await mk(client, hr, uid["employee"], types["EL"], days=2)
    await mk(client, hr, uid["employee"], types["EL"], days=0.5, expect=409)  # nothing left under the cap


async def test_previous_year_and_year_limits(client, payroll, uid, types):
    hr = payroll["hr"]
    prev = await mk(client, hr, uid["employee"], types["EL"], days=1, year=2025)
    assert prev["year"] == 2025
    await mk(client, hr, uid["employee"], types["EL"], days=1, year=2024, expect=422)
    await mk(client, hr, uid["employee"], types["EL"], days=1, year=2027, expect=422)


async def test_create_validation_step_up_and_self_rule(client, payroll, uid, types):
    hr = payroll["hr"]
    emp = uid["employee"]
    await mk(client, hr, emp, types["CL"], expect=422)  # NOT_ENCASHABLE
    await mk(client, hr, emp, types["EL"], days=0, expect=422)
    await mk(client, hr, emp, types["EL"], days=1.25, expect=422)
    await mk(client, hr, emp, types["EL"], days=-1, expect=422)
    await mk(client, hr, emp, types["EL"], reason="x", expect=422)
    await mk(client, hr, emp, types["EL"], surprise=True, expect=422)
    await mk(client, hr, emp, types["EL"], source="other", expect=422)
    await mk(client, hr, emp, types["EL"], current_password="wrong-pass-word", expect=403)
    r = await client.post(ENC, headers=hr, json={"user_id": emp, "leave_type_id": types["EL"], "year": YEAR, "days": 1,
                                                 "period": PERIOD, "reason": "No password"})  # fmt: skip
    assert r.status_code == 422
    await mk(client, hr, uid["hr"], types["EL"], expect=403)  # nobody encashes their own leave
    await mk(client, hr, emp, types["EL"], period="2024-12", expect=422)  # STRUCTURE_MISSING
    await mk(client, hr, emp, types["EL"], period="2030-01", expect=422)  # more than 12 months ahead
    await mk(client, hr, emp, "00000000-0000-0000-0000-000000000001", expect=404)
    r = await client.get(ENC, headers=hr)
    assert r.status_code == 200 and r.json()["total"] == 0  # nothing was created


async def test_period_must_be_open(client, payroll, uid, types):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    await mk(client, hr, uid["employee"], types["EL"], period=PERIOD, expect=409)  # published month, no open revision
    ok = await mk(client, hr, uid["employee"], types["EL"], period="2025-07")
    assert ok["run_status"] == "none"


# ================================================================================================ cancel
async def test_cancel_gives_the_days_back_once(client, payroll, uid, types):
    hr, emp = payroll["hr"], payroll["employee"]
    e = await mk(client, hr, uid["employee"], types["EL"], days=4)
    r = await client.post(f"{ENC}/{e['id']}/cancel", json={"reason": "Changed my mind"}, headers=hr)
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["version"] == 2
    assert (await leave_bal(client, emp))["encashed"] == 0.0
    r = await client.post(f"{ENC}/{e['id']}/cancel", headers=hr)  # the body is optional
    assert r.status_code == 409 and err(r) == "ITEM_LOCKED"
    assert (await leave_bal(client, emp))["encashed"] == 0.0  # not given back twice
    again = await mk(client, hr, uid["employee"], types["EL"], days=6)  # the cap counts live encashments only
    assert again["days"] == 6.0
    r = await client.post(f"{ENC}/00000000-0000-0000-0000-000000000001/cancel", headers=hr)
    assert r.status_code == 404
    r = await client.post(f"{ENC}/{again['id']}/cancel", json={"reason": "x"}, headers=hr)
    assert r.status_code == 422


async def test_list_filters(client, payroll, uid, types):
    hr = payroll["hr"]
    a = await mk(client, hr, uid["employee"], types["EL"], days=2)
    b = await mk(client, hr, uid["manager"], types["EL"], days=1)
    r = await client.get(ENC, headers=hr)
    assert r.json()["total"] == 2
    r = await client.get(ENC, params={"user_id": uid["manager"]}, headers=hr)
    assert [i["id"] for i in r.json()["items"]] == [b["id"]]
    r = await client.get(ENC, params={"status": "cancelled"}, headers=hr)
    assert r.json()["items"] == []
    r = await client.get(ENC, params={"year": 2025}, headers=hr)
    assert r.json()["items"] == []
    r = await client.get(ENC, params={"year": YEAR, "status": "pending", "user_id": uid["employee"]}, headers=hr)
    assert [i["id"] for i in r.json()["items"]] == [a["id"]]
    assert (await client.get(ENC, headers=payroll["employee"])).status_code == 403


# ================================================================================================ engine integration
async def test_encashment_flows_through_create_process_reopen_publish_revise(client, payroll, uid, types, db):
    hr, emp = payroll["hr"], payroll["employee"]
    e = await mk(client, hr, uid["employee"], types["EL"], days=4)
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    (c,) = p["inputs"]["components"]
    assert (c["code"], c["label"], c["amount"]) == ("LEAVE_ENCASH", "Leave encashment (4.0 d)", "2666.68")
    assert c["sources"] == [{"type": "encashment", "id": e["id"]}]
    earn = next(x for x in p["earnings"] if x["code"] == "LEAVE_ENCASH")
    assert earn["amount"] == "2666.68" and earn["monthly"] is None
    assert p["totals"]["gross_earnings"] == "27666.68"
    other = await payslip(client, hr, run["id"], "MGR001")
    assert other["inputs"]["components"] == []

    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    cur = (await client.get(f"{ENC}", params={"status": "processing"}, headers=hr)).json()["items"]
    assert [x["id"] for x in cur] == [e["id"]] and cur[0]["payslip_id"] == p["id"] and cur[0]["run_status"] == "processed"
    r = await client.post(f"{ENC}/{e['id']}/cancel", headers=hr)
    assert r.status_code == 409 and err(r) == "ITEM_LOCKED"
    assert (await leave_bal(client, emp))["encashed"] == 4.0

    assert (await client.post(f"{RUNS}/{run['id']}/reopen", headers=hr)).status_code == 200
    assert (await client.get(ENC, params={"status": "pending"}, headers=hr)).json()["total"] == 1
    assert (await client.post(f"{RUNS}/{run['id']}/process", headers=hr)).status_code == 200
    assert (await publish_run(client, hr, run["id"])).status_code == 200
    paid = (await client.get(ENC, params={"status": "paid"}, headers=hr)).json()["items"]
    assert [x["id"] for x in paid] == [e["id"]]
    r = await client.post(f"{ENC}/{e['id']}/cancel", headers=hr)
    assert r.status_code == 409 and err(r) == "ITEM_LOCKED"
    assert (await leave_bal(client, emp))["encashed"] == 4.0  # a paid encashment keeps its days

    r = await client.post(f"{RUNS}/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Fix LOP"}, headers=hr)
    assert r.status_code == 201, r.text
    rev = r.json()
    assert [(x["code"], x["amount"]) for x in rev["inputs"]["components"]] == [("LEAVE_ENCASH", "2666.68")]
    assert (await client.post(f"/api/admin/payroll/payslips/{rev['id']}/process", headers=hr)).status_code == 200
    r = await client.post(f"/api/admin/payroll/payslips/{rev['id']}/publish", json={"current_password": PASSWORD}, headers=hr)
    assert r.status_code == 200, r.text
    paid = (await client.get(ENC, params={"status": "paid"}, headers=hr)).json()["items"]
    assert paid[0]["payslip_id"] == rev["id"]
    db.expire_all()
    actions = [x.action for x in (await db.execute(select(PayrollItemEvent))).scalars() if x.item_type == "encashment"]
    assert actions == ["create", "process", "reopen", "process", "pay", "carry"]


async def test_encashment_is_not_an_esi_wage(client, payroll, uid, types):
    hr = payroll["hr"]
    # MGR001 earns 40000 gross (no ESI); EMP001 25000 (above the 20000 threshold): lower the threshold so ESI applies
    await set_setting(client, payroll["admin"], esi_wage_threshold=30000)
    run = await new_run(client, hr)
    base = await payslip(client, hr, run["id"])
    esi = next(d for d in base["deductions"] if d["code"] == "ESI")["amount"]
    await mk(client, hr, uid["employee"], types["EL"], days=4)
    assert (await client.post(f"{RUNS}/{run['id']}/refresh", headers=hr)).status_code == 200
    p = await payslip(client, hr, run["id"])
    assert next(d for d in p["deductions"] if d["code"] == "ESI")["amount"] == esi  # unchanged: LEAVE_ENCASH is no ESI wage
    assert D(p["totals"]["gross_earnings"]) == D(base["totals"]["gross_earnings"]) + D("2666.68")


# ================================================================================================ leave admin side
async def test_quota_below_used_counts_encashed_days(client, payroll, uid, types):
    admin = payroll["admin"]
    await mk(client, payroll["hr"], uid["employee"], types["EL"], days=4)
    body = {"year": YEAR, "items": [{"leave_type_id": types["EL"], "quota": 3.5}]}
    r = await client.put(f"/api/admin/employees/{uid['employee']}/leave-balances", headers=admin, json=body)
    assert r.status_code in (409, 422) and err(r) == "QUOTA_BELOW_USED"
    body["items"][0]["quota"] = 4.0
    r = await client.put(f"/api/admin/employees/{uid['employee']}/leave-balances", headers=admin, json=body)
    assert r.status_code == 200, r.text
    item = next(x for x in r.json()["items"] if x["leave_type"]["code"] == "EL")
    assert (item["quota"], item["encashed"], item["remaining"]) == (4.0, 4.0, 0.0)


async def test_leave_type_encashment_fields(client, payroll, types):
    admin = payroll["admin"]
    base = {"code": "PL", "name": "Privilege Leave", "annual_quota": 15}
    r = await client.post("/api/admin/leave-types", headers=admin, json=base)
    assert r.status_code == 201, r.text
    assert (r.json()["encashable"], r.json()["encash_max_days_per_year"], r.json()["encash_min_balance"]) == (False, None, 0.0)
    tid = r.json()["id"]
    r = await client.put(f"/api/admin/leave-types/{tid}", headers=admin, json={
        **base, "encashable": True, "encash_max_days_per_year": 7.5, "encash_min_balance": 3})  # fmt: skip
    assert r.status_code == 200, r.text
    assert (r.json()["encashable"], r.json()["encash_max_days_per_year"], r.json()["encash_min_balance"]) == (True, 7.5, 3.0)
    # keys that are not sent keep their value (older clients)
    r = await client.put(f"/api/admin/leave-types/{tid}", headers=admin, json={**base, "annual_quota": 14})
    assert r.status_code == 200
    assert (r.json()["annual_quota"], r.json()["encashable"], r.json()["encash_max_days_per_year"]) == (14.0, True, 7.5)
    r = await client.put(f"/api/admin/leave-types/{tid}", headers=admin, json={**base, "encash_max_days_per_year": None})
    assert r.json()["encash_max_days_per_year"] is None  # null = no cap
    for bad in ({"encash_max_days_per_year": 0}, {"encash_max_days_per_year": 2.3}, {"encash_min_balance": -1},
                {"encash_min_balance": 1.25}, {"encashable": "yes"}):  # fmt: skip
        r = await client.put(f"/api/admin/leave-types/{tid}", headers=admin, json={**base, **bad})
        assert r.status_code == 422, bad
    assert (await client.get(f"/api/admin/leave-types/{tid}", headers=payroll["employee"])).json()["encashable"] is True


async def test_leave_type_with_encashments_cannot_be_deleted(client, payroll, uid, types):
    await mk(client, payroll["hr"], uid["employee"], types["EL"], days=1)
    r = await client.delete(f"/api/admin/leave-types/{types['EL']}", headers=payroll["admin"])
    assert r.status_code == 409


# ================================================================================================ audit / storage
async def test_audit_has_no_amounts_or_reasons_and_money_is_encrypted(client, payroll, uid, types, db):
    hr = payroll["hr"]
    e = await mk(client, hr, uid["employee"], types["EL"], days=4, reason="Confidential family reason")
    await client.post(f"{ENC}/{e['id']}/cancel", json={"reason": "Secret cancel text"}, headers=hr)
    db.expire_all()
    rows = [a for a in (await db.execute(select(AuditLog))).scalars() if a.action.startswith("payroll.encashment")]
    assert {a.action for a in rows} == {"payroll.encashment.create", "payroll.encashment.cancel"}
    blob = " ".join(f"{a.before}{a.after}" for a in rows)
    for secret in ("2666", "666.67", "Confidential", "Secret cancel"):
        assert secret not in blob
    created = next(a for a in rows if a.action == "payroll.encashment.create")
    assert created.after["leave_type"] == "EL" and created.after["year"] == YEAR and created.after["days"] == 4.0
    from sqlalchemy import LargeBinary, type_coerce

    raw = (await db.execute(select(type_coerce(LeaveEncashment.amount, LargeBinary)))).scalars().first()
    assert b"2666" not in bytes(raw)
    ev = [x for x in (await db.execute(select(PayrollItemEvent))).scalars() if x.item_type == "encashment"]
    assert [x.action for x in ev] == ["create", "cancel"] and ev[0].amount == "2666.68"
    assert ev[1].detail == {"reason": "Secret cancel text"}


async def test_unauthenticated_and_employee_access(client, payroll, uid, types):
    assert (await client.get(ENC)).status_code == 401
    emp = payroll["employee"]
    r = await client.post(ENC, headers=emp, json={})
    assert r.status_code == 403
    r = await client.post(f"{ENC}/{uid['employee']}/cancel", headers=emp)
    assert r.status_code == 403


async def test_every_source_lands_on_one_payslip_and_the_lines_total_up(client, payroll, uid, types):
    hr = payroll["hr"]
    e = await mk(client, hr, uid["employee"], types["EL"], days=4)
    r = await client.post(
        "/api/admin/payroll/loans", headers=hr,
        json={"user_id": uid["employee"], "kind": "advance", "principal": "3000.00", "emi": "1000.00", "start_period": PERIOD,
              "reason": "Festival advance", "current_password": PASSWORD},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/admin/payroll/items", headers=hr,
        json={"user_id": uid["employee"], "period": PERIOD, "code": "BONUS", "label": "Diwali", "amount": "5000.00"},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    got = {c["code"]: c["amount"] for c in p["inputs"]["components"]}
    assert got == {"BONUS": "5000.00", "ADVANCE_RECOVERY": "1000.00", "LEAVE_ENCASH": "2666.68"}
    assert {s["type"] for c in p["inputs"]["components"] for s in c["sources"]} == {"one_time", "loan_installment", "encashment"}
    assert p["totals"]["gross_earnings"] == "32666.68"  # 25000 + 5000 + 2666.68
    await process_and_publish(client, hr, run["id"])
    assert (await client.get(ENC, params={"status": "paid"}, headers=hr)).json()["items"][0]["id"] == e["id"]
    stmts = (await client.get("/api/admin/payroll/loans", params={"status": "active"}, headers=hr)).json()["items"]
    assert stmts[0]["recovered"] == "1000.00" and stmts[0]["outstanding"] == "2000.00"
