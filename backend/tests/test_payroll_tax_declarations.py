"""Plan 17 work package 5b: investment declarations and proofs (contract 19.7): rules gate, windows, draft vs submitted (T3),
on-behalf entry, proof maker-checker (T4), file access, redaction, declarations xlsx."""

from __future__ import annotations

import datetime as dt
import io
import json
import uuid

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.core.clock import utcnow
from app.models import AuditLog, File, PayrollItemEvent, TaxDeclaration, TaxProof
from app.services import files as files_svc
from app.services import notifications
from tests.conftest import PASSWORD
from tests.payroll_controls_helpers import extra_user, quiet  # noqa: F401  (fixture)
from tests.payroll_tax_helpers import (  # noqa: F401  (fixtures)
    D_CLOSED,
    FY,
    ME,
    P_OPEN,
    PAN_EMP,
    TAX,
    add_rules,
    clerk,
    err,
    fld,
    item,
    pdf,
    save,
    submit,
    upload_proof_file,
)

pytestmark = pytest.mark.usefixtures("quiet", "clock", "storage")


async def verified(db, uid, **kw):
    return await add_rules(db, uid, status="verified", **kw)


async def declared(client, H, items=None, regime="old"):
    """EMP001 saves and submits a declaration; returns the submitted declaration."""
    d = await save(client, H["employee"], items or [item("80C", "100000.00", description="PPF")], regime=regime)
    return await submit(client, H["employee"], d["version"])


async def proof_ready(client, H, db, uid, amount="100000.00"):
    """Declared in the declaration window, proof window open: returns (declaration, file_id)."""
    d = await declared(client, H, [item("80C", amount, description="PPF")])
    return d, await upload_proof_file(client, H["employee"])


# ================================================================================================ rules gate + reads
async def test_employee_calls_need_verified_rules(client, H, db, uid):
    await add_rules(db, uid, status="draft")
    assert (await client.get(f"{ME}/declaration", headers=H["employee"])).status_code == 409
    r = await client.get(f"{ME}/declaration", headers=H["employee"])
    assert err(r) == "TAX_RULES_NOT_VERIFIED"
    r = await client.put(
        f"{ME}/declaration", json={"fy": FY, "expected_version": 0, "regime": "new", "items": []}, headers=H["employee"]
    )
    assert r.status_code == 409 and err(r) == "TAX_RULES_NOT_VERIFIED"
    years = (await client.get(f"{ME}/years", headers=H["employee"])).json()["items"]
    assert [(y["fy"], y["verified"], y["current"]) for y in years] == [(FY, False, True)]  # no rules, no limits shown


async def test_get_creates_nothing_and_shows_the_window_state(client, H, db, uid, clock):
    await verified(db, uid)
    r = await client.get(f"{ME}/declaration", headers=H["employee"])
    assert r.status_code == 200
    d = r.json()
    assert d["id"] is None and d["version"] == 0 and d["draft"] == {"regime": None, "items": []} and d["submitted"] is None
    assert d["can_edit"] is True and d["edit_reason"] == "DECLARATION_WINDOW" and d["rules_version"] == 1 and d["dirty"] is False
    assert (await db.execute(select(TaxDeclaration))).first() is None
    clock.day = D_CLOSED
    d = (await client.get(f"{ME}/declaration", headers=H["employee"])).json()
    assert d["can_edit"] is False and d["edit_reason"] == "CLOSED"
    clock.day = P_OPEN
    assert (await client.get(f"{ME}/declaration", headers=H["employee"])).json()["edit_reason"] == "PROOF_WINDOW"
    assert (await client.get(f"{ME}/declaration?fy=2026-28", headers=H["employee"])).status_code == 422
    assert (await client.get(f"{ME}/declaration?fy=2030-31", headers=H["employee"])).status_code == 409


# ================================================================================================ save / validation
async def test_save_normalises_items_and_masks_pans(client, H, db, uid):
    await verified(db, uid)
    items = [
        item("80C", "150000.00", description="PPF"),
        item(
            "HRA",
            None,
            monthly_rent="20000.00",
            from_month="2026-04",
            to_month="2027-03",
            landlord_name="R K",
            landlord_pan="abcpe1234f",
            city="Chennai",
            metro=True,
        ),
        item("24B", "180000.00", lender_name="Bank", lender_pan="PQRPT6789K"),
        item("OTHER_INCOME", "5000.00", description="Interest"),
    ]
    d = await save(client, H["employee"], items)
    assert d["version"] == 1 and d["id"]
    got = {i["section"]: i for i in d["draft"]["items"]}
    assert [i["id"] for i in d["draft"]["items"]] == ["i-1", "i-2", "i-3", "i-4"]
    assert got["HRA"]["amount"] == "240000.00"  # 12 months x 20000.00, computed
    assert got["HRA"]["details"]["landlord_pan"] == "XXXXX1234X" and got["24B"]["details"]["lender_pan"] == "XXXXX6789X"
    assert got["80C"]["flags"] == [] and d["dirty"] is True and d["submitted"] is None
    # ids are stable on the next save; an unknown id is refused
    items2 = [{**item("80C", "120000.00", description="PPF"), "id": "i-1"}]
    d2 = await save(client, H["employee"], items2, version=1)
    assert [i["id"] for i in d2["draft"]["items"]] == ["i-1"] and d2["version"] == 2
    r = await client.put(
        f"{ME}/declaration",
        json={
            "fy": FY,
            "expected_version": 2,
            "regime": "old",
            "items": [{**item("80C", "1.00", description="x"), "id": "i-77"}],
        },
        headers=H["employee"],
    )
    assert r.status_code == 422 and fld(r) == "items[0].id"


async def test_save_validation_errors(client, H, db, uid):
    await verified(db, uid)

    async def put(items, **kw):
        body = {"fy": FY, "expected_version": 0, "regime": "old", "items": items, **kw}
        return await client.put(f"{ME}/declaration", json=body, headers=H["employee"])

    r = await put([item("NOPE", "1000.00")])
    assert r.status_code == 422 and err(r) == "INVALID_SECTION"
    r = await put([item("80C", "1000.00", lender_pan="ABCPE1234F")])
    assert r.status_code == 422 and fld(r) == "items[0].details.lender_pan"  # not a field of 80C
    r = await put([item("24B", "1000.00", lender_pan="not-a-pan")])
    assert r.status_code == 422 and fld(r) == "items[0].details.lender_pan"
    for bad in ("0.00", "-5.00", "12.345", "abc", "999999999999.00"):
        r = await put([item("80C", bad)])
        assert r.status_code == 422, bad
    r = await put([item("80C", None)])
    assert r.status_code == 422 and fld(r) == "items[0].amount"
    r = await put([item("HRA", None, monthly_rent="20000.00", from_month="2026-04", to_month="2027-03")])
    assert r.status_code == 422 and err(r) == "LANDLORD_PAN_REQUIRED"  # 240000 a year > 100000.00 threshold
    r = await put(
        [item("HRA", None, monthly_rent="5000.00", from_month="2026-03", to_month="2026-09", landlord_pan="ABCPE1234F")]
    )
    assert r.status_code == 422 and fld(r) == "items[0].details.from_month"  # before the financial year
    r = await put(
        [item("HRA", None, monthly_rent="5000.00", from_month="2026-09", to_month="2026-05", landlord_pan="ABCPE1234F")]
    )
    assert r.status_code == 422 and fld(r) == "items[0].details.to_month"
    r = await put(
        [item("HRA", None, monthly_rent="10000001.00", from_month="2026-04", to_month="2026-04", landlord_pan="ABCPE1234F")]
    )
    assert r.status_code == 422 and fld(r) == "items[0].details.monthly_rent"
    r = await put([item("80C", "1.00", description="x")] * 41)
    assert r.status_code == 422
    r = await put([item("80C", "1.00", description="x")], regime="both")
    assert r.status_code == 422
    r = await client.put(f"{ME}/declaration", json={"fy": FY, "expected_version": 0, "items": []}, headers=H["employee"])
    assert r.status_code == 422  # regime must be sent (null clears it)
    r = await client.put(
        f"{ME}/declaration", json={"fy": FY, "expected_version": 0, "regime": None, "items": [], "x": 1}, headers=H["employee"]
    )
    assert r.status_code == 422
    ok = await put(
        [item("HRA", None, monthly_rent="5000.00", from_month="2026-04", to_month="2026-09")]
    )  # 30000 a year: no PAN needed
    assert ok.status_code == 200, ok.text


async def test_flags_not_in_regime_and_above_limit(client, H, db, uid):
    await verified(db, uid)
    items = [
        item("80D_SELF", "30000.00", description="Policy", senior=False),  # above the 25000.00 limit
        item("80D_SELF", "30000.00", description="Policy", senior=True),  # senior limit 50000.00: fine
        item("PREV_EMPLOYER", "100000.00", income="100000.00"),  # regime independent
    ]
    d = await save(client, H["employee"], items, regime="new")  # the new regime allows none of the Chapter VI-A sections
    assert [i["flags"] for i in d["draft"]["items"]] == [["ABOVE_LIMIT", "NOT_IN_REGIME"], ["NOT_IN_REGIME"], []]
    d = await save(client, H["employee"], items, regime="old", version=1)
    assert [i["flags"] for i in d["draft"]["items"]] == [["ABOVE_LIMIT"], [], []]


async def test_optimistic_locking(client, H, db, uid):
    await verified(db, uid)
    d = await save(client, H["employee"], [item("80C", "1000.00", description="a")])
    r = await client.put(
        f"{ME}/declaration", json={"fy": FY, "expected_version": 0, "regime": "old", "items": []}, headers=H["employee"]
    )
    assert r.status_code == 409 and err(r) == "VERSION_CONFLICT"
    r = await client.put(
        f"{ME}/declaration", json={"fy": FY, "expected_version": 5, "regime": "old", "items": []}, headers=H["employee"]
    )
    assert err(r) == "VERSION_CONFLICT"
    r = await client.post(
        f"{ME}/declaration/submit", json={"fy": FY, "expected_version": d["version"] + 1}, headers=H["employee"]
    )
    assert err(r) == "VERSION_CONFLICT"
    r = await client.post(f"{ME}/declaration/submit", json={"fy": FY, "expected_version": 0}, headers=H["employee"])
    assert r.status_code == 422  # ge=1


# ================================================================================================ draft vs submitted (T3)
async def test_draft_never_feeds_the_submitted_version(client, H, db, uid):
    await verified(db, uid)
    await save(client, H["employee"], [item("80C", "100000.00", description="PPF")])
    r = await client.post(f"{ME}/declaration/submit", json={"fy": FY, "expected_version": 1}, headers=H["employee"])
    assert r.status_code == 200
    s = r.json()
    assert s["submitted"]["version"] == 1 and s["submitted"]["regime"] == "old" and s["dirty"] is False and s["version"] == 2
    assert (await submit(client, H["employee"], 2, expect=422)) and True  # identical: NO_CHANGE
    r = await client.post(f"{ME}/declaration/submit", json={"fy": FY, "expected_version": 2}, headers=H["employee"])
    assert err(r) == "NO_CHANGE"
    # an edit changes the draft only
    e = await save(client, H["employee"], [{**item("80C", "50000.00", description="PPF"), "id": "i-1"}], version=2, regime="new")
    assert e["dirty"] is True and e["draft"]["regime"] == "new" and e["draft"]["items"][0]["amount"] == "50000.00"
    assert e["submitted"]["regime"] == "old" and e["submitted"]["items"][0]["amount"] == "100000.00"
    row = (await db.execute(select(TaxDeclaration))).scalars().one()
    await db.refresh(row)
    assert row.submitted["items"][0]["amount"] == "100000.00" and row.regime == "old" and row.submitted_version == 1
    again = await submit(client, H["employee"], e["version"])
    assert (
        again["submitted"]["version"] == 2
        and again["submitted"]["items"][0]["amount"] == "50000.00"
        and again["submitted"]["regime"] == "new"
    )
    events = [
        e.action
        for e in (await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.item_type == "tax_declaration"))).scalars()
    ]
    assert events.count("submit") == 2 and "create" in events and "update" in events


async def test_submit_requires_a_regime_and_something(client, H, db, uid):
    await verified(db, uid)
    d = await save(client, H["employee"], [item("80C", "1000.00", description="a")], regime=None)
    r = await client.post(f"{ME}/declaration/submit", json={"fy": FY, "expected_version": d["version"]}, headers=H["employee"])
    assert r.status_code == 422 and fld(r) == "regime"
    r = await client.post(f"{ME}/declaration/submit", json={"fy": "2026-27", "expected_version": 1}, headers=H["manager"])
    assert err(r) == "NO_CHANGE"  # nothing saved by the manager


# ================================================================================================ windows, regime lock, unlock
async def test_window_closed_and_regime_locked(client, H, db, uid, clock):
    await verified(db, uid)
    d = await declared(
        client, H, [item("80C", "100000.00", description="PPF"), item("OTHER_INCOME", "100.00", description="int")]
    )
    clock.day = D_CLOSED
    r = await client.put(
        f"{ME}/declaration",
        json={"fy": FY, "expected_version": d["version"], "regime": "old", "items": []},
        headers=H["employee"],
    )
    assert r.status_code == 409 and err(r) == "WINDOW_CLOSED"
    r = await client.post(f"{ME}/declaration/submit", json={"fy": FY, "expected_version": d["version"]}, headers=H["employee"])
    assert err(r) == "WINDOW_CLOSED"
    # proof window: proof items yes, regime and other items no
    clock.day = P_OPEN
    keep = [{**x, "details": x["details"]} for x in (d["draft"]["items"])]
    items = [{"id": i["id"], "section": i["section"], "amount": i["amount"], "details": i["details"]} for i in keep]
    r = await client.put(
        f"{ME}/declaration",
        json={"fy": FY, "expected_version": d["version"], "regime": "new", "items": items},
        headers=H["employee"],
    )
    assert r.status_code == 409 and err(r) == "REGIME_LOCKED"
    changed = [dict(i) for i in items]
    changed[1]["amount"] = "999.00"  # OTHER_INCOME needs no proof: locked
    r = await client.put(
        f"{ME}/declaration",
        json={"fy": FY, "expected_version": d["version"], "regime": "old", "items": changed},
        headers=H["employee"],
    )
    assert r.status_code == 409 and err(r) == "WINDOW_CLOSED"
    changed = [dict(i) for i in items]
    changed[0]["amount"] = "90000.00"  # 80C needs a proof: allowed
    r = await client.put(
        f"{ME}/declaration",
        json={"fy": FY, "expected_version": d["version"], "regime": "old", "items": changed},
        headers=H["employee"],
    )
    assert r.status_code == 200 and r.json()["edit_reason"] == "PROOF_WINDOW"
    r = await client.post(
        f"{ME}/declaration/submit", json={"fy": FY, "expected_version": r.json()["version"]}, headers=H["employee"]
    )
    assert r.status_code == 200 and r.json()["submitted"]["version"] == 2


async def test_hr_unlock_reopens_editing_but_keeps_the_regime_unless_an_approver_allows_it(client, H, db, uid, clock, seeded):
    await verified(db, uid)
    _, clk = await clerk(client, db, seeded)
    await declared(client, H)
    clock.day = D_CLOSED
    body = {"fy": FY, "until": "2026-09-30", "reason": "Joined late", "current_password": PASSWORD}
    url = f"{TAX}/declarations/{uid['employee']}/unlock"
    assert (await client.post(url, json=body, headers=H["employee"])).status_code == 403
    r = await client.post(f"{TAX}/declarations/{uid['hr']}/unlock", json=body, headers=H["hr"])
    assert r.status_code == 403 and err(r) == "FORBIDDEN"  # not self
    for bad in ({**body, "until": "2026-08-14"}, {**body, "until": "2027-04-01"}, {**body, "reason": "x"}):
        r = await client.post(url, json=bad, headers=H["hr"])
        assert r.status_code == 422, bad
    no_pw = {k: v for k, v in body.items() if k != "current_password"}
    assert (await client.post(url, json=no_pw, headers=H["hr"])).status_code == 422  # M2: step-up is required
    r = await client.post(url, json={**body, "current_password": "wrong"}, headers=H["hr"])
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    r = await client.post(url, json={**body, "allow_regime_change": True}, headers=clk)
    assert r.status_code == 403 and err(r) == "FORBIDDEN"  # only payroll.approve holders may free the regime
    assert (await db.execute(select(TaxDeclaration).where(TaxDeclaration.unlocked_until.is_not(None)))).first() is None
    notifications.OUTBOX.clear()
    r = await client.post(url, json=body, headers=H["hr"])
    assert r.status_code == 200 and r.json()["unlocked_until"] == "2026-09-30" and r.json()["edit_reason"] == "UNLOCKED"
    types = [m["payload"]["type"] for m in notifications.OUTBOX]
    assert "tax_declaration_changed" in types  # the employee is told
    items = [{"id": "i-1", "section": "80C", "amount": "50000.00", "details": {"description": "PPF"}}]
    version = r.json()["version"]
    r = await client.put(
        f"{ME}/declaration", json={"fy": FY, "expected_version": version, "regime": "new", "items": items}, headers=H["employee"]
    )
    assert r.status_code == 409 and err(r) == "REGIME_LOCKED"  # an unlock alone does not free the regime
    r = await client.put(
        f"{ME}/declaration", json={"fy": FY, "expected_version": version, "regime": "old", "items": items}, headers=H["employee"]
    )
    assert r.status_code == 200 and r.json()["draft"]["regime"] == "old"
    version = r.json()["version"]
    # an approver (ADM001) unlocks with allow_regime_change: the regime may change
    r = await client.post(url, json={**body, "allow_regime_change": True}, headers=H["admin"])
    assert r.status_code == 200
    version = r.json()["version"]
    r = await client.put(
        f"{ME}/declaration", json={"fy": FY, "expected_version": version, "regime": "new", "items": items}, headers=H["employee"]
    )
    assert r.status_code == 200 and r.json()["draft"]["regime"] == "new"
    clock.day = dt.date(2026, 10, 1)  # past the unlock date
    r = await client.put(
        f"{ME}/declaration",
        json={"fy": FY, "expected_version": r.json()["version"], "regime": "new", "items": items},
        headers=H["employee"],
    )
    assert err(r) == "WINDOW_CLOSED"
    ev = [e for e in (await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.action == "unlock"))).scalars()]
    assert len(ev) == 2 and {e.detail["reason"] for e in ev} == {"Joined late"}
    assert sorted(e.detail["allow_regime_change"] for e in ev) == [False, True]
    row = (await db.execute(select(TaxDeclaration))).scalars().one()
    assert row.unlock_reason == "Joined late"
    audits = list((await db.execute(select(AuditLog).where(AuditLog.action == "payroll.tax_declaration.unlock"))).scalars())
    assert len(audits) == 2 and "Joined late" not in json.dumps([a.after for a in audits])


# ================================================================================================ on behalf
async def test_hr_enters_a_declaration_on_behalf(client, H, db, uid):
    await verified(db, uid)
    body = {"fy": FY, "expected_version": 0, "regime": "old", "items": [item("80C", "70000.00", description="LIC")]}
    url = f"{TAX}/declarations/{uid['employee']}"
    assert (await client.put(url, json=body, headers=H["employee"])).status_code == 403  # no payroll.manage
    r = await client.put(url, json=body, headers=H["hr"])
    assert r.status_code == 422  # step-up password missing
    r = await client.put(url, json={**body, "current_password": "wrong"}, headers=H["hr"])
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    r = await client.put(f"{TAX}/declarations/{uid['hr']}", json={**body, "current_password": PASSWORD}, headers=H["hr"])
    assert r.status_code == 403 and err(r) == "FORBIDDEN"  # not self
    notifications.OUTBOX.clear()
    r = await client.put(url, json={**body, "current_password": PASSWORD}, headers=H["hr"])
    assert r.status_code == 200, r.text
    got = r.json()
    assert (
        got["entered_by"]["employee_id"] == "HR001"
        and got["user"]["employee_id"] == "EMP001"
        and got["draft"]["items"][0]["amount"] == "70000.00"
    )
    assert [m for m in notifications.OUTBOX if m["payload"]["type"] == "tax_declaration_changed"]
    r = await client.post(
        f"{TAX}/declarations/{uid['employee']}/submit",
        json={"fy": FY, "expected_version": got["version"], "current_password": PASSWORD},
        headers=H["hr"],
    )
    assert r.status_code == 200 and r.json()["submitted"]["version"] == 1
    # the audit row marks the entry and carries no values
    rows = list((await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.tax_declaration.%")))).scalars())
    assert any(r.after.get("on_behalf") is True for r in rows)
    assert "70000" not in json.dumps([r.after for r in rows])
    # the employee sees the HR entry
    me = (await client.get(f"{ME}/declaration", headers=H["employee"])).json()
    assert me["submitted"]["items"][0]["amount"] == "70000.00"


# ================================================================================================ proofs
async def test_proof_lifecycle_and_validation(client, H, db, uid, clock):
    await verified(db, uid)
    d, fid = await proof_ready(client, H, db, uid)
    item_id = d["submitted"]["items"][0]["id"]
    body = {"fy": FY, "item_id": item_id, "claimed": "100000.00", "file_ids": [fid]}
    r = await client.post(f"{ME}/proofs", json=body, headers=H["employee"])
    assert r.status_code == 409 and err(r) == "WINDOW_CLOSED"  # the declaration window is open, the proof window is not
    clock.day = P_OPEN
    r = await client.post(f"{ME}/proofs", json={**body, "item_id": "i-99"}, headers=H["employee"])
    assert r.status_code == 422 and err(r) == "ITEM_NOT_SUBMITTED"
    r = await client.post(f"{ME}/proofs", json={**body, "claimed": "100000.01"}, headers=H["employee"])
    assert r.status_code == 422 and err(r) == "AMOUNT_ABOVE_LIMIT"
    r = await client.post(f"{ME}/proofs", json={**body, "claimed": "0.00"}, headers=H["employee"])
    assert r.status_code == 422
    r = await client.post(f"{ME}/proofs", json={**body, "file_ids": [str(uuid.uuid4())]}, headers=H["employee"])
    assert r.status_code == 422 and fld(r) == "file_ids[0]"
    r = await client.post(f"{ME}/proofs", json={**body, "file_ids": [fid, fid]}, headers=H["employee"])
    assert r.status_code == 422
    r = await client.post(f"{ME}/proofs", json={**body, "file_ids": []}, headers=H["employee"])
    assert r.status_code == 422
    other = await upload_proof_file(client, H["manager"])  # somebody else's upload
    r = await client.post(f"{ME}/proofs", json={**body, "file_ids": [other]}, headers=H["employee"])
    assert r.status_code == 422 and fld(r) == "file_ids[0]"
    r = await client.post(f"{ME}/proofs", json={**body, "claimed": "60000.00"}, headers=H["employee"])
    assert r.status_code == 201, r.text
    p = r.json()
    assert (
        p["status"] == "submitted"
        and p["claimed"] == "60000.00"
        and p["files"][0]["id"] == fid
        and p["version"] == 1
        and p["item_id"] == item_id
    )
    assert p["files"][0]["url"] == f"/api/files/{fid}" and p["files"][0]["content_type"] == "application/pdf"
    # one live proof per item; the upload is used
    fid2 = await upload_proof_file(client, H["employee"])
    r = await client.post(f"{ME}/proofs", json={**body, "file_ids": [fid2]}, headers=H["employee"])
    assert r.status_code == 409
    fid3 = await upload_proof_file(client, H["employee"])
    mine = (await client.get(f"{ME}/proofs", headers=H["employee"])).json()["items"]
    assert [x["id"] for x in mine] == [p["id"]]
    assert (await client.get(f"{ME}/proofs", headers=H["manager"])).json()["items"] == []
    # cancel: owner only
    assert (await client.post(f"{ME}/proofs/{p['id']}/cancel", headers=H["manager"])).status_code == 404
    c = await client.post(f"{ME}/proofs/{p['id']}/cancel", headers=H["employee"])
    assert c.status_code == 200 and c.json()["status"] == "cancelled"
    assert err(await client.post(f"{ME}/proofs/{p['id']}/cancel", headers=H["employee"])) == "NOT_PENDING"
    # a cancelled proof frees the item AND its files
    r = await client.post(f"{ME}/proofs", json={**body, "file_ids": [fid, fid3]}, headers=H["employee"])
    assert r.status_code == 201, r.text
    denied = list((await db.execute(select(AuditLog).where(AuditLog.action == "payroll.denied"))).scalars())
    assert len(denied) == 1  # the manager's probe


async def test_proof_file_count_follows_the_setting(client, H, db, uid, clock):
    await verified(db, uid)
    d, fid = await proof_ready(client, H, db, uid)
    clock.day = P_OPEN
    r = await client.put("/api/admin/payroll/settings", json={"settings": {"payroll.tax_proof_max_files": 1}}, headers=H["admin"])
    assert r.status_code == 200, r.text
    f2 = await upload_proof_file(client, H["employee"])
    r = await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-1", "claimed": "1.00", "file_ids": [fid, f2]}, headers=H["employee"]
    )
    assert r.status_code == 422 and fld(r) == "file_ids"


async def decide_setup(client, H, db, uid, clock):
    await verified(db, uid)
    d, fid = await proof_ready(client, H, db, uid)
    clock.day = P_OPEN
    r = await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-1", "claimed": "80000.00", "file_ids": [fid]}, headers=H["employee"]
    )
    assert r.status_code == 201, r.text
    return r.json(), fid


async def test_proof_verify_and_reject(client, H, db, uid, clock):
    p, fid = await decide_setup(client, H, db, uid, clock)
    url = f"{TAX}/proofs/{p['id']}"
    assert (
        await client.post(f"{url}/verify", json={"expected_version": 1, "verified": "1.00"}, headers=H["employee"])
    ).status_code == 403
    r = await client.post(f"{url}/verify", json={"expected_version": 9, "verified": "1.00"}, headers=H["hr"])
    assert err(r) == "VERSION_CONFLICT"
    r = await client.post(f"{url}/verify", json={"expected_version": 1, "verified": "80000.01"}, headers=H["hr"])
    assert r.status_code == 422 and err(r) == "AMOUNT_ABOVE_LIMIT"
    r = await client.post(f"{url}/verify", json={"expected_version": 1, "verified": "0.00"}, headers=H["hr"])
    assert r.status_code == 422 and fld(r) == "comment"
    r = await client.post(f"{url}/reject", json={"expected_version": 1}, headers=H["hr"])
    assert r.status_code == 422
    notifications.OUTBOX.clear()
    r = await client.post(
        f"{url}/verify", json={"expected_version": 1, "verified": "75000.00", "comment": "Partly legible"}, headers=H["hr"]
    )
    assert r.status_code == 200, r.text
    v = r.json()
    assert (
        v["status"] == "verified"
        and v["verified"] == "75000.00"
        and v["comment"] == "Partly legible"
        and v["user"]["employee_id"] == "EMP001"
    )
    push = [m for m in notifications.OUTBOX if m["payload"]["type"] == "tax_proof_decision"]
    assert (
        len(push) == 1
        and push[0]["payload"]["title"] == "Investment proof verified"
        and push[0]["payload"]["body"] == "Section 80C"
    )
    assert "75000" not in json.dumps(push[0]["payload"])
    again = await client.post(f"{url}/reject", json={"expected_version": 2, "comment": "Too late"}, headers=H["admin"])
    assert again.status_code == 409 and err(again) == "NOT_PENDING"
    me = (await client.get(f"{ME}/proofs", headers=H["employee"])).json()["items"][0]
    assert me["status"] == "verified" and me["verified"] == "75000.00"
    d = (await client.get(f"{ME}/declaration", headers=H["employee"])).json()
    assert d["proofs"] == {"submitted": 0, "verified": 1, "rejected": 0}
    assert (await client.post(f"{ME}/proofs/{p['id']}/cancel", headers=H["employee"])).status_code == 409
    audit_rows = list((await db.execute(select(AuditLog).where(AuditLog.action == "payroll.tax_proof.verify"))).scalars())
    assert audit_rows[0].after["checker_id"] == str(uid["hr"]) and "75000" not in json.dumps(audit_rows[0].after)


async def test_proof_reject_frees_the_item(client, H, db, uid, clock):
    p, fid = await decide_setup(client, H, db, uid, clock)
    notifications.OUTBOX.clear()
    r = await client.post(
        f"{TAX}/proofs/{p['id']}/reject", json={"expected_version": 1, "comment": "Blurred scan"}, headers=H["admin"]
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["comment"] == "Blurred scan"
    assert [m for m in notifications.OUTBOX if m["payload"]["title"] == "Investment proof rejected"]
    f2 = await upload_proof_file(client, H["employee"])
    r = await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-1", "claimed": "80000.00", "file_ids": [f2]}, headers=H["employee"]
    )
    assert r.status_code == 201  # a new proof for the same item after a rejection
    events = [
        e.action for e in (await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.item_type == "tax_proof"))).scalars()
    ]
    assert events.count("create") == 2 and "reject" in events


async def test_proof_decider_is_not_the_employee_nor_the_person_who_entered_the_declaration(client, H, db, uid, clock, seeded):
    await verified(db, uid)
    # HR enters the employee's declaration on behalf; the employee uploads the proof
    base = {
        "fy": FY,
        "expected_version": 0,
        "regime": "old",
        "items": [item("80C", "90000.00", description="LIC")],
        "current_password": PASSWORD,
    }
    r = await client.put(f"{TAX}/declarations/{uid['employee']}", json=base, headers=H["hr"])
    assert r.status_code == 200
    r = await client.post(
        f"{TAX}/declarations/{uid['employee']}/submit",
        json={"fy": FY, "expected_version": 1, "current_password": PASSWORD},
        headers=H["hr"],
    )
    assert r.status_code == 200
    fid = await upload_proof_file(client, H["employee"])
    clock.day = P_OPEN
    r = await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-1", "claimed": "90000.00", "file_ids": [fid]}, headers=H["employee"]
    )
    p = r.json()
    url = f"{TAX}/proofs/{p['id']}"
    for action, body in (("verify", {"verified": "90000.00"}), ("reject", {"comment": "Not legible"})):
        r = await client.post(f"{url}/{action}", json={"expected_version": 1, **body}, headers=H["hr"])
        assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED", action
    # a second HR person who also edited on behalf is caught by the money history, not only by entered_by
    _, hr2 = await extra_user(client, db, seeded, "HR002", "hr")
    row = (await db.execute(select(TaxDeclaration))).scalars().one()
    await db.refresh(row)
    assert row.entered_by == uuid.UUID(uid["hr"])  # set once
    r = await client.put(
        f"{TAX}/declarations/{uid['employee']}",
        json={
            **base,
            "expected_version": row.version,
            "items": [{"id": "i-1", "section": "80C", "amount": "90000.00", "details": {"description": "LIC"}}],
        },
        headers=hr2,
    )
    assert r.status_code == 200, r.text
    await db.refresh(row)
    assert row.entered_by == uuid.UUID(uid["hr"])
    r = await client.post(f"{url}/verify", json={"expected_version": 1, "verified": "1.00"}, headers=hr2)
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    # ADM001 never touched the declaration: allowed
    r = await client.post(f"{url}/verify", json={"expected_version": 1, "verified": "90000.00"}, headers=H["admin"])
    assert r.status_code == 200 and r.json()["verified"] == "90000.00"


async def test_hr_cannot_decide_their_own_proof(client, H, db, uid, clock):
    await verified(db, uid)
    d = await save(client, H["hr"], [item("80C", "40000.00", description="PPF")])
    await submit(client, H["hr"], d["version"])
    fid = await upload_proof_file(client, H["hr"])
    clock.day = P_OPEN
    r = await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-1", "claimed": "40000.00", "file_ids": [fid]}, headers=H["hr"]
    )
    p = r.json()
    r = await client.post(f"{TAX}/proofs/{p['id']}/verify", json={"expected_version": 1, "verified": "40000.00"}, headers=H["hr"])
    assert r.status_code == 403 and err(r) == "FORBIDDEN"
    assert list((await db.execute(select(AuditLog).where(AuditLog.action == "payroll.denied"))).scalars())
    assert (
        await client.post(
            f"{TAX}/proofs/{p['id']}/verify", json={"expected_version": 1, "verified": "40000.00"}, headers=H["admin"]
        )
    ).status_code == 200
    assert (
        await client.post(
            f"{TAX}/proofs/{uuid.uuid4()}/verify", json={"expected_version": 1, "verified": "1.00"}, headers=H["admin"]
        )
    ).status_code == 404


# ================================================================================================ files
async def test_proof_files_are_owner_only_until_referenced_then_visible_to_payroll_viewers(client, H, db, uid, clock):
    await verified(db, uid)
    d, fid = await proof_ready(client, H, db, uid)
    f = await db.get(File, uuid.UUID(fid))
    assert f.purpose == "tax_proof" and f.is_encrypted is True
    assert (await client.get(f"/api/files/{fid}", headers=H["employee"])).status_code == 200  # owner
    assert (await client.get(f"/api/files/{fid}", headers=H["hr"])).status_code == 404  # not attached to a proof yet
    clock.day = P_OPEN
    await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-1", "claimed": "1000.00", "file_ids": [fid]}, headers=H["employee"]
    )
    r = await client.get(f"/api/files/{fid}", headers=H["hr"])
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")
    assert (await client.get(f"/api/files/{fid}", headers=H["manager"])).status_code == 404
    seen = list((await db.execute(select(AuditLog).where(AuditLog.action == "file.view_other"))).scalars())
    assert len(seen) == 1 and seen[0].after["purpose"] == "tax_proof"
    # the purpose is accepted from POST /files for everybody with payroll.view_own, form16 is not
    r = await client.post(
        "/api/files", data={"purpose": "form16"}, files={"file": ("a.pdf", pdf("x"), "application/pdf")}, headers=H["hr"]
    )
    assert r.status_code == 422
    r = await client.post(
        "/api/files", data={"purpose": "tax_proof"}, files={"file": ("a.txt", b"hello", "text/plain")}, headers=H["employee"]
    )
    assert r.status_code == 422


async def test_orphan_purge_keeps_files_a_proof_references(client, H, db, uid, clock):
    await verified(db, uid)
    d, fid = await proof_ready(client, H, db, uid)
    unused = await upload_proof_file(client, H["employee"])
    clock.day = P_OPEN
    await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-1", "claimed": "1000.00", "file_ids": [fid]}, headers=H["employee"]
    )
    for row in (await db.execute(select(File))).scalars():
        row.created_at = utcnow() - dt.timedelta(days=3)
    await db.commit()
    removed = await files_svc.cleanup_orphans(db)
    ids = {str(f.id) for f in (await db.execute(select(File))).scalars()}
    assert removed == 1 and fid in ids and unused not in ids


# ================================================================================================ lists and reports
async def test_admin_lists_and_reveal(client, H, db, uid, clock):
    await verified(db, uid)
    await add_rules(db, uid, fy="2025-26", status="verified")  # another year does not matter
    await declared(
        client, H,
        [item("24B", "50000.00", lender_name="Bank", lender_pan="PQRPT6789K"), item("80C", "10000.00", description="x")],
    )  # fmt: skip
    r = await client.get(f"{TAX}/declarations", params={"fy": FY}, headers=H["hr"])
    assert r.status_code == 200
    items = {i["user"]["employee_id"]: i for i in r.json()["items"]}
    assert items["EMP001"]["status"] == "submitted" and items["EMP001"]["regime"] == "old" and items["EMP001"]["items"] == 2
    assert items["MGR001"]["status"] == "none" and items["MGR001"]["regime"] is None and items["MGR001"]["flags"] == ["NO_PAN"]
    assert r.json()["total"] == len(r.json()["items"])
    assert "50000" not in json.dumps(r.json())  # the list carries no money
    for st, who in (("none", "MGR001"), ("submitted", "EMP001")):
        got = (await client.get(f"{TAX}/declarations", params={"fy": FY, "status": st}, headers=H["hr"])).json()["items"]
        assert who in {i["user"]["employee_id"] for i in got} and all(i["status"] == st for i in got)
    assert (await client.get(f"{TAX}/declarations", params={"fy": FY, "q": "emp001"}, headers=H["hr"])).json()["total"] == 1
    assert (await client.get(f"{TAX}/declarations", params={"fy": FY, "regime": "new"}, headers=H["hr"])).json()["total"] == 0
    assert (await client.get(f"{TAX}/declarations", headers=H["employee"])).status_code == 403
    masked = (await client.get(f"{TAX}/declarations/{uid['employee']}", params={"fy": FY}, headers=H["hr"])).json()
    assert masked["submitted"]["items"][0]["details"]["lender_pan"] == "XXXXX6789X" and masked["user"]["employee_id"] == "EMP001"
    rev = f"{TAX}/declarations/{uid['employee']}/reveal"
    r = await client.get(f"{TAX}/declarations/{uid['employee']}", params={"fy": FY, "reveal": "true"}, headers=H["hr"])
    assert r.status_code == 422 and "PQRPT6789K" not in r.text  # M5: GET never reveals
    assert (await client.post(rev, json={"fy": FY}, headers=H["hr"])).status_code == 422
    r = await client.post(rev, json={"fy": FY, "current_password": "nope"}, headers=H["hr"])
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    # payroll.manage is required as well as the password
    r = await client.post(rev, json={"fy": FY, "current_password": PASSWORD}, headers=H["manager"])
    assert r.status_code == 403
    r = await client.post(rev, json={"fy": FY, "current_password": PASSWORD}, headers=H["hr"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    shown = r.json()
    assert shown["submitted"]["items"][0]["details"]["lender_pan"] == "PQRPT6789K"
    views = [
        r.after for r in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.tax_declaration.view"))).scalars()
    ]
    assert sorted(v["revealed"] for v in views) == [False, True]
    assert "PQRPT6789K" not in json.dumps([r.after for r in (await db.execute(select(AuditLog))).scalars()])
    assert (await client.get(f"{TAX}/declarations/{uuid.uuid4()}", headers=H["hr"])).status_code == 404
    assert (await client.get(f"{TAX}/proofs", params={"fy": FY}, headers=H["hr"])).json()["items"] == []
    assert (await client.get(f"{TAX}/proofs", headers=H["employee"])).status_code == 403


async def test_declarations_xlsx_masks_identifiers_unless_asked_with_the_password(client, H, db, uid, clock):
    await verified(db, uid)
    await declared(client, H, [item("24B", "50000.00", lender_name="Bank", lender_pan="PQRPT6789K")])
    url = f"{TAX}/reports/declarations?fy={FY}"
    r = await client.post(url, json={}, headers=H["hr"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    ws = load_workbook(io.BytesIO(r.content)).active
    rows = list(ws.iter_rows(values_only=True))
    assert (
        ws.title == "Declarations"
        and rows[0][0] == "Employee ID"
        and rows[1][0] == "EMP001"
        and rows[1][4] == "24B"
        and rows[1][5] == 50000.0
    )
    assert rows[1][7] == "XXXXX6789X"
    r = await client.post(url, json={"include_identifiers": True}, headers=H["hr"])
    assert r.status_code == 422  # step-up
    r = await client.post(url, json={"include_identifiers": True, "current_password": "bad"}, headers=H["hr"])
    assert r.status_code == 403
    r = await client.post(url, json={"include_identifiers": True, "current_password": PASSWORD}, headers=H["hr"])
    assert list(load_workbook(io.BytesIO(r.content)).active.iter_rows(values_only=True))[1][7] == "PQRPT6789K"
    assert (await client.post(url, json={}, headers=H["employee"])).status_code == 403
    audits = list((await db.execute(select(AuditLog).where(AuditLog.action == "payroll.report.export"))).scalars())
    assert [a.after["kind"] for a in audits] == ["tax_declarations", "tax_declarations"] and "PQRST" not in json.dumps(
        [a.after for a in audits]
    )


async def test_rows_are_unreadable_without_the_key_and_encrypted_at_rest(client, H, db, uid):
    await verified(db, uid)
    await save(client, H["employee"], [item("24B", "50000.00", lender_name="Bank", lender_pan="PQRPT6789K")])
    raw = (await db.execute(__import__("sqlalchemy").text("SELECT draft_enc FROM tax_declarations"))).scalar_one()
    assert b"PQRPT6789K" not in bytes(raw) and b"50000" not in bytes(raw)
    p = TaxProof.__table__.c
    assert p.claimed_enc is not None and p.verified_enc is not None and p.comment_enc is not None


# ================================================================================================ phase 5b security review
async def test_masked_or_untyped_pans_are_refused_and_a_masked_roundtrip_keeps_the_stored_pan(client, H, db, uid):
    await verified(db, uid)

    async def put(items, version=0):
        return await client.put(
            f"{ME}/declaration",
            json={"fy": FY, "expected_version": version, "regime": "old", "items": items},
            headers=H["employee"],
        )

    for bad in ("XXXXX1234X", "xxxxx1234x", "ABCDE1234F", "ABCXE1234F"):  # the mask, and a 4th letter that is no holder type
        r = await put([item("24B", "1000.00", lender_name="Bank", lender_pan=bad)])
        assert r.status_code == 422 and fld(r) == "items[0].details.lender_pan", bad
    d = (await put([item("24B", "1000.00", lender_name="Bank", lender_pan="PQRPT6789K")])).json()
    shown = d["draft"]["items"][0]
    assert shown["details"]["lender_pan"] == "XXXXX6789X"
    # the app sends back exactly what it was shown: the stored PAN stays
    echo = [{"id": shown["id"], "section": "24B", "amount": "2000.00", "details": shown["details"]}]
    d = (await put(echo, d["version"])).json()
    assert d["draft"]["items"][0]["amount"] == "2000.00"
    row = (await db.execute(select(TaxDeclaration))).scalars().one()
    await db.refresh(row)
    assert row.draft["items"][0]["details"]["lender_pan"] == "PQRPT6789K"
    # a NEW item (no id) cannot use the mask
    r = await put(echo + [item("24B", "10.00", lender_name="B", lender_pan="XXXXX6789X")], d["version"])
    assert r.status_code == 422
    # the mask of a different PAN than the stored one is no shortcut either
    other = [
        {"id": shown["id"], "section": "24B", "amount": "2000.00", "details": {**shown["details"], "lender_pan": "XXXXX1111X"}}
    ]
    assert (await put(other, d["version"])).status_code == 422


async def test_item_section_is_fixed_and_proofed_items_cannot_change(client, H, db, uid, clock):
    await verified(db, uid)
    d = await save(
        client, H["employee"], [item("80C", "100000.00", description="PPF"), item("80CCD1B", "10000.00", description="NPS")]
    )
    ids = [i["id"] for i in d["draft"]["items"]]
    swap = [{"id": ids[0], "section": "80CCD1B", "amount": "100000.00", "details": {"description": "PPF"}}]
    r = await client.put(
        f"{ME}/declaration",
        json={"fy": FY, "expected_version": d["version"], "regime": "old", "items": swap},
        headers=H["employee"],
    )
    assert r.status_code == 422 and fld(r) == "items[0].section"  # a new id is required for another section
    d = await submit(client, H["employee"], d["version"])
    fid = await upload_proof_file(client, H["employee"])
    clock.day = P_OPEN
    r = await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": ids[0], "claimed": "100000.00", "file_ids": [fid]}, headers=H["employee"]
    )
    assert r.status_code == 201
    proof = r.json()
    keep = [{"id": i["id"], "section": i["section"], "amount": i["amount"], "details": i["details"]} for i in d["draft"]["items"]]

    async def put(items, version):
        return await client.put(
            f"{ME}/declaration",
            json={"fy": FY, "expected_version": version, "regime": "old", "items": items},
            headers=H["employee"],
        )

    edited = [dict(keep[0], amount="50000.00"), keep[1]]
    r = await put(edited, d["version"])
    assert r.status_code == 409 and err(r) == "CONFLICT"  # edit of an item with a live proof
    r = await put([keep[1]], d["version"])
    assert r.status_code == 409 and err(r) == "CONFLICT"  # removal
    r = await put([keep[0], dict(keep[1], amount="11000.00")], d["version"])  # the other item (no proof) may change
    assert r.status_code == 200
    # a draft that drifted from the submitted item cannot be submitted while the proof lives (tampered here)
    row = (await db.execute(select(TaxDeclaration))).scalars().one()
    await db.refresh(row)
    row.draft = {"items": [dict(keep[0], amount="1.00"), keep[1]]}
    await db.commit()
    r = await client.post(f"{ME}/declaration/submit", json={"fy": FY, "expected_version": row.version}, headers=H["employee"])
    assert r.status_code == 409 and err(r) == "CONFLICT"
    # once the proof is cancelled the item is editable again
    assert (await client.post(f"{ME}/proofs/{proof['id']}/cancel", headers=H["employee"])).status_code == 200
    r = await put([dict(keep[0], amount="50000.00"), keep[1]], row.version)
    assert r.status_code == 200


async def test_verify_rechecks_the_declared_item(client, H, db, uid, clock):
    await verified(db, uid)
    await proof_ready(client, H, db, uid)
    fid = await upload_proof_file(client, H["employee"])
    clock.day = P_OPEN
    r = await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-1", "claimed": "100000.00", "file_ids": [fid]}, headers=H["employee"]
    )
    pid = r.json()["id"]
    row = (await db.execute(select(TaxDeclaration))).scalars().one()
    await db.refresh(row)
    row.submitted = {"items": [dict(row.submitted["items"][0], amount="40000.00")]}  # tampering that bypassed the API
    await db.commit()
    r = await client.post(f"{TAX}/proofs/{pid}/verify", json={"expected_version": 1, "verified": "100000.00"}, headers=H["admin"])
    assert r.status_code == 409 and err(r) == "CONFLICT"
    row.submitted = {"items": [dict(row.submitted["items"][0], amount="100000.00", section="80CCD1B")]}
    await db.commit()
    r = await client.post(f"{TAX}/proofs/{pid}/verify", json={"expected_version": 1, "verified": "100000.00"}, headers=H["admin"])
    assert r.status_code == 409 and err(r) == "CONFLICT"
    row.submitted = {"items": [dict(row.submitted["items"][0], section="80C")]}
    await db.commit()
    r = await client.post(f"{TAX}/proofs/{pid}/verify", json={"expected_version": 1, "verified": "100000.00"}, headers=H["admin"])
    assert r.status_code == 200


async def test_a_proof_decider_cannot_then_enter_the_declaration_on_behalf(client, H, db, uid, clock):
    await verified(db, uid)
    await proof_ready(client, H, db, uid)
    fid = await upload_proof_file(client, H["employee"])
    clock.day = P_OPEN
    r = await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-1", "claimed": "100000.00", "file_ids": [fid]}, headers=H["employee"]
    )
    r = await client.post(
        f"{TAX}/proofs/{r.json()['id']}/verify", json={"expected_version": 1, "verified": "100000.00"}, headers=H["hr"]
    )
    assert r.status_code == 200
    row = (await db.execute(select(TaxDeclaration))).scalars().one()
    await db.refresh(row)
    body = {
        "fy": FY,
        "expected_version": row.version,
        "regime": "old",
        "items": [{"id": "i-1", "section": "80C", "amount": "100000.00", "details": {"description": "PPF"}}],
        "current_password": PASSWORD,
    }
    r = await client.put(f"{TAX}/declarations/{uid['employee']}", json=body, headers=H["hr"])
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    r = await client.post(
        f"{TAX}/declarations/{uid['employee']}/submit",
        json={"fy": FY, "expected_version": row.version, "current_password": PASSWORD},
        headers=H["hr"],
    )
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    r = await client.put(f"{TAX}/declarations/{uid['employee']}", json=body, headers=H["admin"])  # somebody else may
    assert r.status_code == 200


async def test_the_employee_gets_the_section_catalogue_and_the_file_cap(client, H, db, uid):
    await verified(db, uid)
    d = (await client.get(f"{ME}/declaration", headers=H["employee"])).json()
    secs = {s["code"]: s for s in d["sections"]}
    assert set(secs) >= {"80C", "HRA", "24B", "OTHER_INCOME"}
    assert secs["80D_SELF"]["senior_limit"] == "50000.00" and secs["80D_SELF"]["fields"] == ["description", "senior"]
    assert secs["HRA"]["landlord_pan_above"] == "100000.00" and secs["80C"]["proof_required"] is True
    assert secs["OTHER_INCOME"]["proof_required"] is False and d["proof_max_files"] == 5
    saved = await save(client, H["employee"], [item("80C", "1000.00", description="x")])
    assert [s["code"] for s in saved["sections"]] == [s["code"] for s in d["sections"]]  # also on save / submit answers
    admin_view = (await client.get(f"{TAX}/declarations/{uid['employee']}", params={"fy": FY}, headers=H["hr"])).json()
    assert "sections" not in admin_view


async def test_a_used_upload_cannot_back_a_second_proof(client, H, db, uid, clock):
    await verified(db, uid)
    await declared(client, H, [item("80C", "100000.00", description="a"), item("80CCD1B", "10000.00", description="b")])
    fid = await upload_proof_file(client, H["employee"])
    clock.day = P_OPEN
    ok = await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-1", "claimed": "1000.00", "file_ids": [fid]}, headers=H["employee"]
    )
    assert ok.status_code == 201
    r = await client.post(
        f"{ME}/proofs", json={"fy": FY, "item_id": "i-2", "claimed": "1000.00", "file_ids": [fid]}, headers=H["employee"]
    )
    assert r.status_code == 422 and fld(r) == "file_ids[0]"
