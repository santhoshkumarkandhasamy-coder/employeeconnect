"""Overtime (plan 15 s.3 "Overtime", contract 17.8).

Off by default (`payroll.overtime_enabled`) and per employee (`employee_payroll_details.overtime_eligible`). `compute` derives one
`overtime_days` row per employee-day from the attendance punches; HR reviews every row (approve with the same or fewer minutes,
or reject) before it counts. Approved rows are payable source rows of the component engine (payable status `approved`): the
engine pulls the approved MINUTES of a month into one OVERTIME line and `payroll_runs.recompute` turns them into money from the
structure the payslip adopts, the employee's shift and the run settings (`payroll_calc.overtime_amount`), so the amount always
matches the payslip. Minutes are not money (plaintext columns); the comment is encrypted; audit rows carry counts only.

Lock order (P7): run row (target month) -> overtime rows (ORDER BY id), the last tables of the order.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import Attendance, EmployeePayrollDetails, LeaveRequest, OvertimeDay, User
from ..schemas.payroll import OvertimeBulk, OvertimeUpdate
from . import attendance as att
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_days as pdays
from . import payroll_items as items_svc
from . import payroll_runs as runs
from . import payroll_settings as pset

log = logging.getLogger("app.payroll")

ENTITY = "overtime_day"
OT = OvertimeDay
LIVE = ("approved", "processing", "paid")
EDITABLE = ("computed", "approved", "rejected")
MAX_DAY_MINUTES = 1440


# ============================================================================================ derivation (pure)
def candidate_minutes(
    worked: int, shift_minutes: int, *, off_day: bool, half_leave: bool, offday_full: bool, rounding: int, minimum: int
) -> int:
    """Overtime minutes of one day. Working day: worked - shift (half the shift on an approved half-day leave). Holiday / weekly
    off: all worked minutes when `offday_full`, else the same rule as a working day. Floored to `rounding` minutes; below
    `minimum` (or zero) there is no overtime."""
    if off_day and offday_full:
        base = worked
    else:
        base = worked - (shift_minutes // 2 if half_leave else shift_minutes)
    if base <= 0:
        return 0
    m = min(base, MAX_DAY_MINUTES) // max(1, rounding) * max(1, rounding)
    return m if m > 0 and m >= minimum else 0


async def _settings(session: AsyncSession) -> dict[str, Any]:
    return {
        k[len("payroll.") :]: await pset.current_value(session, k) for k in pset.DEFAULTS if k.startswith("payroll.overtime_")
    }


async def require_enabled(session: AsyncSession) -> dict[str, Any]:
    cfg = await _settings(session)
    if not cfg.get("overtime_enabled", False):
        raise AppError("OVERTIME_DISABLED", "Overtime is switched off")
    return cfg


# ============================================================================================ compute
async def compute(session: AsyncSession, actor: Principal, raw_period: str) -> dict[str, Any]:
    cfg = await require_enabled(session)
    today = await runs._today(session)
    period = comps.parse_target_period(raw_period, today)
    start, end = pdays.month_bounds(period)

    q = (
        select(User)
        .join(EmployeePayrollDetails, EmployeePayrollDetails.user_id == User.id)
        .where(
            EmployeePayrollDetails.overtime_eligible.is_(True),
            EmployeePayrollDetails.include_in_payroll.is_(True),
            User.deleted_at.is_(None),
        )
        .order_by(User.employee_id)
    )
    users = {u.id: u for u in (await session.execute(q)).unique().scalars() if u.shift is not None}
    created = updated = flagged = 0
    if users:
        uids = list(users)
        holidays = set(await att.holiday_map(session, start, end))
        att_rows = (
            (
                await session.execute(
                    select(Attendance).where(
                        Attendance.user_id.in_(uids),
                        Attendance.work_date >= start,
                        Attendance.work_date <= end,
                        Attendance.check_out_at.is_not(None),
                        Attendance.worked_minutes.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        half_days: dict[uuid.UUID, list[tuple[dt.date, dt.date]]] = {}
        lq = select(LeaveRequest).where(
            LeaveRequest.user_id.in_(uids),
            LeaveRequest.status == "approved",
            LeaveRequest.half_day.is_(True),
            LeaveRequest.from_date <= end,
            LeaveRequest.to_date >= start,
        )
        for lr in (await session.execute(lq)).scalars():
            half_days.setdefault(lr.user_id, []).append((lr.from_date, lr.to_date))
        await items_svc._lock_runs(session, [period])  # run row first (P7)
        existing = {
            (r.user_id, r.work_date): r
            for r in (
                await session.execute(
                    select(OT)
                    .where(OT.user_id.in_(uids), OT.period == period)
                    .order_by(OT.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }
        seen: set[tuple[uuid.UUID, dt.date]] = set()
        for a in sorted(att_rows, key=lambda x: (str(x.user_id), x.work_date)):
            user = users[a.user_id]
            if a.auto_closed and not cfg.get("overtime_include_auto_closed", False):
                continue
            shift_min = calc.shift_length_minutes(user.shift.start_time, user.shift.end_time)
            off = att.is_weekly_off(user.shift, a.work_date) or a.work_date in holidays
            half = any(f <= a.work_date <= t for f, t in half_days.get(a.user_id, []))
            mins = candidate_minutes(
                int(a.worked_minutes), shift_min, off_day=off, half_leave=half,
                offday_full=bool(cfg.get("overtime_offday_full", True)), rounding=int(cfg.get("overtime_rounding_minutes", 30)),
                minimum=int(cfg.get("overtime_min_minutes", 30)),
            )  # fmt: skip
            key = (a.user_id, a.work_date)
            seen.add(key)
            flags = sorted(f for f, on in (("AUTO_CLOSED", a.auto_closed), ("SUSPICIOUS", a.suspicious), ("OFF_DAY", off)) if on)
            row = existing.get(key)
            if row is None:
                if mins <= 0:
                    continue
                row = OT(
                    user_id=a.user_id, work_date=a.work_date, period=period, attendance_id=a.id, computed_minutes=mins,
                    flags=flags, status="computed", version=1, created_by=actor.id, updated_by=actor.id,
                )  # fmt: skip
                session.add(row)
                await pc.flush(session)
                comps.add_event(
                    session, item_type="overtime", item=row, action="create", actor_id=actor.id, detail={"minutes": mins}
                )
                created += 1
            elif row.status == "computed":
                if (row.computed_minutes, sorted(row.flags or []), row.attendance_id) != (mins, flags, a.id):
                    row.computed_minutes, row.flags, row.attendance_id = mins, flags, a.id
                    row.updated_by, row.version = actor.id, row.version + 1
                    comps.add_event(
                        session, item_type="overtime", item=row, action="update", actor_id=actor.id, detail={"minutes": mins}
                    )
                    updated += 1
            elif row.status in ("approved", "rejected") and mins != row.computed_minutes:
                if "ATTENDANCE_CHANGED" not in (row.flags or []):
                    row.flags = sorted({*(row.flags or []), "ATTENDANCE_CHANGED"})
                    row.updated_by, row.version = actor.id, row.version + 1
                    comps.add_event(session, item_type="overtime", item=row, action="update", actor_id=actor.id,
                                    detail={"attendance_changed": True, "minutes": mins})  # fmt: skip
                    flagged += 1
        for key, row in existing.items():  # a day that no longer qualifies (attendance removed or changed below the minimum)
            if key in seen or row.status != "computed" or row.computed_minutes == 0:
                continue
            row.computed_minutes, row.updated_by, row.version = 0, actor.id, row.version + 1
            comps.add_event(session, item_type="overtime", item=row, action="update", actor_id=actor.id, detail={"minutes": 0})
            updated += 1
    audit(session, action="payroll.overtime.compute", actor=actor, entity_type=ENTITY, entity_id=None,
          after={"period": pc.period_str(period), "created": created, "updated": updated, "flagged_changed": flagged,
                 "employees": len(users)})  # fmt: skip
    await pc.commit(session)
    return {"period": pc.period_str(period), "created": created, "updated": updated, "flagged_changed": flagged}


# ============================================================================================ reads
async def _out_many(session: AsyncSession, rows: list[OT]) -> list[dict[str, Any]]:
    people = await items_svc._people(session, {x for r in rows for x in (r.user_id, r.reviewer_id)})
    return [
        {
            "id": str(r.id),
            "user": pc.user_out(people.get(r.user_id)),
            "work_date": r.work_date.isoformat(),
            "computed_minutes": r.computed_minutes,
            "approved_minutes": r.approved_minutes,
            "flags": list(r.flags or []),
            "status": r.status,
            "reviewer": pc.user_out(people.get(r.reviewer_id)),
            "reviewed_at": pc.iso(r.reviewed_at),
            "comment": r.comment,
            "version": r.version,
        }
        for r in rows
    ]


async def list_days(
    session: AsyncSession,
    actor: Principal,
    *,
    period: str | None,
    user_id: uuid.UUID | None,
    status: str | None,
    page: PageParams,
) -> dict[str, Any]:
    await require_enabled(session)
    cond: list[Any] = []
    if period:
        cond.append(OT.period == comps.parse_target_period(period, await runs._today(session)))
    if user_id:
        cond.append(OT.user_id == user_id)
    if status:
        cond.append(OT.status == status)
    total = (await session.execute(select(func.count()).select_from(OT).where(*cond))).scalar_one()
    q = (
        select(OT).join(User, User.id == OT.user_id).where(*cond)
        .order_by(User.employee_id, OT.work_date, OT.id).offset(page.offset).limit(page.page_size)
    )  # fmt: skip
    out = make_page(await _out_many(session, list((await session.execute(q)).scalars())), page, total)
    summary: dict[uuid.UUID, dict[str, int]] = {}
    for uid, st, comp, appr in (
        await session.execute(select(OT.user_id, OT.status, OT.computed_minutes, OT.approved_minutes).where(*cond))
    ).all():
        s = summary.setdefault(uid, {"approved_minutes": 0, "computed_minutes": 0, "pending_rows": 0})
        s["computed_minutes"] += comp
        if st in LIVE:
            s["approved_minutes"] += appr or 0
        if st == "computed":
            s["pending_rows"] += 1
    out["summary"] = [{"user_id": str(u), **v} for u, v in sorted(summary.items(), key=lambda kv: str(kv[0]))]
    audit(session, action="payroll.overtime.list", actor=actor, entity_type=ENTITY, after={"count": len(out["items"])})
    await pc.commit(session)
    return out


async def my_overtime(session: AsyncSession, actor: Principal, raw_period: str | None) -> dict[str, Any]:
    cfg = await _settings(session)
    today = await runs._today(session)
    period = comps.parse_target_period(raw_period, today) if raw_period else today.replace(day=1)
    det = await session.get(EmployeePayrollDetails, actor.id)
    eligible = bool(det and det.overtime_eligible)
    base = {"period": pc.period_str(period)}
    if not cfg.get("overtime_enabled", False):
        return {"enabled": False, "items": [], **base}
    items: list[dict[str, Any]] = []
    if eligible:
        q = select(OT).where(OT.user_id == actor.id, OT.period == period).order_by(OT.work_date)
        for r in (await session.execute(q)).scalars():
            st = "approved" if r.status == "processing" else r.status
            items.append(
                {"work_date": r.work_date.isoformat(), "computed_minutes": r.computed_minutes,
                 "approved_minutes": r.approved_minutes, "status": st}
            )  # fmt: skip
    return {"enabled": True, "eligible": eligible, **base, "items": items}


# ============================================================================================ review
def _apply(row: OT, actor: Principal, status: str, minutes: int | None, comment: str | None) -> None:
    row.status = status
    row.approved_minutes = minutes if status == "approved" else None
    row.reviewer_id, row.reviewed_at, row.comment = actor.id, utcnow(), comment
    row.flags = [f for f in (row.flags or []) if f != "ATTENDANCE_CHANGED"]
    row.updated_by, row.version = actor.id, row.version + 1


async def update_day(session: AsyncSession, actor: Principal, day_id: uuid.UUID, body: OvertimeUpdate) -> dict[str, Any]:
    await require_enabled(session)
    peek = (await session.execute(select(OT).where(OT.id == day_id))).scalar_one_or_none()
    if peek is None:
        raise AppError("NOT_FOUND", "Overtime day not found")
    await pc.forbid_self(session, actor, peek.user_id, entity_type=ENTITY, entity_id=day_id)
    approve = body.status == "approved"
    if approve:  # a month that is published without a revision for the employee cannot take new pay (P9)
        await comps.check_target_period(session, [peek.user_id], peek.period, lock=True)
    row = (
        await session.execute(select(OT).where(OT.id == day_id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one()
    if row.status not in EDITABLE:
        raise AppError("ITEM_LOCKED", f"The overtime day is {row.status} and can no longer be changed")
    if row.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The overtime day was changed by someone else; reload it")
    minutes = body.approved_minutes if body.approved_minutes is not None else row.computed_minutes
    if approve and minutes > row.computed_minutes:
        raise AppError("VALIDATION_ERROR", "approved_minutes cannot exceed the computed minutes",
                       details=[{"field": "approved_minutes", "message": "above computed_minutes"}])  # fmt: skip
    _apply(row, actor, body.status, minutes if approve else None, body.comment)
    await pc.flush(session)
    comps.add_event(session, item_type="overtime", item=row, action="approve" if approve else "reject", actor_id=actor.id,
                    detail={"minutes": minutes if approve else None, "comment": body.comment})  # fmt: skip
    audit(session, action="payroll.overtime.review", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"user_id": str(row.user_id), "decision": body.status, "period": pc.period_str(row.period)})  # fmt: skip
    await pc.commit(session)
    fresh = (await session.execute(select(OT).where(OT.id == day_id).execution_options(populate_existing=True))).scalar_one()
    return (await _out_many(session, [fresh]))[0]


async def bulk(session: AsyncSession, actor: Principal, body: OvertimeBulk) -> dict[str, Any]:
    await require_enabled(session)
    ids = list(body.ids)
    peeked = {r.id: r for r in (await session.execute(select(OT).where(OT.id.in_(ids)))).scalars()}
    approve = body.action == "approve"
    skipped: list[dict[str, str]] = []
    todo: list[uuid.UUID] = []
    for i in ids:
        r = peeked.get(i)
        if r is None:
            skipped.append({"id": str(i), "reason": "NOT_FOUND"})
        elif r.user_id == actor.id:
            skipped.append({"id": str(i), "reason": "FORBIDDEN"})
        else:
            todo.append(i)
    periods = sorted({peeked[i].period for i in todo})
    await items_svc._lock_runs(session, periods)  # run rows first, ascending (P7)
    locked = (
        {
            r.id: r
            for r in (
                await session.execute(
                    select(OT).where(OT.id.in_(todo)).order_by(OT.id).with_for_update().execution_options(populate_existing=True)
                )
            ).scalars()
        }
        if todo
        else {}
    )
    closed: dict[tuple[uuid.UUID, dt.date], bool] = {}
    updated = 0
    for i in todo:
        r = locked[i]
        if r.status not in EDITABLE:
            skipped.append({"id": str(i), "reason": "ITEM_LOCKED"})
            continue
        if approve:
            k = (r.user_id, r.period)
            if k not in closed:
                try:
                    await comps.check_target_period(session, [r.user_id], r.period)
                    closed[k] = False
                except AppError:
                    closed[k] = True
            if closed[k]:
                skipped.append({"id": str(i), "reason": "PERIOD_LOCKED"})
                continue
        _apply(r, actor, "approved" if approve else "rejected", r.computed_minutes if approve else None, body.comment)
        await pc.flush(session)
        comps.add_event(session, item_type="overtime", item=r, action="approve" if approve else "reject", actor_id=actor.id,
                        detail={"minutes": r.approved_minutes, "comment": body.comment, "bulk": True})  # fmt: skip
        updated += 1
    audit(session, action="payroll.overtime.review", actor=actor, entity_type=ENTITY, entity_id=None,
          after={"decision": "approved" if approve else "rejected", "bulk": True, "updated": updated, "skipped": len(skipped)})  # fmt: skip
    await pc.commit(session)
    return {"updated": updated, "skipped": skipped}


# ============================================================================================ component engine provider
async def _prepare(session: AsyncSession, rows: list[OT]) -> None:
    """New (`approved`) rows are only pulled while overtime is on AND the employee is eligible; rows already `processing` or
    `paid` (a revision carries them) always stay in so a published payslip never loses its overtime line."""
    enabled = bool(await pset.current_value(session, "payroll.overtime_enabled"))
    uids = {r.user_id for r in rows}
    eligible = {
        d.user_id
        for d in (await session.execute(select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id.in_(uids)))).scalars()
        if d.overtime_eligible
    }
    for r in rows:
        r.pull_ok = r.status in ("processing", "paid") or (enabled and r.user_id in eligible)


def _lines(rows: list[OT]) -> list[dict]:
    rows = [r for r in rows if getattr(r, "pull_ok", True)]
    total = sum(int(r.approved_minutes or 0) for r in rows)
    if not rows or total <= 0:
        return []
    return [
        {
            "code": "OVERTIME",
            "label": "Overtime",
            "amount": "0.00",  # filled in by payroll_runs.recompute (structure, shift and run settings)
            "minutes": total,
            "minutes_total": total,
            "sources": [{"type": "overtime", "id": str(r.id)} for r in rows],
        }
    ]


async def _warn(session: AsyncSession, user_ids: list[uuid.UUID], period: dt.date) -> dict[uuid.UUID, list[str]]:
    if not user_ids or not await pset.current_value(session, "payroll.overtime_enabled"):
        return {}
    q = select(OT.user_id).where(OT.user_id.in_(user_ids), OT.period == period, OT.status == "computed", OT.computed_minutes > 0)
    return {u: ["OVERTIME_UNREVIEWED"] for u in set((await session.execute(q)).scalars())}


comps.register(
    comps.Provider(
        "overtime", OvertimeDay, _lines, LIVE, prepare=_prepare, warn=_warn, order=60, payable="approved", count_in_caps=False
    )
)
