# api/worker image: identical to backend/Dockerfile plus OPTIONAL firebase-admin for real FCM push.
# Build context: repository root. Keep in sync with backend/Dockerfile (compose uses THIS file).
#   FIREBASE_ADMIN_VERSION=            (empty, default) -> not installed; push is skipped, app works (see backend/app/services/fcm.py)
#   FIREBASE_ADMIN_VERSION=6.9.0       -> pip install firebase-admin==6.9.0 (pin a version you have tested)
FROM python:3.12-slim AS base
ARG FIREBASE_ADMIN_VERSION=
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app/backend
WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt \
    && if [ -n "$FIREBASE_ADMIN_VERSION" ]; then pip install "firebase-admin==$FIREBASE_ADMIN_VERSION"; fi

COPY backend/ backend/
COPY database/ database/

RUN useradd --system --uid 10001 --no-create-home app && mkdir -p /data/files && chown app /data/files
USER app
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
