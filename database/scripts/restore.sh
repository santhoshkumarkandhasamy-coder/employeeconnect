#!/usr/bin/env bash
# Restore an encrypted backup made by backup.sh.
#
#   restore.sh <backup-file.dump.gpg> [--target-db NAME] [--yes]
#
# Decrypts via gpg (private key in keyring for public-key backups, or BACKUP_PASSPHRASE_FILE for symmetric)
# and streams into pg_restore; no plaintext dump touches disk.
#
# Safe default: restores into a NEW database name (employeeconnect_restore_<stamp>) so a restore drill never
# overwrites production. To replace the live DB pass --target-db <existing> --yes (objects are dropped and recreated).
# Connection: libpq env (PGHOST PGPORT PGUSER PGPASSWORD) or DATABASE_URL for host/credentials.
# After restoring into a fresh DB the app role grants are re-applied below (backup uses --no-privileges).
set -euo pipefail
umask 077

log() { echo "[restore $(date -u +%FT%TZ)] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

FILE="${1:-}"; shift || true
[[ -n "$FILE" && -f "$FILE" ]] || die "usage: restore.sh <backup.dump.gpg> [--target-db NAME] [--yes]"
TARGET_DB=""; ASSUME_YES=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-db) TARGET_DB="${2:-}"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    *) die "unknown argument $1" ;;
  esac
done

command -v pg_restore >/dev/null || die "pg_restore not found"
command -v psql >/dev/null || die "psql not found"
command -v gpg >/dev/null || die "gpg not found"

# verify checksum when present
if [[ -f "${FILE}.sha256" ]]; then
  ( cd "$(dirname "$FILE")" && sha256sum -c "$(basename "$FILE").sha256" >/dev/null ) || die "checksum mismatch - backup corrupted"
  log "checksum ok"
fi

if [[ -n "${DATABASE_URL:-}" && -z "${PGHOST:-}" ]]; then
  # derive libpq env from the URL: scheme://user:pass@host:port/db
  U="${DATABASE_URL#*://}"; CRED="${U%%@*}"; HP="${U#*@}"; HOSTPORT="${HP%%/*}"; DBN="${HP#*/}"; DBN="${DBN%%\?*}"
  export PGUSER="${CRED%%:*}" PGPASSWORD="${CRED#*:}" PGHOST="${HOSTPORT%%:*}"
  [[ "$HOSTPORT" == *:* ]] && export PGPORT="${HOSTPORT#*:}"
  : "${PGDATABASE:=$DBN}"
fi

if [[ -z "$TARGET_DB" ]]; then
  TARGET_DB="employeeconnect_restore_$(date -u +%Y%m%dT%H%M%SZ)"
  CREATE_DB=1
else
  CREATE_DB=0
  if [[ $ASSUME_YES -ne 1 ]]; then
    read -r -p "This will DROP and recreate objects in database '$TARGET_DB'. Type the db name to continue: " ans
    [[ "$ans" == "$TARGET_DB" ]] || die "aborted"
  fi
fi

if [[ -n "${BACKUP_PASSPHRASE_FILE:-}" ]]; then
  GPG_ARGS=(--batch --pinentry-mode loopback --passphrase-file "$BACKUP_PASSPHRASE_FILE" --decrypt)
else
  GPG_ARGS=(--batch --decrypt)   # public-key backup: gpg-agent supplies the private key / passphrase
fi

if [[ $CREATE_DB -eq 1 ]]; then
  log "creating database $TARGET_DB"
  psql -v ON_ERROR_STOP=1 -d postgres -c "CREATE DATABASE \"$TARGET_DB\"" >/dev/null
  RESTORE_FLAGS=(--no-owner --exit-on-error)
else
  RESTORE_FLAGS=(--no-owner --clean --if-exists --exit-on-error)
fi

log "restoring into $TARGET_DB"
gpg "${GPG_ARGS[@]}" "$FILE" | pg_restore "${RESTORE_FLAGS[@]}" --dbname="$TARGET_DB" || die "restore failed"

# re-apply runtime grants (dump was taken with --no-privileges). Ignore if app_user does not exist.
psql -v ON_ERROR_STOP=0 -d "$TARGET_DB" >/dev/null 2>&1 <<'SQL' || true
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_logs FROM app_user;
SQL

log "sanity check"
psql -v ON_ERROR_STOP=1 -d "$TARGET_DB" -At -c \
  "SELECT 'users=' || (SELECT count(*) FROM users) || ' audit_logs=' || (SELECT count(*) FROM audit_logs) || ' alembic=' || coalesce((SELECT version_num FROM alembic_version LIMIT 1),'none')" >&2
log "done. Point DATABASE_URL at '$TARGET_DB' (or rename it) after verifying."
