# Security review 01 - Authentication (backend, database, Flutter)

Reviewer: security-reviewer (read-only). Scope: backend/app (core/security, rbac, rate_limit, audit, crypto, config, errors, main, services/auth, services/employees, routers/auth, routers/admin_employees, schemas), database (0001 migration, seed.py, schema.sql), frontend/lib (core/api, core/storage, core/security, features/auth, app/router). Checked against OWASP API Top 10 2023, OWASP Mobile Top 10 2024, BUILD_PROMPT.md and docs/plans/01-auth.md section 6.

Not verifiable: `deploy/` (docker-compose, nginx.conf, .env.example) does not exist yet, so nginx `limit_req`, HSTS, TLS config, X-Real-IP overwrite and whether port 8000 is published could not be checked. Platform folders (`frontend/android`, `frontend/ios`) are not committed either. Tests were not run.

Counts: Critical 0 | High 4 | Medium 12 | Low 12. Contract mismatches: 9 (section B).

---

## A. Findings

### HIGH

**H1. Client-IP trust model breaks the IP rate limit (bypass when on, global lockout when off)**
- `backend/app/main.py:24-25`, `backend/app/core/config.py:19`, `backend/Dockerfile:22`, `backend/.env.example:4`, `backend/app/services/auth.py:153-154`.
- With `TRUST_PROXY_HEADERS=true` the raw `X-Real-IP` header is trusted from ANY peer, is not validated as an IP, and is used verbatim in the Redis key `rl:login:ip:{ip}`. If port 8000 is reachable (Dockerfile `EXPOSE 8000`, binds 0.0.0.0) or nginx does not overwrite the header, an attacker sends a fresh `X-Real-IP` per request and the 20/10min IP limit never trips. The per-employee-id limit (10/10min) and DB lockout still stop single-account guessing, but password spraying (one common password against every employee id) is then unthrottled, and the audit `ip` is forgeable.
- With the default `false` (and `uvicorn --proxy-headers` whose default `forwarded-allow-ips` is 127.0.0.1, so it ignores nginx in another container), every request has the nginx container IP. The whole company then shares one budget of 20 logins per 10 minutes: normal 9:00 sign-ins return 429 for everyone, and one attacker can lock everyone out of login.
- Fix: replace the boolean with `TRUSTED_PROXIES` (CIDR list). Take `X-Real-IP`/`X-Forwarded-For` only if `request.client.host` is in that list, parse with `ipaddress.ip_address`, discard invalid values, collapse IPv6 to /64. Pass `--forwarded-allow-ips` to uvicorn. Do not publish port 8000 (compose `expose:` only, internal network). Make nginx use `proxy_set_header X-Real-IP $remote_addr;` (overwrite, never append). Refuse to start in prod if a proxy is configured but `TRUSTED_PROXIES` is empty.

**H2. Root/jailbreak detection is a stub that always reports "clean"**
- `frontend/lib/core/security/device_integrity.dart:14-19` (`isCompromised()` returns `false`); consumed at `frontend/lib/features/auth/presentation/auth_controller.dart:110-117`.
- BUILD_PROMPT requires root/jailbreak detection and plan O3 blocks Admin on compromised devices. As built, Admin tokens, the biometric flag and all stored secrets are usable on a rooted device with no warning, and `BlockedScreen`/banner are dead code (OWASP M7/M8).
- Fix: integrate a maintained detector (for example freerasp or a jailbreak-detection plugin) plus Play Integrity / App Attest; fail closed for Admin when the check errors. Re-check on resume, not only at login.

**H3. Mobile hardening that the plan relies on is not in the repo, and the screenshot block fails silently**
- `frontend/` has no `android/` or `ios/` (README says "not committed yet"). `frontend/lib/core/security/screen_protect.dart:21-29` swallows `MissingPluginException`, so with no native handler FLAG_SECURE is never set and nothing reports it. `allowBackup=false`, R8/minify, network security config and the iOS equivalent (snapshot blur / `UIScreen.isCaptured` handling) do not exist. iOS has no handler at all (the doc comment only describes Android).
- Impact: screenshots, screen recording and recents thumbnails of login, change-password, lock screen and any later screen; token store restorable through Android backup (OWASP M8/M9).
- Fix: commit the generated platform folders with the hardening applied (MainActivity.kt channel, `android:allowBackup="false"`, `usesCleartextTraffic="false"`, R8 on), add an iOS AppDelegate blur-on-inactive and capture detection, and make `setSecure` report failure in release so a missing handler is caught by a test.

**H4. Certificate pinning is optional and silently off in release builds; HTTPS is not enforced**
- `frontend/lib/core/config/env.dart:6-8,13`, `frontend/lib/core/api/cert_pinning.dart:66-67`.
- If `CERT_PINS` is omitted from a release build, `applyCertPinning` returns without doing anything; the app then trusts every system/user-installed CA (MITM of tokens and PII on hostile Wi-Fi with an installed root). `API_BASE_URL` can also be `http://` and nothing rejects it.
- Fix: in `createDio` throw (or assert with a `kReleaseMode` check) when `kReleaseMode && pins.isEmpty` or when `Uri.parse(baseUrl).scheme != 'https'`. Require at least two pins. Add a CI step that checks the release command contains `CERT_PINS`.

### MEDIUM

**M1. Lockout enables targeted DoS and leaks account existence; the locked path is a timing oracle**
- `backend/app/services/auth.py:169-181` (423 returned before any hash work), `:110-141`.
- (a) 423 `ACCOUNT_LOCKED` is only ever returned for real accounts, so an attacker sends 5 wrong passwords to an id and learns it exists (the contract requires 423, plan section 6 says "no enumeration"). (b) The locked branch skips Argon2, so it answers about 150 ms faster than the other branches. (c) Anyone can keep any account, including the only Admin, locked indefinitely at 5 attempts per 15 min from one IP (limit is 10/10min per id). HR cannot unlock admins (`employees.py:73-76`) and `create_admin.py` does not exist, so a locked sole admin has no in-app recovery.
- Fix: run `verify_dummy` on the locked path so timing matches; add a break-glass CLI (`create_admin.py` / `unlock_admin.py`); consider progressive per-IP delays so a single source cannot hold an account locked, and only reveal the lock countdown after a correct password.

**M2. Argon2 runs synchronously on the event loop (CPU DoS)**
- `backend/app/core/security.py:37-62`, called from `services/auth.py:164,187,207,286,297`, `services/employees.py:187,192,306,307`.
- Each verify is about 100-250 ms of blocking CPU and 64 MiB. FastAPI async handlers stall all requests (including `/healthz`) while it runs; about 20 concurrent logins from different IPs (see H1) freeze the API.
- Fix: run every hash/verify via `anyio.to_thread.run_sync` (or `run_in_threadpool`) with a bounded `CapacityLimiter` of about 4.

**M3. Access tokens outlive logout, logout-all, password change, admin reset (up to 15 min)**
- `backend/app/core/rbac.py:56-63`, `backend/app/services/auth.py:256-306`, `services/employees.py:303-325`.
- The principal check loads the user row but never looks at session state, so a stolen access token keeps working for up to 15 minutes after the victim logs out everywhere or changes the password. The plan edge case "user changes password on device A, device B's next refresh fails" only bites after B's access token expires.
- Fix: add a `sid` (family id) claim, or compare `iat` to a new `users.token_valid_after` column in `get_principal`; set it in logout-all, change-password, reset-password, deactivate.

**M4. Wrong current password on /auth/change-password is double-counted toward lockout (client retries it)**
- Client: `frontend/lib/core/api/dio_client.dart:105-131` (any 401 on a non-`noAuth` request triggers refresh then one replay). Server: `backend/app/services/auth.py:284-285` returns 401 `INVALID_CREDENTIALS` for a wrong current password and increments `failed_login_count` each time (`:110-141`).
- Each wrong entry therefore counts twice, so a user is locked after 3 mistakes instead of 5, and the replay can return 423 mid-flow. See B1.
- Fix: in `onError` only refresh when the code is `UNAUTHORIZED` or `TOKEN_EXPIRED` (not `INVALID_CREDENTIALS`), or move wrong-current-password to a non-401 code. Add an interceptor test.

**M5. Refresh rotation edge cases: no grace for lost responses, revoked tokens reported as reuse, session never expires**
- `backend/app/services/auth.py:226-238`, `:72`, `:246-250`; client `dio_client.dart:148-153`.
- (a) If the response to `/auth/refresh` is lost (tunnel, app killed between server commit and `saveTokens`), the retry presents a used token, so the whole family is revoked and the user is logged out. (b) `revoked_at` set by logout / logout-all / password change / deactivate is also reported as `TOKEN_REUSED` and writes an `auth.refresh_reuse` audit row, so every legitimate second device produces a false theft alarm and the signal becomes useless. (c) Each rotation issues a fresh 30-day expiry (`issue_tokens` at `:72`), so a family lives forever while it is used; there is no absolute lifetime.
- Fix: (a) allow re-presenting the immediate predecessor within about 10-20 s if its successor is unused. (b) Return `UNAUTHORIZED`/`SESSION_REVOKED` for `revoked_at` rows and reserve `TOKEN_REUSED` for `used_at` rows. (c) Add `family_started_at` and cap at e.g. 90 days.

**M6. Biometric unlock is a UI flag, not a key gate; tokens readable without it**
- `frontend/lib/core/storage/secure_storage.dart:26-28,40-43`, `auth_controller.dart:40-50`, `core/security/biometric_gate.dart:24-34`.
- Plan section 3 says the refresh token is kept with biometric-protected access. Here tokens sit in ordinary secure storage and the biometric prompt is only a boolean `if`. On a rooted device (see H2) or with Frida the check is skipped and the refresh token is readable. With biometrics off, a cold start restores the session with no gate at all. `_repo.logout()` on a cancelled prompt (`auth_controller.dart:45-48`) also lets anyone holding the phone force a sign-out.
- Fix: store the refresh token behind a biometric-bound Keystore/Keychain item (`setUserAuthenticationRequired` / `biometryCurrentSet` access control) and read it only after the prompt. On cancel just stay locked.

**M7. Insecure configuration defaults and late key validation**
- `backend/app/core/config.py:16,62-68`, `backend/app/core/crypto.py:24`, `config.py:31`.
- `APP_ENV` defaults to `dev` and is an unchecked string: forgetting it, or `APP_ENV=production`, silently enables `/docs`, `/openapi.json`, CORS origins and weak secrets, and skips every prod check. The prod check is only a substring blacklist on the JWT secret. `DATA_ENCRYPTION_KEYS` is declared required in Settings (so a `.env` file satisfies it) but `crypto.py` reads `os.environ` directly, so with `.env`-only config the first encrypted write (for example creating an employee with a phone, `employees.py:208`) raises `RuntimeError` and returns 500. Fernet keys are never validated at startup.
- Fix: `app_env: Literal["dev","test","prod"]` with no default (or default `prod`); build `MultiFernet` from `get_settings()` and instantiate it in `create_app()` so a bad key stops startup; in prod also require a Redis password and a non-owner DB role.

**M8. Privilege handling by role name; HR can mint HR/Manager accounts; last-admin check is racy**
- `backend/app/services/employees.py:73-76,90-92,167-168,226-232`; seed `database/seed.py:39-43`.
- Admin protection is `role.name == "admin"`. HR (holds `employee.create`/`employee.update`) may create or promote any non-admin role (`hr`, `manager`, or any custom role the admin later builds with a superset of HR's permissions), and may `reset-password` any non-admin, including managers, then log in as them (takeover of approvers). Only the audit log reveals it. `_guard_last_admin` counts admins without locking the other admin rows, so two concurrent deactivations of two different admins can both pass with count 2 and leave zero.
- Fix: authorise by permission-set containment (an actor may only assign roles whose permissions are a subset of the actor's), gate role assignment behind an Admin-only `role.assign`, forbid reset-password on accounts whose role holds permissions the actor lacks; lock all admin rows (`SELECT ... FOR UPDATE` where role=admin) or use `pg_advisory_xact_lock` in `_guard_last_admin`.

**M9. Audit redaction and content gaps**
- `backend/app/core/audit.py:14-20`, `backend/app/services/auth.py:165`, `backend/app/services/employees.py:46-56,248-252`, `backend/app/models/security.py:104`.
- `redact()` masks only top-level keys by substring; nested dicts and lists are stored raw, and PII keys (`personal_email`, `phone`, `address`, `dob`, `emergency_*`, `home_location`) are not in `_SENSITIVE`, so the next feature that audits profile changes will write plaintext PII into an unencrypted JSONB. The snapshot already stores `full_name` and `official_email`. For unknown ids, `actor_employee_id=eid` stores whatever was typed in the id field, which is often a password typed into the wrong box. Phone changes in `update_employee` are not in the snapshot (unaudited). `row_hmac` exists but is never populated, so tamper evidence is absent.
- Fix: recursive redaction plus a per-entity allow-list of loggable fields; log `eid` only if it matches `^[A-Z0-9._-]{1,32}$`, else a SHA-256 prefix; audit phone changes as `"***"` changed; populate `row_hmac` (HMAC with a key held outside the DB).

**M10. FLAG_SECURE has no reference count and does not cover the app-switcher snapshot**
- `frontend/lib/core/security/screen_protect.dart:41-52`, used by `login_screen.dart:109`, `change_password_screen.dart:78`, `lock_screen.dart:18`.
- Each `SecureScreen` sets secure in `initState` and clears it in `dispose`. When the router replaces Login with ChangePassword or Home with Lock, the new screen's `initState` normally runs before the old one's `dispose`, so the last call is `secure:false` and the new sensitive screen is unprotected (confirm on a device). Also, the OS takes the recents snapshot when the app pauses on the previous screen (for example Home with employee data), which is not a `SecureScreen`; the lock only appears after resume.
- Fix: a counter (call native only on 0<->1) or set the flag app-wide while authenticated; draw an opaque overlay on `AppLifecycleState.inactive` so the snapshot is blank.

**M11. 403 `ACCOUNT_INACTIVE` / `PASSWORD_CHANGE_REQUIRED` are not handled mid-session**
- Server: `backend/app/core/rbac.py:60-61,71-72`. Client: `frontend/lib/core/api/dio_client.dart:105` (only 401), `api_exception.dart:31-35` (`isAuthFailure` is defined but never used).
- A deactivated user, or one whose password was reset while logged in, keeps a "logged in" UI where every call fails with 403 until the access token expires and refresh fails. Admin reset sets `must_change_password`, but the app never routes to the change-password screen.
- Fix: in `onError` treat 403 `ACCOUNT_INACTIVE` as `_endSession`, and 403 `PASSWORD_CHANGE_REQUIRED` as a transition to `AuthNeedsPasswordChange` (re-fetch `/auth/me`).

**M12. Forgot-password: timing differs, queue not reachable, no client flow**
- `backend/app/services/auth.py:310-336`; `backend/app/routers/admin_employees.py` (no list route); `frontend/lib/features/auth/presentation/login_screen.dart:183-186`.
- Existing active users cause an extra SELECT plus INSERT, audit and commit, while unknown users return immediately, so response time distinguishes them (plan: "identical timing"). Any caller can open a request for every valid id (queue spam, 1 open per user). `GET /admin/password-reset-requests` (api-contract.md:351) is not implemented, so Admin/HR cannot see requests at all. The Flutter app has no forgot screen (plan 5.4) and calls nothing.
- Fix: do the DB work in a background task after returning a fixed-time response (or pad to a minimum latency); implement the admin list route; add the screen or drop it from the plan.

### LOW

- **L1. Rate limiter fails open with 1 s timeouts per call** - `backend/app/core/rate_limit.py:33-35`, `core/redis.py:13`. During a Redis outage every login pays two 1 s timeouts and spraying across accounts is unthrottled (per-account lockout only). Documented as O1; add a small in-process fallback limiter and an alert. The key space is attacker-controlled (see H1).
- **L2. `.dockerignore` is in the wrong place** - `backend/.dockerignore` is not read when the build context is the repo root (`backend/Dockerfile:1-2`, `COPY backend/ backend/` at line 11). `.venv`, tests, caches and any local `backend/.env` are baked into the image. Move it to the repo root (or `backend/Dockerfile.dockerignore`) and add `.env*`.
- **L3. Password policy diverges between client, server and seed** - server `security.py:71-78` uses Unicode `isupper/isalnum`, so a space counts as a symbol and an accented letter counts as a letter; client `validators.dart:26-29` uses ASCII classes (a password like "Passwordé1" passes the client and fails the server); `database/seed.py:94-115` has no common-password check and the seed docstring example `Temp#Passw0rd!` passes it. Server `details` messages ("is too common") are shown without the field name (`change_password_screen.dart:62-63`). Align on one definition, reject whitespace as the only symbol, and reuse the server list in seed.
- **L4. Background/inactivity timing uses the wall clock** - `frontend/lib/core/security/inactivity_watcher.dart:58-72`. Setting the device time back defeats the 30 min background logout and the lock. Use a monotonic clock. The 30 min logout also only runs on resume, so `/auth/logout` is never called if the app stays backgrounded (acceptable if documented; server refresh expiry still applies).
- **L5. iOS keychain accessibility not set** - `secure_storage.dart:26-28`. Defaults allow backup migration. Add `iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock_this_device)`.
- **L6. JWT claims** - `backend/app/core/security.py:122-151`. No `iss`/`aud`; `jti` and `role`/`pwd` claims are unused for decisions (good: authz is DB-driven). Add `iss`/`aud` for future service separation.
- **L7. Device tokens** - `backend/app/services/auth.py:343-350`, `routers/auth.py:108-113`. The FCM token is in the URL path (access-log exposure) and a token already registered to another user is silently re-assigned. Logout never deletes the device token (client `auth_repository.dart:48-63` does not call DELETE), so a shared device keeps receiving the previous user's pushes. Move the token to a request body, bind to `device_id`, delete on logout.
- **L8. Settings and filters unbounded** - `services/auth.py:376-395` returns raw setting values (a typo such as `auth.inactivity_timeout_minutes=100000` effectively disables the inactivity lock; the client only checks `> 0`, `auth_controller.dart:128`). `admin_employees.py:22-24` has no length cap on `role`, and `services/employees.py:129` does not escape `%`/`_` in `q`. Clamp config (1-30 min) on server and client.
- **L9. Contract endpoints/files missing** - `DELETE /admin/employees/{id}` (api-contract.md:345; `employee.delete` exists in seed), `GET /admin/password-reset-requests` (api-contract.md:351), `backend/create_admin.py` (plan 01 task 8). Needed for break-glass (M1).
- **L10. Client-supplied `X-Request-ID` is trusted** - `backend/app/main.py:26` stores up to 64 attacker-chosen characters in audit `request_id` and logs. Validate `^[A-Za-z0-9-]{8,64}$` or generate server-side.
- **L11. Crypto hardening** - `backend/app/core/crypto.py:22-75`. One Fernet key set for every column and no per-column context, so a DB-write attacker can swap ciphertexts between fields/rows; no rotation/re-encrypt script although the docstring promises one; `InvalidToken` after a key mistake becomes a generic 500. Bind a column label into the plaintext and ship `rotate_keys.py`. Fernet (AES-128-CBC + HMAC) is otherwise acceptable.
- **L12. Session-ending on any 4xx during refresh/bootstrap** - `dio_client.dart:154-158` and `auth_controller.dart:55-60` clear tokens for any 4xx, including 429 or a proxy 4xx; only 401/403 with an auth code should. `/auth/refresh` itself has no server-side throttle (rely on nginx `limit_req`, unverifiable now) and the client sends `device_name: Platform.operatingSystem` (`auth_repository.dart:26`), which is useless in a device list.

---

## B. Frontend <-> backend contract check (auth)

Agrees (verified):
- Login request fields `employee_id,password,device_id,device_name` (`auth_repository.dart:22-27` vs `schemas/auth.py:11-15`).
- Login response `access_token, refresh_token, token_type, expires_in, must_change_password, user{id,employee_id,full_name,role,permissions}` (`services/auth.py:77-86,211`, `schemas/auth.py:39-48` vs `auth_user.dart:22-40`).
- `/auth/me` flat shape with `must_change_password` (`routers/auth.py:83-90` vs `auth_user.dart:28-29`).
- Refresh body `{refresh_token}`, response without `user` (`dio_client.dart:143-153` vs `routers/auth.py:39-41`).
- Logout body `{refresh_token}` -> 204, logout-all no body (`auth_repository.dart:48-63` vs `routers/auth.py:44-57`).
- Change-password body `{current_password,new_password}` with a login-shaped reply that is persisted (`auth_repository.dart:39-45` vs `routers/auth.py:60-66`).
- Error envelope `{"error":{code,message,details[{field,message}],request_id}}` (`errors.py:54-58` vs `api_exception.dart:56-77`).
- 423 `details:[{field:"locked_until",message:"<ISO Z>"}]` (`services/auth.py:30-36`, `clock.py:18-19` vs `api_exception.dart:45-48`, `login_screen.dart:63-65`).
- 429 `Retry-After` integer seconds (`rate_limit.py:45` vs `api_exception.dart:68`, `login_screen.dart:66-68`).
- `/config` -> `auth.inactivity_timeout_minutes` (`services/auth.py:389` vs `auth_repository.dart:68-70`).
- `TOKEN_REUSED` ends the session and clears the biometric flag (`dio_client.dart:111-114,162-165`, `secure_storage.dart:75-81`).

Mismatches:

| # | Topic | Backend | Frontend | Effect |
|---|---|---|---|---|
| B1 | Wrong current password on change-password | `services/auth.py:284-285`: 401 `INVALID_CREDENTIALS` and `_register_failure` (+1 failure); contract `api-contract.md:81` | `dio_client.dart:105-131`: every 401 on an authenticated call triggers refresh + one replay; `change_password_screen.dart:60-61` expects `INVALID_CREDENTIALS` | Each wrong entry counts twice toward lockout (M4); spurious token rotation |
| B2 | 403 `ACCOUNT_INACTIVE` on normal requests | `rbac.py:60-61` | `dio_client.dart:105` handles only 401; `api_exception.dart:31-35 isAuthFailure` unused | UI keeps session until the access token expires (M11) |
| B3 | 403 `PASSWORD_CHANGE_REQUIRED` on normal requests (admin reset while logged in) | `rbac.py:71-72` | no handler; only the `must_change_password` flag from login/me is read (`auth_controller.dart:118-121`) | User sees errors instead of being routed to `/change-password` |
| B4 | `TOKEN_REUSED` after password change / logout-all / admin reset / deactivate | `services/auth.py:226-238`: any `revoked_at` row -> `TOKEN_REUSED` + `auth.refresh_reuse` audit | `auth_controller.dart:141` shows "For your security, please sign in again." | Plan edge case wants a "password changed elsewhere" message; false reuse alarms (M5b). Device B also keeps working until its 15 min access token expires (M3) |
| B5 | Refresh response `must_change_password` | `TokenResponse` includes it (`schemas/auth.py:39-44`) | `dio_client.dart:148-153` ignores it | No impact today (reset revokes tokens), but flag drift would go undetected |
| B6 | Refresh failures other than 401 | 429/422/403 possible (`RefreshRequest` min length gives 422, `schemas/auth.py:19`) | `dio_client.dart:154-158` ends session on any non-network, non-5xx response | A 429 from nginx kills the session (L12) |
| B7 | Device-token lifecycle | `DELETE /me/device-tokens/{fcm_token}` exists (`routers/auth.py:108`); plan: delete on logout | `auth_repository.dart:48-63` never calls it; no FCM dependency in `pubspec.yaml` | Pushes to the wrong user on shared devices once FCM lands (L7) |
| B8 | Forgot password | `POST /auth/forgot-password` 202 (`routers/auth.py:69-74`); contract lines 84-85 | no repository method or screen (`login_screen.dart:183-186` is text only); plan 5.4 lists the screen | Missing client flow |
| B9 | Admin endpoints in contract | `DELETE /admin/employees/{id}` (`api-contract.md:345`) and `GET /admin/password-reset-requests` (`:351`) are absent from `routers/admin_employees.py` | n/a (no admin UI yet) | Contract drift; reset requests cannot be handled (M12, L9) |

Minor, not mismatches: client `validateEmployeeId` max 32 vs backend 64 (`validators.dart:52` vs `schemas/auth.py:12`) is consistent in practice. `PASSWORD_REUSED` and `LAST_ADMIN` set their status explicitly (`services/auth.py:287`, `employees.py:92`) so they work although they are absent from `errors.STATUS_CODES`; add them to that table.

---

## C. Checklist results (plan section 6 and BUILD_PROMPT)

Implemented correctly
- Argon2id (`security.py:29`, `argon2-cffi` default type is ID), t=3 m=64 MiB p=2, rehash on login (`services/auth.py:206-207`), 128-char cap (`schemas/auth.py:8`), dummy verify for unknown ids (`:164`), canonical id and rate-limit keys (`auth.py:26-27,152`, so case variants do not bypass; DB column is CITEXT with a canonical CHECK).
- JWT: HS256 pinned in validator and decode (`config.py:53-58`, `security.py:140-146`), `exp/sub/iat` required, 10 s leeway, secret min 32, no permissions in token, authz and status loaded from the DB on every request so deactivation is immediate (`rbac.py:57-63`).
- Refresh: 256-bit random, SHA-256 at rest, unique index, `FOR UPDATE` row lock in rotation (`:219`), rotation in one transaction with `replaced_by`, family revoke on reuse; the per-user row lock during login serialises concurrent guesses (`:158`).
- Lockout stored in the DB (survives Redis flush), reset on success/expiry/admin unlock/reset, 423 before verification, inactive state disclosed only after a correct password.
- RBAC: every route in `routers/auth.py` and `routers/admin_employees.py` has a `Depends(require_permission(...))`; admin routes demand the specific permission; managers are limited to direct reports and out-of-scope ids return 404 (`employees.py:156-160`; no BOLA found in this module); self-demotion, self-deactivation, last-admin and HR-targeting-admin are blocked (modulo M8). No endpoint trusts a client-supplied role or user id for authorisation.
- Temp passwords: `secrets`, 12 chars, policy-checked, returned once, `Cache-Control: no-store` (`main.py:31`), never logged; audit redaction masks `password`/`token`/`hash`/`otp` top-level keys.
- Errors: single envelope, generic 500 without stack trace (`errors.py:79-82`), `/docs` and `/openapi.json` off in prod, non-root container user, `app_user` DML-only role, audit table append-only via trigger for every role plus REVOKE (`0001_foundation.py:246-262`), employee PII columns Fernet-encrypted (`models/users.py:95-104`), SQL via bound parameters (no injection found).
- Client: tokens only in `flutter_secure_storage` (no SharedPreferences, no logging interceptor, no `print` in `lib/`), single-flight refresh with one replay, password fields disable suggestions/autocorrect, biometric flag cleared on logout/password change/TOKEN_REUSED, SPKI pinning logic is sound when pins are set (system roots disabled, `cert_pinning.dart:70-74`), inactivity lock and 30 min background logout exist, route guard covers all states (`router.dart:38-53`).

Not done / not verifiable: root detection (H2), FLAG_SECURE and platform hardening (H3), pinning enforcement (H4), nginx/TLS/HSTS/`limit_req` (no `deploy/`), release obfuscation (README only), push token cleanup (L7), forgot-password UI (B8).

---

## D. Summary

Counts: Critical 0, High 4 (H1-H4), Medium 12 (M1-M12), Low 12 (L1-L12). Contract mismatches 9 (B1-B9; B1-B4 matter most).

Top issues to fix first
1. H1: trusted-proxy handling. Either spoofable `X-Real-IP` (spraying unthrottled) or a single shared IP bucket that rate-limits the whole company. Add `TRUSTED_PROXIES`, validate IPs, do not publish port 8000.
2. H2/H3/H4: the mobile controls required by BUILD_PROMPT (root detection, screenshot blocking, pinning) exist as stubs or docs only and fail silently. Commit platform folders with the hardening and make release builds fail without pins.
3. M4/B1: the app's interceptor retries a wrong-current-password 401, doubling lockout counts. Small client fix (refresh only on `UNAUTHORIZED`/`TOKEN_EXPIRED`).
4. M3: access tokens stay valid after logout, logout-all, password change and reset; add a `token_valid_after` check in `get_principal`.
5. M2: Argon2 on the event loop makes the API easy to DoS; move hashing to a thread pool.
6. M1: lockout is an account-existence oracle and a way to lock out the only Admin; add a dummy verify on the locked path and a break-glass CLI.
7. M8: HR can create/promote HR and Manager accounts and reset their passwords; authorise by permission containment and lock admin rows for the last-admin guard.
8. M5: add a short rotation grace window, split revoked vs reused, add an absolute session lifetime.
