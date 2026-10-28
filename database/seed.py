#!/usr/bin/env python
"""Idempotent seed: roles, permission catalogue + role mapping, default leave types, default
shift/department, default settings, first Admin user.

Usage (after `alembic upgrade head`, run as the owner role):
    DATABASE_URL=postgresql://app_owner:***@host/db \
    ADMIN_EMPLOYEE_ID=ADMIN001 ADMIN_PASSWORD='Temp#Passw0rd!' python database/seed.py

Env:
  DATABASE_URL         required (asyncpg / plain forms both accepted)
  ADMIN_EMPLOYEE_ID    required on first run (stored upper-case)
  ADMIN_PASSWORD       required on first run; must satisfy the password policy; hashed with Argon2id
  ADMIN_FULL_NAME      default "System Administrator"
  ADMIN_EMAIL          optional official email
  SEED_SKIP_ADMIN      1/true (or pass --no-admin): never create an admin here; use scripts.create_admin instead
  OFFICE_LAT / OFFICE_LNG / OFFICE_RADIUS_M / OFFICE_TIMEZONE   optional overrides for office.* settings

Re-running never overwrites existing rows, except that the `admin` role is always re-synced to the
full permission catalogue (its permissions are immutable). The admin user is created with
must_change_password = true and is never modified if it already exists.
Requires: sqlalchemy, psycopg, argon2-cffi.
"""
from __future__ import annotations

import json
import os
import re
import sys

from sqlalchemy import create_engine, text

# ---------------------------------------------------------------- catalogue (00-foundation.md section 6)
ADMIN, HR, MANAGER, EMPLOYEE = "admin", "hr", "manager", "employee"

# code -> (description, roles that hold it)
PERMISSIONS: dict[str, tuple[str, set[str]]] = {
    "employee.view": ("View employees (managers: direct reports only)", {ADMIN, HR, MANAGER}),
    "employee.view_sensitive": ("View sensitive employee fields", {ADMIN, HR}),
    "employee.create": ("Create employees", {ADMIN, HR}),
    "employee.update": ("Update employees", {ADMIN, HR}),
    "employee.delete": ("Soft-delete employees", {ADMIN}),
    "employee.deactivate": ("Deactivate / activate employees", {ADMIN, HR}),
    "employee.unlock": ("Unlock locked accounts", {ADMIN, HR}),
    "employee.reset_password": ("Reset employee passwords, handle reset requests", {ADMIN, HR}),
    "role.view": ("View roles and permission catalogue", {ADMIN, HR}),
    "role.manage": ("Create/edit roles and their permissions", {ADMIN}),
    "role.assign": ("Create/promote/edit/reset accounts holding any role above plain self-service", {ADMIN}),
    "department.manage": ("Manage departments", {ADMIN, HR}),
    "shift.manage": ("Manage shifts", {ADMIN, HR}),
    "holiday.manage": ("Manage holidays", {ADMIN, HR}),
    "leave_type.manage": ("Manage leave types and quotas", {ADMIN, HR}),
    "leave_balance.manage": ("Manage employee leave balances", {ADMIN, HR}),
    "settings.manage": ("Manage settings (food cut-off, geofence, limits)", {ADMIN}),
    "attendance.view_team": ("View attendance of direct reports", {ADMIN, HR, MANAGER}),
    "attendance.manual": ("Enter or correct attendance manually", {ADMIN, HR}),
    "task.assign_team": ("Assign tasks to direct reports", {ADMIN, HR, MANAGER}),
    "task.assign_all": ("Assign tasks to any employee", {ADMIN, HR}),
    "attendance.view_all": ("View attendance of everyone", {ADMIN, HR}),
    "late.view_team": ("View late records of direct reports", {ADMIN, HR, MANAGER}),
    "late.view_all": ("View late records of everyone", {ADMIN, HR}),
    "request.view_team": ("View leave/permission/on-duty requests of direct reports", {ADMIN, HR, MANAGER}),
    "request.approve_team": ("Approve/reject requests of direct reports", {ADMIN, HR, MANAGER}),
    "request.view_all": ("View all leave/permission/on-duty requests", {ADMIN, HR}),
    "request.approve_all": ("Approve/reject any request", {ADMIN, HR}),
    "client_location.manage": ("Manage client locations for on duty (geo coordinates)", {ADMIN, HR}),
    "food.view_all": ("View daily food enrollment lists and counts", {ADMIN, HR}),
    "announcement.manage": ("Create/edit/delete announcements", {ADMIN, HR}),
    "audit.view": ("View audit logs", {ADMIN}),
    "export.all": ("Export data for any scope", {ADMIN, HR}),
    "export.team": ("Export data for direct reports", {ADMIN, HR, MANAGER}),
    "export.sensitive": ("Include leave/permission reasons and decision comments in exports", {ADMIN, HR}),
    "company.manage": ("Manage company name and logo", {ADMIN}),
    "console.access": ("Sign in to the admin web console", {ADMIN}),  # decision: Admin only
    "session.manage": ("View and revoke login sessions and devices", {ADMIN, HR}),
    "system.view": ("View system health", {ADMIN}),
    "license.manage": ("View and install the license", {ADMIN}),
    "payroll.view_own": ("View and download own payslips", {ADMIN, HR, MANAGER, EMPLOYEE}),
    "payroll.view_all": ("View payroll (read-only): runs, structures, payslips", {ADMIN, HR}),
    "payroll.manage": ("Manage payroll: settings, structures, details, runs, drafts", {ADMIN, HR}),
    "payroll.publish": ("Publish payroll runs and payslips", {ADMIN, HR}),
}

ROLES = [
    (ADMIN, "Full access; permissions are immutable"),
    (HR, "Human resources"),
    (MANAGER, "Team manager (scope = direct reports)"),
    (EMPLOYEE, "Standard employee (self-service only)"),
]

LEAVE_TYPES = [  # code, name, quota, is_paid, allow_half_day
    ("CL", "Casual Leave", "12.0", True, True),
    ("SL", "Sick Leave", "12.0", True, True),
    ("EL", "Earned Leave", "15.0", True, True),
    ("LOP", "Loss of Pay", "0.0", False, True),  # unpaid, quota 0 => treated as unlimited (365/yr) by the app
]

DEFAULT_SETTINGS = {
    "office.lat": 12.9716,
    "office.lng": 77.5946,
    "office.radius_m": 150,
    "office.timezone": "Asia/Kolkata",
    "attendance.selfie_required": False,
    "attendance.checkout_geofence": False,
    "attendance.max_accuracy_m": 100,
    "attendance.half_day_minutes": 240,
    "attendance.max_fix_age_s": 60,
    "attendance.max_fix_future_s": 30,
    "attendance.reject_mock_location": True,
    "attendance.selfie_max_age_s": 600,
    "attendance.location_retention_days": 90,
    "attendance.selfie_retention_days": 90,
    "food.cutoff_mode": "previous_day",
    "food.cutoff_time": "18:00",
    "food.max_advance_days": 14,
    "permission.monthly_count_limit": 3,
    "permission.monthly_minutes_limit": 240,
    "permission.max_minutes_per_request": 120,
    "leave.backdate_days": 7,
    "leave.allow_cancel_approved": False,
    "leave.max_advance_days": 365,
    "leave.reason_retention_days": 0,
    "permission.past_grace_minutes": 5,
    "auth.inactivity_timeout_minutes": 5,
    "auth.offline_cache_max_hours": 72,
    "console.session_max_hours": 12,
    "console.inactivity_timeout_minutes": 15,
    # payroll (plan 12). PT: Tamil Nadu half-yearly (Sep + Mar payroll, slab on half-yearly gross).
    # VERIFY THE PT SLABS AGAINST CURRENT TAMIL NADU RULES before the first real run; they are editable settings.
    "payroll.pay_days_basis": "calendar",
    "payroll.lop_absent_days": True,
    "payroll.lop_half_day_attendance": True,
    "payroll.pf_employee_rate_pct": 12,
    "payroll.pf_employer_rate_pct": 12,
    "payroll.pf_wage_ceiling": 15000,
    "payroll.esi_wage_threshold": 20000,
    "payroll.esi_employee_rate_pct": 0.75,
    "payroll.esi_employer_rate_pct": 3.25,
    "payroll.pt_frequency": "half_yearly",
    "payroll.pt_slabs": [
        {"min_gross": "0.00", "max_gross": "21000.00", "amount": "0.00", "months": [3, 9]},
        {"min_gross": "21001.00", "max_gross": "30000.00", "amount": "135.00", "months": [3, 9]},
        {"min_gross": "30001.00", "max_gross": "45000.00", "amount": "315.00", "months": [3, 9]},
        {"min_gross": "45001.00", "max_gross": "60000.00", "amount": "690.00", "months": [3, 9]},
        {"min_gross": "60001.00", "max_gross": "75000.00", "amount": "1025.00", "months": [3, 9]},
        {"min_gross": "75001.00", "max_gross": None, "amount": "1250.00", "months": [3, 9]},
    ],
    "payroll.rounding": "rupee",
    "payroll.show_employer_contributions": False,
    "payroll.employer_share_from_employee": True,
    "payroll.mask_pan": True,
    "payroll.publish_requires_second_person": False,
    "payroll.company_address": "",
    "payroll.payslip_footer": "This is a computer-generated payslip and does not require a signature.",
}


def _sync_url(url: str) -> str:
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def password_problems(pw: str, employee_id: str, full_name: str) -> list[str]:
    bad = []
    if not 8 <= len(pw) <= 128:
        bad.append("length must be 8-128")
    if not re.search(r"[A-Z]", pw):
        bad.append("needs an upper-case letter")
    if not re.search(r"[a-z]", pw):
        bad.append("needs a lower-case letter")
    if not re.search(r"\d", pw):
        bad.append("needs a digit")
    if not re.search(r"[^A-Za-z0-9]", pw):
        bad.append("needs a symbol")
    low = pw.lower()
    if employee_id.lower() in low:
        bad.append("must not contain the employee id")
    for tok in re.split(r"\s+", full_name.lower()):
        if len(tok) >= 3 and tok in low:
            bad.append("must not contain a part of the name")
            break
    return bad


def seed(conn) -> None:
    # roles
    for name, desc in ROLES:
        conn.execute(text("INSERT INTO roles (name, description, is_system) VALUES (:n, :d, true) "
                          "ON CONFLICT (name) DO NOTHING"), {"n": name, "d": desc})

    # permission catalogue (codes new to this database are granted to non-admin roles below even when the role was customised)
    known = {r[0] for r in conn.execute(text("SELECT code FROM permissions"))}
    new_codes = [c for c in PERMISSIONS if c not in known]
    for code, (desc, _) in PERMISSIONS.items():
        conn.execute(text("INSERT INTO permissions (code, module, description) VALUES (:c, :m, :d) "
                          "ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description, module = EXCLUDED.module"),
                     {"c": code, "m": code.split(".")[0], "d": desc})

    # role -> permission mapping
    for role in (ADMIN, HR, MANAGER, EMPLOYEE):
        role_id = conn.execute(text("SELECT id FROM roles WHERE name = :n"), {"n": role}).scalar_one()
        if role == ADMIN:  # immutable: always the full catalogue
            conn.execute(text("INSERT INTO role_permissions (role_id, permission_id) "
                              "SELECT :r, id FROM permissions ON CONFLICT DO NOTHING"), {"r": role_id})
            continue
        existing = conn.execute(text("SELECT count(*) FROM role_permissions WHERE role_id = :r"), {"r": role_id}).scalar_one()
        if existing:  # admin may have customised this role; only grant permissions that are new to this database
            codes = [c for c in new_codes if role in PERMISSIONS[c][1]]
        else:
            codes = [c for c, (_, roles) in PERMISSIONS.items() if role in roles]
        if codes:
            conn.execute(text("INSERT INTO role_permissions (role_id, permission_id) "
                              "SELECT :r, id FROM permissions WHERE code = ANY(:codes) ON CONFLICT DO NOTHING"),
                         {"r": role_id, "codes": codes})

    # leave types
    for code, name, quota, paid, half in LEAVE_TYPES:
        conn.execute(text("INSERT INTO leave_types (code, name, annual_quota, is_paid, allow_half_day) "
                          "VALUES (:c, :n, :q, :p, :h) ON CONFLICT (code) DO NOTHING"),
                     {"c": code, "n": name, "q": quota, "p": paid, "h": half})

    # default department + shift
    conn.execute(text("INSERT INTO departments (name, code) VALUES ('General', 'GEN') ON CONFLICT (code) DO NOTHING"))
    conn.execute(text("INSERT INTO shifts (name, start_time, end_time, grace_minutes) "
                      "VALUES ('General Shift', '09:30', '18:30', 10) ON CONFLICT (name) DO NOTHING"))

    # settings (existing keys are kept)
    settings = dict(DEFAULT_SETTINGS)
    env = os.environ
    if env.get("OFFICE_LAT"):
        settings["office.lat"] = float(env["OFFICE_LAT"])
    if env.get("OFFICE_LNG"):
        settings["office.lng"] = float(env["OFFICE_LNG"])
    if env.get("OFFICE_RADIUS_M"):
        settings["office.radius_m"] = int(env["OFFICE_RADIUS_M"])
    if env.get("OFFICE_TIMEZONE"):
        settings["office.timezone"] = env["OFFICE_TIMEZONE"]
    for key, value in settings.items():
        conn.execute(text("INSERT INTO settings (key, value) VALUES (:k, CAST(:v AS jsonb)) ON CONFLICT (key) DO NOTHING"),
                     {"k": key, "v": json.dumps(value)})

    # company singleton (migration 0017 inserts it; keep a fresh/hand-built database consistent)
    conn.execute(text("INSERT INTO company (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))

    seed_admin(conn)


def seed_admin(conn) -> None:
    if "--no-admin" in sys.argv[1:] or (os.environ.get("SEED_SKIP_ADMIN") or "").strip().lower() in ("1", "true", "yes"):
        print("admin: skipped (--no-admin / SEED_SKIP_ADMIN); use `python -m scripts.create_admin create` for the first admin")
        return
    eid = (os.environ.get("ADMIN_EMPLOYEE_ID") or "").strip().upper()
    pw = os.environ.get("ADMIN_PASSWORD") or ""
    has_admin = conn.execute(text(
        "SELECT count(*) FROM users u JOIN roles r ON r.id = u.role_id WHERE r.name = 'admin' AND u.status = 'active'"
    )).scalar_one()
    if not eid or not pw:
        if has_admin:
            print("admin: already present, ADMIN_* not provided - skipped")
            return
        sys.exit("ADMIN_EMPLOYEE_ID and ADMIN_PASSWORD are required to create the first admin")
    if conn.execute(text("SELECT 1 FROM users WHERE employee_id = :e"), {"e": eid}).first():
        print(f"admin: user {eid} already exists - left unchanged")
        return
    full_name = os.environ.get("ADMIN_FULL_NAME", "System Administrator")
    problems = password_problems(pw, eid, full_name)
    if problems:
        sys.exit("ADMIN_PASSWORD rejected: " + "; ".join(problems))

    from argon2 import PasswordHasher  # lazy: only needed when creating the admin
    pwd_hash = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2).hash(pw)

    conn.execute(text("""
        INSERT INTO users (employee_id, password_hash, full_name, official_email, role_id, department_id, shift_id,
                           must_change_password, status, join_date)
        VALUES (:e, :h, :n, :m,
                (SELECT id FROM roles WHERE name = 'admin'),
                (SELECT id FROM departments WHERE code = 'GEN'),
                (SELECT id FROM shifts WHERE name = 'General Shift'),
                true, 'active', CURRENT_DATE)
    """), {"e": eid, "h": pwd_hash, "n": full_name, "m": (os.environ.get("ADMIN_EMAIL") or None)})
    conn.execute(text("INSERT INTO employee_profiles (user_id) SELECT id FROM users WHERE employee_id = :e"), {"e": eid})
    conn.execute(text("INSERT INTO audit_logs (actor_employee_id, action, entity_type, entity_id, outcome) "
                      "VALUES ('seed', 'employee.create', 'user', :e, 'success')"), {"e": eid})
    print(f"admin: created {eid} (must_change_password = true)")


def main() -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set")
    engine = create_engine(_sync_url(url))
    with engine.begin() as conn:
        seed(conn)
    print("seed complete")


if __name__ == "__main__":
    main()
