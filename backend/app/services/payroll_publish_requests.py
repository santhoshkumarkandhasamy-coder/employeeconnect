"""Publish requests: maker-checker for publishing payroll (plan 17 work package 5a, contract 19.2).

While maker-checker applies to a run (run snapshot OR the current setting) nobody publishes directly: a maker (`payroll.manage`)
creates a request for a processed run (scope run) or one processed payslip of a published run (scope payslip), and a different
person (`payroll.publish` + step-up) approves it. The approval runs the existing publish internals in the SAME transaction.

Bound to what was reviewed: the request stores the exact set of `{payslip_id, version, result_mac}` (HMAC, HKDF-derived key) and
`processed_at`; the approval publishes exactly that set or answers 409 PUBLISH_STALE (reopen, refresh, payslip changes and expiry
mark open requests `stale` themselves).

Maker set (who cannot approve): the requester, the run's `contributors`, the `contributors` / creator / processor of every payslip
in the set, the actors of the money history of every component source pulled into those payslips (except the pure lifecycle
actions process / reopen / pay / carry / propose), the F&F makers for rows owned by an F&F (its checker is NOT a maker) and the
creators / editors of the `manual` / `import` salary structures used. It is computed under locks at decision time and stored in
the same UPDATE as `decided_by` (the database repeats the rule with CHECK constraints and a trigger).

Lock order (plan 17 L): run -> payslips -> salary structures (share) -> component source rows -> F&F rows -> the request row LAST
(its version is re-checked). Payment rows are never locked here. Audit rows, logs, pushes and errors carry ids, periods, counts
and codes only: never amounts, names of employees or comments.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import as_utc, utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    FnfSettlement,
    PayrollItemEvent,
    PayrollPublishRequest,
    PayrollRun,
    Payslip,
    Permission,
    Role,
    SalaryStructure,
    User,
)
from ..schemas.payroll_controls import PublishRequestApprove, PublishRequestCreate, PublishRequestReject, VersionIn
from . import admin_security, notifications
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_runs as runs
from . import payroll_settings as pset

log = logging.getLogger("app.payroll")

ENTITY = "payroll_publish_request"
TTL_KEY = "payroll.publish_request_ttl_hours"
LIFECYCLE_ACTIONS = ("process", "reopen", "pay", "carry")  # not the work of a maker; `propose` with a user as actor IS (arrears)
MAX_PUSH_TARGETS = 20
PUSH_COALESCE_S = 600
ZERO = Decimal("0.00")
REASON_ORDER = (
    "REQUESTED",
    "PROCESSED_RUN",
    "EDITED_PAYSLIP",
    "CREATED_COMPONENT",
    "CREATED_STRUCTURE",
    "FNF_MAKER",
    "OWN_PAYSLIP",
)


# ============================================================================================ snapshot
def result_mac(p: Payslip) -> str:
    """HMAC over everything the checker reviews in one payslip (never a plain hash: amounts are low-entropy)."""
    return pc.mac_hex(
        pc.SNAPSHOT_LABEL,
        {
            "k": "payslip", "id": str(p.id), "version": p.version, "revision": p.revision, "hold": p.hold,
            "result": p.result, "inputs": p.inputs, "identity": p.identity, "employee": p.employee_snapshot,
            "days": [str(p.basis_days), str(p.employed_days), str(p.paid_days)],
            "structure": str(p.structure_id) if p.structure_id else None,
        },
    )  # fmt: skip


def _totals(rows: list[Payslip]) -> dict[str, str]:
    from .payroll_reports import _employer_cost  # local import: payroll_reports imports payroll_runs

    gross = ded = net = cost = ZERO
    for p in rows:
        if p.result is None:
            continue
        gross += Decimal(p.result["gross"])
        ded += Decimal(p.result["total_deductions"])
        net += Decimal(p.result["net"])
        cost += _employer_cost(p.result)
    return {
        "gross_earnings": calc.money_str(gross), "total_deductions": calc.money_str(ded),
        "net_pay": calc.money_str(net), "employer_cost": calc.money_str(cost),
    }  # fmt: skip


def build_snapshot(todo: list[Payslip], held: int, processed_at: dt.datetime | None) -> dict[str, Any]:
    return {
        "payslips": [
            {"payslip_id": str(p.id), "version": p.version, "result_mac": result_mac(p)}
            for p in sorted(todo, key=lambda x: str(x.id))
        ],
        "held": held,
        "warnings": sum(bool(runs._split(p.warnings)[1]) for p in todo),
        "processed_at": pc.iso(processed_at),
        "totals": _totals(todo),
    }


def snapshot_mac(req_id: uuid.UUID, run_id: uuid.UUID, scope: str, payslip_id: uuid.UUID | None, snap: dict[str, Any]) -> str:
    return pc.mac_hex(
        pc.SNAPSHOT_LABEL,
        {"k": "request", "id": str(req_id), "run": str(run_id), "scope": scope,
         "payslip": str(payslip_id) if payslip_id else None, "snapshot": snap},
    )  # fmt: skip


def _live_set(
    run: PayrollRun, rows: list[Payslip], scope: str, payslip_id: uuid.UUID | None
) -> tuple[list[Payslip], int, dt.datetime | None]:
    """(payslips that would be published, held processed payslips, processed_at) for the scope, from locked rows."""
    if scope == "payslip":
        ps = [p for p in rows if p.id == payslip_id and p.status == "processed" and not p.hold]
        return ps, 0, ps[0].processed_at if ps else None
    todo = [p for p in rows if p.status == "processed" and not p.hold]
    held = sum(1 for p in rows if p.status == "processed" and p.hold)
    return todo, held, run.processed_at


def _same(stored: dict[str, Any], live: dict[str, Any]) -> bool:
    return (
        stored.get("payslips") == live["payslips"] and stored.get("processed_at") == live["processed_at"]
        and stored.get("held") == live["held"]
    )  # fmt: skip


# ============================================================================================ events, audit, push
def _event(session: AsyncSession, req: PayrollPublishRequest, action: str, actor_id: uuid.UUID | None, user_id: uuid.UUID,
           period: dt.date, **detail: Any) -> None:  # fmt: skip
    """Money history row (append-only, encrypted detail). `user_id` = the employee of a payslip request, else the requester."""
    ref = SimpleNamespace(id=req.id, user_id=user_id, status=req.status, payslip_id=req.payslip_id)
    comps.add_event(session, item_type="publish_request", item=ref, action=action, actor_id=actor_id, period=period,
                    detail=detail or None)  # fmt: skip


def _audit(session: AsyncSession, actor: Any, action: str, req: PayrollPublishRequest, run: PayrollRun, **extra: Any) -> None:
    decided = req.status in ("approved", "rejected")
    audit(
        session, action=action, actor=actor, entity_type=ENTITY, entity_id=req.id,
        after={
            "scope": req.scope, "run_id": str(req.run_id), "period": pc.period_str(run.period),
            "payslips": len(req.snapshot.get("payslips", [])), "status": req.status, "stale_reason": req.stale_reason,
            "maker_id": str(req.requested_by), "checker_id": str(req.decided_by) if decided and req.decided_by else None,
            **extra,
        },
    )  # fmt: skip


def _map_db(exc: IntegrityError) -> AppError:
    state = pc.sqlstate(exc)
    text_ = str(getattr(exc, "orig", exc))
    if (
        "ck_payroll_publish_requests_checker" in text_
        or "ck_payroll_publish_requests_requester_in_makers" in text_
        or (state == "23000" and "decider" in text_)
    ):
        return AppError("SECOND_PERSON_REQUIRED", "A different person must decide this publish request")
    if state == "23505" and "uq_payroll_publish_requests_open" in text_:
        return AppError("PUBLISH_REQUEST_EXISTS", "A publish request is already open")
    return pc.map_integrity(exc)


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _map_db(exc) from None


async def _commit(session: AsyncSession) -> None:
    try:
        await pc.commit(session)
    except AppError:
        raise
    except IntegrityError as exc:  # pragma: no cover - pc.commit maps integrity errors itself
        await session.rollback()
        raise _map_db(exc) from None


async def _push_publishers(
    session: AsyncSession, redis: Redis, exclude: set[uuid.UUID], maker_name: str, period: dt.date, req_id: uuid.UUID
) -> None:
    try:
        q = (
            select(User.id).join(Role, Role.id == User.role_id)
            .where(User.status == "active", User.deleted_at.is_(None), Role.permissions.any(Permission.code == "payroll.publish"))
            .order_by(User.id)
        )  # fmt: skip
        ids = [u for u in (await session.execute(q)).unique().scalars() if u not in exclude][:MAX_PUSH_TARGETS]
        if not ids:
            return
        if not await redis.set("ctr:payroll_approval:publish", "1", nx=True, ex=PUSH_COALESCE_S):
            return  # coalesced: a publish push went out in the last 10 minutes
        await notifications.notify(
            session, ids, type="payroll_approval", entity_id=req_id, title="Approval needed",
            body=f"{maker_name} asked to publish payroll for {runs.month_label(period)}", extra={"kind": "publish"},
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.exception("publish request push failed (ignored)")


async def _push_requester(session: AsyncSession, user_id: uuid.UUID, req_id: uuid.UUID, title: str, period: dt.date) -> None:
    try:
        await notifications.notify(
            session, [user_id], type="payroll_approval", entity_id=req_id, title=title, body=runs.month_label(period),
            extra={"kind": "publish"},
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.exception("publish request push failed (ignored)")


# ============================================================================================ access and output
async def _req_or_404(session: AsyncSession, req_id: uuid.UUID, *, lock: bool = False) -> PayrollPublishRequest:
    q = select(PayrollPublishRequest).where(PayrollPublishRequest.id == req_id)
    if lock:
        q = q.with_for_update()
    r = (await session.execute(q)).scalar_one_or_none()
    if r is None:
        raise AppError("NOT_FOUND", "Publish request not found")
    return r


async def _people(session: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, User]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).unique().scalars()}


async def _out_many(session: AsyncSession, rows: list[PayrollPublishRequest], *, detail: bool) -> list[dict[str, Any]]:
    runs_ = (
        {
            r.id: r
            for r in (await session.execute(select(PayrollRun).where(PayrollRun.id.in_({x.run_id for x in rows})))).scalars()
        }
        if rows
        else {}
    )
    pslips = (
        {
            p.id: p
            for p in (
                await session.execute(select(Payslip).where(Payslip.id.in_({x.payslip_id for x in rows if x.payslip_id})))
            ).scalars()
        }
        if any(x.payslip_id for x in rows)
        else {}
    )
    maker_ids = {uuid.UUID(m) for r in rows for m in (r.makers or [])} if detail else set()
    people = await _people(
        session,
        {x for r in rows for x in (r.requested_by, r.decided_by)} | {p.user_id for p in pslips.values()} | maker_ids,
    )
    out = []
    for r in rows:
        run, ps = runs_[r.run_id], pslips.get(r.payslip_id) if r.payslip_id else None
        snap = dict(r.snapshot)
        snapshot = {
            "payslips": len(snap.get("payslips", [])), "held": snap.get("held", 0), "warnings": snap.get("warnings", 0),
            "processed_at": snap.get("processed_at"),
        }  # fmt: skip
        if detail:
            snapshot["totals"] = snap.get("totals")
        decided = r.status in ("approved", "rejected")
        out.append(
            {
                "id": str(r.id),
                "scope": r.scope,
                "run": {"id": str(run.id), "period": pc.period_str(run.period), "status": run.status},
                "payslip": (
                    {"id": str(ps.id), "user": pc.user_out(people.get(ps.user_id)), "revision": ps.revision}
                    if ps is not None
                    else None
                ),
                "status": r.status,
                "stale_reason": r.stale_reason,
                "snapshot": snapshot,
                "comment": r.comment,
                "requested_by": pc.user_out(people.get(r.requested_by)),
                "requested_at": pc.iso(r.requested_at),
                "expires_at": pc.iso(r.expires_at),
                "makers": (
                    [pc.user_out(people.get(uuid.UUID(m))) for m in sorted(r.makers or [])]
                    if detail and decided and r.makers
                    else None
                ),
                "decided_by": pc.user_out(people.get(r.decided_by)),
                "decided_at": pc.iso(r.decided_at),
                "decision_comment": r.decision_comment,
                "version": r.version,
            }
        )
    return out


async def _one(session: AsyncSession, req_id: uuid.UUID) -> dict[str, Any]:
    r = await _req_or_404(session, req_id)
    await session.refresh(r)
    return (await _out_many(session, [r], detail=True))[0]


# ============================================================================================ staleness
async def _mark(
    session: AsyncSession, r: PayrollPublishRequest, run: PayrollRun, reason: str, actor_id: uuid.UUID | None, *,
    action: str = "stale", actor: Any = None,
) -> None:  # fmt: skip
    r.status, r.stale_reason, r.version, r.updated_by = "stale", reason, r.version + 1, actor_id
    user_id = r.requested_by
    if r.payslip_id is not None:
        user_id = (await session.execute(select(Payslip.user_id).where(Payslip.id == r.payslip_id))).scalar_one()
    _event(session, r, action, actor_id, user_id, run.period, reason=reason)
    _audit(session, actor or _Who(actor_id), f"payroll.publish_request.{action}", r, run)
    requester, period, rid = r.requested_by, run.period, r.id

    async def _push() -> None:
        await _push_requester(session, requester, rid, "Publish request is outdated", period)

    pc.after_commit(session, _push)


class _Who:
    def __init__(self, actor_id: uuid.UUID | None):
        self.id, self.employee_id = actor_id, None


async def mark_stale(
    session: AsyncSession, run_id: uuid.UUID, reason: str, actor_id: uuid.UUID | None, *, payslip_id: uuid.UUID | None = None,
    actor: Any = None,
) -> int:  # fmt: skip
    """Open requests of the run become `stale` in the caller's transaction (run and payslip locks are already held; the request
    rows are locked last). With `payslip_id` only the request of that payslip and run-scope requests are affected."""
    q = select(PayrollPublishRequest).where(PayrollPublishRequest.run_id == run_id, PayrollPublishRequest.status == "submitted")
    if payslip_id is not None:
        q = q.where(or_(PayrollPublishRequest.scope == "run", PayrollPublishRequest.payslip_id == payslip_id))
    rows = list((await session.execute(q.order_by(PayrollPublishRequest.id).with_for_update())).scalars())
    if not rows:
        return 0
    run = await session.get(PayrollRun, run_id)
    for r in rows:
        await _mark(session, r, run, reason, actor_id, actor=actor)
    return len(rows)


async def expire_overdue(session: AsyncSession, *, run_id: uuid.UUID | None = None, commit: bool = True) -> int:
    """Open requests past `expires_at` become `stale` / EXPIRED (system actor). Used lazily by the reads (own commit) and by the hourly
    job; `commit=False` joins the caller's transaction (create)."""
    now = utcnow()
    q = select(PayrollPublishRequest).where(PayrollPublishRequest.status == "submitted", PayrollPublishRequest.expires_at < now)
    if run_id is not None:
        q = q.where(PayrollPublishRequest.run_id == run_id)
    # lock order (as every other path): run first, request rows last. Peek at the candidates, lock their runs in id order, then lock
    # and re-read the requests that are still overdue.
    run_ids = sorted(set((await session.execute(q.with_only_columns(PayrollPublishRequest.run_id))).scalars()))
    if not run_ids:
        return 0
    await session.execute(select(PayrollRun.id).where(PayrollRun.id.in_(run_ids)).order_by(PayrollRun.id).with_for_update())
    rows = list((await session.execute(q.order_by(PayrollPublishRequest.id).with_for_update())).scalars())
    if not rows:
        return 0
    for r in rows:
        await _mark(session, r, await session.get(PayrollRun, r.run_id), "EXPIRED", None, action="expire")
    if commit:
        await pc.commit(session)
    return len(rows)


# ============================================================================================ maker set
async def _sources(session: AsyncSession, payslips: list[Payslip], *, lock: bool) -> dict[tuple[str, uuid.UUID], Any]:
    """Source rows of the components in the payslips, (type, id) -> row. `lock` takes them FOR UPDATE in provider order (the same
    order the publish itself uses, so the maker set is computed from rows nobody can change meanwhile)."""
    comps.ensure_providers()
    wanted = comps._source_ids(payslips)
    found: dict[tuple[str, uuid.UUID], Any] = {}
    for prov in comps.PROVIDERS:
        want = sorted(wanted.get(prov.type, {}))
        if not want:
            continue
        if lock:
            if prov.lock_parents is not None:
                await prov.lock_parents(session, want)
            rows = await comps._lock_rows(session, prov, want)
        else:
            rows = {r.id: r for r in (await session.execute(select(prov.model).where(prov.model.id.in_(want)))).scalars()}
        for rid, row in rows.items():
            found[(prov.type, rid)] = row
    return found


async def compute_makers(
    session: AsyncSession, run: PayrollRun, payslips: list[Payslip], *, scope: str, requester_id: uuid.UUID | None, lock: bool
) -> dict[str, set[str]]:
    """user id (text) -> reason codes. `lock=True` (decision time) locks structures (share), component rows and F&F rows first."""
    reasons: dict[str, set[str]] = defaultdict(set)
    if requester_id is not None:
        reasons[str(requester_id)].add("REQUESTED")
    if scope == "run":
        for u in [*(run.contributors or []), *(str(x) for x in (run.created_by, run.processed_by) if x)]:
            reasons[u].add("PROCESSED_RUN")
    for p in payslips:
        for u in [*(p.contributors or []), *(str(x) for x in (p.created_by, p.updated_by, p.processed_by) if x)]:
            reasons[u].add("EDITED_PAYSLIP")
    if lock:
        await runs._share_lock_structures(session, payslips)
    if lock:  # structures that triggered arrears rows are share-locked with the others, before the component rows
        extra = {
            r.trigger_structure_id
            for (typ, _), r in (await _sources(session, payslips, lock=False)).items()
            if typ == "arrears" and r.trigger_structure_id
        }
        await runs._share_lock_structures(session, [SimpleNamespace(structure_id=i) for i in extra])  # type: ignore[list-item]
    rows = await _sources(session, payslips, lock=lock)
    fnf_ids: set[uuid.UUID] = set()
    event_keys: dict[str, set[uuid.UUID]] = defaultdict(set)
    for (typ, rid), row in rows.items():
        fid = getattr(row, "fnf_id", None)
        if fid is not None:
            fnf_ids.add(fid)  # owned by a final settlement: its makers count, not the checker who wrote the row
            continue
        event_keys[typ].add(rid)
        if typ == "loan_installment":
            event_keys["loan"].add(row.loan_id)  # the loan itself: created, rescheduled, skipped
    if event_keys:
        cond = [and_(PayrollItemEvent.item_type == t, PayrollItemEvent.item_id.in_(sorted(ids))) for t, ids in event_keys.items()]
        q = select(PayrollItemEvent.actor_id).where(
            or_(*cond), PayrollItemEvent.actor_id.is_not(None), PayrollItemEvent.action.not_in(LIFECYCLE_ACTIONS)
        )
        for aid in set((await session.execute(q)).scalars()):
            reasons[str(aid)].add("CREATED_COMPONENT")
    if fnf_ids:
        fq = select(FnfSettlement).where(FnfSettlement.id.in_(sorted(fnf_ids))).order_by(FnfSettlement.id)
        if lock:
            fq = fq.with_for_update()
        for f in (await session.execute(fq)).scalars():
            for u in [*(f.contributors or []), *(str(x) for x in (f.created_by, f.last_edited_by, f.submitted_by) if x)]:
                reasons[u].add("FNF_MAKER")
    sids = {p.structure_id for p in payslips if p.structure_id}
    # the structure that triggered an arrears row decided its amount: its creator and editors are makers too
    sids |= {r.trigger_structure_id for (typ, _), r in rows.items() if typ == "arrears" and r.trigger_structure_id}
    sids = sorted(sids)
    if sids:
        sq = select(SalaryStructure).where(SalaryStructure.id.in_(sids), SalaryStructure.source.in_(("manual", "import")))
        for s in (await session.execute(sq)).scalars():
            for u in (s.created_by, s.updated_by):
                if u is not None:
                    reasons[str(u)].add("CREATED_STRUCTURE")
    return reasons


def _reasons_out(rs: set[str]) -> list[str]:
    return [r for r in REASON_ORDER if r in rs]


async def _gate(
    session: AsyncSession,
    actor: Principal,
    makers: dict[str, set[str]],
    req: PayrollPublishRequest,
    employee_id: uuid.UUID | None,
) -> None:
    """Checker rules: not the employee of a payslip request (403 FORBIDDEN, audited), not in the maker set (403 SECOND_PERSON_REQUIRED)."""
    if req.scope == "payslip" and employee_id == actor.id:
        raise await pc.deny(session, actor, entity_type=ENTITY, entity_id=req.id, reason="self")
    if str(actor.id) in makers or actor.id == req.requested_by:
        raise AppError("SECOND_PERSON_REQUIRED", "A different person must decide a request for payroll you prepared")


# ============================================================================================ reads
async def publish_check(
    session: AsyncSession, actor: Principal, *, run_id: uuid.UUID | None = None, payslip_id: uuid.UUID | None = None
) -> dict[str, Any]:
    scope = "payslip" if payslip_id is not None else "run"
    ps_one: Payslip | None = None
    if payslip_id is not None:
        ps_one = (await session.execute(select(Payslip).where(Payslip.id == payslip_id))).scalar_one_or_none()
        if ps_one is None:
            raise AppError("NOT_FOUND", "Payslip not found")
        run_id = ps_one.run_id
    run = await runs._run_or_404(session, run_id)  # type: ignore[arg-type]
    await expire_overdue(session, run_id=run.id)
    rows = await runs._run_payslips(session, run.id)
    todo, held, _ = _live_set(run, rows, scope, payslip_id)
    q = select(PayrollPublishRequest).where(PayrollPublishRequest.status == "submitted", PayrollPublishRequest.run_id == run.id)
    q = (
        q.where(PayrollPublishRequest.payslip_id == payslip_id)
        if scope == "payslip"
        else q.where(PayrollPublishRequest.scope == "run")
    )
    open_req = (await session.execute(q)).scalars().first()
    mc = await runs.maker_checker_applies(session, run)
    makers = await compute_makers(
        session, run, todo, scope=scope, requester_id=open_req.requested_by if open_req else None, lock=False
    )
    employee_id = ps_one.user_id if ps_one is not None else None
    me = set(makers.get(str(actor.id), set()))
    own = scope == "payslip" and employee_id == actor.id
    if own:
        me.add("OWN_PAYSLIP")
    ready = bool(todo) and ((scope == "run" and run.status == "processed") or (scope == "payslip" and run.status == "published"))
    can_request = mc and ready and open_req is None and actor.has("payroll.manage") and not own
    can_approve = bool(
        mc
        and open_req is not None
        and ready
        and actor.has("payroll.publish")
        and not own
        and str(actor.id) not in makers
        and as_utc(open_req.expires_at) > utcnow()
    )
    can_direct = bool(not mc and ready and actor.has("payroll.publish") and not own)
    people = await _people(session, {uuid.UUID(m) for m in makers})
    out = {
        "maker_checker": mc,
        "can_request": bool(can_request),
        "can_publish_directly": can_direct,
        "can_approve": can_approve,
        "open_request": {"id": str(open_req.id), "status": open_req.status} if open_req else None,
        "me": {"reasons": _reasons_out(me)},
        "makers": [
            {"user": pc.user_out(people.get(uuid.UUID(m))), "reasons": _reasons_out(rs)}
            for m, rs in sorted(makers.items(), key=lambda kv: kv[0])
            if people.get(uuid.UUID(m)) is not None
        ],
        "publishable": {"payslips": len(todo), "held": held},
    }
    audit(session, action="payroll.publish_request.check", actor=actor, entity_type="payroll_run", entity_id=run.id,
          after={"scope": scope, "payslips": len(todo)})  # fmt: skip
    await pc.commit(session)
    return out


async def list_requests(
    session: AsyncSession, actor: Principal, *, status: str | None, run_id: uuid.UUID | None, page: PageParams
) -> dict[str, Any]:
    await expire_overdue(session)
    cond = []
    if status:
        cond.append(PayrollPublishRequest.status == status)
    if run_id:
        cond.append(PayrollPublishRequest.run_id == run_id)
    total = (await session.execute(select(func.count()).select_from(PayrollPublishRequest).where(*cond))).scalar_one()
    q = (
        select(PayrollPublishRequest).where(*cond)
        .order_by(PayrollPublishRequest.requested_at.desc(), PayrollPublishRequest.id)
        .offset(page.offset).limit(page.page_size)
    )  # fmt: skip
    rows = list((await session.execute(q)).scalars())
    items = await _out_many(session, rows, detail=False)
    await pc.audit_read(session, actor, "payroll.publish_request.list", ENTITY, count=len(items))
    return make_page(items, page, total)


async def get_request(session: AsyncSession, actor: Principal, req_id: uuid.UUID) -> dict[str, Any]:
    r = await _req_or_404(session, req_id)
    await expire_overdue(session, run_id=r.run_id)
    await session.refresh(r)
    out = (await _out_many(session, [r], detail=True))[0]
    audit(session, action="payroll.publish_request.view", actor=actor, entity_type=ENTITY, entity_id=r.id)
    await pc.commit(session)
    return out


# ============================================================================================ create / withdraw
async def create(
    session: AsyncSession, redis: Redis, actor: Principal, *, run_id: uuid.UUID | None, payslip_id: uuid.UUID | None,
    body: PublishRequestCreate,
) -> dict[str, Any]:  # fmt: skip
    scope = "payslip" if payslip_id is not None else "run"
    if payslip_id is not None:
        owner = (await session.execute(select(Payslip.user_id, Payslip.run_id).where(Payslip.id == payslip_id))).first()
        if owner is None:
            raise AppError("NOT_FOUND", "Payslip not found")
        run_id = owner.run_id
        await pc.forbid_self(session, actor, owner.user_id, entity_type="payslip", entity_id=payslip_id)
    run = await runs._run_or_404(session, run_id, lock=True)  # type: ignore[arg-type]
    if not await runs.maker_checker_applies(session, run):
        raise AppError("MAKER_CHECKER_OFF", "Publishing does not need a second person; publish directly")
    rows = await runs._run_payslips(session, run.id, lock=True)
    if scope == "run":
        if run.status != "processed":
            raise AppError("PAYROLL_LOCKED" if run.status == "published" else "CONFLICT", "Only a processed run can be published")
    else:
        ps = next((p for p in rows if p.id == payslip_id), None)
        if ps is None or run.status != "published" or ps.status != "processed":
            raise AppError("PAYROLL_LOCKED", "Only a processed payslip of a published run can be published")
    todo, held, processed_at = _live_set(run, rows, scope, payslip_id)
    if not todo:
        raise AppError("NOTHING_TO_PUBLISH", "There is no processed payslip to publish")
    await expire_overdue(session, run_id=run.id, commit=False)
    q = select(PayrollPublishRequest.id).where(
        PayrollPublishRequest.status == "submitted", PayrollPublishRequest.run_id == run.id
    )
    q = (
        q.where(PayrollPublishRequest.payslip_id == payslip_id)
        if scope == "payslip"
        else q.where(PayrollPublishRequest.scope == "run")
    )
    if (await session.execute(q.limit(1))).first():
        raise AppError("PUBLISH_REQUEST_EXISTS", "A publish request is already open")
    snap = build_snapshot(todo, held, processed_at)
    rid, now = uuid.uuid4(), utcnow()
    ttl = int(await pset.current_value(session, TTL_KEY))
    req = PayrollPublishRequest(
        id=rid, run_id=run.id, scope=scope, payslip_id=payslip_id, status="submitted", snapshot=snap,
        snapshot_mac=snapshot_mac(rid, run.id, scope, payslip_id, snap), comment=body.comment, requested_by=actor.id,
        requested_at=now, expires_at=now + dt.timedelta(hours=ttl), makers=[], version=1, updated_by=actor.id,
    )  # fmt: skip
    session.add(req)
    await _flush(session)
    employee = todo[0].user_id if scope == "payslip" else actor.id
    _event(session, req, "create", actor.id, employee, run.period)
    _audit(session, actor, "payroll.publish_request.create", req, run)
    exclude = {actor.id, *(uuid.UUID(c) for c in (run.contributors or []) if scope == "run")}
    exclude |= {uuid.UUID(c) for p in todo for c in (p.contributors or [])}
    name, period = actor.user.full_name, run.period

    async def _push() -> None:
        await _push_publishers(session, redis, exclude, name, period, rid)

    pc.after_commit(session, _push)
    await _commit(session)
    return await _one(session, rid)


async def withdraw(session: AsyncSession, actor: Principal, req_id: uuid.UUID, body: VersionIn) -> dict[str, Any]:
    peek = await _req_or_404(session, req_id)
    run = await runs._run_or_404(session, peek.run_id, lock=True)
    ps = None
    if peek.payslip_id is not None:
        ps = (await session.execute(select(Payslip).where(Payslip.id == peek.payslip_id).with_for_update())).scalar_one_or_none()
    r = await _req_or_404(session, req_id, lock=True)  # the request row last
    if r.status != "submitted":
        raise AppError("NOT_SUBMITTED", "Only a submitted publish request can be withdrawn")
    if r.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The publish request was changed meanwhile; reload it")
    now = utcnow()
    r.status, r.withdrawn_by, r.withdrawn_at, r.version, r.updated_by = "withdrawn", actor.id, now, r.version + 1, actor.id
    if ps is not None and ps.status in ("draft", "processed"):
        pc.touch(ps, actor.id)  # the withdrawer becomes a contributor (a published run is locked: the payslip row carries it)
    elif ps is None and run.status in ("draft", "processed"):
        pc.touch(run, actor.id)
    await _flush(session)
    _event(session, r, "withdraw", actor.id, ps.user_id if ps is not None else r.requested_by, run.period)
    _audit(session, actor, "payroll.publish_request.withdraw", r, run)
    await _commit(session)
    return await _one(session, req_id)


# ============================================================================================ decisions
async def _locked_context(session: AsyncSession, peek: PayrollPublishRequest) -> tuple[PayrollRun, list[Payslip], Payslip | None]:
    """Run -> payslips (lock order of the publish itself)."""
    run = await runs._run_or_404(session, peek.run_id, lock=True)
    rows = await runs._run_payslips(session, run.id, lock=True)
    ps = next((p for p in rows if p.id == peek.payslip_id), None) if peek.payslip_id else None
    return run, rows, ps


async def _stale_now(
    session: AsyncSession, actor: Principal, r: PayrollPublishRequest, run: PayrollRun, reason: str, *, action: str = "stale"
) -> AppError:
    await _mark(session, r, run, reason, actor.id, action=action, actor=actor)
    await pc.commit(session)
    return AppError("PUBLISH_STALE", "The payroll changed since this request was made; ask for a new publish request")


async def reject(session: AsyncSession, actor: Principal, req_id: uuid.UUID, body: PublishRequestReject) -> dict[str, Any]:
    peek = await _req_or_404(session, req_id)
    run, rows, ps = await _locked_context(session, peek)
    todo, _, _ = _live_set(run, rows, peek.scope, peek.payslip_id)
    makers = await compute_makers(session, run, todo, scope=peek.scope, requester_id=peek.requested_by, lock=True)
    r = await _req_or_404(session, req_id, lock=True)
    await _gate(session, actor, makers, r, ps.user_id if ps is not None else None)
    if r.status != "submitted":
        raise AppError("NOT_SUBMITTED", "The publish request is not waiting for a decision")
    if r.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The publish request was changed meanwhile; reload it")
    r.status, r.decided_by, r.decided_at, r.decision_comment = "rejected", actor.id, utcnow(), body.comment
    r.makers = sorted(set(r.makers or []) | set(makers))
    r.version, r.updated_by = r.version + 1, actor.id
    await _flush(session)
    _event(session, r, "reject", actor.id, ps.user_id if ps is not None else r.requested_by, run.period)
    _audit(session, actor, "payroll.publish_request.reject", r, run)
    requester, period = r.requested_by, run.period

    async def _push() -> None:
        await _push_requester(session, requester, req_id, "Publish request rejected", period)

    pc.after_commit(session, _push)
    await _commit(session)
    return await _one(session, req_id)


async def approve(
    session: AsyncSession, redis: Redis, actor: Principal, req_id: uuid.UUID, body: PublishRequestApprove
) -> dict[str, Any]:
    peek = await _req_or_404(session, req_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.publish_request.approve")
    # cheap pre-gate (no locks): the requester and the stored maker set can never approve, whatever state the request is in
    employee_id = (
        (await session.execute(select(Payslip.user_id).where(Payslip.id == peek.payslip_id))).scalar_one_or_none()
        if peek.payslip_id
        else None
    )
    await _gate(session, actor, {m: set() for m in (peek.makers or [])}, peek, employee_id)
    if peek.status != "submitted":
        raise AppError("NOT_SUBMITTED", "The publish request is not waiting for a decision")
    run, rows, ps = await _locked_context(session, peek)
    todo, held, processed_at = _live_set(run, rows, peek.scope, peek.payslip_id)
    makers = await compute_makers(session, run, todo, scope=peek.scope, requester_id=peek.requested_by, lock=True)
    r = await _req_or_404(session, req_id, lock=True)  # the request row LAST
    await _gate(session, actor, makers, r, employee_id)
    if r.status != "submitted":
        raise AppError("NOT_SUBMITTED", "The publish request is not waiting for a decision")
    if r.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The publish request was changed meanwhile; reload it")
    if as_utc(r.expires_at) <= utcnow():
        raise await _stale_now(session, actor, r, run, "EXPIRED", action="expire")
    stored = r.snapshot
    if r.snapshot_mac != snapshot_mac(r.id, r.run_id, r.scope, r.payslip_id, stored):
        raise await _stale_now(session, actor, r, run, "PAYSLIP_CHANGED")  # the stored snapshot itself was tampered with
    want_status = "processed" if r.scope == "run" else "published"
    if run.status != want_status:
        raise await _stale_now(session, actor, r, run, "RUN_REOPENED")
    if r.scope == "payslip" and (ps is None or ps.status != "processed"):
        raise await _stale_now(session, actor, r, run, "PAYSLIP_CHANGED")
    live = build_snapshot(todo, held, processed_at)
    if not _same(stored, live):
        raise await _stale_now(session, actor, r, run, "PAYSLIP_CHANGED")
    # decide first (the database checks the decider against the contributors), then publish in the same transaction
    r.status, r.decided_by, r.decided_at, r.decision_comment = "approved", actor.id, utcnow(), body.comment
    r.makers = sorted(set(r.makers or []) | set(makers))
    r.version, r.updated_by = r.version + 1, actor.id
    await _flush(session)
    try:
        if r.scope == "run":
            pushes = await runs.publish_run_core(session, actor, run, todo, request_id=r.id)
        else:
            pushes = await runs.publish_payslip_core(session, actor, run, ps, request_id=r.id)  # type: ignore[arg-type]
    except AppError:
        await session.rollback()  # COMPONENTS_CHANGED / FNF_NOT_INCLUDED: nothing is published, the request stays submitted
        raise
    except IntegrityError as exc:
        await session.rollback()
        raise _map_db(exc) from None
    _event(session, r, "approve", actor.id, ps.user_id if ps is not None else r.requested_by, run.period)
    _audit(session, actor, "payroll.publish_request.approve", r, run, self_included=any(p.user_id == actor.id for p in todo))
    requester, period = r.requested_by, run.period

    async def _push() -> None:
        await _push_requester(session, requester, req_id, "Publish request approved", period)

    pc.after_commit(session, _push)
    await _commit(session)
    await runs._push_published(session, pushes)
    await runs._push_settled(session)
    out = {"request": await _one(session, req_id)}
    if peek.scope == "run":
        out["run"] = await runs.run_out(session, await runs._run_or_404(session, peek.run_id))
    else:
        out["payslip"] = await runs.admin_payslip_out(session, await session.get(Payslip, peek.payslip_id))
    return out
