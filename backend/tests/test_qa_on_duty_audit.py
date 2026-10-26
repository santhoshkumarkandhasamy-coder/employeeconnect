"""QA (black-box + audit table reads): audit coverage and no coordinate/note leakage (contract 14.3, 14.5, 14.9)."""

from __future__ import annotations

import json

from sqlalchemy import select

from app.models import AuditLog
from tests.qa_on_duty_support import FAR, NEAR, assign, check_in, check_out, clock, new_loc, set_settings  # noqa: F401


async def audits(db, action=None):
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action == action)
    return list((await db.execute(q)).scalars())


async def test_location_and_assignment_audit(client, H, uid, db):
    loc = await new_loc(client, H["admin"], "Audited Site")
    await assign(client, H["admin"], loc["id"], [uid["employee"], uid["manager"]])
    await assign(client, H["admin"], loc["id"], [uid["employee"]])
    await client.put(
        f"/api/admin/duty-locations/{loc['id']}",
        headers=H["admin"],
        json={"name": "Audited Site 2", "lat": 13.0, "lng": 80.0, "radius_m": 300, "assign_mode": "selected", "is_active": True},
    )
    await client.delete(f"/api/admin/duty-locations/{loc['id']}", headers=H["admin"])
    for a in ("duty_location.create", "duty_location.update", "duty_location.delete", "duty_location.assignments"):
        assert await audits(db, a), a
    last = (await audits(db, "duty_location.assignments"))[-1]
    after = last.after or {}
    assert uid["manager"] in json.dumps(after.get("removed")) and "Test " not in json.dumps(after)  # ids, not names


async def test_rejected_punch_is_audited_without_coordinates(client, H, uid, clock, db):
    loc = await new_loc(client, H["admin"], "Audited Site", radius=150)
    await assign(client, H["admin"], loc["id"], [uid["employee"]])
    r = await check_in(client, H["employee"], loc["id"], clock, p=FAR)
    assert r.status_code == 422
    rows = await audits(db, "attendance.punch_rejected")
    assert rows
    blob = json.dumps([(x.after, x.before) for x in rows], default=str)
    assert "on_duty_check_in" in blob
    assert "13.0030" not in blob and "lat" not in blob.lower().replace("late", "")


async def test_notes_and_coordinates_never_in_audit(client, H, uid, clock, db):
    loc = await new_loc(client, H["admin"], "Audited Site")
    await assign(client, H["admin"], loc["id"], [uid["employee"]])
    await check_in(client, H["employee"], loc["id"], clock, note="TOP-SECRET-NOTE")
    clock.advance(minutes=5)
    await check_out(client, H["employee"], clock, note="ANOTHER-SECRET")
    await client.post(
        "/api/on-duty/requests", headers=H["employee"],
        json={"from_date": "2026-10-28", "to_date": "2026-10-28", "location_id": None, "purpose": "PURPOSE-TEXT-SECRET"},
    )  # fmt: skip
    rows = await audits(db)
    blob = json.dumps([(x.action, x.before, x.after) for x in rows if not x.action.startswith("duty_location")], default=str)
    assert "TOP-SECRET-NOTE" not in blob and "ANOTHER-SECRET" not in blob
    assert "PURPOSE-TEXT-SECRET" not in blob
    assert "13.0009" not in blob
    assert await audits(db, "on_duty.apply")


async def test_suspicious_punch_audited(client, H, uid, clock, db):
    await set_settings(client, H["admin"], **{"attendance.reject_mock_location": False})
    loc = await new_loc(client, H["admin"], "Audited Site")
    await assign(client, H["admin"], loc["id"], [uid["employee"]])
    assert (await check_in(client, H["employee"], loc["id"], clock, is_mock_location=True)).status_code == 201
    assert await audits(db, "on_duty.suspicious_punch")


async def test_cancel_and_decision_audit(client, H, uid, clock, db):
    r = await client.post(
        "/api/on-duty/requests", headers=H["employee"],
        json={"from_date": "2026-10-28", "to_date": "2026-10-28", "location_id": None, "purpose": "x"},
    )  # fmt: skip
    rid = r.json()["id"]
    await client.put(f"/api/admin/requests/on_duty/{rid}/approve", headers=H["manager"], json={})
    await client.post(f"/api/on-duty/requests/{rid}/cancel", headers=H["employee"])
    acts = {a.action for a in await audits(db)}
    assert "request.approve" in acts and "on_duty.apply" in acts
