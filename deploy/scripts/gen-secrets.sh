#!/usr/bin/env bash
# Prints fresh secrets for deploy/.env (nothing is written to disk). Needs only openssl.
set -euo pipefail
echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
echo "APP_DB_PASSWORD=$(openssl rand -hex 24)"
echo "REDIS_PASSWORD=$(openssl rand -hex 24)"
echo "JWT_SECRET=$(openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n')"
echo "AUDIT_HMAC_KEY=$(openssl rand -hex 32)"
# Fernet key = urlsafe base64 of 32 random bytes (with padding)
echo "DATA_ENCRYPTION_KEYS=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"
