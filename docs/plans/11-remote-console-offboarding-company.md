# 11 - Remote access, Admin web console, Offboarding, Company registration, Licensing, Password reset

Status: backend IMPLEMENTED (contract section 12 is final). Remote access / devops items still follow the open questions at the end.
User decisions that override this text: unlicensed default is **50 seats** with **no expiry** (`expires_at` NULL = never), admins count as seats;
the console is internet-reachable and **Admin only** (`console.access` is not granted to hr); password recovery is admin reset only.
Contract: `docs/api-contract.md` section 12. Migration: `0017_company_console_offboarding`.
Out of scope and not reintroduced: trips, SOS, live location, home location. No multi-tenancy.

## 0. What already exists (reuse, do not rebuild)
- Deactivate/delete already revoke all refresh families, invalidate access tokens, delete device tokens and cancel pending requests (contract s.9).
- The app already ends the session on 403 `ACCOUNT_INACTIVE` and on refresh failures (`dio_client.dart` `_endSession`), and wipes `json_cache` and export folders on logout/lock.
- nginx (TLS, rate zones, security headers) is the only published service; SPKI pinning in the app checks the LEAF certificate only
  (`cert_pinning.dart`, `SecurityContext(withTrustedRoots:false)`), with >= 2 pins required in release builds.
- No `firebase_messaging` in the app yet (`NoPushTokenSource`), so pushes are a bonus here. API status codes are what actually triggers the wipe.

---

## 1. Remote access (internet, not only office Wi-Fi)

### 1.1 Options
| Option | How | Pros | Cons |
|---|---|---|---|
| **A. Public DNS + router port-forward** (recommended if the office line has a public IPv4) | `hr.<company-domain>` A record -> office public IP; router forwards WAN 443 (and 80 only for ACME HTTP-01) -> server 443/80 | No third party in the data path; end-to-end TLS; pinning unchanged; free | Opens an inbound port on the office network; needs a static IP or DDNS; impossible behind ISP CGNAT |
| **D. Small VPS TCP relay** (recommended if CGNAT / no inbound allowed) | Server keeps an outbound WireGuard tunnel to a ~USD 5/month VPS; the VPS runs HAProxy/nginx `stream` in TCP passthrough mode on 443 with PROXY protocol | No inbound ports at the office; TLS still terminates on OUR nginx (VPS only sees ciphertext); pinning unchanged; static IP from the VPS | One small rented host to patch; extra hop latency (~10-30 ms) |
| B. Cloudflare Tunnel (`cloudflared`) | Outbound-only tunnel; Cloudflare edge terminates TLS | Easiest, free, DDoS shield, works behind CGNAT | **Cloudflare decrypts every request (passwords, personal data) at its edge**, which conflicts with "data stays on our server" in spirit. **Breaks pinning**: the app would see Cloudflare's rotating edge certificate, so pins cannot be kept stable (the release build refuses fewer than 2 pins). Only acceptable with pinning removed or a paid custom-certificate plan. Not recommended |
| C. VPN for everyone (WireGuard/Tailscale) | Staff install a VPN client | Nothing public | Too heavy for every employee; good only for admins/SSH/console |

Recommendation: **A**, falling back to **D**. **B** only if the user explicitly accepts TLS termination at Cloudflare and weaker pinning.

### 1.2 DNS, DDNS, certificates
- One hostname everywhere (e.g. `hr.company.com`). Use **split-horizon DNS** inside the office (local DNS resolves it to the LAN IP) so the app works on office Wi-Fi without NAT hairpinning.
- No static IP: DDNS. Either the router's built-in DDNS client, or a `ddclient` cron/container that updates the record through the DNS provider API (TTL 300 s). With a provider such as Cloudflare DNS, use **DNS-only (grey cloud)**: no proxying.
- Public certificate: Let's Encrypt via host `certbot`/`acme.sh`. Prefer **DNS-01** so port 80 does not have to be forwarded. HTTP-01 also works: nginx serves `/.well-known/acme-challenge/` on :80.
  Deploy hook copies to `deploy/certs/` and runs `nginx -s reload`.
- **Pinning implications**: the app pins the leaf key only. The renewal MUST keep the same key (`certbot --reuse-key` / `acme.sh` without `--force-new-key`), otherwise every installed app breaks every 60-90 days.
  Pins = current leaf SPKI + offline spare key SPKI (`spki-pin.sh --gen-backup`). Key rotation rule (DEPLOY 1.3.2) stays the same: ship an app with the spare pin first, then switch keys.
  Pinning to Let's Encrypt intermediates is NOT possible with the current leaf-only check, and LE rotates intermediates anyway.

### 1.3 Configurable API base URL (Flutter)
- Precedence: saved server URL (secure storage key `server.base_url`, survives wipe) > build-time `API_BASE_URL` > none.
  If neither is set, or the build-time value is the placeholder `https://localhost/api`, the app shows **Server setup** on first run.
- Server setup: user types `hr.company.com` (scheme/`/api` added automatically). Release builds accept https only; no userinfo/query/fragment; port optional.
  The app calls `GET /api/public/company` (through the pinned client) and shows the returned company name + logo for confirmation before saving.
  The response must contain `"product":"employee-connect"`.
- "Change server" link on the login screen. Changing the server wipes everything (section 3.3), including branding.
- Pins stay build-time, so a user can only reach servers holding the pinned key. A phishing URL fails the TLS check. The company builds its own APK with `CERT_PINS`.

### 1.4 Firewall / hardening (devops, DEPLOY.md s.6 rewrite)
- Forward ONLY 443 (+80 if HTTP-01). Never 22/5432/6379/8000. SSH only from LAN/VPN. Disable router UPnP and remote admin, and change the router admin password.
- Keep the `DOCKER-USER` chain rule. For internet exposure, replace the LAN-only DROP with: allow 80/443 from any, drop everything else published.
- nginx: a default server that closes connections with unknown `Host`/SNI (`return 444`, `ssl_reject_handshake on`), so IP scanners hit nothing. Only `server_name ${PUBLIC_HOSTNAME}` serves the app.
- Option D: `listen 8443 ssl proxy_protocol; set_real_ip_from <wg-peer-ip>; real_ip_header proxy_protocol;` so audit/rate limits see real client IPs.
- Optional: fail2ban on nginx access logs (bursts of 401/429/444); unattended upgrades; external uptime check of `/nginx-health`.
- Existing per-employee login limits + lockout stay the primary brute-force control. Re-tune the per-IP zones (internet clients are no longer one NAT IP).

---

## 2. Admin web console

### 2.1 Choice: Flutter web, separate entrypoint, served by the existing nginx
- `frontend/lib/main_console.dart` + `lib/console/` (shell, sidebar, dashboard). It reuses the existing admin repositories, models, API client and most admin widgets.
  Built with `flutter build web --target lib/main_console.dart --base-href /console/ --no-web-resources-cdn --pwa-strategy none`.
  The output is static files copied into the nginx image (multi-stage `deploy/console.Dockerfile`). No new runtime container and no new language.
- Why not a separate static SPA: it would duplicate every model and screen in a second stack without a dedicated agent. Flutter web's ~2-3 MB first load is acceptable for a handful of admin users.
  Same origin as `/api`, so there is no CORS.
- Mobile-only plugins (secure storage, local_auth, geolocator, image_picker, root detection, screenshot blocking) must not be imported by the console entrypoint.
  Use conditional imports/stubs. Pinning does not apply in a browser (the browser trust store is used, which is why a public certificate is required).

### 2.2 Console auth (browser-safe)
- `POST /api/auth/web/login|refresh|logout`. The refresh token lives ONLY in an HttpOnly cookie `__Secure-ec_rt` (Secure, SameSite=Strict, Path=/api/auth/web).
  The access token is kept in memory only (never localStorage). A page reload calls `/auth/web/refresh`.
- CSRF: the cookie is sent only to `/api/auth/web/*`. Those endpoints also require header `X-EC-Client: console` and `Origin == PUBLIC_ORIGIN`. All other API calls use the bearer header (no cookie auth).
- New permission `console.access` (admin only, decision) is checked at web login and refresh. Web sessions: `client_type='web'`, absolute lifetime `console.session_max_hours` (12), inactivity sign-out `console.inactivity_timeout_minutes` (15).

### 2.3 Console pages (each hidden unless the caller holds its permission; the server is the authority)
Dashboard (`GET /admin/dashboard`) | Employees (existing CRUD + Offboard + Sessions tab) | Attendance today (`GET /admin/attendance/day`) | Requests (existing `/admin/requests`) | Food (existing `/admin/food/daily`) |
Announcements (existing) | Sessions and devices (`/admin/sessions`) | Audit log (existing) | System health (`GET /admin/system/health`) | Company (`/admin/company`) | Export (existing, browser blob download).
Auto-refresh: dashboard every 60 s while the tab is visible; no websockets.

### 2.4 System health data sources
DB ping + latency, Redis ping, migration revision (`alembic_version`), app version, worker heartbeat (worker writes Redis `ec:worker:heartbeat` every 60 s, stale > 180 s),
last DB/uploads backup time (the backup container writes `/status/backup.json` to a new small volume `status`, mounted read-only into api), uploads volume free space (statvfs),
TLS cert expiry (public `fullchain.pem` mounted read-only into api), FCM configured (bool). Nothing secret is returned.

---

## 3. Offboarding (disconnect a leaving employee)

### 3.1 Server
- `POST /admin/employees/{id}/deactivate` gains an optional body `{ "reason": "resigned|terminated|contract_end|absconded|other" }` and stores `deactivated_at/by/reason`. Activate clears them.
  Existing privilege rules, `LAST_ADMIN` and no-self rules apply unchanged.
- Order: in one transaction set inactive, revoke all refresh families, invalidate access tokens, cancel pending requests and collect then delete device tokens. After commit, send a best-effort data-only push `{type:"account_disabled"}` to the collected tokens.
- **Change:** a request with a validly signed access token whose user is inactive/deleted answers **403 `ACCOUNT_INACTIVE`** (was 401 `UNAUTHORIZED`).
  `POST /auth/refresh` with a token revoked because the user is inactive/deleted answers **403 `ACCOUNT_INACTIVE`** (was 401 `SESSION_REVOKED`).
  This is not an enumeration risk because the caller proves possession of that user's token. We use the existing code `ACCOUNT_INACTIVE` (the app already handles it); there is no new `ACCOUNT_DISABLED` code.
- Separate admin action "Sign out all devices" (`POST /admin/employees/{id}/revoke-sessions`) and per-session revoke (`DELETE /admin/sessions/{id}`). These do not deactivate. The app gets `SESSION_REVOKED`, which means a normal sign-out.
- Login of a deactivated user: unchanged (403 `ACCOUNT_INACTIVE` only with the correct password, otherwise 401).

### 3.2 App reaction
Triggers: any API call answers 403 `ACCOUNT_INACTIVE`; refresh answers it; `account_disabled` push (when FCM lands), which only causes an immediate `GET /auth/me`, so a push alone never wipes.
On app resume, if more than 60 s have passed since the last successful call, the app calls `GET /auth/me`.

### 3.3 Wipe (one function `LocalWipe.all(keepServer: bool)`)
Secure storage (tokens, biometric flag, all keys except `server.base_url` + cached branding when `keepServer`), `json_cache`, export/temp folders, picked selfies/attachments,
in-memory and disk image caches (profile photos, announcement images), Riverpod state (invalidate providers / restart the router), shown notifications.
When push lands, also delete the FCM token. Afterwards the app shows **AccountDisabledScreen** ("Your access has been removed. Contact HR.") with a single "Sign in with another account" button that goes to login.
A plain `SESSION_REVOKED` keeps the current behaviour (wipe + login with a "signed out" message).
- Offline device: data cannot be wiped until the phone next talks to the server. Mitigation: new setting `auth.offline_cache_max_hours` (default 72, via `/config`).
  If no authenticated call has succeeded for that long, the app wipes cached data on open and needs a successful online check before showing data again.
- **Remote uninstall is not possible** on Android or iOS for a normally installed (sideloaded/Play) app: the OS gives apps no right to uninstall themselves or be uninstalled remotely.
  Real remote removal needs MDM: Android Enterprise (work profile / fully managed, e.g. Google endpoint management, Intune, or self-hosted Headwind MDM, which fits the on-prem rule) or Apple MDM for managed apps.
  The design is wipe + lock. MDM is an optional future item, documented in DEPLOY.md.

---

## 4. Company registration (single company per deployment)

- Singleton `company` row. No multi-tenancy: one deployment = one company is simpler and safer, and nothing in the requirements needs more. Advise against multi-tenancy.
- **No unauthenticated setup endpoint** (the server is now internet-facing, so an open "register company" call would be a takeover vector). Flow: the first admin is created with the CLI (existing).
  On first login, a holder of `company.manage` with `configured=false` is forced into the **Company setup** wizard (mobile and console): name, then optional logo, then done.
  Optional: `create_admin create ... --company-name "Acme"` CLI flag. Before setup, everyone sees the default "Employee Connect" branding.
- Logo: png/jpeg only (no SVG: script risk), upload <= 2 MB, type checked by magic bytes, re-encoded to PNG with the longest side <= 512 px and metadata stripped, stored unencrypted with purpose `company_logo`. Replacing it deletes the old file.
- Public read (needed on the login screen before auth): `GET /public/company` and `GET /public/company/logo`. They are cacheable (ETag = sha256, `?v=<logo_version>` cache-buster) and rate limited.
- Display: login screen (logo + name), app bar / home header, console header + browser title, server-setup confirmation. The app caches branding (name + logo bytes) locally and refreshes it from `/config.company.logo_version`.

---

## 5. Per-user licensing (seat limit)

### 5.1 Model: vendor-signed license key (the company admin cannot raise it)
- The vendor issues a license token `ECL1.<base64url(payload JSON)>.<base64url(Ed25519 signature)>`. Payload:
  `{ "license_id":"L-2026-0042", "licensed_to":"Acme Pvt Ltd", "deployment_id":"uuid", "max_users":50, "issued_at":"ts", "expires_at":"ts|null", "grace_days":14, "on_expiry":"block_new_users|read_only" }`.
- The backend image contains only the vendor PUBLIC key(s) (`app/core/license_keys.py`, a list to allow rotation). The private key and the issuing tool (`tools/license/issue_license.py`, run by the vendor offline) never ship in `deploy/` or the image.
- `deployment_id` is a random UUID created by migration 0017 (in the `company` row), shown in the console/CLI. The vendor puts it in the token, so a license cannot be copied to another installation.
- Install: console/app Admin > License (`PUT /admin/license`, step-up password) or CLI `python -m scripts.create_admin license install <file>`. The token is verified on install, at startup and on every seat check (cached in memory per process, keyed on the stored token's hash).
  A token with `issued_at` older than the installed one is refused (`LICENSE_OUTDATED`), so an old larger license cannot be re-installed after a downgrade.
- Honest limit: on-prem, a person with root on the server can patch the code. Signing stops changing the limit through the app, API, DB settings or `.env`. Beyond that, enforcement is contractual. No phone-home (data stays on the server).

### 5.2 Rules
- Seats used = `users` with `status='active' AND deleted_at IS NULL` (admins included; deactivated/offboarded/deleted users free their seat immediately).
- Enforced server-side on every seat-consuming path: `POST /admin/employees`, `POST /admin/employees/{id}/activate`, CLI `create_admin create` and the CLI reactivate path.
  The check runs inside the same transaction after `SELECT ... FROM license WHERE id=1 FOR UPDATE`, which serializes concurrent creates so two admins cannot both take the last seat.
  Full -> 409 `USER_LIMIT_REACHED` `details:[{field:"max_users",message:"50"},{field:"active_users",message:"50"}]`, audited (`license.seat_refused`, outcome failure).
- No license installed: built-in default of `UNLICENSED_MAX_USERS` = **50** seats, **no expiry** (value is a code constant, not env). Decided. `expires_at` NULL in a signed license also means it never expires.
- A new license with fewer seats than active users: nothing is deactivated automatically. New creates/activations are blocked until the count is below the limit, and the console shows a banner.
- Expiry: from `expires_at` until `+grace_days` the status is `grace` (banner + daily `admin_alert` push to `license.manage` holders, nothing blocked). After grace the status is `expired` and behaviour follows the signed `on_expiry`:
  - `block_new_users` (default): only seat-consuming actions are refused with 403 `LICENSE_EXPIRED`. Employees keep using the app normally.
  - `read_only`: every write under `/admin/*` (except `deactivate`, `delete`, `revoke-sessions`, `PUT /admin/license`) and all employee write endpoints answer 403 `LICENSE_EXPIRED`. Reads, login/refresh/logout and change-password keep working, so data is never held hostage and exports still work.
  We recommend `block_new_users`: locking attendance/leave hurts employees, not the buyer.
- An invalid stored token (bad signature or wrong `deployment_id`, e.g. after a DB copy) behaves like `unlicensed` and is logged at ERROR.

### 5.3 Display
Console dashboard + Admin > License: "Seats 47 / 50", status chip (valid/grace/expired/unlicensed), expiry date, `deployment_id` (copy button for the vendor), install-license form.
The employee-create form shows seats left and disables Save at 0 (the server still decides).

---

## 6. Passwords: admin reset, self change, forgot password

What exists (keep): (a) **admin reset** `POST /admin/employees/{id}/reset-password` generates a temporary password (shown once, valid `TEMP_PASSWORD_HOURS`=72), sets `must_change_password`, revokes all sessions/access tokens, unlocks, marks open requests `handled`, requires step-up for privileged targets, is audited and limited to 10/min (fail closed).
(b) **self change** `POST /auth/change-password` (policy, `PASSWORD_REUSED`, revokes other sessions, returns a new pair, `CURRENT_PASSWORD_INVALID` counts toward lockout once).
(c) **forgot** `POST /auth/forgot-password` always 202 with min latency and creates an Admin/HR queue item. `/auth/reset-password` is currently a stub (400).

Additions (small, no SMTP needed):
- Admin console + mobile: "Reset password" also available from the Password-reset-requests inbox and the employee detail. The temp-password dialog has copy + FLAG_SECURE on mobile, and the console clears it from memory on close.
  Guidance text: hand it over in person or by phone, never by chat/email.
- New push `type:"reset_request"` (id = requester user id, name + employee id only) to active holders of `employee.reset_password` that may act on that user (privilege rules). At most 1 per requester per hour (Redis), best effort. The dashboard shows the open-request count.
- Forgot-password per-employee-id limit: 3 per hour and 10 per day per employee id (Redis, silent: still 202 so nothing is revealed); existing per-IP nginx/app limits stay.
- The app's change-password screen also offers "Sign out other devices" (already implied: change-password revokes them; just state it in the UI).

**User decision: admin reset only.** No email/OTP/SMS self-service and no SMTP dependency. Employees keep self change-password in the app.
- `POST /auth/reset-password` (the OTP stub) is REMOVED (route deleted, so it answers 404). `/auth/forgot-password` only queues a request for Admin/HR, and the contract text about OTP/SMTP mail is dropped.
- Migration 0017 drops the unused `password_reset_requests.otp_hash`, `otp_expires_at`, `otp_attempts` columns (and `ck_password_reset_requests_attempts`).
  `SMTP_*` settings stay optional in config (unused by auth). The app's forgot-password screen only says "Your request was sent to HR/Admin. They will give you a temporary password."
- Recovery for admins themselves: another admin resets them, or the server CLI `create_admin reset <id>` (break-glass, existing).

---

## 7. Database (migration `0017_company_console_offboarding`)
- `company`: `id smallint PK DEFAULT 1 CHECK (id = 1)`; `deployment_id uuid NOT NULL DEFAULT gen_random_uuid()`; `name text NULL CHECK (name IS NULL OR char_length(btrim(name)) BETWEEN 2 AND 100)`;
  `logo_file_id uuid NULL FK files ON DELETE SET NULL`; `logo_version integer NOT NULL DEFAULT 0 CHECK (>= 0)`; `setup_completed_at timestamptz`; `CHECK (setup_completed_at IS NULL OR name IS NOT NULL)`;
  `created_by`, `updated_by uuid NULL FK users RESTRICT`; `created_at`, `updated_at` + `set_updated_at` trigger.
  The migration INSERTs the single row (generates `deployment_id`). "Configured" = `setup_completed_at IS NOT NULL`.
- `license`: `id smallint PK DEFAULT 1 CHECK (id = 1)`; `token text NOT NULL CHECK (char_length(token) <= 4096)`; `license_id text NOT NULL`; `licensed_to text NOT NULL`; `max_users integer NOT NULL CHECK (max_users BETWEEN 1 AND 100000)`;
  `issued_at timestamptz NOT NULL`; `expires_at timestamptz NULL CHECK (expires_at IS NULL OR expires_at > issued_at)`; `grace_days integer NOT NULL DEFAULT 14 CHECK (BETWEEN 0 AND 90)`;
  `on_expiry text NOT NULL DEFAULT 'block_new_users' CHECK (IN ('block_new_users','read_only'))`; `installed_at timestamptz NOT NULL DEFAULT now()`; `installed_by uuid NULL FK users RESTRICT` (null = CLI).
  The parsed columns are a display copy only: the server always re-verifies `token`. No row means unlicensed. The seat lock uses `SELECT ... FOR UPDATE` on this row, or on `company` when there is no license row.
- `files.ck_files_purpose`: add `'company_logo'`.
- `password_reset_requests`: drop `otp_hash`, `otp_expires_at`, `otp_attempts` + `ck_password_reset_requests_attempts` (admin-reset only).
- `users`: `deactivated_at timestamptz`, `deactivated_by uuid FK users RESTRICT`, `deactivation_reason text CHECK (IN ('resigned','terminated','contract_end','absconded','other'))`;
  `CHECK ((status='inactive') OR (deactivated_at IS NULL))`. Backfill: inactive rows get `deactivated_at = coalesce(deleted_at, updated_at)`, reason `other`.
- `refresh_tokens`: `client_type text NOT NULL DEFAULT 'mobile' CHECK (IN ('mobile','web'))`, `app_version text CHECK (char_length <= 32)`, `platform text CHECK (IN ('android','ios','web'))`;
  partial index `ix_refresh_tokens_live ON (user_id, family_id) WHERE revoked_at IS NULL AND replaced_by IS NULL`.
- Permissions seed: `company.manage` (admin), `console.access` (admin only per decision; the migration also granted hr, remove that grant), `session.manage` (admin, hr; privilege rules restrict HR to plain employees), `system.view` (admin), `license.manage` (admin).
- Settings seed: `console.session_max_hours` 12 (1..24), `console.inactivity_timeout_minutes` 15 (5..60), `auth.offline_cache_max_hours` 72 (0 = off, else 12..720).
  No license values in `settings` (they must not be admin-editable).
- Regenerate `schema.sql`.

## 8. Security considerations
- License: Ed25519 verification with a constant-time library (`cryptography`). The payload is parsed only after the signature verifies, with a size cap of 4 KB. The vendor private key never touches the repo/server.
  Seat check and insert happen in one transaction under a row lock. `PUT /admin/license` requires step-up and is audited (`license.install`, license_id/max_users/expiry, never the full token).
- Passwords: temporary passwords are never logged/emailed and expire after `TEMP_PASSWORD_HOURS`. There is no self-service reset path (no OTP), so an internet attacker cannot take over an account through email.
  Forgot-password always answers 202 with min latency and has a per-id limit. Every admin reset revokes all sessions and is step-up protected for privileged targets. Reset-request pushes carry names only.
- Internet exposure raises the stakes. Reviewers must confirm that 8000/5432/6379 are unreachable and that unknown Host/SNI is rejected. The admin API stays permission-gated as today.
- The console is a new browser attack surface. Strict CSP for `/console/` only: `default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`.
  No CDN (fonts/CanvasKit served locally). The access token stays in memory and the refresh token in an HttpOnly cookie; CSRF guards as in 2.2.
- `device_name` / `app_version` / company name are user-supplied. Strip control characters, enforce length limits, and render them as plain text only.
- Public company endpoints reveal only name + logo. They are rate limited and the logo is re-encoded (no polyglots).
- Wipe must be complete and idempotent. A push never wipes without server confirmation. Changing the server wipes everything.
- Sessions list: IPs are personal data, visible only with `session.manage`. Every read of `/admin/sessions` and every revoke is audited.
- Cloudflare option (if ever chosen) must be recorded as an accepted risk (TLS termination by a third party, pinning disabled).

## 9. Tasks (in order)
**database-engineer**
1. Migration 0017 per section 7 (+ downgrade): company row insert with `deployment_id`, `license` table, users/refresh_tokens columns, files purpose, drop OTP columns.
   Seed permissions/settings/grants idempotently in `seed.py`, regenerate `schema.sql`.
2. Verify `LAST_ADMIN` row-lock logic is unaffected by the new users columns, and that the seat lock (license/company row `FOR UPDATE`) cannot deadlock with it (always lock license/company first).

**backend-developer**
1. `company` service + routes: `/public/company`, `/public/company/logo`, `/admin/company` GET/PUT, logo POST/DELETE. Add `company` to `/auth/me` and `/config`. Add `--company-name` to `create_admin`.
2. Auth dependency + refresh: return 403 `ACCOUNT_INACTIVE` for inactive/deleted users (section 3.1). Store `client_type/platform/app_version` at login.
3. Deactivate body/reason + columns. Send the post-commit `account_disabled` push (no-op until FCM is configured). Add `revoke-sessions`, `/admin/sessions` list/revoke (privilege rules, audit).
4. Web auth endpoints (cookie, CSRF guards, `console.access`, web lifetime/inactivity). Settings validation for the 3 new keys. Return `auth.offline_cache_max_hours` in `/config`.
5. `/admin/dashboard`, `/admin/attendance/day`, `/admin/system/health` (worker heartbeat in `app.jobs.scheduler`, read status file, cert expiry).
6. Rate limits: public company 60/min/IP, web login uses the same per-employee and per-IP limits as mobile login, sessions list 30/min, dashboard 60/min, health 30/min.
7. Licensing: `app/core/license_keys.py` (vendor public keys), verifier (Ed25519, `deployment_id`, outdated check), seat check in create/activate/CLI under row lock, `USER_LIMIT_REACHED`/`LICENSE_EXPIRED`/`LICENSE_INVALID`/`LICENSE_OUTDATED`,
   `read_only` middleware for `/admin/*` + employee writes when signed `on_expiry=read_only` and expired, `GET/PUT /admin/license`, CLI `license install|show`, daily grace/expiry `admin_alert` job, seats block in `/admin/dashboard`.
   Vendor-side `tools/license/issue_license.py` + `gen_vendor_key.py` (kept outside `deploy/`; private key never committed).
8. Passwords: delete the `/auth/reset-password` route and OTP code paths, add the per-id forgot limit (3/h, 10/day, silent), `reset_request` push to eligible admins (1/requester/h).
9. Update api-contract section 12 from PROPOSED to final once implemented; tests for all of the above.

**flutter-developer**
1. Server setup screen + "Change server" + base URL resolution (section 1.3). Dio base URL from a provider, not the const.
2. Branding: cached company name/logo on login, app bar/home. Company setup wizard + Admin > Company screen (`company.manage`).
3. `LocalWipe.all` (section 3.3), AccountDisabledScreen, resume `/auth/me` check, offline-cache max age. Route `ACCOUNT_INACTIVE` (any call/refresh) to wipe + that screen.
4. Admin > Employee: Offboard action (reason picker, confirm), Sessions tab with revoke, "Sign out all devices".
5. Console: `main_console.dart`, web auth repository (cookie refresh, in-memory access token), shell + pages in 2.3, conditional imports so no mobile plugin is compiled for web.
6. When push lands: handle `account_disabled` (verify via `/auth/me`) and delete the FCM token on wipe.
7. License: Admin > License screen (mobile + console: seats, status, expiry, deployment_id copy, install with step-up), seats-left hint on employee create, map `USER_LIMIT_REACHED`/`LICENSE_EXPIRED` to clear messages, grace/expired banner.
8. Passwords: remove OTP entry UI from forgot-password (message only), reset-password action in the console (employee detail + requests inbox), temp-password dialog (copy, FLAG_SECURE on mobile, cleared on close).

**devops-engineer**
1. nginx: `server_name ${PUBLIC_HOSTNAME}` + reject-unknown default server; ACME location on :80; `/console/` static location with its own CSP/cache headers (shared `security-headers` include, because `add_header` in a location drops server-level headers);
   `location = /api/public/company/logo` (cacheable); exact-match `location = /api/admin/company/logo` (3m body, upload zone); `/api/auth/web/` in the `authapi` zone; optional `CONSOLE_ALLOW_CIDRS` allowlist.
2. `deploy/console.Dockerfile` (multi-stage Flutter build -> nginx-unprivileged image with `/console`). Compose: `status` volume (backup rw, api ro), cert mounted ro into api, `.env` `PUBLIC_HOSTNAME`, `PUBLIC_ORIGIN`, `CONSOLE_ALLOW_CIDRS`.
3. Backup entrypoint writes `/status/backup.json` (`db_last_success_at`, `uploads_last_success_at`).
4. DEPLOY.md: new "Remote access" section (options A/D/B with the table in 1.1, port-forward steps, DDNS, split DNS, certbot `--reuse-key` + deploy hook, pin workflow), rewrite firewall section 6 for internet exposure, WireGuard relay recipe for option D, console build/serve, MDM note (3.3).
5. Optional: fail2ban jail + uptime check example.

**security-reviewer**
Internet exposure scan (only 443/80, unknown Host rejected, no /docs). Pin rotation with `--reuse-key`. Console CSP/XSS, cookie flags, CSRF on `/auth/web/*`, token never in web storage.
Public company endpoints (enumeration, logo polyglots, rate limit). `ACCOUNT_INACTIVE` change leaks nothing without a valid token. Wipe completeness on device (file system + secure storage inspection).
Sessions IDOR/privilege rules (HR vs admin). Audit coverage for offboard/revoke/company changes. License: forged/edited/other-deployment/older tokens refused, the limit cannot be raised via API/settings/env, race on the last seat.
Passwords: no reset path left without an admin, forgot-password timing/enumeration, temp password never in logs/audit. Write `docs/reviews/11-remote-console-offboarding-security.md`.

**qa-tester**
Backend: company CRUD + public reads before/after setup, logo validation (svg/oversize/polyglot), deactivate -> next access call 403 + refresh 403 + login rules, revoke-sessions vs deactivate codes, sessions list scope (HR cannot see/revoke admin),
web auth cookie/CSRF/`console.access`, dashboard blocks null without permission, attendance/day statuses, health with Redis/worker down, new settings ranges.
License: unlicensed default 50, create/activate at limit -> 409, deactivate frees a seat, concurrent last-seat creates (exactly one wins), grace vs expired, both `on_expiry` modes (reads/login/change-password/deactivate still work in read_only), tampered/other-deployment/outdated tokens.
Passwords: admin reset (temp expiry, sessions revoked, step-up, own-account refused), self change, forgot per-id limit still 202, `/auth/reset-password` is 404.
Flutter: server setup validation (http rejected in release, wrong product rejected), branding refresh on `logo_version`, wipe on `ACCOUNT_INACTIVE` (files gone, server URL kept), resume check, offline max age.
Manual: phone on mobile data reaches the server via the public hostname; split DNS on office Wi-Fi; cert renewal keeps pins valid.

## 10. Open questions for the user
1. **Network**: does the office internet connection have a public IPv4 address (static or dynamic), or is it behind CGNAT? Do you own a domain name (e.g. `company.com`) we can add `hr.` to? (This decides between options A and D.)
2. **Cloudflare**: do you want to rule out Cloudflare Tunnel (recommended), given that it decrypts traffic at Cloudflare and requires dropping certificate pinning?
3. **Console reach**: should the admin web console be reachable from the internet, or only from the office LAN/admin VPN? (Recommended: LAN/VPN only; the in-app admin area stays usable from anywhere.) DECIDED: internet-reachable, Admin only.
4. **MDM**: does the company plan to use company-managed phones (Android Enterprise/MDM)? If not, wipe + lock is the final design and remote uninstall stays out of scope.
5. **Licensing policy**: (a) Do you confirm vendor-signed license keys (you hold the private key and issue a key per customer using their `deployment_id`)?
   (b) What is the free seat count with no license installed (DECIDED: 50, no expiry)? (c) What is the default behaviour after expiry (proposed `block_new_users`; `read_only` available per contract)?
   (d) What is the grace period (proposed 14 days)? (e) Should admins count as seats (DECIDED: yes)?

Decided: password recovery is admin-reset only (no email/OTP/SMS self-service); employees keep in-app change-password.
