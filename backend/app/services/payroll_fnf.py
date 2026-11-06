"""Full and final settlement with maker-checker (plan 16 work package 4b, contract 18.8).

draft -> submitted -> approved -> settled (+ cancelled; reject and revoke go back to draft). The F&F is an ORCHESTRATOR (F11): the
approval writes rows into the existing source tables (one-time items, leave encashments, loan installments), all tagged `fnf_id`,
so process / reopen / publish / revise and every payroll cap work unchanged. `settled` is set by `on_published` in the publish
transaction of the payslip that carries the settlement.

Maker-checker (enforced here, by CHECK constraints and trigger of migration 0026, and by the audit rows): the checker is not the
employee (403 FORBIDDEN, audited), nor the creator, the last editor, the submitter or anybody in the append-only `contributors`
list (403 SECOND_PERSON_REQUIRED). The step-up password is verified before any state check. Approve and revoke are bound to the
version the checker reviewed (`expected_version`; every edit bumps it, and the trigger freezes a submitted row).

Lock order (F15): payroll_runs -> users -> payslips -> salary_structures -> payroll_one_time_items -> payroll_loans ->
payroll_loan_installments -> payroll_arrears -> leave_balances -> leave_encashments -> reimbursement_claims -> overtime_days ->
fnf_settlements. Every action that takes the F&F row takes the users row FIRST; approve / revoke lock the F&F row LAST and
re-check `version`. Audit rows, logs, pushes and error messages carry ids, dates, counts and codes only: never money, labels,
references, reasons, comments or remarks.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    EmployeePayrollDetails,
    FnfSettlement,
    LeaveBalance,
    LeaveEncashment,
    PayrollLoan,
    PayrollLoanInstallment,
    PayrollOneTimeItem,
    PayrollRun,
    Payslip,
    User,
)
from ..schemas.payroll import PERIOD_RE, FnfApprove, FnfCreate, FnfReject, FnfRevoke, FnfUpdate, VersionIn
from . import admin_security, notifications
from . import attendance as att
from . import leave as leave_svc
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_days as pdays
from . import payroll_fnf_calc as fc
from . import payroll_fnf_out as out
from . import payroll_increments as ig
from . import payroll_items as items_svc
from . import payroll_loans as loans_svc
from . import payroll_runs as runs
from . import payroll_settings as pset
from . import payroll_structures as structs_svc

log = logging.getLogger("app.payroll")

ENTITY = "fnf_settlement"
LIVE = out.LIVE
ZERO = Decimal("0.00")


def _bad(field: str, message: str, code: str = "VALIDATION_ERROR") -> AppError:
    return AppError(code, message, details=[{"field": field, "message": message}])


# ============================================================================================ small helpers
def _touch(f: FnfSettlement, actor_id: uuid.UUID) -> None:
    """Append the actor to the never-shrinking `contributors` list (a new list: the ORM must see the change)."""
    ids = list(f.contributors or [])
    if str(actor_id) not in ids:
        ids.append(str(actor_id))
    f.contributors = ids


def _is_party(actor_id: uuid.UUID, f: FnfSettlement) -> bool:
    """True when the actor prepared the record in any way: creator, last editor, current submitter, or anybody in the history."""
    return actor_id in (f.created_by, f.last_edited_by, f.submitted_by) or str(actor_id) in (f.contributors or [])


async def _gate(session: AsyncSession, actor: Principal, f: FnfSettlement) -> None:
    """Checker rules: not the employee (403 FORBIDDEN, audited), not a party (403 SECOND_PERSON_REQUIRED)."""
    if f.user_id == actor.id:
        raise await pc.deny(session, actor, entity_type=ENTITY, entity_id=f.id, reason="self")
    if _is_party(actor.id, f):
        raise AppError("SECOND_PERSON_REQUIRED", "A different person must decide a settlement you prepared or submitted")


def _guard_maker(actor: Principal, f: FnfSettlement) -> None:
    """The person who last decided the record (reject / revoke keep `decided_by`) can never become a maker of it: the database
    CHECK would refuse it (decided_by must differ from the last editor and the submitter)."""
    if f.decided_by is not None and f.decided_by == actor.id:
        raise AppError("SECOND_PERSON_REQUIRED", "The person who decided this settlement cannot edit or submit it")


def _map_db_error(exc: IntegrityError) -> AppError:
    """Database refusals: the maker-checker CHECKs -> 403, the live-unique index -> 409 FNF_EXISTS, the lock trigger -> 409."""
    state = pc.sqlstate(exc)
    text_ = str(getattr(exc, "orig", exc))
    if state == "23514" and "ck_fnf_settlements_checker" in text_:
        return AppError("SECOND_PERSON_REQUIRED", "A different person must make this change")
    if state == "23505" and "uq_fnf_settlements_live" in text_:
        return AppError("FNF_EXISTS", "A final settlement is already open for this employee")
    if state == "23505" and "uq_fnf_settlements_period" in text_:
        return AppError("FNF_EXISTS", "An approved or settled final settlement already exists for this employee and month")
    return pc.map_integrity(exc)


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _map_db_error(exc) from None


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _map_db_error(exc) from None


def _event(
    session: AsyncSession, f: FnfSettlement, action: str, actor_id: uuid.UUID | None, *, amount: str | None = None, **detail: Any
) -> None:
    comps.add_event(
        session, item_type="fnf", item=f, action=action, actor_id=actor_id, period=f.settlement_period, amount=amount,
        detail=detail or None,
    )  # fmt: skip


def _counts(f: FnfSettlement) -> dict[str, int]:
    p = out.plan_of(f)
    return {"items": len(p["items"]), "encashments": len(p["encashments"]), "loans": len(p["loans"])}


def _audit(session: AsyncSession, actor: Any, action: str, f: FnfSettlement, **extra: Any) -> None:
    """Never amounts, labels, references, reasons, comments or remarks (contract 18.8)."""
    checker = f.decided_by if f.status in ("approved", "settled") else None
    audit(
        session, action=action, actor=actor, entity_type=ENTITY, entity_id=f.id,
        after={
            "user_id": str(f.user_id), "status": f.status, "last_working_day": f.last_working_day.isoformat(),
            "settlement_period": pc.period_str(f.settlement_period), "exit_reason": f.exit_reason,
            "shortfall_days": f.shortfall_days, "counts": _counts(f), "maker_id": str(f.created_by),
            "checker_id": str(checker) if checker else None, **extra,
        },
    )  # fmt: skip


def _parse_period(raw: str, lwd: dt.date) -> dt.date:
    m = PERIOD_RE.match(raw or "")
    if not m or not 2000 <= int(m.group(1)) <= 2200:
        raise _bad("settlement_period", "must be YYYY-MM", "INVALID_PERIOD")
    p = dt.date(int(m.group(1)), int(m.group(2)), 1)
    lo, hi = fc.settlement_window(lwd)
    if not lo <= p <= hi:
        raise _bad("settlement_period", "from the month of the last working day up to three months later", "INVALID_PERIOD")
    return p


def _check_dates(user: User, resignation: dt.date | None, lwd: dt.date, today: dt.date) -> None:
    if not 2000 <= lwd.year <= 2200:
        raise _bad("last_working_day", "year must be 2000-2200", "INVALID_DATE")
    if user.join_date is not None and lwd < user.join_date:
        raise _bad("last_working_day", "must not be before the joining date", "INVALID_DATE")
    if lwd > today + dt.timedelta(days=fc.MAX_LWD_AHEAD_DAYS):
        raise _bad("last_working_day", "at most 180 days ahead", "INVALID_DATE")
    if resignation is not None and (resignation > lwd or resignation.year < 2000):
        raise _bad("resignation_date", "must not be after the last working day", "INVALID_DATE")


async def _check_exit_bounds(session: AsyncSession, user: User, lwd: dt.date) -> None:
    """Review H1: a last working day can only shorten the employment window, so one later than the day before the deactivation
    (or deletion) is refused: it would pay a departed employee. Re-run at approve (the account may have been deactivated since)."""
    gone = user.deactivated_at or user.deleted_at
    if gone is None:
        return
    tz = att.office_tz(await att.load_settings(session))
    if lwd > pdays.gone_last_day(gone, tz):
        raise _bad("last_working_day", "must be before the deactivation date of the account", "INVALID_DATE")


async def _refuse_when_settled(session: AsyncSession, user: User, det: EmployeePayrollDetails | None) -> None:
    """Review M1: an employee with a settled F&F cannot get another one until they were reactivated (which clears the details
    last working day) or rejoined after that last working day."""
    q = (
        select(FnfSettlement.last_working_day)
        .where(FnfSettlement.user_id == user.id, FnfSettlement.status == "settled")
        .order_by(FnfSettlement.last_working_day.desc(), FnfSettlement.id)
        .limit(1)
    )
    settled_lwd = (await session.execute(q)).scalars().first()
    if settled_lwd is None:
        return
    rejoined = user.join_date is not None and user.join_date > settled_lwd
    reactivated = det is None or det.last_working_day is None
    if not (rejoined or reactivated):
        raise AppError("FNF_EXISTS", "This employee already has a settled final settlement; reactivate or rejoin them first")


async def _details_row(session: AsyncSession, user_id: uuid.UUID, actor_id: uuid.UUID) -> EmployeePayrollDetails:
    det = await session.get(EmployeePayrollDetails, user_id)
    if det is None:
        det = EmployeePayrollDetails(
            user_id=user_id, payment_mode="bank_transfer", include_in_payroll=True, overtime_eligible=False, updated_by=actor_id
        )
        session.add(det)
    return det


def _set_lwd(session: AsyncSession, actor: Principal, det: EmployeePayrollDetails, lwd: dt.date | None) -> None:
    if det.last_working_day != lwd:
        det.last_working_day, det.updated_by = lwd, actor.id
        audit(session, action="payroll.details.update", actor=actor, entity_type="user", entity_id=det.user_id,
              after={"fields": ["last_working_day"]})  # fmt: skip


async def _setting_notice_days(session: AsyncSession) -> int:
    return int(await pset.current_value(session, "payroll.notice_period_days"))


def _shortfall(f: FnfSettlement, plan: dict[str, Any]) -> int:
    ov = plan.get("override")
    return fc.notice_for(
        f.exit_reason, f.resignation_date, f.last_working_day, f.notice_required_days, waived=f.notice_waived,
        override_days=ov["days"] if ov else None,
    ).shortfall  # fmt: skip


# ============================================================================================ list / get / preview
async def list_fnf(
    session: AsyncSession, actor: Principal, *, status: str | None, q: str | None, page: PageParams
) -> dict[str, Any]:
    cond: list[Any] = []
    if status:
        cond.append(FnfSettlement.status == status)
    join = select(FnfSettlement).join(User, User.id == FnfSettlement.user_id)
    cnt = select(func.count()).select_from(FnfSettlement).join(User, User.id == FnfSettlement.user_id)
    if q:
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cond.append(func.lower(User.full_name).like(func.lower(like), escape="\\") | User.employee_id.ilike(like, escape="\\"))
    total = (await session.execute(cnt.where(*cond))).scalar_one()
    rows = list(
        (await session.execute(join.where(*cond).order_by(FnfSettlement.created_at.desc(), FnfSettlement.id).offset(page.offset).limit(page.page_size)))
        .unique().scalars()
    )  # fmt: skip
    today = await runs._today(session)
    items: list[dict[str, Any]] = []
    periods = {r.settlement_period for r in rows}
    run_status = {}
    if periods:
        run_status = {
            p: s
            for p, s in (
                await session.execute(select(PayrollRun.period, PayrollRun.status).where(PayrollRun.period.in_(periods)))
            ).all()
        }
    for f in rows:
        user = await session.get(User, f.user_id)
        det = await session.get(EmployeePayrollDetails, f.user_id)
        warnings = await out.checklist(session, f, user, det, today, None, [], light=True) if f.status in LIVE else []
        items.append(out.list_item(f, user, run_status.get(f.settlement_period, "none"), warnings))
    await pc.audit_read(session, actor, "payroll.fnf.list", ENTITY, count=len(items))
    return make_page(items, page, total)


async def get_fnf(session: AsyncSession, actor: Principal, fnf_id: uuid.UUID) -> dict[str, Any]:
    f = await out.or_404(session, fnf_id)
    res = await out.full_out(session, f)
    audit(session, action="payroll.fnf.view", actor=actor, entity_type=ENTITY, entity_id=f.id)
    await pc.commit(session)
    return res


async def _one(session: AsyncSession, fnf_id: uuid.UUID) -> dict[str, Any]:
    f = await out.or_404(session, fnf_id)
    await session.refresh(f)
    return await out.full_out(session, f)


async def preview(session: AsyncSession, actor: Principal, fnf_id: uuid.UUID) -> dict[str, Any]:
    f = await out.or_404(session, fnf_id)
    user = await structs_svc.get_user_or_404(session, f.user_id)
    det = await session.get(EmployeePayrollDetails, f.user_id)
    today = await runs._today(session)
    structs = await out.structures_of(session, f.user_id)
    settings = await out.settings_of(session)
    if f.status in ("approved", "settled"):
        est = await out.payslip_estimate(session, f)
    else:
        est = None
    if est is not None and est["result"] is not None:
        view = out.estimate_view(est, "payslip")
    else:
        extra: list[dict[str, Any]] = []
        flags: list[str] = []
        if f.status in ("draft", "submitted"):
            extra, flags = await out.planned_lines(session, f, user, structs, settings)
        run = (await session.execute(select(PayrollRun).where(PayrollRun.period == f.settlement_period))).scalar_one_or_none()
        est = await out.estimate(session, f, user, det, extra, today, run=run)
        if est["result"] is None:
            view = {"estimate": True, "days": {"basis_days": float(est["days"].basis_days), "employed_days": float(est["days"].employed_days),
                                              "lop_days": float(est["days"].lop_auto), "paid_days": 0.0},
                    "earnings": [], "deductions": [], "totals": None, "warnings": sorted({*est["warnings"], est["error"], *flags}),
                    "source": "structure"}  # fmt: skip
        else:
            view = out.estimate_view(est)
            view["warnings"] = sorted({*view["warnings"], *flags})
    audit(session, action="payroll.fnf.preview", actor=actor, entity_type=ENTITY, entity_id=f.id)
    await pc.commit(session)
    return view


# ============================================================================================ create / edit
async def create_fnf(session: AsyncSession, actor: Principal, user_id: uuid.UUID, body: FnfCreate) -> dict[str, Any]:
    await structs_svc.get_user_or_404(session, user_id)
    await pc.forbid_self(session, actor, user_id, entity_type=ENTITY, entity_id=user_id)
    today = await runs._today(session)
    await items_svc._lock_users(session, [user_id])  # BEFORE the existence gate below (plan 16 lesson 4a)
    user = await structs_svc.get_user_or_404(session, user_id)
    _check_dates(user, body.resignation_date, body.last_working_day, today)
    await _check_exit_bounds(session, user, body.last_working_day)
    period = _parse_period(body.settlement_period, body.last_working_day)
    live = (
        await session.execute(
            select(FnfSettlement.id).where(FnfSettlement.user_id == user_id, FnfSettlement.status.in_(LIVE)).limit(1)
        )
    ).first()
    if live is not None:
        raise AppError("FNF_EXISTS", "A final settlement is already open for this employee")
    det = await session.get(EmployeePayrollDetails, user_id)
    await _refuse_when_settled(session, user, det)
    required = body.notice_period_days
    if required is None:
        required = (
            det.notice_period_days
            if det is not None and det.notice_period_days is not None
            else await _setting_notice_days(session)
        )
    f = FnfSettlement(
        user_id=user_id, resignation_date=body.resignation_date, last_working_day=body.last_working_day,
        exit_reason=body.exit_reason, settlement_period=period, notice_required_days=required, notice_waived=False,
        shortfall_days=0, plan=out.blank_plan(), remarks=body.remarks, status="draft", version=1, created_by=actor.id,
        last_edited_by=actor.id, contributors=[str(actor.id)], updated_by=actor.id,
    )  # fmt: skip
    f.shortfall_days = _shortfall(f, f.plan)
    session.add(f)
    await _flush(session)  # the details last working day is NOT written: only the approval does (review H1)
    _event(session, f, "create", actor.id)
    _audit(session, actor, "payroll.fnf.create", f)
    fid = f.id
    await _commit(session)
    return await _one(session, fid)


async def _lock_draftish(session: AsyncSession, actor: Principal, fnf_id: uuid.UUID) -> FnfSettlement:
    """404, self rule, then users row, then the F&F row (users first: approve takes them in that order too)."""
    peek = await out.or_404(session, fnf_id)
    await pc.forbid_self(session, actor, peek.user_id, entity_type=ENTITY, entity_id=fnf_id)
    await items_svc._lock_users(session, [peek.user_id])
    return await out.or_404(session, fnf_id, lock=True)


async def _validate_plan(session: AsyncSession, f: FnfSettlement, user: User, plan: dict[str, Any], today: dt.date) -> None:
    ov = plan.get("override")
    if ov is not None and ov["days"] > f.notice_required_days:
        raise _bad("notice.override.days", "must not exceed the required notice days")
    for i, e in enumerate(plan["encashments"]):
        if int(e["year"]) not in (f.last_working_day.year, f.last_working_day.year - 1):
            raise _bad(f"encashments[{i}].year", "the year of the last working day or the year before")
        try:
            await out.encashment_quote(session, f, user, e)
        except AppError as exc:
            if not exc.details:
                exc.details = [{"field": f"encashments[{i}]", "message": exc.message}]
            else:
                exc.details = [{"field": f"encashments[{i}]", "message": exc.details[0].get("message", exc.message)}]
            raise
    if plan["loans"]:
        ids = [uuid.UUID(str(x["loan_id"])) for x in plan["loans"]]
        have = {lo.id: lo for lo in (await session.execute(select(PayrollLoan).where(PayrollLoan.id.in_(ids)))).scalars()}
        for i, x in enumerate(plan["loans"]):
            lo = have.get(uuid.UUID(str(x["loan_id"])))
            if lo is None or lo.user_id != f.user_id or lo.status != "active":
                raise _bad(f"loans[{i}].loan_id", "must be an active loan of the employee")
            if x["mode"] == "outside" and dt.date.fromisoformat(x["paid_on"]) > today:
                raise _bad(f"loans[{i}].paid_on", "must not be in the future")


async def update_fnf(session: AsyncSession, actor: Principal, fnf_id: uuid.UUID, body: FnfUpdate) -> dict[str, Any]:
    f = await _lock_draftish(session, actor, fnf_id)
    if f.status != "draft":
        raise AppError("NOT_DRAFT", "Only a draft settlement can be edited")
    if f.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The settlement was changed meanwhile; reload it")
    _guard_maker(actor, f)
    sent = body.model_fields_set - {"expected_version"}
    if not sent:
        raise _bad("expected_version", "send at least one field to change")
    user = await structs_svc.get_user_or_404(session, f.user_id)
    today = await runs._today(session)
    plan = out.plan_of(f)
    if "resignation_date" in sent:
        f.resignation_date = body.resignation_date
    if "last_working_day" in sent:
        f.last_working_day = body.last_working_day  # type: ignore[assignment]
    if "exit_reason" in sent:
        f.exit_reason = body.exit_reason  # type: ignore[assignment]
    if "notice_period_days" in sent:
        det0 = await session.get(EmployeePayrollDetails, f.user_id)
        f.notice_required_days = (
            body.notice_period_days
            if body.notice_period_days is not None
            else (
                det0.notice_period_days
                if det0 is not None and det0.notice_period_days is not None
                else await _setting_notice_days(session)
            )
        )
    _check_dates(user, f.resignation_date, f.last_working_day, today)
    await _check_exit_bounds(session, user, f.last_working_day)
    f.settlement_period = (
        _parse_period(body.settlement_period, f.last_working_day)
        if "settlement_period" in sent
        else _parse_period(pc.period_str(f.settlement_period), f.last_working_day)
    )  # an unchanged month must still fit a changed last working day
    if "notice" in sent and body.notice is not None:
        f.notice_waived = body.notice.waived
        ov = body.notice.override
        plan["override"] = {"days": ov.days, "reason": ov.reason} if ov is not None else None
    if "encashments" in sent and body.encashments is not None:
        plan["encashments"] = [
            {"leave_type_id": str(e.leave_type_id), "year": e.year, "days": str(leave_svc.d1(e.days))} for e in body.encashments
        ]
    if "loans" in sent and body.loans is not None:
        plan["loans"] = [
            {
                "loan_id": str(x.loan_id),
                "mode": x.mode,
                "paid_on": x.paid_on.isoformat() if x.paid_on else None,
                "reference": x.reference,
            }
            for x in body.loans
        ]
    if "items" in sent and body.items is not None:
        plan["items"] = [{"code": i.code, "label": i.label, "amount": calc.money_str(i.amount)} for i in body.items]
    if "move_open_items" in sent and body.move_open_items is not None:
        plan["move_open_items"] = body.move_open_items
    if "remarks" in sent:
        f.remarks = body.remarks
    await _validate_plan(session, f, user, plan, today)
    f.plan = plan
    f.shortfall_days = _shortfall(f, plan)
    f.version, f.updated_by, f.last_edited_by = f.version + 1, actor.id, actor.id
    _touch(f, actor.id)
    await _flush(session)
    _event(session, f, "update", actor.id)
    _audit(session, actor, "payroll.fnf.update", f)
    await _commit(session)
    return await _one(session, fnf_id)


# ============================================================================================ maker actions
async def submit_fnf(session: AsyncSession, redis: Redis, actor: Principal, fnf_id: uuid.UUID, body: VersionIn) -> dict[str, Any]:
    f = await _lock_draftish(session, actor, fnf_id)
    if f.status != "draft":
        raise AppError("NOT_DRAFT", "Only a draft settlement can be submitted")
    if f.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The settlement was changed meanwhile; reload it")
    _guard_maker(actor, f)
    user = await structs_svc.get_user_or_404(session, f.user_id)
    today = await runs._today(session)
    plan = out.plan_of(f)
    active = list(
        (
            await session.execute(select(PayrollLoan).where(PayrollLoan.user_id == f.user_id, PayrollLoan.status == "active"))
        ).scalars()
    )
    modes = {str(x["loan_id"]) for x in plan["loans"]}
    missing = [lo for lo in active if str(lo.id) not in modes]
    if missing:
        raise AppError("LOAN_UNRESOLVED", "Choose how every active loan is settled",
                       details=[{"field": "loans", "message": "every active loan needs a mode"}])  # fmt: skip
    await _validate_plan(session, f, user, plan, today)  # quotes are re-checked (409 INSUFFICIENT_BALANCE)
    await _check_exit_bounds(session, user, f.last_working_day)
    structs, settings = await out.structures_of(session, f.user_id), await out.settings_of(session)
    money = await out.notice_money(session, f, structs, settings)
    if f.shortfall_days > 0:
        if money["missing"]:
            raise AppError("STRUCTURE_MISSING", "The employee has no salary structure for the notice recovery", status=422)
        out.notice_checked(money)
    quotes = [await out.encashment_quote(session, f, user, e) for e in plan["encashments"]]
    by_loan = await loans_svc._insts_by_loan(session, [lo.id for lo in active])
    outstanding = {str(lo.id): calc.q2(Decimal(lo.principal) - loans_svc._sum(by_loan[lo.id], "paid")) for lo in active}
    snap = _quote_snapshot(money, quotes, plan, outstanding)  # what the checker reviews (review M2)
    name, emp_name = actor.user.full_name, user.full_name
    f.status, f.submitted_by, f.submitted_at = "submitted", actor.id, utcnow()
    f.summary = {"quote": snap}  # encrypted; the lock trigger lets draft>submitted carry it, a submitted row is then frozen
    f.decided_by = f.decided_at = f.decision_comment = None  # a new round: the old decision is history (the trigger allows it)
    f.version, f.updated_by = f.version + 1, actor.id
    _touch(f, actor.id)
    await _flush(session)
    _event(session, f, "submit", actor.id)
    _audit(session, actor, "payroll.fnf.submit", f)
    await _commit(session)
    await ig._push_approvers(
        session, redis, actor.id, name, kind="fnf", entity_id=fnf_id, text=f"a final settlement for {emp_name}"
    )
    return await _one(session, fnf_id)


async def withdraw_fnf(session: AsyncSession, actor: Principal, fnf_id: uuid.UUID, body: VersionIn) -> dict[str, Any]:
    f = await _lock_draftish(session, actor, fnf_id)
    if f.status != "submitted":
        raise AppError("NOT_SUBMITTED", "Only a submitted settlement can be withdrawn")
    if f.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The settlement was changed meanwhile; reload it")
    _guard_maker(actor, f)
    f.status, f.submitted_by, f.submitted_at, f.summary = "draft", None, None, None  # `contributors` keeps the submitter
    f.version, f.updated_by = f.version + 1, actor.id
    _touch(f, actor.id)
    await _flush(session)
    _event(session, f, "withdraw", actor.id)
    _audit(session, actor, "payroll.fnf.withdraw", f)
    await _commit(session)
    return await _one(session, fnf_id)


async def cancel_fnf(session: AsyncSession, actor: Principal, fnf_id: uuid.UUID, reason: str) -> dict[str, Any]:
    f = await _lock_draftish(session, actor, fnf_id)
    if f.status not in ("draft", "submitted"):
        raise AppError("NOT_DRAFT", "Only a draft or submitted settlement can be cancelled")
    f.status, f.cancel_reason = "cancelled", reason
    f.version, f.updated_by = f.version + 1, actor.id
    # a draft / submitted F&F never wrote the details last working day (only the approval does): nothing to clear
    await _flush(session)
    _event(session, f, "cancel", actor.id, reason=reason)
    _audit(session, actor, "payroll.fnf.cancel", f)
    await _commit(session)
    return await _one(session, fnf_id)


# ============================================================================================ checker actions
async def reject_fnf(session: AsyncSession, actor: Principal, fnf_id: uuid.UUID, body: FnfReject) -> dict[str, Any]:
    peek = await out.or_404(session, fnf_id)
    await items_svc._lock_users(session, [peek.user_id])
    f = await out.or_404(session, fnf_id, lock=True)
    await _gate(session, actor, f)
    if f.status != "submitted":
        raise AppError("NOT_SUBMITTED", "The settlement is not waiting for a decision")
    if f.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The settlement was changed meanwhile; reload it")
    maker, employee = f.created_by, await session.get(User, f.user_id)
    f.status, f.decided_by, f.decided_at, f.decision_comment = "draft", actor.id, utcnow(), body.comment
    f.submitted_by = f.submitted_at = None
    f.summary = None  # the frozen quote belongs to the round that was rejected
    f.version, f.updated_by = f.version + 1, actor.id
    await _flush(session)
    _event(session, f, "reject", actor.id, comment=body.comment)
    _audit(session, actor, "payroll.fnf.reject", f)
    name = employee.full_name if employee else ""
    await _commit(session)
    await ig._push_maker(session, maker, kind="fnf", entity_id=fnf_id, title="Final settlement rejected", body=name)
    return await _one(session, fnf_id)


def _quote_snapshot(
    money: dict[str, Any], quotes: list[dict[str, Any]], plan: dict[str, Any], outstanding: dict[str, Decimal]
) -> dict[str, Any]:
    """What the checker reviews: notice basis / divisor / daily rate / amount, the encashment days / rate / amount per plan line
    and the outstanding amount per planned loan. Strings only (money is never a float); identical at submit and at approve."""
    return {
        "notice": None if money["missing"] else {k: str(money[k]) for k in ("basis", "divisor", "daily_rate", "amount")},
        "encashments": [
            {"leave_type_id": str(e["leave_type_id"]), "year": int(e["year"]), "days": str(q["days"]),
             "rate": calc.money_str(q["rate"]), "amount": calc.money_str(q["amount"])}
            for e, q in zip(plan["encashments"], quotes, strict=True)
        ],
        "loans": [
            {"loan_id": str(x["loan_id"]), "mode": x["mode"], "outstanding": calc.money_str(outstanding[str(x["loan_id"])])}
            for x in plan["loans"]
        ],
    }  # fmt: skip


async def _paid_after_exit(session: AsyncSession, f: FnfSettlement) -> bool:
    """True when a processed / published payslip with employed days exists for a month after the last working day month (other
    than the settlement-only ones): the approval then recovers pay that was already made (the PAID_AFTER_EXIT warning)."""
    lwd_month = fc.month_of(f.last_working_day)
    q = (
        select(Payslip.inputs)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(Payslip.user_id == f.user_id, Payslip.status.in_(("processed", "published")), PayrollRun.period > lwd_month)
    )
    return any(not (inputs or {}).get("settlement_only") for inputs in (await session.execute(q)).scalars())


def _created_ids(f: FnfSettlement) -> dict[str, Any]:
    return (f.summary or {}).get("created") or {}


async def approve_fnf(
    session: AsyncSession, redis: Redis, actor: Principal, fnf_id: uuid.UUID, body: FnfApprove
) -> dict[str, Any]:
    peek = await out.or_404(session, fnf_id)
    await admin_security.require_reauth(
        session, redis, actor, body.current_password, action="payroll.fnf.approve"
    )  # before state checks
    await _gate(session, actor, peek)
    if peek.status != "submitted":
        raise AppError("NOT_SUBMITTED", "The settlement is not waiting for a decision")
    if peek.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The settlement was changed meanwhile; reload it")
    user_id, period, plan0 = peek.user_id, peek.settlement_period, out.plan_of(peek)
    today = await runs._today(session)
    # ---- 1. run, users (F15); the target month rule runs under the run lock
    run_status = await comps.check_target_period(session, [user_id], period, lock=True)
    if (
        run_status == "processed"
    ):  # review L5: the processed payslips do not carry the new lines and publish would refuse for everyone
        raise AppError("PERIOD_LOCKED", "The settlement month is processed; reopen the run first")
    await items_svc._lock_users(session, [user_id])
    user = await structs_svc.get_user_or_404(session, user_id)
    structs = await out.structures_of(session, user_id)
    settings = await out.settings_of(session)
    move = bool(plan0["move_open_items"])
    # ---- 2. rows that will be modified, in the global order (one-time items, loans + installments, arrears, balances, encashments, claims)
    moving: list[tuple[str, Any, list[str]]] = []
    if move:
        moving += await out.open_items(session, peek, lock=True, kinds=("one_time",))
    loan_ids = sorted(uuid.UUID(str(x["loan_id"])) for x in plan0["loans"])
    locked_loans: dict[uuid.UUID, tuple[PayrollLoan, list[PayrollLoanInstallment]]] = {}
    for lid in loan_ids:
        locked_loans[lid] = await loans_svc._lock_loan(session, lid)
    if move:
        moving += await out.open_items(session, peek, lock=True, kinds=("arrears",))
    lines = sorted(enumerate(plan0["encashments"]), key=lambda t: (t[1]["leave_type_id"], int(t[1]["year"])))
    quotes: dict[int, dict[str, Any]] = {}
    for idx, e in lines:
        try:
            quotes[idx] = await out.encashment_quote(session, peek, user, e, lock=True)
        except AppError as exc:
            exc.details = [
                {
                    "field": f"encashments[{idx}]",
                    "message": exc.details[0].get("message", exc.message) if exc.details else exc.message,
                }
            ]
            raise
    if move:
        moving += await out.open_items(session, peek, lock=True, kinds=("encashment", "reimbursement"))
    # ---- 3. the F&F row LAST, then re-check what the checker reviewed
    f = await out.or_404(session, fnf_id, lock=True)
    await _gate(session, actor, f)
    if f.status != "submitted":
        raise AppError("NOT_SUBMITTED", "The settlement is not waiting for a decision")
    if f.version != body.expected_version or f.settlement_period != period or f.user_id != user_id:
        raise AppError("VERSION_CONFLICT", "The settlement was changed meanwhile; reload it")
    plan = out.plan_of(f)
    maker, employee_name = f.created_by, user.full_name
    await _check_exit_bounds(session, user, f.last_working_day)  # the account may have been deactivated since the draft (H1)
    dup = (
        await session.execute(
            select(FnfSettlement.id).where(
                FnfSettlement.user_id == user_id, FnfSettlement.settlement_period == period, FnfSettlement.id != f.id,
                FnfSettlement.status.in_(("approved", "settled")),
            ).limit(1)
        )
    ).first()  # fmt: skip
    if dup is not None:
        raise AppError("FNF_EXISTS", "An approved or settled final settlement already exists for this employee and month")
    now = utcnow()
    created: dict[str, Any] = {"notice": None, "items": [], "encashments": [], "installments": {}}
    new_rows = 0
    # ---- loans must all be resolved
    active = list(
        (
            await session.execute(select(PayrollLoan).where(PayrollLoan.user_id == user_id, PayrollLoan.status == "active"))
        ).scalars()
    )
    modes = {str(x["loan_id"]): x for x in plan["loans"]}
    for lo in active:
        if str(lo.id) not in modes:
            raise _bad("loans", "every active loan needs a mode", "LOAN_UNRESOLVED")
    # ---- notice
    money = await out.notice_money(session, f, structs, settings)
    notice_amt = ZERO
    if f.shortfall_days > 0:
        if money["missing"]:
            raise _bad("notice", "the employee has no salary structure for the notice recovery", "STRUCTURE_MISSING")
        try:
            notice_amt = out.notice_checked(money)
        except AppError:
            raise
        new_rows += 1 if notice_amt > 0 else 0
    new_rows += len(plan["items"]) + len(plan["encashments"])
    new_rows += sum(1 for x in modes.values() if x["mode"] == "payroll")
    # ---- loans (validation before any row is written)
    loan_snap: list[dict[str, Any]] = []
    for i, x in enumerate(plan["loans"]):
        lid = uuid.UUID(str(x["loan_id"]))
        loan, insts = locked_loans[lid]
        if loan.user_id != user_id:
            raise _bad(f"loans[{i}]", "not a loan of the employee", "LOAN_NOT_ACTIVE")
        if loan.status != "active":
            raise _bad(f"loans[{i}]", "the loan is not active", "LOAN_NOT_ACTIVE")
        if any(k.status == "processing" for k in insts):
            raise _bad(f"loans[{i}]", "an installment is inside a processed payslip; reopen the run first", "ITEM_LOCKED")
        if any(k.fnf_id is not None and k.status in loans_svc.LIVE and k.fnf_id != f.id for k in insts):
            raise _bad(f"loans[{i}]", "the loan belongs to another final settlement", "FNF_MANAGED")
        outstanding = calc.q2(Decimal(loan.principal) - loans_svc._sum(insts, "paid"))
        if outstanding <= 0:
            raise _bad(f"loans[{i}]", "nothing is outstanding on this loan", "LOAN_NOT_ACTIVE")
        if x["mode"] == "payroll" and any(k.status == "paid" and k.period == period for k in insts):
            raise _bad(f"loans[{i}]", "an installment of the settlement month was already deducted", "ITEM_LOCKED")
        loan_snap.append({"loan_id": str(lid), "outstanding": calc.money_str(outstanding), "mode": x["mode"]})
    # ---- review M2: what the checker reviewed at submit must still be what is about to be written
    now_snap = _quote_snapshot(
        money, [quotes[i] for i in range(len(plan["encashments"]))], plan,
        {x["loan_id"]: Decimal(x["outstanding"]) for x in loan_snap},
    )  # fmt: skip
    if (f.summary or {}).get("quote") != now_snap:
        raise AppError(
            "FNF_STALE_QUOTE",
            "The notice, encashment or loan amounts changed since the settlement was submitted; reject it or withdraw and submit again",
        )
    # ---- caps of the settlement month (new rows + rows moved into it; moved arrears and claims count in the total)
    to_move = [(k, r, fl) for k, r, fl in moving]
    counts = (await comps.live_counts(session, [user_id], period))[user_id]
    try:
        comps.check_caps(
            counts,
            new_rows + sum(1 for k, _r, _f in to_move if k in ("one_time", "encashment")),
            adding_total=new_rows + len(to_move),
        )
    except AppError as exc:
        raise _bad("items", exc.message, "TOO_MANY_ITEMS") from None
    # ---- write the rows
    enc_snap: list[dict[str, Any]] = []
    if f.shortfall_days > 0 and notice_amt > 0:
        row = PayrollOneTimeItem(
            user_id=user_id, period=period, code="NOTICE_RECOVERY", label=f"Notice period shortfall ({f.shortfall_days} d)",
            amount=calc.money_str(notice_amt), status="pending", version=1, fnf_id=f.id, created_by=actor.id, updated_by=actor.id,
        )  # fmt: skip
        session.add(row)
        await _flush(session)
        created["notice"] = str(row.id)
        comps.add_event(session, item_type="one_time", item=row, action="create", actor_id=actor.id, detail={"fnf_id": str(f.id)})
    for it in plan["items"]:
        amount = Decimal(it["amount"])
        if amount > calc.MAX_MONEY:
            raise _bad("items", "the amount would exceed the maximum allowed")
        row = PayrollOneTimeItem(
            user_id=user_id, period=period, code=it["code"], label=it["label"], amount=calc.money_str(amount), status="pending",
            version=1, fnf_id=f.id, created_by=actor.id, updated_by=actor.id,
        )  # fmt: skip
        session.add(row)
        await _flush(session)
        created["items"].append(str(row.id))
        comps.add_event(session, item_type="one_time", item=row, action="create", actor_id=actor.id, detail={"fnf_id": str(f.id)})
    for idx in range(len(plan["encashments"])):
        q = quotes[idx]
        bal, days = q["balance"], q["days"]
        bal.encashed = leave_svc.d1(bal.encashed) + days
        row = LeaveEncashment(
            user_id=user_id, leave_type_id=q["lt"].id, year=q["year"], days=days, daily_rate=calc.money_str(q["rate"]),
            amount=calc.money_str(q["amount"]), period=period, source="fnf", reason="Final settlement", status="pending",
            version=1, fnf_id=f.id, created_by=actor.id, updated_by=actor.id,
        )  # fmt: skip
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:  # the balance CHECK is the safety net behind the lock
            await session.rollback()
            raise _bad(f"encashments[{idx}]", "not enough leave balance to encash", "INSUFFICIENT_BALANCE") from None
        created["encashments"].append(str(row.id))
        enc_snap.append(
            {"rate": calc.money_str(q["rate"]), "amount": calc.money_str(q["amount"]), "available": leave_svc.num(q["available"])}
        )
        comps.add_event(session, item_type="encashment", item=row, action="create", actor_id=actor.id,
                        detail={"fnf_id": str(f.id), "leave_type": q["lt"].code, "year": q["year"], "days": str(days)})  # fmt: skip
    for i, x in enumerate(plan["loans"]):
        lid = uuid.UUID(str(x["loan_id"]))
        loan, insts = locked_loans[lid]
        outstanding = Decimal(loan_snap[i]["outstanding"])
        for k in insts:
            if k.status == "pending":
                loans_svc._cancel_inst(session, k, actor.id, "foreclose", {"fnf_id": str(f.id)})
        await _flush(session)
        loan.version, loan.updated_by = loan.version + 1, actor.id
        if x["mode"] == "payroll":
            new = loans_svc._new_inst(loan, actor.id, loans_svc._next_seq(insts), period, outstanding, kind="foreclosure")
            new.fnf_id = f.id
            session.add(new)
            await _flush(session)
            created["installments"][str(lid)] = str(new.id)
            comps.add_event(
                session, item_type="loan_installment", item=new, action="create", actor_id=actor.id, detail={"fnf_id": str(f.id)}
            )
            loans_svc._loan_event(session, loan, "foreclose", actor.id, {"fnf_id": str(f.id), "mode": "payroll"})
        else:
            loan.status, loan.close_mode, loan.closed_at = "closed", "foreclosed_outside", now
            loans_svc._loan_event(session, loan, "foreclose", actor.id,
                                  {"fnf_id": str(f.id), "mode": "outside", "paid_on": x["paid_on"], "reference": x["reference"]})  # fmt: skip
    # ---- move open items into the settlement month
    moved_out: list[dict[str, Any]] = []
    counts_moved: dict[str, int] = {}
    for kind, r, fl in to_move:
        prev = r.period
        r.period, r.updated_by, r.version = period, actor.id, r.version + 1
        etype = {"one_time": "one_time", "encashment": "encashment", "arrears": "arrears", "reimbursement": "reimbursement"}[kind]
        comps.add_event(
            session, item_type=etype, item=r, action="update", actor_id=actor.id, prev_period=prev, detail={"fnf_id": str(f.id)}
        )
        counts_moved[kind] = counts_moved.get(kind, 0) + 1
        moved_out.append({"type": kind, "id": str(r.id), "period": pc.period_str(prev), "code": getattr(r, "code", None),
                          "status": r.status, "flags": fl, "action": "move"})  # fmt: skip
    if to_move:
        await _flush(session)
    # ---- the estimate over everything now pending in the settlement month: the net must not be negative (Q5)
    run = (await session.execute(select(PayrollRun).where(PayrollRun.period == period))).scalar_one_or_none()
    open_ps = await out.live_payslip(session, f)  # the open (draft) payslip carries the manual TDS / PT / adjustments (review L4)
    est = await out.estimate(
        session, f, user, await session.get(EmployeePayrollDetails, user_id), [], today,
        run=run, payslip=open_ps if open_ps is not None and open_ps.status == "draft" else None,
    )  # fmt: skip
    if est["result"] is None:
        raise _bad("notice", "the employee has no salary structure for the settlement month", "STRUCTURE_MISSING")
    res = est["result"]
    if res.net < 0:
        raise AppError("FNF_NEGATIVE", "The net amount of the settlement is negative; record a loan as paid outside or reduce the items",
                       details=[{"field": "items", "message": "the net amount is negative"}])  # fmt: skip
    f.summary = {
        "earnings": calc.money_str(res.gross), "deductions": calc.money_str(res.total_deductions),
        "net_estimate": calc.money_str(res.net), "computed_at": now.isoformat(),
        "notice": {k: money[k] for k in ("basis", "divisor", "daily_rate")} | {"amount": calc.money_str(notice_amt)},
        "encashments": enc_snap, "loans": loan_snap, "created": created, "open_items": moved_out,
    }  # fmt: skip
    f.status, f.decided_by, f.decided_at, f.decision_comment = "approved", actor.id, now, body.comment
    f.version, f.updated_by = f.version + 1, actor.id
    # review H1: ONLY the approval (second person + step-up) makes the last working day count for payroll / ESI / leavers
    _set_lwd(session, actor, await _details_row(session, user_id, actor.id), f.last_working_day)
    late = await _paid_after_exit(session, f)
    await _flush(session)
    _event(session, f, "approve", actor.id, amount=calc.money_str(res.net), comment=body.comment, run_status=run_status,
           paid_after_exit=late)  # fmt: skip
    _audit(session, actor, "payroll.fnf.approve", f, moved=counts_moved, paid_after_exit=late)
    await _commit(session)
    await ig._push_maker(session, maker, kind="fnf", entity_id=fnf_id, title="Final settlement approved", body=employee_name)
    return await _one(session, fnf_id)


async def revoke_fnf(session: AsyncSession, redis: Redis, actor: Principal, fnf_id: uuid.UUID, body: FnfRevoke) -> dict[str, Any]:
    peek = await out.or_404(session, fnf_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.fnf.revoke")
    if peek.user_id == actor.id:
        raise await pc.deny(session, actor, entity_type=ENTITY, entity_id=fnf_id, reason="self")
    if peek.status != "approved":
        raise AppError("FNF_NOT_REVOCABLE", "Only an approved settlement can be revoked")
    if peek.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The settlement was changed meanwhile; reload it")
    period, user_id = peek.settlement_period, peek.user_id
    # run rows first (F15): the settlement month and the months the regenerated loan schedule may have to skip to (review L3)
    await items_svc._lock_runs(session, [comps.month_add(period, k) for k in range(REVOKE_MONTHS)])
    await items_svc._lock_users(session, [user_id])
    created = _created_ids(peek)
    item_ids = sorted(
        uuid.UUID(x) for x in ([created["notice"]] if created.get("notice") else []) + list(created.get("items") or [])
    )
    # one-time items, then loans + installments, then balances + encashments, then the F&F row (global order)
    items = []
    if item_ids:
        items = list(
            (
                await session.execute(
                    select(PayrollOneTimeItem)
                    .where(PayrollOneTimeItem.id.in_(item_ids))
                    .order_by(PayrollOneTimeItem.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
    plan = out.plan_of(peek)
    loan_ids = sorted(uuid.UUID(str(x["loan_id"])) for x in plan["loans"])
    locked: dict[uuid.UUID, tuple[PayrollLoan, list[PayrollLoanInstallment]]] = {}
    for lid in loan_ids:
        locked[lid] = await loans_svc._lock_loan(session, lid)
    enc_ids = sorted(uuid.UUID(x) for x in created.get("encashments") or [])
    encs: list[LeaveEncashment] = []
    bals: dict[uuid.UUID, Any] = {}
    if enc_ids:
        encs = list(
            (
                await session.execute(select(LeaveEncashment).where(LeaveEncashment.id.in_(enc_ids)).order_by(LeaveEncashment.id))
            ).scalars()
        )
        for e in sorted(encs, key=lambda r: (str(r.leave_type_id), r.year)):
            b = (
                await session.execute(
                    select(LeaveBalance)
                    .where(
                        LeaveBalance.user_id == e.user_id,
                        LeaveBalance.leave_type_id == e.leave_type_id,
                        LeaveBalance.year == e.year,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            bals[e.id] = b
        encs = list(
            (
                await session.execute(
                    select(LeaveEncashment)
                    .where(LeaveEncashment.id.in_(enc_ids))
                    .order_by(LeaveEncashment.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
    f = await out.or_404(session, fnf_id, lock=True)  # LAST
    if f.status != "approved":
        raise AppError("FNF_NOT_REVOCABLE", "Only an approved settlement can be revoked")
    if f.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The settlement was changed meanwhile; reload it")
    inst_ids = {uuid.UUID(v) for v in (created.get("installments") or {}).values()}
    insts_tagged = [k for _l, ks in locked.values() for k in ks if k.id in inst_ids]
    # the database triggers repeat both checks; the service refuses first with a clear code (review L2)
    if any(r.user_id != f.user_id or r.fnf_id != f.id for r in [*items, *encs, *insts_tagged]):
        raise AppError("CONFLICT", "A line of this settlement does not belong to it or to the employee; contact support")
    if any(r.status in ("processing", "paid") for r in [*items, *encs, *insts_tagged]):
        raise AppError(
            "FNF_NOT_REVOCABLE", "Part of the settlement is inside a processed or published payslip; reopen the run first"
        )
    reason = body.reason
    for r in items:
        if r.status == "pending":
            items_svc._cancel_row(session, r, actor.id, f"Final settlement revoked: {reason}")
    for e in encs:
        if e.status != "pending":
            continue
        b = bals.get(e.id)
        if b is None or leave_svc.d1(b.encashed) < leave_svc.d1(e.days):
            raise AppError("CONFLICT", "The leave balance is inconsistent; contact support")
        b.encashed = leave_svc.d1(b.encashed) - leave_svc.d1(e.days)
        e.status, e.updated_by, e.version = "cancelled", actor.id, e.version + 1
        comps.add_event(
            session,
            item_type="encashment",
            item=e,
            action="cancel",
            actor_id=actor.id,
            detail={"fnf_id": str(f.id), "reason": reason},
        )
    outside: list[dict[str, str]] = []
    for i, x in enumerate(plan["loans"]):
        loan, insts = locked[uuid.UUID(str(x["loan_id"]))]
        if x["mode"] == "outside":
            outside.append({"field": f"loans[{i}]", "message": "closed as paid outside; it stays closed"})
            continue
        tagged = [k for k in insts if k.id in inst_ids and k.status == "pending"]
        for k in tagged:
            loans_svc._cancel_inst(session, k, actor.id, "cancel", {"fnf_id": str(f.id), "reason": reason})
        await _flush(session)
        if loan.status != "active":
            continue
        paid = loans_svc._sum(insts, "paid")
        remaining = calc.q2(Decimal(loan.principal) - paid)
        if remaining > 0:
            first = period
            lives = [k.period for k in insts if k.status in loans_svc.LIVE]
            if lives and first <= max(lives):
                first = comps.month_add(max(lives), 1)
            first = await _first_open_month(session, f.user_id, first)  # review L3
            comps.check_caps((await comps.live_counts(session, [f.user_id], first))[f.user_id])
            seq0 = loans_svc._next_seq(insts) - 1
            emi = min(Decimal(loan.emi), remaining)
            rows = [
                loans_svc._new_inst(loan, actor.id, seq0 + n, per, amt)
                for n, (per, amt) in enumerate(loans_svc.make_schedule(remaining, emi, first), 1)
            ]
            session.add_all(rows)
            await _flush(session)
            for r in rows:
                comps.add_event(
                    session,
                    item_type="loan_installment",
                    item=r,
                    action="create",
                    actor_id=actor.id,
                    detail={"fnf_id": str(f.id), "revoked": True},
                )
        loan.version, loan.updated_by = loan.version + 1, actor.id
        loans_svc._loan_event(session, loan, "reschedule", actor.id, {"fnf_id": str(f.id), "revoked": True})
    f.status, f.summary = "draft", None
    f.submitted_by = f.submitted_at = None
    f.version, f.updated_by = f.version + 1, actor.id
    det = await session.get(EmployeePayrollDetails, f.user_id)  # the approval wrote it: the revoked draft no longer counts (H1)
    if det is not None and det.last_working_day == f.last_working_day:
        _set_lwd(session, actor, det, None)
    await _flush(session)
    _event(session, f, "revoke", actor.id, reason=reason, revoked_by=str(actor.id))
    _audit(session, actor, "payroll.fnf.revoke", f)
    await _commit(session)
    res = await _one(session, fnf_id)
    res["details"] = outside
    return res


REVOKE_MONTHS = 6  # months of run rows locked by a revoke (settlement month + the ones a regenerated loan schedule may skip)


async def _first_open_month(session: AsyncSession, user_id: uuid.UUID, first: dt.date) -> dt.date:
    """The first month from `first` on that accepts a new pay row for the employee (17.0 target month rule): a published run
    without an open draft revision for them is skipped. The run rows were locked by the caller."""
    for k in range(REVOKE_MONTHS):
        month = comps.month_add(first, k)
        try:
            await comps.check_target_period(session, [user_id], month)
        except AppError as exc:
            if exc.code == "PERIOD_LOCKED":
                continue
            raise
        return month
    raise AppError("PERIOD_LOCKED", "The following months are published; open a revision for the employee first")


# ============================================================================================ settlement on publish
async def on_published(session: AsyncSession, p: Payslip, actor_id: uuid.UUID, now: dt.datetime) -> None:
    """Called by `_publish_one` after the pay items were marked paid, inside the publish transaction. Takes the F&F row LAST
    (F15). approved -> settled (final_payslip_id = this payslip); a settled one only carries `final_payslip_id` to a revision."""
    period = (await session.execute(select(PayrollRun.period).where(PayrollRun.id == p.run_id))).scalar_one_or_none()
    if period is None:
        return
    q = (
        select(FnfSettlement)
        .where(FnfSettlement.user_id == p.user_id, FnfSettlement.settlement_period == period, FnfSettlement.status.in_(("approved", "settled")))
        .order_by(FnfSettlement.created_at, FnfSettlement.id)  # deterministic (the unique index allows one per user and month)
        .with_for_update()
        .execution_options(populate_existing=True)
    )  # fmt: skip
    f = (await session.execute(q)).scalars().first()
    if f is None:
        return
    emp_id = (await session.execute(select(User.employee_id).where(User.id == actor_id))).scalar_one_or_none()
    who = SimpleNamespace(id=actor_id, employee_id=emp_id)
    if f.status == "approved":
        await _check_included(session, f, p)
        f.status, f.final_payslip_id, f.version, f.updated_by = "settled", p.id, f.version + 1, actor_id
        await _flush(session)
        _event(session, f, "settle", actor_id, payslip_id=str(p.id))
        _audit(session, who, "payroll.fnf.settle", f)
        session.info.setdefault("fnf_settled", []).append((f.user_id, f.id))
    elif f.final_payslip_id != p.id and p.supersedes_id is not None and f.final_payslip_id == p.supersedes_id:
        await _check_included(session, f, p)
        f.final_payslip_id, f.version, f.updated_by = p.id, f.version + 1, actor_id
        await _flush(session)
        _event(session, f, "settle", actor_id, payslip_id=str(p.id), carried=True)
        _audit(session, who, "payroll.fnf.settle", f, carried=True)


async def _check_included(session: AsyncSession, f: FnfSettlement, p: Payslip) -> None:
    """Every row the approval created must be paid on THIS payslip; else the settlement would be marked settled without its pay."""
    for model in (PayrollOneTimeItem, LeaveEncashment, PayrollLoanInstallment):
        q = (
            select(model.id)
            .where(model.fnf_id == f.id, model.status != "cancelled", (model.status != "paid") | (model.payslip_id != p.id))
            .limit(1)
        )
        if (await session.execute(q)).first() is not None:
            raise AppError(
                "FNF_NOT_INCLUDED", "The final settlement lines are not all in this payslip; reopen, refresh and process again"
            )


async def push_settled(session: AsyncSession) -> None:
    """Best effort, after the publish commit: the employee learns the statement is ready (never amounts)."""
    pending = session.info.pop("fnf_settled", [])
    for user_id, fnf_id in pending:
        try:
            await notifications.notify(
                session, [user_id], type="fnf_settled", entity_id=fnf_id, title="Final settlement completed",
                body="Your final settlement statement is available.",
            )  # fmt: skip
        except Exception:  # noqa: BLE001
            log.exception("fnf_settled push failed (ignored)")


# ============================================================================================ employee side
async def my_fnf(session: AsyncSession, actor: Principal) -> dict[str, Any]:
    q = (
        select(FnfSettlement)
        .where(FnfSettlement.user_id == actor.id, FnfSettlement.status.in_(("submitted", "approved", "settled")))
        .order_by(FnfSettlement.created_at.desc(), FnfSettlement.id)
        .limit(1)
    )  # fmt: skip
    f = (await session.execute(q)).scalars().first()
    if f is None:
        return {"fnf": None}
    return {
        "fnf": {
            "id": str(f.id), "status": "settled" if f.status == "settled" else "in_progress",
            "last_working_day": f.last_working_day.isoformat(), "settlement_period": pc.period_str(f.settlement_period),
            "final_payslip_id": str(f.final_payslip_id) if f.final_payslip_id else None,
        }
    }  # fmt: skip


async def my_settled_or_404(session: AsyncSession, actor: Principal) -> FnfSettlement:
    """Only the caller's own SETTLED settlement; anything else is a 404, audited as a probe (no existence leak)."""
    q = (
        select(FnfSettlement)
        .where(FnfSettlement.user_id == actor.id, FnfSettlement.status == "settled")
        .order_by(FnfSettlement.created_at.desc(), FnfSettlement.id)
        .limit(1)
    )  # fmt: skip
    f = (await session.execute(q)).scalars().first()
    if f is None:
        raise await pc.deny(session, actor, entity_type=ENTITY, entity_id=actor.id, reason="not_own_or_not_settled", status=404)
    return f
