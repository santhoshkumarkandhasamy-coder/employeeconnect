"""Late details (plan 04). Rows are written by attendance check-in; this module only reads them."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import iso_z, utcnow
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import LateRecord, User
from . import attendance as att
from . import files as files_svc


def late_out(r: LateRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "work_date": r.work_date.isoformat(),
        "shift_start": r.shift_start.strftime("%H:%M"),
        "grace_minutes": r.grace_minutes,
        "check_in_at": iso_z(r.check_in_at),
        "minutes_late": r.minutes_late,
    }


def _range(cond: list, d_from: dt.date | None, d_to: dt.date | None) -> None:
    att.check_range(d_from, d_to)
    if d_from:
        cond.append(LateRecord.work_date >= d_from)
    if d_to:
        cond.append(LateRecord.work_date <= d_to)


async def my_late(session: AsyncSession, actor: Principal, params: PageParams, d_from: dt.date | None, d_to: dt.date | None):
    cond: list = [LateRecord.user_id == actor.id]
    _range(cond, d_from, d_to)
    total = (await session.execute(select(func.count()).select_from(LateRecord).where(*cond))).scalar_one()
    q = select(LateRecord).where(*cond).order_by(LateRecord.work_date.desc()).limit(params.page_size).offset(params.offset)
    return make_page([late_out(r) for r in (await session.execute(q)).scalars()], params, total)


async def my_summary(session: AsyncSession, actor: Principal, month: str | None) -> dict[str, Any]:
    cfg = await att.load_settings(session)
    today_d = utcnow().astimezone(att.office_tz(cfg)).date()
    first, last = att.parse_month(month, today_d)
    q = (
        select(LateRecord)
        .where(LateRecord.user_id == actor.id, LateRecord.work_date >= first, LateRecord.work_date <= last)
        .order_by(LateRecord.work_date)
    )
    rows = list((await session.execute(q)).scalars())
    total = sum(r.minutes_late for r in rows)
    return {
        "month": f"{first.year:04d}-{first.month:02d}",
        "late_count": len(rows),
        "total_minutes_late": total,
        "average_minutes_late": round(total / len(rows), 1) if rows else 0,
        "days": [{"date": r.work_date.isoformat(), "minutes_late": r.minutes_late} for r in rows],
    }


async def team_late(
    session: AsyncSession,
    actor: Principal,
    params: PageParams,
    *,
    d_from: dt.date | None,
    d_to: dt.date | None,
    department_id: uuid.UUID | None,
    employee_id: str | None,
):
    cond = att.scope_conditions(actor, "late.view_all", "late.view_team", department_id, employee_id)
    cfg = await att.load_settings(session)
    d_from, d_to = att.cap_range(d_from, d_to, utcnow().astimezone(att.office_tz(cfg)).date())
    _range(cond, d_from, d_to)
    base = select(LateRecord, User).join(User, User.id == LateRecord.user_id).where(*cond)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    q = base.order_by(LateRecord.work_date.desc(), User.employee_id).limit(params.page_size).offset(params.offset)
    rows = (await session.execute(q)).unique().all()
    photos = await files_svc.photo_file_ids(session, [u.id for _, u in rows])
    return make_page([{**late_out(r), "user": att.user_summary(u, photos.get(u.id))} for r, u in rows], params, total)
