"""Phase 4a security review fixes: H1 (every contributor is a party), M1 (batch approve bound to reviewed rows), M2 (employee
lock on direct structure writes), L1 (money bounds), L4 (daily step-up window), L6 (derived import-binding key).

The trigger changes of migration 0024 (L2) need PostgreSQL; their text is checked in test_migration_0024.py."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import types
import uuid
from decimal import Decimal

import pytest

from tests.conftest import PASSWORD
from tests.payroll_fixtures import add_structure, structure_body
from tests.test_payroll_increments import (
    EMP,
    INC,
    approve,
    bulk_body,
    err,
    expected_of,
    login,
    make,
    make_user,
    pct_body,
    ready,
    submit,
)


@pytest.fixture(autouse=True)
def _relaxed_limits(monkeypatch):
    from app.services import admin_security
    from app.services import payroll_common as pc

    async def no_limit(*a, **k):
        return None

    monkeypatch.setattr(pc, "limit", no_limit)
    monkeypatch.setattr(admin_security, "REAUTH_PER_MINUTE", 10_000)
    monkeypatch.setattr(admin_security, "REAUTH_PER_DAY", 10_000)


# ================================================================================================ H1
async def test_editors_and_withdrawn_submitters_cannot_decide(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    await make_user(db, seeded, "hr", "HR002")
    hr2 = await login(client, "HR002")
    # HR drafts, the admin edits the draft, HR submits: the admin (last editor) may neither approve nor reject
    inc = await make(client, hr, uid["employee"])
    r = await client.put(f"{INC}/{inc['id']}", json={**pct_body(percent="15.00"), "expected_version": 1}, headers=admin)
    assert r.status_code == 200, r.text
    inc = await submit(client, hr, r.json())
    body = {"expected_version": inc["version"], "current_password": PASSWORD}
    r = await client.post(f"{INC}/{inc['id']}/approve", json=body, headers=admin)
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    r = await client.post(
        f"{INC}/{inc['id']}/reject", json={"expected_version": inc["version"], "comment": "Mine"}, headers=admin
    )
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    done = await approve(client, hr2, inc)  # somebody who took no part
    assert done["status"] == "approved" and done["decided_by"]["employee_id"] == "HR002"

    # an earlier editor who was overwritten by a later edit is still a party
    other = await make(client, hr, uid["manager"])
    other = (await client.put(f"{INC}/{other['id']}", json={**pct_body(), "expected_version": 1}, headers=hr2)).json()
    other = (
        await client.put(f"{INC}/{other['id']}", json={**pct_body(percent="9.00"), "expected_version": 2}, headers=hr)
    ).json()
    other = await submit(client, hr, other)
    r = await client.post(
        f"{INC}/{other['id']}/approve", json={"expected_version": other["version"], "current_password": PASSWORD}, headers=hr2
    )
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"

    # a withdrawn submission stays on record: HR002 submits and withdraws, HR resubmits, HR002 still cannot decide
    await add_structure(client, hr, uid["admin"], basic="25000", hra="5000")
    third = await make(client, hr, uid["admin"])
    third = await submit(client, hr2, third)
    r = await client.post(f"{INC}/{third['id']}/withdraw", json={"expected_version": third["version"]}, headers=hr2)
    assert r.status_code == 200 and r.json()["submitted_by"] is None
    third = await submit(client, hr, r.json())
    r = await client.post(
        f"{INC}/{third['id']}/reject", json={"expected_version": third["version"], "comment": "Mine"}, headers=hr2
    )
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"


async def test_batch_reject_and_approve_refuse_a_row_editor(client, payroll, uid, db, seeded):
    hr, admin = payroll["hr"], payroll["admin"]
    await make_user(db, seeded, "hr", "HR002")
    hr2 = await login(client, "HR002")
    b = (await client.post(f"{INC}/bulk", json=bulk_body(), headers=hr)).json()["batch"]
    rows = (await client.get(INC, params={"batch_id": b["id"]}, headers=hr)).json()["items"]
    row = next(r for r in rows if r["user"]["employee_id"] == "EMP001")
    r = await client.put(
        f"{INC}/{row['id']}",
        json={**pct_body(user_eff=row["effective_from"], percent="5.00"), "expected_version": row["version"]},
        headers=hr2,
    )
    assert r.status_code == 200, r.text
    assert (await client.post(f"{INC}/batches/{b['id']}/submit", json={}, headers=hr)).status_code == 200
    exp = await expected_of(client, hr, b["id"])
    # the row editor cannot reject the batch, and approving reports only the row he edited as SECOND_PERSON_REQUIRED
    r = await client.post(f"{INC}/batches/{b['id']}/reject", json={"comment": "Redo"}, headers=hr2)
    assert r.status_code == 403 and err(r) == "SECOND_PERSON_REQUIRED"
    r = await client.post(f"{INC}/batches/{b['id']}/approve", json={"expected": exp, "current_password": PASSWORD}, headers=hr2)
    assert r.status_code == 200 and r.json()["approved"] == 1  # the row he did not touch
    assert r.json()["failed"] == [{"id": row["id"], "reason": "SECOND_PERSON_REQUIRED"}]
    exp = await expected_of(client, hr, b["id"])
    r = await client.post(f"{INC}/batches/{b['id']}/approve", json={"expected": exp, "current_password": PASSWORD}, headers=admin)
    assert r.status_code == 200 and r.json()["approved"] == 1 and r.json()["failed"] == [], r.text


# ================================================================================================ M1
async def test_batch_approve_is_bound_to_the_reviewed_rows(client, payroll, uid):
    hr, admin = payroll["hr"], payroll["admin"]
    b = (await client.post(f"{INC}/bulk", json=bulk_body(), headers=hr)).json()["batch"]
    bid = b["id"]
    assert (await client.post(f"{INC}/batches/{bid}/submit", json={}, headers=hr)).status_code == 200
    exp = await expected_of(client, hr, bid)
    assert len(exp) == 2
    for bad in (
        {"current_password": PASSWORD},
        {"expected": None, "current_password": PASSWORD},
        {"expected": [{"id": exp[0]["id"]}], "current_password": PASSWORD},
        {"expected": [{**exp[0], "x": 1}], "current_password": PASSWORD},
    ):
        r = await client.post(f"{INC}/batches/{bid}/approve", json=bad, headers=admin)
        assert r.status_code == 422, bad
    # one listed with a wrong version, one not listed at all: nothing is approved, both are reported
    stale = [{"id": exp[0]["id"], "version": exp[0]["version"] + 7}]
    r = await client.post(f"{INC}/batches/{bid}/approve", json={"expected": stale, "current_password": PASSWORD}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["approved"] == 0
    assert {f["id"]: f["reason"] for f in r.json()["failed"]} == {e["id"]: "VERSION_CONFLICT" for e in exp}
    # the reviewed rows go through (unknown ids in `expected` are ignored)
    ghost = {"id": str(uuid.uuid4()), "version": 1}
    r = await client.post(
        f"{INC}/batches/{bid}/approve", json={"expected": [*exp, ghost], "current_password": PASSWORD}, headers=admin
    )
    assert r.status_code == 200 and r.json()["approved"] == 2 and r.json()["failed"] == []


# ================================================================================================ M2
async def test_direct_structure_writes_lock_the_employee_first(client, payroll, uid, monkeypatch):
    from app.services import payroll_items as items_svc

    seen: list[str] = []
    real = items_svc._lock_users

    async def spy(session, ids):
        seen.extend(str(i) for i in ids)
        await real(session, ids)

    monkeypatch.setattr(items_svc, "_lock_users", spy)
    hr = payroll["hr"]
    created = await add_structure(client, hr, uid["admin"], basic="25000", hra="5000")
    assert seen == [uid["admin"]]
    seen.clear()
    r = await client.put(
        f"/api/admin/payroll/structures/{created['id']}", json=structure_body(basic="26000", hra="5000"), headers=hr
    )
    assert r.status_code == 200, r.text
    assert seen == [uid["admin"]]
    seen.clear()
    assert (await client.delete(f"/api/admin/payroll/structures/{created['id']}", headers=hr)).status_code == 204
    assert seen == [uid["admin"]]


# ================================================================================================ L1
async def test_money_bounds_are_enforced_on_the_computed_structure(client, payroll, uid):
    from app.services import payroll_increments as ig

    hr = payroll["hr"]
    await add_structure(client, hr, uid["admin"], basic="9000000", hra="0")
    r = await client.post(f"{EMP}/{uid['admin']}/increments", json=pct_body(percent="50.00"), headers=hr)
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "monthly_gross", r.text
    ok = {"basic": "100.00", "hra": "10.00", "allowances": [{"amount": "5.00"}]}
    assert ig.within_money_bounds(ok, Decimal("115.00"))
    assert not ig.within_money_bounds({**ok, "basic": "10000000.01"}, Decimal("115.00"))
    assert not ig.within_money_bounds({**ok, "allowances": [{"amount": "10000000.01"}]}, Decimal("115.00"))
    assert not ig.within_money_bounds(ok, Decimal("10000000.01"))


# ================================================================================================ L4
async def test_step_up_has_a_daily_window(client, payroll, uid, monkeypatch):
    from app.services import admin_security

    assert "REAUTH_PER_DAY = 20" in open(admin_security.__file__, encoding="utf-8").read()
    monkeypatch.setattr(admin_security, "REAUTH_PER_DAY", 4)  # the fixture already used one step-up of the admin
    inc = await ready(client, payroll, uid)
    codes = []
    for _ in range(4):
        r = await client.post(
            f"{INC}/{inc['id']}/approve", json={"expected_version": 99, "current_password": PASSWORD}, headers=payroll["admin"]
        )
        codes.append(r.status_code)
    assert codes == [409, 409, 409, 429]
    assert err(r) == "RATE_LIMITED"


async def test_step_up_limiter_fails_closed(client, payroll, uid, monkeypatch):
    """Redis down: the step-up is refused (503), the password is not even checked."""
    inc = await ready(client, payroll, uid)

    async def down(*a, **k):
        raise ConnectionError("redis down")

    from app.core.redis import get_redis

    class Down:
        def pipeline(self, *a, **k):
            raise ConnectionError("redis down")

        def __getattr__(self, name):
            return down

    app = client._transport.app
    app.dependency_overrides[get_redis] = lambda: Down()
    try:
        r = await client.post(
            f"{INC}/{inc['id']}/approve", json={"expected_version": 2, "current_password": PASSWORD}, headers=payroll["admin"]
        )
    finally:
        app.dependency_overrides.pop(get_redis, None)
    assert r.status_code == 503, r.text


# ================================================================================================ L6
def test_import_dry_run_binding_uses_a_derived_key(monkeypatch):
    from app.core.config import get_settings
    from app.services import payroll_common as pc
    from app.services import payroll_items, payroll_structure_import

    raw = get_settings().jwt_secret.encode()
    key = pc.import_binding_key()
    assert len(key) == 32 and key != raw and key == pc.import_binding_key()
    actor = types.SimpleNamespace(id=uuid.uuid4())
    digest = "a" * 64
    msg = f"{actor.id}|structures|{digest}".encode()
    got = payroll_structure_import._dry_run_key(actor, digest)
    assert got != "payroll:import:dry:" + hmac.new(raw, msg, hashlib.sha256).hexdigest()
    assert got == "payroll:import:dry:" + hmac.new(key, msg, hashlib.sha256).hexdigest()
    assert payroll_items._dry_run_key(actor, dt.date(2026, 4, 1), digest).startswith("payroll:import:dry:")
    monkeypatch.setattr(get_settings(), "jwt_secret", "another-secret-value-for-the-test-0123456789-abc")
    assert pc.import_binding_key() != key


# ================================================================================================ L2 / H1 / L3 (text of the migration)
def test_migration_text_for_the_review_fixes():
    import importlib.util
    import sys
    from pathlib import Path

    db = Path(__file__).resolve().parents[2] / "database"
    sys.path.insert(0, str(db))
    try:
        spec = importlib.util.spec_from_file_location("mig0024r", db / "alembic" / "versions" / "0024_salary_increments.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(db))
    up = mod.UPGRADE
    assert "status NOT IN ('approved','rejected') OR (decided_by IS NOT NULL" in up  # L3
    assert "decided_by IS DISTINCT FROM last_edited_by" in up  # H1
    assert "decided_by::text <> ALL (contributors)" in up
    assert "pg_trigger_depth() > 1 AND (to_jsonb(NEW) -> col) = 'null'::jsonb" in up  # L2 (FK action only)
