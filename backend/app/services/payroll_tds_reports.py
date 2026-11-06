"""TDS engine reads (plan 17 work package 5c, contract 19.9): admin preview, summary, the employee's own projection and the data of the
per-employee "Tax computation statement". Nothing here writes tax data; the audit rows carry ids, the FY and counts only."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    EmployeePayrollDetails,
    PayrollRun,
    PayrollTaxComputation,
    Payslip,
    SalaryStructure,
    User,
)
from . import payroll_calc as calc
from . import payroll_common as pc
from . import payroll_tds as ptds
from . import tax_engine as tx
from . import tax_rules as tr
from .license import get_company
from .tax_declarations import MAX_REPORT_ROWS

ZERO = Decimal("0.00")
_PERIOD = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
RANK = {"published": 3, "processed": 2, "draft": 1}


def _bad(field: str, msg: str) -> AppError:
    return AppError("VALIDATION_ERROR", msg, details=[{"field": field, "message": msg}])


async def resolve_period(session: AsyncSession, fy: str, raw: str | None) -> dt.date:
    """The month to compute: `raw` (must lie inside the FY), else this month for the current FY, the first month of a future FY or the
    last month of a past FY."""
    first, last = tr.fy_bounds(fy)
    if raw is not None:
        m = _PERIOD.match(raw)
        if not m:
            raise _bad("period", "period must be YYYY-MM")
        d = dt.date(int(m.group(1)), int(m.group(2)), 1)
        if not first <= d <= last.replace(day=1):
            raise _bad("period", "period must be inside the financial year")
        return d
    today = await tr.today(session)
    if today < first:
        return first
    if today > last:
        return last.replace(day=1)
    return today.replace(day=1)


async def _structs(session: AsyncSession, user_id: uuid.UUID, fy: str) -> list[tuple[dt.date, SalaryStructure]]:
    end = tr.fy_bounds(fy)[1]
    q = select(SalaryStructure).where(SalaryStructure.user_id == user_id, SalaryStructure.effective_from <= end)
    return [(s.effective_from, s) for s in (await session.execute(q)).scalars()]


def _too_many(n: int) -> None:
    if n > MAX_REPORT_ROWS:
        raise AppError("TOO_MANY_ROWS", "Too many rows for one report")


async def _live_payslip(
    session: AsyncSession, user_id: uuid.UUID, period: dt.date, *, published_only: bool = False
) -> Payslip | None:
    q = (
        select(Payslip)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(Payslip.user_id == user_id, PayrollRun.period == period, Payslip.status != "superseded")
    )
    if published_only:
        q = q.where(Payslip.status == "published")
    best: Payslip | None = None
    for p in (await session.execute(q)).scalars():
        if p.result is None:
            continue
        if best is None or (RANK.get(p.status, 0), p.revision) > (RANK.get(best.status, 0), best.revision):
            best = p
    return best


def compose(rc: ptds.RunCtx, figs: dict[str, Any], flags: list[str]) -> dict[str, Any]:
    """A TdsComputation (19.9) for a computation that is not tied to a payslip (preview, projection, statement)."""
    assert rc.row is not None
    return {
        "fy": rc.fy, "mode": rc.mode, "rules": {"id": str(rc.row.id), "version": rc.row.version}, "regime": figs["regime"],
        "regime_source": figs["regime_source"], "declaration_version": figs["declaration_version"], "proof_basis": figs["proof_basis"],
        "remaining_months": figs["remaining_months"], "annual": figs["annual"], "tds_paid_so_far": figs["tds_paid_so_far"],
        "previous_employer_tds": figs["previous_employer_tds"], "remaining_tax": figs["remaining_tax"],
        "one_time_tax": figs["one_time_tax"], "suggested": figs["suggested"], "applied": None, "override": None,
        "flags": flags, "steps": figs["steps"], "shortfall": "0.00", "computed_at": pc.iso(ptds.utcnow()),
    }  # fmt: skip


async def estimate(
    session: AsyncSession, rc: ptds.RunCtx, user_id: uuid.UUID, period: dt.date, regime: str | None = None, *,
    published_only: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:  # fmt: skip
    """(TdsComputation, other regime summary) for one employee and month: the month's live payslip when there is one, else the
    structure in force. Based on the SUBMITTED declaration and the verified proofs only. `published_only` (the employee's own
    projection): only PUBLISHED payslips feed the figures (earlier months and this month), so nothing unpublished leaks."""
    assert rc.row is not None and rc.rules is not None
    ud = (await ptds.load_data(session, rc.fy, period, [user_id], published_only=published_only))[user_id]
    structs = await _structs(session, user_id, rc.fy)
    det = await session.get(EmployeePayrollDetails, user_id)
    lwd = det.last_working_day if det is not None else None
    treat = rc.rules["component_treatment"]
    slip = await _live_payslip(session, user_id, period, published_only=published_only)
    if slip is not None:
        this = ptds.this_from_result(slip.result, treat)
        pan = bool((slip.identity or {}).get("pan"))
        final = ptds.is_final(slip.inputs, lwd, period)
    else:
        this = ptds.this_from_structure(structs, period, treat)
        pan = bool(det is not None and det.pan)
        final = ptds.is_final(None, lwd, period)
    inp = ptds.build_input(rc, ud, period=period, this=this, structs=structs, lwd=lwd, final=final, has_pan=pan)
    figs = tx.compute(rc.rules, inp, regime)
    flags = list(figs["flags"]) + ptds.extra_flags(rc, ud, this, final)
    other = "old" if figs["regime"] == "new" else "new"
    alt = tx.compute(rc.rules, inp, other)
    return compose(rc, figs, flags), {"regime": other, "total_tax": alt["annual"]["total_tax"]}


# ============================================================================================ admin preview
async def preview(session: AsyncSession, actor: Principal, *, user_id: uuid.UUID, fy: str | None, period: str | None,
                  regime: str | None) -> dict[str, Any]:  # fmt: skip
    user = await session.get(User, user_id)
    if user is None:
        raise AppError("NOT_FOUND", "Employee not found")
    if fy is not None:
        tr.check_fy(fy)
        start = await resolve_period(session, fy, period)
    elif period is not None:
        m = _PERIOD.match(period)
        if not m:
            raise _bad("period", "period must be YYYY-MM")
        start = dt.date(int(m.group(1)), int(m.group(2)), 1)
        fy = tr.fy_of(start)
    else:
        fy = tr.fy_of(await tr.today(session))
        start = await resolve_period(session, fy, None)
    rc = await ptds.current_ctx(session, fy)
    comp, other = await estimate(session, rc, user_id, start, regime)
    audit(session, action="payroll.tax.preview", actor=actor, entity_type="payroll_tax", entity_id=user_id,
          after={"user_id": str(user_id), "fy": fy})  # fmt: skip
    await pc.commit(session)
    return {"computation": comp, "other_regime": other}


# ============================================================================================ employee projection
async def own_projection(session: AsyncSession, actor: Principal, fy: str | None) -> dict[str, Any]:
    fy = tr.check_fy(fy) if fy else tr.fy_of(await tr.today(session))
    rc = await ptds.current_ctx(session, fy)
    period = await resolve_period(session, fy, None)
    comp, other = await estimate(session, rc, actor.id, period, published_only=True)
    comp["override"] = None
    comp["flags"] = [f for f in comp["flags"] if f not in ptds.HR_ONLY_FLAGS]  # HR review flags are not for the employee
    audit(session, action="payroll.tax.view_own", actor=actor, entity_type="payroll_tax", entity_id=actor.id, after={"fy": fy})
    await pc.commit(session)
    return {"estimate": True, "computation": comp, "other_regime": other}


# ============================================================================================ admin summary
def _tds_line(p: Payslip) -> Decimal:
    return ptds._ded(p.result or {}, "TDS")


async def summary(
    session: AsyncSession, actor: Principal, *, fy: str | None, q: str | None, department_id: uuid.UUID | None, flag: str | None,
    page: PageParams,
) -> dict[str, Any]:  # fmt: skip
    fy = tr.check_fy(fy) if fy else tr.fy_of(await tr.today(session))
    await ptds.current_ctx(session, fy)  # 409 when the engine is off / the rules are not verified
    rows = (
        await session.execute(
            select(PayrollTaxComputation, Payslip, PayrollRun.period)
            .join(Payslip, Payslip.id == PayrollTaxComputation.payslip_id)
            .join(PayrollRun, PayrollRun.id == Payslip.run_id)
            .where(PayrollTaxComputation.fy == fy, Payslip.status != "superseded")
            .limit(MAX_REPORT_ROWS + 1)
        )
    ).all()
    _too_many(len(rows))
    latest: dict[uuid.UUID, tuple[PayrollTaxComputation, Payslip, dt.date]] = {}
    for tc, ps, per in rows:
        cur = latest.get(tc.user_id)
        if cur is None or (per, RANK.get(ps.status, 0), ps.revision) > (cur[2], RANK.get(cur[1].status, 0), cur[1].revision):
            latest[tc.user_id] = (tc, ps, per)
    users = {}
    if latest:
        uq = select(User).where(User.id.in_(list(latest)))
        if department_id is not None:
            uq = uq.where(User.department_id == department_id)
        users = {u.id: u for u in (await session.execute(uq)).unique().scalars()}
    needle = (q or "").strip().lower()
    items: list[tuple[str, str, dict[str, Any]]] = []
    tot_tax = tot_paid = tot_rem = ZERO
    for uid, (tc, ps, _per) in latest.items():
        u = users.get(uid)
        if u is None or (needle and needle not in u.full_name.lower() and needle not in u.employee_id.lower()):
            continue
        c = tc.computation or {}
        flags = list(c.get("flags", []))
        if flag and flag not in flags:
            continue
        annual = c.get("annual") or {}
        tax, paid, rem = (
            Decimal(annual.get("total_tax", "0")),
            Decimal(c.get("tds_paid_so_far", "0")),
            Decimal(c.get("remaining_tax", "0")),
        )
        tot_tax, tot_paid, tot_rem = tot_tax + tax, tot_paid + paid, tot_rem + rem
        items.append(
            (
                u.full_name.lower(), u.employee_id,
                {
                    "user": pc.user_out(u), "regime": tc.regime, "total_tax": tx.money(tax), "tds_paid_so_far": tx.money(paid),
                    "remaining_tax": tx.money(rem), "suggested_this_month": tc.suggested,
                    "manual_this_month": calc.money_str(_tds_line(ps)),  # the TDS line of the payslip (manual in suggest mode)
                    "flags": flags,
                },
            )
        )  # fmt: skip
    items.sort(key=lambda t: (t[0], t[1]))
    window = [t[2] for t in items[page.offset : page.offset + page.page_size]]
    out = make_page(window, page, len(items))
    out["totals"] = {"total_tax": tx.money(tot_tax), "tds_paid_so_far": tx.money(tot_paid), "remaining_tax": tx.money(tot_rem)}
    audit(session, action="payroll.tax.summary", actor=actor, entity_type="payroll_tax", after={"count": len(items), "fy": fy})
    await pc.commit(session)
    return out


# ============================================================================================ statement data
async def statement_view(
    session: AsyncSession, actor: Principal, user_id: uuid.UUID, fy: str | None
) -> tuple[dict[str, Any], str, bytes | None]:
    """(view, footer, logo) for the "Tax computation statement" PDF. Working data only: it is NOT a Form 16."""
    from . import company as company_svc

    fy = tr.check_fy(fy) if fy else tr.fy_of(await tr.today(session))
    rc = await ptds.current_ctx(session, fy)
    user = await session.get(User, user_id)
    if user is None:
        raise AppError("NOT_FOUND", "Employee not found")
    det = await session.get(EmployeePayrollDetails, user_id)
    first, last = tr.fy_bounds(fy)
    q = (
        select(Payslip, PayrollRun.period)
        .join(PayrollRun, PayrollRun.id == Payslip.run_id)
        .where(Payslip.user_id == user_id, PayrollRun.period >= first, PayrollRun.period <= last, Payslip.status == "published")
        .limit(MAX_REPORT_ROWS + 1)
    )
    months: dict[dt.date, Payslip] = {}
    got = (await session.execute(q)).all()
    _too_many(len(got))
    for p, per in got:
        if p.result is not None and (per not in months or p.revision > months[per].revision):
            months[per] = p
    latest = (
        await session.execute(
            select(PayrollTaxComputation, PayrollRun.period, Payslip.status, Payslip.revision)
            .join(Payslip, Payslip.id == PayrollTaxComputation.payslip_id)
            .join(PayrollRun, PayrollRun.id == Payslip.run_id)
            .where(PayrollTaxComputation.fy == fy, PayrollTaxComputation.user_id == user_id, Payslip.status != "superseded")
            .limit(MAX_REPORT_ROWS + 1)
        )
    ).all()
    _too_many(len(latest))
    if latest:
        tc = max(latest, key=lambda r: (r[1], RANK.get(r[2], 0), r[3]))[
            0
        ]  # latest month, then published > processed > draft, then revision
        comp = ptds.computation_out(tc)
    else:
        comp, _ = await estimate(session, rc, user_id, await resolve_period(session, fy, None))
    ud = (await ptds.load_data(session, fy, last, [user_id]))[user_id]
    sections = {s["code"]: s["name"] for s in (rc.rules or {}).get("sections", [])}
    verified = ptds.matched_verified(ud)
    decl = ud.declaration
    items = []
    if decl is not None and decl.submitted is not None:
        for it in decl.submitted.get("items", []):
            items.append({"section": it["section"], "name": sections.get(it["section"], it["section"]), "declared": it["amount"],
                          "verified": tx.money(verified[it["id"]]) if it["id"] in verified else None})  # fmt: skip
    company = await get_company(session)
    settings = await pc_settings(session)
    logo = await company_svc.logo_bytes(session)
    pan = det.pan if det is not None else None
    view = {
        "company": {"name": company.name or "", "address": (settings.get("payroll.company_address") or None)},
        "employee": {
            "employee_id": user.employee_id,
            "full_name": user.full_name,
            "department": user.department.name if user.department else None,
            "designation": det.designation if det else None,
            "pan": pc.mask_pan(pan),
        },
        "fy": fy,
        "regime": comp["regime"],
        "regime_source": comp["regime_source"],
        "annual": comp["annual"],
        "remaining_tax": comp["remaining_tax"],
        "tds_paid_so_far": comp["tds_paid_so_far"],
        "previous_employer_tds": comp["previous_employer_tds"],
        "months": [
            {"period": pc.period_str(per), "gross": p.result["gross"], "tds": calc.money_str(_tds_line(p))}
            for per, p in sorted(months.items())
        ],  # fmt: skip
        "declarations": items,
        "flags": comp["flags"],
        "rules_version": comp["rules"]["version"],
    }
    return view, str(settings.get("payroll.payslip_footer") or ""), (logo[0] if logo else None)


async def pc_settings(session: AsyncSession) -> dict[str, Any]:
    from . import payroll_settings as pset

    return (await pset.read_all(session))["settings"]
