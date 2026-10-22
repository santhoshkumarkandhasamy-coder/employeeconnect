"""Contract sweep over every documented operation (OpenAPI): authentication and RBAC denial, error envelope shape."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import bearer

PUBLIC = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/refresh"),
    ("POST", "/api/auth/forgot-password"),
    ("POST", "/api/auth/web/login"),
    ("POST", "/api/auth/web/refresh"),
    ("POST", "/api/auth/web/logout"),
    ("GET", "/api/public/company"),
    ("GET", "/api/public/company/logo"),
}
# admin-prefixed reads that the contract opens to every authenticated user
OPEN_TO_ALL = {
    ("GET", "/api/admin/departments"),
    ("GET", "/api/admin/departments/{dept_id}"),
    ("GET", "/api/admin/shifts"),
    ("GET", "/api/admin/shifts/{shift_id}"),
    ("GET", "/api/admin/leave-types"),
    ("GET", "/api/admin/leave-types/{type_id}"),
    ("GET", "/api/admin/holidays"),
}


def operations(app) -> list[tuple[str, str]]:
    out = []
    for path, item in app.openapi()["paths"].items():
        for method in item:
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                out.append((method.upper(), path))
    return sorted(out)


def fill(path: str) -> str:
    import re

    def sub(m):
        name = m.group(1)
        return "leave" if name == "type" else ("tok" if name == "fcm_token" else str(uuid.uuid4()))

    return re.sub(r"\{([^}]+)\}", sub, path)


def assert_envelope(r) -> None:
    e = r.json()["error"]
    assert isinstance(e["code"], str) and isinstance(e["message"], str) and e["request_id"]


async def test_every_operation_requires_authentication(client, app):
    ops = operations(app)
    assert len(ops) >= 90  # guards against the sweep silently shrinking
    for method, path in ops:
        if (method, path) in PUBLIC:
            continue
        r = await client.request(method, fill(path), json={} if method != "GET" else None)
        assert r.status_code == 401, (method, path, r.status_code, r.text[:200])
        assert_envelope(r)
        assert r.json()["error"]["code"] in {"UNAUTHORIZED", "TOKEN_EXPIRED"}, (method, path)


async def test_employee_is_denied_on_every_admin_operation(client, app, tokens_for):
    h = bearer(await tokens_for("employee"))
    checked = 0
    for method, path in operations(app):
        if not path.startswith("/api/admin/") or (method, path) in OPEN_TO_ALL:
            continue
        if path.startswith("/api/admin/requests"):
            continue  # needs request.* perms; covered below with its own expectations
        r = await client.request(method, fill(path), headers=h, json={} if method != "GET" else None)
        assert r.status_code == 403, (method, path, r.status_code, r.text[:200])
        assert r.json()["error"]["code"] == "FORBIDDEN", (method, path)
        assert_envelope(r)
        checked += 1
    assert checked >= 35


async def test_employee_is_denied_request_admin_and_team_views(client, tokens_for):
    h = bearer(await tokens_for("employee"))
    rid = uuid.uuid4()
    for method, url in [
        ("GET", "/api/admin/requests"),
        ("PUT", f"/api/admin/requests/leave/{rid}/approve"),
        ("PUT", f"/api/admin/requests/permission/{rid}/reject"),
        ("GET", "/api/attendance/team"),
        ("GET", "/api/late/team"),
        ("GET", "/api/admin/export?type=all&from=2026-10-01&to=2026-10-02"),
    ]:
        r = await client.request(method, url, headers=h, json={} if method == "PUT" else None)
        assert r.status_code == 403, (method, url, r.status_code)


@pytest.mark.parametrize("role", ["manager", "hr"])
async def test_non_admin_roles_cannot_touch_admin_only_operations(client, tokens_for, role):
    """Role/permission/settings/audit are admin-only for manager; HR lacks role.manage."""
    h = bearer(await tokens_for(role))
    rid = uuid.uuid4()
    denied = [
        ("POST", "/api/admin/roles", {"name": "xx_role", "description": None}),
        ("PUT", f"/api/admin/roles/{rid}/permissions", {"permission_codes": [], "current_password": "x"}),
        ("DELETE", f"/api/admin/roles/{rid}", None),
        ("PUT", "/api/admin/settings", {"settings": {"food.cutoff_time": "17:00"}}),
    ]
    for method, url, body in denied:
        r = await client.request(method, url, headers=h, json=body)
        assert r.status_code == 403, (role, method, url, r.status_code, r.text[:200])


async def test_employee_cannot_write_holidays(client, tokens_for):
    h = bearer(await tokens_for("employee"))
    hid = uuid.uuid4()
    for method, url, body in [
        ("POST", "/api/holidays", {"date": "2026-12-25", "name": "Xmas"}),
        ("PATCH", f"/api/holidays/{hid}", {"name": "x"}),
        ("DELETE", f"/api/holidays/{hid}", None),
    ]:
        r = await client.request(method, url, headers=h, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN", (method, url)
    assert (await client.get("/api/holidays?year=2026", headers=h)).status_code == 200


async def test_unhandled_error_is_internal_error_envelope_without_leak(app):
    from httpx import ASGITransport, AsyncClient

    async def boom():
        raise RuntimeError("secret-internal-detail")

    app.add_api_route("/api/_boom", boom, methods=["GET"])
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t") as c:
        r = await c.get("/api/_boom", headers={"X-Request-ID": "abcdef123456"})
    assert r.status_code == 500
    e = r.json()["error"]
    assert e["code"] == "INTERNAL_ERROR" and "secret-internal-detail" not in r.text
    assert e["request_id"] == "abcdef123456"


async def test_request_id_header_is_validated(client):
    r = await client.get("/api/auth/me", headers={"X-Request-ID": "bad id!!"})
    assert r.status_code == 401
    rid = r.json()["error"]["request_id"]
    assert rid != "bad id!!" and r.headers["x-request-id"] == rid


async def test_otp_reset_route_is_removed(client):
    r = await client.post(
        "/api/auth/reset-password", json={"employee_id": "EMP001", "otp": "123456", "new_password": "N3w!Passw0rd#xx"}
    )
    assert r.status_code == 404  # password recovery is admin-reset only: the route no longer exists


async def test_validation_error_envelope_lists_fields(client, tokens_for):
    h = bearer(await tokens_for("employee"))
    r = await client.post("/api/leave/requests", headers=h, json={"from_date": "2026-13-01"})
    assert r.status_code == 422
    e = r.json()["error"]
    assert e["code"] == "VALIDATION_ERROR" and e["details"] and all({"field", "message"} <= d.keys() for d in e["details"])


# ---------------------------------------------------------------- rate limits named in the contract
async def _codes(client, method, url, h, n, body=None):
    return [(await client.request(method, url, headers=h, json=body)).status_code for _ in range(n)]


async def test_home_rate_limit_60_per_minute(client, tokens_for):
    h = bearer(await tokens_for("employee"))
    codes = await _codes(client, "GET", "/api/home", h, 61)
    assert codes[:60] == [200] * 60 and codes[60] == 429
    r = await client.get("/api/home", headers=h)
    assert r.json()["error"]["code"] == "RATE_LIMITED" and int(r.headers["retry-after"]) > 0
    # per user: somebody else is unaffected
    assert (await client.get("/api/home", headers=bearer(await tokens_for("manager")))).status_code == 200


async def test_food_get_and_bulk_rate_limits(client, tokens_for):
    h = bearer(await tokens_for("employee"))
    codes = await _codes(client, "GET", "/api/food/my-enrollments", h, 61)
    assert codes[:60] == [200] * 60 and codes[60] == 429
    h2 = bearer(await tokens_for("manager"))
    codes = await _codes(client, "PUT", "/api/food/enrollments", h2, 11, {"items": [{"date": "2026-10-01", "need_food": True}]})
    assert 429 not in codes[:10] and codes[10] == 429
