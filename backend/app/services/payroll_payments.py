"""Bank payment batches (plan 17 work package 5a, contract 19.5).

draft -> submitted -> approved -> exported -> closed (+ cancelled; withdraw and reject go back to draft). A batch pays the live
PUBLISHED payslips of ONE published run. Beneficiary data is the payslip `identity` snapshot (what the publish checker reviewed);
when the employee's current details differ, the line is flagged BANK_DETAILS_CHANGED and the maker picks `account_source`.

* One live line per payslip (database partial unique); a revision pays only the DIFFERENCE to what the earlier copies were paid
  (paid and exported lines count as paid, a pending line of an earlier copy blocks); a negative difference is a RECOVERY blocker.
  When an earlier copy's exported line is later confirmed FAILED the revision's delta no longer covers the net: the payslip becomes an
  UNDERPAID_CHAIN blocker (and a PAYMENT_UNDERPAID dashboard alert) until somebody settles it outside the system. A payslip (or an
  ancestor) with a failed line gets the PREVIOUSLY_FAILED flag on its next line; whoever marked a line failed (`failed_by`) can never
  create, edit, submit or approve a batch that pays that payslip again.
* Submit freezes the lines and stores `lines_mac` (HMAC, HKDF key of its own). Approve re-derives EVERY pending line from the live
  payslip and the current details and compares (409 BATCH_STALE), then compares the MAC; nothing is approved otherwise.
* The file is rendered from the stored lines only (payroll_payment_file), the first download stores its SHA-256 and marks the batch
  and its lines exported in ONE transaction; later downloads rebuild the identical bytes and verify the hash.
* Lock order (plan 17 L): payroll_runs (share) -> batch -> lines -> payslips (share) -> employee_payroll_details (share). Publishing
  never locks payment rows, so there is no cycle.
* Maker-checker (service, database CHECKs and triggers, audit `maker_id` / `checker_id`): the checker is not the creator, last editor,
  submitter or anybody in `contributors`, is not paid by a line (403 FORBIDDEN for the whole batch) and did not change the bank
  details of a line employee within `payroll.bank_change_alert_days` (at least 7 days; every change of the append-only history
  counts, not only the latest). Step-up before every state check. Approve and the first download re-check the value date and that
  the debit account is still active. Only an `exported` batch can be downloaded again (an alert says so); a `closed` batch cannot.
Audit rows, pushes, logs and errors carry ids, periods, references and counts only: never amounts, accounts, IFSC, names or comments.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from dataclasses import field as dc_field
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import as_utc, utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    CompanyBankAccount,
    EmployeeBankChange,
    EmployeePayrollDetails,
    PayrollPaymentBatch,
    PayrollPaymentLine,
    PayrollRun,
    Payslip,
    Permission,
    Role,
    User,
)
from ..schemas.payroll_controls import (
    PaymentBatchApprove,
    PaymentBatchCreate,
    PaymentBatchReject,
    PaymentBatchUpdate,
    PaymentConfirm,
    PaymentFileIn,
    VersionIn,
)
from . import admin_security, notifications
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_payment_file as pf
from . import payroll_runs as runs
from . import payroll_settings as pset

log = logging.getLogger("app.payroll")

ENTITY = "payment_batch"
MAX_LINES = 1000
MAX_DAYS_AHEAD = 30
LIVE_LINE = ("pending", "exported", "paid")
MAX_PUSH_TARGETS = 20
PUSH_COALESCE_S = 600
BLOCKERS = (
    "NO_BANK_DETAILS", "INVALID_ACCOUNT", "INVALID_IFSC", "NAME", "NOT_BANK_TRANSFER", "HELD", "ZERO_NET", "ALREADY_PAID",
    "IN_OTHER_BATCH", "RECOVERY", "UNDERPAID_CHAIN",
)  # fmt: skip
SETTING_FORMAT, SETTING_TYPE, SETTING_DAYS = (
    "payroll.bank_file_format",
    "payroll.bank_payment_type",
    "payroll.bank_change_alert_days",
)
MIN_ALERT_DAYS = pset.MIN_BANK_DAYS  # the look-back window is never shorter, whatever the setting holds (the database repeats it)


def _bad(field: str, message: str, code: str = "VALIDATION_ERROR") -> AppError:
    return AppError(code, message, details=[{"field": field, "message": message}])


# ============================================================================================ errors / events / audit
def _map_db(exc: IntegrityError) -> AppError:
    state = pc.sqlstate(exc)
    text_ = str(getattr(exc, "orig", exc))
    if (
        "ck_payroll_payment_batches_checker" in text_
        or "recently changed the bank details" in text_
        or "marked an earlier payment" in text_
    ):
        return AppError("SECOND_PERSON_REQUIRED", "A different person must make this change")
    if "approver is paid by a line" in text_:
        return AppError("FORBIDDEN", "A batch that pays you cannot be approved by you")
    if state == "23505":
        return AppError("CONFLICT", "A payslip of this batch is already in another live payment line, or the batch changed")
    if state == "23000":
        return AppError("CONFLICT", "The payment batch cannot be changed in its current state")
    return pc.map_integrity(exc)


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _map_db(exc) from None


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        session.info.pop("after_commit", None)
        await session.rollback()
        raise _map_db(exc) from None
    for fn in session.info.pop("after_commit", []):
        try:
            await fn()
        except Exception:  # noqa: BLE001
            log.exception("after-commit callback failed (ignored)")


def _event(
    session: AsyncSession, b: PayrollPaymentBatch, action: str, actor_id: uuid.UUID, period: dt.date, **detail: Any
) -> None:
    """Money history (append-only). `user_id` of a batch event is the batch creator; details never hold amounts or accounts."""
    from types import SimpleNamespace

    ref = SimpleNamespace(id=b.id, user_id=b.created_by, status=b.status, payslip_id=None)
    comps.add_event(
        session, item_type="payment_batch", item=ref, action=action, actor_id=actor_id, period=period, detail=detail or None
    )


def _audit(
    session: AsyncSession,
    actor: Any,
    action: str,
    b: PayrollPaymentBatch,
    run: PayrollRun,
    lines: list[PayrollPaymentLine],
    **extra: Any,
) -> None:
    live = [ln for ln in lines if ln.status != "excluded"]
    decided = b.status in ("approved", "exported", "closed")
    audit(
        session, action=action, actor=actor, entity_type=ENTITY, entity_id=b.id,
        after={
            "run_id": str(b.run_id), "period": pc.period_str(run.period), "reference": b.reference, "lines": len(live),
            "excluded": len(lines) - len(live), "status": b.status, "flags_count": sum(len(ln.flags or []) for ln in live),
            "maker_id": str(b.submitted_by or b.created_by), "checker_id": str(b.decided_by) if decided and b.decided_by else None,
            **extra,
        },
    )  # fmt: skip


# ============================================================================================ derivation
@dataclass
class Derived:
    payslip: Payslip
    amount: Decimal
    flags: list[str]
    beneficiary: dict[str, Any]
    delta: bool


def _norm(v: str | None) -> str:
    return (v or "").strip()


def _acct(bank_name: str | None, account: str | None, ifsc: str | None) -> dict[str, str]:
    return {"bank_name": _norm(bank_name), "account": _norm(account), "ifsc": _norm(ifsc).upper()}


def derive(
    ps: Payslip, det: EmployeePayrollDetails | None, user: User | None, *, chain_paid: Decimal, chain_pending: bool,
    own_live: PayrollPaymentLine | None, now: dt.datetime, alert_days: int, previously_failed: bool = False,
) -> Derived | str:  # fmt: skip
    """The line a published payslip would produce, or the blocker code that prevents it."""
    snap, ident = ps.employee_snapshot or {}, ps.identity or {}
    if ps.hold:
        return "HELD"
    if snap.get("payment_mode", "bank_transfer") != "bank_transfer":
        return "NOT_BANK_TRANSFER"
    net = Decimal(ps.result["net"]) if ps.result else Decimal("0.00")
    if net <= 0:
        return "ZERO_NET"
    a = _acct(ident.get("bank_name"), ident.get("bank_account"), ident.get("bank_ifsc"))
    if not a["account"] or not a["ifsc"]:
        return "NO_BANK_DETAILS"
    if not pf.ACCOUNT_RE.match(a["account"]):
        return "INVALID_ACCOUNT"
    if not pf.IFSC_RE.match(a["ifsc"]):
        return "INVALID_IFSC"
    name = snap.get("full_name") or ""
    if not pf.bank_safe_name(name):
        return "NAME"
    if own_live is not None:
        if own_live.status in ("paid", "exported") and calc.q2(chain_paid + Decimal(own_live.amount)) < net:
            return "UNDERPAID_CHAIN"  # an earlier copy's exported line failed after this delta was computed
        return "ALREADY_PAID" if own_live.status == "paid" else "IN_OTHER_BATCH"
    if chain_pending:
        return "IN_OTHER_BATCH"
    amount = calc.q2(net - chain_paid)
    if amount < 0:
        return "RECOVERY"
    if amount == 0:
        return "ALREADY_PAID"
    flags: list[str] = []
    cur = None
    cur_mode = det.payment_mode if det is not None else "bank_transfer"
    cur_acct = _acct(det.bank_name if det else None, det.bank_account if det else None, det.bank_ifsc if det else None)
    if det is None or cur_mode != "bank_transfer" or cur_acct != a:
        flags.append("BANK_DETAILS_CHANGED")
        cur = cur_acct
    changed_at = det.bank_changed_at if det is not None else None
    if changed_at is not None and as_utc(changed_at) > now - dt.timedelta(days=max(alert_days, MIN_ALERT_DAYS)):
        flags.append("RECENT_BANK_CHANGE")
    delta = ps.revision > 1 and chain_paid > 0
    if delta:
        flags.append("REVISION_DELTA")
    if not _norm(ident.get("pan")):
        flags.append("NO_PAN")
    if previously_failed:
        flags.append("PREVIOUSLY_FAILED")
    beneficiary = {
        "name": name, "snapshot": a, "current": cur, "changed_at": pc.iso(changed_at) if changed_at else None,
        "payslip_version": ps.version, "delta": delta,
    }  # fmt: skip
    return Derived(ps, amount, flags, beneficiary, delta)


@dataclass
class Ctx:
    """Everything needed to derive lines for the live published payslips of one run."""

    run: PayrollRun
    live: dict[uuid.UUID, Payslip]  # user -> live published payslip
    users: dict[uuid.UUID, User]
    details: dict[uuid.UUID, EmployeePayrollDetails]
    ancestors: dict[uuid.UUID, list[uuid.UUID]]  # payslip id -> earlier revisions (supersedes chain)
    lines: dict[uuid.UUID, list[PayrollPaymentLine]]  # payslip id -> live lines in ANY batch except `exclude_batch`
    now: dt.datetime
    alert_days: int
    failed: dict[uuid.UUID, list[PayrollPaymentLine]] = dc_field(
        default_factory=dict
    )  # payslip id -> its failed lines (any batch)

    def failed_lines(self, ps_id: uuid.UUID) -> list[PayrollPaymentLine]:
        """Failed lines of the payslip and of every earlier copy of it."""
        return [ln for pid in (ps_id, *self.ancestors.get(ps_id, [])) for ln in self.failed.get(pid, [])]

    def failers(self, ps_id: uuid.UUID) -> set[uuid.UUID]:
        return {ln.failed_by for ln in self.failed_lines(ps_id) if ln.failed_by is not None}

    def derive(self, ps: Payslip) -> Derived | str:
        mine = [ln for ln in self.lines.get(ps.id, []) if ln.status in LIVE_LINE]
        own = next((ln for ln in mine if ln.status == "paid"), None) or (mine[0] if mine else None)
        paid, pending = Decimal("0.00"), False
        for aid in self.ancestors.get(ps.id, []):
            for ln in self.lines.get(aid, []):
                if ln.status in ("paid", "exported"):
                    paid += Decimal(ln.amount)
                elif ln.status == "pending":
                    pending = True
        return derive(ps, self.details.get(ps.user_id), self.users.get(ps.user_id), chain_paid=paid, chain_pending=pending,
                      own_live=own, now=self.now, alert_days=self.alert_days,
                      previously_failed=bool(self.failed_lines(ps.id)))  # fmt: skip


async def _context(
    session: AsyncSession, run: PayrollRun, *, exclude_batch: uuid.UUID | None = None, lock_share: bool = False
) -> Ctx:
    q = select(Payslip).where(Payslip.run_id == run.id).order_by(Payslip.user_id, Payslip.revision)
    if lock_share:
        q = q.order_by(Payslip.id).with_for_update(read=True)
    rows = list((await session.execute(q)).scalars())
    by_id = {p.id: p for p in rows}
    live = {p.user_id: p for p in rows if p.status == "published"}
    ancestors: dict[uuid.UUID, list[uuid.UUID]] = {}
    for p in rows:
        chain, cur = [], p.supersedes_id
        while cur is not None and cur in by_id and cur not in chain:
            chain.append(cur)
            cur = by_id[cur].supersedes_id
        ancestors[p.id] = chain
    uids = list(live)
    users = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(uids)))).unique().scalars()} if uids else {}
    dq = select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id.in_(uids)) if uids else None
    if dq is not None and lock_share:
        dq = dq.order_by(EmployeePayrollDetails.user_id).with_for_update(read=True)
    details = {d.user_id: d for d in (await session.execute(dq)).scalars()} if dq is not None else {}
    lines: dict[uuid.UUID, list[PayrollPaymentLine]] = {}
    ids = [p.id for p in rows]
    if ids:
        lq = select(PayrollPaymentLine).where(PayrollPaymentLine.payslip_id.in_(ids), PayrollPaymentLine.status.in_(LIVE_LINE))
        if exclude_batch is not None:
            lq = lq.where(PayrollPaymentLine.batch_id != exclude_batch)
        for ln in (await session.execute(lq)).scalars():
            lines.setdefault(ln.payslip_id, []).append(ln)
    failed: dict[uuid.UUID, list[PayrollPaymentLine]] = {}
    if ids:
        fq = select(PayrollPaymentLine).where(PayrollPaymentLine.payslip_id.in_(ids), PayrollPaymentLine.status == "failed")
        for ln in (await session.execute(fq)).scalars():
            failed.setdefault(ln.payslip_id, []).append(ln)
    days = max(int(await pset.current_value(session, SETTING_DAYS)), MIN_ALERT_DAYS)
    return Ctx(run, live, users, details, ancestors, lines, utcnow(), days, failed)


# ============================================================================================ lines
def _line_used(ln: PayrollPaymentLine) -> dict[str, str]:
    b = ln.beneficiary
    return b["current"] if ln.account_source == "current" and b.get("current") else b["snapshot"]


def lines_mac(b: PayrollPaymentBatch, lines: list[PayrollPaymentLine]) -> str:
    pending = sorted((ln for ln in lines if ln.status == "pending"), key=lambda x: x.seq)
    return pc.mac_hex(
        pc.LINES_LABEL,
        {
            "batch": str(b.id), "run": str(b.run_id), "debit": str(b.debit_account_id), "value_date": b.value_date.isoformat(),
            "format": b.format, "type": b.payment_type,
            "lines": [
                {"id": str(ln.id), "seq": ln.seq, "payslip": str(ln.payslip_id), "version": ln.beneficiary.get("payslip_version"),
                 "amount": ln.amount, "source": ln.account_source, "name": ln.beneficiary.get("name"), "to": _line_used(ln),
                 "flags": sorted(ln.flags or [])}
                for ln in pending
            ],
        },
    )  # fmt: skip


def _new_line(batch_id: uuid.UUID, seq: int, d: Derived, actor_id: uuid.UUID) -> PayrollPaymentLine:
    if not Decimal("0.00") < d.amount <= calc.MAX_MONEY or d.amount > Decimal(d.payslip.result["net"]):
        raise _bad("lines", "an amount is outside the allowed range", "AMOUNT_ABOVE_LIMIT")
    return PayrollPaymentLine(
        batch_id=batch_id, payslip_id=d.payslip.id, user_id=d.payslip.user_id, seq=seq, amount=calc.money_str(d.amount),
        beneficiary=d.beneficiary, account_source="snapshot", flags=list(d.flags), status="pending", version=1, updated_by=actor_id,
    )  # fmt: skip


def _stale_reason(ln: PayrollPaymentLine, d: Derived | str, live: Payslip | None) -> str | None:
    """Why a stored line no longer matches the live data (None = still the same). Codes of contract 19.5 `BATCH_STALE`."""
    if live is None or live.id != ln.payslip_id:
        return "PAYSLIP_SUPERSEDED"
    if isinstance(d, str):
        return (
            "HELD"
            if d == "HELD"
            else (
                "BANK_DETAILS_CHANGED"
                if d in ("NO_BANK_DETAILS", "INVALID_ACCOUNT", "INVALID_IFSC", "NAME", "NOT_BANK_TRANSFER")
                else "AMOUNT_CHANGED"
            )
        )
    if calc.money_str(d.amount) != ln.amount:
        return "AMOUNT_CHANGED"
    b, nb = ln.beneficiary, d.beneficiary
    if b.get("snapshot") != nb["snapshot"] or b.get("current") != nb["current"] or b.get("changed_at") != nb["changed_at"]:
        return "BANK_DETAILS_CHANGED"
    if {f for f in ln.flags or [] if f != "RECENT_BANK_CHANGE"} != {f for f in d.flags if f != "RECENT_BANK_CHANGE"}:
        return "AMOUNT_CHANGED"
    if ln.account_source == "current" and not (
        nb["current"] and pf.ACCOUNT_RE.match(nb["current"]["account"]) and pf.IFSC_RE.match(nb["current"]["ifsc"])
    ):
        return "BANK_DETAILS_CHANGED"
    return None


def _stale_details(items: list[tuple[PayrollPaymentLine, str]]) -> AppError:
    return AppError(
        "BATCH_STALE", "Payslips or bank details changed since the lines were prepared; withdraw, refresh and submit again",
        details=[{"field": f"lines[{ln.seq}]", "message": reason} for ln, reason in items],
    )  # fmt: skip


def _guard_failer(ctx: Ctx, actor: Principal, lines: list[PayrollPaymentLine]) -> None:
    """Whoever confirmed an earlier payment of a payslip (or of an earlier copy) failed cannot prepare, submit or approve the re-batch."""
    for ln in lines:
        if ln.status != "excluded" and actor.id in ctx.failers(ln.payslip_id):
            raise AppError("SECOND_PERSON_REQUIRED", "You marked an earlier payment of an employee in this batch as failed")


# ============================================================================================ access / output
async def _batch_or_404(session: AsyncSession, batch_id: uuid.UUID, *, lock: bool = False) -> PayrollPaymentBatch:
    q = select(PayrollPaymentBatch).where(PayrollPaymentBatch.id == batch_id)
    if lock:
        q = q.with_for_update()
    b = (await session.execute(q)).scalar_one_or_none()
    if b is None:
        raise AppError("NOT_FOUND", "Payment batch not found")
    return b


async def _lines_of(session: AsyncSession, batch_id: uuid.UUID, *, lock: bool = False) -> list[PayrollPaymentLine]:
    q = select(PayrollPaymentLine).where(PayrollPaymentLine.batch_id == batch_id).order_by(PayrollPaymentLine.seq)
    if lock:
        q = q.order_by(PayrollPaymentLine.id).with_for_update()
    return sorted((await session.execute(q)).scalars(), key=lambda x: x.seq)


async def _lock_run(session: AsyncSession, run_id: uuid.UUID, *, share: bool = True) -> PayrollRun:
    q = select(PayrollRun).where(PayrollRun.id == run_id)
    run = (await session.execute(q.with_for_update(read=share))).scalar_one_or_none()
    if run is None:
        raise AppError("NOT_FOUND", "Payroll run not found")
    return run


def _masked(a: dict[str, str] | None) -> dict[str, str] | None:
    return None if a is None else {"account": pc.mask_tail(a["account"]) or "", "ifsc": a["ifsc"]}


def _line_out(ln: PayrollPaymentLine, people: dict, payslips: dict) -> dict[str, Any]:
    b = ln.beneficiary
    return {
        "id": str(ln.id), "seq": ln.seq, "user": pc.user_out(people.get(ln.user_id)),
        "payslip": {"id": str(ln.payslip_id), "revision": payslips[ln.payslip_id].revision if ln.payslip_id in payslips else None},
        "amount": ln.amount, "beneficiary_name": b.get("name"), **_masked(b["snapshot"]),  # type: ignore[arg-type]
        "account_source": ln.account_source, "current": _masked(b.get("current")), "flags": list(ln.flags or []),
        "status": ln.status, "reference": pc.mask_tail(ln.reference) if ln.reference else None,
        "paid_on": ln.paid_on.isoformat() if ln.paid_on else None, "version": ln.version,
    }  # fmt: skip


async def _blockers(session: AsyncSession, run: PayrollRun, batch: PayrollPaymentBatch) -> list[dict[str, Any]]:
    """Live blockers of a draft batch: published payslips of the run that cannot (or no longer) get a line."""
    ctx = await _context(session, run)
    in_batch = {ln.payslip_id for ln in await _lines_of(session, batch.id) if ln.status == "pending"}
    out = []
    for uid, ps in sorted(ctx.live.items(), key=lambda kv: str(kv[0])):
        if ps.id in in_batch:
            continue
        d = ctx.derive(ps)
        if isinstance(d, str):
            out.append({"user": pc.user_out(ctx.users.get(uid)), "payslip_id": str(ps.id), "reason": d})
    return out


async def _out(
    session: AsyncSession, b: PayrollPaymentBatch, *, detail: bool, blockers: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    run = await session.get(PayrollRun, b.run_id)
    lines = await _lines_of(session, b.id)
    live = [ln for ln in lines if ln.status != "excluded"]
    acct = await session.get(CompanyBankAccount, b.debit_account_id)
    uids = {ln.user_id for ln in lines} | {x for x in (b.created_by, b.submitted_by, b.decided_by, b.exported_by) if x}
    people = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(uids)))).unique().scalars()} if uids else {}
    payslips = (
        {p.id: p for p in (await session.execute(select(Payslip).where(Payslip.id.in_([ln.payslip_id for ln in lines])))).scalars()}
        if lines else {}
    )  # fmt: skip
    total = sum((Decimal(ln.amount) for ln in live), Decimal("0.00"))
    if detail and blockers is None:
        blockers = await _blockers(session, run, b) if b.status == "draft" and run.status == "published" else []
    out = {
        "id": str(b.id), "reference": b.reference, "run": {"id": str(run.id), "period": pc.period_str(run.period)}, "status": b.status,
        "debit_account": {"id": str(acct.id), "label": acct.label, "account": pc.mask_tail(acct.account)} if acct else None,
        "value_date": b.value_date.isoformat(), "format": b.format,
        "counts": {
            "lines": len(live), "excluded": len(lines) - len(live), "exported": sum(ln.status == "exported" for ln in lines),
            "paid": sum(ln.status == "paid" for ln in lines), "failed": sum(ln.status == "failed" for ln in lines),
        },
        "total": calc.money_str(total),
        "file_sha256": b.file_sha256, "exported_at": pc.iso(b.exported_at), "exported_by": pc.user_out(people.get(b.exported_by)),
        "created_by": pc.user_out(people.get(b.created_by)), "created_at": pc.iso(b.created_at),
        "submitted_by": pc.user_out(people.get(b.submitted_by)) if b.status != "draft" else None,
        "submitted_at": pc.iso(b.submitted_at) if b.status != "draft" else None,
        "decided_by": pc.user_out(people.get(b.decided_by)), "decided_at": pc.iso(b.decided_at),
        "decision_comment": b.decision_comment, "version": b.version,
    }  # fmt: skip
    if detail:
        out["lines"] = [_line_out(ln, people, payslips) for ln in lines]
        out["blockers"] = blockers or []
    return out


async def _one(session: AsyncSession, batch_id: uuid.UUID, *, blockers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    b = await _batch_or_404(session, batch_id)
    await session.refresh(b)
    return await _out(session, b, detail=True, blockers=blockers)


async def list_batches(
    session: AsyncSession, actor: Principal, *, run_id: uuid.UUID | None, status: str | None, page: PageParams
) -> dict[str, Any]:
    cond = []
    if run_id:
        cond.append(PayrollPaymentBatch.run_id == run_id)
    if status:
        cond.append(PayrollPaymentBatch.status == status)
    total = (await session.execute(select(func.count()).select_from(PayrollPaymentBatch).where(*cond))).scalar_one()
    q = (
        select(PayrollPaymentBatch).where(*cond).order_by(PayrollPaymentBatch.created_at.desc(), PayrollPaymentBatch.id)
        .offset(page.offset).limit(page.page_size)
    )  # fmt: skip
    rows = list((await session.execute(q)).scalars())
    items = [await _out(session, b, detail=False) for b in rows]
    await pc.audit_read(session, actor, "payroll.payment_batch.list", ENTITY, count=len(items))
    return make_page(items, page, total)


async def get_batch(session: AsyncSession, actor: Principal, batch_id: uuid.UUID) -> dict[str, Any]:
    b = await _batch_or_404(session, batch_id)
    out = await _out(session, b, detail=True)
    audit(session, action="payroll.payment_batch.view", actor=actor, entity_type=ENTITY, entity_id=b.id)
    await pc.commit(session)
    return out


# ============================================================================================ create / edit
async def _today(session: AsyncSession) -> dt.date:
    return await runs._today(session)


def _check_value_date(value: dt.date, today: dt.date) -> None:
    if not today <= value <= today + dt.timedelta(days=MAX_DAYS_AHEAD):
        raise _bad("value_date", "from today up to 30 days ahead", "INVALID_DATE")


def _check_value_date_not_past(value: dt.date, today: dt.date) -> None:
    if value < today:
        raise _bad("value_date", "the value date has passed; cancel this batch and make a new one", "INVALID_DATE")


async def _debit_account(session: AsyncSession, account_id: uuid.UUID | None) -> CompanyBankAccount:
    q = select(CompanyBankAccount).where(CompanyBankAccount.active.is_(True))
    q = q.where(CompanyBankAccount.id == account_id) if account_id else q.where(CompanyBankAccount.is_default.is_(True))
    a = (await session.execute(q.with_for_update(read=True))).scalars().first()
    if a is None:
        raise _bad(
            "debit_account_id", "choose an active company bank account" if account_id else "no default company bank account"
        )
    return a


async def _next_reference(session: AsyncSession, run: PayrollRun) -> str:
    prefix = f"PAY-{run.period.year:04d}-{run.period.month:02d}-"
    n = (
        await session.execute(
            select(func.count()).select_from(PayrollPaymentBatch).where(PayrollPaymentBatch.reference.like(prefix + "%"))
        )
    ).scalar_one()
    return f"{prefix}{n + 1:02d}"


async def create_batch(session: AsyncSession, actor: Principal, run_id: uuid.UUID, body: PaymentBatchCreate) -> dict[str, Any]:
    _check_value_date(body.value_date, await _today(session))
    run = await _lock_run(session, run_id)
    if run.status != "published":
        raise AppError("PAYROLL_LOCKED", "Payment batches are only created for a published run")
    acct = await _debit_account(session, body.debit_account_id)
    ctx = await _context(session, run)
    scope = set(body.user_ids) if body.user_ids is not None else None
    if scope is not None and not scope <= set(ctx.live):
        raise _bad("user_ids", "every employee needs a published payslip in this run")
    derived: list[Derived] = []
    blockers: list[dict[str, Any]] = []
    for uid, ps in sorted(
        ctx.live.items(), key=lambda kv: (ctx.users[kv[0]].employee_id if kv[0] in ctx.users else "", str(kv[0]))
    ):
        if scope is not None and uid not in scope:
            continue
        d = ctx.derive(ps)
        if not isinstance(d, str) and actor.id in ctx.failers(ps.id):
            d = "FAILED_BY_YOU"  # a different person must prepare the re-batch of a payment this caller marked failed
        if isinstance(d, str):
            blockers.append({"user": pc.user_out(ctx.users.get(uid)), "payslip_id": str(ps.id), "reason": d})
        else:
            if d.amount > calc.MAX_MONEY:
                raise _bad("lines", "an amount is above the allowed maximum", "AMOUNT_ABOVE_LIMIT")
            derived.append(d)
    if not derived:
        raise AppError("NOTHING_TO_PAY", "No payslip can be paid in a new batch",
                       details=[{"field": b["user"]["employee_id"] if b["user"] else "", "message": b["reason"]} for b in blockers[:200]])  # fmt: skip
    if len(derived) > MAX_LINES:
        raise AppError(
            "TOO_MANY_ROWS",
            f"A batch holds at most {MAX_LINES} lines",
            details=[{"field": "user_ids", "message": "select fewer employees"}],
        )
    fmt, ptype = str(await pset.current_value(session, SETTING_FORMAT)), str(await pset.current_value(session, SETTING_TYPE))
    batch = None
    for _ in range(5):  # the reference number is unique: a concurrent creator takes the next free one
        batch = PayrollPaymentBatch(
            id=uuid.uuid4(), run_id=run.id, reference=await _next_reference(session, run), debit_account_id=acct.id,
            value_date=body.value_date, format=fmt, payment_type=ptype, status="draft", created_by=actor.id,
            last_edited_by=actor.id, contributors=[str(actor.id)], version=1, updated_by=actor.id,
        )  # fmt: skip
        try:
            async with session.begin_nested():
                session.add(batch)
                await session.flush()
            break
        except IntegrityError as exc:
            if pc.sqlstate(exc) != "23505":
                await session.rollback()
                raise _map_db(exc) from None
            batch = None  # the savepoint was rolled back and the object expunged
    if batch is None:
        raise AppError("CONFLICT", "Could not allocate a batch reference; retry")
    lines = [_new_line(batch.id, i, d, actor.id) for i, d in enumerate(derived, start=1)]
    session.add_all(lines)
    await _flush(session)
    _event(session, batch, "create", actor.id, run.period)
    _audit(session, actor, "payroll.payment_batch.create", batch, run, lines)
    await _commit(session)
    return await _one(session, batch.id, blockers=blockers)


async def _draft_for_edit(
    session: AsyncSession, actor: Principal, batch_id: uuid.UUID, version: int | None
) -> tuple[PayrollRun, PayrollPaymentBatch]:
    peek = await _batch_or_404(session, batch_id)
    run = await _lock_run(session, peek.run_id)
    b = await _batch_or_404(session, batch_id, lock=True)
    return run, b


def _guard_maker(actor: Principal, b: PayrollPaymentBatch) -> None:
    """The person who last decided the batch (reject keeps `decided_by`) can never become a maker of it (database CHECK)."""
    if b.decided_by is not None and b.decided_by == actor.id:
        raise AppError("SECOND_PERSON_REQUIRED", "The person who decided this batch cannot edit or submit it")


def _touch(b: PayrollPaymentBatch, actor_id: uuid.UUID) -> None:
    pc.touch(b, actor_id)


async def _refresh(
    session: AsyncSession, actor: Principal, b: PayrollPaymentBatch, lines: list[PayrollPaymentLine], ctx: Ctx
) -> tuple[list[PayrollPaymentLine], int]:
    """Stale pending lines (payslip revised, amount or bank details changed) are excluded and, when the live payslip can still be
    paid, replaced by a NEW line with a new sequence number (a line is never rewritten). Returns (new lines, refreshed count)."""
    new: list[PayrollPaymentLine] = []
    refreshed = 0
    seq = max((ln.seq for ln in lines), default=0)
    ex_ctx = ctx
    for ln in [x for x in lines if x.status == "pending"]:
        live = ex_ctx.live.get(ln.user_id)
        d = ex_ctx.derive(live) if live is not None else "HELD"
        if _stale_reason(ln, d, live) is None:
            continue
        ln.status, ln.version, ln.updated_by = "excluded", ln.version + 1, actor.id
        refreshed += 1
        await _flush(session)  # free the unique slot before the replacement is inserted
        if isinstance(d, Derived):
            seq += 1
            nl = _new_line(b.id, seq, d, actor.id)
            session.add(nl)
            new.append(nl)
    if new:
        await _flush(session)
    return new, refreshed


async def update_batch(session: AsyncSession, actor: Principal, batch_id: uuid.UUID, body: PaymentBatchUpdate) -> dict[str, Any]:
    run, b = await _draft_for_edit(session, actor, batch_id, body.expected_version)
    if b.status != "draft":
        raise AppError("NOT_DRAFT", "Only a draft batch can be edited")
    if b.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The batch was changed meanwhile; reload it")
    _guard_maker(actor, b)
    if run.status != "published":
        raise AppError("PAYROLL_LOCKED", "The run is not published")
    sent = body.model_fields_set - {"expected_version"}
    if body.value_date is not None:
        _check_value_date(body.value_date, await _today(session))
        b.value_date = body.value_date
    if body.debit_account_id is not None and body.debit_account_id != b.debit_account_id:
        b.debit_account_id = (await _debit_account(session, body.debit_account_id)).id
    lines = await _lines_of(session, b.id, lock=True)
    by_id = {ln.id: ln for ln in lines}
    for field, ids in (("exclude_line_ids", body.exclude_line_ids or []), ("include_line_ids", body.include_line_ids or [])):
        for i, lid in enumerate(ids):
            if lid not in by_id:
                raise _bad(f"{field}[{i}]", "not a line of this batch")
    ctx = await _context(session, run, exclude_batch=b.id, lock_share=True)
    for lid in body.exclude_line_ids or []:
        ln = by_id[lid]
        if ln.status == "pending":
            ln.status, ln.version, ln.updated_by = "excluded", ln.version + 1, actor.id
    await _flush(session)
    added: list[PayrollPaymentLine] = []
    seq = max((ln.seq for ln in lines), default=0)
    for i, lid in enumerate(body.include_line_ids or []):
        ln = by_id[lid]
        if ln.status != "excluded":
            continue
        live = ctx.live.get(ln.user_id)
        d = ctx.derive(live) if live is not None else "HELD"
        if isinstance(d, str):
            raise _bad(f"include_line_ids[{i}]", d, "VALIDATION_ERROR")
        if _stale_reason(ln, d, live) is None:  # unchanged since it was prepared: the same line is switched back on
            ln.status, ln.version, ln.updated_by = "pending", ln.version + 1, actor.id
        else:  # the data moved on: a NEW line with a new sequence number
            seq += 1
            nl = _new_line(b.id, seq, d, actor.id)
            session.add(nl)
            added.append(nl)
        await _flush(session)
    lines = lines + added
    for i, item in enumerate(body.account_sources or []):
        ln = by_id.get(item.line_id) or next((x for x in added if x.id == item.line_id), None)
        if ln is None:
            raise _bad(f"account_sources[{i}].line_id", "not a line of this batch")
        cur = (ln.beneficiary or {}).get("current")
        if item.source == "current":
            if "BANK_DETAILS_CHANGED" not in (ln.flags or []):
                raise _bad(f"account_sources[{i}].source", "only a line with changed bank details can use the current account")
            if not cur or not pf.ACCOUNT_RE.match(cur.get("account", "")) or not pf.IFSC_RE.match(cur.get("ifsc", "")):
                raise _bad(f"account_sources[{i}].source", "the current bank details are not usable")
        ln.account_source, ln.version, ln.updated_by = item.source, ln.version + 1, actor.id
    await _flush(session)
    fresh, refreshed = await _refresh(session, actor, b, lines, ctx)
    lines = lines + fresh
    _guard_failer(ctx, actor, lines)
    b.last_edited_by, b.version, b.updated_by = actor.id, b.version + 1, actor.id
    _touch(b, actor.id)
    await _flush(session)
    _event(session, b, "update", actor.id, run.period)
    _audit(session, actor, "payroll.payment_batch.update", b, run, lines, fields=sorted(sent), refreshed=refreshed)
    await _commit(session)
    return await _one(session, batch_id)


# ============================================================================================ submit / withdraw
async def _push_approvers(
    session: AsyncSession, redis: Redis, exclude: set[uuid.UUID], maker: str, ref: str, bid: uuid.UUID
) -> None:
    try:
        q = (
            select(User.id).join(Role, Role.id == User.role_id)
            .where(User.status == "active", User.deleted_at.is_(None), Role.permissions.any(Permission.code == "payroll.approve"))
            .order_by(User.id)
        )  # fmt: skip
        ids = [u for u in (await session.execute(q)).unique().scalars() if u not in exclude][:MAX_PUSH_TARGETS]
        if not ids:
            return
        if not await redis.set("ctr:payroll_approval:payment_batch", "1", nx=True, ex=PUSH_COALESCE_S):
            return
        await notifications.notify(
            session, ids, type="payroll_approval", entity_id=bid, title="Approval needed",
            body=f"{maker} submitted payment batch {ref}", extra={"kind": "payment_batch"},
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.exception("payment batch push failed (ignored)")


async def _push_maker(session: AsyncSession, maker_id: uuid.UUID, bid: uuid.UUID, title: str, ref: str) -> None:
    try:
        await notifications.notify(
            session, [maker_id], type="payroll_approval", entity_id=bid, title=title, body=ref, extra={"kind": "payment_batch"}
        )  # fmt: skip
    except Exception:  # noqa: BLE001
        log.exception("payment batch push failed (ignored)")


async def submit_batch(
    session: AsyncSession, redis: Redis, actor: Principal, batch_id: uuid.UUID, body: VersionIn
) -> dict[str, Any]:
    run, b = await _draft_for_edit(session, actor, batch_id, body.expected_version)
    if b.status != "draft":
        raise AppError("NOT_DRAFT", "Only a draft batch can be submitted")
    if b.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The batch was changed meanwhile; reload it")
    _guard_maker(actor, b)
    if run.status != "published":
        raise AppError("PAYROLL_LOCKED", "The run is not published")
    lines = await _lines_of(session, b.id, lock=True)
    pending = [ln for ln in lines if ln.status == "pending"]
    if not pending:
        raise AppError(
            "NOTHING_TO_PAY", "The batch has no line to pay", details=[{"field": "lines", "message": "no pending line"}]
        )
    ctx = await _context(session, run, exclude_batch=b.id, lock_share=True)
    stale = [
        (ln, r)
        for ln in pending
        if (
            r := _stale_reason(
                ln, ctx.derive(ctx.live[ln.user_id]) if ln.user_id in ctx.live else "HELD", ctx.live.get(ln.user_id)
            )
        )
    ]
    if stale:
        raise _stale_details(stale)
    _guard_failer(ctx, actor, pending)
    _check_value_date(b.value_date, await _today(session))
    now = utcnow()
    b.status, b.submitted_by, b.submitted_at = "submitted", actor.id, now
    b.decided_by = b.decided_at = b.decision_comment = None  # a new round: the old decision is history
    b.lines_mac = lines_mac(b, lines)
    b.version, b.updated_by = b.version + 1, actor.id
    _touch(b, actor.id)
    await _flush(session)
    _event(session, b, "submit", actor.id, run.period)
    _audit(session, actor, "payroll.payment_batch.submit", b, run, lines)
    exclude = {actor.id, *(uuid.UUID(c) for c in b.contributors or [])}
    name, ref, bid = actor.user.full_name, b.reference, b.id

    async def _push() -> None:
        await _push_approvers(session, redis, exclude, name, ref, bid)

    pc.after_commit(session, _push)
    await _commit(session)
    return await _one(session, batch_id)


async def withdraw_batch(session: AsyncSession, actor: Principal, batch_id: uuid.UUID, body: VersionIn) -> dict[str, Any]:
    run, b = await _draft_for_edit(session, actor, batch_id, body.expected_version)
    if b.status != "submitted":
        raise AppError("NOT_SUBMITTED", "Only a submitted batch can be withdrawn")
    if b.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The batch was changed meanwhile; reload it")
    _guard_maker(actor, b)
    lines = await _lines_of(session, b.id, lock=True)
    b.status, b.lines_mac = "draft", None  # `submitted_by` / `contributors` keep the submitter
    b.version, b.updated_by = b.version + 1, actor.id
    _touch(b, actor.id)
    await _flush(session)
    _event(session, b, "withdraw", actor.id, run.period)
    _audit(session, actor, "payroll.payment_batch.withdraw", b, run, lines)
    await _commit(session)
    return await _one(session, batch_id)


# ============================================================================================ checker actions
def _is_party(actor_id: uuid.UUID, b: PayrollPaymentBatch) -> bool:
    return actor_id in (b.created_by, b.last_edited_by, b.submitted_by) or str(actor_id) in (b.contributors or [])


async def _gate(
    session: AsyncSession, actor: Principal, b: PayrollPaymentBatch, lines: list[PayrollPaymentLine], *, paid_check: bool
) -> None:
    """403 FORBIDDEN (whole batch) when a live line pays the checker; 403 SECOND_PERSON_REQUIRED for a party or for somebody who changed
    the bank details of a line employee recently."""
    live = [ln for ln in lines if ln.status != "excluded"]
    if paid_check and any(ln.user_id == actor.id for ln in live):
        raise await pc.deny(session, actor, entity_type=ENTITY, entity_id=b.id, reason="self")
    if _is_party(actor.id, b):
        raise AppError("SECOND_PERSON_REQUIRED", "A different person must decide a batch you prepared or submitted")
    if paid_check and live:
        days = max(int(await pset.current_value(session, SETTING_DAYS)), MIN_ALERT_DAYS)
        since = utcnow() - dt.timedelta(days=days)
        q = select(EmployeeBankChange.changed_at).where(
            EmployeeBankChange.user_id.in_([ln.user_id for ln in live]), EmployeeBankChange.changed_by == actor.id
        )  # the whole append-only history: an earlier change counts even after somebody else changed the details again
        if any(as_utc(at) > since for at in (await session.execute(q)).scalars()):
            raise AppError("SECOND_PERSON_REQUIRED", "You changed the bank details of an employee in this batch recently")


async def reject_batch(session: AsyncSession, actor: Principal, batch_id: uuid.UUID, body: PaymentBatchReject) -> dict[str, Any]:
    run, b = await _draft_for_edit(session, actor, batch_id, body.expected_version)
    lines = await _lines_of(session, b.id, lock=True)
    await _gate(session, actor, b, lines, paid_check=False)
    if b.status != "submitted":
        raise AppError("NOT_SUBMITTED", "The batch is not waiting for a decision")
    if b.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The batch was changed meanwhile; reload it")
    b.status, b.lines_mac = "draft", None
    b.decided_by, b.decided_at, b.decision_comment = actor.id, utcnow(), body.comment
    b.version, b.updated_by = b.version + 1, actor.id
    await _flush(session)
    _event(session, b, "reject", actor.id, run.period)
    _audit(session, actor, "payroll.payment_batch.reject", b, run, lines)
    maker, ref, bid = b.submitted_by or b.created_by, b.reference, b.id

    async def _push() -> None:
        await _push_maker(session, maker, bid, "Payment batch rejected", ref)

    pc.after_commit(session, _push)
    await _commit(session)
    return await _one(session, batch_id)


async def _recheck_payable(session: AsyncSession, b: PayrollPaymentBatch) -> CompanyBankAccount | None:
    """Approve and the first download: the value date must still be today or later and the debit account still active."""
    _check_value_date_not_past(b.value_date, await _today(session))
    acct = await session.get(CompanyBankAccount, b.debit_account_id)
    if acct is None or not acct.active:
        raise _bad("debit_account_id", "the debit account is no longer active; choose another and submit again")
    return acct


async def approve_batch(
    session: AsyncSession, redis: Redis, actor: Principal, batch_id: uuid.UUID, body: PaymentBatchApprove
) -> dict[str, Any]:
    await _batch_or_404(session, batch_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.payment_batch.approve")
    run, b = await _draft_for_edit(session, actor, batch_id, body.expected_version)
    lines = await _lines_of(session, b.id, lock=True)
    await _gate(session, actor, b, lines, paid_check=True)
    if b.status != "submitted":
        raise AppError("NOT_SUBMITTED", "The batch is not waiting for a decision")
    if b.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The batch was changed meanwhile; reload it")
    if run.status != "published":
        raise AppError("PAYROLL_LOCKED", "The run is not published")
    await _recheck_payable(session, b)
    pending = [ln for ln in lines if ln.status == "pending"]
    ctx = await _context(session, run, exclude_batch=b.id, lock_share=True)  # payslips and details share-locked
    _guard_failer(ctx, actor, pending)
    stale = [
        (ln, r)
        for ln in pending
        if (
            r := _stale_reason(
                ln, ctx.derive(ctx.live[ln.user_id]) if ln.user_id in ctx.live else "HELD", ctx.live.get(ln.user_id)
            )
        )
    ]
    if stale:
        raise _stale_details(stale)
    if not pending or b.lines_mac != lines_mac(b, lines):
        raise AppError("BATCH_STALE", "The lines of this batch do not match what was submitted",
                       details=[{"field": "lines", "message": "AMOUNT_CHANGED"}])  # fmt: skip
    b.status, b.decided_by, b.decided_at, b.decision_comment = "approved", actor.id, utcnow(), body.comment
    b.version, b.updated_by = b.version + 1, actor.id
    await _flush(session)
    _event(session, b, "approve", actor.id, run.period)
    _audit(session, actor, "payroll.payment_batch.approve", b, run, lines)
    maker, ref, bid = b.submitted_by or b.created_by, b.reference, b.id

    async def _push() -> None:
        await _push_maker(session, maker, bid, "Payment batch approved", ref)

    pc.after_commit(session, _push)
    await _commit(session)
    return await _one(session, batch_id)


async def cancel_batch(session: AsyncSession, actor: Principal, batch_id: uuid.UUID, reason: str) -> dict[str, Any]:
    run, b = await _draft_for_edit(session, actor, batch_id, None)
    lines = await _lines_of(session, b.id, lock=True)
    if b.status not in ("draft", "submitted", "approved"):
        raise AppError("NOT_DRAFT", "Only a batch that was not exported can be cancelled")
    b.status, b.cancel_reason = "cancelled", reason
    b.version, b.updated_by = b.version + 1, actor.id
    if b.decided_by != actor.id:  # a checker who cancels must not become a contributor (database CHECK)
        _touch(b, actor.id)
    await _flush(session)  # the batch first: the line trigger allows pending -> excluded only under a cancelled batch
    for ln in lines:
        if ln.status == "pending":
            ln.status, ln.version, ln.updated_by = "excluded", ln.version + 1, actor.id
    await _flush(session)
    _event(session, b, "cancel", actor.id, run.period)
    _audit(session, actor, "payroll.payment_batch.cancel", b, run, lines)
    await _commit(session)
    return await _one(session, batch_id)


# ============================================================================================ file
def _file_lines(lines: list[PayrollPaymentLine], statuses: tuple[str, ...]) -> list[pf.FileLine]:
    out = []
    for ln in lines:
        if ln.status not in statuses:
            continue
        a = _line_used(ln)
        out.append(pf.FileLine(ln.seq, ln.beneficiary.get("name") or "", a["account"], a["ifsc"], ln.amount,
                               bool(ln.beneficiary.get("delta"))))  # fmt: skip
    return out


async def download_file(
    session: AsyncSession, redis: Redis, actor: Principal, batch_id: uuid.UUID, body: PaymentFileIn
) -> tuple[bytes, str]:
    await _batch_or_404(session, batch_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.payment_batch.download")
    run, b = await _draft_for_edit(session, actor, batch_id, None)
    lines = await _lines_of(session, b.id, lock=True)
    acct = await session.get(CompanyBankAccount, b.debit_account_id)
    if b.status == "closed":
        raise AppError("BATCH_CLOSED", "The batch is closed; its file can no longer be downloaded")
    if b.status not in ("approved", "exported"):
        raise AppError("NOT_APPROVED", "The batch must be approved before the file can be downloaded")
    first = b.status == "approved"
    if first:
        acct = await _recheck_payable(session, b)
    try:
        if first:
            if b.lines_mac != lines_mac(b, lines):
                raise AppError("BATCH_STALE", "The lines of this batch do not match what was approved",
                               details=[{"field": "lines", "message": "AMOUNT_CHANGED"}])  # fmt: skip
            fl = _file_lines(lines, ("pending",))
        else:
            fl = _file_lines(lines, ("exported", "paid", "failed"))
        data = pf.render(reference=b.reference, payment_type=b.payment_type, debit_account=acct.account if acct else "",
                         value_date=b.value_date, period=run.period, lines=fl)  # fmt: skip
    except ValueError:
        raise AppError("CONFLICT", "The file cannot be built: a stored line is not bank-safe; ask for a new batch") from None
    digest = pf.sha256_hex(data)
    count = len(fl)
    if first:
        now = utcnow()
        b.status, b.exported_by, b.exported_at, b.file_sha256 = "exported", actor.id, now, digest
        b.version, b.updated_by = b.version + 1, actor.id
        await _flush(
            session
        )  # the batch first: the line trigger allows pending -> exported only under an approved / exported batch
        for ln in lines:
            if ln.status == "pending":
                ln.status, ln.version, ln.updated_by = "exported", ln.version + 1, actor.id
        await _flush(session)
        _event(session, b, "export", actor.id, run.period)
        _audit(session, actor, "payroll.payment_batch.export", b, run, lines, file_sha256=digest)
    else:
        if digest != b.file_sha256:
            audit(session, action="payroll.payment_batch.redownload", actor=actor, entity_type=ENTITY, entity_id=b.id,
                  outcome="failure", after={"reference": b.reference, "reason": "file_changed"})  # fmt: skip
            await pc.commit(session)
            raise AppError("CONFLICT", "The stored lines no longer reproduce the exported file; contact an administrator")
        _audit(session, actor, "payroll.payment_batch.redownload", b, run, lines, file_sha256=digest)
    ref, bid = b.reference, b.id
    title = "Bank file downloaded" if first else "Bank file downloaded again"

    async def _alert() -> None:
        await admin_security.notify_admins(session, entity_id=bid, title=title, body=f"{ref}: {count} lines")

    pc.after_commit(session, _alert)
    await _commit(session)
    return data, f"bank-{ref}.csv"


# ============================================================================================ confirm
async def confirm_batch(
    session: AsyncSession, redis: Redis, actor: Principal, batch_id: uuid.UUID, body: PaymentConfirm
) -> dict[str, Any]:
    await _batch_or_404(session, batch_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.payment_batch.confirm")
    run, b = await _draft_for_edit(session, actor, batch_id, None)
    lines = await _lines_of(session, b.id, lock=True)
    if b.status != "exported":
        raise AppError("NOT_APPROVED", "Only an exported batch can be confirmed")
    today = await _today(session)
    tz = runs.att.office_tz(await runs.att.load_settings(session))
    exported_day = as_utc(b.exported_at).astimezone(tz).date() if b.exported_at else today
    if body.paid_on > today:
        raise _bad("paid_on", "must not be in the future", "INVALID_DATE")
    if body.paid_on < exported_day:
        raise _bad("paid_on", "must not be before the day the file was downloaded", "INVALID_DATE")
    by_id = {ln.id: ln for ln in lines}
    chosen: dict[uuid.UUID, tuple[str, str | None]] = {}
    for i, item in enumerate(body.lines or []):
        ln = by_id.get(item.line_id)
        if ln is None:
            raise _bad(f"lines[{i}].line_id", "not a line of this batch")
        if ln.status != "exported" or item.line_id in chosen:
            raise AppError(
                "CONFLICT",
                "A line is not waiting for confirmation",
                details=[{"field": f"lines[{i}].line_id", "message": "not exported"}],
            )
        chosen[item.line_id] = (item.status, item.reference or body.reference)
    if body.all_paid:
        for ln in lines:
            if ln.status == "exported" and ln.id not in chosen:
                chosen[ln.id] = ("paid", body.reference)
    if not chosen:
        raise AppError("CONFLICT", "No line is waiting for confirmation")
    for (
        lid
    ) in chosen:  # nobody confirms (least of all fails) a payment to themselves: a failed line frees the slot to be paid again
        await pc.forbid_self(session, actor, by_id[lid].user_id, entity_type=ENTITY, entity_id=b.id)
    paid = failed = 0
    for lid, (status, ref) in chosen.items():
        ln = by_id[lid]
        ln.status, ln.reference, ln.version, ln.updated_by = status, ref, ln.version + 1, actor.id
        ln.paid_on = body.paid_on if status == "paid" else None
        ln.failed_by = actor.id if status == "failed" else None
        paid += status == "paid"
        failed += status == "failed"
    await _flush(session)  # lines first: they are final under an exported batch
    if not any(ln.status == "exported" for ln in lines):
        b.status = "closed"
    b.version, b.updated_by = b.version + 1, actor.id
    await _flush(session)
    _event(session, b, "confirm", actor.id, run.period, paid=paid, failed=failed)
    _audit(session, actor, "payroll.payment_batch.confirm", b, run, lines, paid=paid, failed=failed)
    await _commit(session)
    return await _one(session, batch_id)


# ============================================================================================ dashboard
async def dashboard_alerts(session: AsyncSession, run: PayrollRun | None, today: dt.date) -> list[str]:
    """PAYMENT_NOT_EXPORTED (published run, pay date passed, a payable payslip has no exported / paid line), PAYMENT_FAILED_LINES and
    PAYMENT_UNDERPAID (an earlier copy's exported line failed after a revision delta was paid: the employee got less than the net)."""
    if run is None or run.status != "published":
        return []
    alerts: list[str] = []
    failed = (
        await session.execute(
            select(PayrollPaymentLine.id).join(PayrollPaymentBatch, PayrollPaymentBatch.id == PayrollPaymentLine.batch_id)
            .where(PayrollPaymentBatch.run_id == run.id, PayrollPaymentLine.status == "failed").limit(1)
        )
    ).first()  # fmt: skip
    if failed:
        alerts.append("PAYMENT_FAILED_LINES")
    ctx = await _context(session, run)
    if any(ctx.derive(ps) == "UNDERPAID_CHAIN" for ps in ctx.live.values()):
        alerts.append("PAYMENT_UNDERPAID")
    if run.pay_date is not None and today >= run.pay_date:
        for ps in ctx.live.values():
            if any(ln.status in ("exported", "paid") for ln in ctx.lines.get(ps.id, [])):
                continue
            d = ctx.derive(ps)
            if isinstance(d, Derived) or d == "IN_OTHER_BATCH":  # something is still to be paid and no exported file covers it
                alerts.append("PAYMENT_NOT_EXPORTED")
                break
    return alerts
