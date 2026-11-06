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
| Leave types | /admin/leave-types | `leave_type.manage` (GET: any authed) | `code, name, annual_quota, is_paid, allow_half_day, requires_attachment_after_days, max_consecutive_days, is_active` (plan 15 adds `encashable`, `encash_max_days_per_year`, `encash_min_balance`, see 17.1) |
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
**PUT leave-balances** `{ "year":2026,"items":[{"leave_type_id","quota":12.0}] }` (quota cannot go below used+pending+encashed -> 409 `QUOTA_BELOW_USED`). 403 `FORBIDDEN` for your own balance (review 06 M3) and for peer / more privileged accounts (the privilege rules above apply).
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
- **Leave balances.** `GET /admin/employees/{id}/leave-balances?year=` (default current year) -> `{year,items:[{leave_type:{id,code,name},quota,used,pending,encashed,remaining}]}` for active types; read only (a type with no row yet shows the quota it would get).
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
PROPOSED (section 17.7): `loan_created`, `reimbursement_submitted`, `reimbursement_decision` (never amounts).

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
- Codes: `^[A-Z][A-Z0-9_]{1,15}$`. Reserved codes (not usable for structure allowances/fixed deductions): `BASIC, HRA, PF, ESI, PT, TDS, LOP`, earnings adjustments `BONUS, ARREARS, INCENTIVE, REIMBURSEMENT, OTHER_EARNING`, deduction adjustments `ADVANCE_RECOVERY, LOAN_EMI, OTHER_DEDUCTION`. PROPOSED (17.1): system-only `OVERTIME`, `LEAVE_ENCASH`, and read-only `inputs.components`.
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

## 15. Payroll phase 2 (plan 14) - IMPLEMENTED: phase 1 (statutory: section 16)

### 15.1 Dashboard, variance, reports (permission `payroll.view_all`; read-only)
| Method | Path | Notes |
|---|---|---|
| GET | /admin/payroll/dashboard?period=YYYY-MM | default current month. `{period, run:{id,status}\|null, headcount, held, errors, warnings, totals:{gross,deductions,net,employer_cost}, departments[], trend[12], joiners[], leavers[], calendar:{cutoff_day, process_by, pay_date, alerts[]}}`. Money = 2-decimal strings. Alerts: `RUN_NOT_CREATED`, `RUN_NOT_PROCESSED`, `RUN_NOT_PUBLISHED`. Audit `payroll.dashboard.view`. |
| GET | /admin/payroll/variance?period= | `{period, previous, items:[{employee_id,name,department,gross_prev,gross,net_prev,net,net_delta,lop_prev,lop,reasons[]}]}`; reasons `NEW`, `LEFT_OR_NOT_PAID`, `STRUCTURE_CHANGE`, `LOP_CHANGE`, `ADJUSTMENTS`, `PT_CHANGE`. Sorted by absolute net change. |
| POST | /admin/payroll/reports/{kind}?period= | kind `department\|headcount\|ctc\|lop\|bank_advice`; body `{current_password?}`; returns .xlsx (no-store). `bank_advice` needs `payroll.manage` + `current_password` (step-up). 404 when no run for the month. Audit `payroll.report.export`. |

### 15.2 New settings (`/admin/payroll/settings`, integers 1..31)
`payroll.cutoff_day` (25), `payroll.process_day` (28, in the period month), `payroll.pay_day` (1, in the following month).

## 16. Payroll statutory (plan 14 phase 2) - IMPLEMENTED (backend, migrations 0020 and 0021)

All endpoints are under `/api/admin/payroll/statutory`. Only PUBLISHED runs are used (one published copy per employee). A month without a run is 404 `NOT_FOUND`; a run that is not published is 409 `RUN_NOT_PUBLISHED` (the dues list shows `run_status` and no items instead). Money in responses is a 2-decimal string. Money, UAN, ESI IP and PAN are never written to the audit log or application logs.

**The EPFO ECR and ESIC column layouts below follow the formats known when this was written. The user must verify them against the current EPFO / ESIC portals before filing.**

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | /statutory/dues?period=YYYY-MM&months=1..12 | `payroll.view_all` | `period` is the LAST month listed (default current), `months` default 1. `{months:[{period, run_status\|null, items:[{type pf\|esi\|pt\|tds, amount, employees, status paid\|unpaid, paid_on, reference, paid_amount, mismatch}], blockers[]}]}`. PF due = employee + employer share, ESI due = employee + employer share, PT and TDS = sum of deductions. `mismatch` = challan amount differs from the computed due. Audit `payroll.statutory.dues_view`. |
| POST | /statutory/pf_ecr?period= | `payroll.view_all` + step-up | Body `{current_password}` REQUIRED (422 `VALIDATION_ERROR` when missing, 403 `REAUTH_FAILED` when wrong; the step-up limit of 5/min applies). PF ECR text file (`text/plain`, `\r\n` lines, fields joined by `#~#`, no header): UAN, member name, gross wages, EPF wages, EPS wages, EDLI wages, EPF contribution (employee), EPS contribution, EPF-EPS difference (employer), NCP days, refund of advances. Whole rupees. EPF wages = basic capped at `payroll.pf_wage_ceiling` (full basic when the structure says so); EPS / EDLI wages = EPF wages capped at 15,000; EPS = 8.33% of EPS wages; difference = employer share - EPS; NCP days = ceil(basis days - paid days); refund = 0. Only employees with PF > 0. Member name: ASCII letters, space, `.`, `'` and `-` only (other ASCII characters become a space, spaces collapsed, max 85 chars); a name with non-ASCII characters or nothing left after cleaning is a `NAME` blocker (never written as `?`). Identifiers (UAN) are read from the employee's CURRENT payroll details. |
| POST | /statutory/esi?period= | `payroll.view_all` + step-up | Body `{current_password}` REQUIRED (as for pf_ecr). .xlsx, sheets `ESI Contribution` (IP Number, IP Name, No of Days, Total Monthly Wages, Reason Code for 0 wages, Last Working Day) and `Reconciliation` (wages, employee, employer share). Only employees with ESI > 0. ESI IP numbers are read from the employee's CURRENT payroll details. |
| POST | /statutory/pt?period= | `payroll.view_all` | .xlsx, sheets `PT by Employee` (id, name, department, gross, slab, PT) and `PT by Slab` (slab, PT, employees, total). Slab is matched on the PT amount against the run's `pt_slabs` snapshot (TN half-yearly slabs apply to the 6-month gross). No identifiers needed, no password (an optional body is ignored). |
| PUT | /statutory/challans | `payroll.manage` | Body `{type, period:"YYYY-MM", status:"paid"\|"unpaid", paid_on?, reference?, amount?, current_password}`. `current_password` (step-up) always required. `paid`: `paid_on` required (not before the period start, not in the future), `reference` 1-60 chars `[A-Za-z0-9 ._/-]`, `amount` (string, must be > 0, else 422) defaults to the computed due; 409 `NOTHING_DUE` when no `amount` is given and the computed due is 0; 409 `CHALLAN_ALREADY_PAID` if already paid. `unpaid`: no other fields; clears paid_on and reference; 409 `CHALLAN_NOT_PAID` if not paid. Returns `{type, period, status, paid_on, reference}`. Audit `payroll.statutory.challan_paid` / `challan_unpaid` (entity_id = challan id; type, period, status and, for paid, `paid_on`; never amount or reference). EVERY change also appends a row to `statutory_challan_events` (migration 0021: action, status, paid_on, encrypted reference and amount, plus the `prev_*` values that were overwritten or cleared, actor, time). The table is append-only (UPDATE / DELETE / TRUNCATE revoked from `app_user` and blocked by trigger, except key rotation of the encrypted columns); `statutory_challans` rows can no longer be deleted by `app_user`. Not exposed through the API. |

File endpoints: audit `payroll.statutory.export` (kind, period, row count), `Cache-Control: no-store`. `pf_ecr` and `esi` (they contain UAN / ESI IP numbers): rate limit `payroll:statutory_file` 10/min and 30/day (fail closed) plus the step-up limit, failed step-ups audited as `admin.reauth_failed`, and a best-effort `admin_alert` push to the `role.assign` holders after each download. `pt`: `payroll:report` 10/min (fail closed). Dues: `payroll:statutory` 30/min and 500/day (fail closed). Challan write: `payroll:write` 60/min (fail closed) plus the step-up limit.

**Blockers.** Amounts come from the frozen payslip, but identifiers are read from the employee's CURRENT payroll details (`PUT /employees/{id}/details`), so correcting them clears a blocker immediately without re-running payroll. Employees with a PF amount but no valid UAN (12 digits) or no usable ECR name (`NAME`), an ESI amount but no valid ESI IP number (10-17 digits), or a TDS amount but no valid PAN are never skipped silently. `dues` lists them in `blockers: [{employee_id, name, missing:[{field: UAN\|ESI_IP\|PAN\|NAME, reason: missing\|invalid}]}]`; PF ECR and ESI file requests are refused with 409 `STATUTORY_BLOCKERS` (`details[]` = `{field: employee_id, message}`) until fixed. Identifier values are never returned. PT files have no blockers.

Errors: `RUN_NOT_PUBLISHED` 409, `STATUTORY_BLOCKERS` 409, `CHALLAN_ALREADY_PAID` 409, `CHALLAN_NOT_PAID` 409, `INVALID_DATE` 422, `REAUTH_FAILED` 403.

## 17. Payroll pay components (plan 15 = plan 14 phase 3) - 3a implemented in the backend (migration 0022; step 1 = 17.2, step 2 = 17.3 to 17.5), 3b implemented in the backend (migration 0023; 17.6 to 17.9, loan numbering fix in 17.3)

Plan: `docs/plans/15-payroll-phase3.md`. Section 13.0 conventions apply (money strings, `period` = `YYYY-MM`, labels 1-40 / reasons 3-200 chars cleaned, unknown fields 422,
self rule 403 `FORBIDDEN`, admin unknown ids 404, employee routes 404 for anything not their own). Money, labels, reasons, descriptions and comments are never written to audit rows,
logs, push payloads or error messages. Sections marked **[3b]** ship with work package 3b.

### 17.0 Common
**Permissions.** Reads `payroll.view_all`; admin writes `payroll.manage`; employee loan/overtime views `payroll.view_own`; NEW `reimbursement.submit` (seeded to admin, hr, manager,
employee by 0023) for own claims and bill upload; manager claim step = `request.approve_team` (direct reports) or `request.approve_all`, list with `request.view_team` / `view_all`.

**Payable lifecycle** (`status` values that mean the same thing in every source table):
| Table | before payable | payable ("pending") | in processed payslip | in published payslip | final, not paid |
|---|---|---|---|---|---|
| one-time items | - | `pending` | `processing` | `paid` | `cancelled` |
| loan installments | - | `pending` | `processing` | `paid` | `skipped`, `cancelled` |
| arrears | `proposed` | `pending` | `processing` | `paid` | `dismissed` |
| encashments | - | `pending` | `processing` | `paid` | `cancelled` |
| claims [3b] | `submitted`, `verified` | `approved` | `processing` | `paid` | `rejected`, `cancelled` |
| overtime days [3b] | `computed` | `approved` | `processing` | `paid` | `rejected` |
Only rows before `processing` can be edited / cancelled (else 409 `ITEM_LOCKED`). `processing` and `paid` rows carry `payslip_id`.

**Target month rule** (every `period` / `start_period` in a request): no run yet, or a `draft` / `processed` run, or a `published` run in which the employee has an open draft revision.
Else 409 `PERIOD_LOCKED`. More than 12 months after the current month -> 422 `INVALID_PERIOD`. Responses carry `run_status: "none|draft|processed|published"` so the UI can say
"lands after the run is reopened" (processed) or "lands in the open revision" (published).

**Flags in lists** (computed, not stored): `PERIOD_CLOSED` (payable row whose month is published and the employee has no open revision: it will not be paid until moved).

**Rate limits** (per user, Redis; writes fail closed -> 503 `TEMPORARILY_UNAVAILABLE`): admin reads `payroll:admin_read` 120/min; admin writes `payroll:write` 60/min;
loan actions, import commit, arrears accept / recalculate, encashment create, overtime compute `payroll:action` 10/min; import dry-run and template `payroll:import` 10/min + 50/day;
employee reads `payroll:own_list` 60/min, loan detail `payroll:own_detail` (30/min + 200/day, shared with payslips); claim submit/cancel `claims:write` 10/min + 30/day;
manager verify/reject `claims:decide` 60/min; step-up 5/min (shared, section 9).

**Step-up** (`current_password`, 403 `REAUTH_FAILED`, 422 `VALIDATION_ERROR` when missing): loan create / reschedule / foreclose / cancel, import commit, arrears accept, encashment create.

### 17.1 Changes to existing sections
- **13.0 reserved codes** gain `OVERTIME` (earning, ESI wage) and `LEAVE_ENCASH` (earning, not an ESI wage). They are system-only: `PUT /admin/payroll/payslips/{id}` with either code in
  `adjustments` -> 422. Structure allowances / fixed deductions cannot use them (422).
- **13.6 AdminPayslip.inputs** gains read-only `components` (system lines pulled from section 17 sources; not accepted by PUT, sending it -> 422):
  ```json
  "components": [ { "code":"LOAN_EMI","label":"Loan L-0007 (3/12)","amount":"2500.00","sources":[{"type":"loan_installment","id":"uuid"}] },
                  { "code":"REIMBURSEMENT","label":"Reimbursement - Travel","amount":"1840.00","sources":[{"type":"reimbursement","id":"uuid"},{"type":"reimbursement","id":"uuid"}] },
                  { "code":"OVERTIME","label":"Overtime (6.5 h)","amount":"1625.00","minutes":390,"sources":[{"type":"overtime","id":"uuid"}] } ]
  ```
  `sources[].type`: `one_time | loan_installment | arrears | encashment | reimbursement | overtime`. The employee `Payslip` (13.2) is unchanged: these lines simply appear in
  `earnings` / `deductions` like adjustments (`monthly: null`).
- **13.5 / 13.6 behaviour**: run create, refresh, process, single payslip process and revise (re)pull `components`; `PUT` keeps them. Process moves the pulled rows to `processing`,
  reopen back to the payable state, publish to `paid`. Publish answers 409 `COMPONENTS_CHANGED` if a pulled row is no longer in the expected state (reload, refresh, process again).
- **13.7 warning codes** gain `OVERTIME_UNREVIEWED`, `OVERTIME_CAPPED`, `ARREARS_PROPOSED`, `MANUAL_COMPONENT_OVERLAP`. **15.1 variance** reasons gain `COMPONENTS`.
- **Section 4** `GET /leave/balance` leave items and **section 9** admin leave balances gain `"encashed": 2.0`; `remaining = quota - used - pending - encashed`;
  `QUOTA_BELOW_USED` compares with `used + pending + encashed` (message "below used + pending + encashed"); a leave type with encashments cannot be deleted (409 in use). **Implemented in the leave service:** applying for leave, the balance screens, `home` remaining days, the join-date quota recompute (a row with `encashed > 0` needs review, like one with `used`/`pending`) and the DB check all use `used + pending + encashed <= quota`. **Section 9** leave types gain `encashable` (bool, default false), `encash_max_days_per_year` (number|null, 0.5 steps),
  `encash_min_balance` (number, default 0), on `POST`/`PUT /admin/leave-types` and in every response; `PUT` keeps the stored value of a key that is not sent (older clients), `encash_max_days_per_year: null` removes the cap; 0.5 steps, `encash_max_days_per_year` > 0, `encash_min_balance` >= 0, otherwise 422.
- **13.4 payroll details** gain `overtime_eligible` (bool, default false) [3b] in `GET` and `PUT /admin/payroll/employees/{user_id}/details`; it needs no step-up (`current_password`) when it is the only key sent, and `null` is 422. The self rule applies. Implemented.
- **Section 11 push** types: `loan_created`, `reimbursement_submitted`, `reimbursement_decision` (17.7).
- **`POST /files`** accepts `purpose=reimbursement_bill` [3b] (needs `reimbursement.submit`; jpeg/png/pdf <= 10 MB, encrypted at rest).

### 17.2 One-time items (bonus, incentive, other earning / deduction) and bulk upload
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/items?period=&user_id=&code=&status=&batch_id=&page=&page_size= | `payroll.view_all` |
| POST | /admin/payroll/items | `payroll.manage` |
| PUT | /admin/payroll/items/{id} | `payroll.manage` (pending only) |
| POST | /admin/payroll/items/{id}/cancel | `payroll.manage` (pending only) |
| GET | /admin/payroll/items/template | `payroll.manage` |
| POST | /admin/payroll/items/import?dry_run=true\|false | `payroll.manage` (+ step-up when `dry_run=false`) |
| GET | /admin/payroll/items/batches?period= | `payroll.view_all` |
| POST | /admin/payroll/items/batches/{id}/cancel | `payroll.manage` |

**OneTimeItem**:
```json
{ "id":"uuid","user":UserSummary,"period":"2026-11","code":"BONUS","label":"Diwali bonus","amount":"5000.00",
  "status":"pending|processing|paid|cancelled","run_status":"draft","flags":[],"batch_id":"uuid"|null,"payslip_id":"uuid"|null,
  "version":1,"created_at":"ts","created_by":UserSummary,"updated_at":"ts" }
```
**POST** `{ "user_id":"uuid","period":"2026-11","code":"BONUS|INCENTIVE|OTHER_EARNING|OTHER_DEDUCTION","label":"Diwali bonus","amount":"5000.00" }` -> 201 OneTimeItem.
`amount` > 0 (a deduction uses `OTHER_DEDUCTION`). **PUT** `{ "expected_version":1, "period"?, "label"?, "amount"? }` -> 200. **cancel** `{ "reason":"Duplicate entry" }` -> 200.
Errors: 409 `PERIOD_LOCKED`, `ITEM_LOCKED`, `VERSION_CONFLICT`, `TOO_MANY_ITEMS` (> 20 per employee-month); 403 `FORBIDDEN` (own); 422. `PUT` needs at least one of `period`, `label`, `amount` (else 422); a non-pending item (processing, paid or cancelled) is `ITEM_LOCKED`; cancelling a batch twice is 409 `CONFLICT`. `GET /admin/payroll/items` takes `period`, `user_id`, `code`, `status`, `batch_id` and the standard page parameters.
**template** -> `.xlsx` with sheet `Items` (header `Employee ID | Code | Label | Amount`, one example row) and sheet `Codes`. `Cache-Control: no-store`.
**import** multipart: `file` (xlsx <= 1 MB), `period` (`YYYY-MM`), and for `dry_run=false` also `file_sha256` (from the dry run) and `current_password`. `dry_run` defaults to `true` when omitted. Commit answers 201 and both modes send `Cache-Control: no-store`. The parser reads sheet 1 only and checks the zip directory (entries, unpacked size, ratio, macros, external links) before opening the file; a spreadsheet formula cell, or text starting with `= + - @`, is a `FORMULA_NOT_ALLOWED` row error. The `Items` sheet of the template carries one example row (`EXAMPLE-DELETE-ME`) that fails with `UNKNOWN_EMPLOYEE` if it is left in.
Dry run -> 200 (nothing is written; audit `payroll.import.dry_run` with rows/errors counts):
```json
{ "period":"2026-11","file_sha256":"<64 hex>","rows":120,"valid":118,
  "errors":[ { "row":14,"field":"employee_id","message":"UNKNOWN_EMPLOYEE" }, { "row":40,"field":"amount","message":"INVALID_AMOUNT" } ],
  "warnings":[ { "row":22,"field":"employee_id","message":"DUPLICATE_OF_EXISTING_ITEM" } ],
  "by_code":[ { "code":"BONUS","count":110,"total":"550000.00" }, { "code":"OTHER_DEDUCTION","count":8,"total":"4000.00" } ],
  "run_status":"draft" }
```
Row error messages: `UNKNOWN_EMPLOYEE`, `NOT_ELIGIBLE`, `SELF`, `INVALID_CODE`, `INVALID_LABEL`, `INVALID_AMOUNT`, `FORMULA_NOT_ALLOWED`, `TOO_MANY_ITEMS`, `DUPLICATE_ROW` (same employee+code+label+amount twice).
`FORMULA_NOT_ALLOWED` on `label` is decided on the CLEANED label (markup, control and zero-width characters removed first), so `<b>=1+1</b>` is refused; the JSON `POST /admin/payroll/items` and `PUT /items/{id}` apply the same rule (422 on `label`).
**Warnings** (not errors, the row stays valid): `DUPLICATE_OF_EXISTING_ITEM` = a live (pending / processing / paid) item of that month with the same employee, code, label and amount already exists.
**Dry run is bound to the commit (review 3a L3).** A dry run stores `HMAC-SHA256(key derived from the server secret by HKDF with a purpose label, actor | period | file sha256)` in Redis for 15 minutes; a commit without that record (never checked, other admin, other month, other file, expired, used already) is 409 `IMPORT_NOT_CHECKED`. If Redis is down the dry run and the commit are 503 `TEMPORARILY_UNAVAILABLE` (fail closed). The record is deleted after a successful commit. nginx: the exact location `/api/admin/payroll/items/import` allows a 1100k body (all other JSON routes 256k).
Commit -> 201 `{ "batch_id":"uuid","period":"2026-11","created":118 }` only when there are NO row errors (else 422 `IMPORT_INVALID` with the same `errors` as `details`
(`field` = `"row 14: employee_id"`)). The whole batch is one transaction. Errors: 409 `IMPORT_FILE_CHANGED` (`file_sha256` differs), 409 `IMPORT_DUPLICATE` (same file + period already imported),
409 `PERIOD_LOCKED`, 422 `FILE_TYPE_NOT_ALLOWED` (not xlsx, macro, external links, zip limits), 422 `FILE_TOO_LARGE`, 422 `IMPORT_INVALID` (header mismatch, > 500 rows: `details[0].field` = `file`),
409 `IMPORT_NOT_CHECKED`, 503 `TEMPORARILY_UNAVAILABLE` (parser busy / timeout / Redis down). Under the run lock the commit also locks the employees' `users` rows (`FOR UPDATE`, id order) and re-validates the row caps on locked data. Audit `payroll.import.commit` (batch_id, period, rows).
**Batch** `{ "id","period","rows","created_at","created_by":UserSummary,"cancelled_at":"ts|null","counts":{"pending":0,"processing":0,"paid":118,"cancelled":0} }`.
**Batch cancel** `{ "reason" }` -> 200 `{ "cancelled":110,"not_cancelled":8 }` (pending rows only).

**Read audit (review 3a L8).** The list reads `GET /admin/payroll/items` (`payroll.item.list`), `/items/batches` (`payroll.import.list`), `/loans` (`payroll.loan.list`), `/arrears`, `/encashments` and the encashment quote are audited with `after={count}` only (rows returned; no filters, ids, amounts or labels). Single-record reads keep their existing `*.view` entries.

### 17.3 Loans and advances
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/loans?user_id=&status=active\|closed\|cancelled&kind=&page=&page_size= | `payroll.view_all` |
| POST | /admin/payroll/loans | `payroll.manage` + step-up |
| GET | /admin/payroll/loans/{id} | `payroll.view_all` (audit `payroll.loan.view`) |
| POST | /admin/payroll/loans/{id}/skip | `payroll.manage` |
| POST | /admin/payroll/loans/{id}/reschedule | `payroll.manage` + step-up |
| POST | /admin/payroll/loans/{id}/foreclose | `payroll.manage` + step-up |
| POST | /admin/payroll/loans/{id}/cancel | `payroll.manage` + step-up |
| GET | /me/loans | `payroll.view_own` |
| GET | /me/loans/{id} | `payroll.view_own` (owner; audit `payroll.loan.view_own`) |

**Loan** (admin):
```json
{ "id":"uuid","reference":"L-0007","user":UserSummary,"kind":"loan|advance","principal":"30000.00","emi":"2500.00","start_period":"2026-11",
  "recovered":"5000.00","in_process":"2500.00","outstanding":"22500.00","status":"active|closed|cancelled",
  "close_mode":null|"recovered"|"foreclosed_payroll"|"foreclosed_outside"|"cancelled","reason":"Medical emergency",
  "installments":[ { "id":"uuid","seq":1,"period":"2026-11","amount":"2500.00","kind":"emi|foreclosure","status":"pending|processing|paid|skipped|cancelled","payslip_id":null,"flags":[] } ],
  "version":3,"created_at":"ts","created_by":UserSummary,"closed_at":"ts|null" }
```
List items omit `installments` and `reason` and add `next_installment: { "period","amount" } | null`. `outstanding = principal - recovered`; `in_process` is part of it.
**POST** `{ "user_id","kind":"loan","principal":"30000.00","emi":"2500.00","start_period":"2026-11","reason":"Medical emergency","current_password" }` -> 201 Loan.
Rules: principal 1.00..1000000.00; 0 < emi <= principal; ceil(principal/emi) <= `payroll.loan_max_tenure_months` (60); active loans+advances < `payroll.loan_max_active` (3).
**skip** `{ "installment_id","reason","expected_version" }` -> 200 Loan (installment `skipped`, one appended after the last). **reschedule** `{ "emi","from_period","reason","expected_version","current_password" }`.
**foreclose** `{ "mode":"payroll","period":"2026-12","reason","expected_version","current_password" }` or `{ "mode":"outside","paid_on":"2026-11-20","reference":"UTR123","reason","expected_version","current_password" }`
(`paid_on` not in the future; `reference` 1-60 `[A-Za-z0-9 ._/-]`, encrypted, stored in the event only). **cancel** `{ "reason","expected_version","current_password" }`.
Errors: 409 `LOAN_NOT_ACTIVE`, `LOAN_HAS_RECOVERIES` (cancel), `ITEM_LOCKED` (an installment is processing), `PERIOD_LOCKED`, `VERSION_CONFLICT`, `LOAN_LIMIT_REACHED`; 422 (tenure, amounts); 403.
Audit `payroll.loan.create|skip|reschedule|foreclose|cancel|close|view` (reference, kind, periods; never amounts, reasons or the external payment reference).
**Implementation notes (3a step 2).** (1) `outstanding` is `0.00` for `closed` and `cancelled` loans (`principal - recovered` only while `active`); `recovered` is the sum of `paid` installments and `in_process` of `processing` ones. (2) Payslip labels: `Loan L-0007 (3/12)` / `Advance L-0007 (3/12)` (position among the live installments), `Loan L-0007 foreclosure`. (3) **skip** needs a `pending` `emi` installment (else 409 `ITEM_LOCKED`); the deferred copy lands in the month after the last live installment (after the skipped one when it was the last) and must satisfy the target month rule; installment `seq` numbers are unique per loan among the LIVE installments only (migration 0023: partial unique index on status not `cancelled`/`skipped`, check `seq >= 1`); a new installment (skip, reschedule, foreclosure) takes `max(live seq) + 1`, so skipping, rescheduling and foreclosing never use numbers up (the old 'numbers exhausted' 422 and the close-and-reissue workaround are gone). A skipped or cancelled installment may therefore share its `seq` with a live one; lists order by `seq`, then `period`. The tenure limit is checked on live installments (create, reschedule). (4) **reschedule** cancels the `pending` installments from `from_period` on and regenerates them from the amount still to schedule = `principal - paid - processing - pending installments before from_period`; `emi` must be <= that amount, `from_period` must be after the last paid / processing / kept installment month (422 `from_period`), the total installment count must respect `payroll.loan_max_tenure_months`. (5) **foreclose** `payroll`: `period` must not be a month that already has a `paid` installment (422); `outside`: no installment is created, the loan closes at once (`foreclosed_outside`); both refuse while an installment is `processing` (409 `ITEM_LOCKED`). The `payroll` foreclosure installment (`kind: foreclosure`) closes the loan as `foreclosed_payroll` when it is published. (6) **cancel** cancels the `pending` installments (409 `LOAN_HAS_RECOVERIES` if any is processing or paid). (7) A loan closes itself (`recovered`) in the publish that makes the paid installments add up to the principal (audit `payroll.loan.close`). (8) `reference` comes from the sequence `payroll_loan_ref_seq` on PostgreSQL and from "highest number + 1" on other databases (tests). (9) The caps of 17.0 (`TOO_MANY_ITEMS`, 20 / 40 lines per month) are checked for the first month of a (re)schedule, a skip target and a foreclosure month, not for every month of a long schedule. (10) A `pending` installment in a published month without an open revision shows `PERIOD_CLOSED`. (11) `skip` is a loan action (10/min `payroll:action`, no step-up); the step-up limiter counts every call that asks for a password. `GET /me/loans/{id}` answers 404 for another employee's loan and audits the probe (`payroll.denied`).
**GET /me/loans** -> `{ "items":[ { "id","reference","kind","principal","emi","outstanding","status","start_period","next_installment":{period,amount}|null } ] }` (un-paginated, active first).
**GET /me/loans/{id}** -> the same + `"installments":[ { "seq","period","amount","status":"scheduled|deducted|deferred|cancelled" } ]` (`pending`/`processing` -> `scheduled`,
`paid` -> `deducted`, `skipped` -> `deferred`). No `reason`, no payslip ids, no actors.

### 17.4 Auto arrears
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/arrears?status=proposed\|pending\|processing\|paid\|dismissed&user_id=&page=&page_size= | `payroll.view_all` |
| GET | /admin/payroll/arrears/{id} | `payroll.view_all` (audit `payroll.arrears.view`) |
| POST | /admin/payroll/employees/{user_id}/arrears/recalculate | `payroll.manage` |
| POST | /admin/payroll/arrears/{id}/accept | `payroll.manage` + step-up |
| POST | /admin/payroll/arrears/{id}/dismiss | `payroll.manage` |

**Arrears**:
```json
{ "id":"uuid","user":UserSummary,"code":"ARREARS|OTHER_DEDUCTION","amount":"4800.00","from_period":"2026-08","to_period":"2026-10","period":null|"2026-11",
  "status":"proposed|pending|processing|paid|dismissed","run_status":"none|draft|processed|published"|null,
  "trigger_structure":{ "id":"uuid","effective_from":"2026-08-01" }|null,
  "breakdown":[ { "period":"2026-08","paid_days":31.0,"paid_earnings":"40000.00","new_earnings":"41600.00","already_paid_arrears":"0.00","diff":"1600.00","basic_diff":"1000.00" } ],
  "version":2,"created_at":"ts","updated_at":"ts" }
```
Proposals are created / recomputed automatically in the same transaction as a structure create, update or delete (13.4) that affects a month with a published payslip;
the structure endpoints' responses are unchanged. **recalculate** -> 200 `{ "arrears": Arrears | null }` (null = no difference; an open proposal is then dismissed by the system).
**accept** `{ "period":"2026-11","expected_version":2,"current_password" }` -> 200 (status `pending`). **dismiss** `{ "reason" }` -> 200 (`proposed` or `pending` only).
Errors: 409 `ARREARS_STALE` (version changed; reload), `NOT_PROPOSED`, `ITEM_LOCKED`, `PERIOD_LOCKED`; 403 (own). Arrears are not PF or ESI wages (plan 15 Q1).
**Implementation notes (3a step 2).** (1) The Arrears object also carries `payslip_id` and `flags` (`PERIOD_CLOSED`). For `OTHER_DEDUCTION` the `amount` is the positive magnitude and each breakdown `diff` is signed. (2) Every recalculation (structure create / update / delete that reaches a month with a published payslip, or `recalculate`) scans ALL published months of the employee: a month counts when the structure in force for it now differs from the one the live payslip used and the difference is not zero; the proposal is recomputed in place (`version + 1`, event `propose`) only when code, amount or breakdown changed. `from_period` / `to_period` are the first / last month in the breakdown; `trigger_structure` is the structure now in force for the first month in the breakdown. Breakdown entries also carry `paid_structure_id` and `new_structure_id`. (3) `paid_earnings` = the earning lines of the live payslip that have a `monthly` amount (BASIC, HRA, allowances); `new_earnings` is the new structure for the same `basis_days` / `paid_days` with the run's rounding; `already_paid_arrears` = signed diffs of earlier `processing` / `paid` arrears for that month. (4) A difference below 1.00 in total, or no difference, dismisses the open row (system, no actor, reason "No difference after recalculation"). An accepted (`pending`) row that is altered goes back to `proposed` with `period: null`. (5) **accept** checks in this order: not `processing`/`paid` (`ITEM_LOCKED`), is `proposed` (`NOT_PROPOSED`), `expected_version` (`ARREARS_STALE`), then the target month (`PERIOD_LOCKED`, 422 `INVALID_PERIOD`) and the item caps (`TOO_MANY_ITEMS`); the employee must be payroll-eligible for the month (422). **dismiss** works on `proposed` and `pending` (409 `ITEM_LOCKED` for processing / paid, `NOT_PROPOSED` for dismissed). Audit `payroll.arrears.propose|accept|dismiss|recalculate|view` (ids, periods, counts; never amounts). (7) **Revisions (review 3a H1).** A month is compared whenever the structure now in force differs from the one the live payslip used OR arrears were already `processing` / `paid` for it; the signed `diff = new_earnings - paid_earnings - already_paid_arrears` (negative = recovery). So a revision that adopts the new structure after the arrears were paid yields an `OTHER_DEDUCTION` proposal for the amount that would otherwise be paid twice. Publishing a REVISION (`POST /runs/{id}/publish` or `POST /payslips/{id}/publish` of revision > 1) recomputes the employee's proposal in the same transaction (audit `payroll.arrears.propose`, `after.trigger = "revision"`): an accepted-but-unpaid (`pending`) row goes back to `proposed` with `period: null` and `version + 1`, or is dismissed by the system when nothing is left to pay. Revising (creating the draft) is not refused. (8) **Reads.** `GET /admin/payroll/arrears` is audited as `payroll.arrears.list` with `after={count}`.
(6) Payslip warning `ARREARS_PROPOSED` is set when the employee has a `proposed` row at the moment the payslip's components are pulled (run create / refresh / process / revise); lines: `Arrears Aug 2026 - Oct 2026` (one month: `Arrears Aug 2026`), `Salary recovery (arrears)`.

### 17.5 Leave encashment
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/encashments?user_id=&year=&status=&page=&page_size= | `payroll.view_all` |
| GET | /admin/payroll/employees/{user_id}/encashment-quote?leave_type_id=&year=&days=&period= | `payroll.view_all` |
| POST | /admin/payroll/encashments | `payroll.manage` + step-up |
| POST | /admin/payroll/encashments/{id}/cancel | `payroll.manage` (pending only) |

**quote** -> `{ "leave_type":{id,code,name},"year":2026,"available_days":6.0,"days":4.0,"daily_rate":"666.67","amount":"2666.68","basis":"basic","divisor":30,"period":"2026-11" }`
(writes nothing; `days` optional, defaults to `available_days`).
**POST** `{ "user_id","leave_type_id","year":2026,"days":4.0,"period":"2026-11","source":"request","reason":"Year-end encashment","current_password" }` -> 201 Encashment
`{ "id","user":UserSummary,"leave_type":{id,code,name},"year","days","daily_rate","amount","period","source":"request|fnf","status","run_status","payslip_id","version","created_at","created_by" }`.
`year` = current or previous; `days` > 0 in 0.5 steps. Errors: 422 `NOT_ENCASHABLE`, 409 `INSUFFICIENT_BALANCE` (`details:[{field:"available_days",message:"6.0"}]`), 422 `STRUCTURE_MISSING`,
409 `PERIOD_LOCKED`; cancel 409 `ITEM_LOCKED`. Audit `payroll.encashment.create|cancel` (leave type code, year, days; never amounts).
**Implementation notes (3a step 2).** (1) `available_days = max(0, min(quota - used - pending - encashed - encash_min_balance, encash_max_days_per_year - days of live encashments of that year))`; `quote` also answers 409 `INSUFFICIENT_BALANCE` when `days` is above it, 422 for `days` that is not a positive 0.5 step, 422 `STRUCTURE_MISSING`, 422 for a year other than the current or the previous one, 403 for the employee role. (2) `daily_rate = round(monthly basic (or monthly gross when `payroll.encashment_basis = gross`) / payroll.encashment_divisor, 2)`, `amount = round(days x daily_rate, 2)` in paise whatever `payroll.rounding` says (the example above: 4.0 x 666.67 = 2666.68); the monthly figure comes from the structure in force for the payout month and the settings read at creation. (3) `POST` answers 201 and adds `days` to `leave_balances.encashed` in the same transaction (row created for the year when missing); a leave type that is not `encashable` is 422 `NOT_ENCASHABLE`. `source` accepts `request` and `fnf`. (4) **cancel** takes an OPTIONAL body `{ "reason": "..." }` (3-200 chars, kept in the encrypted money history only); `pending` only; gives the days back. A `processing` / `paid` encashment keeps its days (409 `ITEM_LOCKED`). (5) Payslip line: code `LEAVE_ENCASH`, label `Leave encashment (4.0 d)`, one line per encashment, `sources[].type = encashment`. Cancelled rows are not counted by the yearly cap. (6) `amount` above 10,000,000.00 (`MAX_MONEY`) is 422 `VALIDATION_ERROR` on `days` for the quote and the create (nothing is locked or changed). (7) `GET /encashments` is audited as `payroll.encashment.list` (`after={count}`), the quote as `payroll.encashment.quote` (entity = the employee, `after={count:1}`).

### 17.6 Reimbursement claims [3b]
| Method | Path | Perm |
|---|---|---|
| GET | /reimbursements/categories | `reimbursement.submit` |
| POST | /reimbursements | `reimbursement.submit` |
| GET | /reimbursements?status=&year=&page=&page_size= | `reimbursement.submit` |
| GET | /reimbursements/{id} | `reimbursement.submit` (owner) |
| POST | /reimbursements/{id}/cancel | `reimbursement.submit` (owner; submitted / verified) |
| GET | /admin/reimbursements?stage=manager\|hr\|all&status=&employee_id=&period=&category=&page=&page_size= | `request.view_team` (stage `manager`, direct reports) / `payroll.view_all` |
| GET | /admin/reimbursements/{id} | same scope (audit `reimbursement.view`) |
| PUT | /admin/reimbursements/{id}/verify | `request.approve_team` / `request.approve_all` (direct report, submitted) |
| PUT | /admin/reimbursements/{id}/approve | `payroll.manage` |
| PUT | /admin/reimbursements/{id}/reject | manager scope while `submitted` and `manager_step = pending`, or `payroll.manage` while `submitted`/`verified` |
| PUT | /admin/reimbursements/{id}/payout | `payroll.manage` (approved only) |

**categories** -> `{ "items":[ { "code":"travel","name":"Travel","max_amount":"5000.00"|null,"bill_required":true } ], "max_age_days":60 }`.
**POST** `{ "category":"travel","expense_date":"2026-10-28","amount":"1240.00","description":"Cab to client site","bill_file_id":"uuid"|null }` (description 3-300 chars) -> 201 Claim:
```json
{ "id":"uuid","category":{"code":"travel","name":"Travel"},"expense_date":"2026-10-28","amount":"1240.00","approved_amount":null,"description":"Cab to client site",
  "bill":{ "url":"/api/files/<id>","content_type":"image/jpeg|image/png|application/pdf" }|null,
  "status":"submitted|verified|approved|paid|rejected|cancelled","manager_step":"pending|done|skipped","period":null|"2026-11",
  "verifier":UserSummary|null,"verified_at":"ts|null","approver":UserSummary|null,"decided_at":"ts|null","decision_comment":null,"created_at":"ts","version":1 }
```
The employee sees `processing` as `approved`. Admin item = Claim + `"user":UserSummary`, real `status` (incl. `processing`), `payslip_id`, `run_status`, `flags`, `combined` (true when the manager verified and approved in one call, i.e. verifier = approver).
Errors: 422 `INVALID_CATEGORY`, `INVALID_DATE` (future / older than `max_age_days`), `AMOUNT_ABOVE_LIMIT` (`details:[{field:"amount",message:"5000.00"}]`), `BILL_REQUIRED`,
`VALIDATION_ERROR` (bill not your own unused `reimbursement_bill` upload); 409 `DUPLICATE_BILL`, `CLAIM_LIMIT_REACHED` (per month); 429; 503. Cancel: 409 `NOT_PENDING`; 404 not yours.
**verify** `{ "comment":"optional" }` -> 200 (status `verified`). **approve** `{ "approved_amount":"1240.00","period":"2026-11","comment":"optional","expected_version":2 }` -> 200 (status `approved`);
`approved_amount` > 0 and <= `amount`. Allowed on `verified`, or on `submitted` when `manager_step` is `skipped` or the caller is the employee's manager holding `payroll.manage`
(then `manager_step` = `done`, `verifier` = caller, audit `combined:true`). The approve audit also carries `step_skipped_reason` (`no_manager|manager_inactive|manager_no_perm|requester_inactive`, or `at_submit` when the step was skipped at submit and the manager has since become able to act; null when not skipped). A claim whose manager is no longer actionable can still be approved by HR on `submitted` (spec choice, unchanged). The approver must not be the verifier otherwise (403 `SECOND_PERSON_REQUIRED`).
**reject** `{ "comment":"required" }` -> 200. **payout** `{ "period":"2026-12","expected_version":3 }` -> 200.
Errors: 409 `NOT_PENDING`, `VERSION_CONFLICT`, `PERIOD_LOCKED`, `ITEM_LOCKED`, `REQUESTER_INACTIVE` (verify only); 403 `FORBIDDEN` (own), 404 (out of scope; audited `reimbursement.denied`).
Bills: `GET /api/files/{id}` is allowed for the owner, the owner's current manager (with `request.view_team`/`approve_team`) while the claim is `submitted` with `manager_step = pending`, and `payroll.view_all`
holders; cross-user reads are audited `file.view_other`. Bills are kept (not purged).

**Implementation notes (3b).**
1. `reimbursement.submit` is granted to every existing role by 0023 (and is in `database/seed.py`); it is a self-service permission (does not make a role "elevated" in the admin role checks). `POST /files` with `purpose=reimbursement_bill` needs it, stores the file encrypted and writes a `file.upload` audit row.
2. **Statuses.** The employee API shows `processing` as `approved`; filters `?status=approved` on `GET /reimbursements` match approved and processing. `GET /reimbursements?year=` filters on the YEAR of `expense_date`. A claim read that is not yours is 404 and audited `reimbursement.denied` (reason `not_owner`).
3. **Submit** checks, in order: `INVALID_CATEGORY`, `INVALID_DATE` (expense date in the future or older than `payroll.reimbursement_max_age_days`, office timezone), `AMOUNT_ABOVE_LIMIT` (`details:[{field:"amount",message:"<category max>"}]`), the bill (`BILL_REQUIRED` when the category needs one and none is sent; a `bill_file_id` that is not your own unused `reimbursement_bill` upload is 422 `VALIDATION_ERROR` on `bill_file_id`), `DUPLICATE_BILL` (same sha256 on another live claim, any employee), `CLAIM_LIMIT_REACHED` (all claims created in the calendar month, cancelled and rejected ones included). `manager_step` is `pending` when the employee's manager is active and holds `request.approve_team` / `approve_all`, else `skipped` (decided again at approve time with the manager as he is then).
4. **Manager scope.** `verify` works for DIRECT reports only, also for holders of `request.approve_all` (HR is not the manager of anybody else's team: 404, audited `reimbursement.denied` with `out_of_scope`). `reject` by a manager: direct reports, `submitted` with `manager_step = pending` only (else 409 `NOT_PENDING`); `reject` by `payroll.manage`: `submitted` / `verified`, any employee. `GET /admin/reimbursements?stage=manager` needs `request.view_team` (or `approve_team` / `approve_all`) and lists direct reports' `submitted` claims whose manager step is pending (explicit `status` overrides that default); `stage=hr|all` needs `payroll.view_all` (403 otherwise); `stage=hr` defaults to the HR queue (`verified`, or `submitted` with `manager_step = skipped`); the default stage is `all` for `payroll.view_all` holders, else `manager`. `GET /admin/reimbursements/{id}` is 404 outside that scope. `employee_id` is the employee code (case-insensitive), `period` the payout month.
5. **Approve** checks: self (403 `FORBIDDEN`), target month (P9, `PERIOD_LOCKED`; `INVALID_PERIOD`), `expected_version` (`VERSION_CONFLICT`), status/step (`NOT_PENDING`; a `submitted` claim whose manager step is still pending is `NOT_PENDING` for HR unless the caller is the employee's manager: combined), `SECOND_PERSON_REQUIRED` when the caller verified it, payroll eligibility of the employee for the payout month (422), `approved_amount` above the claimed amount (422 `AMOUNT_ABOVE_LIMIT` on `approved_amount`, the message does not repeat amounts). An inactive employee skips the manager step (`manager_step` becomes `skipped`). **payout** `409 ITEM_LOCKED` once `processing` / `paid`, `NOT_PENDING` unless `approved`. Audit `reimbursement.submit|cancel|verify|approve|reject|reschedule` (payout move) `|view|list|denied` (ids, category code, periods, `combined`; never amounts, descriptions or comments); money history events `create|verify|approve|reject|cancel|update|process|reopen|pay|carry`.
6. **Payslip lines.** One `REIMBURSEMENT` line per category and month (`Reimbursement - <category name>`, amount = sum of the approved amounts, `sources` = every claim). Claims do not count towards the 20 / 40 line caps. A claim is not a PF or ESI wage. Held payslips keep their claims `approved`.
7. Bills (`GET /api/files/{id}`): owner; `payroll.view_all` holders once a claim carries the file; the owner's current manager (`request.view_team` / `approve_team`) while the claim is `submitted` with `manager_step = pending`; anything else (including an upload no claim uses, for non-owners) is 404. Cross-user reads are audited `file.view_other`. `cleanup_orphans` treats a bill referenced by a claim as live; bills are never purged.
8. Rate limits (Redis, fail closed on writes): submit / cancel `claims:write` 10/min + 30/day; verify / reject / approve / payout `claims:decide` 60/min; admin reads `payroll:admin_read`; employee reads `payroll:own_list` / `payroll:own_detail`.

### 17.7 Push (section 11; data messages, never amounts)
- `loan_created` to the employee, id = loan id, "Salary advance / loan added", body "A recovery schedule was added to your payslips. Open the app for details."
- `reimbursement_submitted` to the stage approver (manager; or the `payroll.manage` holders, max 20, coalesced 10 min per requester), id = claim id, body = employee name only.
  Also sent to the `payroll.manage` holders when a claim becomes `verified`.
- `reimbursement_decision` to the employee, id = claim id, title "Expense claim approved" / "Expense claim rejected", body "Your <category name> claim was <status>."
Best effort, after commit.

### 17.8 Overtime [3b]
| Method | Path | Perm |
|---|---|---|
| POST | /admin/payroll/overtime/compute?period= | `payroll.manage` |
| GET | /admin/payroll/overtime?period=&user_id=&status=&page=&page_size= | `payroll.view_all` |
| PUT | /admin/payroll/overtime/{id} | `payroll.manage` (computed / approved / rejected) |
| POST | /admin/payroll/overtime/bulk | `payroll.manage` |
| GET | /me/overtime?period= | `payroll.view_own` |

All refuse with 409 `OVERTIME_DISABLED` while `payroll.overtime_enabled` is false (GET `/me/overtime` returns `{ "enabled":false,"items":[] }`).
**compute** -> 200 `{ "period":"2026-11","created":34,"updated":5,"flagged_changed":1 }` (eligible employees only; `computed` rows rebuilt, reviewed rows only flagged).
**OvertimeDay** `{ "id","user":UserSummary,"work_date":"2026-11-08","computed_minutes":120,"approved_minutes":90|null,"flags":["OFF_DAY"],"status":"computed|approved|rejected|processing|paid","reviewer":UserSummary|null,"reviewed_at":"ts|null","comment":null,"version":1 }`.
List = paginated rows ordered by employee, date, plus `"summary":[{ "user_id","approved_minutes":540,"computed_minutes":600,"pending_rows":2 }]` for the filtered month (no money).
**PUT** `{ "status":"approved","approved_minutes":90,"comment":null,"expected_version":1 }` (`approved_minutes` 0..computed_minutes, default computed) or `{ "status":"rejected","comment":"required","expected_version":1 }`.
**bulk** `{ "ids":["uuid", ...] (1..200), "action":"approve|reject","comment":"..." }` -> 200 `{ "updated":40,"skipped":[{ "id","reason":"ITEM_LOCKED|FORBIDDEN" }] }`. Bulk `approve` also takes `include_flagged` (default false): without it rows flagged `SUSPICIOUS` / `AUTO_CLOSED` are skipped with reason `NEEDS_REVIEW`; `ATTENDANCE_CHANGED` rows are skipped with `NEEDS_REVIEW` even with it. `reject` is never skipped for flags. The audit `after` carries `include_flagged`, `ids_count` and `ids_digest` (sha256 prefix of the sorted approved ids), no ids.
**/me/overtime** -> `{ "enabled":true,"eligible":true,"period":"2026-11","items":[{ "work_date","computed_minutes","approved_minutes","status" }] }` (no money, no reviewer).
Errors: 409 `OVERTIME_DISABLED`, `ITEM_LOCKED`, `VERSION_CONFLICT`; 422 (`approved_minutes` above computed); 403 (own rows).
**Implementation notes (3b).**
1. Permissions are checked before the switch (an employee gets 403 whatever the setting). `GET /admin/payroll/overtime` is 409 while overtime is off too. Rate limits: compute `payroll:action` 10/min (fail closed), `PUT` / `bulk` `payroll:write` 60/min (fail closed), list `payroll:admin_read`, `/me/overtime` `payroll:own_list`.
2. **compute** works for employees with `overtime_eligible` and `include_in_payroll`, not deleted, with a shift, from attendance rows that have a check-out (and `worked_minutes`). Working day: `worked - shift minutes` (shift end before start = overnight; half the shift on an approved half-day leave); holiday / weekly off: all worked minutes while `payroll.overtime_offday_full` (else the working-day rule); the minutes are floored to `overtime_rounding_minutes`, capped at 1440, and a day below `overtime_min_minutes` has no row. Auto-closed days are skipped unless `overtime_include_auto_closed` (then flagged `AUTO_CLOSED`); `SUSPICIOUS` and `OFF_DAY` flags as in 17.8. A `computed` row that no longer qualifies stays `computed` with `computed_minutes = 0`. `approved` / `rejected` rows whose attendance minutes changed, or whose attendance was deleted / lost its check-out, get the fresh `computed_minutes` (0 when gone), `approved_minutes` is clamped down to it and the row is flagged `ATTENDANCE_CHANGED` (no new DB flag value: a removed day is `ATTENDANCE_CHANGED` with `computed_minutes = 0`). Approving such a row clears the flag only when the request sends explicit `approved_minutes`; a rejection clears it. Bulk approve always skips `ATTENDANCE_CHANGED` rows. `processing` / `paid` rows are never touched. Audit `payroll.overtime.compute|review|list` (counts, period, decision).
3. **PUT** `approved` also needs the target month rule (P9): a month that is published without an open revision for the employee is 409 `PERIOD_LOCKED`; rejecting is always possible. The reviewer must not be the employee (403 `FORBIDDEN`, audited `payroll.denied`). **bulk** answers 200 with `skipped[].reason` in `ITEM_LOCKED | FORBIDDEN | NOT_FOUND | PERIOD_LOCKED | NEEDS_REVIEW` (the last three are additions to the list above); ids must be unique; `comment` is required for `reject`.
4. **Payslip line.** The pull produces one `OVERTIME` component per payslip with the approved minutes (`minutes` = minutes paid after the monthly cap, `minutes_total` = approved minutes before the cap); `payroll_runs.recompute` fills the amount (a revision keeps the already PAID rows at their published amount: the line carries `paid_minutes` / `paid_ids` / `carried`, only the newly approved minutes are priced, with the monthly cap applied to what is left; publishing a revision is 409 `COMPONENTS_CHANGED` if a paid row of the superseded copy is not carried; when nothing new is payable the new rows stay `approved`): `hours = min(minutes, overtime_max_hours_month x 60) / 60`, `hourly = BASIC / overtime_month_days / shift hours`, `amount = round(hours x hourly x overtime_multiplier)` with the run rounding, basic from the structure the payslip adopts, shift from the employee at recompute time. The label is `Overtime (7.5 h)`. No line (rows stay `approved`) when the amount is 0, the employee has no shift, or the approved minutes are 0. New (`approved`) rows are pulled only while `payroll.overtime_enabled` is true and the employee is still eligible; rows already `processing` / `paid` always stay (a revision never loses published overtime). Warnings `OVERTIME_ATTENDANCE_CHANGED` (an approved row carries `ATTENDANCE_CHANGED`: its attendance changed after the review), `OVERTIME_CAPPED` (cap applied) and `OVERTIME_UNREVIEWED` (a `computed` row with minutes exists for the month; set when the components are pulled). OVERTIME is an ESI wage, not a PF wage. Overtime lines do not count towards the 20 / 40 line caps.
5. `/me/overtime` maps `processing` to `approved`; `eligible` is only present when `enabled` is true; `period` defaults to the current month.

### 17.9 New settings (`/admin/payroll/settings`, snapshotted into each run like the other `payroll.*` keys)
| Key | Default | Range |
|---|---|---|
| `payroll.loan_max_tenure_months` | 60 | 1..120 |
| `payroll.loan_max_active` | 3 | 1..10 |
| `payroll.encashment_basis` | `"basic"` | `basic` \| `gross` |
| `payroll.encashment_divisor` | 30 | 1..31 |
| `payroll.reimbursement_categories` [3b] | travel, food, medical, phone, internet, other (all `bill_required`, no max) | <= 20 items `{code ^[a-z][a-z0-9_]{1,19}$, name 1-40, max_amount money|null, bill_required}` |
| `payroll.reimbursement_max_age_days` [3b] | 60 | 1..365 |
| `payroll.reimbursement_max_claims_per_month` [3b] | 30 | 1..200 |
| `payroll.overtime_enabled` [3b] | false | bool |
| `payroll.overtime_multiplier` [3b] | 2 | 1..3 (2 decimals) |
| `payroll.overtime_month_days` [3b] | 26 | 20..31 |
| `payroll.overtime_min_minutes` [3b] | 30 | 0..240 |
| `payroll.overtime_rounding_minutes` [3b] | 30 | 1..60 (floor) |
| `payroll.overtime_max_hours_month` [3b] | 50 | 0..300 (0 = no cap) |
| `payroll.overtime_offday_full` [3b] | true | bool |
| `payroll.overtime_include_auto_closed` [3b] | false | bool |
Removing a reimbursement category keeps existing claims (shown with their stored code); new claims need an active category.

### 17.10 Error codes (new)
409: `PERIOD_LOCKED`, `ITEM_LOCKED`, `TOO_MANY_ITEMS`, `COMPONENTS_CHANGED`, `IMPORT_DUPLICATE`, `IMPORT_FILE_CHANGED`, `LOAN_NOT_ACTIVE`, `LOAN_HAS_RECOVERIES`, `LOAN_LIMIT_REACHED`,
`ARREARS_STALE`, `NOT_PROPOSED`, `DUPLICATE_BILL`, `CLAIM_LIMIT_REACHED`, `OVERTIME_DISABLED`. 422: `IMPORT_INVALID`, `NOT_ENCASHABLE`, `INVALID_CATEGORY`, `AMOUNT_ABOVE_LIMIT`, `BILL_REQUIRED`.
Reused: `VERSION_CONFLICT`, `NOT_PENDING`, `INSUFFICIENT_BALANCE`, `REQUESTER_INACTIVE`, `SECOND_PERSON_REQUIRED`, `STRUCTURE_MISSING` (as 422), `INVALID_PERIOD`, `INVALID_DATE`,
`FILE_TYPE_NOT_ALLOWED`, `FILE_TOO_LARGE`, `REAUTH_FAILED`, `FORBIDDEN`, `RATE_LIMITED`, `TEMPORARILY_UNAVAILABLE`.

## 18. Payroll increments, structure import, CTC / gratuity, full and final (plan 16 = plan 14 phase 4) - PROPOSED

Plan: `docs/plans/16-payroll-phase4.md`. Sections 13.0 and 17.0 conventions apply (money strings, `period` = `YYYY-MM`, labels 1-40 / reasons and comments 3-200 chars cleaned,
unknown fields 422, self rule 403 `FORBIDDEN` audited `payroll.denied`, admin unknown ids 404, employee routes 404 for anything not their own). Money, percents, labels, reasons and comments
are never written to audit rows, logs, push payloads or error messages. **[4a]** = work package 4a (migration 0024), **[4b]** = 4b (migration 0025).

### 18.0 Common
**Permissions.** Reads, CTC calculator, gratuity, F&F preview / statement: `payroll.view_all`. Maker actions: `payroll.manage`. Checker actions (approve / reject increments, batches and F&F; F&F revoke):
NEW `payroll.approve` (seeded to `admin` and `hr` by 0024). Employee F&F view: `payroll.view_own`.

**Maker-checker.** The checker must not be the employee (403 `FORBIDDEN`), nor the creator or the submitter of the record (403 `SECOND_PERSON_REQUIRED`). The database enforces the same with CHECK constraints.
Approve audits carry `maker_id` and `checker_id`.

**Step-up** (`current_password`; 422 `VALIDATION_ERROR` when missing, 403 `REAUTH_FAILED`; verified before any state check; shared 5/min limiter): increment approve, batch approve, structure import commit,
arrears bulk accept, F&F approve, F&F revoke, changing `payroll.structure_changes_need_approval`.

**Rate limits** (per user, Redis; writes fail closed -> 503 `TEMPORARILY_UNAVAILABLE`): reads `payroll:admin_read` 120/min; drafts / edits / submit / withdraw / cancel `payroll:write` 60/min;
approve / reject / revoke / batch create / bulk accept `payroll:action` 10/min; import dry run and template `payroll:import` 10/min + 50/day (shared with 17.2); CTC calculator `payroll:calc` 60/min;
gratuity xlsx and F&F statement PDF `payroll:report` 10/min; employee `/me/fnf` `payroll:own_list`, statement `payroll:own_detail` (shared with payslips).

**Money history.** Every create / edit / state change of an increment or F&F appends to `payroll_item_events` (`item_type` `increment` | `fnf`); not exposed through the API.

### 18.1 Changes to existing sections
- **13.4 Structure** gains read-only `"source":"manual|increment|import"`, `"increment_id":"uuid"|null`. **[4a]** While `payroll.structure_changes_need_approval` is true (default), `POST /admin/payroll/employees/{user_id}/structures`
  is allowed only when the employee has NO structure, and `PUT` / `DELETE /admin/payroll/structures/{id}` only on the employee's only structure while it is `manual` and not in use; otherwise 409 `APPROVAL_REQUIRED`
  ("Use an increment"). Unchanged when the setting is false.
- **13.3 settings**: changing `payroll.structure_changes_need_approval` needs `role.assign` + `current_password` (same rules and errors as `payroll.publish_requires_second_person`); after commit an `admin_alert` push goes to `role.assign` holders.
- **13.4 payroll details [4b]** gain `"notice_period_days": 30|null` (editable, 0..365, no step-up when it is the only key sent) and read-only `"last_working_day":"2026-12-15"|null` (written by 18.8 when the F&F is APPROVED and cleared when it is revoked; sending it -> 422).
- **13.5 / 13.6 [4b]**: payroll eligibility and the employment window end at the EARLIEST of the month end, `last_working_day` (details, set only by an approved F&F) and deactivation date - 1: a last working day can only shorten the window. An employee with an `approved` F&F whose
  `settlement_period` is the run month is eligible even when not employed in it: a **settlement-only payslip** (`employed_days` 0, `paid_days` 0, structure earnings 0, components only, warning `FNF_SETTLEMENT_ONLY`).
  AdminPayslip gains `"final_settlement": { "fnf_id":"uuid","status":"approved|settled" } | null`; employee `Payslip` and list items gain `"is_final": true|false`. New warning codes `FNF_SETTLEMENT_ONLY`, `EXIT_IN_MONTH` (LWD inside the month).
- **13.0 reserved codes [4b]** gain `NOTICE_RECOVERY` (deduction, system-only, not a PF / ESI wage): refused in `adjustments`, structure components and the 17.2 endpoints / import (422).
- **17.2 OneTimeItem**, **17.3 installments**, **17.5 Encashment** gain `"fnf_id":"uuid"|null` [4b]. Rows with `fnf_id` can only be edited / cancelled through 18.8 (17.2 / 17.3 / 17.5 actions on them -> 409 `FNF_MANAGED`).
- **15.1 dashboard** alerts gain `FNF_PENDING_APPROVAL`, `EXIT_WITHOUT_FNF` (deactivated or LWD passed, with an active loan or no F&F) [4b]; `leavers` use `last_working_day` when set. **15.1 reports** kind `gratuity` [4a] (18.3).
- **16 ESI file** "Last Working Day" uses `last_working_day` when set [4b].
- **Section 9 activate** [4b]: 409 `FNF_OPEN` while the employee has a `draft` / `submitted` / `approved` F&F; after `settled`, activation clears `last_working_day` (audited `payroll.details.update`).
- **Section 11 push** types: `payroll_approval`, `fnf_settled` (18.10).

### 18.2 CTC calculator [4a]
| Method | Path | Perm |
|---|---|---|
| POST | /admin/payroll/ctc/calculate | `payroll.view_all` (pure calculation, writes nothing; audit `payroll.ctc.calculate` with `after={mode}` only) |

Request:
```json
{ "mode":"from_gross|from_ctc", "amount":"40000.00", "frequency":"monthly|annual",
  "pf_applicable":true, "pf_on_full_basic":false, "esi_applicable":true, "pt_applicable":true,
  "split":{ "basic_pct":50, "hra_pct_of_basic":40 } | null,
  "include_gratuity":true | null, "employer_share_from_employee":false | null }
```
`null` = the current payroll setting (the response example below uses `employer_share_from_employee: false`; with `true`, `employer_recovered` equals employer PF + ESI and lowers `ctc` and the net estimate). `amount` 1.00..10000000.00 (annual: up to 120000000.00, then divided by 12, rounded to paise). `basic_pct` 1..100, `hra_pct_of_basic` 0..100 (integers).
Response 200:
```json
{ "mode":"from_ctc","frequency":"annual",
  "split":{ "basic":"20000.00","hra":"8000.00","allowances":[ { "code":"SPECIAL","name":"Special allowance","amount":"12000.00" } ] },
  "monthly":{ "gross":"40000.00","employer_pf":"1800.00","employer_esi":"0.00","employer_recovered":"0.00","gratuity_accrual":"961.54","ctc":"42761.54" },
  "annual":{ "gross":"480000.00","ctc":"513138.48" },
  "employee":{ "pf":"1800.00","esi":"0.00","pt_annual_estimate":"2500.00","net_monthly_estimate":"37991.67" },
  "unallocated_monthly":"0.00",
  "flags":[ "PF_CEILING_APPLIED" ],
  "rules":{ "pf_employee_rate_pct":12,"pf_employer_rate_pct":12,"pf_wage_ceiling":15000,"esi_wage_threshold":20000,"esi_employee_rate_pct":0.75,"esi_employer_rate_pct":3.25,
            "pt_frequency":"half_yearly","rounding":"rupee","gratuity_wage_basis":"basic" } }
```
Gross is whole rupees; split per plan 16 F9 (allowance = remainder). `ctc = gross + employer_pf + employer_esi - employer_recovered + gratuity_accrual`. `from_ctc`: the largest gross whose monthly CTC <= target;
`unallocated_monthly` = target - CTC. Flags: `ESI_APPLIES`, `PF_CEILING_APPLIED`, `PF_NOT_APPLICABLE`, `TARGET_TOO_LOW` (no gross >= 1 fits; 422 instead when the result would be empty), `NO_PT`.
`net_monthly_estimate` excludes TDS. Errors: 422 `VALIDATION_ERROR`.

### 18.3 Gratuity accrual [4a]
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/gratuity?as_of=YYYY-MM-DD&department_id=&eligible_only=false&q=&page=&page_size= | `payroll.view_all` (audit `payroll.gratuity.view`, `after={count}`) |
| POST | /admin/payroll/reports/gratuity?period=YYYY-MM | `payroll.view_all` (15.1 report kind; `as_of` = last day of the month; .xlsx, no-store; audit `payroll.report.export`) |

`as_of` default today, 2000-01-01..today. Employees: not deleted, `include_in_payroll`, employed on `as_of` (or LWD / deactivation inside the month of `as_of`).
```json
{ "as_of":"2026-11-30","basis":"basic","cap":"2000000.00","min_years":5,
  "items":[ { "user":UserSummary,"join_date":"2019-06-01"|null,"exit_date":"2026-12-15"|null,
              "service":{ "years":7,"months":5,"days":29 },"counted_years":7,"eligible":true,
              "wage":"22000.00","accrued":"88846.00","monthly_accrual":"1057.69","flags":[] } ],
  "totals":{ "accrued":"1250000.00","eligible_accrued":"910000.00","monthly_accrual":"45000.00" },
  "page":1,"page_size":50,"total":48 }
```
`counted_years` = completed years + 1 when the remainder is more than 6 months. `accrued = min(cap, round_rupee(wage x 15 / 26 x counted_years))`, shown for every employee; `eligible` = completed years >= `payroll.gratuity_min_years`
(`exit_reason = death` [4b]: always). `wage` = monthly basic of the structure in force on `as_of` (or the larger of basic and 50% of gross when `payroll.gratuity_wage_basis = basic_or_half_gross`).
`monthly_accrual = round(wage x 15 / 26 / 12, 2)`. Flags: `NO_JOIN_DATE`, `NO_STRUCTURE` (money null), `CAPPED`. `totals` cover the whole filtered set (not the page). xlsx sheet `Gratuity` has the same columns. Errors: 422 `INVALID_DATE`.

### 18.4 Increments [4a]
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/increments?status=&user_id=&batch_id=&effective_from=YYYY-MM&page=&page_size= | `payroll.view_all` (audit `payroll.increment.list`, `after={count}`) |
| GET | /admin/payroll/increments/{id} | `payroll.view_all` (audit `payroll.increment.view`) |
| POST | /admin/payroll/employees/{user_id}/increments | `payroll.manage` |
| PUT | /admin/payroll/increments/{id} | `payroll.manage` (draft) |
| POST | /admin/payroll/increments/{id}/submit | `payroll.manage` (draft) |
| POST | /admin/payroll/increments/{id}/withdraw | `payroll.manage` (submitted -> draft) |
| POST | /admin/payroll/increments/{id}/approve | `payroll.approve` + step-up (submitted) |
| POST | /admin/payroll/increments/{id}/reject | `payroll.approve` (submitted) |
| POST | /admin/payroll/increments/{id}/cancel | `payroll.manage` (draft / submitted) |
| GET | /admin/payroll/employees/{user_id}/salary-history | `payroll.view_all` (audit `payroll.structure.view`) |

**Increment**:
```json
{ "id":"uuid","user":UserSummary,"batch_id":"uuid"|null,"kind":"percent|amount|structure|remove|import",
  "effective_from":"2027-04-01","percent":"10.00"|null,"method":"proportional|split_from_gross"|null,
  "base_structure":{ "id":"uuid","effective_from":"2026-04-01","monthly_gross":"40000.00" }|null,
  "replaces_structure_id":"uuid"|null,
  "proposed":{ "basic":"22000.00","hra":"8800.00","allowances":[ { "code":"SPECIAL","name":"Special allowance","amount":"13200.00","prorate":true,"esi_wage":true } ],
               "fixed_deductions":[],"tds_monthly":"1200.00","pf_applicable":true,"pf_on_full_basic":false,"esi_applicable":true,"pt_applicable":true,"monthly_gross":"44000.00" }|null,
  "change":{ "old_gross":"40000.00","new_gross":"44000.00","amount":"4000.00","percent":"10.00" },
  "diff":[ { "code":"BASIC","old":"20000.00","new":"22000.00" },{ "code":"SPECIAL","old":"12000.00","new":"13200.00" } ],
  "warnings":[ "BACKDATED","TDS_REVIEW" ],"reason":"Annual increment 2027",
  "status":"draft|submitted|approved|rejected|cancelled","run_status":"none|draft|processed|published",
  "structure_id":"uuid"|null,"arrears_proposed":true|null,
  "created_by":UserSummary,"created_at":"ts","submitted_by":UserSummary|null,"submitted_at":"ts|null",
  "decided_by":UserSummary|null,"decided_at":"ts|null","decision_comment":null,"version":1 }
```
List items omit `proposed`, `diff` and `decision_comment`. `run_status` is the status of the run of `effective_from`'s month. `change.percent` is rounded to 2 decimals (negative for a decrease).
**POST** (201 Increment, status `draft`), one of:
```json
{ "kind":"percent","effective_from":"2027-04-01","percent":"10.00","method":"proportional","reason":"Annual increment 2027" }
{ "kind":"amount","effective_from":"2027-04-01","monthly_gross":"44000.00","method":"split_from_gross","reason":"Promotion" }
{ "kind":"structure","effective_from":"2027-04-01","structure":{ "basic":"22000.00","hra":"8800.00","allowances":[],"fixed_deductions":[],"tds_monthly":"0.00",
  "pf_applicable":true,"pf_on_full_basic":false,"esi_applicable":true,"pt_applicable":true },"replaces_structure_id":null,"reason":"Correction" }
{ "kind":"remove","replaces_structure_id":"uuid","reason":"Entered in error" }
```
`effective_from` = 1st of a month, <= 12 months ahead, >= current month - `payroll.increment_backdate_months` (422 `INVALID_PERIOD`); `remove` takes the structure's own date. `percent` 0.01..`payroll.increment_max_pct`
(422 `INVALID_PERCENT`). `structure` follows the 13.4 validation (reserved codes, <= 15 allowances, <= 10 deductions). `kind: import` is created only by 18.6.
**PUT** `{ "expected_version":1, ...the same fields as POST for its kind (kind cannot change) }` -> 200. **submit** / **withdraw** `{ "expected_version":1 }` -> 200.
**approve** `{ "expected_version":2,"comment":null,"current_password":"..." }` -> 200 Increment (`approved`, `structure_id` set, `arrears_proposed` true when an arrears proposal was created / refreshed).
**reject** `{ "expected_version":2,"comment":"required" }` -> 200 (`rejected`, final). **cancel** `{ "reason":"..." }` -> 200.
Second person (approve and reject, single and batch): the checker is not the employee (403 `FORBIDDEN`) and took no part in the proposal: not the creator, the **last editor**
(`last_edited_by`, set on create and every PUT, never by submit / approve), the current submitter, nor anybody in the append-only history of editors and submitters (a withdraw does not erase it). The database repeats this in
`ck_salary_increments_checker` (approved AND rejected rows) and `ck_salary_increments_checker_history`.
The step-up (`current_password`) of all approve endpoints is limited to 5 per minute and 20 per day per admin (429 `RATE_LIMITED`; the limiter fails closed, 503 when Redis is down).
A structure component or the gross above 10,000,000.00 (also after scaling or splitting) is refused with 422 `VALIDATION_ERROR` (`monthly_gross`).
Approve check order: step-up, self (403), second person (403 `SECOND_PERSON_REQUIRED`), `submitted` (409 `NOT_SUBMITTED`), version (409 `VERSION_CONFLICT`), base unchanged (409 `INCREMENT_STALE`),
`STRUCTURE_EXISTS` / `LATER_STRUCTURE_EXISTS` / `STRUCTURE_IN_USE` (replace / remove), `AFTER_EXIT` (422). The structure is created with `source: "increment"`, `increment_id`, `created_by` = checker; the 17.4 arrears hook runs in the same transaction.
Errors (all): 409 `INCREMENT_EXISTS` (a draft / submitted one for the employee and month), `NOT_SUBMITTED`, `NOT_DRAFT`, `VERSION_CONFLICT`, `INCREMENT_STALE`, `STRUCTURE_EXISTS`, `LATER_STRUCTURE_EXISTS`, `STRUCTURE_IN_USE`;
422 `STRUCTURE_MISSING` (no structure in force on `effective_from`), `INVALID_PERCENT`, `INVALID_PERIOD`, `AFTER_EXIT`, `NO_CHANGE` (submit of an identical structure), `VALIDATION_ERROR`; 403 `FORBIDDEN`, `SECOND_PERSON_REQUIRED`, `REAUTH_FAILED`.
Warnings: `BACKDATED`, `TDS_REVIEW`, `DECREASE`, `LARGE_CHANGE`, `ESI_THRESHOLD_CROSSED`, `PF_CEILING_CROSSED`, `NO_CHANGE`.
Audit `payroll.increment.create|update|submit|withdraw|approve|reject|cancel` (`after={kind, effective_from, status, maker_id, checker_id, structure_id}`; never percents or amounts).
**salary-history** -> `{ "items":[ { "type":"structure","structure_id":"uuid","effective_from":"2026-04-01","effective_to":"2027-03-31"|null,"monthly_gross":"40000.00","change_percent":"8.11"|null,
"source":"manual|increment|import","increment_id":"uuid"|null,"approved_by":UserSummary|null },
{ "type":"increment","increment_id":"uuid","effective_from":"2027-04-01","status":"submitted","kind":"percent","new_gross":"44000.00" } ] }` (newest first; increments only while draft / submitted).

### 18.5 Increment batches (bulk hike) [4a]
| Method | Path | Perm |
|---|---|---|
| POST | /admin/payroll/increments/bulk | `payroll.manage` |
| GET | /admin/payroll/increments/batches?kind=percent\|import&page=&page_size= | `payroll.view_all` |
| GET | /admin/payroll/increments/batches/{id} | `payroll.view_all` |
| POST | /admin/payroll/increments/batches/{id}/submit | `payroll.manage` |
| POST | /admin/payroll/increments/batches/{id}/approve | `payroll.approve` + step-up |
| POST | /admin/payroll/increments/batches/{id}/reject | `payroll.approve` |
| POST | /admin/payroll/increments/batches/{id}/cancel | `payroll.manage` |

**bulk** request:
```json
{ "effective_from":"2027-04-01","percent":"8.00","method":"proportional","reason":"Annual increment FY 2027-28",
  "scope":{ "department_ids":["uuid"]|null,"user_ids":["uuid"]|null,"exclude_user_ids":["uuid"] },
  "overrides":[ { "user_id":"uuid","percent":"12.00" } ] }
```
`scope` null lists = all active payroll employees with a structure; at most 500 employees (else 422 `TOO_MANY_ROWS`). -> 201
`{ "batch":Batch,"created":46,"skipped":[ { "user":UserSummary,"reason":"NO_STRUCTURE|SELF|INCREMENT_EXISTS|STRUCTURE_EXISTS|LATER_STRUCTURE_EXISTS|NOT_ELIGIBLE|AFTER_EXIT" } ] }`.
Per-employee changes use `PUT /increments/{id}` while draft; `cancel` of a single row removes it from the batch outcome.
**Batch**:
```json
{ "id":"uuid","kind":"percent|import","effective_from":"2027-04-01"|null,"percent":"8.00"|null,"method":"proportional"|null,"rows":46,"reason":"...",
  "counts":{ "draft":0,"submitted":44,"approved":0,"rejected":0,"cancelled":2 },
  "totals":{ "old_gross":"1850000.00","new_gross":"1998000.00","delta_monthly":"148000.00","delta_annual":"1776000.00" },
  "file_sha256":"<64 hex>"|null,"created_by":UserSummary,"created_at":"ts","cancelled_at":"ts|null" }
```
`totals` cover the live rows (draft / submitted / approved). **submit** `{}` -> 200 `{ "submitted":44,"skipped":[ { "id","reason":"NO_CHANGE|NOT_DRAFT" } ] }`.
**approve** `{ "expected":[ { "id":"uuid","version":2 } ],"comment":null,"current_password":"..." }` -> 200 `{ "approved":43,"failed":[ { "id":"uuid","reason":"VERSION_CONFLICT|INCREMENT_STALE|LATER_STRUCTURE_EXISTS|STRUCTURE_EXISTS|AFTER_EXIT|SECOND_PERSON_REQUIRED|FORBIDDEN" } ],"arrears_proposed":5 }`
(one transaction; failed rows stay `submitted`; the batch maker counts as maker of every row). `expected` is **required** (422 when missing or malformed; an empty list is valid and approves nothing): the `id` and `version` of every
row the checker reviewed (the `submitted` rows of `GET /increments?batch_id=&status=submitted`). A submitted row that is not listed, or listed with another version, is not approved and is reported as `VERSION_CONFLICT`; unknown ids are ignored. **reject** `{ "comment":"required" }` -> 200 `{ "rejected":44 }` (403 `SECOND_PERSON_REQUIRED` when the caller made the batch or took part in any submitted row). **cancel** `{ "reason" }` -> 200 `{ "cancelled":44,"not_cancelled":0 }` (draft / submitted rows).
Audit `payroll.increment_batch.create|submit|approve|reject|cancel` (batch id, rows, counts).

### 18.6 Bulk salary-structure import [4a]
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/structures/template | `payroll.manage` |
| POST | /admin/payroll/structures/import?dry_run=true\|false | `payroll.manage` (+ step-up when `dry_run=false`) |

**template** -> `.xlsx`, sheets `Structures` (header `Employee ID | Effective From | Gross | Basic | HRA | TDS Monthly | PF | PF On Full Basic | ESI | PT`), `Components`
(header `Employee ID | Type | Code | Name | Amount | Prorate | ESI Wage`) and `Help` (rules, reserved codes, split rule). One example row per sheet (`EXAMPLE-DELETE-ME`, fails `UNKNOWN_EMPLOYEE`). `Cache-Control: no-store`.
Cells: `Effective From` text `YYYY-MM` (or a date cell; the 1st is used); flags `Y` / `N` (blank = default `Y`, `N`, `Y`, `Y`); `Type` `allowance` | `deduction`; `Prorate` / `ESI Wage` only for allowances (blank = `Y`).
Either `Gross` alone (split per the split settings, no component rows) or `Basic` (+ optional `HRA` and component rows).
**import** multipart: `file` (xlsx <= 1 MB), for `dry_run=false` also `file_sha256`, `reason` (3-200) and `current_password`. Same container checks, parser limits, formula refusal, HMAC dry-run binding
(key = actor | `structures` | sha256, 15 min, Redis fail closed) and nginx body rule as 17.2. Structures sheet <= 500 data rows, Components <= 2000.
Dry run -> 200 (writes nothing; audit `payroll.structure_import.dry_run` with rows / errors counts):
```json
{ "file_sha256":"<64 hex>","rows":120,"valid":118,
  "errors":[ { "row":14,"sheet":"Structures","field":"employee_id","message":"UNKNOWN_EMPLOYEE" },
             { "row":33,"sheet":"Components","field":"code","message":"RESERVED_CODE" } ],
  "warnings":[ { "row":22,"sheet":"Structures","field":"effective_from","message":"BACKDATED" },
               { "row":40,"sheet":"Structures","field":"employee_id","message":"NEEDS_APPROVAL" } ],
  "actions":{ "create":80,"increment":38 },
  "totals":{ "monthly_gross":"4200000.00" } }
```
Row errors: `UNKNOWN_EMPLOYEE`, `DELETED`, `SELF`, `DUPLICATE_ROW`, `INVALID_PERIOD`, `INVALID_AMOUNT`, `INVALID_FLAG`, `INVALID_TYPE`, `INVALID_CODE`, `RESERVED_CODE`, `DUPLICATE_CODE`, `TOO_MANY_COMPONENTS`,
`GROSS_AND_PARTS`, `MISSING_BASIC`, `ORPHAN_COMPONENT`, `STRUCTURE_EXISTS`, `LATER_STRUCTURE_EXISTS`, `INCREMENT_EXISTS`, `AFTER_EXIT`, `FORMULA_NOT_ALLOWED`.
Warnings: `BACKDATED`, `NO_CHANGE`, `NEEDS_APPROVAL` (becomes a submitted increment), `ESI_THRESHOLD_CROSSED`, `TDS_REVIEW`.
Commit -> 201 `{ "batch_id":"uuid","created":80,"increments_submitted":38,"arrears_proposed":0 }` only with no row errors (else 422 `IMPORT_INVALID`, `details[].field` = `"Structures row 14: employee_id"`).
One transaction. Employees without a structure: structure created (`source: "import"`, `import_batch_id`). Employees with one: a `submitted` increment (kind `import`, maker = importer) when
`payroll.structure_changes_need_approval` is true, else a structure directly (arrears hook runs for backdated rows). Errors: 409 `IMPORT_NOT_CHECKED`, `IMPORT_FILE_CHANGED`, `IMPORT_DUPLICATE` (same file imported before),
422 `FILE_TYPE_NOT_ALLOWED`, `FILE_TOO_LARGE`, `IMPORT_INVALID` (header mismatch / missing sheet / too many rows: `details[0].field` = `file`), 503 `TEMPORARILY_UNAVAILABLE`, 403 `REAUTH_FAILED`.
Audit `payroll.structure_import.commit` (batch id, created, increments_submitted).

### 18.7 Arrears bulk accept [4a]
| POST | /admin/payroll/arrears/bulk-accept | `payroll.manage` + step-up |
|---|---|---|

`{ "items":[ { "id":"uuid","expected_version":2 } ] (1..200, unique),"period":"2027-05","current_password":"..." }` -> 200
`{ "accepted":40,"skipped":[ { "id":"uuid","reason":"ARREARS_STALE|NOT_PROPOSED|ITEM_LOCKED|PERIOD_LOCKED|TOO_MANY_ITEMS|NOT_ELIGIBLE|FORBIDDEN|NOT_FOUND" } ] }`.
Same per-row checks as 17.4 accept; one transaction; run lock first. Audit `payroll.arrears.bulk_accept` (`after={period, accepted, skipped}`).

### 18.8 Full and final settlement [4b]
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/fnf?status=&q=&page=&page_size= | `payroll.view_all` (audit `payroll.fnf.list`, `after={count}`) |
| POST | /admin/payroll/employees/{user_id}/fnf | `payroll.manage` |
| GET | /admin/payroll/fnf/{id} | `payroll.view_all` (audit `payroll.fnf.view`) |
| PUT | /admin/payroll/fnf/{id} | `payroll.manage` (draft) |
| GET | /admin/payroll/fnf/{id}/preview | `payroll.view_all` (audit `payroll.fnf.preview`) |
| POST | /admin/payroll/fnf/{id}/submit | `payroll.manage` (draft) |
| POST | /admin/payroll/fnf/{id}/withdraw | `payroll.manage` (submitted -> draft) |
| POST | /admin/payroll/fnf/{id}/approve | `payroll.approve` + step-up (submitted) |
| POST | /admin/payroll/fnf/{id}/reject | `payroll.approve` (submitted -> draft) |
| POST | /admin/payroll/fnf/{id}/revoke | `payroll.approve` + step-up (approved -> draft) |
| POST | /admin/payroll/fnf/{id}/cancel | `payroll.manage` (draft / submitted) |
| GET | /admin/payroll/fnf/{id}/statement | `payroll.view_all` (PDF; audit `payroll.fnf.statement`) |

**POST** `{ "resignation_date":"2026-11-15"|null,"last_working_day":"2026-12-15","exit_reason":"resignation|termination|retirement|death|contract_end|absconding|other",
"notice_period_days":30|null,"settlement_period":"2026-12","remarks":null }` -> 201 FnF (`draft`). `notice_period_days` null = payroll details value, else `payroll.notice_period_days`.
Does NOT touch `employee_payroll_details.last_working_day` (only the approval does, see 18.14). Rules: LWD >= join date, <= today + 180 days and <= the day before the account's deactivation / deletion (422 `INVALID_DATE`; re-checked at submit and approve); `resignation_date` <= LWD (422 `INVALID_DATE`);
`settlement_period` from the LWD month to LWD month + 3 and 17.0 target month rule at approve (422 `INVALID_PERIOD`). Errors: 409 `FNF_EXISTS` (a live one, or a `settled` one unless the employee was reactivated or rejoined after its LWD), 403 `FORBIDDEN` (own), 422.
**FnF**:
```json
{ "id":"uuid","user":UserSummary,"status":"draft|submitted|approved|settled|cancelled",
  "resignation_date":"2026-11-15"|null,"last_working_day":"2026-12-15","exit_reason":"resignation","settlement_period":"2026-12","run_status":"none|draft|processed|published",
  "notice":{ "required_days":30,"served_days":31,"shortfall_days":0,"computed_shortfall_days":0,"waived":false,"override":{ "days":5,"reason":"..." }|null,
             "basis":"gross","divisor":30,"daily_rate":"1333.33","amount":"0.00","item_id":"uuid"|null },
  "encashments":[ { "leave_type":{ "id","code":"EL","name":"Earned leave" },"year":2026,"days":6.0,"available_days":8.0,"daily_rate":"666.67","amount":"4000.02",
                    "encashment_id":"uuid"|null,"status":"pending|processing|paid|cancelled"|null } ],
  "loans":[ { "loan":{ "id","reference":"L-0007","kind":"loan" },"outstanding":"22500.00","mode":"payroll|outside"|null,"paid_on":"2026-12-10"|null,"reference":"UTR123"|null,
              "installment_id":"uuid"|null,"status":"pending|processing|paid|closed"|null } ],
  "items":[ { "code":"OTHER_EARNING|OTHER_DEDUCTION|BONUS|INCENTIVE","label":"Pro-rata bonus","amount":"3000.00","item_id":"uuid"|null,"status":"pending|processing|paid|cancelled"|null } ],
  "move_open_items":true,
  "open_items":[ { "type":"one_time|encashment|arrears|reimbursement","id":"uuid","period":"2027-01"|null,"code":"BONUS","status":"pending","flags":["PERIOD_CLOSED"],"action":"move|keep" } ],
  "gratuity":{ "eligible":true,"service":{ "years":7,"months":6,"days":14 },"counted_years":8,"wage":"22000.00","amount":"101538.00","capped":false,"basis":"basic",
               "note":"For information. Gratuity is paid separately, outside payroll." },
  "warnings":[ "CLAIMS_OPEN","TDS_REVIEW" ],
  "summary":{ "earnings":"52000.00","deductions":"4100.00","net_estimate":"47900.00","computed_at":"ts" }|null,
  "final_payslip":{ "id":"uuid","revision":1,"status":"draft|processed|published|superseded" }|null,
  "remarks":null,"created_by":UserSummary,"created_at":"ts","submitted_by":UserSummary|null,"submitted_at":"ts|null",
  "decided_by":UserSummary|null,"decided_at":"ts|null","decision_comment":null,"version":3 }
```
`notice.amount` / `encashments[].amount` are quotes until approval and the stored amounts after. `summary` is the approval snapshot (null before). `gratuity` is null without a join date or structure.
List items: `{ id, user, status, last_working_day, settlement_period, exit_reason, run_status, warnings, created_at }` (no money).
**PUT** (draft) `{ "expected_version":3, "resignation_date"?, "last_working_day"?, "exit_reason"?, "notice_period_days"?, "settlement_period"?,
"notice"?:{ "waived":false,"override":{ "days":5,"reason":"Handover completed early" }|null },
"encashments"?:[ { "leave_type_id":"uuid","year":2026,"days":6.0 } ] (<= 10),
"loans"?:[ { "loan_id":"uuid","mode":"payroll" } | { "loan_id":"uuid","mode":"outside","paid_on":"2026-12-10","reference":"UTR123" } ],
"items"?:[ { "code":"OTHER_EARNING","label":"Pro-rata bonus","amount":"3000.00" } ] (<= 20), "move_open_items"?:true, "remarks"?:"..." }` -> 200 FnF.
Lists are replaced as a whole. `override.days` 0..required; `items[].code` one of the 17.2 codes (`NOTICE_RECOVERY` -> 422); loan ids must be the employee's `active` loans; `reference` per 17.3.
Errors: 409 `NOT_DRAFT`, `VERSION_CONFLICT`, `INSUFFICIENT_BALANCE` (encashment days), 422 `NOT_ENCASHABLE`, `INVALID_DATE`, `INVALID_PERIOD`, `VALIDATION_ERROR`.
**preview** -> 200 `{ "estimate":true,"days":{ "basis_days":31.0,"employed_days":15.0,"lop_days":0.0,"paid_days":15.0 },"earnings":[ Line ],"deductions":[ Line ],
"totals":{ "gross_earnings","total_deductions","net_pay" },"warnings":[...],"source":"structure|payslip" }` (Line = 13.2 earnings/deductions item). Uses the open payslip of the settlement month when one exists
(`source: payslip`), else the structure. Rate limit `payroll:admin_read`.
**submit** / **withdraw** `{ "expected_version" }` -> 200. Submit needs a mode for every active loan (422 `LOAN_UNRESOLVED`) and re-checks quotes (409 `INSUFFICIENT_BALANCE`).
**approve** `{ "expected_version":4,"comment":null,"current_password":"..." }` -> 200 FnF (`approved`). In one transaction (lock order plan 16 F15, F&F row last): target month rule for `settlement_period`,
then creates the `NOTICE_RECOVERY` one-time item (when amount > 0), the other items, the encashments (`source: fnf`), the loan foreclosures (`payroll`: installment in `settlement_period`; `outside`: loan closed),
moves open items (when `move_open_items`), all with `fnf_id`, events and the `summary` snapshot. Any failure rolls everything back: 409 `PERIOD_LOCKED`, `ITEM_LOCKED`, `INSUFFICIENT_BALANCE`, `LOAN_NOT_ACTIVE`,
`TOO_MANY_ITEMS`, `FNF_NEGATIVE` (net estimate below 0), `FNF_STALE_QUOTE` (notice, encashment or loan amounts differ from the ones frozen at submit), `FNF_EXISTS` (another approved / settled F&F for the employee and month), `VERSION_CONFLICT`, `NOT_SUBMITTED`; 403 `SECOND_PERSON_REQUIRED`, `FORBIDDEN`, `REAUTH_FAILED`; `details[0].field` names the part (`notice`, `encashments[0]`, `loans[1]`, `items[2]`).
**reject** `{ "expected_version","comment":"required" }` -> 200 (back to `draft`, `decision_comment` set). **cancel** `{ "reason" }` -> 200 (draft / submitted; changes nothing in the payroll details).
**revoke** `{ "expected_version","reason","current_password" }` -> 200 (`draft`): only while no `fnf_id` row is `processing` / `paid` (409 `FNF_NOT_REVOCABLE`); cancels created items and encashments (days back),
cancels `payroll` foreclosure installments and regenerates the loan schedule with its EMI from `settlement_period`; `outside` foreclosures stay closed (`details` lists them). Moved items stay moved.
**Settlement**: the publish (13.5 / 13.6) of the employee's payslip in `settlement_period` sets `settled` and `final_payslip_id` in the same transaction (audit `payroll.fnf.settle`, push `fnf_settled`);
publishing a later revision of that payslip moves `final_payslip_id`. Reopen leaves the F&F `approved`.
**statement** -> `application/pdf`, `filename="fnf-EMP014-2026-12.pdf"`, `Cache-Control: no-store`, `nosniff`, CSP as 13.8. Content: company, employee, dates, exit reason, notice (required / served / shortfall / waived),
lines of the final payslip grouped (salary for the final month, leave encashment, notice recovery, loan recovery, other items, statutory deductions), loans settled outside (reference, date),
net payable in figures and words, gratuity information block. Watermark `DRAFT - NOT FINAL` until `settled` (from `preview` data). 503 `TEMPORARILY_UNAVAILABLE` when PDF slots are busy.
Audit `payroll.fnf.create|update|submit|withdraw|approve|reject|revoke|cancel|settle` (`after={status, last_working_day, settlement_period, exit_reason, shortfall_days, counts:{items, encashments, loans}, maker_id, checker_id}`; never amounts, labels, references or remarks).

### 18.9 Employee F&F [4b]
| Method | Path | Perm |
|---|---|---|
| GET | /me/fnf | `payroll.view_own` |
| GET | /me/fnf/statement | `payroll.view_own` (owner, `settled` only; audit `payroll.fnf.view_own`) |

**GET /me/fnf** -> `{ "fnf": { "id":"uuid","status":"in_progress|settled","last_working_day":"2026-12-15","settlement_period":"2026-12","final_payslip_id":"uuid"|null } | null }`
(`submitted` / `approved` show as `in_progress`; `draft` and `cancelled` are not shown; no money). **statement** -> PDF as 18.8 without watermark; anything else 404 (audited `payroll.denied`).
Only reachable while the account is active (a deactivated user cannot sign in).

### 18.10 Push (section 11; data messages, never amounts or percents)
- `payroll_approval` to the `payroll.approve` holders except the maker (max 20, coalesced 10 min per kind): id = increment / batch / F&F id, `"kind":"increment|increment_batch|fnf"`, title "Approval needed", body "<maker name> submitted <n> increment(s)" / "<maker name> submitted a final settlement for <employee name>".
  Also to the maker on approve / reject: title "Increment approved" / "Final settlement rejected", body = employee name or batch row count.
- `fnf_settled` to the employee (best effort; usually no device after deactivation), id = F&F id, "Final settlement completed", body "Your final settlement statement is available."
Best effort, after commit.

### 18.11 New settings (`/admin/payroll/settings`, snapshotted into each run like the other `payroll.*` keys)
| Key | Default | Range |
|---|---|---|
| `payroll.structure_changes_need_approval` [4a] | true | bool (change needs `role.assign` + step-up) |
| `payroll.split_basic_pct` [4a] | 50 | 1..100 |
| `payroll.split_hra_pct_of_basic` [4a] | 40 | 0..100 |
| `payroll.split_allowance_code` / `payroll.split_allowance_name` [4a] | `SPECIAL` / `Special allowance` | code per 13.0, not reserved / name 1-40 |
| `payroll.increment_max_pct` [4a] | 50 | 1..100 |
| `payroll.increment_backdate_months` [4a] | 12 | 0..24 |
| `payroll.ctc_include_gratuity` [4a] | true | bool |
| `payroll.gratuity_min_years` [4a] | 5 | 0..10 |
| `payroll.gratuity_cap` [4a] | `"2000000.00"` | money |
| `payroll.gratuity_wage_basis` [4a] | `"basic"` | `basic` \| `basic_or_half_gross` |
| `payroll.notice_period_days` [4b] | 30 | 0..365 |
| `payroll.notice_recovery_basis` [4b] | `"gross"` | `gross` \| `basic` |
| `payroll.notice_recovery_divisor` [4b] | 30 | 1..31 |

### 18.12 Error codes (new)
409: `APPROVAL_REQUIRED`, `INCREMENT_EXISTS`, `INCREMENT_STALE`, `LATER_STRUCTURE_EXISTS`, `NOT_SUBMITTED`, `NOT_DRAFT`, `FNF_EXISTS`, `FNF_OPEN`, `FNF_NOT_REVOCABLE`, `FNF_NEGATIVE`, `FNF_MANAGED`, `FNF_STALE_QUOTE` (review fix, 18.14).
422: `INVALID_PERCENT`, `AFTER_EXIT`, `NO_CHANGE`, `LOAN_UNRESOLVED`, `TOO_MANY_ROWS`.
Reused: `SECOND_PERSON_REQUIRED`, `STRUCTURE_EXISTS`, `STRUCTURE_IN_USE`, `STRUCTURE_MISSING` (422), `VERSION_CONFLICT`, `PERIOD_LOCKED`, `ITEM_LOCKED`, `TOO_MANY_ITEMS`, `INSUFFICIENT_BALANCE`, `NOT_ENCASHABLE`,
`LOAN_NOT_ACTIVE`, `ARREARS_STALE`, `NOT_PROPOSED`, `IMPORT_*` (17.2), `FILE_TYPE_NOT_ALLOWED`, `FILE_TOO_LARGE`, `INVALID_PERIOD`, `INVALID_DATE`, `REAUTH_FAILED`, `FORBIDDEN`, `RATE_LIMITED`, `TEMPORARILY_UNAVAILABLE`.


### 18.13 Implementation notes, work package 4a step 1 (increments, batches, arrears bulk accept, F2) - IMPLEMENTED
Where the backend refines the text above (Flutter should follow these):
- **Approve response** `arrears_proposed` is `true` when an arrears proposal exists after the approval, otherwise `null` (never `false`); it is `null` on every other read.
- **Batch submit** `skipped[].reason` can also be `FORBIDDEN` (the caller is the employee of that row; the row stays `draft`). Rows of a batch that were cancelled are not reported.
- **Batch approve** on a batch with no `submitted` row answers 200 `{ "approved":0, "failed":[], "arrears_proposed":0 }`. `failed[].reason` is the error code of the failed check (`INCREMENT_STALE`, `STRUCTURE_EXISTS`,
  `LATER_STRUCTURE_EXISTS`, `STRUCTURE_IN_USE`, `AFTER_EXIT`, `SECOND_PERSON_REQUIRED`, `FORBIDDEN`). A caller who is the batch creator fails every row with `SECOND_PERSON_REQUIRED`.
- **Batch reject** answers 403 `SECOND_PERSON_REQUIRED` for the batch creator or any row's creator / submitter; rows whose employee is the caller stay `submitted` (not counted in `rejected`).
- **Batch cancel** sets `cancelled_at` only when no row is left that could not be cancelled (`not_cancelled` = 0); rows of the caller as employee count as not cancelled.
- **Bulk hike** `scope`: `user_ids` given = exactly those employees (not eligible -> `NOT_ELIGIBLE`, no structure -> `NO_STRUCTURE`, the caller -> `SELF`); `user_ids` null = every active employee included in payroll WITH a structure
  (others are silently not part of the scope). Unknown ids -> 404. `department_ids` and `user_ids` together intersect. Every `overrides[].user_id` must belong to the scope (else 422) and its percent is capped like the batch percent
  (422 `INVALID_PERCENT`, `details[].field` = `overrides[i].percent`). When no employee qualifies the call is 422 `VALIDATION_ERROR` (no empty batch is created).
- **Salary history / lists**: an increment of kind `remove` has `proposed: null`, `diff: []` and `change.new_gross/amount/percent: null`.
- **Edit**: the month of a bulk row is fixed by its batch (422 `VALIDATION_ERROR`, field `effective_from`); `kind` cannot change (422, field `kind`).
- **Remove / replace** do not apply the "at most `increment_backdate_months` back" rule (they correct an existing structure); the 12-months-ahead rule still applies to replace. A replace updates the structure row IN PLACE
  (same id, `source` becomes `increment`), so draft payslips keep pointing at it; a remove deletes the row.
- **Audit** rows of increments carry `user_id` as well (`after = { user_id, kind, effective_from, status, maker_id, checker_id, structure_id }`). The setting change audit of `payroll.structure_changes_need_approval` follows `payroll.settings.update`.
- **F2 (18.1)** is evaluated BEFORE the in-use check: a first structure that a processed / published payslip uses can no longer be edited or deleted directly (409 `APPROVAL_REQUIRED`, was `STRUCTURE_IN_USE`); `STRUCTURE_IN_USE` stays
  for the switch-off mode. Structure responses (13.4) and the employee list items are unchanged apart from `source` and `increment_id`.
- **Not in step 1**: structure import (18.6), CTC calculator (18.2), gratuity (18.3) and everything marked [4b]. `kind: import` increments and `salary_increment_batches.kind = 'import'` exist in the schema for step 2.

### 18.14 Implementation notes, work package 4b (full and final settlement) - IMPLEMENTED
Where the backend refines 18.0, 18.1 [4b] and 18.8 to 18.12 (Flutter should follow these):
- **New error codes**: 409 `FNF_NOT_INCLUDED` (publishing a payslip that does not carry every non-cancelled line the F&F created; reopen, refresh and process again), 422 `LOAN_UNRESOLVED` (submit, and again at approve).
  Approve / submit also answer 422 `STRUCTURE_MISSING` (`details[0].field` `notice`) when there is a notice shortfall and no structure in force for the LWD month.
- **Maker-checker**: the checker is not the employee (403 `FORBIDDEN`, audited `payroll.denied`), nor the creator, the last editor, the submitter or anybody in the append-only `contributors` list (403 `SECOND_PERSON_REQUIRED`).
  `reject` and `revoke` keep `decided_by`: that person can no longer edit or submit the record (403 `SECOND_PERSON_REQUIRED` on PUT; the next `submit` clears the old decision). The revoker must not be the employee; the revoker and the reason go to the money history only.
  Order for approve: step-up (422 when `current_password` is missing, 403 `REAUTH_FAILED`), employee / second person, `NOT_SUBMITTED`, `VERSION_CONFLICT`, then the target month (`PERIOD_LOCKED`) and the content checks.
- **PUT**: at least one field besides `expected_version` (else 422); `last_working_day`, `exit_reason`, `settlement_period`, `notice`, `encashments`, `loans`, `items`, `move_open_items` cannot be `null`; `resignation_date`, `remarks` and `notice_period_days` can
  (`notice_period_days: null` = the payroll details value, else the company setting). Changing the LWD changes the F&F only (the details follow at approval). `override.days` above `notice_period_days` is 422. Encashment lines are validated with the quote
  (year = LWD year or the year before; 409 `INSUFFICIENT_BALANCE` / 422 `NOT_ENCASHABLE` carry `details[0].field = "encashments[i]"`), loan lines must be active loans of the employee (`paid_on` not in the future).
- **FnF shape**: `notice.served_days` is `null` without a resignation date (shortfall 0 then); `notice` also has `status` (of the created recovery item, else `null`); `encashments[].available_days` is `null` when the quote failed (the response then carries the
  `ENCASHMENT_INVALID` warning). `loans` lists every ACTIVE loan of the employee (`mode: null` until chosen) plus the ones in the plan; after approval `outstanding` is the amount fixed at approval and an `outside` loan shows `status: "closed"`.
  `open_items` of an approved F&F is the list of rows that were moved. `summary` is `null` until approval and again after a revoke. `run_status` is the status of the settlement month's run.
- **Open items** (`move_open_items`): rows `pending` (one-time items, encashments, accepted arrears) or `approved` (claims) in a month other than the settlement month, whose month is after the LWD month or that are `PERIOD_CLOSED`. Rows created by the F&F itself are never listed.
  Moved rows keep their `fnf_id` null (they are not owned by the settlement).
- **Rows created at approval** have `created_by` = the checker. Notice: label `Notice period shortfall (n d)`. Encashment rows: `source = fnf`, reason `Final settlement`. A `payroll` loan: pending installments are cancelled and one `foreclosure` installment for the outstanding amount is created in the settlement month.
  The estimate over everything pending in the settlement month must not be negative (409 `FNF_NEGATIVE`, `details[0].field` `items`); the whole approval rolls back.
- **Revoke** response is the FnF plus `details`: `[{ "field":"loans[i]", "message":"closed as paid outside; it stays closed" }]` for `outside` foreclosures (empty list otherwise). The loan schedule restarts in the settlement month (or after the last live installment) with the loan's EMI.
  `FNF_NOT_REVOCABLE` is also returned when the F&F is not `approved`. A `settled` F&F cannot be revoked.
- **Preview**: `source` is `payslip` only for an `approved` / `settled` F&F that has a payslip in the settlement month (draft / processed / published copy, its stored figures); in every other case it is the estimate from the structure (`structure`), including the lines a
  draft would add. `warnings` of the preview are the payslip warnings of the estimate plus the plan flags; the full checklist is `FnF.warnings`. Checklist codes: `ARREARS_PROPOSED`, `CLAIMS_OPEN` (claims `submitted` / `verified`), `OVERTIME_UNREVIEWED`, `INCREMENT_PENDING`, `PAID_AFTER_EXIT`,
  `ACCOUNT_STILL_ACTIVE`, `NEGATIVE_NET_ESTIMATE`, `TDS_REVIEW`, `NO_JOIN_DATE`, `STRUCTURE_MISSING`, `ENCASHMENT_INVALID`. List items compute the cheap subset only (no `PAID_AFTER_EXIT`).
- **Settlement-only payslip**: eligibility needs the F&F `approved` (or `settled`) and the settlement month different from the LWD month. `employed_days` and `paid_days` are 0; BASIC / HRA / allowance lines are present with amount `0.00`; warning `FNF_SETTLEMENT_ONLY` replaces `ZERO_PAID_DAYS`.
  An already created DRAFT run must be refreshed (`POST /runs/{id}/refresh`) to pick the employee up. Professional tax and ESI/PF still follow the normal rules on the components (a settlement-only payslip is calculated like any other). A published month without an open revision for the employee stays `PERIOD_LOCKED`
  (only the employee's normal payslip of the LWD month can be revised; a later published month has no payslip for a leaver).
- **Warnings** `EXIT_IN_MONTH` (LWD inside the month) and `FNF_SETTLEMENT_ONLY` are payslip warnings. `AdminPayslip.final_settlement` is set for `approved` and `settled`; `is_final` is true for the payslip of the settlement month (employee: only when `settled`; admin: also while `approved`, both for every revision).
- **Settle**: `on_published` settles inside the publish transaction (run publish and single-payslip publish). It refuses (409 `FNF_NOT_INCLUDED`, nothing is published) when a created line is not `paid` on that payslip. A revision publish moves `final_payslip_id` (same check).
- **13.4 details**: `notice_period_days` alone needs no step-up; `last_working_day` is read-only (sending it is 422). **Dashboard**: the new alerts are in `calendar.alerts`; `EXIT_WITHOUT_FNF` = a user (not deleted) who left (deactivated, or LWD passed) with an active loan,
  or deactivated in the last 90 days with no F&F at all. `leavers[].date` is the LWD when set (users with an LWD in another month are not leavers of the deactivation month).
- **17.2 / 17.3 / 17.5**: `fnf_id` is on OneTimeItem, installments and Encashment. 17.5 `POST /encashments` with `source: "fnf"` is 409 `FNF_MANAGED`. Loan `skip` / `reschedule` / `foreclose` / `cancel` on a loan with a live F&F installment: 409 `FNF_MANAGED`.
- **Increments / import / gratuity** treat the LWD like the deactivation date (`AFTER_EXIT`, gratuity `exit_date`).
- **Push**: `payroll_approval` (`kind: "fnf"`): to approvers on submit (`<maker name> submitted a final settlement for <employee name>`) and to the maker on approve ("Final settlement approved") / reject ("Final settlement rejected", body = employee name). `fnf_settled` after the publish commit.
- **Statements**: `GET /admin/payroll/fnf/{id}/statement` works in every status (watermark `DRAFT - NOT FINAL` unless `settled`); `GET /me/fnf/statement` only for the caller's settled F&F (anything else 404, audited `payroll.denied`; success audited `payroll.fnf.view_own`).
- **Rate limits** as 18.0; reads, preview and list `payroll:admin_read`; the statement `payroll:report` (fail closed).
- **Security review fixes (4b, migration 0025 edited in place; request / response shapes unchanged)**:
  - **Details last working day (H1)**: `employee_payroll_details.last_working_day` is written ONLY by `approve` (second person + step-up) and cleared by `revoke`; a draft, submitted, rejected or cancelled F&F keeps the LWD on the F&F row alone, so
    payroll eligibility, the ESI file, dashboard `leavers`, increment `AFTER_EXIT` and gratuity ignore it until it is approved. The employment window is `min(month end, LWD, deactivation / deletion date - 1)`: an LWD can never extend pay past the deactivation.
    Create / PUT / submit / approve refuse an LWD later than the deactivation (or deletion) date - 1 (422 `INVALID_DATE`, `details[0].field` `last_working_day`). An LWD before a month that is already processed / published is NOT refused (HR files late exits):
    `PAID_AFTER_EXIT` warns on the draft, and the approval audit row and money-history event carry `paid_after_exit: true`.
  - **One settlement per exit (M1)**: create is 409 `FNF_EXISTS` while a `settled` F&F exists, unless the employee was reactivated (details LWD cleared) or `join_date` is after that LWD. Database: unique `(user_id, settlement_period)` for `approved` / `settled`
    (409 `FNF_EXISTS`). Cancelling a draft / submitted F&F never changes the details, so a second F&F cannot clear the first exit's LWD.
  - **Quote freeze (M2)**: `submit` stores the notice (basis, divisor, daily rate, amount), the encashment rate / amount per line and the outstanding amount per planned loan in the submitted (frozen, encrypted) row; `approve` recomputes them and answers
    409 `FNF_STALE_QUOTE` when anything differs (nothing is written; the checker rejects, or the maker withdraws and submits again). Withdraw and reject discard the snapshot; the approval summary replaces it. Not exposed in `FnF`.
  - **Maker guard (L1)**: `submit` and `withdraw` are 403 `SECOND_PERSON_REQUIRED` for the person who last decided the record. **Approve (L5)**: 409 `PERIOD_LOCKED` when the settlement month's run is `processed` (reopen it first); the net check (L4) uses the run's
    settings snapshot and the manual TDS / PT / adjustments of the open payslip; the item cap (L6) counts moved arrears and claims. **Revoke (L2, L3)**: every cancelled row must carry this F&F's id and the employee (else 409 `CONFLICT`, database triggers repeat it);
    the regenerated loan schedule starts at the first month that accepts a new row (published months without an open revision for the employee are skipped).

## 19. Payroll controls and delivery: publish maker-checker, bank payment file, investment declarations / Form 16, TDS engine (plan 17 = plan 14 phase 5) - PROPOSED; 5a (19.1 [5a], 19.2 to 19.5, 19.10 [5a], 19.12 [5a], notes in 19.14) and 5b (19.1 [5b], 19.6 to 19.8, 19.10 [5b], 19.12 [5b], notes in 19.15) implemented in the backend

Plan: `docs/plans/17-payroll-phase5.md`. Sections 13.0, 17.0 and 18.0 conventions apply (money strings, `period` = `YYYY-MM`, labels 1-40 / reasons and comments 3-200 chars cleaned, unknown fields 422,
self rule 403 `FORBIDDEN` audited `payroll.denied`, admin unknown ids 404, employee routes 404 for anything not their own, audited probe). `fy` = financial year `YYYY-YY` (`"2026-27"` = 1 Apr 2026 .. 31 Mar 2027).
Money, PAN, bank accounts, IFSC, landlord / lender PAN, bank references, file names, labels, reasons and comments are never written to audit rows, logs, push payloads or error messages.
**[5a]** = work package 5a (migration 0026), **[5b]** = 5b (0027), **[5c]** = 5c (0028).

### 19.0 Common
**Permissions.** Reads `payroll.view_all`; maker actions `payroll.manage`; publish checker `payroll.publish`; payment batch, tax rules and Form 16 checker `payroll.approve`; employee tax self-service `payroll.view_own`.
**Maker-checker.** As 18.14: the checker is not the employee (403 `FORBIDDEN`), nor the creator, last editor, submitter or anybody in the append-only `contributors` / `makers` list (403 `SECOND_PERSON_REQUIRED`). The database repeats it with CHECK constraints. Decision audits carry `maker_id` (requester / submitter) and `checker_id`.
**Step-up** (`current_password`; 422 `VALIDATION_ERROR` when missing, 403 `REAUTH_FAILED`; verified before any state check; 5/min + 20/day per admin for approve endpoints, fail closed): publish request approve, payment batch approve, bank file download, payment confirm,
bank account create (and reactivating an account or changing the default flag, 19.4), rules verify, declaration on behalf, Form 16 commit / publish / withdraw, 24Q and computation statement exports, declarations xlsx with identifiers, and changing `payroll.bank_change_alert_days`, `payroll.form16_requires_second_person`, `payroll.tds_engine_enabled` (these also need `role.assign`, like 13.3, and send an `admin_alert` push).
**Rate limits** (per user, Redis; writes fail closed -> 503 `TEMPORARILY_UNAVAILABLE`): reads `payroll:admin_read` 120/min; creates / edits / submit / withdraw / cancel `payroll:write` 60/min; approve / reject / verify / publish / remind `payroll:action` 10/min;
bank file `payroll:bank_file` 10/min + 30/day; Form 16 upload `payroll:import` 10/min + 50/day (shared with 17.2); 24Q / statements `payroll:statutory_file` 10/min + 30/day; employee tax reads `payroll:own_list` 60/min; employee declaration / proof writes `tax:write` 30/min + 200/day; employee Form 16 download `payroll:own_detail` (shared with payslips).
**Money caps.** Monthly amounts 0.01..10000000.00; annual tax amounts 0.01..120000000.00; bank line <= the payslip net; batch totals are computed, never accepted. Over the cap -> 422 `VALIDATION_ERROR` (`details.field` = the field).
**Money history.** Every create / edit / state change appends to `payroll_item_events` (`item_type` `publish_request`, `payment_batch`, `tax_rules`, `tax_declaration`, `tax_proof`, `form16`); not exposed.

### 19.1 Changes to existing sections
- **13.5 / 13.6 publish [5a]**: while maker-checker applies to the run (run snapshot OR current `payroll.publish_requires_second_person`), `POST /runs/{id}/publish` and `POST /payslips/{id}/publish` answer 409 `PUBLISH_REQUEST_REQUIRED`; publishing happens through 19.2. Unchanged when it does not apply.
- **13.5 Run** gains `"publish_request": { "id","status","requested_by":UserSummary,"requested_at","stale_reason" } | null` (latest request). **13.6 AdminPayslip** gains the same for scope `payslip`.
- **13.5 / 13.6 [5a]**: run create, refresh, process, reopen and payslip PUT / process / reopen / revise add the actor to `contributors` (not exposed). Reopen, refresh and payslip-level process / reopen mark open publish requests of that run `stale`.
- **13.2 [5a]**: `GET /payslips/{id}` and `/pdf` for the owner set `first_viewed_at` / `first_downloaded_at` in `payslip_deliveries` (no response change).
- **13.4 details [5a]**: a change of `bank_name`, `bank_account`, `bank_ifsc` or `payment_mode` sets `bank_changed_by` / `bank_changed_at` (read-only `"bank_changed_at":"ts"|null` added to the details object).
- **13.6 AdminPayslip [5c]** gains `"tds_computation": TdsComputation | null` (19.9); **PUT** gains `"tds_override_reason"` (3-200, required when `tds` is sent while the run's engine mode is `apply`, else 422). Warnings gain `TDS_OVERRIDDEN`, `TDS_CAPPED_NET`, `TDS_EXCESS`, `TAX_RULES_NOT_VERIFIED`, `NO_PAN_206AA`, `PERQUISITE_REVIEW`, `LEAVE_ENCASH_REVIEW`, `RULES_CHANGED`.
- **13.5 Run.config [5c]** snapshot gains `tds_engine_enabled`, `tds_engine_mode`, `tax_rules: { "fy","id","version" } | null`.
- **16 challans [5c]**: `PUT /statutory/challans` with `type: "tds"` and `status: "paid"` also takes `bsr_code` (`^\d{7}$`), `challan_serial` (`^\d{1,5}$`) and `deposited_on` (date, not future); all three are REQUIRED for TDS from 5c on (422). Response adds `"bsr_code":"XXX4567"` (masked), `"challan_serial"`, `"deposited_on"`. Encrypted; never audited.
- **15.1 dashboard [5a]** alerts gain `PUBLISH_PENDING_APPROVAL`, `PAYMENT_UNDERPAID` (an earlier copy's exported line failed after a revision delta was exported), `PAYMENT_NOT_EXPORTED` (published run, pay date passed, eligible lines not in an exported batch), `PAYMENT_FAILED_LINES`; **[5b]** `TAX_RULES_NOT_VERIFIED` (current FY), `PROOFS_PENDING`.
- **`POST /files` [5b]** accepts `purpose=tax_proof` (`payroll.view_own`; jpeg / png / pdf <= 5 MB) and `purpose=form16` (internal only: created by 19.8 upload, not accepted from `POST /files`).
- **Section 11 push** types: `payslip_reminder`, `form16_available`, `tax_proof_decision`, `tax_declaration_changed`, `tax_window`; `payroll_approval` kinds gain `publish`, `payment_batch`, `tax_rules`, `form16` (19.12).

### 19.2 Publish requests (maker-checker for publish) [5a]
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/runs/{id}/publish-check | `payroll.view_all` |
| GET | /admin/payroll/payslips/{id}/publish-check | `payroll.view_all` |
| POST | /admin/payroll/runs/{id}/publish-requests | `payroll.manage` (run `processed`) |
| POST | /admin/payroll/payslips/{id}/publish-requests | `payroll.manage` (payslip `processed`, run `published`) |
| GET | /admin/payroll/publish-requests?status=&run_id=&page=&page_size= | `payroll.view_all` (audit `payroll.publish_request.list`, `after={count}`) |
| GET | /admin/payroll/publish-requests/{id} | `payroll.view_all` (audit `payroll.publish_request.view`) |
| POST | /admin/payroll/publish-requests/{id}/withdraw | `payroll.manage` (submitted) |
| POST | /admin/payroll/publish-requests/{id}/approve | `payroll.publish` + step-up (submitted) |
| POST | /admin/payroll/publish-requests/{id}/reject | `payroll.publish` (submitted) |

**publish-check** -> 200
```json
{ "maker_checker":true,"can_request":false,"can_publish_directly":false,"can_approve":false,
  "open_request":{ "id":"uuid","status":"submitted" }|null,
  "me":{ "reasons":["EDITED_PAYSLIP","CREATED_COMPONENT"] },
  "makers":[ { "user":UserSummary,"reasons":["PROCESSED_RUN"] } ],
  "publishable":{ "payslips":46,"held":2 } }
```
Reasons: `PROCESSED_RUN`, `EDITED_PAYSLIP`, `CREATED_COMPONENT`, `CREATED_STRUCTURE`, `FNF_MAKER`, `REQUESTED`, `OWN_PAYSLIP` (scope payslip only). `can_publish_directly` is true only when maker-checker does not apply.
**create** `{ "comment":null|"..." }` -> 201 PublishRequest. **withdraw** `{ "expected_version":1 }` -> 200. **reject** `{ "expected_version":1,"comment":"required" }` -> 200.
**approve** `{ "expected_version":1,"comment":null,"current_password":"..." }` -> 200 `{ "request":PublishRequest,"run":Run }` (scope payslip: `"payslip":AdminPayslip` instead of `run`). The publish (13.5 / 13.6 internals, pushes, F&F settle, arrears resync) runs in the same transaction.
**PublishRequest**:
```json
{ "id":"uuid","scope":"run|payslip","run":{ "id":"uuid","period":"2026-10","status":"processed" },
  "payslip":{ "id":"uuid","user":UserSummary,"revision":2 }|null,
  "status":"submitted|approved|rejected|withdrawn|stale","stale_reason":null|"RUN_REOPENED|RUN_REFRESHED|PAYSLIP_CHANGED|EXPIRED",
  "snapshot":{ "payslips":46,"held":2,"warnings":5,"processed_at":"ts",
               "totals":{ "gross_earnings":"1850000.00","total_deductions":"160000.00","net_pay":"1690000.00","employer_cost":"0.00" } },
  "comment":null,"requested_by":UserSummary,"requested_at":"ts","expires_at":"ts",
  "makers":[ UserSummary ]|null,"decided_by":UserSummary|null,"decided_at":"ts|null","decision_comment":null,"version":1 }
```
`makers` is filled at approve / reject (the set computed under locks). List items omit `snapshot.totals` and `makers`.
Approve check order: step-up; scope payslip and the caller is the employee (403 `FORBIDDEN`); caller in the maker set (requester, run / payslip contributors, actors of the components' money history, F&F makers for F&F rows, creators / editors of `manual` / `import` structures used) (403 `SECOND_PERSON_REQUIRED`);
`submitted` (409 `NOT_SUBMITTED`); `expected_version` (409 `VERSION_CONFLICT`); expired (409 `PUBLISH_STALE`, request becomes `stale`); run / payslip still `processed` and the set of processed non-held payslips, their versions and result MACs equal the snapshot (409 `PUBLISH_STALE`, request becomes `stale`);
then the publish errors (409 `COMPONENTS_CHANGED`, `FNF_NOT_INCLUDED`: everything rolls back, the request stays `submitted`).
Errors (all): 409 `MAKER_CHECKER_OFF`, `PUBLISH_REQUEST_EXISTS`, `PUBLISH_REQUEST_REQUIRED` (13.5 / 13.6), `PUBLISH_STALE`, `NOT_SUBMITTED`, `VERSION_CONFLICT`, `PAYROLL_LOCKED`, `NOTHING_TO_PUBLISH`, `COMPONENTS_CHANGED`, `FNF_NOT_INCLUDED`; 403 `FORBIDDEN`, `SECOND_PERSON_REQUIRED`, `REAUTH_FAILED`; 429.
Audit `payroll.publish_request.create|withdraw|approve|reject|expire|stale` (`after={scope, run_id, period, payslips, status, stale_reason, maker_id, checker_id, self_included}`); the publish itself keeps `payroll.run.publish` / `payroll.payslip.publish` with `after.request_id`.

### 19.3 Payslip delivery (push only) [5a]
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/runs/{id}/delivery?state=viewed\|not_viewed\|no_device&q=&page=&page_size= | `payroll.view_all` (audit `payroll.delivery.view`, `after={count}`) |
| POST | /admin/payroll/runs/{id}/delivery/remind | `payroll.manage` |

**delivery** -> `{ "counts":{ "published":46,"pushed":44,"viewed":30,"downloaded":12,"no_device":2 },"items":[ { "user":UserSummary,"payslip_id":"uuid","revision":1,"pushed_at":"ts|null","push_count":1,
"reminders":0,"first_viewed_at":"ts|null","first_downloaded_at":"ts|null","has_device":true } ],"page":1,"page_size":50,"total":46 }`.
**remind** `{ "user_ids":["uuid"]|null }` (null = every not-viewed employee with a device; <= 500) -> 200 `{ "sent":14,"skipped":[ { "user_id":"uuid","reason":"VIEWED|NO_DEVICE|TOO_SOON|LIMIT_REACHED|NOT_PUBLISHED" } ] }`.
Max one reminder per payslip per 24 h and 3 in total. Run must be published (409 `PAYROLL_LOCKED` otherwise). Push `payslip_reminder`. Audit `payroll.delivery.remind` (`after={run_id, sent, skipped}`).

### 19.4 Company bank (debit) accounts [5a]
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/bank-accounts | `payroll.view_all` |
| POST | /admin/payroll/bank-accounts | `payroll.manage` + step-up |
| PUT | /admin/payroll/bank-accounts/{id} | `payroll.manage` |

**BankAccount** `{ "id","label":"HDFC salary account","bank_name":"HDFC Bank","account":"XXXXXX4321","ifsc":"HDFC0001234","is_default":true,"active":true,"created_at","created_by":UserSummary }` (account always masked).
**POST** `{ "label","bank_name","account":"^\\d{9,18}$","ifsc":"^[A-Z]{4}0[A-Z0-9]{6}$","is_default":false,"current_password" }` -> 201; `admin_alert` push to `role.assign` holders. **PUT** `{ "label"?, "is_default"?, "active"?, "current_password"? }` -> 200 (account / IFSC cannot change: 422; create a new account). **Reactivating** an account (`active: true` on an inactive one) and **every change of `is_default`** need `current_password` (422 when missing, 403 `REAUTH_FAILED`, verified first) and send an `admin_alert` push; a label change or a deactivation needs neither.
Deactivating an account used by a `draft` / `submitted` / `approved` batch -> 409 `IN_USE`. Audit `payroll.bank_account.create|update` (id, label changes as field names only).

### 19.5 Payment batches and the bank file [5a]
| Method | Path | Perm |
|---|---|---|
| POST | /admin/payroll/runs/{id}/payment-batches | `payroll.manage` (run `published`) |
| GET | /admin/payroll/payment-batches?run_id=&status=&page=&page_size= | `payroll.view_all` (audit `payroll.payment_batch.list`, `after={count}`) |
| GET | /admin/payroll/payment-batches/{id} | `payroll.view_all` (audit `payroll.payment_batch.view`) |
| PUT | /admin/payroll/payment-batches/{id} | `payroll.manage` (draft) |
| POST | /admin/payroll/payment-batches/{id}/submit | `payroll.manage` (draft) |
| POST | /admin/payroll/payment-batches/{id}/withdraw | `payroll.manage` (submitted -> draft) |
| POST | /admin/payroll/payment-batches/{id}/approve | `payroll.approve` + step-up (submitted) |
| POST | /admin/payroll/payment-batches/{id}/reject | `payroll.approve` (submitted -> draft) |
| POST | /admin/payroll/payment-batches/{id}/cancel | `payroll.manage` (draft / submitted / approved, before export) |
| POST | /admin/payroll/payment-batches/{id}/file | `payroll.manage` + step-up (approved / exported / closed) |
| POST | /admin/payroll/payment-batches/{id}/confirm | `payroll.manage` + step-up (exported) |

**create** `{ "debit_account_id":"uuid"|null (null = default),"value_date":"2026-10-31","user_ids":["uuid"]|null }` -> 201 PaymentBatch (`draft`). `value_date` today..today + 30 (422 `INVALID_DATE`). <= 1000 lines (422 `TOO_MANY_ROWS`). No line at all -> 422 `NOTHING_TO_PAY` (with `blockers` in `details`).
**PaymentBatch**:
```json
{ "id":"uuid","reference":"PAY-2026-10-01","run":{ "id":"uuid","period":"2026-10" },"status":"draft|submitted|approved|exported|closed|cancelled",
  "debit_account":{ "id":"uuid","label":"HDFC salary account","account":"XXXXXX4321" },"value_date":"2026-10-31","format":"generic_neft_csv",
  "counts":{ "lines":44,"excluded":1,"exported":0,"paid":0,"failed":0 },"total":"1650000.00",
  "lines":[ { "id":"uuid","seq":1,"user":UserSummary,"payslip":{ "id":"uuid","revision":1 },"amount":"35300.00",
              "beneficiary_name":"A B","account":"XXXXXX1234","ifsc":"HDFC0001234","account_source":"snapshot|current",
              "current":{ "account":"XXXXXX9876","ifsc":"ICIC0000456" }|null,
              "flags":["BANK_DETAILS_CHANGED","RECENT_BANK_CHANGE","REVISION_DELTA","NO_PAN","PREVIOUSLY_FAILED"],"status":"pending|excluded|exported|paid|failed",
              "reference":"XXXX7788"|null,"paid_on":"2026-10-31"|null,"version":1 } ],
  "blockers":[ { "user":UserSummary,"payslip_id":"uuid","reason":"NO_BANK_DETAILS|INVALID_ACCOUNT|INVALID_IFSC|NAME|NOT_BANK_TRANSFER|HELD|ZERO_NET|ALREADY_PAID|IN_OTHER_BATCH|RECOVERY|UNDERPAID_CHAIN|FAILED_BY_YOU" } ],
  "file_sha256":"<64 hex>"|null,"exported_at":"ts|null","exported_by":UserSummary|null,
  "created_by":UserSummary,"created_at":"ts","submitted_by":UserSummary|null,"submitted_at":"ts|null","decided_by":UserSummary|null,"decided_at":"ts|null","decision_comment":null,"version":1 }
```
`current` is set only for `BANK_DETAILS_CHANGED` lines. `amount` = live net, or for a revision the live net minus the paid lines of the earlier copies (`REVISION_DELTA`). List items omit `lines` and `blockers`. Accounts are always masked; the unmasked values exist only in the file.
**PUT** `{ "expected_version":1, "value_date"?, "debit_account_id"?, "exclude_line_ids"?:["uuid"], "include_line_ids"?:["uuid"], "account_sources"?:[ { "line_id":"uuid","source":"current" } ] }` -> 200 (lines `excluded` stay in the batch, not in the file; `source` only on `BANK_DETAILS_CHANGED` lines, else 422).
**submit** / **withdraw** `{ "expected_version" }` -> 200 (submit freezes lines and stores the lines MAC; a submit with no `pending` line -> 422 `NOTHING_TO_PAY`).
**approve** `{ "expected_version":2,"comment":null,"current_password":"..." }` -> 200 PaymentBatch (`approved`). Order: step-up; a line pays the caller (403 `FORBIDDEN`, whole batch); caller is a party, changed the bank details of a line employee within `payroll.bank_change_alert_days` (any change in the append-only history counts, not only the latest), or marked an earlier payment of a line's payslip (or of an earlier copy) failed (403 `SECOND_PERSON_REQUIRED`); the value date has not passed (422 `INVALID_DATE`) and the debit account is still active (422 `VALIDATION_ERROR`, field `debit_account_id`);
`submitted` (409 `NOT_SUBMITTED`); `expected_version` (409 `VERSION_CONFLICT`); every pending line re-derived from the live payslip and the current details equals the frozen line and MAC (409 `BATCH_STALE`, `details[]` = `{field:"lines[i]", message: PAYSLIP_SUPERSEDED|HELD|AMOUNT_CHANGED|BANK_DETAILS_CHANGED}`).
**reject** `{ "expected_version","comment":"required" }` -> 200 (`draft`). **cancel** `{ "reason" }` -> 200 (lines `excluded`, slot freed).
**file** `{ "current_password" }` -> `text/csv; charset=us-ascii`, `Content-Disposition: attachment; filename="bank-PAY-2026-10-01.csv"`, `Cache-Control: no-store`, `nosniff`. First call (batch `approved`; the value date and the debit account are re-checked like at approve): batch `exported`, lines `exported`, `file_sha256` stored. While the batch is `exported` later calls return the identical bytes (audit `payroll.payment_batch.redownload`, `admin_alert` "Bank file downloaded again"); a `closed` batch is 409 `BATCH_CLOSED`. After the first download an `admin_alert` push to `role.assign` holders. Not allowed for `draft` / `submitted` / `cancelled` (409 `NOT_APPROVED`).
**File format `generic_neft_csv`** (CRLF, comma, header row, no quotes needed because every field is bank-safe):
`Sr No,Payment Type,Debit Account No,Beneficiary Name,Beneficiary Account No,IFSC,Amount,Value Date,Narration,Reference`
- `Sr No` = line seq; `Payment Type` = `payroll.bank_payment_type` (`NEFT`); `Beneficiary Name` = upper-cased, characters outside `A-Z 0-9 . space` removed, spaces collapsed, max 35 (empty -> blocker `NAME`); `Amount` = `35300.00`; `Value Date` = `DD/MM/YYYY`;
  `Narration` = `SALARY OCT 2026` (`SALARY REV OCT 2026` for a delta); `Reference` = `PAY-2026-10-01-0001`. Excluded lines are not written. The user must verify the layout with the bank (plan 17 Q-L11).
**confirm** `{ "paid_on":"2026-10-31","lines":[ { "line_id":"uuid","status":"paid|failed","reference":"UTR1234" } ] | null,"all_paid":true|false,"reference":null|"BATCH-REF","current_password" }` -> 200 PaymentBatch.
`all_paid: true` marks every `exported` line paid (shared optional reference). `reference` 1-40 `[A-Za-z0-9 ._/-]`, encrypted, shown masked. `paid_on` not in the future, not before `exported_at` date. The batch becomes `closed` when no line is `exported`. Failed lines free their payslip for a new batch. **Self rule per line**: a line that pays the caller cannot be confirmed by the caller, as paid or failed, and `all_paid` includes it (403 `FORBIDDEN`, audited `payroll.denied`, nothing changes). The confirmer of a `failed` line is recorded (`failed_by`, never exposed) and can never create, edit, submit or approve a batch that pays that payslip again (create lists the employee as blocker `FAILED_BY_YOU`; edit / submit / approve answer 403 `SECOND_PERSON_REQUIRED`; the database repeats it).
Errors (all): 409 `PAYROLL_LOCKED` (run not published), `NOT_DRAFT`, `NOT_SUBMITTED`, `NOT_APPROVED`, `BATCH_CLOSED`, `VERSION_CONFLICT`, `BATCH_STALE`, `IN_USE`, `CONFLICT` (a payslip is already in a live line: partial unique); 422 `NOTHING_TO_PAY`, `TOO_MANY_ROWS`, `INVALID_DATE`, `VALIDATION_ERROR`; 403 `FORBIDDEN`, `SECOND_PERSON_REQUIRED`, `REAUTH_FAILED`; 429; 503.
Audit `payroll.payment_batch.create|update|submit|withdraw|approve|reject|cancel|export|redownload|confirm` (`after={run_id, period, reference, lines, excluded, status, flags_count, maker_id, checker_id, file_sha256 (export only), paid, failed}`; never amounts, accounts, IFSC, names or references).

### 19.6 Tax year rules [5b]
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/tax/years | `payroll.view_all` |
| GET | /admin/payroll/tax/years/{fy}/rules | `payroll.view_all` (all versions, newest first) |
| POST | /admin/payroll/tax/years/{fy}/rules | `payroll.manage` (new draft = copy of the latest version) |
| GET | /admin/payroll/tax/rules/{id} | `payroll.view_all` |
| PUT | /admin/payroll/tax/rules/{id} | `payroll.manage` (draft) |
| POST | /admin/payroll/tax/rules/{id}/submit | `payroll.manage` (draft) |
| POST | /admin/payroll/tax/rules/{id}/withdraw | `payroll.manage` (submitted -> draft) |
| POST | /admin/payroll/tax/rules/{id}/verify | `payroll.approve` + step-up (submitted) |
| POST | /admin/payroll/tax/rules/{id}/reject | `payroll.approve` (submitted -> draft) |
| PUT | /admin/payroll/tax/years/{fy}/windows | `payroll.manage` (+ `current_password` when a passed window is reopened or its closing date moved later) |

**years** -> `{ "items":[ { "fy":"2026-27","verified_version":null|2,"open_version":{ "id","version":1,"status":"draft" }|null,"windows":Windows,"current":true } ] }` (FYs with a rules row, plus the current FY).
**Windows** `{ "declaration_opens_on":"2026-04-01","declaration_closes_on":"2026-06-30","proof_opens_on":"2027-01-01","proof_closes_on":"2027-02-28" }` (inside the FY, opens <= closes; 422 `INVALID_DATE`). Reopening a window whose closing date has passed, or moving that date later, needs `current_password` in the body (422 when missing, 403 `REAUTH_FAILED`) and alerts the admins (`admin_alert`, no dates); window changes are audited `payroll.tax_rules.windows` (dates, `reopened`) and send `tax_window` pushes when a window opens.
**TaxRules** (object; plain JSON, not secret):
```json
{ "id":"uuid","fy":"2026-27","version":1,"status":"draft|submitted|verified|superseded","rules_sha256":"<64 hex>","note":"UNVERIFIED seed copied from FY 2025-26 ...",
  "rules":{
    "labels":{ "act":"Income-tax Act, 2025","tds_section":"Section 192","form16":"Form 16","return":"Form 24Q" },
    "default_regime":"new",
    "regimes":{
      "new":{ "slabs":[ { "from":"0.00","to":"400000.00","rate_pct":"0" },{ "from":"400000.00","to":"800000.00","rate_pct":"5" },{ "from":"800000.00","to":"1200000.00","rate_pct":"10" },
                        { "from":"1200000.00","to":"1600000.00","rate_pct":"15" },{ "from":"1600000.00","to":"2000000.00","rate_pct":"20" },{ "from":"2000000.00","to":"2400000.00","rate_pct":"25" },
                        { "from":"2400000.00","to":null,"rate_pct":"30" } ],
              "standard_deduction":"75000.00","rebate":{ "income_limit":"1200000.00","max":"60000.00","marginal_relief":true },
              "surcharge":[ { "above":"5000000.00","rate_pct":"10" },{ "above":"10000000.00","rate_pct":"15" },{ "above":"20000000.00","rate_pct":"25" } ],"surcharge_marginal_relief":true,
              "pt_deductible":false,"hra_exemption":false,"allowed_sections":[] },
      "old":{ "slabs":[ { "from":"0.00","to":"250000.00","rate_pct":"0" },{ "from":"250000.00","to":"500000.00","rate_pct":"5" },{ "from":"500000.00","to":"1000000.00","rate_pct":"20" },{ "from":"1000000.00","to":null,"rate_pct":"30" } ],
              "standard_deduction":"50000.00","rebate":{ "income_limit":"500000.00","max":"12500.00","marginal_relief":false },
              "surcharge":[ { "above":"5000000.00","rate_pct":"10" },{ "above":"10000000.00","rate_pct":"15" },{ "above":"20000000.00","rate_pct":"25" },{ "above":"50000000.00","rate_pct":"37" } ],"surcharge_marginal_relief":true,
              "pt_deductible":true,"hra_exemption":true,"allowed_sections":["80C","80CCC","80CCD1","80CCD1B","80D_SELF","80D_PARENTS","80E","80G","80TTA","24B","HRA"] } },
    "cess_pct":"4","no_pan_min_rate_pct":"20","round_income_to":10,"round_tax_to":10,"loan_perquisite_threshold":"20000.00",
    "sections":[ { "code":"80C","name":"Section 80C (PF, PPF, ELSS, life insurance, tuition fees, home loan principal)","limit":null,"group":"80C_GROUP","proof_required":true,"fields":["description"] },
                 { "code":"80CCD1B","name":"NPS (additional)","limit":"50000.00","group":null,"proof_required":true,"fields":["description"] },
                 { "code":"80D_SELF","name":"Health insurance - self and family","limit":"25000.00","senior_limit":"50000.00","group":null,"proof_required":true,"fields":["description","senior"] },
                 { "code":"24B","name":"Home loan interest (self-occupied)","limit":"200000.00","group":null,"proof_required":true,"fields":["lender_name","lender_pan"] },
                 { "code":"HRA","name":"House rent","limit":null,"group":null,"proof_required":true,"fields":["monthly_rent","from_month","to_month","landlord_name","landlord_pan","city","metro"],"landlord_pan_above":"100000.00" },
                 { "code":"PREV_EMPLOYER","name":"Previous employer this year (Form 12B)","limit":null,"group":null,"proof_required":true,"fields":["income","tds","pt","pf"] },
                 { "code":"OTHER_INCOME","name":"Other income","limit":null,"group":null,"proof_required":false,"fields":["description"] } ],
    "group_limits":[ { "group":"80C_GROUP","limit":"150000.00" } ],
    "hra":{ "metro_pct":"50","non_metro_pct":"40","rent_minus_basic_pct":"10","metro_cities":["Delhi","Mumbai","Kolkata","Chennai"] },
    "component_treatment":{ "default":"taxable","REIMBURSEMENT":"exempt","LEAVE_ENCASH":"taxable" } },
  "attestation":null|"Verified against ... on ...","created_by":UserSummary,"last_edited_by":UserSummary,"submitted_by":UserSummary|null,"submitted_at":"ts|null",
  "verified_by":UserSummary|null,"verified_at":"ts|null","version_row":3 }
```
(The values above are the UNVERIFIED seed of plan 17 Q-L1, shown only as the shape; the code contains no tax constant.) Validation: slabs contiguous from `0.00`, ascending, exactly one open-ended last slab; `rate_pct` 0..50 (2 decimals); money within caps;
`sections` <= 40, codes `^[A-Z0-9_]{2,16}$`, unique; `allowed_sections` / `group` reference existing codes / groups; `component_treatment` keys are payslip codes, values `taxable|exempt`. Any error -> 422 with `details.field` as a JSON path (`rules.regimes.new.slabs[2].to`).
**POST** `{}` -> 201 (409 `DRAFT_EXISTS` when a draft / submitted version exists). **PUT** `{ "version_row":3,"rules":{...},"note":"..." }` -> 200 (whole `rules` replaced). **submit** / **withdraw** `{ "version_row" }` -> 200.
**verify** `{ "version_row":4,"rules_sha256":"<64 hex of the reviewed rules>","attestation":"I verified these rules against <source> on <date>" (20-500),"current_password" }` -> 200: the previous verified version becomes `superseded` in the same transaction; runs adopt it at their next refresh. 409 `RULES_CHANGED` when the sha differs.
**reject** `{ "version_row","comment":"required" }` -> 200.
Errors: 409 `DRAFT_EXISTS`, `NOT_DRAFT`, `NOT_SUBMITTED`, `VERSION_CONFLICT`, `RULES_CHANGED`; 422 `VALIDATION_ERROR`, `INVALID_DATE`; 403 `SECOND_PERSON_REQUIRED`, `REAUTH_FAILED`.
Audit `payroll.tax_rules.create|update|submit|withdraw|verify|reject|windows` (`after={fy, version, status, rules_sha256, maker_id, checker_id}`; rules are not secret but values are not copied into audit rows).

### 19.7 Investment declarations and proofs [5b]
Employee (`payroll.view_own`; the FY must have a verified rules version, else 409 `TAX_RULES_NOT_VERIFIED`):
| Method | Path | Notes |
|---|---|---|
| GET | /me/tax/years | `{ "items":[ { "fy","windows":Windows,"verified":true,"current":true } ] }` |
| GET | /me/tax/declaration?fy= | default current FY; creates nothing |
| PUT | /me/tax/declaration | draft save |
| POST | /me/tax/declaration/submit | freezes the draft as the submitted version |
| POST | /me/tax/proofs | needs a submitted item |
| GET | /me/tax/proofs?fy= | own proofs |
| POST | /me/tax/proofs/{id}/cancel | `submitted` only |

**Declaration** (employee and admin; admin adds `user`, `entered_by`, `unlocked_until`; the employee's GET / PUT / submit answers also carry `sections` and `proof_max_files`, see the end of this block):
```json
{ "id":"uuid"|null,"fy":"2026-27","rules_version":2,"windows":Windows,"can_edit":true,"edit_reason":"DECLARATION_WINDOW|PROOF_WINDOW|UNLOCKED|CLOSED",
  "draft":{ "regime":"old|new"|null,
            "items":[ { "id":"i-1","section":"80C","amount":"150000.00","details":{ "description":"PPF" },"flags":[] },
                      { "id":"i-2","section":"HRA","amount":"240000.00","details":{ "monthly_rent":"20000.00","from_month":"2026-04","to_month":"2027-03","landlord_name":"R K","landlord_pan":"XXXXX1234X","city":"Chennai","metro":true },"flags":[] } ] },
  "submitted":{ "regime":"old","items":[ ... ],"version":3,"submitted_at":"ts" }|null,
  "dirty":true,
  "proofs":{ "submitted":1,"verified":2,"rejected":0 },
  "version":5 }
```
Employee answers add `"sections":[ { "code","name","limit":"150000.00"|null,"senior_limit":null,"proof_required":true,"fields":["description"],"landlord_pan_above":null } ]` (the catalogue of the VERIFIED rules; build the form from it) and `"proof_max_files":5` (setting `payroll.tax_proof_max_files`).
`dirty` = the draft differs from the submitted version. PAN fields are masked in GET (the employee sees their own landlord / lender PAN masked too; admin reveal see below). `amount` = annual amount (HRA: computed annual rent, read-only from `monthly_rent` x months).
**PUT** `{ "fy":"2026-27","expected_version":5,"regime":"old|new"|null,"items":[ { "id":"i-1"|null,"section":"80C","amount":"150000.00","details":{...} } ] }` (<= 40 items; `id` null = new, server assigns) -> 200.
Rules: section must exist in the verified rules (422 `INVALID_SECTION`); `details` keys only those of the section's `fields` (422); PAN per 13.4 format AND a real holder type (4th letter one of `ABCFGHJLPT`); the masked form `XXXXX1234X` is 422 for a new value, but an existing item id that sends back the mask of its own stored PAN keeps the stored PAN; the `section` of an existing item id cannot change (422, use a new item); an item with a live (`submitted` / `verified`) proof cannot be edited or removed (409 `CONFLICT`, cancel a submitted proof first); `landlord_pan` required when annual rent > `landlord_pan_above` (422 `LANDLORD_PAN_REQUIRED`); months inside the FY; sections not allowed by the regime are kept with flag `NOT_IN_REGIME`;
regime change after `declaration_closes_on` (and no unlock) -> 409 `REGIME_LOCKED`; outside every window -> 409 `WINDOW_CLOSED` (proof window allows edits of items of sections with `proof_required` only, not the regime).
**submit** `{ "fy","expected_version" }` -> 200 (copies draft to `submitted`, `submitted.version` + 1; identical -> 422 `NO_CHANGE`).
**Proof** `{ "id","fy","item_id":"i-1","section":"80C","claimed":"150000.00","files":[ { "id":"uuid","url":"/api/files/<id>","content_type":"application/pdf" } ],"status":"submitted|verified|rejected|cancelled","verified":"120000.00"|null,"comment":null,"verified_at":"ts|null","version":1 }`.
**POST /me/tax/proofs** `{ "fy","item_id","claimed":"150000.00","file_ids":["uuid"] (1..`proof_max_files` (default 5), own unused `tax_proof` uploads) }` -> 201. `item_id` must be in the SUBMITTED version (422 `ITEM_NOT_SUBMITTED`); `claimed` <= the submitted item amount (422 `AMOUNT_ABOVE_LIMIT`); proof window or unlock (409 `WINDOW_CLOSED`); <= 10 live proofs per item.

Admin:
| Method | Path | Perm |
|---|---|---|
| GET | /admin/payroll/tax/declarations?fy=&status=none\|draft\|submitted&regime=&q=&page=&page_size= | `payroll.view_all` (audit `payroll.tax_declaration.list`, `after={count}`) |
| GET | /admin/payroll/tax/declarations/{user_id}?fy= | `payroll.view_all` (audit `payroll.tax_declaration.view`, `after.revealed=false`; third-party PANs masked; `?reveal=true` is 422) |
| POST | /admin/payroll/tax/declarations/{user_id}/reveal | `payroll.manage` + step-up. Body `{ "fy"?, "current_password" }` -> the declaration with unmasked third-party PANs (`no-store`; audit `payroll.tax_declaration.view`, `after.revealed=true`; `payroll:reveal` limits of 13.0) |
| PUT | /admin/payroll/tax/declarations/{user_id} | `payroll.manage` + step-up (on behalf; not self) |
| POST | /admin/payroll/tax/declarations/{user_id}/submit | `payroll.manage` + step-up (on behalf) |
| POST | /admin/payroll/tax/declarations/{user_id}/unlock | `payroll.manage` + step-up (not self) |
| GET | /admin/payroll/tax/proofs?fy=&status=&user_id=&page=&page_size= | `payroll.view_all` (audit `payroll.tax_proof.list`, `after={count}`) |
| POST | /admin/payroll/tax/proofs/{id}/verify | `payroll.manage` (submitted) |
| POST | /admin/payroll/tax/proofs/{id}/reject | `payroll.manage` (submitted) |
| POST | /admin/payroll/tax/reports/declarations?fy= | `payroll.view_all` (+ step-up when `include_identifiers`) |

List item `{ "user":UserSummary,"fy","status":"none|draft|submitted","regime":"old|new"|null,"submitted_at","dirty":false,"items":7,"proofs":{ "submitted":1,"verified":2,"rejected":0 },"flags":["NO_PAN"] }` (no money).
On-behalf PUT / submit: body as the employee's plus `current_password`; sets `entered_by`; push `tax_declaration_changed` to the employee. **unlock** `{ "fy","until":"2026-07-15","reason","current_password","allow_regime_change":false }` -> 200 (until <= FY end, >= today). An unlock keeps the chosen regime locked (409 `REGIME_LOCKED`) unless `allow_regime_change` is true, which only `payroll.approve` holders may set (403 `FORBIDDEN`). The employee and the approvers are notified.
**verify** `{ "expected_version":1,"verified":"120000.00","comment":null }` -> 200 Proof; `verified` 0.00..claimed (0.00 = nothing accepted, comment required); verifier not the employee (403 `FORBIDDEN`) and not the declaration's `entered_by` (403 `SECOND_PERSON_REQUIRED`); the item must still exist in the submitted version with the proof's section and an amount >= `claimed` (409 `CONFLICT`). Whoever decided on a proof of a declaration cannot then PUT / submit it on behalf (403 `SECOND_PERSON_REQUIRED`).
**reject** `{ "expected_version","comment":"required" }` -> 200. Push `tax_proof_decision`.
**declarations xlsx** body `{ "include_identifiers":false,"current_password"? }`: sheet `Declarations` (employee, regime, section, declared, proof verified; identifiers masked unless `include_identifiers`), `no-store`. Audit `payroll.report.export` (`kind: tax_declarations`, rows, include_identifiers).
Errors (all): 409 `TAX_RULES_NOT_VERIFIED`, `WINDOW_CLOSED`, `REGIME_LOCKED`, `VERSION_CONFLICT`, `NOT_PENDING`; 422 `INVALID_SECTION`, `LANDLORD_PAN_REQUIRED`, `ITEM_NOT_SUBMITTED`, `AMOUNT_ABOVE_LIMIT`, `NO_CHANGE`, `VALIDATION_ERROR`; 403 `FORBIDDEN`, `SECOND_PERSON_REQUIRED`, `REAUTH_FAILED`; 404.
Audit `payroll.tax_declaration.save|submit|unlock|view|list|view_own`, `payroll.tax_proof.create|cancel|verify|reject` (`after={fy, user_id, version, items, regime, on_behalf, status}`; never amounts, details or PANs).
Files: `GET /api/files/{id}` for a `tax_proof` file = owner, `payroll.view_all` holders once a proof references it (cross-user read audited `file.view_other`); anything else 404.

### 19.8 Form 16 documents [5b]
| Method | Path | Perm |
|---|---|---|
| POST | /admin/payroll/tax/form16/upload?fy=&kind=form16\|part_a\|part_b\|form12ba&dry_run=true\|false | `payroll.manage` (+ step-up when `dry_run=false`) |
| GET | /admin/payroll/tax/form16?fy=&kind=&status=&q=&page=&page_size= | `payroll.view_all` (audit `payroll.form16.list`, `after={count}`) |
| GET | /admin/payroll/tax/form16/{id}/file | `payroll.view_all` (audit `payroll.form16.download`) |
| POST | /admin/payroll/tax/form16/publish | `payroll.approve` + step-up |
| POST | /admin/payroll/tax/form16/{id}/withdraw | `payroll.manage` + step-up |
| GET | /me/tax/form16 | `payroll.view_own` |
| GET | /me/tax/form16/{id}/file | `payroll.view_own` (owner, `published`; audit `payroll.form16.view_own`) |

**upload** multipart: `files` (1..20 PDFs, each <= 2 MB, total <= 40 MB; PDF checks of `POST /files`), for `dry_run=false` also `manifest_sha256` (from the dry run) and `current_password`. Matching uses the file NAME only: when the name holds a PAN-shaped token ONLY PANs decide (an unknown PAN is `UNKNOWN_PAN`; Employee ID tokens are ignored); otherwise one `Employee ID` token separated by `_`, `-`, space or `.` (IDs shorter than 3 characters or shaped like a year / FY never match). A file that names the caller is `FORBIDDEN`.
Dry run -> 200 (stores nothing but the HMAC binding `actor | fy | kind | sorted file sha256 list`, Redis 15 min, fail closed):
```json
{ "fy":"2026-27","kind":"form16","manifest_sha256":"<64 hex>","files":4,
  "matched":[ { "index":0,"user":UserSummary,"sha256":"<64 hex>","replaces_id":"uuid"|null } ],
  "errors":[ { "index":2,"message":"UNMATCHED|AMBIGUOUS|DUPLICATE_EMPLOYEE|NOT_PDF|FILE_TOO_LARGE|DELETED_EMPLOYEE" } ] }
```
`index` = position in the multipart list (file names are never echoed, logged or stored). Commit -> 201 `{ "upload_batch_id":"uuid","created":3,"replaced":1 }` only without errors (else 422 `IMPORT_INVALID`); 409 `IMPORT_NOT_CHECKED`, `IMPORT_FILE_CHANGED`.
Commit stores each PDF as a `form16` file (encrypted) and a document `uploaded`; a replaced live document becomes `withdrawn`. nginx: exact location for this path with a 42m body.
**Form16Document** `{ "id","user":UserSummary,"fy","kind","status":"uploaded|published|withdrawn","sha256","uploaded_by":UserSummary,"uploaded_at","published_by":UserSummary|null,"published_at":"ts|null","withdrawn_at":"ts|null" }`.
**publish** `{ "fy":"2026-27","documents":[ { "id":"uuid","sha256":"<64 hex>" } ] (1..500),"current_password" }` -> 200 `{ "published":3,"failed":[ { "id","reason":"NOT_UPLOADED|SHA_MISMATCH|SECOND_PERSON_REQUIRED|FORBIDDEN" } ] }` (one transaction; failed rows stay `uploaded`;
`SECOND_PERSON_REQUIRED` when the caller uploaded it and `payroll.form16_requires_second_person` is on; `FORBIDDEN` for the caller's own document). Push `form16_available` per employee (coalesced).
**withdraw** `{ "reason","current_password" }` -> 200 (published or uploaded; the employee no longer sees it).
**/me/tax/form16** -> `{ "items":[ { "id","fy","kind","published_at" } ] }` (published only, newest FY first). **file** -> `application/pdf`, `filename="form16-EMP001-2026-27.pdf"` (`part-a`, `part-b`, `form12ba`), `no-store`, `nosniff`, CSP as 13.8; anything else 404 (audited `payroll.denied`).
Audit `payroll.form16.dry_run|commit|publish|withdraw|download|view_own|list` (`after={fy, kind, files, created, replaced, published, failed}`; never file names or PANs).

### 19.9 TDS engine [5c]
| Method | Path | Perm |
|---|---|---|
| POST | /admin/payroll/tax/preview | `payroll.view_all` (pure; audit `payroll.tax.preview`, `after={user_id, fy}`) |
| GET | /admin/payroll/tax/summary?fy=&q=&department_id=&flag=&page=&page_size= | `payroll.view_all` (audit `payroll.tax.summary`, `after={count}`) |
| POST | /admin/payroll/tax/employees/{user_id}/statement?fy= | `payroll.view_all` + step-up, body `{ "current_password" }` (PDF; audit `payroll.tax.statement`) |
| POST | /admin/payroll/tax/24q?fy=&quarter=1..4 | `payroll.view_all` + step-up (xlsx; audit `payroll.statutory.export` kind `24q`) |
| GET | /me/tax/projection?fy= | `payroll.view_own` (audit `payroll.tax.view_own`) |

All answer 409 `TDS_ENGINE_OFF` while `payroll.tds_engine_enabled` is false (permissions are checked first), and 409 `TAX_RULES_NOT_VERIFIED` when the FY has no verified rules.
**TdsComputation** (AdminPayslip `tds_computation`, preview and summary detail; employee projection = the same without `steps` details of other income sources):
```json
{ "fy":"2026-27","mode":"suggest|apply","rules":{ "id":"uuid","version":2 },"regime":"new","regime_source":"declared|default",
  "declaration_version":3|null,"proof_basis":"declared|verified","remaining_months":6,
  "annual":{ "salary_paid":"240000.00","salary_this_month":"40000.00","salary_projected":"200000.00","previous_employer_income":"0.00","other_income":"0.00",
             "exemptions":"0.00","standard_deduction":"75000.00","pt":"0.00","chapter_via":"0.00","taxable_income":"405000.00",
             "tax_on_income":"250.00","rebate":"250.00","surcharge":"0.00","cess":"0.00","total_tax":"0.00" },
  "tds_paid_so_far":"0.00","previous_employer_tds":"0.00","remaining_tax":"0.00","one_time_tax":"0.00",
  "suggested":"0.00","applied":"0.00","override":{ "amount":"500.00","reason":"Employee request" }|null,
  "flags":["REGIME_DEFAULT","NO_DECLARATION"],"computed_at":"ts" }
```
**preview** `{ "user_id":"uuid","fy":"2026-27"|null,"period":"2026-11"|null,"regime":"old|new"|null }` -> 200 `{ "computation":TdsComputation,"other_regime":{ "regime":"old","total_tax":"12500.00" } }` (writes nothing; `regime` overrides the declared one for comparison).
**summary** -> paginated `{ "user":UserSummary,"regime","total_tax","tds_paid_so_far","remaining_tax","suggested_this_month","manual_this_month","flags" }` items + `"totals":{ "total_tax","tds_paid_so_far","remaining_tax" }` (whole filtered set).
**/me/tax/projection** -> `{ "estimate":true,"computation":TdsComputation,"other_regime":{ "regime","total_tax" } }` (own only; based on the SUBMITTED declaration; the app labels it an estimate).
**statement** PDF: company, employee (PAN masked), FY, regime, the `annual` figures, month-wise TDS deducted, declarations used, watermark `TAX COMPUTATION - NOT FORM 16`, `no-store`, `nosniff`, CSP.
**24Q xlsx** sheets: `Deductees` (per month and employee: employee id, name, PAN, section label from rules, amount paid, TDS, date paid (pay date), date deducted, challan BSR / serial / date), `Salary Details` (quarter 4 only: per employee annual figures and regime), `Challans`, `Blockers` (`NO_PAN`, `CHALLAN_MISSING`, `RUN_NOT_PUBLISHED`). Published payslips only. Working data for the return utility, not a filing file.
**Payslip behaviour**: the engine runs at run create / refresh / process / revise and payslip process (never at publish), stores one computation per payslip (frozen once processed), and in `apply` mode sets the TDS line to `applied` (= `suggested` unless `override`). `suggest` mode: TDS line = manual value, `suggested` shown beside it.
Engine enabled but rules not verified for the payslip's FY: warning `TAX_RULES_NOT_VERIFIED`, manual TDS used.
Errors: 409 `TDS_ENGINE_OFF`, `TAX_RULES_NOT_VERIFIED`, `RUN_NOT_PUBLISHED` (24Q of a quarter with an unpublished month: listed as blocker instead when at least one month is published); 422 `INVALID_PERIOD`, `VALIDATION_ERROR`; 403 `REAUTH_FAILED`.

### 19.10 New settings (`/admin/payroll/settings`, snapshotted into each run like the other `payroll.*` keys)
| Key | Default | Range |
|---|---|---|
| `payroll.publish_request_ttl_hours` [5a] | 72 | 1..336 |
| `payroll.bank_file_format` [5a] | `"generic_neft_csv"` | `generic_neft_csv` |
| `payroll.bank_payment_type` [5a] | `"NEFT"` | `NEFT` \| `RTGS` \| `IMPS` |
| `payroll.bank_change_alert_days` [5a] | 30 | 7..90 (change needs `role.assign` + step-up + `admin_alert`; the services and the database also floor it at 7) |
| `payroll.form16_requires_second_person` [5b] | true | bool (change needs `role.assign` + step-up) |
| `payroll.tax_proof_max_files` [5b] | 5 | 1..10 |
| `payroll.tds_engine_enabled` [5c] | false | bool (change needs `role.assign` + step-up; `admin_alert`) |
| `payroll.tds_engine_mode` [5c] | `"suggest"` | `suggest` \| `apply` |
| `payroll.tds_one_time_in_month` [5c] | true | bool |
| `payroll.tds_proofs_from_month` [5c] | 1 | 1..12 (calendar month from which only verified proofs count; 1 = January) |
| `payroll.tds_include_other_income` [5c] | false | bool |
Declaration / proof windows are per FY (19.6 `windows`), not settings. Tax slabs, rates and limits are never settings: they live only in verified `tax_year_rules`.

### 19.11 Audit redaction allow-list (new non-money keys)
`scope`, `run_id`, `request_id`, `payslips`, `stale_reason`, `self_included`, `reference` (batch reference `PAY-...` only), `lines`, `excluded`, `paid`, `failed`, `flags_count`, `file_sha256`, `fy`, `version`, `rules_sha256`, `regime`, `items`, `on_behalf`, `kind`, `files`, `created`, `replaced`, `published`, `quarter`, `include_identifiers`, `maker_id`, `checker_id`.
Never allow-listed: any amount, `account`, `ifsc`, `pan`, `landlord_pan`, `lender_pan`, bank `reference` of a line, file names, comments, reasons, attestation text.

### 19.12 Push (section 11; data messages, never amounts, accounts or PANs)
- `payroll_approval` kinds: `publish` (to `payroll.publish` holders outside the maker set, max 20, coalesced 10 min: "<maker name> asked to publish payroll for October 2026"; to the requester on approve / reject / stale), `payment_batch` (to `payroll.approve` holders: "<maker name> submitted payment batch PAY-2026-10-01"; to the maker on decision), `tax_rules` ("Tax rules FY 2026-27 v2 submitted for verification"), `form16` ("<n> Form 16 documents ready to publish").
- `payslip_reminder` to the employee, id = payslip id, "Payslip available", body "Your payslip for October 2026 is waiting in the app."
- `form16_available` to the employee, id = document id, "Form 16 available", body "Your Form 16 for FY 2026-27 is ready to download."
- `tax_proof_decision` to the employee, id = proof id, "Investment proof verified" / "Investment proof rejected", body = section name.
- `tax_declaration_changed` to the employee, id = declaration id, "Tax declaration updated", body "HR updated your FY 2026-27 declaration."
- `tax_window` to every active employee with `payroll.view_own` and `include_in_payroll`, "Investment declaration open" / "Proof submission closes in 3 days", body = FY and date.
- `admin_alert` to `role.assign` holders: bank file downloaded or downloaded again (batch reference, line count), debit account created / reactivated / default changed (fixed text, never the label), protected setting changed.
Best effort, after commit.

### 19.13 Error codes (new)
409: `PUBLISH_REQUEST_REQUIRED`, `PUBLISH_REQUEST_EXISTS`, `PUBLISH_STALE`, `MAKER_CHECKER_OFF`, `BATCH_STALE`, `NOT_APPROVED`, `BATCH_CLOSED`, `IN_USE`, `DRAFT_EXISTS`, `RULES_CHANGED`, `TAX_RULES_NOT_VERIFIED`, `WINDOW_CLOSED`, `REGIME_LOCKED`, `TDS_ENGINE_OFF`.
422: `NOTHING_TO_PAY`, `INVALID_SECTION`, `LANDLORD_PAN_REQUIRED`, `ITEM_NOT_SUBMITTED`.
Reused: `SECOND_PERSON_REQUIRED`, `FORBIDDEN`, `REAUTH_FAILED`, `NOT_SUBMITTED`, `NOT_DRAFT`, `NOT_PENDING`, `VERSION_CONFLICT`, `PAYROLL_LOCKED`, `NOTHING_TO_PUBLISH`, `COMPONENTS_CHANGED`, `FNF_NOT_INCLUDED`, `CONFLICT`, `TOO_MANY_ROWS`, `NO_CHANGE`, `AMOUNT_ABOVE_LIMIT`,
`IMPORT_INVALID`, `IMPORT_NOT_CHECKED`, `IMPORT_FILE_CHANGED`, `INVALID_DATE`, `INVALID_PERIOD`, `RUN_NOT_PUBLISHED`, `VALIDATION_ERROR`, `RATE_LIMITED`, `TEMPORARILY_UNAVAILABLE`.

### 19.14 Implementation notes, work package 5a (publish requests, delivery, bank accounts, payment batches) - IMPLEMENTED
Where the backend refines 19.0 to 19.5 (Flutter should follow these). Errors keep the common shape `{"error":{"code","message","details":[{field,message}],"request_id"}}`.
- **Direct publish gate (19.1)**: order is step-up, 404, `PAYROLL_LOCKED` (not processed), then 409 `PUBLISH_REQUEST_REQUIRED` (run snapshot OR current setting on). The old 403 `SECOND_PERSON_REQUIRED` on direct publish no longer exists. `MAKER_CHECKER_OFF` is only the answer of the two
  `POST .../publish-requests` create endpoints; withdraw / reject / approve of a request that is already open keep working when the setting is switched off.
- **Run / AdminPayslip** carry `publish_request` = the newest request of the run (scope run) or of that payslip (scope payslip), `{id,status,requested_by,requested_at,stale_reason}`, else `null`. `GET /details` gains read-only `bank_changed_at`.
- **Maker set**: requester; `contributors` of the run (scope run only) and of every payslip in the set (plus their `created_by` / `updated_by` / `processed_by`); actors of the money history of every component source in those payslips except the pure lifecycle actions `process`, `reopen`, `pay`,
  `carry` (`propose` of an arrears row counts when a user, not the system, was the actor; for an installment the parent loan's history counts too); for rows owned by an F&F the F&F creator / last editor / submitter / contributors (reason `FNF_MAKER`; its checker is NOT a maker); creators / editors of `manual` / `import` structures used, including the structure that triggered each arrears row in the payslips (`trigger_structure_id`).
  `reject` follows the same rule as `approve` (the database CHECK applies to every decision): the requester and the makers get 403 `SECOND_PERSON_REQUIRED` on both, the employee of a payslip request 403 `FORBIDDEN`. Reasons returned by `publish-check`: `REQUESTED`, `PROCESSED_RUN`, `EDITED_PAYSLIP`,
  `CREATED_COMPONENT`, `CREATED_STRUCTURE`, `FNF_MAKER`, `OWN_PAYSLIP`. `makers[]` of `publish-check` lists everybody (the caller included); `can_approve` is also false when the open request expired. `PublishRequest.makers` is filled for `approved` / `rejected` requests only.
- **Approve order** (as 19.2): step-up; employee / requester / stored makers (403); `NOT_SUBMITTED`; the full maker set computed under locks (403); `VERSION_CONFLICT`; expiry; stored snapshot MAC; run / payslip state; snapshot equality. Every `PUBLISH_STALE` first marks the request `stale` (committed) and then answers 409.
  `stale_reason` on the request: `RUN_REOPENED` (run no longer processed / published), `PAYSLIP_CHANGED` (set, version or MAC differs, or the stored snapshot was altered), `EXPIRED`. A `COMPONENTS_CHANGED` / `FNF_NOT_INCLUDED` answer rolls everything back and the request stays `submitted`.
- **Staleness hooks**: reopen run -> `RUN_REOPENED`; refresh -> `RUN_REFRESHED`; payslip process / reopen of a published run -> `PAYSLIP_CHANGED` for EVERY open request of that run (deliberately wide). Expiry (`payroll.publish_request_ttl_hours`) is applied lazily by `GET publish-requests`, the detail, `publish-check` and create, and hourly by the job
  `payroll.publish_request_expiry`. `withdraw` makes the withdrawer a contributor of the run (processed run) or of the payslip (published run). `DELETE` of a run / payslip that has publish requests is 409 `CONFLICT`.
- **Money history** for requests and batches: `payroll_item_events.user_id` is the employee of a payslip request, else the requester; for a batch it is the batch creator. Details never hold amounts, accounts or comments.
- **Push**: `payroll_approval` with `kind` `publish` / `payment_batch`; request pushes go to `payroll.publish` holders outside the maker set (max 20, coalesced 10 minutes, body "<maker name> asked to publish payroll for June 2025") and to the requester on approve / reject / stale (body = month label). Batch pushes: submit -> `payroll.approve` holders
  who are not parties ("<maker name> submitted payment batch PAY-2025-06-01"), approve / reject -> the submitter (title "Payment batch approved|rejected", body = reference). `admin_alert`: bank file download (body "PAY-2025-06-01: 2 lines"; title "Bank file downloaded again" for a re-download), bank account created ("A company bank account was added"), reactivated or default changed ("The default or active state of a company bank account was changed"; never the label), `bank_change_alert_days` changed ("... changed the bank detail change window.").
- **Delivery (19.3)**: `first_viewed_at` is set by `GET /payslips/{id}` only (the list does not count); `/pdf` sets `first_downloaded_at` and, when still empty, `first_viewed_at`. `pushed_at` / `push_count` are stamped after the publish push when it reached at least one registered device. Reminder skip order:
  `NOT_PUBLISHED`, `VIEWED`, `NO_DEVICE`, `LIMIT_REACHED`, `TOO_SOON`. With `user_ids: null` only not-viewed employees with a device are considered (others are not listed in `skipped`). `skipped` in the audit row is a count. A payslip published before migration 0026 gets its delivery row on first view / remind.
- **Bank accounts (19.4)**: `IN_USE` covers batches in `draft`, `submitted`, `approved`; deactivating clears `is_default`; an inactive account cannot be made default (422); creating with `is_default: true` clears the previous default; `PUT` sends at least one of `label`, `is_default`, `active` (nulls and any account / IFSC / bank name field are 422).
- **PaymentBatch (19.5)**: `lines[].account` / `ifsc` are ALWAYS the snapshot (what the publish reviewed), `current` is the employee's current pair (masked) on `BANK_DETAILS_CHANGED` lines, `account_source` says which one the file uses. `lines[].reference` is masked; `total` = sum of the non-excluded lines; `counts.lines` = non-excluded lines.
  `reference` is `PAY-YYYY-MM-NN` with the run's month and a running number. `blockers` is returned by create and by a `draft` batch's detail (live), empty for other statuses. `NOTHING_TO_PAY` carries `details: [{field: <employee_id>, message: <blocker code>}]` (<= 200). A user in `user_ids` without a published payslip is 422 `VALIDATION_ERROR`.
- **Lines and revisions**: paid AND exported lines of the earlier copies count as paid when the revision delta is computed; a `pending` line of an earlier copy blocks with `IN_OTHER_BATCH`; delta 0 -> `ALREADY_PAID`; negative -> `RECOVERY`. `NO_PAN` is a flag only.
  `PREVIOUSLY_FAILED` is a flag on a line whose payslip, or an earlier copy of it, has a `failed` line (counted in the audit `flags_count`). When an earlier copy's `exported` line is confirmed `failed` AFTER a revision delta was computed from it, the delta no longer covers the net: the payslip is the blocker `UNDERPAID_CHAIN` (batch create / draft detail) and the dashboard shows `PAYMENT_UNDERPAID`; it is settled outside the system (there is no top-up line).
  `PUT` with only `expected_version` REFRESHES the draft: every pending line whose payslip was revised or whose amount / bank details changed is excluded and, when the live payslip can still be paid, replaced by a NEW line with a new `seq` (an `include_line_ids` of a changed line does the same). `submit` and `approve` answer 409 `BATCH_STALE` with
  `details: [{field:"lines[<seq>]", message: PAYSLIP_SUPERSEDED|HELD|AMOUNT_CHANGED|BANK_DETAILS_CHANGED}]` (the line's `seq`). `value_date` is checked at create, edit and submit.
- **Decisions**: `reject` returns the batch to `draft` and keeps `decided_by`; that person (and every party) gets 403 `SECOND_PERSON_REQUIRED` on `PUT` / `submit` / `withdraw`; the next `submit` clears the decision. `withdraw` keeps `submitted_by`. A checker who cancels an approved batch is not added to `contributors`. `cancel` is allowed in `draft` / `submitted` / `approved`
  (else 409 `NOT_DRAFT`). `file` on `draft` / `submitted` / `cancelled`, and `confirm` on anything but `exported`, answer 409 `NOT_APPROVED`. A stored line that no longer reproduces the exported bytes answers 409 `CONFLICT` (audited failure `payroll.payment_batch.download`).
- **Confirm**: `lines` entries override `all_paid`; unlisted `exported` lines become `paid` only when `all_paid` is true; a line not `exported` is 409 `CONFLICT`; `failed` lines keep `paid_on` null and may carry a reference; `paid_on` must be today or earlier and not before the day of the download (office time zone).
- **Second approver**: unconditional. The setting `payroll.bank_file_requires_second_person` was removed before deployment (the database CHECKs of migration 0026 always require a different approver); it is now an unknown key (422).
- **Bank change control (security review)**: `payroll.bank_change_alert_days` is 7..90 and guarded (`role.assign` + step-up + `admin_alert`); the services and the database trigger use `max(setting, 7)`. Every change of the payment mode / bank details is appended to the append-only table `employee_bank_changes` (by the database trigger; migration 0026) and an approver who appears in it within the window is refused, not only the person of the latest change. `RECENT_BANK_CHANGE` uses the same window. A bank change always needs a marker newer than the previous one.
- **Failed payments (security review)**: only the confirm endpoint creates a `failed` line; it applies the self rule per line (see 19.5), records `failed_by` and the confirmer is excluded from the re-batch of that payslip. A second person is NOT required to mark a line failed (not implemented: the exclusions above and the audit trail are the control). `PREVIOUSLY_FAILED` is shown to the checker.
- **Approve / first download**: the value date (today or later) and the active debit account are re-checked at approve and at the first download; the batch then has to be cancelled and remade. The download of a `closed` batch is 409 `BATCH_CLOSED`.
- **Dashboard**: `calendar.alerts` gains `PUBLISH_PENDING_APPROVAL` (any open request), `PAYMENT_NOT_EXPORTED` (published run, pay date passed, a payslip still has something to pay and no exported / paid line), `PAYMENT_FAILED_LINES`, `PAYMENT_UNDERPAID` (an `UNDERPAID_CHAIN` payslip exists).
- **Rate limits** as 19.0: reads `payroll:admin_read`; request create / withdraw, batch create / edit / submit / withdraw / cancel / confirm, bank account create / update `payroll:write`; request approve / reject, batch approve / reject, remind `payroll:action`; file `payroll:bank_file` (10/min + 30/day), all fail closed (503 `TEMPORARILY_UNAVAILABLE`).
- **Audit actions** (never amounts, accounts, IFSC, names, references of lines or comments): `payroll.publish_request.create|withdraw|approve|reject|expire|stale|list|view|check`, `payroll.delivery.view|remind`, `payroll.bank_account.list|create|update`, `payroll.payment_batch.list|view|create|update|submit|withdraw|approve|reject|cancel|export|redownload|confirm`.
- **Migration 0026 downgrade** refuses while ANY publish request, payslip delivery, payment batch or payment line exists.

### 19.15 Implementation notes, work package 5b (tax rules, declarations / proofs, Form 16) - IMPLEMENTED
Where the backend refines 19.6 to 19.8 (Flutter should follow these). Errors keep the common shape `{"error":{"code","message","details":[{field,message}],"request_id"}}`.
- **Rules**: `TaxRules` also carries `windows`. `rules.note` is the editable note (`PUT` `note`: absent keeps `rules.note`, `""` removes it); `submit` answers 422 `VALIDATION_ERROR` (`details.field` `rules.note`) while it still starts with `UNVERIFIED` (the database refuses to verify such rules too), so the seed needs an edited,
  reviewed copy first. The seed lists old-regime `allowed_sections` (80CCC, 80CCD1, 80D_PARENTS, 80E, 80G, 80TTA) that its own `sections` catalogue does not define: validation (422 `rules.regimes.old.allowed_sections[i]`) forces the reviewer to add those sections or drop the codes. Validation is strict (unknown keys 422); declaration `fields` are limited to
  `description, senior, lender_name, lender_pan, monthly_rent, from_month, to_month, landlord_name, landlord_pan, city, metro, income, tds, pt, pf`. `POST .../rules` for a financial year without rows copies the newest version of the latest earlier year with `rules.note = "UNVERIFIED copy of FY ..."`; 404 when there is nothing to copy.
  `reject` follows the same rule as `verify` (403 `SECOND_PERSON_REQUIRED` for creator, editors, submitter and `contributors`; the maker uses `withdraw`). Reject / withdraw return the version to `draft`, clear `submitted_by/at`, and the reject comment is stored encrypted in `payroll_item_events.detail_enc` (`{"comment"}`); `withdraw` is limited to the submitter or a `payroll.approve` holder (403 `FORBIDDEN`) and adds nobody to `contributors` except the submitter.
  `PUT .../windows` changes every version row of the FY (both pairs or neither per window; omitted / null = closed); the `tax_window` push goes out only when a window becomes open today through that call (no scheduler job yet for "closes in 3 days").
- **Declarations**: `GET /me/tax/declaration` for an employee without a row returns `id: null`, `version: 0`; the first `PUT` must send `expected_version: 0`, later ones the returned version; `regime` must be sent (null clears it). `edit_reason` priority: `DECLARATION_WINDOW`, `UNLOCKED`, `PROOF_WINDOW`, `CLOSED`.
  In the proof window the regime is locked (409 `REGIME_LOCKED`) and only items of sections with `proof_required` may be added / changed / removed (409 `WINDOW_CLOSED`); submit applies the same limits against the submitted version. Item flags: `NOT_IN_REGIME` (the section is allowed in the other regime only; sections that no regime lists, such as `PREV_EMPLOYER`, never get it) and `ABOVE_LIMIT`
  (information; the limit, or `senior_limit` when `details.senior` is true, is applied by the engine). `submit` re-validates against the current verified rules and needs a regime (422 `regime`); a draft without any saved row is 422 `NO_CHANGE`. HRA `amount` is always computed from `monthly_rent` x months.
  `unlock` creates the (empty) declaration when none exists. On-behalf `PUT` / `submit` set `entered_by` once; later on-behalf editors are remembered in the money history (`on_behalf`) and are refused as proof deciders as well (403 `SECOND_PERSON_REQUIRED`).
- **Proofs**: the database allows ONE live (`submitted` / `verified`) proof per declaration item (`uq_tax_proofs_live_item`); the "<= 10 live proofs per item" of 19.7 is therefore 409 `CONFLICT` for a second one (cancel or get the first rejected). `file_ids` are the employee's own `tax_proof` uploads not used by another non-cancelled proof, at most `payroll.tax_proof_max_files`.
  Proof creation needs the proof window or an unlock (409 `WINDOW_CLOSED`) and a section with `proof_required`. Verify / reject: employee 403 `FORBIDDEN` (audited `payroll.denied`), then entered-by 403 `SECOND_PERSON_REQUIRED`, then `NOT_PENDING`, `VERSION_CONFLICT`. Admin proof objects carry `user` and `claimed`.
  `GET /api/files/{id}` of a `tax_proof` file: owner, or `payroll.view_all` once a proof lists it (audited `file.view_other`); a `form16` file is never served by `/api/files` (404 for everybody, including the uploader).
- **Form 16**: `dry_run` defaults to `true`. Dry-run `matched[]` entries also carry `"warnings": ["REPLACES"]` when `replaces_id` is set. Matching tokens are delimited by any non-alphanumeric character; a file naming two different employees (two Employee IDs, two PANs, or an ID and a PAN) is `AMBIGUOUS`; every file of an employee that appears twice is `DUPLICATE_EMPLOYEE`.
  `manifest_sha256` = sha256 of `fy|kind|sorted (file sha256:employee id) pairs`, so a commit must reproduce the reviewed file-to-employee mapping (else `IMPORT_FILE_CHANGED`); the Redis binding (15 min, single use, consumed with GETDEL before any work and restored when the commit fails) is an HMAC over `actor | fy | kind | manifest`. A document whose stored file row is missing is 503 `FILE_UNAVAILABLE`. Commit order: step-up, `IMPORT_FILE_CHANGED`, `IMPORT_INVALID` (`details[].field` = `files[i]`, `message` = the error code), `IMPORT_NOT_CHECKED`. Publish `failed[].reason` order: `NOT_UPLOADED` (also another FY / unknown id), `FORBIDDEN`, `SHA_MISMATCH`, `SECOND_PERSON_REQUIRED`.
  `withdraw` of a document of the caller's own: 403 `FORBIDDEN`; an already withdrawn one: 409 `CONFLICT`. `payroll.form16_requires_second_person` is a guarded setting (`role.assign` + password + `admin_alert`); each document keeps the value it was uploaded under.
- **Audit** (never amounts, PANs, file names, comments, reasons, attestation): `payroll.tax_rules.*`, `payroll.tax_declaration.save|submit|unlock|view|list|view_own`, `payroll.tax_proof.create|cancel|verify|reject|list`, `payroll.form16.dry_run|commit|publish|withdraw|download|view_own|list`, `payroll.report.export` (`kind: tax_declarations`). Money history: `item_type` `tax_rules` / `tax_declaration` / `tax_proof` / `form16` (publish is action `approve`; `user_id` of a rules event is the actor).

