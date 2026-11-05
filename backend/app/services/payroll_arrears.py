"""Auto arrears (plan 15 s.3, contract 17.4): proposals, never automatic money.

A structure create / update / delete that reaches a month with a PUBLISHED payslip makes `on_structure_change` recompute
the employee's open proposal in the same transaction: for every published month the structure now in force is compared with
the one the live payslip used (earnings of BASIC, HRA and allowances for the same basis / paid days and the run's rounding
rules), minus arrears already paid or processing for that month. The total becomes ONE proposal per employee
(ARREARS when positive, OTHER_DEDUCTION when negative, nothing under 1.00). HR accepts it into an open month (step-up);
from then on it is a payable source row of the component engine. Arrears are not PF or ESI wages (plan 15 Q1).

Lock order (P7): run row (target month) -> payroll_arrears. Payslips are only read here, never locked.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import PayrollArrears, PayrollRun, Payslip, SalaryStructure
from ..schemas.payroll import ArrearsAccept, ArrearsBulkAccept
from . import admin_security
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_items as items_svc
from . import payroll_runs as runs
from . import payroll_structures as structs_svc

log = logging.getLogger("app.payroll")

ENTITY = "payroll_arrears"
MIN_ABS = Decimal("1.00")
OPEN = ("proposed", "pending")
PAID_STATES = ("processing", "paid")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def _d(x: str | Decimal) -> Decimal:
    return Decimal(x)


# ============================================================================================ calculation
async def _published_months(session: AsyncSession, user_id: uuid.UUID) -> list[tuple[Payslip, PayrollRun]]:
    q = (
        select(Payslip, PayrollRun)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(Payslip.user_id == user_id, Payslip.status == "published")
        .order_by(PayrollRun.period)
    )
    return [(ps, run) for ps, run in (await session.execute(q)).all() if ps.result is not None]


async def latest_published_month(session: AsyncSession, user_id: uuid.UUID) -> dt.date | None:
    months = await _published_months(session, user_id)
    return months[-1][1].period if months else None


def _paid_earnings(result: dict) -> tuple[Decimal, Decimal]:
    """(structure lines total, basic) of a payslip result: the earning lines that carry a `monthly` amount."""
    total = Decimal(0)
    basic = Decimal("0.00")
    for e in result.get("earnings", []):
        if e.get("monthly") is not None:
            total += _d(e["amount"])
            if e["code"] == "BASIC":
                basic = _d(e["amount"])
    return calc.q2(total), basic


async def compute(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any] | None:
    """The proposal for the employee as of now, or None when there is no (material) difference. Writes nothing."""
    months = await _published_months(session, user_id)
    if not months:
        return None
    structures = list((await session.execute(select(SalaryStructure).where(SalaryStructure.user_id == user_id))).scalars())
    timeline = [(s.effective_from, s) for s in structures]
    prior = (
        await session.execute(
            select(PayrollArrears).where(PayrollArrears.user_id == user_id, PayrollArrears.status.in_(PAID_STATES))
        )
    ).scalars()
    already: dict[str, Decimal] = {}
    for a in prior:  # signed per-month diffs that were (or are being) paid
        for entry in a.breakdown:
            already[entry["period"]] = already.get(entry["period"], Decimal(0)) + _d(entry["diff"])
    breakdown: list[dict[str, Any]] = []
    for ps, run in months:
        new = calc.structure_for(timeline, run.period)
        if new is None:
            continue
        key = pc.period_str(run.period)
        done = calc.q2(already.get(key, Decimal(0)))
        if new.id == ps.structure_id and done == 0:
            continue  # same structure and nothing paid: no difference. With arrears already paid the month MUST be
            # compared (a revision that adopted the new structure would otherwise pay the same difference twice, H1)
        rules = calc.Rules.from_config(run.config or {})
        basic_new, new_total = calc.structure_earnings(structs_svc.data_of(new), rules, _d(ps.basis_days), _d(ps.paid_days))
        paid_total, paid_basic = _paid_earnings(ps.result)
        diff = calc.q2(new_total - paid_total - done)
        if diff == 0:
            continue
        breakdown.append(
            {
                "period": key,
                "paid_days": float(ps.paid_days),
                "paid_structure_id": str(ps.structure_id) if ps.structure_id else None,
                "new_structure_id": str(new.id),
                "paid_earnings": calc.money_str(paid_total),
                "new_earnings": calc.money_str(new_total),
                "already_paid_arrears": calc.money_str(done),
                "diff": calc.money_str(diff),
                "basic_diff": calc.money_str(basic_new - paid_basic),
            }
        )
    if not breakdown:
        return None
    total = calc.q2(sum((_d(b["diff"]) for b in breakdown), Decimal(0)))
    if abs(total) < MIN_ABS:
        return None
    return {
        "code": "ARREARS" if total > 0 else "OTHER_DEDUCTION",
        "amount": calc.money_str(abs(total)),
        "breakdown": breakdown,
        "from_period": dt.date.fromisoformat(breakdown[0]["period"] + "-01"),
        "to_period": dt.date.fromisoformat(breakdown[-1]["period"] + "-01"),
        "trigger_structure_id": uuid.UUID(breakdown[0]["new_structure_id"]),
    }


# ============================================================================================ sync (the in-place proposal)
async def sync_proposal(session: AsyncSession, user_id: uuid.UUID, actor_id: uuid.UUID | None) -> PayrollArrears | None:
    """Recompute the employee's open proposal in place (create / refresh / dismiss). The caller commits.
    `processing` and `paid` rows are never touched; the computation subtracts them."""
    await session.flush()  # a structure delete / update of the caller must be visible to the queries below
    q = (
        select(PayrollArrears)
        .where(PayrollArrears.user_id == user_id, PayrollArrears.status.in_(OPEN))
        .order_by(PayrollArrears.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    open_rows = list((await session.execute(q)).scalars())
    result = await compute(session, user_id)
    if result is None:
        for r in open_rows:
            r.status, r.dismiss_reason, r.version = "dismissed", "No difference after recalculation", r.version + 1
            r.updated_by = actor_id
            comps.add_event(session, item_type="arrears", item=r, action="dismiss", actor_id=None)
        await pc.flush(session)
        return None
    row = open_rows[0] if open_rows else None
    for extra in open_rows[1:]:  # cannot happen (one open row per employee); keep the table tidy anyway
        extra.status, extra.dismiss_reason, extra.version = "dismissed", "Superseded", extra.version + 1
        comps.add_event(session, item_type="arrears", item=extra, action="dismiss", actor_id=None)
    if row is not None and (row.code, row.amount, row.breakdown) == (result["code"], result["amount"], result["breakdown"]):
        return row  # unchanged: no version churn, an accepted row stays accepted
    if row is None:
        row = PayrollArrears(
            user_id=user_id, status="proposed", version=1, created_by=actor_id, updated_by=actor_id, period=None,
            code=result["code"], amount=result["amount"], breakdown=result["breakdown"],
            from_period=result["from_period"], to_period=result["to_period"],
            trigger_structure_id=result["trigger_structure_id"],
        )  # fmt: skip
        session.add(row)
        await pc.flush(session)
        comps.add_event(session, item_type="arrears", item=row, action="propose", actor_id=actor_id)
        return row
    prev_amount, prev_period = row.amount, row.period
    row.code, row.amount, row.breakdown = result["code"], result["amount"], result["breakdown"]
    row.from_period, row.to_period = result["from_period"], result["to_period"]
    row.trigger_structure_id = result["trigger_structure_id"]
    row.status, row.period = "proposed", None  # an accepted-but-pending row goes back to HR
    row.version, row.updated_by = row.version + 1, actor_id
    await pc.flush(session)
    comps.add_event(session, item_type="arrears", item=row, action="propose", actor_id=actor_id,
                    prev_amount=prev_amount, prev_period=prev_period)  # fmt: skip
    return row


async def on_structure_change(
    session: AsyncSession, actor: Principal, user_id: uuid.UUID, effective_dates: list[dt.date]
) -> None:
    """Called by the structure endpoints inside their transaction. Only structures that reach a month with a published
    payslip matter (contract 17.4)."""
    latest = await latest_published_month(session, user_id)
    if latest is None or min(effective_dates) > latest:
        return
    row = await sync_proposal(session, user_id, actor.id)
    audit(session, action="payroll.arrears.propose", actor=actor, entity_type=ENTITY, entity_id=row.id if row else None,
          after={"user_id": str(user_id), "proposal": row is not None, "trigger": "structure"})  # fmt: skip


# ============================================================================================ output
async def _out_many(session: AsyncSession, rows: list[PayrollArrears]) -> list[dict[str, Any]]:
    people = await items_svc._people(session, {r.user_id for r in rows})
    states = await comps.row_states(session, rows)
    sids = {r.trigger_structure_id for r in rows if r.trigger_structure_id}
    structs = {}
    if sids:
        structs = {
            s.id: s for s in (await session.execute(select(SalaryStructure).where(SalaryStructure.id.in_(sids)))).scalars()
        }
    out = []
    for r in rows:
        s = structs.get(r.trigger_structure_id)
        state = states[r.id]
        out.append(
            {
                "id": str(r.id),
                "user": pc.user_out(people.get(r.user_id)),
                "code": r.code,
                "amount": calc.money_str(_d(r.amount)),
                "from_period": pc.period_str(r.from_period),
                "to_period": pc.period_str(r.to_period),
                "period": pc.period_str(r.period) if r.period else None,
                "status": r.status,
                "run_status": state[0] if r.period else None,
                "flags": state[1],
                "trigger_structure": {"id": str(s.id), "effective_from": s.effective_from.isoformat()} if s else None,
                "breakdown": r.breakdown,
                "payslip_id": str(r.payslip_id) if r.payslip_id else None,
                "version": r.version,
                "created_at": pc.iso(r.created_at),
                "updated_at": pc.iso(r.updated_at),
            }
        )
    return out


async def _one(session: AsyncSession, arrears_id: uuid.UUID) -> dict[str, Any]:
    row = await _or_404(session, arrears_id)
    await session.refresh(row)
    return (await _out_many(session, [row]))[0]


async def _or_404(session: AsyncSession, arrears_id: uuid.UUID, *, lock: bool = False) -> PayrollArrears:
    q = select(PayrollArrears).where(PayrollArrears.id == arrears_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = (await session.execute(q)).scalar_one_or_none()
    if row is None:
        raise AppError("NOT_FOUND", "Arrears not found")
    return row


async def list_arrears(
    session: AsyncSession, *, status: str | None, user_id: uuid.UUID | None, page: PageParams
) -> dict[str, Any]:
    cond = []
    if status:
        cond.append(PayrollArrears.status == status)
    if user_id:
        cond.append(PayrollArrears.user_id == user_id)
    total = (await session.execute(select(func.count()).select_from(PayrollArrears).where(*cond))).scalar_one()
    q = (
        select(PayrollArrears).where(*cond).order_by(PayrollArrears.created_at.desc(), PayrollArrears.id)
        .offset(page.offset).limit(page.page_size)
    )  # fmt: skip
    return make_page(await _out_many(session, list((await session.execute(q)).scalars())), page, total)


async def get_arrears(session: AsyncSession, actor: Principal, arrears_id: uuid.UUID) -> dict[str, Any]:
    out = await _one(session, arrears_id)
    audit(session, action="payroll.arrears.view", actor=actor, entity_type=ENTITY, entity_id=arrears_id,
          after={"user_id": out["user"]["id"] if out["user"] else None})  # fmt: skip
    await pc.commit(session)
    return out


# ============================================================================================ actions
async def recalculate(session: AsyncSession, actor: Principal, user_id: uuid.UUID) -> dict[str, Any]:
    await structs_svc.get_user_or_404(session, user_id)
    await pc.forbid_self(session, actor, user_id, entity_type=ENTITY, entity_id=user_id)
    row = await sync_proposal(session, user_id, actor.id)
    audit(session, action="payroll.arrears.recalculate", actor=actor, entity_type=ENTITY, entity_id=row.id if row else None,
          after={"user_id": str(user_id), "proposal": row is not None})  # fmt: skip
    rid = row.id if row else None
    await pc.commit(session)
    return {"arrears": await _one(session, rid) if rid else None}


async def accept(
    session: AsyncSession, redis: Redis, actor: Principal, arrears_id: uuid.UUID, body: ArrearsAccept
) -> dict[str, Any]:
    today = await runs._today(session)
    target = comps.parse_target_period(body.period, today)
    peek = await _or_404(session, arrears_id)
    await pc.forbid_self(session, actor, peek.user_id, entity_type=ENTITY, entity_id=arrears_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.arrears.accept")
    await items_svc._eligible_user(session, peek.user_id, target)
    await items_svc._lock_runs(session, [target])  # run row first (P7)
    row = await _or_404(session, arrears_id, lock=True)
    if row.status in PAID_STATES:
        raise AppError("ITEM_LOCKED", f"The arrears are {row.status} and can no longer be changed")
    if row.status != "proposed":
        raise AppError("NOT_PROPOSED", "These arrears are not an open proposal")
    if row.version != body.expected_version:
        raise AppError("ARREARS_STALE", "The proposal was recalculated meanwhile; reload it")
    run_status = await comps.check_target_period(session, [row.user_id], target)
    await items_svc._lock_users(session, [row.user_id])
    comps.check_caps((await comps.live_counts(session, [row.user_id], target))[row.user_id])
    row.status, row.period, row.updated_by, row.version = "pending", target, actor.id, row.version + 1
    await pc.flush(session)
    comps.add_event(session, item_type="arrears", item=row, action="accept", actor_id=actor.id)
    audit(session, action="payroll.arrears.accept", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"user_id": str(row.user_id), "period": pc.period_str(target), "run_status": run_status})  # fmt: skip
    await pc.commit(session)
    return await _one(session, arrears_id)


async def bulk_accept(session: AsyncSession, redis: Redis, actor: Principal, body: ArrearsBulkAccept) -> dict[str, Any]:
    """Plan 16 / contract 18.7: the 17.4 accept for up to 200 proposals in ONE transaction. One step-up, run row first, then the
    employees, then the arrears rows (global lock order). A row that fails a check is skipped with its reason; the others go on."""
    today = await runs._today(session)
    target = comps.parse_target_period(body.period, today)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.arrears.bulk_accept")
    wanted = {i.id: i.expected_version for i in body.items}
    peek = list((await session.execute(select(PayrollArrears).where(PayrollArrears.id.in_(list(wanted))))).scalars())
    skipped: list[dict[str, str]] = []
    found = {r.id: r for r in peek}
    for rid in wanted:
        if rid not in found:
            skipped.append({"id": str(rid), "reason": "NOT_FOUND"})
    await items_svc._lock_runs(session, [target])  # run row first (P7)
    mine = [r for r in found.values() if r.user_id != actor.id]
    for r in found.values():
        if r.user_id == actor.id:
            skipped.append({"id": str(r.id), "reason": "FORBIDDEN"})
    await items_svc._lock_users(session, [r.user_id for r in mine])
    q = (
        select(PayrollArrears).where(PayrollArrears.id.in_([r.id for r in mine])).order_by(PayrollArrears.id)
        .with_for_update().execution_options(populate_existing=True)
    )  # fmt: skip
    rows = list((await session.execute(q)).scalars()) if mine else []
    counts = await comps.live_counts(session, list({r.user_id for r in rows}), target) if rows else {}
    accepted = 0
    for row in rows:
        reason: str | None = None
        if row.status in PAID_STATES:
            reason = "ITEM_LOCKED"
        elif row.status != "proposed":
            reason = "NOT_PROPOSED"
        elif row.version != wanted[row.id]:
            reason = "ARREARS_STALE"
        else:
            try:
                await items_svc._eligible_user(session, row.user_id, target)
            except AppError:
                reason = "NOT_ELIGIBLE"
        if reason is None:
            try:
                await comps.check_target_period(session, [row.user_id], target)
            except AppError:
                reason = "PERIOD_LOCKED"
        if reason is None:
            try:
                comps.check_caps(counts[row.user_id])
            except AppError:
                reason = "TOO_MANY_ITEMS"
        if reason is not None:
            skipped.append({"id": str(row.id), "reason": reason})
            continue
        row.status, row.period, row.updated_by, row.version = "pending", target, actor.id, row.version + 1
        counts[row.user_id]["total"] += 1
        accepted += 1
        await pc.flush(session)
        comps.add_event(session, item_type="arrears", item=row, action="accept", actor_id=actor.id)
    audit(session, action="payroll.arrears.bulk_accept", actor=actor, entity_type=ENTITY, entity_id=None,
          after={"period": pc.period_str(target), "accepted": accepted, "skipped": len(skipped)})  # fmt: skip
    await pc.commit(session)
    return {"accepted": accepted, "skipped": skipped}


async def dismiss(session: AsyncSession, actor: Principal, arrears_id: uuid.UUID, reason: str) -> dict[str, Any]:
    peek = await _or_404(session, arrears_id)
    await pc.forbid_self(session, actor, peek.user_id, entity_type=ENTITY, entity_id=arrears_id)
    if peek.period is not None:
        await items_svc._lock_runs(session, [peek.period])
    row = await _or_404(session, arrears_id, lock=True)
    if row.status in PAID_STATES:
        raise AppError("ITEM_LOCKED", f"The arrears are {row.status} and can no longer be changed")
    if row.status not in OPEN:
        raise AppError("NOT_PROPOSED", "These arrears are not open")
    prev_period = row.period
    row.status, row.dismiss_reason, row.updated_by, row.version = "dismissed", reason, actor.id, row.version + 1
    await pc.flush(session)
    comps.add_event(session, item_type="arrears", item=row, action="dismiss", actor_id=actor.id,
                    prev_period=prev_period, detail={"reason": reason})  # fmt: skip
    audit(session, action="payroll.arrears.dismiss", actor=actor, entity_type=ENTITY, entity_id=row.id,
          after={"user_id": str(row.user_id)})  # fmt: skip
    await pc.commit(session)
    return await _one(session, arrears_id)


# ============================================================================================ component engine provider
def _label(r: PayrollArrears) -> str:
    if r.code == "OTHER_DEDUCTION":
        return "Salary recovery (arrears)"
    a, b = r.from_period, r.to_period
    fa = f"{MONTHS[a.month - 1]} {a.year}"
    return f"Arrears {fa}" if a == b else f"Arrears {fa} - {MONTHS[b.month - 1]} {b.year}"


def _lines(rows: list[PayrollArrears]) -> list[dict]:
    return [
        {
            "code": r.code,
            "label": _label(r),
            "amount": calc.money_str(_d(r.amount)),
            "sources": [{"type": "arrears", "id": str(r.id)}],
        }
        for r in rows
    ]


async def _warn(session: AsyncSession, user_ids: list[uuid.UUID], period: dt.date) -> dict[uuid.UUID, list[str]]:
    """ARREARS_PROPOSED: the employee has an open proposal that HR has not accepted or dismissed."""
    q = select(PayrollArrears.user_id).where(PayrollArrears.user_id.in_(user_ids), PayrollArrears.status == "proposed")
    return {uid: ["ARREARS_PROPOSED"] for uid in (await session.execute(q)).scalars()}


comps.register(comps.Provider("arrears", PayrollArrears, _lines, comps.LIVE, warn=_warn, order=30))
