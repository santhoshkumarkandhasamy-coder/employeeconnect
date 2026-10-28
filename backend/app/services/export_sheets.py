"""Attendance / Leave / Late / Permission / On Duty sheets for the Excel export (plan 10).

Every builder takes `scope` as a mandatory positional argument and applies `scope.restrict(...)` to BOTH its count query and
its row query (one shared `_filtered` per sheet, so they cannot drift apart). Data minimisation: no coordinates, selfies,
accuracy / mock-location evidence, e-mail or any profile data. Leave / permission reasons and decision comments are
encrypted at rest and may be health data: they are EXCLUDED by default (the columns are absent, the ciphertext is not even
selected) and only included when the caller asked for `include_reasons=true` AND holds `export.sensitive` (review 08 M2;
the router enforces that, builders just honour `filters.include_reasons`). They are decrypted one value at a time: an
undecryptable value becomes a placeholder instead of failing the whole export or leaking an exception. Free text is
sanitised by services.xlsx.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import LargeBinary, func, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..core.config import get_settings
from ..core.crypto import DataDecryptionError, decrypt_bytes
from ..models import (
    Attendance,
    ClientLocation,
    Department,
    LateRecord,
    LeaveRequest,
    LeaveType,
    OnDutyRequest,
    PermissionRequest,
    Shift,
    User,
)
from . import attendance as att
from .xlsx import SheetSpec

if TYPE_CHECKING:
    from .export import ExportFilters, ExportScope

BATCH = 1000
UNREADABLE = "[unreadable]"

ATTENDANCE_HEADERS = [
    "Date",
    "Employee ID",
    "Name",
    "Department",
    "Shift",
    "Check-in",
    "Check-out",
    "Status",
    "Minutes Late",
    "Worked Hours",
    "Within Geofence",
    "Client Location",  # on-duty punch: the client site (empty = office)
]
LEAVE_HEADERS = [  # full list (include_reasons=true)
    "Request ID",
    "Employee ID",
    "Name",
    "Department",
    "Leave Type",
    "From",
    "To",
    "Days",
    "Half Day",
    "Status",
    "Reason",
    "Approver",
    "Decision Comment",
    "Applied On",
    "Decided On",
]
SENSITIVE_HEADERS = frozenset({"Reason", "Decision Comment", "Comment"})  # dropped unless include_reasons=true
LATE_HEADERS = ["Date", "Employee ID", "Name", "Department", "Shift Start", "Check-in", "Minutes Late"]
ON_DUTY_HEADERS = [  # full list (include_reasons=true)
    "Request ID",
    "Employee ID",
    "Name",
    "Department",
    "Client Location",
    "From",
    "To",
    "Days",
    "Status",
    "Reason",
    "Approver",
    "Decision Comment",
    "Applied On",
    "Decided On",
]
PERMISSION_HEADERS = [  # full list (include_reasons=true)
    "Request ID",
    "Employee ID",
    "Name",
    "Department",
    "Date",
    "From",
    "To",
    "Minutes",
    "Status",
    "Reason",
    "Approver",
    "Comment",
]


def headers_for(full: list[str], include_reasons: bool) -> list[str]:
    return full if include_reasons else [h for h in full if h not in SENSITIVE_HEADERS]


def _local(value: dt.datetime | None, tz) -> dt.datetime | None:
    """Excel has no time zones: write wall-clock time in the office zone as a naive datetime."""
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite (tests) returns naive values; they are UTC
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(tz).replace(tzinfo=None, microsecond=0)


def _plain(raw: bytes | None) -> str:
    """Decrypt one stored value; NULL (purged by the retention job) -> ''; undecryptable -> a placeholder."""
    if raw is None:
        return ""
    try:
        return decrypt_bytes(raw).decode("utf-8")
    except (DataDecryptionError, UnicodeDecodeError):
        return UNREADABLE


def _num(v: Decimal | int | float | None) -> float | int | None:
    return float(v) if isinstance(v, Decimal) else v


def _user_filters(q, scope: ExportScope, filters: ExportFilters, user_id_column):
    """Scope first (mandatory), then the optional department / employee filters (they can only narrow it)."""
    q = scope.restrict(q, user_id_column)
    if filters.department_id is not None:
        q = q.where(User.department_id == filters.department_id)
    if filters.user_id is not None:
        q = q.where(User.id == filters.user_id)
    return q


async def _office_tz(session: AsyncSession):
    return att.office_tz(await att.load_settings(session))


# ---------------------------------------------------------------- Attendance
def attendance_sheet(
    session: AsyncSession, scope: ExportScope, filters: ExportFilters, d_from: dt.date, d_to: dt.date
) -> SheetSpec:
    def filtered(q):
        q = q.join(User, User.id == Attendance.user_id).where(Attendance.work_date >= d_from, Attendance.work_date <= d_to)
        return _user_filters(q, scope, filters, Attendance.user_id)

    async def count() -> int:
        return int((await session.execute(filtered(select(func.count()).select_from(Attendance)))).scalar_one())

    async def rows() -> AsyncIterator[tuple]:
        tz = await _office_tz(session)
        q = filtered(
            select(
                Attendance.work_date,
                User.employee_id,
                User.full_name,
                Department.name,
                Shift.name,
                Attendance.check_in_at,
                Attendance.check_out_at,
                Attendance.status,
                Attendance.minutes_late,
                Attendance.worked_minutes,
                Attendance.check_in_within_fence,
                ClientLocation.name,
            ).select_from(Attendance)
        )
        q = (
            q.outerjoin(Department, Department.id == User.department_id)
            .outerjoin(Shift, Shift.id == Attendance.shift_id)
            .outerjoin(ClientLocation, ClientLocation.id == Attendance.client_location_id)
            .order_by(Attendance.work_date, User.employee_id)
        )
        result = await session.stream(q.execution_options(yield_per=BATCH))
        async for day, eid, name, dept, shift, cin, cout, status, late, worked, fence, site in result:
            yield (
                day,
                eid,
                name,
                dept or "",
                shift or "",
                _local(cin, tz),
                _local(cout, tz),
                status,
                late,
                None if worked is None else round(worked / 60, 2),
                "" if fence is None else ("Yes" if fence else "No"),
                site or "",
            )

    return SheetSpec("Attendance", ATTENDANCE_HEADERS, count, rows, get_settings().export_max_rows)


# ---------------------------------------------------------------- Late
def late_sheet(session: AsyncSession, scope: ExportScope, filters: ExportFilters, d_from: dt.date, d_to: dt.date) -> SheetSpec:
    def filtered(q):
        q = q.join(User, User.id == LateRecord.user_id).where(LateRecord.work_date >= d_from, LateRecord.work_date <= d_to)
        return _user_filters(q, scope, filters, LateRecord.user_id)

    async def count() -> int:
        return int((await session.execute(filtered(select(func.count()).select_from(LateRecord)))).scalar_one())

    async def rows() -> AsyncIterator[tuple]:
        tz = await _office_tz(session)
        q = filtered(
            select(
                LateRecord.work_date,
                User.employee_id,
                User.full_name,
                Department.name,
                LateRecord.shift_start,
                LateRecord.check_in_at,
                LateRecord.minutes_late,
            ).select_from(LateRecord)
        )
        q = q.outerjoin(Department, Department.id == User.department_id).order_by(LateRecord.work_date, User.employee_id)
        result = await session.stream(q.execution_options(yield_per=BATCH))
        async for day, eid, name, dept, start, cin, late in result:
            yield (day, eid, name, dept or "", start, _local(cin, tz), late)

    return SheetSpec("Late", LATE_HEADERS, count, rows, get_settings().export_max_rows)


# ---------------------------------------------------------------- Leave
def leave_sheet(session: AsyncSession, scope: ExportScope, filters: ExportFilters, d_from: dt.date, d_to: dt.date) -> SheetSpec:
    def filtered(q):
        # requests overlapping the range
        q = q.join(User, User.id == LeaveRequest.user_id).where(LeaveRequest.from_date <= d_to, LeaveRequest.to_date >= d_from)
        return _user_filters(q, scope, filters, LeaveRequest.user_id)

    async def count() -> int:
        return int((await session.execute(filtered(select(func.count()).select_from(LeaveRequest)))).scalar_one())

    inc = filters.include_reasons

    async def rows() -> AsyncIterator[tuple]:
        tz = await _office_tz(session)
        approver = aliased(User)
        cols = [
            LeaveRequest.id,
            User.employee_id,
            User.full_name,
            Department.name,
            LeaveType.name,
            LeaveRequest.from_date,
            LeaveRequest.to_date,
            LeaveRequest.days,
            LeaveRequest.half_day,
            LeaveRequest.status,
        ]
        if inc:
            cols.append(type_coerce(LeaveRequest.reason, LargeBinary))
        cols.append(approver.full_name)
        if inc:
            cols.append(type_coerce(LeaveRequest.decision_comment, LargeBinary))
        cols += [LeaveRequest.created_at, LeaveRequest.decided_at]
        q = filtered(select(*cols).select_from(LeaveRequest))
        q = (
            q.outerjoin(Department, Department.id == User.department_id)
            .outerjoin(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
            .outerjoin(approver, approver.id == LeaveRequest.approver_id)
            .order_by(LeaveRequest.from_date, User.employee_id, LeaveRequest.id)
        )
        result = await session.stream(q.execution_options(yield_per=BATCH))
        async for r in result:
            rid, eid, name, dept, ltype, f, t, days, half, status, *rest = r
            if inc:
                reason, appr, comment, applied, decided = rest
            else:
                appr, applied, decided = rest
            out = [str(rid), eid, name, dept or "", ltype or "", f, t, _num(days), "Yes" if half else "No", status]
            if inc:
                out.append(_plain(reason))
            out.append(appr or "")
            if inc:
                out.append(_plain(comment))
            out += [_local(applied, tz), _local(decided, tz)]
            yield tuple(out)

    return SheetSpec("Leave", headers_for(LEAVE_HEADERS, inc), count, rows, get_settings().export_max_rows)


# ---------------------------------------------------------------- Permission
def permission_sheet(
    session: AsyncSession, scope: ExportScope, filters: ExportFilters, d_from: dt.date, d_to: dt.date
) -> SheetSpec:
    def filtered(q):
        q = q.join(User, User.id == PermissionRequest.user_id).where(
            PermissionRequest.request_date >= d_from, PermissionRequest.request_date <= d_to
        )
        return _user_filters(q, scope, filters, PermissionRequest.user_id)

    async def count() -> int:
        return int((await session.execute(filtered(select(func.count()).select_from(PermissionRequest)))).scalar_one())

    inc = filters.include_reasons

    async def rows() -> AsyncIterator[tuple[Any, ...]]:
        approver = aliased(User)
        cols = [
            PermissionRequest.id,
            User.employee_id,
            User.full_name,
            Department.name,
            PermissionRequest.request_date,
            PermissionRequest.from_time,
            PermissionRequest.to_time,
            PermissionRequest.minutes,
            PermissionRequest.status,
        ]
        if inc:
            cols.append(type_coerce(PermissionRequest.reason, LargeBinary))
        cols.append(approver.full_name)
        if inc:
            cols.append(type_coerce(PermissionRequest.decision_comment, LargeBinary))
        q = filtered(select(*cols).select_from(PermissionRequest))
        q = (
            q.outerjoin(Department, Department.id == User.department_id)
            .outerjoin(approver, approver.id == PermissionRequest.approver_id)
            .order_by(PermissionRequest.request_date, User.employee_id, PermissionRequest.id)
        )
        result = await session.stream(q.execution_options(yield_per=BATCH))
        async for r in result:
            rid, eid, name, dept, day, f, t, minutes, status, *rest = r
            if inc:
                reason, appr, comment = rest
            else:
                (appr,) = rest
            out = [str(rid), eid, name, dept or "", day, f, t, minutes, status]
            if inc:
                out.append(_plain(reason))
            out.append(appr or "")
            if inc:
                out.append(_plain(comment))
            yield tuple(out)

    return SheetSpec("Permission", headers_for(PERMISSION_HEADERS, inc), count, rows, get_settings().export_max_rows)


# ---------------------------------------------------------------- On Duty
def on_duty_sheet(session: AsyncSession, scope: ExportScope, filters: ExportFilters, d_from: dt.date, d_to: dt.date) -> SheetSpec:
    def filtered(q):
        # requests overlapping the range
        q = q.join(User, User.id == OnDutyRequest.user_id).where(OnDutyRequest.from_date <= d_to, OnDutyRequest.to_date >= d_from)
        return _user_filters(q, scope, filters, OnDutyRequest.user_id)

    async def count() -> int:
        return int((await session.execute(filtered(select(func.count()).select_from(OnDutyRequest)))).scalar_one())

    inc = filters.include_reasons

    async def rows() -> AsyncIterator[tuple]:
        tz = await _office_tz(session)
        approver = aliased(User)
        cols = [
            OnDutyRequest.id,
            User.employee_id,
            User.full_name,
            Department.name,
            ClientLocation.name,
            OnDutyRequest.from_date,
            OnDutyRequest.to_date,
            OnDutyRequest.days,
            OnDutyRequest.status,
        ]
        if inc:
            cols.append(type_coerce(OnDutyRequest.reason, LargeBinary))
        cols.append(approver.full_name)
        if inc:
            cols.append(type_coerce(OnDutyRequest.decision_comment, LargeBinary))
        cols += [OnDutyRequest.created_at, OnDutyRequest.decided_at]
        q = filtered(select(*cols).select_from(OnDutyRequest))
        q = (
            q.outerjoin(Department, Department.id == User.department_id)
            .outerjoin(ClientLocation, ClientLocation.id == OnDutyRequest.client_location_id)
            .outerjoin(approver, approver.id == OnDutyRequest.approver_id)
            .order_by(OnDutyRequest.from_date, User.employee_id, OnDutyRequest.id)
        )
        result = await session.stream(q.execution_options(yield_per=BATCH))
        async for r in result:
            rid, eid, name, dept, site, f, t, days, status, *rest = r
            if inc:
                reason, appr, comment, applied, decided = rest
            else:
                appr, applied, decided = rest
            out = [str(rid), eid, name, dept or "", site or "", f, t, days, status]
            if inc:
                out.append(_plain(reason))
            out.append(appr or "")
            if inc:
                out.append(_plain(comment))
            out += [_local(applied, tz), _local(decided, tz)]
            yield tuple(out)

    return SheetSpec("On Duty", headers_for(ON_DUTY_HEADERS, inc), count, rows, get_settings().export_max_rows)
