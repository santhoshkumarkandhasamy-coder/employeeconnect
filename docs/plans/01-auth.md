# 01 - Authentication

Contract: `docs/api-contract.md` section 1 (+ `/config`, `/me/device-tokens`). Foundation: `00-foundation.md`.

## 1. Scope
Employee ID + password login, forced first-login password change, Argon2id hashing, 15 min JWT + rotating revocable refresh token,
logout / logout-all, lockout, Redis rate limiting, forgot-password (Admin request, optional email OTP), biometric unlock and inactivity
logout (client side), audit of auth events. "Admin-lite" included so users can exist: `POST/GET/PUT /admin/employees`, `unlock`, `reset-password`,
`deactivate`, minimal departments/shifts/roles read, `create_admin.py`. Full admin UI is plan 09.

## 2. Database (tables in 0001; no new ones)
`users`, `roles`, `permissions`, `role_permissions`, `refresh_tokens`, `device_tokens`, `password_reset_requests`, `audit_logs`, `settings`. Seed first admin.

## 3. Mechanics

**Password hashing**: `argon2-cffi` Argon2id (time=3, mem=64 MiB, parallel=2; tune to ~100-250 ms). Rehash on login if params changed. Hash verification for unknown employee_id runs against a dummy hash (timing equalisation).

**Password policy** (server-enforced, mirrored in Flutter): 8-128 chars, >=1 upper, lower, digit, symbol; must not contain employee_id or full name tokens; not in a bundled top-1000 common list; must differ from current. Failed rules returned in `details`.

**Access token**: JWT HS256, 15 min, claims `sub` (user id), `jti`, `iat`, `exp`, `role`, `pwd` (true if must_change_password). No permissions in the token. Each request: verify signature/exp, load user row (status, deleted_at, must_change_password) and cached permission set; inactive/deleted -> 403 `ACCOUNT_INACTIVE`. So deactivation takes effect immediately.

**Refresh token**: 256-bit random (`secrets.token_urlsafe(48)`), only SHA-256 stored, 30 day expiry, one `family_id` per login.
Rotation on `/auth/refresh`: validate hash, not revoked, not expired, `used_at IS NULL`; mark `used_at`, insert new token in same family (`replaced_by`), return new pair (single transaction, row lock `FOR UPDATE`).
Reuse detection: presenting a token with `used_at` set (or revoked) -> revoke the entire family, audit `auth.refresh_reuse`, 401 `TOKEN_REUSED`, client forces re-login. Client must single-flight refresh (one in-flight refresh; queue other 401s) to avoid self-inflicted reuse.
`logout` revokes the family of the presented token; `logout-all` revokes all tokens of the user and deletes the device's FCM token on logout.

**Login flow**
1. Rate limit check (Redis): sliding window, keys `rl:login:ip:{ip}` (default 20 / 10 min) and `rl:login:eid:{lower(employee_id)}` (10 / 10 min). Exceeded -> 429 + `Retry-After`. Count every attempt.
2. Lookup user (case-insensitive employee_id). Unknown -> dummy verify, `INVALID_CREDENTIALS`.
3. Inactive/deleted -> 403 `ACCOUNT_INACTIVE` only after password verified correct (no status leak to guessers).
4. If `locked_until > now` -> 423 `ACCOUNT_LOCKED` (do not verify password; do not increment).
5. Verify password. Fail: `failed_login_count += 1`; at 5 set `locked_until = now + 15 min`, audit `auth.account_locked`; audit `auth.login_failed`; 401 (or 423 on the attempt that locks). Success: reset counter and `locked_until`, set `last_login_at`, issue tokens, audit `auth.login_success`.
6. Lock expiry: a login after `locked_until` passed resets the counter. Admin unlock clears both immediately (audited).
Lockout is per account in DB (survives Redis flush); Redis limits are per IP / ID burst control. Redis down: fail closed for login rate limiting? Decision: fail open with DB lockout still active, log error (O1).

**Forced password change**: new users and admin-reset users have `must_change_password=true`. Login succeeds and returns `must_change_password:true`; the token has `pwd=true`; all other endpoints return 403 `PASSWORD_CHANGE_REQUIRED` (dependency applied globally). `change-password` verifies current password (counts toward lockout on failure), applies policy, updates hash, sets `password_changed_at`, clears flag, revokes ALL refresh tokens, issues a fresh pair.

**Forgot password**: `/auth/forgot-password` always 202 with identical body/timing. Creates `password_reset_requests(open)` (max 1 open per user, rate limit 3/hour/ID and per IP). Admin/HR see it in admin list and use `reset-password` (temp password, flag set, sessions revoked, request marked handled). If SMTP configured and user has official email: also email a 6-digit OTP (hash stored, 10 min, max 5 attempts) usable at `/auth/reset-password`. Starting with Admin-reset only is acceptable for v1 (O2).

**Admin-lite actions** (`employee.create|unlock|reset_password|deactivate`): temp password = 12 chars random meeting policy, returned once in response, never logged or stored plain; deactivate revokes all tokens; guard `LAST_ADMIN`; all audited.

**Biometric / inactivity (client)**: after first successful login the user may enable biometrics (stored flag in secure storage). When enabled, the refresh token is kept in secure storage with biometric-protected access (`local_auth` gate on app resume/cold start); failing/disabled biometrics falls back to password login. Server unaware. Inactivity timer (from `/config auth.inactivity_timeout_minutes`, default 5) in foreground; on expiry lock the app behind biometric/password gate; after 30 min background/inactive perform full logout (call `/auth/logout`, wipe secure storage). Disable biometric on logout, password change, or `TOKEN_REUSED`.

## 4. Edge cases
- Employee ID case/whitespace: trim, case-insensitive compare, store canonical upper case.
- Password with unicode / leading spaces: not trimmed, length counted in characters.
- Concurrent refresh from two devices: separate families, independent.
- Refresh while user deactivated -> 403 `ACCOUNT_INACTIVE`, family revoked.
- Clock skew: JWT leeway 10 s.
- Token expiry mid-request: Dio interceptor refreshes once and replays the request once; second 401 -> logout.
- User changes password on device A: device B's next refresh fails -> login screen with message.
- Admin resets password for a locked user: unlocks as well.
- Admin demotes/deactivates self or last admin: blocked.
- Locked account message shows remaining time from `locked_until`.
- Deleting FCM token on logout; token re-registered at next login.
- No-network login: clear error state, no retry storm.

## 5. Flutter screens (features/auth)
1. Splash: read secure storage, check device integrity (root/jailbreak -> block screen or warn per policy O3), try refresh -> Home / Login.
2. Login: Employee ID + password (obscure toggle), inline errors, lockout countdown, loading state; FLAG_SECURE on.
3. Force Change Password: current, new, confirm; live policy checklist; on success go Home.
4. Forgot Password: employee ID -> confirmation message ("contact Admin / check email"), optional OTP entry screen.
5. App Lock screen: biometric prompt / password fallback (after inactivity or resume).
6. Settings (in Profile later): "Use biometrics" toggle, "Log out", "Log out of all devices".
Providers: `authControllerProvider` (state: unauthenticated / authenticating / needsPasswordChange / authenticated(user, permissions) / locked), `sessionRepository`. Router guard redirects by state. Dio interceptor: attach token, single-flight refresh, map error JSON to `ApiException`. SSL pinning: SHA-256 SPKI pins from `--dart-define`, backup pin included.

## 6. Security considerations
- Generic errors (no enumeration), uniform response timing, HTTPS only + HSTS, nginx extra limit `limit_req` on `/api/auth/`.
- JWT secret >=32 bytes from env; algorithm pinned (`HS256` only, reject `none`); `jti` kept for future denylist.
- Refresh token hashed at rest; rotation + reuse detection; token never logged; redact `Authorization`/`password` fields in logs.
- Argon2 parameters in config; DoS guard: max password length 128.
- Tokens only in flutter_secure_storage (Keystore/Keychain); excluded from OS backups (`android:allowBackup=false`).
- Screenshots blocked on auth screens; release obfuscation; pinning.
- Audit: login success/fail, lock, logout, logout-all, password change, refresh reuse, admin unlock/reset.
- Do not return password hash anywhere (schema exclusion test).

## 7. Tasks
**database-engineer**: migration 0001 (see foundation), constraints on users/refresh_tokens, audit append-only trigger, seed roles/permissions; verify indexes (`users.employee_id`, `refresh_tokens.token_hash`).
**backend-developer**: 1) security.py (argon2, jwt, policy validator, token hashing) 2) rbac dependency + current_user dependency with forced-change gate 3) rate limiter (Redis Lua/INCR window) 4) auth service + routers (login, refresh, logout, logout-all, change-password, forgot, reset, me) 5) `/config`, device tokens 6) admin-lite employees (create/list/get/update, unlock, reset-password, deactivate/activate) 7) audit hooks 8) `create_admin.py`.
**flutter-developer**: scaffold (if not done), dio client + interceptors + pinning, secure storage, auth repository/controller, screens 1-5, router guard, inactivity watcher, biometric gate, integrity check, FCM token registration.
**security-reviewer**: verify checklist in section 6, timing/enumeration, rotation race, lockout bypass via case variants, token claims, log redaction, nginx limits.
**qa-tester**: pytest: login ok/fail, lockout at 5th, auto-unlock after 15 min (freeze time), admin unlock, rate limit 429, forced change gate on other endpoints, refresh rotation, reuse revokes family, logout/logout-all, deactivated user blocked with valid access token, policy table tests, RBAC on admin-lite. Flutter: login form validation, interceptor refresh single-flight test, router guard tests.

## 8. Open decisions
- O1 Redis outage behaviour for rate limit (proposed fail-open, DB lockout remains).
- O2 Forgot password v1: Admin-reset only, OTP email optional later. Confirm SMTP availability.
- O3 Root/jailbreak: block app or warn + log? (proposed block for Admin role, warn others).
- O4 Inactivity timeout values (5 min lock / 30 min logout proposed).
