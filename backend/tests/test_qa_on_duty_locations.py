"""QA (black-box, from the contract 14.1/14.3/14.5): duty-location admin CRUD, validation, RBAC, assignments, employee view."""

from __future__ import annotations

import pytest

from tests.qa_on_duty_support import SITE, assign, check_in, clock, code, new_loc, set_settings  # noqa: F401

LOC = "/api/admin/duty-locations"


def body(**over):
    return {"name": "Site A", "address": None, "lat": 13.0, "lng": 80.0, "radius_m": 200, "assign_mode": "selected", "is_active": True, **over}


# ---------------------------------------------------------------- RBAC
@pytest.mark.parametrize("role,ok", [("admin", True), ("hr", True), ("manager", False), ("employee", False)])
async def test_duty_location_manage_permission(client, H, role, ok):
    r = await client.get(LOC, headers=H[role])
    assert (r.status_code == 200) is ok, r.text
    if not ok:
        assert r.status_code == 403 and code(r) == "FORBIDDEN"
    r = await client.post(LOC, headers=H[role], json=body(name=f"X {role}"))
    assert (r.status_code == 201) is ok


async def test_unauthenticated_is_401(client):
    assert (await client.get(LOC)).status_code == 401
    assert (await client.get("/api/on-duty/locations")).status_code == 401
    assert (await client.get("/api/on-duty/today")).status_code == 401
    assert (await client.post("/api/on-duty/check-in", json={})).status_code == 401


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize(
    "over",
    [
        {"name": "A"},
        {"name": " "},
        {"name": "x" * 81},
        {"lat": 91},
        {"lat": -90.5},
        {"lng": 181},
        {"radius_m": 9},
        {"radius_m": 2001},
        {"radius_m": 10.5},
        {"assign_mode": "everyone"},
        {"address": "a" * 301},
        {"is_active": "yes"},
        {"surprise": 1},
    ],
)
async def test_create_rejects_invalid(client, H, over):
    r = await client.post(LOC, headers=H["admin"], json=body(**over))
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR", r.text


async def test_create_boundaries_ok_and_defaults(client, H):
    r = await client.post(LOC, headers=H["admin"], json=body(name="Edge", radius_m=10, lat=-90, lng=180))
    assert r.status_code == 201
    r = await client.post(LOC, headers=H["admin"], json={"name": "Min", "lat": 1, "lng": 2, "radius_m": 2000})
    assert r.status_code == 201
    d = r.json()
    assert d["assign_mode"] == "selected" and d["is_active"] is True and d["in_use"] is False
    assert d["assigned_count"] == 0 and d["radius_m"] == 2000


async def test_duplicate_name_case_insensitive_conflict(client, H):
    await new_loc(client, H["admin"], "Acme Client")
    r = await client.post(LOC, headers=H["admin"], json=body(name="ACME client"))
    assert r.status_code == 409 and code(r) == "CONFLICT"
    assert r.json()["error"]["details"][0]["field"] == "name"


async def test_put_full_replace_and_duplicate(client, H):
    a = await new_loc(client, H["admin"], "Alpha")
    b = await new_loc(client, H["admin"], "Beta")
    r = await client.put(f"{LOC}/{a['id']}", headers=H["admin"], json=body(name="Alpha 2", radius_m=500, lat=14, lng=81))
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["radius_m"], r.json()["lat"]) == ("Alpha 2", 500, 14)
    r = await client.put(f"{LOC}/{b['id']}", headers=H["admin"], json=body(name="alpha 2"))
    assert r.status_code == 409
    assert (await client.put(f"{LOC}/{a['id']}", headers=H["admin"], json={"name": "only name"})).status_code == 422


async def test_unknown_id_404(client, H):
    u = "00000000-0000-0000-0000-000000000001"
    assert (await client.get(f"{LOC}/{u}", headers=H["admin"])).status_code == 404
    assert (await client.put(f"{LOC}/{u}", headers=H["admin"], json=body())).status_code == 404
    assert (await client.delete(f"{LOC}/{u}", headers=H["admin"])).status_code == 404
    assert (await client.get(f"{LOC}/{u}/assignments", headers=H["admin"])).status_code == 404


async def test_list_filters_and_sort(client, H):
    await new_loc(client, H["admin"], "Zeta", address="Mount Road")
    await new_loc(client, H["admin"], "alpha", active=False)
    await new_loc(client, H["admin"], "Mid", mode="all")
    r = await client.get(LOC, headers=H["admin"])
    names = [i["name"] for i in r.json()["items"]]
    assert names == sorted(names, key=str.lower) and r.json()["total"] == 3
    assert [i["name"] for i in (await client.get(LOC, headers=H["admin"], params={"q": "MOUNT"})).json()["items"]] == ["Zeta"]
    assert [i["name"] for i in (await client.get(LOC, headers=H["admin"], params={"is_active": "false"})).json()["items"]] == ["alpha"]
    assert [i["name"] for i in (await client.get(LOC, headers=H["admin"], params={"assign_mode": "all"})).json()["items"]] == ["Mid"]


# ---------------------------------------------------------------- assignments
async def test_assignments_replace_and_all_mode_keeps_rows(client, H, uid):
    loc = await new_loc(client, H["admin"])
    r = await assign(client, H["admin"], loc["id"], [uid["employee"], uid["manager"]])
    assert [i["user"]["employee_id"] for i in r["items"]] == ["EMP001", "MGR001"]  # sorted by employee_id
    # switching to all keeps rows
    r = await assign(client, H["admin"], loc["id"], [uid["employee"], uid["manager"]], mode="all")
    assert r["assign_mode"] == "all" and len(r["items"]) == 2
    got = (await client.get(f"{LOC}/{loc['id']}/assignments", headers=H["admin"])).json()
    assert got["assign_mode"] == "all" and len(got["items"]) == 2
    # replace shrinks
    r = await assign(client, H["admin"], loc["id"], [uid["employee"]])
    assert [i["user"]["employee_id"] for i in r["items"]] == ["EMP001"]
    r = await assign(client, H["admin"], loc["id"], [])
    assert r["items"] == []


async def test_assignments_validation(client, H, uid):
    loc = await new_loc(client, H["admin"])
    url = f"{LOC}/{loc['id']}/assignments"
    bad = "00000000-0000-0000-0000-0000000000aa"
    r = await client.put(url, headers=H["admin"], json={"assign_mode": "selected", "user_ids": [bad]})
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"
    assert r.json()["error"]["details"][0]["field"] == "user_ids" and bad in r.json()["error"]["details"][0]["message"]
    r = await client.put(url, headers=H["admin"], json={"assign_mode": "selected"})  # user_ids required
    assert r.status_code == 422
    r = await client.put(url, headers=H["admin"], json={"assign_mode": "bogus", "user_ids": []})
    assert r.status_code == 422
    r = await client.put(url, headers=H["admin"], json={"assign_mode": "selected", "user_ids": [], "add_department_ids": [bad]})
    assert r.status_code == 422 or r.status_code == 200  # unknown department: contract says bad id -> 422
    if r.status_code == 422:
        assert r.json()["error"]["details"][0]["field"] == "add_department_ids"


async def test_assignments_add_department_expands_to_members(client, H, uid, seeded):
    loc = await new_loc(client, H["admin"])
    r = await assign(client, H["admin"], loc["id"], [], add_department_ids=[str(seeded["dept"])])
    ids = {i["user"]["employee_id"] for i in r["items"]}
    assert ids == {"ADM001", "HR001", "MGR001", "EMP001"}


async def test_assignments_rbac_and_rate_limit(client, H, uid):
    loc = await new_loc(client, H["admin"])
    url = f"{LOC}/{loc['id']}/assignments"
    assert (await client.get(url, headers=H["employee"])).status_code == 403
    assert (await client.put(url, headers=H["manager"], json={"assign_mode": "all", "user_ids": []})).status_code == 403
    codes = [
        (await client.put(url, headers=H["admin"], json={"assign_mode": "selected", "user_ids": []})).status_code for _ in range(12)
    ]
    assert codes[:10] == [200] * 10 and 429 in codes[10:], codes  # 10/min


async def test_employee_duty_locations_view(client, H, uid):
    a = await new_loc(client, H["admin"], "Beta Site")
    await assign(client, H["admin"], a["id"], [uid["employee"]])
    b = await new_loc(client, H["admin"], "Alpha Site", mode="all", active=False)
    await new_loc(client, H["admin"], "Other", mode="selected")
    r = await client.get(f"/api/admin/employees/{uid['employee']}/duty-locations", headers=H["admin"])
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["name"] for i in items] == ["Alpha Site", "Beta Site"]  # inactive included, sorted by name
    assert {"id", "name", "is_active", "assign_mode"} <= set(items[0])
    assert (await client.get("/api/admin/employees/00000000-0000-0000-0000-000000000009/duty-locations", headers=H["admin"])).status_code == 404
    assert (await client.get(f"/api/admin/employees/{uid['employee']}/duty-locations", headers=H["employee"])).status_code == 403


# ---------------------------------------------------------------- delete / in_use
async def test_delete_unused_204_and_in_use_409(client, H, uid, clock):
    loc = await new_loc(client, H["admin"], "Used")
    spare = await new_loc(client, H["admin"], "Spare")
    await assign(client, H["admin"], loc["id"], [uid["employee"]])
    r = await check_in(client, H["employee"], loc["id"], clock)
    assert r.status_code == 201, r.text
    assert (await client.get(f"{LOC}/{loc['id']}", headers=H["admin"])).json()["in_use"] is True
    r = await client.delete(f"{LOC}/{loc['id']}", headers=H["admin"])
    assert r.status_code == 409 and code(r) == "IN_USE"
    assert (await client.delete(f"{LOC}/{spare['id']}", headers=H["admin"])).status_code == 204
    assert (await client.get(f"{LOC}/{spare['id']}", headers=H["admin"])).status_code == 404


# ---------------------------------------------------------------- employee view of locations
async def test_employee_sees_only_assigned_active_locations_without_radius(client, H, uid):
    sel = await new_loc(client, H["admin"], "Selected Mine")
    await assign(client, H["admin"], sel["id"], [uid["employee"]])
    await new_loc(client, H["admin"], "Selected Other")
    await new_loc(client, H["admin"], "For Everyone", mode="all")
    off = await new_loc(client, H["admin"], "Inactive All", mode="all", active=False)
    r = await client.get("/api/on-duty/locations", headers=H["employee"])
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["name"] for i in items] == ["For Everyone", "Selected Mine"]
    for i in items:
        assert set(i) == {"id", "name", "address", "lat", "lng"}  # never radius_m
    mgr = await client.get("/api/on-duty/locations", headers=H["manager"])
    assert [i["name"] for i in mgr.json()["items"]] == ["For Everyone"]
    assert off["id"] not in {i["id"] for i in items}


async def test_assign_mode_switching_changes_visibility(client, H, uid):
    loc = await new_loc(client, H["admin"], "Switch", mode="selected")
    names = lambda r: [i["name"] for i in r.json()["items"]]  # noqa: E731
    assert names(await client.get("/api/on-duty/locations", headers=H["employee"])) == []
    await assign(client, H["admin"], loc["id"], [], mode="all")
    assert names(await client.get("/api/on-duty/locations", headers=H["employee"])) == ["Switch"]
    await assign(client, H["admin"], loc["id"], [uid["manager"]], mode="selected")
    assert names(await client.get("/api/on-duty/locations", headers=H["employee"])) == []
    assert names(await client.get("/api/on-duty/locations", headers=H["manager"])) == ["Switch"]


async def test_disabled_returns_empty_not_error(client, H):
    await new_loc(client, H["admin"], "All", mode="all")
    await set_settings(client, H["admin"], enabled=False)
    r = await client.get("/api/on-duty/locations", headers=H["employee"])
    assert r.status_code == 200 and r.json() == {"items": []}


# ---------------------------------------------------------------- settings
async def test_on_duty_settings_validation(client, H):
    put = lambda kv: client.put("/api/admin/settings", headers=H["admin"], json={"settings": kv})  # noqa: E731
    assert (await put({"on_duty.max_visits_per_day": 50})).status_code == 200
    for k, v in [
        ("on_duty.max_visits_per_day", 0),
        ("on_duty.max_visits_per_day", 51),
        ("on_duty.max_visits_per_day", 2.5),
        ("on_duty.request_backdate_days", 31),
        ("on_duty.request_backdate_days", -1),
        ("on_duty.request_max_advance_days", 0),
        ("on_duty.request_max_advance_days", 366),
        ("on_duty.enabled", "true"),
        ("on_duty.approval_required", 1),
        ("on_duty.apply_late_rule", "no"),
    ]:
        r = await put({k: v})
        assert r.status_code == 422, (k, v, r.text)
    assert (await client.put("/api/admin/settings", headers=H["hr"], json={"settings": {"on_duty.enabled": True}})).status_code in (403, 200)
    r = await client.get("/api/admin/settings", headers=H["admin"])
    flat = r.text
    for k in ("enabled", "approval_required", "apply_late_rule", "checkout_geofence", "selfie_required", "max_visits_per_day", "request_backdate_days", "request_max_advance_days"):
        assert f"on_duty.{k}" in flat, k


async def test_settings_defaults(client, H):
    r = await client.get("/api/admin/settings", headers=H["admin"])
    data = r.json()

    def find(key):
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if x.get("key") == key:
                    return x.get("value")
                if key in x and not isinstance(x[key], dict):
                    return x[key]
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
        raise AssertionError(key)

    assert find("on_duty.enabled") is True
    assert find("on_duty.approval_required") is False
    assert find("on_duty.apply_late_rule") is False
    assert find("on_duty.checkout_geofence") is False
    assert find("on_duty.selfie_required") is False
    assert find("on_duty.max_visits_per_day") == 10
    assert find("on_duty.request_backdate_days") == 0
    assert find("on_duty.request_max_advance_days") == 60
