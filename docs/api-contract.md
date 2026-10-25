# Employee Connect - API Contract (single source of truth)

Backend (FastAPI) and frontend (Flutter) both build against this file. Any change to an endpoint
must be made here first. Version 1.0.

## 0. Conventions

- Base URL: `https://<server>/api`. HTTPS only. JSON (`application/json`) unless stated. Field names `snake_case`.
- Auth: `Authorization: Bearer <access_token>` on every endpoint except those marked **Public**.
- IDs: UUID strings, except `employee_id` (business key, string, e.g. `EMP001`).
- Dates `YYYY-MM-DD`; times `HH:MM` (24h, office timezone); timestamps ISO-8601 UTC with `Z`.
  "Office timezone" is `office.timezone` setting (default `Asia/Kolkata`); `work_date` is computed in it.
- Decimals for leave days are JSON numbers with one decimal (e.g. `0.5`).
- Roles: `admin`, `hr`, `manager`, `employee`. "Perm" column = permission code (see 00-foundation.md section 6).
  **Self** = any authenticated user acting on their own data. **Team** = users whose `manager_id` is the caller.
- Forced password change: while `must_change_password=true` every endpoint except `/auth/change-password`,
  `/auth/logout`, `/auth/me` returns `403 PASSWORD_CHANGE_REQUIRED`.

### Error format (all non-2xx)
```json
{ "error": { "code": "VALIDATION_ERROR", "message": "Human readable text",
             "details": [ { "field": "to_date", "message": "must be >= from_date" } ],
             "request_id": "c0a8..." } }
```
`details` is optional (array, may be empty). Codes -> HTTP status:
`VALIDATION_ERROR` 422, `BAD_REQUEST` 400, `UNAUTHORIZED` 401, `INVALID_CREDENTIALS` 401, `TOKEN_EXPIRED` 401,
`TOKEN_REUSED` 401, `SESSION_REVOKED` 401, `CURRENT_PASSWORD_INVALID` 400, `FORBIDDEN` 403, `PASSWORD_CHANGE_REQUIRED` 403, `ACCOUNT_INACTIVE` 403, `NOT_FOUND` 404,
`CONFLICT` 409, `ACCOUNT_LOCKED` 423 (details: `locked_until`), `RATE_LIMITED` 429 (header `Retry-After`),
`USER_LIMIT_REACHED` 409, `LICENSE_OUTDATED` 409, `LICENSE_EXPIRED` 403, `LICENSE_INVALID` 422 (section 12.6),
`DATA_UNREADABLE` / `FILE_UNAVAILABLE` / `TEMPORARILY_UNAVAILABLE` 503 (stored data undecryptable with the configured keys; limiter down on uploads), `INTERNAL_ERROR` 500. Domain codes (all 409/422) are listed per module.

### Pagination
Query `?page=1&page_size=20` (default 20, max 100). Response envelope:
```json
{ "items": [ ... ], "page": 1, "page_size": 20, "total": 134 }
```
Un-paginated lists (small, bounded) return `{ "items": [ ... ] }`. Sorting: `?sort=field` / `?sort=-field` where noted.

### Common objects
```json
UserSummary: { "id": "uuid", "employee_id": "EMP001", "full_name": "A B", "department": "IT" | null, "photo_url": "/api/files/<id>" | null }
LatLng:      { "lat": 12.9716, "lng": 77.5946 }
```

---

## 1. Auth (module 01)

| Method | Path | Access | Notes |
|---|---|---|---|
| POST | /auth/login | Public | rate limited |
| POST | /auth/refresh | Public (refresh token) | rotating |
| POST | /auth/logout | Self | revokes presented refresh token |
| POST | /auth/logout-all | Self | revokes all refresh tokens of the user |
| POST | /auth/change-password | Self | also used for forced change |
| POST | /auth/forgot-password | Public | always 202; queues a request for Admin/HR (no OTP, no email) |
| POST | /auth/web/login, /auth/web/refresh, /auth/web/logout, /auth/web/change-password | Public / cookie / Self | admin console only (section 12.3) |
| GET | /public/company, /public/company/logo | Public | branding (section 12.2) |
| GET | /auth/me | Self | user + permissions |
| GET | /config | Self | client configuration |
| POST | /me/device-tokens | Self | register FCM token |
| DELETE | /me/device-tokens/{fcm_token} | Self | on logout |

**POST /auth/login**
```json
// req
{ "employee_id": "EMP001", "password": "string", "device_id": "uuid-or-string", "device_name": "Pixel 8",
  "app_version": "1.4.0" /* optional, <= 32 */, "platform": "android" /* optional: android|ios */ }
// 200
{ "access_token": "jwt", "refresh_token": "opaque", "token_type": "Bearer", "expires_in": 900,
  "must_change_password": false,
  "user": { "id": "uuid", "employee_id": "EMP001", "full_name": "A B", "role": "employee",
            "permissions": ["..."] } }
```
Errors: 401 `INVALID_CREDENTIALS` (same message and timing for unknown ID / wrong password / **locked account with a
wrong password**), 423 `ACCOUNT_LOCKED` (`details:[{field:"locked_until",message:"2026-10-21T10:15:00Z"}]`),
403 `ACCOUNT_INACTIVE`, 403 `TEMP_PASSWORD_EXPIRED` (only with the correct password: the admin-issued temporary password is older than `TEMP_PASSWORD_HOURS`, default 72; the user must ask an administrator for a new one; the attempt is audited as `auth.login_failed` `after.reason="temp_password_expired"`), 429 `RATE_LIMITED`.
**Change (anti-enumeration):** 423 and 403 `ACCOUNT_INACTIVE` are returned ONLY when the supplied password is correct.
The 5th wrong attempt that locks the account still answers 401 `INVALID_CREDENTIALS`; the client learns about the lock
(and the countdown) when the user next enters the correct password. Do not show "locked" UI from a 401.
Auto-unlock after `LOCKOUT_MINUTES`; Admin/HR `unlock` or the server CLI `python -m scripts.create_admin unlock <id>` clear it.

**POST /auth/refresh** `{ "refresh_token": "opaque" }` -> same shape as login (without `user`, `must_change_password` included;
the client must store the flag).
Errors (all 401): `TOKEN_EXPIRED` (refresh token expired OR the session hit its absolute lifetime, `SESSION_MAX_DAYS`, default 90) |
`SESSION_REVOKED` (revoked by logout, logout-all, password change, admin reset, deactivation: "signed out elsewhere", NOT a
theft signal) | `TOKEN_REUSED` (a rotated token was replayed outside the grace window; whole family revoked, audited) |
`UNAUTHORIZED` (unknown token). **403 `ACCOUNT_INACTIVE`** for any token of a deactivated/deleted user (checked before `SESSION_REVOKED`; the app wipes local data and shows "access removed").
Any of these = end the session. 429/5xx/network errors must NOT end the session.
Rotation grace: re-presenting the immediately previous refresh token within `REFRESH_GRACE_SECONDS` (15 s) while its successor
was never used returns a fresh pair instead of `TOKEN_REUSED` (covers a lost response). Rotation never extends a session past
`SESSION_MAX_DAYS` since the original login.
**Access tokens are invalidated server side** on logout, logout-all, change-password, admin reset-password, deactivate and
delete: the next call with an old access token returns 401 `UNAUTHORIZED` (message "Session no longer valid"). Clients
already refresh+replay once on 401 `UNAUTHORIZED`/`TOKEN_EXPIRED`; after logout/password change that refresh fails with
`SESSION_REVOKED` and the session ends. Access-token JWTs now carry `iss=employeeconnect` and `aud=employeeconnect-api`.

**POST /auth/logout** `{ "refresh_token": "opaque" }` -> 204. **POST /auth/logout-all** -> 204.

**POST /auth/change-password** `{ "current_password": "...", "new_password": "..." }` -> 200 new token pair
(login shape, `must_change_password:false`); all other sessions revoked and their access tokens invalidated. Errors:
**400 `CURRENT_PASSWORD_INVALID`** (wrong current password; was 401 `INVALID_CREDENTIALS`, which clients refreshed and replayed,
double-counting toward lockout; it now counts exactly once, never replay it), 423 `ACCOUNT_LOCKED` (the attempt that reaches the
threshold, or while locked), 422 `VALIDATION_ERROR` (policy; `details` lists failed rules), 422 `PASSWORD_REUSED` (same as current).

**POST /auth/forgot-password** `{ "employee_id": "EMP001" }` -> 202 `{ "message": "If the account exists, a request was sent." }`
(creates a `password_reset_requests` row for Admin/HR; nothing is emailed). The response always takes at least
`FORGOT_MIN_LATENCY_MS` (300 ms) so timing does not reveal whether the id exists. Silent per-employee-id limits: 3 per hour and 10 per day (Redis;
over the limit the call is still 202 and nothing is queued), plus the per-IP limit. A new request pushes `type:"reset_request"` (id = requester, name + employee id only) to the
active holders of `employee.reset_password` who may act on that account, at most once per requester per hour. Admin/HR list the queue with
`GET /admin/password-reset-requests` (section 9).
**Password recovery is admin reset only** (decision): `POST /auth/reset-password` no longer exists (404) and `INVALID_OTP` is retired. Employees change their own password with `POST /auth/change-password`.

**Client IP / proxies (ops note).** `X-Real-IP` / `X-Forwarded-For` are used for audit and IP rate limits only when the direct
peer is inside `TRUSTED_PROXIES` (CIDR list; replaces `TRUST_PROXY_HEADERS`). nginx must overwrite the header
(`proxy_set_header X-Real-IP $remote_addr;`), must be the only way in (do not publish port 8000), and `TRUSTED_PROXIES` must be
the nginx network. IPv6 clients are rate-limited per /64. `X-Request-ID` is accepted only if it matches `^[A-Za-z0-9-]{8,64}$`,
else a server id is generated.

**GET /auth/me** -> `{ "id","employee_id","full_name","role","permissions":[...],"must_change_password":false,"department":{...}|null,"company":{name,logo_url,logo_version,configured} }`

**GET /config** (`auth.inactivity_timeout_minutes` is clamped server side to 1-30) ->
```json
{ "food": { "cutoff_mode": "previous_day", "cutoff_time": "18:00" },
  "office": { "lat": 12.97, "lng": 77.59, "radius_m": 150, "timezone": "Asia/Kolkata" },
  "attendance": { "selfie_required": false },
  "auth": { "inactivity_timeout_minutes": 5, "offline_cache_max_hours": 72 },
  "company": { "name": "Acme"|null, "logo_url": "/api/public/company/logo?v=3"|null, "logo_version": 3, "configured": true },
  "permission": { "monthly_count_limit": 3, "monthly_minutes_limit": 240, "max_minutes_per_request": 120 } }
```
**POST /me/device-tokens** `{ "fcm_token": "...", "platform": "android|ios", "device_id": "..." }` -> 204 (upsert).

---

## 2. Profile (module 07 plan; contract here)

| Method | Path | Access / Perm |
|---|---|---|
| GET | /me/profile | Self |
| PUT | /me/profile | Self |
| POST | /me/profile/photo | Self (multipart `file`, jpeg/png <= 5 MB) |
| GET | /home | Self |
| GET | /home/celebrations?days=7 | Self |
| GET | /files/{id} | Owner, or holder of a perm that grants the parent resource |
| POST | /files | Self (multipart: `file`, `purpose`) |

Profile object:
```json
{ "user_id": "uuid", "employee_id": "EMP001", "full_name": "A B", "department": {"id":"uuid","name":"IT"},
  "role": "employee", "official_email": "a@corp.com", "photo_url": "/api/files/<id>" | null,
  "dob": "1995-04-12" | null, "wedding_anniversary": "2020-02-01" | null,
  "personal_email": "x@gmail.com" | null, "phone": "+919999999999" | null,
  "emergency_contact": { "name": "..", "relationship": "..", "phone": "+91.." } | null,
  "blood_group": "O+" | null, "address": "..." | null,
  "show_celebrations": false }
```
**PUT /me/profile** body: any subset of `dob, wedding_anniversary, personal_email, phone, emergency_contact,
blood_group, address`. `full_name`, `official_email`, `department` are read-only for self (Admin/HR edit via
admin employees API). `blood_group` in `A+,A-,B+,B-,AB+,AB-,O+,O-`.
-> 200 profile object. Also accepts `show_celebrations` (bool). It is an OPT-IN, default `false` (migration 0014 reset every existing row to `false`): only users who
set it to `true` appear in the birthday/anniversary feed of their colleagues. Only sent keys change; an explicit
`null` clears a field. Validation (422 `VALIDATION_ERROR`): phone `^\+?[0-9]{8,15}$` (spaces, dashes,
dots, brackets are stripped first), valid emails, `dob` past and age 16-100, `wedding_anniversary` past, `address` <= 300 chars, `emergency_contact`
needs `name` + `phone` (`relationship` optional). Unknown/read-only keys (`full_name`,
`official_email`, `department`, `role`, `user_id`) -> 422. Stored values are encrypted; if they cannot be decrypted (key mismatch) GET returns those
fields as null (logged), PUT answers 503 `DATA_UNREADABLE`. Self edits are audited as `profile.update` with field NAMES only.
**POST /me/profile/photo** multipart `file` (jpeg/png, <= 5 MB; type from magic bytes) -> 200 full profile object (new `photo_url`). The image is
re-encoded to at most 512 px on the longest side (EXIF/GPS dropped) and the previous photo is deleted. Shares the per-user upload limiter of
`POST /files` (429 `RATE_LIMITED`, 503 `TEMPORARILY_UNAVAILABLE` if Redis is down). Errors: 422 `FILE_TOO_LARGE`, 422 `FILE_TYPE_NOT_ALLOWED`.
Visible via `GET /files/{id}` to every signed-in user, but only while it is an active user's CURRENT photo (a replaced photo is deleted). nginx allows
6 MB bodies on this exact route (deploy/nginx.conf). `profile_photo` is NOT accepted by `POST /files` (422 `VALIDATION_ERROR`, unsupported purpose).
**POST /files** -> `{ "id":"uuid","url":"/api/files/<id>","content_type":"image/jpeg","size_bytes":12345 }`.
`purpose` in `selfie|leave_attachment|announcement_image|announcement_pdf` (profile photos only through `POST /me/profile/photo`). Limits: 10 MB; jpeg/png/pdf only;
content sniffed server side. Errors: 422 `FILE_TOO_LARGE`, 422 `FILE_TYPE_NOT_ALLOWED`.
Implementation notes (v1): type is decided by magic bytes (client Content-Type/filename ignored). Per purpose: `selfie|profile_photo|announcement_image`
= jpeg/png, max 5 MB; `leave_attachment` = jpeg/png/pdf, max 10 MB; `announcement_pdf` = pdf, max 10 MB (all also capped by `MAX_UPLOAD_MB`).
Images are re-encoded (EXIF/GPS dropped, longest side <= 2048 px), so `size_bytes`/`content_type` describe the stored file.
`announcement_image|announcement_pdf` uploads need `announcement.manage` (else 403). Upload rate limit 20/min/user (429 `RATE_LIMITED`). The limiter fails CLOSED for uploads (503 `TEMPORARILY_UNAVAILABLE` if Redis is down). Images over `MAX_IMAGE_PIXELS` (16 Mpx decoded) -> 422
`FILE_TYPE_NOT_ALLOWED`; PDFs containing /JavaScript, /JS, /Launch, /EmbeddedFile, /RichMedia, /OpenAction, /AA, /SubmitForm, /GoToR, /ImportData or /XFA are refused (same code; a best-effort
screen on the raw bytes: names inside compressed object streams are not visible, so clients must still treat PDFs as untrusted content). Denied reads of existing files
are audited (`file.access_denied`); an unreadable encrypted blob (key mismatch) returns 503 `FILE_UNAVAILABLE`.
**GET /files/{id}** returns the bytes with the stored `Content-Type`, `Content-Disposition` (`inline` for images, `attachment` for pdf),
`X-Content-Type-Options: nosniff`, `Cache-Control: private, no-store`; send the bearer token (use an authenticated image loader, not a bare URL).
Access: owner always; `profile_photo` any signed-in user while it is an active user's current photo; `selfie` `attendance.view_all`, or `attendance.view_team` for direct reports;
`leave_attachment` ONLY through a leave request that carries the file and that the caller may see (`request.view_all`/`approve_all` = any requester,
`request.view_team`/`approve_team` = requester currently reports to the caller); a file never attached to a request is owner-only; `announcement_*` any user while the announcement is
published/targeted at them, `announcement.manage` always. Anything not allowed (or unknown) is 404 `NOT_FOUND`, never 403. Selfies and leave
attachments are encrypted at rest. `UserSummary.photo_url` is `/api/files/<id>` once the profile photo is set (via `POST /me/profile/photo`).
**GET /home** -> (every key is always present; a part that fails server-side is `null`, `celebrations` is then `[]`)
```json
{ "today_attendance": { "check_in_at": "ts|null", "check_out_at": "ts|null", "status": "present|late|half_day|null" },
  "leave_balances": [ {"leave_type":"Casual","remaining":8.0} ],
  "permission_remaining": { "count": 2, "minutes": 180 },
  "unread_announcements": 3, "pending_requests": 1,
  "food_today": { "date":"2026-10-21","need_food": null, "can_change": true },
  "celebrations": [ { "user": UserSummary, "type": "birthday|anniversary", "date": "2026-10-22", "years": null } ] }
```
`pending_requests` = the caller's OWN leave + permission requests in status `pending`. `permission_remaining` = this month's remaining count/minutes.
`celebrations` = the next 7 days (same items as `/home/celebrations?days=7`). Rate limit 60/min.
**GET /home/celebrations?days=7** -> `{ "items": [ { "user": UserSummary, "type", "date", "years" } ] }` sorted by date (`days` 1-30, else 422).
`date` is the upcoming occurrence (Feb 29 birthdays fall on Feb 28 in non-leap years; today counts). `years` is the number of years for
`anniversary` and ALWAYS `null` for `birthday` (never reveals age or birth year). Only active users with `show_celebrations=true` (opt-in, default
false) appear. Cached in Redis per office day and cache generation; the generation is bumped when anyone changes dob/anniversary/opt-in, and the opt-in is
re-checked against the database on every read, so a failed cache invalidation cannot keep showing someone who opted out.

---

## 3. Attendance (module 02)

| Method | Path | Access / Perm |
|---|---|---|
| POST | /attendance/check-in | Self |
| POST | /attendance/check-out | Self |
| GET | /attendance/today | Self |
| GET | /attendance?from=&to=&page= | Self |
| GET | /attendance/calendar?month=2026-10 | Self |
| GET | /attendance/summary?month=2026-10 | Self |
| GET | /attendance/team?date=&from=&to=&department_id=&employee_id=&page= | `attendance.view_team` / `attendance.view_all` |

**POST /attendance/check-in**
```json
{ "lat": 12.9716, "lng": 77.5946, "accuracy_m": 12.5, "fix_time": "2026-10-21T03:30:00Z",
  "selfie_file_id": "uuid|null", "client_time": "ts", "is_mock_location": false }
```
`accuracy_m` and `fix_time` (platform timestamp of the GPS fix, UTC ISO-8601) are REQUIRED on check-in, and on check-out when
`attendance.checkout_geofence=true` (else 422 `VALIDATION_ERROR`, `details` names the missing field).
-> 201 `AttendanceRecord`:
```json
{ "id":"uuid","work_date":"2026-10-21","check_in_at":"ts","check_out_at":null,
  "status":"present|late|half_day","minutes_late":0,"worked_minutes":null,
  "within_fence":true,"distance_m":null,"selfie_url":"/api/files/<id>|null","shift":{"start":"09:30","end":"18:30","grace_minutes":10} }
```
Errors: 409 `ALREADY_CHECKED_IN`, 409 `ON_LEAVE`, 422 `OUTSIDE_GEOFENCE` (details only `[{"field":"within_fence","message":"false"}]`;
NO distance/radius), 422 `SELFIE_REQUIRED`, 422 `LOCATION_ACCURACY_LOW` (accuracy > `attendance.max_accuracy_m`, default 100),
422 `LOCATION_FIX_STALE` (`fix_time` older than `attendance.max_fix_age_s` = 60 or more than `attendance.max_fix_future_s` = 30 ahead of
server time: ask the user to wait for a fresh fix), 422 `MOCK_LOCATION_DETECTED` (`is_mock_location=true` while
`attendance.reject_mock_location` is on, the default), 422 `SELFIE_STALE` (selfie uploaded more than `attendance.selfie_max_age_s` = 600 s ago),
422 `SELFIE_REUSED` (the file already backs another punch: upload a new selfie for every punch), 422 `NO_SHIFT_ASSIGNED`.
Rejected punches are audited (`attendance.punch_rejected`, reason only) and counted: after `PUNCH_FAIL_LIMIT` (default 5) rejected
punches (geofence/accuracy/stale fix/mock) in `PUNCH_FAIL_WINDOW_SECONDS` (600) the user gets 429 `RATE_LIMITED` with `Retry-After`; a
successful punch resets the counter.
**POST /attendance/check-out** same body -> 200 record. Errors: 409 `NOT_CHECKED_IN`, 409 `ALREADY_CHECKED_OUT`, 422 `OUTSIDE_GEOFENCE` (only if `attendance.checkout_geofence=true`).
**GET /attendance** -> paginated `AttendanceRecord`. **GET /attendance/today** -> record or `{ "record": null }`.
**GET /attendance/calendar** ->
`{ "month":"2026-10","days":[ {"date":"2026-10-01","status":"present|late|half_day|absent|on_leave|holiday|weekly_off|future","holiday_name":null,"leave_type":null} ] }`
**GET /attendance/summary** -> `{ "month","present":18,"late":2,"absent":1,"on_leave":2,"half_day":0,"total_worked_minutes":9600 }`
**GET /attendance/team** -> paginated records with `user: UserSummary`.

Implementation notes (v1):
- Domain error `details` are `[{field,message}]`: `ALREADY_CHECKED_IN` -> `[{"field":"record_id","message":"<existing id>"}]`;
  `OUTSIDE_GEOFENCE` -> `[{"field":"within_fence","message":"false"}]` (changed: no `distance_m`/`radius_m`). HTTP 422/409 as listed;
  `RATE_LIMITED` 429 after 10 check-in/out calls per minute per user.
- check-in/out body also accepts optional `is_mock_location`: stored on the row, the row is flagged `suspicious` and audited
  (`attendance.suspicious_punch`) when mock rejection is switched off; never trusted as proof that a fix is genuine. Server time stays
  authoritative: `client_time` is diagnostics only, `fix_time` is only compared with server time. Check-out accepts `selfie_file_id`.
  `accuracy_m`/`fix_time` are only enforced on check-out when `attendance.checkout_geofence=true`.
- `distance_m` in `AttendanceRecord` is `null` for the employee (check-in, today, history) and for managers; `GET /attendance/team`
  returns it only to `attendance.view_all` holders (HR/admin). `within_fence` is always present.
- Night shifts (shift `end_time` < `start_time`): a check-out after midnight closes the previous day's open record until shift end + 4 h;
  `GET /attendance/today` returns that open record meanwhile; the 00:30 auto-close skips it until that window has passed.
- Team lists (`/attendance/team`, `/late/team`) are capped to 366 days: `from`..`to` longer than that -> 422 `INVALID_DATE_RANGE`; a missing
  bound is filled in (both missing = the last 366 days); `date=` (single day) is exempt.
- Retention (worker job `attendance.retention`, daily 03:30): settings `attendance.location_retention_days` (default 90) nulls stored punch
  coordinates, `attendance.selfie_retention_days` (default 90) deletes selfie files (their `selfie_url` then disappears); 0 = keep.
- `AttendanceRecord` has an extra `auto_closed` bool (no check-out by end of day; `check_out_at`/`worked_minutes` null, flagged for HR).
  `shift` is null only if the shift row is gone. Records of days with no check-out are never given a status change.
- Late: `late` iff check-in is strictly after shift start + grace (second precision); `minutes_late` = floor minutes after shift start.
  Not late on holidays/weekly-offs, on a first-half half-day leave, or when an approved permission covers shift start..to_time and
  check-in <= to_time + grace. Check-out with `worked_minutes` < `attendance.half_day_minutes` (default 240) sets status `half_day`
  (the late record, if any, is kept).
- Calendar: record status > `holiday` > `weekly_off` > `on_leave` (any approved leave) > `absent` (strictly before today) > `future`
  (today with no check-in yet is `future`). Users with no shift use Mon-Fri as weekly pattern in the calendar.
- Summary counts are disjoint by calendar status: a late day counts under `late` only (not also `present`); a half_day under `half_day`.
- `month` omitted = current office month; malformed -> 422 `VALIDATION_ERROR`. `/attendance/team` and `/late/team`: `*.view_all` = everyone,
  `*.view_team` = direct reports only; `employee_id` filter is the business key (case-insensitive).
- Holidays: `GET /holidays?year=` (any authed user) -> `{ "items":[{id,date,name}] }`. Writes need `holiday.manage` (audited):
  `POST /holidays {date,name}` -> 201 (409 `CONFLICT` if the date is taken), `PATCH /holidays/{id} {date?,name?,is_active?}`,
  `DELETE /holidays/{id}` -> 204. `GET /admin/holidays` still works but is DEPRECATED (remove from the app; it will be dropped).

---

## 4. Leave and Permission (module 03)

| Method | Path | Access / Perm |
|---|---|---|
| GET | /leave/types | Self |
| GET | /leave/balance?year=2026 | Self |
| POST | /leave/requests | Self |
| GET | /leave/requests?status=&from=&to=&page= | Self |
| GET | /leave/requests/{id} | Self (owner) |
| POST | /leave/requests/{id}/cancel | Self (owner, pending only) |
| POST | /permission/requests | Self |
| GET | /permission/requests?status=&from=&to=&page= | Self |
| GET | /permission/requests/{id} | Self (owner) |
| POST | /permission/requests/{id}/cancel | Self (owner, pending only) |
| GET | /admin/requests?type=leave\|permission&status=&department_id=&employee_id=&from=&to=&page= | `request.view_team` / `request.view_all` |
| PUT | /admin/requests/{type}/{id}/approve | `request.approve_team` / `request.approve_all` |
| PUT | /admin/requests/{type}/{id}/reject | same |

**GET /leave/balance** ->
```json
{ "year": 2026,
  "leave": [ { "leave_type_id":"uuid","code":"CL","name":"Casual Leave","quota":12.0,"used":3.0,"pending":1.0,"remaining":8.0 } ],
  "permission": { "month":"2026-10","limit_count":3,"used_count":1,"remaining_count":2,
                  "limit_minutes":240,"used_minutes":60,"remaining_minutes":180 } }
```
`remaining = quota - used - pending`.
**POST /leave/requests**
```json
{ "leave_type_id":"uuid","from_date":"2026-11-03","to_date":"2026-11-05","half_day":false,
  "half_day_session":null,"reason":"Family function","attachment_file_id":null }
```
`half_day_session` in `first|second`, only when `half_day=true` (then `from_date==to_date`).
-> 201 `LeaveRequest`:
```json
{ "id":"uuid","leave_type":{"id":"uuid","code":"CL","name":"Casual Leave"},"from_date":"..","to_date":"..",
  "half_day":false,"half_day_session":null,"days":3.0,"reason":"..","attachment_url":null,"attachment_content_type":null,
  "status":"pending|approved|rejected|cancelled","approver":UserSummary|null,"decision_comment":null,
  "decided_at":null,"created_at":"ts" }
```
`days` = working days in range (excludes weekly-offs and holidays).
Errors: 409 `INSUFFICIENT_BALANCE`, 409 `OVERLAPPING_REQUEST`, 422 `INVALID_DATE_RANGE` (past beyond `leave.backdate_days`, to<from, `to_date` later than
`leave.max_advance_days` (365) ahead, year outside 2000-2200, max-consecutive breach), 422 `ZERO_WORKING_DAYS`, 422 `ATTACHMENT_REQUIRED`,
422 `HALF_DAY_NOT_ALLOWED`, 422 `LEAVE_TYPE_INACTIVE`, **404 `NOT_FOUND` when `leave_type_id` does not exist** (decision: kept as 404, not 422; the app
should show a "refresh leave types" message), 429 `RATE_LIMITED` (header `Retry-After`), 503 `TEMPORARILY_UNAVAILABLE` (limiter down: writes are refused, retry).
**POST /permission/requests** `{ "date":"2026-11-03","from_time":"16:00","to_time":"17:30","reason":".." }` -> 201
`{ "id","date","from_time","to_time","minutes":90,"reason","status","approver","decision_comment","decided_at","created_at" }`.
Errors: 409 `PERMISSION_LIMIT_EXCEEDED` (count or minutes; details name which), 409 `OVERLAPPING_REQUEST`, 422 `INVALID_TIME_RANGE`
(to<=from, outside shift hours, > `max_minutes_per_request`, past date or, for today, a start time more than `permission.past_grace_minutes` (5) ago).
**Cancel** -> 200 updated request. Errors 409 `NOT_PENDING`, 404 (not yours / unknown), 429.
**Approve** `{ "comment": "optional" }` -> 200 request. **Reject** `{ "comment": "required" }` -> 200 request.
Errors: 409 `NOT_PENDING`, 409 `INSUFFICIENT_BALANCE` (re-check at approval), 409 `OVERLAPPING_REQUEST` (also: approved permission on a leave day / approved leave
at the permission time), 409 `REQUESTER_INACTIVE` (approve only; the employee is deactivated/deleted; reject still works), 403 `FORBIDDEN` (self-approval only),
**404 `NOT_FOUND` for an unknown id AND for a request outside your approval scope** (changed: it used to be 403; no existence leak), 429 `RATE_LIMITED`.
Admin list item = request object + `"user": UserSummary`, `"type":"leave|permission"`.

Implementation notes (v1, module 03):
- `GET /leave/types` -> `{items:[{id,code,name,annual_quota,is_paid,allow_half_day,requires_attachment_after_days,max_consecutive_days}]}` (active only).
- Balance rows are created lazily per (user, type, year) (also on `GET /leave/balance` and on employee creation). Mid-year joiner (join year = balance
  year): quota = annual_quota x remaining days / days in year, rounded half-up to 0.5. No carry-forward: each year starts at the type quota.
  Unpaid type (`is_paid=false`, `annual_quota=0`, e.g. seeded `LOP`) gets quota 365.0 (effectively unlimited).
- `GET /leave/balance.permission` also returns `pending_count` and `pending_minutes`; `remaining_* = limit - approved - pending`.
  Limits count pending + approved of the request date's calendar month; rejected/cancelled do not count (`details[0].field` = `count` or `minutes`).
- A leave request must lie within ONE calendar year (else 422 `INVALID_DATE_RANGE`; the app splits it). Past start allowed up to `leave.backdate_days`
  (7). `max_consecutive_days` (calendar span) violations are `INVALID_DATE_RANGE`. `half_day=true` needs `half_day_session` and `from_date==to_date`
  (else 422 `VALIDATION_ERROR`); the type must allow it (`HALF_DAY_NOT_ALLOWED`). `attachment_file_id` must be your own `leave_attachment` upload.
  `INSUFFICIENT_BALANCE` has `details:[{field:"remaining",message:"<days left>"}]`. Overlap = any pending/approved leave of yours on a date in range.
- Split rule: `max_consecutive_days` and `requires_attachment_after_days` are evaluated on the MERGED block = the new request plus your pending/approved requests of the
  SAME type that touch it (directly or across a gap made only of weekly-offs/holidays, searched +-31 days). Total working days of the block decide whether an
  attachment is needed; it is satisfied if ANY request in the block carries one. The app should send the same `attachment_file_id` with every part of a split.
- `attachment_url` (`/api/files/<id>`, bearer token required) is returned to the owner and in the admin list/decision responses, so approvers can open it
  (fetch with the Authorization header into memory; never put a token in a URL). `attachment_content_type` is `image/jpeg|image/png|application/pdf|null`.
- Encryption/retention: `reason` and `decision_comment` are encrypted at rest. With setting `leave.reason_retention_days` > 0 (default 0 = keep) a daily job
  blanks them and removes the attachment of decided requests (approved/rejected/cancelled) whose last day is older than that: `reason` is then `""`,
  `decision_comment` null, `attachment_url` null. Open (pending) requests are never purged.
- `GET /leave/balance?year=`: rows are only stored for last/this/next year; any other year in 2000-2200 returns the computed quota with used=0, pending=0 and
  writes nothing; outside 2000-2200 is 422 `VALIDATION_ERROR`. Changing an employee's `join_date` recomputes the quota of the old/new join year when it has no
  used or pending days (audit `leave.quota_recompute`; rows with usage are only flagged in that audit entry for HR).
- Deactivating or deleting an employee auto-cancels their PENDING leave and permission requests (balance `pending` restored, audit `leave.auto_cancel` /
  `permission.auto_cancel`); approved requests stay.
- Rate limits (per user, Redis, fail closed -> 503 `TEMPORARILY_UNAVAILABLE`): apply + cancel of both kinds share `LEAVE_WRITE_RATE_PER_MIN` (10) and
  `LEAVE_WRITE_RATE_PER_DAY` (50); approve + reject share `LEAVE_DECIDE_RATE_PER_MIN` (60, per approver). Exceeded -> 429 `RATE_LIMITED` + `Retry-After`.
- Permission: date today or later and a shift working day; times inside shift hours; duration <= `permission.max_minutes_per_request`; all breaches are
  422 `INVALID_TIME_RANGE`. 409 `OVERLAPPING_REQUEST` also when a PENDING or approved full-day leave covers the date, or a half-day leave whose session
  window overlaps (first = shift start to the shift midpoint, second = midpoint to shift end). Approving a leave fails with the same 409 when an approved
  permission lies on its days (any time for full-day, inside the session for half-day). Overnight shifts (end <= start): `date` is the day the shift
  STARTS and after-midnight hours belong to that shift; times must lie in `[start, 24:00)` or `[00:00, end]`. No shift -> 422 `NO_SHIFT_ASSIGNED`.
- Cancel: pending only (else 409 `NOT_PENDING`). Cancelling APPROVED requests before they start is available only if setting
  `leave.allow_cancel_approved=true` (default false) and then restores the balance. **This one setting governs BOTH leave and permission requests**
  (decision: no separate `permission.allow_cancel_approved`; the table rows say "pending only", which is the default). Non-owner/unknown id -> 404 (never 403).
- Approvals: `request.approve_all` = any request; `request.approve_team` = requester's `manager_id` is the caller (current manager at decision time).
  Approving/rejecting your own request is 403 `FORBIDDEN` for everyone. Reject body needs a non-blank `comment` (else 422). Approve body optional.
  Approved/rejected responses are the admin item shape (`type`, `user`). `GET /admin/requests` `type` defaults to `leave`; `status`, `employee_id`
  (business key, case-insensitive), `department_id`, `from`/`to` (leave: overlapping range; permission: request date) filter; `view_team` = direct reports only.
- Audit actions: `leave.apply|cancel|auto_cancel|quota_recompute|retention_purge`, `permission.apply|cancel|auto_cancel`, `request.approve|reject`,
  `request.decide_denied` (self / out of scope; outcome failure), `request.decide_refused` (409 refusals; outcome failure), `leave.rollover`.
- Push (best effort, never blocks): new request -> the manager if active AND holding `request.approve_team`/`approve_all`, otherwise every active
  `request.approve_all` holder (max 20; at most one push per requester+approver per 10 minutes in that fan-out case), payload `type=new_request`;
  decision -> requester, `type=leave_decision|permission_decision`, `id` = request id.
- New error codes: `INSUFFICIENT_BALANCE`, `OVERLAPPING_REQUEST`, `PERMISSION_LIMIT_EXCEEDED`, `NOT_PENDING` (409); `INVALID_DATE_RANGE`,
  `INVALID_TIME_RANGE`, `ZERO_WORKING_DAYS`, `ATTACHMENT_REQUIRED`, `HALF_DAY_NOT_ALLOWED`, `LEAVE_TYPE_INACTIVE` (422); `REQUESTER_INACTIVE` (409). New
  editable settings (section 9): `leave.max_advance_days` (365, 0 = no horizon), `leave.reason_retention_days` (0 = off), `permission.past_grace_minutes` (5),
  plus the existing `leave.backdate_days` and `leave.allow_cancel_approved`. The admin
  leave-balance endpoints (section 9, `QUOTA_BELOW_USED`) are not part of this change.

---

## 5. Late (module 04)

| GET | /late?from=&to=&page= | Self |
| GET | /late/summary?month=2026-10 | Self |
| GET | /late/team?from=&to=&department_id=&employee_id=&page= | `late.view_team` / `late.view_all` |

Item: `{ "id","work_date","shift_start":"09:30","grace_minutes":10,"check_in_at":"ts","minutes_late":22 }` (team adds `user`).
Summary: `{ "month":"2026-10","late_count":3,"total_minutes_late":65,"average_minutes_late":21.7,"days":[{"date":"..","minutes_late":22}] }`.
`minutes_late` = minutes after shift start (not after grace); a record exists only if check-in is later than start + grace.

---

## 6. Food (module 05)

| Method | Path | Access / Perm |
|---|---|---|
| GET | /food/my-enrollments?from=&to= | Self (default: today..today+13) |
| PUT | /food/enrollment | Self |
| PUT | /food/enrollments | Self (bulk, max 31) |
| GET | /admin/food/daily?date=&department_id= | `food.view_all` |

**PUT /food/enrollment** `{ "date":"2026-10-22","need_food":true }` -> 200
`{ "date","need_food":true,"can_change":true,"cutoff_at":"2026-10-21T12:30:00Z" }`.
**PUT /food/enrollments** `{ "items":[{"date":"..","need_food":true}, ...] }` -> 200 `{ "items":[...results], "failed":[{"date","code"}] }`
(partial success; each failure `FOOD_CUTOFF_PASSED` etc.).
**GET /food/my-enrollments** -> `{ "items":[ {"date","need_food":true|false|null,"can_change":bool,"cutoff_at":"ts","is_holiday":false} ] }`
(`null` = not answered; treated as "No" for counts).
Each my-enrollments item also has `is_weekly_off` (bool); `can_change` is false for past, holiday, weekly-off, beyond-horizon dates and after `cutoff_at`
(`now < cutoff_at` = allowed; `cutoff_at` = `food.cutoff_time` office-tz on the previous day (`previous_day`) or the day itself (`same_day`)).
Range max 93 days (422 `INVALID_DATE_RANGE`). Bulk: 1..31 unique dates (duplicates -> 422), `need_food` must be a JSON boolean (strict). No audit/admin override
after cut-off (plan O2 deferred). Rate limits: PUT single 30/min, bulk 10/min, GET 60/min per user (429 `RATE_LIMITED`).
Errors: 409 `FOOD_CUTOFF_PASSED`, 422 `INVALID_DATE` (past, holiday/weekly-off, > `food.max_advance_days` ahead).
**GET /admin/food/daily** -> `{ "date","cutoff_at","count_yes":42,"count_no":10,"count_unanswered":5,
 "employees":[ { "user":UserSummary,"need_food":true } ] }` (employees = every active user answered yes; query `?include=all` adds no/unanswered).
Added: each employee has `on_leave` (approved full-day leave that date); response has `by_department:[{department,count_yes,count_no,count_unanswered}]`;
`need_food` is `null` for unanswered (include=all). `date` defaults to today (office tz). Inactive/deleted users are excluded everywhere.
Review 04: the `employees` list is paginated (`?page=1&page_size=500`, page_size 1..500, default 500); the response also carries `page`, `page_size` and
`total` (number of rows matching `include`). The `count_*` and `by_department` aggregates always cover everyone, not just the page. Each call by an
`food.view_all` holder is audit logged (`food.daily.view`: date, department, include, page, rows). Date window: today - 3650 days .. today + 400 days,
else 422 `INVALID_DATE`; `GET /food/my-enrollments` accepts `from`/`to` within today -/+ 400 days, else 422 `INVALID_DATE` (never a 500).
Commit-time cut-off: after the write and right before COMMIT the cut-off is evaluated again with a fresh clock; a change that crossed `cutoff_at` while it was
being processed is rolled back and answers 409 `FOOD_CUTOFF_PASSED` (bulk: the late item is reported in `failed`).

---

## 7. Announcements (module 06)

| Method | Path | Access / Perm |
|---|---|---|
| GET | /announcements?unread=&page= | Self (only targeted/all) |
| GET | /announcements/unread-count | Self |
| GET | /announcements/{id} | Self |
| POST | /announcements/{id}/read | Self |
| GET | /admin/announcements?page= | `announcement.manage` |
| POST | /admin/announcements | `announcement.manage` |
| PUT | /admin/announcements/{id} | `announcement.manage` |
| DELETE | /admin/announcements/{id} | `announcement.manage` (soft delete) |
| GET | /admin/announcements/{id}/reads?page= | `announcement.manage` |

Lists use the standard page envelope `{ "items": [...], "page", "page_size", "total" }` (`page_size` default 20, max 100), newest first.
Announcement (employee view): `{ "id","title","body","priority":"normal|important|urgent","image_url":null,"pdf_url":null,
"published_at":"ts","expires_at":null,"is_read":false,"created_by":UserSummary,"updated_by":UserSummary|null,"updated_at":"ts|null" }`.
`updated_by`/`updated_at` are set once someone edits the announcement (show "edited by X"); both null = never edited. The employee view does NOT
contain `target_all`/`department_ids` (who else was targeted is not revealed).
Admin view (`/admin/announcements*`) has no `is_read` and adds `"target_all":true,"department_ids":[],"version":3,"read_count":12,"target_count":50,
"status":"published|expired"`.
Create/PUT body: `{ title(1-150), body(1-5000), priority, image_file_id?, pdf_file_id?, target_all, department_ids[], expires_at?, renotify? }`
-> 201 (create) / 200 (PUT = full replace; omitted image/pdf are cleared; `renotify` is PUT-only). PUT optionally takes `If-Match: "<version>"` (the
admin view's `version`, +1 on every PUT): a different current version -> 409 `CONFLICT` and nothing is changed; without the header there is no check.
PUT on an EXPIRED announcement needs an explicit new future `expires_at` (422 otherwise), so an edit can never silently republish it.
`announcement.manage` is global (not scoped to a department). An announcement written by an account whose role holds permissions the editor lacks
(e.g. HR vs an Admin-authored one) cannot be edited or deleted by that editor (403 `FORBIDDEN`). Title/body are plain text (markup and control
characters are stripped). `target_all=false` requires >= 1 department (422); `expires_at` must be timezone-aware and in the future (422);
`image_file_id`/`pdf_file_id` must be files uploaded with purpose `announcement_image`/`announcement_pdf` (422 otherwise); unknown
departments -> 422. A push (`type=announcement`, `id`, `title`, first 100 chars of body) is sent to the active
users in the audience in a background task on create, and on PUT only with `renotify=true`. Push abuse limits: re-sending one announcement
is allowed once per 60 min (`ANNOUNCEMENT_RENOTIFY_COOLDOWN_MIN`), else 429 `RATE_LIMITED` with `Retry-After`; each admin has a budget of 20 push-generating
writes (create + renotify) per rolling 24 h (`ANNOUNCEMENT_PUSH_BUDGET_PER_DAY`), else 429; both fail closed (503 `TEMPORARILY_UNAVAILABLE`) when Redis is
down. High-priority delivery (`urgent`) happens at most once per announcement; later re-sends of an urgent announcement go out as normal pushes.
**GET /announcements?unread=true|false&page=&page_size=**: only announcements visible to the caller (not deleted, published, not expired, and
`target_all` or the caller's CURRENT department targeted; users without a department see only `target_all`). `unread` omitted = all.
**GET /announcements/{id}** does not mark as read. Not visible/unknown id -> 404 `NOT_FOUND` (never 403).
**GET /announcements/unread-count** -> `{ "unread_count": 3 }` (same number as `/home` `unread_announcements`).
**POST /announcements/{id}/read** -> 204, idempotent (404 if not visible). Rate limits per user: 120/min on this call and 120/min on
`GET /announcements*`; 30/min on `GET /admin/announcements` and `.../reads`; 10/min on DELETE; 30/min on POST/PUT (all 429 `RATE_LIMITED`).
**DELETE** soft-deletes (204). Expired announcements stay in the admin list; deleted ones do not. 30 days after deletion
(`ANNOUNCEMENT_DELETED_RETENTION_DAYS`, 0 = off) the worker unlinks the image/PDF so the blobs are removed by the orphan sweep.
**GET /admin/announcements/{id}/reads?page=** -> `{ "items":[{ "user": UserSummary, "read_at":"ts" }], "page","page_size","total":12,
"read_count":12, "target_count":50 }`. `target_count` = active users currently in the audience; `read_count` = those of them who have read it
(= `total`). Changing targets keeps existing read rows but they stop counting when the reader leaves the audience.
Admin create/update/delete are audited (`announcement.create|update|delete`; title/priority/targets plus the body's SHA-256 and length, never the body text;
update also records `renotify`).

---

## 8. (removed)

The former module 08 (Safe Reach Home trips, SOS, live location, location consent, home location) has been removed from the
product. There are no `/trips`, `/sos`, `/admin/trips`, `/admin/sos` or `/me/location-consent` endpoints. The section number is
kept so that references to sections 9-11 stay valid.

---

## 9. Admin (module 09)

All under `/admin`. Mutations write `audit_logs`. List endpoints paginated, support `?q=` text search.
Generic CRUD shape for each master: `GET /x`, `POST /x` (201), `GET /x/{id}`, `PUT /x/{id}` (full replace of editable fields), `DELETE /x/{id}` (204).
Delete of an entity in use -> 409 `IN_USE` (use `is_active=false` instead).

| Resource | Path | Perm | Body fields |
|---|---|---|---|
| Departments | /admin/departments | `department.manage` (GET: any authed) | `name, code, is_active` |
| Shifts | /admin/shifts | `shift.manage` (GET: any authed) | `name, start_time, end_time, grace_minutes, working_days:[1..7], is_active` |
| Holidays | /holidays?year= (moved from /admin/holidays) | `holiday.manage` (GET: any authed) | `date, name` |
| Leave types | /admin/leave-types | `leave_type.manage` (GET: any authed) | `code, name, annual_quota, is_paid, allow_half_day, requires_attachment_after_days, max_consecutive_days, is_active` |
| Roles | /admin/roles | `role.manage` (GET `role.view`) | `name, description` |
| Role permissions | PUT /admin/roles/{id}/permissions | `role.manage` | `{ "permission_codes": ["..."], "current_password": "<your own password>" }` (**both required**, see Step-up re-authentication) |
| Permission catalogue | GET /admin/permissions | `role.view` | -> `{items:[{code,module,description}]}` |
| Settings | GET/PUT /admin/settings | `settings.manage` | `{ "settings": { "food.cutoff_time":"18:00", ... } }` (PUT merges, validates keys/types/ranges, see below) |
| Audit logs | GET /admin/audit-logs?actor=&action=&entity_type=&entity_id=&from=&to=&page= | `audit.view` | read-only |

**Settings validation (review 04 M4/M5).** Every editable key has a type and an inclusive range; anything else is 422 `VALIDATION_ERROR` with
`details:[{field:<key>,message}]`. NaN/Infinity, booleans for numbers, floats with a fractional part for integer keys and out-of-range values are
rejected (integral floats such as `200.0` are stored as `200`). Ranges: `office.lat` -90..90, `office.lng` -180..180, `office.radius_m` 10..2000,
`attendance.max_accuracy_m` 5..500, `attendance.half_day_minutes` 30..720, `attendance.max_fix_age_s` 5..300, `attendance.max_fix_future_s` 0..120,
`attendance.selfie_max_age_s` 30..3600, `attendance.location_retention_days` and `attendance.selfie_retention_days` 1..3650 (0 = keep forever can NOT be set),
`food.max_advance_days` 1..60, `auth.inactivity_timeout_minutes` 1..30,
`permission.monthly_count_limit` 0..100, `permission.monthly_minutes_limit` 0..10080, `permission.max_minutes_per_request` 1..1440,
`permission.past_grace_minutes` 0..1440, `leave.backdate_days` 0..365, `leave.max_advance_days` 0..3650 (0 = no horizon), `leave.reason_retention_days` 0..3650 (0 = keep).
Booleans: `attendance.selfie_required`, `attendance.checkout_geofence`, `attendance.reject_mock_location`, `leave.allow_cancel_approved`. `food.cutoff_time` must exist in the
office timezone (a time inside a daylight-saving gap is refused). The audit row `settings.update` stores `after = { "changes": [ {key, before, after}, ... ] }`
(only keys whose value changed; values are never masked; no row when nothing changed).

Employees (`/admin/employees`):
| Method | Path | Perm |
|---|---|---|
| GET | /admin/employees?q=&department_id=&role=&status=&page= | `employee.view` (manager: team only) |
| POST | /admin/employees | `employee.create` (+ `current_password` when the new role is admin-capable) |
| GET | /admin/employees/{id} | `employee.view` (sensitive fields need `employee.view_sensitive`) |
| PUT | /admin/employees/{id} | `employee.update` (+ `current_password` when the resulting role is admin-capable) |
| DELETE | /admin/employees/{id} | `employee.delete` (soft delete, deactivates, frees nothing; employee_id never reused) |
| POST | /admin/employees/{id}/deactivate / activate | `employee.deactivate` |
| POST | /admin/employees/{id}/unlock | `employee.unlock` |
| POST | /admin/employees/{id}/reset-password | `employee.reset_password` (+ `current_password` when the target holds any permission; never for yourself) |
| GET | /admin/employees/{id}/leave-balances?year= | `leave_balance.manage` |
| PUT | /admin/employees/{id}/leave-balances | `leave_balance.manage` |
| GET | /admin/password-reset-requests?status= | `employee.reset_password` |

**DELETE /admin/employees/{id}** -> 204 (soft delete: sets `deleted_at`, `status=inactive`, revokes all sessions and access
tokens; the record disappears from lists and returns 404 afterwards; login answers `ACCOUNT_INACTIVE`; the `employee_id` stays
reserved). Errors: 403 self-delete / insufficient privilege, 404, 409 `LAST_ADMIN`.

**GET /admin/password-reset-requests?status=open|handled|expired&page=&page_size=** (perm `employee.reset_password`) ->
paginated `{ items:[{ id, status, requested_at, handled_at|null, handled_by|null (user uuid),
user:{ id, employee_id, full_name } }], page, page_size, total }`, newest first. A request becomes `handled` when an
Admin/HR runs reset-password for that user.

**Privilege rules (permission based, replaces role-name checks).** Two rules, both evaluated on the server from the live permission set:
(1) *containment*: every permission of the target role R must be held by the actor; (2) *elevated*: if R holds any permission at all (anything above plain
self-service: hr, manager, admin, custom roles) the actor must also hold the Admin-only permission **`role.assign`**. Rules (1) + (2) apply to: creating an account with
role R, changing an account to role R, editing an account that holds role R, resetting its password, and **activating, deactivating and unlocking** it (review 06 H1:
HR cannot reactivate, deactivate or unlock HR, Manager or Admin accounts). **Delete** needs rule (1) only. Consequence: HR manages plain `employee` accounts only; Admin does everything.
Violations -> 403 `FORBIDDEN`. Reactivating an account sets `must_change_password=true`.
**Team scope (review 06 M2).** An actor without `employee.update`/`employee.create` (a custom role that only holds `employee.view`, e.g. a manager) only reaches employees with
`manager_id == actor`: the list, detail, deactivate/activate/unlock/reset-password/delete, leave balances and `/admin/password-reset-requests` all apply this; anything else answers 404 `NOT_FOUND`.
Team-scoped viewers also get the reduced employee shape without `is_locked`, `locked_until`, `last_login_at`.

**Step-up re-authentication (review 06 M1).** Some actions need the ACTING admin's own password in the JSON body (`current_password`, 1-128 chars). The server decides whether it is needed;
the schema keeps the field optional where it is conditional.

| Endpoint | `current_password` required when |
|---|---|
| POST /admin/employees | the new account's role is admin-capable (holds `role.assign` or `role.manage`) |
| PUT /admin/employees/{id} | the RESULTING role is admin-capable (promotion to it, or editing an account that already has it) |
| POST /admin/employees/{id}/reset-password (body optional otherwise) | the target's role holds any permission (hr, manager, admin, custom); not needed for plain `employee` targets |
| PUT /admin/roles/{id}/permissions | always (the field is required by the schema) |

- Missing where required -> 422 `VALIDATION_ERROR` with `details:[{field:"current_password",message:...}]`.
- Wrong password -> 403 `REAUTH_FAILED` (message "Your password is incorrect"). The attempt is audited as `admin.reauth_failed` (outcome `failure`, `after.for` = the action, e.g. `employee.update`, `role.permissions`); the password itself is never stored, logged or echoed.
- Attempts are limited to 5/min per acting admin (429 `RATE_LIMITED`); the limiter fails closed (Redis down -> 503 `TEMPORARILY_UNAVAILABLE`). The client must NOT end the session on 403 `REAUTH_FAILED` (it is not an auth error); show the prompt again.
- Order of checks: target lookup and privilege rules first, then the password check, then the change. A reset of your own account is refused before any password check: 403 `FORBIDDEN` ("use change-password").
- After a committed create / promotion / reset of an admin-capable account, every active holder of `role.assign` gets a high-priority push `type:"admin_alert"` (title "Administrator account created" / "Account promoted to an administrator role" / "Administrator password reset", names and employee ids only).

**POST /admin/employees**
```json
{ "employee_id":"EMP042","full_name":"A B","official_email":"a@corp.com","phone":"+91..","department_id":"uuid",
  "shift_id":"uuid","role_id":"uuid","manager_id":"uuid|null","join_date":"2026-10-01","current_password":"<only for admin-capable roles>" }
```
-> 201 `{ "employee": {...}, "temporary_password": "Xk9#..." }`. The temporary password is **always generated by the server** (there is no `initial_password`; sending it, or any other unknown field, is 422), shown exactly once,
`must_change_password=true`, and valid only for `TEMP_PASSWORD_HOURS` (default 72) after creation / reset: after that, login answers 403 `TEMP_PASSWORD_EXPIRED` and an administrator must reset it. Changing the password clears the deadline.
Errors: 409 `EMPLOYEE_ID_EXISTS`, 409 `EMAIL_EXISTS`, 403 `FORBIDDEN` (privilege rules), 403 `REAUTH_FAILED`, 422 validation (incl. missing `current_password`), 429. Rate limit 30/min per admin.
Employee object (as built in backend plan 01: `department`/`shift`/`role` are `{id,name}` objects, `manager` is `{id,employee_id,full_name}|null`, dates/timestamps as strings): `{ id, employee_id, full_name, official_email, department, shift, role, manager, status:"active|inactive", is_locked, locked_until, join_date, last_login_at }`.
**reset-password** body optional `{ "current_password": "..." }` (see Step-up) -> `{ "temporary_password":"..." }` (generated, shown once, valid `TEMP_PASSWORD_HOURS`). Revokes all sessions and access tokens, sets `must_change_password`, unlocks the account and marks open reset requests `handled`. Errors: 403 `FORBIDDEN` (own account, or privilege rules), 403 `REAUTH_FAILED`, 404, 429 (10/min per admin, fail closed).
**PUT leave-balances** `{ "year":2026,"items":[{"leave_type_id","quota":12.0}] }` (quota cannot go below used+pending -> 409 `QUOTA_BELOW_USED`). 403 `FORBIDDEN` for your own balance (review 06 M3) and for peer / more privileged accounts (the privilege rules above apply).
Guardrails (last-admin check now row-locks all active admins, so two concurrent removals cannot leave zero): 409 `LAST_ADMIN` (cannot deactivate/delete/demote the last active admin), 403 on self deactivate/delete, system roles (`admin,hr,manager,employee`) not deletable; `admin` permissions not editable.
Audit log item: `{ id,ts,actor:UserSummary|null,action:"employee.create",entity_type,entity_id,before,after,ip,outcome }` (sensitive values masked as `"***"`).

**Admin module implementation notes (plan 09; these refine/extend the tables above).**
- **Masters list shape.** `GET /admin/departments|shifts|leave-types?q=&is_active=&page=&page_size=` -> `{items,page,page_size,total}`; default `page_size` is 100 (max 100) so dropdowns get everything in one call.
  Item shapes: department `{id,name,code,is_active}` (`code` stored upper case); shift `{id,name,start_time:"HH:MM",end_time,grace_minutes,working_days:[1..7],is_active}`;
  leave type `{id,code,name,annual_quota,is_paid,allow_half_day,requires_attachment_after_days|null,max_consecutive_days|null,is_active}` (`code` upper case, `annual_quota` 0..999.9, one decimal).
  PUT = full replace. Duplicate name/code -> 409 `CONFLICT` (`details:[{field}]`). Delete -> 204, or 409 `IN_USE` when referenced: department (users, announcement targets), shift (users, attendance rows),
  leave type (any request, or any balance with used/pending > 0; untouched zero-usage balance rows are deleted with the type). Changing a leave type's `annual_quota` or a shift does not rewrite history/existing balances.
  Holidays: unchanged (`/holidays`, duplicate date -> 409 `CONFLICT`). The old `/admin/departments|shifts` minimal reads are replaced by these (their items are a superset).
- **Roles.** `GET /admin/roles` -> `{items:[Role]}` (unpaginated); `GET /admin/roles/{id}` -> Role; `POST` (201) body `{name,description|null,permission_codes?:[...]}`; `PUT /admin/roles/{id}` body `{name,description}` -> Role;
  `PUT /admin/roles/{id}/permissions` body `{permission_codes,current_password}` (full replace; 403 `REAUTH_FAILED` on a wrong password) -> Role; `DELETE` -> 204.
  `Role = {id,name,description|null,is_system,user_count,permission_codes:[sorted]}`. `name` matches `^[a-z][a-z0-9_]*$` (2-30). Errors: 422 unknown permission code; 409 `CONFLICT` duplicate name; 409 `IN_USE` role still assigned to users;
  403 `FORBIDDEN` for: any change to `admin`, deleting or renaming a system role, granting a permission the caller does not hold, removing permissions from the caller's own role.
  Permissions are read from the database on every request, so a permission change is effective immediately (no cache, no session revocation). Audit: `role.create|update|delete`, `role.permissions` (`before={permission_codes}`, `after={permission_codes,added,removed}`).
- **PUT /admin/employees/{id}** now also accepts the personal fields `phone, dob, wedding_anniversary, personal_email, emergency_contact, blood_group, address` with the exact validation of `PUT /me/profile`.
  These are optional and only-sent-keys-change (explicit `null` clears; **behaviour change:** `phone: null` used to mean "unchanged", now it clears; omit the key to keep it). `show_celebrations` is ignored. The org fields stay full-replace.
  `manager_id` must be an active, non-deleted user, not the employee and must not create a reporting cycle (422 `VALIDATION_ERROR`, `details.field=manager_id`); an unchanged manager is not re-validated. Changing the role revokes all sessions and access tokens of that employee.
  The response (PUT and GET detail) is the Employee object plus `profile`: `{dob,wedding_anniversary,personal_email,phone,emergency_contact,blood_group,address}` for holders of `employee.view_sensitive` (admin, hr), otherwise `profile: null`
  (list responses and create have no `profile` key). Reading a detail with `profile` writes audit `employee.view_sensitive`. Personal-field edits write `profile.update_admin` with `after={fields:[names]}` (names only, never values);
  `employee.update` carries the changed org fields, plus `profile_fields` and `sessions_revoked` when applicable.
  Sending any personal key needs `employee.view_sensitive`, else 403 `FORBIDDEN` (review 06 M4); the employee gets a push `type:"profile_changed"` naming the changed fields (never values). Unknown body fields are 422 (`extra=forbid`); `full_name` is cleaned, `phone` normalised.
  `profile` is `null` when the target's role holds permissions the viewer lacks (review 06 M5). `blood_group` and `emergency_relationship` are encrypted at rest (migration 0013); the API shape is unchanged.
- **Leave balances.** `GET /admin/employees/{id}/leave-balances?year=` (default current year) -> `{year,items:[{leave_type:{id,code,name},quota,used,pending,remaining}]}` for active types; read only (a type with no row yet shows the quota it would get).
  `PUT` body `{year,items:[{leave_type_id,quota}]}` (1..50 items, no duplicates, quota 0..999.9) -> same shape as GET; 409 `QUOTA_BELOW_USED`, 422 unknown type. Audit `leave_balance.update` `after={year,changes:[{leave_type:code,before,after}]}`.
  **New:** `POST /admin/leave-balances/apply-defaults` (perm `leave_balance.manage`) body `{year}` -> `{year,employees,created}`: creates the missing balance rows of that year for every active employee from the types' default quota; idempotent, existing rows are never changed. Audit `leave_balance.apply_defaults`.
- **Audit logs.** `GET /admin/audit-logs?actor=&action=&entity_type=&entity_id=&from=&to=&page=&page_size=` (perm `audit.view`) -> paginated, newest first. `actor` = user uuid or employee_id (case-insensitive); `action` = exact or dotted prefix (`employee` matches `employee.update`);
  `from`/`to` are inclusive UTC dates. Item `actor` is `{id,employee_id,full_name,department,photo_url}`; for failed logins of unknown ids `id` and `full_name` are null (`employee_id` is a sanitised/hashed value); `actor` is null for system rows. `before`/`after` are masked again on output. No export.
- **Rate limits (review 06 M6, per acting admin).** Employee detail with `profile` 60/min and 500/day (fail closed), create 30/min, reset-password 10/min (fail closed), role writes (create/update/permissions/delete) 10/min, audit viewer 30/min, apply-defaults 2/min, step-up attempts 5/min (fail closed). More than `SENSITIVE_VIEW_ALERT_PER_HOUR` (default 100) personal-data detail views in an hour push one `admin_alert` to the `role.assign` holders. Exceeding a limit -> 429 `RATE_LIMITED` with `Retry-After`.
- **Deactivate / delete side effects (review 06 L4).** The employee's push device tokens are deleted, pending leave/permission requests are cancelled, and all sessions and access tokens are revoked. Self-deactivate / self-delete -> 403; last active admin -> 409 `LAST_ADMIN`.
- **Audit viewer reads are audited (review 06 L3).** Each `GET /admin/audit-logs` writes `audit.view` with the filters and paging only (never the returned content).
- **New error codes** (this section): 403 `REAUTH_FAILED`, 403 `TEMP_PASSWORD_EXPIRED` (login, section 1).
- **New error code** `IN_USE` (409). Settings (`GET/PUT /admin/settings`) and `GET /admin/password-reset-requests` are unchanged.

---

## 10. Export (module 10)

`GET /admin/export?type=all|attendance|leave|late|permission|food&from=2026-10-01&to=2026-10-31&department_id=&employee_id=&include_reasons=false`
- Perm: `export.all` (any scope) or `export.team` (forced to direct reports). `include_reasons=true` additionally needs `export.sensitive` (see below).
- Response 200: binary `.xlsx`, `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
  `Content-Disposition: attachment; filename="employee-connect-all-2026-10-01_2026-10-31.xlsx"`.
- `type=all` -> sheets `Attendance`, `Leave`, `Late`, `Permission` (food excluded). Single type -> one sheet. `food` -> sheet `Food`
  (columns Date, Employee ID, Name, Department, Need Food).
- `from`/`to` required, `to>=from`, max range 366 days (422 `RANGE_TOO_LARGE`); food accepts a single date range too.
- A text cell whose first non-whitespace character is `= + - @` or the full-width `＝ ＋ － ＠` is prefixed with `'` (formula injection; leading spaces do not hide a trigger, plain text starting with a tab or newline is left alone). Empty result -> workbook with header-only sheets.
- Every export is audit logged (`export.run`).
- Status: all types are implemented (`attendance`, `leave`, `late`, `permission`, `food`, and `all` = the first four).
  `employee_id` filter is the business id (case-insensitive).
- Response headers: `Content-Type` xlsx, `Content-Disposition: attachment; filename="employee-connect-<type>-<from>_<to>.xlsx"` (built only from the enum type and ISO dates),
  `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`.
- Sheets and columns (fixed names; the `Leave` and `Permission` lists below are the `include_reasons=true` shape): `Attendance` (Date, Employee ID, Name, Department, Shift, Check-in, Check-out, Status, Minutes Late, Worked Hours, Within Geofence),
  `Leave` (Request ID, Employee ID, Name, Department, Leave Type, From, To, Days, Half Day, Status, Reason, Approver, Decision Comment, Applied On, Decided On),
  `Late` (Date, Employee ID, Name, Department, Shift Start, Check-in, Minutes Late), `Permission` (Request ID, Employee ID, Name, Department, Date, From, To, Minutes, Status,
  Reason, Approver, Comment), `Food` (Date, Employee ID, Name, Department, Need Food).
  - Filtering: Attendance/Late by `work_date`, Permission by its date, Leave = requests OVERLAPPING the range (all statuses, incl. cancelled/rejected). Check-in/out and
    applied/decided times are written as real Excel datetimes in the OFFICE time zone (naive wall-clock). Deactivated employees are included.
  - Not exported: coordinates, distances, selfies, mock-location/suspicious flags, e-mail, phone, address, DOB or any encrypted profile data. Computed absent/holiday rows (plan O1) are NOT included.
  - **Reasons are opt-in (review 08 M2).** By default the `Leave` sheet has no `Reason` and `Decision Comment` columns and the `Permission` sheet has no `Reason` and `Comment` columns (they are not selected or decrypted at all).
    With `include_reasons=true` the columns are present, in the positions listed above. The flag needs the permission **`export.sensitive`** (seeded for `admin` and `hr`, migration 0015; custom roles must be granted it);
    without it the request is refused with 403 `FORBIDDEN` (never silently downgraded). The flag is accepted but has no effect for `attendance`, `late` and `food`. Each value is decrypted individually: purged (retention) -> empty cell, undecryptable -> `[unreadable]`.
  - Scope tightening: `export.all` gives everyone only if the caller also holds the `*.view_all` permission of every requested type; otherwise the scope is the caller's direct reports.
- Review 04 changes:
  - **Per-type permission** on top of `export.all|export.team`: `food` needs `food.view_all` (so a Manager gets 403 `FORBIDDEN` for food); types:
    attendance = `attendance.view_all|view_team`, leave and permission = `request.view_all|view_team`, late = `late.view_all|view_team`. `type=all` needs every included type.
  - **Scope** is computed once in the router: `export.all` = everyone, `export.team` = direct reports. Every sheet builder takes it as a mandatory argument.
  - Unknown `department_id` or `employee_id` -> 404 `NOT_FOUND` for callers with the everyone scope. For a TEAM-scoped caller an unknown id and an out-of-scope id (employee outside the team, department with none of the caller's reports) give the SAME 403 `FORBIDDEN` with the same message (review 08 L2), so ids cannot be enumerated.
  - Dates must lie in 2000-01-01..2200-12-31 (422 `INVALID_DATE`).
  - Rows are counted (`SELECT count(*)`, same filters) BEFORE anything is written: over `EXPORT_MAX_ROWS` (default 200000) per sheet -> 422 `RANGE_TOO_LARGE`.
    The build runs in a worker thread, at most `EXPORT_MAX_CONCURRENCY` (default 2) per process, within `EXPORT_TIMEOUT_S` (default 60 s; exceeded -> 422 `RANGE_TOO_LARGE`).
    All slots busy for 5 s -> 503 `TEMPORARILY_UNAVAILABLE` with `Retry-After`. Temp files live in a private directory under `EXPORT_TEMP_DIR` and are always removed.
  - Audit `export.run` `after` now also has `rows` (`{ "<Sheet>": n }`) and `total_rows`.
  - Text cells are stored as strings (never formulas or error codes), XML-illegal characters (incl. U+FFFE/U+FFFF) are stripped and cells are cut at 32000 characters.
  - Rate limit 5/min/user (fail closed). `GET/PUT /admin/settings` is live (`settings.manage`, audited as `settings.update`; unknown keys -> 422).
- Review 08 changes (backend):
  - **Daily quota per user and UTC day (M3).** At most `EXPORT_DAILY_MAX_EXPORTS` (default 30) exports and `EXPORT_DAILY_MAX_ROWS` (default 2,000,000) delivered rows. Over it -> **429 `EXPORT_QUOTA_EXCEEDED`** with `Retry-After` (seconds to UTC midnight); a request that fails or is refused does not use up the count, and a build that would pass the row ceiling is discarded undelivered. Redis down -> 503 `TEMPORARILY_UNAVAILABLE` (fail closed). A single export of at least `EXPORT_WARN_ROWS` rows, or crossing `EXPORT_DAILY_WARN_EXPORTS` / `EXPORT_DAILY_WARN_ROWS` in a day, writes a WARNING to the `app.export` log (no blocking; no step-up re-authentication).
  - **Team window (L3).** A team-scoped caller (no everyone scope) is only known by the CURRENT `manager_id`, so `from` is clamped to the last `EXPORT_TEAM_WINDOW_MONTHS` (default 12; 0 = off) months before today (UTC); the effective start is recorded as `effective_from` in the audit row. Soft-deleted employees are not part of a team scope. Everyone-scope callers are never clamped.
  - **Audited refusals (L1).** A refused or failed request that reached the export handler (403 type / scope / `include_reasons`, 404, 422 `RANGE_TOO_LARGE` / validation, 429, 503) writes `export.run` with `outcome:"failure"` and `after` = the request (`type, from, to, department_id, employee_id` (a value that is not a well-formed id is stored as a hash), `include_reasons` as requested) plus `result:"denied"|"failed"`, `code`, `status`. Callers without `export.all`/`export.team` are rejected by the permission dependency and are not audited there.
  - The success row's `after` additionally has `include_reasons` (what was actually exported), `effective_from`, `daily_exports` and `daily_rows`.
  - **Capacity (L4/L5).** The build slot is taken before the row-count pre-flight and the counts run inside the same `EXPORT_TIMEOUT_S` budget (PostgreSQL: `statement_timeout` = budget + 5 s for the export transaction). At most `EXPORT_MAX_PENDING_DOWNLOADS` (default 8) workbooks may be built-or-waiting-for-download per process; more -> 503 `TEMPORARILY_UNAVAILABLE` with `Retry-After`. `EXPORT_TEMP_DIR` is REQUIRED (absolute path) when `APP_ENV=prod`: the process refuses to start without it; the directory is created with mode 0700 and the sweep only removes `ec-export-<pid>-*` directories of a dead process, or of this process when idle for an hour (never symlinks or foreign-owned directories).
  - **New error code:** 429 `EXPORT_QUOTA_EXCEEDED`. **New permission:** `export.sensitive`.

---

## 11. Notifications (push payloads, FCM data messages)

`{ "type": "leave_decision|permission_decision|new_request|announcement|birthday|admin_alert|profile_changed", "id": "uuid", "title","body" }`.
`admin_alert` (to `role.assign` holders; id = the affected user) and `profile_changed` (to the employee; id = their user id) carry names / field names only, never values.
Sent to device tokens only; no sensitive data in payload (no coordinates). Known limitation: `announcement` pushes currently carry the title and the
first 100 characters of the body through FCM; once the app has a push receiver, `important`/`urgent` ones should carry only type, id and a generic title.
Announcement push volume is limited server side (re-send cooldown, daily per-admin budget, high priority once per announcement; see section 7).
PROPOSED (section 13): `payslip_published` (id = payslip id, generic title/body, never amounts).

---

## 12. Remote console, offboarding, company, licensing, passwords (plan 11) - IMPLEMENTED

> Status: IMPLEMENTED in the backend (migration 0017). Plan: `docs/plans/11-remote-console-offboarding-company.md`.
> Decisions that override the plan text: unlicensed default is **50 seats** with **no expiry**; admins count as seats; the console is internet-reachable and
> **Admin only** (`console.access` is seeded for `admin` only; migration 0017 also granted it to `hr`, remove that grant, see DEPLOY/DB notes); password recovery is admin reset only.

### 12.1 Changed behaviour
- **Inactive/deleted user with a validly signed access token** -> 403 `ACCOUNT_INACTIVE` on any authenticated endpoint (was 401 `UNAUTHORIZED`).
- **POST /auth/refresh** with any token of an inactive/deleted user -> 403 `ACCOUNT_INACTIVE` (was 401 `SESSION_REVOKED`). Other revocations keep `SESSION_REVOKED`.
  Client rule: `ACCOUNT_INACTIVE` = full local wipe + "access removed" screen; `SESSION_REVOKED` = normal sign-out.
- **POST /auth/login** accepts optional `"app_version"` (<= 32 chars) and `"platform": "android|ios"`. These and `device_name`/`device_id` (control characters and markup stripped) are stored on the session and carried over on refresh rotation.
- **POST /admin/employees/{id}/deactivate** accepts an optional body `{ "reason": "resigned|terminated|contract_end|absconded|other" }` (default `other`).
  The Employee object gains `deactivated_at` (ts|null) and `deactivation_reason` (string|null). Activate clears both; delete stamps them when empty. After commit a data-only push `{ "type":"account_disabled" }` (no id/title/body) goes to the user's devices, then the tokens are deleted (deactivate and delete).
- **POST /admin/employees** and **POST /admin/employees/{id}/activate**: 409 `USER_LIMIT_REACHED` (`details:[{field:"max_users",message:"50"},{field:"active_users",message:"50"}]`) when no seat is free;
  403 `LICENSE_EXPIRED` after expiry + grace. The CLI `create_admin create` path applies the same check (the CLI has no separate reactivate command).
- **GET /auth/me** and **GET /config** gain `"company": { "name": "Acme"|null, "logo_url": "/api/public/company/logo?v=3"|null, "logo_version": 3, "configured": true }`.
  `/config` gains `"auth": { ..., "offline_cache_max_hours": 72 }` (0 = off).
- **POST /auth/reset-password** is REMOVED (404). Password recovery is admin reset only. **POST /auth/forgot-password** only queues a request for Admin/HR (no OTP, no email).
  Silent per-employee-id limits 3/h and 10/day (still 202). Push `{type:"reset_request", id:<requester user id>}` (title/body: name + employee id) to eligible reset admins, max 1 per requester per hour.
- Error `INVALID_OTP` is retired.
- New permissions: `company.manage` (admin), `console.access` (admin), `session.manage` (admin, hr; privilege rules apply), `system.view` (admin), `license.manage` (admin).
- New settings (validated like section 9): `console.session_max_hours` 1..24 (12), `console.inactivity_timeout_minutes` 5..60 (15), `auth.offline_cache_max_hours` 0 or 12..720 (72).
- New push types (section 11): `account_disabled` (no id, no text), `reset_request`.

### 12.2 Company (single company per deployment)
| Method | Path | Access |
|---|---|---|
| GET | /public/company | **Public**, 60/min/IP |
| GET | /public/company/logo?v= | **Public**, 60/min/IP |
| GET | /admin/company | `company.manage` |
| PUT | /admin/company | `company.manage` |
| POST | /admin/company/logo | `company.manage` (multipart `file`, png/jpeg <= 2 MB, upload limit 20/min) |
| DELETE | /admin/company/logo | `company.manage` |

**GET /public/company** -> `{ "product":"employee-connect", "api_version":"1", "configured":true, "name":"Acme Pvt Ltd"|null, "logo_url":"/api/public/company/logo?v=3"|null, "logo_version":3 }`.
**GET /public/company/logo** -> PNG bytes, `Cache-Control: public, max-age=86400`, `ETag` (`"<sha256>"`), 304 on `If-None-Match`. 404 if there is no logo. `?v=` is only a cache-buster.
**GET /admin/company** -> `{ "name","logo_url","logo_version","configured","setup_completed_at":"ts|null","deployment_id":"uuid","updated_at":"ts","updated_by":UserSummary|null }`.
**PUT /admin/company** `{ "name":"Acme Pvt Ltd" }` (2-100 chars after cleaning; control chars and markup stripped; unknown fields 422) -> 200 admin company object. The first successful PUT sets `setup_completed_at`. Audit `company.update`.
**POST /admin/company/logo** -> 200 admin company object (`logo_version` +1). Type by magic bytes; re-encoded to PNG, longest side <= 512 px, metadata and trailing data stripped. SVG/other -> 422 `FILE_TYPE_NOT_ALLOWED`, > 2 MB -> 422 `FILE_TOO_LARGE`. The old file is deleted. Audit `company.logo_update`.
`purpose=company_logo` is not accepted by `POST /files`.
**DELETE /admin/company/logo** -> 204 (`logo_version` +1; no logo = no-op). There is no unauthenticated setup endpoint: company setup is done by a signed-in `company.manage` holder, or `python -m scripts.create_admin create ... --company-name "Acme"`.

### 12.3 Console (web) auth
| Method | Path | Access |
|---|---|---|
| POST | /auth/web/login | Public; same rate limits/lockout/anti-enumeration as `/auth/login`; requires `console.access` |
| POST | /auth/web/refresh | Refresh cookie |
| POST | /auth/web/logout | Refresh cookie |
| POST | /auth/web/change-password | Bearer (`console.access`); forced first-login change from the console |

All four require header `X-EC-Client: console` and `Origin` equal to `PUBLIC_ORIGIN` (env, e.g. `https://hr.company.com`; unset = console auth disabled), else 403 `FORBIDDEN`.
**login** body `{ "employee_id","password" }` (unknown fields 422) -> 200 `{ "access_token","token_type":"Bearer","expires_in":900,"must_change_password":false,"user":{...as login} }` and
`Set-Cookie: __Secure-ec_rt=<opaque>; HttpOnly; Secure; SameSite=Strict; Path=/api/auth/web; Max-Age=<console.session_max_hours*3600>`. The refresh token is never in the body.
Errors as `/auth/login`, plus 403 `FORBIDDEN` without `console.access` (only after a correct password; audited as `auth.login_failed` reason `no_console_access`).
**refresh** (no body, cookie only) -> 200 same shape minus `user`, rotates the cookie. A missing cookie is 401 `UNAUTHORIZED`; errors as `/auth/refresh` (a mobile refresh token is not accepted here and vice versa); any 401/403 also clears the cookie.
The session's absolute lifetime is `console.session_max_hours`. Idle backstop: a refresh more than `console.inactivity_timeout_minutes` + 15 min (the access-token lifetime) after the previous one -> 401 `TOKEN_EXPIRED`; the console should also sign out by itself after the inactivity timeout.
**logout** -> 204 + cookie cleared; revokes only that web session. **change-password** body as `/auth/change-password`; the new refresh token goes into the cookie, the response body has no `refresh_token`.
Web sessions are stored with `client_type="web"`, `platform="web"` and appear in `/admin/sessions`.

### 12.4 Sessions and offboarding
| Method | Path | Access |
|---|---|---|
| GET | /admin/sessions?user_id=&employee_id=&client_type=mobile\|web&page=&page_size= | `session.manage` (audited `session.view`, 30/min) |
| GET | /admin/employees/{id}/sessions | `session.manage` + privilege rules (30/min) |
| DELETE | /admin/sessions/{id} | `session.manage` + privilege rules (target user) |
| POST | /admin/employees/{id}/revoke-sessions | `session.manage` + privilege rules |

Session item (`id` = refresh family id; only live families: not revoked, not expired):
`{ "id":"uuid","user":UserSummary,"client_type":"mobile|web","platform":"android|ios|web"|null,"device_name":"Pixel 8"|null,"device_id":"..."|null,"app_version":"1.4.0"|null,"ip":"203.0.113.7"|null,"started_at":"ts","last_seen_at":"ts","expires_at":"ts" }`
(`last_seen_at` = newest token issue time in the family). Lists are paginated, newest first, and only contain accounts the caller may manage (privilege rules: HR sees plain employees only; always your own).
DELETE -> 204: revokes the family and invalidates the user's access tokens (that device gets `SESSION_REVOKED` on refresh; the user's other devices simply refresh and carry on). Audit `session.revoke`.
revoke-sessions -> 200 `{ "revoked": 3 }` (all families, access tokens invalidated, the account stays active). Audit `session.revoke_all`. Unknown or out-of-scope id -> 404; privilege-rule violation -> 403 `FORBIDDEN`.

### 12.5 Console data
**GET /admin/dashboard** (`console.access`, 60/min) -> each block is `null` when the caller lacks its permission (given in brackets):
```json
{ "date":"2026-10-22",
  "employees": { "active":120, "inactive":8 },                                        // employee.view + employee.update|create (all scope)
  "attendance": { "checked_in":95, "checked_out":10, "late":7, "on_leave":6, "not_checked_in":19 }, // attendance.view_all
  "requests": { "leave_pending":4, "permission_pending":2 },                           // request.view_all
  "food": { "date":"2026-10-22", "count_yes":80, "count_no":20, "count_unanswered":20, "tomorrow_yes":60 }, // food.view_all
  "announcements": { "active":3 },                                                     // announcement.manage
  "sessions": { "mobile":110, "web":2 },                                               // session.manage
  "password_reset_requests_open": 1,                                                   // employee.reset_password
  "seats": { "used":120, "total":150, "status":"valid" } }                             // console.access
```
`checked_in` = everyone who punched in today (checked-out and late are subsets of it); `not_checked_in` excludes holidays, weekly offs and approved leave; `unanswered` counts active employees without a food answer.
**GET /admin/attendance/day?date=&status=all|checked_in|late|not_checked_in|on_leave&department_id=&q=&page=&page_size=** (`attendance.view_all`; `attendance.view_team` = direct reports) -> paginated
`{ "user":UserSummary, "status":"present|late|half_day|not_checked_in|on_leave|holiday|weekly_off", "check_in_at":"ts|null", "check_out_at":"ts|null", "minutes_late":0, "leave_type":"CL"|null }`
for every active employee in scope, ordered by employee id. `date` defaults to today (office tz) and must be within today - 366 days..today (422 `INVALID_DATE`). `status=checked_in` = present, late or half_day. No coordinates, distances or selfies.
**GET /admin/system/health** (`system.view`, 30/min) ->
`{ "status":"ok|degraded", "app_version":"1.2.0", "db":{"ok":true,"latency_ms":3,"migration":"0017"}, "redis":{"ok":true}, "worker":{"ok":true,"last_heartbeat_at":"ts"}, "backup":{"db_last_success_at":"ts|null","uploads_last_success_at":"ts|null","ok":true}, "storage":{"uploads_free_bytes":123,"uploads_free_pct":71}, "tls":{"not_after":"ts|null","days_left":54}, "push":{"configured":false} }`.
`degraded` means any `ok:false` (a missing `backup.json` counts), worker heartbeat older than 180 s (the worker writes Redis `ec:worker:heartbeat` every 60 s), backup older than 26 h, or TLS under 14 days. Env: `STATUS_DIR` (default `/status`, file `backup.json`), `TLS_CERT_FILE` (default `/certs/fullchain.pem`), `APP_VERSION`.

### 12.6 License (seat limit)
| Method | Path | Access |
|---|---|---|
| GET | /admin/license | `license.manage` |
| PUT | /admin/license | `license.manage` + step-up (`current_password` always required) |

License object: `{ "status":"valid|grace|expired|unlicensed|invalid", "license_id":"L-2026-0042"|null, "licensed_to":"Acme Pvt Ltd"|null, "max_users":50, "active_users":47, "seats_available":3,
"issued_at":"ts|null", "expires_at":"ts|null", "grace_until":"ts|null", "on_expiry":"block_new_users|read_only", "deployment_id":"uuid" }`.
**Unlicensed (no license installed, or a stored token that fails verification = `invalid`): `max_users` = 50 (code constant `UNLICENSED_MAX_USERS`, not an env value or setting), no expiry.** `expires_at = null` means the license never expires. Admins count as seats; deactivated and deleted users free their seat at once.
**PUT /admin/license** `{ "license_key":"ECL1.<payload>.<sig>", "current_password":"..." }` -> 200 license object. Errors: 422 `LICENSE_INVALID` (bad format/signature/unknown vendor key/`deployment_id` mismatch/bad fields; audited), 409 `LICENSE_OUTDATED` (`issued_at` older than the installed license), 403 `REAUTH_FAILED`, 429 (5/min per admin, fail closed). Audit `license.install` (license_id, max_users, expires_at; never the token). There is no other way to change the limit (not in settings or env). The CLI equivalent is `python -m scripts.create_admin license install FILE` (audited as `cli`) and `license show`.
Tokens are signed with the vendor Ed25519 key; the backend only contains the vendor PUBLIC key(s) (`app/core/license_keys.py`). Vendor tools: `tools/license/gen_vendor_key.py`, `tools/license/issue_license.py` (offline, outside `deploy/`).
Seat checks run in the same transaction as the insert/activate, after `SELECT ... FOR UPDATE` on the license row (company row when no license), always BEFORE any users row lock. Refusals are audited (`license.seat_refused`, outcome failure).
A license with fewer seats than active users deactivates nobody; new creates/activations are refused until the count is below the limit.
After `expires_at` the status is `grace` for `grace_days` (nothing blocked; daily `admin_alert` push to `license.manage` holders), then `expired`: seat-consuming actions answer 403 `LICENSE_EXPIRED`.
Expired + signed `on_expiry=read_only`: every non-GET request answers 403 `LICENSE_EXPIRED` except `/auth/*`, `/me/device-tokens`, deactivate, delete, revoke-sessions, `DELETE /admin/sessions/{id}` and `PUT /admin/license`. Reads, auth, change-password and export keep working.
New error codes: 409 `USER_LIMIT_REACHED`, 403 `LICENSE_EXPIRED`, 422 `LICENSE_INVALID`, 409 `LICENSE_OUTDATED`.

---

## 13. Payroll and Payslips (plan 12) - IMPLEMENTED (backend)

> Status: backend IMPLEMENTED (migration `0018_payroll`, `app/routers/payroll.py`). Plan: `docs/plans/12-payroll.md`. Flutter builds against this section; the "13.8 Implementation notes" below list every place where the backend settled something the plan left open. Errors use the standard envelope `{"error": {"code","message","details":[{"field","message"}],"request_id"}}` (section 1).

### 13.0 Conventions for this section
- **Money** is a JSON **string** with exactly 2 decimals, `^\d{1,8}\.\d{2}$`, range `"0.00"`..`"10000000.00"` (requests accept 0-2 decimals, e.g. `"25000"`). Never a float. Days are numbers with one decimal (`29.5`).
- `period` is `YYYY-MM` in JSON (stored as the 1st of the month).
- Every money, bank and identity field is encrypted at rest. None of them is ever written to audit rows, logs, push payloads or error messages.
- **Self rule:** no caller can change, revise, process or publish their OWN structure, payroll details or payslip -> 403 `FORBIDDEN`. Admin endpoints otherwise cover all employees (the s.9 privilege rules do not apply).
- Admin payroll routes: unknown ids -> 404. Employee routes: anything that is not the caller's published/superseded payslip -> 404.
- Codes: `^[A-Z][A-Z0-9_]{1,15}$`. Reserved codes (not usable for structure allowances/fixed deductions): `BASIC, HRA, PF, ESI, PT, TDS, LOP`, earnings adjustments `BONUS, ARREARS, INCENTIVE, REIMBURSEMENT, OTHER_EARNING`, deduction adjustments `ADVANCE_RECOVERY, LOAN_EMI, OTHER_DEDUCTION`.
- Labels/names: 1-40 chars, cleaned (control chars and markup stripped). Unknown body fields -> 422 (`extra=forbid`).
- Rate limits (per user, Redis; writes/exports fail closed -> 503 `TEMPORARILY_UNAVAILABLE`): employee payslip detail + PDF 30/min and 200/day; admin reads 120/min; draft writes 60/min; run/payslip actions 10/min; register 5/min; details `reveal` 30/min and 200/day; step-up 5/min (shared with s.9).
- License `read_only` mode (12.6): `POST /admin/payroll/runs/{id}/register` is treated as a read (exempt), like export.

### 13.1 Permissions
`payroll.view_own` (admin, hr, manager, employee; granted to every existing role by 0018), `payroll.view_all` (admin, hr; read-only admin views), `payroll.manage` (admin, hr; all writes except publish), `payroll.publish` (admin, hr).

### 13.2 Employee endpoints
| Method | Path | Perm |
|---|---|---|
| GET | /payslips?year=2026 | `payroll.view_own` |
| GET | /payslips/{id} | `payroll.view_own` (owner; status published or superseded) |
| GET | /payslips/{id}/pdf | same |

**GET /payslips?year=** (default current year, 2000..2200) -> un-paginated, newest month first, only the LIVE (published) revision per month. **No amounts.**
```json
{ "year": 2026,
  "items": [ { "id":"uuid","period":"2026-10","revision":2,"published_at":"2026-10-31T09:00:00Z","revised":true,"pay_date":"2026-10-31"|null } ] }
```
**GET /payslips/{id}** -> `Payslip` (audit `payroll.payslip.view_own`):
```json
{ "id":"uuid","period":"2026-10","revision":2,"status":"published|superseded","published_at":"ts","pay_date":"2026-10-31"|null,
  "company": { "name":"Aksan Capital Advisory Private Limited","address":"..."|null,"logo_url":"/api/public/company/logo?v=3"|null },
  "employee": { "employee_id":"EMP001","full_name":"A B","department":"Advisory"|null,"designation":"Analyst"|null,"join_date":"2024-06-01"|null,
                "pan":"XXXXX1234X"|null,"uan":"100200300400"|null,"esi_ip_number":"1234567890"|null,
                "bank_name":"HDFC Bank"|null,"bank_account":"XXXXXX1234"|null,"payment_mode":"bank_transfer|cheque|cash" },
  "days": { "basis_days":31.0,"employed_days":31.0,"lop_days":1.5,"paid_days":29.5 },
  "earnings":   [ { "code":"BASIC","label":"Basic","monthly":"20000.00","amount":"19032.00" },
                  { "code":"BONUS","label":"Diwali bonus","monthly":null,"amount":"5000.00" } ],
  "deductions": [ { "code":"PF","label":"Provident Fund","amount":"1800.00" },
                  { "code":"TDS","label":"Income Tax (TDS)","amount":"1200.00" } ],
  "employer_contributions": [ { "code":"PF_ER","label":"Employer PF","amount":"1800.00" } ] | null,
  "totals": { "gross_earnings":"38500.00","total_deductions":"3200.00","net_pay":"35300.00",
              "net_pay_words":"Rupees Thirty-Five Thousand Three Hundred Only" },
  "revision_info": { "reason":"Leave approved late","supersedes_id":"uuid","superseded_by_id":null } | null }
```
`monthly` = structure amount before proration (null for adjustments). `employer_contributions` is null unless `payroll.show_employer_contributions`. `pan` is masked when `payroll.mask_pan` (default); `bank_account` is always masked to the last 4.
`revision_info` is null for revision 1 with no successor. A superseded payslip is still readable by its owner (`superseded_by_id` set) so the app can show history.
**GET /payslips/{id}/pdf** -> `application/pdf`, `Content-Disposition: attachment; filename="payslip-EMP001-2026-10.pdf"` (`-r2` suffix for revisions), `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`. Audit `payroll.payslip.download`.
Errors: 404, 429, 503 `DATA_UNREADABLE` (undecryptable), 503 `TEMPORARILY_UNAVAILABLE` (PDF slots busy, `Retry-After`).

### 13.3 Payroll settings
| GET | /admin/payroll/settings | `payroll.view_all` |
| PUT | /admin/payroll/settings | `payroll.manage` (merge; audit `payroll.settings.update` with key/before/after). Changing `payroll.publish_requires_second_person` additionally needs `role.assign` AND `current_password` (see below) |

```json
{ "settings": {
  "payroll.pay_days_basis":"calendar",            // calendar | fixed_30 | working_days
  "payroll.lop_absent_days":true, "payroll.lop_half_day_attendance":true,
  "payroll.pf_employee_rate_pct":12, "payroll.pf_employer_rate_pct":12, "payroll.pf_wage_ceiling":15000,   // 0..20, 0..20, 0..1000000 (0 = none)
  "payroll.esi_wage_threshold":20000, "payroll.esi_employee_rate_pct":0.75, "payroll.esi_employer_rate_pct":3.25,  // 0..1000000, 0..10, 0..10
  "payroll.pt_frequency":"half_yearly",           // monthly | half_yearly (default half_yearly = Tamil Nadu: PT in Mar + Sep, slab on half-yearly gross)
  "payroll.pt_slabs":[ { "min_gross":"25000.00","max_gross":null,"amount":"200.00","months":null },
                       { "min_gross":"25000.00","max_gross":null,"amount":"300.00","months":[2] } ],
  "payroll.rounding":"rupee",                     // rupee | paise
  "payroll.show_employer_contributions":false, "payroll.employer_share_from_employee":true, "payroll.mask_pan":true, "payroll.publish_requires_second_person":false,
  "payroll.company_address":"", "payroll.payslip_footer":"This is a computer-generated payslip and does not require a signature." } }
```
PT slabs: <= 20 rows, `min_gross <= max_gross`. For any month, the slabs that apply to that month must not overlap. A slab with `months` wins over one with `months:null` for the same range. Default = the Tamil Nadu half-yearly slabs (see plan 12 section 4.1; to be verified against current rules), `months:[3,9]`; `[]` = no PT.
PUT body: `{ "settings": { ... }, "current_password": "<optional>" }`. `current_password` (the caller's OWN password) is required ONLY when the request changes the stored value of `payroll.publish_requires_second_person` (turning it on or off; sending the value it already has is not a change). That change also needs the `role.assign` permission (admin; `payroll.manage` alone is not enough). Errors for it: 403 `FORBIDDEN` (no `role.assign`), 422 `VALIDATION_ERROR` (`current_password` missing), 403 `REAUTH_FAILED`. All other keys behave as before and need no password. Existing clients that never touch this key are unaffected.
Rates allow 2 decimals. Errors: 422 `VALIDATION_ERROR` (`details.field` = key). These keys are not accepted by `PUT /admin/settings`. Changes never affect existing runs until `refresh` (draft) because runs snapshot the settings.

### 13.4 Employees, structures, payroll details
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/employees?q=&department_id=&has_structure=true\|false&status=active\|inactive&page=&page_size= | `payroll.view_all` |
| GET | /admin/payroll/employees/{user_id}/structures | `payroll.view_all` (audit `payroll.structure.view`) |
| POST | /admin/payroll/employees/{user_id}/structures | `payroll.manage` |
| PUT | /admin/payroll/structures/{id} | `payroll.manage` |
| DELETE | /admin/payroll/structures/{id} | `payroll.manage` |
| GET | /admin/payroll/employees/{user_id}/details?reveal=false | `payroll.view_all` (audit `payroll.details.view`, `after.revealed`) |
| PUT | /admin/payroll/employees/{user_id}/details | `payroll.manage` + step-up (`current_password` always required) |

Employees list item: `{ "user":UserSummary, "status":"active|inactive", "include_in_payroll":true, "current_structure": { "id","effective_from":"2026-04-01","monthly_gross":"40000.00" } | null, "flags":["NO_BANK_DETAILS","NO_PAN"] }` (paginated).
**Structure** (request = the same fields without the read-only ones):
```json
{ "id":"uuid","user_id":"uuid","effective_from":"2026-04-01","effective_to":"2026-09-30"|null,
  "basic":"20000.00","hra":"8000.00",
  "allowances":[ { "code":"SPECIAL","name":"Special allowance","amount":"10000.00","prorate":true,"esi_wage":true } ],
  "fixed_deductions":[ { "code":"CANTEEN","name":"Canteen","amount":"500.00" } ],
  "tds_monthly":"1200.00",
  "pf_applicable":true,"pf_on_full_basic":false,"esi_applicable":true,"pt_applicable":true,
  "monthly_gross":"38000.00","in_use":true,
  "created_at":"ts","created_by":UserSummary|null,"updated_at":"ts" }
```
Read-only: `id, user_id, effective_to` (day before the next structure), `monthly_gross` (basic + hra + allowances), `in_use`, `created_*`, `updated_at`. `effective_from` must be the 1st of a month (422 `INVALID_PERIOD`). `basic` > 0; <= 15 allowances, <= 10 fixed deductions, unique codes.
GET structures -> `{ "items":[Structure] }`, newest first. POST -> 201. PUT = full replace. DELETE -> 204.
Errors: 409 `STRUCTURE_EXISTS` (same `effective_from`), 409 `STRUCTURE_IN_USE` (PUT/DELETE when a processed/published payslip uses it), 403 `FORBIDDEN` (own), 422.
Audit `payroll.structure.create|update|delete` with `after={effective_from, fields:[changed names]}`.
**Payroll details**:
```json
{ "user_id":"uuid","designation":"Analyst"|null,"payment_mode":"bank_transfer","include_in_payroll":true,
  "bank_name":"HDFC Bank"|null,"bank_account":"XXXXXX1234"|null,"bank_ifsc":"HDFC0001234"|null,
  "pan":"XXXXX1234X"|null,"uan":"XXXXXXXX0400"|null,"esi_ip_number":"XXXXXX7890"|null,
  "revealed":false,"updated_at":"ts|null","updated_by":UserSummary|null }
```
`?reveal=true` returns unmasked values. PUT body: the same editable fields (`designation, payment_mode, include_in_payroll, bank_name, bank_account, bank_ifsc, pan, uan, esi_ip_number`) + `current_password`. Only sent keys change; `null` clears. Formats: PAN `^[A-Z]{5}[0-9]{4}[A-Z]$`, IFSC `^[A-Z]{4}0[A-Z0-9]{6}$`, account `^\d{9,18}$`, UAN `^\d{12}$`, ESI IP `^\d{10}$` (upper-cased, spaces removed) -> 200 masked object. Errors: 403 `REAUTH_FAILED`, 403 `FORBIDDEN` (own), 422. Audit `payroll.details.update` `after={fields:[names]}`.

### 13.5 Runs
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/runs?year= | `payroll.view_all` |
| POST | /admin/payroll/runs | `payroll.manage` |
| GET | /admin/payroll/runs/{id} | `payroll.view_all` (audit `payroll.run.view`) |
| DELETE | /admin/payroll/runs/{id} | `payroll.manage` (draft and never published) -> 204 |
| POST | /admin/payroll/runs/{id}/refresh | `payroll.manage` (draft) |
| POST | /admin/payroll/runs/{id}/process | `payroll.manage` (draft) |
| POST | /admin/payroll/runs/{id}/reopen | `payroll.manage` (processed) |
| POST | /admin/payroll/runs/{id}/publish | `payroll.publish` + step-up |
| GET | /admin/payroll/runs/{id}/payslips?q=&status=&flag=error\|warning\|hold&page=&page_size= | `payroll.view_all` |
| POST | /admin/payroll/runs/{id}/employees/{user_id}/revise | `payroll.manage` |
| POST | /admin/payroll/runs/{id}/register | `payroll.view_all` (+ `payroll.manage` and step-up when `include_bank`) |

**POST /admin/payroll/runs** `{ "period":"2026-10","pay_date":"2026-10-31"|null }` -> 201 `Run`. Draft payslips are created for every eligible employee (plan 4.2).
Errors: 409 `PAYROLL_RUN_EXISTS`, 422 `INVALID_PERIOD` (future month, malformed, outside 2000..2200).
**Run**:
```json
{ "id":"uuid","period":"2026-10","status":"draft|processed|published","pay_date":"2026-10-31"|null,"attendance_cutoff":"2026-10-25",
  "counts": { "employees":48,"draft":2,"processed":45,"published":0,"held":1,"errors":0,"warnings":3,"open_revisions":0 },
  "totals": { "gross_earnings":"1850000.00","total_deductions":"160000.00","net_pay":"1690000.00","employer_pf":"95000.00","employer_esi":"4200.00" },
  "config": { "pay_days_basis":"calendar","pf_employee_rate_pct":12, "...":"snapshot of payroll.* settings" },
  "company_name":null,"created_at":"ts","created_by":UserSummary,"processed_at":"ts|null","processed_by":UserSummary|null,
  "published_at":"ts|null","published_by":UserSummary|null }
```
`totals` cover the live copy per employee (published, else the open one), excluding held payslips. List item: `{ id, period, status, employees, net_pay_total, published_at }` (un-paginated, newest first).
**refresh** -> 200 Run (re-snapshots settings, sets `attendance_cutoff` = min(period end, today), re-pulls structures/LOP/identity for drafts, adds newly eligible employees, keeps manual inputs).
**process** -> 200 Run. Blocked -> 409 `PAYROLL_BLOCKED` `details:[{ "field":"EMP014","message":"STRUCTURE_MISSING" }, ...]` and nothing changes. Held payslips stay draft.
**reopen** -> 200 Run (processed payslips back to draft). 409 `PAYROLL_LOCKED` after publish.
**publish** `{ "current_password":"..." }` -> 200 Run. Publishes every processed, non-held payslip and locks the run. After commit, a push `payslip_published` goes to each employee.
Errors: 409 `PAYROLL_LOCKED` (not processed / already published), 409 `NOTHING_TO_PUBLISH`, 403 `REAUTH_FAILED`, 403 `SECOND_PERSON_REQUIRED` (rule on, and the caller processed the run or is the last editor / processor of a payslip being published; see 13.8).
Self rule at publish: a run that contains the caller's own processed payslip CAN be published by the caller (otherwise a company with one HR person could never publish), but the caller can never have edited that payslip. The audit row records `self_included:true`. The single-payslip publish (13.6) of your own payslip is 403 `FORBIDDEN`.
**Payslip list item** (admin): `{ "id","user":UserSummary,"revision":1,"status","hold":false,"paid_days":29.5,"lop_days":1.5,"gross_earnings":"..","total_deductions":"..","net_pay":"..","errors":["NEGATIVE_NET_PAY"],"warnings":["PENDING_LEAVE"] }`. Draft rows with an error have null money fields.
**revise** `{ "reason":"Leave approved after payroll" }` (3-200 chars) -> 201 `AdminPayslip` (new draft `revision+1` copied from the live published payslip, LOP re-pulled, inputs kept; or revision 1 when the employee has no payslip in a published run).
Errors: 409 `PAYROLL_LOCKED` (run not published), 409 `CONFLICT` (an open revision already exists), 403 `FORBIDDEN` (own), 404.
**register** `{ "include_bank":false,"current_password":"<only when include_bank>" }` -> `.xlsx` (sheets `Register`, `Summary`, and `Bank` when `include_bank`; plan section 5).
`filename="payroll-register-2026-10.xlsx"`, `Cache-Control: no-store`. Audit `payroll.register.export` (`after={include_bank, rows}`). Errors: 403 `FORBIDDEN`, 403 `REAUTH_FAILED`, 429, 503.

### 13.6 Payslips (admin)
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/payslips/{id} | `payroll.view_all` (audit `payroll.payslip.view`) |
| PUT | /admin/payroll/payslips/{id} | `payroll.manage` (draft only) |
| DELETE | /admin/payroll/payslips/{id} | `payroll.manage` (draft only) -> 204 |
| POST | /admin/payroll/payslips/{id}/process | `payroll.manage` (draft, run published = held or revision) |
| POST | /admin/payroll/payslips/{id}/reopen | `payroll.manage` (processed, run published) |
| POST | /admin/payroll/payslips/{id}/publish | `payroll.publish` + step-up (processed, run published) |
| GET | /admin/payroll/payslips/{id}/pdf | `payroll.view_all` (non-published copies watermarked "DRAFT - NOT VALID"; audit `payroll.payslip.download`) |

**AdminPayslip** = `Payslip` (13.2) plus (note: in AdminPayslip `employee.pan`, `employee.uan` and `employee.esi_ip_number` are ALWAYS masked, e.g. `XXXXX1234X`, `XXXXXXXX0400`, `XXXXXX7890`, whatever `payroll.mask_pan` says; the admin PDF is masked the same way. Unmasked values only via `GET .../details?reveal=true`. The employee's own `Payslip` / PDF is unchanged):
```json
{ "run_id":"uuid","user":UserSummary,"status":"draft|processed|published|superseded","hold":false,"version":3,"structure_id":"uuid"|null,
  "lop": { "auto":1.5,"override":null,"override_reason":null,
           "breakdown": { "unpaid_leave":1.0,"absent":0.0,"half_day_attendance":0.5,"not_employed":0.0,"pending_leave_days":0.0 } },
  "inputs": { "tds":"1200.00","pt_override":null,
              "adjustments":[ { "code":"BONUS","label":"Diwali bonus","amount":"5000.00" } ],"note":"..."|null },
  "errors":[],"warnings":["LOP_OVERRIDDEN"],
  "processed_at":"ts|null","processed_by":UserSummary|null,"published_by":UserSummary|null,"updated_at":"ts" }
```
In a draft with an error, `earnings`/`deductions` are `[]` and `totals` is null.
**PUT** (draft only):
```json
{ "expected_version":3,
  "lop_override":1.0, "lop_override_reason":"Forgot to check in on 14th, confirmed present",
  "tds":"1200.00","pt_override":null,
  "adjustments":[ { "code":"ARREARS","label":"Sept increment arrears","amount":"2500.00" } ],
  "hold":false,"note":null }
```
Only sent keys change; `null` clears an override. `lop_override` 0..employed_days in 0.5 steps, reason 3-200 chars required with it. <= 10 adjustments. `pt_override` >= 0.
-> 200 AdminPayslip, recomputed. Audit `payroll.payslip.update` `after={fields:[names]}`.
Errors: 409 `VERSION_CONFLICT` (reload), 409 `PAYROLL_LOCKED` (not draft), 403 `FORBIDDEN` (own payslip), 422.
**process** -> 200 (409 `PAYROLL_BLOCKED` as for runs). **publish** `{ "current_password" }` -> 200. If it is a revision, the previous published copy becomes `superseded` in the same transaction. After commit, push `payslip_published`. Same errors as run publish.
Audit actions: `payroll.payslip.process|reopen|publish|delete|revise`.

### 13.7 Push, errors
- Push (section 11) `{ "type":"payslip_published", "id":"<payslip id>", "title":"Payslip available"|"Revised payslip available", "body":"Your payslip for October 2026 is ready." }`. Never amounts. Best effort, after commit.
- New error codes: 409 `PAYROLL_RUN_EXISTS`, `PAYROLL_LOCKED`, `PAYROLL_BLOCKED`, `NOTHING_TO_PUBLISH`, `STRUCTURE_EXISTS`, `STRUCTURE_IN_USE`, `VERSION_CONFLICT`; 422 `INVALID_PERIOD`; 403 `SECOND_PERSON_REQUIRED`. Reused: `FORBIDDEN`, `REAUTH_FAILED`, `DATA_UNREADABLE`, `TEMPORARILY_UNAVAILABLE`, `RATE_LIMITED`.
- Payslip error codes (in `errors`, block processing): `STRUCTURE_MISSING`, `NEGATIVE_NET_PAY`. Warning codes: `NO_SHIFT`, `PENDING_LEAVE`, `LOP_OVERRIDDEN`, `ZERO_PAID_DAYS`, `NO_BANK_DETAILS`, `NO_PAN`, `STRUCTURE_CHANGED`, `NOT_ELIGIBLE`.

### 13.8 Implementation notes (decisions the backend took; none changes a shape above unless stated)
- **ESI threshold (changed):** `payroll.esi_wage_threshold` defaults to 20000 (was 21000). Eligibility is still decided on the monthly gross with an inclusive comparison: gross <= 20000 gets ESI, gross above 20000 gets none. Editable setting.
- **Employer share recovery (new):** `payroll.employer_share_from_employee` (bool, default true). When true the payslip `deductions` contain extra lines `PF_ER_REC` "PF - employer share (recovered)" and `ESI_ER_REC` "ESI - employer share (recovered)" (equal to the employer contribution), and the `PF`/`ESI` lines are labelled "PF - employee share" / "ESI - employee share"; `total_deductions` and `net_pay` include both. `employer_contributions` (if shown) still lists what the company remits. `Run.totals` gains `employer_recovered` (sum of the recovered lines) and `employer_cost` (= employer_pf + employer_esi - employer_recovered, so 0 when on and the rule is applied to everyone). Register columns: `PF`, `ESI` (employee share), `PF Employer Share Recovered`, `ESI Employer Share Recovered`, `Employer PF`, `Employer ESI` (remitted), `Employer Cost (net of recovery)`. Clients must not hard-code the PF/ESI deduction lines: render `deductions` as sent. Codes `PF_ER_REC`, `ESI_ER_REC` are reserved.
- **Money in lists:** `Run.totals`, payslip list money and register amounts are computed in Python from the decrypted live copy. `net_pay_total` of a run list item = the run's `totals.net_pay`.
- **Rounding:** `payroll.rounding=rupee` rounds every COMPUTED line (prorated earnings, PF, ESI, PT) half-up to the rupee. Amounts typed by HR (adjustments, fixed deductions, TDS, PT override) are used exactly as entered. **ESI contributions (employee and employer) are always rounded UP to the next rupee** (statutory rule; the plan said "ESI wage"). BASIC and HRA are always ESI wages; allowances use their `esi_wage` flag.
- **PT:** the slab is looked up on the gross rounded UP to a whole rupee. `half_yearly` = gross of this month plus the live copy (published, else open) of the previous 5 calendar months that exist in payroll (missing months count as 0). A slab with `months: null` deducts in every month in `half_yearly` mode too. `pt_override` applies whenever it is set (also when the structure has `pt_applicable=false`).
- **TDS:** the payslip stores `inputs.tds = null` meaning "structure default"; the API returns the EFFECTIVE value in `inputs.tds` (so the editor shows the default). Sending `tds: null` returns to the default.
- **Errors vs warnings:** both are stored as codes in `payslips.warnings`; `errors` (`STRUCTURE_MISSING`, `NEGATIVE_NET_PAY`) are split out when reading. A payslip with an error has no stored result, so `earnings: []`, `totals: null` and null money in lists.
- **LOP:** approved leave covers an absent day; a pending request does NOT (the day stays absent LOP and `pending_leave_days` + warning `PENDING_LEAVE` tell HR to resolve it, then refresh). An approved half-day leave with no attendance row leaves 0.5 day absent. `lop_auto` is capped at the employed days. `lop_override_reason` (3-200 chars) is REQUIRED whenever `lop_override` is sent with a value.
- **Run/payslip status codes:** `process`/`refresh` on a published run -> 409 `PAYROLL_LOCKED`; on a run that is already `processed` -> 409 `CONFLICT`. `reopen` on a draft run -> 409 `CONFLICT`. Payslip-level `process` on a run that is not published -> 409 `CONFLICT`. A draft payslip that is `processed` individually leaves `hold` (set to false). `PUT`/`DELETE` of a non-draft payslip -> 409 `PAYROLL_LOCKED`.
- **Lists:** `GET /admin/payroll/runs/{id}/payslips` lists every non-superseded copy, so an employee with an open revision appears twice (`published` revision 1 and `draft` revision 2); `status=superseded` shows old copies. Sorted by employee name. `GET .../employees` items also carry `include_in_payroll`.
- **Structures:** deleting a structure that only DRAFT payslips reference clears `structure_id` on those drafts (they re-resolve on the next refresh/process). A draft saved with `PUT` keeps the structure it was computed with and shows `STRUCTURE_CHANGED` when a newer one applies; `refresh` and `process` adopt the newest.
- **Details:** `GET .../details` for an employee without a row returns the defaults (`payment_mode: bank_transfer`, `include_in_payroll: true`, all else null). `PUT` audit `after.fields` lists only the fields that actually changed.
- **Payslip PDF:** extra watermark `SUPERSEDED` for superseded copies; draft/processed copies carry `DRAFT - NOT VALID`. Response also sends `Content-Security-Policy: default-src 'none'`.
- **Audit:** `include_bank` and `self_included` are allow-listed in the audit redactor; `payroll.settings.update` records `payroll.pt_slabs` as a JSON string (the generic redactor would otherwise mask the slab `amount`s). `payroll.denied` is written for the self rule and for employee-route 404 probes; a plain missing-permission 403 is not audited.
- **Locking:** database trigger errors (SQLSTATE 23000) are mapped to 409 `PAYROLL_LOCKED`; unique violations (23505) to 409 `CONFLICT` (`PAYROLL_RUN_EXISTS` / `STRUCTURE_EXISTS` on the creating endpoints).
- **Maker-checker (review 10, M1):** `SECOND_PERSON_REQUIRED` is enforced at run publish and payslip publish when the run's snapshot OR the CURRENT `payroll.publish_requires_second_person` setting is on (switching it off after a run was created does not release that run). The publisher must not be the person who processed the run (`processed_by`), nor the `processed_by` / `updated_by` (last editor) of any payslip being published. Changing the setting needs `role.assign` + `current_password` (13.3).
- **Admin masking (review 10, M2):** AdminPayslip JSON and the admin PDF always mask PAN / UAN / ESI IP number (13.6). The employee's own payslip JSON/PDF still shows UAN / ESI IP in full and PAN per `payroll.mask_pan`.
- **Successor lookup (review 10, L2):** `revision_info.superseded_by_id` in the EMPLOYEE view only points to a published (or already superseded) successor; an unpublished draft revision is invisible to the employee. The admin view still shows it.
- **Structures (review 10, L1):** process and publish share-lock (`FOR SHARE`) the structures they read, so a structure cannot change between the calculation and the payslip becoming processed; delete clears `structure_id` only on DRAFT payslips. The database refuses UPDATE / DELETE of a structure used by a processed / published / superseded payslip.
- **PDF (review 10, L3/L4):** both PDF endpoints fail closed (503 `TEMPORARILY_UNAVAILABLE`) when the rate-limit store is down. A timed-out PDF build keeps its slot until its worker thread ends; logos over 1 MB or 4 million pixels are not drawn.
- **Database locks (review 10, H1/L6):** migration 0018 allows `*_enc`-only updates of payslips and structures when the transaction has `SET LOCAL app.key_rotation = 'on'` (set only by `python -m scripts.rotate_keys`), so key rotation works on published/superseded rows. A published -> superseded change without a published successor in the same transaction fails at commit.
- **Rate limit keys** (per user): `payroll:own_list` 60/min, `own_detail` 30/min + 200/day (detail and pdf share it), `admin_read` 120/min, `write` 60/min (fail closed), `action` 10/min (fail closed), `register` 5/min (fail closed), `reveal` 30/min + 200/day (fail closed).
