# 09 - Admin Module

Contract: api-contract section 9 (+ approvals in section 4). Tables: all master tables already exist; no new tables. Admin-lite employee APIs were delivered in 01; leave types/balances in 03; announcements in 06; this plan completes and consolidates the in-app Admin area.

## Scope
Employees (add/modify/remove/deactivate/unlock/reset password), roles and permissions, departments, shifts, holidays, leave types and quotas, permission limits and food cut-off (settings), approvals, announcements, reports/export, audit logs.

## Rules
- Permission gating per endpoint (see foundation matrix); Admin screens render by `permissions` list; server is the authority.
- Employees: create requires unique employee_id/official_email, department, shift, role, optional manager (must be active and not self, no manager cycles). Temp password shown once with copy/share. Modify allowed fields: name, email, phone, department, shift, role, manager, join_date. Role change revokes cached permissions and optionally all sessions. "Remove" = soft delete (deleted_at, status inactive, tokens revoked, history kept, employee_id never reusable); hard delete not offered. Guardrails: `LAST_ADMIN`, no self-delete/deactivate, HR cannot modify Admin users or assign Admin role (only `role.manage` holders can).
- Roles: list/create/edit custom roles, assign permission codes from catalogue; system roles undeletable, Admin immutable; delete role blocked while users assigned (`IN_USE`). Permission changes audited with before/after code lists.
- Masters (departments, shifts, holidays, leave types): create/update/delete with `IN_USE` protection, prefer deactivate. Shift change applies to future attendance; holidays year filter; duplicate holiday date -> 409.
- Leave quotas: per-employee balance adjust (`/admin/employees/{id}/leave-balances`) and bulk "apply default quota to all" action (idempotent, new year).
- Settings: whitelist of keys with types/ranges validated (cutoff HH:MM, radius 20-5000, limits >= 0...), every change audited; client config cached max 5 min.
- Approvals: unified queue for leave + permission with scope (manager team / HR+Admin all), filter by status/type/department/employee, approve/reject sheet with comment (reject requires comment), bulk approve optional (O1).
- Audit logs: filterable viewer (actor, action, entity, date range), read-only, export not in v1; no secret values.
- Password reset requests inbox (from forgot-password) with "Reset password" action.

## Edge cases
Editing own role/permissions (blocked for Admin self-demotion); two admins editing same record (use `updated_at` check -> 409 `CONFLICT`, O2); deactivating a manager with reports (reports' `manager_id` retained but approvals fall to HR/Admin; warn in UI); deleting department with users (`IN_USE`); setting change mid-day; big lists (server-side search/pagination); import employees via CSV (out of v1, O3).

## Flutter (features/admin)
Admin home (grid of tiles by permission): Employees (list with search/filters, detail, create/edit form, actions menu: deactivate, unlock, reset password, delete), Roles & Rights (role list, permission checklist grouped by module), Approvals (tabs Pending/History, detail), Masters (Departments, Shifts, Holidays calendar, Leave types, Settings forms incl. food cut-off and permission limits), Announcements (from 06), Reports & Export (from 10), Audit Logs (filters + detail JSON view). Confirmation dialogs for destructive actions; temporary password dialog with FLAG_SECURE.

## Security
Every mutation audited with before/after (masked); privilege escalation tests (HR assigning Admin role, editing Admin user); IDOR on employee ids; manager scope on employee list; rate limit on reset-password; temp passwords not logged.

## Tasks
1. database-engineer: verify constraints/indexes for admin queries (users search by employee_id/name trigram optional), audit indexes.
2. backend-developer: complete employees CRUD with guardrails, roles + permissions endpoints, master CRUDs (departments, shifts, holidays, leave types), settings service with schema validation, audit-logs endpoint, password-reset-requests list, permission cache invalidation.
3. flutter-developer: admin screens above (shared form/list widgets), permission-based navigation.
4. security-reviewer: privilege escalation, audit completeness (every mutation has a log), masking, guardrails.
5. qa-tester: RBAC matrix across all admin endpoints (parametrised role x endpoint), guardrail tests (last admin, self-delete), IN_USE protections, audit rows created, settings validation, soft-delete behaviour.

Open: O1 bulk approve; O2 optimistic concurrency on master edits; O3 CSV import of employees (proposed v2).
