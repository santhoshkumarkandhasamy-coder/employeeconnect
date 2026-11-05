"""Company (debit) bank accounts (plan 17 work package 5a, contract 19.4).

Bank name, account number and IFSC are encrypted and IMMUTABLE (the database refuses a change except key rotation): to change one,
create a new account. Only the label, the default flag and the active flag can change. Creating an account needs the step-up
password and alerts the `role.assign` holders. The account number is always masked in responses; the unmasked value exists only in
the bank file. Audit rows carry ids and field names only.
"""

from __future__ import annotations

import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.rbac import Principal
from ..models import CompanyBankAccount, PayrollPaymentBatch, User
from ..schemas.payroll_controls import BankAccountCreate, BankAccountUpdate
from . import admin_security
from . import payroll_common as pc

ENTITY = "company_bank_account"
IN_USE_STATUSES = ("draft", "submitted", "approved")


def account_out(a: CompanyBankAccount, creator: User | None) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "label": a.label,
        "bank_name": a.bank_name,
        "account": pc.mask_tail(a.account),
        "ifsc": a.ifsc,
        "is_default": a.is_default,
        "active": a.active,
        "created_at": pc.iso(a.created_at),
        "created_by": pc.user_out(creator),
    }


async def _out_many(session: AsyncSession, rows: list[CompanyBankAccount]) -> list[dict[str, Any]]:
    ids = {a.created_by for a in rows}
    people = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(ids)))).unique().scalars()} if ids else {}
    return [account_out(a, people.get(a.created_by)) for a in rows]


async def list_accounts(session: AsyncSession, actor: Principal) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(select(CompanyBankAccount).order_by(CompanyBankAccount.created_at, CompanyBankAccount.id))
        ).scalars()
    )
    items = await _out_many(session, rows)
    await pc.audit_read(session, actor, "payroll.bank_account.list", ENTITY, count=len(items))
    return {"items": items}


async def _clear_default(session: AsyncSession, actor: Principal, keep: uuid.UUID | None) -> None:
    q = update(CompanyBankAccount).where(CompanyBankAccount.is_default.is_(True), CompanyBankAccount.active.is_(True))
    if keep is not None:
        q = q.where(CompanyBankAccount.id != keep)
    await session.execute(q.values(is_default=False, updated_by=actor.id))


async def create_account(session: AsyncSession, redis: Redis, actor: Principal, body: BankAccountCreate) -> dict[str, Any]:
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.bank_account.create")
    if body.is_default:
        await _clear_default(session, actor, None)
    a = CompanyBankAccount(
        label=body.label, bank_name=body.bank_name, account=body.account, ifsc=body.ifsc, is_default=body.is_default,
        active=True, created_by=actor.id,
    )  # fmt: skip
    session.add(a)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise pc.map_integrity(exc) from None
    audit(session, action="payroll.bank_account.create", actor=actor, entity_type=ENTITY, entity_id=a.id,
          after={"is_default": a.is_default})  # fmt: skip
    label, aid = a.label, a.id

    async def _alert() -> None:
        await admin_security.notify_admins(session, entity_id=aid, title="Company bank account added", body=label)

    pc.after_commit(session, _alert)
    await pc.commit(session)
    await session.refresh(a)
    return account_out(a, actor.user)


async def update_account(
    session: AsyncSession, actor: Principal, account_id: uuid.UUID, body: BankAccountUpdate
) -> dict[str, Any]:
    a = (
        await session.execute(select(CompanyBankAccount).where(CompanyBankAccount.id == account_id).with_for_update())
    ).scalar_one_or_none()
    if a is None:
        raise AppError("NOT_FOUND", "Bank account not found")
    sent = sorted(body.model_fields_set)
    if body.active is False and a.active:
        used = (
            await session.execute(
                select(PayrollPaymentBatch.id)
                .where(PayrollPaymentBatch.debit_account_id == a.id, PayrollPaymentBatch.status.in_(IN_USE_STATUSES))
                .limit(1)
            )
        ).first()
        if used:
            raise AppError("IN_USE", "A payment batch that is not exported yet uses this account")
    if body.label is not None:
        a.label = body.label
    if body.active is not None:
        a.active = body.active
        if not body.active:
            a.is_default = False  # an inactive account is never the default
    if body.is_default is not None:
        if body.is_default and not a.active:
            raise AppError("VALIDATION_ERROR", "An inactive account cannot be the default",
                           details=[{"field": "is_default", "message": "activate the account first"}])  # fmt: skip
        if body.is_default:
            await _clear_default(session, actor, a.id)
        a.is_default = body.is_default
    a.updated_by = actor.id
    audit(session, action="payroll.bank_account.update", actor=actor, entity_type=ENTITY, entity_id=a.id, after={"fields": sent})
    await pc.commit(session)
    await session.refresh(a)
    creator = await session.get(User, a.created_by)
    return account_out(a, creator)
