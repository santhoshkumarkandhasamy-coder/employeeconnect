"""encrypt leave/permission reason + decision_comment at rest (review 03 M1)

Revision ID: 0009
Revises: 0008

reason / decision_comment (plaintext text) become reason_enc / decision_comment_enc (bytea, Fernet via the app's
EncryptedString). The reason column becomes nullable: the retention job nulls it for old decided requests.

Online upgrade (the only safe path for a database that already holds requests):
  1. add the *_enc columns
  2. backfill in batches of 500 inside the migration transaction: encrypt with DATA_ENCRYPTION_KEYS (newest key), decrypt
     again and compare before the row is written; any failure aborts and rolls back the whole migration (PostgreSQL DDL is
     transactional), so there is never a half-converted state
  3. verify that no row still has plaintext without ciphertext
  4. only then drop the plaintext columns
DATA_ENCRYPTION_KEYS must be set in the environment that runs `alembic upgrade`. Keep a backup (database/scripts/backup.sh)
before running it; the plaintext is gone after step 4.

Offline mode (`alembic upgrade --sql`, used by scripts/gen_schema.sh) cannot encrypt: it emits the final structure only and
a comment; use it for empty databases / documentation, never to upgrade a database with data.

Downgrade decrypts back into plaintext columns (needs the same keys); rows whose reason was purged get the text '[purged]'.
"""
import sqlalchemy as sa
from alembic import context, op
from migration_sql import run

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLES = ("leave_requests", "permission_requests")
BATCH = 500

ADD = r"""
ALTER TABLE leave_requests ADD COLUMN reason_enc bytea;
ALTER TABLE leave_requests ADD COLUMN decision_comment_enc bytea;
ALTER TABLE permission_requests ADD COLUMN reason_enc bytea;
ALTER TABLE permission_requests ADD COLUMN decision_comment_enc bytea;
"""

DROP_PLAIN = r"""
ALTER TABLE leave_requests DROP COLUMN reason;
ALTER TABLE leave_requests DROP COLUMN decision_comment;
ALTER TABLE permission_requests DROP COLUMN reason;
ALTER TABLE permission_requests DROP COLUMN decision_comment;
"""

DOWN_ADD = r"""
ALTER TABLE leave_requests ADD COLUMN reason text;
ALTER TABLE leave_requests ADD COLUMN decision_comment text;
ALTER TABLE permission_requests ADD COLUMN reason text;
ALTER TABLE permission_requests ADD COLUMN decision_comment text;
"""

DOWN_FINISH = r"""
UPDATE leave_requests SET reason = '[purged]' WHERE reason IS NULL;
UPDATE permission_requests SET reason = '[purged]' WHERE reason IS NULL;
ALTER TABLE leave_requests ALTER COLUMN reason SET NOT NULL;
ALTER TABLE permission_requests ALTER COLUMN reason SET NOT NULL;
ALTER TABLE leave_requests DROP COLUMN reason_enc;
ALTER TABLE leave_requests DROP COLUMN decision_comment_enc;
ALTER TABLE permission_requests DROP COLUMN reason_enc;
ALTER TABLE permission_requests DROP COLUMN decision_comment_enc;
"""


def _crypto():
    from app.core.crypto import decrypt_bytes, encrypt_bytes, validate_keys

    try:
        validate_keys()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("DATA_ENCRYPTION_KEYS must be set to run migration 0009 (it encrypts existing data)") from exc
    return encrypt_bytes, decrypt_bytes


def _enc(value, encrypt, decrypt):
    if value is None:
        return None
    token = encrypt(value.encode("utf-8"))
    if decrypt(token).decode("utf-8") != value:  # never write a token we cannot read back
        raise RuntimeError("encryption round-trip failed")
    return token


def _backfill(conn) -> None:
    encrypt, decrypt = _crypto()
    for t in TABLES:
        last = None
        while True:
            q = f"SELECT id, reason, decision_comment FROM {t} WHERE (reason IS NOT NULL OR decision_comment IS NOT NULL) AND reason_enc IS NULL AND decision_comment_enc IS NULL"  # noqa: S608,E501
            if last is not None:
                q += " AND id > :last"
            q += " ORDER BY id LIMIT :n"
            rows = conn.execute(sa.text(q), {"n": BATCH, **({"last": last} if last is not None else {})}).all()
            if not rows:
                break
            for rid, reason, comment in rows:
                conn.execute(
                    sa.text(f"UPDATE {t} SET reason_enc = :r, decision_comment_enc = :c WHERE id = :id"),  # noqa: S608
                    {"r": _enc(reason, encrypt, decrypt), "c": _enc(comment, encrypt, decrypt), "id": rid},
                )
            last = rows[-1][0]
        left = conn.execute(
            sa.text(
                f"SELECT count(*) FROM {t} WHERE (reason IS NOT NULL AND reason_enc IS NULL) "  # noqa: S608
                "OR (decision_comment IS NOT NULL AND decision_comment_enc IS NULL)"
            )
        ).scalar_one()
        if left:
            raise RuntimeError(f"{t}: {left} row(s) still have plaintext without ciphertext; aborting")


def _restore(conn) -> None:
    _, decrypt = _crypto()
    for t in TABLES:
        rows = conn.execute(sa.text(f"SELECT id, reason_enc, decision_comment_enc FROM {t}")).all()  # noqa: S608
        for rid, r, c in rows:
            conn.execute(
                sa.text(f"UPDATE {t} SET reason = :r, decision_comment = :c WHERE id = :id"),  # noqa: S608
                {
                    "r": decrypt(bytes(r)).decode("utf-8") if r is not None else None,
                    "c": decrypt(bytes(c)).decode("utf-8") if c is not None else None,
                    "id": rid,
                },
            )


def upgrade() -> None:
    run(ADD)
    if context.is_offline_mode():
        op.execute(sa.DDL("-- offline: NO data conversion; only for empty databases (see migration docstring)"))
    else:
        _backfill(op.get_bind())
    run(DROP_PLAIN)


def downgrade() -> None:
    run(DOWN_ADD)
    if not context.is_offline_mode():
        _restore(op.get_bind())
    run(DOWN_FINISH)
