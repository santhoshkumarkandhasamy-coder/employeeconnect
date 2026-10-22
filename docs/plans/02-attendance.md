# 02 - Attendance

Contract: api-contract section 3. Tables: `attendance`, `late_records` (also used by 04), `holidays` (created here), `files` (selfie). Needs `shifts`, `users.shift_id`, settings `office.*`.

## Business rules
- One check-in and one check-out per user per `work_date` (UNIQUE). `work_date` = date of check-in in office timezone. Server time is authoritative; `client_time` only stored for diagnostics (reject if skew > 5 min? log only).
- Geofence: Haversine distance from office point <= `office.radius_m` else `OUTSIDE_GEOFENCE`. Reject `accuracy_m > attendance.max_accuracy_m` (100). Mock-location flag from client logged, not trusted. Check-out geofence only if `attendance.checkout_geofence`.
- Selfie optional unless `attendance.selfie_required`; uploaded first via `/files`, referenced by id; must belong to caller and purpose `selfie`.
- Late marking (on check-in, same transaction): `late` if `check_in_at > shift_start + grace`; `minutes_late = check_in - shift_start` (whole minutes, floor). Creates `late_records` row. Only on shift working days that are not holidays and not covered by approved leave. If an approved permission request covers shift start through `to_time`, check-in at or before `to_time + grace` is not late.
- Half-day: if `worked_minutes` at check-out < half of shift length (configurable `attendance.half_day_minutes`, default 240) status becomes `half_day`.
- Approved full-day leave on the date -> `ON_LEAVE` 409. Half-day leave allowed to check in.
- Holiday / weekly-off check-in allowed, status `present`, never late (overtime).
- Auto-close job (00:30 office time): records with no check-out get `auto_closed=true`, `check_out_at` null, worked_minutes null (flagged for HR).
- Calendar statuses computed: record -> its status; else holiday > weekly_off > on_leave (approved) > absent (past working day) > future.
- Users without a shift -> `NO_SHIFT_ASSIGNED`.

## Edge cases
Double tap / retry of check-in (idempotent: second call returns 409 with existing record id); night shifts (shift end < start) not supported in v1 (O1); timezone change of setting does not rewrite history; user deactivated mid-day; check-out before check-in time due to clock issues rejected by CHECK; selfie upload fails -> check-in can proceed without it if not required; GPS off -> client blocks with prompt; offline -> check-in not queued (needs server time).

## Flutter (features/attendance)
Attendance screen: big Check-in/Check-out button with state, today's times, distance to office indicator, optional selfie capture (camera, compressed 1024px), permission rationale for location. Calendar tab (month view, colour-coded statuses, tap day -> detail), History tab (paginated list), month summary card. Team view (manager/HR) inside admin later (list by date). Cache last calendar/history.

## Security
Location encrypted at rest; selfie access restricted to owner and `attendance.view_*` holders; rate limit 10/min; image re-encode strips EXIF; audit not needed for self actions.

## Tasks
1. database-engineer: migration 0002 attendance, late_records, holidays (+ admin holiday seed none); indexes.
2. backend-developer: geo util, shift/holiday helpers (`working_day(user,date)`), attendance service, routers, calendar/summary, team endpoint scope, auto-close job + worker skeleton, holidays read endpoint (CRUD in 09).
3. flutter-developer: location service, repository, providers, screens, selfie capture.
4. security-reviewer: geofence spoofing, file access, location encryption.
5. qa-tester: late boundary tests (exactly at grace, +1 min), geofence edge, duplicate check-in, holiday/leave cases, calendar, team scope.

Open: O1 night shifts; O2 allow multiple sessions/day (proposed no); O3 treat mock-location as hard reject (proposed log + flag).
