"""Food report sheet for the Excel export (plan 05 / 10). `food_sheet()` returns a SheetSpec for services.xlsx, so the
export module can combine it with other sheets. Scope is a mandatory argument (see services/export.py)."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..models import Department, FoodEnrollment, User
from .xlsx import SheetSpec

if TYPE_CHECKING:
    from .export import ExportFilters, ExportScope

FOOD_HEADERS = ["Date", "Employee ID", "Name", "Department", "Need Food"]
EXPORT_MAX_ROWS = 200_000  # default; the effective cap is EXPORT_MAX_ROWS from the environment
BATCH = 1000


def _filtered(q, scope: ExportScope, filters: ExportFilters, d_from: dt.date, d_to: dt.date):
    q = q.join(User, User.id == FoodEnrollment.user_id).where(FoodEnrollment.date >= d_from, FoodEnrollment.date <= d_to)
    q = scope.restrict(q, FoodEnrollment.user_id)  # the allowed-employee restriction; applied to every query of the sheet
    if filters.department_id is not None:
        q = q.where(User.department_id == filters.department_id)
    if filters.user_id is not None:
        q = q.where(User.id == filters.user_id)
    return q


def food_sheet(session: AsyncSession, scope: ExportScope, filters: ExportFilters, d_from: dt.date, d_to: dt.date) -> SheetSpec:
    async def count() -> int:
        q = _filtered(select(func.count()).select_from(FoodEnrollment), scope, filters, d_from, d_to)
        return int((await session.execute(q)).scalar_one())

    async def rows() -> AsyncIterator[tuple]:
        """Stored answers only (Yes/No); unanswered days have no row. Streamed from the DB in batches (yield_per)."""
        q = _filtered(
            select(FoodEnrollment.date, User.employee_id, User.full_name, Department.name, FoodEnrollment.need_food).select_from(
                FoodEnrollment
            ),
            scope,
            filters,
            d_from,
            d_to,
        )
        q = q.outerjoin(Department, Department.id == User.department_id).order_by(FoodEnrollment.date, User.employee_id)
        result = await session.stream(q.execution_options(yield_per=BATCH))
        async for day, eid, name, dept, need in result:
            yield (day, eid, name, dept or "", "Yes" if need else "No")

    return SheetSpec("Food", FOOD_HEADERS, count, rows, get_settings().export_max_rows)
