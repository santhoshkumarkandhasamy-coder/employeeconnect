#!/usr/bin/env bash
# Encrypted PostgreSQL backup with daily rotation.
#
#   pg_dump (custom format) -> gpg encrypt -> $BACKUP_DIR/employeeconnect_YYYYmmddTHHMMSSZ.dump.gpg (+ .sha256)
#
# Encryption (one of, checked in this order):
#   BACKUP_GPG_RECIPIENT     public-key encryption; recipient key must be in the keyring (private key kept OFF this server)
#   BACKUP_PASSPHRASE_FILE   symmetric AES256 with the passphrase read from this file (chmod 600)
# Connection: PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE (libpq env), or DATABASE_URL (sync or asyncpg form).
# Other env:
#   STATUS_DIR (optional; writes backup.json for the api health check)
#   BACKUP_DIR (default ./backups)   BACKUP_RETENTION_DAYS (default 14)
#
# Exit code non-zero on any failure; no plaintext dump is ever written to disk.
set -euo pipefail
umask 077

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR}/employeeconnect_${STAMP}.dump.gpg"
TMP="${OUT}.partial"

# Status file for the api health endpoint: $STATUS_DIR/backup.json {"db_last_success_at":..,"uploads_last_success_at":..}.
# Written only after a successful backup, preserving the other key. No STATUS_DIR (or not a directory) = skipped. Never fails the backup.
write_status() {  # $1 = db | uploads
  local dir="${STATUS_DIR:-}" f db up now
  [ -n "$dir" ] && [ -d "$dir" ] || return 0
  f="$dir/backup.json"; now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  db="$(sed -n 's/.*"db_last_success_at": *"\([^"]*\)".*/\1/p' "$f" 2>/dev/null || true)"
  up="$(sed -n 's/.*"uploads_last_success_at": *"\([^"]*\)".*/\1/p' "$f" 2>/dev/null || true)"
  if [ "$1" = db ]; then db="$now"; else up="$now"; fi
  { printf '{"db_last_success_at":%s,"uploads_last_success_at":%s}\n' \
      "$([ -n "$db" ] && printf '"%s"' "$db" || printf null)" "$([ -n "$up" ] && printf '"%s"' "$up" || printf null)" \
      > "$f.tmp" && chmod 644 "$f.tmp" && mv "$f.tmp" "$f"; } 2>/dev/null || echo "warning: could not write $f" >&2
}

log() { echo "[backup $(date -u +%FT%TZ)] $*" >&2; }
die() { log "ERROR: $*"; rm -f "$TMP"; exit 1; }

command -v pg_dump >/dev/null || die "pg_dump not found"
command -v gpg >/dev/null || die "gpg not found"
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || die "BACKUP_RETENTION_DAYS must be an integer"

if [[ -n "${DATABASE_URL:-}" && -z "${PGDATABASE:-}" ]]; then
  PG_CONN="${DATABASE_URL/postgresql+asyncpg:/postgresql:}"
  PG_CONN="${PG_CONN/postgresql+psycopg:/postgresql:}"
else
  PG_CONN=""
fi

if [[ -n "${BACKUP_GPG_RECIPIENT:-}" ]]; then
  GPG_ARGS=(--batch --yes --trust-model always --encrypt --recipient "$BACKUP_GPG_RECIPIENT")
elif [[ -n "${BACKUP_PASSPHRASE_FILE:-}" ]]; then
  [[ -r "$BACKUP_PASSPHRASE_FILE" ]] || die "cannot read BACKUP_PASSPHRASE_FILE"
  GPG_ARGS=(--batch --yes --pinentry-mode loopback --passphrase-file "$BACKUP_PASSPHRASE_FILE"
            --symmetric --cipher-algo AES256)
else
  die "set BACKUP_GPG_RECIPIENT or BACKUP_PASSPHRASE_FILE (backups must be encrypted)"
fi

mkdir -p "$BACKUP_DIR"

log "dumping database"
if [[ -n "$PG_CONN" ]]; then
  pg_dump --format=custom --no-owner --no-privileges --dbname="$PG_CONN" | gpg "${GPG_ARGS[@]}" --output "$TMP" \
    || die "dump/encrypt failed"
else
  pg_dump --format=custom --no-owner --no-privileges | gpg "${GPG_ARGS[@]}" --output "$TMP" \
    || die "dump/encrypt failed"
fi
# pipefail covers pg_dump failure; also refuse an empty result
[[ -s "$TMP" ]] || die "backup file is empty"
mv "$TMP" "$OUT"
( cd "$BACKUP_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )
log "wrote $OUT ($(du -h "$OUT" | cut -f1))"
write_status db

# rotation: delete backups (and checksums) older than the retention window
find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'employeeconnect_*.dump.gpg' -o -name 'employeeconnect_*.dump.gpg.sha256' \) \
  -mtime +"$RETENTION_DAYS" -print -delete | while read -r f; do log "rotated out $f"; done || true

log "done"
