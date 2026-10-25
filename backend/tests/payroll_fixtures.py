"""Shared payroll test fixtures and helpers (fixtures are re-exported by conftest.py)."""

import pytest
import pytest_asyncio

from tests.conftest import PASSWORD, USERS, bearer

PERIOD = "2025-06"  # a past month: independent of the clock; June 2025 has 30 calendar days
EFF = "2025-01-01"


@pytest_asyncio.fixture
async def H(client):
    """role -> auth headers"""
    out = {}
    for role, eid in USERS.items():
        r = await client.post("/api/auth/login", json={"employee_id": eid, "password": PASSWORD})
        assert r.status_code == 200, r.text
        out[role] = bearer(r.json())
    return out


@pytest.fixture
def uid(seeded):
    return {k: str(v) for k, v in seeded["users"].items()}


def structure_body(basic="20000", hra="0", eff=EFF, **extra):
    body = {
        "effective_from": eff, "basic": basic, "hra": hra, "allowances": [], "fixed_deductions": [], "tds_monthly": "0",
        "pf_applicable": True, "pf_on_full_basic": False, "esi_applicable": True, "pt_applicable": True,
    }  # fmt: skip
    body.update(extra)
    return body


async def add_structure(client, hdr, user_id, **kw):
    r = await client.post(f"/api/admin/payroll/employees/{user_id}/structures", json=structure_body(**kw), headers=hdr)
    assert r.status_code == 201, r.text
    return r.json()


async def set_setting(client, hdr, **settings):
    r = await client.put(
        "/api/admin/payroll/settings", json={"settings": {f"payroll.{k}": v for k, v in settings.items()}}, headers=hdr
    )
    assert r.status_code == 200, r.text


async def new_run(client, hdr, period=PERIOD):
    r = await client.post("/api/admin/payroll/runs", json={"period": period, "pay_date": "2025-06-30"}, headers=hdr)
    assert r.status_code == 201, r.text
    return r.json()


async def exclude_from_payroll(client, hdr, user_id):
    r = await client.put(
        f"/api/admin/payroll/employees/{user_id}/details",
        json={"include_in_payroll": False, "current_password": PASSWORD},
        headers=hdr,
    )
    assert r.status_code == 200, r.text


@pytest_asyncio.fixture
async def payroll(client, H, uid):
    """EMP001 (20000 basic) + MGR001 (30000) have structures and are paid; HR001 / ADM001 are excluded. No absent LOP."""
    await set_setting(client, H["admin"], lop_absent_days=False)
    await exclude_from_payroll(client, H["admin"], uid["hr"])
    await exclude_from_payroll(client, H["hr"], uid["admin"])
    await add_structure(client, H["hr"], uid["employee"], basic="20000", hra="5000")
    await add_structure(client, H["hr"], uid["manager"], basic="30000", hra="10000")
    return H


async def payslips_of(client, hdr, run_id, **params):
    r = await client.get(f"/api/admin/payroll/runs/{run_id}/payslips", params=params, headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def row_for(client, hdr, run_id, emp):
    return next(i for i in await payslips_of(client, hdr, run_id) if i["user"]["employee_id"] == emp)


async def publish_run(client, hdr, run_id, pw=PASSWORD):
    return await client.post(f"/api/admin/payroll/runs/{run_id}/publish", json={"current_password": pw}, headers=hdr)


async def process_and_publish(client, hdr, run_id):
    r = await client.post(f"/api/admin/payroll/runs/{run_id}/process", headers=hdr)
    assert r.status_code == 200, r.text
    r = await publish_run(client, hdr, run_id)
    assert r.status_code == 200, r.text
    return r.json()
