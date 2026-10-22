#!/usr/bin/env python
"""Break-glass administration CLI (run on the server, with DATABASE_URL etc. in the environment).

    python -m scripts.create_admin create  EMPLOYEE_ID "Full Name" [--email x@corp.com] [--company-name "Acme"]
                                                                                          # password: prompt or ADMIN_PASSWORD
    python -m scripts.create_admin unlock  EMPLOYEE_ID                                    # clears lockout counters
    python -m scripts.create_admin reset   EMPLOYEE_ID                                    # new temp password, printed once
    python -m scripts.create_admin license install FILE                                   # verify (Ed25519, public key) + store
    python -m scripts.create_admin license show                                           # status, seats, deployment id

Use when the only Admin is locked out (login lockout is per account) or lost. Every action is written to the audit
log (actor_employee_id = "cli", entity = the user) and all sessions of the target are revoked on reset.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import getpass
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.audit import audit
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.errors import AppError
from app.models import EmployeeProfile, Role, User
from app.services import company as company_svc
from app.services import license as license_svc
from app.services.auth import bump_token_epoch, canonical_employee_id, revoke_all_for_user


class CliError(Exception):
    pass


async def _user(session: AsyncSession, employee_id: str) -> User:
    u = (
        (
            await session.execute(
                select(User).where(User.employee_id == canonical_employee_id(employee_id)).with_for_update(of=User)
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if u is None:
        raise CliError(f"no such employee: {employee_id}")
    return u


async def create_admin(
    session: AsyncSession,
    employee_id: str,
    full_name: str,
    password: str,
    email: str | None = None,
    company_name: str | None = None,
) -> User:
    eid = canonical_employee_id(employee_id)
    problems = security.validate_password(password, eid, full_name)
    if problems:
        raise CliError("password rejected: " + "; ".join(problems))
    await license_svc.lock_for_seats(session)  # license/company row BEFORE any users row (deadlock rule)
    try:
        await license_svc.check_seat(session, None)  # audited as cli, commits before raising
    except AppError as exc:
        raise CliError(f"{exc.code}: {exc.message}") from exc
    if (await session.execute(select(User.id).where(User.employee_id == eid))).first():
        raise CliError(f"employee id already exists: {eid}")
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        raise CliError("admin role missing - run database/seed.py first")
    user = User(
        id=uuid.uuid4(),
        employee_id=eid,
        password_hash=await security.ahash_password(password),
        full_name=full_name.strip(),
        official_email=email,
        role_id=role.id,
        status="active",
        must_change_password=True,
        failed_login_count=0,
    )
    session.add(user)
    await session.flush()
    session.add(EmployeeProfile(user_id=user.id))
    audit(session, action="cli.create_admin", actor_employee_id="cli", entity_type="user", entity_id=user.id)
    await session.commit()
    if company_name:
        try:
            await company_svc.update_name(session, None, company_name)
        except AppError as exc:
            raise CliError(f"admin created, but the company name was refused: {exc.message}") from exc
    return user


async def license_install(session: AsyncSession, path: str) -> dict:
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CliError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        return await license_svc.install_token(session, token, actor=None)
    except AppError as exc:
        raise CliError(f"{exc.code}: {exc.message}") from exc


async def unlock(session: AsyncSession, employee_id: str) -> User:
    u = await _user(session, employee_id)
    u.failed_login_count = 0
    u.locked_until = None
    audit(session, action="cli.unlock", actor_employee_id="cli", entity_type="user", entity_id=u.id)
    await session.commit()
    return u


async def reset_password(session: AsyncSession, employee_id: str) -> str:
    u = await _user(session, employee_id)
    temp = security.generate_temp_password(u.employee_id, u.full_name)
    u.password_hash = await security.ahash_password(temp)
    u.must_change_password = True
    u.temp_password_expires_at = utcnow() + dt.timedelta(hours=get_settings().temp_password_hours)  # review L4
    u.failed_login_count = 0
    u.locked_until = None
    await revoke_all_for_user(session, u.id)
    bump_token_epoch(u)
    audit(session, action="cli.reset_password", actor_employee_id="cli", entity_type="user", entity_id=u.id)
    await session.commit()
    return temp


async def _main(args: argparse.Namespace) -> int:
    async with get_sessionmaker()() as session:
        try:
            if args.cmd == "create":
                pw = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password: ")
                u = await create_admin(session, args.employee_id, args.full_name, pw, args.email, args.company_name)
                print(f"created admin {u.employee_id} (must change password at first login)")
            elif args.cmd == "license":
                if args.license_cmd == "install":
                    info = await license_install(session, args.file)
                else:
                    info = await license_svc.license_out(session)
                for k, v in info.items():
                    print(f"{k}: {v}")
            elif args.cmd == "unlock":
                u = await unlock(session, args.employee_id)
                print(f"unlocked {u.employee_id}")
            else:
                print("temporary password (shown once):", await reset_password(session, args.employee_id))
        except CliError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("employee_id")
    c.add_argument("full_name")
    c.add_argument("--email")
    c.add_argument("--company-name", dest="company_name")
    sub.add_parser("unlock").add_argument("employee_id")
    sub.add_parser("reset").add_argument("employee_id")
    lic = sub.add_parser("license").add_subparsers(dest="license_cmd", required=True)
    lic.add_parser("install").add_argument("file")
    lic.add_parser("show")
    sys.exit(asyncio.run(_main(ap.parse_args())))


if __name__ == "__main__":
    main()
