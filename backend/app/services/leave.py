"""Leave + permission + on-duty requests and the approval flow (plans 03 and 13).

Decisions on the plan's open questions (also in docs/api-contract.md section 4):
  O1 pro-rata   a balance row created for the join year of a mid-year joiner gets
                annual_quota * remaining_days_of_year / days_in_year, rounded half-up to 0.5. Other years: full quota.
  O2 year end   a leave request must lie inside ONE calendar year (INVALID_DATE_RANGE); the client splits it.
  O3 cancel     only pending requests can be cancelled. Cancelling an APPROVED request (only before it starts) is
                available behind the setting `leave.allow_cancel_approved` (default false) and restores the balance.
                The same setting governs approved PERMISSION requests.
  O4 unpaid     a leave type with is_paid=false and annual_quota=0 is "LOP": its balance quota is 365.0 (effectively
                unlimited; the DB check used+pending<=quota needs a finite number).
  O5 holidays   `days` is computed once at submit time and never recalculated.
  carry-forward none in v1: every year starts from the type's quota (the rollover job only creates the rows).

Locking order: user row (apply only) -> request row -> balance row. Balance math is
`pending += days` on submit, `pending -= days; used += days` on approve, `pending -= days` on reject/cancel(pending),
`used -= days` on cancel(approved); DB CHECKs (used,pending >= 0, used+pending <= quota) are the safety net.

On-duty requests (submitted in services/onduty.py) share list / get / cancel / approve / reject with leave and permission.
They have no balance. A leave and an on-duty request may never cover the same day (checked on submit and on approval).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import iso_z, utcnow
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import (
    ClientLocation,
    File,
    LeaveBalance,
    LeaveRequest,
    LeaveType,
    OnDutyRequest,
    Permission,
    PermissionRequest,
    Role,
    RolePermission,
    Setting,
    User,
)
from . import attendance as att
from . import files as files_svc
from .notifications import notify

log = logging.getLogger("app.leave")

TENTH = Decimal("0.1")
HALF = Decimal("0.5")
UNLIMITED_QUOTA = Decimal("365.0")
MAX_QUOTA = Decimal("999.9")

_DEFAULTS: dict[str, Any] = {
    "office.timezone": "Asia/Kolkata",
    "leave.backdate_days": 7,
    "leave.allow_cancel_approved": False,
    "leave.max_advance_days": 365,  # 0 = no horizon (the 2000-2200 year range still applies)
    "leave.reason_retention_days": 0,  # 0 = keep forever; N = purge reasons/comments/attachments N days after the request
    "permission.past_grace_minutes": 5,
    "permission.monthly_count_limit": 3,
    "permission.monthly_minutes_limit": 240,
    "permission.max_minutes_per_request": 120,
}

KINDS = {"leave": LeaveRequest, "permission": PermissionRequest, "on_duty": OnDutyRequest}
LABELS = {"leave": "leave", "permission": "permission", "on_duty": "on duty"}  # for push titles / bodies
DATE_RANGE_MODELS = (LeaveRequest, OnDutyRequest)  # models with from_date / to_date (permission has request_date)
MIN_YEAR, MAX_YEAR = 2000, 2200
MERGE_GAP_SEARCH_DAYS = 31  # adjacency search window around a new request (a gap of only weekly-offs/holidays)
NEWREQ_PUSH_COALESCE_S = 600  # no-manager fan-out: one push per (requester, approver) per 10 minutes
MAX_FANOUT = 20


def d1(v: Any) -> Decimal:
    return Decimal(str(v)).quantize(TENTH)


def num(v: Any) -> float:
    return float(d1(v))


async def _cfg(session: AsyncSession) -> dict[str, Any]:
    rows = {r.key: r.value for r in (await session.execute(select(Setting).where(Setting.key.in_(list(_DEFAULTS))))).scalars()}
    return {k: rows.get(k, d) for k, d in _DEFAULTS.items()}


def _today(cfg: dict[str, Any]) -> dt.date:
    return utcnow().astimezone(att.office_tz(cfg)).date()


def _err(code: str, message: str, field: str | None = None) -> AppError:
    return AppError(code, message, details=[{"field": field, "message": message}] if field else None)


# ================================================================ balances
def initial_quota(lt: LeaveType, user: User, year: int) -> Decimal:
    base = d1(lt.annual_quota)
    if not lt.is_paid and base == 0:
        return UNLIMITED_QUOTA
    jd = user.join_date
    if jd is not None and base > 0:
        if jd.year > year:
            return d1(0)
        if jd.year == year and jd > dt.date(year, 1, 1):
            total = (dt.date(year, 12, 31) - dt.date(year, 1, 1)).days + 1
            left = (dt.date(year, 12, 31) - jd).days + 1
            raw = base * Decimal(left) / Decimal(total)
            base = ((raw * 2).quantize(Decimal(1), rounding=ROUND_HALF_UP) / 2).quantize(TENTH)
    return min(base, MAX_QUOTA)


async def _get_or_create(
    session: AsyncSession, user: User, lt: LeaveType, year: int, *, lock: bool = False
) -> tuple[LeaveBalance, bool]:
    q = select(LeaveBalance).where(
        LeaveBalance.user_id == user.id, LeaveBalance.leave_type_id == lt.id, LeaveBalance.year == year
    )
    if lock:
        q = q.with_for_update()
    bal = (await session.execute(q)).scalar_one_or_none()
    if bal is not None:
        return bal, False
    try:
        async with session.begin_nested():
            session.add(
                LeaveBalance(
                    user_id=user.id,
                    leave_type_id=lt.id,
                    year=year,
                    quota=initial_quota(lt, user, year),
                    used=d1(0),
                    pending=d1(0),
                )
            )
    except IntegrityError:  # a concurrent request created it first
        bal = (await session.execute(q)).scalar_one_or_none()
        if bal is None:  # not a race (e.g. the year CHECK): never turn it into a 500 NoResultFound
            raise
        return bal, False
    return (await session.execute(q)).scalar_one(), True


async def get_balance(session: AsyncSession, user: User, lt: LeaveType, year: int, *, lock: bool = False) -> LeaveBalance:
    """Lazily creates the (user, type, year) row. `lock=True` takes SELECT ... FOR UPDATE."""
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise _err("INVALID_DATE_RANGE", "Year is outside the supported range", "from_date")
    return (await _get_or_create(session, user, lt, year, lock=lock))[0]


async def ensure_balances(session: AsyncSession, user: User, year: int) -> int:
    """Create missing balance rows for every active leave type. Caller commits. Returns rows actually created."""
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise _err("INVALID_DATE_RANGE", "Year is outside the supported range")
    types = (await session.execute(select(LeaveType).where(LeaveType.is_active.is_(True)))).scalars().all()
    have = set(
        (
            await session.execute(
                select(LeaveBalance.leave_type_id).where(LeaveBalance.user_id == user.id, LeaveBalance.year == year)
            )
        ).scalars()
    )
    created = 0
    for lt in types:
        if lt.id not in have:
            _, made = await _get_or_create(session, user, lt, year)
            created += int(made)
    return created


async def rollover_balances(session: AsyncSession, year: int) -> int:
    """Jan 1 job: balance rows for `year` for every active user (no carry-forward in v1). Idempotent.
    One transaction per user: a bad row for one user is logged and skipped, the rest still get their rows."""
    ids = list(
        (await session.execute(select(User.id).where(User.status == "active", User.deleted_at.is_(None)).order_by(User.id)))
        .unique()
        .scalars()
    )
    total = failed = 0
    for uid in ids:
        try:
            u = await session.get(User, uid)
            if u is None:
                continue
            total += await ensure_balances(session, u, year)
            await session.commit()
        except Exception:  # noqa: BLE001 - isolate one user's failure
            await session.rollback()
            failed += 1
            log.exception("rollover failed for user %s (year %s); continuing", uid, year)
    audit(
        session,
        action="leave.rollover",
        entity_type="leave_balance",
        after={"year": year, "created": total, "failed_users": failed},
        outcome="failure" if failed else "success",
    )
    await session.commit()
    return total


async def _move(
    session: AsyncSession, bal: LeaveBalance, *, used: Decimal = Decimal("0.0"), pending: Decimal = Decimal("0.0")
) -> None:
    new_used, new_pending = d1(bal.used) + used, d1(bal.pending) + pending
    if new_used < 0 or new_pending < 0:
        log.error("balance %s would go negative (used=%s pending=%s)", bal.id, new_used, new_pending)
        raise AppError("CONFLICT", "Leave balance is inconsistent; contact HR")
    if new_used + new_pending > d1(bal.quota):
        raise _err("INSUFFICIENT_BALANCE", "Not enough leave balance", "days")
    bal.used, bal.pending = new_used, new_pending
    try:
        await session.flush()
    except IntegrityError as exc:  # CHECK constraint safety net
        raise _err("INSUFFICIENT_BALANCE", "Not enough leave balance", "days") from exc


async def on_duty_overlaps(
    session: AsyncSession, user_id: uuid.UUID, d_from: dt.date, d_to: dt.date, statuses: tuple[str, ...]
) -> bool:
    """A pending/approved (per `statuses`) on-duty request of this user touching d_from..d_to."""
    q = select(OnDutyRequest.id).where(
        OnDutyRequest.user_id == user_id,
        OnDutyRequest.status.in_(statuses),
        OnDutyRequest.from_date <= d_to,
        OnDutyRequest.to_date >= d_from,
    )
    return (await session.execute(q.limit(1))).first() is not None


# ================================================================ output
async def _users(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    if not ids:
        return {}
    rows = (await session.execute(select(User).where(User.id.in_(ids)))).unique().scalars()
    return {u.id: u for u in rows}


def _hm(t: dt.time) -> str:
    return t.strftime("%H:%M")


async def _render(session: AsyncSession, rows: list, *, admin: bool) -> list[dict[str, Any]]:
    uids = {r.approver_id for r in rows if r.approver_id}
    if admin:
        uids |= {r.user_id for r in rows}
    users = await _users(session, uids)
    photos = await files_svc.photo_file_ids(session, list(users))
    type_ids = {r.leave_type_id for r in rows if isinstance(r, LeaveRequest)}
    types = {}
    if type_ids:
        types = {t.id: t for t in (await session.execute(select(LeaveType).where(LeaveType.id.in_(type_ids)))).scalars()}
    file_ids = {r.attachment_file_id for r in rows if isinstance(r, LeaveRequest) and r.attachment_file_id}
    loc_ids = {r.client_location_id for r in rows if isinstance(r, OnDutyRequest)}
    locs: dict[uuid.UUID, ClientLocation] = {}
    if loc_ids:
        locs = {c.id: c for c in (await session.execute(select(ClientLocation).where(ClientLocation.id.in_(loc_ids)))).scalars()}
    ctypes: dict[uuid.UUID, str] = {}
    if file_ids:
        ctypes = {
            fid: ct for fid, ct in (await session.execute(select(File.id, File.content_type).where(File.id.in_(file_ids)))).all()
        }
    out = []
    for r in rows:
        tail = {
            "reason": r.reason or "",  # "" once the retention job purged it
            "status": r.status,
            "approver": att.user_summary(users[r.approver_id], photos.get(r.approver_id)) if r.approver_id else None,
            "decision_comment": r.decision_comment,
            "decided_at": iso_z(r.decided_at) if r.decided_at else None,
            "created_at": iso_z(r.created_at),
        }
        if isinstance(r, LeaveRequest):
            lt = types[r.leave_type_id]
            item = {
                "id": str(r.id),
                "leave_type": {"id": str(lt.id), "code": lt.code, "name": lt.name},
                "from_date": r.from_date.isoformat(),
                "to_date": r.to_date.isoformat(),
                "half_day": r.half_day,
                "half_day_session": r.half_day_session,
                "days": num(r.days),
                "attachment_url": files_svc.file_url(r.attachment_file_id),
                "attachment_content_type": ctypes.get(r.attachment_file_id) if r.attachment_file_id else None,
                **tail,
            }
        elif isinstance(r, OnDutyRequest):
            loc = locs[r.client_location_id]
            item = {
                "id": str(r.id),
                "client_location": {"id": str(loc.id), "name": loc.name, "address": loc.address},
                "from_date": r.from_date.isoformat(),
                "to_date": r.to_date.isoformat(),
                "days": r.days,
                **tail,
            }
        else:
            item = {
                "id": str(r.id),
                "date": r.request_date.isoformat(),
                "from_time": _hm(r.from_time),
                "to_time": _hm(r.to_time),
                "minutes": r.minutes,
                **tail,
            }
        if admin:
            u = users[r.user_id]
            item["type"] = next(k for k, m in KINDS.items() if isinstance(r, m))
            item["user"] = att.user_summary(u, photos.get(u.id))
        out.append(item)
    return out


async def _one(session: AsyncSession, row, *, admin: bool = False) -> dict[str, Any]:
    return (await _render(session, [row], admin=admin))[0]


# ================================================================ read endpoints
async def list_types(session: AsyncSession) -> dict[str, Any]:
    q = select(LeaveType).where(LeaveType.is_active.is_(True)).order_by(LeaveType.name)
    return {
        "items": [
            {
                "id": str(t.id),
                "code": t.code,
                "name": t.name,
                "annual_quota": num(t.annual_quota),
                "is_paid": t.is_paid,
                "allow_half_day": t.allow_half_day,
                "requires_attachment_after_days": t.requires_attachment_after_days,
                "max_consecutive_days": t.max_consecutive_days,
            }
            for t in (await session.execute(q)).scalars()
        ]
    }


def _month_bounds(d: dt.date) -> tuple[dt.date, dt.date]:
    first = d.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return first, nxt - dt.timedelta(days=1)


async def permission_usage(session: AsyncSession, user_id: uuid.UUID, month_day: dt.date) -> dict[str, int]:
    first, last = _month_bounds(month_day)
    rows = (
        await session.execute(
            select(PermissionRequest.status, func.count(), func.coalesce(func.sum(PermissionRequest.minutes), 0))
            .where(
                PermissionRequest.user_id == user_id,
                PermissionRequest.request_date >= first,
                PermissionRequest.request_date <= last,
                PermissionRequest.status.in_(("pending", "approved")),
            )
            .group_by(PermissionRequest.status)
        )
    ).all()
    agg = {s: (int(c), int(m)) for s, c, m in rows}
    return {
        "used_count": agg.get("approved", (0, 0))[0],
        "used_minutes": agg.get("approved", (0, 0))[1],
        "pending_count": agg.get("pending", (0, 0))[0],
        "pending_minutes": agg.get("pending", (0, 0))[1],
    }


async def balance(session: AsyncSession, actor: Principal, year: int | None) -> dict[str, Any]:
    cfg = await _cfg(session)
    today = _today(cfg)
    year = year or today.year
    user = actor.user
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise _err("VALIDATION_ERROR", "year out of range", "year")
    # Rows are only ever written for the previous, current and next year; any other year is computed, never stored.
    persist = today.year - 1 <= year <= today.year + 1
    if persist:
        await ensure_balances(session, user, year)
        await session.commit()
    rows = (
        await session.execute(
            select(LeaveBalance, LeaveType)
            .join(LeaveType, LeaveType.id == LeaveBalance.leave_type_id)
            .where(LeaveBalance.user_id == user.id, LeaveBalance.year == year)
            .order_by(LeaveType.name)
        )
    ).all()
    entries = [(b.quota, b.used, b.pending, lt) for b, lt in rows]
    if not persist:
        have = {lt.id for _, _, _, lt in entries}
        types = (await session.execute(select(LeaveType).where(LeaveType.is_active.is_(True)))).scalars().all()
        entries += [(initial_quota(lt, user, year), d1(0), d1(0), lt) for lt in types if lt.id not in have]
        entries.sort(key=lambda e: e[3].name)
    leave = [
        {
            "leave_type_id": str(lt.id),
            "code": lt.code,
            "name": lt.name,
            "quota": num(quota),
            "used": num(used),
            "pending": num(pending),
            "remaining": num(d1(quota) - d1(used) - d1(pending)),
        }
        for quota, used, pending, lt in entries
        if lt.is_active or d1(used) + d1(pending) > 0
    ]
    u = await permission_usage(session, user.id, today)
    lim_c, lim_m = int(cfg["permission.monthly_count_limit"]), int(cfg["permission.monthly_minutes_limit"])
    return {
        "year": year,
        "leave": leave,
        "permission": {
            "month": f"{today.year:04d}-{today.month:02d}",
            "limit_count": lim_c,
            "used_count": u["used_count"],
            "pending_count": u["pending_count"],
            "remaining_count": max(0, lim_c - u["used_count"] - u["pending_count"]),
            "limit_minutes": lim_m,
            "used_minutes": u["used_minutes"],
            "pending_minutes": u["pending_minutes"],
            "remaining_minutes": max(0, lim_m - u["used_minutes"] - u["pending_minutes"]),
        },
    }


# ================================================================ apply: leave
async def working_days(session: AsyncSession, user: User, d_from: dt.date, d_to: dt.date) -> int:
    holidays = await att.holiday_map(session, d_from, d_to)
    n, d = 0, d_from
    while d <= d_to:
        if not att.is_weekly_off(user.shift, d) and d not in holidays:
            n += 1
        d += dt.timedelta(days=1)
    return n


async def _lock_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialises concurrent submits of one user (the EXCLUDE constraints stay the final guard)."""
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())


async def _holders(session: AsyncSession, codes: tuple[str, ...], *, exclude: uuid.UUID | None = None) -> list[uuid.UUID]:
    q = (
        select(User.id)
        .join(Role, Role.id == User.role_id)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(Permission.code.in_(codes), User.status == "active", User.deleted_at.is_(None))
        .distinct()
        .order_by(User.id)
    )
    if exclude is not None:
        q = q.where(User.id != exclude)
    return list((await session.execute(q)).scalars())


async def _approvers(session: AsyncSession, requester: User) -> tuple[list[uuid.UUID], bool]:
    """(recipients, is_fanout). The manager is notified only when active AND able to act (request.approve_team or
    approve_all); otherwise every active request.approve_all holder (capped), which is a fan-out."""
    if requester.manager_id:
        m = await session.get(User, requester.manager_id)
        if m is not None and m.status == "active" and m.deleted_at is None:
            if m.id in await _holders(session, ("request.approve_team", "request.approve_all")):
                return [m.id], False
    return (await _holders(session, ("request.approve_all",), exclude=requester.id))[:MAX_FANOUT], True


async def _coalesce(redis: Redis | None, requester_id: uuid.UUID, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Fan-out dedupe: at most one new-request push per (requester, approver) per 10 minutes. Redis trouble = no fan-out."""
    if redis is None:
        return ids
    keep = []
    for uid in ids:
        try:
            if await redis.set(f"push:newreq:{requester_id}:{uid}", "1", nx=True, ex=NEWREQ_PUSH_COALESCE_S):
                keep.append(uid)
        except Exception:  # noqa: BLE001
            log.error("push coalescing unavailable; skipping fan-out push")
            return []
    return keep


async def _push_new_request(
    session: AsyncSession, redis: Redis | None, requester: User, entity_id: uuid.UUID, kind: str, approvers, fanout: bool
) -> None:
    ids = await _coalesce(redis, requester.id, approvers) if fanout else approvers
    await notify(
        session,
        ids,
        type="new_request",
        entity_id=entity_id,
        title=f"New {kind} request",
        body=f"{requester.full_name} requested {kind}",
    )


def _gap_is_idle(user: User, hol: dict[dt.date, str], end: dt.date, start: dt.date) -> bool:
    """True when every day strictly between `end` and `start` is a weekly-off or holiday (so the two ranges touch)."""
    d = end + dt.timedelta(days=1)
    while d < start:
        if not att.is_weekly_off(user.shift, d) and d not in hol:
            return False
        d += dt.timedelta(days=1)
    return True


async def _merged_block(
    session: AsyncSession, user: User, lt: LeaveType, d_from: dt.date, d_to: dt.date, days: Decimal, has_att: bool
) -> tuple[dt.date, dt.date, Decimal, bool]:
    """The new request merged with this user's pending/approved requests of the SAME type that touch it, directly or
    across a gap made only of weekly-offs/holidays (review 03 M2). Returns (from, to, total working days, any attachment)."""
    lo, hi = d_from - dt.timedelta(days=MERGE_GAP_SEARCH_DAYS), d_to + dt.timedelta(days=MERGE_GAP_SEARCH_DAYS)
    rows = (
        (
            await session.execute(
                select(LeaveRequest).where(
                    LeaveRequest.user_id == user.id,
                    LeaveRequest.leave_type_id == lt.id,
                    LeaveRequest.status.in_(("pending", "approved")),
                    LeaveRequest.from_date <= hi,
                    LeaveRequest.to_date >= lo,
                )
            )
        )
        .scalars()
        .all()
    )
    hol = await att.holiday_map(session, lo, hi)
    items = [(r.from_date, r.to_date, d1(r.days), r.attachment_file_id is not None, False) for r in rows]
    items.append((d_from, d_to, days, has_att, True))
    items.sort(key=lambda x: (x[0], x[1]))
    clusters: list[list] = []  # [from, to, days, has_att, contains_new]
    for f, t, dd, a, is_new in items:
        if clusters and _gap_is_idle(user, hol, clusters[-1][1], f):
            c = clusters[-1]
            c[1], c[2], c[3], c[4] = max(c[1], t), c[2] + dd, c[3] or a, c[4] or is_new
        else:
            clusters.append([f, t, dd, a, is_new])
    c = next(c for c in clusters if c[4])
    return c[0], c[1], c[2], c[3]


async def apply_leave(session: AsyncSession, actor: Principal, body: Any, redis: Redis | None = None) -> dict[str, Any]:
    user = actor.user
    cfg = await _cfg(session)
    today = _today(cfg)
    lt = await session.get(LeaveType, body.leave_type_id)
    if lt is None:
        raise AppError("NOT_FOUND", "Leave type not found")
    if not lt.is_active:
        raise _err("LEAVE_TYPE_INACTIVE", "This leave type is no longer available", "leave_type_id")
    if body.to_date < body.from_date:
        raise _err("INVALID_DATE_RANGE", "to_date must be on or after from_date", "to_date")
    if body.from_date < today - dt.timedelta(days=int(cfg["leave.backdate_days"])):
        raise _err(
            "INVALID_DATE_RANGE", f"Leave cannot start more than {cfg['leave.backdate_days']} days in the past", "from_date"
        )
    if not (MIN_YEAR <= body.from_date.year <= MAX_YEAR and MIN_YEAR <= body.to_date.year <= MAX_YEAR):
        raise _err("INVALID_DATE_RANGE", f"Dates must be between {MIN_YEAR} and {MAX_YEAR}", "from_date")
    horizon = int(cfg["leave.max_advance_days"])
    if horizon > 0 and body.to_date > today + dt.timedelta(days=horizon):
        raise _err("INVALID_DATE_RANGE", f"Leave cannot be booked more than {horizon} days ahead", "to_date")
    if body.from_date.year != body.to_date.year:
        raise _err("INVALID_DATE_RANGE", "A request cannot span two calendar years; submit one request per year", "to_date")
    if body.half_day and not lt.allow_half_day:
        raise _err("HALF_DAY_NOT_ALLOWED", "This leave type does not allow half days", "half_day")
    n = await working_days(session, user, body.from_date, body.to_date)
    if n == 0:
        raise _err("ZERO_WORKING_DAYS", "The selected dates contain no working days", "from_date")
    days = HALF if body.half_day else d1(n)
    if body.attachment_file_id:
        f = await session.get(File, body.attachment_file_id)
        if f is None or f.owner_id != user.id or f.purpose != "leave_attachment":
            raise _err("VALIDATION_ERROR", "Invalid attachment", "attachment_file_id")

    await _lock_user(session, user.id)
    clash = await session.execute(
        select(LeaveRequest.id)
        .where(
            LeaveRequest.user_id == user.id,
            LeaveRequest.status.in_(("pending", "approved")),
            LeaveRequest.from_date <= body.to_date,
            LeaveRequest.to_date >= body.from_date,
        )
        .limit(1)
    )
    if clash.first():
        raise _err("OVERLAPPING_REQUEST", "You already have a pending or approved leave on these dates", "from_date")
    if await on_duty_overlaps(session, user.id, body.from_date, body.to_date, ("pending", "approved")):
        raise _err("OVERLAPPING_REQUEST", "You have a pending or approved on-duty request on these dates", "from_date")
    # Both rules are evaluated on the merged block, so splitting one absence into adjacent requests gains nothing.
    b_from, b_to, b_days, b_att = await _merged_block(
        session, user, lt, body.from_date, body.to_date, days, bool(body.attachment_file_id)
    )
    merged = (b_from, b_to) != (body.from_date, body.to_date)
    note = " (adjacent requests of the same type count together)" if merged else ""
    if lt.max_consecutive_days and (b_to - b_from).days + 1 > lt.max_consecutive_days:
        raise _err("INVALID_DATE_RANGE", f"At most {lt.max_consecutive_days} consecutive days are allowed{note}", "to_date")
    if lt.requires_attachment_after_days is not None and b_days > lt.requires_attachment_after_days and not b_att:
        raise _err(
            "ATTACHMENT_REQUIRED",
            f"An attachment is required for more than {lt.requires_attachment_after_days} days{note}",
            "attachment_file_id",
        )
    bal = await get_balance(session, user, lt, body.from_date.year, lock=True)
    remaining = d1(bal.quota) - d1(bal.used) - d1(bal.pending)
    if remaining < days:
        raise AppError(
            "INSUFFICIENT_BALANCE", "Not enough leave balance", details=[{"field": "remaining", "message": str(remaining)}]
        )
    req = LeaveRequest(
        user_id=user.id,
        leave_type_id=lt.id,
        from_date=body.from_date,
        to_date=body.to_date,
        half_day=body.half_day,
        half_day_session=body.half_day_session if body.half_day else None,
        days=days,
        reason=body.reason,
        attachment_file_id=body.attachment_file_id,
        status="pending",
    )
    session.add(req)
    try:
        await session.flush()
    except IntegrityError as exc:  # EXCLUDE constraint (concurrent submit)
        await session.rollback()
        raise _err("OVERLAPPING_REQUEST", "You already have a pending or approved leave on these dates", "from_date") from exc
    await _move(session, bal, pending=days)
    audit(
        session,
        action="leave.apply",
        actor=actor,
        entity_type="leave_request",
        entity_id=req.id,
        after={"type": lt.code, "from": body.from_date.isoformat(), "to": body.to_date.isoformat(), "days": str(days)},
    )
    approvers, fanout = await _approvers(session, user)
    await session.commit()
    await _push_new_request(session, redis, user, req.id, "leave", approvers, fanout)
    return await _one(session, req)


# ================================================================ apply: permission
# Time model: `date` is the day the shift STARTS; for an overnight shift (end <= start) hours after midnight belong to
# that same shift. All overlap maths is done in minutes relative to the shift start day (after-midnight hours + 1440).
def _mins(t: dt.time) -> int:
    return t.hour * 60 + t.minute


def _shift_window(shift) -> tuple[int, int]:
    s, e = _mins(shift.start_time), _mins(shift.end_time)
    return (s, e + 1440) if e <= s else (s, e)


def _abs_window(shift, from_t: dt.time, to_t: dt.time) -> tuple[int, int]:
    s, e = _shift_window(shift)
    f, t = _mins(from_t), _mins(to_t)
    if e > 1440 and f < s:  # after-midnight part of an overnight shift
        f, t = f + 1440, t + 1440
    return f, t


def _session_window(shift, session_name: str) -> tuple[int, int]:
    s, e = _shift_window(shift)
    mid = s + (e - s) // 2
    return (s, mid) if session_name == "first" else (mid, e)


async def _leave_blocks_permission(
    session: AsyncSession, user_id: uuid.UUID, shift, day: dt.date, f_abs: int, t_abs: int, statuses: tuple[str, ...]
) -> bool:
    """A full-day leave on `day`, or a half-day leave whose session window overlaps the permission window."""
    rows = (
        await session.execute(
            select(LeaveRequest.half_day, LeaveRequest.half_day_session).where(
                LeaveRequest.user_id == user_id,
                LeaveRequest.status.in_(statuses),
                LeaveRequest.from_date <= day,
                LeaveRequest.to_date >= day,
            )
        )
    ).all()
    for half, sess in rows:
        if not half or shift is None:
            return True
        a, b = _session_window(shift, sess)
        if f_abs < b and t_abs > a:
            return True
    return False


async def _permission_blocks_leave(session: AsyncSession, requester: User, r: LeaveRequest) -> bool:
    """An approved permission on a day of this leave (any time for full-day leave, the session window for half-day)."""
    rows = (
        await session.execute(
            select(PermissionRequest.from_time, PermissionRequest.to_time).where(
                PermissionRequest.user_id == r.user_id,
                PermissionRequest.status == "approved",
                PermissionRequest.request_date >= r.from_date,
                PermissionRequest.request_date <= r.to_date,
            )
        )
    ).all()
    if not rows:
        return False
    if not r.half_day or requester.shift is None:
        return True
    a, b = _session_window(requester.shift, r.half_day_session)
    for f_t, t_t in rows:
        f, t = _abs_window(requester.shift, f_t, t_t)
        if f < b and t > a:
            return True
    return False


async def apply_permission(session: AsyncSession, actor: Principal, body: Any, redis: Redis | None = None) -> dict[str, Any]:
    user = actor.user
    cfg = await _cfg(session)
    today = _today(cfg)
    shift = user.shift
    if shift is None or not shift.is_active:
        raise AppError("NO_SHIFT_ASSIGNED", "No shift is assigned to you. Contact HR.")
    if body.date < today:
        raise _err("INVALID_TIME_RANGE", "Permission cannot be requested for a past date", "date")
    if body.to_time <= body.from_time:
        raise _err("INVALID_TIME_RANGE", "to_time must be after from_time", "to_time")
    f_abs, t_abs = _abs_window(shift, body.from_time, body.to_time)
    s_abs, e_abs = _shift_window(shift)
    if f_abs < s_abs or t_abs > e_abs:
        raise _err(
            "INVALID_TIME_RANGE", f"Times must be within shift hours {_hm(shift.start_time)}-{_hm(shift.end_time)}", "from_time"
        )
    if body.date == today:
        now = utcnow().astimezone(att.office_tz(cfg))
        grace = int(cfg["permission.past_grace_minutes"])
        if f_abs < now.hour * 60 + now.minute - grace:
            raise _err("INVALID_TIME_RANGE", "Permission cannot start in the past", "from_time")
    if not await att.working_day(session, user, body.date):
        raise _err("INVALID_TIME_RANGE", "The selected date is not a working day", "date")
    minutes = t_abs - f_abs
    if minutes <= 0:
        raise _err("INVALID_TIME_RANGE", "Requested duration is too short", "to_time")
    max_min = int(cfg["permission.max_minutes_per_request"])
    if minutes > max_min:
        raise _err("INVALID_TIME_RANGE", f"A single permission can be at most {max_min} minutes", "to_time")

    await _lock_user(session, user.id)
    usage = await permission_usage(session, user.id, body.date)
    count = usage["used_count"] + usage["pending_count"]
    mins = usage["used_minutes"] + usage["pending_minutes"]
    lim_c, lim_m = int(cfg["permission.monthly_count_limit"]), int(cfg["permission.monthly_minutes_limit"])
    if count + 1 > lim_c:
        raise _err("PERMISSION_LIMIT_EXCEEDED", f"Monthly permission count limit ({lim_c}) reached", "count")
    if mins + minutes > lim_m:
        raise _err("PERMISSION_LIMIT_EXCEEDED", f"Monthly permission minutes limit ({lim_m}) exceeded", "minutes")
    if await _leave_blocks_permission(session, user.id, shift, body.date, f_abs, t_abs, ("pending", "approved")):
        raise _err("OVERLAPPING_REQUEST", "You have a pending or approved leave covering this time", "date")
    perm_clash = await session.execute(
        select(PermissionRequest.id)
        .where(
            PermissionRequest.user_id == user.id,
            PermissionRequest.request_date == body.date,
            PermissionRequest.status.in_(("pending", "approved")),
            PermissionRequest.from_time < body.to_time,
            PermissionRequest.to_time > body.from_time,
        )
        .limit(1)
    )
    if perm_clash.first():
        raise _err("OVERLAPPING_REQUEST", "You already have a permission request in this time window", "from_time")
    req = PermissionRequest(
        user_id=user.id,
        request_date=body.date,
        from_time=body.from_time.replace(second=0, microsecond=0),
        to_time=body.to_time.replace(second=0, microsecond=0),
        minutes=minutes,
        reason=body.reason,
        status="pending",
    )
    session.add(req)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _err("OVERLAPPING_REQUEST", "You already have a permission request in this time window", "from_time") from exc
    audit(
        session,
        action="permission.apply",
        actor=actor,
        entity_type="permission_request",
        entity_id=req.id,
        after={"date": body.date.isoformat(), "minutes": minutes},
    )
    approvers, fanout = await _approvers(session, user)
    await session.commit()
    await _push_new_request(session, redis, user, req.id, "permission", approvers, fanout)
    return await _one(session, req)


# ================================================================ my requests
def _range_conditions(model, d_from: dt.date | None, d_to: dt.date | None) -> list:
    if d_from and d_to and d_to < d_from:
        raise _err("VALIDATION_ERROR", "to must be >= from", "to")
    cond: list = []
    lo, hi = (model.to_date, model.from_date) if model in DATE_RANGE_MODELS else (model.request_date, model.request_date)
    if d_from:
        cond.append(lo >= d_from)
    if d_to:
        cond.append(hi <= d_to)
    return cond


def _status_cond(model, status: str | None) -> list:
    if status is None:
        return []
    if status not in ("pending", "approved", "rejected", "cancelled"):
        raise _err("VALIDATION_ERROR", "invalid status", "status")
    return [model.status == status]


def _order(model):
    first = model.from_date if model in DATE_RANGE_MODELS else model.request_date
    return (first.desc(), model.created_at.desc())


async def list_mine(
    session: AsyncSession, actor: Principal, kind: str, params: PageParams, status: str | None, d_from, d_to
) -> dict[str, Any]:
    m = KINDS[kind]
    cond = [m.user_id == actor.id, *_status_cond(m, status), *_range_conditions(m, d_from, d_to)]
    total = (await session.execute(select(func.count()).select_from(m).where(*cond))).scalar_one()
    q = select(m).where(*cond).order_by(*_order(m)).limit(params.page_size).offset(params.offset)
    rows = list((await session.execute(q)).scalars())
    return make_page(await _render(session, rows, admin=False), params, total)


async def get_mine(session: AsyncSession, actor: Principal, kind: str, req_id: uuid.UUID) -> dict[str, Any]:
    r = await session.get(KINDS[kind], req_id)
    if r is None or r.user_id != actor.id:
        raise AppError("NOT_FOUND", "Request not found")
    return await _one(session, r)


# ================================================================ cancel
async def _load_locked(session: AsyncSession, kind: str, req_id: uuid.UUID):
    m = KINDS[kind]
    q = select(m).where(m.id == req_id).with_for_update().execution_options(populate_existing=True)
    return (await session.execute(q)).scalar_one_or_none()


async def cancel(session: AsyncSession, actor: Principal, kind: str, req_id: uuid.UUID) -> dict[str, Any]:
    probe = await session.get(KINDS[kind], req_id)  # authorise BEFORE taking the row lock
    if probe is None or probe.user_id != actor.id:
        raise AppError("NOT_FOUND", "Request not found")
    r = await _load_locked(session, kind, req_id)
    if r is None:
        raise AppError("NOT_FOUND", "Request not found")
    cfg = await _cfg(session)
    today = _today(cfg)
    before = r.status
    if r.status == "pending":
        restore_used = False
    elif r.status == "approved":
        start = r.request_date if kind == "permission" else r.from_date
        if not (cfg["leave.allow_cancel_approved"] and start > today):
            raise AppError("NOT_PENDING", "Only pending requests can be cancelled")
        restore_used = True
    else:
        raise AppError("NOT_PENDING", "Only pending requests can be cancelled")
    if kind == "leave":
        lt = await session.get(LeaveType, r.leave_type_id)
        bal = await get_balance(session, actor.user, lt, r.from_date.year, lock=True)
        if restore_used:
            await _move(session, bal, used=-d1(r.days))
        else:
            await _move(session, bal, pending=-d1(r.days))
    r.status = "cancelled"
    r.cancelled_at = utcnow()
    audit(
        session,
        action=f"{kind}.cancel",
        actor=actor,
        entity_type=f"{kind}_request",
        entity_id=r.id,
        before={"status": before},
        after={"status": "cancelled"},
    )
    await session.commit()
    return await _one(session, r)


async def cancel_pending_for_user(session: AsyncSession, user: User, actor: Principal, *, why: str) -> int:
    """Deactivate / delete: cancel the user's PENDING leave + permission + on-duty requests, restoring `pending` balance.
    Caller commits (same transaction as the status change). Audited as leave / permission / on_duty .auto_cancel."""
    n = 0
    leaves = (
        (
            await session.execute(
                select(LeaveRequest)
                .where(LeaveRequest.user_id == user.id, LeaveRequest.status == "pending")
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for r in leaves:
        lt = await session.get(LeaveType, r.leave_type_id)
        try:
            bal = await get_balance(session, user, lt, r.from_date.year, lock=True)
            await _move(session, bal, pending=-d1(r.days))
        except AppError:  # inconsistent balance must not block a deactivation; flagged in the log
            log.error("auto-cancel: balance for request %s could not be restored", r.id)
        r.status, r.cancelled_at = "cancelled", utcnow()
        audit(
            session,
            action="leave.auto_cancel",
            actor=actor,
            entity_type="leave_request",
            entity_id=r.id,
            before={"status": "pending"},
            after={"status": "cancelled", "reason": why, "owner": user.employee_id},
        )
        n += 1
    perms = (
        (
            await session.execute(
                select(PermissionRequest)
                .where(PermissionRequest.user_id == user.id, PermissionRequest.status == "pending")
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    ods = (
        (
            await session.execute(
                select(OnDutyRequest)
                .where(OnDutyRequest.user_id == user.id, OnDutyRequest.status == "pending")
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for kind, rows in (("permission", perms), ("on_duty", ods)):
        for p in rows:
            p.status, p.cancelled_at = "cancelled", utcnow()
            audit(
                session,
                action=f"{kind}.auto_cancel",
                actor=actor,
                entity_type=f"{kind}_request",
                entity_id=p.id,
                before={"status": "pending"},
                after={"status": "cancelled", "reason": why, "owner": user.employee_id},
            )
            n += 1
    await session.flush()
    return n


# ================================================================ approvals
def _scope_problem(actor: Principal, requester: User) -> str | None:
    """None = allowed, else 'self' or 'scope'."""
    if requester.id == actor.id:
        return "self"
    if actor.has("request.approve_all"):
        return None
    if actor.has("request.approve_team") and requester.manager_id == actor.id:
        return None
    return "scope"


async def _audit_denied(session: AsyncSession, actor: Principal, kind: str, req_id: uuid.UUID, why: str, decision: str) -> None:
    audit(
        session,
        action="request.decide_denied",
        actor=actor,
        entity_type=f"{kind}_request",
        entity_id=req_id,
        after={"why": why, "decision": decision},
        outcome="failure",
    )
    await session.commit()


async def decide(
    session: AsyncSession, actor: Principal, kind: str, req_id: uuid.UUID, *, approve: bool, comment: str | None
) -> dict[str, Any]:
    decision = "approve" if approve else "reject"
    # 1) authorise on an UNLOCKED read: unknown and out-of-scope ids look identical (404); denied attempts are audited
    probe = await session.get(KINDS[kind], req_id)
    if probe is None:
        raise AppError("NOT_FOUND", "Request not found")
    requester = (await _users(session, {probe.user_id}))[probe.user_id]
    problem = _scope_problem(actor, requester)
    if problem == "self":
        await _audit_denied(session, actor, kind, req_id, problem, decision)
        raise AppError("FORBIDDEN", "You cannot approve or reject your own request")
    if problem:
        await _audit_denied(session, actor, kind, req_id, problem, decision)
        raise AppError("NOT_FOUND", "Request not found")
    # 2) lock and decide; any refusal is audited (committed) before it is raised
    who = SimpleNamespace(id=actor.id, employee_id=actor.employee_id)  # a rollback expires the ORM objects below
    try:
        return await _decide_locked(session, actor, kind, req_id, requester, approve=approve, comment=comment)
    except AppError as exc:
        await session.rollback()
        audit(
            session,
            action="request.decide_refused",
            actor=who,
            entity_type=f"{kind}_request",
            entity_id=req_id,
            after={"code": exc.code, "decision": decision},
            outcome="failure",
        )
        await session.commit()
        raise


async def _decide_locked(
    session: AsyncSession, actor: Principal, kind: str, req_id: uuid.UUID, requester: User, *, approve: bool, comment: str | None
) -> dict[str, Any]:
    r = await _load_locked(session, kind, req_id)
    if r is None:
        raise AppError("NOT_FOUND", "Request not found")
    if r.status != "pending":
        raise AppError("NOT_PENDING", "This request is no longer pending")
    if approve and (requester.status != "active" or requester.deleted_at is not None):
        raise AppError("REQUESTER_INACTIVE", "The employee is no longer active; this request cannot be approved")
    if kind == "leave":
        lt = await session.get(LeaveType, r.leave_type_id)  # still approvable if the type was deactivated meanwhile
        bal = await get_balance(session, requester, lt, r.from_date.year, lock=True)
        days = d1(r.days)
        if approve:
            if d1(bal.pending) < days:
                raise _err("INSUFFICIENT_BALANCE", "Pending balance does not cover this request", "days")
            clash = await session.execute(
                select(LeaveRequest.id)
                .where(
                    LeaveRequest.user_id == r.user_id,
                    LeaveRequest.id != r.id,
                    LeaveRequest.status == "approved",
                    LeaveRequest.from_date <= r.to_date,
                    LeaveRequest.to_date >= r.from_date,
                )
                .limit(1)
            )
            if clash.first():
                raise _err("OVERLAPPING_REQUEST", "Overlaps another approved leave", "from_date")
            if await _permission_blocks_leave(session, requester, r):
                raise _err("OVERLAPPING_REQUEST", "The employee has an approved permission in this period", "from_date")
            if await on_duty_overlaps(session, r.user_id, r.from_date, r.to_date, ("approved",)):
                raise _err("OVERLAPPING_REQUEST", "The employee is on approved on duty in this period", "from_date")
            await _move(session, bal, used=days, pending=-days)
        else:
            await _move(session, bal, pending=-days)
    elif kind == "on_duty":
        if approve:
            clash = await session.execute(
                select(LeaveRequest.id)
                .where(
                    LeaveRequest.user_id == r.user_id,
                    LeaveRequest.status == "approved",
                    LeaveRequest.from_date <= r.to_date,
                    LeaveRequest.to_date >= r.from_date,
                )
                .limit(1)
            )
            if clash.first():
                raise _err("OVERLAPPING_REQUEST", "The employee is on approved leave in this period", "from_date")
    elif approve:
        shift = requester.shift
        f_abs, t_abs = _abs_window(shift, r.from_time, r.to_time) if shift is not None else (0, 0)
        if await _leave_blocks_permission(session, r.user_id, shift, r.request_date, f_abs, t_abs, ("approved",)):
            raise _err("OVERLAPPING_REQUEST", "The employee is on approved leave at this time", "date")
    r.status = "approved" if approve else "rejected"
    r.approver_id = actor.id
    r.decision_comment = (comment or "").strip() or None
    r.decided_at = utcnow()
    audit(
        session,
        action="request.approve" if approve else "request.reject",
        actor=actor,
        entity_type=f"{kind}_request",
        entity_id=r.id,
        before={"status": "pending"},
        after={"status": r.status, "owner": requester.employee_id, "has_comment": bool(r.decision_comment)},
    )
    await session.commit()
    await notify(
        session,
        [r.user_id],
        type=f"{kind}_decision",
        entity_id=r.id,
        title=f"{LABELS[kind].capitalize()} request {r.status}",
        body=f"Your {LABELS[kind]} request was {r.status}",
    )
    return await _one(session, r, admin=True)


# ================================================================ quota follow-up on join_date change
async def recompute_prorata(session: AsyncSession, user: User, actor: Principal, old_join: dt.date | None) -> dict[str, int]:
    """join_date changed: re-derive the quota of the balance rows of the OLD and NEW join year when nothing was used or
    reserved; rows with usage are only reported (audit `needs_review`) for HR. Caller commits."""
    years = {d.year for d in (old_join, user.join_date) if d is not None}
    if not years:
        return {"recomputed": 0, "needs_review": 0}
    rows = (
        await session.execute(
            select(LeaveBalance, LeaveType)
            .join(LeaveType, LeaveType.id == LeaveBalance.leave_type_id)
            .where(LeaveBalance.user_id == user.id, LeaveBalance.year.in_(years))
            .with_for_update()
        )
    ).all()
    changed: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for b, lt in rows:
        new_q = initial_quota(lt, user, b.year)
        if new_q == d1(b.quota):
            continue
        item = {"year": b.year, "type": lt.code, "quota": str(d1(b.quota)), "new_quota": str(new_q)}
        if d1(b.used) == 0 and d1(b.pending) == 0:
            b.quota = new_q
            changed.append(item)
        else:
            review.append(item)
    if changed or review:
        audit(
            session,
            action="leave.quota_recompute",
            actor=actor,
            entity_type="user",
            entity_id=user.id,
            before={"join_date": old_join.isoformat() if old_join else None},
            after={
                "join_date": user.join_date.isoformat() if user.join_date else None,
                "recomputed": changed,
                "needs_review": review,
            },
        )
    await session.flush()
    return {"recomputed": len(changed), "needs_review": len(review)}


# ================================================================ retention
async def purge_old_text(session: AsyncSession, batch: int = 500) -> dict[str, int]:
    """Retention (review 03 M1): `leave.reason_retention_days` > 0 nulls reason/decision_comment of DECIDED (approved,
    rejected, cancelled) requests whose last day is older than that, and deletes their attachment files once no request
    references them. 0 (default) keeps everything."""
    cfg = await _cfg(session)
    days = int(cfg["leave.reason_retention_days"])
    out = {"leave": 0, "permission": 0, "on_duty": 0, "files": 0}
    if days <= 0:
        return out
    cutoff = _today(cfg) - dt.timedelta(days=days)
    closed = ("approved", "rejected", "cancelled")
    for kind, m, last_col in (
        ("leave", LeaveRequest, LeaveRequest.to_date),
        ("permission", PermissionRequest, PermissionRequest.request_date),
        ("on_duty", OnDutyRequest, OnDutyRequest.to_date),
    ):
        extra = [m.attachment_file_id.is_not(None)] if kind == "leave" else []
        while True:
            ids = list(
                (
                    await session.execute(
                        select(m.id)
                        .where(
                            m.status.in_(closed),
                            last_col < cutoff,
                            or_(m.reason.is_not(None), m.decision_comment.is_not(None), *extra),
                        )
                        .order_by(m.id)
                        .limit(batch)
                    )
                ).scalars()
            )
            if not ids:
                break
            file_ids: set[uuid.UUID] = set()
            if kind == "leave":
                file_ids = {
                    f
                    for f in (
                        await session.execute(select(LeaveRequest.attachment_file_id).where(LeaveRequest.id.in_(ids)))
                    ).scalars()
                    if f
                }
                await session.execute(
                    update(LeaveRequest)
                    .where(LeaveRequest.id.in_(ids))
                    .values(reason=None, decision_comment=None, attachment_file_id=None)
                )
            else:
                await session.execute(update(m).where(m.id.in_(ids)).values(reason=None, decision_comment=None))
            for fid in file_ids:
                still = (
                    await session.execute(select(LeaveRequest.id).where(LeaveRequest.attachment_file_id == fid).limit(1))
                ).first()
                f = await session.get(File, fid)
                if still is None and f is not None:
                    await files_svc.delete_blob(f)
                    await session.delete(f)
                    out["files"] += 1
            out[kind] += len(ids)
            await session.commit()
    if out["leave"] or out["permission"] or out["on_duty"]:
        audit(session, action="leave.retention_purge", entity_type="leave_request", after={**out, "retention_days": days})
        await session.commit()
    return out


async def admin_list(
    session: AsyncSession,
    actor: Principal,
    params: PageParams,
    *,
    kind: str,
    status: str | None,
    department_id: uuid.UUID | None,
    employee_id: str | None,
    d_from: dt.date | None,
    d_to: dt.date | None,
) -> dict[str, Any]:
    m = KINDS[kind]
    scope = att.scope_conditions(actor, "request.view_all", "request.view_team", department_id, employee_id)
    cond = [*scope, *_status_cond(m, status), *_range_conditions(m, d_from, d_to)]
    base = select(m).join(User, User.id == m.user_id).where(*cond)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = list(
        (await session.execute(base.order_by(*_order(m)).limit(params.page_size).offset(params.offset))).scalars().unique()
    )
    return make_page(await _render(session, rows, admin=True), params, total)
