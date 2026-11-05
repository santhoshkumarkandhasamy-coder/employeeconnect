"""Leave encashment (plan 15 s.3, contract 17.5): HR turns unused leave days into pay.

Creating an encashment reserves the days at once (`leave_balances.encashed`, so leave requests, the balance screens and the
quota checks all see them) and fixes the daily rate and the amount. It is then a payable source row of the component engine
and lands on the payslip of its month as LEAVE_ENCASH (not an ESI wage). Cancelling a pending encashment gives the days back.

Lock order (P7): run row (target month) -> leave_balances -> leave_encashments.
Amounts, rates and reasons are encrypted columns; the audit log carries leave type code, year, days and periods only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import LeaveBalance, LeaveEncashment, LeaveType, SalaryStructure
from ..schemas.payroll import EncashmentCreate
from . import admin_security
from . import leave as leave_svc
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_items as items_svc
from . import payroll_runs as runs
from . import payroll_settings as pset
from . import payroll_structures as structs_svc

ENTITY = "payroll_encashment"
LIVE = ("pending", "processing", "paid")
ENC = LeaveEncashment


def _d(x: Any) -> Decimal:
    return Decimal(str(x))


def _bad(field: str, message: str) -> AppError:
    return AppError("VALIDATION_ERROR", message, details=[{"field": field, "message": message}])


# ============================================================================================ rate (pure)
def daily_rate(monthly: Decimal, divisor: int) -> Decimal:
    """monthly basic (or gross) / divisor, rounded to paise."""
    return calc.q2(monthly / Decimal(divisor))


def amount_for(days: Decimal, rate: Decimal) -> Decimal:
    return calc.q2(days * rate)


# ============================================================================================ checks
async def _leave_type(session: AsyncSession, leave_type_id: uuid.UUID) -> LeaveType:
    lt = await session.get(LeaveType, leave_type_id)
    if lt is None:
        raise AppError("NOT_FOUND", "Leave type not found")
    if not lt.encashable:
        raise AppError("NOT_ENCASHABLE", "This leave type cannot be encashed")
    return lt


def _check_year(year: int, today: dt.date) -> None:
    if year not in (today.year, today.year - 1):
        raise _bad("year", "only the current or the previous year can be encashed")


async def _rate_for(session: AsyncSession, user_id: uuid.UUID, period: dt.date) -> tuple[Decimal, str, int]:
    """(daily rate, basis, divisor) from the structure in force for the payout month."""
    structures = list((await session.execute(select(SalaryStructure).where(SalaryStructure.user_id == user_id))).scalars())
    s = calc.structure_for([(x.effective_from, x) for x in structures], period)
    if s is None:
        raise AppError("STRUCTURE_MISSING", "The employee has no salary structure for the payout month", status=422)
    basis = str(await pset.current_value(session, "payroll.encashment_basis"))
    divisor = int(await pset.current_value(session, "payroll.encashment_divisor"))
    data = structs_svc.data_of(s)
    monthly = data.monthly_gross if basis == "gross" else data.basic
    return daily_rate(monthly, divisor), basis, divisor


async def _available(
    session: AsyncSession, user_id: uuid.UUID, lt: LeaveType, year: int, balance: LeaveBalance | None, initial: Decimal
) -> Decimal:
    """quota - used - pending - encashed - encash_min_balance, capped by encash_max_days_per_year minus what was encashed."""
    d1 = leave_svc.d1
    if balance is None:
        quota, used, pending, encashed = d1(initial), d1(0), d1(0), d1(0)
    else:
        quota, used, pending, encashed = d1(balance.quota), d1(balance.used), d1(balance.pending), d1(balance.encashed)
    avail = quota - used - pending - encashed - d1(lt.encash_min_balance)
    if lt.encash_max_days_per_year is not None:
        rows = (
            await session.execute(
                select(ENC.days).where(ENC.user_id == user_id, ENC.leave_type_id == lt.id, ENC.year == year, ENC.status.in_(LIVE))
            )
        ).scalars()
        avail = min(avail, d1(lt.encash_max_days_per_year) - sum((d1(x) for x in rows), Decimal(0)))
    return max(Decimal("0.0"), avail)


def _insufficient(available: Decimal) -> AppError:
    return AppError(
        "INSUFFICIENT_BALANCE", "Not enough leave balance to encash",
        details=[{"field": "available_days", "message": str(available)}],
    )  # fmt: skip


# ============================================================================================ quote
async def quote(
    session: AsyncSession, user_id: uuid.UUID, leave_type_id: uuid.UUID, year: int, days: Decimal | None, raw_period: str
) -> dict[str, Any]:
    today = await runs._today(session)
    period = comps.parse_target_period(raw_period, today)
    _check_year(year, today)
    user = await structs_svc.get_user_or_404(session, user_id)
    lt = await _leave_type(session, leave_type_id)
    await items_svc._eligible_user(session, user_id, period)
    await comps.check_target_period(session, [user_id], period)
    bal = (
        await session.execute(
            select(LeaveBalance).where(
                LeaveBalance.user_id == user_id, LeaveBalance.leave_type_id == lt.id, LeaveBalance.year == year
            )
        )
    ).scalar_one_or_none()
    available = await _available(session, user_id, lt, year, bal, leave_svc.initial_quota(lt, user, year))
    use = leave_svc.d1(days) if days is not None else available
    if days is not None and (use <= 0 or (use * 2) != (use * 2).to_integral_value()):
        raise _bad("days", "must be greater than 0, in steps of 0.5")
    if use > available:
        raise _insufficient(available)
    rate, basis, divisor = await _rate_for(session, user_id, period)
    return {
        "leave_type": {"id": str(lt.id), "code": lt.code, "name": lt.name},
        "year": year,
        "available_days": leave_svc.num(available),
        "days": leave_svc.num(use),
        "daily_rate": calc.money_str(rate),
        "amount": calc.money_str(amount_for(use, rate)),
        "basis": basis,
        "divisor": divisor,
        "period": pc.period_str(period),
    }


# ============================================================================================ output
async def _out_many(session: AsyncSession, rows: list[LeaveEncashment]) -> list[dict[str, Any]]:
    people = await items_svc._people(session, {x for r in rows for x in (r.user_id, r.created_by)})
    states = await comps.row_states(session, rows)
    lts = {}
    ids = {r.leave_type_id for r in rows}
    if ids:
        lts = {lt.id: lt for lt in (await session.execute(select(LeaveType).where(LeaveType.id.in_(ids)))).scalars()}
    out = []
    for r in rows:
        lt = lts.get(r.leave_type_id)
        out.append(
            {
                "id": str(r.id),
                "user": pc.user_out(people.get(r.user_id)),
                "leave_type": {"id": str(lt.id), "code": lt.code, "name": lt.name} if lt else None,
                "year": r.year,
                "days": leave_svc.num(r.days),
                "daily_rate": calc.money_str(_d(r.daily_rate)),
                "amount": calc.money_str(_d(r.amount)),
                "period": pc.period_str(r.period),
                "source": r.source,
                "status": r.status,
                "run_status": states[r.id][0],
                "flags": states[r.id][1],
                "payslip_id": str(r.payslip_id) if r.payslip_id else None,
                "version": r.version,
                "created_at": pc.iso(r.created_at),
                "created_by": pc.user_out(people.get(r.created_by)),
            }
        )
    return out


async def _or_404(session: AsyncSession, enc_id: uuid.UUID, *, lock: bool = False) -> LeaveEncashment:
    q = select(ENC).where(ENC.id == enc_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = (await session.execute(q)).scalar_one_or_none()
    if row is None:
        raise AppError("NOT_FOUND", "Encashment not found")
    return row


async def list_encashments(
    session: AsyncSession, *, user_id: uuid.UUID | None, year: int | None, status: str | None, page: PageParams
) -> dict[str, Any]:
    cond = []
    if user_id:
        cond.append(ENC.user_id == user_id)
    if year:
        cond.append(ENC.year == year)
    if status:
        cond.append(ENC.status == status)
    total = (await session.execute(select(func.count()).select_from(ENC).where(*cond))).scalar_one()
    q = select(ENC).where(*cond).order_by(ENC.created_at.desc(), ENC.id).offset(page.offset).limit(page.page_size)
    return make_page(await _out_many(session, list((await session.execute(q)).scalars())), page, total)


# ============================================================================================ create / cancel
async def create(session: AsyncSession, redis: Redis, actor: Principal, body: EncashmentCreate) -> dict[str, Any]:
    today = await runs._today(session)
    period = comps.parse_target_period(body.period, today)
    await pc.forbid_self(session, actor, body.user_id, entity_type=ENTITY, entity_id=body.user_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.encashment.create")
    _check_year(body.year, today)
    user = await items_svc._eligible_user(session, body.user_id, period)
    lt = await _leave_type(session, body.leave_type_id)
    rate, _basis, _divisor = await _rate_for(session, user.id, period)
    run_status = await comps.check_target_period(session, [user.id], period, lock=True)  # run row first (P7)
    comps.check_caps((await comps.live_counts(session, [user.id], period))[user.id])
    bal = await leave_svc.get_balance(session, user, lt, body.year, lock=True)  # then the balance row
    days = leave_svc.d1(body.days)
    available = await _available(session, user.id, lt, body.year, bal, bal.quota)
    if days > available:
        raise _insufficient(available)
    bal.encashed = leave_svc.d1(bal.encashed) + days
    row = LeaveEncashment(
        user_id=user.id, leave_type_id=lt.id, year=body.year, days=days, daily_rate=calc.money_str(rate),
        amount=calc.money_str(amount_for(days, rate)), period=period, source=body.source, reason=body.reason,
        status="pending", version=1, created_by=actor.id, updated_by=actor.id,
    )  # fmt: skip
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:  # the CHECK used + pending + encashed <= quota is the safety net behind the lock
        await session.rollback()
        raise _insufficient(available) from None
    comps.add_event(session, item_type="encashment", item=row, action="create", actor_id=actor.id,
                    detail={"reason": body.reason, "days": str(days), "leave_type": lt.code, "year": body.year})  # fmt: skip
    audit(session, action="payroll.encashment.create", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"user_id": str(user.id), "leave_type": lt.code, "year": body.year, "days": leave_svc.num(days),
                 "period": pc.period_str(period), "source": body.source, "run_status": run_status})  # fmt: skip
    rid = row.id
    await pc.commit(session)
    return (await _out_many(session, [await _or_404(session, rid)]))[0]


async def cancel(session: AsyncSession, actor: Principal, enc_id: uuid.UUID, reason: str | None) -> dict[str, Any]:
    peek = await _or_404(session, enc_id)
    await pc.forbid_self(session, actor, peek.user_id, entity_type=ENTITY, entity_id=enc_id)
    await items_svc._lock_runs(session, [peek.period])  # run row, then the balance, then the encashment (P7)
    bal = (
        await session.execute(
            select(LeaveBalance)
            .where(
                LeaveBalance.user_id == peek.user_id,
                LeaveBalance.leave_type_id == peek.leave_type_id,
                LeaveBalance.year == peek.year,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    row = await _or_404(session, enc_id, lock=True)
    if row.status != "pending":
        raise AppError("ITEM_LOCKED", f"The encashment is {row.status} and can no longer be cancelled")
    if bal is None or leave_svc.d1(bal.encashed) < leave_svc.d1(row.days):
        raise AppError("CONFLICT", "The leave balance is inconsistent; contact support")
    bal.encashed = leave_svc.d1(bal.encashed) - leave_svc.d1(row.days)
    row.status, row.updated_by, row.version = "cancelled", actor.id, row.version + 1
    await pc.flush(session)
    comps.add_event(session, item_type="encashment", item=row, action="cancel", actor_id=actor.id,
                    detail={"reason": reason} if reason else None)  # fmt: skip
    audit(session, action="payroll.encashment.cancel", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"user_id": str(row.user_id), "year": row.year, "days": leave_svc.num(row.days),
                 "period": pc.period_str(row.period)})  # fmt: skip
    await pc.commit(session)
    return (await _out_many(session, [await _or_404(session, enc_id)]))[0]


# ============================================================================================ component engine provider
def _lines(rows: list[LeaveEncashment]) -> list[dict]:
    return [
        {
            "code": "LEAVE_ENCASH",
            "label": f"Leave encashment ({leave_svc.num(r.days)} d)",
            "amount": calc.money_str(_d(r.amount)),
            "sources": [{"type": "encashment", "id": str(r.id)}],
        }
        for r in rows
    ]


comps.register(comps.Provider("encashment", LeaveEncashment, _lines, comps.LIVE))
