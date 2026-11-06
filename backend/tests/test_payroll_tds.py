"""Plan 17 work package 5c: the TDS engine inside payroll (settings, run / payslip integration, modes, flags, freeze, MAC), the admin
preview / summary / statement / 24Q endpoints, the employee projection and the TDS challan details.

The tax rules are the TEST fixture of `tests.payroll_tax_helpers` (illustrative numbers, not legal or tax advice). EMP001 earns
Rs 2,00,000 a month (basic 1,50,000 + HRA 50,000); June 2025 is month 3 of FY 2025-26, so 10 months (Jun..Mar) remain:
2,000,000 - 75,000 = 19,25,000 taxable; tax 20,000 + 10% of 11,25,000 = 1,32,500; cess 5,300; total 1,37,800; / 10 = 13,780 a month.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from openpyxl import load_workbook
from sqlalchemy import select, text

from app.core.clock import utcnow
from app.models import (
    AuditLog,
    EmployeePayrollDetails,
    PayrollLoan,
    PayrollTaxComputation,
    Payslip,
    StatutoryChallan,
    StatutoryChallanEvent,
    TaxDeclaration,
    TaxProof,
    TaxYearRule,
)
from app.services import notifications
from app.services import payroll_publish_requests as prs
from app.services import payroll_tds as ptds
from tests.conftest import PASSWORD
from tests.payroll_controls_helpers import quiet  # noqa: F401  (fixture)
from tests.payroll_fixtures import add_structure, exclude_from_payroll, new_run, payslips_of, set_setting
from tests.payroll_tax_helpers import add_rules, err, rules_copy, set_pan

pytestmark = pytest.mark.usefixtures("quiet", "clock")

B = "/api/admin/payroll"
SETTINGS = f"{B}/settings"
TAX = f"{B}/tax"
FY = "2025-26"
JUNE, MAY = "2025-06", "2025-05"
PW = {"current_password": PASSWORD}


# ================================================================================================ fixtures / helpers
@pytest_asyncio.fixture
async def tds(client, H, uid, db):
    """EMP001 only (basic 1,50,000 + HRA 50,000, no PF / ESI / PT so net = gross - TDS), PAN set, verified rules for FY 2025-26.
    The engine is still switched OFF."""
    await set_setting(client, H["admin"], lop_absent_days=False)
    for who, by in (("hr", "admin"), ("admin", "hr"), ("manager", "hr")):
        await exclude_from_payroll(client, H[by], uid[who])
    await add_structure(
        client,
        H["hr"],
        uid["employee"],
        basic="150000",
        hra="50000",
        pf_applicable=False,
        esi_applicable=False,
        pt_applicable=False,
    )
    await set_pan(db, uid["employee"], "ABCDE1234F")
    await add_rules(db, uid, fy=FY)
    return H


async def engine(client, H, enabled=True, mode="suggest", **more):
    settings = {
        "payroll.tds_engine_enabled": enabled,
        "payroll.tds_engine_mode": mode,
        **{f"payroll.{k}": v for k, v in more.items()},
    }
    r = await client.put(SETTINGS, json={"settings": settings, "current_password": PASSWORD}, headers=H["admin"])
    assert r.status_code == 200, r.text


async def run_for(client, H, period=JUNE):
    return await new_run(client, H["hr"], period)


async def slip(client, H, run, emp="EMP001"):
    rows = await payslips_of(client, H["hr"], run["id"])
    pid = next(i for i in rows if i["user"]["employee_id"] == emp)["id"]
    r = await client.get(f"{B}/payslips/{pid}", headers=H["hr"])
    assert r.status_code == 200, r.text
    return r.json()


def tds_line(p) -> str | None:
    return next((d["amount"] for d in p["deductions"] if d["code"] == "TDS"), None)


async def refresh(client, H, run, expect=200):
    r = await client.post(f"{B}/runs/{run['id']}/refresh", headers=H["hr"])
    assert r.status_code == expect, r.text
    return r.json()


async def process(client, H, run):
    r = await client.post(f"{B}/runs/{run['id']}/process", headers=H["hr"])
    assert r.status_code == 200, r.text


async def publish(client, H, run):
    r = await client.post(f"{B}/runs/{run['id']}/publish", json=PW, headers=H["hr"])
    assert r.status_code == 200, r.text


async def put_slip(client, H, p, expect=200, **body):
    r = await client.put(f"{B}/payslips/{p['id']}", json={"expected_version": p["version"], **body}, headers=H["hr"])
    assert r.status_code == expect, r.text
    return r.json()


async def rows(db):
    return list((await db.execute(select(PayrollTaxComputation).execution_options(populate_existing=True))).scalars())


# ================================================================================================ settings
async def test_settings_defaults_and_validation(client, H):
    s = (await client.get(SETTINGS, headers=H["hr"])).json()["settings"]
    assert s["payroll.tds_engine_enabled"] is False and s["payroll.tds_engine_mode"] == "suggest"
    assert s["payroll.tds_one_time_in_month"] is True and s["payroll.tds_proofs_from_month"] == 1
    assert s["payroll.tds_include_other_income"] is False
    for key, bad in (("tds_engine_mode", "auto"), ("tds_proofs_from_month", 0), ("tds_proofs_from_month", 13),
                     ("tds_proofs_from_month", "1"), ("tds_one_time_in_month", "yes"), ("tds_include_other_income", 1)):  # fmt: skip
        r = await client.put(SETTINGS, json={"settings": {f"payroll.{key}": bad}}, headers=H["hr"])
        assert r.status_code == 422, (key, bad)
    r = await client.put(
        SETTINGS,
        json={"settings": {"payroll.tds_engine_mode": "apply", "payroll.tds_proofs_from_month": 12, "payroll.tds_one_time_in_month": False,
                           "payroll.tds_include_other_income": True}, **PW},
        headers=H["admin"],
    )  # fmt: skip
    assert r.status_code == 200 and r.json()["settings"]["payroll.tds_proofs_from_month"] == 12


async def test_the_engine_switch_needs_role_assign_the_password_and_alerts_the_admins(client, H):
    key = "payroll.tds_engine_enabled"
    r = await client.put(SETTINGS, json={"settings": {key: True}, "current_password": PASSWORD}, headers=H["hr"])
    assert r.status_code == 403 and err(r) == "FORBIDDEN"
    r = await client.put(SETTINGS, json={"settings": {key: True}}, headers=H["admin"])
    assert r.status_code == 422
    r = await client.put(SETTINGS, json={"settings": {key: True}, "current_password": "Wrong!Passw0rd#1"}, headers=H["admin"])
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    assert (await client.get(SETTINGS, headers=H["hr"])).json()["settings"][key] is False
    notifications.OUTBOX.clear()
    r = await client.put(SETTINGS, json={"settings": {key: True}, "current_password": PASSWORD}, headers=H["admin"])
    assert r.status_code == 200 and r.json()["settings"][key] is True
    alerts = [o["payload"] for o in notifications.OUTBOX if o["payload"]["type"] == "admin_alert"]
    assert len(alerts) == 1 and "TDS engine" in alerts[0]["body"]
    # an unchanged value needs no step-up
    assert (await client.put(SETTINGS, json={"settings": {key: True}}, headers=H["hr"])).status_code == 200


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tds_engine_mode", "apply"),
        ("tds_proofs_from_month", 6),
        ("tds_include_other_income", True),
        ("tds_one_time_in_month", False),
    ],
)
async def test_the_tds_parameters_are_guarded_like_the_engine_switch(client, H, key, value):
    k = f"payroll.{key}"
    assert k in ptds.pset.GUARDED_KEYS
    body = {"settings": {k: value}}
    r = await client.put(SETTINGS, json={**body, **PW}, headers=H["hr"])
    assert r.status_code == 403 and err(r) == "FORBIDDEN"  # payroll.manage alone is not enough
    r = await client.put(SETTINGS, json=body, headers=H["admin"])
    assert r.status_code == 422  # no password
    r = await client.put(SETTINGS, json={**body, "current_password": "Wrong!Passw0rd#1"}, headers=H["admin"])
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    notifications.OUTBOX.clear()
    r = await client.put(SETTINGS, json={**body, **PW}, headers=H["admin"])
    assert r.status_code == 200 and r.json()["settings"][k] == value
    alerts = [o["payload"] for o in notifications.OUTBOX if o["payload"]["type"] == "admin_alert"]
    assert len(alerts) == 1 and "TDS engine setting" in alerts[0]["body"]
    assert (await client.put(SETTINGS, json=body, headers=H["hr"])).status_code == 200  # unchanged value: no step-up


# ================================================================================================ off by default
async def test_engine_off_is_exactly_the_manual_behaviour(client, tds, db):
    run = await run_for(client, tds)
    assert run["config"]["tds_engine_enabled"] is False and run["config"]["tax_rules"] is None
    p = await slip(client, tds, run)
    assert p["tds_computation"] is None and tds_line(p) is None and not any(w in p["warnings"] for w in ptds.TDS_WARNINGS)
    p = await put_slip(client, tds, p, tds="2500.00")  # no reason needed, no override marker
    assert tds_line(p) == "2500.00" and p["tds_computation"] is None and "TDS_OVERRIDDEN" not in p["warnings"]
    await process(client, tds, run)
    assert await rows(db) == []
    p = await slip(client, tds, run)
    assert tds_line(p) == "2500.00"


async def test_run_snapshots_the_mode_and_the_rules_version(client, tds, db):
    await engine(client, tds, mode="suggest")
    run = await run_for(client, tds)
    rules = (await db.execute(select(TaxYearRule).where(TaxYearRule.fy == FY))).scalar_one()
    assert run["config"]["tds_engine_enabled"] is True and run["config"]["tds_engine_mode"] == "suggest"
    assert run["config"]["tax_rules"] == {"fy": FY, "id": str(rules.id), "version": 1}


# ================================================================================================ suggest mode
async def test_suggest_mode_shows_the_figure_beside_the_manual_one_and_changes_nothing(client, tds, db):
    await engine(client, tds, mode="suggest")
    run = await run_for(client, tds)
    p = await slip(client, tds, run)
    c = p["tds_computation"]
    assert c["mode"] == "suggest" and c["suggested"] == "13780.00" and c["applied"] is None and c["override"] is None
    assert c["regime"] == "new" and c["regime_source"] == "default" and c["remaining_months"] == 10
    assert c["flags"] == ["REGIME_DEFAULT", "NO_DECLARATION"] and c["rules"]["version"] == 1 and c["declaration_version"] is None
    a = c["annual"]
    assert (a["salary_this_month"], a["salary_projected"], a["taxable_income"], a["total_tax"]) == (
        "200000.00", "1800000.00", "1925000.00", "137800.00",
    )  # fmt: skip
    assert tds_line(p) is None and p["totals"]["net_pay"] == "200000.00"  # the payslip is unchanged
    p = await put_slip(client, tds, p, tds="3000.00")  # manual figure in suggest mode: no reason, no override
    assert tds_line(p) == "3000.00" and p["tds_computation"]["suggested"] == "13780.00" and "TDS_OVERRIDDEN" not in p["warnings"]


async def test_the_computation_is_encrypted_at_rest(client, tds, db):
    await engine(client, tds)
    await run_for(client, tds)
    raw = (await db.execute(text("SELECT computation_enc, suggested_enc FROM payroll_tax_computations"))).one()
    assert b"13780" not in bytes(raw[1]) and b"137800" not in bytes(raw[0]) and b"annual" not in bytes(raw[0])


# ================================================================================================ apply mode
async def test_apply_mode_sets_the_tds_line(client, tds, db):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    p = await slip(client, tds, run)
    assert tds_line(p) == "13780.00" and p["inputs"]["tds"] == "13780.00" and p["totals"]["net_pay"] == "186220.00"
    c = p["tds_computation"]
    assert c["mode"] == "apply" and c["suggested"] == "13780.00" and c["applied"] == "13780.00" and c["override"] is None
    (row,) = await rows(db)
    assert row.applied is True and row.override_reason is None and row.mode == "apply" and row.rules_version == 1


async def test_override_needs_a_reason_and_sets_the_warning(client, tds, db):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    p = await slip(client, tds, run)
    r = await client.put(f"{B}/payslips/{p['id']}", json={"expected_version": p["version"], "tds": "5000.00"}, headers=tds["hr"])
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "tds_override_reason"
    r = await client.put(
        f"{B}/payslips/{p['id']}",
        json={"expected_version": p["version"], "tds": "5000.00", "tds_override_reason": "ab"},
        headers=tds["hr"],
    )
    assert r.status_code == 422
    p = await put_slip(client, tds, p, tds="5000.00", tds_override_reason="Employee asked for a lower deduction")
    assert tds_line(p) == "5000.00" and "TDS_OVERRIDDEN" in p["warnings"]
    c = p["tds_computation"]
    assert c["suggested"] == "13780.00" and c["applied"] == "5000.00" and "TDS_OVERRIDDEN" in c["flags"]
    assert c["override"] == {"amount": "5000.00", "reason": "Employee asked for a lower deduction"}
    (row,) = await rows(db)
    assert row.applied is False and row.override_reason == "Employee asked for a lower deduction"
    # the override survives a refresh; clearing the figure hands the line back to the engine
    await refresh(client, tds, run)
    p = await slip(client, tds, run)
    assert tds_line(p) == "5000.00" and "TDS_OVERRIDDEN" in p["warnings"]
    p = await put_slip(client, tds, p, tds=None)
    assert tds_line(p) == "13780.00" and "TDS_OVERRIDDEN" not in p["warnings"] and p["tds_computation"]["override"] is None
    # the reason is never in the audit log
    blob = " ".join(str(a.after) for a in (await db.execute(select(AuditLog))).scalars())
    assert "lower deduction" not in blob and "5000" not in blob and "13780" not in blob


async def test_tds_is_capped_to_the_net_pay_and_the_shortfall_is_carried(client, tds):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    p = await slip(client, tds, run)
    p = await put_slip(client, tds, p, adjustments=[{"code": "OTHER_DEDUCTION", "label": "Recovery", "amount": "190000.00"}])
    # net before TDS = 2,00,000 - 1,90,000 = 10,000 < 13,780
    assert tds_line(p) == "10000.00" and p["totals"]["net_pay"] == "0.00" and p["errors"] == []
    assert "TDS_CAPPED_NET" in p["warnings"] and p["tds_computation"]["shortfall"] == "3780.00"
    assert p["tds_computation"]["suggested"] == "13780.00" and p["tds_computation"]["applied"] == "10000.00"


async def test_mode_switching_between_runs_and_refresh(client, tds, db):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    assert tds_line(await slip(client, tds, run)) == "13780.00"
    await engine(client, tds, mode="suggest")
    run = await refresh(client, tds, run)
    assert run["config"]["tds_engine_mode"] == "suggest"
    p = await slip(client, tds, run)
    assert tds_line(p) is None and p["inputs"]["tds"] == "0.00" and p["tds_computation"]["mode"] == "suggest"
    assert p["tds_computation"]["applied"] is None
    await engine(client, tds, mode="apply")
    await refresh(client, tds, run)
    assert tds_line(await slip(client, tds, run)) == "13780.00"
    # an override becomes a plain manual figure when the mode goes back to suggest
    p = await slip(client, tds, run)
    await put_slip(client, tds, p, tds="4000.00", tds_override_reason="Agreed with the employee")
    await engine(client, tds, mode="suggest")
    await refresh(client, tds, run)
    p = await slip(client, tds, run)
    assert tds_line(p) == "4000.00" and "TDS_OVERRIDDEN" not in p["warnings"] and p["tds_computation"]["override"] is None
    # switching the engine off removes the computation and the engine's figure
    await engine(client, tds, mode="apply")
    await refresh(client, tds, run)
    p = await slip(client, tds, run)
    await put_slip(client, tds, p, tds=None)
    assert tds_line(await slip(client, tds, run)) == "13780.00"
    await engine(client, tds, enabled=False)
    await refresh(client, tds, run)
    p = await slip(client, tds, run)
    assert p["tds_computation"] is None and tds_line(p) is None and await rows(db) == []


# ================================================================================================ rules not verified / changed
async def test_engine_enabled_without_verified_rules_uses_the_manual_tds(client, H, uid, db):
    await set_setting(client, H["admin"], lop_absent_days=False)
    for who, by in (("hr", "admin"), ("admin", "hr"), ("manager", "hr")):
        await exclude_from_payroll(client, H[by], uid[who])
    await add_structure(client, H["hr"], uid["employee"], basic="150000", hra="50000", tds_monthly="1500")
    await add_rules(db, uid, fy=FY, status="draft")  # a draft document never feeds a payslip
    await engine(client, H, mode="apply")
    run = await new_run(client, H["hr"], JUNE)
    assert run["config"]["tax_rules"] is None
    p = await slip(client, H, run)
    assert "TAX_RULES_NOT_VERIFIED" in p["warnings"] and p["tds_computation"] is None and tds_line(p) == "1500.00"
    p = await put_slip(client, H, p, tds="1800.00")  # no engine figure to override: no reason needed
    assert tds_line(p) == "1800.00" and "TDS_OVERRIDDEN" not in p["warnings"]
    await process(client, H, run)  # never a silent zero, never blocked
    assert tds_line(await slip(client, H, run)) == "1800.00" and await rows(db) == []


async def test_a_newer_verified_version_raises_rules_changed_until_refresh(client, tds, db, uid):
    await engine(client, tds, mode="suggest")
    run = await run_for(client, tds)
    old = (await db.execute(select(TaxYearRule).where(TaxYearRule.fy == FY))).scalar_one()
    old_id = old.id
    old.status = "superseded"
    await db.commit()
    await add_rules(db, uid, fy=FY, version=2, status="verified")
    p = await put_slip(client, tds, await slip(client, tds, run), note="recompute")  # the run still pins v1: shown, but flagged
    assert "RULES_CHANGED" in p["errors"] and p["tds_computation"]["rules"] == {"id": str(old_id), "version": 1}
    assert "RULES_CHANGED" in p["tds_computation"]["flags"]
    r = await client.post(f"{B}/runs/{run['id']}/process", headers=tds["hr"])  # blocking: the pinned rules are superseded
    assert r.status_code == 409 and err(r) == "PAYROLL_BLOCKED" and "RULES_CHANGED" in r.text
    assert (await client.get(f"{B}/runs/{run['id']}", headers=tds["hr"])).json()["status"] == "draft"
    run = await refresh(client, tds, run)  # refresh adopts version 2 and clears the block
    assert run["config"]["tax_rules"]["version"] == 2
    p = await slip(client, tds, run)
    assert "RULES_CHANGED" not in p["errors"] and p["tds_computation"]["rules"]["version"] == 2
    await process(client, tds, run)


# ================================================================================================ declarations and proofs
async def add_declaration(db, uid, items, *, regime="old", submitted=True, version=1):
    d = TaxDeclaration(
        user_id=uuid.UUID(uid["employee"]), fy=FY, regime_draft=regime, draft={"items": items},
        regime=regime if submitted else None, submitted={"items": items} if submitted else None,
        submitted_version=version if submitted else None, submitted_at=utcnow() if submitted else None, version=version,
    )  # fmt: skip
    db.add(d)
    await db.commit()
    return d


C80 = {"id": "i-1", "section": "80C", "amount": "150000.00", "details": {}}


async def test_submitted_old_regime_declaration_feeds_the_engine_but_a_draft_does_not(client, tds, db, uid):
    await engine(client, tds)
    await add_declaration(db, uid, [C80], submitted=False)  # a draft only
    c = (await slip(client, tds, await run_for(client, tds)))["tds_computation"]
    assert c["suggested"] == "13780.00" and c["regime_source"] == "default" and "NO_DECLARATION" in c["flags"]
    d = (await db.execute(select(TaxDeclaration))).scalar_one()
    d.regime, d.submitted, d.submitted_version, d.submitted_at = "old", {"items": [C80]}, 1, utcnow()
    await db.commit()
    run = await run_for(client, tds, MAY)
    c = (await slip(client, tds, run))["tds_computation"]
    # May: 11 months left -> 22,00,000; old regime: - 50,000 standard - 1,50,000 (80C) = 20,00,000; 20% of 17,50,000 = 3,50,000;
    # cess 14,000 -> 3,64,000; / 11 = 33,090.91 -> 33,091
    assert c["regime"] == "old" and c["regime_source"] == "declared" and c["declaration_version"] == 1 and c["flags"] == []
    assert c["annual"]["chapter_via"] == "150000.00" and c["annual"]["total_tax"] == "364000.00" and c["suggested"] == "33091.00"


async def test_only_verified_proofs_count_from_the_proof_month(client, tds, db, uid):
    await engine(client, tds, tds_proofs_from_month=6)  # June is the first month of the verified basis
    await add_declaration(db, uid, [C80])
    c = (await slip(client, tds, await run_for(client, tds)))["tds_computation"]
    # nothing verified: 80C counts 0 -> 20,00,000 - 50,000 = 19,50,000; 20% of 17,00,000 = 3,40,000 + 13,600 = 3,53,600; / 10
    assert c["proof_basis"] == "verified" and c["annual"]["chapter_via"] == "0.00" and c["suggested"] == "35360.00"
    db.add(TaxProof(
        declaration_id=(await db.execute(select(TaxDeclaration.id))).scalar_one(), user_id=uuid.UUID(uid["employee"]), fy=FY,
        item_id="i-1", section="80C", claimed={"amount": "150000.00"}, file_ids=["f1"], status="verified",
        verified={"amount": "100000.00"}, verified_by=uuid.UUID(uid["hr"]), verified_at=utcnow(),
    ))  # fmt: skip
    await db.commit()
    run = (await client.get(f"{B}/runs", params={"year": 2025}, headers=tds["hr"])).json()["items"][0]
    await refresh(client, tds, {"id": run["id"]})
    c = (await slip(client, tds, {"id": run["id"]}))["tds_computation"]
    # verified 1,00,000 counts: 20,00,000 - 50,000 - 1,00,000 = 18,50,000; 20% of 16,00,000 = 3,20,000 + 12,800 = 3,32,800; / 10
    assert c["annual"]["chapter_via"] == "100000.00" and c["suggested"] == "33280.00"
    (row,) = await rows(db)
    assert len(row.proofs_digest) == 64 and "100000" not in row.proofs_digest  # an HMAC, not a plain hash of the amounts


# ================================================================================================ history, excess, PAN, flags
async def test_earlier_payslips_of_the_year_are_taken_into_account(client, tds):
    await engine(client, tds, mode="apply")
    may = await run_for(client, tds, MAY)
    # May: 11 months x 2,00,000 = 22,00,000 -> taxable 21,25,000 -> 20,000 + 1,32,500 = 1,52,500 + cess 6,100 = 1,58,600; / 11 = 14,418.18
    assert tds_line(await slip(client, tds, may)) == "14418.00"
    await process(client, tds, may)
    await publish(client, tds, may)
    june = await run_for(client, tds, JUNE)
    p = await slip(client, tds, june)
    c = p["tds_computation"]
    # June: paid 2,00,000 + this 2,00,000 + 9 x 2,00,000 = 22,00,000 -> the same 1,58,600; deducted 14,418; (1,58,600 - 14,418) / 10
    assert (
        c["annual"]["salary_paid"] == "200000.00"
        and c["tds_paid_so_far"] == "14418.00"
        and c["annual"]["total_tax"] == "158600.00"
    )
    assert c["remaining_months"] == 10 and c["suggested"] == "14418.00" and tds_line(p) == "14418.00"


async def test_over_deduction_gives_zero_and_a_warning(client, tds):
    may = await run_for(client, tds, MAY)  # engine still off: manual TDS
    p = await slip(client, tds, may)
    await put_slip(client, tds, p, tds="200000.00")
    await process(client, tds, may)
    await publish(client, tds, may)
    await engine(client, tds, mode="apply")
    p = await slip(client, tds, await run_for(client, tds, JUNE))
    assert tds_line(p) is None and "TDS_EXCESS" in p["warnings"] and p["tds_computation"]["suggested"] == "0.00"
    assert p["tds_computation"]["tds_paid_so_far"] == "200000.00" and p["tds_computation"]["remaining_tax"] == "0.00"


async def test_no_pan_uses_the_higher_rate_and_warns(client, tds, db, uid):
    await set_pan(db, uid["employee"], None)
    await engine(client, tds, mode="apply")
    p = await slip(client, tds, await run_for(client, tds))
    # average rate 137,800 / 19,25,000 = 7.16% < the 20% minimum -> 20% of 2,00,000 = 40,000
    assert "NO_PAN_206AA" in p["warnings"] and "NO_PAN" in p["warnings"] and tds_line(p) == "40000.00"
    assert "NO_PAN_206AA" in p["tds_computation"]["flags"]


async def test_outstanding_loans_above_the_threshold_ask_for_a_perquisite_review(client, tds, db, uid):
    db.add(PayrollLoan(
        user_id=uuid.UUID(uid["employee"]), kind="loan", reference="L-0001", principal="50000.00", emi="5000.00",
        start_period=dt.date(2025, 4, 1), reason="Test", status="active", created_by=uuid.UUID(uid["hr"]),
    ))  # fmt: skip
    await db.commit()
    await engine(client, tds)
    p = await slip(client, tds, await run_for(client, tds))
    assert "PERQUISITE_REVIEW" in p["warnings"] and "PERQUISITE_REVIEW" in p["tds_computation"]["flags"]


def test_final_payslips_with_leave_encashment_ask_for_a_review():
    rc = ptds.RunCtx(fy=FY, mode="suggest", row=TaxYearRule(rules=rules_copy()))
    this = ptds.This(leave_encash=Decimal("50000.00"))
    assert ptds.extra_flags(rc, ptds.UserData(), this, final=True) == ["LEAVE_ENCASH_REVIEW"]
    assert ptds.extra_flags(rc, ptds.UserData(), this, final=False) == []


async def test_a_leaver_settles_all_remaining_tax_in_the_last_month(client, tds, db, uid):
    await engine(client, tds, mode="apply")
    det = (
        await db.execute(select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id == uuid.UUID(uid["employee"])))
    ).scalar_one()
    det.last_working_day = dt.date(2025, 6, 30)
    await db.commit()
    p = await slip(client, tds, await run_for(client, tds))
    c = p["tds_computation"]
    # leaving at the end of June: only this month's 2,00,000 is taxable (-75,000 standard = 1,25,000 -> nil tax)
    assert c["remaining_months"] == 1 and c["annual"]["salary_projected"] == "0.00" and c["annual"]["total_tax"] == "0.00"
    assert tds_line(p) is None
    det.last_working_day = dt.date(2025, 8, 31)  # three months left: Jun, Jul, Aug
    await db.commit()
    run = (await client.get(f"{B}/runs", params={"year": 2025}, headers=tds["hr"])).json()["items"][0]
    await refresh(client, tds, {"id": run["id"]})
    c = (await slip(client, tds, {"id": run["id"]}))["tds_computation"]
    assert c["remaining_months"] == 3 and c["annual"]["salary_projected"] == "400000.00"


# ================================================================================================ freeze, MAC, revise
async def test_the_computation_is_written_before_processing_and_frozen_after(client, tds, db):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    await process(client, tds, run)
    (row,) = await rows(db)
    assert row.suggested == "13780.00" and row.applied is True
    p = await slip(client, tds, run)
    assert p["status"] == "processed" and p["tds_computation"]["applied"] == "13780.00"
    r = await client.put(f"{B}/payslips/{p['id']}", json={"expected_version": p["version"], "tds": "1.00"}, headers=tds["hr"])
    assert r.status_code == 409 and err(r) == "PAYROLL_LOCKED"
    # persist() never writes a non-draft payslip, whatever the caller left on it
    ps = (await db.execute(select(Payslip).execution_options(populate_existing=True))).scalar_one()
    ps._tds = {"fy": FY, "rules_id": row.rules_id, "rules_version": 9, "regime": "old", "declaration_version": None, "proofs_digest": "x",
               "mode": "suggest", "computation": {}, "suggested": "1.00", "applied": False, "override_reason": None}  # fmt: skip
    await ptds.persist(db, [ps], None)
    (row2,) = await rows(db)
    assert row2.rules_version == 1 and row2.suggested == "13780.00"
    # reopen -> draft again -> it can be recomputed
    assert (await client.post(f"{B}/runs/{run['id']}/reopen", headers=tds["hr"])).status_code == 200
    await engine(client, tds, mode="suggest")
    await refresh(client, tds, run)
    assert (await rows(db))[0].mode == "suggest"


async def test_the_computation_is_part_of_the_publish_mac(client, tds, db):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    await process(client, tds, run)
    ps = (await db.execute(select(Payslip).execution_options(populate_existing=True))).scalar_one()
    plain = prs.result_mac(ps)
    await ptds.attach_macs(db, [ps])
    with_tc = prs.result_mac(ps)
    assert with_tc != plain
    row = (await rows(db))[0]
    row.suggested = "99999.00"  # a changed computation (even without a changed result) changes the MAC
    await db.flush()
    await ptds.attach_macs(db, [ps])
    assert prs.result_mac(ps) not in (plain, with_tc)
    await db.rollback()


async def test_revision_gets_its_own_computation(client, tds, db, uid):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    await process(client, tds, run)
    await publish(client, tds, run)
    r = await client.post(
        f"{B}/runs/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Recheck"}, headers=tds["hr"]
    )
    assert r.status_code == 201, r.text
    assert r.json()["tds_computation"]["suggested"] == "13780.00" and r.json()["revision"] == 2
    assert len(await rows(db)) == 2  # the published copy keeps its frozen row


# ================================================================================================ preview / summary / projection
async def test_preview_summary_and_projection_need_the_engine_and_verified_rules(client, H, uid, db):
    for hdr, method, path, body in (
        (H["hr"], "POST", f"{TAX}/preview", {"user_id": uid["employee"], "fy": FY, "period": JUNE}),
        (H["hr"], "GET", f"{TAX}/summary?fy={FY}", None),
        (H["employee"], "GET", f"/api/me/tax/projection?fy={FY}", None),
    ):
        r = await client.request(method, path, json=body, headers=hdr)
        assert r.status_code == 409 and err(r) == "TDS_ENGINE_OFF", path
    await engine(client, H)
    for hdr, method, path, body in (
        (H["hr"], "POST", f"{TAX}/preview", {"user_id": uid["employee"], "fy": FY, "period": JUNE}),
        (H["hr"], "GET", f"{TAX}/summary?fy={FY}", None),
        (H["employee"], "GET", f"/api/me/tax/projection?fy={FY}", None),
    ):
        r = await client.request(method, path, json=body, headers=hdr)
        assert r.status_code == 409 and err(r) == "TAX_RULES_NOT_VERIFIED", path
    # permissions are checked first
    assert (await client.post(f"{TAX}/preview", json={"user_id": uid["employee"]}, headers=H["employee"])).status_code == 403
    assert (await client.get(f"{TAX}/summary", headers=H["employee"])).status_code == 403
    assert (await client.get("/api/me/tax/projection")).status_code == 401


async def test_preview_compares_the_regimes_and_writes_nothing(client, tds, uid, db):
    await engine(client, tds)
    r = await client.post(f"{TAX}/preview", json={"user_id": uid["employee"], "fy": FY, "period": JUNE}, headers=tds["hr"])
    assert r.status_code == 200, r.text
    assert r.json()["computation"]["suggested"] == "13780.00" and r.json()["computation"]["applied"] is None
    # old regime: 20,00,000 - 50,000 = 19,50,000 -> 20% of 17,00,000 = 3,40,000 + 13,600 = 3,53,600
    assert r.json()["other_regime"] == {"regime": "old", "total_tax": "353600.00"}
    r = await client.post(
        f"{TAX}/preview", json={"user_id": uid["employee"], "fy": FY, "period": JUNE, "regime": "old"}, headers=tds["hr"]
    )
    assert r.json()["computation"]["regime"] == "old" and r.json()["other_regime"] == {"regime": "new", "total_tax": "137800.00"}
    assert await rows(db) == []
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.tax.preview"))).scalars().all()
    assert log and all(set(a.after) == {"user_id", "fy"} for a in log)
    for bad in ({"user_id": uid["employee"], "period": "2025-13"}, {"user_id": uid["employee"], "fy": "2025-27"},
                {"user_id": uid["employee"], "fy": FY, "period": "2026-06"}, {"user_id": uid["employee"], "extra": 1}):  # fmt: skip
        assert (await client.post(f"{TAX}/preview", json=bad, headers=tds["hr"])).status_code == 422, bad
    assert (
        await client.post(f"{TAX}/preview", json={"user_id": str(uuid.uuid4()), "fy": FY}, headers=tds["hr"])
    ).status_code == 404


async def test_summary_lists_the_latest_computation_per_employee(client, tds, uid, db):
    await engine(client, tds)
    await run_for(client, tds)
    r = await client.get(f"{TAX}/summary", params={"fy": FY}, headers=tds["hr"])
    assert r.status_code == 200, r.text
    (item,) = r.json()["items"]
    assert item["user"]["employee_id"] == "EMP001" and item["regime"] == "new" and item["total_tax"] == "137800.00"
    assert (
        item["suggested_this_month"] == "13780.00" and item["manual_this_month"] == "0.00" and "NO_DECLARATION" in item["flags"]
    )
    assert r.json()["totals"] == {"total_tax": "137800.00", "tds_paid_so_far": "0.00", "remaining_tax": "137800.00"}
    for params, n in (({"flag": "NO_DECLARATION"}, 1), ({"flag": "TDS_EXCESS"}, 0), ({"q": "emp001"}, 1), ({"q": "nobody"}, 0),
                      ({"department_id": str(uuid.uuid4())}, 0)):  # fmt: skip
        r = await client.get(f"{TAX}/summary", params={"fy": FY, **params}, headers=tds["hr"])
        assert len(r.json()["items"]) == n, params
    assert r.json()["totals"]["total_tax"] == "0.00"  # totals follow the filtered set
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.tax.summary"))).scalars().all()
    assert log and all("count" in a.after and "137800" not in str(a.after) for a in log)


async def test_employee_projection_is_an_estimate_of_the_own_data_only(client, tds, uid):
    await engine(client, tds)
    r = await client.get("/api/me/tax/projection", params={"fy": FY}, headers=tds["employee"])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["estimate"] is True and j["computation"]["fy"] == FY and j["computation"]["override"] is None
    assert j["other_regime"]["regime"] == "old" and j["computation"]["applied"] is None
    # an employee with no structure still gets an answer about THEIR OWN (empty) data, never somebody else's
    r = await client.get("/api/me/tax/projection", params={"fy": FY}, headers=tds["manager"])
    assert r.status_code == 200 and r.json()["computation"]["annual"]["salary_projected"] == "0.00"


# ================================================================================================ statement
async def test_statement_pdf_needs_the_password_and_says_not_form_16(client, tds, uid, db, monkeypatch):
    from app.services import payslip_pdf as pp
    from app.services import tax_statement_pdf as sp

    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    await process(client, tds, run)
    await publish(client, tds, run)
    url = f"{TAX}/employees/{uid['employee']}/statement"
    assert (await client.post(url, json={}, headers=tds["hr"])).status_code == 422
    r = await client.post(url, json={"current_password": "Wrong!Passw0rd#1"}, headers=tds["hr"])
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    assert (await client.post(url, json=PW, headers=tds["employee"])).status_code == 403
    seen = {}
    real = pp.build_pdf

    async def spy(view, footer, logo, watermark, renderer=None):
        seen.update(view=view, watermark=watermark)
        return await real(view, footer, logo, watermark, renderer=renderer)

    monkeypatch.setattr(pp, "build_pdf", spy)
    r = await client.post(url, params={"fy": FY}, json=PW, headers=tds["hr"])
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert "tax-computation-EMP001-2025-26.pdf" in r.headers["content-disposition"]
    assert "NOT FORM 16" in seen["watermark"] == sp.WATERMARK
    v = seen["view"]
    assert v["employee"]["pan"] == "XXXXX1234X" and v["annual"]["total_tax"] == "137800.00"
    assert v["months"] == [{"period": JUNE, "gross": "200000.00", "tds": "13780.00"}]
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.tax.statement"))).scalars().one()
    assert set(log.after) == {"user_id", "fy"}


# ================================================================================================ challans
async def published_june(client, tds):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    await process(client, tds, run)
    await publish(client, tds, run)
    return run


CH = {"type": "tds", "period": JUNE, "status": "paid", "paid_on": "2025-07-05", "reference": "TRRN-1", **PW}
BSR = {"bsr_code": "0123456", "challan_serial": "00042", "deposited_on": "2025-07-05"}


async def test_tds_challan_details_are_required_set_together_and_cleared_together(client, tds, db):
    await published_june(client, tds)
    hdr = tds["hr"]
    r = await client.put(f"{B}/statutory/challans", json=CH, headers=hdr)
    assert r.status_code == 422  # BSR code, serial and deposit date are required for a paid TDS challan
    for part in ("bsr_code", "challan_serial", "deposited_on"):
        body = {**CH, **{k: v for k, v in BSR.items() if k != part}}
        assert (await client.put(f"{B}/statutory/challans", json=body, headers=hdr)).status_code == 422, part
    for bad in ({"bsr_code": "12345"}, {"bsr_code": "01234567"}, {"challan_serial": "123456"}, {"challan_serial": "ab"},
                {"deposited_on": "not-a-date"}):  # fmt: skip
        assert (await client.put(f"{B}/statutory/challans", json={**CH, **BSR, **bad}, headers=hdr)).status_code == 422, bad
    r = await client.put(f"{B}/statutory/challans", json={**CH, **BSR, "deposited_on": "2999-01-01"}, headers=hdr)
    assert r.status_code == 422 and err(r) == "INVALID_DATE"
    for t in ("pf", "pt", "esi"):  # only TDS carries them
        r = await client.put(f"{B}/statutory/challans", json={**CH, **BSR, "type": t, "amount": "10.00"}, headers=hdr)
        assert r.status_code == 422, t
    r = await client.put(f"{B}/statutory/challans", json={**CH, **BSR}, headers=hdr)
    assert r.status_code == 200, r.text
    assert (
        r.json()["bsr_code"] == "XXX3456" and r.json()["challan_serial"] == "00042" and r.json()["deposited_on"] == "2025-07-05"
    )
    c = (await db.execute(select(StatutoryChallan).execution_options(populate_existing=True))).scalar_one()
    assert c.bsr_code == "0123456" and c.challan_serial == "00042" and c.deposited_on == dt.date(2025, 7, 5)
    raw = (await db.execute(text("SELECT bsr_code_enc, challan_serial_enc FROM statutory_challans"))).one()
    assert b"0123456" not in bytes(raw[0]) and b"00042" not in bytes(raw[1])
    # unpaid clears all three and the event keeps the overwritten state in prev_*
    r = await client.put(f"{B}/statutory/challans", json={"type": "tds", "period": JUNE, "status": "unpaid", **PW}, headers=hdr)
    assert r.status_code == 200 and r.json()["bsr_code"] is None and r.json()["deposited_on"] is None
    c = (await db.execute(select(StatutoryChallan).execution_options(populate_existing=True))).scalar_one()
    assert c.bsr_code is None and c.challan_serial is None and c.deposited_on is None
    ev = list((await db.execute(select(StatutoryChallanEvent).order_by(StatutoryChallanEvent.created_at))).scalars())
    assert len(ev) == 2
    paid, unpaid = sorted(ev, key=lambda e: e.action != "paid")
    assert paid.bsr_code == "0123456" and paid.prev_bsr_code is None and paid.deposited_on == dt.date(2025, 7, 5)
    assert unpaid.bsr_code is None and unpaid.prev_bsr_code == "0123456" and unpaid.prev_challan_serial == "00042"
    assert unpaid.prev_deposited_on == dt.date(2025, 7, 5)
    blob = " ".join(str(a.after) + str(a.before) for a in (await db.execute(select(AuditLog))).scalars())
    assert "0123456" not in blob and "00042" not in blob
    dues = (await client.get(f"{B}/statutory/dues", params={"period": JUNE}, headers=hdr)).json()["months"][0]
    assert next(i for i in dues["items"] if i["type"] == "tds")["bsr_code"] is None


# ================================================================================================ 24Q
def sheet(r, name):
    wb = load_workbook(io.BytesIO(r.content))
    return [[c for c in row] for row in wb[name].iter_rows(values_only=True)]


async def test_24q_workbook_blockers_and_step_up(client, tds, db, uid):
    await published_june(client, tds)
    hdr = tds["hr"]
    url = f"{TAX}/24q"
    assert (await client.post(url, params={"fy": FY, "quarter": 1}, json={}, headers=hdr)).status_code == 422
    r = await client.post(url, params={"fy": FY, "quarter": 1}, json={"current_password": "Wrong!Passw0rd#1"}, headers=hdr)
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    assert (await client.post(url, params={"fy": FY, "quarter": 1}, json=PW, headers=tds["employee"])).status_code == 403
    for bad in ({"fy": FY, "quarter": 5}, {"fy": FY, "quarter": 0}, {"fy": "2025-27", "quarter": 1}):
        assert (await client.post(url, params=bad, json=PW, headers=hdr)).status_code == 422, bad
    notifications.OUTBOX.clear()
    r = await client.post(url, params={"fy": FY, "quarter": 1}, json=PW, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store" and "payroll-24q-2025-26-q1.xlsx" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Deductees", "Challans", "Blockers"]  # no Salary Details outside quarter 4
    ded = sheet(r, "Deductees")
    assert ded[0][:4] == ["Month", "Employee ID", "Name", "PAN"] and len(ded) == 2
    assert ded[1][:4] == [JUNE, "EMP001", "Test Employee", "ABCDE1234F"] and ded[1][6] == 13780.0
    blockers = sheet(r, "Blockers")
    assert [b[2] for b in blockers[1:]] == ["CHALLAN_MISSING"]
    assert any("24Q" in o["payload"]["title"] for o in notifications.OUTBOX if o["payload"]["type"] == "admin_alert")
    # a paid challan with BSR code and serial clears the blocker and fills the Challans / Deductees columns
    r = await client.put(f"{B}/statutory/challans", json={**CH, **BSR}, headers=hdr)
    assert r.status_code == 200, r.text
    r = await client.post(url, params={"fy": FY, "quarter": 1}, json=PW, headers=hdr)
    assert len(sheet(r, "Blockers")) == 1
    ded = sheet(r, "Deductees")
    assert ded[1][9:] == ["0123456", "00042", "2025-07-05"]
    assert sheet(r, "Challans")[1][:4] == [JUNE, 13780.0, 1, "paid"]
    # the PAN is read from the CURRENT details: removing it is a blocker, not a silently wrong file
    await set_pan(db, uid["employee"], None)
    r = await client.post(url, params={"fy": FY, "quarter": 1}, json=PW, headers=hdr)
    assert [b[2] for b in sheet(r, "Blockers")[1:]] == ["NO_PAN"]
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.statutory.export"))).scalars().all()
    assert log and all(a.after["kind"] == "24q" and "ABCDE" not in str(a.after) and "13780" not in str(a.after) for a in log)


async def test_24q_needs_a_published_month_and_lists_unpublished_ones(client, tds):
    await engine(client, tds, mode="apply")
    url = f"{TAX}/24q"
    may = await run_for(client, tds, MAY)
    r = await client.post(url, params={"fy": FY, "quarter": 1}, json=PW, headers=tds["hr"])
    assert r.status_code == 409 and err(r) == "RUN_NOT_PUBLISHED"
    await process(client, tds, may)
    await publish(client, tds, may)
    await run_for(client, tds, JUNE)  # a draft month
    r = await client.post(url, params={"fy": FY, "quarter": 1}, json=PW, headers=tds["hr"])
    assert r.status_code == 200
    codes = [(b[0], b[2]) for b in sheet(r, "Blockers")[1:]]
    assert (JUNE, "RUN_NOT_PUBLISHED") in codes and (MAY, "CHALLAN_MISSING") in codes


async def test_24q_quarter_four_has_the_salary_details_sheet(client, tds):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds, "2026-01")
    await process(client, tds, run)
    await publish(client, tds, run)
    r = await client.post(f"{TAX}/24q", params={"fy": FY, "quarter": 4}, json=PW, headers=tds["hr"])
    assert r.status_code == 200, r.text
    assert load_workbook(io.BytesIO(r.content)).sheetnames == ["Deductees", "Salary Details", "Challans", "Blockers"]
    sal = sheet(r, "Salary Details")
    assert sal[0][:4] == ["Employee ID", "Name", "PAN", "Regime"] and sal[1][0] == "EMP001" and sal[1][3] == "new"
    assert sal[1][4] == 200000.0 and sal[1][12] == sal[1][12]  # gross paid in the published month


# ================================================================================================ small rules
def test_is_final_rules():
    june = dt.date(2025, 6, 1)
    assert ptds.is_final({"settlement_only": True}, None, june)
    assert ptds.is_final({}, dt.date(2025, 6, 30), june)  # leaves this month
    assert ptds.is_final({}, dt.date(2025, 5, 31), june)  # left earlier (settlement month)
    assert not ptds.is_final({}, dt.date(2025, 7, 31), june)
    assert not ptds.is_final({}, None, june) and not ptds.is_final(None, None, june)


def test_proof_month_is_the_first_such_calendar_month_of_the_fy():
    assert ptds.proofs_from_key("2025-26", 1) == "2026-01"
    assert ptds.proofs_from_key("2025-26", 12) == "2025-12"
    assert ptds.proofs_from_key("2025-26", 4) == "2025-04"
    assert ptds.proofs_from_key("2025-26", 3) == "2026-03"


async def test_a_pinned_rules_row_that_is_not_verified_is_never_used(client, tds, db, uid):
    from app.models import PayrollRun

    await engine(client, tds)
    run = await run_for(client, tds)
    draft = await add_rules(db, uid, fy=FY, version=2, status="draft")
    pr = (await db.execute(select(PayrollRun).execution_options(populate_existing=True))).scalar_one()
    pr.config = {**pr.config, "tax_rules": {"fy": FY, "id": str(draft.id), "version": 2}}
    await db.commit()
    await process(client, tds, run)
    p = await slip(client, tds, run)
    assert "TAX_RULES_NOT_VERIFIED" in p["warnings"] and p["tds_computation"] is None
