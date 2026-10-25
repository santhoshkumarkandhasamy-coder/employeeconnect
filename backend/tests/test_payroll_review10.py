"""Security review 10 (payroll) fixes: maker-checker (M1), admin masking (M2), L1-L4, key rotation vs lock triggers (H1)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.core.redis import get_redis
from app.models import Payslip
from app.services import key_rotation, payslip_pdf
from app.services import payroll_runs as runs
from tests.conftest import PASSWORD
from tests.payroll_fixtures import new_run, process_and_publish, publish_run, row_for

KEY = "payroll.publish_requires_second_person"
SETTINGS = "/api/admin/payroll/settings"


async def put_rule(client, hdr, value, **extra):
    return await client.put(SETTINGS, json={"settings": {KEY: value}, **extra}, headers=hdr)


# ================================================================================================ M1
async def test_changing_the_rule_needs_role_assign_and_password(client, payroll):
    hr, admin = payroll["hr"], payroll["admin"]
    r = await put_rule(client, hr, True, current_password=PASSWORD)  # payroll.manage is not enough
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    r = await put_rule(client, admin, True)  # no password
    assert r.status_code == 422
    r = await put_rule(client, admin, True, current_password="Wrong!Passw0rd#1")
    assert r.status_code == 403 and r.json()["error"]["code"] == "REAUTH_FAILED"
    assert (await client.get(SETTINGS, headers=hr)).json()["settings"][KEY] is False  # nothing changed
    assert (await put_rule(client, admin, True, current_password=PASSWORD)).status_code == 200
    # switching it OFF is gated the same way
    assert (await put_rule(client, hr, False, current_password=PASSWORD)).status_code == 403
    assert (await put_rule(client, admin, False)).status_code == 422
    assert (await put_rule(client, admin, False, current_password=PASSWORD)).status_code == 200


async def test_unchanged_rule_and_other_keys_need_no_extra_step(client, payroll):
    hr = payroll["hr"]
    assert (await put_rule(client, hr, False)).status_code == 200  # same value as stored
    r = await client.put(SETTINGS, json={"settings": {"payroll.rounding": "paise", KEY: False}}, headers=hr)
    assert r.status_code == 200


async def test_rule_on_in_run_copy_survives_switching_the_setting_off(client, payroll):
    hr, admin = payroll["hr"], payroll["admin"]
    assert (await put_rule(client, admin, True, current_password=PASSWORD)).status_code == 200
    run = await new_run(client, hr)  # snapshot: rule ON
    assert (await put_rule(client, admin, False, current_password=PASSWORD)).status_code == 200
    await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=hr)
    r = await publish_run(client, hr, run["id"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "SECOND_PERSON_REQUIRED"
    assert (await publish_run(client, admin, run["id"])).status_code == 200


async def test_rule_switched_on_after_the_run_was_created_still_applies(client, payroll):
    hr, admin = payroll["hr"], payroll["admin"]
    run = await new_run(client, hr)  # snapshot: rule OFF
    await client.post(f"/api/admin/payroll/runs/{run['id']}/process", headers=hr)
    assert (await put_rule(client, admin, True, current_password=PASSWORD)).status_code == 200
    r = await publish_run(client, hr, run["id"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "SECOND_PERSON_REQUIRED"
    assert (await publish_run(client, admin, run["id"])).status_code == 200


async def test_last_editor_of_a_payslip_cannot_publish_it(client, payroll, db):
    hr, admin = payroll["hr"], payroll["admin"]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    row = await row_for(client, hr, run["id"], "EMP001")
    rev = await client.post(
        f"/api/admin/payroll/runs/{run['id']}/employees/{row['user']['id']}/revise", json={"reason": "fix"}, headers=hr
    )
    assert rev.status_code == 201, rev.text
    pid = rev.json()["id"]
    assert (await client.post(f"/api/admin/payroll/payslips/{pid}/process", headers=admin)).status_code == 200
    assert (await put_rule(client, admin, True, current_password=PASSWORD)).status_code == 200
    # processed by the admin; make HR the last editor behind the service's back
    ps = (await db.execute(select(Payslip).where(Payslip.id == uuid.UUID(pid)))).scalar_one()
    ps.updated_by, ps.processed_by = ps.created_by, None  # last editor = HR, nobody recorded as the processor
    await db.commit()
    r = await client.post(f"/api/admin/payroll/payslips/{pid}/publish", json={"current_password": PASSWORD}, headers=hr)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SECOND_PERSON_REQUIRED"
    r = await client.post(f"/api/admin/payroll/payslips/{pid}/publish", json={"current_password": PASSWORD}, headers=admin)
    assert r.status_code == 200, r.text


# ================================================================================================ M2 / L2
async def _details(client, hdr, uid):
    r = await client.put(
        f"/api/admin/payroll/employees/{uid}/details",
        json={"pan": "ABCDE1234F", "uan": "100200300400", "esi_ip_number": "1234567890", "current_password": PASSWORD},
        headers=hdr,
    )
    assert r.status_code == 200, r.text


async def test_admin_payslip_json_and_pdf_view_mask_identifiers(client, payroll, uid, db):
    hr = payroll["hr"]
    # even with PAN masking switched off for employees
    await client.put(SETTINGS, json={"settings": {"payroll.mask_pan": False}}, headers=hr)
    await _details(client, hr, uid["employee"])
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    row = await row_for(client, hr, run["id"], "EMP001")
    p = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=hr)).json()
    assert p["employee"]["pan"] == "XXXXX1234X"
    assert p["employee"]["uan"] == "XXXXXXXX0400" and p["employee"]["esi_ip_number"] == "XXXXXX7890"
    ps = (await db.execute(select(Payslip).where(Payslip.id == uuid.UUID(row["id"])))).scalar_one()
    view, _, _ = await runs.pdf_parts(db, ps, admin=True)
    e = view["employee"]
    assert (e["pan"], e["uan"], e["esi_ip_number"]) == ("XXXXX1234X", "XXXXXXXX0400", "XXXXXX7890")
    own, _, _ = await runs.pdf_parts(db, ps, admin=False)  # the employee's own PDF keeps their numbers
    assert own["employee"]["pan"] == "ABCDE1234F" and own["employee"]["uan"] == "100200300400"
    mine = (await client.get(f"/api/payslips/{row['id']}", headers=payroll["employee"])).json()
    assert mine["employee"]["uan"] == "100200300400" and mine["employee"]["esi_ip_number"] == "1234567890"


async def test_employee_does_not_see_an_unpublished_revision_but_admin_does(client, payroll):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    row = await row_for(client, hr, run["id"], "EMP001")
    r = await client.post(
        f"/api/admin/payroll/runs/{run['id']}/employees/{row['user']['id']}/revise", json={"reason": "fix"}, headers=hr
    )
    assert r.status_code == 201
    draft_rev = r.json()["id"]
    mine = (await client.get(f"/api/payslips/{row['id']}", headers=payroll["employee"])).json()
    assert mine["revision_info"] is None
    admin_view = (await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=hr)).json()
    assert admin_view["revision_info"]["superseded_by_id"] == draft_rev
    # once the revision is published the employee's old copy points to it
    await client.post(f"/api/admin/payroll/payslips/{draft_rev}/process", headers=hr)
    pub = await client.post(f"/api/admin/payroll/payslips/{draft_rev}/publish", json={"current_password": PASSWORD}, headers=hr)
    assert pub.status_code == 200, pub.text
    mine = (await client.get(f"/api/payslips/{row['id']}", headers=payroll["employee"])).json()
    assert mine["revision_info"]["superseded_by_id"] == draft_rev


# ================================================================================================ L1
async def test_structure_delete_only_clears_draft_payslips(client, payroll, uid):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    listing = (await client.get(f"/api/admin/payroll/employees/{uid['employee']}/structures", headers=hr)).json()
    sid = listing["items"][0]["id"]
    assert (await client.delete(f"/api/admin/payroll/structures/{sid}", headers=hr)).status_code == 204
    row = await row_for(client, hr, run["id"], "EMP001")
    assert row["status"] == "draft"


# ================================================================================================ L3
async def test_pdf_slot_is_held_until_the_worker_thread_finishes(monkeypatch):
    import time

    from app.core.errors import AppError

    monkeypatch.setattr(payslip_pdf, "BUILD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(payslip_pdf, "render", lambda *a: time.sleep(0.3))
    sem = payslip_pdf._semaphore()
    free = sem._value
    with pytest.raises(AppError) as e:
        await payslip_pdf.build_pdf({}, "", None, None)
    assert e.value.code == "TEMPORARILY_UNAVAILABLE"
    assert sem._value == free - 1  # the thread is still running: its slot is NOT free yet
    await asyncio.sleep(0.5)
    assert sem._value == free


def test_oversized_logo_is_skipped_without_decoding(monkeypatch):
    import PIL.Image

    calls = []
    monkeypatch.setattr(PIL.Image, "open", lambda *a, **k: calls.append(1))
    view = {
        "period": "2025-06", "revision": 1, "company": {"name": "Acme"},
        "employee": {"employee_id": "E1", "full_name": "A B", "payment_mode": "bank_transfer"},
        "days": {"basis_days": 30, "employed_days": 30, "lop_days": 0, "paid_days": 30},
        "earnings": [], "deductions": [], "totals": None, "revision_info": None,
    }  # fmt: skip
    out = payslip_pdf.render(view, "", b"x" * (payslip_pdf.MAX_LOGO_BYTES + 1), None)
    assert out.startswith(b"%PDF-") and calls == []


# ================================================================================================ L4
class _BrokenRedis:
    def pipeline(self, *a, **k):
        raise ConnectionError("down")

    async def incr(self, *a, **k):
        raise ConnectionError("down")


async def test_pdf_endpoints_fail_closed_when_redis_is_down(client, app, payroll):
    hr = payroll["hr"]
    run = await new_run(client, hr)
    await process_and_publish(client, hr, run["id"])
    row = await row_for(client, hr, run["id"], "EMP001")
    app.dependency_overrides[get_redis] = lambda: _BrokenRedis()
    for url, hdr in (
        (f"/api/admin/payroll/payslips/{row['id']}/pdf", hr),
        (f"/api/payslips/{row['id']}/pdf", payroll["employee"]),
    ):
        r = await client.get(url, headers=hdr)
        assert r.status_code == 503 and r.json()["error"]["code"] == "TEMPORARILY_UNAVAILABLE", url


# ================================================================================================ H1 (unit part)
async def test_rotation_sets_the_trigger_bypass_only_on_postgres():
    class Dialect:
        def __init__(self, name):
            self.name = name

    class Bind:
        def __init__(self, name):
            self.dialect = Dialect(name)

    class FakeSession:
        def __init__(self, name):
            self.bind, self.sql = Bind(name), []

        def get_bind(self):
            return self.bind

        async def execute(self, stmt, *a, **k):
            self.sql.append(str(stmt))

    pg, lite = FakeSession("postgresql"), FakeSession("sqlite")
    await key_rotation._allow_rotation(pg)
    await key_rotation._allow_rotation(lite)
    assert pg.sql == ["SET LOCAL app.key_rotation = 'on'"] and lite.sql == []


def test_migration_bypass_is_limited_to_enc_columns():
    import importlib.util
    import sys
    from pathlib import Path

    db = Path(__file__).resolve().parents[2] / "database"
    sys.path.insert(0, str(db))
    try:
        spec = importlib.util.spec_from_file_location("mig0018_r10", db / "alembic" / "versions" / "0018_payroll.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(db))
    up = mod.UPGRADE
    assert up.count("current_setting('app.key_rotation', true) = 'on'") == 2  # payslips + salary_structures only
    assert "right(key, 4) <> '_enc'" in up
    runs_fn = up.split("CREATE FUNCTION payroll_runs_lock")[1].split("$$;")[0]
    assert "app.key_rotation" not in runs_fn
    assert "trg_payslips_superseded_check" in up and "trg_salary_structures_lock" in up
    assert "DROP FUNCTION IF EXISTS payroll_strip_enc(jsonb)" in mod.DOWNGRADE
