"""Investment declarations and proofs (plan 17 work package 5b, contract 19.7).

* T2: every employee-facing call needs a VERIFIED rules version for the FY (409 `TAX_RULES_NOT_VERIFIED`); sections, limits and field lists
  come from it, nothing is hard-coded.
* T3: `draft` is what the employee edits; `submitted` (+ regime, version, time) is frozen at submit and is the only part that feeds tax.
* T4: proofs are maker-checker: HR verifies / rejects with `expected_version`; the decider is not the employee (403 `FORBIDDEN`) and not
  the person who entered the declaration on behalf of the employee (403 `SECOND_PERSON_REQUIRED`, also checked against the money history).
* Declarations, claimed / verified amounts, unlock reasons and comments are encrypted; third-party PANs (landlord / lender) are masked in
  every response unless an admin reveals them (audited). Audit rows carry ids, the FY, counts and the regime only.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import and_, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..core.text import clean_text
from ..models import (
    Department,
    EmployeePayrollDetails,
    File,
    PayrollItemEvent,
    TaxDeclaration,
    TaxProof,
    TaxYearRule,
    User,
)
from ..schemas.payroll_tax import (
    DeclarationSave,
    DeclarationsReportIn,
    DeclarationSubmit,
    DeclarationUnlock,
    ProofCreate,
    ProofReject,
    ProofVerify,
)
from . import admin_security, notifications, xlsx
from . import payroll_common as pc
from . import payroll_settings as ps
from . import tax_rules as tr
from .files import file_url

ENTITY = "tax_declaration"
PROOF_ENTITY = "tax_proof"
MAX_ITEMS = 40
PROOF_STATES = ("submitted", "verified", "rejected")
MAX_REPORT_ROWS = 200_000
_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _bad(path: str, msg: str) -> AppError:
    return AppError("VALIDATION_ERROR", "The declaration is not valid", details=[{"field": path, "message": msg}])


# ============================================================================================ edit window
def edit_state(decl: TaxDeclaration | None, windows: dict | None, day: dt.date) -> tuple[bool, str]:
    """(can_edit, reason). DECLARATION_WINDOW > UNLOCKED (HR) > PROOF_WINDOW (proof items only) > CLOSED."""
    if tr.window_open(windows, "declaration", day):
        return True, "DECLARATION_WINDOW"
    if decl is not None and decl.unlocked_until is not None and day <= decl.unlocked_until:
        return True, "UNLOCKED"
    if tr.window_open(windows, "proof", day):
        return True, "PROOF_WINDOW"
    return False, "CLOSED"


# ============================================================================================ items
def _canon(item: dict) -> str:
    return json.dumps({k: item.get(k) for k in ("id", "section", "amount", "details")}, sort_keys=True, separators=(",", ":"))


def _same(a_regime: str | None, a_items: list[dict], b_regime: str | None, b_items: list[dict]) -> bool:
    return a_regime == b_regime and [_canon(i) for i in a_items] == [_canon(i) for i in b_items]


def _next_ids(*groups: list[dict]) -> int:
    top = 0
    for items in groups:
        for i in items:
            m = re.fullmatch(r"i-(\d+)", str(i.get("id", "")))
            if m:
                top = max(top, int(m.group(1)))
    return top


def _detail(path: str, kind: str, key: str, v: Any, fy: str) -> Any:
    if kind == "bool":
        if not isinstance(v, bool):
            raise _bad(path, "must be true or false")
        return v
    if kind == "text":
        if not isinstance(v, str):
            raise _bad(path, "must be text")
        c = clean_text(v)
        if not 1 <= len(c) <= 100:
            raise _bad(path, "must be 1-100 characters")
        return c
    if kind == "pan":
        p = v.strip().upper() if isinstance(v, str) else ""
        if not tr.valid_pan(p):
            raise _bad(path, "must be a PAN such as ABCPE1234F")
        return p
    if kind == "month":
        if not isinstance(v, str) or not tr.month_in_fy(fy, v):
            raise _bad(path, "must be a month (YYYY-MM) inside the financial year")
        return v
    cap = tr.MONTHLY_CAP if key == "monthly_rent" else tr.ANNUAL_CAP
    return tr.money(path, v, cap=cap, positive=key == "monthly_rent")  # money: Decimal, formatted by the caller


def _months(a: str, b: str) -> int:
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7]) + 1


def build_items(
    rules: dict, fy: str, regime: str | None, raw: list, draft_ids: set[str], submitted: list[dict], current: list[dict]
) -> list[dict]:
    """Validate and normalise the items of a save against the verified rules. Flags: `NOT_IN_REGIME` (the section belongs to the
    other regime only) and `ABOVE_LIMIT` (information; the engine applies the limits)."""
    sections = {s["code"]: s for s in rules.get("sections", [])}
    effective = regime or rules.get("default_regime")
    regimes = rules.get("regimes", {})
    in_any = set().union(*[set(r.get("allowed_sections", [])) for r in regimes.values()]) if regimes else set()
    allowed_here = set(regimes.get(effective, {}).get("allowed_sections", []))
    next_n = _next_ids(current, submitted, [{"id": i} for i in draft_ids])
    seen_ids: set[str] = set()
    existing = {i["id"]: i for i in current}
    out: list[dict] = []
    for idx, it in enumerate(raw):
        p = f"items[{idx}]"
        sec = sections.get(it.section)
        if sec is None:
            raise AppError("INVALID_SECTION", "The section is not part of the tax rules",
                           details=[{"field": f"{p}.section", "message": "unknown section"}])  # fmt: skip
        if it.id is not None:
            if not _ITEM_ID.match(it.id) or it.id not in draft_ids:
                raise _bad(f"{p}.id", "unknown item id")
            if it.id in seen_ids:
                raise _bad(f"{p}.id", "duplicate item id")
            old = existing.get(it.id)
            if old is not None and old["section"] != it.section:
                raise _bad(f"{p}.section", "the section of an item cannot change; remove it and add a new item")
            item_id = it.id
        else:
            next_n += 1
            item_id = f"i-{next_n}"
        seen_ids.add(item_id)
        fields = sec["fields"]
        details: dict[str, Any] = {}
        for k, v in (it.details or {}).items():
            if k not in fields:
                raise _bad(f"{p}.details.{k}", "not a field of this section")
            kind = tr.SECTION_FIELDS[k]
            stored = ((existing.get(it.id) or {}).get("details") or {}).get(k) if it.id is not None else None
            if kind == "pan" and stored and isinstance(v, str) and v.strip().upper() == pc.mask_pan(stored):
                details[k] = stored  # the client sent back the masked value it was shown: keep the stored PAN
                continue
            val = _detail(f"{p}.details.{k}", kind, k, v, fy)
            details[k] = tr._m(val) if isinstance(val, Decimal) else val
        if "income" in fields and "tds" in details and Decimal(details["tds"]) > Decimal(details.get("income", "0")):
            raise _bad(f"{p}.details.tds", "the TDS of the previous employer cannot exceed its income")
        flags: list[str] = []
        computed = {"monthly_rent", "from_month", "to_month"} <= set(fields)
        if computed:
            for k in ("monthly_rent", "from_month", "to_month"):
                if k not in details:
                    raise _bad(f"{p}.details.{k}", "is required")
            n = _months(details["from_month"], details["to_month"])
            if n < 1:
                raise _bad(f"{p}.details.to_month", "must not be before from_month")
            annual = Decimal(details["monthly_rent"]) * n
            if annual > tr.ANNUAL_CAP:
                raise _bad(f"{p}.details.monthly_rent", f"the annual rent must be at most {tr.ANNUAL_CAP:.2f}")
            threshold = sec.get("landlord_pan_above")
            if (
                "landlord_pan" in fields
                and threshold is not None
                and annual > Decimal(threshold)
                and not details.get("landlord_pan")
            ):
                raise AppError("LANDLORD_PAN_REQUIRED", "The landlord's PAN is required for this annual rent",
                               details=[{"field": f"{p}.details.landlord_pan", "message": "required above the PAN threshold"}])  # fmt: skip
            amount = annual
        else:
            if it.amount is None:
                raise _bad(f"{p}.amount", "is required")
            amount = tr.money(f"{p}.amount", it.amount, positive=True)
        limit = sec.get("senior_limit") if details.get("senior") is True and sec.get("senior_limit") else sec.get("limit")
        if limit is not None and amount > Decimal(limit):
            flags.append("ABOVE_LIMIT")
        if it.section in in_any and it.section not in allowed_here:
            flags.append("NOT_IN_REGIME")
        out.append({"id": item_id, "section": it.section, "amount": tr._m(amount), "details": details, "flags": flags})
    return out


def _proof_required(rules: dict, section: str) -> bool:
    return any(s["code"] == section and s.get("proof_required") for s in rules.get("sections", []))


def _proof_window_ok(rules: dict, old: list[dict], new: list[dict]) -> bool:
    """In the proof window only items of sections that need a proof may change (add, edit, remove)."""
    keep = lambda items: {i["id"]: _canon(i) for i in items if not _proof_required(rules, i["section"])}  # noqa: E731
    return keep(old) == keep(new)


async def _live_items(session: AsyncSession, decl_id: uuid.UUID) -> set[str]:
    """Item ids that hold a live (submitted / verified) proof."""
    rows = await session.execute(
        select(TaxProof.item_id).where(TaxProof.declaration_id == decl_id, TaxProof.status.in_(("submitted", "verified")))
    )
    return set(rows.scalars())


def _check_proofed_items(live: set[str], submitted: list[dict], new: list[dict]) -> None:
    """M1: an item with a live proof cannot be edited or removed (cancel a submitted proof first; a verified one is final)."""
    if not live:
        return
    frozen = {i["id"]: _canon(i) for i in submitted if i["id"] in live}
    now = {i["id"]: _canon(i) for i in new}
    if any(now.get(iid) != canon for iid, canon in frozen.items()):
        raise AppError("CONFLICT", "An item with a proof cannot be changed or removed; cancel the proof first")


def _regime_is_locked(d: TaxDeclaration, reason: str, new_regime: str | None, unlock_allows: bool) -> bool:
    """M2: the regime follows the window. A closed window with an HR unlock keeps the chosen regime unless the unlock allowed a change."""
    if reason == "DECLARATION_WINDOW":
        return False
    chosen = d.regime if d.regime is not None else d.regime_draft
    if reason == "UNLOCKED":
        return chosen is not None and new_regime != chosen and not unlock_allows
    return new_regime != d.regime_draft


async def _unlock_allows_regime(session: AsyncSession, d: TaxDeclaration) -> bool:
    """The newest `unlock` event of the declaration (encrypted detail) says whether an approver allowed a regime change.
    "Newest" is the highest declaration `version` recorded in the event: timestamps can tie, ids are random."""
    if d.id is None:
        return False
    events = (
        await session.execute(
            select(PayrollItemEvent).where(
                PayrollItemEvent.item_type == "tax_declaration",
                PayrollItemEvent.item_id == d.id,
                PayrollItemEvent.action == "unlock",
            )
        )
    ).scalars()
    newest = max(events, key=lambda e: int((e.detail or {}).get("version", 0)), default=None)
    return bool(newest and (newest.detail or {}).get("allow_regime_change") is True)


# ============================================================================================ output
def _mask_item(item: dict, reveal: bool) -> dict:
    d = dict(item.get("details") or {})
    if not reveal:
        for k in ("landlord_pan", "lender_pan"):
            if d.get(k):
                d[k] = pc.mask_pan(d[k])
    return {
        "id": item["id"],
        "section": item["section"],
        "amount": item["amount"],
        "details": d,
        "flags": list(item.get("flags") or []),
    }


def _items_of(blob: dict | None) -> list[dict]:
    return list((blob or {}).get("items", []))


def declaration_out(
    decl: TaxDeclaration | None, *, fy: str, rules: TaxYearRule | None, windows: dict | None, day: dt.date,
    counts: dict[str, int] | None = None, user: User | None = None, entered_by: User | None = None, admin: bool = False,
    reveal: bool = False, catalogue: dict[str, Any] | None = None,
) -> dict[str, Any]:  # fmt: skip
    can_edit, reason = edit_state(decl, windows, day)
    draft_items = _items_of(decl.draft) if decl else []
    sub_items = _items_of(decl.submitted) if decl else []
    regime_draft = decl.regime_draft if decl else None
    if decl is None or decl.submitted is None:
        dirty = bool(draft_items) or regime_draft is not None
    else:
        dirty = not _same(regime_draft, draft_items, decl.regime, sub_items)
    out: dict[str, Any] = {
        "id": str(decl.id) if decl else None,
        "fy": fy,
        "rules_version": rules.version if rules else None,
        "windows": tr.windows_out(windows),
        "can_edit": can_edit,
        "edit_reason": reason,
        "draft": {"regime": regime_draft, "items": [_mask_item(i, reveal) for i in draft_items]},
        "submitted": (
            {"regime": decl.regime, "items": [_mask_item(i, reveal) for i in sub_items], "version": decl.submitted_version,
             "submitted_at": pc.iso(decl.submitted_at)}
            if decl is not None and decl.submitted is not None else None
        ),
        "dirty": dirty,
        "proofs": {s: (counts or {}).get(s, 0) for s in PROOF_STATES},
        "version": decl.version if decl else 0,
    }  # fmt: skip
    if catalogue is not None:
        out.update(catalogue)
    if admin:
        out["user"] = pc.user_out(user)
        out["entered_by"] = pc.user_out(entered_by)
        out["unlocked_until"] = decl.unlocked_until.isoformat() if decl and decl.unlocked_until else None
    return out


async def catalogue_of(session: AsyncSession, rules: TaxYearRule | None) -> dict[str, Any]:
    """What the employee form needs from the VERIFIED rules: the sections (code, name, limits, proof flag, fields) and the proof file cap."""
    sections = [
        {
            "code": s["code"], "name": s["name"], "limit": s.get("limit"), "senior_limit": s.get("senior_limit"),
            "proof_required": bool(s.get("proof_required")), "fields": list(s.get("fields", [])),
            "landlord_pan_above": s.get("landlord_pan_above"),
        }
        for s in (rules.rules if rules else {}).get("sections", [])
    ]  # fmt: skip
    return {"sections": sections, "proof_max_files": int(await ps.current_value(session, "payroll.tax_proof_max_files"))}


async def _counts(session: AsyncSession, decl_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    if not decl_ids:
        return {}
    rows = await session.execute(
        select(TaxProof.declaration_id, TaxProof.status, func.count())
        .where(TaxProof.declaration_id.in_(decl_ids))
        .group_by(TaxProof.declaration_id, TaxProof.status)
    )
    out: dict[uuid.UUID, dict[str, int]] = {}
    for did, st, n in rows.all():
        out.setdefault(did, {})[st] = n
    return out


# ============================================================================================ loading
def _map_db(exc: IntegrityError) -> AppError:
    text_ = str(getattr(exc, "orig", exc))
    state = pc.sqlstate(exc)
    if state == "23505" and "uq_tax_declarations_user_fy" in text_:
        return AppError("VERSION_CONFLICT", "The declaration was changed by someone else; reload and retry")
    if state == "23505" and "uq_tax_proofs_live_item" in text_:
        return AppError("CONFLICT", "This item already has a proof; cancel it first")
    if "decider entered the declaration" in text_ or "entered the declaration on behalf" in text_ or "verified a proof" in text_:
        return AppError("SECOND_PERSON_REQUIRED", "The person who entered the declaration cannot decide on its proofs")
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
    except IntegrityError as exc:  # pragma: no cover
        await session.rollback()
        raise _map_db(exc) from None


async def _decl(session: AsyncSession, user_id: uuid.UUID, fy: str, *, lock: bool = False) -> TaxDeclaration | None:
    q = select(TaxDeclaration).where(TaxDeclaration.user_id == user_id, TaxDeclaration.fy == fy)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(q)).scalar_one_or_none()


async def _active_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    u = (await session.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))).unique().scalar_one_or_none()
    if u is None:
        raise AppError("NOT_FOUND", "Employee not found")
    return u


def _decl_event(session: AsyncSession, d: TaxDeclaration, action: str, actor_id: uuid.UUID, **detail: Any) -> None:
    tr.event(
        session,
        "tax_declaration",
        d.id,
        d.user_id,
        "submitted" if d.submitted is not None else "draft",
        action,
        actor_id,
        detail or None,
    )


def _audit(session: AsyncSession, actor: Principal, action: str, d: TaxDeclaration, *, on_behalf: bool, **extra: Any) -> None:
    items = len(_items_of(d.draft))
    audit(
        session, action=action, actor=actor, entity_type=ENTITY, entity_id=d.id,
        after={"fy": d.fy, "user_id": str(d.user_id), "version": d.version, "items": items, "regime": d.regime_draft,
               "on_behalf": on_behalf, **extra},
    )  # fmt: skip


def _notify_changed(session: AsyncSession, d: TaxDeclaration) -> None:
    user_id, did, fy = d.user_id, d.id, d.fy

    async def _send() -> None:
        await notifications.notify(
            session, [user_id], type="tax_declaration_changed", entity_id=did, title="Tax declaration updated",
            body=f"HR updated your FY {fy} declaration.",
        )  # fmt: skip

    pc.after_commit(session, _send)


# ============================================================================================ declaration: read
async def years_own(session: AsyncSession) -> dict[str, Any]:
    rows = list(
        (await session.execute(select(TaxYearRule).order_by(TaxYearRule.fy.desc(), TaxYearRule.version.desc()))).scalars()
    )
    cur = tr.fy_of(await tr.today(session))
    groups: dict[str, list[TaxYearRule]] = {}
    for r in rows:
        groups.setdefault(r.fy, []).append(r)
    groups.setdefault(cur, [])
    items = []
    for fy in sorted(groups, reverse=True):
        g = groups[fy]
        v = next((r for r in g if r.status == "verified"), None)
        if v is None and fy != cur:
            continue  # employees never see a year whose rules nobody verified
        items.append({"fy": fy, "windows": tr.windows_out((v or (g[0] if g else None)).windows if (v or g) else None),
                      "verified": v is not None, "current": fy == cur})  # fmt: skip
    return {"items": items}


async def get_own(session: AsyncSession, actor: Principal, fy: str | None) -> dict[str, Any]:
    day = await tr.today(session)
    fy = tr.check_fy(fy) if fy else tr.fy_of(day)
    rules = await tr.verified_rules(session, fy)
    d = await _decl(session, actor.id, fy)
    counts = (await _counts(session, [d.id])).get(d.id, {}) if d else {}
    out = declaration_out(d, fy=fy, rules=rules, windows=rules.windows, day=day, counts=counts,
                          catalogue=await catalogue_of(session, rules))  # fmt: skip
    audit(session, action="payroll.tax_declaration.view_own", actor=actor, entity_type=ENTITY, entity_id=d.id if d else None,
          after={"fy": fy})  # fmt: skip
    await _commit(session)
    return out


async def get_admin(session: AsyncSession, actor: Principal, user_id: uuid.UUID, fy: str | None, reveal: bool) -> dict[str, Any]:
    user = await _active_user(session, user_id)
    day = await tr.today(session)
    fy = tr.check_fy(fy) if fy else tr.fy_of(day)
    rows = await tr.rows_of(session, fy)
    rules = next((r for r in rows if r.status == "verified"), None)
    windows = rules.windows if rules else (rows[0].windows if rows else None)
    d = await _decl(session, user_id, fy)
    counts = (await _counts(session, [d.id])).get(d.id, {}) if d else {}
    entered = await session.get(User, d.entered_by) if d and d.entered_by else None
    out = declaration_out(d, fy=fy, rules=rules, windows=windows, day=day, counts=counts, user=user, entered_by=entered, admin=True,
                          reveal=reveal)  # fmt: skip
    audit(session, action="payroll.tax_declaration.view", actor=actor, entity_type=ENTITY, entity_id=d.id if d else None,
          after={"fy": fy, "user_id": str(user_id), "revealed": reveal})  # fmt: skip
    await _commit(session)
    return out


async def reveal_admin(
    session: AsyncSession, redis: Redis, actor: Principal, user_id: uuid.UUID, fy: str | None, password: Any
) -> dict[str, Any]:  # fmt: skip
    """M5: unmasked third-party PANs need payroll.manage (router) and the caller's own password."""
    await admin_security.require_reauth(session, redis, actor, password, action="payroll.tax_declaration.reveal")
    return await get_admin(session, actor, user_id, fy, True)


# ============================================================================================ declaration: save / submit
async def _save(
    session: AsyncSession, actor: Principal, target: User, body: DeclarationSave, *, on_behalf: bool
) -> dict[str, Any]:  # fmt: skip
    day = await tr.today(session)
    rules = await tr.verified_rules(session, body.fy)
    d = await _decl(session, target.id, body.fy, lock=True)
    created = d is None
    if created:
        if body.expected_version != 0:
            raise AppError("VERSION_CONFLICT", "The declaration was changed by someone else; reload and retry")
        d = TaxDeclaration(user_id=target.id, fy=body.fy, version=1, updated_by=actor.id)
    elif d.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The declaration was changed by someone else; reload and retry")
    can_edit, reason = edit_state(d, rules.windows, day)
    if not can_edit:
        raise AppError("WINDOW_CLOSED", "The declaration window is closed")
    regime_changed = body.regime != d.regime_draft
    if regime_changed and _regime_is_locked(
        d, reason, body.regime, reason == "UNLOCKED" and await _unlock_allows_regime(session, d)
    ):
        raise AppError("REGIME_LOCKED", "The tax regime is fixed once the declaration window has closed")
    old_items = _items_of(d.draft)
    items = build_items(
        rules.rules, body.fy, body.regime, body.items, {i["id"] for i in old_items}, _items_of(d.submitted), old_items
    )  # fmt: skip
    if d.id is not None:
        _check_proofed_items(await _live_items(session, d.id), _items_of(d.submitted), items)
    if reason == "PROOF_WINDOW" and not _proof_window_ok(rules.rules, old_items, items):
        raise AppError("WINDOW_CLOSED", "Only items that need a proof can be changed in the proof window")
    d.regime_draft, d.draft, d.updated_by = body.regime, {"items": items}, actor.id
    if on_behalf and d.entered_by is None:
        d.entered_by = actor.id  # set once (database trigger)
    if created:
        session.add(d)
    else:
        d.version += 1
    await _flush(session)
    _decl_event(session, d, "create" if created else "update", actor.id, on_behalf=on_behalf)
    _audit(session, actor, "payroll.tax_declaration.save", d, on_behalf=on_behalf)
    if on_behalf:
        _notify_changed(session, d)
    await _commit(session)
    return await _declaration_view(session, d, rules, day, target, admin=on_behalf)


async def _declaration_view(
    session: AsyncSession, d: TaxDeclaration, rules: TaxYearRule, day: dt.date, user: User, *, admin: bool
) -> dict[str, Any]:  # fmt: skip
    counts = (await _counts(session, [d.id])).get(d.id, {})
    entered = await session.get(User, d.entered_by) if admin and d.entered_by else None
    return declaration_out(
        d,
        fy=d.fy,
        rules=rules,
        windows=rules.windows,
        day=day,
        counts=counts,
        user=user,
        entered_by=entered,
        admin=admin,
        catalogue=None if admin else await catalogue_of(session, rules),
    )


async def save_own(session: AsyncSession, actor: Principal, body: DeclarationSave) -> dict[str, Any]:
    return await _save(session, actor, actor.user, body, on_behalf=False)


async def _forbid_if_decider(session: AsyncSession, actor: Principal, user_id: uuid.UUID, fy: str) -> None:
    """L1: whoever decided on a proof of this declaration must not also enter or submit it on behalf of the employee."""
    hit = (
        await session.execute(
            select(TaxProof.id)
            .join(TaxDeclaration, TaxDeclaration.id == TaxProof.declaration_id)
            .where(TaxDeclaration.user_id == user_id, TaxDeclaration.fy == fy, TaxProof.verified_by == actor.id)
            .limit(1)
        )
    ).first()
    if hit:
        raise AppError(
            "SECOND_PERSON_REQUIRED", "The person who decided on a proof of this declaration cannot enter it on behalf"
        )


async def save_on_behalf(
    session: AsyncSession, redis: Redis, actor: Principal, user_id: uuid.UUID, body: DeclarationSave, password: Any
) -> dict[str, Any]:  # fmt: skip
    await admin_security.require_reauth(session, redis, actor, password, action="payroll.tax_declaration.save")
    await pc.forbid_self(session, actor, user_id, entity_type=ENTITY, entity_id=user_id)
    target = await _active_user(session, user_id)
    await _forbid_if_decider(session, actor, user_id, body.fy)
    return await _save(session, actor, target, body, on_behalf=True)


async def _submit(
    session: AsyncSession, actor: Principal, target: User, body: DeclarationSubmit, *, on_behalf: bool
) -> dict[str, Any]:
    day = await tr.today(session)
    rules = await tr.verified_rules(session, body.fy)
    d = await _decl(session, target.id, body.fy, lock=True)
    if d is None:
        raise AppError("NO_CHANGE", "There is nothing to submit")
    if d.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The declaration was changed by someone else; reload and retry")
    can_edit, reason = edit_state(d, rules.windows, day)
    if not can_edit:
        raise AppError("WINDOW_CLOSED", "The declaration window is closed")
    if d.regime_draft is None:
        raise _bad("regime", "choose a tax regime before submitting")
    items = _items_of(d.draft)
    prior = _items_of(d.submitted)
    if d.submitted is not None and _same(d.regime_draft, items, d.regime, prior):
        raise AppError("NO_CHANGE", "The draft is identical to the submitted declaration")
    if d.regime_draft != d.regime and (
        reason == "PROOF_WINDOW"
        or (reason == "UNLOCKED" and d.regime is not None and not await _unlock_allows_regime(session, d))
    ):
        raise AppError("REGIME_LOCKED", "The tax regime is fixed once the declaration window has closed")
    _check_proofed_items(await _live_items(session, d.id), prior, items)
    if reason == "PROOF_WINDOW":
        if not _proof_window_ok(rules.rules, prior, items):
            raise AppError("WINDOW_CLOSED", "Only items that need a proof can be changed in the proof window")
    refreshed = build_items(  # flags follow the CURRENT verified rules at the moment of submission
        rules.rules, body.fy, d.regime_draft, _as_inputs(items), {i["id"] for i in items}, prior, items
    )  # fmt: skip
    d.submitted = {"items": refreshed}
    d.regime = d.regime_draft
    d.submitted_version = (d.submitted_version or 0) + 1
    d.submitted_at = utcnow()
    d.draft = {"items": refreshed}
    d.updated_by = actor.id
    if on_behalf and d.entered_by is None:
        d.entered_by = actor.id
    d.version += 1
    await _flush(session)
    _decl_event(session, d, "submit", actor.id, on_behalf=on_behalf, submitted_version=d.submitted_version)
    _audit(session, actor, "payroll.tax_declaration.submit", d, on_behalf=on_behalf, submitted_version=d.submitted_version)
    if on_behalf:
        _notify_changed(session, d)
    await _commit(session)
    return await _declaration_view(session, d, rules, day, target, admin=on_behalf)


def _as_inputs(items: list[dict]) -> list[Any]:
    from types import SimpleNamespace

    return [
        SimpleNamespace(id=i["id"], section=i["section"], amount=i["amount"], details=dict(i.get("details") or {})) for i in items
    ]


async def submit_own(session: AsyncSession, actor: Principal, body: DeclarationSubmit) -> dict[str, Any]:
    return await _submit(session, actor, actor.user, body, on_behalf=False)


async def submit_on_behalf(
    session: AsyncSession, redis: Redis, actor: Principal, user_id: uuid.UUID, body: DeclarationSubmit, password: Any
) -> dict[str, Any]:  # fmt: skip
    await admin_security.require_reauth(session, redis, actor, password, action="payroll.tax_declaration.submit")
    await pc.forbid_self(session, actor, user_id, entity_type=ENTITY, entity_id=user_id)
    target = await _active_user(session, user_id)
    await _forbid_if_decider(session, actor, user_id, body.fy)
    return await _submit(session, actor, target, body, on_behalf=True)


async def unlock(
    session: AsyncSession, redis: Redis, actor: Principal, user_id: uuid.UUID, body: DeclarationUnlock
) -> dict[str, Any]:  # fmt: skip
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.tax_declaration.unlock")
    await pc.forbid_self(session, actor, user_id, entity_type=ENTITY, entity_id=user_id)
    if body.allow_regime_change and not actor.has("payroll.approve"):
        raise AppError("FORBIDDEN", "Only an approver can allow a regime change when unlocking")
    target = await _active_user(session, user_id)
    day = await tr.today(session)
    _first, last = tr.fy_bounds(body.fy)
    if body.until < day or body.until > last:
        raise AppError("INVALID_DATE", "The date must be today or later and inside the financial year",
                       details=[{"field": "until", "message": "must be between today and the end of the financial year"}])  # fmt: skip
    d = await _decl(session, user_id, body.fy, lock=True)
    created = d is None
    if created:
        d = TaxDeclaration(user_id=user_id, fy=body.fy, version=1, updated_by=actor.id)
    else:
        d.version += 1
    d.unlocked_until, d.unlock_reason, d.updated_by = body.until, body.reason, actor.id
    if created:
        session.add(d)
    await _flush(session)
    _decl_event(
        session,
        d,
        "unlock",
        actor.id,
        reason=body.reason,
        until=body.until.isoformat(),
        allow_regime_change=body.allow_regime_change,
        version=d.version,
    )
    _audit(session, actor, "payroll.tax_declaration.unlock", d, on_behalf=False, allow_regime_change=body.allow_regime_change)
    approvers = [u for u in await tr.holders(session, "payroll.approve") if u != actor.id][:20]
    did, fy, until, who = d.id, body.fy, body.until.isoformat(), actor.employee_id

    async def _send() -> None:
        await notifications.notify(
            session, [user_id], type="tax_declaration_changed", entity_id=did, title="Tax declaration unlocked",
            body=f"HR reopened your FY {fy} declaration until {until}.",
        )  # fmt: skip
        await notifications.notify(
            session, approvers, type="payroll_approval", entity_id=did, title="Tax declaration unlocked",
            body=f"{who} unlocked an FY {fy} declaration until {until}.", extra={"kind": "tax_declaration"},
        )  # fmt: skip

    pc.after_commit(session, _send)
    await _commit(session)
    rows = await tr.rows_of(session, body.fy)
    rules = next((r for r in rows if r.status == "verified"), None)
    windows = rules.windows if rules else (rows[0].windows if rows else None)
    counts = (await _counts(session, [d.id])).get(d.id, {})
    entered = await session.get(User, d.entered_by) if d.entered_by else None
    return declaration_out(
        d, fy=body.fy, rules=rules, windows=windows, day=day, counts=counts, user=target, entered_by=entered, admin=True
    )


# ============================================================================================ admin list
def _like(q: str) -> str:
    return "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def list_declarations(
    session: AsyncSession, actor: Principal, *, fy: str | None, status: str | None, regime: str | None, q: str | None, page: PageParams
) -> dict[str, Any]:  # fmt: skip
    fy = tr.check_fy(fy) if fy else tr.fy_of(await tr.today(session))
    cond: list[Any] = [User.deleted_at.is_(None), User.status == "active"]
    cond.append(~exists().where(EmployeePayrollDetails.user_id == User.id, EmployeePayrollDetails.include_in_payroll.is_(False)))
    if q:
        like = _like(q)
        cond.append(func.lower(User.full_name).like(func.lower(like), escape="\\") | User.employee_id.ilike(like, escape="\\"))
    join = and_(TaxDeclaration.user_id == User.id, TaxDeclaration.fy == fy)
    if status == "none":
        cond.append(TaxDeclaration.id.is_(None))
    elif status == "draft":
        cond.append(TaxDeclaration.id.is_not(None))
        cond.append(TaxDeclaration.submitted_version.is_(None))
    elif status == "submitted":
        cond.append(TaxDeclaration.submitted_version.is_not(None))
    if regime:
        cond.append(func.coalesce(TaxDeclaration.regime, TaxDeclaration.regime_draft) == regime)
    base = select(User, TaxDeclaration).outerjoin(TaxDeclaration, join).where(*cond)
    total = (
        await session.execute(select(func.count(User.id)).select_from(User).outerjoin(TaxDeclaration, join).where(*cond))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(func.lower(User.full_name), User.employee_id).offset(page.offset).limit(page.page_size)
            )
        )
        .unique()
        .all()
    )
    decl_ids = [d.id for _u, d in rows if d is not None]
    counts = await _counts(session, decl_ids)
    uids = [u.id for u, _d in rows]
    pans = {}
    if uids:
        det = (await session.execute(select(EmployeePayrollDetails).where(EmployeePayrollDetails.user_id.in_(uids)))).scalars()
        pans = {x.user_id: bool(x.pan) for x in det}
    items = []
    for u, d in rows:
        if d is None:
            st, reg, n, dirty = "none", None, 0, False
        else:
            draft_items, sub_items = _items_of(d.draft), _items_of(d.submitted)
            if d.submitted is None:
                st, reg, n, dirty = "draft", d.regime_draft, len(draft_items), bool(draft_items) or d.regime_draft is not None
            else:
                st, reg, n = "submitted", d.regime, len(sub_items)
                dirty = not _same(d.regime_draft, draft_items, d.regime, sub_items)
        c = counts.get(d.id, {}) if d else {}
        items.append(
            {
                "user": pc.user_out(u), "fy": fy, "status": st, "regime": reg, "submitted_at": pc.iso(d.submitted_at) if d else None,
                "dirty": dirty, "items": n, "proofs": {s: c.get(s, 0) for s in PROOF_STATES},
                "flags": [] if pans.get(u.id) else ["NO_PAN"],
            }
        )  # fmt: skip
    await pc.audit_read(session, actor, "payroll.tax_declaration.list", ENTITY, count=len(items))
    return make_page(items, page, total)


# ============================================================================================ proofs
async def _files_of(session: AsyncSession, ids: list[Any]) -> dict[str, File]:
    wanted = [uuid.UUID(str(i)) for i in ids]
    if not wanted:
        return {}
    return {str(f.id): f for f in (await session.execute(select(File).where(File.id.in_(wanted)))).scalars()}


def proof_out(p: TaxProof, files: dict[str, File], user: User | None = None, *, admin: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(p.id), "fy": p.fy, "item_id": p.item_id, "section": p.section, "claimed": (p.claimed or {}).get("amount"),
        "files": [{"id": str(i), "url": file_url(i), "content_type": files[str(i)].content_type if str(i) in files else None}
                  for i in p.file_ids],
        "status": p.status, "verified": (p.verified or {}).get("amount") if p.verified else None, "comment": p.comment,
        "verified_at": pc.iso(p.verified_at), "version": p.version,
    }  # fmt: skip
    if admin:
        out["user"] = pc.user_out(user)
    return out


async def _proof_out_many(session: AsyncSession, rows: list[TaxProof], *, admin: bool) -> list[dict[str, Any]]:
    files = await _files_of(session, [i for p in rows for i in p.file_ids])
    people = await tr.people(session, {p.user_id for p in rows}) if admin else {}
    return [proof_out(p, files, people.get(p.user_id), admin=admin) for p in rows]


async def create_proof(session: AsyncSession, actor: Principal, body: ProofCreate) -> dict[str, Any]:
    day = await tr.today(session)
    rules = await tr.verified_rules(session, body.fy)
    d = await _decl(session, actor.id, body.fy, lock=True)
    if d is None or d.submitted is None:
        raise AppError("ITEM_NOT_SUBMITTED", "Submit the declaration before adding proofs",
                       details=[{"field": "item_id", "message": "the item is not part of a submitted declaration"}])  # fmt: skip
    item = next((i for i in _items_of(d.submitted) if i["id"] == body.item_id), None)
    if item is None:
        raise AppError("ITEM_NOT_SUBMITTED", "The item is not part of the submitted declaration",
                       details=[{"field": "item_id", "message": "the item is not part of the submitted declaration"}])  # fmt: skip
    if not (tr.window_open(rules.windows, "proof", day) or (d.unlocked_until is not None and day <= d.unlocked_until)):
        raise AppError("WINDOW_CLOSED", "The proof window is closed")
    claimed = tr.money("claimed", body.claimed, positive=True)
    if claimed > Decimal(item["amount"]):
        raise AppError("AMOUNT_ABOVE_LIMIT", "The claimed amount is above the declared amount",
                       details=[{"field": "claimed", "message": "must not exceed the declared amount"}])  # fmt: skip
    if not _proof_required(rules.rules, item["section"]):
        raise _bad("item_id", "no proof is needed for this section")
    max_files = int(await ps.current_value(session, "payroll.tax_proof_max_files"))
    if len(body.file_ids) > max_files or len(set(body.file_ids)) != len(body.file_ids):
        raise _bad("file_ids", f"send 1 to {max_files} different files")
    files = {f.id: f for f in (await session.execute(select(File).where(File.id.in_(body.file_ids)))).scalars()}
    for i, fid in enumerate(body.file_ids):
        f = files.get(fid)
        if f is None or f.owner_id != actor.id or f.purpose != "tax_proof":
            raise _bad(f"file_ids[{i}]", "must be one of your own proof uploads")
    used = {
        str(x)
        for ids in (await session.execute(select(TaxProof.file_ids).where(TaxProof.user_id == actor.id, TaxProof.status != "cancelled"))).scalars()
        for x in ids
    }  # fmt: skip
    for i, fid in enumerate(body.file_ids):
        if str(fid) in used:
            raise _bad(f"file_ids[{i}]", "this upload is already used by a proof")
    live = (
        await session.execute(
            select(TaxProof.id)
            .where(
                TaxProof.declaration_id == d.id, TaxProof.item_id == body.item_id, TaxProof.status.in_(("submitted", "verified"))
            )
            .limit(1)
        )
    ).first()
    if live:  # the database allows one live proof per item (partial unique index); answer cleanly before it fires
        raise AppError("CONFLICT", "This item already has a proof; cancel it first")
    p = TaxProof(
        declaration_id=d.id, user_id=actor.id, fy=body.fy, item_id=body.item_id, section=item["section"],
        claimed={"amount": tr._m(claimed)}, file_ids=_ids_for(session, body.file_ids), status="submitted",
        version=1, updated_by=actor.id,
    )  # fmt: skip
    session.add(p)
    await _flush(session)
    _proof_event(session, p, "create", actor.id)
    audit(session, action="payroll.tax_proof.create", actor=actor, entity_type=PROOF_ENTITY, entity_id=p.id,
          after=_proof_audit(p, d, on_behalf=False))  # fmt: skip
    await _commit(session)
    return (await _proof_out_many(session, [p], admin=False))[0]


def _ids_for(session: AsyncSession, ids: list[uuid.UUID]) -> list[Any]:
    """uuid[] on PostgreSQL; the SQLite test harness stores file_ids as a JSON list, so there the ids are plain strings."""
    return list(ids) if session.get_bind().dialect.name == "postgresql" else [str(i) for i in ids]


def _proof_audit(p: TaxProof, d: TaxDeclaration, *, on_behalf: bool = False) -> dict[str, Any]:
    return {"fy": p.fy, "user_id": str(p.user_id), "version": p.version, "files": len(p.file_ids), "regime": d.regime,
            "on_behalf": on_behalf, "status": p.status}  # fmt: skip


def _proof_event(session: AsyncSession, p: TaxProof, action: str, actor_id: uuid.UUID, **detail: Any) -> None:
    tr.event(session, "tax_proof", p.id, p.user_id, p.status, action, actor_id, detail or None)


async def list_own_proofs(session: AsyncSession, actor: Principal, fy: str | None) -> dict[str, Any]:
    fy = tr.check_fy(fy) if fy else tr.fy_of(await tr.today(session))
    rows = list(
        (await session.execute(select(TaxProof).where(TaxProof.user_id == actor.id, TaxProof.fy == fy).order_by(TaxProof.created_at.desc(), TaxProof.id)))
        .scalars()
    )  # fmt: skip
    return {"items": await _proof_out_many(session, rows, admin=False)}


async def cancel_proof(session: AsyncSession, actor: Principal, proof_id: uuid.UUID) -> dict[str, Any]:
    p = (
        await session.execute(
            select(TaxProof).where(TaxProof.id == proof_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if p is None or p.user_id != actor.id:
        raise await pc.deny(session, actor, entity_type=PROOF_ENTITY, entity_id=proof_id, reason="not_owner", status=404)
    if p.status != "submitted":
        raise AppError("NOT_PENDING", "Only a submitted proof can be cancelled")
    p.status, p.updated_by = "cancelled", actor.id
    p.version += 1
    await _flush(session)
    _proof_event(session, p, "cancel", actor.id)
    d = await session.get(TaxDeclaration, p.declaration_id)
    audit(session, action="payroll.tax_proof.cancel", actor=actor, entity_type=PROOF_ENTITY, entity_id=p.id,
          after=_proof_audit(p, d))  # fmt: skip
    await _commit(session)
    return (await _proof_out_many(session, [p], admin=False))[0]


async def list_proofs(
    session: AsyncSession, actor: Principal, *, fy: str | None, status: str | None, user_id: uuid.UUID | None, page: PageParams
) -> dict[str, Any]:  # fmt: skip
    fy = tr.check_fy(fy) if fy else tr.fy_of(await tr.today(session))
    cond: list[Any] = [TaxProof.fy == fy]
    if status:
        cond.append(TaxProof.status == status)
    if user_id:
        cond.append(TaxProof.user_id == user_id)
    total = (await session.execute(select(func.count()).select_from(TaxProof).where(*cond))).scalar_one()
    rows = list(
        (await session.execute(select(TaxProof).where(*cond).order_by(TaxProof.created_at, TaxProof.id).offset(page.offset).limit(page.page_size)))
        .scalars()
    )  # fmt: skip
    items = await _proof_out_many(session, rows, admin=True)
    await pc.audit_read(session, actor, "payroll.tax_proof.list", PROOF_ENTITY, count=len(items))
    return make_page(items, page, total)


async def _decide_precheck(session: AsyncSession, actor: Principal, proof_id: uuid.UUID) -> tuple[TaxProof, TaxDeclaration]:
    p = (
        await session.execute(
            select(TaxProof).where(TaxProof.id == proof_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if p is None:
        raise AppError("NOT_FOUND", "Proof not found")
    if p.user_id == actor.id:
        raise await pc.deny(session, actor, entity_type=PROOF_ENTITY, entity_id=proof_id, reason="self")
    d = (
        await session.execute(
            select(TaxDeclaration).where(TaxDeclaration.id == p.declaration_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    if d.entered_by == actor.id or await _entered_on_behalf(session, d, actor.id):
        raise AppError("SECOND_PERSON_REQUIRED", "The person who entered this declaration cannot decide on its proofs")
    if p.status != "submitted":
        raise AppError("NOT_PENDING", "This proof has already been decided")
    return p, d


async def _entered_on_behalf(session: AsyncSession, d: TaxDeclaration, actor_id: uuid.UUID) -> bool:
    """The database remembers only the first person who entered a declaration on behalf; the money history remembers everybody."""
    evs = (
        await session.execute(
            select(PayrollItemEvent).where(
                PayrollItemEvent.item_type == "tax_declaration",
                PayrollItemEvent.item_id == d.id,
                PayrollItemEvent.actor_id == actor_id,
            )
        )
    ).scalars()
    return any((e.detail or {}).get("on_behalf") is True for e in evs)


def _push_decision(session: AsyncSession, p: TaxProof, title: str, rules: TaxYearRule | None) -> None:
    section = next((s["name"] for s in (rules.rules if rules else {}).get("sections", []) if s["code"] == p.section), p.section)
    pid, uid = p.id, p.user_id

    async def _send() -> None:
        await notifications.notify(session, [uid], type="tax_proof_decision", entity_id=pid, title=title, body=section)

    pc.after_commit(session, _send)


async def _decided_out(session: AsyncSession, p: TaxProof) -> dict[str, Any]:
    return (await _proof_out_many(session, [p], admin=True))[0]


async def verify_proof(session: AsyncSession, actor: Principal, proof_id: uuid.UUID, body: ProofVerify) -> dict[str, Any]:
    p, d = await _decide_precheck(session, actor, proof_id)
    if p.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The proof was changed by someone else; reload and retry")
    verified = tr.money("verified", body.verified)
    claimed = Decimal((p.claimed or {}).get("amount", "0"))
    item = next((i for i in _items_of(d.submitted) if i["id"] == p.item_id), None)  # M1: the item must still be what was proved
    if item is None or item["section"] != p.section or Decimal(item["amount"]) < claimed:
        raise AppError("CONFLICT", "The declared item no longer matches this proof; reject it so the employee can resubmit")
    if verified > claimed:
        raise AppError("AMOUNT_ABOVE_LIMIT", "The verified amount is above the claimed amount",
                       details=[{"field": "verified", "message": "must not exceed the claimed amount"}])  # fmt: skip
    if verified == 0 and not body.comment:
        raise _bad("comment", "explain why nothing was accepted")
    p.status, p.verified = "verified", {"amount": tr._m(verified)}
    p.verified_by, p.verified_at, p.comment, p.updated_by = actor.id, utcnow(), body.comment, actor.id
    p.version += 1
    await _flush(session)
    _proof_event(session, p, "verify", actor.id, **({"comment": body.comment} if body.comment else {}))
    audit(session, action="payroll.tax_proof.verify", actor=actor, entity_type=PROOF_ENTITY, entity_id=p.id,
          after={**_proof_audit(p, d), "checker_id": str(actor.id)})  # fmt: skip
    rules = (
        await session.execute(select(TaxYearRule).where(TaxYearRule.fy == p.fy, TaxYearRule.status == "verified"))
    ).scalar_one_or_none()
    _push_decision(session, p, "Investment proof verified", rules)
    await _commit(session)
    return await _decided_out(session, p)


async def reject_proof(session: AsyncSession, actor: Principal, proof_id: uuid.UUID, body: ProofReject) -> dict[str, Any]:
    p, d = await _decide_precheck(session, actor, proof_id)
    if p.version != body.expected_version:
        raise AppError("VERSION_CONFLICT", "The proof was changed by someone else; reload and retry")
    p.status, p.verified_by, p.verified_at, p.comment, p.updated_by = "rejected", actor.id, utcnow(), body.comment, actor.id
    p.version += 1
    await _flush(session)
    _proof_event(session, p, "reject", actor.id, comment=body.comment)
    audit(session, action="payroll.tax_proof.reject", actor=actor, entity_type=PROOF_ENTITY, entity_id=p.id,
          after={**_proof_audit(p, d), "checker_id": str(actor.id)})  # fmt: skip
    rules = (
        await session.execute(select(TaxYearRule).where(TaxYearRule.fy == p.fy, TaxYearRule.status == "verified"))
    ).scalar_one_or_none()
    _push_decision(session, p, "Investment proof rejected", rules)
    await _commit(session)
    return await _decided_out(session, p)


# ============================================================================================ declarations xlsx
async def declarations_xlsx(
    session: AsyncSession, redis: Redis, actor: Principal, fy: str | None, body: DeclarationsReportIn
) -> tuple[xlsx.BuiltWorkbook, str, int]:  # fmt: skip
    if body.include_identifiers:
        await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.report.export")
    fy = tr.check_fy(fy) if fy else tr.fy_of(await tr.today(session))
    q = (
        select(User, TaxDeclaration).join(TaxDeclaration, and_(TaxDeclaration.user_id == User.id, TaxDeclaration.fy == fy))
        .where(User.deleted_at.is_(None), TaxDeclaration.submitted_version.is_not(None))
        .order_by(User.employee_id).limit(MAX_REPORT_ROWS + 1)
    )  # fmt: skip
    found = (await session.execute(q)).unique().all()
    if len(found) > MAX_REPORT_ROWS:
        raise AppError("TOO_MANY_ROWS", "Too many declarations for one workbook")
    verified: dict[tuple[uuid.UUID, str], Decimal] = {}
    for p in (await session.execute(select(TaxProof).where(TaxProof.fy == fy, TaxProof.status == "verified"))).scalars():
        key = (p.declaration_id, p.item_id)
        verified[key] = verified.get(key, Decimal(0)) + Decimal((p.verified or {}).get("amount", "0"))
    depts = {d.id: d.name for d in (await session.execute(select(Department))).scalars()}
    table: list[tuple] = []
    for u, d in found:
        for it in _items_of(d.submitted):
            det = it.get("details") or {}
            pan = det.get("landlord_pan") or det.get("lender_pan")
            ident = (pan if body.include_identifiers else pc.mask_pan(pan)) if pan else None
            v = verified.get((d.id, it["id"]))
            table.append(
                (u.employee_id, u.full_name, depts.get(u.department_id), d.regime, it["section"], float(Decimal(it["amount"])),
                 float(v) if v is not None else None, ident)
            )  # fmt: skip

    async def count() -> int:
        return len(table)

    async def rows() -> AsyncIterator[tuple]:
        for r in table:
            yield r

    headers = ["Employee ID", "Name", "Department", "Regime", "Section", "Declared", "Proof verified", "Landlord / lender PAN"]
    built = await xlsx.build_workbook([xlsx.SheetSpec("Declarations", headers, count, rows, MAX_REPORT_ROWS)])
    try:
        audit(session, action="payroll.report.export", actor=actor, entity_type=ENTITY,
              after={"kind": "tax_declarations", "fy": fy, "rows": len(table), "include_identifiers": body.include_identifiers})  # fmt: skip
        await pc.commit(session)
    except BaseException:
        built.close()
        raise
    return built, f"tax-declarations-{fy}.xlsx", len(table)
