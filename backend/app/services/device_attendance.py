"""From device punches to attendance (plan 18 section 5, contract 21.3-21.6).

`derive_day(user, work_date)` is the ONE pure-ish function that recomputes a day from scratch: the phone punch times kept on the
row (`app_check_in_at` / `app_check_out_at`), the accepted device punches of every device user mapped to the employee, the manual
flag, the shift, leave / on duty / payroll state and the `device.*` settings. Running it twice gives the same row. It never commits
(callers do) and locks the attendance row FOR UPDATE; a concurrent phone check-in (IntegrityError) triggers one retry.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import as_utc, iso_z, utcnow
from ..core.errors import AppError
from ..core.pagination import make_page
from ..core.rbac import Principal
from ..models import (
    Attendance,
    Device,
    DevicePunch,
    DevicePunchOutcome,
    DeviceUser,
    LateRecord,
    PayrollRun,
    Shift,
    User,
)
from . import attendance as att
from . import device_settings

log = logging.getLogger("app.device_attendance")

STATE_LABELS = {0: "check_in", 1: "check_out", 2: "break_out", 3: "break_in", 4: "ot_in", 5: "ot_out"}
METHOD_LABELS = {0: "password", 1: "fingerprint", 2: "card", 3: "password", 4: "card", 15: "face", 25: "palm"}
BLOCK_TEXT = {
    "inactive_user": "the employee is not active",
    "payroll_locked": "the payroll for that month is processed",
    "outside_window": "the day is outside the device apply window",
    "manual_entry": "the day is a manual entry",
    "on_leave": "the employee is on approved leave",
    "on_duty": "the employee is on duty at a client site",
    "no_shift": "the employee has no shift",
}
MAPPED = ("auto", "manual")


def state_label(state: int | None) -> str:
    return STATE_LABELS.get(state, "unknown") if state is not None else "unknown"


def method_label(verify: int | None) -> str:
    return METHOD_LABELS.get(verify, "other") if verify is not None else "other"


@dataclass
class DayResult:
    action: str  # created | updated | unchanged | deleted | not_applied | none
    reason: str | None = None


# ---------------------------------------------------------------- work date of a punch
def punch_work_date(punch_at: dt.datetime, shift: Shift | None, tz: ZoneInfo) -> dt.date:
    """Day shift: the office-local date. Night shift (end < start): a punch in the morning up to shift end + buffer belongs to
    the previous date."""
    local = as_utc(punch_at).astimezone(tz)  # type: ignore[union-attr]
    day = local.date()
    if shift is not None and att.is_overnight(shift) and local.time() < shift.start_time:
        end = dt.datetime.combine(day, shift.end_time, tzinfo=tz)
        if local <= end + att.OVERNIGHT_BUFFER:
            return day - dt.timedelta(days=1)
    return day


# ---------------------------------------------------------------- outcomes
async def set_outcomes(session: AsyncSession, rows: list[tuple[int, uuid.UUID | None, dt.date | None, str, str | None]]) -> None:
    """Upsert (punch_id, user_id, work_date, outcome, reason) rows. Only rows whose values changed are written, and the write is
    INSERT ... ON CONFLICT DO UPDATE, so two writers (sync and recompute) can never collide on a primary key (review L4)."""
    if not rows:
        return
    existing = {
        pid: (uid, wd, outcome, reason)
        for pid, uid, wd, outcome, reason in (
            await session.execute(
                select(
                    DevicePunchOutcome.punch_id, DevicePunchOutcome.user_id, DevicePunchOutcome.work_date,
                    DevicePunchOutcome.outcome, DevicePunchOutcome.reason,
                ).where(DevicePunchOutcome.punch_id.in_([r[0] for r in rows]))
            )
        ).all()
    }  # fmt: skip
    todo = list(  # one row per punch (a duplicate key in one ON CONFLICT statement would fail); the last value wins
        {
            pid: {"punch_id": pid, "user_id": user_id, "work_date": wd, "outcome": outcome, "reason": reason}
            for pid, user_id, wd, outcome, reason in rows
            if existing.get(pid) != (user_id, wd, outcome, reason)
        }.values()
    )
    if not todo:
        return
    table = DevicePunchOutcome.__table__
    ins = (postgresql.insert if session.get_bind().dialect.name == "postgresql" else sqlite.insert)(table)
    await session.execute(
        ins.values(todo).on_conflict_do_update(
            index_elements=["punch_id"],
            set_={
                "user_id": ins.excluded.user_id, "work_date": ins.excluded.work_date, "outcome": ins.excluded.outcome,
                "reason": ins.excluded.reason, "updated_at": func.now(),
            },
        )
    )  # fmt: skip


async def unapplied_punches(
    session: AsyncSession, device_id: uuid.UUID, cfg: dict[str, Any], dcfg: device_settings.DeviceCfg, limit: int = 5000
) -> list[tuple[int, str, dt.datetime, str | None]]:
    """Punches of this device inside the apply window that have NO outcome row (a run died between the insert and the
    outcomes). Same tuple shape as the freshly inserted rows, so `apply_new_punches` can take them (review L4)."""
    tz = att.office_tz(cfg)
    today_d = utcnow().astimezone(tz).date()
    since = dt.datetime.combine(today_d - dt.timedelta(days=dcfg.apply_window_days + 1), dt.time(0), tzinfo=tz).astimezone(dt.UTC)
    q = (
        select(DevicePunch.id, DevicePunch.device_user_id, DevicePunch.punch_at, DevicePunch.quarantine)
        .outerjoin(DevicePunchOutcome, DevicePunchOutcome.punch_id == DevicePunch.id)
        .where(DevicePunch.device_id == device_id, DevicePunch.punch_at >= since, DevicePunchOutcome.punch_id.is_(None))
        .order_by(DevicePunch.id)
        .limit(limit)
    )
    return [(r[0], r[1], r[2], r[3]) for r in (await session.execute(q)).all()]


# ---------------------------------------------------------------- helpers
async def _mapped_pairs(session: AsyncSession, user_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    q = select(DeviceUser.device_id, DeviceUser.device_user_id).where(
        DeviceUser.user_id == user_id, DeviceUser.match_method.in_(MAPPED)
    )
    return [(d, u) for d, u in (await session.execute(q)).all()]


def _pairs_cond(pairs: list[tuple[uuid.UUID, str]]):
    return or_(*[and_(DevicePunch.device_id == d, DevicePunch.device_user_id == u) for d, u in pairs])


async def _day_punches(
    session: AsyncSession, pairs: list[tuple[uuid.UUID, str]], work_date: dt.date, shift: Shift | None, tz: ZoneInfo
) -> list[DevicePunch]:
    if not pairs:
        return []
    lo = dt.datetime.combine(work_date - dt.timedelta(days=1), dt.time(0), tzinfo=tz).astimezone(dt.UTC)
    hi = dt.datetime.combine(work_date + dt.timedelta(days=2), dt.time(0), tzinfo=tz).astimezone(dt.UTC)
    q = select(DevicePunch).where(_pairs_cond(pairs), DevicePunch.punch_at >= lo, DevicePunch.punch_at < hi)
    rows = (await session.execute(q.order_by(DevicePunch.punch_at, DevicePunch.id))).scalars().all()
    return [p for p in rows if punch_work_date(p.punch_at, shift, tz) == work_date]


async def payroll_locked(session: AsyncSession, work_date: dt.date) -> bool:
    q = select(PayrollRun.id).where(
        PayrollRun.period == work_date.replace(day=1), PayrollRun.status.in_(("processed", "published"))
    )
    return (await session.execute(q.limit(1))).first() is not None


async def _blocking_reason(
    session: AsyncSession, user: User, rec: Attendance | None, shift: Shift | None, work_date: dt.date, today_d: dt.date,
    dcfg: device_settings.DeviceCfg, force_window: bool,
) -> str | None:  # fmt: skip
    if user.status != "active" or user.deleted_at is not None:
        return "inactive_user"
    if await payroll_locked(session, work_date):
        return "payroll_locked"
    if not force_window and work_date < today_d - dt.timedelta(days=dcfg.apply_window_days):
        return "outside_window"
    if rec is not None and rec.manual:
        return "manual_entry"
    if any(not lv.half_day for lv in await att._approved_leaves(session, user.id, work_date, work_date)):
        return "on_leave"
    if not dcfg.apply_on_on_duty_days and await att.on_duty_site(session, user.id, work_date) is not None:
        return "on_duty"
    if shift is None or (rec is None and not shift.is_active):
        return "no_shift"
    return None


# ---------------------------------------------------------------- derive_day
async def derive_day(
    session: AsyncSession,
    user_id: uuid.UUID,
    work_date: dt.date,
    *,
    cfg: dict[str, Any] | None = None,
    dcfg: device_settings.DeviceCfg | None = None,
    force_window: bool = False,
    _retry: bool = True,
) -> DayResult:
    cfg = cfg or await att.load_settings(session)
    dcfg = dcfg or await device_settings.load(session)
    tz = att.office_tz(cfg)
    today_d = utcnow().astimezone(tz).date()
    user = (await session.execute(select(User).where(User.id == user_id))).unique().scalar_one_or_none()
    if user is None:
        return DayResult("none")
    rec = (
        await session.execute(
            select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == work_date).with_for_update()
        )
    ).scalar_one_or_none()
    shift = (await session.get(Shift, rec.shift_id)) if rec is not None and rec.shift_id else user.shift
    pairs = await _mapped_pairs(session, user.id)
    punches = await _day_punches(session, pairs, work_date, user.shift or shift, tz)
    accepted = [p for p in punches if not p.quarantine]
    outcomes: dict[int, tuple[str, str | None]] = {p.id: ("quarantined", p.quarantine) for p in punches if p.quarantine}

    def flush_outcomes() -> list[tuple[int, uuid.UUID | None, dt.date | None, str, str | None]]:
        return [(pid, user.id, work_date, o, r) for pid, (o, r) in outcomes.items()]

    blocked = await _blocking_reason(session, user, rec, shift, work_date, today_d, dcfg, force_window)
    if blocked:
        for p in accepted:
            outcomes[p.id] = ("not_applied", blocked)
        await set_outcomes(session, flush_outcomes())
        return DayResult("not_applied", blocked)

    gap = dt.timedelta(minutes=dcfg.checkout_min_gap_minutes)
    # (time, rank, kind, punch); rank orders ties: phone times first (a tie keeps the phone as the source)
    events: list[tuple[dt.datetime, int, str, DevicePunch | None]] = []
    app_in = as_utc(rec.app_check_in_at) if rec is not None else None
    app_out = as_utc(rec.app_check_out_at) if rec is not None else None
    if app_in is not None:
        events.append((app_in, 0, "app_in", None))
    if app_out is not None:
        events.append((app_out, 0, "app_out", None))
    for p in accepted:
        events.append((as_utc(p.punch_at), 1, "dev", p))  # type: ignore[arg-type]
    events.sort(key=lambda e: (e[0], e[1], e[3].id if e[3] else 0))
    kept: list[tuple[dt.datetime, int, str, DevicePunch | None]] = []
    for ev in events:
        if ev[2] == "dev" and kept and ev[0] - kept[-1][0] < gap:
            outcomes[ev[3].id] = ("not_applied", "duplicate")  # type: ignore[union-attr]
            continue
        kept.append(ev)

    if not kept:  # nothing left to show for this day
        if rec is not None and rec.check_in_source == "device" and rec.app_check_in_at is None:
            # a device-only row whose punches all vanished (mapping removed) disappears; app rows never do
            await session.execute(delete(LateRecord).where(LateRecord.attendance_id == rec.id))
            audit(session, action="attendance.device_unapply", entity_type="attendance", entity_id=rec.id,
                  after={"owner": user.employee_id, "work_date": work_date.isoformat()})  # fmt: skip
            await session.delete(rec)
            await session.flush()
            await set_outcomes(session, flush_outcomes())
            return DayResult("deleted")
        await set_outcomes(session, flush_outcomes())
        return DayResult("none")

    first = kept[0]
    out_events = [e for e in kept[1:] if e[2] in ("dev", "app_out")]
    last = out_events[-1] if out_events else None
    in_utc = first[0]
    in_dev_p = first[3] if first[2] == "dev" else None
    in_src = "device" if in_dev_p is not None else "app"
    out_utc = last[0] if last else None
    out_dev_p = last[3] if last is not None and last[2] == "dev" else None
    out_src = None if last is None else ("device" if out_dev_p is not None else "app")
    if out_utc is not None and out_utc <= in_utc:
        out_utc, out_dev_p, out_src = None, None, None

    for e in kept:
        p = e[3]
        if p is None:
            continue
        if p is in_dev_p:
            outcomes[p.id] = ("check_in", None)
        elif p is out_dev_p:
            outcomes[p.id] = ("check_out", None)
        else:
            outcomes[p.id] = ("extra", None)

    assert shift is not None  # no_shift is a blocking reason
    minutes_late = 0
    if await att.working_day(session, user, work_date):
        minutes_late = await att.minutes_late_for(session, user, shift, work_date, in_utc.astimezone(tz))
    worked = int((out_utc - in_utc).total_seconds() // 60) if out_utc else None
    status = "late" if minutes_late else "present"
    if worked is not None and worked < int(cfg["attendance.half_day_minutes"]):
        status = "half_day"
    fields: dict[str, Any] = {
        "check_in_at": in_utc,
        "check_out_at": out_utc,
        "check_in_source": in_src,
        "check_in_device_id": in_dev_p.device_id if in_dev_p is not None else None,
        "check_out_source": out_src,
        "check_out_device_id": out_dev_p.device_id if out_dev_p is not None else None,
        "status": status,
        "minutes_late": minutes_late,
        "worked_minutes": worked,
    }
    action = "unchanged"
    if rec is None:
        rec = Attendance(
            user_id=user.id, work_date=work_date, shift_id=shift.id,
            check_in_within_fence=True if in_src == "device" else None, **fields,
        )  # fmt: skip
        action = "created"
        try:
            async with session.begin_nested():
                session.add(rec)
                await session.flush()
        except IntegrityError:
            if not _retry:
                raise
            if rec in session:
                session.expunge(rec)
            return await derive_day(session, user_id, work_date, cfg=cfg, dcfg=dcfg, force_window=force_window, _retry=False)
    else:
        changed = any(
            (as_utc(getattr(rec, k)) if isinstance(v, dt.datetime) or k in ("check_in_at", "check_out_at") else getattr(rec, k))
            != v
            for k, v in fields.items()
        ) or (out_utc is not None and rec.auto_closed)
        for k, v in fields.items():
            setattr(rec, k, v)
        if in_src == "device" and rec.check_in_within_fence is None:
            rec.check_in_within_fence = True
        if out_utc is not None:
            rec.auto_closed = False  # a late check-out re-opens the day for normal handling
        if changed:
            action = "updated"
        await session.flush()
    await _sync_late_record(session, rec, user, shift, minutes_late, in_utc)
    await set_outcomes(session, flush_outcomes())
    return DayResult(action)


async def _sync_late_record(
    session: AsyncSession, rec: Attendance, user: User, shift: Shift, minutes_late: int, in_utc: dt.datetime
) -> None:
    cur = (await session.execute(select(LateRecord).where(LateRecord.attendance_id == rec.id))).scalar_one_or_none()
    if not minutes_late:
        if cur is not None:
            await session.delete(cur)
            await session.flush()
        return
    if cur is None:
        session.add(
            LateRecord(
                user_id=user.id, attendance_id=rec.id, work_date=rec.work_date, shift_start=shift.start_time,
                grace_minutes=shift.grace_minutes, check_in_at=in_utc, minutes_late=minutes_late,
            )
        )  # fmt: skip
    else:
        cur.check_in_at, cur.minutes_late = in_utc, minutes_late
        cur.shift_start, cur.grace_minutes = shift.start_time, shift.grace_minutes
    await session.flush()


# ---------------------------------------------------------------- recompute entry points
async def _commit_day(session: AsyncSession, user_id: uuid.UUID, day: dt.date, cfg, dcfg) -> bool:
    try:
        await derive_day(session, user_id, day, cfg=cfg, dcfg=dcfg)
        await session.commit()
        return True
    except Exception:  # noqa: BLE001 - one bad day must not stop the rest
        await session.rollback()
        log.exception("recompute of one day failed (user %s, %s)", user_id, day)
        return False


async def _run_pairs(
    session, pairs: set[tuple[uuid.UUID, dt.date]], cfg, dcfg, done: set[tuple[uuid.UUID, dt.date]] | None
) -> int:
    done = done if done is not None else set()
    count = 0
    for uid, day in sorted(pairs, key=lambda x: (str(x[0]), x[1])):
        if (uid, day) in done:
            continue
        done.add((uid, day))
        count += await _commit_day(session, uid, day, cfg, dcfg)
    return count


async def apply_new_punches(
    session: AsyncSession, device_id: uuid.UUID, rows: list[tuple[int, str, dt.datetime, str | None]], cfg, dcfg,
    done: set[tuple[uuid.UUID, dt.date]] | None = None,
) -> int:  # fmt: skip
    """Outcomes for freshly inserted punches (id, device_user_id, punch_at, quarantine) and a recompute of every affected
    (employee, work date). Returns the number of days recomputed."""
    if not rows:
        return 0
    tz = att.office_tz(cfg)
    dus = {
        du.device_user_id: du
        for du in (await session.execute(select(DeviceUser).where(DeviceUser.device_id == device_id, DeviceUser.device_user_id.in_({r[1] for r in rows})))).scalars()
    }  # fmt: skip
    user_ids = {du.user_id for du in dus.values() if du.user_id}
    users = (
        {u.id: u for u in (await session.execute(select(User).where(User.id.in_(user_ids)))).unique().scalars()}
        if user_ids
        else {}
    )
    simple: list[tuple[int, uuid.UUID | None, dt.date | None, str, str | None]] = []
    pairs: set[tuple[uuid.UUID, dt.date]] = set()
    for pid, duid, punch_at, quarantine in rows:
        local_day = as_utc(punch_at).astimezone(tz).date()  # type: ignore[union-attr]
        if quarantine:
            simple.append((pid, None, local_day, "quarantined", quarantine))
            continue
        du = dus.get(duid)
        if du is None or du.match_method == "unmatched":
            simple.append((pid, None, local_day, "unmatched", None))
        elif du.match_method == "ignored":
            simple.append((pid, None, local_day, "ignored", "mapping_ignored"))
        else:
            u = users.get(du.user_id)  # type: ignore[arg-type]
            pairs.add((du.user_id, punch_work_date(punch_at, u.shift if u else None, tz)))  # type: ignore[arg-type]
    await set_outcomes(session, simple)
    await session.commit()
    return await _run_pairs(session, pairs, cfg, dcfg, done)


async def recompute_device_users(
    session: AsyncSession, keys: list[tuple[uuid.UUID, str]], *, cfg=None, dcfg=None, all_dates: bool = False,
    done: set[tuple[uuid.UUID, dt.date]] | None = None,
) -> int:  # fmt: skip
    """After a mapping change: refresh the outcomes of those device users' punches inside the apply window and recompute the
    affected days of the previous and the new employee. Returns the number of days recomputed."""
    if not keys:
        return 0
    cfg = cfg or await att.load_settings(session)
    dcfg = dcfg or await device_settings.load(session)
    tz = att.office_tz(cfg)
    today_d = utcnow().astimezone(tz).date()
    since = dt.datetime.combine(today_d - dt.timedelta(days=dcfg.apply_window_days + 1), dt.time(0), tzinfo=tz).astimezone(dt.UTC)
    cond = _pairs_cond(keys)
    q = select(DevicePunch).where(cond)
    if not all_dates:
        q = q.where(DevicePunch.punch_at >= since)
    punches = list((await session.execute(q)).scalars())
    if not punches:
        return 0
    mapping = {
        (du.device_id, du.device_user_id): du
        for du in (await session.execute(select(DeviceUser).where(or_(*[and_(DeviceUser.device_id == d, DeviceUser.device_user_id == u) for d, u in keys])))).scalars()
    }  # fmt: skip
    prev = {
        o.punch_id: o
        for o in (await session.execute(select(DevicePunchOutcome).where(DevicePunchOutcome.punch_id.in_([p.id for p in punches])))).scalars()
    }  # fmt: skip
    pairs: set[tuple[uuid.UUID, dt.date]] = {(o.user_id, o.work_date) for o in prev.values() if o.user_id and o.work_date}
    uids = {du.user_id for du in mapping.values() if du.user_id}
    users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(uids)))).unique().scalars()} if uids else {}
    simple: list[tuple[int, uuid.UUID | None, dt.date | None, str, str | None]] = []
    for p in punches:
        local_day = as_utc(p.punch_at).astimezone(tz).date()  # type: ignore[union-attr]
        if p.quarantine:
            simple.append((p.id, None, local_day, "quarantined", p.quarantine))
            continue
        du = mapping.get((p.device_id, p.device_user_id))
        if du is None or du.match_method == "unmatched":
            simple.append((p.id, None, local_day, "unmatched", None))
        elif du.match_method == "ignored":
            simple.append((p.id, None, local_day, "ignored", "mapping_ignored"))
        else:
            u = users.get(du.user_id)  # type: ignore[arg-type]
            pairs.add((du.user_id, punch_work_date(p.punch_at, u.shift if u else None, tz)))  # type: ignore[arg-type]
    await set_outcomes(session, simple)
    await session.commit()
    return await _run_pairs(session, pairs, cfg, dcfg, done)


async def recompute_window(session: AsyncSession) -> int:
    """Nightly reconcile: every mapped employee, every day of the apply window (leave / on duty / payroll may have changed)."""
    cfg = await att.load_settings(session)
    dcfg = await device_settings.load(session)
    today_d = utcnow().astimezone(att.office_tz(cfg)).date()
    uids = (
        (await session.execute(select(DeviceUser.user_id).where(DeviceUser.match_method.in_(MAPPED)).distinct())).scalars().all()
    )
    done = 0
    for uid in uids:
        for back in range(dcfg.apply_window_days + 1):
            done += await _commit_day(session, uid, today_d - dt.timedelta(days=back), cfg, dcfg)
    return done


# ---------------------------------------------------------------- manual attendance integration (contract 21.6)
async def day_punches_out(session: AsyncSession, user_id: uuid.UUID, work_date: dt.date) -> list[dict[str, Any]]:
    q = (
        select(DevicePunch, DevicePunchOutcome, Device.name)
        .join(DevicePunchOutcome, DevicePunchOutcome.punch_id == DevicePunch.id)
        .join(Device, Device.id == DevicePunch.device_id)
        .where(DevicePunchOutcome.user_id == user_id, DevicePunchOutcome.work_date == work_date)
        .order_by(DevicePunch.punch_at, DevicePunch.id)
    )
    return [
        {"punch_at": iso_z(p.punch_at), "device_name": name, "method": method_label(p.verify_mode), "outcome": o.outcome}
        for p, o, name in (await session.execute(q)).all()
    ]


async def has_device_punches(session: AsyncSession, user_id: uuid.UUID, work_date: dt.date) -> bool:
    q = select(DevicePunchOutcome.punch_id).where(
        DevicePunchOutcome.user_id == user_id,
        DevicePunchOutcome.work_date == work_date,
        DevicePunchOutcome.outcome != "quarantined",
    )
    return (await session.execute(q.limit(1))).first() is not None


async def refresh_day(session: AsyncSession, user_id: uuid.UUID, work_date: dt.date) -> None:
    """After a phone punch or manual entry: bring the punch outcomes of that day in step. Never fails the caller's change."""
    try:
        await derive_day(session, user_id, work_date)
        await session.commit()
    except Exception:  # noqa: BLE001
        await session.rollback()
        log.exception("could not refresh device outcomes for %s", work_date)


async def use_device(
    session: AsyncSession, actor: Principal, user_id: uuid.UUID, work_date: dt.date, reason: str
) -> dict[str, Any]:
    user = await att._manual_target(session, actor, user_id)  # 404 unknown, 403 yourself
    cfg = await att.load_settings(session)
    dcfg = await device_settings.load(session)
    att._manual_date(cfg, work_date)
    rec = (
        await session.execute(
            select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == work_date).with_for_update()
        )
    ).scalar_one_or_none()
    tz = att.office_tz(cfg)
    pairs = await _mapped_pairs(session, user.id)
    shift = (await session.get(Shift, rec.shift_id)) if rec is not None and rec.shift_id else user.shift
    accepted = [p for p in await _day_punches(session, pairs, work_date, user.shift or shift, tz) if not p.quarantine]
    if not accepted and not (rec is not None and rec.app_check_in_at is not None):
        raise AppError("CONFLICT", "There are no phone or device punches for this day", status=409)
    before = att._manual_snapshot(rec)
    if rec is not None:
        rec.manual, rec.manual_by, rec.manual_reason = False, None, None
        await session.flush()
    res = await derive_day(session, user.id, work_date, cfg=cfg, dcfg=dcfg, force_window=True)
    if res.action in ("not_applied", "none"):
        await session.rollback()
        why = BLOCK_TEXT.get(res.reason or "", "there is nothing to apply")
        raise AppError("CONFLICT", f"Device punches cannot be used: {why}", status=409)
    rec = (
        await session.execute(select(Attendance).where(Attendance.user_id == user.id, Attendance.work_date == work_date))
    ).scalar_one_or_none()
    audit(
        session, action="attendance.device_use", actor=actor, entity_type="attendance", entity_id=rec.id if rec else None,
        before=before, after={"owner": user.employee_id, "work_date": work_date.isoformat(), "reason_length": len(reason)},
    )  # fmt: skip
    await session.commit()
    return await att.manual_get(session, actor, user.id, work_date)


# ---------------------------------------------------------------- reads: employee view and admin punch log
async def my_punches(
    session: AsyncSession, actor: Principal, d_from: dt.date | None, d_to: dt.date | None, on_date: dt.date | None
):
    cfg = await att.load_settings(session)
    today_d = utcnow().astimezone(att.office_tz(cfg)).date()
    if on_date is not None:
        d_from = d_to = on_date
    d_from = d_from or d_to or today_d
    d_to = d_to or d_from
    att.check_range(d_from, d_to)
    if (d_to - d_from).days + 1 > 31:
        raise AppError(
            "INVALID_DATE_RANGE", "Date range may not exceed 31 days", details=[{"field": "to", "message": "at most 31 days"}]
        )
    q = (
        select(DevicePunch, DevicePunchOutcome, Device.name)
        .join(DevicePunchOutcome, DevicePunchOutcome.punch_id == DevicePunch.id)
        .join(Device, Device.id == DevicePunch.device_id)
        .where(
            DevicePunchOutcome.user_id == actor.id,
            DevicePunchOutcome.work_date >= d_from,
            DevicePunchOutcome.work_date <= d_to,
            DevicePunchOutcome.outcome != "quarantined",
        )
        .order_by(DevicePunch.punch_at, DevicePunch.id)
    )
    items = [
        {
            "punch_at": iso_z(p.punch_at),
            "work_date": o.work_date.isoformat() if o.work_date else None,
            "device_name": name,
            "method": method_label(p.verify_mode),
            "used_as": o.outcome if o.outcome in ("check_in", "check_out") else "none",
        }
        for p, o, name in (await session.execute(q)).all()
    ]
    return {"items": items}


def office_bounds(tz: ZoneInfo, d_from: dt.date, d_to: dt.date) -> tuple[dt.datetime, dt.datetime]:
    lo = dt.datetime.combine(d_from, dt.time(0), tzinfo=tz).astimezone(dt.UTC)
    hi = dt.datetime.combine(d_to + dt.timedelta(days=1), dt.time(0), tzinfo=tz).astimezone(dt.UTC)
    return lo, hi


async def punch_log(
    session: AsyncSession,
    params: Any,
    *,
    device_id: uuid.UUID | None,
    employee_id: str | None,
    device_user_id: str | None,
    d_from: dt.date,
    d_to: dt.date,
    outcome: str | None,
) -> dict[str, Any]:
    att.check_range(d_from, d_to)
    if (d_to - d_from).days + 1 > 92:
        raise AppError(
            "INVALID_DATE_RANGE", "Date range may not exceed 92 days", details=[{"field": "to", "message": "at most 92 days"}]
        )
    tz = att.office_tz(await att.load_settings(session))
    lo, hi = office_bounds(tz, d_from, d_to)
    cond = [DevicePunch.punch_at >= lo, DevicePunch.punch_at < hi]
    if device_id:
        cond.append(DevicePunch.device_id == device_id)
    if device_user_id:
        cond.append(DevicePunch.device_user_id == device_user_id.strip())
    if employee_id and employee_id.strip():
        uid = (await session.execute(select(User.id).where(User.employee_id == employee_id.strip().upper()))).scalar_one_or_none()
        if uid is None:
            return make_page([], params, 0)
        cond.append(DevicePunchOutcome.user_id == uid)
    if outcome:
        cond.append(func.coalesce(DevicePunchOutcome.outcome, "unmatched") == outcome)
    base = (
        select(DevicePunch, DevicePunchOutcome, Device.name)
        .select_from(DevicePunch)
        .join(Device, Device.id == DevicePunch.device_id)
        .outerjoin(DevicePunchOutcome, DevicePunchOutcome.punch_id == DevicePunch.id)
        .where(*cond)
    )
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(
            base.order_by(DevicePunch.punch_at.desc(), DevicePunch.id.desc()).limit(params.page_size).offset(params.offset)
        )
    ).all()
    uids = {o.user_id for _, o, _ in rows if o is not None and o.user_id}
    users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(uids)))).unique().scalars()} if uids else {}
    items = []
    for p, o, name in rows:
        u = users.get(o.user_id) if o is not None and o.user_id else None
        items.append(
            {
                "id": str(p.id),
                "device": {"id": str(p.device_id), "name": name},
                "device_user_id": p.device_user_id,
                "user": att.user_summary(u) if u else None,
                "punch_at": iso_z(p.punch_at),
                "device_time": p.device_time.strftime("%Y-%m-%dT%H:%M:%S"),
                "offset_applied_s": p.offset_applied_s,
                "state": state_label(p.state),
                "verify_mode": p.verify_mode,
                "method": method_label(p.verify_mode),
                "work_date": o.work_date.isoformat() if o is not None and o.work_date else None,
                "outcome": o.outcome if o is not None else ("quarantined" if p.quarantine else "unmatched"),
                "reason": (o.reason if o is not None else p.quarantine),
            }
        )
    return make_page(items, params, total)
