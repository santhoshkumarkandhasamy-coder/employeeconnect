#!/usr/bin/env python
"""DEV ONLY. Creates demo data through the public admin API of a LOCAL dev stack (docker-compose.dev.yml).

    DEV_ADMIN_INITIAL_PW=<pw given to create_admin> DEV_ADMIN_PW=<new admin pw> DEV_DEMO_PW=<pw for demo users> \
        python deploy/scripts/dev_demo.py [http://127.0.0.1:8000]

Idempotent enough for a fresh DB: changes the forced admin password, creates one shift, a manager (MGR001) with two direct
reports (EMP001, EMP002), gives the demo users a known password (so no forced-change screen) and sets leave balances.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/") + "/api"


def call(method, path, body=None, token=None, ok=(200, 201, 204)):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> {e.code} {e.read().decode()[:400]}")


def items(x):
    return x if isinstance(x, list) else x.get("items", x.get("data", []))


def login(eid, pw):
    return call("POST", "/auth/login", {"employee_id": eid, "password": pw})


def main():
    init, final, demo = (os.environ[k] for k in ("DEV_ADMIN_INITIAL_PW", "DEV_ADMIN_PW", "DEV_DEMO_PW"))
    r = login("ADMIN001", init if os.environ.get("DEV_ADMIN_INITIAL_PW") else final)
    if r["must_change_password"]:
        r = call("POST", "/auth/change-password", {"current_password": init, "new_password": final}, r["access_token"])
    tok = r["access_token"]

    roles = {x["name"]: x["id"] for x in items(call("GET", "/admin/roles", token=tok))}
    print("roles:", list(roles))
    shift = call("POST", "/admin/shifts", {"name": "Dev General 09:30-18:30", "start_time": "09:30:00", "end_time": "18:30:00",
                                           "grace_minutes": 10, "working_days": [1, 2, 3, 4, 5, 6]}, tok)
    sid = shift["id"]
    mgr_role = roles.get("manager") or next(v for k, v in roles.items() if "manag" in k.lower())
    emp_role = roles.get("employee") or next(v for k, v in roles.items() if "employ" in k.lower())

    def create(eid, name, role, manager=None):
        body = {"employee_id": eid, "full_name": name, "role_id": role, "shift_id": sid, "join_date": "2025-01-06"}
        if manager:
            body["manager_id"] = manager
        res = call("POST", "/admin/employees", body, tok)
        u = login(eid, res["temporary_password"])
        call("POST", "/auth/change-password", {"current_password": res["temporary_password"], "new_password": demo}, u["access_token"])
        print("created", eid, name)
        return res["employee"]["id"]

    mgr = create("MGR001", "Meera Manager", mgr_role)
    ids = [mgr, create("EMP001", "Ravi Kumar", emp_role, mgr), create("EMP002", "Anita Sharma", emp_role, mgr)]

    types = items(call("GET", "/admin/leave-types", token=tok))
    year = int(os.environ.get("DEV_YEAR", "2026"))
    quotas = {"casual": 12, "sick": 8}
    for uid in ids:
        its = [{"leave_type_id": t["id"], "quota": quotas.get(t.get("code", "").lower(), quotas.get(t["name"].lower().split()[0], 10))}
               for t in types if t.get("is_active", True)]
        call("PUT", f"/admin/employees/{uid}/leave-balances", {"year": year, "items": its}, tok)
    print("leave balances set for", year, [t.get("code") or t["name"] for t in types])


main()
