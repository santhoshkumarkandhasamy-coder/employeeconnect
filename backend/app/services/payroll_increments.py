"""Salary increments with maker-checker, bulk hikes and salary history (plan 16 work package 4a, contract 18.4 / 18.5).

draft -> submitted -> approved | rejected (+ cancelled). The checker approves the STORED proposed structure, never a
recomputation (F3). Approval creates, replaces or removes a `salary_structures` row in the same transaction and runs the
auto-arrears hook of plan 15 for backdated effective dates.

Maker-checker is enforced three times: here (403 FORBIDDEN for the employee, 403 SECOND_PERSON_REQUIRED for the creator or the
submitter), by the CHECK constraints of migration 0024, and by the audit rows (`maker_id`, `checker_id`).

Lock order (F15, increments are outside run processing): salary_increment_batches -> salary_increments -> users ->
salary_structures -> payroll_arrears. Money, percents, reasons and comments are encrypted columns; audit rows and push payloads
carry ids, kinds, months and counts only.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import as_utc, utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    EmployeePayrollDetails,
    PayrollRun,
    Payslip,
    Permission,
    Role,
    SalaryIncrement,
    SalaryIncrementBatch,
    SalaryStructure,
    User,
)
from ..schemas.payroll import (
    BatchApprove,
    BatchReject,
    BulkIncrementCreate,
    IncrementApprove,
    IncrementCreate,
    IncrementReject,
    IncrementUpdate,
    VersionIn,
)
from . import admin_security, notifications
from . import attendance as att
from . import payroll_arrears as arrears_svc
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_increment_calc as ic
from . import payroll_items as items_svc
from . import payroll_settings as pset
from . import payroll_structures as structs_svc

log = logging.getLogger("app.payroll")

ENTITY = "salary_increment"
BATCH_ENTITY = "salary_increment_batch"
LIVE = ("draft", "submitted")
FINAL = ("approved", "rejected", "cancelled")
MAX_BATCH_ROWS = 500
PUSH_COALESCE_S = 600
MAX_PUSH_TARGETS = 20

SKIP_REASONS = {  # AppError code -> `skipped[].reason` of the bulk hike
    "STRUCTURE_MISSING": "NO_STRUCTURE",
    "STRUCTURE_EXISTS": "STRUCTURE_EXISTS",
    "LATER_STRUCTURE_EXISTS": "LATER_STRUCTURE_EXISTS",
    "INCREMENT_EXISTS": "INCREMENT_EXISTS",
    "AFTER_EXIT": "AFTER_EXIT",
}


def _bad(field_: str, message: str, code: str = "VALIDATION_ERROR") -> AppError:
    return AppError(code, message, details=[{"field": field_, "message": message}])


# ============================================================================================ configuration
@dataclass(frozen=True)
class Cfg:
    today: dt.date
    tz: Any
    max_pct: Decimal
    backdate_months: int
    esi_threshold: Decimal
    pf_ceiling: Decimal
    basic_pct: int
    hra_pct: int
    split_code: str
    split_name: str


async def load_cfg(session: AsyncSession) -> Cfg:
    s = (await pset.read_all(session))["settings"]
    tz = att.office_tz(await att.load_settings(session))
    return Cfg(
        today=dt.datetime.now(tz).date(), tz=tz,
        max_pct=Decimal(str(s["payroll.increment_max_pct"])),
        backdate_months=int(s["payroll.increment_backdate_months"]),
        esi_threshold=Decimal(str(s["payroll.esi_wage_threshold"])),
        pf_ceiling=Decimal(str(s["payroll.pf_wage_ceiling"])),
        basic_pct=int(s["payroll.split_basic_pct"]), hra_pct=int(s["payroll.split_hra_pct_of_basic"]),
        split_code=str(s["payroll.split_allowance_code"]), split_name=str(s["payroll.split_allowance_name"]),
    )  # fmt: skip


def check_effective(eff: dt.date, cfg: Cfg, *, lower: bool = True) -> None:
    """1st of a month, at most 12 months ahead, at most `increment_backdate_months` back (422 INVALID_PERIOD)."""
    if eff.day != 1 or not 2000 <= eff.year <= 2200:
        raise _bad("effective_from", "must be the 1st of a month (years 2000-2200)", "INVALID_PERIOD")
    first = cfg.today.replace(day=1)
    if eff > comps.month_add(first, 12):
        raise _bad("effective_from", "at most 12 months ahead", "INVALID_PERIOD")
    if lower and eff < comps.month_add(first, -cfg.backdate_months):
        raise _bad("effective_from", f"at most {cfg.backdate_months} months back", "INVALID_PERIOD")


def check_percent(pct: Decimal, cfg: Cfg, field_: str = "percent") -> Decimal:
    if pct <= 0 or pct > cfg.max_pct:
        raise _bad(field_, f"must be between 0.01 and {cfg.max_pct}", "INVALID_PERCENT")
    return pct


def last_employed_day(user: User, tz: Any) -> dt.date | None:
    gone = user.deactivated_at or user.deleted_at
    return as_utc(gone).astimezone(tz).date() - dt.timedelta(days=1) if gone is not None else None


# ============================================================================================ planning (resolution of one proposal)
@dataclass
class Plan:
    eff: dt.date
    kind: str
    base: SalaryStructure
    replaces: SalaryStructure | None
    proposed: dict[str, Any] | None
    old_gross: Decimal
    new_gross: Decimal | None
    percent: Decimal | None
    method: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class UserCtx:
    user: User
    structures: list[SalaryStructure]  # oldest first
    latest_published: dt.date | None
    live: dict[dt.date, uuid.UUID]  # effective_from -> id of a draft / submitted increment
    in_use: set[uuid.UUID] = field(default_factory=set)


def stored_form(s: SalaryStructure) -> dict[str, Any]:
    return ic.normalise(
        {
            **s.components,
            "pf_applicable": s.pf_applicable,
            "pf_on_full_basic": s.pf_on_full_basic,
            "esi_applicable": s.esi_applicable,
            "pt_applicable": s.pt_applicable,
        }  # fmt: skip
    )


def body_structure(body: Any) -> dict[str, Any]:
    """StructureBody -> the stored form."""
    return ic.normalise(structs_svc._components(body) | {f: getattr(body, f) for f in ic.FLAGS})


def _in_force(structures: list[SalaryStructure], eff: dt.date) -> SalaryStructure | None:
    return calc.structure_for([(s.effective_from, s) for s in structures], eff)


def build_plan(
    *,
    kind: str,
    eff: dt.date | None,
    percent: Decimal | None = None,
    monthly_gross: Decimal | None = None,
    method: str | None = None,
    structure: dict[str, Any] | None = None,
    replaces_id: uuid.UUID | None = None,
    cfg: Cfg,
    ctx: UserCtx,
    exclude_id: uuid.UUID | None = None,
) -> Plan:
    """Validate one proposal against the employee's current state and compute the proposed structure. Raises AppError."""
    structures = ctx.structures
    target: SalaryStructure | None = None
    if replaces_id is not None or kind == "remove":
        target = next((s for s in structures if s.id == replaces_id), None)
        if target is None:
            raise AppError("NOT_FOUND", "Structure not found")
    if kind == "remove":
        assert target is not None
        eff = target.effective_from
    if eff is None:
        raise _bad("effective_from", "is required")
    if kind == "percent":
        check_percent(percent if percent is not None else Decimal(0), cfg)
    if kind == "amount" and (monthly_gross is None or monthly_gross <= 0):
        raise _bad("monthly_gross", "must be greater than 0")
    if kind != "remove":
        check_effective(eff, cfg, lower=target is None)  # a correction of an existing structure may reach further back
    replacing = target is not None
    if replacing:
        assert target is not None
        if target.effective_from != eff:
            raise _bad("replaces_structure_id", "must be the structure effective on that date")
        if target.id in ctx.in_use:
            raise AppError("STRUCTURE_IN_USE", "This structure is used by a processed or published payslip")
    elif any(s.effective_from == eff for s in structures):
        raise AppError("STRUCTURE_EXISTS", "A structure with this effective date already exists")
    base = target if replacing else _in_force(structures, eff)
    if base is None:
        raise AppError("STRUCTURE_MISSING", "The employee has no salary structure in force on that date")
    if not replacing and any(s.effective_from > eff for s in structures):
        raise AppError("LATER_STRUCTURE_EXISTS", "A later structure exists; it would override this change")
    other = ctx.live.get(eff)
    if other is not None and other != exclude_id:
        raise AppError("INCREMENT_EXISTS", "A draft or submitted increment already exists for that employee and month")
    if kind != "remove":
        last = last_employed_day(ctx.user, cfg.tz)
        if last is not None and eff > last.replace(day=1):
            raise AppError("AFTER_EXIT", "The employee has left before that month")

    old = stored_form(base)
    old_gross = ic.gross_of(old)
    proposed: dict[str, Any] | None
    pct: Decimal | None = None
    try:
        if kind == "remove":
            proposed = None
        elif kind in ("percent", "amount"):
            if kind == "percent":
                assert percent is not None
                pct = percent
                target_gross = ic.percent_target(old_gross, percent)
            else:
                assert monthly_gross is not None
                target_gross = calc.q2(monthly_gross)
            if method == "split_from_gross":
                proposed = ic.split_from_gross(
                    old, target_gross, basic_pct=cfg.basic_pct, hra_pct_of_basic=cfg.hra_pct,
                    code=cfg.split_code, name=cfg.split_name,
                )  # fmt: skip
            else:
                proposed = ic.scale_proportional(old, target_gross, cfg.split_code)
        else:
            assert structure is not None
            proposed = ic.normalise(structure)
    except ic.IncrementCalcError as exc:
        raise _bad("effective_from" if kind == "remove" else "monthly_gross", str(exc)) from None
    new_gross = ic.gross_of(proposed) if proposed is not None else None
    backdated = ctx.latest_published is not None and eff <= ctx.latest_published
    warns = ic.warnings_for(
        old, proposed, kind=kind, esi_threshold=cfg.esi_threshold, pf_ceiling=cfg.pf_ceiling, max_pct=cfg.max_pct,
        backdated=backdated,
    )  # fmt: skip
    return Plan(
        eff, kind, base, target, proposed, old_gross, new_gross, pct, method if kind in ("percent", "amount") else None, warns
    )


async def _contexts(
    session: AsyncSession, users: list[User], *, lock: bool = False, with_in_use: bool = False
) -> dict[uuid.UUID, UserCtx]:
    ids = [u.id for u in users]
    out = {u.id: UserCtx(u, [], None, {}) for u in users}
    if not ids:
        return out
    q = select(SalaryStructure).where(SalaryStructure.user_id.in_(ids))
    if lock:
        q = q.order_by(SalaryStructure.id).with_for_update().execution_options(populate_existing=True)
    for s in (await session.execute(q)).scalars():
        out[s.user_id].structures.append(s)
    for c in out.values():
        c.structures.sort(key=lambda s: s.effective_from)
    q2 = (
        select(Payslip.user_id, func.max(PayrollRun.period))
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(Payslip.user_id.in_(ids), Payslip.status == "published")
        .group_by(Payslip.user_id)
    )
    for uid, period in (await session.execute(q2)).all():
        out[uid].latest_published = period
    q3 = select(SalaryIncrement.user_id, SalaryIncrement.effective_from, SalaryIncrement.id).where(
        SalaryIncrement.user_id.in_(ids), SalaryIncrement.status.in_(LIVE)
    )
    for uid, eff, iid in (await session.execute(q3)).all():
        out[uid].live[eff] = iid
    if with_in_use:
        used = await structs_svc._in_use_ids(session, [s.id for c in out.values() for s in c.structures])
        for c in out.values():
            c.in_use = {s.id for s in c.structures if s.id in used}
    return out


# ============================================================================================ output
async def _people(session: AsyncSession, rows: list[SalaryIncrement]) -> dict[uuid.UUID, User]:
    return await items_svc._people(session, {x for r in rows for x in (r.user_id, r.created_by, r.submitted_by, r.decided_by)})


def _structure_gross(s: SalaryStructure) -> str:
    return calc.money_str(structs_svc.data_of(s).monthly_gross)


async def _outs(
    session: AsyncSession, rows: list[SalaryIncrement], *, detail: bool, arrears: dict[uuid.UUID, bool] | None = None
) -> list[dict[str, Any]]:
    people = await _people(session, rows)
    sids = {x for r in rows for x in (r.base_structure_id,) if x}
    bases: dict[uuid.UUID, SalaryStructure] = {}
    if sids:
        bases = {s.id: s for s in (await session.execute(select(SalaryStructure).where(SalaryStructure.id.in_(sids)))).scalars()}
    periods = {r.effective_from for r in rows}
    runs: dict[dt.date, str] = {}
    if periods:
        runs = {
            p: st for p, st in (await session.execute(select(PayrollRun.period, PayrollRun.status).where(PayrollRun.period.in_(periods)))).all()
        }  # fmt: skip
    out = []
    for r in rows:
        base = bases.get(r.base_structure_id) if r.base_structure_id else None
        old_g = Decimal(r.old_gross) if r.old_gross is not None else None
        new_g = Decimal(r.new_gross) if r.new_gross is not None else None
        o: dict[str, Any] = {
            "id": str(r.id),
            "user": pc.user_out(people.get(r.user_id)),
            "batch_id": str(r.batch_id) if r.batch_id else None,
            "kind": r.kind,
            "effective_from": r.effective_from.isoformat(),
            "percent": r.percent,
            "method": r.method,
            "base_structure": (
                {"id": str(base.id), "effective_from": base.effective_from.isoformat(), "monthly_gross": _structure_gross(base)}
                if base
                else None
            ),
            "replaces_structure_id": str(r.replaces_structure_id) if r.replaces_structure_id else None,
            "change": ic.change(old_g, new_g),
            "warnings": list(r.warnings or []),
            "reason": r.reason,
            "status": r.status,
            "run_status": runs.get(r.effective_from, "none"),
            "structure_id": str(r.structure_id) if r.structure_id else None,
            "arrears_proposed": (arrears or {}).get(r.id),
            "created_by": pc.user_out(people.get(r.created_by)),
            "created_at": pc.iso(r.created_at),
            "submitted_by": pc.user_out(people.get(r.submitted_by)),
            "submitted_at": pc.iso(r.submitted_at),
            "decided_by": pc.user_out(people.get(r.decided_by)),
            "decided_at": pc.iso(r.decided_at),
            "version": r.version,
        }
        if detail:
            o["proposed"] = {**r.proposed, "monthly_gross": calc.money_str(ic.gross_of(r.proposed))} if r.proposed else None
            o["diff"] = ic.diff(stored_form(base), r.proposed) if (base and r.proposed) else []
            o["decision_comment"] = r.decision_comment
        out.append(o)
    return out


async def _row_or_404(session: AsyncSession, inc_id: uuid.UUID, *, lock: bool = False) -> SalaryIncrement:
    q = select(SalaryIncrement).where(SalaryIncrement.id == inc_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = (await session.execute(q)).scalar_one_or_none()
    if row is None:
        raise AppError("NOT_FOUND", "Increment not found")
    return row


async def _one(session: AsyncSession, inc_id: uuid.UUID, *, arrears: bool | None = None) -> dict[str, Any]:
    row = await _row_or_404(session, inc_id)
    await session.refresh(row)
    return (await _outs(session, [row], detail=True, arrears={row.id: arrears} if arrears is not None else None))[0]


def _event(session: AsyncSession, inc: SalaryIncrement, action: str, actor_id: uuid.UUID | None, **detail: Any) -> None:
    comps.add_event(
        session, item_type="increment", item=inc, action=action, actor_id=actor_id, period=inc.effective_from,
        amount=inc.new_gross, prev_amount=inc.old_gross, detail=detail or None,
    )  # fmt: skip


def _audit(session: AsyncSession, actor: Principal, action: str, inc: SalaryIncrement) -> None:
    """Never percents, amounts or reasons (contract 18.4)."""
    checker = inc.decided_by if inc.status in ("approved", "rejected") else None
    audit(
        session, action=action, actor=actor, entity_type=ENTITY, entity_id=inc.id,
        after={
            "user_id": str(inc.user_id), "kind": inc.kind, "effective_from": inc.effective_from.isoformat(),
            "status": inc.status, "maker_id": str(inc.created_by), "checker_id": str(checker) if checker else None,
            "structure_id": str(inc.structure_id) if inc.structure_id else None,
        },
    )  # fmt: skip


def _apply_plan(inc: SalaryIncrement, plan: Plan, reason: str | None = None) -> None:
    inc.effective_from = plan.eff
    inc.method = plan.method
    inc.percent = calc.money_str(plan.percent) if plan.percent is not None else None
    inc.base_structure_id = plan.base.id
    inc.base_updated_at = plan.base.updated_at
    inc.replaces_structure_id = plan.replaces.id if plan.replaces is not None else None
    inc.proposed = plan.proposed
    inc.old_gross = calc.money_str(plan.old_gross)
    inc.new_gross = calc.money_str(plan.new_gross) if plan.new_gross is not None else None
    inc.warnings = list(plan.warnings)
    if reason is not None:
        inc.reason = reason


def _new_row(actor: Principal, user_id: uuid.UUID, plan: Plan, reason: str, batch_id: uuid.UUID | None = None) -> SalaryIncrement:
    inc = SalaryIncrement(
        user_id=user_id, batch_id=batch_id, kind=plan.kind, status="draft", version=1, reason=reason,
        created_by=actor.id, updated_by=actor.id, effective_from=plan.eff,
    )  # fmt: skip
    _apply_plan(inc, plan)
    return inc


# ============================================================================================ push (best effort, after commit)
async def _push_approvers(
    session: AsyncSession, redis: Redis, maker_id: uuid.UUID, maker_name: str, *, kind: str, entity_id: uuid.UUID, text: str
) -> None:
    try:
        q = (
            select(User.id)
            .join(Role, Role.id == User.role_id)
            .where(
                User.status == "active", User.deleted_at.is_(None), User.id != maker_id,
                Role.permissions.any(Permission.code == "payroll.approve"),
            )
            .order_by(User.id)
            .limit(MAX_PUSH_TARGETS)
        )  # fmt: skip
        ids = list((await session.execute(q)).unique().scalars())
        if not ids:
            return
        if not await redis.set(f"ctr:payroll_approval:{kind}", "1", nx=True, ex=PUSH_COALESCE_S):
            return  # coalesced: a push for this kind went out in the last 10 minutes
        await notifications.notify(
            session, ids, type="payroll_approval", entity_id=entity_id, title="Approval needed",
            body=f"{maker_name} submitted {text}", extra={"kind": kind},
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.exception("payroll_approval push failed (ignored)")


async def _push_maker(
    session: AsyncSession, maker_id: uuid.UUID, *, kind: str, entity_id: uuid.UUID, title: str, body: str
) -> None:
    try:
        await notifications.notify(
            session, [maker_id], type="payroll_approval", entity_id=entity_id, title=title, body=body, extra={"kind": kind}
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.exception("payroll_approval push failed (ignored)")


# ============================================================================================ reads
async def list_increments(
    session: AsyncSession,
    actor: Principal,
    *,
    status: str | None,
    user_id: uuid.UUID | None,
    batch_id: uuid.UUID | None,
    effective_from: str | None,
    page: PageParams,
) -> dict[str, Any]:
    cond = []
    if status:
        cond.append(SalaryIncrement.status == status)
    if user_id:
        cond.append(SalaryIncrement.user_id == user_id)
    if batch_id:
        cond.append(SalaryIncrement.batch_id == batch_id)
    if effective_from:
        cond.append(SalaryIncrement.effective_from == comps.parse_target_period(effective_from, dt.date(2200, 1, 1)))
    total = (await session.execute(select(func.count()).select_from(SalaryIncrement).where(*cond))).scalar_one()
    q = (
        select(SalaryIncrement).where(*cond).order_by(SalaryIncrement.created_at.desc(), SalaryIncrement.id)
        .offset(page.offset).limit(page.page_size)
    )  # fmt: skip
    rows = list((await session.execute(q)).scalars())
    out = await _outs(session, rows, detail=False)
    await pc.audit_read(session, actor, "payroll.increment.list", ENTITY, count=len(out))
    return make_page(out, page, total)


async def get_increment(session: AsyncSession, actor: Principal, inc_id: uuid.UUID) -> dict[str, Any]:
    out = await _one(session, inc_id)
    audit(session, action="payroll.increment.view", actor=actor, entity_type=ENTITY, entity_id=inc_id)
    await pc.commit(session)
    return out


async def salary_history(session: AsyncSession, actor: Principal, user_id: uuid.UUID) -> dict[str, Any]:
    await structs_svc.get_user_or_404(session, user_id)
    structures = await structs_svc._all_structures(session, user_id)  # newest first
    inc_by_id: dict[uuid.UUID, SalaryIncrement] = {}
    iids = [s.increment_id for s in structures if s.increment_id]
    if iids:
        inc_by_id = {
            i.id: i for i in (await session.execute(select(SalaryIncrement).where(SalaryIncrement.id.in_(iids)))).scalars()
        }
    people = await items_svc._people(session, {i.decided_by for i in inc_by_id.values()})
    items: list[tuple[dt.date, dict[str, Any]]] = []
    for idx, s in enumerate(structures):
        gross = structs_svc.data_of(s).monthly_gross
        prev = structs_svc.data_of(structures[idx + 1]).monthly_gross if idx + 1 < len(structures) else None
        nxt = structures[idx - 1].effective_from if idx > 0 else None
        inc = inc_by_id.get(s.increment_id) if s.increment_id else None
        pct = calc.q2((gross - prev) * 100 / prev) if prev else None
        items.append((s.effective_from, {
            "type": "structure", "structure_id": str(s.id), "effective_from": s.effective_from.isoformat(),
            "effective_to": (nxt - dt.timedelta(days=1)).isoformat() if nxt else None,
            "monthly_gross": calc.money_str(gross), "change_percent": calc.money_str(pct) if pct is not None else None,
            "source": s.source, "increment_id": str(s.increment_id) if s.increment_id else None,
            "approved_by": pc.user_out(people.get(inc.decided_by)) if inc and inc.decided_by else None,
        }))  # fmt: skip
    live = (
        await session.execute(select(SalaryIncrement).where(SalaryIncrement.user_id == user_id, SalaryIncrement.status.in_(LIVE)))
    ).scalars()
    for i in live:
        items.append((i.effective_from, {
            "type": "increment", "increment_id": str(i.id), "effective_from": i.effective_from.isoformat(),
            "status": i.status, "kind": i.kind, "new_gross": i.new_gross,
        }))  # fmt: skip
    items.sort(key=lambda t: t[0], reverse=True)
    audit(session, action="payroll.structure.view", actor=actor, entity_type="user", entity_id=user_id)
    await pc.commit(session)
    return {"items": [i for _, i in items]}


# ============================================================================================ draft: create / update
def _body_args(body: IncrementCreate) -> dict[str, Any]:
    return {
        "kind": body.kind, "eff": body.effective_from, "percent": body.percent, "monthly_gross": body.monthly_gross,
        "method": body.method, "replaces_id": body.replaces_structure_id,
        "structure": body_structure(body.structure) if body.structure is not None else None,
    }  # fmt: skip


async def create_increment(session: AsyncSession, actor: Principal, user_id: uuid.UUID, body: IncrementCreate) -> dict[str, Any]:
    user = await structs_svc.get_user_or_404(session, user_id)
    await pc.forbid_self(session, actor, user_id, entity_type=ENTITY, entity_id=None)
    cfg = await load_cfg(session)
    await items_svc._lock_users(session, [user_id])
    ctx = (await _contexts(session, [user], with_in_use=body.replaces_structure_id is not None or body.kind == "remove"))[user_id]
    plan = build_plan(cfg=cfg, ctx=ctx, **_body_args(body))
    inc = _new_row(actor, user_id, plan, body.reason)
    session.add(inc)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError("INCREMENT_EXISTS", "A draft or submitted increment already exists for that employee and month") from None
    _event(session, inc, "create", actor.id, reason=body.reason)
    _audit(session, actor, "payroll.increment.create", inc)
    iid = inc.id
    await pc.commit(session)
    return await _one(session, iid)


async def update_increment(session: AsyncSession, actor: Principal, inc_id: uuid.UUID, body: IncrementUpdate) -> dict[str, Any]:
    peek = await _row_or_404(session, inc_id)
    await pc.forbid_self(session, actor, peek.user_id, entity_type=ENTITY, entity_id=inc_id)
    inc = await _row_or_404(session, inc_id, lock=True)  # increments before users (F15)
    await items_svc._lock_users(session, [inc.user_id])
    if inc.status != "draft":
        raise AppError("NOT_DRAFT", "Only a draft increment can be edited")
    if inc.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The increment was changed meanwhile; reload it")
    if body.kind != inc.kind:
        raise _bad("kind", "the kind of an increment cannot change")
    if inc.batch_id is not None and body.effective_from != inc.effective_from:
        raise _bad("effective_from", "the month of a bulk row is set by its batch")
    user = await structs_svc.get_user_or_404(session, inc.user_id)
    cfg = await load_cfg(session)
    ctx = (await _contexts(session, [user], with_in_use=body.kind in ("structure", "remove")))[inc.user_id]
    plan = build_plan(cfg=cfg, ctx=ctx, exclude_id=inc.id, **_body_args(body))
    _apply_plan(inc, plan, body.reason)
    inc.version, inc.updated_by = inc.version + 1, actor.id
    await pc.flush(session)
    _event(session, inc, "update", actor.id, reason=body.reason)
    _audit(session, actor, "payroll.increment.update", inc)
    await pc.commit(session)
    return await _one(session, inc_id)


# ============================================================================================ maker actions
async def _draft_for_action(session: AsyncSession, actor: Principal, inc_id: uuid.UUID, version: int | None) -> SalaryIncrement:
    peek = await _row_or_404(session, inc_id)
    await pc.forbid_self(session, actor, peek.user_id, entity_type=ENTITY, entity_id=inc_id)
    inc = await _row_or_404(session, inc_id, lock=True)
    if version is not None and inc.version != version:
        raise AppError("VERSION_CONFLICT", "The increment was changed meanwhile; reload it")
    return inc


async def submit_increment(
    session: AsyncSession, redis: Redis, actor: Principal, inc_id: uuid.UUID, body: VersionIn
) -> dict[str, Any]:
    inc = await _draft_for_action(session, actor, inc_id, None)
    if inc.status != "draft":
        raise AppError("NOT_DRAFT", "Only a draft increment can be submitted")
    if inc.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The increment was changed meanwhile; reload it")
    if "NO_CHANGE" in (inc.warnings or []):
        raise AppError("NO_CHANGE", "The proposed structure is identical to the current one")
    name = actor.user.full_name
    inc.status, inc.submitted_by, inc.submitted_at = "submitted", actor.id, utcnow()
    inc.version, inc.updated_by = inc.version + 1, actor.id
    await pc.flush(session)
    _event(session, inc, "submit", actor.id)
    _audit(session, actor, "payroll.increment.submit", inc)
    await pc.commit(session)
    await _push_approvers(session, redis, actor.id, name, kind="increment", entity_id=inc_id, text="1 increment")
    return await _one(session, inc_id)


async def withdraw_increment(session: AsyncSession, actor: Principal, inc_id: uuid.UUID, body: VersionIn) -> dict[str, Any]:
    inc = await _draft_for_action(session, actor, inc_id, None)
    if inc.status != "submitted":
        raise AppError("NOT_SUBMITTED", "Only a submitted increment can be withdrawn")
    if inc.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The increment was changed meanwhile; reload it")
    inc.status, inc.submitted_by, inc.submitted_at = "draft", None, None
    inc.version, inc.updated_by = inc.version + 1, actor.id
    await pc.flush(session)
    _event(session, inc, "withdraw", actor.id)
    _audit(session, actor, "payroll.increment.withdraw", inc)
    await pc.commit(session)
    return await _one(session, inc_id)


async def cancel_increment(session: AsyncSession, actor: Principal, inc_id: uuid.UUID, reason: str) -> dict[str, Any]:
    inc = await _draft_for_action(session, actor, inc_id, None)
    if inc.status not in LIVE:
        raise AppError("NOT_DRAFT", "Only a draft or submitted increment can be cancelled")
    inc.status, inc.decision_comment = "cancelled", reason
    inc.version, inc.updated_by = inc.version + 1, actor.id
    await pc.flush(session)
    _event(session, inc, "cancel", actor.id, reason=reason)
    _audit(session, actor, "payroll.increment.cancel", inc)
    await pc.commit(session)
    return await _one(session, inc_id)


# ============================================================================================ checker actions
async def _gate(session: AsyncSession, actor: Principal, inc: SalaryIncrement) -> None:
    """The checker is not the employee (403 FORBIDDEN, audited) and neither the creator nor the submitter."""
    if inc.user_id == actor.id:
        raise await pc.deny(session, actor, entity_type=ENTITY, entity_id=inc.id, reason="self")
    if actor.id in (inc.created_by, inc.submitted_by):
        raise AppError("SECOND_PERSON_REQUIRED", "A different person must decide an increment you prepared or submitted")


def _gate_reason(actor_id: uuid.UUID, inc: SalaryIncrement, batch_maker: uuid.UUID | None = None) -> str | None:
    if inc.user_id == actor_id:
        return "FORBIDDEN"
    if actor_id in (inc.created_by, inc.submitted_by, batch_maker):
        return "SECOND_PERSON_REQUIRED"
    return None


def check_approvable(inc: SalaryIncrement, ctx: UserCtx, cfg: Cfg) -> None:
    """Everything that can stop an approval, in the contract order (base unchanged, existence, in use, exit). Raises AppError.
    The STORED proposal is what gets applied; nothing is recomputed."""
    structures = ctx.structures
    replacing = inc.kind == "remove" or inc.replaces_structure_id is not None
    if inc.base_structure_id is None or (replacing and inc.replaces_structure_id is None):
        raise AppError("INCREMENT_STALE", "The structure this increment was based on no longer exists")
    base = next((s for s in structures if s.id == inc.base_structure_id), None)
    if base is None or as_utc(base.updated_at) != as_utc(inc.base_updated_at):
        raise AppError("INCREMENT_STALE", "The structure this increment was based on has changed; create a new increment")
    if not replacing and any(base.effective_from < s.effective_from < inc.effective_from for s in structures):
        raise AppError("INCREMENT_STALE", "Another structure now sits between the base and the effective date")
    if not replacing:
        if any(s.effective_from == inc.effective_from for s in structures):
            raise AppError("STRUCTURE_EXISTS", "A structure with this effective date already exists")
        if any(s.effective_from > inc.effective_from for s in structures):
            raise AppError("LATER_STRUCTURE_EXISTS", "A later structure exists; it would override this change")
    else:
        target = next((s for s in structures if s.id == inc.replaces_structure_id), None)
        if target is None or target.effective_from != inc.effective_from:
            raise AppError("INCREMENT_STALE", "The structure to replace no longer exists")
        if target.id in ctx.in_use:
            raise AppError("STRUCTURE_IN_USE", "This structure is used by a processed or published payslip")
    if inc.kind != "remove":
        last = last_employed_day(ctx.user, cfg.tz)
        if last is not None and inc.effective_from > last.replace(day=1):
            raise AppError("AFTER_EXIT", "The employee has left before that month")


async def _delete_structure_row(session: AsyncSession, s: SalaryStructure) -> None:
    await session.execute(
        update(Payslip).where(Payslip.structure_id == s.id, Payslip.status == "draft").values(structure_id=None)
    )
    await session.delete(s)
    await session.flush()


async def apply_approval(
    session: AsyncSession, actor: Principal, inc: SalaryIncrement, ctx: UserCtx, cfg: Cfg, comment: str | None
) -> bool:
    """Mutations of an approval (after `check_approvable`): the structure row, the increment row, events, the arrears hook.
    Returns True when an arrears proposal exists afterwards. The caller commits."""
    structures = ctx.structures
    replacing = inc.replaces_structure_id is not None
    prop = inc.proposed
    old_base = next(s for s in structures if s.id == inc.base_structure_id)
    backdated = ctx.latest_published is not None and inc.effective_from <= ctx.latest_published
    inc.warnings = ic.warnings_for(  # re-checked at approval, against the state that is in force NOW
        stored_form(old_base), prop, kind=inc.kind, esi_threshold=cfg.esi_threshold, pf_ceiling=cfg.pf_ceiling,
        max_pct=cfg.max_pct, backdated=backdated,
    )  # fmt: skip
    now = utcnow()
    structure: SalaryStructure | None = None
    if inc.kind == "remove":
        victim = next(s for s in structures if s.id == inc.replaces_structure_id)
        inc.structure_id = None
    elif replacing:
        structure = next(s for s in structures if s.id == inc.replaces_structure_id)
        victim = None
        _fill_structure(structure, prop)
        structure.source, structure.increment_id = "increment", inc.id
        structure.import_batch_id = inc.batch_id if inc.kind == "import" else structure.import_batch_id
        structure.updated_by = actor.id
        inc.structure_id = structure.id
    else:
        victim = None
        structure = SalaryStructure(
            user_id=inc.user_id, effective_from=inc.effective_from, components={}, source="increment", increment_id=inc.id,
            import_batch_id=inc.batch_id if inc.kind == "import" else None, created_by=actor.id, updated_by=actor.id,
        )  # fmt: skip
        _fill_structure(structure, prop)
        session.add(structure)
        await pc.flush(session)
        inc.structure_id = structure.id
    inc.status, inc.decided_by, inc.decided_at, inc.decision_comment = "approved", actor.id, now, comment
    inc.version, inc.updated_by = inc.version + 1, actor.id
    await pc.flush(session)
    if victim is not None:
        await _delete_structure_row(session, victim)
        ctx.structures = [s for s in ctx.structures if s.id != victim.id]
    elif structure is not None and structure not in ctx.structures:
        ctx.structures.append(structure)
    _event(session, inc, "remove" if inc.kind == "remove" else "approve", actor.id, comment=comment)
    proposal = False
    if ctx.latest_published is not None and inc.effective_from <= ctx.latest_published:
        row = await arrears_svc.sync_proposal(session, inc.user_id, actor.id)
        audit(
            session, action="payroll.arrears.propose", actor=actor, entity_type=arrears_svc.ENTITY,
            entity_id=row.id if row else None,
            after={"user_id": str(inc.user_id), "proposal": row is not None, "trigger": "increment"},
        )  # fmt: skip
        proposal = row is not None
    return proposal


def _fill_structure(s: SalaryStructure, prop: dict[str, Any]) -> None:
    s.components = {k: prop[k] for k in ic.MONEY_KEYS}
    s.pf_applicable, s.pf_on_full_basic = prop["pf_applicable"], prop["pf_on_full_basic"]
    s.esi_applicable, s.pt_applicable = prop["esi_applicable"], prop["pt_applicable"]


async def approve_increment(
    session: AsyncSession, redis: Redis, actor: Principal, inc_id: uuid.UUID, body: IncrementApprove
) -> dict[str, Any]:
    peek = await _row_or_404(session, inc_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.increment.approve")
    cfg = await load_cfg(session)
    inc = await _row_or_404(session, inc_id, lock=True)
    await _gate(session, actor, inc)
    if inc.status != "submitted":
        raise AppError("NOT_SUBMITTED", "The increment is not waiting for a decision")
    if inc.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The increment was changed meanwhile; reload it")
    user = await structs_svc.get_user_or_404(session, peek.user_id)
    await items_svc._lock_users(session, [user.id])
    ctx = (await _contexts(session, [user], lock=True, with_in_use=True))[user.id]
    check_approvable(inc, ctx, cfg)
    maker, employee_name = inc.created_by, user.full_name
    proposal = await apply_approval(session, actor, inc, ctx, cfg, body.comment)
    _audit(session, actor, "payroll.increment.approve", inc)
    await pc.commit(session)
    await _push_maker(session, maker, kind="increment", entity_id=inc_id, title="Increment approved", body=employee_name)
    return await _one(session, inc_id, arrears=True if proposal else None)


async def reject_increment(session: AsyncSession, actor: Principal, inc_id: uuid.UUID, body: IncrementReject) -> dict[str, Any]:
    await _row_or_404(session, inc_id)
    inc = await _row_or_404(session, inc_id, lock=True)
    await _gate(session, actor, inc)
    if inc.status != "submitted":
        raise AppError("NOT_SUBMITTED", "The increment is not waiting for a decision")
    if inc.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The increment was changed meanwhile; reload it")
    maker = inc.created_by
    user = await session.get(User, inc.user_id)
    inc.status, inc.decided_by, inc.decided_at, inc.decision_comment = "rejected", actor.id, utcnow(), body.comment
    inc.version, inc.updated_by = inc.version + 1, actor.id
    await pc.flush(session)
    _event(session, inc, "reject", actor.id, comment=body.comment)
    _audit(session, actor, "payroll.increment.reject", inc)
    name = user.full_name if user else ""
    await pc.commit(session)
    await _push_maker(session, maker, kind="increment", entity_id=inc_id, title="Increment rejected", body=name)
    return await _one(session, inc_id)


# ============================================================================================ batches (bulk hike)
async def _batch_or_404(session: AsyncSession, batch_id: uuid.UUID, *, lock: bool = False) -> SalaryIncrementBatch:
    q = select(SalaryIncrementBatch).where(SalaryIncrementBatch.id == batch_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    b = (await session.execute(q)).scalar_one_or_none()
    if b is None:
        raise AppError("NOT_FOUND", "Batch not found")
    return b


async def _batch_outs(session: AsyncSession, batches: list[SalaryIncrementBatch]) -> list[dict[str, Any]]:
    if not batches:
        return []
    ids = [b.id for b in batches]
    rows = list((await session.execute(select(SalaryIncrement).where(SalaryIncrement.batch_id.in_(ids)))).scalars())
    by_batch: dict[uuid.UUID, list[SalaryIncrement]] = {}
    for r in rows:
        by_batch.setdefault(r.batch_id, []).append(r)
    people = await items_svc._people(session, {b.created_by for b in batches})
    out = []
    for b in batches:
        mine = by_batch.get(b.id, [])
        counts = {s: 0 for s in ("draft", "submitted", "approved", "rejected", "cancelled")}
        old_t = new_t = Decimal(0)
        for r in mine:
            counts[r.status] += 1
            if r.status in ("draft", "submitted", "approved") and r.old_gross is not None and r.new_gross is not None:
                old_t += Decimal(r.old_gross)
                new_t += Decimal(r.new_gross)
        out.append(
            {
                "id": str(b.id), "kind": b.kind,
                "effective_from": b.effective_from.isoformat() if b.effective_from else None,
                "percent": b.percent, "method": b.method, "rows": b.rows, "reason": b.reason, "counts": counts,
                "totals": {
                    "old_gross": calc.money_str(old_t), "new_gross": calc.money_str(new_t),
                    "delta_monthly": calc.money_str(new_t - old_t), "delta_annual": calc.money_str((new_t - old_t) * 12),
                },
                "file_sha256": b.file_sha256, "created_by": pc.user_out(people.get(b.created_by)),
                "created_at": pc.iso(b.created_at), "cancelled_at": pc.iso(b.cancelled_at),
            }
        )  # fmt: skip
    return out


def _batch_audit(session: AsyncSession, actor: Principal, action: str, b: SalaryIncrementBatch, **extra: Any) -> None:
    audit(
        session, action=action, actor=actor, entity_type=BATCH_ENTITY, entity_id=b.id,
        after={"kind": b.kind, "effective_from": b.effective_from.isoformat() if b.effective_from else None, "rows": b.rows, **extra},
    )  # fmt: skip


async def list_batches(session: AsyncSession, kind: str | None, page: PageParams) -> dict[str, Any]:
    cond = [SalaryIncrementBatch.kind == kind] if kind else []
    total = (await session.execute(select(func.count()).select_from(SalaryIncrementBatch).where(*cond))).scalar_one()
    q = (
        select(SalaryIncrementBatch).where(*cond)
        .order_by(SalaryIncrementBatch.created_at.desc(), SalaryIncrementBatch.id)
        .offset(page.offset).limit(page.page_size)
    )  # fmt: skip
    rows = list((await session.execute(q)).scalars())
    return make_page(await _batch_outs(session, rows), page, total)


async def get_batch(session: AsyncSession, batch_id: uuid.UUID) -> dict[str, Any]:
    return (await _batch_outs(session, [await _batch_or_404(session, batch_id)]))[0]


async def create_batch(session: AsyncSession, actor: Principal, body: BulkIncrementCreate) -> dict[str, Any]:
    cfg = await load_cfg(session)
    check_effective(body.effective_from, cfg)
    check_percent(body.percent, cfg)
    overrides: dict[uuid.UUID, Decimal] = {}
    for i, o in enumerate(body.overrides):
        overrides[o.user_id] = check_percent(o.percent, cfg, f"overrides[{i}].percent")
    scope = body.scope
    explicit = scope.user_ids is not None
    q = select(User).where(User.deleted_at.is_(None))
    if scope.department_ids is not None:
        q = q.where(User.department_id.in_(scope.department_ids))
    if explicit:
        q = q.where(User.id.in_(scope.user_ids))
    users = list((await session.execute(q.order_by(User.employee_id))).unique().scalars())
    if explicit:
        missing = set(scope.user_ids) - {u.id for u in users}
        if missing and scope.department_ids is None:
            raise AppError(
                "NOT_FOUND", "Employee not found", details=[{"field": "scope.user_ids", "message": "unknown employee"}]
            )
    excluded = set(scope.exclude_user_ids)
    users = [u for u in users if u.id not in excluded]
    details = await _details_of(session, [u.id for u in users])
    ctxs = await _contexts(session, users)
    skipped: list[tuple[User, str]] = []
    candidates: list[User] = []
    for u in users:
        det = details.get(u.id)
        eligible = u.status == "active" and (det is None or det.include_in_payroll)
        if not explicit and not (eligible and ctxs[u.id].structures):
            continue  # "all active payroll employees with a structure": silently not part of the scope
        candidates.append(u)
    if len(candidates) > MAX_BATCH_ROWS:
        raise _bad("scope", f"at most {MAX_BATCH_ROWS} employees per batch", "TOO_MANY_ROWS")
    stray = set(overrides) - {u.id for u in candidates}
    if stray:
        raise _bad("overrides", "every override must name an employee of the scope")
    await items_svc._lock_users(session, [u.id for u in candidates])  # F15: users before structures
    ctxs = await _contexts(session, candidates)  # re-read under the lock
    plans: list[tuple[User, Plan]] = []
    for u in candidates:
        det = details.get(u.id)
        if u.id == actor.id:
            skipped.append((u, "SELF"))
            continue
        if u.status != "active" or (det is not None and not det.include_in_payroll):
            skipped.append((u, "NOT_ELIGIBLE"))
            continue
        try:
            plan = build_plan(
                kind="percent", eff=body.effective_from, percent=overrides.get(u.id, body.percent), method=body.method,
                cfg=cfg, ctx=ctxs[u.id],
            )  # fmt: skip
        except AppError as exc:
            reason = SKIP_REASONS.get(exc.code)
            if reason is None:
                raise
            skipped.append((u, reason))
            continue
        plans.append((u, plan))
    if not plans:
        raise AppError(
            "VALIDATION_ERROR", "No employee qualifies for the increment",
            details=[{"field": "scope", "message": f"{len(skipped)} employee(s) skipped"}],
        )  # fmt: skip
    batch = SalaryIncrementBatch(
        kind="percent", effective_from=body.effective_from, percent=calc.money_str(body.percent), method=body.method,
        rows=len(plans), reason=body.reason, created_by=actor.id,
    )  # fmt: skip
    session.add(batch)
    await pc.flush(session)
    for u, plan in plans:
        inc = _new_row(actor, u.id, plan, body.reason, batch.id)
        session.add(inc)
        await _flush_unique(session)
        _event(session, inc, "create", actor.id, reason=body.reason, batch_id=str(batch.id))
    _batch_audit(session, actor, "payroll.increment_batch.create", batch, created=len(plans), skipped=len(skipped))
    bid = batch.id
    await pc.commit(session)
    return {
        "batch": await get_batch(session, bid),
        "created": len(plans),
        "skipped": [{"user": pc.user_out(u), "reason": r} for u, r in skipped],
    }


async def _flush_unique(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError("INCREMENT_EXISTS", "An increment was created concurrently; retry") from None


async def _details_of(session: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, EmployeePayrollDetails]:
    if not ids:
        return {}
    q = select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id.in_(ids))
    return {d.user_id: d for d in (await session.execute(q)).scalars()}


async def _lock_batch_rows(session: AsyncSession, batch_id: uuid.UUID) -> list[SalaryIncrement]:
    q = (
        select(SalaryIncrement).where(SalaryIncrement.batch_id == batch_id).order_by(SalaryIncrement.id)
        .with_for_update().execution_options(populate_existing=True)
    )  # fmt: skip
    return list((await session.execute(q)).scalars())


async def submit_batch(session: AsyncSession, redis: Redis, actor: Principal, batch_id: uuid.UUID) -> dict[str, Any]:
    await _batch_or_404(session, batch_id)
    batch = await _batch_or_404(session, batch_id, lock=True)
    if batch.cancelled_at is not None:
        raise AppError("NOT_DRAFT", "The batch was cancelled")
    rows = await _lock_batch_rows(session, batch_id)
    done: list[SalaryIncrement] = []
    skipped = []
    name = actor.user.full_name
    for r in rows:
        if r.status != "draft":
            if r.status != "cancelled":
                skipped.append({"id": str(r.id), "reason": "NOT_DRAFT"})
            continue
        if r.user_id == actor.id:
            skipped.append({"id": str(r.id), "reason": "FORBIDDEN"})
            continue
        if "NO_CHANGE" in (r.warnings or []):
            skipped.append({"id": str(r.id), "reason": "NO_CHANGE"})
            continue
        r.status, r.submitted_by, r.submitted_at = "submitted", actor.id, utcnow()
        r.version, r.updated_by = r.version + 1, actor.id
        done.append(r)
    await pc.flush(session)
    submitted = len(done)
    for r in done:
        _event(session, r, "submit", actor.id, batch_id=str(batch_id))
    _batch_audit(session, actor, "payroll.increment_batch.submit", batch, submitted=submitted, skipped=len(skipped))
    await pc.commit(session)
    if submitted:
        await _push_approvers(
            session, redis, actor.id, name, kind="increment_batch", entity_id=batch_id, text=f"{submitted} increment(s)"
        )
    return {"submitted": submitted, "skipped": skipped}


async def approve_batch(
    session: AsyncSession, redis: Redis, actor: Principal, batch_id: uuid.UUID, body: BatchApprove
) -> dict[str, Any]:
    await _batch_or_404(session, batch_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.increment_batch.approve")
    cfg = await load_cfg(session)
    batch = await _batch_or_404(session, batch_id, lock=True)
    maker = batch.created_by
    rows = [r for r in await _lock_batch_rows(session, batch_id) if r.status == "submitted"]
    users = {u.id: u for u in await _users_of(session, [r.user_id for r in rows])}
    await items_svc._lock_users(session, list(users))
    ctxs = await _contexts(session, list(users.values()), lock=True, with_in_use=True)
    approved, proposed, failed = 0, 0, []
    for r in rows:
        reason = _gate_reason(actor.id, r, maker)
        if reason is None:
            try:
                check_approvable(r, ctxs[r.user_id], cfg)
            except AppError as exc:
                reason = exc.code
        if reason is not None:
            failed.append({"id": str(r.id), "reason": reason})
            continue
        if await apply_approval(session, actor, r, ctxs[r.user_id], cfg, body.comment):
            proposed += 1
        _audit(session, actor, "payroll.increment.approve", r)
        approved += 1
    _batch_audit(
        session, actor, "payroll.increment_batch.approve", batch, approved=approved, failed=len(failed),
        arrears_proposed=proposed, maker_id=str(maker), checker_id=str(actor.id),
    )  # fmt: skip
    await pc.commit(session)
    if approved:
        await _push_maker(
            session,
            maker,
            kind="increment_batch",
            entity_id=batch_id,
            title="Increments approved",
            body=f"{approved} increment(s)",
        )
    return {"approved": approved, "failed": failed, "arrears_proposed": proposed}


async def _users_of(session: AsyncSession, ids: list[uuid.UUID]) -> list[User]:
    if not ids:
        return []
    return list((await session.execute(select(User).where(User.id.in_(set(ids))))).unique().scalars())


async def reject_batch(session: AsyncSession, actor: Principal, batch_id: uuid.UUID, body: BatchReject) -> dict[str, Any]:
    await _batch_or_404(session, batch_id)
    batch = await _batch_or_404(session, batch_id, lock=True)
    rows = [r for r in await _lock_batch_rows(session, batch_id) if r.status == "submitted"]
    if actor.id == batch.created_by or any(actor.id in (r.created_by, r.submitted_by) for r in rows):
        raise AppError("SECOND_PERSON_REQUIRED", "A different person must decide a batch you prepared or submitted")
    done: list[SalaryIncrement] = []
    now = utcnow()
    for r in rows:
        if r.user_id == actor.id:
            continue  # never decide your own pay: the row stays submitted for another checker
        r.status, r.decided_by, r.decided_at, r.decision_comment = "rejected", actor.id, now, body.comment
        r.version, r.updated_by = r.version + 1, actor.id
        done.append(r)
    await pc.flush(session)
    n = len(done)
    for r in done:
        _event(session, r, "reject", actor.id, comment=body.comment, batch_id=str(batch_id))
    _batch_audit(
        session,
        actor,
        "payroll.increment_batch.reject",
        batch,
        rejected=n,
        maker_id=str(batch.created_by),
        checker_id=str(actor.id),
    )
    maker = batch.created_by
    await pc.commit(session)
    if n:
        await _push_maker(
            session, maker, kind="increment_batch", entity_id=batch_id, title="Increments rejected", body=f"{n} increment(s)"
        )
    return {"rejected": n}


async def cancel_batch(session: AsyncSession, actor: Principal, batch_id: uuid.UUID, reason: str) -> dict[str, Any]:
    await _batch_or_404(session, batch_id)
    batch = await _batch_or_404(session, batch_id, lock=True)
    rows = await _lock_batch_rows(session, batch_id)
    done: list[SalaryIncrement] = []
    kept = 0
    for r in rows:
        if r.status in LIVE and r.user_id != actor.id:
            r.status, r.decision_comment = "cancelled", reason
            r.version, r.updated_by = r.version + 1, actor.id
            done.append(r)
        elif r.status in ("approved", "rejected") or (r.status in LIVE and r.user_id == actor.id):
            kept += 1
    await pc.flush(session)
    n = len(done)
    for r in done:
        _event(session, r, "cancel", actor.id, reason=reason, batch_id=str(batch_id))
    if kept == 0 and batch.cancelled_at is None:
        batch.cancelled_at, batch.cancelled_by = utcnow(), actor.id
    _batch_audit(session, actor, "payroll.increment_batch.cancel", batch, cancelled=n, not_cancelled=kept)
    await pc.commit(session)
    return {"cancelled": n, "not_cancelled": kept}
