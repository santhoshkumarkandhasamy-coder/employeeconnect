#!/usr/bin/env bash
# Backs up the uploads volume (photos, leave attachments, selfies) - NOT covered by backup.sh (pg_dump only).
# Files are already Fernet-encrypted at rest for selfies/attachments; profile photos and announcement files are not,
# so the archive is GPG-encrypted like the DB dumps (same BACKUP_GPG_RECIPIENT or BACKUP_PASSPHRASE_FILE).
#
# Run from the host (deploy/), e.g. nightly cron:
#   docker run --rm -v employeeconnect_uploads:/data:ro -v employeeconnect_backups:/backups \
#     -e BACKUP_GPG_RECIPIENT -e BACKUP_PASSPHRASE_FILE -e BACKUP_RETENTION_DAYS \
#     -v "$PWD/secrets/backup:/run/backup-secrets:ro" --entrypoint /usr/local/bin/backup-uploads.sh employeeconnect-backup
# Output: $BACKUP_DIR/employeeconnect_uploads_<stamp>.tgz.gpg (+ .sha256); older than BACKUP_RETENTION_DAYS are deleted.
set -euo pipefail
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

SRC="${UPLOADS_DIR:-/data}"
OUT="${BACKUP_DIR:-/backups}"
KEEP="${BACKUP_RETENTION_DAYS:-14}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
file="$OUT/employeeconnect_uploads_${stamp}.tgz.gpg"
tmp="${file}.partial"   # written here first and renamed only on success, so a failed tar never leaves a file that looks like a valid backup
trap 'rm -f "$tmp"' EXIT
[ -d "$SRC" ] || { echo "uploads dir $SRC missing" >&2; exit 1; }
umask 077
if [ -n "${BACKUP_GPG_RECIPIENT:-}" ]; then
  tar czf - -C "$SRC" . | gpg --batch --yes --trust-model always -e -r "$BACKUP_GPG_RECIPIENT" -o "$tmp"
elif [ -n "${BACKUP_PASSPHRASE_FILE:-}" ]; then
  tar czf - -C "$SRC" . | gpg --batch --yes --pinentry-mode loopback --passphrase-file "$BACKUP_PASSPHRASE_FILE" -c -o "$tmp"
else
  echo "refusing to write an unencrypted uploads backup: set BACKUP_GPG_RECIPIENT or BACKUP_PASSPHRASE_FILE" >&2
  exit 1
fi
[ -s "$tmp" ] || { echo "uploads backup is empty" >&2; exit 1; }
mv "$tmp" "$file"
( cd "$OUT" && sha256sum "$(basename "$file")" > "$(basename "$file").sha256" )
find "$OUT" -name 'employeeconnect_uploads_*' -mtime "+$KEEP" -delete
echo "uploads backup written: $file"
write_status uploads
