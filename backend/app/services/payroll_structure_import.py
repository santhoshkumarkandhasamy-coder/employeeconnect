"""Bulk salary-structure import (plan 16 F7, contract 18.6): xlsx template, dry run and a one-transaction commit.

Two sheets are read: `Structures` (one row per employee) and `Components` (allowances / fixed deductions). The container checks,
parser limits, worker thread with a timeout, formula refusal and the HMAC dry-run binding are the ones of the one-time item
upload (`xlsx_import`, `payroll_items`); the dry-run key is `HMAC(secret, actor | "structures" | sha256)`, 15 minutes, Redis
fail closed. The file is never stored.

Commit, all or nothing, under the `users` row locks (id order) and then the structure rows (F15):
  * an employee WITHOUT a structure gets one directly (`source = import`, `import_batch_id`);
  * an employee WITH one gets a `submitted` increment (kind `import`, maker = the importer) in the import batch while
    `payroll.structure_changes_need_approval` is on (a second person approves the batch or the rows), else a structure directly;
  * a structure created directly that reaches a published month runs the arrears hook of plan 15 in the same transaction.
A row whose structure equals the one in force (`NO_CHANGE`) is valid but writes nothing.

Money is never written to audit rows, logs or error messages; errors name the sheet, the row, the field and a code.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import re
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import utcnow
from ..core.errors import AppError
from ..core.rbac import Principal
from ..core.text import clean_text
from ..models import SalaryIncrementBatch, SalaryStructure, User
from . import admin_security, xlsx, xlsx_import
from . import payroll_arrears as arrears_svc
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_increment_calc as ic
from . import payroll_increments as ig
from . import payroll_items as items_svc
from . import payroll_structures as structs_svc

log = logging.getLogger("app.payroll")

DRY_RUN_TTL_S = items_svc.DRY_RUN_TTL_S
MAX_STRUCTURE_ROWS = 500
MAX_COMPONENT_ROWS = 2000
MAX_ALLOWANCES, MAX_DEDUCTIONS = 15, 10
STRUCTURES = xlsx_import.SheetDef(
    "structures", "Structures",
    ("Employee ID", "Effective From", "Gross", "Basic", "HRA", "TDS Monthly", "PF", "PF On Full Basic", "ESI", "PT"),
    ("employee_id", "effective_from", "gross", "basic", "hra", "tds", "pf", "pf_on_full_basic", "esi", "pt"),
    MAX_STRUCTURE_ROWS,
)  # fmt: skip
COMPONENTS = xlsx_import.SheetDef(
    "components", "Components",
    ("Employee ID", "Type", "Code", "Name", "Amount", "Prorate", "ESI Wage"),
    ("employee_id", "type", "code", "name", "amount", "prorate", "esi_wage"),
    MAX_COMPONENT_ROWS,
)  # fmt: skip
SHEET_ORDER = {"Structures": 0, "Components": 1}
_EID = re.compile(r"^[A-Z0-9._-]{1,32}$")
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
# plan warnings that are part of the import report (the contract list plus the plan 16 section 3 increment warnings)
REPORTED = ("BACKDATED", "NO_CHANGE", "ESI_THRESHOLD_CROSSED", "TDS_REVIEW", "DECREASE", "LARGE_CHANGE", "PF_CEILING_CROSSED")
ERROR_OF_PLAN = {  # AppError code of ig.build_plan -> import row error
    "STRUCTURE_EXISTS": "STRUCTURE_EXISTS",
    "LATER_STRUCTURE_EXISTS": "LATER_STRUCTURE_EXISTS",
    "STRUCTURE_MISSING": "LATER_STRUCTURE_EXISTS",  # every structure of the employee starts after the date
    "INCREMENT_EXISTS": "INCREMENT_EXISTS",
    "AFTER_EXIT": "AFTER_EXIT",
    "INVALID_PERIOD": "INVALID_PERIOD",
}


def _bad(field_: str, message: str) -> AppError:
    return AppError("VALIDATION_ERROR", message, details=[{"field": field_, "message": message}])


# ============================================================================================ template
HELP_ROWS: list[tuple[str, str]] = [
    (
        "Sheets",
        "Structures: one row per employee. Components: allowances and fixed deductions of employees on the Structures sheet.",
    ),
    ("Employee ID", "The employee's ID as in the app. The example rows (EXAMPLE-DELETE-ME) fail the check: delete them."),
    ("Effective From", "Month the structure starts, written as text YYYY-MM (for example 2027-04); a date cell uses its month."),
    (
        "Gross",
        "Monthly gross in rupees. Use EITHER Gross (split by the company rule, no Components rows) OR Basic (+ HRA and Components).",
    ),
    (
        "Split rule",
        "When only Gross is given: Basic = basic % of gross, HRA = HRA % of basic, the rest is one allowance. The percentages are payroll settings.",
    ),
    ("Basic / HRA / TDS Monthly", "Monthly amounts in rupees (at most 2 decimals). HRA and TDS Monthly may stay empty (0)."),
    ("PF, ESI, PT", "Y or N. Empty means Y. PF On Full Basic: Y or N, empty means N."),
    ("Components: Type", "allowance or deduction. At most 15 allowances and 10 deductions per employee."),
    ("Components: Code", "Capital letters, digits and _, 2-16 characters, starting with a letter, unique for the employee."),
    ("Reserved codes", ", ".join(sorted(calc.RESERVED_CODES)) + " cannot be used."),
    ("Prorate, ESI Wage", "Allowances only: Y or N, empty means Y. Leave empty for deductions."),
    (
        "Employees with a structure",
        "They get a submitted increment that a second person approves (while the approval switch is on).",
    ),
    ("Employees without one", "The structure is created directly."),
    (
        "Limits",
        "File up to 1 MB, 500 rows on Structures, 2000 rows on Components. No formulas: cells starting with = + - @ are refused.",
    ),
]


async def template() -> xlsx.BuiltWorkbook:
    """`Structures`, `Components` (each with one example row that fails with UNKNOWN_EMPLOYEE if it is left in) and `Help`."""
    structures = [("EXAMPLE-DELETE-ME", "2027-04", None, 25000, 10000, 0, "Y", "N", "Y", "Y")]
    components = [("EXAMPLE-DELETE-ME", "allowance", "CONVEYANCE", "Conveyance", 1600, "Y", "Y")]

    def spec(title: str, headers: list[str], table: list[tuple]) -> xlsx.SheetSpec:
        async def rows() -> AsyncIterator[tuple]:
            for r in table:
                yield r

        async def count() -> int:
            return len(table)

        return xlsx.SheetSpec(title, headers, count, rows, 100)

    return await xlsx.build_workbook(
        [
            spec("Structures", list(STRUCTURES.header), structures),
            spec("Components", list(COMPONENTS.header), components),
            spec("Help", ["Topic", "Rule"], HELP_ROWS),
        ]
    )


# ============================================================================================ cell readers
def _text(v: Any) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return None
    t = str(v).strip()
    return t or None


def _month(v: Any) -> dt.date | None:
    """`YYYY-MM` text, or a date / datetime cell (its month), as the 1st of the month (years 2000-2200)."""
    day: dt.date | None = None
    if isinstance(v, dt.datetime):
        day = v.date().replace(day=1)
    elif isinstance(v, dt.date):
        day = v.replace(day=1)
    elif isinstance(v, str) and (m := _MONTH.match(v.strip())):
        day = dt.date(int(m.group(1)), int(m.group(2)), 1)
    if day is None or not 2000 <= day.year <= 2200:
        return None
    return day


def _flag(v: Any, default: bool) -> bool | None:
    """Y / N (blank = default); None = invalid."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    t = str(v).strip().upper()
    return {"Y": True, "N": False}.get(t)


@dataclass
class _Comp:
    row: int
    kind: str  # allowance | deduction
    code: str
    name: str
    amount: Decimal
    prorate: bool
    esi_wage: bool


@dataclass
class Planned:
    row: int
    user: User
    eff: dt.date
    proposed: dict[str, Any]
    gross: Decimal
    mode: str  # create (first structure) | direct (F2 off) | increment
    plan: ig.Plan | None
    warnings: list[str] = field(default_factory=list)

    @property
    def no_change(self) -> bool:
        return "NO_CHANGE" in self.warnings


class _Report:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []

    def err(self, sheet: str, row: int, field_: str, msg: str) -> None:
        self.errors.append({"row": row, "sheet": sheet, "field": field_, "message": msg})

    def warn(self, row: int, field_: str, msg: str) -> None:
        self.warnings.append({"row": row, "sheet": "Structures", "field": field_, "message": msg})

    def count(self) -> int:
        return len(self.errors)

    def sorted(self) -> None:
        key = lambda e: (SHEET_ORDER[e["sheet"]], e["row"])  # noqa: E731
        self.errors.sort(key=key)
        self.warnings.sort(key=key)


# ============================================================================================ validation
def _amount(v: Any, *, positive: bool) -> Decimal | None:
    a = xlsx_import.amount_of(v)
    if a is None or a > calc.MAX_MONEY or a < 0 or (positive and a == 0):
        return None
    return a


async def _validate(
    session: AsyncSession,
    actor: Principal,
    sheets: dict[str, list[xlsx_import.ParsedRow]],
    *,
    lock: bool,
) -> tuple[_Report, list[Planned]]:
    """Every row of both sheets against the database. With `lock` the employees' user rows are locked first (id order), then
    their structure rows, so the result stays true until the transaction ends."""
    rep = _Report()
    s_rows, c_rows = sheets["structures"], sheets["components"]
    cfg = await ig.load_cfg(session)
    needs_approval = await structs_svc._approval_required(session)

    def eid_of(pr: xlsx_import.ParsedRow) -> str:
        return (_text(pr.values.get("employee_id")) or "").upper()

    eids = {e for pr in [*s_rows, *c_rows] if "employee_id" not in pr.formula and _EID.match(e := eid_of(pr))}
    users: dict[str, User] = {}
    if eids:
        if lock:
            ids = list((await session.execute(select(User.id).where(User.employee_id.in_(sorted(eids))))).scalars())
            await items_svc._lock_users(session, ids)
        q = select(User).where(User.employee_id.in_(sorted(eids)))
        if lock:
            q = q.execution_options(populate_existing=True)
        users = {u.employee_id.upper(): u for u in (await session.execute(q)).unique().scalars()}

    def resolve(sheet: str, pr: xlsx_import.ParsedRow) -> User | None:
        if "employee_id" in pr.formula:
            return None
        eid = eid_of(pr)
        user = users.get(eid) if _EID.match(eid) else None
        if user is None:
            rep.err(sheet, pr.row, "employee_id", "UNKNOWN_EMPLOYEE")
        elif user.deleted_at is not None:
            rep.err(sheet, pr.row, "employee_id", "DELETED")
        elif user.id == actor.id:
            rep.err(sheet, pr.row, "employee_id", "SELF")
        else:
            return user
        return None

    # ------------------------------------------------------------------------------------------ Components
    comps_by_eid: dict[str, list[_Comp]] = defaultdict(list)
    bad_comp_eids: set[str] = set()  # employees with an error on one of their Components rows
    any_comp_eids: set[str] = set()  # employees with at least one Components row
    structure_eids = {eid_of(pr) for pr in s_rows if "employee_id" not in pr.formula}
    for pr in c_rows:
        n0 = rep.count()
        v = pr.values
        for f in sorted(pr.formula):
            rep.err("Components", pr.row, f, "FORMULA_NOT_ALLOWED")
        user = resolve("Components", pr)
        eid = eid_of(pr)
        if user is not None and eid not in structure_eids:
            rep.err("Components", pr.row, "employee_id", "ORPHAN_COMPONENT")
        kind = None
        if "type" not in pr.formula:
            t = (_text(v.get("type")) or "").lower()
            if t in ("allowance", "deduction"):
                kind = t
            else:
                rep.err("Components", pr.row, "type", "INVALID_TYPE")
        code = None
        if "code" not in pr.formula:
            raw = (_text(v.get("code")) or "").upper()
            if not calc.CODE_RE.match(raw):
                rep.err("Components", pr.row, "code", "INVALID_CODE")
            elif raw in calc.RESERVED_CODES:
                rep.err("Components", pr.row, "code", "RESERVED_CODE")
            else:
                code = raw
        name = None
        if "name" not in pr.formula:
            cleaned = clean_text(_text(v.get("name")) or "")
            if cleaned.startswith(xlsx_import.FORMULA_START):
                rep.err("Components", pr.row, "name", "FORMULA_NOT_ALLOWED")
            elif 1 <= len(cleaned) <= 40:
                name = cleaned
            else:
                rep.err("Components", pr.row, "name", "INVALID_NAME")
        amount = None
        if "amount" not in pr.formula:
            amount = _amount(v.get("amount"), positive=True)
            if amount is None:
                rep.err("Components", pr.row, "amount", "INVALID_AMOUNT")
        prorate = esi_wage = True
        for fname in ("prorate", "esi_wage"):
            if fname in pr.formula:
                continue
            raw_flag = v.get(fname)
            parsed = _flag(raw_flag, True)
            if parsed is None or (kind == "deduction" and raw_flag is not None):
                rep.err("Components", pr.row, fname, "INVALID_FLAG")
            elif fname == "prorate":
                prorate = parsed
            else:
                esi_wage = parsed
        if rep.count() == n0 and user is not None and kind and code and name and amount is not None:
            comps_by_eid[eid].append(_Comp(pr.row, kind, code, name, amount, prorate, esi_wage))
        if eid:
            any_comp_eids.add(eid)
            if rep.count() > n0:
                bad_comp_eids.add(eid)
    for eid, items in comps_by_eid.items():
        seen: set[str] = set()
        counts = {"allowance": 0, "deduction": 0}
        for c in items:
            if c.code in seen:
                rep.err("Components", c.row, "code", "DUPLICATE_CODE")
                bad_comp_eids.add(eid)
                continue
            seen.add(c.code)
            counts[c.kind] += 1
            if counts[c.kind] > (MAX_ALLOWANCES if c.kind == "allowance" else MAX_DEDUCTIONS):
                rep.err("Components", c.row, "employee_id", "TOO_MANY_COMPONENTS")
                bad_comp_eids.add(eid)

    # ------------------------------------------------------------------------------------------ Structures
    parsed_rows: list[tuple[xlsx_import.ParsedRow, User, dt.date, dict[str, Any]]] = []
    seen_eids: set[str] = set()
    for pr in s_rows:
        n0 = rep.count()
        v = pr.values
        for f in sorted(pr.formula):
            rep.err("Structures", pr.row, f, "FORMULA_NOT_ALLOWED")
        user = resolve("Structures", pr)
        eid = eid_of(pr)
        if user is not None:
            if eid in seen_eids:
                rep.err("Structures", pr.row, "employee_id", "DUPLICATE_ROW")
            seen_eids.add(eid)
        eff = None
        if "effective_from" not in pr.formula:
            eff = _month(v.get("effective_from"))
            if eff is None:
                rep.err("Structures", pr.row, "effective_from", "INVALID_PERIOD")
        money: dict[str, Decimal | None] = {}
        for fname, positive in (("gross", True), ("basic", True), ("hra", False), ("tds", False)):
            money[fname] = None
            if fname in pr.formula or v.get(fname) is None:
                continue
            money[fname] = _amount(v.get(fname), positive=positive)
            if money[fname] is None:
                rep.err("Structures", pr.row, fname, "INVALID_AMOUNT")
        flags: dict[str, bool] = {}
        for fname, default in (("pf", True), ("pf_on_full_basic", False), ("esi", True), ("pt", True)):
            flags[fname] = default
            if fname in pr.formula:
                continue
            parsed = _flag(v.get(fname), default)
            if parsed is None:
                rep.err("Structures", pr.row, fname, "INVALID_FLAG")
            else:
                flags[fname] = parsed
        comps = comps_by_eid.get(eid, [])
        has_parts = v.get("basic") is not None or v.get("hra") is not None
        gross = money["gross"]
        if v.get("gross") is not None and "gross" not in pr.formula and (has_parts or eid in any_comp_eids):
            rep.err("Structures", pr.row, "gross", "GROSS_AND_PARTS")
        elif v.get("gross") is None and v.get("basic") is None and not (pr.formula & {"gross", "basic"}):
            rep.err("Structures", pr.row, "basic", "MISSING_BASIC")
        if rep.count() > n0 or user is None or eff is None or eid in bad_comp_eids:
            continue
        # the proposed structure
        try:
            if gross is not None:
                proposed = ic.split_from_gross(
                    {"basic": "1.00"}, gross, basic_pct=cfg.basic_pct, hra_pct_of_basic=cfg.hra_pct,
                    code=cfg.split_code, name=cfg.split_name,
                )  # fmt: skip
            else:
                basic = money["basic"]
                if basic is None:
                    continue
                proposed = {
                    "basic": calc.money_str(basic), "hra": calc.money_str(money["hra"] or Decimal(0)),
                    "allowances": [
                        {"code": c.code, "name": c.name, "amount": calc.money_str(c.amount), "prorate": c.prorate, "esi_wage": c.esi_wage}
                        for c in comps if c.kind == "allowance"
                    ],
                    "fixed_deductions": [
                        {"code": c.code, "name": c.name, "amount": calc.money_str(c.amount)} for c in comps if c.kind == "deduction"
                    ],
                }  # fmt: skip
        except ic.IncrementCalcError:
            rep.err("Structures", pr.row, "gross", "INVALID_AMOUNT")
            continue
        proposed = ic.normalise(
            {
                **proposed,
                "tds_monthly": calc.money_str(money["tds"] or Decimal(0)),
                "pf_applicable": flags["pf"],
                "pf_on_full_basic": flags["pf_on_full_basic"],
                "esi_applicable": flags["esi"],
                "pt_applicable": flags["pt"],
            }  # fmt: skip
        )
        parsed_rows.append((pr, user, eff, proposed))

    # ------------------------------------------------------------------------------------------ state of the employees
    ctxs = await ig._contexts(session, [u for _, u, _, _ in parsed_rows], lock=lock)
    planned: list[Planned] = []
    for pr, user, eff, proposed in parsed_rows:
        ctx = ctxs[user.id]
        gross_total = ic.gross_of(proposed)
        if not ig.within_money_bounds(proposed, gross_total):
            rep.err("Structures", pr.row, "gross", "INVALID_AMOUNT")
            continue
        warns: list[str] = []
        if not ctx.structures:
            last = ig.last_employed_day(user, cfg.tz)
            try:
                ig.check_effective(eff, cfg, lower=False)
            except AppError:
                rep.err("Structures", pr.row, "effective_from", "INVALID_PERIOD")
                continue
            if last is not None and eff > last.replace(day=1):
                rep.err("Structures", pr.row, "effective_from", "AFTER_EXIT")
                continue
            if eff in ctx.live:
                rep.err("Structures", pr.row, "effective_from", "INCREMENT_EXISTS")
                continue
            if ctx.latest_published is not None and eff <= ctx.latest_published:
                warns.append("BACKDATED")
            planned.append(Planned(pr.row, user, eff, proposed, gross_total, "create", None, warns))
            continue
        try:
            plan = ig.build_plan(kind="import", eff=eff, structure=proposed, cfg=cfg, ctx=ctx)
        except AppError as exc:
            code = ERROR_OF_PLAN.get(exc.code)
            if code is None:
                raise
            rep.err("Structures", pr.row, "effective_from", code)
            continue
        warns = [w for w in plan.warnings if w in REPORTED]
        mode = "direct"
        if needs_approval:
            mode = "increment"
            if "NO_CHANGE" not in warns:
                warns.append("NEEDS_APPROVAL")
        planned.append(Planned(pr.row, user, eff, proposed, gross_total, mode, plan, warns))
    for p in planned:
        for w in p.warnings:
            rep.warn(p.row, "effective_from" if w == "BACKDATED" else "employee_id", w)
    rep.sorted()
    return rep, planned


# ============================================================================================ dry-run binding (HMAC, Redis)
def _dry_run_key(actor: Principal, digest: str) -> str:
    """HMAC(actor, "structures", sha256 of the file) under the server secret: only this admin's dry run of this exact file
    unlocks the commit."""
    msg = f"{actor.id}|structures|{digest}".encode()
    mac = hmac.new(pc.import_binding_key(), msg, hashlib.sha256).hexdigest()
    return f"payroll:import:dry:{mac}"


def _no_rows() -> AppError:
    return AppError(
        "IMPORT_INVALID", "The Structures sheet has no data rows", details=[{"field": "file", "message": "no data rows"}]
    )


def _invalid(rep: _Report) -> AppError:
    return AppError(
        "IMPORT_INVALID", "The file has errors; nothing was imported",
        details=[{"field": f"{e['sheet']} row {e['row']}: {e['field']}", "message": e["message"]} for e in rep.errors],
    )  # fmt: skip


# ============================================================================================ import
async def import_structures(
    session: AsyncSession,
    redis: Redis,
    actor: Principal,
    *,
    data: bytes,
    dry_run: bool,
    file_sha256: str | None,
    reason: str | None,
    password: SecretStr | None,
) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    dry_key = _dry_run_key(actor, digest)
    clean_reason = ""
    if not dry_run:
        if not file_sha256 or not re.fullmatch(r"[0-9a-f]{64}", file_sha256):
            raise _bad("file_sha256", "send the file_sha256 returned by the dry run")
        clean_reason = clean_text(reason or "")
        if not 3 <= len(clean_reason) <= 200:
            raise _bad("reason", "must be 3-200 characters")
        await admin_security.require_reauth(session, redis, actor, password, action="payroll.structure_import.commit")
        if file_sha256 != digest:
            raise AppError("IMPORT_FILE_CHANGED", "The file differs from the one that was checked; run the dry run again")
        dup = await session.execute(
            select(SalaryIncrementBatch.id).where(
                SalaryIncrementBatch.file_sha256 == digest, SalaryIncrementBatch.kind == "import"
            )
        )
        if dup.first():
            raise AppError("IMPORT_DUPLICATE", "This file was already imported")
        await items_svc._require_dry_run(redis, dry_key)
    sheets = await xlsx_import.parse_sheets(data, [STRUCTURES, COMPONENTS])
    if not sheets["structures"]:
        raise _no_rows()
    rep, planned = await _validate(session, actor, sheets, lock=not dry_run)

    if dry_run:
        actions = {"create": 0, "increment": 0}
        total = Decimal("0.00")
        for p in planned:
            total += p.gross
            if not p.no_change:
                actions["increment" if p.mode == "increment" else "create"] += 1
        await items_svc._remember_dry_run(redis, dry_key)
        audit(session, action="payroll.structure_import.dry_run", actor=actor, entity_type="salary_increment_batch",
              after={"rows": len(sheets["structures"]), "valid": len(planned), "errors": len(rep.errors)})  # fmt: skip
        await pc.commit(session)
        return {
            "file_sha256": digest, "rows": len(sheets["structures"]), "valid": len(planned), "errors": rep.errors,
            "warnings": rep.warnings, "actions": actions, "totals": {"monthly_gross": calc.money_str(total)},
        }  # fmt: skip

    if rep.errors:
        raise _invalid(rep)
    applied = [p for p in planned if not p.no_change]
    if not applied:
        raise AppError("IMPORT_INVALID", "Nothing to import: every row equals the current structure",
                       details=[{"field": "file", "message": "no row changes a structure"}])  # fmt: skip
    batch = SalaryIncrementBatch(kind="import", file_sha256=digest, rows=len(applied), reason=clean_reason, created_by=actor.id)
    session.add(batch)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError("IMPORT_DUPLICATE", "This file was already imported") from None
    created = submitted = proposals = 0
    for p in applied:
        if p.mode == "increment":
            assert p.plan is not None
            inc = ig._new_row(actor, p.user.id, p.plan, clean_reason, batch.id)
            inc.status, inc.submitted_by, inc.submitted_at = "submitted", actor.id, utcnow()
            session.add(inc)
            await ig._flush_unique(session)
            ig._event(session, inc, "create", actor.id, reason=clean_reason, batch_id=str(batch.id))
            ig._event(session, inc, "submit", actor.id, batch_id=str(batch.id))
            ig._audit(session, actor, "payroll.increment.create", inc)
            submitted += 1
            continue
        st = SalaryStructure(
            user_id=p.user.id, effective_from=p.eff, components={}, source="import", import_batch_id=batch.id,
            created_by=actor.id, updated_by=actor.id,
        )  # fmt: skip
        ig._fill_structure(st, p.proposed)
        session.add(st)
        await pc.flush(session)
        audit(session, action="payroll.structure.create", actor=actor, entity_type="salary_structure", entity_id=st.id,
              after={"user_id": str(p.user.id), "effective_from": p.eff.isoformat(), "source": "import", "batch_id": str(batch.id)})  # fmt: skip
        created += 1
        latest = await arrears_svc.latest_published_month(session, p.user.id)
        if latest is not None and p.eff <= latest:
            row = await arrears_svc.sync_proposal(session, p.user.id, actor.id)
            audit(session, action="payroll.arrears.propose", actor=actor, entity_type=arrears_svc.ENTITY,
                  entity_id=row.id if row else None,
                  after={"user_id": str(p.user.id), "proposal": row is not None, "trigger": "import"})  # fmt: skip
            proposals += int(row is not None)
    audit(session, action="payroll.structure_import.commit", actor=actor, entity_type="salary_increment_batch", entity_id=batch.id,
          after={"batch_id": str(batch.id), "created": created, "increments_submitted": submitted})  # fmt: skip
    batch_id, name = batch.id, actor.user.full_name
    await pc.commit(session)
    try:
        await redis.delete(dry_key)  # one dry run, one commit
    except Exception:  # noqa: BLE001 - the duplicate-file guard still holds
        log.warning("import dry-run record not deleted")
    if submitted:
        await ig._push_approvers(
            session, redis, actor.id, name, kind="increment_batch", entity_id=batch_id, text=f"{submitted} increment(s)"
        )
    return {"batch_id": str(batch_id), "created": created, "increments_submitted": submitted, "arrears_proposed": proposals}
