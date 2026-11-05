"""Security review follow-ups for payroll statutory (step-up on identifier files, current identifiers, ECR names, challan events)."""

# ruff: noqa: F811
import pytest
from sqlalchemy import select, text

from tests.conftest import PASSWORD
from tests.payroll_fixtures import PERIOD
from tests.test_payroll_statutory import (  # noqa: F401 - `stat` is a fixture
    PW,
    S,
    _details,
    _dues,
    _ids,
    _publish,
    _reset_reauth,
    stat,
)


async def test_identifier_files_need_step_up_but_pt_does_not(client, stat, uid, db, redis):
    from app.models import AuditLog

    await _ids(client, stat, uid)
    await _publish(client, stat)
    hdr = stat["hr"]
    for kind in ("pf_ecr", "esi"):
        await _reset_reauth(redis)
        r = await client.post(f"{S}/{kind}", params={"period": PERIOD}, headers=hdr)  # no body at all
        assert r.status_code == 422, kind
        r = await client.post(f"{S}/{kind}", params={"period": PERIOD}, json={}, headers=hdr)
        assert r.status_code == 422, kind
        r = await client.post(f"{S}/{kind}", params={"period": PERIOD}, json={"current_password": "wrong-password"}, headers=hdr)
        assert r.status_code == 403 and r.json()["error"]["code"] == "REAUTH_FAILED", kind
        r = await client.post(f"{S}/{kind}", params={"period": PERIOD}, json=PW, headers=hdr)
        assert r.status_code == 200, r.text
    r = await client.post(f"{S}/pt", params={"period": PERIOD}, headers=hdr)  # PT: unchanged, no password
    assert r.status_code == 200
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.statutory.export"))).scalars().all()
    assert sorted(a.after["kind"] for a in logs) == ["esi", "pf_ecr", "pt"]
    failed = (await db.execute(select(AuditLog).where(AuditLog.action == "admin.reauth_failed"))).scalars().all()
    assert {a.after["for"] for a in failed} == {"payroll.statutory.pf_ecr", "payroll.statutory.esi"}


async def test_identifier_files_have_a_daily_cap(client, stat, uid, redis):
    await _ids(client, stat, uid)
    await _publish(client, stat)
    codes = []
    for _ in range(12):
        await _reset_reauth(redis)
        r = await client.post(f"{S}/pf_ecr", params={"period": PERIOD}, json=PW, headers=stat["hr"])
        codes.append(r.status_code)
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]
    keys = [k.decode() if isinstance(k, bytes) else k for k in await redis.keys("rl:payroll:statutory_file:*")]
    assert any(k.endswith(":86400") for k in keys) and any(k.endswith(":60") for k in keys)


async def test_identifiers_come_from_current_details_not_the_frozen_payslip(client, stat, uid, redis):
    await _ids(client, stat, uid, mgr=False)  # the manager has no UAN when payroll is published
    await _publish(client, stat)
    r = await client.post(f"{S}/pf_ecr", params={"period": PERIOD}, json=PW, headers=stat["hr"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "STATUTORY_BLOCKERS"
    assert [b["employee_id"] for b in (await _dues(client, stat))["blockers"]] == ["MGR001"]
    await _details(client, stat["hr"], uid["manager"], uan="100200300401", pan="ABCDE1234G")  # fix the details only
    assert (await _dues(client, stat))["blockers"] == []
    await _reset_reauth(redis)
    r = await client.post(f"{S}/pf_ecr", params={"period": PERIOD}, json=PW, headers=stat["hr"])
    assert r.status_code == 200, r.text
    assert "100200300401" in r.text and "ABCDE" not in r.text


def test_ecr_name_allowlist():
    from app.services.payroll_statutory import ecr_name

    assert ecr_name("  Ravi   Kumar  ") == "Ravi Kumar"
    assert ecr_name("O'Brien-Smith Jr.") == "O'Brien-Smith Jr."
    assert ecr_name("A#B~C, D3E") == "A B C D E"  # symbols and digits become a space, spaces collapse
    assert len(ecr_name("A" * 200)) == 85
    assert ecr_name("José Ramírez") is None  # non-ASCII: never written as '?'
    assert ecr_name("குமார்") is None
    assert ecr_name("1234 ###") is None  # nothing left
    assert ecr_name("") is None


def test_unusable_ecr_name_is_a_blocker():
    from app.services import payroll_statutory as ps

    def row(name):
        class R:
            ident = {"uan": "100200300400"}
            result = {"deductions": [{"code": "PF", "amount": "10.00"}], "employer": []}
            employee_id = "X1"

        R.name = name
        return R()

    assert ps._blockers([row("Ravi Kumar")], ("pf",)) == []
    out = ps._blockers([row("José")], ("pf",))
    assert out[0]["missing"] == [{"field": "NAME", "reason": "invalid"}]
    assert ps._blockers([row("")], ("pf",))[0]["missing"] == [{"field": "NAME", "reason": "missing"}]
    assert ps._blockers([row("José")], ("esi",)) == []  # the ESI workbook is unicode-safe


async def test_first_challan_audit_has_entity_id_and_paid_on(client, stat, uid, db, redis):
    from app.models import AuditLog, StatutoryChallan

    await _ids(client, stat, uid)
    await _publish(client, stat)
    await _reset_reauth(redis)
    body = {"type": "pf", "period": PERIOD, "status": "paid", "paid_on": "2025-07-10", "reference": "TRRN-1", **PW}
    assert (await client.put(f"{S}/challans", json=body, headers=stat["hr"])).status_code == 200
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.statutory.challan_paid"))).scalar_one()
    challan = (await db.execute(select(StatutoryChallan))).scalar_one()
    assert log.entity_id == str(challan.id)
    assert log.after["paid_on"] == "2025-07-10" and "TRRN" not in str(log.after)


async def test_challan_amount_and_nothing_due_rules(client, stat, uid, redis):
    await _ids(client, stat, uid)
    await _publish(client, stat)
    hdr = stat["hr"]
    base = {"type": "pf", "period": PERIOD, "status": "paid", "paid_on": "2025-07-10", "reference": "R1", **PW}
    for bad in ("0.00", "0", "-5.00"):
        await _reset_reauth(redis)
        r = await client.put(f"{S}/challans", json={**base, "amount": bad}, headers=hdr)
        assert r.status_code == 422, bad
    tds = {**base, "type": "tds"}  # nothing is due for TDS in this run
    await _reset_reauth(redis)
    r = await client.put(f"{S}/challans", json=tds, headers=hdr)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NOTHING_DUE"
    await _reset_reauth(redis)
    r = await client.put(f"{S}/challans", json={**tds, "amount": "100.00"}, headers=hdr)
    assert r.status_code == 200  # an explicit positive amount is still recordable


async def test_every_challan_change_writes_an_append_only_event(client, stat, uid, db, redis):
    from app.models import StatutoryChallanEvent

    await _ids(client, stat, uid)
    await _publish(client, stat)
    hdr = stat["hr"]
    paid = {"type": "pf", "period": PERIOD, "status": "paid", "paid_on": "2025-07-10", "reference": "TRRN-123", **PW}
    unpaid = {"type": "pf", "period": PERIOD, "status": "unpaid", **PW}
    repaid = {**paid, "paid_on": "2025-07-12", "reference": "TRRN-456", "amount": "6000.00"}
    for body in (paid, unpaid, repaid):
        await _reset_reauth(redis)
        assert (await client.put(f"{S}/challans", json=body, headers=hdr)).status_code == 200
    events = (await db.execute(select(StatutoryChallanEvent))).scalars().all()
    assert len(events) == 3
    by = {(e.action, e.reference or e.prev_reference): e for e in events}
    first, cleared, second = by[("paid", "TRRN-123")], by[("unpaid", "TRRN-123")], by[("paid", "TRRN-456")]
    assert first.prev_status is None and first.amount == "6480.00" and str(first.paid_on) == "2025-07-10"
    # mark unpaid: the cleared paid_on / reference are kept in prev_*
    assert cleared.prev_status == "paid" and cleared.prev_reference == "TRRN-123" and str(cleared.prev_paid_on) == "2025-07-10"
    assert cleared.paid_on is None and cleared.reference is None and cleared.status == "unpaid"
    # re-pay: the overwritten amount is kept
    assert second.prev_status == "unpaid" and second.prev_amount == "6480.00" and second.amount == "6000.00"
    assert all(e.actor_id is not None and e.challan_id == first.challan_id for e in events)
    raw = (await db.execute(text("SELECT reference_enc, prev_reference_enc, amount_enc FROM statutory_challan_events"))).all()
    blob = b"".join(bytes(v) for row in raw for v in row if v is not None)
    assert b"TRRN" not in blob and b"6480" not in blob


@pytest.mark.parametrize("kind", ["pf_ecr", "esi"])
async def test_non_hr_cannot_use_identifier_files_even_with_password(client, H, kind):
    r = await client.post(f"{S}/{kind}", json={"current_password": PASSWORD}, headers=H["employee"])
    assert r.status_code == 403
