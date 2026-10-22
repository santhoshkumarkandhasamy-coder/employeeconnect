# Build context: repository root (so database/ is available to Alembic/seed):
#   docker build -f backend/Dockerfile -t employeeconnect-api .
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app/backend
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend/ backend/
COPY database/ database/

RUN useradd --system --uid 10001 --no-create-home app && mkdir -p /data/files && chown app /data/files
USER app
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
# Client IP: uvicorn's own proxy-header rewriting is OFF; the app resolves X-Real-IP / X-Forwarded-For itself and only
# when the direct peer is inside TRUSTED_PROXIES (set it to the nginx container network, e.g. 172.18.0.0/16).
# Do NOT publish port 8000 on the host (compose `expose:` only) and make nginx OVERWRITE the header:
#   proxy_set_header X-Real-IP $remote_addr;
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
