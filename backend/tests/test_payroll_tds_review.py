# ruff: noqa: F811
"""Plan 17 work package 5c: fixes of the security review (blocking codes, final-month shortfall, replaced manual TDS, own projection,
proof matching, MAC body, audit actions, 24Q cell types, statement choice, F&F figure, report row caps).

The tax rules are the TEST fixture of `tests.payroll_tax_helpers` (illustrative numbers). Setup and helpers come from
`tests.test_payroll_tds` (EMP001 earns Rs 2,00,000 a month; FY 2025-26).
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.core.clock import utcnow
from app.models import AuditLog, PayrollLoan, PayrollTaxComputation, Payslip, TaxDeclaration, TaxProof
from app.services import notifications
from app.services import payroll_tds as ptds
from app.services import payroll_tds_reports as reports
from tests.payroll_controls_helpers import quiet  # noqa: F401  (fixture)
from tests.payroll_tax_helpers import add_rules, err, set_pan
from tests.test_payroll_tds import (  # noqa: F401  (fixtures + helpers)
    FY,
    JUNE,
    MAY,
    PW,
    TAX,
    B,
    add_declaration,
    engine,
    process,
    publish,
    put_slip,
    refresh,
    rows,
    run_for,
    sheet,
    slip,
    tds,
    tds_line,
)

pytestmark = pytest.mark.usefixtures("quiet", "clock")

BONUS = [
    {"code": "OTHER_EARNING", "label": "Bonus", "amount": "3000000.00"},
    {"code": "OTHER_DEDUCTION", "label": "Recovery", "amount": "3150000.00"},
]  # net before TDS = 3,200,000 - 3,150,000 = 50,000, far below the tax of the month


async def set_lwd(db, uid, day):
    from app.models import EmployeePayrollDetails

    det = (
        await db.execute(select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id == uuid.UUID(uid["employee"])))
    ).scalar_one()
    det.last_working_day = day
    await db.commit()


# ================================================================================================ 4: shortfall on the final payslip
async def test_a_capped_shortfall_on_a_leavers_last_payslip_blocks_until_an_explicit_override(client, tds, db, uid):
    await engine(client, tds, mode="apply")
    await set_lwd(db, uid, dt.date(2025, 6, 30))
    run = await run_for(client, tds)
    p = await put_slip(client, tds, await slip(client, tds, run), adjustments=BONUS)
    assert tds_line(p) == "50000.00" and float(p["tds_computation"]["shortfall"]) > 0
    assert "TDS_SHORTFALL_FINAL" in p["errors"] and "TDS_SHORTFALL_FINAL" in p["tds_computation"]["flags"]
    r = await client.post(f"{B}/runs/{run['id']}/process", headers=tds["hr"])
    assert r.status_code == 409 and err(r) == "PAYROLL_BLOCKED" and "TDS_SHORTFALL_FINAL" in r.text
    # only an explicit override with a reason clears it
    r = await client.put(f"{B}/payslips/{p['id']}", json={"expected_version": p["version"], "tds": "50000.00"}, headers=tds["hr"])
    assert r.status_code == 422
    p = await put_slip(client, tds, p, tds="50000.00", tds_override_reason="Balance to be recovered by the employee")
    assert "TDS_SHORTFALL_FINAL" not in p["errors"] and "TDS_OVERRIDDEN" in p["warnings"]
    assert float(p["tds_computation"]["shortfall"]) > 0  # the shortfall stays visible on the computation
    await process(client, tds, run)


async def test_a_capped_shortfall_in_the_last_month_of_the_year_blocks(client, tds):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds, "2026-03")
    p = await put_slip(client, tds, await slip(client, tds, run), adjustments=BONUS)
    assert "TDS_SHORTFALL_FINAL" in p["errors"]
    r = await client.post(f"{B}/runs/{run['id']}/process", headers=tds["hr"])
    assert r.status_code == 409 and "TDS_SHORTFALL_FINAL" in r.text


async def test_a_capped_shortfall_in_an_ordinary_month_only_warns(client, tds):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    p = await put_slip(client, tds, await slip(client, tds, run), adjustments=[BONUS[1]])
    p = await put_slip(client, tds, p, adjustments=[{"code": "OTHER_DEDUCTION", "label": "Recovery", "amount": "190000.00"}])
    assert "TDS_CAPPED_NET" in p["warnings"] and "TDS_SHORTFALL_FINAL" not in p["errors"]
    await process(client, tds, run)


# ================================================================================================ 7: a manual TDS replaced by apply mode
async def test_apply_mode_flags_and_audits_a_replaced_manual_tds(client, tds, db):
    run = await run_for(client, tds)  # engine off
    p = await put_slip(client, tds, await slip(client, tds, run), tds="2500.00")
    assert tds_line(p) == "2500.00"
    await engine(client, tds, mode="apply")
    await refresh(client, tds, run)
    p = await slip(client, tds, run)
    assert tds_line(p) == "13780.00" and "TDS_MANUAL_REPLACED" in p["warnings"]
    assert "TDS_MANUAL_REPLACED" in p["tds_computation"]["flags"]
    await refresh(client, tds, run)  # the notice stays until somebody decides about the line
    assert "TDS_MANUAL_REPLACED" in (await slip(client, tds, run))["warnings"]
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.payslip.tds_manual_replaced"))).scalars().all()
    assert len(logs) == 1 and set(logs[0].after) == {"payslip_id", "user_id", "mode"}
    assert "2500" not in str(logs[0].after) and "13780" not in str(logs[0].after)
    p = await put_slip(client, tds, await slip(client, tds, run), tds="9000.00", tds_override_reason="Agreed with the employee")
    assert "TDS_MANUAL_REPLACED" not in p["warnings"]
    await process(client, tds, run)  # a warning, never a block


async def test_apply_mode_does_not_flag_an_equal_manual_figure_or_the_structure_default(client, tds, db):
    run = await run_for(client, tds)
    await engine(client, tds, mode="apply")
    await refresh(client, tds, run)
    assert "TDS_MANUAL_REPLACED" not in (await slip(client, tds, run))["warnings"]
    assert (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.payslip.tds_manual_replaced"))).first() is None


# ================================================================================================ 13: distinct override audit action
async def test_a_tds_override_has_its_own_audit_action_without_values(client, tds, db):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    p = await put_slip(client, tds, await slip(client, tds, run), tds="5000.00", tds_override_reason="Employee asked for less")
    await put_slip(client, tds, p, tds=None)
    logs = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.payslip.tds_override").order_by(AuditLog.id)))
        .scalars()
        .all()
    )
    assert [a.after["cleared"] for a in logs] == [False, True]
    for a in logs:
        assert set(a.after) == {"payslip_id", "user_id", "mode", "cleared"} and a.after["mode"] == "apply"
        assert "5000" not in str(a.after) and "asked" not in str(a.after)
    # a plain payslip edit that does not touch the TDS writes none
    p = await slip(client, tds, run)
    await put_slip(client, tds, p, note="x")
    assert len((await db.execute(select(AuditLog).where(AuditLog.action == "payroll.payslip.tds_override"))).scalars().all()) == 2


# ================================================================================================ 5 + 15: own projection
async def test_the_own_projection_uses_published_payslips_only_and_hides_hr_flags(client, tds, db, uid, clock):
    clock.day = dt.date(2025, 6, 10)
    db.add(PayrollLoan(
        user_id=uuid.UUID(uid["employee"]), kind="loan", reference="L-0001", principal="50000.00", emi="5000.00",
        start_period=dt.date(2025, 4, 1), reason="Test", status="active", created_by=uuid.UUID(uid["hr"]),
    ))  # fmt: skip
    await db.commit()
    await engine(client, tds, mode="apply")
    may = await run_for(client, tds, MAY)
    await process(client, tds, may)  # processed, NOT published
    june = await run_for(client, tds, JUNE)
    await put_slip(
        client,
        tds,
        await slip(client, tds, june),
        adjustments=[{"code": "OTHER_EARNING", "label": "Bonus", "amount": "1000000.00"}],
    )

    async def mine():
        r = await client.get("/api/me/tax/projection", params={"fy": FY}, headers=tds["employee"])
        assert r.status_code == 200, r.text
        return r.json()["computation"]

    c = await mine()
    assert c["tds_paid_so_far"] == "0.00" and c["annual"]["salary_paid"] == "0.00"  # May is not published
    assert c["annual"]["salary_this_month"] == "200000.00"  # the unpublished bonus of June is not used
    assert "PERQUISITE_REVIEW" not in c["flags"] and c["override"] is None
    # the admin preview still sees (and flags) everything
    r = await client.post(f"{TAX}/preview", json={"user_id": uid["employee"], "fy": FY, "period": JUNE}, headers=tds["hr"])
    assert r.status_code == 200, r.text
    assert (
        "PERQUISITE_REVIEW" in r.json()["computation"]["flags"]
        and r.json()["computation"]["annual"]["salary_this_month"] == "1200000.00"
    )
    await publish(client, tds, may)
    c = await mine()
    assert c["tds_paid_so_far"] != "0.00" and c["annual"]["salary_paid"] == "200000.00"
    assert "PERQUISITE_REVIEW" not in c["flags"]


def test_hr_only_flags_are_known():
    assert {"PERQUISITE_REVIEW", "LEAVE_ENCASH_REVIEW", "RULES_CHANGED"} <= ptds.HR_ONLY_FLAGS


# ================================================================================================ 6 + 10: proofs
async def test_a_proof_counts_only_for_the_same_declaration_and_section(client, tds, db, uid):
    await add_declaration(db, uid, [{"id": "i-1", "section": "80C", "amount": "150000.00", "details": {}}])
    decl_id = (await db.execute(select(TaxDeclaration.id))).scalar_one()
    other = TaxDeclaration(user_id=uuid.UUID(uid["hr"]), fy=FY, regime_draft="old", draft={"items": []}, version=1)
    db.add(other)
    await db.flush()

    def proof(declaration_id, user, section, amount):
        return TaxProof(
            declaration_id=declaration_id, user_id=uuid.UUID(user), fy=FY, item_id="i-1", section=section,
            claimed={"amount": "150000.00"}, file_ids=["f1"], status="verified", verified={"amount": amount},
            verified_by=uuid.UUID(uid["manager"]), verified_at=utcnow(),
        )  # fmt: skip

    db.add(proof(decl_id, uid["employee"], "80D", "90000.00"))  # filed for another section
    await db.commit()
    ud = (await ptds.load_data(db, FY, dt.date(2025, 6, 1), [uuid.UUID(uid["employee"])]))[uuid.UUID(uid["employee"])]
    assert ptds.matched_verified(ud) == {}
    d1 = ud.declaration.id
    ud.verified_section["i-1"] = "80C"  # the same section: counts
    assert ptds.matched_verified(ud) == {"i-1": ud.verified["i-1"]} and d1 == decl_id


async def test_a_proof_of_a_foreign_declaration_never_loads(client, tds, db, uid):
    await add_declaration(db, uid, [{"id": "i-1", "section": "80C", "amount": "150000.00", "details": {}}])
    other = TaxDeclaration(user_id=uuid.UUID(uid["employee"]), fy="2024-25", regime_draft="old", draft={"items": []}, version=1)
    db.add(other)
    await db.flush()
    db.add(TaxProof(
        declaration_id=other.id, user_id=uuid.UUID(uid["employee"]), fy=FY, item_id="i-1", section="80C",
        claimed={"amount": "1.00"}, file_ids=["f1"], status="verified", verified={"amount": "150000.00"},
        verified_by=uuid.UUID(uid["hr"]), verified_at=utcnow(),
    ))  # fmt: skip
    await db.commit()
    ud = (await ptds.load_data(db, FY, dt.date(2025, 6, 1), [uuid.UUID(uid["employee"])]))[uuid.UUID(uid["employee"])]
    assert ud.verified == {} and ptds.matched_verified(ud) == {}


def test_previous_employer_tds_above_its_income_is_refused_on_save():
    from types import SimpleNamespace

    from app.core.errors import AppError
    from app.services import tax_declarations as td
    from tests.payroll_tax_helpers import rules_copy

    def item(income, tds):
        return SimpleNamespace(id=None, section="PREV_EMPLOYER", amount="100000.00", details={"income": income, "tds": tds})

    ok = td.build_items(rules_copy(), FY, "new", [item("100000.00", "100000.00")], set(), [], [])
    assert ok[0]["details"]["tds"] == "100000.00"
    with pytest.raises(AppError) as e:
        td.build_items(rules_copy(), FY, "new", [item("100000.00", "100000.01")], set(), [], [])
    assert e.value.code == "VALIDATION_ERROR" and "details.tds" in str(e.value.details)
    with pytest.raises(AppError):  # no income stated at all
        td.build_items(
            rules_copy(),
            FY,
            "new",
            [SimpleNamespace(id=None, section="PREV_EMPLOYER", amount="1.00", details={"tds": "5.00"})],
            set(),
            [],
            [],
        )


# ================================================================================================ 8: the MAC covers the computation body
async def test_the_publish_mac_covers_the_computation_body_and_the_override(client, tds, db):
    from app.services import payroll_publish_requests as prs

    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    p = await put_slip(client, tds, await slip(client, tds, run), tds="5000.00", tds_override_reason="Employee asked for less")
    assert p["tds_computation"]["override"]["amount"] == "5000.00"
    await process(client, tds, run)
    ps = (await db.execute(select(Payslip).execution_options(populate_existing=True))).scalar_one()
    await ptds.attach_macs(db, [ps])
    base = prs.result_mac(ps)
    row = (await rows(db))[0]
    row.computation = {**row.computation, "steps": {"x": "tampered"}}  # same decision fields, different body
    await db.flush()
    await ptds.attach_macs(db, [ps])
    changed = prs.result_mac(ps)
    assert changed != base
    row.override_reason = "A different reason"
    await db.flush()
    await ptds.attach_macs(db, [ps])
    assert prs.result_mac(ps) not in (base, changed)
    await db.rollback()


# ================================================================================================ 11: 24Q cell types
async def test_24q_amounts_are_numbers_formatted_with_two_decimals(client, tds):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    await process(client, tds, run)
    await publish(client, tds, run)
    r = await client.post(f"{TAX}/24q", params={"fy": FY, "quarter": 1}, json=PW, headers=tds["hr"])
    assert r.status_code == 200, r.text
    ws = load_workbook(io.BytesIO(r.content))["Deductees"]
    amount, tds_cell = ws["F2"], ws["G2"]
    assert amount.value == 200000 and tds_cell.value == 13780
    assert amount.number_format == "0.00" and tds_cell.number_format == "0.00" and amount.data_type == "n"
    ch = load_workbook(io.BytesIO(r.content))["Challans"]
    assert ch["B2"].number_format == "0.00"


# ================================================================================================ 12: statement picks the right computation
async def test_the_statement_uses_the_published_copy_not_a_newer_draft_revision(client, tds, db, uid):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    await process(client, tds, run)
    await publish(client, tds, run)
    r = await client.post(
        f"{B}/runs/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Recheck"}, headers=tds["hr"]
    )
    assert r.status_code == 201, r.text
    draft_id = uuid.UUID(r.json()["id"])
    tc = (await db.execute(select(PayrollTaxComputation).where(PayrollTaxComputation.payslip_id == draft_id))).scalar_one()
    tc.computation = {**tc.computation, "annual": {**tc.computation["annual"], "total_tax": "1.00"}}
    await db.commit()
    view, _footer, _logo = await reports.statement_view(db, None, uuid.UUID(uid["employee"]), FY)
    assert view["annual"]["total_tax"] == "137800.00"  # the published copy outranks the newer draft revision


async def test_the_statement_download_alerts_the_admins(client, tds, uid):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    await process(client, tds, run)
    await publish(client, tds, run)
    notifications.OUTBOX.clear()
    r = await client.post(f"{TAX}/employees/{uid['employee']}/statement", params={"fy": FY}, json=PW, headers=tds["hr"])
    assert r.status_code == 200, r.text
    alerts = [o["payload"] for o in notifications.OUTBOX if o["payload"]["type"] == "admin_alert"]
    assert len(alerts) == 1 and "statement" in alerts[0]["title"].lower()


# ================================================================================================ 16: row caps
async def test_report_loads_are_capped(client, tds, monkeypatch):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    await process(client, tds, run)
    await publish(client, tds, run)
    monkeypatch.setattr(reports, "MAX_REPORT_ROWS", 0)
    r = await client.get(f"{TAX}/summary", params={"fy": FY}, headers=tds["hr"])
    assert r.status_code == 422 and err(r) == "TOO_MANY_ROWS"
    from app.services import tax_24q

    monkeypatch.setattr(tax_24q, "MAX_REPORT_ROWS", 0)
    r = await client.post(f"{TAX}/24q", params={"fy": FY, "quarter": 1}, json=PW, headers=tds["hr"])
    assert r.status_code == 422 and err(r) == "TOO_MANY_ROWS"
    uid_row = await client.get(f"{TAX}/summary", params={"fy": FY}, headers=tds["hr"])
    assert uid_row.status_code == 422


# ================================================================================================ 3: rules superseded in a published run
async def test_a_revision_of_a_published_run_keeps_the_pinned_rules_with_a_non_blocking_notice(client, tds, db, uid):
    await engine(client, tds, mode="apply")
    run = await run_for(client, tds)
    await process(client, tds, run)
    await publish(client, tds, run)
    old = (await db.execute(select(ptds.TaxYearRule).where(ptds.TaxYearRule.fy == FY))).scalar_one()
    old.status = "superseded"
    await db.commit()
    await add_rules(db, uid, fy=FY, version=2, status="verified")
    r = await client.post(
        f"{B}/runs/{run['id']}/employees/{uid['employee']}/revise", json={"reason": "Recheck"}, headers=tds["hr"]
    )
    assert r.status_code == 201, r.text
    j = r.json()
    assert "RULES_SUPERSEDED" in j["warnings"] and "RULES_CHANGED" not in j["errors"]
    assert j["tds_computation"]["rules"]["version"] == 1


def test_the_new_blocking_codes():
    from app.services import payroll_calc as calc

    assert {"RULES_CHANGED", "TDS_SHORTFALL_FINAL"} <= calc.ERROR_CODES
    assert {"TDS_MANUAL_REPLACED", "PREV_EMPLOYER_UNVERIFIED", "RULES_SUPERSEDED"} <= calc.WARNING_CODES


# ================================================================================================ 14: F&F uses the engine figure
async def test_the_fnf_estimate_uses_the_engine_tds_when_it_is_on(client, H, uid, db):
    from tests.payroll_fixtures import add_structure, exclude_from_payroll, set_setting
    from tests.test_payroll_fnf import mk

    await set_setting(client, H["admin"], lop_absent_days=False)
    await exclude_from_payroll(client, H["admin"], uid["hr"])
    await exclude_from_payroll(client, H["hr"], uid["admin"])
    await add_structure(client, H["hr"], uid["employee"], basic="20000", hra="5000", tds_monthly="1500")
    f = await mk(client, H["hr"], uid["employee"])
    pv = (await client.get(f"/api/admin/payroll/fnf/{f['id']}/preview", headers=H["hr"])).json()
    assert pv["tds"] is None and any(d["code"] == "TDS" and d["amount"] == "1500.00" for d in pv["deductions"])
    await set_pan(db, uid["employee"], "ABCDE1234F")
    await add_rules(db, uid, fy="2025-26")
    await engine(client, H, mode="apply")
    pv = (await client.get(f"/api/admin/payroll/fnf/{f['id']}/preview", headers=H["hr"])).json()
    assert pv["tds"]["mode"] == "apply" and pv["tds"]["suggested"] == "0.00" and pv["tds"]["shortfall"] == "0.00"
    assert not any(
        d["code"] == "TDS" and Decimal(d["amount"]) > 0 for d in pv["deductions"]
    )  # the engine, not the structure default


def test_the_fnf_statement_shows_the_tds_shortfall():
    from types import SimpleNamespace

    from app.services import payroll_fnf_out as out

    res = SimpleNamespace(earnings=[], deductions=[], gross=Decimal("1"), total_deductions=Decimal("0"), net=Decimal("1"))
    days = SimpleNamespace(basis_days=30, employed_days=30, lop_auto=0)
    info = {"mode": "apply", "suggested": "9000.00", "applied": "6000.00", "shortfall": "3000.00"}
    v = out.estimate_view({"result": res, "days": days, "warnings": ["TDS_SHORTFALL_FINAL"], "tds_info": info})
    assert v["tds"] == info and "TDS_SHORTFALL_FINAL" in v["warnings"]
    assert out.estimate_view({"result": res, "days": days, "warnings": []})["tds"] is None
