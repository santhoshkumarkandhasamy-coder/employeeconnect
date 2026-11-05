"""Plan 15 work package 3a, step 1: the component engine (calc, pull, status flow, revisions, guards) and one-time items.

Everything runs on SQLite; row locks (`FOR UPDATE`) and the DB triggers are PostgreSQL-only and covered by the migration test.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.models import AuditLog, PayrollItemEvent, PayrollOneTimeItem
from app.services import payroll_calc as calc
from app.services import payroll_components as comps
from scripts.check_reserved_codes import find_reserved
from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, new_run, process_and_publish, publish_run, row_for

ITEMS = "/api/admin/payroll/items"


# ================================================================================================ pure calc
def _calc(components=(), adjustments=(), basic="10000", hra="0", **struct):
    data = calc.StructureData(basic=D(basic), hra=D(hra), **struct)
    rules = calc.Rules(pt_slabs=(), employer_share_from_employee=False)
    inputs = calc.PayInputs(
        adjustments=tuple(calc.Adjustment(*a) for a in adjustments), components=tuple(calc.Adjustment(*c) for c in components)
    )
    return calc.calculate(data, rules, dt.date(2025, 6, 1), D(30), D(30), inputs, esi_is_eligible=True)


def _line(rows, code):
    return next((D(r["amount"]) for r in rows if r["code"] == code), None)


def test_overtime_is_an_esi_wage_and_leave_encashment_is_not():
    base = _calc()
    ot = _calc(components=[("OVERTIME", "Overtime (2 h)", D("1000.00"))])
    le = _calc(components=[("LEAVE_ENCASH", "Leave encashment", D("1000.00"))])
    assert _line(base.deductions, "ESI") == D("75.00")  # ceil(10000 x 0.75%)
    assert _line(ot.deductions, "ESI") == D("83.00")  # ceil(11000 x 0.75%): OVERTIME counts
    assert _line(le.deductions, "ESI") == D("75.00")  # LEAVE_ENCASH does not
    assert ot.gross == le.gross == D("11000.00")
    assert _line(ot.earnings, "OVERTIME") == D("1000.00") and _line(le.earnings, "LEAVE_ENCASH") == D("1000.00")


def test_components_behave_like_adjustments_and_never_touch_pf():
    r = _calc(
        components=[("BONUS", "Diwali", D("5000.00")), ("LOAN_EMI", "Loan L-0001 (1/5)", D("2000.00"))],
        adjustments=[("INCENTIVE", "Sales", D("500.00"))],
    )
    assert r.gross == D("15500.00")
    assert _line(r.deductions, "LOAN_EMI") == D("2000.00")
    assert _line(r.deductions, "PF") == D("1200.00")  # 12% of basic only: bonus is no PF wage
    assert r.net == r.gross - r.total_deductions
    # ESI wage = basic + INCENTIVE (BONUS is not): ceil(10500 x 0.75%)
    assert _line(r.deductions, "ESI") == D("79.00")


def test_pt_is_looked_up_on_the_gross_including_components():
    slabs = (calc.PtSlab(D("0"), D("14999"), D("0")), calc.PtSlab(D("15000"), None, D("200")))
    rules = calc.Rules(pt_slabs=slabs, pt_frequency="monthly", employer_share_from_employee=False)
    data = calc.StructureData(basic=D("10000"), pf_applicable=False, esi_applicable=False)
    june = dt.date(2025, 6, 1)
    without = calc.calculate(data, rules, june, D(30), D(30), calc.PayInputs())
    with_bonus = calc.calculate(
        data, rules, june, D(30), D(30), calc.PayInputs(components=(calc.Adjustment("OVERTIME", "OT", D("6000.00")),))
    )
    assert _line(without.deductions, "PT") is None  # 0 is not listed
    assert _line(with_bonus.deductions, "PT") == D("200.00")


def test_system_codes_are_reserved_everywhere():
    assert calc.SYSTEM_ONLY_CODES == {"OVERTIME", "LEAVE_ENCASH", "NOTICE_RECOVERY"}
    assert calc.SYSTEM_ONLY_CODES <= calc.RESERVED_CODES  # refused as allowance / fixed deduction codes
    assert "OVERTIME" in calc.ESI_ADJUSTMENTS and "LEAVE_ENCASH" not in calc.ESI_ADJUSTMENTS
    assert {"OVERTIME_UNREVIEWED", "OVERTIME_CAPPED", "ARREARS_PROPOSED", "MANUAL_COMPONENT_OVERLAP"} <= calc.WARNING_CODES


def test_check_reserved_codes_script_finds_offenders():
    assert find_reserved({"allowances": [{"code": "TRAVEL"}], "fixed_deductions": []}) == []
    found = find_reserved(
        {"allowances": [{"code": "OVERTIME", "amount": "1.00"}], "fixed_deductions": [{"code": "leave_encash"}, {"code": "X"}]}
    )
    assert found == [("allowance", "OVERTIME"), ("fixed_deduction", "LEAVE_ENCASH")]


def test_target_period_horizon():
    today = dt.date(2026, 11, 5)
    assert comps.parse_target_period("2025-06", today) == dt.date(2025, 6, 1)  # the past is fine
    assert comps.parse_target_period("2027-11", today) == dt.date(2027, 11, 1)  # exactly 12 months ahead
    for bad in ("2027-12", "2026-13", "x", "1999-01"):
        with pytest.raises(AppError) as e:
            comps.parse_target_period(bad, today)
        assert e.value.code == "INVALID_PERIOD"


async def test_structure_codes_overtime_and_leave_encash_are_refused(client, payroll, uid):
    for code in ("OVERTIME", "LEAVE_ENCASH"):
        for key in ("allowances", "fixed_deductions"):
            body = {
                "effective_from": "2025-02-01", "basic": "20000", "hra": "0", "tds_monthly": "0", "allowances": [],
                "fixed_deductions": [],
            }  # fmt: skip
            body[key] = [{"code": code, "name": "x", "amount": "100"}]
            r = await client.post(f"/api/admin/payroll/employees/{uid['employee']}/structures", json=body, headers=payroll["hr"])
            assert r.status_code == 422, (code, key, r.text)


# ================================================================================================ helpers
async def mk(client, hdr, user_id, *, period=PERIOD, code="BONUS", label="Diwali bonus", amount="5000.00", expect=201):
    r = await client.post(
        ITEMS, json={"user_id": user_id, "period": period, "code": code, "label": label, "amount": amount}, headers=hdr
    )
    assert r.status_code == expect, r.text
    return r.json()


async def payslip(client, hdr, run_id, emp="EMP001"):
    row = await row_for(client, hdr, run_id, emp)
    r = await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


async def rows(db):
    db.expire_all()
    return list((await db.execute(select(PayrollOneTimeItem).order_by(PayrollOneTimeItem.created_at))).scalars())


async def events(db):
    db.expire_all()
    return list((await db.execute(select(PayrollItemEvent))).scalars())


# ================================================================================================ create / refresh / PUT keeps
async def test_item_before_the_run_is_pulled_at_run_create(client, payroll, uid):
    hr = payroll["hr"]
    item = await mk(client, hr, uid["employee"])
    assert item["status"] == "pending" and item["run_status"] == "none" and item["flags"] == []
    assert item["amount"] == "5000.00" and item["user"]["employee_id"] == "EMP001" and item["version"] == 1
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    comp = p["inputs"]["components"]
    assert comp == [
        {"code": "BONUS", "label": "Diwali bonus", "amount": "5000.00", "sources": [{"type": "one_time", "id": item["id"]}]}
    ]
    assert p["adjustments"] if "adjustments" in p else True
    bonus = next(e for e in p["earnings"] if e["code"] == "BONUS")
    assert bonus["amount"] == "5000.00" and bonus["monthly"] is None
    assert p["totals"]["gross_earnings"] == "30000.00"
    other = await payslip(client, hr, run["id"], "MGR001")
    assert other["inputs"]["components"] == [] and other["totals"]["gross_earnings"] == "40000.00"


async def test_refresh_pulls_new_and_drops_cancelled_items(client, payroll, uid):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    assert (await payslip(client, hr, run["id"]))["inputs"]["components"] == []
    a = await mk(client, hr, uid["employee"], label="A", amount="1000")
    await mk(client, hr, uid["employee"], code="OTHER_DEDUCTION", label="Fine", amount="200")
    r = await client.post(f"/api/admin/payroll/runs/{run['id']}/refresh", headers=hr)
    assert r.status_code == 200, r.text
    p = await payslip(client, hr, run["id"])
    assert {c["code"] for c in p["inputs"]["components"]} == {"BONUS", "OTHER_DEDUCTION"}
    assert any(d["code"] == "OTHER_DEDUCTION" and d["amount"] == "200.00" for d in p["deductions"])
    r = await client.post(f"{ITEMS}/{a['id']}/cancel", json={"reason": "Duplicate entry"}, headers=hr)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    await client.post(f"/api/admin/payroll/runs/{run['id']}/refresh", headers=hr)
    p = await payslip(client, hr, run["id"])
    assert [c["code"] for c in p["inputs"]["components"]] == ["OTHER_DEDUCTION"]


async def test_put_keeps_the_stored_components_and_rejects_client_components(client, payroll, uid):
    hr = payroll["hr"]
    await mk(client, hr, uid["employee"])
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    forged = [{"code": "BONUS", "label": "x", "amount": "999999.00", "sources": []}]
    r = await client.put(
        f"/api/admin/payroll/payslips/{p['id']}", json={"expected_version": p["version"], "components": forged}, headers=hr
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    for code in ("OVERTIME", "LEAVE_ENCASH"):
        r = await client.put(
            f"/api/admin/payroll/payslips/{p['id']}",
            json={"expected_version": p["version"], "adjustments": [{"code": code, "label": "x", "amount": "100"}]},
            headers=hr,
        )
        assert r.status_code == 422, code
    r = await client.put(
        f"/api/admin/payroll/payslips/{p['id']}", json={"expected_version": p["version"], "note": "kept"}, headers=hr
    )
    assert r.status_code == 200, r.text
    assert r.json()["inputs"]["components"] == p["inputs"]["components"]
    assert r.json()["totals"]["gross_earnings"] == "30000.00"


async def test_manual_overlap_warning(client, payroll, uid):
    hr = payroll["hr"]
    await mk(client, hr, uid["employee"], code="OTHER_DEDUCTION", label="System", amount="100")
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    assert "MANUAL_COMPONENT_OVERLAP" not in p["warnings"]
    r = await client.put(
        f"/api/admin/payroll/payslips/{p['id']}",
        json={"expected_version": p["version"], "adjustments": [{"code": "OTHER_DEDUCTION", "label": "Manual", "amount": "50"}]},
        headers=hr,
    )
    assert r.status_code == 200 and "MANUAL_COMPONENT_OVERLAP" in r.json()["warnings"]


# ================================================================================================ status flow
async def test_waiting_processing_paid_and_reopen(client, payroll, uid, db):
    hr = payroll["hr"]
    item = await mk(client, hr, uid["employee"])
    run = await new_run(client, hr)
    assert [r.status for r in await rows(db)] == ["pending"]

    r = await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=hr)
    assert r.status_code == 200, r.text
    p = await payslip(client, hr, run["id"])
    (row,) = await rows(db)
    assert row.status == "processing" and str(row.payslip_id) == p["id"]
    # processing rows cannot be edited or cancelled
    r = await client.put(f"{ITEMS}/{item['id']}", json={"expected_version": row.version, "amount": "1"}, headers=hr)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ITEM_LOCKED"
    r = await client.post(f"{ITEMS}/{item['id']}/cancel", json={"reason": "No longer needed"}, headers=hr)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ITEM_LOCKED"

    r = await client.post(f"/api/admin/payroll/runs/{run['id']}/reopen", headers=hr)
    assert r.status_code == 200, r.text
    (row,) = await rows(db)
    assert row.status == "pending" and row.payslip_id is None
    # editable again, and the change is pulled by the next process
    r = await client.put(f"{ITEMS}/{item['id']}", json={"expected_version": row.version, "amount": "7000"}, headers=hr)
    assert r.status_code == 200 and r.json()["amount"] == "7000.00" and r.json()["version"] == row.version + 1

    await process_and_publish(client, hr, run["id"])
    (row,) = await rows(db)
    p = await payslip(client, hr, run["id"])
    assert row.status == "paid" and str(row.payslip_id) == p["id"] and p["status"] == "published"
    assert next(e for e in p["earnings"] if e["code"] == "BONUS")["amount"] == "7000.00"
    r = await client.put(f"{ITEMS}/{item['id']}", json={"expected_version": row.version, "amount": "1"}, headers=hr)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ITEM_LOCKED"
    # the employee's own payslip shows the line like an adjustment
    emp = await client.post("/api/auth/login", json={"employee_id": "EMP001", "password": PASSWORD})
    eh = {"Authorization": f"Bearer {emp.json()['access_token']}"}
    own = (await client.get(f"/api/payslips/{p['id']}", headers=eh)).json()
    assert any(e["code"] == "BONUS" and e["amount"] == "7000.00" and e["monthly"] is None for e in own["earnings"])

    # append-only money history: create, edit, process (x2), reopen, pay
    ev = Counter(e.action for e in await events(db))
    assert ev == {"create": 1, "update": 1, "process": 2, "reopen": 1, "pay": 1}
    amounts = sorted(e.amount for e in await events(db) if e.action == "update")
    assert amounts == ["7000.00"] and next(e for e in await events(db) if e.action == "update").prev_amount == "5000.00"


async def test_held_payslip_stays_draft_and_its_items_stay_pending(client, payroll, uid, db):
    hr = payroll["hr"]
    await mk(client, hr, uid["employee"])
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    r = await client.put(
        f"/api/admin/payroll/payslips/{p['id']}", json={"expected_version": p["version"], "hold": True}, headers=hr
    )
    assert r.status_code == 200, r.text
    assert (await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=hr)).status_code == 200
    assert [r.status for r in await rows(db)] == ["pending"]
    assert (await payslip(client, hr, run["id"]))["status"] == "draft"


async def test_deleted_draft_payslip_and_deleted_run_release_nothing_wrongly(client, payroll, uid, db):
    hr = payroll["hr"]
    await mk(client, hr, uid["employee"])
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    assert (await client.delete(f"/api/admin/payroll/payslips/{p['id']}", headers=hr)).status_code == 204
    assert [r.status for r in await rows(db)] == ["pending"]
    assert (await client.delete(f"/api/admin/payroll/runs/{run['id']}", headers=hr)).status_code == 204
    assert [r.status for r in await rows(db)] == ["pending"]  # waits for the next run of that month
    run2 = await new_run(client, hr)
    assert len((await payslip(client, hr, run2["id"]))["inputs"]["components"]) == 1


async def test_single_payslip_process_pulls_new_items(client, payroll, uid, db):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    # a published month: new items need an open draft revision (P9)
    await mk(client, hr, uid["employee"], expect=409)
    r = await client.post(
        f"/api/admin/payroll/runs/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Bonus forgotten"}, headers=hr
    )
    assert r.status_code == 201, r.text
    rev = r.json()
    item = await mk(client, hr, uid["employee"])
    assert item["run_status"] == "published"
    # the revision does not know it yet; process (pull) does
    assert rev["inputs"]["components"] == []
    r = await client.post(f"/api/admin/payroll/payslips/{rev['id']}/process", headers=hr)
    assert r.status_code == 200, r.text
    assert r.json()["inputs"]["components"][0]["sources"] == [{"type": "one_time", "id": item["id"]}]
    (row,) = await rows(db)
    assert row.status == "processing" and str(row.payslip_id) == rev["id"]
    # reopen the payslip: back to pending
    assert (await client.post(f"/api/admin/payroll/payslips/{rev['id']}/reopen", headers=hr)).status_code == 200
    (row,) = await rows(db)
    assert row.status == "pending" and row.payslip_id is None


async def test_revision_carries_paid_rows_and_pulls_new_ones(client, payroll, uid, db):
    hr = payroll["hr"]
    first = await mk(client, hr, uid["employee"], label="First", amount="1000")
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    old = await payslip(client, hr, run["id"])
    r = await client.post(
        f"/api/admin/payroll/runs/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Correction"}, headers=hr
    )
    assert r.status_code == 201, r.text
    rev = r.json()
    # the paid row is carried into the draft revision (its amount still counts), nothing is double counted
    assert [c["sources"][0]["id"] for c in rev["inputs"]["components"]] == [first["id"]]
    assert rev["totals"]["gross_earnings"] == old["totals"]["gross_earnings"]
    second = await mk(client, hr, uid["employee"], label="Second", amount="250")
    assert (await client.post(f"/api/admin/payroll/payslips/{rev['id']}/process", headers=hr)).status_code == 200
    r = await client.post(f"/api/admin/payroll/payslips/{rev['id']}/publish", json={"current_password": PASSWORD}, headers=hr)
    assert r.status_code == 200, r.text
    by_id = {str(x.id): x for x in await rows(db)}
    assert {x.status for x in by_id.values()} == {"paid"}
    assert str(by_id[first["id"]].payslip_id) == rev["id"] == str(by_id[second["id"]].payslip_id)  # moved to the new copy
    assert Counter(e.action for e in await events(db) if e.action in ("carry", "pay"))["carry"] == 1
    final = (await client.get(f"/api/admin/payroll/payslips/{rev['id']}", headers=hr)).json()
    assert final["status"] == "published" and final["totals"]["gross_earnings"] == "26250.00"
    assert sorted(e["amount"] for e in final["earnings"] if e["code"] == "BONUS") == ["1000.00", "250.00"]


async def test_publish_refuses_when_a_pulled_row_changed(client, payroll, uid, db):
    hr = payroll["hr"]
    await mk(client, hr, uid["employee"])
    run = await new_run(client, hr)
    assert (await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=hr)).status_code == 200
    (row,) = await rows(db)
    row.status, row.payslip_id, row.cancel_reason = "cancelled", None, "forced for the test"  # bypass the service
    await db.commit()
    r = await publish_run(client, hr, run["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "COMPONENTS_CHANGED"
    assert (await payslip(client, hr, run["id"]))["status"] == "processed"  # nothing was published


async def test_period_closed_flag_and_period_exact_pull(client, payroll, uid):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    assert (await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=hr)).status_code == 200
    late = await mk(client, hr, uid["employee"], label="Late")  # processed run: allowed, lands after a reopen
    assert late["run_status"] == "processed" and late["flags"] == []
    assert (await publish_run(client, hr, run["id"])).status_code == 200
    r = await client.get(ITEMS, params={"period": PERIOD}, headers=hr)
    (listed,) = r.json()["items"]
    assert listed["status"] == "pending" and listed["run_status"] == "published" and listed["flags"] == ["PERIOD_CLOSED"]
    # nothing catches up automatically into another month
    nxt = await new_run(client, hr, "2025-07")
    assert (await payslip(client, hr, nxt["id"]))["inputs"]["components"] == []
    # HR moves it to July: pulled by the next refresh
    r = await client.put(f"{ITEMS}/{listed['id']}", json={"expected_version": 1, "period": "2025-07"}, headers=hr)
    assert r.status_code == 200 and r.json()["period"] == "2025-07" and r.json()["flags"] == []
    await client.post(f"/api/admin/payroll/runs/{nxt['id']}/refresh", headers=hr)
    assert len((await payslip(client, hr, nxt["id"]))["inputs"]["components"]) == 1


async def test_variance_reports_components_reason(client, payroll, uid):
    hr = payroll["hr"]
    prev = await new_run(client, hr, "2025-05")
    await process_and_publish(client, hr, prev["id"])
    await mk(client, hr, uid["employee"], period="2025-06")
    await new_run(client, hr, "2025-06")
    r = await client.get("/api/admin/payroll/variance", params={"period": "2025-06"}, headers=hr)
    assert r.status_code == 200, r.text
    reasons = {i["employee_id"]: i["reasons"] for i in r.json()["items"]}
    assert "COMPONENTS" in reasons["EMP001"] and "MGR001" not in reasons


async def test_settings_for_phase3a_are_validated(client, payroll):
    hr = payroll["hr"]
    r = await client.get("/api/admin/payroll/settings", headers=hr)
    s = r.json()["settings"]
    assert (s["payroll.loan_max_tenure_months"], s["payroll.loan_max_active"]) == (60, 3)
    assert (s["payroll.encashment_basis"], s["payroll.encashment_divisor"]) == ("basic", 30)
    for bad in ({"payroll.loan_max_tenure_months": 0}, {"payroll.loan_max_tenure_months": 121}, {"payroll.loan_max_active": 11},
                {"payroll.encashment_divisor": 32}, {"payroll.encashment_divisor": 30.5}, {"payroll.encashment_basis": "net"},
                {"payroll.loan_max_active": True}):  # fmt: skip
        r = await client.put("/api/admin/payroll/settings", json={"settings": bad}, headers=hr)
        assert r.status_code == 422, bad
    r = await client.put(
        "/api/admin/payroll/settings",
        json={"settings": {"payroll.encashment_basis": "gross", "payroll.loan_max_active": 5}},
        headers=hr,
    )
    assert r.status_code == 200 and r.json()["settings"]["payroll.encashment_basis"] == "gross"
    run = await new_run(client, hr)
    assert run["config"]["encashment_basis"] == "gross" and run["config"]["loan_max_active"] == 5  # snapshotted


# ================================================================================================ one-time item API
async def test_item_validation_and_unknown_fields(client, payroll, uid):
    hr = payroll["hr"]
    base = {"user_id": uid["employee"], "period": PERIOD, "code": "BONUS", "label": "Ok", "amount": "10.00"}
    bad = [
        {"amount": "0"}, {"amount": "-5"}, {"amount": 100}, {"amount": "1.234"}, {"amount": "10000000.01"}, {"label": ""},
        {"label": "x" * 41}, {"code": "BASIC"}, {"code": "LOAN_EMI"}, {"code": "OVERTIME"}, {"period": "2025-6"},
        {"period": "2025-13"}, {"extra": 1}, {"user_id": "nope"},
    ]  # fmt: skip
    for patch in bad:
        r = await client.post(ITEMS, json={**base, **patch}, headers=hr)
        assert r.status_code == 422, patch
    r = await client.post(ITEMS, json={**base, "period": "2099-01"}, headers=hr)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_PERIOD"
    r = await client.post(ITEMS, json={**base, "user_id": "00000000-0000-4000-8000-000000000000"}, headers=hr)
    assert r.status_code == 404
    # label is cleaned (markup stripped)
    r = await client.post(ITEMS, json={**base, "label": "<b>Diwali</b>  bonus"}, headers=hr)
    assert r.status_code == 201 and r.json()["label"] == "Diwali bonus"


async def test_item_rbac_self_rule_and_scope(client, payroll, uid, db):
    hr, emp = payroll["hr"], payroll["employee"]
    body = {"user_id": uid["employee"], "period": PERIOD, "code": "BONUS", "label": "x", "amount": "1"}
    assert (await client.post(ITEMS, json=body, headers=emp)).status_code == 403
    assert (await client.get(ITEMS, headers=emp)).status_code == 403
    assert (await client.get(ITEMS, headers=payroll["manager"])).status_code == 403
    assert (await client.post(ITEMS, json=body, headers={})).status_code == 401
    # nobody acts on their own money (HR cannot give HR a bonus; admin cannot touch own either)
    r = await client.post(ITEMS, json={**body, "user_id": uid["hr"]}, headers=hr)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    denied = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.denied"))).scalars().all()
    assert denied and denied[0].after["reason"] == "self" and denied[0].entity_type == "payroll_item"
    # a row of someone else: HR may edit, but a person cannot edit an item about themselves
    item = await mk(client, hr, uid["employee"])
    back = await client.put(
        f"/api/admin/payroll/employees/{uid['hr']}/details",
        json={"include_in_payroll": True, "current_password": PASSWORD},
        headers=payroll["admin"],
    )
    assert back.status_code == 200, back.text
    admin_item = await mk(client, payroll["admin"], uid["hr"])  # admin -> HR is fine
    r = await client.put(f"{ITEMS}/{admin_item['id']}", json={"expected_version": 1, "amount": "2"}, headers=hr)
    assert r.status_code == 403
    r = await client.post(f"{ITEMS}/{admin_item['id']}/cancel", json={"reason": "not mine"}, headers=hr)
    assert r.status_code == 403
    assert (await client.get(ITEMS, headers=payroll["admin"])).status_code == 200
    assert item["id"] != admin_item["id"]


async def test_item_version_conflict_cap_and_filters(client, payroll, uid):
    hr = payroll["hr"]
    first = await mk(client, hr, uid["employee"], label="n0")
    r = await client.put(f"{ITEMS}/{first['id']}", json={"expected_version": 1, "label": "renamed"}, headers=hr)
    assert r.status_code == 200 and r.json()["version"] == 2
    r = await client.put(f"{ITEMS}/{first['id']}", json={"expected_version": 1, "label": "stale"}, headers=hr)
    assert r.status_code == 409 and r.json()["error"]["code"] == "VERSION_CONFLICT"
    assert (await client.put(f"{ITEMS}/{first['id']}", json={"expected_version": 2}, headers=hr)).status_code == 422
    assert (
        await client.put(f"{ITEMS}/{first['id']}", json={"expected_version": 2, "amount": "0"}, headers=hr)
    ).status_code == 422
    for i in range(1, 20):
        await mk(client, hr, uid["employee"], label=f"n{i}", amount="1")
    r = await client.post(
        ITEMS, json={"user_id": uid["employee"], "period": PERIOD, "code": "BONUS", "label": "21st", "amount": "1"}, headers=hr
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "TOO_MANY_ITEMS"
    # a cancelled item frees a slot
    await client.post(f"{ITEMS}/{first['id']}/cancel", json={"reason": "free the slot"}, headers=hr)
    await mk(client, hr, uid["employee"], label="21st", amount="1")
    # moving an item into a full month is refused too
    other = await mk(client, hr, uid["employee"], period="2025-07", label="m")
    r = await client.put(f"{ITEMS}/{other['id']}", json={"expected_version": 1, "period": PERIOD}, headers=hr)
    assert r.status_code == 409 and r.json()["error"]["code"] == "TOO_MANY_ITEMS"
    # filters and paging
    r = await client.get(ITEMS, params={"period": PERIOD, "status": "cancelled"}, headers=hr)
    assert [i["id"] for i in r.json()["items"]] == [first["id"]]
    r = await client.get(ITEMS, params={"user_id": uid["manager"]}, headers=hr)
    assert r.json()["total"] == 0
    r = await client.get(ITEMS, params={"period": PERIOD, "page_size": 5, "page": 2}, headers=hr)
    assert r.json()["total"] == 21 and len(r.json()["items"]) == 5
    assert (await client.get(ITEMS, params={"code": "BASIC"}, headers=hr)).status_code == 422


async def test_item_audit_has_no_money_or_labels_and_values_are_encrypted_at_rest(client, payroll, uid, db):
    hr = payroll["hr"]
    item = await mk(client, hr, uid["employee"], label="SecretLabelXYZ", amount="123456.78")
    await client.put(f"{ITEMS}/{item['id']}", json={"expected_version": 1, "amount": "222222.22"}, headers=hr)
    await client.post(f"{ITEMS}/{item['id']}/cancel", json={"reason": "SecretReasonXYZ"}, headers=hr)
    logs = (await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.item.%")))).scalars().all()
    assert {entry.action for entry in logs} == {"payroll.item.create", "payroll.item.update", "payroll.item.cancel"}
    blob = " ".join(str(entry.before) + str(entry.after) for entry in logs)
    for secret in ("123456", "222222", "SecretLabelXYZ", "SecretReasonXYZ"):
        assert secret not in blob
    raw = (
        await db.execute(
            __import__("sqlalchemy").text("SELECT label_enc, amount_enc, cancel_reason_enc FROM payroll_one_time_items")
        )
    ).one()
    for value in raw:
        assert b"SecretLabelXYZ" not in bytes(value) and b"123456" not in bytes(value) and b"SecretReason" not in bytes(value)
    ev = await events(db)
    assert {e.action for e in ev} == {"create", "update", "cancel"}
    assert next(e for e in ev if e.action == "cancel").detail == {"reason": "SecretReasonXYZ"}


async def test_cancel_needs_a_reason_and_only_pending(client, payroll, uid):
    hr = payroll["hr"]
    item = await mk(client, hr, uid["employee"])
    for body in ({}, {"reason": ""}, {"reason": "ab"}, {"reason": "x" * 201}, {"reason": "ok reason", "extra": 1}):
        assert (await client.post(f"{ITEMS}/{item['id']}/cancel", json=body, headers=hr)).status_code == 422, body
    assert (await client.post(f"{ITEMS}/{item['id']}/cancel", json={"reason": "valid reason"}, headers=hr)).status_code == 200
    r = await client.post(f"{ITEMS}/{item['id']}/cancel", json={"reason": "valid reason"}, headers=hr)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ITEM_LOCKED"
    assert (
        await client.post(f"{ITEMS}/00000000-0000-4000-8000-000000000000/cancel", json={"reason": "valid"}, headers=hr)
    ).status_code == 404


async def test_ineligible_employee_is_refused(client, payroll, uid):
    hr = payroll["hr"]
    # HR001 was excluded from payroll by the fixture; admin tries to give HR an item
    r = await client.post(
        ITEMS,
        json={"user_id": uid["hr"], "period": PERIOD, "code": "BONUS", "label": "x", "amount": "1"},
        headers=payroll["admin"],
    )
    assert r.status_code == 422, r.text
    assert hr  # silence unused


async def test_key_rotation_reencrypts_items_and_events_and_keeps_them_readable(client, payroll, uid, db, monkeypatch):
    """Plan 15 s.5: the new tables are covered by services.key_rotation (discovered from the models)."""
    import os

    from cryptography.fernet import Fernet
    from sqlalchemy import text

    from app.core import crypto
    from app.services import key_rotation

    hr = payroll["hr"]
    item = await mk(client, hr, uid["employee"], label="Rotating", amount="4321.00")
    old = os.environ["DATA_ENCRYPTION_KEYS"]
    new = Fernet.generate_key().decode()
    try:
        monkeypatch.setenv("DATA_ENCRYPTION_KEYS", f"{new},{old}")
        crypto.reset_crypto_cache()
        assert (await key_rotation.verify_all(db))["failed"] > 0
        stats = await key_rotation.rotate_all(db)
        assert stats["rotated"] > 0 and stats["undecryptable"] == 0
        assert (await key_rotation.verify_all(db))["failed"] == 0
        monkeypatch.setenv("DATA_ENCRYPTION_KEYS", new)  # the old key is dropped
        crypto.reset_crypto_cache()
        got = await client.get(ITEMS, params={"period": PERIOD}, headers=hr)
        assert got.status_code == 200 and got.json()["items"][0]["amount"] == "4321.00"
        raw = (await db.execute(text("SELECT amount_enc FROM payroll_one_time_items"))).scalar_one()
        assert Fernet(new.encode()).decrypt(bytes(raw)) == b"4321.00"
        ev = (await db.execute(text("SELECT amount_enc FROM payroll_item_events"))).scalars().all()
        assert ev and all(Fernet(new.encode()).decrypt(bytes(x)) == b"4321.00" for x in ev)
        assert item["id"]
    finally:
        monkeypatch.setenv("DATA_ENCRYPTION_KEYS", old)
        crypto.reset_crypto_cache()
