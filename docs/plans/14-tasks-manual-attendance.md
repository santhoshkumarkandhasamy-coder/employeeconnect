# 14 - Task reminders (calendar) and manual attendance

Status: IMPLEMENTED. Contract: api-contract section 15. Migration: `0020_tasks_manual_attendance`.

## Task reminders
- Table `tasks`: assignee `user_id`, `created_by`, `title` (1-150), `notes_enc` (encrypted, <= 2000), `due_at`, `remind_at`
  (`due_at - remind_minutes_before`, 0..10080 min, null = no reminder), `reminded_at`, `done_at`.
- Employees create / edit / complete / delete their own tasks. `task.assign_team` (manager, hr, admin) assigns to direct
  reports, `task.assign_all` (hr, admin) to any active employee. The assignee can only mark an assigned task done or not
  done; the creator edits and deletes it. Anybody else gets 404.
- Pushes: `task_assigned` to the assignee, `task_done` to the creator when the assignee completes it, `task_reminder` from the
  worker job `tasks.reminders` (every minute, stamps `reminded_at` first, so it fires once). Changing the due time or the
  reminder re-arms it; a reminder time already past when saved is not pushed.
- Phone reminders: the app has no push receiver yet (no Firebase in the app), so reminders are ALSO scheduled on the phone
  with `flutter_local_notifications` (open tasks of the next 60 days, max 60, re-synced when the app opens and after every
  task change, cleared on logout / wipe). Android: `POST_NOTIFICATIONS`, `RECEIVE_BOOT_COMPLETED`, the plugin's receivers,
  core library desugaring; inexact scheduling (no exact-alarm permission), so a reminder can be a few minutes late in deep
  sleep.
- App: More -> "Tasks & reminders" and a Home quick action. Month calendar with a badge of open tasks per day, the selected
  day's list with a done checkbox, add / edit form (title, notes, date, time, reminder, "Assign to" for managers / HR).
  Managers / HR also get "Assigned by me".

## Manual attendance
- `GET/PUT/DELETE /admin/attendance/{user_id}/{work_date}`, permission `attendance.manual` (hr, admin). Nobody can enter or
  change their own attendance (403).
- PUT `{check_in "HH:MM", check_out "HH:MM"|null, reason 3-300}` creates or corrects the day. Rules: date not in the future
  and at most 366 days back; times not in the future; check-out after check-in (night shift: next morning); refused on an
  approved full-day leave (409 `ON_LEAVE`). Lateness (shift start + grace, approved permission / half-day leave), half day
  (`attendance.half_day_minutes`) and worked time are computed exactly like punches; the late record is rebuilt.
- Correcting a real punch keeps its location / selfie evidence. DELETE only removes manual entries that never had a real
  punch (409 otherwise).
- Columns `attendance.manual`, `manual_by`, `manual_reason_enc` (encrypted). Audit `attendance.manual` /
  `attendance.manual_delete` with before/after times and status (never the reason text). Push `attendance_manual` to the
  employee. Records carry `manual: true`; the reason is only returned by the admin endpoint.
- Export: Attendance sheet column `Manual Entry` (Yes/No).
- App: Admin -> "Manual attendance": pick employee (search), date, see the current record, set times and reason, save or
  remove. The employee's history shows "Manual entry by HR".

## Announcements access (review of the reported problem)
The API and the app screens were checked end to end against a live server (employee list / detail / unread count, admin
create). Posting needs `announcement.manage` (admin, hr). It was only reachable from More -> "Manage announcements"; it is
now also on the Admin screen ("Announcements").
