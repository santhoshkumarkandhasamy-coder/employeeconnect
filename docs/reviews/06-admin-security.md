# 06 - Admin module: security review

Date: 2026-10-22. Read-only review by security-reviewer (saved here by the lead; the reviewer had no Write tool).
Scope: backend admin routers/services/schemas (employees, roles, masters, settings, audit viewer, profile apply_fields), contract section 9, frontend/lib/features/admin.

Totals: 0 Critical, 1 High, 7 Medium, 9 Low. No SQL injection, no IDOR for the seeded roles. Core privilege model is sound: permission containment plus Admin-only `role.assign`, immutable `admin` role, no self role change, last-admin guard with row locks, full session/token revocation on role change, deactivate, delete and reset, permissions read live from the DB.

## High

### H1. HR can reactivate/deactivate/unlock other HR and Manager accounts
`services/employees.py:102-106` (`_guard_target` without `elevated_needs_assign`), `:372-397` (`set_active`), `:400-407` (`unlock`); contract line ~602 ("need only rule (1)").
Scenario: Admin deactivates HR user B; colluding HR user A calls `POST /admin/employees/{B}/activate`; B signs in again with the old password with full HR rights. Reverse: rogue HR deactivates all Managers/HR.
Fix: call `_guard_target(actor, u, elevated_needs_assign=True)` in `set_active` (both directions) and `unlock`; consider forcing `must_change_password` on reactivation; update the contract.

## Medium

### M1. No step-up re-auth; admin self-reset; stolen admin token becomes permanent takeover
`employees.py:410-433` (no self check), `:236-291` (create with `initial_password`), `roles.py:152-170` (`set_permissions`).
Scenario: attacker with a 15-minute admin token calls `reset-password` on the admin's own account, signs in with the temp password, locks the admin out.
Fix: forbid self-reset on the admin endpoint (use `/auth/change-password`). Require recent re-authentication (current password supplied in the request body, verified server-side, rate limited) for: password reset of an elevated account; creating/promoting to a role holding `role.assign` or `role.manage`; `PUT /admin/roles/*/permissions`. Push-notify admins when an admin account is created, promoted or reset.

### M2. Custom roles: views are manager-scoped but actions are global
`employees.py:81-82` (`_sees_all`) only applied in list/get; `set_active`, `unlock`, `reset_password`, `delete_employee` (`:372-460`), `list_reset_requests` (`:463`), leave balances (`admin_masters.py:357-413`) never check scope.
Fix: one scope helper on every per-employee action, leave balances and reset requests; if not `_sees_all(actor)`, target must have `manager_id == actor.id`, else 404.

### M3. HR can change own leave balance and balances of Admin/peer HR
`admin_masters.py:377-413` (`set_balances`) has no `_guard_target` or self check.
Fix: reject `u.id == actor.id`; elevated targets require `role.assign`.

### M4. Personal fields written without `employee.view_sensitive`; employee never told
`employees.py:329-333` (`apply_fields` for any `employee.update`), `profile.py:117-149`.
Scenario: insider moves another employee's `home_location` near the office so trips auto-complete, or replaces the emergency phone.
Fix: require `employee.view_sensitive` when any personal key is in `model_fields_set`, else 403; push "Your personal details were changed by HR (fields: ...)" to the employee; consider making `home_location` and `emergency_contact` employee-only.

### M5. HR receives every employee's precise home coordinates (including Admins')
`profile.py:94-106` (`sensitive_out`), `employees.py:228-231`; seed `employee.view_sensitive` = {admin, hr} vs `trip.view_all` = {admin}.
Fix: drop `home_location` from `sensitive_out` unless the actor holds `trip.view_all`; apply containment: omit `profile` for targets whose role is not a subset of the actor's permissions.

### M6. Rate limits missing; reset limit fails open
`admin_employees.py:42-48,33-39`, `admin_roles.py` (all), `admin_masters.py:188-194` (apply-defaults), `:198-212` and `:471` (audit viewer count), `admin_employees.py:116` (`rate_limit.enforce` without `fail_closed`).
Fix: per-actor limits: detail-with-profile ~60/min and 500/day, create 30/min, role/permission writes 10/min, audit viewer 30/min, apply-defaults 2/min. `fail_closed=True` for reset-password and detail-with-profile. Alert admins (push) on more than N sensitive views per hour.

### M7. Temporary passwords never expire
`employees.py:258-277,413-416`, `services/auth.py`.
Fix: store `temp_password_expires_at` (72 h) on generate/reset; login refuses an expired temp password with `TEMP_PASSWORD_EXPIRED`.

## Low

- L1. `EmployeeCreate`/`EmployeeUpdate` ignore unknown fields (`schemas/employees.py:12-38`); `full_name` not `clean_text`ed; create `phone` skips `normalise_phone`. Fix: `extra="forbid"` (drop `show_celebrations` from update), clean name, validate phone.
- L2. Races give 500: `roles.py:173-182` `delete_role`; `employees.py:313-358` official_email race on update; `settings.py:141-142` first inserts; `roles.py:152-170` `set_permissions` has no row lock (lost updates). Fix: savepoint + IntegrityError -> 409, `FOR UPDATE` on the role, `INSERT ... ON CONFLICT` for settings.
- L3. Audit viewer reads are not audited; masking by key substring only; table append-only but no tamper evidence. Fix: write an `audit.view` row with the filters; document PII retention for before/after. (Hash chain / WORM export: deferred, out of scope for lightweight build.)
- L4. Deactivate/delete leave `device_tokens` and active trips behind. Fix: delete DeviceToken rows and close any active trip with `end_reason="account_deactivated"` (check the trips end_reason constraint/migration).
- L5. Managers see `last_login_at`, `is_locked`, `locked_until` of reports (`employees.py:29-47`). Fix: reduced shape when not `_sees_all`.
- L6. `blood_group` and `emergency_relationship` stored plaintext (`models/users.py:102-104`). Fix: `EncryptedString` + migration, move CHECK into the app.
- L7. Admin-chosen `initial_password` (`employees.py:250-258`). Fix: remove it, always generate.
- L8. Flutter: temp password copied to the clipboard and never cleared (`admin_widgets.dart:117-123`). Fix: mark clip sensitive where possible; clear after 60 s and on dialog close if unchanged.
- L9. Flutter: `settings_screen.dart`, `master_screen.dart`, `admin_home_screen.dart` not wrapped in `SecureScreen`; role dropdown offers roles HR cannot assign; permissions in auth state go stale after role edits. Fix: wrap, filter role options client-side, re-fetch `/me` when the admin home opens.

## Unverified (tests run on SQLite only)
Last-admin lock ordering / deadlocks; FK RESTRICT behind `IN_USE` and its race; audit append-only trigger/REVOKE for `app_user`; citext case-insensitive email uniqueness; JSONB settings comparison (`200` vs `200.0`); `ILIKE ... ESCAPE`; `set_permissions` concurrency; role delete cascade on Postgres. Needs a Postgres run (Docker was off).

## Fixes needed

Backend: H1, M1 (self-reset block + password re-confirmation + admin notifications), M2, M3, M4, M5, M6, M7, L1, L2, L3 (audit.view row), L4, L5, L6, L7.
Flutter: L8, L9; H1 UI (confirm dialog on Activate; hide Activate/Deactivate/Unlock for elevated targets without `role.assign`); M1 UI (password re-confirm prompt for protected actions, hide Reset password on own record); remove `initial_password` from the create form if present; handle `TEMP_PASSWORD_EXPIRED` on login.
