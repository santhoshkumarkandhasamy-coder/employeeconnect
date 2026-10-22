# 05 - Food Enrollment

Contract: api-contract section 6. Table: `food_enrollments` (migration 0004), settings `food.*`. ONLY a Yes/No per date; no meal type, no veg/non-veg.

## Rules
- Row per (user, date), `need_food` bool; UNIQUE(user_id,date); upsert on PUT. No row = unanswered (counted as No in totals, shown separately).
- Cut-off (admin setting): `food.cutoff_mode` = `previous_day` (cutoff at `food.cutoff_time` on the day before) or `same_day` (cutoff at time on the day itself). `cutoff_at` computed in office timezone and returned to the client. After cutoff: `FOOD_CUTOFF_PASSED` 409 (server enforced; client disables toggle using `can_change`).
- Allowed dates: today up to `food.max_advance_days` (14) ahead; not past; not holidays or weekly-offs of the user's shift (`INVALID_DATE`).
- Employees on approved full-day leave may still set (no block) but admin list flags `on_leave` (O1).
- Bulk endpoint (set the whole week): per-item results, partial success.
- Admin daily view: count yes/no/unanswered among active employees, list of yes with name/dept, filter by department. Changing the cut-off setting applies to future evaluations only; already-stored rows untouched.
- Admin cannot bypass cutoff through this API; Admin override (edit someone's enrollment after cutoff) via `PUT /admin/food/enrollment` (perm `food.view_all`+`employee.update`)? deferred (O2).
- Export: see 10 (columns Date, Employee ID, Name, Department, Need Food).

## Edge cases
Cut-off exactly at boundary (cutoff_at inclusive-exclusive: change allowed while `now < cutoff_at`); DST not relevant but use tz-aware math; user created today (no earlier rows); user deactivated (excluded from daily counts); setting changed to a time already passed for tomorrow (tomorrow immediately locks); client clock wrong (server decides); concurrent toggles (last write wins, upsert).

## Flutter (features/food)
Food screen: horizontal date strip / list for next 14 days with Yes/No segmented switch per day, cut-off countdown label ("Change before 6:00 PM today"), disabled state after cutoff with lock icon, "Apply to whole week" action. Home card shows today's/ tomorrow's choice. Admin: Food report screen (date picker, counts, list, export button). Optimistic update with rollback on error, cached list.

## Security
Server-side cutoff and date validation; admin list needs `food.view_all`; no PII beyond name/department.

## Tasks
1. database-engineer: migration 0004, index (date).
2. backend-developer: cutoff calculator (unit-testable, injectable clock), enrollment service (single/bulk), my-enrollments, admin daily endpoint, settings validation for `food.*`.
3. flutter-developer: screens above, admin daily screen.
4. security-reviewer: IDOR (user_id never in employee-facing payload), cutoff bypass.
5. qa-tester: cutoff modes before/after/at boundary, invalid dates, bulk partial, counts, RBAC.

Open: O1 flag on-leave employees; O2 admin override after cutoff; O3 default for unanswered (proposed No).
