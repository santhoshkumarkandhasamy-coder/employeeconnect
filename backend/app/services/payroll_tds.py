"""TDS engine integration (plan 17 work package 5c, decisions T6 / T7 / T8, contract 19.9).

`tax_engine` is pure; this module feeds it from the database and writes the result:

* `run_ctx` reads the run snapshot (`config.tds_engine_enabled`, `tds_engine_mode`, `tax_rules {fy,id,version}`): None = the engine is off
  for this run (exactly today's manual TDS). The rules row is the PINNED one of the snapshot (verified or later superseded); a newer
  verified version only raises the warning `RULES_CHANGED` until the draft run is refreshed.
* `evaluate` is called from `payroll_runs.recompute` after the normal calculation. It never touches the database: it leaves the
  computation on the payslip (`ps._tds`) and, in mode `apply`, sets `inputs.tds` (+ `tds_source`) so the caller recalculates.
* `persist` (async) writes / updates / deletes the `payroll_tax_computations` row WHILE the payslip is still `draft`; the database trigger
  freezes it once the payslip is processed, so callers persist BEFORE they set the status.
* Everything the engine decides is stored encrypted (computation, suggested TDS, override reason); audit rows carry ids and counts only.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.clock import utcnow
from ..core.errors import AppError
from ..models import (
    PayrollLoan,
    PayrollLoanInstallment,
    PayrollRun,
    PayrollTaxComputation,
    Payslip,
    SalaryStructure,
    TaxDeclaration,
    TaxProof,
    TaxYearRule,
)
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_settings as pset
from . import tax_engine as tx
from . import tax_rules as tr

ZERO = Decimal("0.00")
PROOFS_LABEL = b"employee-connect/payroll-tds-proofs/v1"
ENGINE_KEYS = (
    "tds_engine_enabled",
    "tds_engine_mode",
    "tds_one_time_in_month",
    "tds_proofs_from_month",
    "tds_include_other_income",
)
TDS_WARNINGS = (
    "TDS_OVERRIDDEN", "TDS_CAPPED_NET", "TDS_EXCESS", "TAX_RULES_NOT_VERIFIED", "NO_PAN_206AA", "PERQUISITE_REVIEW",
    "LEAVE_ENCASH_REVIEW", "RULES_CHANGED",
)  # fmt: skip
LIVE = ("published", "processed")


def _d(x: Any) -> Decimal:
    return Decimal(str(x)) if x is not None else ZERO


# ============================================================================================ contexts
@dataclass
class RunCtx:
    """What the engine needs from a run (or, for previews, from the current settings)."""

    fy: str
    mode: str
    row: TaxYearRule | None  # None = no verified rules (TAX_RULES_NOT_VERIFIED)
    rules_changed: bool = False
    one_time_in_month: bool = True
    proofs_from: str = ""
    include_other_income: bool = False

    @property
    def rules(self) -> dict[str, Any] | None:
        return self.row.rules if self.row is not None else None


@dataclass
class UserData:
    declaration: TaxDeclaration | None = None
    verified: dict[str, Decimal] = field(default_factory=dict)
    history: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (YYYY-MM, payslip result) earlier in the FY
    loans: Decimal = ZERO


@dataclass
class Ctx:
    rc: RunCtx
    ud: UserData


def proofs_from_key(fy: str, month: int) -> str:
    """First `YYYY-MM` of the FY whose calendar month is `month` (1 = January of the second year)."""
    y = int(fy[:4])
    return f"{y if month >= 4 else y + 1}-{month:02d}"


def _make_ctx(cfg: dict[str, Any], fy: str, row: TaxYearRule | None, changed: bool) -> RunCtx:
    return RunCtx(
        fy=fy, mode=str(cfg.get("tds_engine_mode") or "suggest"), row=row, rules_changed=changed,
        one_time_in_month=bool(cfg.get("tds_one_time_in_month", True)),
        proofs_from=proofs_from_key(fy, int(cfg.get("tds_proofs_from_month") or 1)),
        include_other_income=bool(cfg.get("tds_include_other_income", False)),
    )  # fmt: skip


async def verified_row(session: AsyncSession, fy: str) -> TaxYearRule | None:
    return (
        await session.execute(select(TaxYearRule).where(TaxYearRule.fy == fy, TaxYearRule.status == "verified"))
    ).scalar_one_or_none()


async def augment_config(session: AsyncSession, cfg: dict[str, Any], period: dt.date) -> dict[str, Any]:
    """Run snapshot (create / refresh): adds `tax_rules {fy,id,version}` (the verified rules of the run's FY) when the engine is on."""
    out = dict(cfg)
    out["tax_rules"] = None
    if cfg.get("tds_engine_enabled"):
        fy = tr.fy_of(period)
        row = await verified_row(session, fy)
        if row is not None:
            out["tax_rules"] = {"fy": fy, "id": str(row.id), "version": row.version}
    return out


async def run_ctx(session: AsyncSession, run: PayrollRun, *, lock: bool = False) -> RunCtx | None:
    """`lock` = SELECT ... FOR SHARE on the pinned rules row (plan 17 L: a concurrent verify / supersede waits until we commit)."""
    cfg = run.config or {}
    if not cfg.get("tds_engine_enabled"):
        return None
    fy = tr.fy_of(run.period)
    snap = cfg.get("tax_rules") or {}
    row: TaxYearRule | None = None
    if snap.get("id") and snap.get("fy") == fy:
        try:
            q = select(TaxYearRule).where(TaxYearRule.id == uuid.UUID(str(snap["id"])))
            if lock:
                q = q.with_for_update(read=True)
            row = (await session.execute(q)).scalar_one_or_none()
        except ValueError:
            row = None
        if row is not None and (row.fy != fy or row.status not in ("verified", "superseded")):
            row = None  # a draft / submitted document never feeds a payslip
    current = await verified_row(session, fy)
    changed = current is not None and (row is None or current.id != row.id)
    return _make_ctx(cfg, fy, row, changed)


async def current_ctx(session: AsyncSession, fy: str) -> RunCtx:
    """For previews, summaries and the employee projection: the CURRENT settings and the verified rules, 409 otherwise."""
    cfg = {k: v for k, v in (await pset.snapshot(session)).items() if k in ENGINE_KEYS}
    if not cfg.get("tds_engine_enabled"):
        raise AppError("TDS_ENGINE_OFF", "The TDS engine is switched off")
    row = await verified_row(session, fy)
    if row is None:
        raise AppError("TAX_RULES_NOT_VERIFIED", "The tax rules for this financial year have not been verified yet")
    return _make_ctx(cfg, fy, row, False)


# ============================================================================================ loading
def _key(d: dt.date) -> str:
    return f"{d:%Y-%m}"


async def load_data(
    session: AsyncSession, fy: str, period: dt.date, user_ids: list[uuid.UUID], *, lock: bool = False
) -> dict[uuid.UUID, UserData]:
    """`lock` = FOR SHARE on the declarations and proofs read (plan 17 L order: rules, declarations, proofs): a declaration submit or a
    proof decision that races with processing waits, so the stored `proofs_digest` is what the payslip really used."""
    out = {u: UserData() for u in user_ids}
    if not user_ids:
        return out
    dq = select(TaxDeclaration).where(TaxDeclaration.fy == fy, TaxDeclaration.user_id.in_(user_ids))
    if lock:
        dq = dq.order_by(TaxDeclaration.id).with_for_update(read=True)
    for d in (await session.execute(dq)).scalars():
        out[d.user_id].declaration = d
    q = select(TaxProof).where(TaxProof.fy == fy, TaxProof.user_id.in_(user_ids), TaxProof.status == "verified")
    if lock:
        q = q.order_by(TaxProof.id).with_for_update(read=True)
    for p in (await session.execute(q)).scalars():
        out[p.user_id].verified[p.item_id] = _d((p.verified or {}).get("amount"))
    start = tr.fy_bounds(fy)[0]
    q2 = (
        select(Payslip, PayrollRun.period)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(
            Payslip.user_id.in_(user_ids), PayrollRun.period >= start, PayrollRun.period < period.replace(day=1),
            Payslip.status.in_(LIVE),
        )
    )  # fmt: skip
    best: dict[tuple[uuid.UUID, dt.date], Payslip] = {}
    for ps, per in (await session.execute(q2)).all():
        if ps.result is None:
            continue
        cur = best.get((ps.user_id, per))
        if cur is None or (cur.status != "published" and (ps.status == "published" or ps.revision > cur.revision)):
            best[(ps.user_id, per)] = ps
    for (uid, per), ps in sorted(best.items(), key=lambda kv: kv[0][1]):
        out[uid].history.append((_key(per), ps.result))
    loans = (
        (
            await session.execute(
                select(PayrollLoan).where(
                    PayrollLoan.user_id.in_(user_ids), PayrollLoan.status == "active", PayrollLoan.kind == "loan"
                )
            )
        )
        .scalars()
        .all()
    )
    if loans:
        paid: dict[uuid.UUID, Decimal] = {}
        qi = select(PayrollLoanInstallment).where(
            PayrollLoanInstallment.loan_id.in_([lo.id for lo in loans]), PayrollLoanInstallment.status == "paid"
        )
        for i in (await session.execute(qi)).scalars():
            paid[i.loan_id] = paid.get(i.loan_id, ZERO) + _d(i.amount)
        for lo in loans:
            out[lo.user_id].loans += max(ZERO, _d(lo.principal) - paid.get(lo.id, ZERO))
    return out


def proofs_digest(ud: UserData) -> str:
    """HMAC (never a plain hash: amounts are low-entropy) over the verified proofs and the declaration version the engine relied on."""
    decl = ud.declaration
    return pc.mac_hex(
        PROOFS_LABEL,
        {"v": decl.submitted_version if decl is not None else None,
         "proofs": sorted((k, f"{v:.2f}") for k, v in ud.verified.items())},
    )  # fmt: skip


# ============================================================================================ building the input
@dataclass
class This:
    regular: Decimal = ZERO
    one_time: Decimal = ZERO
    basic: Decimal = ZERO
    hra: Decimal = ZERO
    pt: Decimal = ZERO
    leave_encash: Decimal = ZERO


def this_from_result(result: dict[str, Any], treatment: dict[str, str]) -> This:
    t = This()
    for e in result.get("earnings", []):
        amt = _d(e["amount"])
        part = tx.taxable_part(treatment, e["code"], amt)
        if e.get("monthly") is not None:
            t.regular += part
        else:
            t.one_time += part
        if e["code"] == "BASIC":
            t.basic = amt
        elif e["code"] == "HRA":
            t.hra = amt
        elif e["code"] == "LEAVE_ENCASH":
            t.leave_encash += amt
    t.pt = _ded(result, "PT")
    return t


def _ded(result: dict[str, Any], code: str) -> Decimal:
    return sum((_d(d["amount"]) for d in result.get("deductions", []) if d["code"] == code), ZERO)


def _monthly_structure(structs: list[tuple[dt.date, SalaryStructure]], day: dt.date, treatment: dict[str, str]):
    """(taxable monthly total, basic, hra) of the structure in force on `day` for a FULL month, or None."""
    from .payroll_structures import data_of  # local import: that module imports the runs module

    s = calc.structure_for(structs, day)
    if s is None:
        return None
    data = data_of(s)
    total = tx.taxable_part(treatment, "BASIC", data.basic) + tx.taxable_part(treatment, "HRA", data.hra)
    total += sum((tx.taxable_part(treatment, a.code, a.amount) for a in data.allowances), ZERO)
    return q(total), data.basic, data.hra


def q(x: Decimal) -> Decimal:
    return calc.q2(x)


def this_from_structure(structs: list[tuple[dt.date, SalaryStructure]], day: dt.date, treatment: dict[str, str]) -> This:
    m = _monthly_structure(structs, day, treatment)
    if m is None:
        return This()
    return This(regular=m[0], basic=m[1], hra=m[2])


def build_input(
    rc: RunCtx, ud: UserData, *, period: dt.date, this: This, structs: list[tuple[dt.date, SalaryStructure]], lwd: dt.date | None,
    final: bool, has_pan: bool,
) -> tx.EngineInput:  # fmt: skip
    rules = rc.rules or {}
    treat = rules.get("component_treatment", {"default": "taxable"})
    months = tx.fy_months(rc.fy)
    pkey = _key(period)
    idx = months.index(pkey)
    lkey = _key(lwd) if lwd else None
    if final or (lkey is not None and lkey <= pkey):
        future: list[str] = []
    elif lkey is not None and lkey in months:
        future = months[idx + 1 : months.index(lkey) + 1]
    else:
        future = months[idx + 1 :]
    paid = pt = tds = ZERO
    wages: list[tx.MonthWage] = []
    for key, res in ud.history:
        b = h = ZERO
        for e in res.get("earnings", []):
            amt = _d(e["amount"])
            paid += tx.taxable_part(treat, e["code"], amt)
            if e["code"] == "BASIC":
                b = amt
            elif e["code"] == "HRA":
                h = amt
        pt += _ded(res, "PT")
        tds += _ded(res, "TDS")
        wages.append(tx.MonthWage(key, b, h))
    wages.append(tx.MonthWage(pkey, this.basic, this.hra))
    projected = ZERO
    for key in future:
        m = _monthly_structure(structs, dt.date(int(key[:4]), int(key[5:7]), 1), treat)
        if m is None:
            continue
        projected += m[0]
        wages.append(tx.MonthWage(key, m[1], m[2]))
    decl = ud.declaration
    has = decl is not None and decl.submitted is not None
    return tx.EngineInput(
        fy=rc.fy, period=pkey, declared_regime=decl.regime if has else None, has_declaration=has,
        declaration_version=decl.submitted_version if has else None,
        items=list((decl.submitted or {}).get("items", [])) if has else [], verified=dict(ud.verified), proofs_from=rc.proofs_from,
        salary_paid=q(paid), salary_this_month=q(this.regular), one_time_this_month=q(this.one_time), salary_projected=q(projected),
        pt_paid=q(pt + this.pt), wages=wages, tds_paid=q(tds), remaining_months=1 + len(future),
        include_other_income=rc.include_other_income, one_time_in_month=rc.one_time_in_month, has_pan=has_pan,
    )  # fmt: skip


def is_final(inputs: dict[str, Any] | None, lwd: dt.date | None, period: dt.date) -> bool:
    return bool((inputs or {}).get("settlement_only")) or (lwd is not None and lwd.replace(day=1) <= period.replace(day=1))


def extra_flags(rc: RunCtx, ud: UserData, this: This, final: bool) -> list[str]:
    flags: list[str] = []
    thr = (rc.rules or {}).get("loan_perquisite_threshold")
    if thr is not None and ud.loans > Decimal(thr):
        flags.append("PERQUISITE_REVIEW")
    if final and this.leave_encash > 0:
        flags.append("LEAVE_ENCASH_REVIEW")
    if rc.rules_changed:
        flags.append("RULES_CHANGED")
    return flags


# ============================================================================================ the hook (called by recompute)
def prepare(ps: Payslip, ctx: Ctx | None) -> None:
    """Before the calculation: drop engine state that no longer applies. An engine-set TDS line returns to the manual value (None =
    the structure default) when the engine is off, the rules are not verified or the mode is `suggest`; an override keeps its amount
    as a plain manual value. Payslips that never carried engine state are untouched (the engine-off equivalence)."""
    inputs = ps.inputs or {}
    src = inputs.get("tds_source")
    if src is None:
        return
    if ctx is not None and ctx.rc.rules is not None and ctx.rc.mode == "apply":
        return
    new = copy.deepcopy(inputs)
    if src == "engine":
        new["tds"] = None
    new.pop("tds_source", None)
    new.pop("tds_override_reason", None)
    ps.inputs = new


def evaluate(
    ps: Payslip, res: calc.CalcResult, ctx: Ctx, *, structs: list[tuple[dt.date, SalaryStructure]], period: dt.date,
    lwd: dt.date | None, warns: list[str],
) -> bool:  # fmt: skip
    """Computes the TDS for the payslip from its (first) calculation. Returns True when `inputs.tds` changed (mode `apply`), so the
    caller must calculate again. The computation is left on `ps._tds` for `persist` (False = no row; the payslip has no result)."""
    rc, ud = ctx.rc, ctx.ud
    ps._tds = False  # type: ignore[attr-defined]
    if rc.rules is None:
        warns.append("TAX_RULES_NOT_VERIFIED")
        return False
    if res.error == "STRUCTURE_MISSING" or ps.structure_id is None:
        return False
    result = res.to_json()
    treat = rc.rules["component_treatment"]
    this = this_from_result(result, treat)
    final = is_final(ps.inputs, lwd, period)
    inp = build_input(
        rc, ud, period=period, this=this, structs=structs, lwd=lwd, final=final, has_pan=bool((ps.identity or {}).get("pan"))
    )  # fmt: skip
    figs = tx.compute(rc.rules, inp)
    flags = list(figs["flags"]) + extra_flags(rc, ud, this, final)
    suggested = Decimal(figs["suggested"])
    applied: Decimal | None = None
    override: dict[str, str] | None = None
    shortfall = ZERO
    changed = False
    inputs = copy.deepcopy(ps.inputs or {})
    if rc.mode == "apply":
        if inputs.get("tds_source") == "override" and inputs.get("tds") is not None:
            applied = _d(inputs["tds"])
            override = {"amount": tx.money(applied), "reason": str(inputs.get("tds_override_reason") or "")}
            flags.append("TDS_OVERRIDDEN")
        else:
            tds_line = _ded(result, "TDS")
            net_before = max(ZERO, _d(result["net"]) + tds_line)
            applied = min(suggested, net_before)
            if applied < suggested:
                shortfall = suggested - applied
                flags.append("TDS_CAPPED_NET")
            inputs["tds"] = tx.money(applied)
            inputs["tds_source"] = "engine"
            inputs.pop("tds_override_reason", None)
        changed = inputs != (ps.inputs or {})
        ps.inputs = inputs
    for f in flags:
        if f in TDS_WARNINGS:
            warns.append(f)
    comp = {
        **figs, "flags": flags, "mode": rc.mode, "applied": tx.money(applied) if applied is not None else None,
        "override": override, "shortfall": tx.money(shortfall), "computed_at": pc.iso(utcnow()),
    }  # fmt: skip
    ps._tds = {  # type: ignore[attr-defined]
        "fy": rc.fy, "rules_id": rc.row.id, "rules_version": rc.row.version, "regime": figs["regime"],
        "declaration_version": figs["declaration_version"], "proofs_digest": proofs_digest(ud), "mode": rc.mode,
        "computation": comp, "suggested": figs["suggested"], "applied": rc.mode == "apply" and override is None,
        "override_reason": (override or {}).get("reason") or None,
    }  # fmt: skip
    return changed


# ============================================================================================ persistence
async def persist(session: AsyncSession, payslips: list[Payslip], actor_id: uuid.UUID | None) -> None:
    """Write the computation rows of the DRAFT payslips just recomputed (the trigger freezes them with the payslip). Flushes, so the
    caller can change the status afterwards without the ORM reordering the statements."""
    todo = [p for p in payslips if p.status == "draft" and getattr(p, "_tds", None) is not None]
    if not todo:
        return
    await pc.flush(session)  # payslip ids exist; payslip rows are written while still `draft`
    rows = {
        r.payslip_id: r
        for r in (
            await session.execute(select(PayrollTaxComputation).where(PayrollTaxComputation.payslip_id.in_([p.id for p in todo])))
        ).scalars()
    }
    for p in todo:
        tc = p._tds  # type: ignore[attr-defined]
        row = rows.get(p.id)
        if tc is False:
            if row is not None:
                await session.delete(row)
            continue
        if row is None:
            row = PayrollTaxComputation(payslip_id=p.id, user_id=p.user_id, fy=tc["fy"], created_by=actor_id)
            session.add(row)
        row.rules_id, row.rules_version, row.regime = tc["rules_id"], tc["rules_version"], tc["regime"]
        row.declaration_version, row.proofs_digest, row.mode = tc["declaration_version"], tc["proofs_digest"], tc["mode"]
        row.computation, row.suggested, row.applied = tc["computation"], tc["suggested"], tc["applied"]
        row.override_reason = tc["override_reason"]
        row.updated_by = actor_id
        p._tds = None  # type: ignore[attr-defined]
    await pc.flush(session)


async def computation_row(session: AsyncSession, payslip_id: uuid.UUID) -> PayrollTaxComputation | None:
    return (
        await session.execute(select(PayrollTaxComputation).where(PayrollTaxComputation.payslip_id == payslip_id))
    ).scalar_one_or_none()


def computation_out(row: PayrollTaxComputation, *, employee: bool = False) -> dict[str, Any]:
    c = row.computation or {}
    out = {
        "fy": row.fy, "mode": row.mode, "rules": {"id": str(row.rules_id), "version": row.rules_version}, "regime": row.regime,
        "regime_source": c.get("regime_source"), "declaration_version": row.declaration_version, "proof_basis": c.get("proof_basis"),
        "remaining_months": c.get("remaining_months"), "annual": c.get("annual"), "tds_paid_so_far": c.get("tds_paid_so_far"),
        "previous_employer_tds": c.get("previous_employer_tds"), "remaining_tax": c.get("remaining_tax"),
        "one_time_tax": c.get("one_time_tax"), "suggested": row.suggested, "applied": c.get("applied"), "override": c.get("override"),
        "flags": c.get("flags", []), "steps": c.get("steps"), "shortfall": c.get("shortfall"), "computed_at": c.get("computed_at"),
    }  # fmt: skip
    if employee:
        out["override"] = None
    return out


async def payslip_computation(session: AsyncSession, payslip_id: uuid.UUID) -> dict[str, Any] | None:
    row = await computation_row(session, payslip_id)
    return computation_out(row) if row is not None else None


async def attach_macs(session: AsyncSession, payslips: list[Payslip]) -> None:
    """Publish MAC input (T8): the computation row's decision fields travel with the payslip into `result_mac`, so a changed
    computation after a publish request was made makes the request stale. Payslips without a row keep their old MAC."""
    ids = [p.id for p in payslips]
    rows = {}
    if ids:
        q_ = select(PayrollTaxComputation).where(PayrollTaxComputation.payslip_id.in_(ids))
        rows = {r.payslip_id: r for r in (await session.execute(q_)).scalars()}
    for p in payslips:
        r = rows.get(p.id)
        p._tc_mac = (  # type: ignore[attr-defined]
            None
            if r is None
            else {
                "rules": str(r.rules_id),
                "v": r.rules_version,
                "regime": r.regime,
                "decl": r.declaration_version,
                "proofs": r.proofs_digest,
                "mode": r.mode,
                "suggested": r.suggested,
                "applied": r.applied,
                "override": bool(r.override_reason),
            }  # fmt: skip
        )


# ============================================================================================ override rules (PUT payslip)
def apply_active(rc: RunCtx | None) -> bool:
    return rc is not None and rc.rules is not None and rc.mode == "apply"


def set_manual_tds(inputs: dict[str, Any], value: Decimal | None, reason: str | None, rc: RunCtx | None) -> None:
    """PUT /payslips/{id} `tds`. In mode `apply` a value is an OVERRIDE and needs a reason (3-200); null hands the line back to the
    engine. In every other case it is the plain manual figure, as before."""
    if value is None:
        inputs["tds"] = None
        inputs.pop("tds_source", None)
        inputs.pop("tds_override_reason", None)
        return
    inputs["tds"] = calc.money_str(value)
    if apply_active(rc):
        if not reason or not 3 <= len(reason) <= 200:
            raise AppError(
                "VALIDATION_ERROR", "A reason of 3-200 characters is required to override the TDS the engine computed",
                details=[{"field": "tds_override_reason", "message": "required when the TDS engine applies its figure"}],
            )  # fmt: skip
        inputs["tds_source"] = "override"
        inputs["tds_override_reason"] = reason
    else:
        inputs.pop("tds_source", None)
        inputs.pop("tds_override_reason", None)
