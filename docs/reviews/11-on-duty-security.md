# Review 11 - On Duty (2026-10-26)

Read-only security review by security-reviewer. Scope: plan `docs/plans/13-on-duty.md` section 9 and contract `docs/plans/13-on-duty-contract.md`.
The backend and Flutter work were still in progress during the review, so this covers the working tree as it was on 2026-10-26 (uncommitted,
on top of 5ea01a0). See "Not implemented / not reviewed" at the end.

**Result: no Critical or High findings.** Object-level authorisation, location enumeration, radius secrecy, the shared fail counter,
manager scope, encryption at rest and audit redaction all hold in the backend code that exists so far. There are two Medium findings:
both are design risks (client-asserted position, and HR being able to create presence). There are also several Low and Info items.

## Verified (no finding)
- **IDOR `location_id`**: check-in and request apply both resolve through `duty_locations.assigned_active_location`, which needs active + (`all` or an assignment row).
  Unknown, inactive and unassigned give the same 422 `DUTY_LOCATION_UNAVAILABLE` with the same details (`services/on_duty.py:92-94,295-297,595-596`).
  This check runs before any fix or fence logic, so it is not an oracle.
- **IDOR `visit_id`**: check-out loads only the caller's own open visit (`on_duty.py:387-391`). A foreign `visit_id` returns 409 `NO_OPEN_VISIT` or 404, and
  never shows anything about other users. No endpoint takes a visit id as a path parameter.
- **IDOR request ids**: `get_mine`/`cancel` check ownership before the row lock and return 404 otherwise (`on_duty.py:687-705`). Admin decide reuses
  `leave._scope_problem`: self gives 403, out of scope gives 404, and denials are audited (`on_duty.py:786-796`). The admin list uses `scope_conditions` with `request.*`.
- **Radius leakage**: `employee_out` has no `radius_m` (`duty_locations.py:40-42`). `OUTSIDE_DUTY_LOCATION` only carries `within_fence:false` (`on_duty.py:85-89`).
  The employee `Visit` and `/on-duty/today` never contain distance or within-fence. `/admin/employees/{id}/duty-locations` has no radius.
- **Shared fail counter / limiter**: on-duty routes call the office `_punch_limit` + `_run_punch` (`routers/on_duty.py:50-62`), so they share the keys
  `rl:attendance:{id}` and `rl:punchfail:{id}`. `OUTSIDE_DUTY_LOCATION` was added to `PUNCH_FAIL_CODES` (`services/punch.py:28-30`). Refusals before the fence are not counted.
  A user with an open visit gets `VISIT_OPEN` before any fix or fence check, so check-out successes cannot be used to reset the counter during probing.
- **Mock / stale fix / accuracy**: `punch.require_fix`, `enforce_mock` and `enforce_accuracy` are applied to both check-in and check-out, with the same keys as office.
  An accepted mock sets visit `suspicious` and writes the audit entry `on_duty.suspicious_punch`.
- **Selfie replay across tables**: `punch.validate_selfie` checks owner, purpose and freshness, and checks use in both `attendance` and `on_duty_visits`
  (`punch.py:56-71`). There are tests for office-to-visit replay. (See L1 for the race window.)
- **Manager scope on visits**: `/admin/on-duty/visits` needs `attendance.view_team|view_all`. `scope_conditions` limits team viewers to `User.manager_id == actor.id`.
  Distance, accuracy, within-fence and suspicious are null unless the caller has `view_all` (`on_duty.py:463,487-497`). Visit selfies follow the existing `selfie` rule in
  `files.authorize` (owner / `view_all` / direct manager).
- **Encryption**: `check_in_loc_enc`, `check_out_loc_enc` (EncryptedJSON, deferred), `note_enc`, `purpose_enc` and `decision_comment_enc` are all bytea
  (`models/on_duty.py`, migration `0019_on_duty.py:72-112`). The audit rows contain no coordinates or notes. Rejections log only the reason, and diagnostics log only the user id.
- **Admin endpoints**: all `/admin/duty-locations*` endpoints need `duty_location.manage`. Writes are limited to 30/min and the assignments PUT to 10/min, both fail-closed.
  The LIKE search is escaped and bodies use `extra=forbid`. Create, update, delete and assignments are audited, and assignment entries record user ids only.
- **Export**: the `On Duty` sheet has no coordinates, distance, accuracy, selfies or mock/suspicious fields. `Note` is only included with `export.sensitive`.
  Cells go through `xlsx.sanitize_cell` (formula prefixes `= + - @` and full-width variants are escaped). The type permission is `attendance.view_all|view_team`, and the scope is applied.
- **Push**: `on_duty_decision` and `new_request` carry only the date range or the requester's name. There is no push on check-in or check-out.
- **Licence read_only**: the generic `enforce_not_read_only` in `core/rbac.py` covers the new POST routes.
- **Background location (current state)**: `AndroidManifest.xml` has only `ACCESS_FINE_LOCATION` and `ACCESS_COARSE_LOCATION`. There is no `ACCESS_BACKGROUND_LOCATION` and
  no `FOREGROUND_SERVICE`. `Info.plist` has only `NSLocationWhenInUseUsageDescription` and no `UIBackgroundModes`. The Dart code has only
  `Geolocator.getCurrentPosition` (`features/attendance/data/location_service.dart:108`) and no stream API. **The Flutter On Duty code does not exist yet, so this needs a re-check.**
- **Console CSP**: `deploy/nginx.conf:165` is unchanged (img-src self blob: data:, connect-src self). There is no tile-server origin and `deploy/` has no changes.

## Medium
- **M1. Position and the mock flag come from the client, and the exact site coordinates are handed out.** `on_duty.py:319-328`, `punch.py:107-109`,
  `duty_locations.py:40-42`. **Attack:** an employee uses a modified or rooted client, or a mock-location app that hides `isMocked`. They read the exact
  `lat`/`lng` of an assigned site from `/on-duty/locations` and send it with `is_mock_location:false`, a fresh `fix_time` and `accuracy_m:5`. They get checked in at
  a client site from home, and the day counts as worked for payroll. This is the same trust model as office punches (O7 accepted the coordinate exposure),
  but On Duty makes it worse: there can be many sites, nobody on site sees the person, and there is no selfie by default.
  **Fix (flag, do not reject):** set `suspicious` and audit when (a) the distance to the site centre is under about 1 m, or the fix exactly matches the site coordinates; (b) the same
  lat/lng is repeated across punches or days; (c) `accuracy_m` is 0 or the same value every time; (d) travel between consecutive punches is impossible: visit A check-out position/time
  compared with visit B check-in position/time, or the office punch compared with the visit, at more than about 150 km/h (both points are already stored encrypted and can be compared server-side).
  Show `suspicious` in the HR visits list (already exposed to `view_all`). Recommend `on_duty.selfie_required=true` for deployments where this matters.
  Longer term: Play Integrity / App Attest on punch endpoints.
- **M2. A `duty_location.manage` holder (HR) can create attendance for any employee, including themselves.** `duty_locations.py:199-223,296-360`.
  **Attack:** an HR user creates a "branch" at their own home (or a friend of theirs) with `radius_m=2000`, then either assigns themselves or sets `assign_mode=all`. They then
  check in on duty from home every day. That is presence and payroll days with no on-site check. The v1 plan does not let HR edit attendance (O9),
  so this is a new path that creates presence. Only an audit trail exists: there is no second person and no alert.
  **Fix:** (1) when the actor is in the added user ids, or the location is `all`, store `self_assigned:true` in the `duty_location.assignments` audit entry and refuse it
  unless the actor also has an admin-only permission (or a second person approves). (2) Send an `admin_alert` push to admins when a location is created or its
  coordinates or radius change. (3) Consider a lower radius cap for `assign_mode=all`. (4) Add a dashboard tile for locations created or changed in the last 7 days.

## Low
- **L1. Selfie single use is only enforced in the application across tables.** `punch.py:56-71`, `0019_on_duty.py:127-128` (unique per column only).
  **Attack:** an office check-in and an on-duty check-in fired at the same time with the same selfie both pass the SELECT check before either commits, and there is no
  DB constraint that spans tables, so one photo backs two punches. The impact is limited because it is a fresh selfie of the user.
  **Fix:** in the punch transaction, run `UPDATE files SET used_at=now() WHERE id=:id AND used_at IS NULL` and refuse when 0 rows are updated. Alternatively use a `selfie_uses(file_id PK)` table.
- **L2. Visit data that the retention job misses.** `services/attendance.py:744-753` only nulls `*_loc_enc`. `note_enc` (free text that may be personal) is kept
  forever. `check_in/out_distance_m` and `*_accuracy_m` are kept forever in clear. Together with the public coordinates of the site, they give a derived location ring for each punch.
  **Fix:** at the same `attendance.location_retention_days` cutoff, also null `check_in_distance_m`, `check_out_distance_m` and the accuracy columns (keep `check_out_within_fence`).
  Null `note` at the same cutoff or at `leave.reason_retention_days`, and document this in the contract.
- **L3. Check-out is measured against the current coordinates of the location.** `on_duty.py:392-405`. A manager who edits the lat/lng/radius of a site while visits
  are open changes the `within_fence` and distance of those visits. Moving the reference point and reading `check_out_distance_m` also tells them how far the employee is from any
  chosen point. The edit is audited. **Fix:** snapshot `lat`/`lng`/`radius_m` on the visit at check-in and measure check-out against the snapshot. The contract
  currently says "current", so this needs a contract change.
- **L4. Silent probing signals.** `DUTY_LOCATION_UNAVAILABLE` (`on_duty.py:296`) and the `visit_id` mismatch 404 (`on_duty.py:390`) are neither audited nor
  counted. UUIDs cannot be guessed, so the risk is low. **Fix:** audit them as `attendance.punch_rejected` with reason (not counted) so id walking shows up.

## Info
- Radius bisection through the check-in success/fail oracle is still possible at 5 failures per 600 s (about 720 probes a day), the same as office. The punch limiter and fail gate
  fail open when Redis is down (`core/rate_limit.py:18-19`), which is existing behaviour. Consider `fail_closed=True` for `rl:attendance:*`.
- Successful visit check-in and check-out are not audited, the same as office. The visit row is the record.
- An accepted mock on a visit sets `on_duty_visits.suspicious` but not `attendance.suspicious`. No consumer reads `attendance.suspicious` today, but any future
  "suspicious days" view should OR the two.
- The office check-out `VISIT_OPEN` check (`attendance.py:406-416`) runs before the attendance row lock, so a visit check-in that lands at the same moment can slip past it. This is data integrity only.
- In the check-in `IntegrityError` handler (`on_duty.py:374-379`), any integrity error other than the open-visit index is reported as `SELFIE_REUSED`. This is not a security issue, but it can hide bugs.

## Not implemented / not reviewed (code in flux)
- **Flutter is not implemented**: nothing under `frontend/` has changed. There is no `features/on_duty`, no admin locations or assignments screens, no console pages, and no coordinate paste parser.
  Re-review when it lands, covering: manifest/plist still free of background permission or foreground service; no `getPositionStream` or
  `onLocationChanged`; one-shot fix only on screen open, refresh and punch; "Open in Maps" sends site coordinates only; no coordinates, notes or tokens in logs or
  SharedPreferences; the console does not include geolocator; the console has no map tiles and the CSP is unchanged.
- The contract has not been merged into `docs/api-contract.md` yet.
- **Test gaps (backend):** I found no tests for `/admin/duty-locations*` RBAC (non-manage gets 403), the radius being absent from every employee-facing payload, `/admin/on-duty/visits`
  manager scope with nulled fields, or the `On Duty` export sheet columns and formula escaping. `test_on_duty_punch.py`,
  `test_on_duty_requests.py` and `test_migration_0019.py` exist and cover the punch, request and migration rules.
- I did not run the test suite (read-only review).
