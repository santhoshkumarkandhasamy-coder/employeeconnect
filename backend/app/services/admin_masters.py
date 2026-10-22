"""Admin masters (plan 09): departments, shifts, leave types, per-employee leave balances and the audit-log viewer.
Every mutation writes an audit row (before/after in the same transaction). Deleting something that is referenced answers
409 IN_USE (deactivate instead). Holidays live in services/attendance.py, roles in services/roles.py."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit, redact
from ..core.clock import iso_z, utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    AnnouncementDepartment,
    Attendance,
    AuditLog,
    Department,
    LeaveBalance,
    LeaveRequest,
    LeaveType,
    Shift,
    User,
)
from . import employees as employees_svc
from . import leave as leave_svc


def _in_use(what: str) -> AppError:
    return AppError("IN_USE", f"{what} is in use and cannot be deleted; deactivate it instead", status=409)


def _dup(field: str) -> AppError:
    return AppError(
        "CONFLICT",
        f"A record with this {field} already exists",
        status=409,
        details=[{"field": field, "message": "already exists"}],
    )


def _like(q: str) -> str:
    return "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _count(session: AsyncSession, model: Any, *cond: Any) -> int:
    return (await session.execute(select(func.count()).select_from(model).where(*cond))).scalar_one()


async def _taken(session: AsyncSession, model: Any, column: Any, value: str, except_id: uuid.UUID | None) -> bool:
    q = select(model.id).where(func.lower(column) == value.lower())
    if except_id:
        q = q.where(model.id != except_id)
    return (await session.execute(q)).first() is not None


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:  # unique constraint raced with another admin
        await session.rollback()
        raise AppError("CONFLICT", "A record with the same name or code already exists", status=409) from exc


async def _delete(session: AsyncSession, row: Any, what: str) -> None:
    try:
        async with session.begin_nested():
            await session.delete(row)
    except IntegrityError as exc:  # some other table still references it
        raise _in_use(what) from exc


# ================================================================ departments
def dept_out(d: Department) -> dict[str, Any]:
    return {"id": str(d.id), "name": d.name, "code": d.code, "is_active": d.is_active}


async def _dept(session: AsyncSession, dept_id: uuid.UUID) -> Department:
    d = await session.get(Department, dept_id)
    if d is None:
        raise AppError("NOT_FOUND", "Department not found")
    return d


async def list_departments(session: AsyncSession, params: PageParams, q: str | None, active: bool | None) -> dict[str, Any]:
    cond: list[Any] = []
    if q:
        cond.append(or_(Department.name.ilike(_like(q), escape="\\"), Department.code.ilike(_like(q), escape="\\")))
    if active is not None:
        cond.append(Department.is_active.is_(active))
    total = await _count(session, Department, *cond)
    rows = (
        await session.execute(
            select(Department).where(*cond).order_by(Department.name).limit(params.page_size).offset(params.offset)
        )
    ).scalars()
    return make_page([dept_out(d) for d in rows], params, total)


async def get_department(session: AsyncSession, dept_id: uuid.UUID) -> dict[str, Any]:
    return dept_out(await _dept(session, dept_id))


async def create_department(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    name = body.name
    if await _taken(session, Department, Department.name, name, None):
        raise _dup("name")
    if await _taken(session, Department, Department.code, body.code, None):
        raise _dup("code")
    d = Department(name=name, code=body.code, is_active=body.is_active)
    session.add(d)
    await session.flush()
    audit(session, action="department.create", actor=actor, entity_type="department", entity_id=d.id, after=dept_out(d))
    await _commit(session)
    return dept_out(d)


async def update_department(session: AsyncSession, actor: Principal, dept_id: uuid.UUID, body: Any) -> dict[str, Any]:
    d = await _dept(session, dept_id)
    if await _taken(session, Department, Department.name, body.name, d.id):
        raise _dup("name")
    if await _taken(session, Department, Department.code, body.code, d.id):
        raise _dup("code")
    before = dept_out(d)
    d.name, d.code, d.is_active = body.name, body.code, body.is_active
    await session.flush()
    audit(
        session,
        action="department.update",
        actor=actor,
        entity_type="department",
        entity_id=d.id,
        before=before,
        after=dept_out(d),
    )
    await _commit(session)
    return dept_out(d)


async def delete_department(session: AsyncSession, actor: Principal, dept_id: uuid.UUID) -> None:
    d = await _dept(session, dept_id)
    if await _count(session, User, User.department_id == d.id) or await _count(
        session, AnnouncementDepartment, AnnouncementDepartment.department_id == d.id
    ):
        raise _in_use("Department")
    before = dept_out(d)
    await _delete(session, d, "Department")
    audit(session, action="department.delete", actor=actor, entity_type="department", entity_id=dept_id, before=before)
    await _commit(session)


# ================================================================ shifts
def shift_out(s: Shift) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "name": s.name,
        "start_time": s.start_time.strftime("%H:%M"),
        "end_time": s.end_time.strftime("%H:%M"),
        "grace_minutes": s.grace_minutes,
        "working_days": sorted(int(x) for x in s.working_days),
        "is_active": s.is_active,
    }


async def _shift(session: AsyncSession, shift_id: uuid.UUID) -> Shift:
    s = await session.get(Shift, shift_id)
    if s is None:
        raise AppError("NOT_FOUND", "Shift not found")
    return s


async def list_shifts(session: AsyncSession, params: PageParams, q: str | None, active: bool | None) -> dict[str, Any]:
    cond: list[Any] = []
    if q:
        cond.append(Shift.name.ilike(_like(q), escape="\\"))
    if active is not None:
        cond.append(Shift.is_active.is_(active))
    total = await _count(session, Shift, *cond)
    rows = (
        await session.execute(select(Shift).where(*cond).order_by(Shift.name).limit(params.page_size).offset(params.offset))
    ).scalars()
    return make_page([shift_out(s) for s in rows], params, total)


async def get_shift(session: AsyncSession, shift_id: uuid.UUID) -> dict[str, Any]:
    return shift_out(await _shift(session, shift_id))


async def create_shift(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    if await _taken(session, Shift, Shift.name, body.name, None):
        raise _dup("name")
    s = Shift(
        name=body.name,
        start_time=body.start_time.replace(second=0, microsecond=0),
        end_time=body.end_time.replace(second=0, microsecond=0),
        grace_minutes=body.grace_minutes,
        working_days=body.working_days,
        is_active=body.is_active,
    )
    session.add(s)
    await session.flush()
    audit(session, action="shift.create", actor=actor, entity_type="shift", entity_id=s.id, after=shift_out(s))
    await _commit(session)
    return shift_out(s)


async def update_shift(session: AsyncSession, actor: Principal, shift_id: uuid.UUID, body: Any) -> dict[str, Any]:
    """A change applies to FUTURE attendance only: attendance rows keep the snapshot taken when they were recorded."""
    s = await _shift(session, shift_id)
    if await _taken(session, Shift, Shift.name, body.name, s.id):
        raise _dup("name")
    before = shift_out(s)
    s.name, s.grace_minutes, s.working_days, s.is_active = body.name, body.grace_minutes, body.working_days, body.is_active
    s.start_time = body.start_time.replace(second=0, microsecond=0)
    s.end_time = body.end_time.replace(second=0, microsecond=0)
    await session.flush()
    audit(session, action="shift.update", actor=actor, entity_type="shift", entity_id=s.id, before=before, after=shift_out(s))
    await _commit(session)
    return shift_out(s)


async def delete_shift(session: AsyncSession, actor: Principal, shift_id: uuid.UUID) -> None:
    s = await _shift(session, shift_id)
    if await _count(session, User, User.shift_id == s.id) or await _count(session, Attendance, Attendance.shift_id == s.id):
        raise _in_use("Shift")
    before = shift_out(s)
    await _delete(session, s, "Shift")
    audit(session, action="shift.delete", actor=actor, entity_type="shift", entity_id=shift_id, before=before)
    await _commit(session)


# ================================================================ leave types
def type_out(t: LeaveType) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "code": t.code,
        "name": t.name,
        "annual_quota": leave_svc.num(t.annual_quota),
        "is_paid": t.is_paid,
        "allow_half_day": t.allow_half_day,
        "requires_attachment_after_days": t.requires_attachment_after_days,
        "max_consecutive_days": t.max_consecutive_days,
        "is_active": t.is_active,
    }


async def _type(session: AsyncSession, type_id: uuid.UUID) -> LeaveType:
    t = await session.get(LeaveType, type_id)
    if t is None:
        raise AppError("NOT_FOUND", "Leave type not found")
    return t


async def list_leave_types(session: AsyncSession, params: PageParams, q: str | None, active: bool | None) -> dict[str, Any]:
    cond: list[Any] = []
    if q:
        cond.append(or_(LeaveType.name.ilike(_like(q), escape="\\"), LeaveType.code.ilike(_like(q), escape="\\")))
    if active is not None:
        cond.append(LeaveType.is_active.is_(active))
    total = await _count(session, LeaveType, *cond)
    rows = (
        await session.execute(
            select(LeaveType).where(*cond).order_by(LeaveType.name).limit(params.page_size).offset(params.offset)
        )
    ).scalars()
    return make_page([type_out(t) for t in rows], params, total)


async def get_leave_type(session: AsyncSession, type_id: uuid.UUID) -> dict[str, Any]:
    return type_out(await _type(session, type_id))


def _apply_type(t: LeaveType, body: Any) -> None:
    t.code, t.name, t.annual_quota = body.code, body.name, body.annual_quota
    t.is_paid, t.allow_half_day, t.is_active = body.is_paid, body.allow_half_day, body.is_active
    t.requires_attachment_after_days, t.max_consecutive_days = body.requires_attachment_after_days, body.max_consecutive_days


async def create_leave_type(session: AsyncSession, actor: Principal, body: Any) -> dict[str, Any]:
    if await _taken(session, LeaveType, LeaveType.code, body.code, None):
        raise _dup("code")
    if await _taken(session, LeaveType, LeaveType.name, body.name, None):
        raise _dup("name")
    t = LeaveType()
    _apply_type(t, body)
    session.add(t)
    await session.flush()
    audit(session, action="leave_type.create", actor=actor, entity_type="leave_type", entity_id=t.id, after=type_out(t))
    await _commit(session)
    return type_out(t)


async def update_leave_type(session: AsyncSession, actor: Principal, type_id: uuid.UUID, body: Any) -> dict[str, Any]:
    """Changing `annual_quota` only affects balance rows created from now on; existing balances keep their quota
    (use the per-employee balance API or apply-defaults for a new year)."""
    t = await _type(session, type_id)
    if await _taken(session, LeaveType, LeaveType.code, body.code, t.id):
        raise _dup("code")
    if await _taken(session, LeaveType, LeaveType.name, body.name, t.id):
        raise _dup("name")
    before = type_out(t)
    _apply_type(t, body)
    await session.flush()
    audit(
        session,
        action="leave_type.update",
        actor=actor,
        entity_type="leave_type",
        entity_id=t.id,
        before=before,
        after=type_out(t),
    )
    await _commit(session)
    return type_out(t)


async def delete_leave_type(session: AsyncSession, actor: Principal, type_id: uuid.UUID) -> None:
    """In use = any request references it, or any balance row has used/pending days. Untouched balance rows (the
    zero-usage rows created automatically for every employee) are removed together with the type."""
    t = await _type(session, type_id)
    if await _count(session, LeaveRequest, LeaveRequest.leave_type_id == t.id) or await _count(
        session, LeaveBalance, LeaveBalance.leave_type_id == t.id, or_(LeaveBalance.used > 0, LeaveBalance.pending > 0)
    ):
        raise _in_use("Leave type")
    before = type_out(t)
    for b in (await session.execute(select(LeaveBalance).where(LeaveBalance.leave_type_id == t.id))).scalars():
        await session.delete(b)
    await _delete(session, t, "Leave type")
    audit(session, action="leave_type.delete", actor=actor, entity_type="leave_type", entity_id=type_id, before=before)
    await _commit(session)


# ================================================================ leave balances (per employee)
async def _target(session: AsyncSession, actor: Principal, user_id: uuid.UUID) -> User:
    u = (await session.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))).unique().scalar_one_or_none()
    if u is None:
        raise AppError("NOT_FOUND", "Employee not found")
    employees_svc.ensure_in_scope(actor, u)  # review M2: team-scoped custom roles only reach their direct reports
    return u


def _bal_out(t: LeaveType, quota: Any, used: Any, pending: Any) -> dict[str, Any]:
    q, us, pe = leave_svc.d1(quota), leave_svc.d1(used), leave_svc.d1(pending)
    return {
        "leave_type": {"id": str(t.id), "code": t.code, "name": t.name},
        "quota": float(q),
        "used": float(us),
        "pending": float(pe),
        "remaining": float(q - us - pe),
    }


async def get_balances(session: AsyncSession, actor: Principal, user_id: uuid.UUID, year: int | None) -> dict[str, Any]:
    """Read only (nothing is created): a type without a row yet is shown with the quota it would get."""
    u = await _target(session, actor, user_id)
    year = year or utcnow().year
    types = (
        (await session.execute(select(LeaveType).where(LeaveType.is_active.is_(True)).order_by(LeaveType.name))).scalars().all()
    )
    rows = {
        b.leave_type_id: b
        for b in (
            await session.execute(select(LeaveBalance).where(LeaveBalance.user_id == u.id, LeaveBalance.year == year))
        ).scalars()
    }
    items = []
    for t in types:
        b = rows.get(t.id)
        items.append(_bal_out(t, b.quota, b.used, b.pending) if b else _bal_out(t, leave_svc.initial_quota(t, u, year), 0, 0))
    return {"year": year, "items": items}


async def set_balances(session: AsyncSession, actor: Principal, user_id: uuid.UUID, body: Any) -> dict[str, Any]:
    u = await _target(session, actor, user_id)
    if u.id == actor.id:  # review M3: nobody grants themselves leave days
        raise AppError("FORBIDDEN", "You cannot change your own leave balance")
    employees_svc.guard_target(actor, u, elevated_needs_assign=True)  # peers / more privileged accounts: Admin only
    ids = [i.leave_type_id for i in body.items]
    if len(set(ids)) != len(ids):
        raise AppError(
            "VALIDATION_ERROR", "Duplicate leave_type_id", details=[{"field": "items", "message": "duplicate leave type"}]
        )
    types = {t.id: t for t in (await session.execute(select(LeaveType).where(LeaveType.id.in_(ids)))).scalars()}
    missing = [str(i) for i in ids if i not in types]
    if missing:
        raise AppError(
            "VALIDATION_ERROR",
            "Unknown leave type",
            details=[{"field": "items", "message": f"unknown leave_type_id {m}"} for m in missing],
        )
    changes: list[dict[str, Any]] = []
    for item in body.items:
        t = types[item.leave_type_id]
        bal = await leave_svc.get_balance(session, u, t, body.year, lock=True)
        new = leave_svc.d1(item.quota)
        if new < leave_svc.d1(bal.used) + leave_svc.d1(bal.pending):
            raise AppError(
                "QUOTA_BELOW_USED",
                "Quota cannot be lower than used plus pending days",
                details=[{"field": t.code, "message": "below used + pending"}],
            )
        old = leave_svc.d1(bal.quota)
        if new != old:
            bal.quota = new
            changes.append({"leave_type": t.code, "before": float(old), "after": float(new)})
    if changes:
        audit(
            session, action="leave_balance.update", actor=actor, entity_type="user", entity_id=u.id,
            after={"year": body.year, "changes": changes},
        )  # fmt: skip
    await session.commit()
    return await get_balances(session, actor, user_id, body.year)


async def apply_default_quota(session: AsyncSession, actor: Principal, year: int) -> dict[str, Any]:
    """Create the missing balance rows of `year` for every active employee from the leave types' default quota.
    Idempotent: existing rows (and anything edited per employee) are never touched."""
    ids = list(
        (await session.execute(select(User.id).where(User.status == "active", User.deleted_at.is_(None)).order_by(User.id)))
        .unique()
        .scalars()
    )
    created = 0
    for uid in ids:
        u = await session.get(User, uid)
        if u is not None:
            created += await leave_svc.ensure_balances(session, u, year)
    audit(
        session, action="leave_balance.apply_defaults", actor=actor, entity_type="leave_balance",
        after={"year": year, "employees": len(ids), "created": created},
    )  # fmt: skip
    await session.commit()
    return {"year": year, "employees": len(ids), "created": created}


# ================================================================ audit-log viewer
def _day_start(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC)


async def list_audit_logs(
    session: AsyncSession,
    viewer: Principal,
    params: PageParams,
    *,
    actor: str | None,
    action: str | None,
    entity_type: str | None,
    entity_id: str | None,
    from_: dt.date | None,
    to: dt.date | None,
) -> dict[str, Any]:
    """Read-only. Dates are UTC days (`from` inclusive, `to` inclusive). `actor` is a user id or an employee_id;
    `action` matches exactly or as a prefix ending at a dot (`employee` matches `employee.update`)."""
    cond: list[Any] = []
    if actor:
        try:
            cond.append(AuditLog.actor_id == uuid.UUID(actor))
        except ValueError:
            cond.append(func.upper(AuditLog.actor_employee_id) == actor.strip().upper())
    if action:
        cond.append(or_(AuditLog.action == action, AuditLog.action.like(_like(action)[1:-1] + ".%", escape="\\")))
    if entity_type:
        cond.append(AuditLog.entity_type == entity_type)
    if entity_id:
        cond.append(AuditLog.entity_id == entity_id)
    if from_:
        cond.append(AuditLog.ts >= _day_start(from_))
    if to:
        cond.append(AuditLog.ts < _day_start(to) + dt.timedelta(days=1))
    total = await _count(session, AuditLog, *cond)
    rows = (
        (
            await session.execute(
                select(AuditLog).where(*cond).order_by(AuditLog.id.desc()).limit(params.page_size).offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    users = {}
    actor_ids = {r.actor_id for r in rows if r.actor_id}
    if actor_ids:
        users = {x.id: x for x in (await session.execute(select(User).where(User.id.in_(actor_ids)))).unique().scalars()}
    items = []
    for r in rows:
        u = users.get(r.actor_id) if r.actor_id else None
        who = (
            {
                "id": str(u.id),
                "employee_id": u.employee_id,
                "full_name": u.full_name,
                "department": u.department.name if u.department else None,
                "photo_url": None,
            }
            if u
            else (
                {"id": None, "employee_id": r.actor_employee_id, "full_name": None, "department": None, "photo_url": None}
                if r.actor_employee_id
                else None
            )
        )
        items.append(
            {
                "id": r.id,
                "ts": iso_z(r.ts),
                "actor": who,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "before": redact(r.before),  # masked again on the way out, whatever an older writer stored
                "after": redact(r.after),
                "ip": str(r.ip) if r.ip else None,
                "outcome": r.outcome,
            }
        )
    # review L3: reading the audit log is itself audited (the filters, never the content). Written after the query so a
    # view does not list itself. before/after of other rows hold ids and field NAMES only (PII values are masked at write
    # time and again on output); audit rows are kept for the retention period of the deployment (no purge job exists).
    audit(
        session, action="audit.view", actor=viewer, entity_type="audit_log",
        after={
            "filters": {
                k: v
                for k, v in {
                    "actor": actor, "action": action, "entity_type": entity_type, "entity_id": entity_id,
                    "from": from_.isoformat() if from_ else None, "to": to.isoformat() if to else None,
                    "page": params.page, "page_size": params.page_size,
                }.items()
                if v is not None
            }
        },
    )  # fmt: skip
    await session.commit()
    return make_page(items, params, total)
