# Security Review 08: Excel Export (backend `GET /admin/export` + Flutter Reports & Export screen)

Scope: backend `routers/admin_export.py`, `services/export.py`, `export_sheets.py`, `food_export.py`, `xlsx.py`, rbac/audit/rate_limit/config, tests `test_export_full.py`, `test_export_hardening.py`; Flutter `features/export/**`, `core/files/share_file_opener.dart`, `features/food/data/food_repository.dart` (ExportStore / FileOpener); `docs/plans/10-export.md`, `docs/api-contract.md` section 10.

Overall: backend is in good shape; no Critical/High. Main gap is on the device: the share flow leaves a plaintext copy of the workbook that app cleanup never deletes.

| # | Sev | Title |
|---|---|---|
| M1 | Medium | share_plus leaves a copy in `cache/share_plus/` that logout/lock never delete |
| M2 | Medium | Decrypted leave/permission reasons bulk-exported with no separate permission or opt-in |
| M3 | Medium | No ceiling on bulk-export volume beyond 5/min; no step-up re-auth |
| L1 | Low | Denied/failed export attempts not audited |
| L2 | Low | 404 vs 403 lets team-scoped callers enumerate employee IDs |
| L3 | Low | Team scope uses current `manager_id` only (full history from 2000; soft-deleted users) |
| L4 | Low | Count pre-flight outside semaphore/time budget; no limit on finished-but-undrained downloads |
| L5 | Low | `sweep_stale` in shared temp dir can delete live/other-worker dirs |
| L6 | Low | Device files under `Directory.systemTemp` instead of path_provider |
| L7 | Low | Share to any target without warning; ShareResult ignored; "Copy path" |
| L8 | Low | Client buffers workbook in memory twice, no size cap, partial file not cleaned |
| L9 | Low/Info | Visible literal apostrophe prefix; trigger list misses leading spaces and full-width signs |
| I1 | Info | Android project missing; FileProvider/backup rules/cleartext policy unchecked |

## Medium

### M1. share_plus keeps a plaintext copy of the workbook
Where: `frontend/lib/core/files/share_file_opener.dart:13`, `features/export/presentation/export_screen.dart:96-100`, `features/food/data/food_repository.dart:144-150,164-169`.
On Android `Share.shareXFiles` copies each file into `<cacheDir>/share_plus/` and only empties it at the next share. `_deleteQuietly` deletes our copy only; `ExportStore.clear()` wipes only `ec_exports`. The last shared workbook survives logout and user switches (names, attendance, decrypted reasons; same for leave attachments and announcement PDFs through the same opener).
Fix: `ExportStore.clear()` also deletes `<getTemporaryDirectory()>/share_plus`; call at startup, logout, lock and shortly after `shareXFiles` returns / on next resume. Or serve through an own FileProvider over `ec_exports` so only one copy exists. Test: no `*.xlsx` remains in cache after logout.

### M2. Decrypted reasons and decision comments exported with no separate gate
Where: `backend/app/services/export_sheets.py:225-227,251-253,287-289,299`, `export.py:71-77`.
Only gate is `request.view_team|view_all`. The export is a portable file with every reason for up to 366 days, shared to arbitrary targets; sick-leave reasons can be health data.
Fix: exclude Reason and Decision Comment by default; explicit `include_reasons=true` requiring a separate permission (e.g. `export.sensitive`, HR only); record `include_reasons` in the `export.run` audit row.

### M3. No volume ceiling, no step-up re-auth
Where: `routers/admin_export.py:38-40`, `services/xlsx.py:296-298`.
Only 5 requests/min/user; a stolen HR token can pull the whole company history quickly.
Fix: daily per-user quota (exports and/or rows), WARNING log/alert past threshold or for a single large export; consider step-up re-auth (current_password / short-lived claim) for `scope == everyone`.

## Low

- **L1** `routers/admin_export.py:47-56`: FORBIDDEN/NOT_FOUND/RANGE_TOO_LARGE/RATE_LIMITED raised before `audit(...)`. Audit denied/failed attempts (`export.run`, outcome denied|failed, with type/range/filters/code) in a separate commit before re-raising.
- **L2** `services/export.py:118-128`: for team scope return the same 403 for unknown and out-of-scope employee/department IDs; keep 404 for `export.all`. Update `test_team_user_cannot_filter_outside_team_and_unknown_ids_404`.
- **L3** `services/export.py:110`: team scope = current `manager_id`. Clamp `from` for team scope to a policy window (12 months) as there is no reporting history; exclude `deleted_at IS NOT NULL` users; keep rule consistent with the in-app `*.view_team` lists.
- **L4** `services/xlsx.py:296-298,331-336`, `routers/admin_export.py:77-86`: acquire the semaphore before count pre-flight and put counts under the same timeout (+ DB statement_timeout); cap outstanding downloads, released in `BuiltWorkbook.close()`; make `EXPORT_TEMP_DIR` mandatory in DEPLOY.md, size-limited volume.
- **L5** `services/xlsx.py:128-144,308-312`: require `EXPORT_TEMP_DIR` (dedicated, 0700) in production; sweep only dirs owned by dead pids / older than `STALE_AFTER_S` and not streaming.
- **L6** `food_repository.dart:155-157`: use `getApplicationCacheDirectory()`/`getTemporaryDirectory()` from path_provider, not `Directory.systemTemp`.
- **L7** `share_file_opener.dart:13-14`, `export_screen.dart:96-103,130-132`: one-time-per-session confirmation before sharing ("contains personal data of N employees"); check `ShareResult.status`, delete file and show "Share cancelled" if dismissed; remove "Copy path".
- **L8** `features/export/data/export_repository.dart:52-65`: stream to the ExportStore file (`dio.download` / `ResponseType.stream`), abort past a client cap (100 MB), delete partial file on error and when `!mounted` (line 97).
- **L9** `services/xlsx.py:54,59-68,193-196`: keep `data_type="s"`; add the `'` prefix only when `value.lstrip()` starts with a trigger, also cover `＝ ＋ － ＠`; optionally use quotePrefix style instead of a literal apostrophe.

## Informational
- **I1** `frontend/android/` missing: re-review `AndroidManifest.xml` (FileProvider paths, `allowBackup=false` / dataExtractionRules excluding cache and exports, cleartext policy) once generated.

## Verified OK
RBAC gating (`export.all|export.team` + per-type permissions, scope applied in count and row queries, filters only narrow, tests for cross-manager leaks); data minimisation (no coordinates, selfies, email, phone, address, DOB); formula/XML injection (string cells forced to `s`, illegal chars stripped, 32k truncation); filename/header injection (server literal enum + dates, client `safeExportName`); `Cache-Control: no-store` + `nosniff`; date range/size limits (2000-2200, <=366 days, row cap per sheet, 60 s budget, 2 concurrent builds, 503 Retry-After, fail-closed limiter); server temp file cleanup; audit of successful exports; device cleanup of `ec_exports` at startup/logout/lock and screenshot protection.
