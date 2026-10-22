# 10 - Excel Export

Contract: api-contract section 10. No new tables. Library: `openpyxl` (write-only mode for large sheets). Runs after features 02-05 exist.

## Rules
- Endpoint `GET /admin/export?type=&from=&to=&department_id=&employee_id=`; types all|attendance|leave|late|permission|food. "Export All" in the app calls `type=all` (single workbook, sheets Attendance, Leave, Late, Permission); individual types and Food are separate calls.
- Permission: `export.all` (everyone) or `export.team` (forced filter to direct reports; department/employee filters intersect with team). Employees have no access.
- Validation: `from`,`to` required, `to>=from`, range <= 366 days; department/employee ids must exist (404).
- Sheet columns:
  - Attendance: Date, Employee ID, Name, Department, Shift, Check-in, Check-out, Status, Minutes Late, Worked Hours, Within Geofence. (Raw coordinates NOT exported.) Optionally rows for absent days (computed) with status Absent/Leave/Holiday (O1).
  - Leave: Request ID, Employee ID, Name, Department, Leave Type, From, To, Days, Half Day, Status, Reason, Approver, Decision Comment, Applied On, Decided On. Requests overlapping the range included.
  - Late: Date, Employee ID, Name, Department, Shift Start, Check-in, Minutes Late.
  - Permission: Request ID, Employee ID, Name, Department, Date, From, To, Minutes, Status, Reason, Approver, Comment.
  - Food: Date, Employee ID, Name, Department, Need Food (Yes/No/Not answered).
- Formatting: bold frozen header row, auto column widths, date/time cells as real Excel dates in office timezone, filter on header, sheet names fixed. Empty -> header-only sheet.
- Security: strip/escape formula starters (`=+-@` and tab/CR) in text cells; `filename` sanitised; export audit logged with filters and row counts; sensitive personal fields (DOB, phone, address, coordinates) never included.
- Performance: stream via query yields (server-side cursor), expected <= ~50k rows; timeout 60 s; if exceeded return 422 `RANGE_TOO_LARGE`. Synchronous generation in v1; async job + download link is O2. Rate limit 5/min/user.
- Response is a streamed file (`StreamingResponse`) with proper headers; no caching (`Cache-Control: no-store`).

## Edge cases
Deactivated employees included for historical ranges; employee filter with `export.team` outside team -> 403; department with no data; timezone boundaries (range inclusive in office tz); names starting with `=`; huge reason texts truncated at 32,000 chars (Excel cell limit); concurrent exports; Flutter download interrupted.

## Flutter (admin/reports)
Reports & Export screen: date range picker (presets: this month, last month), department dropdown, employee picker, buttons "Export All" and individual (Attendance, Leave, Late, Permission, Food). Download via Dio to app temp dir with progress, then open with share sheet/`open_filex`; error handling for 403/422; file deleted on app logout. Android scoped storage via share/save dialog.

## Tasks
1. database-engineer: review query plans/indexes for date-range scans (attendance.work_date, leave from/to, permission date, food date); no migration unless needed.
2. backend-developer: export service with per-sheet query builders reusing scope helpers, openpyxl writer, sanitiser, router with perms + audit, size/range guards.
3. flutter-developer: reports screen, download + open/share flow.
4. security-reviewer: formula injection, scope bypass via ids, data minimisation, audit.
5. qa-tester: pytest opening workbook with openpyxl (sheet names, headers, row counts, filters, scope for manager vs HR), injection test, empty range, >366 days, RBAC; Flutter widget test for filter form.

Open: O1 include computed absent rows; O2 async export for large ranges; O3 PDF export (out of scope).
