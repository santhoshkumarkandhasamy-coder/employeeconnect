"""Component engine (plan 15, contract 17.0 / 17.1): pay components reach a payslip as system lines inside the encrypted
`payslips.inputs_enc` JSON, key `components`, next to the manual `adjustments`.

Source rows (one-time items, loan instalments, arrears, encashments, reimbursement claims, overtime days) share one lifecycle:

    pending --process--> processing (payslip_id set) --publish--> paid          (reopen: processing -> pending)

`pending` is each provider's `payable` status: `approved` for reimbursement claims and overtime days (they have review steps
before it).

* pull   (P5): rows of the payslip's user with `period` = the run month and `status = <payable>` ('pending'), plus `paid` rows whose
         `payslip_id` is the payslip this one supersedes (carried into a revision) and `processing` rows already attached to it.
         Period-exact: a row of a month that is published without a payslip for the user stays pending (flag PERIOD_CLOSED).
* attach (P6): process moves the pulled rows to `processing`; publish moves them to `paid` and carries `paid` rows of the
         superseded copy over to the new payslip; reopen puts `processing` rows back to `pending`.
* every state change appends a `payroll_item_events` row (append-only money history, no amounts in audit).
* lock order (P7): runs -> payslips -> one-time items -> loans -> instalments -> arrears -> leave balances -> encashments ->
         claims -> overtime. The callers already hold the run and payslip locks; this module locks source rows per table in
         PROVIDERS order, `ORDER BY id`, one query per table, so two transactions cannot lock the same rows in opposite order.

Extension point: `register(Provider(...))` appends a source table; nothing else in the engine changes.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import AppError
from ..models import PayrollItemEvent, PayrollOneTimeItem, PayrollRun, Payslip
from . import payroll_calc as calc

MAX_ONE_TIME_PER_MONTH = 20
MAX_LINES_PER_MONTH = 40
MAX_MONTHS_AHEAD = 12
LIVE = ("pending", "processing", "paid")  # statuses that count towards the caps


@dataclass(frozen=True)
class Provider:
    """One source table. `type` is the `sources[].type` of the payslip line and the event `item_type`.
    The model needs `id, user_id, period, status, payslip_id, version, updated_by, created_at, amount`."""

    type: str
    model: Any
    build: Callable[[list[Any]], list[dict]]  # rows (stable order) -> component lines
    live: tuple[str, ...] = LIVE
    # optional hooks (all keep the global lock order of P7):
    #   prepare      async, before `build`: may load extra data and set plain attributes on the rows (e.g. a line label)
    #   lock_parents async, before this table's rows are locked in attach / publish: lock the parent rows first (loans)
    #   after_paid   async, after rows moved processing -> paid in a publish (e.g. close a fully recovered loan)
    #   warn         async, per user warning codes for the month (stored in inputs["component_warnings"])
    prepare: Callable[[AsyncSession, list[Any]], Awaitable[None]] | None = None
    lock_parents: Callable[[AsyncSession, list[uuid.UUID]], Awaitable[None]] | None = None
    after_paid: Callable[[AsyncSession, list[Any], uuid.UUID], Awaitable[None]] | None = None
    warn: Callable[[AsyncSession, list[uuid.UUID], dt.date], Awaitable[dict[uuid.UUID, list[str]]]] | None = None
    # position in the global lock order of P7 (lower locks first); PROVIDERS is sorted by it on every register, so the
    # import order of the provider modules can never change the lock order (L1)
    order: int = 0
    # the status that means "payable, waiting for a payslip" (P4): `pending` for most tables, `approved` for claims and overtime
    payable: str = "pending"
    # claims and overtime days are many rows per payslip line: they do not count towards the per-month line caps
    count_in_caps: bool = True


def _money(x: str | Decimal) -> str:
    return calc.money_str(Decimal(x))


def _one_time_lines(rows: list[PayrollOneTimeItem]) -> list[dict]:
    return [
        {
            "code": r.code,
            "label": r.label,
            "amount": _money(r.amount),
            "sources": [{"type": "one_time", "id": str(r.id)}],
        }
        for r in rows
    ]


PROVIDERS: list[Provider] = [Provider("one_time", PayrollOneTimeItem, _one_time_lines, order=10)]


def ensure_providers() -> None:
    """Import the provider modules (they call `register` on import). Done lazily because they import the payroll services
    that import this module; every entry point below calls it first, so no process can run the engine without them."""
    from . import payroll_arrears, payroll_encashment, payroll_loans, payroll_overtime, payroll_reimbursements  # noqa: F401


def register(provider: Provider) -> None:
    """Add a source table (idempotent). Keep PROVIDERS in the global lock order of plan 15 P7."""
    if all(p.type != provider.type for p in PROVIDERS):
        PROVIDERS.append(provider)
        PROVIDERS.sort(key=lambda p: p.order)  # stable: equal `order` keeps registration order


# ============================================================================================ events
def add_event(
    session: AsyncSession,
    *,
    item_type: str,
    item: Any,
    action: str,
    actor_id: uuid.UUID | None,
    prev_period: dt.date | None = None,
    prev_amount: str | None = None,
    detail: dict | None = None,
    amount: str | None = None,
    period: dt.date | None = None,
) -> None:
    """Append one money-history row for `item` in its CURRENT state (call after changing it). `detail` is encrypted; it may
    hold reasons / references but never goes to the audit log."""
    session.add(
        PayrollItemEvent(
            item_type=item_type,
            item_id=item.id,
            user_id=item.user_id,
            action=action,
            status=item.status,
            period=period if period is not None else getattr(item, "period", None),
            prev_period=prev_period,
            amount=amount if amount is not None else getattr(item, "event_amount", getattr(item, "amount", None)),
            prev_amount=prev_amount,
            detail=detail,
            payslip_id=getattr(item, "payslip_id", None),
            actor_id=actor_id,
        )  # fmt: skip
    )


# ============================================================================================ periods
def parse_target_period(raw: str, today: dt.date) -> dt.date:
    """`YYYY-MM` -> first of month. Past months are fine (no run yet); more than 12 months ahead is refused."""
    from ..schemas.payroll import PERIOD_RE

    m = PERIOD_RE.match(raw or "")
    if not m:
        raise AppError("INVALID_PERIOD", "period must be YYYY-MM", details=[{"field": "period", "message": "must be YYYY-MM"}])
    y, mo = int(m.group(1)), int(m.group(2))
    if not 2000 <= y <= 2200:
        raise AppError("INVALID_PERIOD", "period year out of range", details=[{"field": "period", "message": "year 2000-2200"}])
    return check_horizon(dt.date(y, mo, 1), today)


def month_add(d: dt.date, n: int) -> dt.date:
    idx = d.year * 12 + (d.month - 1) + n
    return dt.date(idx // 12, idx % 12 + 1, 1)


def check_horizon(period: dt.date, today: dt.date) -> dt.date:
    if period > month_add(today.replace(day=1), MAX_MONTHS_AHEAD):
        raise AppError(
            "INVALID_PERIOD", "The month is too far ahead",
            details=[{"field": "period", "message": f"at most {MAX_MONTHS_AHEAD} months ahead"}],
        )  # fmt: skip
    return period


async def check_target_period(session: AsyncSession, user_ids: list[uuid.UUID], period: dt.date, *, lock: bool = False) -> str:
    """P9. Returns the run status of `period` ('none' | 'draft' | 'processed' | 'published'). A published run only accepts a
    new row for employees that have an open DRAFT revision in it, else 409 PERIOD_LOCKED. `lock` takes the run row
    FOR UPDATE (first in the global order) so process / publish cannot slip in between the check and the insert."""
    q = select(PayrollRun).where(PayrollRun.period == period)
    if lock:
        q = q.with_for_update()
    run = (await session.execute(q)).scalar_one_or_none()
    if run is None:
        return "none"
    if run.status in ("draft", "processed"):
        return run.status
    ids = list(dict.fromkeys(user_ids))
    if ids:
        q2 = select(Payslip.user_id).where(Payslip.run_id == run.id, Payslip.user_id.in_(ids), Payslip.status == "draft")
        have = set((await session.execute(q2)).scalars())
        if any(u not in have for u in ids):
            raise AppError("PERIOD_LOCKED", "That month is published; open a revision for the employee first")
    return "published"


async def row_states(session: AsyncSession, rows: list[Any], payable: str = "pending") -> dict[uuid.UUID, tuple[str, list[str]]]:
    """row.id -> (run_status, flags) for lists. `PERIOD_CLOSED`: a payable row whose month is published and whose employee has
    no open payslip in it (it will not be paid until HR moves it)."""
    periods = {r.period for r in rows if getattr(r, "period", None) is not None}
    out: dict[uuid.UUID, tuple[str, list[str]]] = {}
    if not periods:
        return {r.id: ("none", []) for r in rows}
    runs = {r.period: r for r in (await session.execute(select(PayrollRun).where(PayrollRun.period.in_(periods)))).scalars()}
    open_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    published_ids = [r.id for r in runs.values() if r.status == "published"]
    if published_ids:
        q = select(Payslip.run_id, Payslip.user_id).where(
            Payslip.run_id.in_(published_ids), Payslip.status.in_(("draft", "processed"))
        )
        open_pairs = {(a, b) for a, b in (await session.execute(q)).all()}
    for r in rows:
        run = runs.get(r.period) if getattr(r, "period", None) is not None else None
        status = run.status if run else "none"
        flags: list[str] = []
        if r.status == payable and run is not None and run.status == "published" and (run.id, r.user_id) not in open_pairs:
            flags.append("PERIOD_CLOSED")
        out[r.id] = (status, flags)
    return out


# ============================================================================================ caps
async def live_counts(session: AsyncSession, user_ids: list[uuid.UUID], period: dt.date) -> dict[uuid.UUID, dict[str, int]]:
    """Per user: live one-time items and live component rows in total for the month (row counts, no money)."""
    ensure_providers()
    out = {u: {"one_time": 0, "total": 0} for u in user_ids}
    if not user_ids:
        return out
    for prov in PROVIDERS:
        if not prov.count_in_caps:
            continue
        m = prov.model
        q = (
            select(m.user_id, func.count())
            .where(m.user_id.in_(user_ids), m.period == period, m.status.in_(prov.live))
            .group_by(m.user_id)
        )
        for uid, n in (await session.execute(q)).all():
            out[uid]["total"] += n
            if prov.type == "one_time":
                out[uid]["one_time"] += n
    return out


def check_caps(counts: dict[str, int], adding: int = 1) -> None:
    if counts["one_time"] + adding > MAX_ONE_TIME_PER_MONTH or counts["total"] + adding > MAX_LINES_PER_MONTH:
        raise AppError("TOO_MANY_ITEMS", "That employee already has the maximum number of items for the month")


# ============================================================================================ pull
def _attach_carried(new_lines: list[dict], old: Payslip | None) -> None:
    """M1: a line that carries `paid_minutes` (rows already paid on the superseded copy) keeps the PUBLISHED amount of that
    portion: `carried` = the superseded copy's line of the same code (amount, capped minutes, total minutes). A revision then
    prices only the newly approved minutes; the paid part never changes with the structure, shift or settings."""
    if old is None:
        return
    prev = {c["code"]: c for c in (old.inputs or {}).get("components", [])}
    for line in new_lines:
        if int(line.get("paid_minutes", 0)) > 0 and line["code"] in prev:
            o = prev[line["code"]]
            line["carried"] = {
                "amount": o["amount"],
                "minutes": int(o.get("minutes", 0)),
                "minutes_total": int(o.get("minutes_total", o.get("minutes", 0))),
            }


def _sort_key(r: Any) -> tuple:
    return (r.created_at or dt.datetime.min.replace(tzinfo=dt.UTC), str(r.id))


async def pull_for(session: AsyncSession, payslips: list[Payslip], period: dt.date, *, lock: bool) -> None:
    """(Re)build `inputs['components']` of the given open payslips of ONE run month from the source tables (P2 / P5).
    `lock=True` (process) locks the pulled rows FOR UPDATE; the caller must already hold the run and payslip locks."""
    ensure_providers()
    by_user = {p.user_id: p for p in payslips}
    if not by_user:
        return
    own_ids = [p.id for p in payslips if p.id is not None]
    sup_ids = [p.supersedes_id for p in payslips if p.supersedes_id is not None]
    lines: dict[uuid.UUID, list[dict]] = {u: [] for u in by_user}
    warnings: dict[uuid.UUID, list[str]] = {u: [] for u in by_user}
    for prov in PROVIDERS:
        m = prov.model
        cond = [m.status == prov.payable]
        if sup_ids:
            cond.append(and_(m.status == "paid", m.payslip_id.in_(sup_ids)))
        if own_ids:
            cond.append(and_(m.status == "processing", m.payslip_id.in_(own_ids)))
        q = select(m).where(m.user_id.in_(list(by_user)), m.period == period, or_(*cond)).order_by(m.id)
        if lock:
            q = q.with_for_update()
        grouped: dict[uuid.UUID, list[Any]] = {}
        for r in (await session.execute(q)).scalars():
            p = by_user[r.user_id]
            if r.status == "paid" and r.payslip_id != p.supersedes_id:
                continue
            if r.status == "processing" and r.payslip_id != p.id:
                continue
            grouped.setdefault(r.user_id, []).append(r)
        if prov.prepare is not None and grouped:
            await prov.prepare(session, [r for rs in grouped.values() for r in rs])
        for uid, rs in grouped.items():
            rs.sort(key=_sort_key)
            lines[uid].extend(prov.build(rs))
        if prov.warn is not None:
            for uid, codes in (await prov.warn(session, list(by_user), period)).items():
                warnings[uid].extend(codes)
    olds: dict[uuid.UUID, Payslip] = {}
    if sup_ids:
        olds = {o.id: o for o in (await session.execute(select(Payslip).where(Payslip.id.in_(sup_ids)))).scalars()}
    for uid, p in by_user.items():
        _attach_carried(lines[uid], olds.get(p.supersedes_id) if p.supersedes_id else None)
        inputs = copy.deepcopy(p.inputs or {})
        inputs["components"] = lines[uid]
        if warnings[uid]:
            inputs["component_warnings"] = sorted(set(warnings[uid]))
        else:
            inputs.pop("component_warnings", None)
        p.inputs = inputs


# ============================================================================================ state transitions
def _source_ids(payslips: list[Payslip]) -> dict[str, dict[uuid.UUID, Payslip]]:
    out: dict[str, dict[uuid.UUID, Payslip]] = {}
    for p in payslips:
        for line in (p.inputs or {}).get("components", []):
            for s in line.get("sources", []):
                out.setdefault(s["type"], {})[uuid.UUID(s["id"])] = p
    return out


async def _lock_rows(session: AsyncSession, prov: Provider, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    if not ids:
        return {}
    q = select(prov.model).where(prov.model.id.in_(ids)).order_by(prov.model.id).with_for_update()
    return {r.id: r for r in (await session.execute(q)).scalars()}


def _changed() -> AppError:
    return AppError(
        "COMPONENTS_CHANGED", "Some pay items changed since this payslip was calculated; reload, refresh and process again"
    )


async def attach_processing(session: AsyncSession, payslips: list[Payslip], actor_id: uuid.UUID) -> None:
    """Process: the pulled rows become `processing` and point at their payslip. A row that is not in the state the pull
    left it in is COMPONENTS_CHANGED (cannot happen while the rows were locked by the same transaction)."""
    ensure_providers()
    sources = _source_ids(payslips)
    for prov in PROVIDERS:
        want = sources.get(prov.type, {})
        rows = await _lock_rows(session, prov, sorted(want))
        for rid, p in want.items():
            row = rows.get(rid)
            if row is None or row.user_id != p.user_id:
                raise _changed()
            if row.status == prov.payable:
                row.status, row.payslip_id, row.updated_by, row.version = "processing", p.id, actor_id, row.version + 1
                add_event(session, item_type=prov.type, item=row, action="process", actor_id=actor_id)
            elif row.status == "processing" and row.payslip_id == p.id:
                continue
            elif row.status == "paid" and p.supersedes_id is not None and row.payslip_id == p.supersedes_id:
                continue  # carried on publish
            else:
                raise _changed()


async def release_processing(session: AsyncSession, payslip_ids: list[uuid.UUID], actor_id: uuid.UUID) -> None:
    """Reopen: `processing` rows of these payslips go back to `pending` (`paid` rows are never touched)."""
    ensure_providers()
    if not payslip_ids:
        return
    for prov in PROVIDERS:
        m = prov.model
        q = select(m).where(m.payslip_id.in_(payslip_ids), m.status == "processing").order_by(m.id).with_for_update()
        for row in (await session.execute(q)).scalars():
            row.status, row.payslip_id, row.updated_by, row.version = prov.payable, None, actor_id, row.version + 1
            add_event(session, item_type=prov.type, item=row, action="reopen", actor_id=actor_id)


async def mark_paid(session: AsyncSession, payslips: list[Payslip], actor_id: uuid.UUID) -> None:
    """Publish: `processing` rows of the payslip become `paid`; `paid` rows of the superseded copy are carried over to the new
    payslip (`payslip_id` moves). Anything else is 409 COMPONENTS_CHANGED and the whole publish rolls back."""
    ensure_providers()
    sources = _source_ids(payslips)
    for prov in PROVIDERS:
        want = sources.get(prov.type, {})
        if prov.lock_parents is not None and want:
            await prov.lock_parents(session, sorted(want))
        rows = await _lock_rows(session, prov, sorted(want))
        paid_now: list[Any] = []
        for rid, p in want.items():
            row = rows.get(rid)
            if row is None or row.user_id != p.user_id:
                raise _changed()
            if row.status == "processing" and row.payslip_id == p.id:
                row.status, row.updated_by, row.version = "paid", actor_id, row.version + 1
                add_event(session, item_type=prov.type, item=row, action="pay", actor_id=actor_id)
                paid_now.append(row)
            elif row.status == "paid" and p.supersedes_id is not None and row.payslip_id == p.supersedes_id:
                row.payslip_id, row.updated_by, row.version = p.id, actor_id, row.version + 1
                add_event(session, item_type=prov.type, item=row, action="carry", actor_id=actor_id)
            elif row.status == "paid" and row.payslip_id == p.id:
                continue
            else:
                raise _changed()
        sup = {p.supersedes_id: p for p in payslips if p.supersedes_id is not None}
        if sup:  # M1: a paid row of the superseded copy that the revision does not carry would lose published pay
            m = prov.model
            q = select(m.id).where(m.status == "paid", m.payslip_id.in_(list(sup))).order_by(m.id)
            for rid in (await session.execute(q)).scalars():
                if rid not in want:
                    raise _changed()
        if paid_now and prov.after_paid is not None:
            await prov.after_paid(session, paid_now, actor_id)
