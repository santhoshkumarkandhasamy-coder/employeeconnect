"""Test harness. NO Postgres/Redis needed: SQLite (aiosqlite) + fakeredis.

SQLite adaptation (tests only; production uses the Alembic migrations on PostgreSQL):
  * CITEXT/INET/JSONB/BigInteger/UUID server defaults are mapped to SQLite equivalents
  * CHECK constraints using Postgres syntax (`::text`, `<@`) and Postgres-only partial indexes are dropped
  * shifts.working_days (ARRAY) becomes JSON
Only the foundation tables used by auth are created. Not testable here: the DB-level rules (EXCLUDE
constraints, audit_logs append-only trigger/grants, citext semantics, real FOR UPDATE row locking).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": "sqlite+aiosqlite://",
        "JWT_SECRET": "unit-test-jwt-secret-0123456789abcdef0123456789",
        "DATA_ENCRYPTION_KEYS": Fernet.generate_key().decode(),
        "ARGON2_TIME_COST": "1",
        "ARGON2_MEMORY_KIB": "1024",
        "ARGON2_PARALLELISM": "1",
        "FORGOT_MIN_LATENCY_MS": "0",
    }
)

import fakeredis  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import JSON, BigInteger, CheckConstraint, ColumnDefault  # noqa: E402
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB, ExcludeConstraint  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core import security  # noqa: E402
from app.core.db import get_session  # noqa: E402
from app.core.redis import get_redis  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base, Company, Department, EmployeeProfile, Permission, Role, RolePermission, Shift, User  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
TABLES = [
    "roles",
    "permissions",
    "role_permissions",
    "departments",
    "shifts",
    "users",
    "employee_profiles",
    "files",
    "company",
    "license",
    "refresh_tokens",
    "device_tokens",
    "password_reset_requests",
    "audit_logs",
    "settings",
    "holidays",
    "attendance",
    "late_records",
    "leave_types",
    "leave_balances",
    "leave_requests",
    "permission_requests",
    "announcements",
    "announcement_departments",
    "announcement_reads",
    "food_enrollments",
    "salary_structures",
    "employee_payroll_details",
    "payroll_runs",
    "payslips",
]


@compiles(CITEXT, "sqlite")
def _citext(*_a, **_k):
    return "TEXT COLLATE NOCASE"


@compiles(INET, "sqlite")
def _inet(*_a, **_k):
    return "VARCHAR(45)"


@compiles(JSONB, "sqlite")
def _jsonb(*_a, **_k):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint(*_a, **_k):
    return "INTEGER"


def _adapt_metadata_for_sqlite() -> None:
    for table in Base.metadata.tables.values():
        for col in table.columns:
            sd = col.server_default
            if sd is not None and "gen_random_uuid" in str(getattr(sd, "arg", "")):
                col.server_default = None
                col.default = ColumnDefault(uuid.uuid4)
        for c in list(table.constraints):
            if isinstance(c, ExcludeConstraint):
                table.constraints.discard(c)
            elif isinstance(c, CheckConstraint) and (
                "::" in str(c.sqltext)
                or "<@" in str(c.sqltext)
                or "btrim" in str(c.sqltext)
                or "char_length" in str(c.sqltext)
                or "extract(" in str(c.sqltext)
                or "DATE '" in str(c.sqltext)
                or "trunc(" in str(c.sqltext)
            ):
                table.constraints.discard(c)
        for ix in list(table.indexes):
            if ix.dialect_options["postgresql"]["where"] is not None:
                table.indexes.discard(ix)
    Base.metadata.tables["payslips"].c.warnings.type = JSON()  # text[] in Postgres
    Base.metadata.tables["payslips"].c.warnings.server_default = None
    Base.metadata.tables["payslips"].c.warnings.default = ColumnDefault(list)
    wd = Base.metadata.tables["shifts"].c.working_days
    wd.type = JSON()
    wd.server_default = None
    wd.default = ColumnDefault([1, 2, 3, 4, 5])


_adapt_metadata_for_sqlite()


def _load_seed_module():
    path = ROOT / "database" / "seed.py"
    if not path.exists():
        pytest.skip("database/seed.py not found")
    spec = importlib.util.spec_from_file_location("ec_seed", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["ec_seed"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


PASSWORD = "Str0ng!Passw0rd#1"
USERS = {  # role -> employee_id
    "admin": "ADM001",
    "hr": "HR001",
    "manager": "MGR001",
    "employee": "EMP001",
}


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Base.metadata.tables[t] for t in TABLES]))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def seeded(sessionmaker_):
    """Roles + the real permission catalogue/mapping from database/seed.py, one user per role."""
    seed = _load_seed_module()
    async with sessionmaker_() as s:
        roles = {}
        for name, desc in seed.ROLES:
            r = Role(name=name, description=desc, is_system=True)
            s.add(r)
            roles[name] = r
        perms = {}
        for code, (desc, _) in seed.PERMISSIONS.items():
            p = Permission(code=code, module=code.split(".")[0], description=desc)
            s.add(p)
            perms[code] = p
        await s.flush()
        for code, (_, holders) in seed.PERMISSIONS.items():
            for role_name in holders:
                s.add(RolePermission(role_id=roles[role_name].id, permission_id=perms[code].id))
        s.add(Company(id=1))  # singleton created by migration 0017; the seat lock needs it
        dept = Department(name="General", code="GEN")
        shift = Shift(
            name="General Shift",
            start_time=__import__("datetime").time(9, 30),
            end_time=__import__("datetime").time(18, 30),
            grace_minutes=10,
        )
        s.add_all([dept, shift])
        await s.flush()
        ids = {}
        manager_id = None
        for role_name in ("admin", "hr", "manager", "employee"):
            u = User(
                employee_id=USERS[role_name],
                password_hash=security.hash_password(PASSWORD),
                full_name=f"Test {role_name.title()}",
                official_email=f"{role_name}@corp-mail.com",
                role_id=roles[role_name].id,
                department_id=dept.id,
                shift_id=shift.id,
                must_change_password=False,
                status="active",
                manager_id=manager_id if role_name == "employee" else None,
            )
            s.add(u)
            await s.flush()
            s.add(EmployeeProfile(user_id=u.id))
            ids[role_name] = u.id
            if role_name == "manager":
                manager_id = u.id
        await s.commit()
        return {"roles": {k: v.id for k, v in roles.items()}, "users": ids, "dept": dept.id, "shift": shift.id}


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def app(sessionmaker_, redis, seeded):
    application = create_app()

    async def _session():
        async with sessionmaker_() as s:
            yield s

    async def _redis():
        return redis

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_redis] = _redis
    return application


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def db(sessionmaker_) -> AsyncSession:
    async with sessionmaker_() as s:
        yield s


async def do_login(client, employee_id: str, password: str = PASSWORD, **extra):
    return await client.post("/api/auth/login", json={"employee_id": employee_id, "password": password, **extra})


async def login_tokens(client, employee_id: str, password: str = PASSWORD) -> dict:
    r = await do_login(client, employee_id, password)
    assert r.status_code == 200, r.text
    return r.json()


def bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest_asyncio.fixture
async def tokens_for(client):
    async def _get(role: str) -> dict:
        return await login_tokens(client, USERS[role])

    return _get


from tests.payroll_fixtures import H, payroll, uid  # noqa: E402,F401 - payroll fixtures shared by several test modules
