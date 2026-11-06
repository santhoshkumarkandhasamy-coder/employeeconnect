"""Tax year rules (plan 17 work package 5b, contract 19.6).

Slabs, rates, limits and the section catalogue are DATA: one versioned `tax_year_rules` row per financial year, verified by a second
person (maker-checker; the database repeats the rule with ck_tax_year_rules_verifier). The Python code holds no tax constant. Rules are
not secret, but their values are not copied into audit rows; the audit carries the FY, the version, the status and the sha256 of the
reviewed rules. `draft -> submitted -> verified -> superseded`; reject / withdraw return a submitted version to `draft` (the comment goes to
the encrypted money history). A verified or superseded row is immutable (database trigger): a correction is a new version.

Lock order (plan 17 L): `tax_year_rules` rows of one FY, in version order.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.errors import AppError
from ..core.rbac import Principal
from ..core.text import clean_text
from ..models import Permission, Role, TaxYearRule, User
from ..schemas.payroll_tax import RulesReject, RulesUpdate, RulesVerify, RulesVersionIn, WindowsIn
from . import admin_security, console, notifications
from . import payroll_common as pc
from . import payroll_components as comps
from .payroll_calc import CODE_RE

ENTITY = "tax_year_rules"
ANNUAL_CAP = Decimal("120000000.00")  # annual tax figures (contract 19.0)
MONTHLY_CAP = Decimal("10000000.00")
MAX_SECTIONS = 40
MAX_SLABS = 20
_MONEY = re.compile(r"^\d{1,9}(\.\d{1,2})?$")
_RATE = re.compile(r"^\d{1,3}(\.\d{1,2})?$")
_SECTION_CODE = re.compile(r"^[A-Z0-9_]{2,16}$")
_GROUP_CODE = re.compile(r"^[A-Z0-9_]{2,24}$")
_LABEL_KEY = re.compile(r"^[a-z0-9_]{1,24}$")
_UNVERIFIED = re.compile(r"^\s*UNVERIFIED", re.IGNORECASE)
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PAN_HOLDER_TYPES = "ABCFGHJLPT"  # 4th character of a real PAN: the holder type


def valid_pan(p: str) -> bool:
    """A PAN-shaped value with a real holder type. The masked form (`XXXXX1234X`) is never a PAN (H1)."""
    return bool(PAN_RE.match(p)) and not p.startswith("XXXXX") and p[3] in PAN_HOLDER_TYPES


MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
# declaration detail fields a section may ask for, and their kind (the declaration service validates the values by kind)
SECTION_FIELDS: dict[str, str] = {
    "description": "text", "senior": "bool", "lender_name": "text", "lender_pan": "pan", "monthly_rent": "money",
    "from_month": "month", "to_month": "month", "landlord_name": "text", "landlord_pan": "pan", "city": "text",
    "metro": "bool", "income": "money", "tds": "money", "pt": "money", "pf": "money",
}  # fmt: skip
WINDOW_KEYS = ("declaration_opens_on", "declaration_closes_on", "proof_opens_on", "proof_closes_on")


# ============================================================================================ time and FY
async def today(session: AsyncSession) -> dt.date:
    """Office-timezone date (a seam the tests patch)."""
    return await console.office_today(session)


def fy_label(start_year: int) -> str:
    return f"{start_year:04d}-{(start_year + 1) % 100:02d}"


def fy_of(day: dt.date) -> str:
    return fy_label(day.year if day.month >= 4 else day.year - 1)


def fy_bounds(fy: str) -> tuple[dt.date, dt.date]:
    y = int(fy[:4])
    return dt.date(y, 4, 1), dt.date(y + 1, 3, 31)


def check_fy(fy: str) -> str:
    """Raises 422 for anything that is not `YYYY-YY` with consecutive years."""
    if isinstance(fy, str) and re.fullmatch(r"\d{4}-\d{2}", fy):
        y = int(fy[:4])
        if 2000 <= y <= 2200 and int(fy[5:]) == (y + 1) % 100:
            return fy
    raise AppError("VALIDATION_ERROR", "fy must be a financial year such as 2026-27",
                   details=[{"field": "fy", "message": "must be a financial year such as 2026-27"}])  # fmt: skip


def month_in_fy(fy: str, month: str) -> bool:
    m = MONTH_RE.match(month)
    if not m:
        return False
    first, last = fy_bounds(fy)
    return first <= dt.date(int(m.group(1)), int(m.group(2)), 1) <= last


# ============================================================================================ rules validation
def _bad(path: str, msg: str) -> AppError:
    return AppError("VALIDATION_ERROR", "The tax rules are not valid", details=[{"field": path, "message": msg}])


def _obj(path: str, v: Any, req: set[str], opt: set[str] = frozenset()) -> dict:  # type: ignore[assignment]
    if not isinstance(v, dict):
        raise _bad(path, "must be an object")
    for k in v:
        if k not in req and k not in opt:
            raise _bad(f"{path}.{k}", "unknown field")
    for k in sorted(req):
        if k not in v:
            raise _bad(f"{path}.{k}", "is required")
    return v


def _list(path: str, v: Any, max_len: int, min_len: int = 0) -> list:
    if not isinstance(v, list) or not min_len <= len(v) <= max_len:
        raise _bad(path, f"must be a list of {min_len} to {max_len} entries")
    return v


def _bool(path: str, v: Any) -> bool:
    if not isinstance(v, bool):
        raise _bad(path, "must be true or false")
    return v


def money(path: str, v: Any, *, cap: Decimal = ANNUAL_CAP, positive: bool = False) -> Decimal:
    """A string with at most 2 decimals, 0.00..cap (0.01..cap when `positive`). Messages never contain the submitted value."""
    if not isinstance(v, str) or not _MONEY.match(v.strip()):
        raise _bad(path, 'must be an amount as a string such as "25000.00"')
    d = Decimal(v.strip())
    if d > cap or (positive and d <= 0):
        raise _bad(path, f"must be between {'0.01' if positive else '0.00'} and {cap:.2f}")
    return d.quantize(Decimal("0.01"))


def _m(d: Decimal) -> str:
    return f"{d:.2f}"


def _rate(path: str, v: Any, top: int = 50) -> str:
    if not isinstance(v, str) or not _RATE.match(v.strip()):
        raise _bad(path, 'must be a percentage as a string with at most 2 decimals, such as "7.5"')
    if Decimal(v.strip()) > top:
        raise _bad(path, f"must be between 0 and {top}")
    return v.strip()


def _int(path: str, v: Any, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise _bad(path, f"must be a whole number between {lo} and {hi}")
    return v


def _text(path: str, v: Any, lo: int, hi: int) -> str:
    if not isinstance(v, str):
        raise _bad(path, "must be text")
    c = clean_text(v)
    if not lo <= len(c) <= hi:
        raise _bad(path, f"must be {lo}-{hi} characters")
    return c


def _slabs(path: str, v: Any) -> list[dict]:
    out: list[dict] = []
    prev = Decimal(0)
    items = _list(path, v, MAX_SLABS, 1)
    for i, s in enumerate(items):
        p = f"{path}[{i}]"
        o = _obj(p, s, {"from", "to", "rate_pct"})
        frm = money(f"{p}.from", o["from"])
        if frm != prev:
            raise _bad(f"{p}.from", "must be 0.00 for the first slab and the end of the previous slab for the others")
        last = i == len(items) - 1
        if o["to"] is None:
            if not last:
                raise _bad(f"{p}.to", "only the last slab is open-ended")
            to = None
        else:
            if last:
                raise _bad(f"{p}.to", "the last slab must be open-ended (null)")
            t = money(f"{p}.to", o["to"])
            if t <= frm:
                raise _bad(f"{p}.to", "must be greater than from")
            to, prev = _m(t), t
        out.append({"from": _m(frm), "to": to, "rate_pct": _rate(f"{p}.rate_pct", o["rate_pct"])})
    return out


def _regime(path: str, v: Any, codes: set[str]) -> dict:
    o = _obj(
        path, v,
        {"slabs", "standard_deduction", "rebate", "surcharge", "surcharge_marginal_relief", "pt_deductible", "hra_exemption", "allowed_sections"},
    )  # fmt: skip
    rb = _obj(f"{path}.rebate", o["rebate"], {"income_limit", "max", "marginal_relief"})
    sur: list[dict] = []
    prev = Decimal(-1)
    for i, s in enumerate(_list(f"{path}.surcharge", o["surcharge"], 10)):
        p = f"{path}.surcharge[{i}]"
        so = _obj(p, s, {"above", "rate_pct"})
        above = money(f"{p}.above", so["above"])
        if above <= prev:
            raise _bad(f"{p}.above", "must be greater than the previous threshold")
        prev = above
        sur.append({"above": _m(above), "rate_pct": _rate(f"{p}.rate_pct", so["rate_pct"])})
    allowed: list[str] = []
    for i, c in enumerate(_list(f"{path}.allowed_sections", o["allowed_sections"], MAX_SECTIONS)):
        if not isinstance(c, str) or c not in codes:
            raise _bad(f"{path}.allowed_sections[{i}]", "must be the code of a section in rules.sections")
        if c in allowed:
            raise _bad(f"{path}.allowed_sections[{i}]", "duplicate section")
        allowed.append(c)
    return {
        "slabs": _slabs(f"{path}.slabs", o["slabs"]),
        "standard_deduction": _m(money(f"{path}.standard_deduction", o["standard_deduction"])),
        "rebate": {
            "income_limit": _m(money(f"{path}.rebate.income_limit", rb["income_limit"])),
            "max": _m(money(f"{path}.rebate.max", rb["max"])),
            "marginal_relief": _bool(f"{path}.rebate.marginal_relief", rb["marginal_relief"]),
        },
        "surcharge": sur,
        "surcharge_marginal_relief": _bool(f"{path}.surcharge_marginal_relief", o["surcharge_marginal_relief"]),
        "pt_deductible": _bool(f"{path}.pt_deductible", o["pt_deductible"]),
        "hra_exemption": _bool(f"{path}.hra_exemption", o["hra_exemption"]),
        "allowed_sections": allowed,
    }


def _sections(path: str, v: Any) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for i, s in enumerate(_list(path, v, MAX_SECTIONS, 1)):
        p = f"{path}[{i}]"
        o = _obj(p, s, {"code", "name", "limit", "group", "proof_required", "fields"}, {"senior_limit", "landlord_pan_above"})
        code = o["code"]
        if not isinstance(code, str) or not _SECTION_CODE.match(code):
            raise _bad(f"{p}.code", "must match ^[A-Z0-9_]{2,16}$")
        if code in seen:
            raise _bad(f"{p}.code", "duplicate section code")
        seen.add(code)
        group = o["group"]
        if group is not None and (not isinstance(group, str) or not _GROUP_CODE.match(group)):
            raise _bad(f"{p}.group", "must be null or match ^[A-Z0-9_]{2,24}$")
        fields: list[str] = []
        for j, f in enumerate(_list(f"{p}.fields", o["fields"], len(SECTION_FIELDS))):
            if not isinstance(f, str) or f not in SECTION_FIELDS:
                raise _bad(f"{p}.fields[{j}]", "unknown declaration field")
            if f in fields:
                raise _bad(f"{p}.fields[{j}]", "duplicate field")
            fields.append(f)
        sec = {
            "code": code,
            "name": _text(f"{p}.name", o["name"], 1, 120),
            "limit": None if o["limit"] is None else _m(money(f"{p}.limit", o["limit"], positive=True)),
            "group": group,
            "proof_required": _bool(f"{p}.proof_required", o["proof_required"]),
            "fields": fields,
        }
        if "senior_limit" in o:
            sec["senior_limit"] = _m(money(f"{p}.senior_limit", o["senior_limit"], positive=True))
        if "landlord_pan_above" in o:
            sec["landlord_pan_above"] = _m(money(f"{p}.landlord_pan_above", o["landlord_pan_above"]))
        out.append(sec)
    return out


def validate_rules(rules: Any) -> dict:
    """Schema validation (contract 19.6). Returns the normalised rules; every error is 422 `VALIDATION_ERROR` with the JSON path in
    `details.field` (`rules.regimes.new.slabs[2].to`)."""
    path = "rules"
    o = _obj(
        path, rules,
        {"default_regime", "regimes", "cess_pct", "no_pan_min_rate_pct", "round_income_to", "round_tax_to", "sections", "component_treatment"},
        {"note", "labels", "loan_perquisite_threshold", "group_limits", "hra"},
    )  # fmt: skip
    out: dict[str, Any] = {}
    if "note" in o and o["note"] is not None:
        out["note"] = _text("rules.note", o["note"], 0, 300)
    if "labels" in o:
        lab = o["labels"]
        if not isinstance(lab, dict) or len(lab) > 10:
            raise _bad("rules.labels", "must be an object of at most 10 labels")
        out["labels"] = {}
        for k, val in lab.items():
            if not _LABEL_KEY.match(str(k)):
                raise _bad(f"rules.labels.{k}", "invalid label key")
            out["labels"][k] = _text(f"rules.labels.{k}", val, 1, 80)
    if o["default_regime"] not in ("old", "new"):
        raise _bad("rules.default_regime", "must be old or new")
    out["default_regime"] = o["default_regime"]
    sections = _sections("rules.sections", o["sections"])
    codes = {s["code"] for s in sections}
    regs = _obj("rules.regimes", o["regimes"], {"old", "new"})
    out["regimes"] = {k: _regime(f"rules.regimes.{k}", regs[k], codes) for k in ("new", "old")}
    out["cess_pct"] = _rate("rules.cess_pct", o["cess_pct"])
    out["no_pan_min_rate_pct"] = _rate("rules.no_pan_min_rate_pct", o["no_pan_min_rate_pct"])
    out["round_income_to"] = _int("rules.round_income_to", o["round_income_to"], 1, 1000)
    out["round_tax_to"] = _int("rules.round_tax_to", o["round_tax_to"], 1, 1000)
    if "loan_perquisite_threshold" in o:
        out["loan_perquisite_threshold"] = _m(money("rules.loan_perquisite_threshold", o["loan_perquisite_threshold"]))
    out["sections"] = sections
    groups = {s["group"] for s in sections if s["group"]}
    gl: list[dict] = []
    for i, g in enumerate(_list("rules.group_limits", o.get("group_limits", []), MAX_SECTIONS)):
        p = f"rules.group_limits[{i}]"
        go = _obj(p, g, {"group", "limit"})
        if not isinstance(go["group"], str) or go["group"] not in groups:
            raise _bad(f"{p}.group", "must be a group used by a section in rules.sections")
        if any(x["group"] == go["group"] for x in gl):
            raise _bad(f"{p}.group", "duplicate group")
        gl.append({"group": go["group"], "limit": _m(money(f"{p}.limit", go["limit"], positive=True))})
    out["group_limits"] = gl
    if "hra" in o:
        h = _obj("rules.hra", o["hra"], {"metro_pct", "non_metro_pct", "rent_minus_basic_pct", "metro_cities"})
        cities = [
            _text(f"rules.hra.metro_cities[{i}]", c, 1, 40)
            for i, c in enumerate(_list("rules.hra.metro_cities", h["metro_cities"], 50))
        ]
        out["hra"] = {
            "metro_pct": _rate("rules.hra.metro_pct", h["metro_pct"], 100),
            "non_metro_pct": _rate("rules.hra.non_metro_pct", h["non_metro_pct"], 100),
            "rent_minus_basic_pct": _rate("rules.hra.rent_minus_basic_pct", h["rent_minus_basic_pct"], 100),
            "metro_cities": cities,
        }
    elif any(r["hra_exemption"] for r in out["regimes"].values()):
        raise _bad("rules.hra", "is required when a regime has an HRA exemption")
    ct = o["component_treatment"]
    if not isinstance(ct, dict) or "default" not in ct or len(ct) > 61:
        raise _bad("rules.component_treatment", "must be an object with a `default` entry")
    out["component_treatment"] = {}
    for k, val in ct.items():
        if k != "default" and not (isinstance(k, str) and CODE_RE.match(k)):
            raise _bad(f"rules.component_treatment.{k}", "keys are payslip component codes")
        if val not in ("taxable", "exempt"):
            raise _bad(f"rules.component_treatment.{k}", "must be taxable or exempt")
        out["component_treatment"][k] = val
    return out


def rules_digest(rules: dict) -> str:
    """sha256 of json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=False) (the form migration 0028 uses)."""
    return hashlib.sha256(
        json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def has_unverified_note(rules: dict) -> bool:
    note = rules.get("note")
    return isinstance(note, str) and bool(_UNVERIFIED.match(note))


# ============================================================================================ windows
def parse_windows(raw: dict | None) -> dict[str, dt.date | None]:
    out: dict[str, dt.date | None] = {}
    for k in WINDOW_KEYS:
        v = (raw or {}).get(k)
        try:
            out[k] = dt.date.fromisoformat(v) if isinstance(v, str) else None
        except ValueError:
            out[k] = None
    return out


def windows_out(raw: dict | None) -> dict[str, str | None]:
    return {k: (d.isoformat() if d else None) for k, d in parse_windows(raw).items()}


def window_open(raw: dict | None, kind: str, day: dt.date) -> bool:
    w = parse_windows(raw)
    lo, hi = w[f"{kind}_opens_on"], w[f"{kind}_closes_on"]
    return lo is not None and hi is not None and lo <= day <= hi


# ============================================================================================ loading
def _people_ids(rows: list[TaxYearRule]) -> set[uuid.UUID]:
    ids: set[uuid.UUID] = set()
    for r in rows:
        ids |= {x for x in (r.created_by, r.last_edited_by, r.submitted_by, r.verified_by) if x}
    return ids


async def people(session: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, User]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return {u.id: u for u in (await session.execute(select(User).where(User.id.in_(wanted)))).unique().scalars()}


def rules_out(r: TaxYearRule, ppl: dict[uuid.UUID, User]) -> dict[str, Any]:
    rules = r.rules or {}
    return {
        "id": str(r.id),
        "fy": r.fy,
        "version": r.version,
        "status": r.status,
        "rules_sha256": r.rules_sha256,
        "note": rules.get("note"),
        "rules": rules,
        "windows": windows_out(r.windows),
        "attestation": r.attestation,
        "created_by": pc.user_out(ppl.get(r.created_by)) if r.created_by else None,
        "last_edited_by": pc.user_out(ppl.get(r.last_edited_by)) if r.last_edited_by else None,
        "submitted_by": pc.user_out(ppl.get(r.submitted_by)) if r.submitted_by else None,
        "submitted_at": pc.iso(r.submitted_at),
        "verified_by": pc.user_out(ppl.get(r.verified_by)) if r.verified_by else None,
        "verified_at": pc.iso(r.verified_at),
        "version_row": r.version_row,
    }


async def _out(session: AsyncSession, r: TaxYearRule) -> dict[str, Any]:
    return rules_out(r, await people(session, _people_ids([r])))


async def _get(session: AsyncSession, rules_id: uuid.UUID, *, lock: bool = False) -> TaxYearRule:
    q = select(TaxYearRule).where(TaxYearRule.id == rules_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    r = (await session.execute(q)).scalar_one_or_none()
    if r is None:
        raise AppError("NOT_FOUND", "Tax rules not found")
    return r


async def rows_of(session: AsyncSession, fy: str, *, lock: bool = False) -> list[TaxYearRule]:
    q = select(TaxYearRule).where(TaxYearRule.fy == fy).order_by(TaxYearRule.version.desc())
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return list((await session.execute(q)).scalars())


async def verified_rules(session: AsyncSession, fy: str) -> TaxYearRule:
    """The verified version of the FY or 409 `TAX_RULES_NOT_VERIFIED` (T2: nothing else is ever shown to an employee)."""
    r = (
        await session.execute(select(TaxYearRule).where(TaxYearRule.fy == fy, TaxYearRule.status == "verified"))
    ).scalar_one_or_none()
    if r is None:
        raise AppError("TAX_RULES_NOT_VERIFIED", "The tax rules for this financial year have not been verified yet")
    return r


async def windows_of(session: AsyncSession, fy: str) -> dict | None:
    """Windows of the FY: the verified row, else the newest row (windows are kept equal on every row of the FY)."""
    rows = await rows_of(session, fy)
    for r in rows:
        if r.status == "verified":
            return r.windows
    return rows[0].windows if rows else None


# ============================================================================================ audit / history / push
def _audit(session: AsyncSession, actor: Principal, action: str, r: TaxYearRule, *, maker: uuid.UUID | None = None,
           checker: uuid.UUID | None = None, **extra: Any) -> None:  # fmt: skip
    audit(
        session, action=action, actor=actor, entity_type=ENTITY, entity_id=r.id,
        after={"fy": r.fy, "version": r.version, "status": r.status, "rules_sha256": r.rules_sha256,
               "maker_id": str(maker) if maker else None, "checker_id": str(checker) if checker else None, **extra},
    )  # fmt: skip


def event(session: AsyncSession, item_type: str, item_id: uuid.UUID, user_id: uuid.UUID, status: str, action: str,
          actor_id: uuid.UUID | None, detail: dict | None = None) -> None:  # fmt: skip
    """Money history row (append-only, encrypted detail: reasons / comments live here, never in the audit log)."""
    comps.add_event(
        session, item_type=item_type, item=SimpleNamespace(id=item_id, user_id=user_id, status=status, payslip_id=None),
        action=action, actor_id=actor_id, detail=detail,
    )  # fmt: skip


def _event(session: AsyncSession, r: TaxYearRule, action: str, actor_id: uuid.UUID, detail: dict | None = None) -> None:
    event(session, "tax_rules", r.id, actor_id, r.status, action, actor_id, detail)


async def holders(session: AsyncSession, code: str) -> list[uuid.UUID]:
    q = (
        select(User.id).join(Role, Role.id == User.role_id)
        .where(User.status == "active", User.deleted_at.is_(None), Role.permissions.any(Permission.code == code))
        .order_by(User.id)
    )  # fmt: skip
    return list((await session.execute(q)).unique().scalars())


def _queue_push(session: AsyncSession, user_ids: list[uuid.UUID], rules_id: uuid.UUID, title: str, body: str) -> None:
    async def _send() -> None:
        await notifications.notify(
            session, user_ids, type="payroll_approval", entity_id=rules_id, title=title, body=body, extra={"kind": "tax_rules"}
        )

    if user_ids:
        pc.after_commit(session, _send)


def _map_db(exc: IntegrityError) -> AppError:
    text_ = str(getattr(exc, "orig", exc))
    if "ck_tax_year_rules_verifier" in text_:
        return AppError("SECOND_PERSON_REQUIRED", "A different person must verify these rules")
    if pc.sqlstate(exc) == "23505" and "uq_tax_year_rules_open" in text_:
        return AppError("DRAFT_EXISTS", "A draft or submitted version already exists for this financial year")
    if pc.sqlstate(exc) == "23505" and "uq_tax_year_rules_verified" in text_:
        return AppError("CONFLICT", "The rules were verified concurrently; reload and retry")
    return pc.map_integrity(exc)


async def _commit(session: AsyncSession) -> None:
    try:
        await pc.commit(session)
    except AppError:
        raise
    except IntegrityError as exc:  # pragma: no cover - pc.commit maps integrity errors itself
        await session.rollback()
        raise _map_db(exc) from None


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _map_db(exc) from None


def _check_version(r: TaxYearRule, version_row: int) -> None:
    if r.version_row != version_row:
        raise AppError("VERSION_CONFLICT", "The rules were changed by someone else; reload and retry")


# ============================================================================================ reads
async def list_years(session: AsyncSession) -> dict[str, Any]:
    rows = list(
        (await session.execute(select(TaxYearRule).order_by(TaxYearRule.fy.desc(), TaxYearRule.version.desc()))).scalars()
    )
    cur = fy_of(await today(session))
    by_fy: dict[str, list[TaxYearRule]] = {}
    for r in rows:
        by_fy.setdefault(r.fy, []).append(r)
    by_fy.setdefault(cur, [])
    items = []
    for fy in sorted(by_fy, reverse=True):
        group = by_fy[fy]
        verified = next((r for r in group if r.status == "verified"), None)
        open_ = next((r for r in group if r.status in ("draft", "submitted")), None)
        items.append(
            {
                "fy": fy,
                "verified_version": verified.version if verified else None,
                "open_version": {"id": str(open_.id), "version": open_.version, "status": open_.status} if open_ else None,
                "windows": windows_out((verified or (group[0] if group else None)).windows if group else None),
                "current": fy == cur,
            }
        )
    return {"items": items}


async def list_versions(session: AsyncSession, fy: str) -> dict[str, Any]:
    check_fy(fy)
    rows = await rows_of(session, fy)
    ppl = await people(session, _people_ids(rows))
    return {"items": [rules_out(r, ppl) for r in rows]}


async def get_rules(session: AsyncSession, rules_id: uuid.UUID) -> dict[str, Any]:
    return await _out(session, await _get(session, rules_id))


# ============================================================================================ maker actions
async def create_draft(session: AsyncSession, actor: Principal, fy: str) -> dict[str, Any]:
    check_fy(fy)
    rows = await rows_of(session, fy, lock=True)
    if any(r.status in ("draft", "submitted") for r in rows):
        raise AppError("DRAFT_EXISTS", "A draft or submitted version already exists for this financial year")
    if rows:
        src, rules, windows = rows[0], json.loads(json.dumps(rows[0].rules)), dict(rows[0].windows or {})
    else:  # a new financial year starts from the newest version of the latest earlier year
        earlier = (
            await session.execute(
                select(TaxYearRule)
                .where(TaxYearRule.fy < fy)
                .order_by(TaxYearRule.fy.desc(), TaxYearRule.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if earlier is None:
            raise AppError("NOT_FOUND", "There are no tax rules to copy for this financial year")
        rules, windows = json.loads(json.dumps(earlier.rules)), {}
        rules["note"] = f"UNVERIFIED copy of FY {earlier.fy}; review every value before verification"
        src = None
    r = TaxYearRule(
        fy=fy, version=(src.version + 1) if src else 1, status="draft", rules=rules, rules_sha256=rules_digest(rules), windows=windows,
        created_by=actor.id, last_edited_by=actor.id, contributors=[str(actor.id)], version_row=1, updated_by=actor.id,
    )  # fmt: skip
    session.add(r)
    await _flush(session)
    _event(session, r, "create", actor.id)
    _audit(session, actor, "payroll.tax_rules.create", r, maker=actor.id)
    await _commit(session)
    return await _out(session, r)


async def update_rules(session: AsyncSession, actor: Principal, rules_id: uuid.UUID, body: RulesUpdate) -> dict[str, Any]:
    r = await _get(session, rules_id, lock=True)
    if r.status != "draft":
        raise AppError("NOT_DRAFT", "Only a draft can be edited")
    _check_version(r, body.version_row)
    rules = dict(body.rules)
    if body.note is not None:
        if body.note:
            rules["note"] = body.note
        else:
            rules.pop("note", None)
    rules = validate_rules(rules)
    r.rules, r.rules_sha256 = rules, rules_digest(rules)
    r.last_edited_by, r.updated_by = actor.id, actor.id
    pc.touch(r, actor.id)
    r.version_row += 1
    await _flush(session)
    _event(session, r, "update", actor.id)
    _audit(session, actor, "payroll.tax_rules.update", r, maker=actor.id)
    await _commit(session)
    return await _out(session, r)


async def submit(session: AsyncSession, actor: Principal, rules_id: uuid.UUID, body: RulesVersionIn) -> dict[str, Any]:
    r = await _get(session, rules_id, lock=True)
    if r.status != "draft":
        raise AppError("NOT_DRAFT", "Only a draft can be submitted")
    _check_version(r, body.version_row)
    if has_unverified_note(r.rules):  # the seed note must be removed by an editor who actually reviewed the values
        raise _bad("rules.note", "remove the UNVERIFIED note after reviewing the rules, then submit")
    validate_rules(r.rules)
    r.status, r.submitted_by, r.submitted_at, r.updated_by = "submitted", actor.id, utcnow(), actor.id
    pc.touch(r, actor.id)
    r.version_row += 1
    await _flush(session)
    _event(session, r, "submit", actor.id)
    _audit(session, actor, "payroll.tax_rules.submit", r, maker=actor.id)
    parties = _parties(r) | {actor.id}
    _queue_push(
        session,
        [u for u in await holders(session, "payroll.approve") if u not in parties][:20],
        r.id,
        "Approval needed",
        f"Tax rules FY {r.fy} v{r.version} submitted for verification",
    )
    await _commit(session)
    return await _out(session, r)


async def withdraw(session: AsyncSession, actor: Principal, rules_id: uuid.UUID, body: RulesVersionIn) -> dict[str, Any]:
    r = await _get(session, rules_id, lock=True)
    if r.status != "submitted":
        raise AppError("NOT_SUBMITTED", "Only a submitted version can be withdrawn")
    if actor.id != r.submitted_by and not actor.has("payroll.approve"):  # L4
        raise AppError("FORBIDDEN", "Only the person who submitted these rules, or an approver, can withdraw them")
    _check_version(r, body.version_row)
    maker = r.submitted_by
    r.status, r.submitted_by, r.submitted_at, r.updated_by = "draft", None, None, actor.id
    if actor.id == maker:  # an approver who withdraws must stay free to verify the next submission
        pc.touch(r, actor.id)
    r.version_row += 1
    await _flush(session)
    _event(session, r, "withdraw", actor.id)
    _audit(session, actor, "payroll.tax_rules.withdraw", r, maker=maker)
    await _commit(session)
    return await _out(session, r)


# ============================================================================================ checker actions
def _parties(r: TaxYearRule) -> set[uuid.UUID]:
    out = {x for x in (r.created_by, r.last_edited_by, r.submitted_by) if x}
    for c in r.contributors or []:
        try:
            out.add(uuid.UUID(str(c)))
        except ValueError:
            continue
    return out


def _second_person(r: TaxYearRule, actor: Principal) -> None:
    if actor.id in _parties(r):
        raise AppError("SECOND_PERSON_REQUIRED", "A different person must decide on these rules")


async def verify(session: AsyncSession, redis: Redis, actor: Principal, rules_id: uuid.UUID, body: RulesVerify) -> dict[str, Any]:
    await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.tax_rules.verify")
    r = await _get(session, rules_id, lock=True)
    if r.status != "submitted":
        raise AppError("NOT_SUBMITTED", "Only a submitted version can be verified")
    _second_person(r, actor)
    _check_version(r, body.version_row)
    if body.rules_sha256 != r.rules_sha256 or rules_digest(r.rules) != r.rules_sha256:
        raise AppError("RULES_CHANGED", "The rules differ from the ones you reviewed; reload and review again")
    validate_rules(r.rules)
    if has_unverified_note(r.rules):
        raise _bad("rules.note", "the UNVERIFIED note must be removed before the rules can be verified")
    previous = (
        await session.execute(select(TaxYearRule).where(TaxYearRule.fy == r.fy, TaxYearRule.status == "verified").with_for_update())
    ).scalars().all()  # fmt: skip
    for p in previous:
        p.status, p.updated_by = "superseded", actor.id
    if previous:
        await _flush(session)  # frees the one-verified-row-per-FY unique index before the new row takes it
    maker = r.submitted_by
    r.status, r.verified_by, r.verified_at, r.attestation, r.updated_by = (
        "verified",
        actor.id,
        utcnow(),
        body.attestation,
        actor.id,
    )
    r.version_row += 1
    await _flush(session)
    _event(session, r, "verify", actor.id)
    for p in previous:
        _event(session, p, "update", actor.id, {"superseded_by": str(r.id)})
    _audit(session, actor, "payroll.tax_rules.verify", r, maker=maker, checker=actor.id)
    if maker:
        _queue_push(session, [maker], r.id, "Tax rules verified", f"Tax rules FY {r.fy} v{r.version} were verified")
    await _commit(session)
    return await _out(session, r)


async def reject(session: AsyncSession, actor: Principal, rules_id: uuid.UUID, body: RulesReject) -> dict[str, Any]:
    r = await _get(session, rules_id, lock=True)
    if r.status != "submitted":
        raise AppError("NOT_SUBMITTED", "Only a submitted version can be rejected")
    _second_person(r, actor)
    _check_version(r, body.version_row)
    maker = r.submitted_by
    r.status, r.submitted_by, r.submitted_at, r.updated_by = "draft", None, None, actor.id
    r.version_row += 1
    await _flush(session)
    _event(session, r, "reject", actor.id, {"comment": body.comment})
    _audit(session, actor, "payroll.tax_rules.reject", r, maker=maker, checker=actor.id)
    if maker:
        _queue_push(session, [maker], r.id, "Tax rules rejected", f"Tax rules FY {r.fy} v{r.version} were sent back")
    await _commit(session)
    return await _out(session, r)


# ============================================================================================ windows
def validate_windows(fy: str, body: WindowsIn) -> dict[str, str | None]:
    first, last = fy_bounds(fy)
    out: dict[str, str | None] = {}
    for kind in ("declaration", "proof"):
        lo, hi = getattr(body, f"{kind}_opens_on"), getattr(body, f"{kind}_closes_on")
        if (lo is None) != (hi is None):
            raise AppError("INVALID_DATE", "Send both dates of a window or neither",
                           details=[{"field": f"{kind}_opens_on", "message": "send both dates of a window or neither"}])  # fmt: skip
        if lo is not None and hi is not None:
            for name, d in ((f"{kind}_opens_on", lo), (f"{kind}_closes_on", hi)):
                if not first <= d <= last:
                    raise AppError("INVALID_DATE", "The date is outside the financial year",
                                   details=[{"field": name, "message": "must be inside the financial year"}])  # fmt: skip
            if lo > hi:
                raise AppError("INVALID_DATE", "A window cannot close before it opens",
                               details=[{"field": f"{kind}_closes_on", "message": "must not be before the opening date"}])  # fmt: skip
        out[f"{kind}_opens_on"] = lo.isoformat() if lo else None
        out[f"{kind}_closes_on"] = hi.isoformat() if hi else None
    return out


def _reopens(before: dict, new: dict, day: dt.date) -> bool:
    """M3: a window whose closing date has passed is reopened, or its closing date is moved later."""
    for kind in ("declaration", "proof"):
        old_close = before.get(f"{kind}_closes_on")
        new_close = new.get(f"{kind}_closes_on")
        if old_close and new_close and dt.date.fromisoformat(old_close) < day and new_close > old_close:
            return True
    return False


async def set_windows(
    session: AsyncSession, redis: Redis, actor: Principal, fy: str, body: WindowsIn
) -> dict[str, Any]:  # fmt: skip
    check_fy(fy)
    new = validate_windows(fy, body)
    rows = await rows_of(session, fy, lock=True)
    if not rows:
        raise AppError("NOT_FOUND", "There are no tax rules for this financial year")
    day = await today(session)
    before = windows_out(rows[0].windows)
    reopened = _reopens(before, new, day)
    if reopened:
        await admin_security.require_reauth(session, redis, actor, body.current_password, action="payroll.tax_rules.windows")
    opened = [
        kind for kind in ("declaration", "proof")
        if window_open(new, kind, day) and not window_open(before, kind, day)
    ]  # fmt: skip
    await session.execute(update(TaxYearRule).where(TaxYearRule.fy == fy).values(windows=new, updated_by=actor.id))
    for r in rows:
        await session.refresh(r)
    audit(session, action="payroll.tax_rules.windows", actor=actor, entity_type=ENTITY, entity_id=rows[0].id,
          after={"fy": fy, "version": rows[0].version, "status": rows[0].status, "windows": new, "reopened": reopened})  # fmt: skip
    _event(session, rows[0], "update", actor.id, {"windows": new})
    if reopened:
        entity, who = rows[0].id, actor.employee_id
        pc.after_commit(
            session,
            lambda: admin_security.notify_admins(
                session, entity_id=entity, title="Tax window reopened",
                body=f"A closed tax window of FY {fy} was reopened or extended by {who}.",
            ),
        )  # fmt: skip
    if opened:
        kind = "declaration" if "declaration" in opened else "proof"
        pc.after_commit(session, lambda: _push_window(session, fy, kind, new))
    await _commit(session)
    return {"fy": fy, "windows": new}


async def _push_window(session: AsyncSession, fy: str, kind: str, windows: dict) -> None:
    """`tax_window` to every active employee with payroll.view_own that is paid through payroll (best effort, after commit)."""
    from ..models import EmployeePayrollDetails

    out_of_payroll = exists().where(
        EmployeePayrollDetails.user_id == User.id, EmployeePayrollDetails.include_in_payroll.is_(False)
    )
    q = (
        select(User.id).join(Role, Role.id == User.role_id)
        .where(User.status == "active", User.deleted_at.is_(None), ~out_of_payroll, Role.permissions.any(Permission.code == "payroll.view_own"))
    )  # fmt: skip
    ids = list((await session.execute(q)).unique().scalars())
    title = "Investment declaration open" if kind == "declaration" else "Proof submission open"
    closes = windows.get(f"{kind}_closes_on")
    await notifications.notify(
        session,
        ids,
        type="tax_window",
        entity_id=fy,
        title=title,
        body=f"FY {fy}, until {closes}",
        extra={"fy": fy, "window": kind},
    )
