"""QA support for On Duty black-box tests (written from docs/plans/13-on-duty-contract.md, API only).

2026-10-21 is a Wednesday. Office tz Asia/Kolkata (UTC+5:30). SITE is far from the default Bengaluru office."""

from __future__ import annotations

import datetime as dt

import pytest

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SITE = (13.0, 80.0)
NEAR = (13.0 + 0.0009, 80.0)  # ~100 m north of SITE
FAR = (13.0 + 0.0030, 80.0)  # ~333 m north of SITE
OFFICE = (12.9716, 77.5946)


def ist(y, mo, d, h, mi, s=0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(dt.UTC)


def z(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, monkeypatch, when):
        self.mp = monkeypatch
        self.set(when)

    def set(self, when):
        self.now = when
        self.mp.setattr("app.services.attendance.utcnow", lambda: when)
        self.mp.setattr("app.services.late.utcnow", lambda: when)

    def advance(self, **kw):
        self.set(self.now + dt.timedelta(**kw))


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch, ist(2026, 10, 21, 11, 0))


def code(r) -> str:
    return r.json()["error"]["code"]


async def new_loc(client, h, name="Acme Client", *, lat=SITE[0], lng=SITE[1], radius=200, mode="selected", active=True, address=None):
    r = await client.post(
        "/api/admin/duty-locations",
        headers=h,
        json={"name": name, "address": address, "lat": lat, "lng": lng, "radius_m": radius, "assign_mode": mode, "is_active": active},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def assign(client, h, loc_id, user_ids, mode="selected", **extra):
    r = await client.put(
        f"/api/admin/duty-locations/{loc_id}/assignments",
        headers=h,
        json={"assign_mode": mode, "user_ids": [str(u) for u in user_ids], **extra},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def set_settings(client, h, **kv):
    r = await client.put("/api/admin/settings", headers=h, json={"settings": {f"on_duty.{k}" if "." not in k else k: v for k, v in kv.items()}})
    assert r.status_code == 200, r.text


def pt(p, acc=10.0, clock=None, **extra):
    body = {"lat": p[0], "lng": p[1], "accuracy_m": acc, "fix_time": z(clock.now) if clock else z(dt.datetime.now(dt.UTC))}
    body.update(extra)
    return body


async def check_in(client, h, loc_id, clock, p=NEAR, **extra):
    return await client.post("/api/on-duty/check-in", headers=h, json={"location_id": str(loc_id), **pt(p, clock=clock, **extra)})


async def check_out(client, h, clock, p=NEAR, **extra):
    return await client.post("/api/on-duty/check-out", headers=h, json=pt(p, clock=clock, **extra))


async def office_in(client, h, clock, **extra):
    return await client.post("/api/attendance/check-in", headers=h, json=pt(OFFICE, clock=clock, **extra))


async def office_out(client, h, clock, **extra):
    return await client.post("/api/attendance/check-out", headers=h, json=pt(OFFICE, clock=clock, **extra))


async def seed_leave(db, employee_id: str, d_from, d_to, *, half=False, status="approved"):
    """Insert a leave row directly (the leave API needs balances/types; the leave module is not under test here)."""
    from sqlalchemy import select

    from app.models import LeaveRequest, LeaveType, User

    uid_ = (await db.execute(select(User.id).where(User.employee_id == employee_id))).scalar_one()
    lt = (await db.execute(select(LeaveType).where(LeaveType.code == "CL"))).scalar_one_or_none()
    if lt is None:
        lt = LeaveType(code="CL", name="Casual Leave", annual_quota=12, allow_half_day=True)
        db.add(lt)
        await db.flush()
    lv = LeaveRequest(
        user_id=uid_, leave_type_id=lt.id, from_date=d_from, to_date=d_to, half_day=half,
        half_day_session="first" if half else None, days=0.5 if half else (d_to - d_from).days + 1, reason="r", status=status,
    )  # fmt: skip
    db.add(lv)
    await db.commit()
    return lv
