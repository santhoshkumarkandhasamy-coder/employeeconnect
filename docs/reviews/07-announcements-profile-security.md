# 07 - Announcements and Profile/Home: security review

Date: 2026-10-22. Read-only review by security-reviewer (saved here by the lead; the reviewer had no Write tool).
Scope: backend routers/announcements.py, profile.py, home.py, files.py; services/announcements.py, profile.py, celebrations.py, home.py, files.py, notifications.py, fcm.py; schemas; deploy/nginx.conf; contract sections 2, 7, 11; Flutter features/announcements, profile, home, core/files, core/cache/json_cache.dart, core/security/screen_protect.dart.

Totals: 0 Critical, 0 High, 4 Medium, 9 Low. No targeting leak found: list, detail, unread-count, /files/{id} and push all share one `visible_to()` rule (404 otherwise); admin routes need `announcement.manage`; profile PUT is `extra="forbid"` with audited field names only; uploads use magic bytes, full image re-encode (EXIF dropped), pixel cap, random storage paths; the client renders plain text only; birthday `years` is always null. No deep links exist yet.

## Medium

### M1. Push flooding via create/renotify
`routers/announcements.py:67,83-87` (only 30/min per actor, shared by create and PUT), `services/announcements.py:331-350` (`push`), `schemas/announcements.py:24` (`renotify`). A rogue/compromised HR account can send `PUT ... renotify:true, priority:"urgent"` 30 times a minute (about 43,000 high-priority pushes per employee per day).
Fix: per-announcement renotify cooldown (one per 60 min, Redis SET NX EX, 429 RATE_LIMITED); per-actor daily push budget (about 20 push-generating writes/day, fail_closed); urgent/high priority at most once per announcement; record `renotify: true` in the audit `after`.

### M2. Celebrations opt-out by default, no opt-out control in the app
`database/alembic/versions/0011_profile_show_celebrations.py:14`, `models/users.py:110` (DEFAULT true); Flutter profile never reads/sends `show_celebrations`; `services/celebrations.py:54-79`, `routers/home.py:23-31` (`days` up to 30). Entering a DOB for HR purposes publishes name, photo, department and birthday to every colleague; anniversary `years` reveals marriage year; polling builds a company birthday directory.
Fix: default `false` (opt-in) via migration (reset existing rows to false); return anniversary `years` only on explicit opt-in. Flutter: "Show my birthday/anniversary to colleagues" switch on the profile screen sending `show_celebrations`, with an explanation.

### M3. `POST /api/me/profile/photo` capped at 256 KB by nginx
`deploy/nginx.conf:68` (default `client_max_body_size 256k`), only `location = /api/files` raises it; `routers/profile.py:35-44` falls into `location /api/`. Client photos are 250-700 KB so most uploads get 413; also bypasses nginx upload `limit_req`/`limit_conn`.
Fix: exact-match `location = /api/me/profile/photo` mirroring the `/api/files` block (`limit_except POST`, `limit_req zone=upload`, `limit_conn uploadsrv`, `client_max_body_size 6m`); update the "ONLY upload route" comment.

### M4. Any `announcement.manage` holder can rewrite another author's announcement; audit cannot show what changed
`services/announcements.py:279-303` (`update`, no ownership check, `created_by` unchanged), `_render`, `:243-252` (`_snapshot` excludes body), `:306-312` (delete audit stores only title). HR can edit an Admin-authored announcement body; employees still see "by <Admin>".
Fix: store `updated_by`/`updated_at` and show "edited by X"; restrict edits of announcements authored by someone holding permissions the actor lacks (containment as in review 06); keep body proof: SHA-256 + length of the body in audit before/after (or an `announcement_revisions` table).

## Low

- L1. Employee view leaks `target_all`/`department_ids` (`services/announcements.py:98-99`). Omit from the employee shape; keep in admin view; update contract section 7.
- L2. PUT is a full replace: omitted/null `expires_at` republishes an expired item; expired items can't be edited while staying expired; no optimistic concurrency; `renotify` not audited. Fix: reject edits of expired items unless `expires_at` explicitly re-set; add `version`/`If-Match` (409); audit `renotify`.
- L3. No per-user rate limits on announcement read endpoints and admin list/reads/DELETE (`routers/announcements.py:22-101`); `_stats` (`services/announcements.py:188-203`) does 2 count queries per row (N+1). Fix: 60-120/min reads, 30/min admin list/reads, 10/min DELETE; one grouped stats query.
- L4. Profile photos readable by every user forever; `POST /files` accepts `purpose=profile_photo` at 2048 px (`services/files.py:46,289-290`, `routers/files.py:19-28`); `set_photo` (`services/profile.py:173-191`) has no row lock. Fix: remove `profile_photo` from `POST /files` purposes; in `authorize` allow it only when some active user's `photo_file_id == f.id`; `with_for_update` in `set_photo`.
- L5. Celebrations opt-out can be ignored up to 6 h if the Redis delete fails (`services/celebrations.py:28,82-87`). Fix: generation-versioned cache key (INCR) and filter cached entries against current `show_celebrations` at read time.
- L6. Consent version is client-chosen (`services/profile.py:194-215`, `schemas/profile.py:116-119`); GET reports `granted:true` for an outdated version while trips fail CONSENT_REQUIRED. Fix: reject `granted:true` with a version other than `trip_consent_version` (422); expose `location_consent.version` and `current_version`/`is_current`. (The trips-fix agent may already enforce the version in `set_consent`; verify.)
- L7. PDF screening gaps (`services/files.py:59,160-164`: no `/OpenAction`, `/AA`, `/SubmitForm`, `/GoToR`, `/ImportData`, `/XFA`; compressed object streams invisible); Flutter hands PDFs to the OS share sheet. Fix: extend the deny list; ideally normalise PDFs server-side (qpdf/pikepdf) if cheap; client: in-app viewer inside SecureScreen or a warning before sharing (viewer optional if it needs heavy deps).
- L8. Flutter: announcement detail/list and home not in `SecureScreen`; `announcement_repository.dart:35-52` caches page 1 including bodies and `home_repository.dart:18` caches /home (celebration names, balances) as plaintext JSON not covered by `sensitiveCacheKeys` (`core/cache/json_cache.dart:101-129`); cache survives crash; `SecureScreen` renders before `acquire()` resolves (`screen_protect.dart:~127-145`). Fix: wrap detail and home in SecureScreen; add announcement `body` and `celebrations` to the scrub list or skip disk caching; clear cache at app start; placeholder until protection acquired.
- L9. Soft-deleted announcements keep image/PDF blobs forever (`services/files.py:374`, `services/announcements.py:306-312`). Fix: retention step N days after `deleted_at` nulls file ids so the orphan sweep removes the blobs.

## Informational
- Push payload carries title plus first 100 chars of the body through FCM (`services/notifications.py:45`, `services/fcm.py:38,48`) while contract section 11 says no sensitive data. Fix when the Flutter receiver lands: for `important`/`urgent` send only type, id and a generic title; private lock-screen visibility.
- `announcement.manage` is global (no department scope); document it in the contract/role editor.

## Unverified (SQLite only)
timestamptz comparisons in `visible_to`; `ck_announcements_expiry`; savepoint + IntegrityError on concurrent reads; `FOR UPDATE` in `set_consent`; `type_coerce(dob_enc, LargeBinary)` on real bytea; partial index on `published_at`; FK actions; nginx body limit for the photo route; real FCM delivery (firebase-admin not installed; no client receiver); Redis `scan_iter` invalidation; Pillow behaviour in the Docker image (MPO/progressive JPEG, PNG text chunk limits).

## Fixes needed
Backend: M1, M2 (migration), M3 (nginx), M4, L1-L7 (PDF: deny list at minimum), L9; contract updates for sections 2, 7, 11.
Flutter: M2 switch, M4 "edited by", L6 stale-consent display, L7 warning/viewer, L8 (SecureScreen, cache scrub, startup clear, placeholder).
