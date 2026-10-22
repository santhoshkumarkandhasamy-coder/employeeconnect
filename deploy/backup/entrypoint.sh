#!/usr/bin/env bash
# Backup scheduler: runs backup.sh once a day at BACKUP_TIME_UTC (DB) and BACKUP_UPLOADS_TIME_UTC (uploads, default 02:30; same loop in a subshell) (HH:MM, default 02:00) and once at start-up if there is
# no backup younger than 24 h. Failures are logged and retried at the next slot; the container healthcheck turns red
# when the newest backup is older than ~26 h.
#
# Modes:  `backup-entrypoint.sh`            run the scheduler (default)
#         `backup-entrypoint.sh once`       one DB backup now, then exit
#         `backup-entrypoint.sh once-uploads` one uploads-volume backup now, then exit (volume mounted read-only at /data)
#         `backup-entrypoint.sh <cmd> ...`  exec an arbitrary command after gpg setup (e.g. restore.sh <file>)
set -euo pipefail
umask 077
log() { echo "[backup-sidecar $(date -u +%FT%TZ)] $*" >&2; }

BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_TIME_UTC="${BACKUP_TIME_UTC:-02:00}"
[[ "$BACKUP_TIME_UTC" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { log "BACKUP_TIME_UTC must be HH:MM"; exit 1; }

mkdir -p "${GNUPGHOME:-/tmp/gnupg}"; chmod 700 "${GNUPGHOME:-/tmp/gnupg}"
if [[ -n "${BACKUP_GPG_PUBLIC_KEY_FILE:-}" ]]; then
  [[ -r "$BACKUP_GPG_PUBLIC_KEY_FILE" ]] || { log "cannot read BACKUP_GPG_PUBLIC_KEY_FILE"; exit 1; }
  gpg --batch --import "$BACKUP_GPG_PUBLIC_KEY_FILE" 2>&1 | sed 's/^/[gpg] /' >&2
fi
if [[ -z "${BACKUP_GPG_RECIPIENT:-}" && -z "${BACKUP_PASSPHRASE_FILE:-}" ]]; then
  log "set BACKUP_GPG_RECIPIENT (+ BACKUP_GPG_PUBLIC_KEY_FILE) or BACKUP_PASSPHRASE_FILE"; exit 1
fi

BACKUP_UPLOADS_TIME_UTC="${BACKUP_UPLOADS_TIME_UTC:-02:30}"
[[ "$BACKUP_UPLOADS_TIME_UTC" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { log "BACKUP_UPLOADS_TIME_UTC must be HH:MM"; exit 1; }

if [[ "${1:-}" == "once" ]]; then exec /usr/local/bin/backup.sh; fi
if [[ "${1:-}" == "once-uploads" ]]; then exec /usr/local/bin/backup-uploads.sh; fi
if [[ $# -gt 0 ]]; then exec "$@"; fi

recent() { find "$BACKUP_DIR" -maxdepth 1 -name 'employeeconnect_*.dump.gpg' -mmin -1440 | grep -q .; }

if ! recent; then
  log "no backup in the last 24h, running one now"
  /usr/local/bin/backup.sh || log "start-up backup FAILED"
fi

# nightly loop: daily_loop <HH:MM> <label> <command>
daily_loop() {
  local at="$1" label="$2" cmd="$3" now next
  while true; do
    now=$(date -u +%s)
    next=$(date -u -d "today ${at}" +%s)
    (( next > now )) || next=$(date -u -d "tomorrow ${at}" +%s)
    log "next ${label} backup at $(date -u -d "@$next" +%FT%TZ)"
    sleep $(( next - now ))
    "$cmd" || log "scheduled ${label} backup FAILED"
  done
}

recent_uploads() { find "$BACKUP_DIR" -maxdepth 1 -name 'employeeconnect_uploads_*.tgz.gpg' -mmin -1440 | grep -q .; }
if ! recent_uploads; then
  log "no uploads backup in the last 24h, running one now"
  /usr/local/bin/backup-uploads.sh || log "start-up uploads backup FAILED"
fi

daily_loop "$BACKUP_UPLOADS_TIME_UTC" uploads /usr/local/bin/backup-uploads.sh &
daily_loop "$BACKUP_TIME_UTC" database /usr/local/bin/backup.sh &
wait -n   # either loop ending is a bug: exit so the restart policy recreates the container
