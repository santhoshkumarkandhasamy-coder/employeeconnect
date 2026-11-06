"""Export registry, scope and per-type permissions (plan 10, review 04 M3).

The ROUTER decides who may see which employees (`ExportScope`) exactly once and hands it to every sheet builder as a
mandatory positional argument. A builder cannot be registered without it (checked by tests via the signature), and each
builder must apply `scope.restrict(query, <user id column>)` - there is no optional keyword to forget.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import utcnow
from ..core.config import get_settings
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import Department, User
from . import export_sheets, food_export
from .xlsx import SheetSpec

EXPORT_MIN_DATE = dt.date(2000, 1, 1)
EXPORT_MAX_DATE = dt.date(2200, 12, 31)


@dataclass(frozen=True)
class ExportScope:
    """The employees whose data the caller may export. Built only via `everyone()` (export.all) or `team()`."""

    user_ids: frozenset[uuid.UUID] | None  # None = unrestricted

    @classmethod
    def everyone(cls) -> ExportScope:
        return cls(None)

    @classmethod
    def team(cls, ids: set[uuid.UUID] | frozenset[uuid.UUID]) -> ExportScope:
        return cls(frozenset(ids))

    @property
    def is_team(self) -> bool:
        return self.user_ids is not None

    def allows(self, user_id: uuid.UUID) -> bool:
        return self.user_ids is None or user_id in self.user_ids

    def restrict(self, query: Select, user_id_column: Any) -> Select:
        return query if self.user_ids is None else query.where(user_id_column.in_(self.user_ids))


@dataclass(frozen=True)
class ExportFilters:
    department_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None  # resolved from the business employee_id by `resolve_filters`
    # NOT a row filter: it travels with the filters so every builder sees it without a signature change. Only the router
    # sets it, and only after checking the `export.sensitive` permission (review 08 M2).
    include_reasons: bool = False


# builder(session, scope, filters, d_from, d_to) -> SheetSpec. `scope` is positional and mandatory for every builder.
SheetBuilder = Callable[[AsyncSession, ExportScope, ExportFilters, dt.date, dt.date], SheetSpec]
SHEETS: dict[str, SheetBuilder] = {
    "attendance": export_sheets.attendance_sheet,
    "leave": export_sheets.leave_sheet,
    "late": export_sheets.late_sheet,
    "permission": export_sheets.permission_sheet,
    "food": food_export.food_sheet,
}

# Holding ANY of the listed permissions allows the type (on top of export.all / export.team, which decide the scope).
TYPE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "attendance": ("attendance.view_all", "attendance.view_team"),
    "leave": ("request.view_all", "request.view_team"),
    "late": ("late.view_all", "late.view_team"),
    "permission": ("request.view_all", "request.view_team"),
    "food": ("food.view_all",),
}
SENSITIVE_PERMISSION = "export.sensitive"  # needed for include_reasons=true
SENSITIVE_TYPES = ("leave", "permission")  # the sheets that have Reason / Decision Comment columns
ALL_TYPES = ("attendance", "leave", "late", "permission")  # sheets of type=all (food excluded)


def types_for(requested: str) -> list[str]:
    names = list(ALL_TYPES) if requested == "all" else [requested]
    missing = [n for n in names if n not in SHEETS]
    if missing:
        raise AppError(
            "VALIDATION_ERROR",
            f"Export type '{requested}' is not available yet",
            details=[{"field": "type", "message": "unsupported"}],
        )
    return names


def require_type_permissions(actor: Principal, names: list[str]) -> None:
    for n in names:
        if not any(actor.has(p) for p in TYPE_PERMISSIONS[n]):
            raise AppError("FORBIDDEN", "You do not have permission to export this data")


def today() -> dt.date:
    return utcnow().date()


def require_sensitive(actor: Principal, names: list[str], include_reasons: bool) -> bool:
    """include_reasons=true needs `export.sensitive`; otherwise 403 (never silently downgraded). Returns the effective flag
    (False when no requested sheet has reason columns, so the audit row does not claim something that did not happen)."""
    if not include_reasons:
        return False
    if not actor.has(SENSITIVE_PERMISSION):
        raise AppError("FORBIDDEN", "You do not have permission to export reasons and comments")
    return any(n in SENSITIVE_TYPES for n in names)


def _months_back(day: dt.date, months: int) -> dt.date:
    y, m = divmod(day.year * 12 + (day.month - 1) - months, 12)
    m += 1
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(y, m, min(day.day, last))


def effective_from(scope: ExportScope, d_from: dt.date) -> dt.date:
    """Team scope only knows the CURRENT manager_id (no reporting history), so it may not reach further back than the
    configured window (EXPORT_TEAM_WINDOW_MONTHS, default 12; 0 = off). export.all is never clamped."""
    months = get_settings().export_team_window_months
    if not scope.is_team or months <= 0:
        return d_from
    return max(d_from, _months_back(today(), months))


async def apply_statement_timeout(session: AsyncSession) -> None:
    """PostgreSQL only: cap every statement of this transaction a little above the build budget, so a runaway count or
    streaming query cannot outlive the request (the asyncio timeout alone only abandons the client side)."""
    if session.get_bind().dialect.name != "postgresql":
        return
    ms = int((get_settings().export_timeout_s + 5) * 1000)
    await session.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": str(ms)})


def check_dates(d_from: dt.date, d_to: dt.date) -> None:
    for field, d in (("from", d_from), ("to", d_to)):
        if not EXPORT_MIN_DATE <= d <= EXPORT_MAX_DATE:
            raise AppError("INVALID_DATE", "Date out of range", details=[{"field": field, "message": "must be 2000-2200"}])


async def resolve_scope(session: AsyncSession, actor: Principal, names: list[str]) -> ExportScope:
    """Everyone only if the caller holds export.all AND the `.view_all` permission of every requested type; otherwise the
    caller's direct reports (so export.all together with only `*.view_team` never widens what the caller may read)."""
    if actor.has("export.all") and all(any(p.endswith(".view_all") and actor.has(p) for p in TYPE_PERMISSIONS[n]) for n in names):
        return ExportScope.everyone()
    ids = (await session.execute(select(User.id).where(User.manager_id == actor.id, User.deleted_at.is_(None)))).scalars().all()
    return ExportScope.team(set(ids))


async def resolve_filters(
    session: AsyncSession,
    scope: ExportScope,
    department_id: uuid.UUID | None,
    employee_id: str | None,
    include_reasons: bool = False,
) -> ExportFilters:
    """export.all: unknown department/employee -> 404. Team scope: unknown AND out-of-scope ids give the SAME 403, so a
    manager cannot probe which employee ids or departments exist (review 08 L2)."""
    denied = AppError("FORBIDDEN", "You do not have permission to export this employee or department")
    if department_id is not None:
        if scope.is_team:
            q = select(func.count()).select_from(User).where(User.department_id == department_id, User.id.in_(scope.user_ids))
            if not (await session.execute(q)).scalar_one():
                raise denied  # unknown department, or none of the team is in it
        elif (await session.get(Department, department_id)) is None:
            raise AppError("NOT_FOUND", "Department not found")
    user_id = None
    if employee_id and employee_id.strip():
        user_id = (
            await session.execute(select(User.id).where(User.employee_id == employee_id.strip().upper()))
        ).scalar_one_or_none()
        if scope.is_team:
            if user_id is None or not scope.allows(user_id):
                raise denied
        elif user_id is None:
            raise AppError("NOT_FOUND", "Employee not found")
    return ExportFilters(department_id=department_id, user_id=user_id, include_reasons=include_reasons)


def build_specs(
    names: list[str], session: AsyncSession, scope: ExportScope, filters: ExportFilters, d_from: dt.date, d_to: dt.date
) -> list[SheetSpec]:
    return [SHEETS[n](session, scope, filters, d_from, d_to) for n in names]
