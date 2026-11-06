"""Helpers shared by the plan 17 work package 5a test modules (publish requests, payments, delivery)."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.core import security
from app.models import EmployeePayrollDetails, EmployeeProfile, User
from tests.conftest import PASSWORD, bearer

ADMIN = "/api/admin/payroll"
RUNS = f"{ADMIN}/runs"
PAYSLIPS = f"{ADMIN}/payslips"
REQUESTS = f"{ADMIN}/publish-requests"
BATCHES = f"{ADMIN}/payment-batches"
ACCOUNTS = f"{ADMIN}/bank-accounts"
MC_KEY = "payroll.publish_requires_second_person"
TODAY = dt.datetime(2026, 10, 21, 4, 30, tzinfo=dt.UTC)  # "today" of the payment tests (office tz Asia/Kolkata)


def err(r) -> str:
    return r.json()["error"]["code"]


@pytest.fixture
def quiet(monkeypatch):
    """No endpoint rate limits, a high step-up limit and a fixed 'today'. Opt in with `pytestmark = pytest.mark.usefixtures('quiet')`."""
    from app.services import admin_security
    from app.services import payroll_common as pc

    async def no_limit(*a, **k):
        return None

    async def today(session):
        return TODAY.date()

    monkeypatch.setattr(pc, "limit", no_limit)
    monkeypatch.setattr(admin_security, "REAUTH_PER_MINUTE", 10_000)
    monkeypatch.setattr(admin_security, "REAUTH_PER_DAY", 10_000)
    monkeypatch.setattr("app.services.payroll_runs._today", today)
    monkeypatch.setattr("app.services.payroll_payments.utcnow", lambda: TODAY)
    # bank detail changes are stamped a year ago (one second apart), i.e. outside the look-back window; `recent_bank_changes()` undoes it
    clock = {"t": TODAY - dt.timedelta(days=400)}

    def old_now():
        clock["t"] += dt.timedelta(seconds=1)
        return clock["t"]

    monkeypatch.setattr("app.services.payroll_structures.utcnow", old_now)


@contextmanager
def recent_bank_changes():
    """Bank detail changes inside the block are stamped 'now' (real clock, later than the patched TODAY): inside the look-back window."""
    from app.core.clock import utcnow as real_now

    with patch("app.services.payroll_structures.utcnow", real_now):
        yield


async def extra_user(client, db, seeded, employee_id: str, role: str = "admin", *, on_payroll: bool = False) -> tuple[User, dict]:
    """Another person with a role's permissions (maker-checker needs more people than the four seeded users). Not paid by default."""
    u = User(
        employee_id=employee_id, password_hash=security.hash_password(PASSWORD), full_name=f"Test {employee_id}",
        official_email=f"{employee_id.lower()}@corp-mail.com", role_id=seeded["roles"][role], department_id=seeded["dept"],
        shift_id=seeded["shift"], must_change_password=False, status="active",
    )  # fmt: skip
    db.add(u)
    await db.flush()
    db.add(EmployeeProfile(user_id=u.id))
    if not on_payroll:
        db.add(EmployeePayrollDetails(user_id=u.id, include_in_payroll=False, payment_mode="bank_transfer"))
    await db.commit()
    r = await client.post("/api/auth/login", json={"employee_id": employee_id, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return u, bearer(r.json())


async def enable_mc(client, admin, value: bool = True) -> None:
    r = await client.put(f"{ADMIN}/settings", json={"settings": {MC_KEY: value}, "current_password": PASSWORD}, headers=admin)
    assert r.status_code == 200, r.text


async def process(client, hdr, run_id, expect=200):
    r = await client.post(f"{RUNS}/{run_id}/process", headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def request_run(client, hdr, run_id, expect=201, **body):
    r = await client.post(f"{RUNS}/{run_id}/publish-requests", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def request_payslip(client, hdr, payslip_id, expect=201, **body):
    r = await client.post(f"{PAYSLIPS}/{payslip_id}/publish-requests", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def decide(client, hdr, req, action, expect=200, **body):
    body.setdefault("expected_version", req["version"])
    if action == "approve":
        body.setdefault("current_password", PASSWORD)
    if action == "reject":
        body.setdefault("comment", "Please check the figures")
    r = await client.post(f"{REQUESTS}/{req['id']}/{action}", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


# ---------------------------------------------------------------------------------------------- payments
BANK = {"bank_name": "HDFC Bank", "bank_account": "123456789012", "bank_ifsc": "HDFC0001234"}
OTHER_BANK = {"bank_name": "ICICI Bank", "bank_account": "555566667777", "bank_ifsc": "ICIC0000456"}
DEBIT = {"label": "HDFC salary account", "bank_name": "HDFC Bank", "account": "987654321012", "ifsc": "ICIC0000123"}


async def set_bank(client, hdr, user_id, expect=200, **over):
    body = {**BANK, "pan": "ABCDE1234F", **over, "current_password": PASSWORD}
    r = await client.put(f"{ADMIN}/employees/{user_id}/details", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def published_run(client, payroll, uid, *, alert_days: int = 0, banks: tuple[str, ...] = ("employee", "manager")):
    """EMP001 and MGR001 get bank details, the run is processed and published (maker-checker for publish off). Returns the run."""
    from tests.payroll_fixtures import new_run, process_and_publish

    if (
        alert_days
    ):  # default: markers are old and the setting stays at its default (30); the minimum is 7 and it is a guarded setting
        r = await client.put(
            f"{ADMIN}/settings",
            json={"settings": {"payroll.bank_change_alert_days": alert_days}, "current_password": PASSWORD},
            headers=payroll["admin"],
        )
        assert r.status_code == 200, r.text
    with recent_bank_changes() if alert_days else _nothing():
        for who in banks:
            await set_bank(client, payroll["hr"], uid[who])
    run = await new_run(client, payroll["hr"])
    await process_and_publish(client, payroll["hr"], run["id"])
    return run


@contextmanager
def _nothing():
    yield


async def new_account(client, hdr, expect=201, **over):
    r = await client.post(ACCOUNTS, json={**DEBIT, "is_default": True, "current_password": PASSWORD, **over}, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def new_batch(client, hdr, run_id, expect=201, **over):
    body = {"debit_account_id": None, "value_date": "2026-10-31", "user_ids": None, **over}
    r = await client.post(f"{RUNS}/{run_id}/payment-batches", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def batch_act(client, hdr, batch, action, expect=200, **body):
    if action in ("submit", "withdraw", "approve", "reject") and "expected_version" not in body:
        body["expected_version"] = batch["version"]
    if action in ("approve", "confirm", "file"):
        body.setdefault("current_password", PASSWORD)
    if action == "reject":
        body.setdefault("comment", "Please check the amounts")
    if action == "cancel":
        body.setdefault("reason", "Wrong month")
    r = await client.post(f"{BATCHES}/{batch['id']}/{action}", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r if action == "file" else r.json()


async def approved_batch(client, payroll, uid, run, **over):
    """A submitted batch (HR) approved by the admin."""
    b = await new_batch(client, payroll["hr"], run["id"], **over)
    b = await batch_act(client, payroll["hr"], b, "submit")
    return await batch_act(client, payroll["admin"], b, "approve")
