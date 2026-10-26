"""QA (black-box via API + the public job entry points): 00:30 auto-close and retention for visits (contract 14.4, 14.9)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import Attendance, AuditLog
from app.services import attendance as att
from tests.qa_on_duty_support import assign, check_in, check_out, clock, ist, new_loc, office_in, set_settings  # noqa: F401

OD = "/api/on-duty"


async def _site(client, H, uid):
    loc = await new_loc(client, H["admin"], "Acme Client")
    await assign(client, H["admin"], loc["id"], [uid["employee"]])
    return loc


async def test_auto_close_flags_past_open_visit_and_keeps_closed_ones(client, H, uid, clock, db):
    loc = await _site(client, H, uid)
    await check_in(client, H["employee"], loc["id"], clock)  # open, never closed
    await assign(client, H["admin"], loc["id"], [uid["employee"], uid["manager"]])
    await check_in(client, H["manager"], loc["id"], clock)
    clock.advance(minutes=30)
    await check_out(client, H["manager"], clock)  # closed properly
    now = ist(2026, 10, 22, 0, 30)
    n = await att.auto_close_open_records(db, now)
    visits = (await client.get(f"{OD}/visits", headers=H["employee"])).json()["items"]
    assert len(visits) == 1 and visits[0]["auto_closed"] is True and visits[0]["open"] is False
    assert visits[0]["check_out_at"] is None and visits[0]["visit_minutes"] is None
    mv = (await client.get(f"{OD}/visits", headers=H["manager"])).json()["items"][0]
    assert mv["auto_closed"] is False and mv["check_out_at"] is not None
    # attendance: closed-by-visit row is not flagged; the open one is
    rows = {r.user_id: r for r in (await db.execute(select(Attendance))).scalars()}
    assert rows[__import__("uuid").UUID(uid["manager"])].auto_closed is False
    assert rows[__import__("uuid").UUID(uid["employee"])].auto_closed is True
    assert n == 1
    audits = (await db.execute(select(AuditLog).where(AuditLog.action == "on_duty.auto_close"))).scalars().all()
    assert len(audits) == 1 and (audits[0].after or {}).get("count") == 1
    # idempotent
    assert await att.auto_close_open_records(db, now) == 0
    # auto-closed visit is not open: check-out says NO_OPEN_VISIT, and a new check-in is allowed next day
    clock.set(ist(2026, 10, 22, 10, 0))
    r = await check_out(client, H["employee"], clock)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_OPEN_VISIT"
    t = (await client.get(f"{OD}/today", headers=H["employee"])).json()
    assert t["open_visit"] is None and t["can_check_in"] is True
    assert (await check_in(client, H["employee"], loc["id"], clock)).status_code == 201


async def test_auto_close_does_not_touch_todays_visit(client, H, uid, clock, db):
    loc = await _site(client, H, uid)
    await check_in(client, H["employee"], loc["id"], clock)
    await att.auto_close_open_records(db, ist(2026, 10, 21, 23, 59))
    t = (await client.get(f"{OD}/today", headers=H["employee"])).json()
    assert t["open_visit"] is not None and t["open_visit"]["auto_closed"] is False


async def test_auto_closed_day_cannot_be_ended_via_office_without_visit_open_error(client, H, uid, clock, db):
    """After the 00:30 job an auto-closed visit must not block the office End day forever (VISIT_OPEN is for OPEN visits)."""
    loc = await _site(client, H, uid)
    clock.set(ist(2026, 10, 21, 9, 0))
    await office_in(client, H["employee"], clock)
    clock.set(ist(2026, 10, 21, 10, 0))
    await check_in(client, H["employee"], loc["id"], clock)
    await att.auto_close_open_records(db, ist(2026, 10, 22, 0, 30))
    t = (await client.get(f"{OD}/today", headers=H["employee"])).json()
    assert t["open_visit"] is None
