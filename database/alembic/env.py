"""Alembic environment.

Reads DATABASE_URL (async or sync form) and imports app.models from backend/
so autogenerate can be used for diffs. Migrations here are hand-written SQL and
are authoritative; run them as the owner role (app_owner), not app_user.
"""
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

HERE = Path(__file__).resolve().parent
# database/ (for migration_sql.py) and backend/ (for app.models)
for candidate in (HERE.parent, HERE.parent.parent / "backend", Path("/app/backend")):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if not url:
        if context.is_offline_mode():
            return "postgresql+psycopg://offline/offline"
        raise RuntimeError("DATABASE_URL is not set")
    # The app uses asyncpg; Alembic needs a sync driver.
    for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
