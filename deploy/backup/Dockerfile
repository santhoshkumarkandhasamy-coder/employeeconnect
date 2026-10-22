# Backup sidecar: pg_dump/pg_restore 16 (must match the server major version) + gpg + the repo's backup scripts.
# Build context: repository root.
FROM postgres:16-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends gnupg \
    && rm -rf /var/lib/apt/lists/*
COPY database/scripts/backup.sh /usr/local/bin/backup.sh
COPY database/scripts/backup-uploads.sh /usr/local/bin/backup-uploads.sh
COPY database/scripts/restore.sh /usr/local/bin/restore.sh
COPY deploy/backup/entrypoint.sh /usr/local/bin/backup-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/*.sh && chmod 755 /usr/local/bin/*.sh \
    && mkdir -p /backups && chown postgres:postgres /backups
ENV BACKUP_DIR=/backups UPLOADS_DIR=/data GNUPGHOME=/tmp/gnupg
USER postgres
ENTRYPOINT ["/usr/local/bin/backup-entrypoint.sh"]
