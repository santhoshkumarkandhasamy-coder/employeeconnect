"""Loans and salary advances (plan 15 s.3, contract 17.3): interest-free, fixed EMI, schedule generated at creation.

Each installment is a payable source row of the component engine (payroll_components): it reaches a payslip as LOAN_EMI
(loan) or ADVANCE_RECOVERY (advance), moves pending -> processing -> paid with the run, and closes the loan when the paid
installments add up to the principal.

Lock order (plan 15 P7): run rows (target month) -> payroll_loans -> payroll_loan_installments. The component engine locks
the parent loans before the installments when a payslip is published (`Provider.lock_parents`).
Amounts, reasons and references of external payments are encrypted columns; the audit log carries the loan reference,
kind and periods only.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import ROUND_CEILING, Decimal
from types import SimpleNamespace
from typing import Any

from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import PayrollLoan, PayrollLoanInstallment, User
from ..schemas.payroll import LoanCancel, LoanCreate, LoanForeclose, LoanReschedule, LoanSkip
from . import admin_security, notifications
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_items as items_svc
from . import payroll_runs as runs
from . import payroll_settings as pset

log = logging.getLogger("app.payroll")

ENTITY = "payroll_loan"
LIVE = ("pending", "processing", "paid")
MIN_PRINCIPAL, MAX_PRINCIPAL = Decimal("1.00"), Decimal("1000000.00")
ZERO = Decimal("0.00")
INST = PayrollLoanInstallment


def _bad(field: str, message: str) -> AppError:
    return AppError("VALIDATION_ERROR", message, details=[{"field": field, "message": message}])


def _d(x: str | Decimal) -> Decimal:
    return Decimal(x)


# ============================================================================================ schedule (pure)
def installment_count(total: Decimal, emi: Decimal) -> int:
    return int((total / emi).to_integral_value(rounding=ROUND_CEILING))


def make_schedule(total: Decimal, emi: Decimal, first: dt.date) -> list[tuple[dt.date, Decimal]]:
    """Fixed EMI, one installment per month from `first`; the last one is the remainder."""
    out: list[tuple[dt.date, Decimal]] = []
    left = calc.q2(total)
    i = 0
    while left > 0:
        amt = min(emi, left)
        out.append((comps.month_add(first, i), calc.q2(amt)))
        left = calc.q2(left - amt)
        i += 1
    return out


# ============================================================================================ reads / output
async def _loan_or_404(session: AsyncSession, loan_id: uuid.UUID, *, user_id: uuid.UUID | None = None) -> PayrollLoan:
    q = select(PayrollLoan).where(PayrollLoan.id == loan_id)
    if user_id is not None:
        q = q.where(PayrollLoan.user_id == user_id)
    loan = (await session.execute(q)).scalar_one_or_none()
    if loan is None:
        raise AppError("NOT_FOUND", "Loan not found")
    return loan


async def _lock_loan(session: AsyncSession, loan_id: uuid.UUID) -> tuple[PayrollLoan, list[INST]]:
    """Loan row, then its installments (ORDER BY id) FOR UPDATE: the P7 order."""
    loan = (
        await session.execute(
            select(PayrollLoan).where(PayrollLoan.id == loan_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if loan is None:
        raise AppError("NOT_FOUND", "Loan not found")
    q = select(INST).where(INST.loan_id == loan_id).order_by(INST.id).with_for_update().execution_options(populate_existing=True)
    insts = list((await session.execute(q)).scalars())
    insts.sort(key=lambda i: (i.seq, i.period, str(i.id)))  # numbers repeat only between a live and a skipped / cancelled row
    return loan, insts


async def _insts_by_loan(session: AsyncSession, loan_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[INST]]:
    out: dict[uuid.UUID, list[INST]] = {i: [] for i in loan_ids}
    if loan_ids:
        q = select(INST).where(INST.loan_id.in_(loan_ids)).order_by(INST.loan_id, INST.seq, INST.period, INST.id)
        for i in (await session.execute(q)).scalars():
            out[i.loan_id].append(i)
    return out


def _sum(insts: list[INST], *statuses: str) -> Decimal:
    return calc.q2(sum((_d(i.amount) for i in insts if i.status in statuses), Decimal(0)))


def _next(insts: list[INST]) -> dict[str, str] | None:
    pend = sorted((i for i in insts if i.status == "pending"), key=lambda i: (i.period, i.seq))
    return {"period": pc.period_str(pend[0].period), "amount": calc.money_str(_d(pend[0].amount))} if pend else None


def _outstanding(loan: PayrollLoan, insts: list[INST]) -> Decimal:
    if loan.status != "active":
        return ZERO
    return calc.q2(_d(loan.principal) - _sum(insts, "paid"))


def _inst_out(i: INST, flags: list[str]) -> dict[str, Any]:
    return {
        "id": str(i.id), "seq": i.seq, "period": pc.period_str(i.period), "amount": calc.money_str(_d(i.amount)),
        "kind": i.kind, "status": i.status, "payslip_id": str(i.payslip_id) if i.payslip_id else None, "flags": flags,
    }  # fmt: skip


def _base_out(loan: PayrollLoan, insts: list[INST], people: dict) -> dict[str, Any]:
    return {
        "id": str(loan.id),
        "reference": loan.reference,
        "user": pc.user_out(people.get(loan.user_id)),
        "kind": loan.kind,
        "principal": calc.money_str(_d(loan.principal)),
        "emi": calc.money_str(_d(loan.emi)),
        "start_period": pc.period_str(loan.start_period),
        "recovered": calc.money_str(_sum(insts, "paid")),
        "in_process": calc.money_str(_sum(insts, "processing")),
        "outstanding": calc.money_str(_outstanding(loan, insts)),
        "status": loan.status,
        "close_mode": loan.close_mode,
        "version": loan.version,
        "created_at": pc.iso(loan.created_at),
        "created_by": pc.user_out(people.get(loan.created_by)),
        "closed_at": pc.iso(loan.closed_at),
    }


async def _detail_out(session: AsyncSession, loan: PayrollLoan) -> dict[str, Any]:
    insts = (await _insts_by_loan(session, [loan.id]))[loan.id]
    people = await items_svc._people(session, {loan.user_id, loan.created_by})
    states = await comps.row_states(session, insts)
    out = _base_out(loan, insts, people)
    out["reason"] = loan.reason
    out["installments"] = [_inst_out(i, states[i.id][1]) for i in insts]
    return out


async def list_loans(
    session: AsyncSession, *, user_id: uuid.UUID | None, status: str | None, kind: str | None, page: PageParams
) -> dict[str, Any]:
    cond = []
    if user_id:
        cond.append(PayrollLoan.user_id == user_id)
    if status:
        cond.append(PayrollLoan.status == status)
    if kind:
        cond.append(PayrollLoan.kind == kind)
    total = (await session.execute(select(func.count()).select_from(PayrollLoan).where(*cond))).scalar_one()
    q = (
        select(PayrollLoan).where(*cond).order_by(PayrollLoan.created_at.desc(), PayrollLoan.id)
        .offset(page.offset).limit(page.page_size)
    )  # fmt: skip
    loans = list((await session.execute(q)).scalars())
    by_loan = await _insts_by_loan(session, [lo.id for lo in loans])
    people = await items_svc._people(session, {x for lo in loans for x in (lo.user_id, lo.created_by)})
    items = []
    for lo in loans:
        o = _base_out(lo, by_loan[lo.id], people)
        o["next_installment"] = _next(by_loan[lo.id])
        items.append(o)
    return make_page(items, page, total)


async def get_loan(session: AsyncSession, actor: Principal, loan_id: uuid.UUID) -> dict[str, Any]:
    loan = await _loan_or_404(session, loan_id)
    out = await _detail_out(session, loan)
    audit(session, action="payroll.loan.view", actor=actor, entity_type=ENTITY, entity_id=loan.id,
          after={"reference": loan.reference})  # fmt: skip
    await pc.commit(session)
    return out


# ============================================================================================ employee (own loans only)
_OWN_STATUS = {
    "pending": "scheduled",
    "processing": "scheduled",
    "paid": "deducted",
    "skipped": "deferred",
    "cancelled": "cancelled",
}


def _own_out(loan: PayrollLoan, insts: list[INST]) -> dict[str, Any]:
    return {
        "id": str(loan.id),
        "reference": loan.reference,
        "kind": loan.kind,
        "principal": calc.money_str(_d(loan.principal)),
        "emi": calc.money_str(_d(loan.emi)),
        "outstanding": calc.money_str(_outstanding(loan, insts)),
        "status": loan.status,
        "start_period": pc.period_str(loan.start_period),
        "next_installment": _next(insts) if loan.status == "active" else None,
    }


async def my_loans(session: AsyncSession, actor: Principal) -> dict[str, Any]:
    q = select(PayrollLoan).where(PayrollLoan.user_id == actor.id).order_by(PayrollLoan.created_at.desc(), PayrollLoan.id)
    loans = list((await session.execute(q)).scalars())
    loans.sort(key=lambda lo: lo.status != "active")  # stable: active first
    by_loan = await _insts_by_loan(session, [lo.id for lo in loans])
    return {"items": [_own_out(lo, by_loan[lo.id]) for lo in loans]}


async def my_loan(session: AsyncSession, actor: Principal, loan_id: uuid.UUID) -> dict[str, Any]:
    loan = (await session.execute(select(PayrollLoan).where(PayrollLoan.id == loan_id))).scalar_one_or_none()
    if loan is None or loan.user_id != actor.id:
        raise await pc.deny(session, actor, entity_type=ENTITY, entity_id=loan_id, reason="not_owner", status=404)
    insts = (await _insts_by_loan(session, [loan.id]))[loan.id]
    out = _own_out(loan, insts)
    out["installments"] = [
        {"seq": i.seq, "period": pc.period_str(i.period), "amount": calc.money_str(_d(i.amount)), "status": _OWN_STATUS[i.status]}
        for i in insts
    ]  # fmt: skip
    audit(session, action="payroll.loan.view_own", actor=actor, entity_type=ENTITY, entity_id=loan.id,
          after={"reference": loan.reference})  # fmt: skip
    await pc.commit(session)
    return out


# ============================================================================================ helpers for the writes
async def _next_reference(session: AsyncSession) -> str:
    """L-0001 ... PostgreSQL: the sequence of migration 0022; other databases (tests): highest existing number + 1."""
    if session.get_bind().dialect.name == "postgresql":
        n = (await session.execute(text("SELECT nextval('payroll_loan_ref_seq')"))).scalar_one()
    else:
        last = (
            await session.execute(
                select(PayrollLoan.reference)
                .order_by(func.length(PayrollLoan.reference).desc(), PayrollLoan.reference.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        n = int(last[2:]) + 1 if last else 1
    return f"L-{n:04d}"


def _next_seq(insts: list[INST]) -> int:
    """Installment numbers are unique among LIVE installments only (migration 0023): a new one follows the highest live number,
    so skipped and cancelled installments never use up numbers. Call after the statuses of the dropped rows were changed."""
    return max((i.seq for i in insts if i.status in LIVE), default=0) + 1


def _new_inst(loan: PayrollLoan, actor_id: uuid.UUID, seq: int, period: dt.date, amount: Decimal, kind: str = "emi") -> INST:
    return INST(
        loan_id=loan.id, user_id=loan.user_id, seq=seq, period=period, amount=calc.money_str(amount), kind=kind,
        status="pending", version=1, created_by=actor_id, updated_by=actor_id,
    )  # fmt: skip


def _loan_event(
    session: AsyncSession, loan: PayrollLoan, action: str, actor_id: uuid.UUID | None, detail: dict | None = None
) -> None:
    comps.add_event(
        session, item_type="loan", item=loan, action=action, actor_id=actor_id, period=loan.start_period,
        amount=loan.principal, detail=detail,
    )  # fmt: skip


def _cancel_inst(session: AsyncSession, i: INST, actor_id: uuid.UUID, action: str, detail: dict | None = None) -> None:
    i.status, i.updated_by, i.version = "cancelled", actor_id, i.version + 1
    comps.add_event(session, item_type="loan_installment", item=i, action=action, actor_id=actor_id, detail=detail)


def _require_active(loan: PayrollLoan, expected_version: int) -> None:
    if loan.status != "active":
        raise AppError("LOAN_NOT_ACTIVE", "The loan is not active")
    if loan.version != expected_version:
        raise AppError("VERSION_CONFLICT", "The loan was changed by someone else; reload it")


async def _setting_int(session: AsyncSession, key: str) -> int:
    return int(await pset.current_value(session, key))


async def _prelude(
    session: AsyncSession, redis: Redis | None, actor: Principal, loan_id: uuid.UUID, password: SecretStr | None, action: str
) -> PayrollLoan:
    """404 for an unknown loan, the self rule, then (when a password is expected) the step-up."""
    peek = await _loan_or_404(session, loan_id)
    await pc.forbid_self(session, actor, peek.user_id, entity_type=ENTITY, entity_id=loan_id)
    if redis is not None:
        await admin_security.require_reauth(session, redis, actor, password, action=action)
    return peek


# ============================================================================================ create
async def create_loan(session: AsyncSession, redis: Redis, actor: Principal, body: LoanCreate) -> dict[str, Any]:
    today = await runs._today(session)
    start = comps.parse_target_period(body.start_period, today)
    await pc.forbid_self(session, actor, body.user_id, entity_type=ENTITY, entity_id=body.user_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.loan.create")
    principal, emi = body.principal, body.emi
    if not MIN_PRINCIPAL <= principal <= MAX_PRINCIPAL:
        raise _bad("principal", "must be between 1.00 and 1000000.00")
    if emi <= 0 or emi > principal:
        raise _bad("emi", "must be greater than 0 and at most the principal")
    max_tenure = await _setting_int(session, "payroll.loan_max_tenure_months")
    n = installment_count(principal, emi)
    if n > max_tenure:
        raise _bad("emi", f"the loan would take {n} months; at most {max_tenure} are allowed")
    user = await items_svc._eligible_user(session, body.user_id, start)
    run_status = await comps.check_target_period(session, [user.id], start, lock=True)  # run row first (P7)
    # serialise concurrent creates for one employee so the active-loan limit cannot be overshot (id only: no joined loads)
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())
    active = (
        await session.execute(
            select(func.count()).select_from(PayrollLoan).where(PayrollLoan.user_id == user.id, PayrollLoan.status == "active")
        )
    ).scalar_one()
    if active >= await _setting_int(session, "payroll.loan_max_active"):
        raise AppError("LOAN_LIMIT_REACHED", "The employee already has the maximum number of active loans and advances")
    comps.check_caps((await comps.live_counts(session, [user.id], start))[user.id])
    loan = PayrollLoan(
        user_id=user.id, kind=body.kind, reference=await _next_reference(session), principal=calc.money_str(principal),
        emi=calc.money_str(emi), start_period=start, reason=body.reason, status="active", version=1,
        created_by=actor.id, updated_by=actor.id,
    )  # fmt: skip
    session.add(loan)
    await pc.flush(session)
    rows = [_new_inst(loan, actor.id, seq, per, amt) for seq, (per, amt) in enumerate(make_schedule(principal, emi, start), 1)]
    session.add_all(rows)
    await pc.flush(session)
    _loan_event(session, loan, "create", actor.id, {"reason": body.reason, "installments": len(rows)})
    for r in rows:
        comps.add_event(session, item_type="loan_installment", item=r, action="create", actor_id=actor.id)
    audit(session, action="payroll.loan.create", actor=actor, entity_type=ENTITY, entity_id=loan.id,
          after={"reference": loan.reference, "kind": loan.kind, "user_id": str(user.id), "start_period": pc.period_str(start),
                 "installments": len(rows), "run_status": run_status})  # fmt: skip
    loan_id, user_id = loan.id, user.id
    await pc.commit(session)
    await _push_created(session, user_id, loan_id)
    return await _detail_out(session, await _loan_or_404(session, loan_id))


async def _push_created(session: AsyncSession, user_id: uuid.UUID, loan_id: uuid.UUID) -> None:
    """Data-only push after commit (best effort, never amounts)."""
    try:
        await notifications.notify(
            session, [user_id], type="loan_created", entity_id=loan_id, title="Salary advance / loan added",
            body="A recovery schedule was added to your payslips. Open the app for details.",
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.exception("loan_created push failed (ignored)")


# ============================================================================================ skip (defer one installment)
async def skip(session: AsyncSession, actor: Principal, loan_id: uuid.UUID, body: LoanSkip) -> dict[str, Any]:
    await _prelude(session, None, actor, loan_id, None, "payroll.loan.skip")
    peeked = (await _insts_by_loan(session, [loan_id]))[loan_id]
    target = next((i for i in peeked if i.id == body.installment_id), None)
    if target is None:
        raise AppError("NOT_FOUND", "Installment not found")
    guess = _append_period(peeked, target)
    await items_svc._lock_runs(session, [target.period, guess])  # run rows first, ascending
    loan, insts = await _lock_loan(session, loan_id)
    _require_active(loan, body.expected_version)
    inst = next(i for i in insts if i.id == body.installment_id)
    if inst.status != "pending" or inst.kind != "emi":
        raise AppError("ITEM_LOCKED", f"The installment is {inst.status} and cannot be deferred")
    new_period = _append_period(insts, inst)
    if new_period != guess:
        raise AppError("CONFLICT", "The schedule changed meanwhile; reload and retry")
    run_status = await comps.check_target_period(session, [loan.user_id], new_period)
    inst.status, inst.updated_by, inst.version = "skipped", actor.id, inst.version + 1
    comps.add_event(
        session, item_type="loan_installment", item=inst, action="skip", actor_id=actor.id, detail={"reason": body.reason}
    )
    await pc.flush(session)
    comps.check_caps((await comps.live_counts(session, [loan.user_id], new_period))[loan.user_id])
    new = _new_inst(loan, actor.id, _next_seq(insts), new_period, _d(inst.amount))
    session.add(new)
    await pc.flush(session)
    comps.add_event(session, item_type="loan_installment", item=new, action="create", actor_id=actor.id,
                    detail={"deferred_from": pc.period_str(inst.period)})  # fmt: skip
    loan.version, loan.updated_by = loan.version + 1, actor.id
    _loan_event(session, loan, "skip", actor.id, {"reason": body.reason, "seq": inst.seq})
    audit(session, action="payroll.loan.skip", actor=actor, entity_type=ENTITY, entity_id=loan.id,
          after={"reference": loan.reference, "seq": inst.seq, "period": pc.period_str(inst.period),
                 "moved_to": pc.period_str(new_period), "run_status": run_status})  # fmt: skip
    await pc.commit(session)
    return await _detail_out(session, await _loan_or_404(session, loan_id))


def _append_period(insts: list[INST], skipped: INST) -> dt.date:
    """The month after the last live installment (or after the skipped one when that was the last)."""
    last = max([i.period for i in insts if i.status in LIVE and i.id != skipped.id] + [skipped.period])
    return comps.month_add(last, 1)


# ============================================================================================ reschedule
async def reschedule(
    session: AsyncSession, redis: Redis, actor: Principal, loan_id: uuid.UUID, body: LoanReschedule
) -> dict[str, Any]:
    today = await runs._today(session)
    frm = comps.parse_target_period(body.from_period, today)
    await _prelude(session, redis, actor, loan_id, body.current_password, "payroll.loan.reschedule")
    await items_svc._lock_runs(session, [frm])
    loan, insts = await _lock_loan(session, loan_id)
    _require_active(loan, body.expected_version)
    done = [i for i in insts if i.status in ("paid", "processing")]
    kept = [i for i in insts if i.status == "pending" and i.period < frm]
    drop = [i for i in insts if i.status == "pending" and i.period >= frm]
    remaining = calc.q2(_d(loan.principal) - _sum(done + kept, "paid", "processing", "pending"))
    if remaining <= 0:
        raise _bad("from_period", "there is nothing left to schedule")
    emi = body.emi
    if emi <= 0 or emi > remaining:
        raise _bad("emi", "must be greater than 0 and at most the amount still to be scheduled")
    fixed = done + kept
    if fixed and frm <= max(i.period for i in fixed):
        raise _bad("from_period", "must be after the last recovered or kept installment month")
    n_new = installment_count(remaining, emi)
    if len(fixed) + n_new > await _setting_int(session, "payroll.loan_max_tenure_months"):
        raise _bad("emi", "the new schedule would exceed the maximum tenure")
    run_status = await comps.check_target_period(session, [loan.user_id], frm)
    old_emi = loan.emi
    for i in drop:
        _cancel_inst(session, i, actor.id, "reschedule", {"reason": body.reason})
    await pc.flush(session)
    comps.check_caps((await comps.live_counts(session, [loan.user_id], frm))[loan.user_id])
    seq0 = _next_seq(insts) - 1
    rows = [_new_inst(loan, actor.id, seq0 + k, per, amt) for k, (per, amt) in enumerate(make_schedule(remaining, emi, frm), 1)]
    session.add_all(rows)
    loan.emi, loan.version, loan.updated_by = calc.money_str(emi), loan.version + 1, actor.id
    await pc.flush(session)
    for r in rows:
        comps.add_event(session, item_type="loan_installment", item=r, action="create", actor_id=actor.id)
    _loan_event(session, loan, "reschedule", actor.id, {"reason": body.reason, "emi": calc.money_str(emi), "prev_emi": old_emi})
    audit(session, action="payroll.loan.reschedule", actor=actor, entity_type=ENTITY, entity_id=loan.id,
          after={"reference": loan.reference, "from_period": pc.period_str(frm), "installments": len(rows),
                 "cancelled": len(drop), "run_status": run_status})  # fmt: skip
    await pc.commit(session)
    return await _detail_out(session, await _loan_or_404(session, loan_id))


# ============================================================================================ foreclose
async def foreclose(
    session: AsyncSession, redis: Redis, actor: Principal, loan_id: uuid.UUID, body: LoanForeclose
) -> dict[str, Any]:
    today = await runs._today(session)
    period = comps.parse_target_period(body.period, today) if body.period else None
    if body.mode == "outside" and body.paid_on > today:
        raise _bad("paid_on", "must not be in the future")
    await _prelude(session, redis, actor, loan_id, body.current_password, "payroll.loan.foreclose")
    if period is not None:
        await items_svc._lock_runs(session, [period])
    loan, insts = await _lock_loan(session, loan_id)
    _require_active(loan, body.expected_version)
    if any(i.status == "processing" for i in insts):
        raise AppError("ITEM_LOCKED", "An installment is inside a processed payslip; reopen the run first")
    outstanding = calc.q2(_d(loan.principal) - _sum(insts, "paid"))
    if outstanding <= 0:
        raise AppError("LOAN_NOT_ACTIVE", "Nothing is outstanding on this loan")
    drop = [i for i in insts if i.status == "pending"]
    run_status = None
    new = None
    if body.mode == "payroll":
        if any(i.status == "paid" and i.period == period for i in insts):
            raise _bad("period", "an installment of that month was already deducted")
        run_status = await comps.check_target_period(session, [loan.user_id], period)
    for i in drop:
        _cancel_inst(session, i, actor.id, "foreclose", {"reason": body.reason})
    await pc.flush(session)
    loan.version, loan.updated_by = loan.version + 1, actor.id
    if body.mode == "payroll":
        comps.check_caps((await comps.live_counts(session, [loan.user_id], period))[loan.user_id])
        new = _new_inst(loan, actor.id, _next_seq(insts), period, outstanding, kind="foreclosure")
        session.add(new)
        await pc.flush(session)
        comps.add_event(session, item_type="loan_installment", item=new, action="create", actor_id=actor.id,
                        detail={"reason": body.reason})  # fmt: skip
        _loan_event(session, loan, "foreclose", actor.id, {"reason": body.reason, "mode": "payroll"})
    else:
        loan.status, loan.close_mode, loan.closed_at = "closed", "foreclosed_outside", utcnow()
        _loan_event(session, loan, "foreclose", actor.id,
                    {"reason": body.reason, "mode": "outside", "paid_on": body.paid_on.isoformat(), "reference": body.reference})  # fmt: skip
    audit(session, action="payroll.loan.foreclose", actor=actor, entity_type=ENTITY, entity_id=loan.id,
          after={"reference": loan.reference, "mode": body.mode, "period": pc.period_str(period) if period else None,
                 "run_status": run_status})  # fmt: skip
    await pc.commit(session)
    return await _detail_out(session, await _loan_or_404(session, loan_id))


# ============================================================================================ cancel
async def cancel(session: AsyncSession, redis: Redis, actor: Principal, loan_id: uuid.UUID, body: LoanCancel) -> dict[str, Any]:
    await _prelude(session, redis, actor, loan_id, body.current_password, "payroll.loan.cancel")
    loan, insts = await _lock_loan(session, loan_id)
    _require_active(loan, body.expected_version)
    if any(i.status in ("processing", "paid") for i in insts):
        raise AppError("LOAN_HAS_RECOVERIES", "Installments were already deducted or are in a processed payslip")
    for i in insts:
        if i.status == "pending":
            _cancel_inst(session, i, actor.id, "cancel", {"reason": body.reason})
    loan.status, loan.close_mode, loan.closed_at = "cancelled", "cancelled", utcnow()
    loan.version, loan.updated_by = loan.version + 1, actor.id
    _loan_event(session, loan, "cancel", actor.id, {"reason": body.reason})
    audit(session, action="payroll.loan.cancel", actor=actor, entity_type=ENTITY, entity_id=loan.id,
          after={"reference": loan.reference})  # fmt: skip
    await pc.commit(session)
    return await _detail_out(session, await _loan_or_404(session, loan_id))


# ============================================================================================ component engine provider
async def _prepare(session: AsyncSession, rows: list[INST]) -> None:
    """Payslip label context: loan reference / kind and the position of each installment among the live ones."""
    loan_ids = sorted({r.loan_id for r in rows})
    loans = {lo.id: lo for lo in (await session.execute(select(PayrollLoan).where(PayrollLoan.id.in_(loan_ids)))).scalars()}
    q = select(INST).where(INST.loan_id.in_(loan_ids), INST.status.in_(LIVE)).order_by(INST.loan_id, INST.period, INST.seq)
    live: dict[uuid.UUID, list[uuid.UUID]] = {}
    for i in (await session.execute(q)).scalars():
        live.setdefault(i.loan_id, []).append(i.id)
    for r in rows:
        ids = live.get(r.loan_id, [])
        pos = ids.index(r.id) + 1 if r.id in ids else len(ids)
        r.line_ctx = (loans[r.loan_id].reference, loans[r.loan_id].kind, r.kind, pos, len(ids))


def _lines(rows: list[INST]) -> list[dict]:
    out = []
    for r in rows:
        ref, kind, ikind, pos, total = r.line_ctx
        word = "Advance" if kind == "advance" else "Loan"
        label = f"{word} {ref} foreclosure" if ikind == "foreclosure" else f"{word} {ref} ({pos}/{total})"
        out.append(
            {
                "code": "ADVANCE_RECOVERY" if kind == "advance" else "LOAN_EMI",
                "label": label,
                "amount": calc.money_str(_d(r.amount)),
                "sources": [{"type": "loan_installment", "id": str(r.id)}],
            }
        )
    return out


async def _lock_parents(session: AsyncSession, installment_ids: list[uuid.UUID]) -> None:
    """P7: loans before installments. Called by the engine before it locks the installments of a publish."""
    q = select(INST.loan_id).where(INST.id.in_(installment_ids)).distinct()
    loan_ids = sorted((await session.execute(q)).scalars())
    if loan_ids:
        await session.execute(
            select(PayrollLoan.id).where(PayrollLoan.id.in_(loan_ids)).order_by(PayrollLoan.id).with_for_update()
        )


async def _after_paid(session: AsyncSession, rows: list[INST], actor_id: uuid.UUID) -> None:
    """A loan whose paid installments add up to the principal closes itself (`recovered`, or `foreclosed_payroll`)."""
    loan_ids = sorted({r.loan_id for r in rows})
    loans = (
        await session.execute(
            select(PayrollLoan)
            .where(PayrollLoan.id.in_(loan_ids))
            .order_by(PayrollLoan.id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    by_loan = await _insts_by_loan(session, loan_ids)
    who = SimpleNamespace(id=actor_id, employee_id=None)
    for loan in loans:
        insts = by_loan[loan.id]
        if loan.status != "active" or _sum(insts, "paid") < _d(loan.principal):
            continue
        foreclosed = any(i.status == "paid" and i.kind == "foreclosure" for i in insts)
        loan.status, loan.closed_at = "closed", utcnow()
        loan.close_mode = "foreclosed_payroll" if foreclosed else "recovered"
        loan.version, loan.updated_by = loan.version + 1, actor_id
        _loan_event(session, loan, "close", actor_id, {"close_mode": loan.close_mode})
        audit(session, action="payroll.loan.close", actor=who, entity_type=ENTITY, entity_id=loan.id,
              after={"reference": loan.reference, "close_mode": loan.close_mode})  # fmt: skip


comps.register(
    comps.Provider(
        "loan_installment",
        INST,
        _lines,
        comps.LIVE,
        prepare=_prepare,
        lock_parents=_lock_parents,
        after_paid=_after_paid,
        order=20,
    )
)
