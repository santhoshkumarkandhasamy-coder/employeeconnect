# One-shot image for `alembic upgrade head` and database/seed.py (run as the DB owner role, never as app_user).
# Build context: repository root.  Same base as backend/Dockerfile plus the migration-only dependencies, which are
# not in backend/requirements.txt, see backend/requirements-migrate.txt (alembic + psycopg are needed by database/alembic/env.py and database/seed.py).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app/backend
WORKDIR /app
# requirements-migrate.txt is pinned and includes `-r requirements.txt`, so both files must be present.
COPY backend/requirements.txt backend/requirements-migrate.txt backend/
RUN pip install -r backend/requirements-migrate.txt
COPY backend/ backend/
COPY database/ database/
RUN useradd --system --uid 10001 --no-create-home app
USER app
WORKDIR /app/database
CMD ["alembic", "upgrade", "head"]
