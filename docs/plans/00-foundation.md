# 00 - Foundation Plan

Applies to the whole project. Feature plans 01-10 reference this file. API shapes live in `docs/api-contract.md`.
Feature 08 (Safe Reach Home trips / SOS / live location) was removed from the product; its plan is deleted and the number is not reused.

## 1. Monorepo layout

```
employeeconnect/
  frontend/            Flutter app
  backend/             FastAPI app (+ pytest)
  database/            alembic.ini, alembic/versions, schema.sql, seed/, scripts/backup.sh, restore.sh
  deploy/              docker-compose.yml, nginx/nginx.conf, .env.example, backup cron, certs/ (gitignored)
  docs/                api-contract.md, plans/
  .claude/agents/  README.md  BUILD_PROMPT.md
```
Alembic lives in `database/`; `env.py` imports `app.models` from `backend/` (api image copies both; `PYTHONPATH=/app/backend`).
`schema.sql` is generated from migrations (`pg_dump --schema-only`) after each feature; migrations are authoritative.

## 2. Backend package structure (`backend/`)

```
app/
  main.py                 app factory, routers, middleware, exception handlers
  core/  config.py (pydantic-settings) security.py (argon2, jwt) rbac.py (require_permission, scope helpers)
         crypto.py (EncryptedString type) db.py (async engine/session) redis.py errors.py (AppError + handlers)
         audit.py logging.py (JSON, request_id, redaction) pagination.py time.py (office tz helpers) files.py
  models/                 one module per table group (SQLAlchemy 2.0, declarative)
  schemas/                Pydantic v2 request/response per module
  routers/                auth, me, home, files, attendance, leave, permission, late, food, announcements,
                          admin_employees, admin_masters, admin_roles, admin_settings, admin_requests,
                          admin_food, admin_announcements, admin_audit, export
  services/               business logic, one per module (routers stay thin, no SQL in routers)
  jobs/                   scheduler.py + attendance_autoclose, retention_purge, token_cleanup, balance_rollover
  notifications/          fcm.py (service-account key from file, push only)
  scripts/                create_admin.py, seed.py
tests/                    conftest (test DB + fakeredis), test_auth, test_rbac, test_leave, test_food, test_export, ...
```
Async SQLAlchemy 2.0 + asyncpg; routers call services; services take `AsyncSession` and the acting `Principal`.
Background jobs run in a separate `worker` container (same image, command `python -m app.jobs.scheduler`, APScheduler + Redis lock) - devops adds it to compose.

## 3. Configuration / env vars (`deploy/.env.example`)

| Var | Purpose |
|---|---|
| `APP_ENV` (dev/prod), `LOG_LEVEL`, `CORS_ORIGINS` (empty in prod) | general |
| `DATABASE_URL`, `POSTGRES_USER/PASSWORD/DB` | Postgres |
| `REDIS_URL`, `REDIS_PASSWORD` | Redis |
| `JWT_SECRET` (>=32 bytes), `JWT_ALGORITHM=HS256`, `ACCESS_TOKEN_MINUTES=15`, `REFRESH_TOKEN_DAYS=30` | tokens |
| `DATA_ENCRYPTION_KEYS` | comma list of Fernet keys, newest first (MultiFernet; enables rotation) |
| `AUDIT_HMAC_KEY` | optional audit-row chain HMAC |
| `FILE_STORAGE_DIR=/data/files`, `MAX_UPLOAD_MB=10` | local disk storage (volume) |
| `FCM_CREDENTIALS_FILE` | service account json path (mounted secret) |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM` (optional) | OTP / reset mails |
| `LOGIN_RATE_IP`, `LOGIN_RATE_EID`, `LOCKOUT_THRESHOLD=5`, `LOCKOUT_MINUTES=15` | auth tuning |
| `BACKUP_GPG_RECIPIENT` or `BACKUP_PASSPHRASE_FILE`, `BACKUP_RETENTION_DAYS` | backups |
| `SERVER_NAME`, `TLS_CERT_PATH`, `TLS_KEY_PATH` | nginx |
Runtime-editable business values (food cut-off, geofence, limits...) live in the `settings` table, not env. Secrets are never committed; prod refuses to start with default/short secrets.

## 4. Database design

Conventions: PK `id uuid default gen_random_uuid()` (except noted); `created_at/updated_at timestamptz default now()`;
enums as Postgres `CHECK`-constrained `text` (easier to migrate than native enums); FK `ON DELETE RESTRICT` unless noted;
all lookups indexed on FK columns. Extensions: `pgcrypto`, `btree_gist`, `citext`. Columns suffixed `_enc` are `bytea` holding Fernet tokens (see section 7).
Tables beyond README list: `files`, `announcement_departments`, `password_reset_requests`.

| Table | Columns (type) and constraints |
|---|---|
| **roles** | id; `name` text UNIQUE NOT NULL; description; `is_system` bool default false |
| **permissions** | id; `code` text UNIQUE NOT NULL; `module` text; description |
| **role_permissions** | `role_id` FK, `permission_id` FK; PK (role_id, permission_id); ON DELETE CASCADE |
| **departments** | id; `name` citext UNIQUE; `code` text UNIQUE; `is_active` bool default true |
| **shifts** | id; `name` UNIQUE; `start_time` time; `end_time` time; `grace_minutes` int default 10 CHECK 0-120; `working_days` smallint[] default {1,2,3,4,5} (ISO 1=Mon); `is_active`; CHECK start<>end |
| **users** | id; `employee_id` citext UNIQUE NOT NULL (never reused); `password_hash` text NOT NULL; `full_name` text; `official_email` citext UNIQUE; `role_id` FK NOT NULL; `department_id` FK; `shift_id` FK; `manager_id` FK users (CHECK manager_id<>id); `status` CHECK in (active,inactive) default active; `must_change_password` bool default true; `failed_login_count` int default 0; `locked_until` timestamptz; `last_failed_login_at`; `last_login_at`; `password_changed_at`; `join_date` date; `deleted_at` timestamptz (soft delete); `created_by` FK |
| **employee_profiles** | `user_id` PK FK users CASCADE; `photo_file_id` FK files; `dob_enc`; `wedding_anniversary_enc`; `personal_email_enc`; `phone_enc`; `emergency_name_enc`; `emergency_relationship` text; `emergency_phone_enc`; `blood_group` CHECK in 8 values; `address_enc`; `updated_at` |
| **files** | id; `owner_id` FK users; `purpose` text; `storage_path` text; `content_type`; `size_bytes` int; `sha256` text; `created_at` |
| **attendance** | id; `user_id` FK; `work_date` date; `shift_id` FK (snapshot); `check_in_at` timestamptz NOT NULL; `check_in_loc_enc`; `check_in_distance_m` int; `check_in_within_fence` bool; `check_in_selfie_file_id` FK; `check_out_at`; `check_out_loc_enc`; `check_out_selfie_file_id`; `status` CHECK in (present,late,half_day); `minutes_late` int default 0; `worked_minutes` int; `auto_closed` bool default false; UNIQUE (user_id, work_date); CHECK check_out_at > check_in_at; INDEX (work_date) |
| **late_records** | id; `user_id` FK; `attendance_id` FK UNIQUE; `work_date`; `shift_start` time; `grace_minutes` int; `check_in_at`; `minutes_late` int CHECK >0; UNIQUE (user_id, work_date); INDEX (work_date) |
| **leave_types** | id; `code` UNIQUE; `name`; `annual_quota` numeric(4,1) CHECK >=0; `is_paid` bool; `allow_half_day` bool; `requires_attachment_after_days` int; `max_consecutive_days` int; `is_active` |
| **leave_balances** | id; `user_id`; `leave_type_id`; `year` int; `quota` numeric(4,1); `used` numeric(4,1) default 0; `pending` numeric(4,1) default 0; UNIQUE (user_id, leave_type_id, year); CHECK used>=0 AND pending>=0 AND used+pending<=quota (balance never negative, enforced in DB) |
| **leave_requests** | id; `user_id`; `leave_type_id`; `from_date`; `to_date`; `half_day` bool; `half_day_session` CHECK in (first,second); `days` numeric(4,1) CHECK >0; `reason` text NOT NULL; `attachment_file_id` FK; `status` CHECK in (pending,approved,rejected,cancelled); `approver_id` FK; `decision_comment`; `decided_at`; `cancelled_at`; CHECK to_date>=from_date; CHECK (NOT half_day OR from_date=to_date); EXCLUDE USING gist (user_id WITH =, daterange(from_date,to_date,'[]') WITH &&) WHERE status IN ('pending','approved'); INDEX (status), (user_id, from_date) |
| **permission_requests** | id; `user_id`; `request_date` date; `from_time` time; `to_time` time; `minutes` int; `reason`; `status` (same enum); `approver_id`; `decision_comment`; `decided_at`; `cancelled_at`; CHECK to_time>from_time; EXCLUDE USING gist (user_id WITH =, tsrange(request_date+from_time, request_date+to_time) WITH &&) WHERE status IN ('pending','approved') |
| **holidays** | id; `date` UNIQUE; `name`; `is_active` |
| **food_enrollments** | id; `user_id`; `date`; `need_food` bool NOT NULL; `updated_by` FK; `updated_at`; UNIQUE (user_id, date); INDEX (date) |
| **announcements** | id; `title` text(150); `body` text; `priority` CHECK in (normal,important,urgent); `image_file_id`; `pdf_file_id`; `target_all` bool; `published_at`; `expires_at`; `created_by` FK; `deleted_at` |
| **announcement_departments** | `announcement_id` FK CASCADE; `department_id` FK; PK both |
| **announcement_reads** | `announcement_id` FK CASCADE; `user_id` FK; `read_at`; PK (announcement_id, user_id) |
| **refresh_tokens** | id; `user_id` CASCADE; `family_id` uuid; `token_hash` char(64) UNIQUE (sha256); `device_id`; `device_name`; `ip`; `user_agent`; `issued_at`; `expires_at`; `used_at` (set on rotation); `revoked_at`; `replaced_by` FK; INDEX (user_id), (family_id) |
| **device_tokens** | id; `user_id` CASCADE; `fcm_token` text UNIQUE; `platform` CHECK in (android,ios); `device_id`; `last_seen_at` |
| **password_reset_requests** | id; `user_id`; `status` CHECK in (open,handled,expired); `otp_hash`; `otp_expires_at`; `otp_attempts` int; `requested_at`; `handled_by` FK; `handled_at` |
| **audit_logs** | `id` bigserial; `ts` timestamptz default now(); `actor_id` FK null; `actor_employee_id` text; `action` text; `entity_type`; `entity_id` text; `before` jsonb; `after` jsonb; `ip` inet; `user_agent`; `request_id`; `outcome` CHECK in (success,failure); `row_hmac` text null; INDEX (ts), (actor_id), (entity_type, entity_id), (action) |
| **settings** | `key` text PK; `value` jsonb NOT NULL; `updated_by` FK; `updated_at` |

Default settings keys (seeded): `office.lat/lng/radius_m/timezone`, `attendance.selfie_required=false`, `attendance.checkout_geofence=false`,
`attendance.max_accuracy_m=100`, `food.cutoff_mode=previous_day`, `food.cutoff_time=18:00`, `food.max_advance_days=14`,
`permission.monthly_count_limit=3`, `permission.monthly_minutes_limit=240`, `permission.max_minutes_per_request=120`,
`leave.backdate_days=7`, `auth.inactivity_timeout_minutes=5`.
Seed: roles (admin,hr,manager,employee, is_system), permission catalogue + role mapping, leave types (CL 12, SL 12, EL 15 - editable), one default shift, one department "General", first admin via `create_admin.py` (prompts password; must_change_password=true).

## 5. Integrity rules the DB enforces (not just app)
Leave balance non-negative (CHECK), no overlapping pending/approved leave or permission (EXCLUDE), one attendance row and one food row per user/day (UNIQUE), audit logs append-only (app DB role has INSERT/SELECT only on `audit_logs`; trigger blocks UPDATE/DELETE). App connects as a non-owner role `app_user`; migrations run as `app_owner`.

## 6. RBAC permission catalogue

Self-service endpoints (own profile, attendance, requests, food, announcements read) need authentication only.
`_team` = direct reports (`manager_id = caller`); `_all` = everyone. A user holds the union of their single role's permissions. Checked server-side via `require_permission("code")` dependency; team scope applied in the service layer.

| Code | Admin | HR | Manager | Employee |
|---|:-:|:-:|:-:|:-:|
| employee.view (manager = team only) | Y | Y | Y | - |
| employee.view_sensitive | Y | Y | - | - |
| employee.create / employee.update | Y | Y | - | - |
| employee.delete | Y | - | - | - |
| employee.deactivate | Y | Y | - | - |
| employee.unlock / employee.reset_password | Y | Y | - | - |
| role.view | Y | Y | - | - |
| role.manage | Y | - | - | - |
| department.manage / shift.manage / holiday.manage | Y | Y | - | - |
| leave_type.manage / leave_balance.manage | Y | Y | - | - |
| settings.manage (food cut-off, geofence, limits) | Y | - | - | - |
| attendance.view_team | Y | Y | Y | - |
| attendance.view_all | Y | Y | - | - |
| late.view_team | Y | Y | Y | - |
| late.view_all | Y | Y | - | - |
| request.view_team / request.approve_team | Y | Y | Y | - |
| request.view_all / request.approve_all | Y | Y | - | - |
| food.view_all | Y | Y | - | - |
| announcement.manage | Y | Y | - | - |
| audit.view | Y | - | - | - |
| export.all | Y | Y | - | - |
| export.team | Y | Y | Y | - |
Rules: no self-approval of own request (even Admin; falls to another approver); `_all` implies `_team`; Admin role's permissions are immutable and always the full catalogue; the permission set is cached in Redis (60 s, invalidated on role change). Admin can create custom roles from the catalogue.

## 7. Encryption of sensitive fields

- Library: `cryptography` Fernet via `MultiFernet(DATA_ENCRYPTION_KEYS)` (AES-128-CBC + HMAC-SHA256, authenticated, built-in timestamp). Newest key encrypts; all keys decrypt -> key rotation by prepending a key and running a re-encrypt script (`scripts/rotate_keys.py`). Alternative AES-256-GCM is acceptable if the backend dev prefers; keep behind the same interface.
- Implemented once as SQLAlchemy `TypeDecorator` `EncryptedString` / `EncryptedJSON` (python value <-> bytea). Models never see ciphertext; schemas never serialise `_enc` names.
- Encrypted: DOB, anniversary, personal email, phone, emergency name/phone, address, attendance coordinates. Not encrypted: names, official email, employee_id (needed for search/unique).
- Consequences: no SQL filtering/sorting on encrypted columns. Birthday/anniversary feed: service loads active profiles, decrypts in memory, filters by month/day; result cached in Redis until midnight. Geofence math done in app. Admin search uses unencrypted columns only.
- Keys: env only, never in DB or logs; backups are encrypted separately (GPG/age). Disk/volume encryption (LUKS) recommended on the server (devops note).
- Files (selfies, attachments): stored outside web root, random UUID names, served only through authorised `/api/files/{id}`; consider encrypting selfies at rest the same way (decision O6).

## 8. Audit-log approach

- `core/audit.py` exposes `audit(session, actor, action, entity_type, entity_id, before=None, after=None, outcome="success")`, called in the same DB transaction as the change (rolls back together); failure events (login failed) are written in their own transaction.
- Action names `<entity>.<verb>`: `auth.login_success|login_failed|account_locked|logout|logout_all|password_changed|refresh_reuse`, `employee.create|update|delete|deactivate|activate|unlock|reset_password`, `role.*`, `department.*`, `shift.*`, `holiday.*`, `leave_type.*`, `settings.update`, `announcement.*`, `request.approve|reject`, `export.run`, `profile.update_admin`.
- `before/after` hold only changed fields; secrets (password hashes, tokens) never stored; sensitive profile values stored as `"***"`.
- Context (ip, user agent, request_id) from a request-scoped contextvar. Append-only enforced by DB grants + trigger; optional `row_hmac` chaining (prev hmac + row) to detect tampering. Retention: keep >= 1 year, no purge in v1.

## 9. Cross-cutting backend rules
- Time: store UTC, compute `work_date`, shift times and cut-offs in `office.timezone`.
- Errors: single `AppError(code, status, message, details)` -> contract error format; unexpected -> `INTERNAL_ERROR` with request_id, no stack to client.
- Rate limits (Redis): login (see 01), uploads 20/min/user.
- Uploads: size and magic-byte type check, re-encode images (strip EXIF), random names.
- All list endpoints paginated per contract; N+1 avoided with eager loading.
- Push via FCM best effort; failure never fails the request; invalid tokens pruned.
- Logs: JSON, request_id, never log passwords, tokens, coordinates, PII.

## 10. Flutter structure (`frontend/lib`)

```
main.dart   app/ (app.dart, router.dart [go_router + auth guard], theme/ light+dark, l10n optional)
core/
  api/ dio_client.dart, interceptors (auth header, refresh single-flight, error mapper, request_id), cert_pinning.dart, api_exception.dart
  storage/ secure_storage.dart (tokens, biometric flag), cache_store.dart (Hive/Drift for offline caching)
  security/ device_integrity.dart (root/jailbreak), screen_protect.dart (FLAG_SECURE / iOS blur), inactivity_watcher.dart, biometric_gate.dart
  config/ env.dart (API_BASE_URL dart-define), constants.dart
  widgets/ (AppScaffold, LoadingView, ErrorView, EmptyView, PrimaryButton, form fields) ; utils/ (date, validators)
  location/ location_service.dart
features/<name>/{data (dto, repository, api), domain (models), presentation (screens, widgets, providers)}
  auth/  home/  attendance/  leave/ (balance, request, my_requests; leave+permission)  late/  food/  announcements/
  profile/  admin/{employees,roles,approvals,masters,announcements,reports,audit}
shell: bottom navigation (Home, Attendance, Leave, Food, More) ; More -> Late, Announcements, Profile, Admin (permission-gated)
```
Riverpod (`AsyncNotifier`), Dio, freezed/json_serializable for DTOs, repository pattern per feature, caching of read-only GETs (balances, history, announcements, config) with stale-while-revalidate; writes are online only.
Admin UI entries are shown from `permissions` returned at login (UI hint only; server enforces).

## 11. Ordered build plan (one feature at a time: DB -> backend -> Flutter -> security -> QA)

Phase 0 foundation, then features in order: 01 auth, 02 attendance, 03 leave+permission, 04 late, 05 food, 06 announcements, 07 profile, (08 removed), 09 admin (remaining masters/roles/audit UI; the admin API for each master is built with the feature that needs it but Admin UI screens consolidated here), 10 export.
(Note: employees admin create API + minimal masters CRUD are needed by feature 01 to create users; they are delivered in 01 backend tasks as "admin-lite" and extended in 09.)

**database-engineer**
1. Alembic setup in `database/`, extensions, roles `app_owner/app_user`.
2. Migration 0001: roles, permissions, role_permissions, departments, shifts, users, employee_profiles, files, refresh_tokens, device_tokens, password_reset_requests, audit_logs (+ append-only trigger), settings. Seed script (roles, permissions, default settings/shift/department).
3. Later migrations per feature plan (0002 attendance+late, 0003 leave/permission/holidays, 0004 food, 0005 announcements; 0006 was trips/sos, since removed). Holidays table lands with 0003 but needed earlier by attendance: create in 0002.
4. `schema.sql`, `backup.sh` (pg_dump + gpg, retention), `restore.sh`, restore drill documented.

**backend-developer**
1. Skeleton: config, db, redis, errors, logging, pagination, crypto, audit, rbac, files, health `/healthz`.
2. Models+schemas for foundation tables; `scripts/create_admin.py`, `seed.py`.
3. Feature services/routers in order 01-10 (see each plan). Scheduler/worker skeleton when 02 needs it.

**flutter-developer**
1. Project scaffold, theme, router, dio client with interceptors + pinning, secure storage, shell/bottom nav, shared widgets, error/loading patterns.
2. Features in order 01-10; each: data -> providers -> screens -> widget tests.

**devops-engineer**
1. `deploy/` compose: postgres, redis (password, no public port), api, worker, nginx (TLS 1.2+, HSTS, only 443 exposed, upload size limit, rate limit zone for /api/auth), volumes (pgdata, files), `.env.example`, health checks.
2. Backup cron container/script (encrypted, retention), log rotation, self-signed cert helper for dev, runbook in README.

**security-reviewer** after each feature (checklist in feature plan); full review before release (TLS, pinning, secrets, headers, dependency audit).
**qa-tester** after each feature: pytest for services/endpoints, RBAC matrix tests (parametrised over roles), Flutter widget tests; full regression before release.

## 12. Open decisions (foundation)
- O1 Single role per user (chosen) vs multiple roles.
- O2 Manager scope = direct reports only (chosen); transitive hierarchy later.
- O3 Single office geofence in settings (chosen) vs multiple offices table.
- O4 Worker as extra compose service (chosen) vs in-API scheduler.
- O5 Fernet (chosen) vs AES-GCM.
- O6 Encrypt selfie files at rest? default: yes if cheap (Fernet per file), else rely on disk encryption.
- O7 FCM needs a Google service account file and outbound internet; confirm acceptable under the on-prem rule (README allows it for delivery only).
