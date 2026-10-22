"""Admin module security hardening (docs/reviews/06-admin-security.md)

Revision ID: 0013
Revises: 0012

* users.temp_password_expires_at   (M7) admin-issued temporary passwords stop working after TEMP_PASSWORD_HOURS (72 h).
                                   NULL = no expiry (existing rows and the seeded bootstrap admin are NOT given a deadline,
                                   so nobody is locked out by this migration).
* trips.end_reason                 (L4) CHECK also allows 'account_deactivated' (trip closed because the account was
                                   deactivated or deleted).
* employee_profiles.blood_group / emergency_relationship   (L6) plaintext text columns become blood_group_enc /
                                   emergency_relationship_enc (bytea, Fernet via the app's EncryptedString). The value set
                                   CHECK moves into the application (schemas/profile.py BloodGroup).

Online upgrade of the profile columns (same approach as 0009): add the *_enc columns, backfill in batches of 500 inside the
migration transaction (encrypt with DATA_ENCRYPTION_KEYS, decrypt again and compare before the row is written; any failure
aborts and rolls back everything), verify nothing is left, then drop the plaintext columns. On an empty table the loop does
nothing. DATA_ENCRYPTION_KEYS must be set in the environment that runs `alembic upgrade`. Keep a backup first.

Offline mode (`alembic upgrade --sql`, used by scripts/gen_schema.sh) cannot encrypt: it emits the final structure only; use
it for empty databases / documentation, never to upgrade a database that holds profiles.

Downgrade decrypts back into plaintext columns (needs the same keys). Rows whose value is no longer in the old CHECK set
(blood group) are stored as NULL. Trips closed with 'account_deactivated' are re-labelled 'admin_closed' first.
"""
import sqlalchemy as sa
from alembic import context, op
from migration_sql import run

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

BATCH = 500
BLOOD = "('A+','A-','B+','B-','AB+','AB-','O+','O-')"

ADD = r"""
ALTER TABLE users ADD COLUMN temp_password_expires_at timestamptz;

ALTER TABLE trips DROP CONSTRAINT ck_trips_end_reason;
ALTER TABLE trips ADD CONSTRAINT ck_trips_end_reason CHECK (end_reason IS NULL OR end_reason IN ('reached_home','auto_geofence','cancelled','admin_closed','expired','account_deactivated'));

ALTER TABLE employee_profiles ADD COLUMN blood_group_enc bytea;
ALTER TABLE employee_profiles ADD COLUMN emergency_relationship_enc bytea;
"""

DROP_PLAIN = r"""
ALTER TABLE employee_profiles DROP CONSTRAINT ck_employee_profiles_blood_group;
ALTER TABLE employee_profiles DROP COLUMN blood_group;
ALTER TABLE employee_profiles DROP COLUMN emergency_relationship;
"""

DOWN_ADD = """
ALTER TABLE employee_profiles ADD COLUMN blood_group text;
ALTER TABLE employee_profiles ADD COLUMN emergency_relationship text;
"""

DOWN_FINISH = f"""
UPDATE employee_profiles SET blood_group = NULL WHERE blood_group IS NOT NULL AND blood_group NOT IN {BLOOD};
ALTER TABLE employee_profiles ADD CONSTRAINT ck_employee_profiles_blood_group CHECK (blood_group IS NULL OR blood_group IN {BLOOD});
ALTER TABLE employee_profiles DROP COLUMN blood_group_enc;
ALTER TABLE employee_profiles DROP COLUMN emergency_relationship_enc;

UPDATE trips SET end_reason = 'admin_closed' WHERE end_reason = 'account_deactivated';
ALTER TABLE trips DROP CONSTRAINT ck_trips_end_reason;
ALTER TABLE trips ADD CONSTRAINT ck_trips_end_reason CHECK (end_reason IS NULL OR end_reason IN ('reached_home','auto_geofence','cancelled','admin_closed','expired'));

ALTER TABLE users DROP COLUMN temp_password_expires_at;
"""


def _crypto():
    from app.core.crypto import decrypt_bytes, encrypt_bytes, validate_keys

    try:
        validate_keys()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("DATA_ENCRYPTION_KEYS must be set to run migration 0013 (it encrypts existing data)") from exc
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
    last = None
    while True:
        q = (
            "SELECT user_id, blood_group, emergency_relationship FROM employee_profiles "
            "WHERE (blood_group IS NOT NULL OR emergency_relationship IS NOT NULL) "
            "AND blood_group_enc IS NULL AND emergency_relationship_enc IS NULL"
        )
        if last is not None:
            q += " AND user_id > :last"
        q += " ORDER BY user_id LIMIT :n"
        rows = conn.execute(sa.text(q), {"n": BATCH, **({"last": last} if last is not None else {})}).all()
        if not rows:
            break
        for uid, blood, rel in rows:
            conn.execute(
                sa.text("UPDATE employee_profiles SET blood_group_enc = :b, emergency_relationship_enc = :r WHERE user_id = :id"),
                {"b": _enc(blood, encrypt, decrypt), "r": _enc(rel, encrypt, decrypt), "id": uid},
            )
        last = rows[-1][0]
    left = conn.execute(
        sa.text(
            "SELECT count(*) FROM employee_profiles WHERE (blood_group IS NOT NULL AND blood_group_enc IS NULL) "
            "OR (emergency_relationship IS NOT NULL AND emergency_relationship_enc IS NULL)"
        )
    ).scalar_one()
    if left:
        raise RuntimeError(f"employee_profiles: {left} row(s) still have plaintext without ciphertext; aborting")


def _restore(conn) -> None:
    _, decrypt = _crypto()
    rows = conn.execute(
        sa.text("SELECT user_id, blood_group_enc, emergency_relationship_enc FROM employee_profiles")
    ).all()
    for uid, b, r in rows:
        conn.execute(
            sa.text("UPDATE employee_profiles SET blood_group = :b, emergency_relationship = :r WHERE user_id = :id"),
            {
                "b": decrypt(bytes(b)).decode("utf-8") if b is not None else None,
                "r": decrypt(bytes(r)).decode("utf-8") if r is not None else None,
                "id": uid,
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
