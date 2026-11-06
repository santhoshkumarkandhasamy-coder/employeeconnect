# 13 - On Duty: API contract draft

Status: DRAFT for plan `docs/plans/13-on-duty.md`. To be merged into `docs/api-contract.md` as **section 14**, plus the changes to
sections 2 (`/home`), 1 (`/config`), 3, 4, 9, 10, 11 and 12.5 listed in 14.9. Until merged, backend and Flutter build against this file.
Conventions of `api-contract.md` section 0 apply (base `/api`, bearer auth, UUID ids, office-timezone `work_date`, UTC `Z` timestamps,
error envelope `{"error":{"code","message","details":[{field,message}],"request_id"}}`, page envelope `{items,page,page_size,total}`).
Unknown body fields -> 422 `VALIDATION_ERROR` (`extra=forbid`) on every endpoint below.

Scope guard: one-shot location reads at visit check-in and visit check-out only. No background/continuous tracking, no streams, no trips.

## 14.0 Resolved decisions (plan section 10, proposed defaults adopted)
| # | Question | Resolution |
|---|---|---|
| 1 | Approval | `on_duty.approval_required=false` by default. When on: one request may cover 1-31 calendar days (`to_date - from_date <= 30`) and either one location or "any assigned location" (`location_id: null`). |
| 2 | Assignment | `all` or `selected` only. No stored department link; "add department" expands to the department's CURRENT active members at save time (server side, 14.5). |
| 3 | Late | `on_duty.apply_late_rule=false`: a day that starts with a visit is never late. |
| 4 | Worked time | Span first check-in -> last closing punch (travel included). Half-day rule (`attendance.half_day_minutes`) applies; payroll treats `half_day` as today (half LOP). |
| 5 | Check-out outside site | Allowed and flagged (`on_duty.checkout_geofence=false`); flag visible to `attendance.view_all` only. |
| 6 | Accuracy vs radius | No extra rule; only `attendance.max_accuracy_m` applies. |
| 7 | Coordinates | Assigned employees receive `lat`/`lng` (for "Open in Maps"), never `radius_m`. |
| 8 | Night/outstation | Out of scope. Open visits from a past day are auto-closed (flagged) by the 00:30 job. |
| 9 | HR corrections | No visit edit/delete in v1. Auto-closed visits are flagged for HR review only. |
| 10 | Export | One `On Duty` sheet (`type=on_duty`, also in `type=all`) + two Attendance columns. No on-duty requests sheet in v1. |

## 14.1 Permissions, settings
- New permission `duty_location.manage` ("Manage on-duty locations and assignments"), seeded for `admin`, `hr`.
- Employee endpoints need only authentication (Self). Admin visit views reuse `attendance.view_team` / `attendance.view_all`; on-duty request decisions reuse `request.*`.
- Settings (in `GET/PUT /admin/settings`, `settings.manage`, validated like section 9; bools must be JSON booleans, ints integral):

| Key | Type / range | Default |
|---|---|---|
| `on_duty.enabled` | bool | `true` |
| `on_duty.approval_required` | bool | `false` |
| `on_duty.apply_late_rule` | bool | `false` |
| `on_duty.checkout_geofence` | bool | `false` |
| `on_duty.selfie_required` | bool | `false` |
| `on_duty.max_visits_per_day` | int 1..50 | `10` |
| `on_duty.request_backdate_days` | int 0..30 | `0` |
| `on_duty.request_max_advance_days` | int 1..365 | `60` |

Accuracy (`attendance.max_accuracy_m`), fix age (`attendance.max_fix_age_s`, `max_fix_future_s`), mock rejection (`attendance.reject_mock_location`),
selfie age (`attendance.selfie_max_age_s`), `attendance.half_day_minutes` and retention keys are shared with office punches.

## 14.2 Objects
```json
DutyLocation (employee view; never radius):
{ "id":"uuid", "name":"Acme Client - Guindy", "address":"12 Mount Rd, Chennai"|null, "lat":13.0067, "lng":80.2206 }

DutyLocationAdmin:
{ "id":"uuid", "name":"..", "address":".."|null, "lat":13.0067, "lng":80.2206, "radius_m":200,
  "assign_mode":"all|selected", "is_active":true,
  "assigned_count":14,            // all: active non-deleted employees; selected: active non-deleted assigned users
  "in_use":false,                 // any visit or on-duty request references it (delete would be 409 IN_USE)
  "created_at":"ts", "updated_at":"ts", "created_by":UserSummary|null, "updated_by":UserSummary|null }

Visit (employee view):
{ "id":"uuid", "work_date":"2026-10-26",
  "location": { "id":"uuid", "name":"Acme Client - Guindy" },   // name = snapshot at check-in
  "check_in_at":"2026-10-26T05:12:00Z", "check_out_at":"ts"|null,
  "visit_minutes":95|null,        // floor((check_out_at - check_in_at)/60); null while open or auto-closed
  "open":true,                    // check_out_at null AND not auto_closed
  "auto_closed":false,
  "note":"Quarterly review"|null,
  "check_in_selfie_url":"/api/files/<id>"|null, "check_out_selfie_url":"/api/files/<id>"|null,
  "request_id":"uuid"|null }

AdminVisit = Visit + 
{ "user":UserSummary,
  "check_in_distance_m":12|null, "check_out_distance_m":340|null,
  "check_out_within_fence":false|null, "check_in_accuracy_m":8.5|null, "check_out_accuracy_m":11.0|null,
  "suspicious":false }
  // the six fields above (except user) are null for callers without attendance.view_all (managers)

OnDutyRequest:
{ "id":"uuid", "from_date":"2026-10-28", "to_date":"2026-10-30", "days":3,   // calendar days, inclusive
  "location": { "id":"uuid", "name":".." }|null,                            // null = any assigned location
  "purpose":"Installation at client site",                                  // "" after retention purge
  "status":"pending|approved|rejected|cancelled",
  "approver":UserSummary|null, "decision_comment":".."|null, "decided_at":"ts"|null, "created_at":"ts" }
Admin request item = OnDutyRequest + { "type":"on_duty", "user":UserSummary }
```
Changed `AttendanceRecord` (section 3; every endpoint that returns it, incl. `/attendance/team`):
```json
{ ...existing fields...,
  "work_mode":"office|on_duty|mixed",
  "check_out_source":"office|on_duty"|null,
  "on_duty_locations":["Acme Client - Guindy","Branch - Tambaram"],   // distinct visit location names of the day, first-visit order; [] for office days
  "within_fence":true|false|null }   // CHANGED: null when the day started with a visit (no office check-in). Clients must accept null.
```
`distance_m` rules unchanged (office check-in only; null for a day that started on duty).

## 14.3 Employee endpoints
| Method | Path | Access | Rate limit |
|---|---|---|---|
| GET | /on-duty/locations | Self | 60/min |
| GET | /on-duty/today | Self | 60/min |
| POST | /on-duty/check-in | Self | shared office punch limiter (10/min) + shared punch-fail counter |
| POST | /on-duty/check-out | Self | same |
| GET | /on-duty/visits?from=&to=&page=&page_size= | Self | 60/min |
| POST | /on-duty/requests | Self | shared leave write limiter (`LEAVE_WRITE_RATE_PER_MIN`/`_PER_DAY`, fail closed) |
| GET | /on-duty/requests?status=&from=&to=&page=&page_size= | Self | 60/min |
| GET | /on-duty/requests/{id} | Self (owner) | 60/min |
| POST | /on-duty/requests/{id}/cancel | Self (owner) | shared leave write limiter |

**GET /on-duty/locations** -> 200 `{ "items":[DutyLocation] }`, un-paginated, sorted by name, ACTIVE locations assigned to the caller
(`assign_mode='all'` or an assignment row). `on_duty.enabled=false` -> `{ "items":[] }` (not an error). The client sorts by distance itself.

**GET /on-duty/today** -> 200
```json
{ "work_date":"2026-10-26",
  "enabled":true,
  "attendance":AttendanceRecord|null,
  "open_visit":Visit|null,            // the caller's open visit (any work_date; normally today)
  "visits":[Visit],                   // today's visits, oldest first (includes open_visit)
  "visits_remaining":8,               // max(0, on_duty.max_visits_per_day - visits today)
  "approval": { "required":false, "covered":true, "location_ids":["uuid"], "any_location":false },
  "can_check_in":true,
  "blocked_reason":null }
```
`approval.covered` = an APPROVED request covers today (computed in both modes); `location_ids` = locations of those requests, `any_location` = one of
them has `location_id=null`. `blocked_reason` (advisory; the server re-checks on check-in) is the first that applies of:
`ON_DUTY_DISABLED`, `NO_SHIFT_ASSIGNED`, `NO_LOCATIONS`, `ON_LEAVE`, `VISIT_OPEN`, `MAX_VISITS_REACHED`, `ON_DUTY_NOT_APPROVED` (required and not covered for any location);
`can_check_in = blocked_reason == null`.

**POST /on-duty/check-in**
```json
{ "location_id":"uuid", "lat":13.0069, "lng":80.2201, "accuracy_m":9.0, "fix_time":"2026-10-26T05:11:58Z",
  "selfie_file_id":"uuid"|null, "client_time":"ts"|null, "is_mock_location":false, "note":"Quarterly review"|null }
```
`location_id`, `lat`, `lng`, `accuracy_m`, `fix_time` required (missing -> 422 `VALIDATION_ERROR`, `details.field` names it). `note` <= 200 chars
after cleaning (control chars/markup stripped; blank -> null). -> **201** `{ "visit":Visit, "attendance":AttendanceRecord }`.
Checks, in this order (plan section 5):

| # | Condition | Response | Audited + counted as punch failure |
|---|---|---|---|
| 1 | `on_duty.enabled=false` | 409 `ON_DUTY_DISABLED` | no |
| 1 | no active shift | 422 `NO_SHIFT_ASSIGNED` | no |
| 2 | location unknown, inactive or not assigned | 422 `DUTY_LOCATION_UNAVAILABLE` (same message for all three) | no |
| 3 | approved full-day leave today | 409 `ON_LEAVE` | no |
| 4 | an open visit exists (also the unique-index race) | 409 `VISIT_OPEN`, `details:[{"field":"visit_id","message":"<open visit id>"}]` | no |
| 5 | visits today >= `max_visits_per_day` | 409 `MAX_VISITS_REACHED` | no |
| 6 | `approval_required` and no approved request covering today with this `location_id` or NULL | 409 `ON_DUTY_NOT_APPROVED` | no |
| 7 | fix/mock/accuracy/selfie | 422 `LOCATION_FIX_STALE`, `MOCK_LOCATION_DETECTED`, `LOCATION_ACCURACY_LOW`, `SELFIE_REQUIRED` (when `on_duty.selfie_required`), `SELFIE_STALE`, `SELFIE_REUSED` | yes (as office) |
| 8 | haversine distance > `radius_m` | 422 `OUTSIDE_DUTY_LOCATION`, `details:[{"field":"within_fence","message":"false"}]` (never distance/radius) | yes |

After 5 counted failures in 600 s (shared with office) -> 429 `RATE_LIMITED` + `Retry-After`. Success resets the counter.
Effects: today's attendance row is created (`work_mode=on_duty`, `check_in_at=now`, office fence fields null, `minutes_late=0` unless
`on_duty.apply_late_rule`, status `present`/`late`) or updated (`office` -> `mixed`). The visit links the approved request used (if any).
`is_mock_location=true` accepted (rejection off) -> visit `suspicious=true`, audit `on_duty.suspicious_punch`. Rejections audit
`attendance.punch_rejected` with `after={kind:"on_duty_check_in", reason}` (no coordinates). Holiday/weekly-off: allowed, never late.
Half-day leave: allowed.

**POST /on-duty/check-out**
```json
{ "visit_id":"uuid"|null, "lat":13.0071, "lng":80.2199, "accuracy_m":10.0, "fix_time":"ts",
  "selfie_file_id":"uuid"|null, "client_time":"ts"|null, "is_mock_location":false, "note":"Done"|null }
```
`lat`, `lng`, `accuracy_m`, `fix_time` ALWAYS required (unlike office check-out). `note` non-null replaces the visit note. Selfie optional (never
required at check-out, same as office), single-use rules apply. Works even when `on_duty.enabled=false`, the location is inactive or the caller
is no longer assigned. -> **200** `{ "visit":Visit, "attendance":AttendanceRecord }`.
Errors: 409 `NO_OPEN_VISIT` (no open visit; auto-closed visits are not open), 404 `NOT_FOUND` (`visit_id` given and not the caller's open visit),
422 fix/mock/accuracy/selfie codes (audited + counted), 422 `OUTSIDE_DUTY_LOCATION` only when `on_duty.checkout_geofence=true` (audited + counted);
otherwise stored with `check_out_within_fence=false`. Distance is measured against the location's CURRENT lat/lng/radius.
Attendance update: `check_out_at = max(existing, now)`; `check_out_source='on_duty'` unless the existing value is a later office check-out;
`worked_minutes = check_out_at - check_in_at`; status = `late` if `minutes_late>0` else `present`, then `half_day` if `worked_minutes < attendance.half_day_minutes`.

**GET /on-duty/visits?from=&to=** -> paginated `Visit`, newest first. Defaults: `to` = today, `from` = `to` - 30 days. `to < from` or span > 93 days
-> 422 `INVALID_DATE_RANGE`; malformed -> 422 `VALIDATION_ERROR`. Filter on `work_date`.

**POST /on-duty/requests**
```json
{ "from_date":"2026-10-28", "to_date":"2026-10-30", "location_id":"uuid"|null, "purpose":"Installation at client site" }
```
-> **201** `OnDutyRequest`. Allowed in both approval modes. Errors: 409 `ON_DUTY_DISABLED`; 422 `INVALID_DATE_RANGE` (`to<from`, span > 31 days,
`from_date < today - request_backdate_days`, `to_date > today + request_max_advance_days`); 422 `DUTY_LOCATION_UNAVAILABLE`; 422 `VALIDATION_ERROR`
(`purpose` 1-300 chars after cleaning); 409 `OVERLAPPING_REQUEST` (own pending/approved on-duty request on any date in range, or own pending/approved
FULL-day leave on any date in range; `details:[{"field":"date","message":"2026-10-29"}]` = first clashing date); 429; 503 `TEMPORARILY_UNAVAILABLE`.
Push `new_request` to approvers (14.7). Audit `on_duty.apply`.

**GET /on-duty/requests** -> paginated `OnDutyRequest`, newest first. `status` filter; `from`/`to` = overlapping range.
**GET /on-duty/requests/{id}** -> `OnDutyRequest`; not owner/unknown -> 404.
**POST /on-duty/requests/{id}/cancel** (no body) -> 200 `OnDutyRequest`. Pending only, or approved with `from_date > today` when
`leave.allow_cancel_approved=true`; else 409 `NOT_PENDING`. Not owner/unknown -> 404. Audit `on_duty.cancel`.

## 14.4 Office endpoints (changed, section 3)
- **POST /attendance/check-in**: unchanged. A day that started on duty already has a row -> 409 `ALREADY_CHECKED_IN` (`details` `record_id`).
- **POST /attendance/check-out** ("End day"): 409 `VISIT_OPEN` (`details:[{"field":"visit_id","message":"<id>"}]`) while a visit is open (checked first,
  not counted). 409 `ALREADY_CHECKED_OUT` only when `check_out_source='office'`. A row that started on duty can be closed here (`NOT_CHECKED_IN` only when there is no row).
  Success: `check_out_at = max(existing, now)`, `check_out_source='office'`, `work_mode` `on_duty` -> `mixed`, worked minutes/status recomputed as above.
  Office geofence rule (`attendance.checkout_geofence`) unchanged; an on-duty-only employee may skip this call (the last visit check-out closes the day).
- `GET /attendance/calendar` day gains `"on_duty": true|false` (the day's record has `work_mode` `on_duty` or `mixed`).
- `GET /attendance/summary` gains `"on_duty_days": 3` (records with `work_mode` `on_duty|mixed` in the month; NOT disjoint: those days are also counted under present/late/half_day).
- 00:30 job: rows whose `check_out_at` is set by a visit are not flagged. Open visits of past days -> `auto_closed=true` (`check_out_at` stays null), audit `on_duty.auto_close {count}`.

## 14.5 Admin: duty locations (`duty_location.manage`)
| Method | Path | Rate limit |
|---|---|---|
| GET | /admin/duty-locations?q=&is_active=&assign_mode=&page=&page_size= | 60/min |
| POST | /admin/duty-locations | 30/min (writes) |
| GET | /admin/duty-locations/{id} | 60/min |
| PUT | /admin/duty-locations/{id} | 30/min |
| DELETE | /admin/duty-locations/{id} | 30/min |
| GET | /admin/duty-locations/{id}/assignments | 60/min |
| PUT | /admin/duty-locations/{id}/assignments | 10/min |
| GET | /admin/employees/{id}/duty-locations | 60/min |

**List** -> paginated `DutyLocationAdmin`, sorted by name; `page_size` default 100, max 100; `q` matches name/address (case-insensitive).
**POST** body `{ "name", "address":null, "lat", "lng", "radius_m", "assign_mode":"selected", "is_active":true }` -> 201 `DutyLocationAdmin`.
`name` 2-80 chars after trim/cleaning, `address` <= 300, `lat` -90..90, `lng` -180..180 (finite), `radius_m` int 10..2000, `assign_mode` default `selected`.
**PUT** = full replace of the same fields -> 200. Editing lat/lng/radius never rewrites past visits.
**DELETE** -> 204; 409 `IN_USE` when any visit or request references it (deactivate instead).
Errors: 409 `CONFLICT` duplicate name (case-insensitive, `details:[{"field":"name"}]`), 422 `VALIDATION_ERROR`, 404.
Deactivating: no new check-ins/requests at it; open visits can still check out; existing approved requests stay but cannot be used for check-in there.
Audit `duty_location.create|update|delete` (coordinates allowed: business data).

**GET .../assignments** -> 200
```json
{ "location_id":"uuid", "assign_mode":"selected",
  "items":[ { "user":UserSummary, "assigned_at":"ts" } ] }   // active, non-deleted users only; sorted by employee_id; max 5000
```
Rows are returned (and kept) even while `assign_mode='all'`, so switching back restores the list.
**PUT .../assignments** (full replace)
```json
{ "assign_mode":"selected", "user_ids":["uuid", "..."], "add_department_ids":["uuid"] }
```
`user_ids` required (may be `[]`), unique, <= 5000; each must be an active non-deleted user. `add_department_ids` optional (<= 50): expanded to
those departments' current active members and merged into `user_ids` (no department link stored). Result > 5000 or any bad id -> 422
`VALIDATION_ERROR` with `details:[{"field":"user_ids"|"add_department_ids","message":"<bad uuid>"}]` (first 20 listed). -> 200 same shape as GET.
Rows of previously assigned inactive users are removed by the replace. Audit `duty_location.assignments` `after={assign_mode, added:[user ids], removed:[user ids]}`.

**GET /admin/employees/{id}/duty-locations** -> 200 `{ "items":[ { "id","name","is_active","assign_mode" } ] }` = locations that apply to the
employee (`all` + explicit rows), active and inactive, sorted by name. Unknown/deleted employee -> 404.

## 14.6 Admin: visits, requests, dashboard, attendance day
**GET /admin/on-duty/visits?date=&from=&to=&location_id=&department_id=&employee_id=&open=&page=&page_size=** (`attendance.view_all` = everyone,
`attendance.view_team` = direct reports; 60/min) -> paginated `AdminVisit`, sorted by `check_in_at` desc. `date` = single day (overrides from/to);
from/to capped at 366 days as `/attendance/team` (422 `INVALID_DATE_RANGE`; missing bounds filled). `employee_id` = business key (case-insensitive).
`open=true` = currently open visits only. Unknown `location_id`/`department_id` -> empty result (no 404). Managers get the view_all-only fields as null.

**/admin/requests** (section 4): `type` accepts `on_duty`; `PUT /admin/requests/on_duty/{id}/approve|reject`. Same bodies, scope, rate limits
(`LEAVE_DECIDE_RATE_PER_MIN`) and errors as leave/permission: 403 `FORBIDDEN` (self), 404 (unknown or out of scope), 409 `NOT_PENDING`,
409 `REQUESTER_INACTIVE` (approve), 409 `OVERLAPPING_REQUEST` (approve: a pending/approved full-day leave now overlaps), 422 (reject without comment).
Response = admin request item. `from`/`to` = overlapping range. Approval creates no attendance. Audit `request.approve|reject` (`entity_type=on_duty_request`).
Leave admin items gain `"on_duty_conflict": true|false` (an APPROVED on-duty request of the requester overlaps the leave); approving is still allowed (leave wins; check-in then returns `ON_LEAVE`).
Deactivate/delete employee: pending on-duty requests auto-cancelled (audit `on_duty.auto_cancel`); open visits are left for the 00:30 job.

**GET /admin/dashboard** (12.5): `attendance` block gains `"on_duty": 12` (today's rows with `work_mode` `on_duty|mixed`) and `"on_duty_open": 5`
(visits open now); `requests` block gains `"on_duty_pending": 1`.
**GET /admin/attendance/day** (12.5): items gain `"work_mode":"office|on_duty|mixed"|null` (null without a row) and `"on_duty_locations":[names]`;
`status` filter gains `on_duty` (rows with `work_mode` `on_duty|mixed`). Still no coordinates, distances or selfies.

## 14.7 Push (section 11)
- New type `on_duty_decision` -> requester after approve/reject: `{ "type":"on_duty_decision", "id":"<request id>", "title":"On duty request approved"|"On duty request rejected", "body":"28 Oct - 30 Oct 2026" }`.
- `new_request` for on-duty requests carries `"request_type":"on_duty"` in addition to the existing fields: `{ "type":"new_request", "request_type":"on_duty", "id":"<request id>", "title":"New on duty request", "body":"<full_name> (<employee_id>)" }`; routing/throttling as leave.
- Never coordinates, location names of the user's position, distances or notes. No push for check-in/out or auto-close.

## 14.8 Export (section 10)
- `type` accepts `on_duty`; `type=all` = Attendance, Leave, Late, Permission, **On Duty**. Per-type permission: `attendance.view_all|view_team`; scope rules as attendance.
- Sheet `On Duty` (filter `work_date`, ordered by date, employee id, check-in): Date, Employee ID, Name, Department, Location, Check-in, Check-out,
  Visit Minutes, Auto Closed, Check-out Within Site; plus **Note** (last column) only with `include_reasons=true` (needs `export.sensitive`).
  Never coordinates, distances, accuracy, selfies or suspicious/mock flags.
- Sheet `Attendance` gains columns **Mode** (`office|on_duty|mixed`) and **On Duty Locations** (names joined with `, `), appended after Within Geofence.
  Within Geofence is empty for days that started on duty.

## 14.9 Merge checklist for `docs/api-contract.md`
- s0 error list: domain codes below. s1 `/config` gains:
  ```json
  "on_duty": { "enabled":true, "has_locations":true, "approval_required":false, "selfie_required":false,
               "checkout_geofence":false, "max_visits_per_day":10, "request_backdate_days":0, "request_max_advance_days":60 }
  ```
  `has_locations` = enabled AND the caller has >= 1 active assigned location. The app shows the Office | On Duty switch iff `enabled && has_locations`.
- s2 `/home`: `today_attendance` gains `"work_mode":"office|on_duty|mixed"|null` and `"open_visit": { "id","location_name","check_in_at" }|null`;
  `pending_requests` counts the caller's pending leave + permission + on-duty requests.
- s2 `/files`: visit selfies follow the `selfie` rule (owner, `attendance.view_all`, `attendance.view_team` for direct reports); single use across office and visit punches; `selfie` purpose reused.
- s3: AttendanceRecord, check-out, calendar, summary, job changes (14.2, 14.4). Retention also nulls `on_duty_visits.*_loc_enc` and deletes visit selfies.
- s4: `type=on_duty`, `on_duty_conflict` on leave items, on-duty requests follow `leave.allow_cancel_approved` and `leave.reason_retention_days` (purpose -> `""`, comment -> null).
- s9: `on_duty.*` settings and ranges; permission `duty_location.manage`; duty-location resources (14.5).
- s10: 14.8. s11: 14.7. s12.5: 14.6. License `read_only`: visit punches and on-duty request writes are refused like office punches (403 `LICENSE_EXPIRED`).
- **New error codes:** 409 `ON_DUTY_DISABLED`, `VISIT_OPEN`, `NO_OPEN_VISIT`, `MAX_VISITS_REACHED`, `ON_DUTY_NOT_APPROVED`; 422 `DUTY_LOCATION_UNAVAILABLE`, `OUTSIDE_DUTY_LOCATION`.
  Reused: `ON_LEAVE`, `NO_SHIFT_ASSIGNED`, `LOCATION_*`, `MOCK_LOCATION_DETECTED`, `SELFIE_*`, `OVERLAPPING_REQUEST`, `NOT_PENDING`, `REQUESTER_INACTIVE`, `INVALID_DATE_RANGE`, `IN_USE`, `CONFLICT`, `RATE_LIMITED`.
- **Audit actions:** `duty_location.create|update|delete|assignments`, `on_duty.apply|cancel|auto_cancel|suspicious_punch|auto_close|retention_purge`,
  `attendance.punch_rejected` (`kind` `on_duty_check_in|on_duty_check_out`), `request.approve|reject`. Never visit coordinates or notes in audit rows.
- **Flutter error mapping:** `OUTSIDE_DUTY_LOCATION` "You are outside <name>"; `VISIT_OPEN` reload `/on-duty/today`; `NO_OPEN_VISIT` reload; `MAX_VISITS_REACHED`;
  `ON_DUTY_NOT_APPROVED` show "Request On Duty"; `DUTY_LOCATION_UNAVAILABLE` refresh `/on-duty/locations`; `ON_DUTY_DISABLED` hide the On Duty UI.
