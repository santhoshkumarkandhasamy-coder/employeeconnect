# 13 - On Duty (check-in/out at approved client sites and branches)

Status: PLAN. Contract draft: `docs/plans/13-on-duty-contract.md` (to be merged into `docs/api-contract.md` as a new section 14,
plus the small changes to sections 3, 4, 9, 10, 11 and 12.5 it lists). Migration: `0019_on_duty` (after `0018_payroll`).

**Scope guard.** On Duty is a one-shot location read at visit check-in and at visit check-out only, the same as office attendance.
Not included and not to be reintroduced: continuous/background tracking, live location, location streams, trips, SOS, home location,
geofence-exit alerts. The app must not request background location permission.

## 0. What exists and is reused
- Office punch pipeline in `services/attendance.py`: `_require_fix` (accuracy + fresh `fix_time`), `_enforce_mock`, `_enforce_accuracy`,
  `_validate_selfie` (owner/purpose/freshness/single use), `_audit_reject`, the punch-fail counter (`PUNCH_FAIL_LIMIT`/`WINDOW`), the 10/min
  punch limiter, encrypted `*_loc_enc` columns, the retention job, the 00:30 auto-close job, `minutes_late_for`, `working_day`, leave checks.
- One `attendance` row per user per `work_date` stays the single source for "present" (calendar, summary, `/home`, dashboard,
  `/admin/attendance/day`, export, **payroll** `payroll_days` which treats any attendance row as a worked day). On Duty writes INTO that row,
  so payroll, LOP and the calendar need no logic change.
- Request pattern of module 03 (`/admin/requests`, `request.*` permissions, approve/reject, push, auto-cancel on deactivation).

## 1. Decisions (defaults; see open questions in section 10)
1. **Locations**: admin manages any number of duty locations (name, address, lat, lng, radius, active). Office geofence (`office.*`) is unchanged
   and is NOT a duty location.
2. **Assignment (lightweight)**: per location `assign_mode` = `all` (every active employee) or `selected` (explicit user list). The admin UI offers
   "add all members of department X" as a convenience that expands to user ids at save time (no department link stored; later joiners are not added).
3. **Approval**: setting `on_duty.approval_required` (default **false**). When false, any assigned employee can check in at an assigned location.
   When true, a visit check-in needs an APPROVED on-duty request covering today (and that location, or "any assigned location").
   Requests reuse the leave/permission pattern (`type=on_duty` in `/admin/requests`). Requests can be filed in both modes (planning/visibility);
   only enforcement depends on the setting.
4. **Multiple visits per day**: yes. Each visit is one row (location, check-in, check-out). At most one OPEN visit at a time; at most
   `on_duty.max_visits_per_day` (10) visits per day. The day counts as present once.
5. **Day model**: the `attendance` row is the day summary. `check_in_at` = first punch of the day (office check-in or first visit check-in);
   `check_out_at` = latest closing punch (office check-out or a visit check-out). `work_mode` = `office` | `on_duty` | `mixed`.
   `worked_minutes` = `check_out_at - check_in_at` (span, travel between sites counts). Half-day rule unchanged (`attendance.half_day_minutes`),
   re-evaluated on every closing punch (status can go back from `half_day` to `present`/`late` when a later visit extends the day).
6. **Late**: a day that STARTS with a visit is not late by default (`on_duty.apply_late_rule` default **false**; when true, the normal shift
   start + grace + permission rules apply to the first visit check-in). A day that starts with an office check-in keeps its office late mark;
   later visits never add or remove a late record. On-duty check-in never produces `OUTSIDE_GEOFENCE` against the office fence.
7. **Visit check-out location**: always required (one-shot fix; accuracy/fresh-fix/mock rules apply) and stored with `within_fence`.
   Rejected outside the visit's radius only when `on_duty.checkout_geofence=true` (default **false**); otherwise accepted and flagged
   (`check_out_within_fence=false`, visible to HR/admin only).
8. **Selfie**: `on_duty.selfie_required` (default false), same rules as office selfies (purpose `selfie`, single use across office and visit punches).
9. **Office interplay**:
   - Office check-in when the day already has a row (started on duty) -> existing 409 `ALREADY_CHECKED_IN`. The app hides it and shows "End day".
   - Visit check-in is allowed whether or not the office punch is open, and after an office check-out (the day is extended by the visit).
   - Office check-out ("End day") is refused with 409 `VISIT_OPEN` while a visit is open. `ALREADY_CHECKED_OUT` now means "an OFFICE check-out
     was already done" (`check_out_source='office'`); a check-out time set by a visit does not block it.
   - With `attendance.checkout_geofence=true` an on-duty-only employee does not need the office check-out; the last visit check-out closes the day.
10. **Leave/holiday**: approved full-day leave -> 409 `ON_LEAVE` (same as office). Half-day leave allowed. Holiday/weekly-off visits allowed, never late.
11. **Feature switch**: `on_duty.enabled` (default **true**); the employee UI appears only when the caller has at least one active assigned location.
12. **Night shifts**: not supported for On Duty in v1. `work_date` = office date of the visit check-in; an open visit still open when the 00:30 job runs is
    auto-closed (flagged).

## 2. Database (migration `0019_on_duty`, + downgrade, regenerate `schema.sql`, seed)
**`duty_locations`**
- `id uuid PK`, `name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 2 AND 80)`, unique index `uq_duty_locations_name ON (lower(btrim(name)))`,
  `address text NULL CHECK (char_length(address) <= 300)`, `lat double precision NOT NULL CHECK (lat BETWEEN -90 AND 90)`,
  `lng double precision NOT NULL CHECK (lng BETWEEN -180 AND 180)`, `radius_m integer NOT NULL CHECK (radius_m BETWEEN 10 AND 2000)`,
  `assign_mode text NOT NULL DEFAULT 'selected' CHECK (IN ('all','selected'))`, `is_active boolean NOT NULL DEFAULT true`,
  `created_by`, `updated_by uuid NULL FK users RESTRICT`, `created_at`, `updated_at` + `set_updated_at` trigger. Index `ix_duty_locations_active (is_active)`.
- Coordinates are business data (client site / branch), stored in clear so distances can be computed in SQL/Python. Not personal data.

**`duty_location_assignments`**
- `location_id uuid FK duty_locations ON DELETE CASCADE`, `user_id uuid FK users ON DELETE CASCADE`, `created_at`, `created_by uuid NULL FK users`,
  `PRIMARY KEY (location_id, user_id)`, index `ix_duty_location_assignments_user (user_id)`. Rows are ignored while `assign_mode='all'` (kept, so switching back restores the list).

**`on_duty_requests`**
- `id uuid PK`, `user_id FK users`, `from_date date NOT NULL`, `to_date date NOT NULL`, `CHECK (to_date >= from_date)`, `CHECK (to_date - from_date <= 30)`,
  `location_id uuid NULL FK duty_locations RESTRICT` (NULL = any assigned location), `purpose_enc text NOT NULL` (encrypted, 1-300 chars plaintext),
  `status text NOT NULL DEFAULT 'pending' CHECK (IN ('pending','approved','rejected','cancelled'))`, `approver_id uuid NULL FK users`,
  `decision_comment_enc text NULL`, `decided_at timestamptz NULL`, `created_at`, `updated_at`.
  Indexes `(user_id, from_date)`, `(status)`, `(from_date, to_date)`.

**`on_duty_visits`**
- `id uuid PK`, `user_id FK users`, `attendance_id FK attendance ON DELETE CASCADE`, `work_date date NOT NULL`, `location_id FK duty_locations RESTRICT`,
  `location_name text NOT NULL` (snapshot), `request_id uuid NULL FK on_duty_requests SET NULL`,
  `check_in_at timestamptz NOT NULL`, `check_in_loc_enc` (EncryptedJSON), `check_in_distance_m integer CHECK (>= 0)`, `check_in_accuracy_m real`,
  `check_in_clock_skew_s integer`, `check_in_mock_location boolean NOT NULL DEFAULT false`, `check_in_selfie_file_id uuid NULL FK files SET NULL`,
  `check_out_at timestamptz NULL CHECK (check_out_at IS NULL OR check_out_at > check_in_at)`, `check_out_loc_enc`, `check_out_distance_m integer CHECK (>= 0)`,
  `check_out_within_fence boolean NULL`, `check_out_accuracy_m real`, `check_out_mock_location boolean NOT NULL DEFAULT false`,
  `check_out_selfie_file_id uuid NULL FK files SET NULL`, `note_enc text NULL` (encrypted, <= 200 chars plaintext), `auto_closed boolean NOT NULL DEFAULT false`,
  `suspicious boolean NOT NULL DEFAULT false`, `created_at`, `updated_at`.
- Indexes: `(user_id, work_date)`, `(work_date)`, `(location_id)`, partial UNIQUE `uq_on_duty_visits_one_open ON (user_id) WHERE check_out_at IS NULL AND auto_closed = false`,
  partial UNIQUE on each selfie column `WHERE ... IS NOT NULL`, partial `ix_on_duty_visits_suspicious (work_date) WHERE suspicious`.

**`attendance` (alter)**
- `work_mode text NOT NULL DEFAULT 'office' CHECK (IN ('office','on_duty','mixed'))`.
- `check_out_source text NULL CHECK (IN ('office','on_duty'))`, `CHECK ((check_out_at IS NULL) = (check_out_source IS NULL))`.
  Backfill: `check_out_source='office'` where `check_out_at IS NOT NULL`.
- `check_in_within_fence` / `check_in_distance_m` stay NULL for a day that started on duty (meaning "not an office punch").
- Index `ix_attendance_work_mode (work_date) WHERE work_mode <> 'office'`.

**Seed**: permission `duty_location.manage` ("Manage on-duty locations and assignments", admin + hr). Settings (section 4). `admin` role re-sync picks the permission up.

## 3. API (full shapes in the contract draft)
| Method | Path | Access |
|---|---|---|
| GET | /on-duty/locations | Self (assigned, active only) |
| GET | /on-duty/today | Self |
| POST | /on-duty/check-in | Self |
| POST | /on-duty/check-out | Self |
| GET | /on-duty/visits?from=&to=&page= | Self |
| POST/GET | /on-duty/requests | Self |
| GET | /on-duty/requests/{id} | Self (owner) |
| POST | /on-duty/requests/{id}/cancel | Self (owner, pending) |
| GET/POST | /admin/duty-locations | `duty_location.manage` |
| GET/PUT/DELETE | /admin/duty-locations/{id} | `duty_location.manage` |
| GET/PUT | /admin/duty-locations/{id}/assignments | `duty_location.manage` |
| GET | /admin/employees/{id}/duty-locations | `duty_location.manage` |
| GET | /admin/on-duty/visits?date=&from=&to=&location_id=&department_id=&employee_id=&page= | `attendance.view_team` / `attendance.view_all` |
| GET/PUT | /admin/requests?type=on_duty, /admin/requests/on_duty/{id}/approve\|reject | `request.*` (existing) |

Changed: `AttendanceRecord` gains `work_mode`, `on_duty_locations`; calendar day gains `on_duty`; summary gains `on_duty_days`; `/home`, `/config`,
`/admin/dashboard`, `/admin/attendance/day`, export (`type=on_duty`, two new Attendance columns), settings keys, push types.

## 4. Settings (validated like section 9; `settings.manage`)
| Key | Type / range | Default |
|---|---|---|
| `on_duty.enabled` | bool | true |
| `on_duty.approval_required` | bool | false |
| `on_duty.apply_late_rule` | bool | false |
| `on_duty.checkout_geofence` | bool | false |
| `on_duty.selfie_required` | bool | false |
| `on_duty.max_visits_per_day` | int 1..50 | 10 |
| `on_duty.request_backdate_days` | int 0..30 | 0 |
| `on_duty.request_max_advance_days` | int 1..365 | 60 |

Accuracy, fix age, mock rejection and retention reuse the `attendance.*` keys (one policy for all punches).

## 5. Business rules and validation
**Visit check-in** (one transaction; order matters, each refusal before the fence check is not counted as a punch failure unless noted):
1. `on_duty.enabled` false -> 409 `ON_DUTY_DISABLED`. No active shift -> 422 `NO_SHIFT_ASSIGNED`.
2. `location_id` must be an active location assigned to the caller (`all`, or a row in assignments) -> else 422 `DUTY_LOCATION_UNAVAILABLE`
   (same answer for unknown, inactive and unassigned: no enumeration).
3. Approved full-day leave today -> 409 `ON_LEAVE`.
4. An open visit exists -> 409 `VISIT_OPEN` (`details:[{field:"visit_id",message:"<id>"}]`).
5. Visits today >= `max_visits_per_day` -> 409 `MAX_VISITS_REACHED`.
6. `approval_required` and no approved request covering today with `location_id` = this location or NULL -> 409 `ON_DUTY_NOT_APPROVED`.
7. Fix rules (`_require_fix`, mock, accuracy) and selfie rules -> existing codes, audited + counted.
8. Haversine distance to the location <= `radius_m`, else 422 `OUTSIDE_DUTY_LOCATION` (details only `[{field:"within_fence",message:"false"}]`,
   never distance/radius), audited + counted.
9. Lock or create today's `attendance` row (`SELECT ... FOR UPDATE`; insert on miss, catching the UNIQUE race and re-reading). New row:
   `work_mode='on_duty'`, `check_in_at=now`, office fence columns NULL, `minutes_late` = 0 unless `apply_late_rule` (then `minutes_late_for` on a working day,
   creating `late_records` as today). Existing row: `work_mode` becomes `mixed` if it was `office`.
10. Insert the visit (snapshot `location_name`, link `request_id` if one was used). The one-open-visit unique index is the race backstop -> 409 `VISIT_OPEN`.
11. Reset the punch-fail counter. Mock flag accepted (only when rejection is off) -> `suspicious` + audit `on_duty.suspicious_punch`.

**Visit check-out**: the caller's open visit (optional `visit_id` must match it, else 404). None -> 409 `NO_OPEN_VISIT`. Fix + mock + accuracy rules always;
distance computed and stored; outside -> 422 `OUTSIDE_DUTY_LOCATION` only if `on_duty.checkout_geofence`. Update attendance: `check_out_at = max(existing, now)`,
`check_out_source='on_duty'` (unless the current value is a later office check-out), recompute `worked_minutes` and status
(`late` if `minutes_late>0` else `present`, then `half_day` if below threshold). `attendance.auto_closed` is never touched here (the job only flags past days).

**Office endpoints (changed)**: check-out -> 409 `VISIT_OPEN` while a visit is open; `ALREADY_CHECKED_OUT` iff `check_out_source='office'`; on success
`check_out_at = max(existing, now)`, source `office`, mode `mixed` if the row was `on_duty`. Check-in unchanged.

**Requests**: `from_date` >= today - `request_backdate_days`, `to_date` <= today + `request_max_advance_days`, span <= 31 days -> else 422 `INVALID_DATE_RANGE`.
`location_id` (optional) must be assigned+active -> 422 `DUTY_LOCATION_UNAVAILABLE`. Overlap with own pending/approved on-duty request -> 409 `OVERLAPPING_REQUEST`.
Overlap with a pending/approved FULL-day leave -> 409 `OVERLAPPING_REQUEST` (also re-checked at approval). `purpose` 1-300 chars, cleaned, encrypted.
Approve/reject/cancel/auto-cancel-on-deactivation exactly as leave/permission (self-approval 403, out of scope 404, `NOT_PENDING`, `REQUESTER_INACTIVE`, reject comment required).
Cancelling an approved request follows `leave.allow_cancel_approved` (same single switch). Approval does not create attendance; only a visit does.
Approving a leave over an approved on-duty day is allowed (leave wins; check-in is then `ON_LEAVE`); the app shows a warning on the approver side.

**Locations admin**: name unique (case-insensitive) -> 409 `CONFLICT`; delete when any visit or request references it -> 409 `IN_USE` (deactivate instead).
Deactivating: no new check-ins/requests; an open visit can still be checked out (fence = snapshot of the location's current lat/lng/radius at check-out time;
if the row is gone the distance is null and `within_fence` null). Editing lat/lng/radius does not rewrite past visits. Assignments PUT is a full replace
(<= 5000 user ids, active non-deleted users only, else 422 with the bad ids in `details`).

**Jobs**: the 00:30 auto-close also flags past-day open visits (`auto_closed=true`, `check_out_at` stays NULL), audit `on_duty.auto_close {count}`.
A day whose attendance row has `check_out_at` NULL is flagged as today. Retention (`attendance.retention`) also nulls `on_duty_visits.*_loc_enc` and deletes
visit selfies with the same day counts. `on_duty_requests` purpose/comment retention follows `leave.reason_retention_days`.

## 6. Edge cases
- Double tap on visit check-in -> unique index -> 409 `VISIT_OPEN` (second call), the app reads `/on-duty/today`.
- Two overlapping locations both containing the user: the client picks; server only checks the chosen one.
- Location radius smaller than reported accuracy: allowed (same as office); open question O6 proposes requiring `accuracy_m <= radius_m`.
- Employee unassigned or location deactivated while a visit is open: check-out still works.
- Employee deactivated with an open visit: the auto-close job flags it; pending on-duty requests auto-cancelled.
- Office check-in at 09:30, visit 11:00-15:00, no office check-out: day closes at 15:00 (from the visit); the 00:30 job does not flag the row.
- Visit after office check-out extends `check_out_at` and may lift `half_day`.
- Clock: server time only; `fix_time` freshness as office.
- Timezone setting change does not rewrite history.
- Approval required, request approved for location A, employee is at location B -> `ON_DUTY_NOT_APPROVED` (unless the request has NULL location).
- Payroll: unchanged; an on-duty day is a worked day; a short span can make it `half_day` and therefore half LOP (as office; see O4).

## 7. Flutter
**Employee (mobile, `features/on_duty` + changes in `features/attendance`)**
- Attendance > Today tab: when `/config.on_duty.enabled` and `/on-duty/locations` is non-empty, a segmented control **Office | On Duty**.
- On Duty panel: one-shot fix on open/refresh (no stream); location list sorted by client-computed distance with "nearby" badge for those within range
  (the client knows lat/lng, not the radius: badge uses a conservative 150 m hint only, the server decides). Optional note field. **Check in here** button.
  Open visit card: "On duty at <name> since 10:12" + **Check out**. Today's visit timeline. "End day" (office check-out) disabled while a visit is open.
  "Open in Maps" button per location (external app intent with the site coordinates only; never the user's position).
- Approval-required mode: banner "Approval needed" + **Request On Duty** when no approved request covers today; requests form (dates, location or "any", purpose)
  and list in the Requests area as a third tab beside Leave and Permission (reuse list/detail/cancel widgets).
- History/calendar: "On Duty" chip and location names on records; day detail lists visits; summary card shows `on_duty_days`.
- Error mapping: `OUTSIDE_DUTY_LOCATION` ("You are outside <name>"), `VISIT_OPEN`, `NO_OPEN_VISIT`, `MAX_VISITS_REACHED`, `ON_DUTY_NOT_APPROVED`, `DUTY_LOCATION_UNAVAILABLE` (refresh list), `ON_DUTY_DISABLED`.
- Permissions: only `ACCESS_FINE_LOCATION`/WhenInUse (existing). No `ACCESS_BACKGROUND_LOCATION`, no foreground service, no `Always` on iOS.

**Admin (mobile Admin area)** - visible with `duty_location.manage`
- Admin > On-duty locations: list (search, active filter, assign mode chip, assigned count), create/edit form (name, address, lat/lng numeric fields,
  "Use my current location" one-shot fill, "paste coordinates" parser for `12.97, 77.59` or a maps link, radius 10-2000 m, active), delete/deactivate.
- Assignment screen: All employees / Selected employees; searchable multi-select of employees; "Add department" picker (expands to its active members).
- Admin > Requests: On Duty tab (`type=on_duty`). Admin > Team attendance: mode chip + locations; On-duty visits list (filters date/location/department/employee),
  `distance_m`, `within_fence`, `suspicious` shown to `attendance.view_all` only. Admin > Settings: the `on_duty.*` keys.
- Employee detail: read-only "On-duty locations" section.

**Web console** (`lib/console/pages`)
- New page **On-duty locations** (table, form dialog, assignment dual-list with department bulk add). Coordinates are typed or pasted; no map tiles
  (CSP `connect-src/img-src 'self'` and "data stays on our server": no Google/OSM tiles in v1).
- New page **On-duty visits** (filters, CSV-free; export via Export page `type=on_duty`). Attendance day page: Mode column + `on_duty` status filter.
  Dashboard: `on_duty` count. Requests page: On Duty type. Settings page: `on_duty.*`.
- Console uses no geolocator (no "use my location" button on web).

## 8. Security considerations
- Server is the only authority: assignment, active flag, approval, distance and fix checks are all server side; `location_id` IDOR returns the same 422 for unknown/unassigned.
- Errors never carry distance or radius; employees never receive `radius_m` (prevents boundary bisection). Employees get lat/lng of their assigned sites only (needed for navigation;
  it equals what an office `/config` already exposes for the office). See O7.
- Punch-fail counter and 10/min punch limiter are SHARED between office and on-duty punches (one budget per user), so on-duty cannot be used to probe.
- Mock-location, stale-fix, accuracy rules identical to office; suspicious flag + audit. Selfie single-use across both tables.
- Visit coordinates and notes encrypted at rest; retention purge; never in audit rows, logs, push, export, dashboard or `/admin/attendance/day`.
- No background location; the app reads location only on screen open/refresh and on the punch itself. Reviewer verifies manifests/Info.plist and that no stream API is used.
- Manager scope: `attendance.view_team` = direct reports only, `distance_m`/`suspicious` only for `view_all`.
- Admin writes audited; assignment changes audited with added/removed employee ids (not names). Location coordinates may appear in audit (business data).
- Rate limits: location writes 30/min per admin, assignments PUT 10/min, admin visits list 60/min, employee `/on-duty/locations` 60/min; request writes share the leave limiter.
- License `read_only` mode: on-duty punches and requests are employee writes and follow the same rule as office punches.

## 9. Tasks (in order)
**database-engineer**
1. Migration `0019_on_duty` (+ downgrade): 4 new tables, `attendance.work_mode`/`check_out_source` + backfill + checks + index, constraints and indexes from section 2.
2. Seed: `duty_location.manage` (admin, hr), `on_duty.*` settings. Regenerate `schema.sql`. Migration test like `test_migration_0018.py` (upgrade/downgrade, backfill).

**backend-developer**
1. Models + schemas (`extra=forbid`). Extract the shared punch validators from `services/attendance.py` into a reusable helper (no behaviour change for office).
2. `services/duty_locations.py` + `routers/admin_duty_locations.py`: CRUD, assignments, employee read, audit, rate limits, settings validation for `on_duty.*`.
3. `services/on_duty.py` + `routers/on_duty.py`: locations, today, check-in/out (rules in section 5, row locks, race handling), visits history, shared fail counter/limiter.
4. Office changes: `VISIT_OPEN` on office check-out, `check_out_source`, `max()` rule, `work_mode`; `AttendanceRecord`/calendar/summary/`/home`/`/config` additions.
5. Requests: `on_duty_requests` apply/list/detail/cancel, `/admin/requests?type=on_duty` + approve/reject (re-check overlap), push `on_duty_decision`, `new_request` routing, auto-cancel on deactivate/delete,
   reason retention, `pending_requests` in `/home` includes on-duty.
6. Admin views: `/admin/on-duty/visits`, dashboard `on_duty`, `/admin/attendance/day` mode + filter. `/files` selfie access for visit selfies (same rule as attendance selfies).
7. Jobs: auto-close open visits, retention for visit locations/selfies. Export: `type=on_duty` sheet, Attendance columns, `type=all` includes it.
8. Tests: every rule/edge case in sections 5-6, office regression suite unchanged. Update contract implementation notes.

**flutter-developer**
1. Models/repository/providers for on-duty (`features/on_duty`), `/config.on_duty`, new error codes.
2. Attendance Today tab mode switch + On Duty panel + visit timeline; history/calendar chips; summary `on_duty_days`.
3. On-duty requests (apply, list, detail, cancel) in the Requests area.
4. Admin: locations list/form/assignments, Requests On Duty tab, visits list, team attendance mode, settings keys, employee detail section.
5. Console: On-duty locations page, On-duty visits page, Attendance day/Dashboard/Requests/Settings additions; no mobile plugins in the console build.
6. Widget/unit tests (mode switch visibility, error mapping, coordinate paste parser, assignment selection).

**security-reviewer**
Spoofing (mock, stale fix, replayed selfie across tables), IDOR on `location_id`/`visit_id`/request ids, enumeration of locations via error codes, radius leakage,
shared fail counter, manager scope on visits, background-location absence (manifest/plist/code), audit coverage, encryption + retention of visit data,
console CSP unchanged (no tile servers). Write `docs/reviews/11-on-duty-security.md`.

**qa-tester**
Backend: assign modes (all/selected/switch), inactive/unassigned/unknown -> same 422, fence inside/edge/outside per location radius, accuracy/stale/mock,
one open visit (concurrent check-ins), max visits, approval on/off (location-specific vs any), leave/holiday/weekly-off, late on/off, office+visit combinations
(office first, visit first, visit after office check-out, office check-out while visit open, `ALREADY_CHECKED_OUT` semantics), half_day recompute, auto-close of visits,
retention, export columns/sheet, dashboard/day counts, payroll run with on-duty days (no LOP), deactivation with open visit/pending requests.
Flutter: mode switch, timeline, errors, admin forms and assignments, console pages. Manual: real phone at two sites in one day.

## 10. Open questions for the user
1. **Approval**: confirm default "no approval needed" (`on_duty.approval_required=false`). If approval is used, may one request cover several days (proposed up to 31) and "any assigned location"?
2. **Assignment**: is all-or-selected employees enough, or do you need a stored department assignment (new joiners of a department get the site automatically)? Proposed: v1 without it.
3. **Late**: should a day that starts at a client site be checked against the shift start (late possible)? Proposed: no (`on_duty.apply_late_rule=false`).
4. **Worked time / half day**: is the span from first check-in to last check-out (including travel) the right worked time, and should a short on-duty day become `half_day` (half LOP in payroll)? Proposed: yes, same as office.
5. **Visit check-out outside the site**: allow and flag (proposed), or refuse (`on_duty.checkout_geofence=true`)?
6. **Accuracy vs radius**: should a fix whose accuracy is larger than the site radius be refused for small sites? Proposed: no in v1 (same as office).
7. **Coordinates to employees**: show site lat/lng to assigned employees for "Open in Maps" (proposed yes), or only name/address?
8. **Night shifts / multi-day outstation duty**: out of scope in v1 (an open visit is auto-closed at 00:30). Do you need overnight or outstation trips (without tracking)?
9. **HR corrections**: may HR edit or delete a visit (forgot to check out)? Proposed v2 (today: auto-closed flag for HR review, same as office).
10. **Export**: is one `On Duty Visits` sheet (also in `type=all`) plus two Attendance columns enough? Should on-duty requests get their own sheet?
