"""Key rotation (security review 02, M5): re-encrypt columns and file blobs, resumable, verified, clean errors."""

from __future__ import annotations

import datetime as dt

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.orm import undefer

from app.core import crypto
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import Attendance, EmployeeProfile, File, LeaveRequest, LeaveType, PermissionRequest, User
from app.services import files as files_svc
from app.services import key_rotation
from tests.conftest import USERS


@pytest.fixture
def keys(monkeypatch):
    """Switch DATA_ENCRYPTION_KEYS inside a test; always restore the cached keys afterwards."""
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()

    def use(value: str) -> None:
        monkeypatch.setenv("DATA_ENCRYPTION_KEYS", value)
        crypto.reset_crypto_cache()

    use(old)
    yield old, new, use
    monkeypatch.undo()
    crypto.reset_crypto_cache()


@pytest.fixture(autouse=True)
async def _all_encrypted_tables(engine):
    from app.models import Base

    names = ["food_enrollments"]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[Base.metadata.tables[n] for n in names if n in Base.metadata.tables])
        )


@pytest.fixture
def storage(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(root))
    return root


async def _populate(db, storage):
    uid = (await db.execute(select(User.id).where(User.employee_id == USERS["employee"]))).scalar_one()
    prof = (await db.execute(select(EmployeeProfile).where(EmployeeProfile.user_id == uid))).scalar_one()
    prof.phone = "+91 99999 00000"
    prof.address = "12 Secret Street"
    blob = files_svc.encrypt_bytes(b"selfie-bytes")
    (storage / "ab").mkdir()
    (storage / "ab" / "f1.bin").write_bytes(blob)
    db.add(
        File(
            owner_id=uid, purpose="selfie", storage_path="ab/f1.bin", content_type="image/jpeg", size_bytes=12, is_encrypted=True
        )
    )
    db.add(
        Attendance(
            user_id=uid,
            work_date=dt.date(2026, 10, 21),
            check_in_at=dt.datetime(2026, 10, 21, 4, 0, tzinfo=dt.UTC),
            check_in_loc={"lat": 1.5, "lng": 2.5},
        )
    )
    await db.commit()
    return uid


async def test_rotation_roundtrip_and_old_key_can_be_dropped(db, sessionmaker_, seeded, storage, keys):
    old, new, use = keys
    uid = await _populate(db, storage)

    use(f"{new},{old}")  # step 1: prepend the new key
    pre = await key_rotation.verify_all(db)
    assert pre["failed"] >= 3  # nothing is under the primary key yet
    dry = await key_rotation.rotate_all(db, dry_run=True)
    assert dry["rotated"] >= 4 and dry["undecryptable"] == 0
    assert (await key_rotation.verify_all(db))["failed"] == pre["failed"]  # dry run wrote nothing

    stats = await key_rotation.rotate_all(db)  # step 2: re-encrypt
    assert stats["rotated"] == dry["rotated"] and stats["undecryptable"] == 0 and stats["missing_blobs"] == 0
    assert (await key_rotation.verify_all(db))["failed"] == 0
    again = await key_rotation.rotate_all(db)  # resumable / idempotent
    assert again["rotated"] == 0 and again["already_current"] == stats["rotated"]

    use(new)  # step 3: old key dropped; everything still readable
    async with sessionmaker_() as s:
        prof = (await s.execute(select(EmployeeProfile).where(EmployeeProfile.user_id == uid))).scalar_one()
        assert prof.phone == "+91 99999 00000" and prof.address == "12 Secret Street"
        att = (await s.execute(select(Attendance).options(undefer(Attendance.check_in_loc)))).scalar_one()
        assert att.check_in_loc == {"lat": 1.5, "lng": 2.5}
    assert crypto.decrypt_bytes((storage / "ab" / "f1.bin").read_bytes()) == b"selfie-bytes"
    raw = (await db.execute(text("SELECT phone_enc FROM employee_profiles WHERE phone_enc IS NOT NULL"))).scalar_one()
    assert Fernet(new.encode()).decrypt(bytes(raw)) == b"+91 99999 00000"


async def test_rotation_reports_undecryptable_and_missing(db, seeded, storage, keys):
    old, new, use = keys
    await _populate(db, storage)
    (storage / "ab" / "f1.bin").unlink()
    use(f"{new},{Fernet.generate_key().decode()}")  # the real old key was "lost"
    stats = await key_rotation.rotate_all(db)
    assert stats["undecryptable"] >= 3 and stats["missing_blobs"] == 1 and stats["rotated"] == 0
    assert (await key_rotation.verify_all(db))["failed"] >= 4


async def test_leave_and_permission_text_rotates(db, sessionmaker_, seeded, storage, keys):
    """Review 03 M1: reason / decision_comment of both request kinds are covered by the rotation tooling."""
    old, new, use = keys
    uid = await _populate(db, storage)
    lt = LeaveType(code="CL", name="Casual", annual_quota=12)
    db.add(lt)
    await db.flush()
    lr = LeaveRequest(
        user_id=uid,
        leave_type_id=lt.id,
        from_date=dt.date(2026, 11, 2),
        to_date=dt.date(2026, 11, 2),
        days=1,
        reason="medical reason",
        decision_comment="get well",
        status="approved",
    )
    pr = PermissionRequest(
        user_id=uid, request_date=dt.date(2026, 11, 2), from_time=dt.time(10), to_time=dt.time(11), minutes=60, reason="scan"
    )
    db.add_all([lr, pr])
    await db.commit()
    use(f"{new},{old}")
    stats = await key_rotation.rotate_all(db)
    assert stats["undecryptable"] == 0
    assert (await key_rotation.verify_all(db))["failed"] == 0
    use(new)
    async with sessionmaker_() as s:
        got = (await s.execute(select(LeaveRequest))).scalar_one()
        assert (got.reason, got.decision_comment) == ("medical reason", "get well")
        assert (await s.execute(select(PermissionRequest))).scalar_one().reason == "scan"


def test_encrypted_columns_are_discovered_from_models():
    names = {(t.name, c.name) for t, c in key_rotation.encrypted_columns()}
    assert {
        ("attendance", "check_in_loc_enc"),
        ("attendance", "check_out_loc_enc"),
        ("employee_profiles", "phone_enc"),
        ("employee_profiles", "address_enc"),
        ("leave_requests", "reason_enc"),
        ("leave_requests", "decision_comment_enc"),
        ("permission_requests", "reason_enc"),
        ("permission_requests", "decision_comment_enc"),
    } <= names


def test_invalid_token_maps_to_domain_error():
    with pytest.raises(crypto.DataDecryptionError):
        crypto.decrypt_bytes(b"not-a-fernet-token")


async def test_decryption_error_handler_is_clean_503():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.core.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom():
        crypto.decrypt_bytes(b"garbage")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/boom")
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATA_UNREADABLE"
    assert "InvalidToken" not in r.text and "Traceback" not in r.text


async def test_undecryptable_column_surfaces_as_domain_error(db, seeded, keys):
    old, new, use = keys
    uid = (await db.execute(select(User.id).where(User.employee_id == USERS["employee"]))).scalar_one()
    prof = (await db.execute(select(EmployeeProfile).where(EmployeeProfile.user_id == uid))).scalar_one()
    prof.phone = "12345"
    await db.commit()
    use(Fernet.generate_key().decode())  # key mismatch
    db.expire_all()
    with pytest.raises(crypto.DataDecryptionError):
        await db.execute(select(EmployeeProfile.phone).where(EmployeeProfile.user_id == uid))
        (await db.execute(select(EmployeeProfile).where(EmployeeProfile.user_id == uid))).scalar_one()


async def test_cli_module_importable():
    import scripts.rotate_keys as cli

    assert callable(cli.main)
    with pytest.raises(AppError):
        files_svc._resolve("../escape")
