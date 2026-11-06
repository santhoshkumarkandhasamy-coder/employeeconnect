"""Tasks with calendar reminders (plan 14).

A task has a due date/time and an optional reminder `remind_minutes_before` (0 = at the due time). The reminder job
(`send_due_reminders`, every minute) pushes `task_reminder` once and stamps `reminded_at`; editing the due time or the
reminder re-arms it. A reminder time that has already passed when the task is saved is not pushed.

Who may do what
  * own tasks: create / edit / complete / delete.
  * assigning to someone else: `task.assign_all` (anyone active) or `task.assign_team` (direct reports only). The assignee
    gets a `task_assigned` push and may only mark it done / not done; the creator edits and deletes it and gets a
    `task_done` push when the assignee completes it.
  * anyone else: 404 (task ids are not an oracle).
Notes are encrypted at rest; titles are plain (they appear in push notifications anyway).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import as_utc, iso_z, utcnow
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import Task, User
from . import attendance as att
from . import files as files_svc
from .notifications import notify

log = logging.getLogger("app.tasks")

MAX_RANGE_DAYS = 92
MAX_PAST_DAYS = 366
MAX_AHEAD_DAYS = 3 * 366
REMINDER_BATCH = 500


def _err(code: str, message: str, field: str | None = None) -> AppError:
    return AppError(code, message, details=[{"field": field, "message": message}] if field else None)


def _not_found() -> AppError:
    return AppError("NOT_FOUND", "Task not found")


async def _tz(session: AsyncSession):
    return att.office_tz(await att.load_settings(session))


# ================================================================ output
async def _render(session: AsyncSession, rows: list[Task], actor: Principal) -> list[dict[str, Any]]:
    ids = {r.user_id for r in rows} | {r.created_by for r in rows}
    users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).unique().scalars()} if ids else {}
    photos = await files_svc.photo_file_ids(session, list(users))
    out = []
    for t in rows:
        due = as_utc(t.due_at)
        remind = as_utc(t.remind_at) if t.remind_at else None
        out.append(
            {
                "id": str(t.id),
                "title": t.title,
                "notes": t.notes,
                "due_at": iso_z(due),
                "remind_at": iso_z(remind) if remind else None,
                "remind_minutes_before": int((due - remind).total_seconds() // 60) if remind else None,  # type: ignore[operator]
                "done": t.done_at is not None,
                "done_at": iso_z(t.done_at) if t.done_at else None,
                "assigned": t.created_by != t.user_id,
                "user": att.user_summary(users[t.user_id], photos.get(t.user_id)),
                "created_by": att.user_summary(users[t.created_by], photos.get(t.created_by)),
                "can_edit": t.created_by == actor.id,
                "created_at": iso_z(t.created_at),
            }
        )
    return out


async def _one(session: AsyncSession, t: Task, actor: Principal) -> dict[str, Any]:
    return (await _render(session, [t], actor))[0]


# ================================================================ read
async def list_tasks(
    session: AsyncSession, actor: Principal, d_from: dt.date | None, d_to: dt.date | None, *, assigned_by_me: bool
) -> dict[str, Any]:
    """Tasks due between d_from and d_to (office dates, inclusive; default: this month). `assigned_by_me` lists the tasks
    the caller created for other people instead of the caller's own."""
    tz = await _tz(session)
    today = utcnow().astimezone(tz).date()
    if d_from is None and d_to is None:
        d_from = today.replace(day=1)
        d_to = (d_from + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    d_from = d_from or d_to  # type: ignore[assignment]
    d_to = d_to or d_from
    if d_to < d_from:  # type: ignore[operator]
        raise _err("VALIDATION_ERROR", "to must be >= from", "to")
    if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:  # type: ignore[operator]
        raise _err("INVALID_DATE_RANGE", f"Date range may not exceed {MAX_RANGE_DAYS} days", "to")
    start = dt.datetime.combine(d_from, dt.time(0), tzinfo=tz).astimezone(dt.UTC)  # type: ignore[arg-type]
    end = dt.datetime.combine(d_to + dt.timedelta(days=1), dt.time(0), tzinfo=tz).astimezone(dt.UTC)  # type: ignore[operator]
    who = [Task.created_by == actor.id, Task.user_id != actor.id] if assigned_by_me else [Task.user_id == actor.id]
    q = select(Task).where(*who, Task.due_at >= start, Task.due_at < end).order_by(Task.due_at, Task.created_at).limit(1000)
    rows = list((await session.execute(q)).scalars())
    return {"from": d_from.isoformat(), "to": d_to.isoformat(), "items": await _render(session, rows, actor)}  # type: ignore[union-attr]


async def _visible(session: AsyncSession, actor: Principal, task_id: uuid.UUID) -> Task:
    t = await session.get(Task, task_id)
    if t is None or actor.id not in (t.user_id, t.created_by):
        raise _not_found()
    return t


async def get_task(session: AsyncSession, actor: Principal, task_id: uuid.UUID) -> dict[str, Any]:
    return await _one(session, await _visible(session, actor, task_id), actor)


# ================================================================ write
async def _assignee(session: AsyncSession, actor: Principal, user_id: uuid.UUID | None) -> User:
    if user_id is None or user_id == actor.id:
        return actor.user
    target = await session.get(User, user_id)
    ok = target is not None and target.status == "active" and target.deleted_at is None
    if ok and actor.has("task.assign_all"):
        return target  # type: ignore[return-value]
    if ok and actor.has("task.assign_team") and target.manager_id == actor.id:  # type: ignore[union-attr]
        return target  # type: ignore[return-value]
    if not (actor.has("task.assign_all") or actor.has("task.assign_team")):
        raise AppError("FORBIDDEN", "You can only create tasks for yourself")
    raise _err("VALIDATION_ERROR", "You cannot assign a task to this employee", "user_id")


def _times(body: Any, now: dt.datetime) -> tuple[dt.datetime, dt.datetime | None]:
    due = as_utc(body.due_at)
    if due < now - dt.timedelta(days=MAX_PAST_DAYS) or due > now + dt.timedelta(days=MAX_AHEAD_DAYS):  # type: ignore[operator]
        raise _err("INVALID_DATE_RANGE", "The due date is too far in the past or the future", "due_at")
    mins = body.remind_minutes_before
    remind = due - dt.timedelta(minutes=mins) if mins is not None else None  # type: ignore[operator]
    return due, remind  # type: ignore[return-value]


def _arm(t: Task, remind: dt.datetime | None, now: dt.datetime) -> None:
    """Set the reminder; a time already in the past is marked as sent so it never fires late."""
    t.remind_at = remind
    t.reminded_at = now if remind is not None and remind <= now else None


async def create_task(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    now = utcnow()
    target = await _assignee(session, actor, body.user_id)
    due, remind = _times(body, now)
    t = Task(user_id=target.id, created_by=actor.id, title=body.title, notes=body.notes, due_at=due)
    _arm(t, remind, now)
    session.add(t)
    await session.flush()
    assigned = target.id != actor.id
    audit(
        session, action="task.create", actor=actor, entity_type="task", entity_id=t.id,
        after={"assignee": target.employee_id, "assigned": assigned, "due_at": iso_z(due), "reminder": remind is not None},
    )  # fmt: skip
    await session.commit()
    if assigned:
        await notify(
            session, [target.id], type="task_assigned", entity_id=t.id,
            title="New task", body=f"{actor.user.full_name} assigned you: {t.title}",
        )  # fmt: skip
    return await _one(session, t, actor)


async def update_task(session: AsyncSession, actor: Principal, task_id: uuid.UUID, body: Any) -> dict[str, Any]:
    t = await _visible(session, actor, task_id)
    if t.created_by != actor.id:
        raise AppError("FORBIDDEN", "Only the person who created this task can change it")
    now = utcnow()
    due, remind = _times(body, now)
    before = {"due_at": iso_z(as_utc(t.due_at)), "reminder": t.remind_at is not None}
    changed_time = as_utc(t.due_at) != due or (as_utc(t.remind_at) if t.remind_at else None) != remind
    t.title, t.notes, t.due_at = body.title, body.notes, due
    if changed_time:
        _arm(t, remind, now)
    audit(
        session, action="task.update", actor=actor, entity_type="task", entity_id=t.id,
        before=before, after={"due_at": iso_z(due), "reminder": remind is not None},
    )  # fmt: skip
    await session.commit()
    return await _one(session, t, actor)


async def set_done(session: AsyncSession, actor: Principal, task_id: uuid.UUID, done: bool) -> dict[str, Any]:
    t = await _visible(session, actor, task_id)
    was = t.done_at is not None
    t.done_at = (t.done_at or utcnow()) if done else None
    await session.commit()
    if done and not was and t.created_by != t.user_id and actor.id == t.user_id:
        await notify(
            session, [t.created_by], type="task_done", entity_id=t.id,
            title="Task completed", body=f"{actor.user.full_name} completed: {t.title}",
        )  # fmt: skip
    return await _one(session, t, actor)


async def delete_task(session: AsyncSession, actor: Principal, task_id: uuid.UUID) -> None:
    t = await _visible(session, actor, task_id)
    if t.created_by != actor.id:
        raise AppError("FORBIDDEN", "Only the person who created this task can delete it")
    audit(session, action="task.delete", actor=actor, entity_type="task", entity_id=t.id, before={"title_len": len(t.title)})
    await session.delete(t)
    await session.commit()


# ================================================================ job
async def send_due_reminders(session: AsyncSession, now: dt.datetime | None = None) -> int:
    """Push every reminder that is due (remind_at <= now, not sent, task open). Each is stamped first, so a crash between
    the stamp and the push loses one reminder rather than sending it twice."""
    now = as_utc(now) or utcnow()
    q = (
        select(Task)
        .where(Task.remind_at.is_not(None), Task.remind_at <= now, Task.reminded_at.is_(None), Task.done_at.is_(None))
        .order_by(Task.remind_at)
        .limit(REMINDER_BATCH)
        .with_for_update(skip_locked=True)
    )
    rows = list((await session.execute(q)).scalars())
    if not rows:
        return 0
    tz = await _tz(session)
    items = [(t.id, t.user_id, t.title, as_utc(t.due_at)) for t in rows]
    for t in rows:
        t.reminded_at = now
    await session.commit()
    for tid, uid, title, due in items:
        when = due.astimezone(tz).strftime("%d %b, %H:%M")  # type: ignore[union-attr]
        try:
            await notify(session, [uid], type="task_reminder", entity_id=tid, title=f"Reminder: {title}", body=f"Due {when}")
        except Exception:  # noqa: BLE001 - one failed push must not stop the batch
            log.exception("task reminder push failed for %s", tid)
    return len(items)
