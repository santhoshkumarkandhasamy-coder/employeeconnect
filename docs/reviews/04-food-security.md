# 04 - Food enrollment + Excel export: security review

Date: 2026-10-21. Status: read-only review (no code changed).

## Scope

The leave code was being edited at the same time and is not part of this review.

**Backend:**
- `backend/app/services/food.py`
- `backend/app/services/xlsx.py`
- `backend/app/services/food_export.py`
- `backend/app/services/settings.py`
- `backend/app/routers/food.py`
- `backend/app/routers/admin_settings.py`
- `backend/app/routers/admin_export.py`
- `backend/app/schemas/food.py`
- `backend/tests/test_food.py`

**Supporting code read for context:**
- `core/rbac.py`
- `core/rate_limit.py`
- `core/audit.py`
- the `services/attendance.py` helpers
- `main.py` (headers)

**Frontend:** `frontend/lib/features/food/**` and `core/cache/json_cache.dart`.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 0 |
| Medium   | 5 |
| Low      | 9 |

I found no Critical or High issues. The employee-facing paths are solid:
- The user id always comes from the token, and every schema uses `extra="forbid"`. There is no IDOR or mass-assignment path.
- `need_food` is a `StrictBool`.
- Cut-off, past dates, the max-advance limit, holidays and weekly-offs are all checked on the server against server time. The client clock is not used for these checks.
- The bulk path applies the same per-item validation as the single path and rejects duplicate dates.
- Admin daily requires `food.view_all` (HR and Admin).
- Settings require `settings.manage` (Admin only).
- The export team scope is forced on the server. The client cannot choose it.
- The export rate limit fails closed.
- `nosniff` and `no-store` are set globally (`main.py:41-42`).
- The download filename is built only from a `Literal` type and parsed dates, so it cannot be injected.

---

## Medium

### M1. Export overflow and error paths leave a plaintext PII temp file on disk, and repeated exports can fill the disk

**Where:**
- `backend/app/services/xlsx.py:52`, `:101-105`
- `backend/app/routers/admin_export.py:52-55`

**Problem:**
- openpyxl write-only sheets stream rows into `NamedTemporaryFile(delete=False)` (`openpyxl/worksheet/_writer.py:create_temporary_file`). Only `wb.save()` removes that file.
- When `OverflowError` is raised after 200,000 rows, `wb.save()` never runs. The same happens on any other exception: a DB error, or `CancelledError` when the client disconnects during the build.
- The file keeps the rows written so far: employee id, name, department and food choice. It also keeps an open handle.
- openpyxl removes these files only in an `atexit` hook. That hook never runs in a long-lived worker or after SIGKILL.

**Verified locally:** a 5-row cap with 20 rows left an `openpyxl.*` file in the system temp directory, and the `atexit` cleanup failed. I deleted the test file afterwards.

**Impact:**
- PII stays at rest in `/tmp` of the API container.
- A user with `export.all` (HR or Admin) can repeat an over-limit export 5 times a minute. Each call writes up to 200k rows before failing, which can exhaust disk space.

**Fix:**
- Wrap the build in `try/finally`. On failure, close and delete the workbook writers' temp files, for example the paths in `openpyxl.worksheet._writer.ALL_TEMP_FILES` that this call created.
- Better: count rows first with `SELECT count(*)` using the same filters, and reject before writing anything.
- Point `tempfile.tempdir` at a dedicated tmpfs or volume with a size quota.

### M2. Export runs synchronous CPU and IO on the event loop, with no time budget and no global concurrency cap (DoS)

**Where:**
- `backend/app/services/xlsx.py:49-109`: `emit`, `ws.append` and `wb.save` are synchronous inside an `async def`.
- `backend/app/routers/admin_export.py:51-53`

**Problem:**
- A 200k-row export blocks the worker's event loop for seconds. While it runs, every other request on that worker stalls, including login, punches and trip location uploads.
- The 5/min limit is per actor. Several HR or Admin accounts, or one stolen HR token used in parallel within its quota, can stall all workers.
- `plans/10-export.md` requires a 60 s timeout. It is not implemented.

**Fix:**
- Build the workbook in a worker thread (`anyio.to_thread.run_sync`), first fetching rows in batches, or in a background job.
- Guard it with a process-wide semaphore (for example, 2 concurrent exports) plus a Redis global limit.
- Enforce the 60 s budget with `asyncio.timeout`, mapped to `RANGE_TOO_LARGE`, and still clean up as in M1.

### M3. Extending `SHEETS` can silently drop the team scope, and export has no per-type permission

**Where:**
- `backend/app/routers/admin_export.py:25`, `:50-51`
- `backend/app/services/food_export.py:49`

**Problem:**
- Team scope is passed only as an optional keyword (`manager_id=scope`) to each sheet factory. Most factories will take `**filters`, as `food_sheet` does.
- A future attendance, leave or permission factory that forgets to apply `manager_id` would hand a manager (`export.team`) the whole organisation's data. No error or test would catch it.
- `type="all"` will combine several factories, which multiplies the chance of this.
- There is no per-type permission. Anyone with `export.team` gets every registered type.

**Fix:**
- Resolve scope centrally. Compute the allowed set of user ids in the router, for example `select(User.id).where(User.manager_id == actor.id)`, and pass it as a required positional scope argument. Every factory must then apply `WHERE user_id IN (subquery)`.
- Add a registry test that runs each registered type as a manager and asserts that no out-of-team rows appear.
- Optionally map each type to a required permission (for example, `attendance` needs `attendance.view_team|view_all`).
- `plans/10-export.md` also requires 403 when a team user filters on an `employee_id` outside the team, and 404 for unknown department or employee ids. Today the export silently returns an empty sheet. That is safe but does not match the contract (see L6).

### M4. Settings validation has no ranges for non-food keys

**Where:**
- `backend/app/services/settings.py:30-43`
- `backend/app/schemas/food.py` (`SettingsIn.settings: dict[str, bool|int|float|str]`)

**Problem:**
- The food keys are validated well: mode is an enum, time is `HH:MM`, `max_advance_days` is an int between 1 and 60, and bool is rejected.
- Every other int or float key accepts any number, including negative values, floats for integer keys, and `Infinity` or `NaN`. Python's `json.loads` and pydantic accept these by default. Postgres JSONB then rejects them, which produces a 500.
- A compromised or mistaken Admin request can, for example:
  - set `office.radius_m=1e9`, which disables the geofence;
  - set `attendance.location_retention_days=-1` or `0`, which keeps location data forever and defeats retention;
  - set `trip.location_interval_s=0.01`, which drains device batteries and floods ingest;
  - set `attendance.max_fix_age_s` very large, which accepts stale fixes;
  - set `attendance.reject_mock_location=false` with no confirmation;
  - set `permission.*` limits to negative values.

**Fix:**
- Add a per-key validator table with type, min, max and an integer-only flag for every `KNOWN` key, the same way `validate_food_setting` does it.
- Reject non-finite floats (`math.isfinite`).
- Consider requiring re-authentication, or a second Admin, for the retention and geofence keys.

### M5. Settings audit hides the values of security-relevant keys

**Where:**
- `backend/app/core/audit.py:16-19`, called from `backend/app/services/settings.py:68`

**Problem:**
- `redact()` masks any dict key that contains `location` or `token`. The before/after dicts are keyed by setting name, so changes to `attendance.location_retention_days`, `trip.location_interval_s`, and any future token TTL setting are stored as `***`.
- The audit trail therefore cannot show who shortened or disabled location retention, which is the change it most needs to show.

**Fix:**
- Audit settings changes as a list of objects with key, before and after fields, so the key names are values rather than dict keys.
- Or add an allow-list that skips redaction for `settings.update`. No setting value is a secret today.

---

## Low

### L1. Extreme dates raise an unhandled `OverflowError` (500)

**Where:**
- `backend/app/services/food.py:94`, `:200`, `:245` (`compute_cutoff` does `day - 1 day`; `my_enrollments` does `d_from + 13`)

**Problem:**
- `GET /food/my-enrollments?from=0001-01-01` raises `OverflowError: date value out of range`, and so does `from=9999-12-31`.
- `GET /admin/food/daily?date=0001-01-01` does the same.
- The generic handler turns this into a 500. There is no data exposure, but these requests add noise to error alerting.

**Fix:** Bound the accepted dates, for example to `today +/- 400 days` for `my-enrollments`, and to a sane window for daily and export, returning `INVALID_DATE`.

### L2. Requests at the cut-off boundary are decided at request start

**Where:** `backend/app/services/food.py:168-172`

**Problem:**
- `now` is read once before validation and the upsert. A request that arrives a few milliseconds before `cutoff_at` commits slightly after it. This is acceptable and follows the documented inclusive-exclusive rule.
- `office.timezone` is not part of the cut-off key. An Admin who changes the timezone can reopen or close days immediately. That is an Admin action, it is audited, and it is noted here only for completeness.
- A `cutoff_time` inside a DST gap resolves with `fold=0`, which makes it about 1 hour late. This does not apply to Asia/Kolkata.

**Fix:**
- Optionally re-read `utcnow()` immediately before the upsert, or put the check in the UPDATE as `WHERE now() < :cutoff`.
- Reject `cutoff_time` values that are non-existent in the office timezone.

### L3. Formula sanitiser is correct for xlsx but escapes more than it needs to, and is not ready for CSV

**Where:** `backend/app/services/xlsx.py:32-38`

**What is already safe:**
- In an `.xlsx` file, only strings that openpyxl turns into formulas are dangerous: length above 1 and starting with `=`, which openpyxl gives `data_type='f'`.
- The prefixes `=`, `+`, `-`, `@`, tab, CR and LF are all prefixed with an apostrophe. Control characters are stripped before the prefix check, so a control character followed by `=` cannot bypass it.
- The `=HYPERLINK` and `@SUM` tests pass.
- Full-width look-alikes such as U+FF1D and leading spaces are stored as text cells and are not evaluated by Excel or LibreOffice.

**Gaps:**
1. The apostrophe is a literal character in openpyxl, not Excel's quote-prefix. Values such as `-` or `+91` phone-style strings are shown with a visible apostrophe.
2. Strings equal to Excel error codes (`#N/A`, `#REF!`) become error cells.
3. XML-illegal non-control characters (U+FFFE, U+FFFF) are not stripped and would corrupt the file.
4. There is no 32,767-character truncation. The plan specifies 32,000, and the leave reasons coming in a future sheet will need it.
5. A future CSV export must not reuse this as-is. CSV also needs handling for leading whitespace followed by a formula prefix, the full-width equivalents of `= + - @` (U+FF1D, U+FF0B, U+FF0D, U+FF20), and separator quoting.

**Fix:**
- Set `cell.data_type = "s"` explicitly for all `str` values. Optionally add a `quotePrefix` style instead of the literal apostrophe.
- Strip U+FFFE, U+FFFF and lone surrogates.
- Truncate cells to 32,000 characters.
- Keep a separate CSV sanitiser for any future CSV export.

### L4. Downloaded exports can stay on the device after logout

**Where:** `frontend/lib/features/food/data/food_repository.dart:152-160`

**Problem:**
- `exportStoreProvider` registers its logout/lock listener only when the provider is first read, which happens on the first export of the session.
- If the app is killed after an export, the next session starts with `ec_exports/` still in the cache directory. Logging out in that session will not clear it unless the user exports again. Session-expiry logouts have the same problem.
- The files are in the app-private cache, so this is not reachable by other apps.

**Fix:**
- Clear `ec_exports/` once at app start and on every login.
- Instantiate the provider eagerly, for example from the auth controller.
- Optionally delete each file after it has been opened or shared.

### L5. Admin daily list with `include=all` is unbounded

**Where:** `backend/app/services/food.py:270-278`, `backend/app/routers/food.py:56-66`

**Problem:** `include=all` returns every active employee in one response, with no pagination. It is rate-limited to 60/min, so the risk is limited to load and a large personal-data payload in one response.

**Fix:** Paginate (`limit`/`cursor`), or cap the list and keep the counts as aggregates.

### L6. Export contract gaps

**Where:** `backend/app/routers/admin_export.py:56-69`, `backend/app/services/food_export.py:20-46`

**Problem:**
- The audit row has no row count. The plan requires "filters and row counts".
- There is no 404 for unknown `department_id`/`employee_id`, and no 403 when a team user filters on someone outside the team. Both currently return an empty sheet, which leaks nothing.
- The audit is committed before streaming starts, so an interrupted download is still logged as a success. That is acceptable.

**Fix:**
- Have `food_rows` and future generators report the count, and write it into the audit.
- Validate the filter ids, and return 403 for out-of-team `employee_id`.

### L7. Read access to people lists is not audited, and neither are employee toggles

**Where:** `backend/app/routers/food.py:56-66`

**Problem:**
- HR or Admin viewing the daily food list leaves no audit trail.
- Employee toggles are not audited by design (`test_audit_not_written_for_employee_puts`), but `updated_by`/`updated_at` are stored.
- This is acceptable for low-sensitivity data. I note it in case "admin actions" is read to include bulk reads.

**Fix:** Optionally audit `food.daily.view` with the date and filters.

### L8. Rate limits on non-export endpoints fail open

**Where:**
- `backend/app/routers/food.py:30, 41, 52, 65`
- `backend/app/routers/admin_settings.py:30`

**Problem:** These limits fail open when Redis is down. This matches decision O1 and the endpoints are cheap. `GET /admin/settings` has no limit at all.

**Fix:** None required. Consider `fail_closed=True` for `PUT /admin/settings`.

### L9. Holidays and weekly-offs added after enrollment still count as "Yes"

**Where:** `backend/app/services/food.py:242-306`, `backend/app/services/food_export.py:20-46`

**Problem:**
- Rows stored before a holiday is declared still count as Yes in the daily totals and in the export.
- Employees on a weekly-off are counted as "unanswered".
- This is mainly a data-correctness issue. It could cause meals to be over-ordered.

**Fix:** Exclude holiday dates and weekly-off users from the counts, or flag them in the daily view and the export.

---

## Checked and fine (no finding)

- **IDOR and mass assignment:**
  - `routers/food.py:19` takes the user id from the token.
  - `EnrollmentIn`/`BulkIn` use `extra="forbid"`, and there is no `user_id` field. `test_employee_only_touches_own_rows` covers this.
- **Cut-off:**
  - The cut-off is computed with the server clock and tz-aware wall-clock arithmetic (`food.py:91-106`).
  - Changes are allowed while `now < cutoff_at`.
  - Tightening or loosening the setting takes effect on the next request (no caching), and stored rows are not touched.
  - The client's `serverOffset`/`can_change` values are used only for display.
- **Past, holiday and leave days:**
  - Past dates and dates beyond `max_advance_days` are rejected.
  - Holidays and weekly-offs are rejected on both the single and the bulk path.
  - Enrolling on an approved leave day is allowed by design (plan O1), and the admin view shows an `on_leave` flag.
- **Bulk:** limited to 1 to 31 items with no duplicate dates, each item fully validated, partial success, rate limited.
- **RBAC:**
  - Daily requires `food.view_all` (Admin and HR).
  - Settings GET and PUT require `settings.manage` (Admin only). A manager or HR user cannot change the cut-off.
  - Export requires `export.all|export.team`, with team scope forced on the server (`admin_export.py:50`).
- **Settings food validators:** enum mode, strict `HH:MM`, int between 1 and 60 with bool rejected. Corrupt stored values fall back to the defaults (`food.py:76-81`).
- **SQL injection:** all queries are SQLAlchemy expressions. `employee_id` is bound and limited to `max_length=32`.
- **Export headers:**
  - Correct xlsx MIME type and an `attachment` disposition with a constrained filename.
  - Global `nosniff` and `Cache-Control: no-store`.
  - The client sanitises the filename again (`safeXlsxName`).
- **Export data minimisation:** only Date, Employee ID, Name, Department and Yes/No. The audit row holds the filters and no personal data.
- **Frontend:**
  - The cut-off comes from the server (`cutoff_at`, `can_change`), and the server enforces it on every change.
  - The admin daily report is `autoDispose` and is not cached on disk.
  - The `my-enrollments` cache is scoped per user and cleared on logout and lock.
  - Downloads go through the authenticated Dio client into app-private storage.

## Top issues

1. **M1:** export failures leave plaintext PII temp files in `/tmp`, and repeated over-limit exports can fill the disk (`xlsx.py:52,101-105`).
2. **M2:** export blocks the event loop, with no 60 s budget and no global concurrency cap (`xlsx.py:49-109`).
3. **M3:** the `SHEETS` registry relies on each factory applying `manager_id`. A future sheet can leak organisation-wide data to managers (`admin_export.py:25,50-51`).
4. **M4/M5:** non-food settings have no ranges, so retention, geofence and trip interval can be set to unsafe values, and the audit masks the location-related keys (`settings.py:30-43`, `audit.py:16-19`).
