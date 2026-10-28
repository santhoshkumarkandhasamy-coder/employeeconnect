# Review 11, pass 2 - On Duty (2026-10-26)

Read-only security review by security-reviewer (second pass). Scope: (1) the Flutter app and console On Duty code, which pass 1 did not cover;
(2) the backend fixes for pass-1 findings M1, M2, L1, L2 and L4. State reviewed: commit `caae0bc` plus the uncommitted working tree
(`duty_locations.py` and `punch.py` have formatting-only changes, plus the new `tests/test_review11_on_duty_fixes.py`). The code was still changing while I reviewed it.

**Result: no Critical, High or Medium findings.** All five backend fixes have landed and are correct in what they do. Two of them
(M1 and M2) were built more narrowly than pass 1 recommended, so some risk remains. The Flutter side meets every privacy constraint that was checked, with one Low exception:
the on-duty request purpose is written to the on-disk offline cache.

## Verified (no finding)

### Flutter
- **No background location.** There are no changes under `frontend/android`, `frontend/ios`, `frontend/web`, `pubspec.yaml` or `deploy/` since `5ea01a0`.
  The manifest has only FINE/COARSE (`AndroidManifest.xml:4-5`) and has `allowBackup="false"`. `Info.plist:71` has only `NSLocationWhenInUseUsageDescription`.
  The Dart code has no `getPositionStream`, `onLocationChanged`, `Timer.periodic` or streams. All location reads go through
  `GeolocatorLocationService.current()` (`attendance/data/location_service.dart:68-86`, one `getCurrentPosition`). Location is read once on panel open or refresh
  (`proximityProvider`, `attendance_providers.dart:121`), once per check-out, and once or twice per check-in (the second read happens after a selfie upload so the fix is fresh,
  `on_duty_providers.dart:173-186`). The admin form's "use my location" also reads once (`device_position_reader.dart`).
- **"Open in Maps"** builds the link from site lat/lng only (`on_duty_models.dart:344`, `maps_launcher.dart:22`) and goes through the existing `share_plus` dependency.
  The employee's position is never included.
- **Logs and storage.** There is no `print`, `debugPrint`, `dart:developer` or Dio `LogInterceptor` anywhere in `lib/`. On Duty uses no SharedPreferences or app-state store.
  `OnDutyRepository` has no cache (`on_duty_repository.dart:15`). Selfie temp files are discarded after upload, on failure and on dispose
  (`on_duty_providers.dart:43-46,159,165`). On Duty calls go through the pinned `dioProvider`.
- **Console bundle.** I traced the import closure of `lib/main_console.dart` at `5ea01a0` and in the working tree. The package set is identical
  (geolocator was already pulled in through `location_service.dart`, which is not new). No map, tile or `share_plus` package was added.
  The console passes no `readPosition` (`console_shell.dart`, `const DutyLocationsScreen()`). `deploy/nginx.conf` CSP is unchanged.
- **UI gating matches the backend for every action:**
  visits `attendance.view_team|view_all` (`duty_admin_models.dart:18`, `routers/on_duty.py:132`); detail fields shown only with `view_all`, and the server also nulls them;
  locations, assignments and employee duty-locations `duty_location.manage` (`routers/admin_duty_locations.py:20`); Requests page
  `request.view_*` / decide `request.approve_*` (`leave_logic.dart:253-261`, `routers/leave.py:119,138`); Settings `settings.manage` (Admin only,
  `routers/admin_settings.py:19,29`). The server checks each of these independently.
- **Reused ApprovalsScreen and SettingsScreen in the console do not widen access.** The permissions and self id now come from `adminPermissionsProvider` and `adminSelfIdProvider`.
  On mobile these read the same auth state as before. In the console they read the console session. The console `LeaveRepository` uses a `MemoryJsonCache`,
  so nothing is written to disk (`console_auth.dart:67-75`). Scope and self-approval are enforced on the server (`leave._scope_problem`).
- **Error messages never show distance or radius.** `onDutyErrorMessage` (`on_duty_models.dart:256-277`) names only the site. `fixRejection` shows only the
  device's own accuracy. The "Nearby" badge uses a fixed 150 m client hint (`on_duty_models.dart:199`) and is not derived from the radius. Showing the distance to the site centre is a hint by design.
- **Null `within_fence`.** `AdminVisit` keeps null as null (`duty_admin_models.dart:286`). The card only shows a chip when the value is `== false`
  (`duty_visits_screen.dart:142`), and there is no positive "within fence" label anywhere, so a null (manager view, unmeasured, or purged) can never appear as "within".

### Backend fixes (landed)
- **L1 (landed, correct).** `punch.validate_selfie` takes `SELECT ... FOR UPDATE` on the file row before the use checks (`punch.py:50-52`). Office check-in/out
  and visit check-in/out all reach their final commit without an intermediate commit (`attendance.py:342-390,435-458`; `on_duty.py:383-437,465-510`), so the lock is
  held until the use row is committed, and a concurrent punch then sees it under READ COMMITTED. See Info I2 for a deadlock edge case.
- **L2 (landed, correct).** `purge_pii` nulls `note`, both distances and both accuracies, together with the coordinates, at `attendance.location_retention_days`.
  `check_out_within_fence` is kept (`attendance.py:744-770`).
- **L4 (landed, correct).** `DUTY_LOCATION_UNAVAILABLE` and `VISIT_ID_MISMATCH` are audited as `attendance.punch_rejected` with the reason only. They are not in `PUNCH_FAIL_CODES`,
  and they stay bounded by the shared punch limiter (`on_duty.py:356,455`).
- **M1 (landed, flag only).** `suspicion_reasons` (`on_duty.py:98-153`) adds `centre_with_static_accuracy`, `repeated_coordinates` and `impossible_travel`
  (at least 500 m and more than 150 km/h against the last 10 visits plus office punches from yesterday onwards). It sets `suspicious` and writes an audit entry with signal codes only, never coordinates. Employees never see the flag (tested).
- **M2 (landed, partly).** Adding yourself, directly or through a department, needs `role.assign`, which is Admin only (`duty_locations.py:381-389`).
  The `all` radius cap defaults to 300 m and is an Admin-only setting (`duty_locations.py:52-62`, `settings.py:84`). The audit entry has `self_assigned`. `notify_admins`
  (names only) runs on create, on a geo change, on self-assign and on a switch to `all` through the assignments PUT.

## Low
- **L5. The on-duty request `purpose` is persisted in clear text in the on-disk offline cache.** `frontend/lib/features/leave/data/leave_repository.dart:67-77` caches
  `/on-duty/requests` under `$scope.leave.mine` through `fetchWithCache`. `scrubSensitive` removes `reason` and `decision_comment` but not `purpose`
  (`frontend/lib/core/cache/json_cache.dart:102-134`). On the server, purpose is encrypted (`purpose_enc`) and purged by retention, but on the phone it is plain JSON in the temp
  dir until logout, lock or the next app start. **Attack:** someone with file access to an unlocked or backgrounded rooted device, or a forensic image, reads free-text purposes
  (client names, sometimes medical or personal context). **Fix:** add `'purpose'` to `sensitiveCacheKeys`. Offline, the list then shows type, dates and status only, the same as leave reasons.
  Add a test in `qa_on_duty_contract_test.dart` that asserts the scrubbed cache has no `purpose`.
- **L6 (M1 residual). An exact replay of the site coordinates is not flagged on first use.** `backend/app/services/on_duty.py:139-142` flags "at the centre" only when
  `accuracy_m == 0` or the accuracy repeats an earlier one. `test_centre_with_a_fresh_accuracy_value_is_not_flagged` locks this in. **Attack:** a modified client sends the published
  `lat`/`lng` from `/on-duty/locations` with a new plausible accuracy each time (5.3, 7.8, ...) and small jitter on later days. None of the three signals fires.
  Pass 1 asked for "distance under about 1 m **or** the fix exactly matches the site coordinates". **Fix:** keep the current rule for the 1 m band to limit false positives at the pinned
  spot, but always flag when `dist < 0.05` m (an exact match to 7 decimals is effectively impossible for a real GPS fix). Rename the reason to `exact_site_coordinates`.
  The heuristics cannot stop a jittering attacker. `on_duty.selfie_required=true` and later Play Integrity / App Attest remain the real controls.
- **L7 (M2 residual). HR can still create presence for itself through `assign_mode='all'`, and switching to `all` through the location PUT is not alerted.**
  `backend/app/services/duty_locations.py:268-290` (`update_location`) accepts `assign_mode='all'`. It checks the cap but alerts only when `geo_changed`, and its audit has no
  `self_assigned`. Separately, by design `put_assignments` allows `all` for HR (flag, cap and alert only, `duty_locations.py:390-421`). With
  `on_duty.approval_required=false` (the default, `on_duty.py:53`), HR can create a 300 m site at home, open it to all and check in daily. Admins get the
  "created" push, so this is detected but not prevented. **Fix:** (a) in `update_location`, when `assign_mode` changes to `all`, add `self_assigned: true` to the audit and call
  `_alert_admins(..., "opened a duty location to all employees")`, as the assignments PUT does. (b) Decide explicitly, and record it in the contract, whether a non-`role.assign` actor may set
  `all`. The stricter option is to refuse it unless the actor has `role.assign`, which matches the self-assign rule. The dashboard tile from pass 1 M2(4) is not implemented.
- **L8. The admin visits card calls every suspicious visit "mock location".** `frontend/lib/features/on_duty/presentation/admin/duty_visits_screen.dart:146` shows
  `Suspicious (mock location)`, but `suspicious` is now also set by heuristics (impossible travel, repeated coordinates). HR may dismiss a real impossible-travel flag because the
  employee has no mock app. **Fix:** label it `Suspicious` with a hint ("check the audit log for the reason"), or have `/admin/on-duty/visits` return the signal codes to `view_all` and show them.

## Info
- **I1. Contract not updated for the fixes.** `docs/plans/13-on-duty-contract.md` does not document the new retention scope (note, distance, accuracy), the heuristic flags and their
  audit `signals`, the self-assign 403 or the `on_duty.all_mode_max_radius_m` setting, or `VISIT_ID_MISMATCH` / `DUTY_LOCATION_UNAVAILABLE` in `attendance.punch_rejected`.
- **I2. Lock-order deadlock on a reused selfie.** Office check-out locks the attendance row and then the file (`attendance.py:417,435`). Visit check-in locks the file and then the attendance row
  (`on_duty.py:383,396`). Visit check-out locks visit, then file, then attendance. Two concurrent punches by the same user with the same selfie can deadlock. Postgres aborts one and
  the user gets a 500 instead of `SELFIE_REUSED`. Only the user can trigger this against themselves, so there is no security impact. Optionally map `DeadlockDetected` to `SELFIE_REUSED`, or take the file lock first everywhere.
- **I3.** `repeated_coordinates` also compares a check-out with the same visit's check-in. A phone that returns an identical cached fused fix while stationary would be flagged. This is flag only, so
  it is acceptable. Watch the false-positive rate.
- **I4.** The dashboard `on_duty_open` counts open visits of deactivated users as well (`console.py:181-188`). This is cosmetic.

## What is not landed (from pass 1)
- M1: the exact-coordinate rule as specified (see L6). Recommending `selfie_required=true` is a deployment decision. Play Integrity / App Attest is out of scope.
- M2: the location PUT switch to `all` is not alerted or flagged, refusing `all` for non-admins was not done (see L7), and the "changed in the last 7 days" dashboard tile is not implemented.
- L3 (snapshot of site geometry at check-in) and the pass-1 Info items were not in this fix batch and are unchanged.
- Contract updates (I1).

## Not reviewed
- I did not run any tests (backend or Flutter). I relied on the test names in `tests/test_review11_on_duty_fixes.py`.
- I checked the source manifests only, not the merged Android manifest from a real build. Plugins could in theory add permissions, though geolocator and share_plus do not add background ones.
- I did not open the built `deploy/console/main.dart.js`. The console check was a static import-closure trace, not a bundle inspection.
- I only skimmed `on_duty_request_form.dart`, `request_tab.dart`, `form_widgets.dart`, `duty_assignments_screen.dart` and the coordinate paste regexes (local only, no network), and found nothing.
