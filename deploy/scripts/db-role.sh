#!/bin/sh
# One-shot (compose service `dbrole`): give the NOLOGIN runtime role app_user a password and LOGIN.
# Runs after migrations (which create app_user and its grants). Idempotent; safe on every `up`.
# Env: PGHOST PGUSER PGPASSWORD PGDATABASE (owner role)  APP_DB_PASSWORD
set -eu
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"
psql -v ON_ERROR_STOP=1 -v pw="$APP_DB_PASSWORD" -v db="$PGDATABASE" <<'SQL'
ALTER ROLE app_user LOGIN PASSWORD :'pw';
SELECT format('GRANT CONNECT ON DATABASE %I TO app_user', :'db') \gexec
SQL
echo "app_user can now log in"
