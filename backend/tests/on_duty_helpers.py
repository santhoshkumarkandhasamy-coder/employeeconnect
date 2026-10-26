"""Shared helpers for the On Duty tests (plan 13). 2026-10-21 is a Wednesday; office timezone Asia/Kolkata = UTC+5:30.

Coordinates: SITE is a client site. NEAR is ~100 m north of it, FAR ~333 m north (office is Bengaluru, far away from SITE,
so an on-duty punch can never be confused with an office one)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select

from app.models import DutyLocation, DutyLocationAssignment, File, LeaveRequest, LeaveType, Setting, User
from app.services import attendance as att_svc
from tests.conftest import USERS

SITE = {"lat": 13.0, "lng": 80.0}
NEAR = {"lat": 13.0 + 0.0009, "lng": 80.0, "accuracy_m": 10}  # ~100.07 m north
FAR = {"lat": 13.0 + 0.0030, "lng": 80.0, "accuracy_m": 10}  # ~333 m north
WED = dt.date(2026, 10, 21)
SUN = dt.date(2026, 10, 25)


def ist(y, mo, d, h, mi, s=0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).astimezone(dt.UTC)


def fix() -> dict:
    return {"fix_time": att_svc.utcnow().isoformat()}


async def user_id(db, role: str) -> uuid.UUID:
    return (await db.execute(select(User.id).where(User.employee_id == USERS[role]))).scalar_one()


async def make_loc(db, name="Acme Client", *, radius=200, mode="selected", active=True, assign=("employee",), lat=None, lng=None):
    loc = DutyLocation(
        name=name, address="12 Mount Rd", lat=SITE["lat"] if lat is None else lat, lng=SITE["lng"] if lng is None else lng,
        radius_m=radius, assign_mode=mode, is_active=active,
    )  # fmt: skip
    db.add(loc)
    await db.flush()
    for role in assign:
        db.add(DutyLocationAssignment(location_id=loc.id, user_id=await user_id(db, role)))
    await db.commit()
    return loc


async def set_setting(db, key, value):
    row = await db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    await db.commit()


async def make_file(db, role="employee", purpose="selfie", age_s=30) -> uuid.UUID:
    f = File(
        owner_id=await user_id(db, role), purpose=purpose, storage_path="x", content_type="image/jpeg", size_bytes=1,
        created_at=att_svc.utcnow() - dt.timedelta(seconds=age_s),
    )  # fmt: skip
    db.add(f)
    await db.commit()
    return f.id


async def make_leave(db, role, d_from, d_to, *, half=False, status="approved"):
    lt = (await db.execute(select(LeaveType).where(LeaveType.code == "CL"))).scalar_one_or_none()
    if lt is None:
        lt = LeaveType(code="CL", name="Casual Leave", annual_quota=12, allow_half_day=True)
        db.add(lt)
        await db.flush()
    lv = LeaveRequest(
        user_id=await user_id(db, role), leave_type_id=lt.id, from_date=d_from, to_date=d_to, half_day=half,
        half_day_session="first" if half else None, days=0.5 if half else 1, reason="r", status=status,
    )  # fmt: skip
    db.add(lv)
    await db.commit()
    return lv


def body_in(loc, extra=None, **over):
    return {"location_id": str(loc.id), **NEAR, **fix(), **(extra or {}), **over}


def body_out(extra=None, **over):
    return {**NEAR, **fix(), **(extra or {}), **over}


async def visit_in(client, h, loc, extra=None, **over):
    return await client.post("/api/on-duty/check-in", json=body_in(loc, extra, **over), headers=h)


async def visit_out(client, h, extra=None, **over):
    return await client.post("/api/on-duty/check-out", json=body_out(extra, **over), headers=h)


async def office_in(client, h, extra=None):
    """Office check-in at the (default Bengaluru) office coordinates."""
    return await client.post(
        "/api/attendance/check-in",
        json={"lat": 12.9716, "lng": 77.5946, "accuracy_m": 10, **fix(), **(extra or {})},
        headers=h,
    )


async def office_out(client, h, extra=None):
    return await client.post(
        "/api/attendance/check-out", json={"lat": 12.9716, "lng": 77.5946, "accuracy_m": 10, **fix(), **(extra or {})}, headers=h
    )
