"""Plan 17 work package 5b: tax year rules (contract 19.6): validation, versions, maker-checker, windows, audit redaction.

Database triggers / CHECKs are PostgreSQL-only (test_migration_0027.py); here the service layer enforces and is exercised on SQLite.
HR001 is the maker, ADM001 / ADM002 / ADM003 are checkers.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import AuditLog, PayrollItemEvent, TaxYearRule
from app.services import notifications, tax_rules
from tests.conftest import PASSWORD
from tests.payroll_controls_helpers import extra_user, quiet  # noqa: F401  (fixture)
from tests.payroll_tax_helpers import (  # noqa: F401  (fixtures)
    FY,
    TAX,
    WINDOWS,
    add_rules,
    clerk,
    err,
    fld,
    rules_copy,
)

pytestmark = pytest.mark.usefixtures("quiet", "clock", "storage")
DB = Path(__file__).resolve().parents[2] / "database"


def _migration():
    sys.path.insert(0, str(DB))
    try:
        spec = importlib.util.spec_from_file_location(
            "mig0027r", DB / "alembic" / "versions" / "0027_tax_rules_declarations_form16.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(DB))
    return mod


# ================================================================================================ pure validation
def test_the_migration_seed_is_valid_and_hashes_the_documented_way():
    mod = _migration()
    seed = json.loads(mod.SEED_RULES_JSON)
    assert tax_rules.rules_digest(seed) == mod.SEED_RULES_SHA256
    assert tax_rules.has_unverified_note(seed)
    # the seed lists old-regime sections (80CCC, 80E, ...) that its own catalogue does not define: the reviewer must add or drop them
    with pytest.raises(Exception) as ei:
        tax_rules.validate_rules(seed)
    assert ei.value.details[0]["field"] == "rules.regimes.old.allowed_sections[1]"
    fixed = _reviewed(seed)
    out = tax_rules.validate_rules(fixed)
    assert out == fixed  # normalisation is the identity on well-formed rules, so a stored sha stays correct


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda r: r["regimes"]["new"]["slabs"][1].update({"from": "500000.00"}), "rules.regimes.new.slabs[1].from"),
        (lambda r: r["regimes"]["new"]["slabs"][0].update({"from": "1.00"}), "rules.regimes.new.slabs[0].from"),
        (lambda r: r["regimes"]["new"]["slabs"][2].update({"to": "900000.00"}), "rules.regimes.new.slabs[2].to"),
        (lambda r: r["regimes"]["new"]["slabs"][0].update({"to": None}), "rules.regimes.new.slabs[0].to"),
        (lambda r: r["regimes"]["new"]["slabs"][1].update({"rate_pct": "51"}), "rules.regimes.new.slabs[1].rate_pct"),
        (lambda r: r["regimes"]["new"]["slabs"][1].update({"rate_pct": 5}), "rules.regimes.new.slabs[1].rate_pct"),
        (lambda r: r["regimes"]["new"].update({"standard_deduction": 75000.0}), "rules.regimes.new.standard_deduction"),
        (lambda r: r["regimes"]["new"].update({"standard_deduction": "999999999999.00"}), "rules.regimes.new.standard_deduction"),
        (lambda r: r["regimes"]["old"].update({"allowed_sections": ["NOPE"]}), "rules.regimes.old.allowed_sections[0]"),
        (lambda r: r["sections"][1].update({"code": "80C"}), "rules.sections[1].code"),
        (lambda r: r["sections"][0].update({"code": "bad code"}), "rules.sections[0].code"),
        (lambda r: r["sections"][0].update({"fields": ["bogus"]}), "rules.sections[0].fields[0]"),
        (lambda r: r["group_limits"][0].update({"group": "MISSING"}), "rules.group_limits[0].group"),
        (lambda r: r.update({"default_regime": "both"}), "rules.default_regime"),
        (lambda r: r.update({"surprise": 1}), "rules.surprise"),
        (lambda r: r["component_treatment"].update({"REIMBURSEMENT": "maybe"}), "rules.component_treatment.REIMBURSEMENT"),
        (lambda r: r["component_treatment"].pop("default"), "rules.component_treatment"),
        (
            lambda r: r["regimes"]["new"]["surcharge"].append({"above": "100.00", "rate_pct": "5"}),
            "rules.regimes.new.surcharge[1].above",
        ),
        (lambda r: r.update({"round_tax_to": 0}), "rules.round_tax_to"),
        (lambda r: r.update({"sections": r["sections"] * 6}), "rules.sections"),
    ],
)
def test_rules_validation_reports_the_json_path(mutate, path):
    rules = rules_copy()
    mutate(rules)
    with pytest.raises(Exception) as ei:
        tax_rules.validate_rules(rules)
    assert ei.value.code == "VALIDATION_ERROR"
    assert ei.value.details[0]["field"] == path
    assert "999999999999" not in ei.value.details[0]["message"]


def test_valid_rules_pass_and_hra_requires_its_block():
    assert tax_rules.validate_rules(rules_copy())["regimes"]["old"]["hra_exemption"] is True
    r = rules_copy()
    del r["hra"]
    with pytest.raises(Exception) as ei:
        tax_rules.validate_rules(r)
    assert ei.value.details[0]["field"] == "rules.hra"


def test_fy_helpers():
    import datetime as dt

    assert tax_rules.fy_of(dt.date(2026, 3, 31)) == "2025-26"
    assert tax_rules.fy_of(dt.date(2026, 4, 1)) == "2026-27"
    assert tax_rules.fy_bounds("2026-27") == (dt.date(2026, 4, 1), dt.date(2027, 3, 31))
    for bad in ("2026-28", "26-27", "2026/27", "1999-00"):
        with pytest.raises(Exception) as ei:
            tax_rules.check_fy(bad)
        assert ei.value.code == "VALIDATION_ERROR"


def _reviewed(seed: dict) -> dict:
    """What a reviewer does to the seed: keep only the allowed sections the catalogue defines, drop the UNVERIFIED note."""
    out = json.loads(json.dumps(seed))
    out.pop("note", None)
    codes = {s["code"] for s in out["sections"]}
    out["regimes"]["old"]["allowed_sections"] = [c for c in out["regimes"]["old"]["allowed_sections"] if c in codes]
    return out


# ================================================================================================ lifecycle
async def _draft(client, hdr, fy=FY):
    r = await client.post(f"{TAX}/years/{fy}/rules", json={}, headers=hdr)
    assert r.status_code == 201, r.text
    return r.json()


async def _put(client, hdr, row, rules=None, expect=200, **extra):
    body = {"version_row": row["version_row"], "rules": rules if rules is not None else row["rules"], **extra}
    r = await client.put(f"{TAX}/rules/{row['id']}", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def _submit(client, hdr, row, expect=200):
    r = await client.post(f"{TAX}/rules/{row['id']}/submit", json={"version_row": row["version_row"]}, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


def _verify_body(row, **over):
    return {
        "version_row": row["version_row"], "rules_sha256": row["rules_sha256"],
        "attestation": "I verified these rules against the Finance Act on 2026-04-02", "current_password": PASSWORD, **over,
    }  # fmt: skip


async def _verify(client, hdr, row, expect=200, **over):
    r = await client.post(f"{TAX}/rules/{row['id']}/verify", json=_verify_body(row, **over), headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def test_seed_draft_needs_a_reviewed_edit_then_two_people_to_verify(client, payroll, db, uid, seeded):
    mod = _migration()
    seed = json.loads(mod.SEED_RULES_JSON)
    db.add(
        TaxYearRule(
            fy="2026-27",
            version=1,
            status="draft",
            rules=seed,
            rules_sha256=mod.SEED_RULES_SHA256,
            windows={},
            contributors=[],
            version_row=1,
        )
    )
    await db.commit()
    row = (await client.get(f"{TAX}/years/{FY}/rules", headers=payroll["hr"])).json()["items"][0]
    assert row["status"] == "draft" and row["note"].startswith("UNVERIFIED") and row["created_by"] is None
    # the seed note blocks the submit (and the database blocks the verify)
    r = await client.post(f"{TAX}/rules/{row['id']}/submit", json={"version_row": row["version_row"]}, headers=payroll["hr"])
    assert r.status_code == 422 and fld(r) == "rules.note"
    # the reviewer edits the values and drops the note
    bad = await client.put(
        f"{TAX}/rules/{row['id']}", json={"version_row": row["version_row"], "rules": row["rules"]}, headers=payroll["hr"]
    )
    assert bad.status_code == 422 and fld(bad).startswith("rules.regimes.old.allowed_sections")
    row = await _put(client, payroll["hr"], row, _reviewed(row["rules"]))
    assert row["note"] is None and row["last_edited_by"]["employee_id"] == "HR001"
    row = await _submit(client, payroll["hr"], row)
    assert row["status"] == "submitted" and row["submitted_by"]["employee_id"] == "HR001"
    # the maker cannot verify; a second person can
    r = await client.post(f"{TAX}/rules/{row['id']}/verify", json=_verify_body(row), headers=payroll["hr"])
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    done = await _verify(client, payroll["admin"], row)
    assert done["status"] == "verified" and done["verified_by"]["employee_id"] == "ADM001" and done["attestation"]


async def test_full_lifecycle_supersedes_the_previous_verified_version(client, payroll, db, uid, seeded):
    v1 = await add_rules(db, uid, status="verified")
    row = await _draft(client, payroll["hr"])
    assert (
        row["version"] == 2
        and row["status"] == "draft"
        and row["rules"] == v1.rules
        and row["created_by"]["employee_id"] == "HR001"
    )
    again = await client.post(f"{TAX}/years/{FY}/rules", json={}, headers=payroll["hr"])
    assert again.status_code == 409 and err(again) == "DRAFT_EXISTS"
    edited = rules_copy()
    edited["cess_pct"] = "3.5"
    row = await _put(client, payroll["hr"], row, edited)
    assert row["rules"]["cess_pct"] == "3.5" and row["version_row"] == 2
    stale = await client.put(f"{TAX}/rules/{row['id']}", json={"version_row": 1, "rules": edited}, headers=payroll["hr"])
    assert stale.status_code == 409 and err(stale) == "VERSION_CONFLICT"
    row = await _submit(client, payroll["hr"], row)
    again = await client.put(
        f"{TAX}/rules/{row['id']}", json={"version_row": row["version_row"], "rules": edited}, headers=payroll["hr"]
    )
    assert again.status_code == 409 and err(again) == "NOT_DRAFT"
    # wrong hash (the reviewer saw other rules) -> RULES_CHANGED; missing / wrong password
    assert (
        err(
            await client.post(
                f"{TAX}/rules/{row['id']}/verify", json=_verify_body(row, rules_sha256="0" * 64), headers=payroll["admin"]
            )
        )
        == "RULES_CHANGED"
    )
    body = _verify_body(row)
    del body["current_password"]
    r = await client.post(f"{TAX}/rules/{row['id']}/verify", json=body, headers=payroll["admin"])
    assert r.status_code == 422
    r = await client.post(
        f"{TAX}/rules/{row['id']}/verify", json=_verify_body(row, current_password="wrong"), headers=payroll["admin"]
    )
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    short = await client.post(
        f"{TAX}/rules/{row['id']}/verify", json=_verify_body(row, attestation="too short"), headers=payroll["admin"]
    )
    assert short.status_code == 422
    stale = await client.post(f"{TAX}/rules/{row['id']}/verify", json=_verify_body(row, version_row=1), headers=payroll["admin"])
    assert stale.status_code == 409 and err(stale) == "VERSION_CONFLICT"
    done = await _verify(client, payroll["admin"], row)
    assert done["status"] == "verified"
    versions = (await client.get(f"{TAX}/years/{FY}/rules", headers=payroll["hr"])).json()["items"]
    assert [(v["version"], v["status"]) for v in versions] == [(2, "verified"), (1, "superseded")]
    years = (await client.get(f"{TAX}/years", headers=payroll["hr"])).json()["items"]
    cur = next(y for y in years if y["fy"] == FY)
    assert cur["verified_version"] == 2 and cur["open_version"] is None and cur["current"] is True
    assert cur["windows"] == WINDOWS
    assert (
        err(await client.post(f"{TAX}/rules/{row['id']}/verify", json=_verify_body(done), headers=payroll["admin"]))
        == "NOT_SUBMITTED"
    )


async def test_editor_and_submitter_and_contributors_are_all_parties(client, payroll, db, uid, seeded):
    await add_rules(db, uid, status="verified")
    _, adm2 = await extra_user(client, db, seeded, "ADM002", "admin")
    _, adm3 = await extra_user(client, db, seeded, "ADM003", "admin")
    row = await _draft(client, payroll["hr"])
    row = await _put(client, adm2, row, {**rules_copy(), "cess_pct": "3"})  # ADM002 edits
    row = await _submit(client, payroll["hr"], row)  # HR submits
    for who in (payroll["hr"], adm2):  # creator + submitter, last editor
        r = await client.post(f"{TAX}/rules/{row['id']}/verify", json=_verify_body(row), headers=who)
        assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    r = await client.post(
        f"{TAX}/rules/{row['id']}/reject", json={"version_row": row["version_row"], "comment": "Not right"}, headers=adm2
    )
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    # ADM001 (never touched it) can; and ADM001 is not a party of the withdrawn / re-submitted copy either way
    assert (await _verify(client, adm3, row))["verified_by"]["employee_id"] == "ADM003"


async def test_reject_and_withdraw_return_to_draft_and_keep_the_comment_in_the_history(client, payroll, db, uid, seeded):
    await add_rules(db, uid, status="verified")
    row = await _submit(client, payroll["hr"], await _draft(client, payroll["hr"]))
    r = await client.post(
        f"{TAX}/rules/{row['id']}/reject",
        json={"version_row": row["version_row"], "comment": "Slab 2 is wrong"},
        headers=payroll["admin"],
    )
    assert r.status_code == 200 and r.json()["status"] == "draft" and r.json()["submitted_by"] is None
    row = r.json()
    ev = list(
        (
            await db.execute(
                select(PayrollItemEvent).where(PayrollItemEvent.item_type == "tax_rules", PayrollItemEvent.action == "reject")
            )
        ).scalars()
    )
    assert len(ev) == 1 and ev[0].detail == {"comment": "Slab 2 is wrong"} and ev[0].status == "draft"
    assert (
        err(
            await client.post(
                f"{TAX}/rules/{row['id']}/reject",
                json={"version_row": row["version_row"], "comment": "again"},
                headers=payroll["admin"],
            )
        )
        == "NOT_SUBMITTED"
    )
    row = await _submit(client, payroll["hr"], row)
    w = await client.post(f"{TAX}/rules/{row['id']}/withdraw", json={"version_row": row["version_row"]}, headers=payroll["hr"])
    assert w.status_code == 200 and w.json()["status"] == "draft"
    assert (
        err(
            await client.post(
                f"{TAX}/rules/{row['id']}/withdraw", json={"version_row": w.json()["version_row"]}, headers=payroll["hr"]
            )
        )
        == "NOT_SUBMITTED"
    )
    r = await client.post(f"{TAX}/rules/{row['id']}/reject", json={"version_row": 1}, headers=payroll["admin"])
    assert r.status_code == 422  # a rejection needs its comment


async def test_audit_and_history_never_carry_rule_values(client, payroll, db, uid, seeded):
    await add_rules(db, uid, status="verified")
    row = await _submit(client, payroll["hr"], await _draft(client, payroll["hr"]))
    await _verify(client, payroll["admin"], row)
    rows = list((await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.tax_rules.%")))).scalars())
    assert {r.action for r in rows} >= {"payroll.tax_rules.create", "payroll.tax_rules.submit", "payroll.tax_rules.verify"}
    verify = next(r for r in rows if r.action == "payroll.tax_rules.verify")
    assert set(verify.after) == {"fy", "version", "status", "rules_sha256", "maker_id", "checker_id"}
    assert verify.after["maker_id"] == str(uid["hr"]) and verify.after["checker_id"] == str(uid["admin"])
    blob = json.dumps([r.after for r in rows])
    assert "75000" not in blob and "attestation" not in blob and "I verified" not in blob
    actions = [
        e.action for e in (await db.execute(select(PayrollItemEvent).where(PayrollItemEvent.item_type == "tax_rules"))).scalars()
    ]
    assert actions.count("verify") == 1 and "create" in actions and "submit" in actions


async def test_permissions(client, payroll, db, uid, seeded):
    await add_rules(db, uid, status="verified")
    for who in ("manager", "employee"):
        assert (await client.get(f"{TAX}/years", headers=payroll[who])).status_code == 403
        assert (await client.post(f"{TAX}/years/{FY}/rules", json={}, headers=payroll[who])).status_code == 403
    row = (await client.get(f"{TAX}/years/{FY}/rules", headers=payroll["hr"])).json()["items"][0]
    assert (await client.get(f"{TAX}/rules/{row['id']}", headers=payroll["manager"])).status_code == 403
    assert (
        await client.post(f"{TAX}/rules/{row['id']}/verify", json=_verify_body(row), headers=payroll["manager"])
    ).status_code == 403
    assert (await client.put(f"{TAX}/years/{FY}/windows", json=WINDOWS, headers=payroll["employee"])).status_code == 403
    assert (await client.get(f"{TAX}/rules/{uuid.uuid4()}", headers=payroll["hr"])).status_code == 404
    unknown = await client.put(
        f"{TAX}/rules/{row['id']}", json={"version_row": 1, "rules": {}, "extra": 1}, headers=payroll["hr"]
    )
    assert unknown.status_code == 422


async def test_a_new_year_starts_from_the_latest_earlier_year_marked_unverified(client, payroll, db, uid, seeded):
    await add_rules(db, uid, status="verified")
    row = await _draft(client, payroll["hr"], "2027-28")
    assert row["version"] == 1 and row["note"].startswith("UNVERIFIED") and row["rules"]["cess_pct"] == "4"
    r = await client.post(f"{TAX}/years/2027-28/rules", json={}, headers=payroll["hr"])
    assert r.status_code == 409 and err(r) == "DRAFT_EXISTS"
    r = await client.post(f"{TAX}/years/2020-21/rules", json={}, headers=payroll["hr"])
    assert r.status_code == 404  # nothing earlier to copy
    assert (await client.post(f"{TAX}/years/2026-28/rules", json={}, headers=payroll["hr"])).status_code == 422


# ================================================================================================ windows
async def test_windows_are_validated_audited_and_pushed(client, payroll, db, uid, seeded, clock):
    await add_rules(db, uid, status="verified", windows={})
    notifications.OUTBOX.clear()
    r = await client.put(f"{TAX}/years/{FY}/windows", json=WINDOWS, headers=payroll["hr"])
    assert r.status_code == 200 and r.json()["windows"] == WINDOWS
    row = (await client.get(f"{TAX}/years/{FY}/rules", headers=payroll["hr"])).json()["items"][0]
    assert row["windows"] == WINDOWS
    # the declaration window is open today (clock = 15 May): an opening push went out, never with amounts
    assert [m for m in notifications.OUTBOX if m["payload"]["type"] == "tax_window"]
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.tax_rules.windows"))).scalars().one()
    assert log.after["fy"] == FY
    # again with the same dates: the window was already open, no second push
    notifications.OUTBOX.clear()
    await client.put(f"{TAX}/years/{FY}/windows", json=WINDOWS, headers=payroll["hr"])
    assert not [m for m in notifications.OUTBOX if m["payload"]["type"] == "tax_window"]
    for bad in (
        {**WINDOWS, "declaration_opens_on": "2026-03-31"},
        {**WINDOWS, "proof_closes_on": "2027-04-01"},
        {**WINDOWS, "declaration_opens_on": "2026-07-01"},  # opens after it closes
        {"declaration_opens_on": "2026-04-01", "declaration_closes_on": None},  # one side only
    ):
        r = await client.put(f"{TAX}/years/{FY}/windows", json=bad, headers=payroll["hr"])
        assert r.status_code == 422 and err(r) == "INVALID_DATE", bad
    assert (
        await client.put(
            f"{TAX}/years/2030-31/windows",
            json={"declaration_opens_on": "2030-04-01", "declaration_closes_on": "2030-06-30"},
            headers=payroll["hr"],
        )
    ).status_code == 404
    cleared = await client.put(f"{TAX}/years/{FY}/windows", json={}, headers=payroll["hr"])
    assert cleared.status_code == 200 and all(v is None for v in cleared.json()["windows"].values())


# ================================================================================================ phase 5b security review
async def test_only_the_submitter_or_an_approver_can_withdraw_and_an_approver_stays_free_to_verify(
    client, payroll, db, uid, seeded
):
    await add_rules(db, uid, status="verified")
    _, clk = await clerk(client, db, seeded)
    row = await _submit(client, payroll["hr"], await _draft(client, payroll["hr"]))
    url = f"{TAX}/rules/{row['id']}/withdraw"
    r = await client.post(url, json={"version_row": row["version_row"]}, headers=clk)
    assert r.status_code == 403 and err(r) == "FORBIDDEN"  # neither the submitter nor an approver
    r = await client.post(url, json={"version_row": row["version_row"]}, headers=payroll["admin"])  # an approver may
    assert r.status_code == 200 and r.json()["status"] == "draft"
    stored = (await db.execute(select(TaxYearRule).where(TaxYearRule.id == uuid.UUID(row["id"])))).scalar_one()
    await db.refresh(stored)
    assert str(uid["admin"]) not in (stored.contributors or [])  # withdrawing did not make the approver a party
    row = await _submit(client, payroll["hr"], r.json())
    await _verify(client, payroll["admin"], row)  # still free to verify
    # the submitter withdrawing is unchanged
    row2 = await _submit(client, payroll["hr"], await _draft(client, payroll["hr"]))
    r = await client.post(f"{TAX}/rules/{row2['id']}/withdraw", json={"version_row": row2["version_row"]}, headers=payroll["hr"])
    assert r.status_code == 200


async def test_reopening_a_passed_window_needs_the_password_and_alerts_the_admins(client, payroll, db, uid, seeded, clock):
    await add_rules(db, uid, status="verified")
    url = f"{TAX}/years/{FY}/windows"
    # before the window closes: extending it is an ordinary change
    later = {**WINDOWS, "declaration_closes_on": "2026-07-15"}
    assert (await client.put(url, json=later, headers=payroll["hr"])).status_code == 200
    clock.day = dt.date(2026, 8, 15)  # the declaration window (closes 15 July) has passed
    notifications.OUTBOX.clear()
    same = await client.put(url, json=later, headers=payroll["hr"])
    assert same.status_code == 200  # nothing reopened
    shorter = {**WINDOWS, "declaration_closes_on": "2026-06-01"}
    assert (await client.put(url, json=shorter, headers=payroll["hr"])).status_code == 200  # closing earlier is free
    assert not [m for m in notifications.OUTBOX if m["payload"]["type"] == "admin_alert"]
    reopen = {**WINDOWS, "declaration_closes_on": "2026-09-30"}
    r = await client.put(url, json=reopen, headers=payroll["hr"])
    assert r.status_code == 422  # step-up needed
    r = await client.put(url, json={**reopen, "current_password": "wrong"}, headers=payroll["hr"])
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    assert (await client.get(f"{TAX}/years", headers=payroll["hr"])).json()["items"][0]["windows"][
        "declaration_closes_on"
    ] == "2026-06-01"
    r = await client.put(url, json={**reopen, "current_password": PASSWORD}, headers=payroll["hr"])
    assert r.status_code == 200 and r.json()["windows"]["declaration_closes_on"] == "2026-09-30"
    alerts = [m for m in notifications.OUTBOX if m["payload"]["type"] == "admin_alert"]
    assert alerts and "2026-09-30" not in json.dumps(alerts)
    log = [a for a in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.tax_rules.windows"))).scalars()][-1]
    assert log.after["reopened"] is True and "current_password" not in json.dumps(log.after)
    # the same for the proof window
    clock.day = dt.date(2027, 3, 10)
    r = await client.put(url, json={**reopen, "proof_closes_on": "2027-03-20"}, headers=payroll["hr"])
    assert r.status_code == 422
