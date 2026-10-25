"""Pay days and loss of pay (plan 12 s.4.3). Office timezone, per calendar day.

`load_context` reads holidays, attendance and leave ONCE for a whole run; `compute_days` is then pure per employee
(so a 50 employee run costs three queries, not 150).
"""

from __future__ import annotations

import calendar
import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import as_utc
from ..models import Attendance, LeaveRequest, LeaveType, User
from . import attendance as att

ONE = Decimal("1.0")
HALF = Decimal("0.5")
ZERO = Decimal("0.0")


def month_bounds(period: dt.date) -> tuple[dt.date, dt.date]:
    return period, period.replace(day=calendar.monthrange(period.year, period.month)[1])


def _daterange(a: dt.date, b: dt.date):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


@dataclass(frozen=True)
class LeaveRow:
    status: str  # approved | pending
    is_paid: bool
    from_date: dt.date
    to_date: dt.date
    half_day: bool


@dataclass
class DayContext:
    tz: ZoneInfo
    period_start: dt.date
    period_end: dt.date
    holidays: set[dt.date] = field(default_factory=set)
    attendance: dict[uuid.UUID, dict[dt.date, str]] = field(default_factory=dict)
    leaves: dict[uuid.UUID, list[LeaveRow]] = field(default_factory=dict)


@dataclass
class DaysResult:
    basis_days: Decimal
    employed_days: Decimal
    lop_auto: Decimal
    breakdown: dict[str, float]
    warnings: list[str]


async def load_context(session: AsyncSession, period: dt.date, user_ids: list[uuid.UUID]) -> DayContext:
    start, end = month_bounds(period)
    cfg = await att.load_settings(session)
    ctx = DayContext(tz=att.office_tz(cfg), period_start=start, period_end=end)
    if not user_ids:
        return ctx
    ctx.holidays = set(await att.holiday_map(session, start, end))
    q = select(Attendance.user_id, Attendance.work_date, Attendance.status).where(
        Attendance.user_id.in_(user_ids), Attendance.work_date >= start, Attendance.work_date <= end
    )
    for uid, day, status in (await session.execute(q)).all():
        ctx.attendance.setdefault(uid, {})[day] = status
    q2 = (
        select(LeaveRequest, LeaveType.is_paid)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
        .where(
            LeaveRequest.user_id.in_(user_ids),
            LeaveRequest.status.in_(("approved", "pending")),
            LeaveRequest.from_date <= end,
            LeaveRequest.to_date >= start,
        )
    )
    for lr, paid in (await session.execute(q2)).all():
        ctx.leaves.setdefault(lr.user_id, []).append(LeaveRow(lr.status, bool(paid), lr.from_date, lr.to_date, bool(lr.half_day)))
    return ctx


def employment_window(user: User, period_start: dt.date, period_end: dt.date, tz: ZoneInfo) -> tuple[dt.date, dt.date] | None:
    """[max(period start, join date), min(period end, deactivation date - 1)] or None when the user was not employed."""
    start = max(period_start, user.join_date) if user.join_date else period_start
    end = period_end
    gone = user.deactivated_at or user.deleted_at
    if gone is not None:
        end = min(end, as_utc(gone).astimezone(tz).date() - dt.timedelta(days=1))
    return (start, end) if start <= end else None


def is_working_day(user: User, day: dt.date, holidays: set[dt.date]) -> bool:
    return not att.is_weekly_off(user.shift, day) and day not in holidays


def compute_days(user: User, ctx: DayContext, cutoff: dt.date, cfg: dict[str, Any]) -> DaysResult | None:
    """`cfg` is the run snapshot (keys without the `payroll.` prefix). None = the user was not employed in the period."""
    window = employment_window(user, ctx.period_start, ctx.period_end, ctx.tz)
    if window is None:
        return None
    w_start, w_end = window
    month_days = list(_daterange(ctx.period_start, ctx.period_end))
    in_window = [d for d in month_days if w_start <= d <= w_end]
    working = lambda d: is_working_day(user, d, ctx.holidays)  # noqa: E731
    basis_mode = cfg.get("pay_days_basis", "calendar")

    if basis_mode == "fixed_30":
        basis = Decimal(30)
        out_of_window = [d for d in month_days if d.day <= 30 and not (w_start <= d <= w_end)]
    elif basis_mode == "working_days":
        basis = Decimal(sum(1 for d in month_days if working(d)))
        out_of_window = [d for d in month_days if working(d) and not (w_start <= d <= w_end)]
    else:
        basis = Decimal(len(month_days))
        out_of_window = [d for d in month_days if not (w_start <= d <= w_end)]
    not_employed = Decimal(len(out_of_window))
    employed = max(ZERO, basis - not_employed)

    warnings: list[str] = []
    leaves = ctx.leaves.get(user.id, [])
    approved = [lv for lv in leaves if lv.status == "approved"]
    pending = [lv for lv in leaves if lv.status == "pending"]
    att_days = ctx.attendance.get(user.id, {})
    working_in_window = [d for d in in_window if working(d)]

    def covering(rows: list[LeaveRow], d: dt.date) -> list[LeaveRow]:
        return [lv for lv in rows if lv.from_date <= d <= lv.to_date]

    unpaid = ZERO
    for d in working_in_window:
        for lv in covering(approved, d):
            if not lv.is_paid:
                unpaid += HALF if lv.half_day else ONE

    absent = ZERO
    pending_days = ZERO
    if user.shift is None:
        warnings.append("NO_SHIFT")
    elif cfg.get("lop_absent_days", True):
        for d in working_in_window:
            if d > cutoff or d in att_days:
                continue
            cover = covering(approved, d)
            if any(not lv.half_day for lv in cover):
                continue
            if cover:  # an approved half day leave and no attendance at all: the other half is absent
                absent += HALF
                continue
            absent += ONE
            p = covering(pending, d)
            if p:
                pending_days += HALF if all(lv.half_day for lv in p) else ONE

    half_att = ZERO
    if cfg.get("lop_half_day_attendance", True):
        for d in working_in_window:
            if att_days.get(d) == "half_day" and not any(lv.half_day for lv in covering(approved, d)):
                half_att += HALF

    if pending_days > 0:
        warnings.append("PENDING_LEAVE")
    lop_auto = min(unpaid + absent + half_att, employed)
    return DaysResult(
        basis_days=basis,
        employed_days=employed,
        lop_auto=lop_auto,
        breakdown={
            "unpaid_leave": float(unpaid),
            "absent": float(absent),
            "half_day_attendance": float(half_att),
            "not_employed": float(not_employed),
            "pending_leave_days": float(pending_days),
        },
        warnings=warnings,
    )
