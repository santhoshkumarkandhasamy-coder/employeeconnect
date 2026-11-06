from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from ..core import rate_limit
from ..core.config import get_settings
from ..core.db import get_session
from ..core.errors import AppError
from ..core.pagination import PageParams
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..schemas.attendance import HolidayIn, HolidayPatch, ManualAttendanceIn, PunchRequest
from ..services import attendance as svc
from ..services import late as late_svc

router = APIRouter(tags=["attendance"])


async def _punch_limit(redis: Redis, actor: Principal) -> None:
    await rate_limit.enforce(redis, [(f"rl:attendance:{actor.id}", 10, 60)])  # 10 / min / user
    await rate_limit.gate_check(redis, f"rl:punchfail:{actor.id}", get_settings().punch_fail_limit)


async def _run_punch(redis: Redis, actor: Principal, call):
    """Failed (rejected) punches are counted separately and trigger a cool-down: it throttles geofence probing."""
    st = get_settings()
    try:
        result = await call()
    except AppError as exc:
        if exc.code in svc.PUNCH_FAIL_CODES:
            await rate_limit.gate_fail(redis, f"rl:punchfail:{actor.id}", st.punch_fail_window_seconds)
        raise
    await rate_limit.gate_reset(redis, f"rl:punchfail:{actor.id}")
    return result


@router.post("/attendance/check-in", status_code=201)
async def check_in(
    body: PunchRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission()),
):
    await _punch_limit(redis, actor)
    return await _run_punch(redis, actor, lambda: svc.check_in(session, actor, body))


@router.post("/attendance/check-out")
async def check_out(
    body: PunchRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission()),
):
    await _punch_limit(redis, actor)
    return await _run_punch(redis, actor, lambda: svc.check_out(session, actor, body))


@router.get("/attendance/today")
async def today(session: AsyncSession = Depends(get_session), actor: Principal = Depends(require_permission())):
    return await svc.today(session, actor)


@router.get("/attendance/calendar")
async def calendar(
    month: str | None = Query(None, max_length=7),
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission()),
):
    return await svc.calendar(session, actor, month)


@router.get("/attendance/summary")
async def summary(
    month: str | None = Query(None, max_length=7),
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission()),
):
    return await svc.summary(session, actor, month)


@router.get("/attendance/team")
async def team(
    params: PageParams = Depends(),
    date: dt.date | None = None,
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    department_id: uuid.UUID | None = None,
    employee_id: str | None = Query(None, max_length=32),
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("attendance.view_team", "attendance.view_all")),
):
    return await svc.team(
        session, actor, params, on_date=date, d_from=from_, d_to=to, department_id=department_id, employee_id=employee_id
    )


_MANUAL = Depends(require_permission("attendance.manual"))


@router.get("/admin/attendance/{user_id}/{work_date}")
async def manual_get(
    user_id: uuid.UUID, work_date: dt.date, session: AsyncSession = Depends(get_session), actor: Principal = _MANUAL
):
    return await svc.manual_get(session, actor, user_id, work_date)


@router.put("/admin/attendance/{user_id}/{work_date}")
async def manual_upsert(
    user_id: uuid.UUID,
    work_date: dt.date,
    body: ManualAttendanceIn,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = _MANUAL,
):
    await rate_limit.enforce(redis, [(f"rl:attendance:manual:{actor.id}", 60, 60)], fail_closed=True)
    return await svc.manual_upsert(session, actor, user_id, work_date, body)


@router.delete("/admin/attendance/{user_id}/{work_date}", status_code=204)
async def manual_delete(
    user_id: uuid.UUID, work_date: dt.date, session: AsyncSession = Depends(get_session), actor: Principal = _MANUAL
):
    await svc.manual_delete(session, actor, user_id, work_date)
    return Response(status_code=204)


@router.get("/attendance")
async def history(
    params: PageParams = Depends(),
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission()),
):
    return await svc.history(session, actor, params, from_, to)


@router.get("/holidays")
async def holidays(
    year: int | None = Query(None, ge=2000, le=2200),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission()),
):
    """Read: any authenticated user. Writes need `holiday.manage`."""
    return await svc.list_holidays(session, year)


@router.get("/admin/holidays", deprecated=True)
async def holidays_legacy(
    year: int | None = Query(None, ge=2000, le=2200),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_permission()),
):
    """Deprecated alias of GET /holidays, kept only until the mobile app has migrated."""
    return await svc.list_holidays(session, year)


@router.post("/holidays", status_code=201)
async def holiday_create(
    body: HolidayIn,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("holiday.manage")),
):
    return await svc.create_holiday(session, actor, body)


@router.patch("/holidays/{holiday_id}")
async def holiday_update(
    holiday_id: uuid.UUID,
    body: HolidayPatch,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("holiday.manage")),
):
    return await svc.update_holiday(session, actor, holiday_id, body)


@router.delete("/holidays/{holiday_id}", status_code=204)
async def holiday_delete(
    holiday_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("holiday.manage")),
):
    await svc.delete_holiday(session, actor, holiday_id)


@router.get("/late/summary")
async def late_summary(
    month: str | None = Query(None, max_length=7),
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission()),
):
    return await late_svc.my_summary(session, actor, month)


@router.get("/late/team")
async def late_team(
    params: PageParams = Depends(),
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    department_id: uuid.UUID | None = None,
    employee_id: str | None = Query(None, max_length=32),
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission("late.view_team", "late.view_all")),
):
    return await late_svc.team_late(
        session, actor, params, d_from=from_, d_to=to, department_id=department_id, employee_id=employee_id
    )


@router.get("/late")
async def late_list(
    params: PageParams = Depends(),
    from_: dt.date | None = Query(None, alias="from"),
    to: dt.date | None = None,
    session: AsyncSession = Depends(get_session),
    actor: Principal = Depends(require_permission()),
):
    return await late_svc.my_late(session, actor, params, from_, to)
