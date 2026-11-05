"""Payslip delivery tracking, push only (plan 17 work package 5a, contract 19.3).

One `payslip_deliveries` row per published payslip (created by the publish). `first_viewed_at` / `first_downloaded_at` are write-once
and set by the owner's own payslip endpoints with a conditional UPDATE (a concurrent second view matches no row, so the database
trigger never sees a second write). HR sees a report and can send reminder pushes: at most one per payslip per 24 hours and three in
total. Nothing here carries amounts; audit rows hold ids and counts.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import as_utc, utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import DeviceToken, Payslip, PayslipDelivery, User
from ..schemas.payroll_controls import DeliveryRemind
from . import notifications
from . import payroll_common as pc
from . import payroll_runs as runs

log = logging.getLogger("app.payroll")
MAX_REMINDERS = 3
REMIND_GAP = dt.timedelta(hours=24)


async def mark_seen(session: AsyncSession, ps: Payslip, *, downloaded: bool) -> None:
    """The owner opened (or downloaded) a payslip. Best effort inside the caller's transaction: a failure here never blocks the
    employee (it runs in a savepoint)."""
    now = utcnow()
    try:
        async with session.begin_nested():
            if await session.get(PayslipDelivery, ps.id) is None:  # published before migration 0026: create the row now
                session.add(PayslipDelivery(payslip_id=ps.id, user_id=ps.user_id))
                await session.flush()
            col = PayslipDelivery
            await session.execute(
                update(col).where(col.payslip_id == ps.id, col.first_viewed_at.is_(None)).values(first_viewed_at=now)
            )
            if downloaded:
                await session.execute(
                    update(col).where(col.payslip_id == ps.id, col.first_downloaded_at.is_(None)).values(first_downloaded_at=now)
                )
    except Exception:  # noqa: BLE001
        log.exception("delivery tracking failed (ignored)")


async def _published(session: AsyncSession, run_id: uuid.UUID) -> list[Payslip]:
    q = select(Payslip).where(Payslip.run_id == run_id, Payslip.status == "published").order_by(Payslip.user_id)
    return list((await session.execute(q)).scalars())


async def _devices(session: AsyncSession, user_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not user_ids:
        return set()
    return set((await session.execute(select(DeviceToken.user_id).where(DeviceToken.user_id.in_(user_ids)).distinct())).scalars())


async def report(
    session: AsyncSession, actor: Principal, run_id: uuid.UUID, *, state: str | None, q: str | None, page: PageParams
) -> dict[str, Any]:
    run = await runs._run_or_404(session, run_id)
    rows = await _published(session, run.id)
    deliveries = (
        {
            d.payslip_id: d
            for d in (
                await session.execute(select(PayslipDelivery).where(PayslipDelivery.payslip_id.in_([p.id for p in rows])))
            ).scalars()
        }
        if rows
        else {}
    )
    users = (
        {u.id: u for u in (await session.execute(select(User).where(User.id.in_([p.user_id for p in rows])))).unique().scalars()}
        if rows
        else {}
    )
    devices = await _devices(session, [p.user_id for p in rows])
    counts = {"published": len(rows), "pushed": 0, "viewed": 0, "downloaded": 0, "no_device": 0}
    needle = (q or "").strip().lower()
    items = []
    for p in rows:
        d, u = deliveries.get(p.id), users[p.user_id]
        has_device = p.user_id in devices
        counts["pushed"] += bool(d and d.pushed_at)
        counts["viewed"] += bool(d and d.first_viewed_at)
        counts["downloaded"] += bool(d and d.first_downloaded_at)
        counts["no_device"] += not has_device
        if needle and needle not in u.full_name.lower() and needle not in u.employee_id.lower():
            continue
        viewed = bool(d and d.first_viewed_at)
        if state == "viewed" and not viewed or state == "not_viewed" and viewed or state == "no_device" and has_device:
            continue
        items.append((u.full_name.lower(), u.employee_id, p, d, u, has_device))
    items.sort(key=lambda t: (t[0], t[1]))
    window = items[page.offset : page.offset + page.page_size]
    out = [
        {
            "user": pc.user_out(u),
            "payslip_id": str(p.id),
            "revision": p.revision,
            "pushed_at": pc.iso(d.pushed_at) if d else None,
            "push_count": d.push_count if d else 0,
            "reminders": d.reminders if d else 0,
            "first_viewed_at": pc.iso(d.first_viewed_at) if d else None,
            "first_downloaded_at": pc.iso(d.first_downloaded_at) if d else None,
            "has_device": has,
        }  # fmt: skip
        for _, _, p, d, u, has in window
    ]
    audit(
        session,
        action="payroll.delivery.view",
        actor=actor,
        entity_type="payroll_run",
        entity_id=run.id,
        after={"count": len(out)},
    )
    await pc.commit(session)
    return {"counts": counts, **make_page(out, page, len(items))}


async def remind(session: AsyncSession, actor: Principal, run_id: uuid.UUID, body: DeliveryRemind) -> dict[str, Any]:
    run = await runs._run_or_404(session, run_id)
    if run.status != "published":
        raise AppError("PAYROLL_LOCKED", "Reminders are only sent for a published run")
    rows = await _published(session, run.id)
    by_user = {p.user_id: p for p in rows}
    devices = await _devices(session, list(by_user))
    ids = [p.id for p in rows]
    deliveries: dict[uuid.UUID, PayslipDelivery] = {}
    if ids:
        q = (
            select(PayslipDelivery)
            .where(PayslipDelivery.payslip_id.in_(ids))
            .order_by(PayslipDelivery.payslip_id)
            .with_for_update()
        )
        deliveries = {d.payslip_id: d for d in (await session.execute(q)).scalars()}
    now = utcnow()
    if body.user_ids is None:
        targets = [
            u for u, p in by_user.items()
            if u in devices and not (deliveries.get(p.id) and deliveries[p.id].first_viewed_at)
        ]  # fmt: skip
    else:
        targets = list(dict.fromkeys(body.user_ids))
    sent: list[tuple[uuid.UUID, uuid.UUID]] = []
    skipped: list[dict[str, str]] = []
    for uid in targets:
        p = by_user.get(uid)
        d = deliveries.get(p.id) if p else None
        if p is None:
            reason = "NOT_PUBLISHED"
        elif d and d.first_viewed_at:
            reason = "VIEWED"
        elif uid not in devices:
            reason = "NO_DEVICE"
        elif d and d.reminders >= MAX_REMINDERS:
            reason = "LIMIT_REACHED"
        elif d and d.last_reminded_at and as_utc(d.last_reminded_at) + REMIND_GAP > now:
            reason = "TOO_SOON"
        else:
            reason = ""
        if reason:
            skipped.append({"user_id": str(uid), "reason": reason})
            continue
        if d is None:
            d = PayslipDelivery(payslip_id=p.id, user_id=uid)  # type: ignore[union-attr]
            session.add(d)
        d.reminders, d.last_reminded_at = (d.reminders or 0) + 1, now
        sent.append((uid, p.id))  # type: ignore[union-attr]
    audit(session, action="payroll.delivery.remind", actor=actor, entity_type="payroll_run", entity_id=run.id,
          after={"run_id": str(run.id), "sent": len(sent), "skipped": len(skipped)})  # fmt: skip
    period = run.period

    async def _push() -> None:
        for uid, pid in sent:
            try:
                await notifications.notify(
                    session, [uid], type="payslip_reminder", entity_id=pid, title="Payslip available",
                    body=f"Your payslip for {runs.month_label(period)} is waiting in the app.",
                )  # fmt: skip
            except Exception:  # noqa: BLE001
                log.exception("payslip_reminder push failed (ignored)")

    pc.after_commit(session, _push)
    await pc.commit(session)
    return {"sent": len(sent), "skipped": skipped}
