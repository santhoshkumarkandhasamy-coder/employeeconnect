"""Profile, photo, celebrations and /home."""

from __future__ import annotations

import datetime as dt
import io

import pytest
from PIL import Image
from sqlalchemy import select, text

from app.core.config import get_settings
from app.models import AuditLog, File, User
from app.services import celebrations
from tests.conftest import USERS, bearer


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(root))


@pytest.fixture
async def H(tokens_for):
    return {r: bearer(await tokens_for(r)) for r in USERS}


def jpeg(size=(1600, 1200)) -> bytes:
    out = io.BytesIO()
    im = Image.new("RGB", size, (9, 9, 9))
    ex = Image.Exif()
    ex[0x010F] = "SecretCamCo"
    im.save(out, "JPEG", exif=ex)
    return out.getvalue()


async def test_get_profile_shape(client, H):
    r = await client.get("/api/me/profile", headers=H["employee"])
    assert r.status_code == 200
    j = r.json()
    assert j["employee_id"] == "EMP001" and j["role"] == "employee" and j["department"]["name"] == "General"
    assert "location_consent" not in j and "home_location" not in j  # trip feature removed
    assert j["dob"] is None and j["emergency_contact"] is None
    assert j["photo_url"] is None and j["show_celebrations"] is False  # opt-in (review 07 M2)


async def test_put_partial_merge_and_clear(client, H):
    r = await client.put(
        "/api/me/profile",
        headers=H["employee"],
        json={
            "dob": "1995-04-12",
            "phone": "+91 99999-99999",
            "personal_email": "x@gmail.com",
            "blood_group": "O+",
            "address": "12 Main St",
            "emergency_contact": {"name": "Mum", "relationship": "Mother", "phone": "+919888888888"},
        },
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["phone"] == "+919999999999" and j["dob"] == "1995-04-12" and j["blood_group"] == "O+"
    assert j["emergency_contact"] == {"name": "Mum", "relationship": "Mother", "phone": "+919888888888"}
    # partial: only address changes, the rest is kept
    j = (await client.put("/api/me/profile", headers=H["employee"], json={"address": "New"})).json()
    assert j["address"] == "New" and j["phone"] == "+919999999999"
    # explicit null clears
    j = (await client.put("/api/me/profile", headers=H["employee"], json={"phone": None})).json()
    assert j["phone"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"full_name": "Hacker"},
        {"official_email": "x@y.com"},
        {"department": "IT"},
        {"role": "admin"},
        {"phone": "12345"},
        {"phone": "abc"},
        {"personal_email": "not-an-email"},
        {"dob": "2999-01-01"},
        {"dob": "2015-01-01"},  # too young
        {"dob": "1800-01-01"},  # too old
        {"wedding_anniversary": "2999-01-01"},
        {"blood_group": "Z+"},
        {"address": "x" * 301},
        {"home_location": {"lat": 12.9, "lng": 77.5}},  # removed field: unknown keys are rejected
        {"emergency_contact": {"name": "A"}},
        {"emergency_contact": {"phone": "+919999999999"}},
        {"show_celebrations": None},
    ],
)
async def test_validation_table(client, H, payload):
    r = await client.put("/api/me/profile", headers=H["employee"], json=payload)
    assert r.status_code == 422, (payload, r.text)


async def test_values_encrypted_at_rest_and_audit_has_no_values(client, H, db, seeded):
    await client.put("/api/me/profile", headers=H["employee"], json={"dob": "1990-02-03", "phone": "+919123456789"})
    raw = (
        await db.execute(
            text("SELECT dob_enc, phone_enc FROM employee_profiles WHERE user_id = :u"), {"u": seeded["users"]["employee"].hex}
        )
    ).one()
    assert b"1990" not in bytes(raw[0]) and b"9123456789" not in bytes(raw[1])
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "profile.update"))).scalars().all()
    assert rows and "1990" not in str(rows[0].after) and "9123456789" not in str(rows[0].after)
    assert sorted(rows[0].after["fields"]) == ["dob", "phone"]


async def test_users_only_touch_their_own_profile(client, H):
    await client.put("/api/me/profile", headers=H["employee"], json={"phone": "+919123456789"})
    assert (await client.get("/api/me/profile", headers=H["manager"])).json()["phone"] is None
    assert (await client.put("/api/me/profile", headers=H["manager"], json={"user_id": "x"})).status_code == 422
    assert (await client.get("/api/me/profile")).status_code == 401


async def test_photo_upload_resizes_strips_exif_and_replaces(client, H, db, seeded, storage):
    r = await client.post("/api/me/profile/photo", headers=H["employee"], files={"file": ("p.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    url1 = r.json()["photo_url"]
    assert url1.startswith("/api/files/") and r.json()["employee_id"] == "EMP001"
    img = await client.get(url1, headers=H["employee"])
    assert img.status_code == 200
    im = Image.open(io.BytesIO(img.content))
    assert max(im.size) <= 512 and not im.getexif()
    # any signed-in user can fetch it; it also shows in other users' summaries path
    assert (await client.get(url1, headers=H["manager"])).status_code == 200
    # replacing deletes the old file (row + blob)
    r2 = await client.post(
        "/api/me/profile/photo", headers=H["employee"], files={"file": ("p.jpg", jpeg((300, 300)), "image/jpeg")}
    )
    url2 = r2.json()["photo_url"]
    assert url2 != url1
    assert (await client.get(url1, headers=H["employee"])).status_code == 404
    assert len((await db.execute(select(File).where(File.purpose == "profile_photo"))).scalars().all()) == 1


async def test_photo_rejects_non_images(client, H):
    r = await client.post("/api/me/profile/photo", headers=H["employee"], files={"file": ("p.jpg", b"%PDF-1.4 x", "image/jpeg")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"
    r = await client.post(
        "/api/me/profile/photo",
        headers=H["employee"],
        files={"file": ("p.jpg", b"\xff\xd8\xff" + b"0" * (5 * 1024 * 1024), "image/jpeg")},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert (await client.post("/api/me/profile/photo", files={"file": ("p.jpg", jpeg(), "image/jpeg")})).status_code == 401


async def test_location_consent_endpoint_is_gone(client, H):
    r = await client.post("/api/me/location-consent", headers=H["employee"], json={"granted": True, "consent_version": "1"})
    assert r.status_code == 404


def test_occurrence_feb29_and_year_wrap():
    assert celebrations.occurrence(2, 29, dt.date(2025, 2, 1)) == dt.date(2025, 2, 28)
    assert celebrations.occurrence(2, 29, dt.date(2028, 2, 1)) == dt.date(2028, 2, 29)
    assert celebrations.occurrence(1, 5, dt.date(2025, 12, 30)) == dt.date(2026, 1, 5)
    assert celebrations.occurrence(6, 1, dt.date(2025, 6, 1)) == dt.date(2025, 6, 1)


async def _set(client, headers, **kw):
    assert (await client.put("/api/me/profile", headers=headers, json=kw)).status_code == 200


def _md(days: int, year: int) -> str:
    d = dt.date.today() + dt.timedelta(days=days)
    return dt.date(year, d.month, d.day if not (d.month == 2 and d.day == 29) else 28).isoformat()


async def test_celebrations_feed_privacy_and_optout(client, H):
    await _set(client, H["manager"], dob=_md(2, 1990), wedding_anniversary=_md(3, 2020), show_celebrations=True)
    await _set(client, H["hr"], dob=_md(20, 1985), show_celebrations=True)
    r = await client.get("/api/home/celebrations?days=7", headers=H["employee"])
    assert r.status_code == 200
    items = r.json()["items"]
    assert [(i["user"]["employee_id"], i["type"]) for i in items] == [("MGR001", "birthday"), ("MGR001", "anniversary")]
    b, a = items
    assert b["years"] is None and a["years"] == dt.date.fromisoformat(a["date"]).year - 2020
    assert "1990" not in str(items)  # never the birth year
    # wider window includes HR
    assert len((await client.get("/api/home/celebrations?days=30", headers=H["employee"])).json()["items"]) == 3
    # opt-out removes the user (cache invalidated on profile change)
    await _set(client, H["manager"], show_celebrations=False)
    items = (await client.get("/api/home/celebrations?days=7", headers=H["employee"])).json()["items"]
    assert items == []
    assert (await client.get("/api/home/celebrations?days=31", headers=H["employee"])).status_code == 422
    assert (await client.get("/api/home/celebrations?days=0", headers=H["employee"])).status_code == 422


async def test_celebrations_skip_inactive_and_unreadable(client, H, db, seeded, redis):
    await _set(client, H["manager"], dob=_md(1, 1990), show_celebrations=True)
    await _set(client, H["hr"], dob=_md(1, 1991), show_celebrations=True)
    u = await db.get(User, seeded["users"]["manager"])
    u.status = "inactive"
    await db.commit()
    items = (await client.get("/api/home/celebrations", headers=H["employee"])).json()["items"]
    assert [i["user"]["employee_id"] for i in items] == ["HR001"]
    # a value that cannot be decrypted is skipped, not a 500
    await db.execute(
        text("UPDATE employee_profiles SET dob_enc = :b WHERE user_id = :u"), {"b": b"garbage", "u": seeded["users"]["hr"].hex}
    )
    await db.commit()
    await celebrations.invalidate(redis)
    r = await client.get("/api/home/celebrations", headers=H["employee"])
    assert r.status_code == 200 and r.json()["items"] == []


async def test_home_payload_shape(client, H):
    r = await client.get("/api/home", headers=H["employee"])
    assert r.status_code == 200, r.text
    j = r.json()
    assert set(j) == {
        "today_attendance",
        "leave_balances",
        "permission_remaining",
        "unread_announcements",
        "pending_requests",
        "food_today",
        "celebrations",
    }
    assert j["today_attendance"] == {"check_in_at": None, "check_out_at": None, "status": None}
    assert isinstance(j["leave_balances"], list) and set(j["permission_remaining"]) == {"count", "minutes"}
    assert j["unread_announcements"] == 0 and j["pending_requests"] == 0
    assert set(j["food_today"]) == {"date", "need_food", "can_change"} and j["celebrations"] == []


async def test_home_counts_unread_and_survives_part_failure(client, H, monkeypatch):
    await client.post("/api/admin/announcements", headers=H["hr"], json={"title": "T", "body": "B", "target_all": True})
    assert (await client.get("/api/home", headers=H["employee"])).json()["unread_announcements"] == 1

    async def boom(*a, **k):
        raise RuntimeError("food module down")

    monkeypatch.setattr("app.services.food.today_summary", boom)
    r = await client.get("/api/home", headers=H["employee"])
    assert r.status_code == 200
    j = r.json()
    assert j["food_today"] is None and j["unread_announcements"] == 1 and j["leave_balances"] is not None
    assert (await client.get("/api/home")).status_code == 401


# ------------------------------------------------------------------ review 07 (M2, L5, L6)
async def test_celebrations_are_opt_in_by_default(client, H):
    await _set(client, H["manager"], dob=_md(2, 1990))  # a DOB alone publishes nothing
    assert (await client.get("/api/home/celebrations", headers=H["employee"])).json()["items"] == []
    await _set(client, H["manager"], show_celebrations=True)
    assert len((await client.get("/api/home/celebrations", headers=H["employee"])).json()["items"]) == 1


async def test_opt_out_holds_even_if_cache_invalidation_fails(client, H, monkeypatch):
    await _set(client, H["manager"], dob=_md(2, 1990), show_celebrations=True)
    assert len((await client.get("/api/home/celebrations", headers=H["employee"])).json()["items"]) == 1  # cached now

    async def broken(redis):
        return None

    monkeypatch.setattr(celebrations, "invalidate", broken)  # the Redis cleanup fails silently
    from app.services import profile as profile_svc

    monkeypatch.setattr(profile_svc.celebrations, "invalidate", broken)
    await _set(client, H["manager"], show_celebrations=False)
    assert (await client.get("/api/home/celebrations", headers=H["employee"])).json()["items"] == []


async def test_invalidate_bumps_generation(redis):
    await celebrations.invalidate(redis)
    await celebrations.invalidate(redis)
    assert await celebrations._generation(redis) == "2"


async def test_profile_photo_not_accepted_by_generic_upload(client, H):
    r = await client.post(
        "/api/files",
        headers=H["employee"],
        data={"purpose": "profile_photo"},
        files={"file": ("a.jpg", jpeg((20, 20)), "image/jpeg")},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_replaced_photo_is_unreadable_by_others_even_if_row_survives(client, H, db):
    r = await client.post("/api/me/profile/photo", headers=H["employee"], files={"file": ("a.jpg", jpeg((20, 20)), "image/jpeg")})
    url = r.json()["photo_url"]
    assert (await client.get(url, headers=H["hr"])).status_code == 200
    await db.execute(text("UPDATE employee_profiles SET photo_file_id = NULL"))  # no longer anybody's photo
    await db.commit()
    assert (await client.get(url, headers=H["hr"])).status_code == 404
    assert (await client.get(url, headers=H["employee"])).status_code == 200  # the owner still can
