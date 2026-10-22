"""Food enrollment (plan 05): a Yes/No (`need_food`) per user per date. No row = unanswered (counted as No).

Cut-off rule: a change is allowed while `now < cutoff_at` (inclusive-exclusive). `cutoff_at` is computed in the
office timezone with tz-aware wall-clock arithmetic (DST safe) and is always decided by the server clock.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import iso_z, utcnow
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import Department, FoodEnrollment, LeaveRequest, Setting, User
from . import attendance as att

log = logging.getLogger("app.food")

FOOD_DEFAULTS: dict[str, Any] = {
    "food.cutoff_mode": "previous_day",
    "food.cutoff_time": "18:00",
    "food.max_advance_days": 14,
}
CUTOFF_MODES = ("previous_day", "same_day")
MAX_ADVANCE_LIMIT = 60
MAX_BULK = 31
MAX_LIST_DAYS = 93
MY_WINDOW_DAYS = 400  # my-enrollments: accepted dates are today -/+ 400 days (L1: extreme dates -> clean 422)
DAILY_PAST_DAYS = 3650  # admin daily/report: up to ~10 years back, MY_WINDOW_DAYS ahead
DAILY_PAGE_SIZE = 500  # employees list cap per page (counts are always aggregates over everyone)
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ---------------------------------------------------------------- settings
@dataclass(frozen=True)
class FoodConfig:
    mode: str
    cutoff_time: dt.time
    max_advance_days: int
    tz: ZoneInfo


def parse_hhmm(value: Any) -> dt.time | None:
    m = _HHMM.match(value) if isinstance(value, str) else None
    return dt.time(int(m.group(1)), int(m.group(2))) if m else None


def validate_food_setting(key: str, value: Any) -> None:
    """Used by the settings-write endpoint: a bad value here would silently lock or open enrollment."""

    def bad(msg: str) -> AppError:
        return AppError("VALIDATION_ERROR", msg, details=[{"field": key, "message": msg}])

    if key == "food.cutoff_mode":
        if value not in CUTOFF_MODES:
            raise bad("must be 'previous_day' or 'same_day'")
    elif key == "food.cutoff_time":
        if parse_hhmm(value) is None:
            raise bad("must be HH:MM (24h)")
    elif key == "food.max_advance_days" and (
        isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_ADVANCE_LIMIT
    ):
        raise bad(f"must be an integer between 1 and {MAX_ADVANCE_LIMIT}")


async def load_config(session: AsyncSession) -> FoodConfig:
    keys = [*FOOD_DEFAULTS, "office.timezone"]
    rows = {r.key: r.value for r in (await session.execute(select(Setting).where(Setting.key.in_(keys)))).scalars()}
    vals = {k: rows.get(k, d) for k, d in FOOD_DEFAULTS.items()}
    for k, v in list(vals.items()):  # a corrupt stored value must not break the endpoints
        try:
            validate_food_setting(k, v)
        except AppError:
            log.error("invalid %s setting; using default", k)
            vals[k] = FOOD_DEFAULTS[k]
    return FoodConfig(
        mode=vals["food.cutoff_mode"],
        cutoff_time=parse_hhmm(vals["food.cutoff_time"]),  # type: ignore[arg-type]
        max_advance_days=vals["food.max_advance_days"],
        tz=att.office_tz({"office.timezone": rows.get("office.timezone", att._DEFAULTS["office.timezone"])}),
    )


# ---------------------------------------------------------------- pure calculators (unit-testable)
def compute_cutoff(day: dt.date, mode: str, cutoff_time: dt.time, tz: ZoneInfo) -> dt.datetime:
    """UTC instant after which `day` can no longer be changed. Wall-clock time in the office tz, then converted, so a
    DST change between the cut-off day and the meal day cannot shift it by an hour."""
    try:
        base = day - dt.timedelta(days=1) if mode == "previous_day" else day
        return dt.datetime.combine(base, cutoff_time, tzinfo=tz).astimezone(dt.UTC)
    except OverflowError:  # year 1 / 9999 edge: never a 500
        raise _invalid_date("Date out of range") from None


def cutoff_time_nonexistent(value: str, tzname: str, days: int = 400) -> bool:
    """True when the wall-clock time falls into a daylight-saving gap on any of the next `days` days in that timezone."""
    tm = parse_hhmm(value)
    if tm is None:
        return False
    tz = ZoneInfo(tzname)
    start = utcnow().date()
    for i in range(days):
        naive = dt.datetime.combine(start + dt.timedelta(days=i), tm)
        if naive.replace(tzinfo=tz).astimezone(dt.UTC).astimezone(tz).replace(tzinfo=None) != naive:
            return True
    return False


def office_today(now: dt.datetime, tz: ZoneInfo) -> dt.date:
    return now.astimezone(tz).date()


def can_change(day: dt.date, now: dt.datetime, cfg: FoodConfig) -> bool:
    today = office_today(now, cfg.tz)
    if day < today or day > today + dt.timedelta(days=cfg.max_advance_days):
        return False
    return now < compute_cutoff(day, cfg.mode, cfg.cutoff_time, cfg.tz)


def _invalid_date(msg: str) -> AppError:
    return AppError("INVALID_DATE", msg, details=[{"field": "date", "message": msg}])


def _check_query_window(day: dt.date, today: dt.date, back: int, ahead: int, field: str) -> None:
    """L1: bound query dates so date arithmetic can never overflow (clean 422 INVALID_DATE instead of a 500)."""
    if not today - dt.timedelta(days=back) <= day <= today + dt.timedelta(days=ahead):
        msg = "Date out of range"
        raise AppError("INVALID_DATE", msg, details=[{"field": field, "message": msg}])


def check_date_window(day: dt.date, now: dt.datetime, cfg: FoodConfig) -> None:
    today = office_today(now, cfg.tz)
    if day < today:
        raise _invalid_date("Past dates cannot be changed")
    if day > today + dt.timedelta(days=cfg.max_advance_days):
        raise _invalid_date(f"At most {cfg.max_advance_days} days ahead")


def check_cutoff(day: dt.date, now: dt.datetime, cfg: FoodConfig) -> dt.datetime:
    cutoff = compute_cutoff(day, cfg.mode, cfg.cutoff_time, cfg.tz)
    if now >= cutoff:
        raise AppError("FOOD_CUTOFF_PASSED", "The cut-off time for this date has passed", status=409)
    return cutoff


# ---------------------------------------------------------------- employee side
async def _upsert(session: AsyncSession, user_id: uuid.UUID, day: dt.date, need: bool, actor_id: uuid.UUID) -> None:
    """INSERT .. ON CONFLICT DO UPDATE on UNIQUE(user_id,date): concurrent toggles are idempotent, last write wins."""
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # SQLite in the test-suite
        from sqlalchemy.dialects.sqlite import insert
    ins = insert(FoodEnrollment).values(user_id=user_id, date=day, need_food=need, updated_by=actor_id)
    await session.execute(
        ins.on_conflict_do_update(
            index_elements=["user_id", "date"],
            set_={"need_food": need, "updated_by": actor_id, "updated_at": func.now()},
        )
    )


def _cutoff_passed_now(day: dt.date, cfg: FoodConfig) -> bool:
    """L2: re-evaluated with a FRESH clock after the write and right before COMMIT, so a request that started just
    before `cutoff_at` but finished after it is rolled back instead of silently committing a late change."""
    return utcnow() >= compute_cutoff(day, cfg.mode, cfg.cutoff_time, cfg.tz)


def _result(day: dt.date, need: bool | None, now: dt.datetime, cfg: FoodConfig) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "need_food": need,
        "can_change": can_change(day, now, cfg),
        "cutoff_at": iso_z(compute_cutoff(day, cfg.mode, cfg.cutoff_time, cfg.tz)),
    }


def _check_working(user: User, day: dt.date, holidays: dict[dt.date, str]) -> None:
    if day in holidays:
        raise _invalid_date("Food is not served on holidays")
    if att.is_weekly_off(user.shift, day):
        raise _invalid_date("Food is not served on your weekly off")


def _validate_one(user: User, day: dt.date, now: dt.datetime, cfg: FoodConfig, holidays: dict[dt.date, str]) -> None:
    check_date_window(day, now, cfg)
    _check_working(user, day, holidays)
    check_cutoff(day, now, cfg)


async def set_enrollment(session: AsyncSession, actor: Principal, day: dt.date, need_food: bool) -> dict[str, Any]:
    cfg = await load_config(session)
    now = utcnow()
    check_date_window(day, now, cfg)
    _validate_one(actor.user, day, now, cfg, await att.holiday_map(session, day, day))
    await _upsert(session, actor.id, day, need_food, actor.id)
    if _cutoff_passed_now(day, cfg):
        await session.rollback()
        raise AppError("FOOD_CUTOFF_PASSED", "The cut-off time for this date has passed", status=409)
    await session.commit()
    return _result(day, need_food, now, cfg)


async def set_bulk(session: AsyncSession, actor: Principal, items: list[tuple[dt.date, bool]]) -> dict[str, Any]:
    """Per-item results; one bad date never blocks the others. Each item is validated with a fresh clock; before COMMIT the
    cut-offs are checked again and, if one passed mid-way, the batch is redone once (the late item then fails cleanly)."""
    cfg = await load_config(session)
    holidays = await att.holiday_map(session, min(d for d, _ in items), max(d for d, _ in items))
    # read now: a rollback below expires the ORM objects, and lazy loads are impossible in async code
    uid, shift = actor.id, actor.user.shift
    who = SimpleNamespace(shift=SimpleNamespace(working_days=list(shift.working_days or [])) if shift is not None else None)
    ok: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for attempt in (1, 2):
        ok, failed = [], []
        for day, need in items:
            now = utcnow()
            try:
                _validate_one(who, day, now, cfg, holidays)  # type: ignore[arg-type]
            except AppError as exc:
                failed.append({"date": day.isoformat(), "code": exc.code})
                continue
            await _upsert(session, uid, day, need, uid)
            ok.append(_result(day, need, now, cfg))
        if attempt == 2 or not any(_cutoff_passed_now(dt.date.fromisoformat(r["date"]), cfg) for r in ok):
            break
        await session.rollback()
    await session.commit()
    return {"items": ok, "failed": failed}


async def my_enrollments(session: AsyncSession, actor: Principal, d_from: dt.date | None, d_to: dt.date | None) -> dict[str, Any]:
    cfg = await load_config(session)
    now = utcnow()
    today = office_today(now, cfg.tz)
    d_from = d_from or today
    _check_query_window(d_from, today, MY_WINDOW_DAYS, MY_WINDOW_DAYS, "from")
    if d_to is not None:
        _check_query_window(d_to, today, MY_WINDOW_DAYS, MY_WINDOW_DAYS, "to")
    d_to = d_to or d_from + dt.timedelta(days=13)
    att.check_range(d_from, d_to)
    if (d_to - d_from).days >= MAX_LIST_DAYS:
        raise AppError("INVALID_DATE_RANGE", f"Range may not exceed {MAX_LIST_DAYS} days")
    rows = {
        r.date: r.need_food
        for r in (
            await session.execute(
                select(FoodEnrollment).where(
                    FoodEnrollment.user_id == actor.id, FoodEnrollment.date >= d_from, FoodEnrollment.date <= d_to
                )
            )
        ).scalars()
    }
    holidays = await att.holiday_map(session, d_from, d_to)
    items = []
    day = d_from
    while day <= d_to:
        off = att.is_weekly_off(actor.user.shift, day)
        res = _result(day, rows.get(day), now, cfg)
        res["can_change"] = res["can_change"] and day not in holidays and not off
        res["is_holiday"] = day in holidays
        res["is_weekly_off"] = off
        items.append(res)
        day += dt.timedelta(days=1)
    return {"items": items}


async def today_summary(session: AsyncSession, user: User) -> dict[str, Any]:
    """For the profile `food_today` block."""
    cfg = await load_config(session)
    now = utcnow()
    today = office_today(now, cfg.tz)
    need = (
        await session.execute(
            select(FoodEnrollment.need_food).where(FoodEnrollment.user_id == user.id, FoodEnrollment.date == today)
        )
    ).scalar_one_or_none()
    return {"date": today.isoformat(), "need_food": need, "can_change": can_change(today, now, cfg)}


# ---------------------------------------------------------------- admin side
async def daily(
    session: AsyncSession,
    actor: Principal,
    day: dt.date | None,
    department_id: uuid.UUID | None,
    include: str,
    page: int = 1,
    page_size: int = DAILY_PAGE_SIZE,
) -> dict[str, Any]:
    cfg = await load_config(session)
    now = utcnow()
    today = office_today(now, cfg.tz)
    day = day or today
    _check_query_window(day, today, DAILY_PAST_DAYS, MY_WINDOW_DAYS, "date")
    active = [User.status == "active", User.deleted_at.is_(None)]
    if department_id is not None:
        active.append(User.department_id == department_id)
    on_day = and_(FoodEnrollment.user_id == User.id, FoodEnrollment.date == day)
    yes = func.coalesce(func.sum(case((FoodEnrollment.need_food.is_(True), 1), else_=0)), 0)
    no = func.coalesce(func.sum(case((FoodEnrollment.need_food.is_(False), 1), else_=0)), 0)

    total, n_yes, n_no = (
        await session.execute(
            select(func.count(User.id), yes, no).select_from(User).outerjoin(FoodEnrollment, on_day).where(*active)
        )
    ).one()
    by_dept_rows = (
        await session.execute(
            select(Department.name, func.count(User.id), yes, no)
            .select_from(User)
            .outerjoin(FoodEnrollment, on_day)
            .outerjoin(Department, Department.id == User.department_id)
            .where(*active)
            .group_by(Department.name)
            .order_by(Department.name)
        )
    ).all()

    q = (
        select(User, FoodEnrollment.need_food)
        .outerjoin(FoodEnrollment, on_day)
        .where(*active)
        .order_by(User.full_name, User.employee_id)
    )
    if include != "all":
        q = q.where(FoodEnrollment.need_food.is_(True))
    total_people = int((await session.execute(select(func.count()).select_from(q.order_by(None).subquery()))).scalar_one())
    q = q.limit(page_size).offset((page - 1) * page_size)  # L5: the employee list is paginated, counts stay aggregates
    people = (await session.execute(q)).unique().all()

    leave_ids: set[uuid.UUID] = set()
    if people:
        leave_ids = set(
            (
                await session.execute(
                    select(LeaveRequest.user_id).where(
                        LeaveRequest.user_id.in_([u.id for u, _ in people]),
                        LeaveRequest.status == "approved",
                        LeaveRequest.half_day.is_(False),
                        LeaveRequest.from_date <= day,
                        LeaveRequest.to_date >= day,
                    )
                )
            ).scalars()
        )
    audit(  # L7: HR/Admin reads of the people list are traceable (who looked at which day/filter)
        session,
        action="food.daily.view",
        actor=actor,
        entity_type="food",
        after={
            "date": day.isoformat(),
            "department_id": str(department_id) if department_id else None,
            "include": include,
            "page": page,
            "rows": len(people),
        },
    )
    await session.commit()
    return {
        "date": day.isoformat(),
        "cutoff_at": iso_z(compute_cutoff(day, cfg.mode, cfg.cutoff_time, cfg.tz)),
        "page": page,
        "page_size": page_size,
        "total": total_people,
        "count_yes": int(n_yes),
        "count_no": int(n_no),
        "count_unanswered": int(total) - int(n_yes) - int(n_no),
        "by_department": [
            {"department": name, "count_yes": int(y), "count_no": int(n), "count_unanswered": int(t) - int(y) - int(n)}
            for name, t, y, n in by_dept_rows
        ],
        "employees": [{"user": att.user_summary(u), "need_food": need, "on_leave": u.id in leave_ids} for u, need in people],
    }
