import datetime as dt

from sqlalchemy import select

from app.core import security
from app.models import AuditLog, RefreshToken

from .conftest import bearer, login_tokens


async def refresh(client, token):
    return await client.post("/api/auth/refresh", json={"refresh_token": token})


async def test_refresh_rotates_and_stores_only_hashes(client, db):
    t = await login_tokens(client, "EMP001")
    r = await refresh(client, t["refresh_token"])
    assert r.status_code == 200
    n = r.json()
    assert n["refresh_token"] != t["refresh_token"] and n["access_token"] and n["expires_in"] == 900
    assert n["must_change_password"] is False and "user" not in n
    rows = (await db.execute(select(RefreshToken).order_by(RefreshToken.issued_at))).scalars().all()
    assert len(rows) == 2 and rows[0].family_id == rows[1].family_id
    assert rows[0].used_at is not None and rows[0].replaced_by == rows[1].id
    assert {r.token_hash for r in rows} == {
        security.hash_refresh_token(t["refresh_token"]),
        security.hash_refresh_token(n["refresh_token"]),
    }
    assert all(t["refresh_token"] != r.token_hash for r in rows)
    assert (await client.get("/api/auth/me", headers=bearer(n))).status_code == 200


async def test_reuse_of_rotated_token_revokes_whole_family(client, db):
    t = await login_tokens(client, "EMP001")
    n = (await refresh(client, t["refresh_token"])).json()
    await _age_used_tokens(db)  # outside the lost-response grace window
    reuse = await refresh(client, t["refresh_token"])  # attacker replays the old token
    assert reuse.status_code == 401 and reuse.json()["error"]["code"] == "TOKEN_REUSED"
    legit = await refresh(client, n["refresh_token"])  # the descendant is dead too
    assert legit.status_code == 401 and legit.json()["error"]["code"] == "SESSION_REVOKED"  # family already revoked
    rows = (await db.execute(select(RefreshToken))).scalars().all()
    assert all(r.revoked_at is not None for r in rows)
    assert "auth.refresh_reuse" in [a for (a,) in (await db.execute(select(AuditLog.action))).all()]


async def _age_used_tokens(db, seconds=60):
    await db.rollback()
    for r in (await db.execute(select(RefreshToken).where(RefreshToken.used_at.is_not(None)))).scalars():
        r.used_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)
    await db.commit()


async def test_reuse_does_not_touch_other_devices_family(client, db):
    a = await login_tokens(client, "EMP001")
    b = await login_tokens(client, "EMP001")  # separate family
    await refresh(client, a["refresh_token"])
    await _age_used_tokens(db)
    await refresh(client, a["refresh_token"])  # reuse -> family A revoked
    assert (await refresh(client, b["refresh_token"])).status_code == 200


async def test_unknown_and_expired_refresh_tokens(client, db):
    assert (await refresh(client, "x" * 64)).json()["error"]["code"] == "UNAUTHORIZED"
    t = await login_tokens(client, "EMP001")
    row = (await db.execute(select(RefreshToken))).scalar_one()
    row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)
    row.issued_at = row.expires_at - dt.timedelta(days=1)
    await db.commit()
    r = await refresh(client, t["refresh_token"])
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_EXPIRED"


async def test_refresh_for_deactivated_user_forbidden_and_family_revoked(client, db):
    from app.models import User

    t = await login_tokens(client, "EMP001")
    u = (await db.execute(select(User).where(User.employee_id == "EMP001"))).unique().scalar_one()
    u.status = "inactive"
    await db.commit()
    r = await refresh(client, t["refresh_token"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCOUNT_INACTIVE"
    assert (await db.execute(select(RefreshToken))).scalar_one().revoked_at is not None


async def test_logout_revokes_only_that_family(client):
    a = await login_tokens(client, "EMP001")
    b = await login_tokens(client, "EMP001")
    r = await client.post("/api/auth/logout", headers=bearer(a), json={"refresh_token": a["refresh_token"]})
    assert r.status_code == 204
    assert (await refresh(client, a["refresh_token"])).status_code == 401
    assert (await refresh(client, b["refresh_token"])).status_code == 200


async def test_logout_cannot_revoke_someone_elses_token(client):
    victim = await login_tokens(client, "HR001")
    me = await login_tokens(client, "EMP001")
    await client.post("/api/auth/logout", headers=bearer(me), json={"refresh_token": victim["refresh_token"]})
    assert (await refresh(client, victim["refresh_token"])).status_code == 200


async def test_logout_all_revokes_every_device(client, db):
    a = await login_tokens(client, "EMP001")
    b = await login_tokens(client, "EMP001")
    other = await login_tokens(client, "HR001")
    assert (await client.post("/api/auth/logout-all", headers=bearer(a))).status_code == 204
    for t in (a, b):
        assert (await refresh(client, t["refresh_token"])).status_code == 401
    assert (await refresh(client, other["refresh_token"])).status_code == 200
    acts = [x for (x,) in (await db.execute(select(AuditLog.action))).all()]
    assert "auth.logout_all" in acts


async def test_logout_requires_authentication(client):
    assert (await client.post("/api/auth/logout", json={"refresh_token": "x" * 40})).status_code == 401
    assert (await client.post("/api/auth/logout-all")).status_code == 401
