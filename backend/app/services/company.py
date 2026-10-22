"""Company registration (plan 11 s.4): one company per deployment (singleton row id = 1). There is no unauthenticated
setup call: a signed-in `company.manage` holder names the company; name + logo are public (login screen branding)."""

from __future__ import annotations

from typing import Any

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import audit
from ..core.clock import iso_z, utcnow
from ..core.errors import AppError
from ..core.rbac import Principal
from ..core.text import clean_text
from ..models import Company, File, User
from . import files as files_svc
from .license import get_company

PRODUCT = "employee-connect"
API_VERSION = "1"
LOGO_PATH = "/api/public/company/logo"


def _logo_url(c: Company) -> str | None:
    return f"{LOGO_PATH}?v={c.logo_version}" if c.logo_file_id else None


def _block(c: Company) -> dict[str, Any]:
    return {
        "name": c.name,
        "logo_url": _logo_url(c),
        "logo_version": c.logo_version,
        "configured": c.setup_completed_at is not None,
    }


async def public_block(session: AsyncSession) -> dict[str, Any]:
    """The `company` object of /auth/me and /config."""
    return _block(await get_company(session))


async def public_info(session: AsyncSession) -> dict[str, Any]:
    c = await get_company(session)
    b = _block(c)
    return {
        "product": PRODUCT,
        "api_version": API_VERSION,
        "configured": b["configured"],
        "name": b["name"],
        "logo_url": b["logo_url"],
        "logo_version": b["logo_version"],
    }


async def admin_out(session: AsyncSession) -> dict[str, Any]:
    c = await get_company(session)
    upd = await session.get(User, c.updated_by) if c.updated_by else None
    return {
        **_block(c),
        "setup_completed_at": iso_z(c.setup_completed_at) if c.setup_completed_at else None,
        "deployment_id": str(c.deployment_id),
        "updated_at": iso_z(c.updated_at),
        "updated_by": (
            {
                "id": str(upd.id),
                "employee_id": upd.employee_id,
                "full_name": upd.full_name,
                "department": upd.department.name if upd.department else None,
                "photo_url": None,
            }
            if upd
            else None
        ),
    }


def clean_name(raw: str) -> str:
    name = clean_text(raw)
    if not 2 <= len(name) <= 100:
        raise AppError(
            "VALIDATION_ERROR",
            "Company name must be 2-100 characters",
            details=[{"field": "name", "message": "must be 2-100 characters after cleaning"}],
        )
    return name


async def update_name(session: AsyncSession, actor: Principal | None, raw_name: str) -> dict[str, Any]:
    """PUT /admin/company and the CLI (actor None). The first successful call completes the setup."""
    name = clean_name(raw_name)
    c = await get_company(session, lock=True)
    before = {"name": c.name, "configured": c.setup_completed_at is not None}
    c.name = name
    if c.setup_completed_at is None:
        c.setup_completed_at = utcnow()
    if actor is not None:
        c.updated_by = actor.id
        c.created_by = c.created_by or actor.id
    audit(
        session,
        action="company.update",
        actor=actor,
        actor_employee_id=None if actor else "cli",
        entity_type="company",
        entity_id=1,
        before=before,
        after={"name": name, "configured": True},
    )
    await session.commit()
    return await admin_out(session)


async def set_logo(session: AsyncSession, actor: Principal, upload: UploadFile) -> dict[str, Any]:
    new = await files_svc.store_company_logo(session, actor, upload)
    try:
        c = await get_company(session, lock=True)
        old_id = c.logo_file_id
        c.logo_file_id = new.id
        c.logo_version += 1
        c.updated_by = actor.id
        audit(
            session,
            action="company.logo_update",
            actor=actor,
            entity_type="company",
            entity_id=1,
            after={"logo_version": c.logo_version, "size_bytes": new.size_bytes},
        )
        await session.commit()
    except Exception:
        await session.rollback()
        await files_svc.delete_blob(new)
        raise
    await _drop_file(session, old_id)
    return await admin_out(session)


async def delete_logo(session: AsyncSession, actor: Principal) -> None:
    c = await get_company(session, lock=True)
    old_id = c.logo_file_id
    if old_id is None:
        return
    c.logo_file_id = None
    c.logo_version += 1
    c.updated_by = actor.id
    audit(
        session,
        action="company.logo_update",
        actor=actor,
        entity_type="company",
        entity_id=1,
        after={"logo_version": c.logo_version, "removed": True},
    )
    await session.commit()
    await _drop_file(session, old_id)


async def _drop_file(session: AsyncSession, file_id) -> None:
    """Remove the replaced logo (row + blob). Best effort: the orphan sweep also catches leftovers."""
    if file_id is None:
        return
    f = await session.get(File, file_id)
    if f is None:
        return
    await files_svc.delete_blob(f)
    await session.delete(f)
    await session.commit()


async def logo_bytes(session: AsyncSession) -> tuple[bytes, str] | None:
    """(PNG bytes, sha256 etag) of the current logo, or None."""
    c = await get_company(session)
    if c.logo_file_id is None:
        return None
    f = (await session.execute(select(File).where(File.id == c.logo_file_id))).scalar_one_or_none()
    if f is None:
        return None
    return await files_svc.read_plain(f), f.sha256 or ""
