"""GET /admin/export (contract section 10): all | attendance | leave | late | permission | food (see services.export.SHEETS)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from ..core import rate_limit
from ..core.audit import audit, safe_eid
from ..core.db import get_session
from ..core.errors import AppError
from ..core.rbac import Principal, require_permission
from ..core.redis import get_redis
from ..services import export as export_svc
from ..services import export_quota, xlsx
from ..services.export import SHEETS  # noqa: F401 - re-exported: other modules register their sheets here

log = logging.getLogger("app.export")
router = APIRouter(prefix="/admin", tags=["export"])
MAX_RANGE_DAYS = 366


@router.get("/export")
async def export(
    type: Literal["all", "attendance", "leave", "late", "permission", "on_duty", "food"] = "all",
    from_: dt.date = Query(..., alias="from"),
    to: dt.date = Query(...),
    department_id: uuid.UUID | None = None,
    employee_id: str | None = Query(None, max_length=32),
    include_reasons: bool = False,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    actor: Principal = Depends(require_permission("export.all", "export.team")),
):
    request_info = {
        "type": type,
        "from": str(from_),
        "to": str(to),
        "department_id": str(department_id) if department_id else None,
        "employee_id": safe_eid(employee_id) if employee_id and employee_id.strip() else None,
        "include_reasons": include_reasons,  # as requested; the success row records what was actually exported
    }
    who = SimpleNamespace(id=actor.id, employee_id=actor.employee_id)  # plain values: survive a session rollback
    built: xlsx.BuiltWorkbook | None = None
    reserved = False
    try:
        await rate_limit.enforce(redis, [(f"rl:export:{actor.id}", 5, 60)], fail_closed=True)
        export_svc.check_dates(from_, to)
        if to < from_:
            raise AppError("VALIDATION_ERROR", "to must be >= from", details=[{"field": "to", "message": "must be >= from"}])
        if (to - from_).days + 1 > MAX_RANGE_DAYS:
            raise AppError("RANGE_TOO_LARGE", f"Range may not exceed {MAX_RANGE_DAYS} days")
        names = export_svc.types_for(type)
        export_svc.require_type_permissions(actor, names)  # per-type permission, on top of export.all / export.team
        with_reasons = export_svc.require_sensitive(actor, names, include_reasons)  # export.sensitive, else 403
        scope = await export_svc.resolve_scope(session, actor, names)  # computed ONCE here; never client-chosen
        eff_from = export_svc.effective_from(scope, from_)  # team scope: clamped to the policy window
        filters = await export_svc.resolve_filters(session, scope, department_id, employee_id, with_reasons)
        await export_quota.reserve(redis, who.id)  # daily per-user ceiling
        reserved = True
        await export_svc.apply_statement_timeout(session)
        specs = export_svc.build_specs(names, session, scope, filters, eff_from, to)
        try:
            built = await xlsx.build_workbook(specs)
        except OverflowError:
            raise AppError("RANGE_TOO_LARGE", "Too many rows or the export took too long; narrow the filters") from None
        total_rows = sum(built.counts.values())
        daily_exports, daily_rows = await export_quota.record_rows(redis, who.id, total_rows)
        audit(
            session,
            action="export.run",
            actor=actor,
            entity_type="export",
            after={
                **request_info,
                "include_reasons": with_reasons,
                "effective_from": str(eff_from),
                "scope": "team" if scope.is_team else "all",
                "rows": built.counts,  # per sheet
                "total_rows": total_rows,
                "daily_exports": daily_exports,
                "daily_rows": daily_rows,
            },
        )
        await session.commit()
    except BaseException as exc:
        if built is not None:
            built.close()
        if reserved:
            await export_quota.refund(redis, who.id)  # nothing was delivered
        if isinstance(exc, AppError):
            await _audit_refusal(session, who, request_info, exc)  # review 08 L1
        raise
    name = f"employee-connect-{type}-{from_}_{to}.xlsx"
    return StreamingResponse(
        xlsx.iter_file(built),
        media_type=xlsx.XLSX_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(built.close),  # idempotent; covers a generator that is never exhausted
    )


async def _audit_refusal(session: AsyncSession, actor: SimpleNamespace, info: dict, exc: AppError) -> None:
    """Denied / failed attempts are audited too (own commit, before the error goes out). Never raises: the original error
    must reach the client even if the audit write fails (that is logged)."""
    try:
        await session.rollback()
        audit(
            session,
            action="export.run",
            actor=actor,
            entity_type="export",
            outcome="failure",
            after={**info, "result": "denied" if exc.status in (401, 403) else "failed", "code": exc.code, "status": exc.status},
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        log.exception("could not audit a refused export")
