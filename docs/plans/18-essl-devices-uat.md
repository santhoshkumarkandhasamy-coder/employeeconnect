# Plan 18 acceptance test: check every function before the December start

Go-live for attendance from the machines: 1 December 2026. Run this after the release (see `18-essl-devices-deploy.md`) and before December. Tick each line, note the date and what you saw. A failed line is a bug to report, not something to work around.

Rule for the whole test: nothing is ever deleted on a machine, and nobody clears a machine's memory.

## A. Machines and connection (console, Attendance devices)
- [ ] Each of the 3 machines shows the right name, IP, serial number, model and firmware. Serial numbers: `.201` A6FE201561197, `.202` NFZ8260505793, `.203` NFZ8251103508.
- [ ] **Test connection** works for each machine; a wrong IP (try a spare address in the allowed range) fails with a clear message.
- [ ] **Sync now** works; the sync history shows fetched, inserted and quarantined counts; a second sync right after inserts 0 new punches.
- [ ] The scheduled sync runs by itself (a new row appears about every 5 minutes between 05:00 and 23:30, hourly outside).
- [ ] Clock status: `.202` is about 6 minutes slow and shows a clock warning with an offset; after you correct that machine's clock the status goes back to OK.
- [ ] Unplug or power off one machine for 15 minutes: the device shows offline, an admin alert is sent once, and it recovers by itself when it is back.
- [ ] Switch a machine off in the console (disable): syncing stops; switch it on again: syncing resumes.

## B. History (decided: previous punch records are kept)
- [ ] `.201` was added with "Use punches from" 2024-03-25: after the first sync the punch log shows punches from March 2024 onward (about 29,000), nothing is missing or doubled.
- [ ] Same for `.203` (from 2025-08-29) and `.202` (from 2026-08-07).
- [ ] The record counts in the console match the machine's own count.
- [ ] Old attendance days are NOT changed by the import (open a few old days in Attendance: no new check-in or check-out appears from history).
- [ ] After you map a device user to an employee, that employee's old punches show under their name in the punch log.

## C. Matching users to employees (Unmatched users)
- [ ] Before the codes are changed, the 41 `E…` users and `ACA028`, `ACA035` show as unmatched; `ACA001` is matched.
- [ ] After you change the employee codes by hand, the next sync (or the nightly recompute) matches them automatically.
- [ ] Manual map: choose an employee for one device user (all machines, then this machine only) and check the punches follow.
- [ ] Ignore a device user: their punches stop counting; reset to automatic brings them back.
- [ ] An employee enrolled on two machines (for example `E003` on `.201` and `.203`) is one person: their punches from both machines combine into one day.
- [ ] A person who left (inactive user) is not matched and creates no attendance.

## D. Attendance from the machines (the main function)
Use real people, a normal working day, and compare with eTimeTrackLite for the same day.
- [ ] First punch of the day becomes the check-in, last punch the check-out, with the right times (compare with the machine's own record).
- [ ] The employee sees the punches in the app under My punches and in the attendance record; the record shows the source (machine name).
- [ ] Late and early-leave marks follow the shift rules as for app check-ins.
- [ ] A day with only one punch: check-in only, the open record closes at the usual auto-close time.
- [ ] A day with approved leave, a holiday or a weekly off: the machine punch does not overwrite the leave (it shows as not applied, with the reason).
- [ ] A day entered manually by HR is not overwritten by the machine (HR manual entry wins).
- [ ] A month already covered by a processed payroll run is not changed by new punches (reason: payroll locked).
- [ ] Night shift (if any): the punch is assigned to the right work date.
- [ ] Two punches by the same person within 5 minutes (`device.checkout_min_gap_minutes`) do not become both a check-in and a check-out.

## E. Safety checks
- [ ] Do NOT change a real machine's clock to test this. The "clock was changed" protection (punches held, alert raised) is covered by the automated tests. Only look at the clock status the console shows.
- [ ] Punch counts on a machine never go down because of us (the count-drop alert stays quiet). If it fires, someone cleared logs on the machine or eTimeTrackLite deleted them.
- [ ] The `api` container cannot reach port 4370 on a machine; only the `worker` can (deploy plan, section 5).
- [ ] A new person enrolled on a machine shows as "needs confirmation" and an alert goes to HR; their punches do not count until confirmed.
- [ ] A new device administrator on a machine raises an alert.

## F. Roles and screens
- [ ] Admin: all device screens; HR: unmatched users and punch log; employee: only My punches and the privacy notice.
- [ ] Employees see no machine details (IP, serial) and no one else's punches.
- [ ] The Flutter app and the console both load the device screens without errors on the production URL.

## G. Reports, notifications, housekeeping
- [ ] Attendance export includes days that came from the machines.
- [ ] Admin alerts reach the right people (offline, clock, new users, count drop).
- [ ] `GET /admin/system/health` shows the `devices` block with the right counts.
- [ ] After the first night: the 00:20 reconcile and the 04:00 retention jobs ran without errors (worker log).
- [ ] A backup taken after the first sync restores on a test copy (restore drill), and the device tables are in it.

## H. Parallel run, then switch
- [ ] Run 2 to 3 weeks in November with the machines feeding the app while the old way (eTimeTrackLite and the app's own check-in) continues. Compare a sample of days every week; list differences and fix their cause.
- [ ] 30 November: final comparison for the month, then confirm "go" or "no go".
- [ ] 1 December: from now the app is the source of truth for attendance. Keep eTimeTrackLite read-only for one more month as a fallback. Do not clear the machines.

Results go into `docs/reviews` as a short note (date, tester, failed lines) so nothing is lost between sessions.
