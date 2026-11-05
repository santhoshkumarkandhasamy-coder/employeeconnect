"""Reimbursement claims (plan 15 s.3 "Reimbursements", contract 17.6).

An employee submits a claim with a bill (a `reimbursement_bill` upload). The reporting manager VERIFIES it (or rejects it), then HR
APPROVES the final amount and the payout month. An approved claim is a payable source row of the component engine
(payroll_components, payable status `approved`): it lands on the payslip of its payout month as one REIMBURSEMENT line per
category and moves approved -> processing -> paid with the payslip.

* No actionable manager (none, inactive, or without request.approve_team / approve_all) -> `manager_step = skipped`: straight to HR.
* A manager who also holds `payroll.manage` may verify and approve in ONE call on a `submitted` claim (audited `combined: true`).
* Nobody acts on their own claim; the final approver must differ from the verifier unless the combined path applies.
* Amounts, the description and the comments are encrypted columns; audit rows and push payloads carry ids, category codes and
  periods only.

Lock order (P7): run row (payout month) -> claim row. Submit locks the employee's user row to serialise the monthly claim limit.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import File, ReimbursementClaim, User
from ..schemas.payroll import ClaimApprove, ClaimCreate, ClaimPayout, ClaimReject, ClaimVerify
from . import attendance as att
from . import files as files_svc
from . import leave as leave_svc
from . import notifications
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_items as items_svc
from . import payroll_runs as runs
from . import payroll_settings as pset

log = logging.getLogger("app.payroll")

ENTITY = "reimbursement_claim"
CLAIM = ReimbursementClaim
LIVE = ("approved", "processing", "paid")
PUSH_COALESCE_S = 600
PUSH_MAX_APPROVERS = 20
MANAGER_PERMS = ("request.approve_team", "request.approve_all")


def _d(x: Any) -> Decimal:
    return Decimal(str(x))


def _bad(code: str, field: str, message: str, *, status: int | None = None) -> AppError:
    return AppError(code, message, status=status, details=[{"field": field, "message": message}])


# ============================================================================================ settings
async def _categories(session: AsyncSession) -> list[dict]:
    return list(await pset.current_value(session, "payroll.reimbursement_categories"))


def _cat_name(cats: list[dict], code: str) -> str:
    return next((c["name"] for c in cats if c["code"] == code), code.replace("_", " ").title())


async def categories_out(session: AsyncSession) -> dict[str, Any]:
    cats = await _categories(session)
    return {
        "items": [
            {"code": c["code"], "name": c["name"], "max_amount": c["max_amount"], "bill_required": bool(c["bill_required"])}
            for c in cats
        ],
        "max_age_days": int(await pset.current_value(session, "payroll.reimbursement_max_age_days")),
    }


# ============================================================================================ denied access
async def _deny(session: AsyncSession, actor: Principal, claim_id: Any, reason: str, *, status: int) -> AppError:
    """Audit a refused access (own commit) and return the error: 404 for out-of-scope, 403 for the self rule."""
    who = SimpleNamespace(id=actor.id, employee_id=actor.employee_id)  # plain values survive the rollback
    await session.rollback()
    audit(session, action="reimbursement.denied", actor=who, entity_type=ENTITY, entity_id=claim_id,
          outcome="failure", after={"reason": reason, "status": status})  # fmt: skip
    await session.commit()
    if status == 404:
        return AppError("NOT_FOUND", "Claim not found")
    return AppError("FORBIDDEN", "You cannot act on your own claim")


# ============================================================================================ output
def _employee_status(status: str) -> str:
    return "approved" if status == "processing" else status


async def _files(session: AsyncSession, rows: list[CLAIM]) -> dict[uuid.UUID, File]:
    ids = {r.bill_file_id for r in rows if r.bill_file_id}
    if not ids:
        return {}
    return {f.id: f for f in (await session.execute(select(File).where(File.id.in_(ids)))).scalars()}


async def _out_many(session: AsyncSession, rows: list[CLAIM], *, admin: bool) -> list[dict[str, Any]]:
    cats = await _categories(session)
    people = await items_svc._people(session, {x for r in rows for x in (r.user_id, r.verifier_id, r.approver_id)})
    files = await _files(session, rows)
    states = await comps.row_states(session, rows, payable="approved") if admin else {}
    out = []
    for r in rows:
        f = files.get(r.bill_file_id) if r.bill_file_id else None
        item = {
            "id": str(r.id),
            "category": {"code": r.category, "name": _cat_name(cats, r.category)},
            "expense_date": r.expense_date.isoformat(),
            "amount": calc.money_str(_d(r.amount)),
            "approved_amount": calc.money_str(_d(r.approved_amount)) if r.approved_amount is not None else None,
            "description": r.description,
            "bill": {"url": files_svc.file_url(f.id), "content_type": f.content_type} if f else None,
            "status": r.status if admin else _employee_status(r.status),
            "manager_step": r.manager_step,
            "period": pc.period_str(r.period) if r.period else None,
            "verifier": pc.user_out(people.get(r.verifier_id)),
            "verified_at": pc.iso(r.verified_at),
            "approver": pc.user_out(people.get(r.approver_id)),
            "decided_at": pc.iso(r.decided_at),
            "decision_comment": r.decision_comment,
            "created_at": pc.iso(r.created_at),
            "version": r.version,
        }
        if admin:
            item["user"] = pc.user_out(people.get(r.user_id))
            item["payslip_id"] = str(r.payslip_id) if r.payslip_id else None
            item["combined"] = r.verifier_id is not None and r.verifier_id == r.approver_id  # manager+HR in one call (L1)
            item["run_status"], item["flags"] = states[r.id]
        out.append(item)
    return out


async def _claim(session: AsyncSession, claim_id: uuid.UUID, *, lock: bool = False) -> CLAIM | None:
    q = select(CLAIM).where(CLAIM.id == claim_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(q)).scalar_one_or_none()


def _event(session: AsyncSession, row: CLAIM, action: str, actor_id: uuid.UUID, **kw: Any) -> None:
    comps.add_event(session, item_type="reimbursement", item=row, action=action, actor_id=actor_id, **kw)


# ============================================================================================ manager capability / pushes
async def skip_reason(session: AsyncSession, user: User) -> str | None:
    """Why the manager step cannot run for this employee now (None = a manager can act). L1 audit value."""
    if user.status != "active" or user.deleted_at is not None:
        return "requester_inactive"
    if not user.manager_id:
        return "no_manager"
    m = await session.get(User, user.manager_id)
    if m is None or m.status != "active" or m.deleted_at is not None:
        return "manager_inactive"
    if m.id not in await leave_svc._holders(session, MANAGER_PERMS):
        return "manager_no_perm"
    return None


async def manager_can_act(session: AsyncSession, user: User) -> bool:
    """True when the employee's reporting manager is active and holds request.approve_team / approve_all."""
    if not user.manager_id:
        return False
    m = await session.get(User, user.manager_id)
    if m is None or m.status != "active" or m.deleted_at is not None:
        return False
    return m.id in await leave_svc._holders(session, MANAGER_PERMS)


async def _coalesce(redis: Redis | None, requester_id: uuid.UUID, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Fan-out dedupe: at most one claim push per (requester, approver) per 10 minutes. Redis trouble = no fan-out."""
    if redis is None:
        return ids
    keep = []
    for uid in ids:
        try:
            if await redis.set(f"push:claim:{requester_id}:{uid}", "1", nx=True, ex=PUSH_COALESCE_S):
                keep.append(uid)
        except Exception:  # noqa: BLE001
            log.error("claim push coalescing unavailable; skipping fan-out push")
            return []
    return keep


async def _push_to_hr(session: AsyncSession, redis: Redis | None, claim_id: uuid.UUID, user_id: uuid.UUID, name: str) -> None:
    ids = (await leave_svc._holders(session, ("payroll.manage",), exclude=user_id))[:PUSH_MAX_APPROVERS]
    ids = await _coalesce(redis, user_id, ids)
    if ids:
        await notifications.notify(
            session, ids, type="reimbursement_submitted", entity_id=claim_id, title="Expense claim to approve", body=name
        )  # fmt: skip


async def _push_submitted(
    session: AsyncSession, redis: Redis | None, claim_id: uuid.UUID, user_id: uuid.UUID, name: str, manager_id: uuid.UUID | None
) -> None:
    """Best effort, after commit; the body is the employee name only (never amounts)."""
    try:
        if manager_id is not None:
            await notifications.notify(
                session, [manager_id], type="reimbursement_submitted", entity_id=claim_id, title="Expense claim to verify", body=name
            )  # fmt: skip
        else:
            await _push_to_hr(session, redis, claim_id, user_id, name)
    except Exception:  # noqa: BLE001
        log.exception("reimbursement_submitted push failed (ignored)")


async def _push_verified(session: AsyncSession, redis: Redis | None, claim_id: uuid.UUID, user_id: uuid.UUID, name: str) -> None:
    try:
        await _push_to_hr(session, redis, claim_id, user_id, name)
    except Exception:  # noqa: BLE001
        log.exception("reimbursement_submitted push failed (ignored)")


async def _push_decision(session: AsyncSession, claim_id: uuid.UUID, user_id: uuid.UUID, category: str, status: str) -> None:
    try:
        name = _cat_name(await _categories(session), category)
        await notifications.notify(
            session, [user_id], type="reimbursement_decision", entity_id=claim_id, title=f"Expense claim {status}",
            body=f"Your {name} claim was {status}.",
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.exception("reimbursement_decision push failed (ignored)")


# ============================================================================================ employee side
async def submit(session: AsyncSession, redis: Redis, actor: Principal, body: ClaimCreate) -> dict[str, Any]:
    cats = await _categories(session)
    cat = next((c for c in cats if c["code"] == body.category), None)
    if cat is None:
        raise _bad("INVALID_CATEGORY", "category", "unknown or inactive category")
    today = await runs._today(session)
    max_age = int(await pset.current_value(session, "payroll.reimbursement_max_age_days"))
    if body.expense_date > today or body.expense_date < today - dt.timedelta(days=max_age):
        raise _bad("INVALID_DATE", "expense_date", f"must not be in the future or older than {max_age} days")
    if cat["max_amount"] is not None and body.amount > _d(cat["max_amount"]):
        raise _bad("AMOUNT_ABOVE_LIMIT", "amount", cat["max_amount"])
    bill: File | None = None
    if body.bill_file_id is not None:
        bill = await session.get(File, body.bill_file_id)
        if bill is None or bill.owner_id != actor.id or bill.purpose != "reimbursement_bill":
            raise _bad("VALIDATION_ERROR", "bill_file_id", "upload your bill first (purpose reimbursement_bill)")
        if (await session.execute(select(CLAIM.id).where(CLAIM.bill_file_id == bill.id).limit(1))).first():
            raise _bad("VALIDATION_ERROR", "bill_file_id", "this upload already backs a claim")
    elif cat["bill_required"]:
        raise _bad("BILL_REQUIRED", "bill_file_id", "a bill is required for this category")
    await items_svc._lock_users(session, [actor.id])  # serialises the monthly limit and the duplicate-bill check
    if bill is not None and bill.sha256:
        if session.get_bind().dialect.name == "postgresql":  # two employees uploading the same bytes at once
            from sqlalchemy import text

            await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:h))"), {"h": bill.sha256})
        dup = (
            await session.execute(
                select(CLAIM.id)
                .join(File, File.id == CLAIM.bill_file_id)
                .where(File.sha256 == bill.sha256, CLAIM.status.not_in(("rejected", "cancelled")))
                .limit(1)
            )
        ).first()
        if dup:
            raise AppError("DUPLICATE_BILL", "This bill was already used for another claim")
    tz = att.office_tz(await att.load_settings(session))
    month_start = dt.datetime(today.year, today.month, 1, tzinfo=tz).astimezone(dt.UTC)
    limit = int(await pset.current_value(session, "payroll.reimbursement_max_claims_per_month"))
    n = (
        await session.execute(
            select(func.count()).select_from(CLAIM).where(CLAIM.user_id == actor.id, CLAIM.created_at >= month_start)
        )
    ).scalar_one()
    if n >= limit:
        raise AppError("CLAIM_LIMIT_REACHED", "You reached the maximum number of claims for this month")
    can_act = await manager_can_act(session, actor.user)
    row = CLAIM(
        user_id=actor.id, category=body.category, expense_date=body.expense_date, amount=calc.money_str(body.amount),
        description=body.description, bill_file_id=bill.id if bill else None, status="submitted",
        manager_step="pending" if can_act else "skipped", version=1, created_by=actor.id, updated_by=actor.id,
    )  # fmt: skip
    session.add(row)
    await pc.flush(session)
    _event(session, row, "create", actor.id, detail={"category": row.category})
    audit(session, action="reimbursement.submit", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"category": row.category, "manager_step": row.manager_step})  # fmt: skip
    claim_id, name, manager_id = row.id, actor.user.full_name, actor.user.manager_id if can_act else None
    await pc.commit(session)
    await _push_submitted(session, redis, claim_id, actor.id, name, manager_id)
    return (await _out_many(session, [await _claim(session, claim_id)], admin=False))[0]


async def list_own(
    session: AsyncSession, actor: Principal, status: str | None, year: int | None, page: PageParams
) -> dict[str, Any]:
    cond = [CLAIM.user_id == actor.id]
    if status:
        cond.append(CLAIM.status.in_(("approved", "processing")) if status == "approved" else CLAIM.status == status)
    if year:
        cond.append(CLAIM.expense_date >= dt.date(year, 1, 1))
        cond.append(CLAIM.expense_date <= dt.date(year, 12, 31))
    total = (await session.execute(select(func.count()).select_from(CLAIM).where(*cond))).scalar_one()
    q = select(CLAIM).where(*cond).order_by(CLAIM.created_at.desc(), CLAIM.id).offset(page.offset).limit(page.page_size)
    return make_page(await _out_many(session, list((await session.execute(q)).scalars()), admin=False), page, total)


async def get_own(session: AsyncSession, actor: Principal, claim_id: uuid.UUID) -> dict[str, Any]:
    row = await _claim(session, claim_id)
    if row is None or row.user_id != actor.id:
        raise await _deny(session, actor, claim_id, "not_owner", status=404)
    return (await _out_many(session, [row], admin=False))[0]


async def cancel(session: AsyncSession, actor: Principal, claim_id: uuid.UUID) -> dict[str, Any]:
    peek = await _claim(session, claim_id)
    if peek is None or peek.user_id != actor.id:
        raise await _deny(session, actor, claim_id, "not_owner", status=404)
    row = await _claim(session, claim_id, lock=True)
    if row.status not in ("submitted", "verified"):
        raise AppError("NOT_PENDING", f"The claim is {_employee_status(row.status)} and can no longer be cancelled")
    row.status, row.updated_by, row.version = "cancelled", actor.id, row.version + 1
    await pc.flush(session)
    _event(session, row, "cancel", actor.id)
    audit(session, action="reimbursement.cancel", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"category": row.category})  # fmt: skip
    await pc.commit(session)
    return (await _out_many(session, [await _claim(session, claim_id)], admin=False))[0]


# ============================================================================================ admin reads
def _is_report(actor: Principal, user: User) -> bool:
    return user.manager_id == actor.id and user.id != actor.id


def _manager_reader(actor: Principal) -> bool:
    return actor.has("request.view_team") or any(actor.has(p) for p in MANAGER_PERMS)


async def list_admin(
    session: AsyncSession,
    actor: Principal,
    *,
    stage: str,
    status: str | None,
    employee_id: str | None,
    period: str | None,
    category: str | None,
    page: PageParams,
) -> dict[str, Any]:
    cond: list[Any] = []
    if stage == "manager":
        if not _manager_reader(actor):
            raise AppError("FORBIDDEN", "You do not have permission to perform this action")
        cond += [CLAIM.user_id.in_(select(User.id).where(User.manager_id == actor.id, User.id != actor.id))]
        if status is None:
            cond += [CLAIM.status == "submitted", CLAIM.manager_step == "pending"]
    else:
        if not actor.has("payroll.view_all"):
            raise AppError("FORBIDDEN", "You do not have permission to perform this action")
        if stage == "hr" and status is None:
            cond.append(or_(CLAIM.status == "verified", (CLAIM.status == "submitted") & (CLAIM.manager_step == "skipped")))
    if status:
        cond.append(CLAIM.status == status)
    if category:
        cond.append(CLAIM.category == category)
    if period:
        cond.append(CLAIM.period == comps.parse_target_period(period, await runs._today(session)))
    if employee_id:
        cond.append(CLAIM.user_id.in_(select(User.id).where(func.upper(User.employee_id) == employee_id.strip().upper())))
    total = (await session.execute(select(func.count()).select_from(CLAIM).where(*cond))).scalar_one()
    q = select(CLAIM).where(*cond).order_by(CLAIM.created_at.desc(), CLAIM.id).offset(page.offset).limit(page.page_size)
    out = make_page(await _out_many(session, list((await session.execute(q)).scalars()), admin=True), page, total)
    await pc.audit_read(session, actor, "reimbursement.list", ENTITY, count=len(out["items"]))
    return out


async def _scoped(session: AsyncSession, actor: Principal, claim_id: uuid.UUID, *, manager_ok: bool) -> CLAIM:
    """Admin read/act scope: payroll.view_all sees everything; a manager (when `manager_ok`) only direct reports. Else 404."""
    row = await _claim(session, claim_id)
    if row is None:
        raise AppError("NOT_FOUND", "Claim not found")
    if actor.has("payroll.view_all"):
        return row
    owner = await session.get(User, row.user_id)
    if manager_ok and owner is not None and _is_report(actor, owner):
        return row
    raise await _deny(session, actor, claim_id, "out_of_scope", status=404)


async def get_admin(session: AsyncSession, actor: Principal, claim_id: uuid.UUID) -> dict[str, Any]:
    row = await _scoped(session, actor, claim_id, manager_ok=_manager_reader(actor))
    out = (await _out_many(session, [row], admin=True))[0]
    audit(session, action="reimbursement.view", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"category": row.category})  # fmt: skip
    await pc.commit(session)
    return out


# ============================================================================================ manager step
def _not_pending(row: CLAIM) -> AppError:
    return AppError("NOT_PENDING", f"The claim is {row.status} and cannot be changed this way")


async def verify(session: AsyncSession, redis: Redis, actor: Principal, claim_id: uuid.UUID, body: ClaimVerify) -> dict[str, Any]:
    peek = await _claim(session, claim_id)
    if peek is None:
        raise AppError("NOT_FOUND", "Claim not found")
    if peek.user_id == actor.id:
        raise await _deny(session, actor, claim_id, "self", status=403)
    owner = await session.get(User, peek.user_id)
    if owner is None or not _is_report(actor, owner):
        raise await _deny(session, actor, claim_id, "out_of_scope", status=404)
    row = await _claim(session, claim_id, lock=True)
    if row.status != "submitted" or row.manager_step != "pending":
        raise _not_pending(row)
    if owner.status != "active" or owner.deleted_at is not None:
        raise AppError("REQUESTER_INACTIVE", "The employee is no longer active; HR can decide this claim")
    now = utcnow()
    row.status, row.manager_step, row.verifier_id, row.verified_at = "verified", "done", actor.id, now
    row.updated_by, row.version = actor.id, row.version + 1
    await pc.flush(session)
    _event(session, row, "verify", actor.id, detail={"comment": body.comment} if body.comment else None)
    audit(session, action="reimbursement.verify", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"user_id": str(row.user_id), "category": row.category})  # fmt: skip
    user_id, name = row.user_id, owner.full_name
    await pc.commit(session)
    await _push_verified(session, redis, claim_id, user_id, name)
    return (await _out_many(session, [await _claim(session, claim_id)], admin=True))[0]


async def reject(session: AsyncSession, actor: Principal, claim_id: uuid.UUID, body: ClaimReject) -> dict[str, Any]:
    peek = await _claim(session, claim_id)
    if peek is None:
        raise AppError("NOT_FOUND", "Claim not found")
    if peek.user_id == actor.id:
        raise await _deny(session, actor, claim_id, "self", status=403)
    hr = actor.has("payroll.manage")
    if not hr:
        owner = await session.get(User, peek.user_id)
        if owner is None or not _is_report(actor, owner):
            raise await _deny(session, actor, claim_id, "out_of_scope", status=404)
    row = await _claim(session, claim_id, lock=True)
    ok = ("submitted", "verified") if hr else ("submitted",)
    if row.status not in ok or (not hr and row.manager_step != "pending"):  # L3: a manager only rejects at their own step
        raise _not_pending(row)
    row.status, row.approver_id, row.decided_at, row.decision_comment = "rejected", actor.id, utcnow(), body.comment
    row.updated_by, row.version = actor.id, row.version + 1
    await pc.flush(session)
    _event(session, row, "reject", actor.id, detail={"comment": body.comment})
    audit(session, action="reimbursement.reject", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"user_id": str(row.user_id), "category": row.category, "stage": "hr" if hr else "manager"})  # fmt: skip
    user_id, category = row.user_id, row.category
    await pc.commit(session)
    await _push_decision(session, claim_id, user_id, category, "rejected")
    return (await _out_many(session, [await _claim(session, claim_id)], admin=True))[0]


# ============================================================================================ HR step
async def approve(session: AsyncSession, actor: Principal, claim_id: uuid.UUID, body: ClaimApprove) -> dict[str, Any]:
    today = await runs._today(session)
    period = comps.parse_target_period(body.period, today)
    peek = await _claim(session, claim_id)
    if peek is None:
        raise AppError("NOT_FOUND", "Claim not found")
    if peek.user_id == actor.id:
        raise await _deny(session, actor, claim_id, "self", status=403)
    owner = await session.get(User, peek.user_id)
    run_status = await comps.check_target_period(session, [peek.user_id], period, lock=True)  # run row first (P7)
    row = await _claim(session, claim_id, lock=True)
    if row.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The claim was changed by someone else; reload it")
    combined, skipped_reason = False, None
    if row.status == "verified":
        if row.verifier_id == actor.id:
            raise AppError("SECOND_PERSON_REQUIRED", "A second person must approve a claim you verified", status=403)
    elif row.status == "submitted":
        step_skipped = (
            row.manager_step == "skipped"
            or owner.status != "active"
            or owner.deleted_at is not None
            or not await manager_can_act(session, owner)
        )
        if step_skipped:
            skipped_reason = await skip_reason(session, owner) or "at_submit"
            row.manager_step = "skipped"
        elif _is_report(actor, owner):
            combined = True  # the employee's manager also holds payroll.manage: both steps in one logged call
            row.manager_step, row.verifier_id, row.verified_at = "done", actor.id, utcnow()
        else:
            raise AppError("NOT_PENDING", "The reporting manager has not verified this claim yet")
    else:
        raise _not_pending(row)
    await items_svc._eligible_user(session, row.user_id, period)
    approved = body.approved_amount
    if approved > _d(row.amount):
        raise _bad("AMOUNT_ABOVE_LIMIT", "approved_amount", "must not exceed the claimed amount")
    row.status, row.approved_amount, row.period = "approved", calc.money_str(approved), period
    row.approver_id, row.decided_at, row.decision_comment = actor.id, utcnow(), body.comment
    row.updated_by, row.version = actor.id, row.version + 1
    await pc.flush(session)
    _event(session, row, "approve", actor.id, detail={"comment": body.comment, "combined": combined})
    audit(session, action="reimbursement.approve", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"user_id": str(row.user_id), "category": row.category, "period": pc.period_str(period),
                 "combined": combined, "step_skipped_reason": skipped_reason, "run_status": run_status})  # fmt: skip
    user_id, category = row.user_id, row.category
    await pc.commit(session)
    await _push_decision(session, claim_id, user_id, category, "approved")
    return (await _out_many(session, [await _claim(session, claim_id)], admin=True))[0]


async def payout(session: AsyncSession, actor: Principal, claim_id: uuid.UUID, body: ClaimPayout) -> dict[str, Any]:
    today = await runs._today(session)
    period = comps.parse_target_period(body.period, today)
    peek = await _claim(session, claim_id)
    if peek is None:
        raise AppError("NOT_FOUND", "Claim not found")
    if peek.user_id == actor.id:
        raise await _deny(session, actor, claim_id, "self", status=403)
    run_status = await comps.check_target_period(session, [peek.user_id], period, lock=True)
    row = await _claim(session, claim_id, lock=True)
    if row.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The claim was changed by someone else; reload it")
    if row.status in ("processing", "paid"):
        raise AppError("ITEM_LOCKED", f"The claim is {row.status} and its payout month can no longer be changed")
    if row.status != "approved":
        raise _not_pending(row)
    await items_svc._eligible_user(session, row.user_id, period)
    prev = row.period
    row.period, row.updated_by, row.version = period, actor.id, row.version + 1
    await pc.flush(session)
    _event(session, row, "update", actor.id, prev_period=prev, detail={"payout_moved": True})
    audit(session, action="reimbursement.reschedule", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"user_id": str(row.user_id), "from": pc.period_str(prev) if prev else None,
                 "period": pc.period_str(period), "run_status": run_status})  # fmt: skip
    await pc.commit(session)
    return (await _out_many(session, [await _claim(session, claim_id)], admin=True))[0]


# ============================================================================================ component engine provider
async def _prepare(session: AsyncSession, rows: list[CLAIM]) -> None:
    cats = await _categories(session)
    for r in rows:
        r.line_name = _cat_name(cats, r.category)


def _lines(rows: list[CLAIM]) -> list[dict]:
    """One REIMBURSEMENT line per category: the sum of the approved amounts, every claim listed in `sources`."""
    by_cat: dict[str, list[CLAIM]] = {}
    for r in rows:
        by_cat.setdefault(r.category, []).append(r)
    out = []
    for code in sorted(by_cat):
        rs = by_cat[code]
        total = calc.q2(sum((_d(r.approved_amount) for r in rs), Decimal(0)))
        out.append(
            {
                "code": "REIMBURSEMENT",
                "label": f"Reimbursement - {getattr(rs[0], 'line_name', code.title())}"[:60],
                "amount": calc.money_str(total),
                "sources": [{"type": "reimbursement", "id": str(r.id)} for r in rs],
            }
        )
    return out


comps.register(
    comps.Provider(
        "reimbursement", ReimbursementClaim, _lines, LIVE, prepare=_prepare, order=50, payable="approved", count_in_caps=False
    )
)
