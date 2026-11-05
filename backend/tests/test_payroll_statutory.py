"""Payroll phase 2 (plan 14): statutory files, dues and challans."""

import io

import pytest
import pytest_asyncio
from openpyxl import load_workbook
from sqlalchemy import select, text

from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD, add_structure, exclude_from_payroll, new_run, process_and_publish, set_setting

B = "/api/admin/payroll"
S = f"{B}/statutory"
PW = {"current_password": PASSWORD}


@pytest_asyncio.fixture
async def stat(client, H, uid):
    """EMP001: basic 12000 + hra 3000 (ESI eligible, PF on 12000). MGR001: basic 30000 + hra 10000 (PF capped at 15000)."""
    await set_setting(client, H["admin"], lop_absent_days=False)
    await exclude_from_payroll(client, H["admin"], uid["hr"])
    await exclude_from_payroll(client, H["hr"], uid["admin"])
    await add_structure(client, H["hr"], uid["employee"], basic="12000", hra="3000")
    await add_structure(client, H["hr"], uid["manager"], basic="30000", hra="10000")
    return H


async def _details(client, hdr, user_id, **kw):
    r = await client.put(f"{B}/employees/{user_id}/details", json={**kw, "current_password": PASSWORD}, headers=hdr)
    assert r.status_code == 200, r.text


async def _ids(client, stat, uid, *, emp=True, mgr=True):
    if emp:
        await _details(client, stat["hr"], uid["employee"], uan="100200300400", esi_ip_number="1234567890", pan="ABCDE1234F")
    if mgr:
        await _details(client, stat["hr"], uid["manager"], uan="100200300401", pan="ABCDE1234G")


async def _publish(client, stat):
    run = await new_run(client, stat["hr"])
    await process_and_publish(client, stat["hr"], run["id"])
    return run


async def _dues(client, stat):
    r = await client.get(f"{S}/dues", params={"period": PERIOD}, headers=stat["hr"])
    assert r.status_code == 200, r.text
    return r.json()["months"][0]


async def _reset_reauth(redis):
    for k in await redis.keys("rl:admin:reauth:*"):
        await redis.delete(k)  # the step-up check is limited to 5 per minute


def _pf(month):
    return next(i for i in month["items"] if i["type"] == "pf")


@pytest.mark.parametrize("role", ["employee", "manager"])
@pytest.mark.parametrize(
    "method,path",
    [("GET", "/statutory/dues"), ("POST", "/statutory/pf_ecr"), ("POST", "/statutory/esi"), ("PUT", "/statutory/challans")],
)
async def test_non_hr_forbidden(client, H, role, method, path):
    r = await client.request(method, B + path, headers=H[role], json={} if method == "PUT" else None)
    assert r.status_code == 403


async def test_unpublished_run_is_refused(client, stat):
    r = await client.post(f"{S}/pf_ecr", params={"period": PERIOD}, json=PW, headers=stat["hr"])
    assert r.status_code == 404
    run = await new_run(client, stat["hr"])
    assert (await client.post(f"{B}/runs/{run['id']}/process", headers=stat["hr"])).status_code == 200
    r = await client.post(f"{S}/pf_ecr", params={"period": PERIOD}, json=PW, headers=stat["hr"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "RUN_NOT_PUBLISHED"
    d = await _dues(client, stat)
    assert d["run_status"] == "processed" and d["items"] == []


async def test_missing_uan_blocks_ecr_and_is_listed(client, stat, uid):
    await _ids(client, stat, uid, mgr=False)  # the manager has no UAN / PAN
    await _publish(client, stat)
    r = await client.post(f"{S}/pf_ecr", params={"period": PERIOD}, json=PW, headers=stat["hr"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "STATUTORY_BLOCKERS"
    assert [d["field"] for d in r.json()["error"]["details"]] == ["MGR001"]
    assert "100200300400" not in r.text
    b = {x["employee_id"]: x for x in (await _dues(client, stat))["blockers"]}
    assert {m["field"] for m in b["MGR001"]["missing"]} == {"UAN"} and "EMP001" not in b


async def test_invalid_uan_counts_as_blocker_never_skipped(client, stat, uid):
    await _ids(client, stat, uid)
    await _publish(client, stat)
    # a malformed value that slipped in earlier would still be reported (checked at the service level)
    from app.services import payroll_statutory as ps

    class R:
        ident = {"uan": "12AB"}
        result = {"deductions": [{"code": "PF", "amount": "10.00"}], "employer": []}
        employee_id, name = "X1", "X"

    out = ps._blockers([R()], ("pf",))
    assert out[0]["missing"] == [{"field": "UAN", "reason": "invalid"}]


async def test_pf_ecr_content(client, stat, uid):
    await _ids(client, stat, uid)
    await _publish(client, stat)
    r = await client.post(f"{S}/pf_ecr", params={"period": PERIOD}, json=PW, headers=stat["hr"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store" and "payroll-pf-ecr-2025-06.txt" in r.headers["content-disposition"]
    lines = {ln.split("#~#")[0]: ln.split("#~#") for ln in r.text.strip().split("\r\n")}
    assert set(lines) == {"100200300400", "100200300401"}
    emp, mgr = lines["100200300400"], lines["100200300401"]
    assert len(emp) == 11
    # gross, epf wages, eps wages, edli wages, ee share, eps share, er-eps diff, ncp days, refund
    assert emp[2:] == ["15000", "12000", "12000", "12000", "1440", "1000", "440", "0", "0"]
    assert mgr[2:] == ["40000", "15000", "15000", "15000", "1800", "1250", "550", "0", "0"]


async def test_esi_file_only_eligible_employees(client, stat, uid):
    await _ids(client, stat, uid)
    await _publish(client, stat)
    r = await client.post(f"{S}/esi", params={"period": PERIOD}, json=PW, headers=stat["hr"])
    assert r.status_code == 200, r.text
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["ESI Contribution", "Reconciliation"]
    rows = list(wb["ESI Contribution"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 1 and rows[0][0] == "1234567890" and rows[0][2] == 30 and rows[0][3] == 15000


async def test_esi_missing_ip_blocks(client, stat, uid):
    await _details(client, stat["hr"], uid["employee"], uan="100200300400")
    await _ids(client, stat, uid, emp=False)
    await _publish(client, stat)
    r = await client.post(f"{S}/esi", params={"period": PERIOD}, json=PW, headers=stat["hr"])
    assert r.status_code == 409 and r.json()["error"]["details"][0]["field"] == "EMP001"


async def test_pt_summary(client, stat):
    await _publish(client, stat)  # PT needs no identifiers
    r = await client.post(f"{S}/pt", params={"period": PERIOD}, headers=stat["hr"])
    assert r.status_code == 200, r.text
    assert load_workbook(io.BytesIO(r.content)).sheetnames == ["PT by Employee", "PT by Slab"]


async def test_dues_and_challan_flow(client, stat, uid, db, redis):
    from app.models import AuditLog

    await _ids(client, stat, uid)
    await _publish(client, stat)
    d = await _dues(client, stat)
    pf = _pf(d)
    assert [i["type"] for i in d["items"]] == ["pf", "esi", "pt", "tds"]
    assert pf["amount"] == "6480.00" and pf["employees"] == 2 and pf["status"] == "unpaid"  # (1440+1440) + (1800+1800)
    assert next(i for i in d["items"] if i["type"] == "esi")["employees"] == 1

    body = {"type": "pf", "period": PERIOD, "status": "paid", "paid_on": "2025-07-10", "reference": "TRRN-123"}
    hdr = stat["hr"]
    await _reset_reauth(redis)
    # the step-up password is mandatory and must be right
    assert (await client.put(f"{S}/challans", json=body, headers=hdr)).status_code == 422
    r = await client.put(f"{S}/challans", json={**body, "current_password": "wrong-password"}, headers=hdr)
    assert r.status_code == 403 and r.json()["error"]["code"] == "REAUTH_FAILED"
    # dates outside the allowed window
    for bad in ("2025-05-31", "2999-01-01"):
        r = await client.put(f"{S}/challans", json={**body, "paid_on": bad, "current_password": PASSWORD}, headers=hdr)
        assert r.status_code == 422, bad

    await _reset_reauth(redis)
    r = await client.put(f"{S}/challans", json={**body, "current_password": PASSWORD}, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paid"
    r = await client.put(f"{S}/challans", json={**body, "current_password": PASSWORD}, headers=hdr)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CHALLAN_ALREADY_PAID"

    pf = _pf(await _dues(client, stat))
    assert pf["status"] == "paid" and pf["paid_on"] == "2025-07-10" and pf["reference"] == "TRRN-123"
    assert pf["paid_amount"] == "6480.00" and pf["mismatch"] is False

    # amount and reference are encrypted at rest; the audit log carries no money and no reference
    stored = (await db.execute(text("SELECT amount_enc, reference_enc FROM statutory_challans"))).one()
    assert b"6480" not in bytes(stored[0]) and b"TRRN" not in bytes(stored[1])
    logs = (await db.execute(select(AuditLog).where(AuditLog.action.like("payroll.statutory.%")))).scalars().all()
    assert any(a.action == "payroll.statutory.challan_paid" for a in logs)
    blob = " ".join(str(a.after) + str(a.before) for a in logs)
    assert "6480" not in blob and "TRRN" not in blob

    await _reset_reauth(redis)
    unpaid = {"type": "pf", "period": PERIOD, "status": "unpaid", "current_password": PASSWORD}
    r = await client.put(f"{S}/challans", json=unpaid, headers=hdr)
    assert r.status_code == 200 and r.json()["status"] == "unpaid" and r.json()["reference"] is None
    r = await client.put(f"{S}/challans", json=unpaid, headers=hdr)
    assert r.status_code == 409 and r.json()["error"]["code"] == "CHALLAN_NOT_PAID"
    # an amount different from the due is flagged as a mismatch
    r = await client.put(f"{S}/challans", json={**body, "amount": "6000.00", "current_password": PASSWORD}, headers=hdr)
    assert r.status_code == 200
    assert _pf(await _dues(client, stat))["mismatch"] is True


async def test_challan_needs_published_run(client, stat):
    r = await client.put(
        f"{S}/challans",
        json={"type": "pf", "period": PERIOD, "status": "paid", "paid_on": "2025-07-10", "current_password": PASSWORD},
        headers=stat["hr"],
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    "extra",
    [
        {"type": "gst"},
        {"status": "unpaid"},  # paid_on with unpaid
        {"paid_on": None},  # paid without paid_on
        {"reference": "bad<script>"},
        {"amount": "1.234"},
        {"period": "2025-13"},
    ],
)
async def test_challan_validation(client, stat, extra):
    body = {"type": "pf", "period": PERIOD, "status": "paid", "paid_on": "2025-07-10", "current_password": PASSWORD, **extra}
    r = await client.put(f"{S}/challans", json=body, headers=stat["hr"])
    assert r.status_code == 422
