"""Plan 17 work package 5a: payslip delivery tracking and reminders (contract 19.3) and the new guarded / bank settings (19.10)."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from sqlalchemy import select

from app.models import AuditLog, DeviceToken, PayslipDelivery
from app.services import notifications
from tests.conftest import PASSWORD
from tests.payroll_controls_helpers import ADMIN, RUNS, err, quiet  # noqa: F401  (fixture)
from tests.payroll_fixtures import new_run, process_and_publish, row_for

pytestmark = pytest.mark.usefixtures("quiet")

SETTINGS = f"{ADMIN}/settings"


async def published(client, payroll):
    run = await new_run(client, payroll["hr"])
    await process_and_publish(client, payroll["hr"], run["id"])
    return run


async def add_device(db, user_id: str, token: str = "tok-1") -> None:
    db.add(DeviceToken(user_id=uuid.UUID(user_id), fcm_token=token, platform="android"))
    await db.commit()


async def remind(client, hdr, run_id, expect=200, **body):
    r = await client.post(f"{RUNS}/{run_id}/delivery/remind", json=body, headers=hdr)
    assert r.status_code == expect, r.text
    return r.json()


async def own_payslip_id(client, payroll) -> str:
    return (await client.get("/api/payslips", params={"year": 2025}, headers=payroll["employee"])).json()["items"][0]["id"]


# ================================================================================================ tracking
async def test_publish_creates_rows_and_the_owner_views_set_the_timestamps(client, payroll, db, uid):
    run = await published(client, payroll)
    rows = list((await db.execute(select(PayslipDelivery))).scalars())
    assert len(rows) == 2 and all(r.first_viewed_at is None and r.first_downloaded_at is None and r.reminders == 0 for r in rows)
    pid = await own_payslip_id(client, payroll)  # the list does not count as a view
    db.expire_all()
    assert all(r.first_viewed_at is None for r in (await db.execute(select(PayslipDelivery))).scalars())
    assert (await client.get(f"/api/payslips/{pid}", headers=payroll["employee"])).status_code == 200
    db.expire_all()
    mine = await db.get(PayslipDelivery, uuid.UUID(pid))
    first = mine.first_viewed_at
    assert first is not None and mine.first_downloaded_at is None
    assert (await client.get(f"/api/payslips/{pid}", headers=payroll["employee"])).status_code == 200
    db.expire_all()
    assert (await db.get(PayslipDelivery, uuid.UUID(pid))).first_viewed_at == first  # write-once
    r = await client.get(f"/api/payslips/{pid}/pdf", headers=payroll["employee"])
    assert r.status_code == 200
    db.expire_all()
    mine = await db.get(PayslipDelivery, uuid.UUID(pid))
    assert mine.first_downloaded_at is not None and mine.first_viewed_at == first
    # an admin looking at the payslip is not the employee: nothing changes for the other row
    other = (await db.execute(select(PayslipDelivery).where(PayslipDelivery.payslip_id != uuid.UUID(pid)))).scalar_one()
    row = await row_for(client, payroll["hr"], run["id"], "MGR001")
    assert row["id"] == str(other.payslip_id)
    await client.get(f"/api/admin/payroll/payslips/{row['id']}", headers=payroll["hr"])
    await client.get(f"/api/admin/payroll/payslips/{row['id']}/pdf", headers=payroll["hr"])
    db.expire_all()
    other = (await db.execute(select(PayslipDelivery).where(PayslipDelivery.payslip_id != uuid.UUID(pid)))).scalar_one()
    assert other.first_viewed_at is None and other.first_downloaded_at is None


async def test_a_payslip_published_before_the_migration_gets_its_row_on_first_view(client, payroll, db):
    await published(client, payroll)
    pid = await own_payslip_id(client, payroll)
    await db.delete(await db.get(PayslipDelivery, uuid.UUID(pid)))
    await db.commit()
    assert (await client.get(f"/api/payslips/{pid}", headers=payroll["employee"])).status_code == 200
    db.expire_all()
    assert (await db.get(PayslipDelivery, uuid.UUID(pid))).first_viewed_at is not None


async def test_publish_push_stamps_the_delivery_row(client, payroll, db, uid):
    await add_device(db, uid["employee"])
    run = await published(client, payroll)
    db.expire_all()
    by_user = {r.user_id: r for r in (await db.execute(select(PayslipDelivery))).scalars()}
    assert by_user[uuid.UUID(uid["employee"])].pushed_at is not None and by_user[uuid.UUID(uid["employee"])].push_count == 1
    assert by_user[uuid.UUID(uid["manager"])].pushed_at is None and by_user[uuid.UUID(uid["manager"])].push_count == 0
    assert run["id"]


# ================================================================================================ report
async def test_delivery_report_counts_filters_and_audit(client, payroll, db, uid):
    await add_device(db, uid["employee"])
    run = await published(client, payroll)
    hr = payroll["hr"]
    pid = await own_payslip_id(client, payroll)
    await client.get(f"/api/payslips/{pid}/pdf", headers=payroll["employee"])
    rep = (await client.get(f"{RUNS}/{run['id']}/delivery", headers=hr)).json()
    assert rep["counts"] == {"published": 2, "pushed": 1, "viewed": 1, "downloaded": 1, "no_device": 1}
    assert rep["total"] == 2 and rep["page"] == 1 and rep["page_size"] == 20
    assert [i["user"]["employee_id"] for i in rep["items"]] == ["EMP001", "MGR001"]
    first = rep["items"][0]
    assert set(first) == {
        "user",
        "payslip_id",
        "revision",
        "pushed_at",
        "push_count",
        "reminders",
        "first_viewed_at",
        "first_downloaded_at",
        "has_device",
    }
    assert first["has_device"] is True and first["first_viewed_at"] and first["first_downloaded_at"] and first["reminders"] == 0
    for state, who in (("viewed", ["EMP001"]), ("not_viewed", ["MGR001"]), ("no_device", ["MGR001"])):
        items = (await client.get(f"{RUNS}/{run['id']}/delivery", params={"state": state}, headers=hr)).json()["items"]
        assert [i["user"]["employee_id"] for i in items] == who, state
    q = (await client.get(f"{RUNS}/{run['id']}/delivery", params={"q": "mgr"}, headers=hr)).json()
    assert [i["user"]["employee_id"] for i in q["items"]] == ["MGR001"] and q["counts"]["published"] == 2
    assert (await client.get(f"{RUNS}/{run['id']}/delivery", params={"state": "bogus"}, headers=hr)).status_code == 422
    assert (await client.get(f"{RUNS}/{uuid.uuid4()}/delivery", headers=hr)).status_code == 404
    assert (await client.get(f"{RUNS}/{run['id']}/delivery", headers=payroll["employee"])).status_code == 403
    a = (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.delivery.view"))).scalars().first()
    assert a.after == {"count": 2}


# ================================================================================================ reminders
async def test_remind_rules_24_hours_and_three_in_total(client, payroll, db, uid):
    await add_device(db, uid["employee"])
    await add_device(db, uid["manager"], "tok-2")
    run = await published(client, payroll)
    hr = payroll["hr"]
    notifications.OUTBOX.clear()
    out = await remind(client, hr, run["id"])  # null = every not-viewed employee with a device
    assert out["sent"] == 2 and out["skipped"] == []
    sent = [o for o in notifications.OUTBOX if o["payload"]["type"] == "payslip_reminder"]
    assert len(sent) == 2 and sent[0]["payload"]["title"] == "Payslip available"
    assert sent[0]["payload"]["body"] == "Your payslip for June 2025 is waiting in the app."
    again = await remind(client, hr, run["id"])
    assert again["sent"] == 0 and {s["reason"] for s in again["skipped"]} == {"TOO_SOON"}
    for n in (2, 3):  # two more reminders, each a day apart
        for d in (await db.execute(select(PayslipDelivery))).scalars():
            d.last_reminded_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=25)
        await db.commit()
        assert (await remind(client, hr, run["id"]))["sent"] == 2
        db.expire_all()
        assert {d.reminders for d in (await db.execute(select(PayslipDelivery))).scalars()} == {n}
    for d in (await db.execute(select(PayslipDelivery))).scalars():
        d.last_reminded_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=25)
    await db.commit()
    capped = await remind(client, hr, run["id"])
    assert capped["sent"] == 0 and {s["reason"] for s in capped["skipped"]} == {"LIMIT_REACHED"}
    audits = [a.after for a in (await db.execute(select(AuditLog).where(AuditLog.action == "payroll.delivery.remind"))).scalars()]
    assert audits[0] == {"run_id": run["id"], "sent": 2, "skipped": 0} and audits[-1]["skipped"] == 2


async def test_remind_skips_viewed_no_device_and_unpublished_people(client, payroll, db, uid):
    await add_device(db, uid["employee"])
    run = await published(client, payroll)
    hr = payroll["hr"]
    out = await remind(client, hr, run["id"], user_ids=[uid["employee"], uid["manager"], uid["admin"]])
    reasons = {s["user_id"]: s["reason"] for s in out["skipped"]}
    assert out["sent"] == 1 and reasons == {uid["manager"]: "NO_DEVICE", uid["admin"]: "NOT_PUBLISHED"}
    pid = await own_payslip_id(client, payroll)
    await client.get(f"/api/payslips/{pid}", headers=payroll["employee"])
    out = await remind(client, hr, run["id"], user_ids=[uid["employee"]])
    assert out["sent"] == 0 and out["skipped"] == [{"user_id": uid["employee"], "reason": "VIEWED"}]
    assert (await remind(client, hr, run["id"]))["sent"] == 0  # null = not viewed AND a device: nobody left
    assert (
        await client.post(f"{RUNS}/{run['id']}/delivery/remind", json={"user_ids": [str(uuid.uuid4())] * 501}, headers=hr)
    ).status_code == 422
    assert (await client.post(f"{RUNS}/{run['id']}/delivery/remind", json={"x": 1}, headers=hr)).status_code == 422


async def test_remind_needs_a_published_run_and_the_right_role(client, payroll):
    run = await new_run(client, payroll["hr"])
    r = await client.post(f"{RUNS}/{run['id']}/delivery/remind", json={}, headers=payroll["hr"])
    assert r.status_code == 409 and err(r) == "PAYROLL_LOCKED"
    for who in ("employee", "manager"):
        assert (await client.post(f"{RUNS}/{run['id']}/delivery/remind", json={}, headers=payroll[who])).status_code == 403
    assert (await client.post(f"{RUNS}/{run['id']}/delivery/remind", json={})).status_code == 401
    assert (await client.post(f"{RUNS}/{uuid.uuid4()}/delivery/remind", json={}, headers=payroll["hr"])).status_code == 404


# ================================================================================================ settings (19.10)
async def test_new_settings_defaults_ranges_and_snapshot(client, payroll):
    s = (await client.get(SETTINGS, headers=payroll["hr"])).json()["settings"]
    assert s["payroll.publish_request_ttl_hours"] == 72 and s["payroll.bank_file_requires_second_person"] is True
    assert s["payroll.bank_file_format"] == "generic_neft_csv" and s["payroll.bank_payment_type"] == "NEFT"
    assert s["payroll.bank_change_alert_days"] == 30
    for key, bad in (("publish_request_ttl_hours", 0), ("publish_request_ttl_hours", 337), ("bank_change_alert_days", 91),
                     ("bank_payment_type", "UPI"), ("bank_file_format", "xml"), ("bank_file_requires_second_person", "yes")):  # fmt: skip
        r = await client.put(SETTINGS, json={"settings": {f"payroll.{key}": bad}}, headers=payroll["hr"])
        assert r.status_code == 422, (key, bad)
    r = await client.put(
        SETTINGS,
        json={"settings": {"payroll.publish_request_ttl_hours": 24, "payroll.bank_payment_type": "RTGS"}},
        headers=payroll["hr"],
    )
    assert r.status_code == 200
    run = await new_run(client, payroll["hr"])
    assert run["config"]["publish_request_ttl_hours"] == 24 and run["config"]["bank_payment_type"] == "RTGS"


async def test_bank_file_second_person_switch_is_guarded_and_alerts_admins(client, payroll):
    key = "payroll.bank_file_requires_second_person"
    hr, admin = payroll["hr"], payroll["admin"]
    r = await client.put(SETTINGS, json={"settings": {key: False}, "current_password": PASSWORD}, headers=hr)
    assert r.status_code == 403 and err(r) == "FORBIDDEN"  # role.assign is needed
    r = await client.put(SETTINGS, json={"settings": {key: False}}, headers=admin)
    assert r.status_code == 422  # and the caller's own password
    r = await client.put(SETTINGS, json={"settings": {key: False}, "current_password": "Wrong!Passw0rd#1"}, headers=admin)
    assert r.status_code == 403 and err(r) == "REAUTH_FAILED"
    assert (await client.get(SETTINGS, headers=hr)).json()["settings"][key] is True
    notifications.OUTBOX.clear()
    r = await client.put(SETTINGS, json={"settings": {key: False}, "current_password": PASSWORD}, headers=admin)
    assert r.status_code == 200 and r.json()["settings"][key] is False
    alerts = [o["payload"] for o in notifications.OUTBOX if o["payload"]["type"] == "admin_alert"]
    assert len(alerts) == 1 and "bank file approval rule" in alerts[0]["body"] and "False" not in json.dumps(alerts[0])
    r = await client.put(SETTINGS, json={"settings": {key: False}}, headers=hr)
    assert r.status_code == 200  # unchanged value: no extra step
    r = await client.put(
        SETTINGS, json={"settings": {key: True}, "current_password": PASSWORD}, headers=admin
    )  # back on (both directions)
    assert r.status_code == 200 and r.json()["settings"][key] is True
