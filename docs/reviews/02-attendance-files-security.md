# Security review 02: attendance, late, files

Scope: `backend/app` attendance/late/files routers and services, `core/geo.py`, `core/crypto.py`, `core/rate_limit.py`, `deploy/nginx.conf`, backend tests, Flutter attendance/late code, Android/iOS templates. Leave code and the scheduler were ignored (in flux). Read-only review, no code changed.

Line numbers refer to the files as they were when reviewed.

## Verified as OK (no finding)

- Server time is authoritative. `client_time` only feeds a log line (`services/attendance.py:160-163`); `work_date` and `check_in_at` come from `utcnow()` converted to the office timezone.
- `/files/{id}` is checked against ownership, purpose and manager or `view_all` scope. A denied read returns 404, same as a missing id (`services/files.py:426-427`). Cross-user reads of selfies and leave attachments are audited (`:441-450`).
- Selfie binding on punch: the file must be owned by the actor and have purpose `selfie` (`services/attendance.py:152-153`), with one generic error for both cases.
- `employee_id` and `department_id` filters are ANDed onto the scope condition. They cannot widen a manager's view (`scope_conditions`, `:344-357`).
- Raw lat/lng are never returned by any endpoint. They are stored through `EncryptedJSON`. Responses expose only distance, fence flag and selfie URL.
- Upload type comes from magic bytes. Images are fully decoded and re-encoded, which drops EXIF and trailing data. Storage names are server-random, and `_resolve` is traversal-safe. The filename is never used. Selfies and leave attachments are encrypted at rest. Writes are atomic.
- The double check-in race is closed by the DB `UNIQUE(user_id, work_date)` plus an `IntegrityError` handler. Check-out uses `FOR UPDATE`.
- Unhandled errors return a generic 500 (`core/errors.py:305-308`).
- nginx never relays the client `X-Forwarded-For`. `/docs` is not exposed.

## Findings

### High

**H1. The geofence can be spoofed with no server-side signal, and the accuracy check can be skipped.**

- `schemas/attendance.py:16` and `services/attendance.py:165-166`: `is_mock_location` is client-supplied, so an attacker sets it to false or omits it. If it is true, the server only writes a log line. Nothing is stored on the attendance row, nothing is audited, and the punch is accepted.
- `services/attendance.py:177`: `accuracy_m is not None and > max`. Omitting `accuracy_m` (it is optional, `schemas/attendance.py:13`) bypasses the accuracy gate completely.
- The server never uses accuracy in the fence test (`:169-172`). It also never receives or checks a fix timestamp, so a replayed or stale fix is accepted. It does no device attestation.
- Impact: any employee can use a scripted client, rooted device or Frida hook to mark present from home with a one-line curl. The check is cosmetic.
- Fix:
  - Make `accuracy_m` required (reject null) on check-in, and on check-out when the geofence is enabled.
  - Add a required `fix_time` (the platform location timestamp) and reject a fix older than about 60 s or more than 30 s in the future against server time.
  - Persist `is_mock_location`, `client_time` skew and accuracy on the row, for example a `flags` column or an `attendance.suspicious` audit row. Reject or flag-for-HR any `is_mock_location=true` punch.
  - Verify Play Integrity (Android) and App Attest (iOS) tokens server-side per punch. The client-side root heuristic in `MainActivity.kt` is trivially bypassed, and its own comment says so.
  - For marginal fixes, require `dist + accuracy_m <= radius`, or at least `dist <= radius` plus accuracy within the limit.

### Medium

**M1. The geofence error leaks exact distance and radius, and enables an office-location oracle.** (`services/attendance.py:185-190`, `:169-172`)
- `OUTSIDE_GEOFENCE` returns `distance_m` and `radius_m`. Successful check-ins return `distance_m` as well (`_record_out:125`).
- An attacker can trilaterate the exact office coordinates from three probes, up to 10 per minute per user. The attacker can also bisect the boundary and later spoof a point just inside it.
- Fix:
  - Return only a boolean or a coarse bucket ("about 300 m away"). The client already computes proximity, since `proximityProvider` and `core/utils/geo.dart` exist.
  - Stop returning `distance_m` to non-HR roles.
  - Rate-limit failed fence attempts more tightly (3 per 10 min).

**M2. Selfies can be replayed, and a selfie can be used across days or for both punches.** (`services/attendance.py:145-157`, `:241`)
- `_validate_selfie` checks owner and purpose only. It does not check freshness (`File.created_at`) or that the file is not already referenced by another `Attendance` row.
- One real selfie can be uploaded once and reused for every later check-in. Selfie capture is also client-driven, so it is not proof of presence.
- Fix: require `created_at` within about 10 min, and require that no other attendance row references the file (a unique partial index on the selfie file columns). Pair with H1 attestation.

**M3. Uploads are parsed before authentication, and nginx allows 12 MB bodies on every `/api/` path.** (`deploy/nginx.conf:439-446`, `routers/files.py:19-27`)
- FastAPI reads and parses the multipart or JSON body before it solves the auth dependency. An unauthenticated client can force up to 12 MB of body buffering and parsing per request (20 req/s burst 60 per IP, 40 connections).
- nginx buffers bodies to `/tmp`, which is an unbounded tmpfs (`docker-compose.yml:109` style `tmpfs: ["/tmp"]`, no size). That is RAM exhaustion.
- 12 MB is also accepted on all JSON endpoints, while the app limit for uploads is at most 10 MB.
- Fix:
  - Set `/api/` to `client_max_body_size 64k` (or 1m).
  - Add `location = /api/files { client_max_body_size 11m; limit_req zone=upload burst=5; ... }` with its own strict `limit_req_zone`, for example 30 r/m.
  - Set a size on the nginx tmpfs (`tmpfs: ["/tmp:size=256m"]`).
  - Optionally check `Content-Length` in an ASGI middleware before body parsing.

**M4. Image decoding can be used for memory DoS.** (`services/files.py:238`, `:99-120`)
- `Image.MAX_IMAGE_PIXELS = 40M` only raises above 2x (80 Mpx), and between 1x and 2x it merely warns, so up to 80 Mpx is decoded.
- `im.load()`, `convert()`, `Image.new()` and `paste()` hold several full-size copies. A single 80 Mpx RGBA upload is roughly 1 GB peak.
- 20 uploads per minute per user, run via `asyncio.to_thread` with no concurrency cap, can OOM the API container.
- Fix:
  - Set `MAX_IMAGE_PIXELS` to about 12M and turn `DecompressionBombWarning` into an error.
  - Check `im.size` before `load()` and reject files over the size limit.
  - Use `im.draft('RGB', (2048, 2048))` for JPEGs.
  - Bound concurrent re-encodes with an `asyncio.Semaphore(2)`.
  - Avoid the second full-size copy: `im.convert("RGB")` is already a fresh image, so no `Image.new` plus `paste` is needed.

**M5. No tested key-rotation path for files and location data.** (`core/crypto.py:7-9`, `:22-32`, `:45-50`)
- The docs promise a "re-encrypt script" but none exists (searched the repo). Dropping an old key from `DATA_ENCRYPTION_KEYS` makes every old selfie, attachment and location row undecryptable, and `decrypt_bytes` then raises an unhandled `InvalidToken` and returns 500.
- File blobs and DB fields share one key list. Encrypted files have no key-id and no context binding (AAD), so a DB-write attacker can swap blobs between `files` rows. Fernet cannot do AAD, so consider AES-GCM or AES-256-GCM with `file_id` as AAD.
- The whole file is held in memory, and the Fernet timestamp is visible in each token.
- Fix:
  - Write `scripts/reencrypt.py` using `MultiFernet.rotate()` over `files` (`is_encrypted`) and the `*_enc` columns, resumable and with a verify pass, and test it.
  - Log a structured error with the file id on `InvalidToken`.
  - Use separate keys for files and columns, and store the key version.

**M6. Late-time arithmetic is wall-clock when a DST office timezone is configured.** (`services/attendance.py:195`, `:211`, `:215`, `:217`)
- `start` and `local_in` share the same `ZoneInfo` tzinfo, so Python subtracts and compares them as naive wall-clock values and ignores UTC offsets and `fold`.
- On a DST switch day, `minutes_late` can be off by 60 minutes. Late-record creation for a nonexistent or ambiguous start time (for example 02:30 on a spring-forward day) is inconsistent.
- Today's default is `Asia/Kolkata`, with no DST, so the bug is latent. The test at `test_attendance.py:321` only covers a fixed EDT day, not a transition.
- Fix: convert both to UTC before subtracting and comparing (`start.astimezone(UTC)`), and add transition-day tests for `America/New_York` and Europe zones. Because `office.timezone` is an admin-editable setting, validate it on write.
- Related: overnight shifts (end earlier than start) are not handled. `work_date` is the calendar date at punch time, so a night-shift check-out after midnight returns `NOT_CHECKED_IN` (`:289-293`). The record also gets `auto_closed` at 00:30 (`:460`), and the late rule uses the wrong day.

**M7. No retention or purge for location, selfie or attendance PII, and location blobs are decrypted on every list.**
- Selfies and `check_in_loc_enc` and `check_out_loc_enc` have no purge job in the reviewed code. Only trips have `trip.retention_days` in the seed, and nothing in the reviewed code applies it.
- `Attendance` ORM loads decrypt `check_in_loc` and `check_out_loc` on every history and team row (`models/attendance.py:623`, `:627`) although the value is never returned. This is needless plaintext exposure and CPU.
- Fix: add a retention setting and a worker purge that nulls location and deletes selfies after N days. Use `defer()` or `deferred=True` on the location columns, or load them only where needed.

### Low

**L1. The rate limiter fails open.** (`core/rate_limit.py:34-35`) With Redis down, the check-in limit and upload limit vanish. Failed attempts are still counted, which is good. The "log and allow" choice is documented (O1), but selfie uploads with 10 MB re-encodes should fail closed or fall back to an in-process limiter.

**L2. Punch and denied-access events are not audited.**
- `check_in` and `check_out` write no audit row, and none is written for refused punches such as `OUTSIDE_GEOFENCE`, mock-flagged punches and `ALREADY_CHECKED_*`.
- Denied `/files/{id}` reads (404) are not logged either, so there is no signal for a manager probing ids.
- Fix: write a low-volume `attendance.punch_rejected` audit row (reason only, no coordinates), and log denials in `authorize`.

**L3. PII in logs.** `services/attendance.py:164` and `:166` log `employee_id` with clock skew or the mock-location flag. This is acceptable in app logs, but if logs go to a shared aggregator, hash the id or use the user UUID. Coordinates are not logged (good).

**L4. 404-versus-403 and timing.**
- Behaviour is consistent for `/files`. For other purposes, a missing id returns after one query and a denied selfie takes two to three (`owner` lookup), so there is a small timing difference. UUIDv4 ids make enumeration infeasible, so this is informational.
- `POST /files` returns 403 on a bad purpose permission, which is acceptable.

**L5. PDFs are only magic-byte checked.** (`services/files.py:89-96`, `:41-48`) PDFs (`leave_attachment`, `announcement_pdf`) are stored as uploaded, so they can carry JavaScript, embedded files or launch actions. The response is `attachment` with `nosniff` and a sandbox CSP, which is good, but managers and HR open them in desktop viewers. Fix: reject PDFs containing `/JavaScript`, `/JS`, `/Launch` or `/EmbeddedFile` (or rasterise or sanitise), or restrict leave attachments to images.

**L6. Orphan blob cleanup misses row-less blobs.** (`services/files.py:279-288`) If the process dies between `os.replace` and DB commit, the blob stays forever (only `*.tmp` and rows are swept). Fix: also reconcile blobs with no `files` row. Also `profile_photo` files are readable by every employee even when not attached to a profile, which is minor because ids are random and unlisted.

**L7. Storage permissions rely on umask.** `mkdir(parents=True)` and `open(tmp,"wb")` (`services/files.py:60-63`) use default modes. The Dockerfile uses a dedicated user (`uid 10001`), but set `mkdir(mode=0o700)` and `os.chmod(path, 0o600)` explicitly. Profile photos and announcements are stored unencrypted by design, so the volume permission is their only protection.

**L8. The holidays endpoint is under `/admin/`.** (`routers/attendance.py:98-105`) `GET /admin/holidays` is readable by any authenticated user. That is intentional ("read-only, any authenticated user") and the data is not sensitive. The risk is future drift: when module 09 adds writes, give each method its own permission (`holiday.manage`), and move the read to `/holidays` so that any `/admin/*` blanket policy or WAF rule stays unambiguous.

**L9. The team endpoints have no date-range cap.** (`routers/attendance.py:71-84`, `:117-129`) A team query with no `from` and `to` counts and joins the whole table, with page_size at most 100 and a `COUNT` over a subquery. A manager can page through all direct-report history (that is in scope), but the cost is unbounded. Cap the range to about 366 days. Team scope is only direct reports (`manager_id == actor.id`) and includes soft-deleted users.

**L10. `today` returns different shapes.** (`services/attendance.py:318-325`) It returns `{"record": null}` or a bare record, which is not a security issue but invites client bugs.

### Mobile (Flutter)

**L11. Selfie file left on device.** (`selfie_service.dart:109-116`, `attendance_providers.dart:241-260`) The captured JPEG at `f.path` (image_picker cache) is never deleted after upload or on cancel. Delete it after the upload (`File(path).delete()`), and on logout.

**L12. Coarse location permission.** (`tool/bootstrap.ps1:64`) `ACCESS_COARSE_LOCATION` is declared. On Android 12+ the user may grant "approximate" only, which gives wide accuracy and makes check-in fail with `LOCATION_ACCURACY_LOW` (or defeats the fence if the server accepted it). Declare FINE only, and when only an approximate grant is returned, detect it (`Geolocator.getLocationAccuracy`) and prompt for precise. iOS: handle `reducedAccuracy` the same way. iOS `NSLocationWhenInUse` and Camera strings are correct, and there is no background-location request (good). Android declares no `CAMERA` permission, which is fine with `image_picker` intent capture, but if a camera plugin is added later, request it at use time.

**L13. Cache holds attendance details unencrypted.** (`core/cache/json_cache.dart`) History, calendar and summary JSON (including `selfie_url`, `distance_m`, `within_fence`) is in the OS temp dir and is cleared on logout only through `clear()`. Acceptable, but exclude `distance_m` from the cached payload, or store it in encrypted storage. Android backup is already excluded by `data_extraction_rules.xml`.

**L14. The client sends `client_time`, and the server only logs skew.** This is correct (never trusted). No client-side issue.

## Tests: gaps

- No test for a missing `accuracy_m` (it is skippable), no test that `is_mock_location=true` has an effect, and no stale-fix test.
- No DST-transition test and no overnight-shift test.
- The double-check-in race is tested sequentially (`test_double_checkin_409_with_existing_id`). The `FOR UPDATE` and `IntegrityError` paths run on SQLite, so they are never exercised concurrently. Add a Postgres-backed concurrency test.
- No selfie-replay test, no key-rotation test, and no HEIC or polyglot (PNG+PDF) upload test. Existing coverage of IDOR, traversal, EXIF, trailing payload, bomb, encryption round-trip and audit is good.

## Summary

| Severity | Count |
|---|---|
| Critical | 0 |
| High | 1 |
| Medium | 7 |
| Low | 14 |

Top issues:
1. H1: Geofence and anti-spoofing are advisory only. Accuracy can be omitted, the mock flag is client-controlled and unused, and there is no fix-freshness or attestation check.
2. M1: Exact distance and radius are returned on failure, which lets an attacker recover the office location by trilateration.
3. M3 and M4: Upload surface DoS. Bodies are parsed before auth with a 12 MB limit on all of `/api/`, an unbounded tmpfs, and up to 80 Mpx image decoding without a concurrency cap.
4. M5: There is no tested key-rotation script, so rotating keys is unsafe for encrypted selfies and locations.
5. M2: Selfies are reusable across days and across punches.
6. M6 and M7: Late logic is wrong on DST days and for night shifts, and there is no PII retention for location and selfies.

Authorization, IDOR handling on `/files`, scope filtering and audit of cross-user views were found sound.
