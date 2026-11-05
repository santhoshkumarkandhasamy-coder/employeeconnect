"""One-time pay items (bonus, incentive, other earning / deduction) and the bulk upload (plan 15 s.3, contract 17.2).

Lock order (plan 15 P7): the run row of the target month(s) first, then the item rows. A new item only locks the run row.
Amounts, labels and reasons are encrypted columns; the audit log carries ids, periods, codes and counts only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..core.text import clean_text
from ..models import PayrollImportBatch, PayrollOneTimeItem, PayrollRun, User
from ..schemas.payroll import OneTimeItemCreate, OneTimeItemUpdate
from . import admin_security, xlsx, xlsx_import
from . import attendance as att
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_components as comps
from . import payroll_runs as runs

CODES = ("BONUS", "INCENTIVE", "OTHER_EARNING", "OTHER_DEDUCTION")
_EID = re.compile(r"^[A-Z0-9._-]{1,32}$")
_ENTITY = "payroll_item"


def _bad(field: str, message: str) -> AppError:
    return AppError("VALIDATION_ERROR", message, details=[{"field": field, "message": message}])


# ============================================================================================ output
async def _people(session: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, User]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).unique().scalars()}


def item_out(i: PayrollOneTimeItem, people: dict, state: tuple[str, list[str]]) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "user": pc.user_out(people.get(i.user_id)),
        "period": pc.period_str(i.period),
        "code": i.code,
        "label": i.label,
        "amount": calc.money_str(Decimal(i.amount)),
        "status": i.status,
        "run_status": state[0],
        "flags": state[1],
        "batch_id": str(i.batch_id) if i.batch_id else None,
        "payslip_id": str(i.payslip_id) if i.payslip_id else None,
        "version": i.version,
        "created_at": pc.iso(i.created_at),
        "created_by": pc.user_out(people.get(i.created_by)),
        "updated_at": pc.iso(i.updated_at),
    }


async def _out_many(session: AsyncSession, items: list[PayrollOneTimeItem]) -> list[dict[str, Any]]:
    people = await _people(session, {x for i in items for x in (i.user_id, i.created_by)})
    states = await comps.row_states(session, items)
    return [item_out(i, people, states[i.id]) for i in items]


# ============================================================================================ checks
async def _eligible_user(session: AsyncSession, user_id: uuid.UUID, period: dt.date) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError("NOT_FOUND", "Employee not found")
    if user.deleted_at is not None:
        raise _bad("user_id", "the employee was deleted")
    det = (await runs._load_details(session, [user_id])).get(user_id)
    tz = att.office_tz(await att.load_settings(session))
    if not runs._eligible(user, det, period, tz):
        raise _bad("user_id", "the employee is not eligible for payroll in that month")
    return user


async def _lock_runs(session: AsyncSession, periods: list[dt.date]) -> None:
    """Run rows FOR UPDATE in period order: always the first lock of a pay-item write (plan 15 P7)."""
    ps = sorted(set(periods))
    if ps:
        await session.execute(
            select(PayrollRun.id).where(PayrollRun.period.in_(ps)).order_by(PayrollRun.period).with_for_update()
        )


async def _item_or_404(session: AsyncSession, item_id: uuid.UUID, *, lock: bool = False) -> PayrollOneTimeItem:
    q = select(PayrollOneTimeItem).where(PayrollOneTimeItem.id == item_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    item = (await session.execute(q)).scalar_one_or_none()
    if item is None:
        raise AppError("NOT_FOUND", "Item not found")
    return item


def _require_pending(item: PayrollOneTimeItem) -> None:
    if item.status != "pending":
        raise AppError("ITEM_LOCKED", f"The item is {item.status} and can no longer be changed")


# ============================================================================================ list / create / update / cancel
async def list_items(
    session: AsyncSession,
    *,
    period: str | None,
    user_id: uuid.UUID | None,
    code: str | None,
    status: str | None,
    batch_id: uuid.UUID | None,
    page: PageParams,
) -> dict[str, Any]:
    today = await runs._today(session)
    cond = []
    if period:
        cond.append(PayrollOneTimeItem.period == comps.parse_target_period(period, today))
    if user_id:
        cond.append(PayrollOneTimeItem.user_id == user_id)
    if code:
        cond.append(PayrollOneTimeItem.code == code)
    if status:
        cond.append(PayrollOneTimeItem.status == status)
    if batch_id:
        cond.append(PayrollOneTimeItem.batch_id == batch_id)
    total = (await session.execute(select(func.count()).select_from(PayrollOneTimeItem).where(*cond))).scalar_one()
    q = (
        select(PayrollOneTimeItem).where(*cond)
        .order_by(PayrollOneTimeItem.period.desc(), PayrollOneTimeItem.created_at.desc(), PayrollOneTimeItem.id)
        .offset(page.offset).limit(page.page_size)
    )  # fmt: skip
    rows = list((await session.execute(q)).scalars())
    return make_page(await _out_many(session, rows), page, total)


async def create_item(session: AsyncSession, actor: Principal, body: OneTimeItemCreate) -> dict[str, Any]:
    today = await runs._today(session)
    period = comps.parse_target_period(body.period, today)
    await pc.forbid_self(session, actor, body.user_id, entity_type=_ENTITY, entity_id=body.user_id)
    user = await _eligible_user(session, body.user_id, period)
    await comps.check_target_period(session, [user.id], period, lock=True)
    comps.check_caps((await comps.live_counts(session, [user.id], period))[user.id])
    item = PayrollOneTimeItem(
        user_id=user.id, period=period, code=body.code, label=body.label, amount=calc.money_str(body.amount),
        status="pending", version=1, created_by=actor.id, updated_by=actor.id,
    )  # fmt: skip
    session.add(item)
    await pc.flush(session)
    comps.add_event(session, item_type="one_time", item=item, action="create", actor_id=actor.id)
    audit(session, action="payroll.item.create", actor=actor, entity_type=_ENTITY, entity_id=item.id,
          after={"user_id": str(user.id), "period": pc.period_str(period), "code": body.code})  # fmt: skip
    await pc.commit(session)
    return (await _out_many(session, [await _item_or_404(session, item.id)]))[0]


async def update_item(session: AsyncSession, actor: Principal, item_id: uuid.UUID, body: OneTimeItemUpdate) -> dict[str, Any]:
    today = await runs._today(session)
    peek = await _item_or_404(session, item_id)
    await pc.forbid_self(session, actor, peek.user_id, entity_type=_ENTITY, entity_id=item_id)
    sent = body.model_fields_set - {"expected_version"}
    new_period = comps.parse_target_period(body.period, today) if "period" in sent else peek.period
    await _lock_runs(session, [peek.period, new_period])
    item = await _item_or_404(session, item_id, lock=True)
    _require_pending(item)
    if item.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The item was changed by someone else; reload it")
    prev_period, prev_amount = item.period, item.amount
    changed: list[str] = []
    if "period" in sent and new_period != item.period:
        user = await _eligible_user(session, item.user_id, new_period)
        await comps.check_target_period(session, [user.id], new_period)
        comps.check_caps((await comps.live_counts(session, [user.id], new_period))[user.id])
        item.period = new_period
        changed.append("period")
    if "label" in sent and body.label != item.label:
        item.label = body.label
        changed.append("label")
    if "amount" in sent and calc.money_str(body.amount) != calc.money_str(Decimal(item.amount)):
        item.amount = calc.money_str(body.amount)
        changed.append("amount")
    if changed:
        item.version += 1
        item.updated_by = actor.id
        comps.add_event(
            session, item_type="one_time", item=item, action="update", actor_id=actor.id,
            prev_period=prev_period if "period" in changed else None,
            prev_amount=prev_amount if "amount" in changed else None,
        )  # fmt: skip
        audit(session, action="payroll.item.update", actor=actor, entity_type=_ENTITY, entity_id=item.id,
              after={"fields": sorted(changed), "period": pc.period_str(item.period)})  # fmt: skip
    await pc.commit(session)
    return (await _out_many(session, [await _item_or_404(session, item_id)]))[0]


def _cancel_row(session: AsyncSession, item: PayrollOneTimeItem, actor_id: uuid.UUID, reason: str) -> None:
    item.status, item.cancel_reason, item.updated_by, item.version = "cancelled", reason, actor_id, item.version + 1
    comps.add_event(session, item_type="one_time", item=item, action="cancel", actor_id=actor_id, detail={"reason": reason})


async def cancel_item(session: AsyncSession, actor: Principal, item_id: uuid.UUID, reason: str) -> dict[str, Any]:
    peek = await _item_or_404(session, item_id)
    await pc.forbid_self(session, actor, peek.user_id, entity_type=_ENTITY, entity_id=item_id)
    await _lock_runs(session, [peek.period])
    item = await _item_or_404(session, item_id, lock=True)
    _require_pending(item)
    _cancel_row(session, item, actor.id, reason)
    audit(session, action="payroll.item.cancel", actor=actor, entity_type=_ENTITY, entity_id=item.id,
          after={"period": pc.period_str(item.period), "code": item.code})  # fmt: skip
    await pc.commit(session)
    return (await _out_many(session, [await _item_or_404(session, item_id)]))[0]


# ============================================================================================ batches
async def _batch_out(session: AsyncSession, batches: list[PayrollImportBatch]) -> list[dict[str, Any]]:
    if not batches:
        return []
    counts: dict[uuid.UUID, dict[str, int]] = defaultdict(lambda: {"pending": 0, "processing": 0, "paid": 0, "cancelled": 0})
    q = (
        select(PayrollOneTimeItem.batch_id, PayrollOneTimeItem.status, func.count())
        .where(PayrollOneTimeItem.batch_id.in_([b.id for b in batches]))
        .group_by(PayrollOneTimeItem.batch_id, PayrollOneTimeItem.status)
    )
    for bid, st, n in (await session.execute(q)).all():
        counts[bid][st] = n
    people = await _people(session, {b.created_by for b in batches})
    return [
        {
            "id": str(b.id),
            "period": pc.period_str(b.period),
            "rows": b.rows,
            "created_at": pc.iso(b.created_at),
            "created_by": pc.user_out(people.get(b.created_by)),
            "cancelled_at": pc.iso(b.cancelled_at),
            "counts": dict(counts[b.id]),
        }
        for b in batches
    ]


async def list_batches(session: AsyncSession, period: str | None) -> dict[str, Any]:
    q = select(PayrollImportBatch).order_by(PayrollImportBatch.created_at.desc()).limit(200)
    if period:
        q = q.where(PayrollImportBatch.period == comps.parse_target_period(period, await runs._today(session)))
    return {"items": await _batch_out(session, list((await session.execute(q)).scalars()))}


async def cancel_batch(session: AsyncSession, actor: Principal, batch_id: uuid.UUID, reason: str) -> dict[str, Any]:
    peek = await session.get(PayrollImportBatch, batch_id)
    if peek is None:
        raise AppError("NOT_FOUND", "Batch not found")
    await _lock_runs(session, [peek.period])
    batch = (
        await session.execute(
            select(PayrollImportBatch)
            .where(PayrollImportBatch.id == batch_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if batch.cancelled_at is not None:
        raise AppError("CONFLICT", "The batch was already cancelled")
    q = (
        select(PayrollOneTimeItem)
        .where(PayrollOneTimeItem.batch_id == batch_id)
        .order_by(PayrollOneTimeItem.id)
        .with_for_update()
    )
    items = list((await session.execute(q.execution_options(populate_existing=True))).scalars())
    if any(i.user_id == actor.id for i in items):
        raise await pc.deny(session, actor, entity_type="payroll_import_batch", entity_id=batch_id, reason="self")
    cancelled = skipped = 0
    for i in items:
        if i.status == "pending":
            _cancel_row(session, i, actor.id, reason)
            cancelled += 1
        elif i.status != "cancelled":
            skipped += 1
    batch.cancelled_at, batch.cancelled_by = utcnow(), actor.id
    audit(session, action="payroll.import.cancel", actor=actor, entity_type="payroll_import_batch", entity_id=batch_id,
          after={"period": pc.period_str(batch.period), "cancelled": cancelled, "not_cancelled": skipped})  # fmt: skip
    await pc.commit(session)
    return {"cancelled": cancelled, "not_cancelled": skipped}


# ============================================================================================ template
async def template() -> xlsx.BuiltWorkbook:
    """`Items` (header + one example row that fails with UNKNOWN_EMPLOYEE if it is left in) and `Codes`."""
    items = [("EXAMPLE-DELETE-ME", "BONUS", "Diwali bonus", 5000)]
    codes = [
        ("BONUS", "Earning", "Not PF or ESI wages"),
        ("INCENTIVE", "Earning", "ESI wages"),
        ("OTHER_EARNING", "Earning", "ESI wages"),
        ("OTHER_DEDUCTION", "Deduction", "Amount is entered as a positive number"),
    ]

    def spec(title: str, headers: list[str], table: list[tuple]) -> xlsx.SheetSpec:
        async def rows() -> AsyncIterator[tuple]:
            for r in table:
                yield r

        async def count() -> int:
            return len(table)

        return xlsx.SheetSpec(title, headers, count, rows, 100)

    return await xlsx.build_workbook(
        [spec("Items", list(xlsx_import.HEADER), items), spec("Codes", ["Code", "Type", "Notes"], codes)]
    )


# ============================================================================================ bulk import
def _text(v: Any) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


async def _validate_rows(
    session: AsyncSession, actor: Principal, period: dt.date, parsed: list[xlsx_import.ParsedRow]
) -> tuple[list[dict], list[dict]]:
    """(errors, valid rows). Every error names the spreadsheet row, the field and a code; values are never echoed."""
    errors: list[dict] = []

    def err(row: int, field: str, msg: str) -> None:
        errors.append({"row": row, "field": field, "message": msg})

    eids = {e for pr in parsed if (e := (_text(pr.values.get("employee_id")) or "").upper()) and _EID.match(e)}
    users = {}
    if eids:
        q = select(User).where(User.employee_id.in_(sorted(eids)))
        users = {u.employee_id.upper(): u for u in (await session.execute(q)).unique().scalars()}
    details = await runs._load_details(session, [u.id for u in users.values()])
    tz = att.office_tz(await att.load_settings(session))
    counts = await comps.live_counts(session, [u.id for u in users.values()], period)
    added: dict[uuid.UUID, int] = defaultdict(int)
    seen: set[tuple] = set()
    valid: list[dict] = []

    for pr in parsed:
        n0 = len(errors)
        v = pr.values
        for f in sorted(pr.formula):
            err(pr.row, f, "FORMULA_NOT_ALLOWED")
        user = None
        if "employee_id" not in pr.formula:
            eid = (_text(v.get("employee_id")) or "").upper()
            user = users.get(eid) if _EID.match(eid) else None
            if user is None or user.deleted_at is not None:
                err(pr.row, "employee_id", "UNKNOWN_EMPLOYEE")
                user = None
            elif user.id == actor.id:
                err(pr.row, "employee_id", "SELF")
                user = None
            elif not runs._eligible(user, details.get(user.id), period, tz):
                err(pr.row, "employee_id", "NOT_ELIGIBLE")
                user = None
        code = None
        if "code" not in pr.formula:
            raw = (_text(v.get("code")) or "").upper()
            if raw in CODES:
                code = raw
            else:
                err(pr.row, "code", "INVALID_CODE")
        label = None
        if "label" not in pr.formula:
            raw_label = _text(v.get("label"))
            cleaned = clean_text(raw_label) if raw_label else ""
            if 1 <= len(cleaned) <= 40:
                label = cleaned
            else:
                err(pr.row, "label", "INVALID_LABEL")
        amount = None
        if "amount" not in pr.formula:
            amt = xlsx_import.amount_of(v.get("amount"))
            if amt is not None and 0 < amt <= calc.MAX_MONEY:
                amount = amt
            else:
                err(pr.row, "amount", "INVALID_AMOUNT")
        if user is not None and code and label and amount is not None:
            key = (user.id, code, label, amount)
            if key in seen:
                err(pr.row, "employee_id", "DUPLICATE_ROW")
            else:
                seen.add(key)
                c = counts[user.id]
                if (
                    c["one_time"] + added[user.id] + 1 > comps.MAX_ONE_TIME_PER_MONTH
                    or c["total"] + added[user.id] + 1 > comps.MAX_LINES_PER_MONTH
                ):
                    err(pr.row, "employee_id", "TOO_MANY_ITEMS")
                else:
                    added[user.id] += 1
        if len(errors) == n0:
            valid.append({"row": pr.row, "user": user, "code": code, "label": label, "amount": amount})
    return errors, valid


def _by_code(valid: list[dict]) -> list[dict]:
    agg: dict[str, list] = {}
    for r in valid:
        a = agg.setdefault(r["code"], [0, Decimal("0.00")])
        a[0] += 1
        a[1] += r["amount"]
    return [{"code": c, "count": a[0], "total": calc.money_str(a[1])} for c, a in sorted(agg.items())]


async def import_items(
    session: AsyncSession,
    redis: Redis,
    actor: Principal,
    *,
    data: bytes,
    raw_period: str,
    dry_run: bool,
    file_sha256: str | None,
    password: SecretStr | None,
) -> dict[str, Any]:
    today = await runs._today(session)
    period = comps.parse_target_period(raw_period, today)
    digest = hashlib.sha256(data).hexdigest()
    if not dry_run:
        if not file_sha256 or not re.fullmatch(r"[0-9a-f]{64}", file_sha256):
            raise _bad("file_sha256", "send the file_sha256 returned by the dry run")
        await admin_security.require_reauth(session, redis, actor, password, action="payroll.import.commit")
        if file_sha256 != digest:
            raise AppError("IMPORT_FILE_CHANGED", "The file differs from the one that was checked; run the dry run again")
        dup = await session.execute(
            select(PayrollImportBatch.id).where(PayrollImportBatch.file_sha256 == digest, PayrollImportBatch.period == period)
        )
        if dup.first():
            raise AppError("IMPORT_DUPLICATE", "This file was already imported for that month")
    parsed = await xlsx_import.parse(data)
    if not parsed:
        raise AppError("IMPORT_INVALID", "The sheet has no data rows", details=[{"field": "file", "message": "no data rows"}])
    errors, valid = await _validate_rows(session, actor, period, parsed)
    user_ids = list({r["user"].id for r in valid})
    run_status = await comps.check_target_period(session, user_ids, period, lock=not dry_run)

    if dry_run:
        audit(session, action="payroll.import.dry_run", actor=actor, entity_type="payroll_import_batch",
              after={"period": pc.period_str(period), "rows": len(parsed), "valid": len(valid), "errors": len(errors)})  # fmt: skip
        await pc.commit(session)
        return {
            "period": pc.period_str(period), "file_sha256": digest, "rows": len(parsed), "valid": len(valid),
            "errors": errors, "by_code": _by_code(valid), "run_status": run_status,
        }  # fmt: skip

    if errors:
        raise AppError(
            "IMPORT_INVALID", "The file has errors; nothing was imported",
            details=[{"field": f"row {e['row']}: {e['field']}", "message": e["message"]} for e in errors],
        )  # fmt: skip
    batch = PayrollImportBatch(period=period, file_sha256=digest, rows=len(valid), created_by=actor.id)
    session.add(batch)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError("IMPORT_DUPLICATE", "This file was already imported for that month") from None
    for r in valid:
        item = PayrollOneTimeItem(
            user_id=r["user"].id, period=period, code=r["code"], label=r["label"], amount=calc.money_str(r["amount"]),
            status="pending", batch_id=batch.id, version=1, created_by=actor.id, updated_by=actor.id,
        )  # fmt: skip
        session.add(item)
        await pc.flush(session)
        comps.add_event(
            session, item_type="one_time", item=item, action="create", actor_id=actor.id, detail={"batch": str(batch.id)}
        )
    audit(session, action="payroll.import.commit", actor=actor, entity_type="payroll_import_batch", entity_id=batch.id,
          after={"batch_id": str(batch.id), "period": pc.period_str(period), "rows": len(valid)})  # fmt: skip
    batch_id = batch.id
    await pc.commit(session)
    return {"batch_id": str(batch_id), "period": pc.period_str(period), "created": len(valid)}
