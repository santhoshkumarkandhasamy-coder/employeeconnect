"""Announcements (plan 06, contract section 7).

Visibility for an employee: not deleted, published, not expired, and (target_all or their CURRENT department is targeted).
Anything else is 404 (never 403). Admin side is gated by `announcement.manage` in the router.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import ColumnElement, and_, delete, exists, false, func, or_, select, true
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core import rate_limit
from ..core.audit import audit
from ..core.clock import as_utc, iso_z, utcnow
from ..core.config import get_settings
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import Announcement, AnnouncementDepartment, AnnouncementRead, Department, File, User
from . import attendance as att
from . import files as files_svc
from .notifications import notify

log = logging.getLogger("app.announcements")
PUSH_CHUNK = 1000


# ---------------------------------------------------------------- visibility
def visible_to(user: User) -> list[ColumnElement[bool]]:
    now = utcnow()
    targeted: ColumnElement[bool] = Announcement.target_all.is_(True)
    if user.department_id is not None:
        targeted = or_(
            targeted,
            exists().where(
                AnnouncementDepartment.announcement_id == Announcement.id,
                AnnouncementDepartment.department_id == user.department_id,
            ),
        )
    return [
        Announcement.deleted_at.is_(None),
        Announcement.published_at <= now,
        or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
        targeted,
    ]


def _audience(a: Announcement) -> ColumnElement[bool]:
    """Active users an announcement is aimed at (SQL expression over `User`; `a` = an Announcement row)."""
    return and_(
        User.status == "active",
        User.deleted_at.is_(None),
        or_(
            true() if a.target_all else false(),
            exists().where(
                AnnouncementDepartment.announcement_id == a.id, AnnouncementDepartment.department_id == User.department_id
            ),
        ),
    )


async def _visible_or_404(session: AsyncSession, user: User, ann_id: uuid.UUID) -> Announcement:
    a = (await session.execute(select(Announcement).where(Announcement.id == ann_id, *visible_to(user)))).scalar_one_or_none()
    if a is None:
        raise AppError("NOT_FOUND", "Announcement not found")
    return a


# ---------------------------------------------------------------- output
async def _render(
    session: AsyncSession, rows: list[Announcement], read_ids: set[uuid.UUID] | None, *, admin: bool = False
) -> list[dict[str, Any]]:
    """Employee shape has no targeting fields (review 07 L1); the admin view adds them (the caller adds the stats)."""
    if not rows:
        return []
    ids = [a.id for a in rows]
    depts: dict[uuid.UUID, list[str]] = {}
    if admin:
        dept_rows = (
            await session.execute(select(AnnouncementDepartment).where(AnnouncementDepartment.announcement_id.in_(ids)))
        ).scalars()
        for d in dept_rows:
            depts.setdefault(d.announcement_id, []).append(str(d.department_id))
    user_ids = {a.created_by for a in rows} | {a.updated_by for a in rows if a.updated_by is not None}
    people = {u.id: u for u in (await session.execute(select(User).where(User.id.in_(user_ids)))).unique().scalars()}
    photos = await files_svc.photo_file_ids(session, list(people))

    def summary(uid: uuid.UUID | None) -> dict[str, Any] | None:
        u = people.get(uid) if uid else None
        return att.user_summary(u, photos.get(u.id)) if u else None

    out = []
    for a in rows:
        item: dict[str, Any] = {
            "id": str(a.id),
            "title": a.title,
            "body": a.body,
            "priority": a.priority,
            "image_url": files_svc.file_url(a.image_file_id),
            "pdf_url": files_svc.file_url(a.pdf_file_id),
            "published_at": iso_z(a.published_at),
            "expires_at": iso_z(a.expires_at) if a.expires_at else None,
            "is_read": (a.id in read_ids) if read_ids is not None else False,
            "created_by": summary(a.created_by),
            "updated_by": summary(a.updated_by),  # "edited by X"; null = never edited
            "updated_at": iso_z(a.updated_at) if a.updated_by is not None else None,
        }
        if admin:
            item["target_all"] = a.target_all
            item["department_ids"] = sorted(depts.get(a.id, []))
            item["version"] = a.version
        out.append(item)
    return out


async def _read_ids(session: AsyncSession, user_id: uuid.UUID, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not ids:
        return set()
    q = select(AnnouncementRead.announcement_id).where(
        AnnouncementRead.user_id == user_id, AnnouncementRead.announcement_id.in_(ids)
    )
    return set((await session.execute(q)).scalars())


# ---------------------------------------------------------------- employee side
async def list_mine(session: AsyncSession, actor: Principal, params: PageParams, unread: bool | None) -> dict[str, Any]:
    conds = visible_to(actor.user)
    read_exists = exists().where(AnnouncementRead.announcement_id == Announcement.id, AnnouncementRead.user_id == actor.id)
    if unread is True:
        conds.append(~read_exists)
    elif unread is False:
        conds.append(read_exists)
    total = (await session.execute(select(func.count()).select_from(Announcement).where(*conds))).scalar_one()
    rows = list(
        (
            await session.execute(
                select(Announcement)
                .where(*conds)
                .order_by(Announcement.published_at.desc(), Announcement.id.desc())
                .limit(params.page_size)
                .offset(params.offset)
            )
        ).scalars()
    )
    reads = await _read_ids(session, actor.id, [a.id for a in rows])
    return make_page(await _render(session, rows, reads), params, total)


async def unread_count(session: AsyncSession, user: User) -> int:
    read_exists = exists().where(AnnouncementRead.announcement_id == Announcement.id, AnnouncementRead.user_id == user.id)
    q = select(func.count()).select_from(Announcement).where(*visible_to(user), ~read_exists)
    return int((await session.execute(q)).scalar_one())


async def get_mine(session: AsyncSession, actor: Principal, ann_id: uuid.UUID) -> dict[str, Any]:
    a = await _visible_or_404(session, actor.user, ann_id)
    return (await _render(session, [a], await _read_ids(session, actor.id, [a.id])))[0]


async def mark_read(session: AsyncSession, actor: Principal, ann_id: uuid.UUID) -> None:
    a = await _visible_or_404(session, actor.user, ann_id)
    if await _read_ids(session, actor.id, [a.id]):
        return
    try:
        async with session.begin_nested():
            session.add(AnnouncementRead(announcement_id=a.id, user_id=actor.id))
    except IntegrityError:  # a concurrent duplicate read: already recorded, which is the idempotent outcome
        return
    await session.commit()


# ---------------------------------------------------------------- admin side
def _bad(field: str, message: str) -> AppError:
    return AppError("VALIDATION_ERROR", message, details=[{"field": field, "message": message}])


async def _check_refs(session: AsyncSession, body: Any) -> None:
    for field, fid, purpose in (
        ("image_file_id", body.image_file_id, "announcement_image"),
        ("pdf_file_id", body.pdf_file_id, "announcement_pdf"),
    ):
        if fid is None:
            continue
        f = await session.get(File, fid)
        if f is None or f.purpose != purpose:
            raise _bad(field, f"upload a file with purpose {purpose} first")
    if body.department_ids:
        found = set((await session.execute(select(Department.id).where(Department.id.in_(body.department_ids)))).scalars())
        if any(d not in found for d in body.department_ids):
            raise _bad("department_ids", "unknown department")
    if body.expires_at is not None and body.expires_at <= utcnow():
        raise _bad("expires_at", "must be in the future")


def _in_audience() -> ColumnElement[bool]:
    """Join condition: the active `User` rows an `Announcement` row targets (both tables must be in the FROM clause)."""
    return and_(
        User.status == "active",
        User.deleted_at.is_(None),
        or_(
            Announcement.target_all.is_(True),
            exists().where(
                AnnouncementDepartment.announcement_id == Announcement.id,
                AnnouncementDepartment.department_id == User.department_id,
            ),
        ),
    )


async def _stats(session: AsyncSession, rows: list[Announcement]) -> dict[uuid.UUID, tuple[int, int]]:
    """announcement id -> (read_count, target_count); both only count active users the announcement currently targets.
    Two grouped queries for the whole page (review 07 L3: no per-row queries)."""
    ids = [a.id for a in rows]
    if not ids:
        return {}
    targets = dict(
        (
            await session.execute(
                select(Announcement.id, func.count(User.id))
                .select_from(Announcement)
                .join(User, _in_audience())
                .where(Announcement.id.in_(ids))
                .group_by(Announcement.id)
            )
        ).all()
    )
    reads_ = dict(
        (
            await session.execute(
                select(AnnouncementRead.announcement_id, func.count())
                .select_from(AnnouncementRead)
                .join(Announcement, Announcement.id == AnnouncementRead.announcement_id)
                .join(User, and_(User.id == AnnouncementRead.user_id, _in_audience()))
                .where(AnnouncementRead.announcement_id.in_(ids))
                .group_by(AnnouncementRead.announcement_id)
            )
        ).all()
    )
    return {i: (int(reads_.get(i, 0)), int(targets.get(i, 0))) for i in ids}


async def _admin_view(session: AsyncSession, rows: list[Announcement]) -> list[dict[str, Any]]:
    items = await _render(session, rows, None, admin=True)
    stats = await _stats(session, rows)
    now = utcnow()
    for item, a in zip(items, rows, strict=True):
        item.pop("is_read")
        item["read_count"], item["target_count"] = stats[a.id]
        item["status"] = "expired" if a.expires_at and as_utc(a.expires_at) <= now else "published"
    return items


async def _get_admin(session: AsyncSession, ann_id: uuid.UUID, *, lock: bool = False) -> Announcement:
    q = select(Announcement).where(Announcement.id == ann_id, Announcement.deleted_at.is_(None))
    if lock:
        q = q.with_for_update()
    a = (await session.execute(q)).scalar_one_or_none()
    if a is None:
        raise AppError("NOT_FOUND", "Announcement not found")
    return a


async def admin_list(session: AsyncSession, params: PageParams) -> dict[str, Any]:
    cond = Announcement.deleted_at.is_(None)  # expired ones stay listed
    total = (await session.execute(select(func.count()).select_from(Announcement).where(cond))).scalar_one()
    rows = list(
        (
            await session.execute(
                select(Announcement)
                .where(cond)
                .order_by(Announcement.published_at.desc(), Announcement.id.desc())
                .limit(params.page_size)
                .offset(params.offset)
            )
        ).scalars()
    )
    return make_page(await _admin_view(session, rows), params, total)


def _snapshot(a: Announcement, dept_ids: list[uuid.UUID]) -> dict[str, Any]:
    """Audit snapshot. The body text is not copied into the audit log; its SHA-256 and length show THAT and roughly HOW it
    changed (review 07 M4)."""
    return {
        "title": a.title,
        "priority": a.priority,
        "target_all": a.target_all,
        "department_ids": sorted(str(d) for d in dept_ids),
        "has_image": a.image_file_id is not None,
        "has_pdf": a.pdf_file_id is not None,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "body_sha256": hashlib.sha256(a.body.encode("utf-8")).hexdigest(),
        "body_len": len(a.body),
    }


async def _dept_ids(session: AsyncSession, a: Announcement) -> list[uuid.UUID]:
    q = select(AnnouncementDepartment.department_id).where(AnnouncementDepartment.announcement_id == a.id)
    return list((await session.execute(q)).scalars())


async def _check_author_containment(session: AsyncSession, actor: Principal, a: Announcement) -> None:
    """Review 07 M4 (containment as in review 06): an announcement written by someone whose role holds permissions the
    actor lacks (e.g. HR editing an Admin's announcement) can be neither edited nor deleted by the actor."""
    author = await session.get(User, a.created_by)
    if author is None:
        return
    if not frozenset(p.code for p in author.role.permissions) <= actor.permissions:
        raise AppError("FORBIDDEN", "You cannot change an announcement written by an account with more privileges than your own")


def _unavailable() -> AppError:
    return AppError(
        "TEMPORARILY_UNAVAILABLE", "Service temporarily unavailable. Try again shortly.", headers={"Retry-After": "30"}
    )


def _cooldown_error(ttl: int) -> AppError:
    return AppError(
        "RATE_LIMITED", "This announcement was re-sent recently. Try again later.", headers={"Retry-After": str(max(1, ttl))}
    )


async def _push_gate(redis: Redis, actor: Principal, ann_id: uuid.UUID | None) -> str | None:
    """Review 07 M1. A per-announcement re-send cooldown (when `ann_id` is given) and a per-admin daily budget of
    push-generating writes. Both fail closed when Redis is down. Returns the cooldown key to arm after the write."""
    key = None
    if ann_id is not None:
        key = f"ann:renotify:{ann_id}"
        try:
            ttl = await redis.ttl(key)
        except Exception:  # noqa: BLE001
            log.error("renotify cooldown unavailable; failing closed")
            raise _unavailable() from None
        if ttl and ttl > 0:
            raise _cooldown_error(ttl)
    budget = get_settings().announcement_push_budget_per_day
    await rate_limit.enforce(redis, [(f"rl:ann:push:{actor.id}", budget, 86400)], fail_closed=True)
    return key


async def _arm_cooldown(redis: Redis, key: str) -> bool:
    """True when this request owns the cooldown. False = a concurrent re-send won the race: its push stands, ours is skipped."""
    ex = max(1, get_settings().announcement_renotify_cooldown_min) * 60
    try:
        return bool(await redis.set(key, "1", nx=True, ex=ex))
    except Exception:  # noqa: BLE001
        log.error("renotify cooldown could not be armed")
        return True  # the edit is saved; the daily budget still bounds the damage


async def create(session: AsyncSession, redis: Redis, actor: Principal, body: Any) -> tuple[dict[str, Any], uuid.UUID, bool]:
    """Returns (admin view, id, send the push with high priority)."""
    await _check_refs(session, body)
    await _push_gate(redis, actor, None)
    high = body.priority == "urgent"
    a = Announcement(
        title=body.title,
        body=body.body,
        priority=body.priority,
        image_file_id=body.image_file_id,
        pdf_file_id=body.pdf_file_id,
        target_all=body.target_all,
        published_at=utcnow(),
        expires_at=body.expires_at,
        created_by=actor.id,
        high_priority_pushed=high,
    )
    session.add(a)
    await session.flush()
    session.add_all([AnnouncementDepartment(announcement_id=a.id, department_id=d) for d in body.department_ids])
    audit(
        session, action="announcement.create", actor=actor, entity_type="announcement", entity_id=a.id,
        after=_snapshot(a, body.department_ids),
    )  # fmt: skip
    await session.commit()
    return (await _admin_view(session, [a]))[0], a.id, high


async def update(
    session: AsyncSession, redis: Redis, actor: Principal, ann_id: uuid.UUID, body: Any, if_match: int | None = None
) -> tuple[dict[str, Any], bool | None]:
    """Returns (admin view, push): None = no push, else the high-priority flag for the re-send."""
    a = await _get_admin(session, ann_id, lock=True)
    await _check_author_containment(session, actor, a)
    if if_match is not None and if_match != a.version:
        raise AppError(
            "CONFLICT",
            "The announcement was changed by someone else. Reload and try again.",
            details=[{"field": "version", "message": f"current version is {a.version}"}],
        )
    if a.expires_at is not None and as_utc(a.expires_at) <= utcnow() and body.expires_at is None:
        raise _bad("expires_at", "this announcement has expired; set a new future expires_at to republish it")
    await _check_refs(session, body)
    if body.expires_at is not None and body.expires_at <= as_utc(a.published_at):
        raise _bad("expires_at", "must be after published_at")
    cooldown_key = await _push_gate(redis, actor, a.id) if body.renotify else None
    before = _snapshot(a, await _dept_ids(session, a))
    a.title, a.body, a.priority = body.title, body.body, body.priority
    a.image_file_id, a.pdf_file_id = body.image_file_id, body.pdf_file_id
    a.target_all, a.expires_at = body.target_all, body.expires_at
    a.updated_by, a.version = actor.id, a.version + 1
    push: bool | None = None
    if body.renotify:  # high priority at most once per announcement; later re-sends go out as normal pushes
        push = a.priority == "urgent" and not a.high_priority_pushed
        if push:
            a.high_priority_pushed = True
    await session.execute(delete(AnnouncementDepartment).where(AnnouncementDepartment.announcement_id == a.id))
    session.add_all([AnnouncementDepartment(announcement_id=a.id, department_id=d) for d in body.department_ids])
    audit(
        session, action="announcement.update", actor=actor, entity_type="announcement", entity_id=a.id,
        before=before, after={**_snapshot(a, body.department_ids), "renotify": bool(body.renotify)},
    )  # fmt: skip
    await session.commit()  # existing reads are kept when targets change
    if cooldown_key and not await _arm_cooldown(redis, cooldown_key):
        push = None
    await session.refresh(a)
    return (await _admin_view(session, [a]))[0], push


async def delete_(session: AsyncSession, actor: Principal, ann_id: uuid.UUID) -> None:
    a = await _get_admin(session, ann_id)
    await _check_author_containment(session, actor, a)
    before = _snapshot(a, await _dept_ids(session, a))
    a.deleted_at = utcnow()
    audit(session, action="announcement.delete", actor=actor, entity_type="announcement", entity_id=a.id, before=before)
    await session.commit()


async def purge_deleted_files(session: AsyncSession, days: int | None = None) -> int:
    """Review 07 L9 (worker job): `days` after deletion an announcement loses its image/PDF links, so the orphan sweep
    (files.cleanup_orphans) removes the blobs. 0 = off. Returns the number of announcements scrubbed."""
    days = get_settings().announcement_deleted_retention_days if days is None else days
    if days <= 0:
        return 0
    cutoff = utcnow() - dt.timedelta(days=days)
    res = await session.execute(
        sa_update(Announcement)
        .where(
            Announcement.deleted_at.is_not(None),
            Announcement.deleted_at < cutoff,
            or_(Announcement.image_file_id.is_not(None), Announcement.pdf_file_id.is_not(None)),
        )
        .values(image_file_id=None, pdf_file_id=None)
    )
    n = res.rowcount or 0
    if n:
        audit(
            session, action="announcement.retention_purge", entity_type="announcement",
            after={"count": n, "retention_days": days},
        )  # fmt: skip
    await session.commit()
    return n


async def reads(session: AsyncSession, ann_id: uuid.UUID, params: PageParams) -> dict[str, Any]:
    a = await _get_admin(session, ann_id)
    base = (
        select(AnnouncementRead.read_at, User)
        .join(User, User.id == AnnouncementRead.user_id)
        .where(AnnouncementRead.announcement_id == a.id, _audience(a))
    )
    q = base.order_by(AnnouncementRead.read_at.desc(), User.employee_id).limit(params.page_size).offset(params.offset)
    rows = (await session.execute(q)).unique().all()
    read_count, target_count = (await _stats(session, [a]))[a.id]
    photos = await files_svc.photo_file_ids(session, [u.id for _, u in rows])
    items = [{"user": att.user_summary(u, photos.get(u.id)), "read_at": iso_z(at)} for at, u in rows]
    return {**make_page(items, params, read_count), "read_count": read_count, "target_count": target_count}


# ---------------------------------------------------------------- push (background task; never blocks or fails the request)
async def push(sessionmaker: async_sessionmaker[AsyncSession], ann_id: uuid.UUID, high_priority: bool = False) -> None:
    try:
        async with sessionmaker() as s:
            a = await s.get(Announcement, ann_id)
            if a is None or a.deleted_at is not None:  # deleted while the push was in flight
                return
            ids = list((await s.execute(select(User.id).where(_audience(a)))).scalars())
            body = a.body if len(a.body) <= 100 else a.body[:99] + "..."
            for i in range(0, len(ids), PUSH_CHUNK):
                await notify(
                    s,
                    ids[i : i + PUSH_CHUNK],
                    type="announcement",
                    entity_id=a.id,
                    title=a.title,
                    body=body,
                    high_priority=high_priority,
                )
    except Exception:  # noqa: BLE001
        log.exception("announcement push failed (ignored)")
