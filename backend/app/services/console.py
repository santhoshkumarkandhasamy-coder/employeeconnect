"""Admin console data (contract s.12.5): dashboard counters and the per-day attendance view. No coordinates, distances or
selfies are exposed here. Each dashboard block is None unless the caller holds the permission of that block."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import iso_z, utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    Announcement,
    Attendance,
    FoodEnrollment,
    LeaveRequest,
    LeaveType,
    PasswordResetRequest,
    PermissionRequest,
    RefreshToken,
    User,
)
from . import attendance as att
from . import license as license_svc

DAY_STATUSES = ("all", "checked_in", "late", "not_checked_in", "on_leave")
MAX_PAST_DAYS = 366


async def office_today(session: AsyncSession) -> dt.date:
    cfg = await att.load_settings(session)
    return utcnow().astimezone(att.office_tz(cfg)).date()


def _active() -> list:
    return [User.status == "active", User.deleted_at.is_(None)]


# ---------------------------------------------------------------- per-day statuses
async def day_rows(session: AsyncSession, day: dt.date, conds: list) -> list[dict[str, Any]]:
    """One row per ACTIVE employee in scope: status = the attendance row's status, else holiday > weekly_off > on_leave >
    not_checked_in (same precedence as the employee calendar)."""
    users = (await session.execute(select(User).where(*_active(), *conds).order_by(User.employee_id))).unique().scalars().all()
    if not users:
        return []
    ids = [u.id for u in users]
    recs: dict[uuid.UUID, Attendance] = {}
    for chunk in range(0, len(ids), 500):
        part = ids[chunk : chunk + 500]
        recs.update(
            {
                a.user_id: a
                for a in (
                    await session.execute(select(Attendance).where(Attendance.work_date == day, Attendance.user_id.in_(part)))
                ).scalars()
            }
        )
    leaves: dict[uuid.UUID, str] = {}
    for chunk in range(0, len(ids), 500):
        part = ids[chunk : chunk + 500]
        q = (
            select(LeaveRequest.user_id, LeaveType.code)
            .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
            .where(
                LeaveRequest.user_id.in_(part),
                LeaveRequest.status == "approved",
                LeaveRequest.from_date <= day,
                LeaveRequest.to_date >= day,
            )
        )
        leaves.update({uid: code for uid, code in (await session.execute(q)).all()})
    is_holiday = day in await att.holiday_map(session, day, day)
    out = []
    for u in users:
        a = recs.get(u.id)
        if a is not None:
            status = a.status
        elif is_holiday:
            status = "holiday"
        elif att.is_weekly_off(u.shift, day):
            status = "weekly_off"
        elif u.id in leaves:
            status = "on_leave"
        else:
            status = "not_checked_in"
        out.append(
            {
                "_user": u,
                "status": status,
                "check_in_at": iso_z(a.check_in_at) if a else None,
                "check_out_at": iso_z(a.check_out_at) if a and a.check_out_at else None,
                "minutes_late": a.minutes_late if a else 0,
                "leave_type": leaves.get(u.id) if status == "on_leave" else None,
            }
        )
    return out


def _matches(status_filter: str, status: str) -> bool:
    return {
        "all": True,
        "checked_in": status in ("present", "late", "half_day"),
        "late": status == "late",
        "not_checked_in": status == "not_checked_in",
        "on_leave": status == "on_leave",
    }[status_filter]


async def attendance_day(
    session: AsyncSession,
    actor: Principal,
    params: PageParams,
    *,
    day: dt.date | None,
    status: str,
    department_id: uuid.UUID | None,
    q: str | None,
) -> dict[str, Any]:
    today = await office_today(session)
    day = day or today
    if not today - dt.timedelta(days=MAX_PAST_DAYS) <= day <= today:
        raise AppError(
            "INVALID_DATE",
            f"date must be within the last {MAX_PAST_DAYS} days and not in the future",
            details=[{"field": "date", "message": "out of range"}],
        )
    conds = att.scope_conditions(actor, "attendance.view_all", "attendance.view_team", department_id, None)
    if q:
        esc = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        conds.append(or_(User.employee_id.ilike(like, escape="\\"), User.full_name.ilike(like, escape="\\")))
    rows = [r for r in await day_rows(session, day, conds) if _matches(status, r["status"])]
    page = rows[params.offset : params.offset + params.page_size]
    items = [{"user": att.user_summary(r.pop("_user")), **r} for r in page]
    return make_page(items, params, len(rows))


# ---------------------------------------------------------------- dashboard
async def dashboard(session: AsyncSession, actor: Principal) -> dict[str, Any]:
    today = await office_today(session)
    now = utcnow()
    out: dict[str, Any] = {"date": today.isoformat()}

    if actor.has("employee.view") and (actor.has("employee.update") or actor.has("employee.create")):
        act, inact = (
            await session.execute(
                select(
                    func.coalesce(func.sum(case((User.status == "active", 1), else_=0)), 0),
                    func.coalesce(func.sum(case((User.status == "active", 0), else_=1)), 0),
                ).where(User.deleted_at.is_(None))
            )
        ).one()
        out["employees"] = {"active": int(act), "inactive": int(inact)}
    else:
        out["employees"] = None

    if actor.has("attendance.view_all"):
        rows = await day_rows(session, today, [])
        n = lambda *sts: sum(1 for r in rows if r["status"] in sts)  # noqa: E731
        out["attendance"] = {
            "checked_in": n("present", "late", "half_day"),
            "checked_out": sum(1 for r in rows if r["check_out_at"]),
            "late": n("late"),
            "on_leave": n("on_leave"),
            "not_checked_in": n("not_checked_in"),
        }
    else:
        out["attendance"] = None

    if actor.has("request.view_all"):
        lp = (
            await session.execute(select(func.count()).select_from(LeaveRequest).where(LeaveRequest.status == "pending"))
        ).scalar_one()
        pp = (
            await session.execute(
                select(func.count()).select_from(PermissionRequest).where(PermissionRequest.status == "pending")
            )
        ).scalar_one()
        out["requests"] = {"leave_pending": lp, "permission_pending": pp}
    else:
        out["requests"] = None

    if actor.has("food.view_all"):
        tomorrow = today + dt.timedelta(days=1)
        total = (await session.execute(select(func.count()).select_from(User).where(*_active()))).scalar_one()

        async def answers(day: dt.date) -> tuple[int, int]:
            yes, no = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(case((FoodEnrollment.need_food.is_(True), 1), else_=0)), 0),
                        func.coalesce(func.sum(case((FoodEnrollment.need_food.is_(False), 1), else_=0)), 0),
                    )
                    .select_from(FoodEnrollment)
                    .join(User, and_(User.id == FoodEnrollment.user_id, *_active()))
                    .where(FoodEnrollment.date == day)
                )
            ).one()
            return int(yes), int(no)

        yes, no = await answers(today)
        t_yes, _ = await answers(tomorrow)
        out["food"] = {
            "date": today.isoformat(),
            "count_yes": yes,
            "count_no": no,
            "count_unanswered": max(0, total - yes - no),
            "tomorrow_yes": t_yes,
        }
    else:
        out["food"] = None

    if actor.has("announcement.manage"):
        n_ann = (
            await session.execute(
                select(func.count())
                .select_from(Announcement)
                .where(
                    Announcement.deleted_at.is_(None),
                    Announcement.published_at <= now,
                    or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
                )
            )
        ).scalar_one()
        out["announcements"] = {"active": n_ann}
    else:
        out["announcements"] = None

    if actor.has("session.manage"):
        rows_s = (
            await session.execute(
                select(RefreshToken.client_type, func.count(func.distinct(RefreshToken.family_id)))
                .where(RefreshToken.revoked_at.is_(None), RefreshToken.replaced_by.is_(None), RefreshToken.expires_at > now)
                .group_by(RefreshToken.client_type)
            )
        ).all()
        counts = {k: v for k, v in rows_s}
        out["sessions"] = {"mobile": counts.get("mobile", 0), "web": counts.get("web", 0)}
    else:
        out["sessions"] = None

    if actor.has("employee.reset_password"):
        out["password_reset_requests_open"] = (
            await session.execute(
                select(func.count()).select_from(PasswordResetRequest).where(PasswordResetRequest.status == "open")
            )
        ).scalar_one()
    else:
        out["password_reset_requests_open"] = None

    state = await license_svc.load_state(session)
    out["seats"] = {"used": await license_svc.active_users(session), "total": state.max_users, "status": state.status}
    return out
