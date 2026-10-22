# 04 - Late Details

Contract: api-contract section 5. Data: `late_records` (created in 0002, written by attendance check-in). No new tables.

## Rules
- Record created only by check-in logic (02): late if check-in > shift start + grace; `minutes_late` counts from shift start. Unique per user/day.
- Monthly summary: count, total minutes, average, per-day list; month boundaries in office timezone.
- Manager/HR/Admin: `late/team` with scope (`late.view_team` direct reports, `late.view_all` everyone), filters department/employee/date range.
- Records are immutable system data; HR corrections (excuse a late) are out of v1 (O1). If a late-causing check-in is later excused by an approved permission, do not rewrite; rule in 02 prevents it up front.
- Backfill: if shift grace changes, old records unchanged (snapshot of `grace_minutes`, `shift_start`).

## Edge cases
No records -> empty state with zeros; month with future dates -> only past; user switched shift mid-month (use snapshot fields); deactivated users remain in history/exports; page size limits.

## Flutter (features/late)
Late screen: month selector, summary card (count, total, average), list of late days with check-in time and "x min late". Team late list under admin reports for managers. Cached.

## Security
Self data only for employees; scope filtering server-side; RBAC tests.

## Tasks
1. database-engineer: confirm indexes (user_id, work_date), (work_date); no new migration unless missing.
2. backend-developer: late service (list, summary, team), router, schemas; reuse in export.
3. flutter-developer: late feature screens/providers.
4. security-reviewer: scope checks on `/late/team`.
5. qa-tester: summary math, month boundaries, scope tests, grace boundary integration with check-in.

Open: O1 HR excuse/override of a late mark (proposed v2).
