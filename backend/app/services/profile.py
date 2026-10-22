"""Personal profile (plan 07, contract section 2). Sensitive columns are encrypted by the model column types."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi import UploadFile
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.crypto import DataDecryptionError
from ..core.rbac import Principal
from ..models import EmployeeProfile, File, User
from . import celebrations
from . import files as files_svc

log = logging.getLogger("app.profile")
PHOTO_MAX_SIDE = 512
_PLAIN = (
    EmployeeProfile.photo_file_id,
    EmployeeProfile.show_celebrations,
)


async def _load(session: AsyncSession, user: User) -> Any:
    """The profile row, or a stand-in with only the unencrypted columns when stored values cannot be decrypted
    (key mismatch after rotation): the sensitive fields are then null, the error is logged and nothing returns 500."""
    try:
        p = await session.get(EmployeeProfile, user.id)
    except DataDecryptionError:
        log.error("profile of user %s cannot be decrypted with the configured keys", user.id)
        row = (await session.execute(select(*_PLAIN).where(EmployeeProfile.user_id == user.id))).one_or_none()
        plain = dict(zip(("photo_file_id", "show_celebrations"), row or (None,) * 2, strict=True))
        return SimpleNamespace(
            dob=None, wedding_anniversary=None, personal_email=None, phone=None, emergency_name=None, emergency_phone=None,
            address=None, emergency_relationship=None, blood_group=None, **plain,
        )  # fmt: skip
    if p is None:
        p = EmployeeProfile(user_id=user.id)
        session.add(p)
        await session.flush()
        await session.refresh(p)
    return p


def _emergency(p: Any) -> dict[str, Any] | None:
    if p.emergency_name and p.emergency_phone:
        return {"name": p.emergency_name, "relationship": p.emergency_relationship, "phone": p.emergency_phone}
    return None


def _out(user: User, p: Any) -> dict[str, Any]:
    emergency = _emergency(p)
    return {
        "user_id": str(user.id),
        "employee_id": user.employee_id,
        "full_name": user.full_name,
        "department": {"id": str(user.department.id), "name": user.department.name} if user.department else None,
        "role": user.role.name,
        "official_email": user.official_email,
        "photo_url": files_svc.file_url(p.photo_file_id),
        "dob": p.dob,
        "wedding_anniversary": p.wedding_anniversary,
        "personal_email": p.personal_email,
        "phone": p.phone,
        "emergency_contact": emergency,
        "blood_group": p.blood_group,
        "address": p.address,
        "show_celebrations": bool(p.show_celebrations),
    }


def sensitive_out(p: Any) -> dict[str, Any]:
    """Personal fields as shown to HR/Admin (`employee.view_sensitive`) in the admin employee detail."""
    emergency = _emergency(p)
    return {
        "dob": p.dob,
        "wedding_anniversary": p.wedding_anniversary,
        "personal_email": p.personal_email,
        "phone": p.phone,
        "emergency_contact": emergency,
        "blood_group": p.blood_group,
        "address": p.address,
    }


async def load_profile(session: AsyncSession, user: User) -> Any:
    return await _load(session, user)


async def get_profile(session: AsyncSession, actor: Principal) -> dict[str, Any]:
    return _out(actor.user, await _load(session, actor.user))


def apply_fields(p: Any, body: Any) -> list[str]:
    """Copies the personal fields that were SENT in `body` onto the profile row (an explicit null clears). Returns the
    field NAMES that were touched (never values). Shared by self-service and the admin employee edit."""
    sent = body.model_fields_set
    changed: list[str] = []
    for key in ("dob", "wedding_anniversary"):
        if key in sent:
            v = getattr(body, key)
            setattr(p, key, v.isoformat() if v else None)
            changed.append(key)
    for key in ("personal_email", "phone", "blood_group", "address"):
        if key in sent:
            v = getattr(body, key)
            setattr(p, key, str(v) if v is not None else None)
            changed.append(key)
    if "emergency_contact" in sent:
        e = body.emergency_contact
        p.emergency_name, p.emergency_relationship, p.emergency_phone = (
            (e.name, e.relationship, e.phone) if e else (None, None, None)
        )
        changed.append("emergency_contact")
    return changed


async def update_profile(session: AsyncSession, redis: Redis | None, actor: Principal, body: Any) -> dict[str, Any]:
    p = await session.get(EmployeeProfile, actor.id)  # a decryption failure here answers 503 DATA_UNREADABLE
    if p is None:
        p = EmployeeProfile(user_id=actor.id)
        session.add(p)
    sent = body.model_fields_set
    changed = apply_fields(p, body)
    if "show_celebrations" in sent:
        p.show_celebrations = body.show_celebrations
        changed.append("show_celebrations")
    if changed:
        audit(session, action="profile.update", actor=actor, entity_type="profile", entity_id=actor.id,
              after={"fields": changed})  # field NAMES only, never values  # fmt: skip
    await session.commit()
    await session.refresh(p)
    if redis is not None and {"dob", "wedding_anniversary", "show_celebrations"} & set(changed):
        await celebrations.invalidate(redis)
    return _out(actor.user, p)


async def set_photo(session: AsyncSession, redis: Redis | None, actor: Principal, upload: UploadFile) -> dict[str, Any]:
    stored = await files_svc.upload(session, actor, "profile_photo", upload, max_side=PHOTO_MAX_SIDE)
    new_id = stored["id"]
    p = await session.get(EmployeeProfile, actor.id, with_for_update=True)  # parallel uploads must not orphan a blob
    if p is None:
        p = EmployeeProfile(user_id=actor.id)
        session.add(p)
    old_id = p.photo_file_id
    p.photo_file_id = uuid.UUID(new_id)
    audit(session, action="profile.photo", actor=actor, entity_type="profile", entity_id=actor.id)
    await session.commit()
    if old_id is not None:  # the replaced photo is deleted (row and blob)
        old = await session.get(File, old_id)
        if old is not None and old.owner_id == actor.id and old.purpose == "profile_photo":
            await files_svc.delete_blob(old)
            await session.delete(old)
            await session.commit()
    await session.refresh(p)
    return _out(actor.user, p)
