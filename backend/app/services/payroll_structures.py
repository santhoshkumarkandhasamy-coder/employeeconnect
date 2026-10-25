"""Salary structures, payroll details (bank / PAN / UAN / ESI) and the payroll employee list (contract 13.4)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import EmployeePayrollDetails, Payslip, SalaryStructure, User
from ..schemas.payroll import DetailsIn, StructureIn
from . import admin_security
from . import attendance as att
from . import payroll_common as pc
from .payroll_calc import StructureData, money_str, structure_for

DETAIL_FIELDS = (
    "designation",
    "payment_mode",
    "include_in_payroll",
    "bank_name",
    "bank_account",
    "bank_ifsc",
    "pan",
    "uan",
    "esi_ip_number",
)
MIN_YEAR, MAX_YEAR = 2000, 2200


async def get_user_or_404(session: AsyncSession, user_id: uuid.UUID) -> User:
    u = (await session.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))).unique().scalar_one_or_none()
    if u is None:
        raise AppError("NOT_FOUND", "Employee not found")
    return u


def data_of(s: SalaryStructure) -> StructureData:
    return StructureData.from_stored(
        s.components,
        pf_applicable=s.pf_applicable,
        pf_on_full_basic=s.pf_on_full_basic,
        esi_applicable=s.esi_applicable,
        pt_applicable=s.pt_applicable,
    )


def _components(body: StructureIn) -> dict[str, Any]:
    return {
        "basic": money_str(body.basic),
        "hra": money_str(body.hra),
        "allowances": [
            {"code": a.code, "name": a.name, "amount": money_str(a.amount), "prorate": a.prorate, "esi_wage": a.esi_wage}
            for a in body.allowances
        ],
        "fixed_deductions": [{"code": d.code, "name": d.name, "amount": money_str(d.amount)} for d in body.fixed_deductions],
        "tds_monthly": money_str(body.tds_monthly),
    }


def _check_effective(day: dt.date) -> None:
    if day.day != 1 or not MIN_YEAR <= day.year <= MAX_YEAR:
        raise AppError(
            "INVALID_PERIOD", "effective_from must be the 1st of a month",
            details=[{"field": "effective_from", "message": "must be the 1st of a month (years 2000-2200)"}],
        )  # fmt: skip


async def _in_use(session: AsyncSession, structure_id: uuid.UUID) -> bool:
    q = select(exists().where(Payslip.structure_id == structure_id, Payslip.status.in_(("processed", "published", "superseded"))))
    return bool((await session.execute(q)).scalar())


async def _in_use_ids(session: AsyncSession, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    q = select(Payslip.structure_id).where(
        Payslip.structure_id.in_(ids), Payslip.status.in_(("processed", "published", "superseded"))
    ).distinct()  # fmt: skip
    return set((await session.execute(q)).scalars())


def structure_out(s: SalaryStructure, *, effective_to: dt.date | None, in_use: bool, creator: User | None) -> dict[str, Any]:
    d = data_of(s)
    c = s.components
    return {
        "id": str(s.id),
        "user_id": str(s.user_id),
        "effective_from": s.effective_from.isoformat(),
        "effective_to": effective_to.isoformat() if effective_to else None,
        "basic": c["basic"],
        "hra": c.get("hra", "0.00"),
        "allowances": c.get("allowances", []),
        "fixed_deductions": c.get("fixed_deductions", []),
        "tds_monthly": c.get("tds_monthly", "0.00"),
        "pf_applicable": s.pf_applicable,
        "pf_on_full_basic": s.pf_on_full_basic,
        "esi_applicable": s.esi_applicable,
        "pt_applicable": s.pt_applicable,
        "monthly_gross": money_str(d.monthly_gross),
        "in_use": in_use,
        "created_at": pc.iso(s.created_at),
        "created_by": pc.user_out(creator),
        "updated_at": pc.iso(s.updated_at),
    }


async def _all_structures(session: AsyncSession, user_id: uuid.UUID) -> list[SalaryStructure]:
    q = select(SalaryStructure).where(SalaryStructure.user_id == user_id).order_by(SalaryStructure.effective_from.desc())
    return list((await session.execute(q)).scalars())


async def _outs(session: AsyncSession, rows: list[SalaryStructure]) -> list[dict[str, Any]]:
    """`rows` newest first, all of ONE user."""
    used = await _in_use_ids(session, [r.id for r in rows])
    creators = {}
    for uid in {r.created_by for r in rows if r.created_by}:
        creators[uid] = await session.get(User, uid)
    out = []
    for i, r in enumerate(rows):
        nxt = rows[i - 1].effective_from if i > 0 else None
        out.append(
            structure_out(
                r,
                effective_to=(nxt - dt.timedelta(days=1)) if nxt else None,
                in_use=r.id in used,
                creator=creators.get(r.created_by),
            )
        )
    return out


async def list_structures(session: AsyncSession, actor: Principal, user_id: uuid.UUID) -> dict[str, Any]:
    await get_user_or_404(session, user_id)
    out = await _outs(session, await _all_structures(session, user_id))
    audit(session, action="payroll.structure.view", actor=actor, entity_type="user", entity_id=user_id)
    await pc.commit(session)
    return {"items": out}


async def _one_out(session: AsyncSession, s: SalaryStructure) -> dict[str, Any]:
    rows = await _all_structures(session, s.user_id)
    for o in await _outs(session, rows):
        if o["id"] == str(s.id):
            return o
    raise AppError("NOT_FOUND", "Structure not found")


async def create_structure(session: AsyncSession, actor: Principal, user_id: uuid.UUID, body: StructureIn) -> dict[str, Any]:
    await get_user_or_404(session, user_id)
    await pc.forbid_self(session, actor, user_id, entity_type="salary_structure", entity_id=None)
    _check_effective(body.effective_from)
    taken = (
        await session.execute(
            select(SalaryStructure.id).where(
                SalaryStructure.user_id == user_id, SalaryStructure.effective_from == body.effective_from
            )
        )
    ).first()
    if taken:
        raise AppError("STRUCTURE_EXISTS", "A structure with this effective date already exists")
    s = SalaryStructure(
        user_id=user_id,
        effective_from=body.effective_from,
        components=_components(body),
        pf_applicable=body.pf_applicable,
        pf_on_full_basic=body.pf_on_full_basic,
        esi_applicable=body.esi_applicable,
        pt_applicable=body.pt_applicable,
        created_by=actor.id,
        updated_by=actor.id,
    )
    session.add(s)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError("STRUCTURE_EXISTS", "A structure with this effective date already exists") from None
    audit(
        session, action="payroll.structure.create", actor=actor, entity_type="salary_structure", entity_id=s.id,
        after={"user_id": str(user_id), "effective_from": body.effective_from.isoformat()},
    )  # fmt: skip
    await pc.commit(session)
    return await _one_out(session, s)


async def _structure_or_404(session: AsyncSession, structure_id: uuid.UUID) -> SalaryStructure:
    s = (
        await session.execute(select(SalaryStructure).where(SalaryStructure.id == structure_id).with_for_update())
    ).scalar_one_or_none()
    if s is None:
        raise AppError("NOT_FOUND", "Structure not found")
    return s


async def update_structure(session: AsyncSession, actor: Principal, structure_id: uuid.UUID, body: StructureIn) -> dict[str, Any]:
    s = await _structure_or_404(session, structure_id)
    await pc.forbid_self(session, actor, s.user_id, entity_type="salary_structure", entity_id=s.id)
    if await _in_use(session, s.id):
        raise AppError("STRUCTURE_IN_USE", "This structure is used by a processed or published payslip; add a new one instead")
    _check_effective(body.effective_from)
    if body.effective_from != s.effective_from:
        dup = (
            await session.execute(
                select(SalaryStructure.id).where(
                    SalaryStructure.user_id == s.user_id, SalaryStructure.effective_from == body.effective_from
                )
            )
        ).first()
        if dup:
            raise AppError("STRUCTURE_EXISTS", "A structure with this effective date already exists")
    new_components = _components(body)
    changed = []
    if body.effective_from != s.effective_from:
        changed.append("effective_from")
    for key in ("basic", "hra", "allowances", "fixed_deductions", "tds_monthly"):
        if s.components.get(key) != new_components[key]:
            changed.append(key)
    for flag in ("pf_applicable", "pf_on_full_basic", "esi_applicable", "pt_applicable"):
        if getattr(s, flag) != getattr(body, flag):
            changed.append(flag)
    s.effective_from = body.effective_from
    s.components = new_components
    s.pf_applicable, s.pf_on_full_basic = body.pf_applicable, body.pf_on_full_basic
    s.esi_applicable, s.pt_applicable = body.esi_applicable, body.pt_applicable
    s.updated_by = actor.id
    audit(
        session, action="payroll.structure.update", actor=actor, entity_type="salary_structure", entity_id=s.id,
        after={"user_id": str(s.user_id), "effective_from": body.effective_from.isoformat(), "fields": changed},
    )  # fmt: skip
    await pc.commit(session)
    return await _one_out(session, s)


async def delete_structure(session: AsyncSession, actor: Principal, structure_id: uuid.UUID) -> None:
    s = await _structure_or_404(session, structure_id)
    await pc.forbid_self(session, actor, s.user_id, entity_type="salary_structure", entity_id=s.id)
    if await _in_use(session, s.id):
        raise AppError("STRUCTURE_IN_USE", "This structure is used by a processed or published payslip")
    if (await session.execute(select(exists().where(Payslip.structure_id == s.id)))).scalar():
        # only draft payslips reference it: they fall back to STRUCTURE_MISSING / the previous structure on refresh
        await session.execute(
            update(Payslip).where(Payslip.structure_id == s.id, Payslip.status == "draft").values(structure_id=None)
        )
    info = {"user_id": str(s.user_id), "effective_from": s.effective_from.isoformat()}
    await session.delete(s)
    audit(
        session,
        action="payroll.structure.delete",
        actor=actor,
        entity_type="salary_structure",
        entity_id=structure_id,
        after=info,
    )
    await pc.commit(session)


# ------------------------------------------------------------------------------------------------ details
def _details_out(user_id: uuid.UUID, d: EmployeePayrollDetails | None, *, reveal: bool, updater: User | None) -> dict[str, Any]:
    def m(v: str | None, fn) -> str | None:
        return v if reveal or v is None else fn(v)

    return {
        "user_id": str(user_id),
        "designation": d.designation if d else None,
        "payment_mode": d.payment_mode if d else "bank_transfer",
        "include_in_payroll": d.include_in_payroll if d else True,
        "bank_name": d.bank_name if d else None,
        "bank_account": m(d.bank_account if d else None, pc.mask_tail),
        "bank_ifsc": d.bank_ifsc if d else None,
        "pan": m(d.pan if d else None, pc.mask_pan),
        "uan": m(d.uan if d else None, pc.mask_tail),
        "esi_ip_number": m(d.esi_ip_number if d else None, pc.mask_tail),
        "revealed": reveal,
        "updated_at": pc.iso(d.updated_at) if d else None,
        "updated_by": pc.user_out(updater),
    }


async def get_details(session: AsyncSession, redis: Redis, actor: Principal, user_id: uuid.UUID, reveal: bool) -> dict[str, Any]:
    await get_user_or_404(session, user_id)
    d = await session.get(EmployeePayrollDetails, user_id)
    updater = await session.get(User, d.updated_by) if d and d.updated_by else None
    out = _details_out(user_id, d, reveal=reveal, updater=updater)
    audit(session, action="payroll.details.view", actor=actor, entity_type="user", entity_id=user_id, after={"revealed": reveal})
    await pc.commit(session)
    if reveal:
        await admin_security.note_sensitive_view(session, redis, actor)
    return out


async def put_details(
    session: AsyncSession, redis: Redis, actor: Principal, user_id: uuid.UUID, body: DetailsIn
) -> dict[str, Any]:
    await get_user_or_404(session, user_id)
    await pc.forbid_self(session, actor, user_id, entity_type="employee_payroll_details", entity_id=user_id)
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.details.update")
    sent = [f for f in DETAIL_FIELDS if f in body.model_fields_set]
    d = await session.get(EmployeePayrollDetails, user_id)
    if d is None:
        d = EmployeePayrollDetails(user_id=user_id, payment_mode="bank_transfer", include_in_payroll=True)
        session.add(d)
    changed = []
    for f in sent:
        v = getattr(body, f)
        if getattr(d, f) != v:
            setattr(d, f, v)
            changed.append(f)
    d.updated_by = actor.id
    audit(session, action="payroll.details.update", actor=actor, entity_type="user", entity_id=user_id, after={"fields": changed})
    await pc.commit(session)
    await session.refresh(d)  # updated_at is set by the database
    return _details_out(user_id, d, reveal=False, updater=actor.user)


# ------------------------------------------------------------------------------------------------ employee list
async def list_employees(
    session: AsyncSession,
    q: str | None,
    department_id: uuid.UUID | None,
    has_structure: bool | None,
    status: str | None,
    page: PageParams,
) -> dict[str, Any]:
    cond = [User.deleted_at.is_(None)]
    if q:
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cond.append(func.lower(User.full_name).like(func.lower(like), escape="\\") | User.employee_id.ilike(like, escape="\\"))
    if department_id:
        cond.append(User.department_id == department_id)
    if status in ("active", "inactive"):
        cond.append(User.status == status)
    if has_structure is not None:
        e = exists().where(SalaryStructure.user_id == User.id)
        cond.append(e if has_structure else ~e)
    total = (await session.execute(select(func.count()).select_from(User).where(*cond))).scalar_one()
    users = list(
        (
            await session.execute(
                select(User).where(*cond).order_by(User.full_name, User.employee_id).offset(page.offset).limit(page.page_size)
            )
        )
        .unique()
        .scalars()
    )
    ids = [u.id for u in users]
    today = dt.datetime.now(att.office_tz(await att.load_settings(session))).date()
    structs: dict[uuid.UUID, list[tuple[dt.date, SalaryStructure]]] = {}
    details: dict[uuid.UUID, EmployeePayrollDetails] = {}
    if ids:
        for s in (await session.execute(select(SalaryStructure).where(SalaryStructure.user_id.in_(ids)))).scalars():
            structs.setdefault(s.user_id, []).append((s.effective_from, s))
        for dd in (
            await session.execute(select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id.in_(ids)))
        ).scalars():
            details[dd.user_id] = dd
    items = []
    for u in users:
        cur = structure_for(structs.get(u.id, []), today)
        det = details.get(u.id)
        flags = []
        if det is None or (det.payment_mode == "bank_transfer" and not (det.bank_account and det.bank_ifsc)):
            flags.append("NO_BANK_DETAILS")
        if det is None or not det.pan:
            flags.append("NO_PAN")
        items.append(
            {
                "user": pc.user_out(u),
                "status": u.status,
                "include_in_payroll": det.include_in_payroll if det else True,
                "current_structure": (
                    {
                        "id": str(cur.id),
                        "effective_from": cur.effective_from.isoformat(),
                        "monthly_gross": money_str(data_of(cur).monthly_gross),
                    }
                    if cur
                    else None
                ),
                "flags": flags,
            }
        )
    return make_page(items, page, total)
