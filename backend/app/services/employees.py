"""Admin-lite employee management (plan 01): create, list, get, update, unlock, reset password,
deactivate/activate. Extended by plan 09."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core import rate_limit, security
from ..core.audit import audit
from ..core.clock import as_utc, iso_z, utcnow
from ..core.config import get_settings
from ..core.errors import AppError
from ..core.pagination import PageParams, make_page
from ..core.rbac import Principal
from ..models import Department, DeviceToken, EmployeeProfile, PasswordResetRequest, Role, Shift, User
from ..schemas.employees import EmployeeCreate, EmployeeUpdate
from ..schemas.profile import PERSONAL_KEYS
from . import admin_security, celebrations, notifications
from . import license as license_svc
from . import profile as profile_svc
from .auth import bump_token_epoch, canonical_employee_id, revoke_all_for_user


def employee_out(u: User, *, account_info: bool = True) -> dict[str, Any]:
    """`account_info=False` is the reduced shape for team-scoped viewers (managers): no last_login_at / is_locked /
    locked_until (review L5)."""
    locked_until = as_utc(u.locked_until)
    is_locked = bool(locked_until and locked_until > utcnow())
    mgr = u.manager
    out = {
        "id": str(u.id),
        "employee_id": u.employee_id,
        "full_name": u.full_name,
        "official_email": u.official_email,
        "department": {"id": str(u.department.id), "name": u.department.name} if u.department else None,
        "shift": {"id": str(u.shift.id), "name": u.shift.name} if u.shift else None,
        "role": {"id": str(u.role.id), "name": u.role.name},
        "manager": {"id": str(mgr.id), "employee_id": mgr.employee_id, "full_name": mgr.full_name} if mgr else None,
        "status": u.status,
        "join_date": u.join_date.isoformat() if u.join_date else None,
        "deactivated_at": iso_z(u.deactivated_at) if u.deactivated_at else None,
        "deactivation_reason": u.deactivation_reason,
    }
    if account_info:
        out["is_locked"] = is_locked
        out["locked_until"] = iso_z(locked_until) if is_locked else None
        out["last_login_at"] = iso_z(u.last_login_at) if u.last_login_at else None
    return out


def _temp_expiry() -> dt.datetime:
    return utcnow() + dt.timedelta(hours=get_settings().temp_password_hours)


def _snapshot(u: User) -> dict[str, Any]:
    return {
        "employee_id": u.employee_id,
        "full_name": u.full_name,
        "official_email": u.official_email,
        "role_id": str(u.role_id),
        "department_id": str(u.department_id) if u.department_id else None,
        "shift_id": str(u.shift_id) if u.shift_id else None,
        "manager_id": str(u.manager_id) if u.manager_id else None,
        "status": u.status,
    }


async def _get(session: AsyncSession, user_id: uuid.UUID, *, lock: bool = False) -> User:
    q = select(User).options(selectinload(User.manager)).where(User.id == user_id, User.deleted_at.is_(None))
    user = (await session.execute(q)).unique().scalar_one_or_none()
    if user is None:
        raise AppError("NOT_FOUND", "Employee not found")
    if lock:
        if user.role.name == "admin":
            await _lock_admins(session)  # fixed order (by id) -> no deadlock between two concurrent admin changes
        user = (
            (await session.execute(q.with_for_update(of=User).execution_options(populate_existing=True)))
            .unique()
            .scalar_one_or_none()
        )
        if user is None:
            raise AppError("NOT_FOUND", "Employee not found")
    return user


def _sees_all(actor: Principal) -> bool:
    return actor.has("employee.update") or actor.has("employee.create")


def in_scope(actor: Principal, u: User) -> bool:
    """Custom roles may hold only `employee.view`: such an actor works on direct reports only (review M2)."""
    return _sees_all(actor) or u.manager_id == actor.id


def ensure_in_scope(actor: Principal, u: User) -> None:
    if not in_scope(actor, u):
        raise AppError("NOT_FOUND", "Employee not found")


def _role_permissions(role: Role) -> frozenset[str]:
    return frozenset(p.code for p in role.permissions)


def _check_may_manage_role(actor: Principal, role: Role, *, what: str) -> None:
    """Permission-based (not role-name based) privilege rule for creating / promoting / editing / resetting accounts:
    1. containment: the actor may only deal with roles whose permissions are a subset of their own, so nobody can
       mint or take over an account more powerful than themselves (this is what protects `admin`);
    2. any role that carries permissions at all (i.e. anything above plain self-service) additionally requires the
       Admin-only `role.assign` permission, so HR cannot create/promote/reset HR, Manager or other elevated accounts."""
    perms = _role_permissions(role)
    if not perms <= actor.permissions:
        raise AppError("FORBIDDEN", f"You cannot {what} an account with more privileges than your own")
    if perms and not actor.has("role.assign"):
        raise AppError("FORBIDDEN", f"Only an administrator can {what} accounts with elevated privileges")


def guard_target(actor: Principal, target: User, *, elevated_needs_assign: bool = False) -> None:
    _guard_target(actor, target, elevated_needs_assign=elevated_needs_assign)


def _guard_target(actor: Principal, target: User, *, elevated_needs_assign: bool = False) -> None:
    if elevated_needs_assign:
        _check_may_manage_role(actor, target.role, what="manage")
    elif not _role_permissions(target.role) <= actor.permissions:
        raise AppError("FORBIDDEN", "You cannot manage an account with more privileges than your own")


async def _lock_admins(session: AsyncSession) -> None:
    """Row-lock every active admin (ordered) so concurrent demote/deactivate/delete of two different admins serialise."""
    await session.execute(
        select(User.id)
        .join(Role, Role.id == User.role_id)
        .where(Role.name == "admin", User.status == "active", User.deleted_at.is_(None))
        .order_by(User.id)
        .with_for_update(of=User)
    )


async def _active_admin_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(User)
            .join(Role, Role.id == User.role_id)
            .where(Role.name == "admin", User.status == "active", User.deleted_at.is_(None))
        )
    ).scalar_one()


async def _guard_last_admin(session: AsyncSession, target: User) -> None:
    if target.role.name == "admin" and target.status == "active":
        await _lock_admins(session)
        if await _active_admin_count(session) <= 1:
            raise AppError("LAST_ADMIN", "Cannot remove the last active administrator", status=409)


def _conflict(code: str, message: str) -> AppError:
    return AppError(code, message, status=409)


async def _refs(session: AsyncSession, role_id, department_id, shift_id, manager_id) -> Role:
    role = await session.get(Role, role_id)
    if role is None:
        raise AppError("VALIDATION_ERROR", "Unknown role", details=[{"field": "role_id", "message": "does not exist"}])
    if department_id and await session.get(Department, department_id) is None:
        raise AppError(
            "VALIDATION_ERROR", "Unknown department", details=[{"field": "department_id", "message": "does not exist"}]
        )
    if shift_id and await session.get(Shift, shift_id) is None:
        raise AppError("VALIDATION_ERROR", "Unknown shift", details=[{"field": "shift_id", "message": "does not exist"}])
    return role


async def _check_manager(session: AsyncSession, manager_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    """The manager must exist, be active and not deleted, must not be the user, and must not report (directly or
    indirectly) to the user (no cycles)."""

    def bad(msg: str) -> AppError:
        return AppError("VALIDATION_ERROR", msg, details=[{"field": "manager_id", "message": msg}])

    mgr = await session.get(User, manager_id)
    if mgr is None or mgr.deleted_at is not None:
        raise bad("does not exist")
    if mgr.status != "active":
        raise bad("manager must be an active employee")
    if user_id is None:
        return
    if manager_id == user_id:
        raise bad("A user cannot be their own manager")
    cur, seen = mgr.manager_id, {mgr.id}
    while cur is not None and cur not in seen and len(seen) < 100:
        if cur == user_id:
            raise bad("would create a reporting cycle")
        seen.add(cur)
        nxt = await session.get(User, cur)
        cur = nxt.manager_id if nxt else None


# ------------------------------------------------------------------ queries
async def list_employees(
    session: AsyncSession,
    actor: Principal,
    params: PageParams,
    *,
    q: str | None,
    department_id: uuid.UUID | None,
    role: str | None,
    status: str | None,
) -> dict[str, Any]:
    cond = [User.deleted_at.is_(None)]
    if not _sees_all(actor):
        cond.append(User.manager_id == actor.id)  # manager: direct reports only
    if q:
        esc = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        cond.append(or_(User.employee_id.ilike(like, escape="\\"), User.full_name.ilike(like, escape="\\")))
    if department_id:
        cond.append(User.department_id == department_id)
    if status:
        cond.append(User.status == status)
    if role:
        cond.append(User.role_id.in_(select(Role.id).where(Role.name == role)))
    total = (await session.execute(select(func.count()).select_from(User).where(*cond))).scalar_one()
    rows = (
        (
            await session.execute(
                select(User)
                .options(selectinload(User.manager))
                .where(*cond)
                .order_by(User.employee_id)
                .limit(params.page_size)
                .offset(params.offset)
            )
        )
        .unique()
        .scalars()
        .all()
    )
    info = _sees_all(actor)
    return make_page([employee_out(u, account_info=info) for u in rows], params, total)


def _profile_visible(actor: Principal, u: User) -> bool:
    """Personal data needs `employee.view_sensitive` AND containment: the target's role must be within the viewer's own
    permissions (HR does not see Admin / more privileged accounts' personal data)."""
    return actor.has("employee.view_sensitive") and _role_permissions(u.role) <= actor.permissions


async def _profile_out(session: AsyncSession, actor: Principal, u: User) -> dict[str, Any] | None:
    if not _profile_visible(actor, u):
        return None
    return profile_svc.sensitive_out(await profile_svc.load_profile(session, u))


async def get_employee(session: AsyncSession, redis: Redis, actor: Principal, user_id: uuid.UUID) -> dict[str, Any]:
    u = await _get(session, user_id)
    ensure_in_scope(actor, u)
    out = employee_out(u, account_info=_sees_all(actor))
    if _profile_visible(actor, u):
        await rate_limit.enforce(
            redis,
            [(f"rl:admin:detail:min:{actor.id}", 60, 60), (f"rl:admin:detail:day:{actor.id}", 500, 86400)],
            fail_closed=True,
        )
        out["profile"] = await _profile_out(session, actor, u)
        audit(session, action="employee.view_sensitive", actor=actor, entity_type="user", entity_id=u.id)
        await session.commit()
        await admin_security.note_sensitive_view(session, redis, actor)
    else:
        out["profile"] = None
    return out


# ------------------------------------------------------------------ mutations
async def create_employee(session: AsyncSession, redis: Redis, actor: Principal, body: EmployeeCreate) -> dict[str, Any]:
    eid = canonical_employee_id(body.employee_id)
    role = await _refs(session, body.role_id, body.department_id, body.shift_id, body.manager_id)
    _check_may_manage_role(actor, role, what="create")
    admin_capable = admin_security.is_admin_capable(_role_permissions(role))
    if admin_capable:
        await admin_security.require_reauth(session, redis, actor, body.current_password, action="employee.create")
    if body.manager_id:
        await _check_manager(session, body.manager_id, None)
    await license_svc.lock_for_seats(session)  # license/company row BEFORE any users row (deadlock rule)
    if (await session.execute(select(User.id).where(User.employee_id == eid))).first():
        raise _conflict("EMPLOYEE_ID_EXISTS", "Employee ID already exists")
    if (
        body.official_email
        and (await session.execute(select(User.id).where(User.official_email == str(body.official_email)))).first()
    ):
        raise _conflict("EMAIL_EXISTS", "Email already exists")

    await license_svc.check_seat(session, actor)  # 409 USER_LIMIT_REACHED / 403 LICENSE_EXPIRED (audited)
    temp = security.generate_temp_password(eid, body.full_name)  # always generated, never chosen by the admin

    user = User(
        id=uuid.uuid4(),
        employee_id=eid,
        password_hash=await security.ahash_password(temp),
        full_name=body.full_name.strip(),
        official_email=str(body.official_email) if body.official_email else None,
        role_id=body.role_id,
        department_id=body.department_id,
        shift_id=body.shift_id,
        manager_id=body.manager_id,
        join_date=body.join_date,
        status="active",
        must_change_password=True,
        temp_password_expires_at=_temp_expiry(),
        failed_login_count=0,
        created_by=actor.id,
    )
    session.add(user)
    try:
        await session.flush()
        session.add(EmployeeProfile(user_id=user.id, phone=body.phone))
        from .leave import ensure_balances  # lazy: avoids an import cycle

        await ensure_balances(session, user, (user.join_date or dt.date.today()).year)
        audit(session, action="employee.create", actor=actor, entity_type="user", entity_id=user.id, after=_snapshot(user))
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _conflict("EMPLOYEE_ID_EXISTS", "Employee ID or email already exists") from exc
    created = await _get(session, user.id)
    if admin_capable:
        await admin_security.notify_admins(
            session,
            entity_id=created.id,
            title="Administrator account created",
            body=f"{actor.user.full_name} ({actor.employee_id}) created {created.full_name} ({created.employee_id}) "
            f"with the role {role.name}.",
        )
    return {"employee": employee_out(created), "temporary_password": temp}


async def update_employee(
    session: AsyncSession, redis: Redis | None, actor: Principal, user_id: uuid.UUID, body: EmployeeUpdate
) -> dict[str, Any]:
    u = await _get(session, user_id, lock=True)
    ensure_in_scope(actor, u)
    _guard_target(actor, u, elevated_needs_assign=True)
    sent_personal = [k for k in PERSONAL_KEYS if k in body.model_fields_set]
    if sent_personal and not actor.has("employee.view_sensitive"):  # review M4
        raise AppError("FORBIDDEN", "You are not allowed to change personal details")
    new_role = await _refs(session, body.role_id, body.department_id, body.shift_id, body.manager_id)
    if body.manager_id and body.manager_id != u.manager_id:  # an unchanged (possibly since-deactivated) manager is kept
        await _check_manager(session, body.manager_id, u.id)
    elif body.manager_id == u.id:
        raise AppError(
            "VALIDATION_ERROR", "A user cannot be their own manager", details=[{"field": "manager_id", "message": "invalid"}]
        )
    role_changed = new_role.id != u.role_id
    if role_changed:
        if u.id == actor.id:
            raise AppError("FORBIDDEN", "You cannot change your own role")
        _check_may_manage_role(actor, new_role, what="assign the role of")
        if u.role.name == "admin":
            await _guard_last_admin(session, u)
    admin_capable = admin_security.is_admin_capable(_role_permissions(new_role))
    if admin_capable:  # step-up: the RESULTING role is admin-capable (promotion, or edit of such an account)
        await admin_security.require_reauth(session, redis, actor, body.current_password, action="employee.update")
    if body.official_email and str(body.official_email).lower() != (u.official_email or "").lower():
        if (
            await session.execute(select(User.id).where(User.official_email == str(body.official_email), User.id != u.id))
        ).first():
            raise _conflict("EMAIL_EXISTS", "Email already exists")
    before = _snapshot(u)
    old_join = u.join_date
    u.full_name = body.full_name.strip()
    u.official_email = str(body.official_email) if body.official_email else None
    u.department_id, u.shift_id, u.role_id, u.manager_id, u.join_date = (
        body.department_id,
        body.shift_id,
        body.role_id,
        body.manager_id,
        body.join_date,
    )
    profile = await session.get(EmployeeProfile, u.id)  # undecryptable stored data answers 503 DATA_UNREADABLE
    if profile is None:
        profile = EmployeeProfile(user_id=u.id)
        session.add(profile)
    profile_fields = profile_svc.apply_fields(profile, body)  # only the keys that were sent
    try:
        await session.flush()
    except IntegrityError as exc:  # two admins set the same e-mail at once (the pre-check above cannot see it)
        await session.rollback()
        raise _conflict("EMAIL_EXISTS", "Email already exists") from exc
    if old_join != u.join_date:
        from .leave import recompute_prorata  # lazy: avoids an import cycle

        await recompute_prorata(session, u, actor, old_join)
    await session.refresh(u, attribute_names=["role", "department", "shift"])
    if role_changed:  # the new role's rights must not be mixed with a session opened under the old one
        await revoke_all_for_user(session, u.id)
        bump_token_epoch(u)
    after = _snapshot(u)
    changed_org = {k: v for k, v in after.items() if before[k] != v}
    audit(
        session,
        action="employee.update",
        actor=actor,
        entity_type="user",
        entity_id=u.id,
        before={k: before[k] for k in changed_org},
        after={**changed_org, **({"profile_fields": profile_fields} if profile_fields else {}),
               **({"sessions_revoked": True} if role_changed else {})},
    )  # fmt: skip
    if profile_fields:  # field NAMES only; values are never written to the audit log
        audit(session, action="profile.update_admin", actor=actor, entity_type="profile", entity_id=u.id,
              after={"fields": profile_fields})  # fmt: skip
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _conflict("EMAIL_EXISTS", "Email already exists") from exc
    if redis is not None and {"dob", "wedding_anniversary"} & set(profile_fields):
        await celebrations.invalidate(redis)
    if profile_fields and u.id != actor.id:  # review M4: the employee is told (field names only, never values)
        await notifications.notify(
            session, [u.id], type="profile_changed", entity_id=u.id, title="Your personal details were changed",
            body=f"Your personal details were changed by HR (fields: {', '.join(profile_fields)}).",
        )  # fmt: skip
    if admin_capable and role_changed:
        await admin_security.notify_admins(
            session,
            entity_id=u.id,
            title="Account promoted to an administrator role",
            body=f"{actor.user.full_name} ({actor.employee_id}) changed {u.full_name} ({u.employee_id}) "
            f"to the role {new_role.name}.",
        )
    return await _detail(session, actor, u.id)


async def _detail(session: AsyncSession, actor: Principal, user_id: uuid.UUID) -> dict[str, Any]:
    u = await _get(session, user_id)
    out = employee_out(u, account_info=_sees_all(actor))
    out["profile"] = await _profile_out(session, actor, u)
    return out


async def _close_account_resources(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Deactivate / delete: push registrations go (no more notifications to a closed account). Returns the deleted
    FCM tokens so the caller can send the data-only `account_disabled` push AFTER the commit."""
    tokens = list((await session.execute(select(DeviceToken.fcm_token).where(DeviceToken.user_id == user_id))).scalars())
    await session.execute(delete(DeviceToken).where(DeviceToken.user_id == user_id))
    return tokens


async def set_active(
    session: AsyncSession, actor: Principal, user_id: uuid.UUID, *, active: bool, reason: str = "other"
) -> dict[str, Any]:
    if active:
        await license_svc.lock_for_seats(session)  # license/company row BEFORE the users row lock taken by _get
    u = await _get(session, user_id, lock=True)
    ensure_in_scope(actor, u)
    # both directions need the full privilege rule (review H1): HR must not reactivate / deactivate HR, Manager, Admin
    _guard_target(actor, u, elevated_needs_assign=True)
    if not active:
        if u.id == actor.id:
            raise AppError("FORBIDDEN", "You cannot deactivate your own account")
        await _guard_last_admin(session, u)
    tokens: list[str] = []
    if (u.status == "active") != active:
        if active:
            await license_svc.check_seat(session, actor)
        u.status = "active" if active else "inactive"
        if not active:
            u.deactivated_at, u.deactivated_by, u.deactivation_reason = utcnow(), actor.id, reason
            await revoke_all_for_user(session, u.id)
            bump_token_epoch(u)
            tokens = await _close_account_resources(session, u.id)
            from .leave import cancel_pending_for_user  # lazy: avoids an import cycle

            await cancel_pending_for_user(session, u, actor, why="user_deactivated")
        else:
            u.deactivated_at = u.deactivated_by = u.deactivation_reason = None
            u.must_change_password = True  # a reactivated account must choose a new password at the next sign-in
        audit(
            session,
            action="employee.activate" if active else "employee.deactivate",
            actor=actor,
            entity_type="user",
            entity_id=u.id,
            before={"status": "inactive" if active else "active"},
            after={"status": u.status, **({"reason": reason} if not active else {})},
        )
        await session.commit()
        if tokens:
            await notifications.push_account_disabled(u.id, tokens)
    return employee_out(await _get(session, u.id), account_info=_sees_all(actor))


async def unlock(session: AsyncSession, actor: Principal, user_id: uuid.UUID) -> dict[str, Any]:
    u = await _get(session, user_id, lock=True)
    ensure_in_scope(actor, u)
    _guard_target(actor, u, elevated_needs_assign=True)  # review H1
    u.failed_login_count = 0
    u.locked_until = None
    audit(session, action="employee.unlock", actor=actor, entity_type="user", entity_id=u.id)
    await session.commit()
    return employee_out(await _get(session, u.id), account_info=_sees_all(actor))


async def reset_password(
    session: AsyncSession, redis: Redis, actor: Principal, user_id: uuid.UUID, current_password: SecretStr | None
) -> dict[str, str]:
    u = await _get(session, user_id, lock=True)
    ensure_in_scope(actor, u)
    if u.id == actor.id:  # review M1: a stolen admin token must not become a permanent takeover
        raise AppError("FORBIDDEN", "You cannot reset your own password here; use change-password")
    _guard_target(actor, u, elevated_needs_assign=True)
    perms = _role_permissions(u.role)
    if perms:  # elevated target: the acting admin re-confirms with their own password
        await admin_security.require_reauth(session, redis, actor, current_password, action="employee.reset_password")
    temp = security.generate_temp_password(u.employee_id, u.full_name)
    u.password_hash = await security.ahash_password(temp)
    bump_token_epoch(u)
    u.must_change_password = True
    u.temp_password_expires_at = _temp_expiry()
    u.failed_login_count = 0
    u.locked_until = None  # reset also unlocks
    await revoke_all_for_user(session, u.id)
    open_reqs = (
        (
            await session.execute(
                select(PasswordResetRequest).where(PasswordResetRequest.user_id == u.id, PasswordResetRequest.status == "open")
            )
        )
        .scalars()
        .all()
    )
    for r in open_reqs:
        r.status, r.handled_by, r.handled_at = "handled", actor.id, utcnow()
    audit(session, action="employee.reset_password", actor=actor, entity_type="user", entity_id=u.id)
    await session.commit()
    if admin_security.is_admin_capable(perms):
        await admin_security.notify_admins(
            session,
            entity_id=u.id,
            title="Administrator password reset",
            body=f"{actor.user.full_name} ({actor.employee_id}) reset the password of {u.full_name} ({u.employee_id}).",
        )
    return {"temporary_password": temp}  # returned once; never stored or logged in plain text


async def delete_employee(session: AsyncSession, actor: Principal, user_id: uuid.UUID) -> None:
    """Soft delete: deactivates, sets deleted_at, kills all sessions. The employee_id stays reserved (never reused)."""
    u = await _get(session, user_id, lock=True)
    ensure_in_scope(actor, u)
    _guard_target(actor, u)
    if u.id == actor.id:
        raise AppError("FORBIDDEN", "You cannot delete your own account")
    await _guard_last_admin(session, u)
    before = {"status": u.status}
    u.status = "inactive"
    u.deleted_at = utcnow()
    if u.deactivated_at is None:
        u.deactivated_at, u.deactivated_by, u.deactivation_reason = u.deleted_at, actor.id, "other"
    await revoke_all_for_user(session, u.id)
    bump_token_epoch(u)
    tokens = await _close_account_resources(session, u.id)
    from .leave import cancel_pending_for_user  # lazy: avoids an import cycle

    await cancel_pending_for_user(session, u, actor, why="user_deleted")
    audit(
        session,
        action="employee.delete",
        actor=actor,
        entity_type="user",
        entity_id=u.id,
        before=before,
        after={"status": "inactive", "deleted": True},
    )
    await session.commit()
    if tokens:
        await notifications.push_account_disabled(u.id, tokens)


async def list_reset_requests(
    session: AsyncSession, actor: Principal, params: PageParams, *, status: str | None
) -> dict[str, Any]:
    cond = [PasswordResetRequest.status == status] if status else []
    if not _sees_all(actor):  # review M2: team scope only
        cond.append(User.manager_id == actor.id)
    total = (
        await session.execute(
            select(func.count())
            .select_from(PasswordResetRequest)
            .join(User, User.id == PasswordResetRequest.user_id)
            .where(*cond)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(PasswordResetRequest, User)
            .join(User, User.id == PasswordResetRequest.user_id)
            .where(*cond)
            .order_by(PasswordResetRequest.requested_at.desc())
            .limit(params.page_size)
            .offset(params.offset)
        )
    ).all()
    items = [
        {
            "id": str(r.id),
            "status": r.status,
            "requested_at": iso_z(r.requested_at),
            "handled_at": iso_z(r.handled_at) if r.handled_at else None,
            "handled_by": str(r.handled_by) if r.handled_by else None,
            "user": {"id": str(u.id), "employee_id": u.employee_id, "full_name": u.full_name},
        }
        for r, u in rows
    ]
    return make_page(items, params, total)
