"""Plan 15 work package 3a, step 2: auto arrears (proposal, recompute in place, accept with step-up, engine integration)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.models import AuditLog, PayrollArrears, PayrollItemEvent
from app.services import payroll_calc as calc
from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, add_structure, new_run, process_and_publish, publish_run, row_for, set_setting

ARREARS = "/api/admin/payroll/arrears"
RUNS = "/api/admin/payroll/runs"


@pytest.fixture(autouse=True)
def _relaxed_limits(monkeypatch):
    from app.services import admin_security
    from app.services import payroll_common as pc

    async def no_limit(*a, **k):
        return None

    monkeypatch.setattr(pc, "limit", no_limit)
    monkeypatch.setattr(admin_security, "REAUTH_PER_MINUTE", 10_000)


def err(r):
    return r.json()["error"]["code"]


async def proposals(client, hdr, user_id=None, status=None):
    params = {k: v for k, v in (("user_id", user_id), ("status", status)) if v}
    r = await client.get(ARREARS, params=params, headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def published_june(client, hr):
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    return run


async def payslip(client, hdr, run_id, emp="EMP001"):
    row = await row_for(client, hdr, run_id, emp)
    r = await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


async def accept(client, hdr, a, period="2025-07", expect=200, **kw):
    body = {"period": period, "expected_version": a["version"], "current_password": PASSWORD}
    body.update(kw)
    r = await client.post(f"{ARREARS}/{a['id']}/accept", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


# ================================================================================================ pure
def test_structure_earnings_matches_calculate_lines():
    data = calc.StructureData(
        basic=D("20000"), hra=D("5000"),
        allowances=(calc.Allowance("TRAVEL", "Travel", D("1500"), prorate=False), calc.Allowance("SPL", "Special", D("1001"))),
    )  # fmt: skip
    for rounding in ("rupee", "paise"):
        rules = calc.Rules(rounding=rounding, employer_share_from_employee=False)
        for paid in (D("30"), D("27"), D("12.5"), D("0")):
            basic, total = calc.structure_earnings(data, rules, D("30"), paid)
            res = calc.calculate(data, rules, __import__("datetime").date(2025, 6, 1), D("30"), paid, calc.PayInputs())
            lines = [e for e in res.earnings if e["monthly"] is not None]
            assert total == sum((D(e["amount"]) for e in lines), D("0")).quantize(D("0.01")), (rounding, paid)
            assert basic == D(next(e["amount"] for e in lines if e["code"] == "BASIC"))


# ================================================================================================ proposal
async def test_structure_in_a_paid_month_creates_one_proposal_with_breakdown(client, payroll, uid):
    hr = payroll["hr"]
    assert await proposals(client, hr) == []
    await published_june(client, hr)
    s = await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    items = await proposals(client, hr)
    assert len(items) == 1
    a = items[0]
    assert (a["code"], a["amount"], a["status"], a["period"], a["run_status"], a["version"]) == (
        "ARREARS", "4000.00", "proposed", None, None, 1
    )  # fmt: skip
    assert (a["from_period"], a["to_period"]) == ("2025-06", "2025-06")
    assert a["user"]["employee_id"] == "EMP001" and a["trigger_structure"] == {"id": s["id"], "effective_from": "2025-06-01"}
    (b,) = a["breakdown"]
    assert (b["period"], b["paid_days"], b["paid_earnings"], b["new_earnings"], b["already_paid_arrears"]) == (
        "2025-06", 30.0, "25000.00", "29000.00", "0.00"
    )  # fmt: skip
    assert (b["diff"], b["basic_diff"]) == ("4000.00", "4000.00")
    detail = await client.get(f"{ARREARS}/{a['id']}", headers=hr)
    assert detail.status_code == 200 and detail.json()["breakdown"] == a["breakdown"]


async def test_no_proposal_when_the_structure_does_not_reach_a_paid_month(client, payroll, uid):
    hr = payroll["hr"]
    await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="30000", hra="5000", eff="2025-09-01")  # after the last paid month
    assert await proposals(client, hr) == []
    r = await client.post(f"/api/admin/payroll/employees/{uid['employee']}/arrears/recalculate", headers=hr)
    assert r.status_code == 200 and r.json() == {"arrears": None}  # June used the structure still in force for June


async def test_second_change_recomputes_the_same_row_in_place(client, payroll, uid):
    hr = payroll["hr"]
    await published_june(client, hr)
    s = await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    first = (await proposals(client, hr))[0]
    body = {
        "effective_from": "2025-06-01", "basic": "26000", "hra": "5000", "allowances": [], "fixed_deductions": [],
        "tds_monthly": "0", "pf_applicable": True, "pf_on_full_basic": False, "esi_applicable": True, "pt_applicable": True,
    }  # fmt: skip
    r = await client.put(f"/api/admin/payroll/structures/{s['id']}", json=body, headers=hr)
    assert r.status_code == 200, r.text
    items = await proposals(client, hr)
    assert len(items) == 1 and items[0]["id"] == first["id"]
    assert (items[0]["amount"], items[0]["version"]) == ("6000.00", 2)


async def test_deleting_the_structure_dismisses_the_proposal_by_the_system(client, payroll, uid, db):
    hr = payroll["hr"]
    await published_june(client, hr)
    s = await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    assert len(await proposals(client, hr, status="proposed")) == 1
    assert (await client.delete(f"/api/admin/payroll/structures/{s['id']}", headers=hr)).status_code == 204
    assert await proposals(client, hr, status="proposed") == []
    gone = await proposals(client, hr, status="dismissed")
    assert len(gone) == 1
    db.expire_all()
    ev = [(e.action, e.actor_id) for e in (await db.execute(select(PayrollItemEvent))).scalars() if e.item_type == "arrears"]
    assert ev == [("propose", ev[0][1]), ("dismiss", None)]  # the system (no actor) dismissed it


async def test_lower_structure_is_a_salary_recovery(client, payroll, uid):
    hr = payroll["hr"]
    await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="19000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    assert (a["code"], a["amount"]) == ("OTHER_DEDUCTION", "1000.00")
    assert a["breakdown"][0]["diff"] == "-1000.00"


async def test_a_difference_under_one_rupee_makes_no_proposal(client, payroll, uid):
    hr = payroll["hr"]
    await set_setting(client, payroll["admin"], rounding="paise")
    await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="20000.60", hra="5000", eff="2025-06-01")
    assert await proposals(client, hr) == []


async def test_lop_of_the_paid_month_is_honoured(client, payroll, uid):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    p = await payslip(client, hr, run["id"])
    r = await client.put(
        f"/api/admin/payroll/payslips/{p['id']}",
        json={"expected_version": p["version"], "lop_override": 3, "lop_override_reason": "Unpaid leave"}, headers=hr,
    )  # fmt: skip
    assert r.status_code == 200, r.text
    await process_and_publish(client, hr, run["id"])
    await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    b = a["breakdown"][0]
    assert b["paid_days"] == 27.0 and b["paid_earnings"] == "22500.00" and b["new_earnings"] == "26100.00"  # x 27/30
    assert a["amount"] == "3600.00"


async def test_several_published_months_add_up(client, payroll, uid):
    hr = payroll["hr"]
    await published_june(client, hr)
    run2 = await new_run(client, hr, "2025-07")
    await process_and_publish(client, hr, run2["id"])
    await add_structure(client, hr, uid["employee"], basic="22000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    assert [b["period"] for b in a["breakdown"]] == ["2025-06", "2025-07"]
    assert a["amount"] == "4000.00" and (a["from_period"], a["to_period"]) == ("2025-06", "2025-07")


# ================================================================================================ accept / dismiss
async def test_accept_needs_password_current_version_and_an_open_month(client, payroll, uid):
    hr = payroll["hr"]
    await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    r = await client.post(f"{ARREARS}/{a['id']}/accept", json={"period": "2025-07", "expected_version": 1}, headers=hr)
    assert r.status_code == 422
    await accept(client, hr, a, expect=403, current_password="wrong-pass-word")
    await accept(client, hr, a, expect=409, expected_version=9)  # ARREARS_STALE
    r = await client.post(f"{ARREARS}/{a['id']}/accept", headers=hr,
                          json={"period": "2025-07", "expected_version": 9, "current_password": PASSWORD})  # fmt: skip
    assert err(r) == "ARREARS_STALE"
    r = await client.post(f"{ARREARS}/{a['id']}/accept", headers=hr,
                          json={"period": PERIOD, "expected_version": 1, "current_password": PASSWORD})  # fmt: skip
    assert r.status_code == 409 and err(r) == "PERIOD_LOCKED"  # June is published
    r = await client.post(f"{ARREARS}/{a['id']}/accept", headers=hr,
                          json={"period": "2040-01", "expected_version": 1, "current_password": PASSWORD})  # fmt: skip
    assert r.status_code == 422 and err(r) == "INVALID_PERIOD"
    done = await accept(client, hr, a)
    assert (done["status"], done["period"], done["version"], done["run_status"]) == ("pending", "2025-07", 2, "none")
    r = await client.post(f"{ARREARS}/{a['id']}/accept", headers=hr,
                          json={"period": "2025-08", "expected_version": 2, "current_password": PASSWORD})  # fmt: skip
    assert r.status_code == 409 and err(r) == "NOT_PROPOSED"


async def test_stale_accept_after_a_recalculation(client, payroll, uid):
    hr = payroll["hr"]
    await published_june(client, hr)
    s = await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    body = {
        "effective_from": "2025-06-01", "basic": "25000", "hra": "5000", "allowances": [], "fixed_deductions": [],
        "tds_monthly": "0", "pf_applicable": True, "pf_on_full_basic": False, "esi_applicable": True, "pt_applicable": True,
    }  # fmt: skip
    assert (await client.put(f"/api/admin/payroll/structures/{s['id']}", json=body, headers=hr)).status_code == 200
    r = await client.post(f"{ARREARS}/{a['id']}/accept", headers=hr,
                          json={"period": "2025-07", "expected_version": a["version"], "current_password": PASSWORD})  # fmt: skip
    assert r.status_code == 409 and err(r) == "ARREARS_STALE"


async def test_accepted_row_goes_back_to_proposed_when_a_later_change_alters_it(client, payroll, uid):
    hr = payroll["hr"]
    await published_june(client, hr)
    s = await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    await accept(client, hr, a)
    body = {
        "effective_from": "2025-06-01", "basic": "25000", "hra": "5000", "allowances": [], "fixed_deductions": [],
        "tds_monthly": "0", "pf_applicable": True, "pf_on_full_basic": False, "esi_applicable": True, "pt_applicable": True,
    }  # fmt: skip
    assert (await client.put(f"/api/admin/payroll/structures/{s['id']}", json=body, headers=hr)).status_code == 200
    (b,) = await proposals(client, hr)
    assert (b["id"], b["status"], b["period"], b["amount"], b["version"]) == (a["id"], "proposed", None, "5000.00", 3)


async def test_dismiss(client, payroll, uid):
    hr = payroll["hr"]
    await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    r = await client.post(f"{ARREARS}/{a['id']}/dismiss", json={"reason": "x"}, headers=hr)
    assert r.status_code == 422
    r = await client.post(f"{ARREARS}/{a['id']}/dismiss", json={"reason": "Agreed to waive"}, headers=hr)
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
    r = await client.post(f"{ARREARS}/{a['id']}/dismiss", json={"reason": "Agreed to waive"}, headers=hr)
    assert r.status_code == 409 and err(r) == "NOT_PROPOSED"
    r = await client.post(f"{ARREARS}/00000000-0000-0000-0000-000000000001/dismiss", json={"reason": "Nope nope"}, headers=hr)
    assert r.status_code == 404


async def test_recalculate_proposes_after_the_fact_and_respects_the_self_rule(client, payroll, uid):
    hr = payroll["hr"]
    r = await client.post(f"/api/admin/payroll/employees/{uid['hr']}/arrears/recalculate", headers=hr)
    assert r.status_code == 403 and err(r) == "FORBIDDEN"
    r = await client.post("/api/admin/payroll/employees/00000000-0000-0000-0000-000000000001/arrears/recalculate", headers=hr)
    assert r.status_code == 404
    await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    r = await client.post(f"/api/admin/payroll/employees/{uid['employee']}/arrears/recalculate", headers=hr)
    assert r.status_code == 200 and r.json()["arrears"]["amount"] == "4000.00" and r.json()["arrears"]["version"] == 1


async def test_permissions(client, payroll, uid):
    emp = payroll["employee"]
    for method, path in (
        ("GET", ARREARS), ("GET", f"{ARREARS}/{uid['employee']}"), ("POST", f"{ARREARS}/{uid['employee']}/accept"),
        ("POST", f"{ARREARS}/{uid['employee']}/dismiss"), ("POST", f"/api/admin/payroll/employees/{uid['employee']}/arrears/recalculate"),
    ):  # fmt: skip
        r = await client.request(method, path, headers=emp, json={} if method == "POST" else None)
        assert r.status_code == 403, (method, path)


# ================================================================================================ engine integration
async def test_accepted_arrears_flow_through_process_reopen_publish_and_are_subtracted_after(client, payroll, uid, db):
    hr = payroll["hr"]
    await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)

    # while the proposal is open, July payslips carry the warning
    run2 = await new_run(client, hr, "2025-07")
    p = await payslip(client, hr, run2["id"])
    assert "ARREARS_PROPOSED" in p["warnings"] and p["inputs"]["components"] == []

    await accept(client, hr, a, period="2025-07")
    assert (await client.post(f"{RUNS}/{run2['id']}/refresh", headers=hr)).status_code == 200
    p = await payslip(client, hr, run2["id"])
    assert "ARREARS_PROPOSED" not in p["warnings"]
    (c,) = p["inputs"]["components"]
    assert (c["code"], c["label"], c["amount"]) == ("ARREARS", "Arrears Jun 2025", "4000.00")
    assert c["sources"] == [{"type": "arrears", "id": a["id"]}]
    earn = {e["code"]: e for e in p["earnings"]}
    assert earn["ARREARS"]["amount"] == "4000.00" and earn["ARREARS"]["monthly"] is None
    # arrears are neither PF nor ESI wages: PF stays 12% of the (new) basic
    assert next(d for d in p["deductions"] if d["code"] == "PF")["amount"] == "1800.00"

    assert (await client.post(f"{RUNS}/{run2['id']}/process", headers=hr)).status_code == 200
    cur = (await client.get(f"{ARREARS}/{a['id']}", headers=hr)).json()
    assert (cur["status"], cur["run_status"]) == ("processing", "processed") and cur["payslip_id"] == p["id"]
    r = await client.post(f"{ARREARS}/{a['id']}/dismiss", json={"reason": "Too late now"}, headers=hr)
    assert r.status_code == 409 and err(r) == "ITEM_LOCKED"

    assert (await client.post(f"{RUNS}/{run2['id']}/reopen", headers=hr)).status_code == 200
    assert (await client.get(f"{ARREARS}/{a['id']}", headers=hr)).json()["status"] == "pending"
    assert (await client.post(f"{RUNS}/{run2['id']}/process", headers=hr)).status_code == 200
    assert (await publish_run(client, hr, run2["id"])).status_code == 200
    paid = (await client.get(f"{ARREARS}/{a['id']}", headers=hr)).json()
    assert paid["status"] == "paid"

    # what was paid is subtracted: no second proposal
    r = await client.post(f"/api/admin/payroll/employees/{uid['employee']}/arrears/recalculate", headers=hr)
    assert r.json() == {"arrears": None}
    assert [x["id"] for x in await proposals(client, hr)] == [a["id"]]
    # ... and a further raise only proposes the new difference
    await add_structure(client, hr, uid["employee"], basic="25000", hra="5000", eff="2025-07-01")
    (n,) = await proposals(client, hr, status="proposed")
    assert n["amount"] == "1000.00" and n["breakdown"][0]["period"] == "2025-07"
    assert n["breakdown"][0]["paid_earnings"] == "29000.00" and n["breakdown"][0]["new_earnings"] == "30000.00"
    db.expire_all()
    actions = [
        e.action for e in (await db.execute(select(PayrollItemEvent))).scalars() if e.item_id.hex == a["id"].replace("-", "")
    ]
    assert actions == ["propose", "accept", "process", "reopen", "process", "pay"]


async def test_negative_arrears_become_a_deduction_line(client, payroll, uid):
    hr = payroll["hr"]
    await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="19000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    await accept(client, hr, a, period="2025-07")
    run2 = await new_run(client, hr, "2025-07")
    p = await payslip(client, hr, run2["id"])
    (c,) = p["inputs"]["components"]
    assert (c["code"], c["label"], c["amount"]) == ("OTHER_DEDUCTION", "Salary recovery (arrears)", "1000.00")
    assert any(d["code"] == "OTHER_DEDUCTION" and d["amount"] == "1000.00" for d in p["deductions"])


async def test_audit_has_no_amounts(client, payroll, uid, db):
    hr = payroll["hr"]
    await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    await client.get(f"{ARREARS}/{a['id']}", headers=hr)
    await accept(client, hr, a)
    db.expire_all()
    rows = [x for x in (await db.execute(select(AuditLog))).scalars() if x.action.startswith("payroll.arrears")]
    assert {x.action for x in rows} >= {"payroll.arrears.propose", "payroll.arrears.view", "payroll.arrears.accept"}
    blob = " ".join(f"{x.before}{x.after}" for x in rows)
    for secret in ("4000", "29000", "25000"):
        assert secret not in blob


async def test_arrears_are_stored_encrypted(db, client, payroll, uid):
    hr = payroll["hr"]
    await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    from sqlalchemy import LargeBinary, type_coerce

    raw = (await db.execute(select(type_coerce(PayrollArrears.amount, LargeBinary)))).scalars().first()
    assert raw is not None and b"4000" not in bytes(raw)
    raw = (await db.execute(select(type_coerce(PayrollArrears.breakdown, LargeBinary)))).scalars().first()
    assert b"29000" not in bytes(raw)


# ================================================================================================ H1 (security review 3a)
async def revise_and_publish(client, hr, run_id, user_id):
    r = await client.post(f"{RUNS}/{run_id}/employees/{user_id}/revise", json={"reason": "Adopt the new structure"}, headers=hr)
    assert r.status_code == 201, r.text
    rev = r.json()
    r = await client.post(f"/api/admin/payroll/payslips/{rev['id']}/process", headers=hr)
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/admin/payroll/payslips/{rev['id']}/publish", json={"current_password": PASSWORD}, headers=hr)
    assert r.status_code == 200, r.text
    return rev


async def test_revising_a_month_after_its_arrears_were_paid_proposes_the_recovery(client, payroll, uid):
    hr = payroll["hr"]
    june = await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    july = await new_run(client, hr, "2025-07")
    await accept(client, hr, a, period="2025-07")
    await process_and_publish(client, hr, july["id"])  # 4000.00 paid as arrears
    assert (await client.get(f"{ARREARS}/{a['id']}", headers=hr)).json()["status"] == "paid"

    await revise_and_publish(client, hr, june["id"], uid["employee"])  # June now pays the new structure itself
    assert (await payslip(client, hr, june["id"]))["totals"]["gross_earnings"] == "29000.00"
    (rec,) = await proposals(client, hr, status="proposed")  # paying 4000 again would be a double payment: recover it
    assert (rec["code"], rec["amount"]) == ("OTHER_DEDUCTION", "4000.00")
    (b,) = rec["breakdown"]
    assert (b["paid_earnings"], b["new_earnings"], b["already_paid_arrears"], b["diff"]) == (
        "29000.00", "29000.00", "4000.00", "-4000.00"
    )  # fmt: skip


async def test_revision_published_while_arrears_are_accepted_but_unpaid_pays_nothing_twice(client, payroll, uid):
    hr = payroll["hr"]
    june = await published_june(client, hr)
    await add_structure(client, hr, uid["employee"], basic="24000", hra="5000", eff="2025-06-01")
    (a,) = await proposals(client, hr)
    july = await new_run(client, hr, "2025-07")
    await accept(client, hr, a, period="2025-07")  # pending in July
    await revise_and_publish(client, hr, june["id"], uid["employee"])
    # nothing is left to pay: the accepted row is gone from the payable set
    assert await proposals(client, hr, status="pending") == []
    assert (await client.post(f"{RUNS}/{july['id']}/refresh", headers=hr)).status_code == 200
    p = await payslip(client, hr, july["id"])
    assert p["inputs"]["components"] == []
    await process_and_publish(client, hr, july["id"])
    assert (await payslip(client, hr, july["id"]))["totals"]["gross_earnings"] == "29000.00"


# ================================================================================================ L1, L2, L7, L8 (security review 3a)
def test_providers_are_in_the_lock_order_whatever_the_import_order():
    from app.services import payroll_components as comps

    comps.ensure_providers()
    assert [p.type for p in comps.PROVIDERS] == [
        "one_time",
        "loan_installment",
        "arrears",
        "encashment",
        "reimbursement",
        "overtime",
    ]
    assert [p.order for p in comps.PROVIDERS] == sorted(p.order for p in comps.PROVIDERS)
    saved = list(comps.PROVIDERS)
    try:
        comps.PROVIDERS.reverse()
        comps.register(comps.Provider("zz_test", PayrollArrears, lambda rows: [], order=5))
        assert [p.type for p in comps.PROVIDERS][0] == "zz_test"  # a new source is placed by its order, not appended
        comps.PROVIDERS[:] = saved
        comps.PROVIDERS.reverse()
        comps.register(comps.Provider("zz_test2", PayrollArrears, lambda rows: [], order=70))
        assert [p.type for p in comps.PROVIDERS][-1] == "zz_test2"
    finally:
        comps.PROVIDERS[:] = saved


async def test_item_create_and_import_lock_the_user_rows_after_the_run(client, payroll, uid, monkeypatch):
    from app.services import payroll_items as items_svc

    calls = []
    real = items_svc._lock_users

    async def spy(session, ids):
        calls.append(sorted(ids))
        return await real(session, ids)

    monkeypatch.setattr(items_svc, "_lock_users", spy)
    body = {"user_id": uid["employee"], "period": "2025-07", "code": "BONUS", "label": "Diwali", "amount": "10"}
    assert (await client.post("/api/admin/payroll/items", json=body, headers=payroll["hr"])).status_code == 201
    assert calls == [[__import__("uuid").UUID(uid["employee"])]]


def test_encashment_amount_is_capped_at_the_money_ceiling():
    from app.core.errors import AppError
    from app.services import payroll_encashment as enc

    assert enc.checked_amount(D("1"), D("100.00")) == D("100.00")
    with pytest.raises(AppError) as e:
        enc.checked_amount(D("20"), calc.MAX_MONEY)
    assert e.value.code == "VALIDATION_ERROR"


def test_loan_installment_numbers_follow_the_live_installments():
    """Migration 0023: numbers are unique among LIVE installments only, so skipped / cancelled rows never use them up."""
    import datetime as dt
    import uuid

    from app.services import payroll_loans as loans

    loan = type("L", (), {"id": uuid.uuid4(), "user_id": uuid.uuid4()})()

    def inst(seq, status):
        return type("I", (), {"seq": seq, "status": status})()

    assert loans._next_seq([]) == 1
    rows = [inst(1, "paid"), inst(2, "skipped"), inst(3, "cancelled"), inst(2, "pending")]
    assert loans._next_seq(rows) == 3  # 3 is cancelled and 2 is live
    rows = [inst(n, "cancelled") for n in range(1, 400)] + [inst(4, "pending")]
    assert loans._next_seq(rows) == 5
    assert loans._new_inst(loan, uuid.uuid4(), 500, dt.date(2025, 7, 1), D("1")).seq == 500  # no 120 ceiling any more


async def test_list_and_quote_reads_are_audited_with_counts_only(client, payroll, uid, db):
    hr = payroll["hr"]
    base = "/api/admin/payroll"
    for path in ("/items", "/items/batches", "/loans", "/arrears", "/encashments"):
        assert (await client.get(base + path, headers=hr)).status_code == 200, path
    db.expire_all()
    rows = {x.action: x for x in (await db.execute(select(AuditLog))).scalars() if x.action.endswith(".list")}
    assert set(rows) >= {
        "payroll.item.list", "payroll.import.list", "payroll.loan.list", "payroll.arrears.list", "payroll.encashment.list",
    }  # fmt: skip
    assert all(x.after == {"count": 0} for x in rows.values())
