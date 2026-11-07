# 18 - eSSL / ZK biometric attendance devices (read-only pull)

Status: PROPOSED. Contract: api-contract section 21. Migration: `0030_essl_devices` (down_revision `0029`).
New tables: `devices`, `device_users`, `device_punches` (append-only), `device_punch_outcomes`, `device_syncs`.
New attendance columns: sources, device ids, preserved app punch times. Permissions: `device.view`, `device.map`, `device.manage`.

## 1. Goal and fixed decisions
- Three office machines (eSSL / ZKTeco, ZK protocol) at `192.168.0.201`, `.202`, `.203`, port 4370, are polled directly by the
  **worker** (scheduled job + "Sync now"). The customer's eTimeTrackLite stays independent and untouched.
- We read ONLY: device info (serial, model, firmware, clock, record counts), the user list (device user id, name, card,
  privilege) and attendance punch logs. **Fingerprint / face / palm templates and any biometric data are never requested,
  received or stored.** The device PIN that comes with the user record is discarded in the parser and never stored or logged.
- **Never write to a device**: no clear logs, no delete / set user, no set time, no enable / disable, no restart, no unlock.
  Enforced by a command allow-list in the client (section 6) and verified by tests and a packet capture.
- Device users are matched to app employees by employee ID; unmatched users are reported for HR to map or ignore.
- An office device punch is in-office presence (source `device`): no geofence, no selfie.

## 2. Network (ops, owned by backend-developer under `deploy/`, reviewed by security-reviewer)
- Prod VM `192.168.1.33` gets the secondary address `192.168.0.33/24` on `eth0` permanently via netplan
  (`deploy/netplan/60-device-lan.yaml.example` + README step). Containers reach the devices through Docker NAT (masquerade).
- The worker gets a fixed address on the `egress` network (`EGRESS_SUBNET`, `WORKER_EGRESS_IP` in `.env`). Host firewall
  (`DOCKER-USER` chain, script `deploy/firewall/devices.sh`, idempotent): ALLOW `WORKER_EGRESS_IP` -> `192.168.0.201-203`
  tcp/udp 4370; DROP every other docker subnet -> `192.168.0.0/24` and -> `192.168.1.0/24` (except existing needs).
- App-level guard against SSRF: env `DEVICE_ALLOWED_CIDRS` (default `192.168.0.0/24`, NOT a UI setting). Device IPs outside
  it, loopback, link-local, multicast, docker subnets and the server's own addresses are refused (422
  `DEVICE_ADDRESS_NOT_ALLOWED`). Port 1-65535, default 4370.
- **The API never opens a socket to a device.** Test connection and Sync now are queued (Redis) and run in the worker, so one
  process owns all device connections.

## 3. Database (migration 0030)
### devices
`id uuid pk`, `name text` 1-60 (unique on `lower(name)`), `ip inet not null`, `port int not null default 4370`
(CHECK 1..65535), `transport text not null default 'tcp'` (CHECK in tcp, udp), `comm_key_enc bytea null` (Fernet, numeric
0..999999), `enabled bool default true`, `location_label text null` (<= 80), `timezone text null` (IANA; null = office tz),
`clock_correction text not null default 'auto'` (CHECK auto, off), `ingest_from date not null` (punches before this
office date are not stored), `serial_number text null` (pinned identity), `model text`, `firmware text`, `platform text`,
`last_clock_offset_s int null`, `clock_status text null` (CHECK ok, drift, invalid), `last_log_count int`,
`last_user_count int`, `last_users_read_at`, `last_sync_at`, `last_success_at`, `last_error_code text`, `last_error_at`,
`consecutive_failures int not null default 0` (>= 0), `circuit_open_until timestamptz null`, `last_alert_at`,
`created_by uuid fk users`, timestamps. UNIQUE (`ip`, `port`). Delete is RESTRICTed by punches (disable instead).

### device_users (one row per user slot seen on a device)
`id uuid pk`, `device_id fk devices ON DELETE RESTRICT`, `device_uid int` (internal slot), `device_user_id text` 1-24
(as on the device), `name_enc bytea` (encrypted), `card_enc bytea null` (encrypted), `is_device_admin bool`,
`user_id uuid null fk users`, `match_method text not null` CHECK in (`auto`, `manual`, `unmatched`, `ignored`),
`matched_by uuid null`, `matched_at`, `present_on_device bool default true`, `first_seen_at`, `last_seen_at`.
UNIQUE (`device_id`, `device_user_id`); index `user_id`; partial index `WHERE match_method = 'unmatched'`.
CHECK: `user_id IS NOT NULL` iff `match_method IN ('auto','manual')`.

### device_punches (raw, append-only)
`id bigint identity pk`, `device_id fk RESTRICT`, `device_user_id text not null`, `device_uid int null`,
`device_time timestamp (no tz) not null` (raw device clock), `punch_at timestamptz not null` (corrected UTC),
`offset_applied_s int not null default 0`, `state smallint null`, `verify_mode smallint null`,
`quarantine text null` CHECK in (`clock_invalid`, `future`), `sync_id uuid fk device_syncs`, `ingested_at default now()`.
**UNIQUE (`device_id`, `device_user_id`, `device_time`)** = idempotency key (raw time, so a changed offset never duplicates).
Indexes (`device_user_id`, `punch_at`), (`punch_at`). Trigger `device_punches_append_only`: UPDATE and TRUNCATE always
raise; DELETE only when `OLD.punch_at < now() - interval '90 days'` (retention job). Same pattern as `audit_logs`.

### device_punch_outcomes (mutable result of the latest recompute)
`punch_id bigint pk fk device_punches ON DELETE CASCADE`, `user_id uuid null`, `work_date date null`,
`outcome text not null` CHECK in (`check_in`, `check_out`, `extra`, `unmatched`, `ignored`, `quarantined`, `not_applied`),
`reason text null` (CHECK in `on_leave`, `on_duty`, `no_shift`, `inactive_user`, `manual_entry`, `outside_window`,
`payroll_locked`, `mapping_ignored`, `clock_invalid`, `future`, `duplicate`), `updated_at`.

### device_syncs (history + job status for Sync now / Test)
`id uuid pk`, `device_id null` (null for an unsaved test), `kind text` CHECK in (`schedule`, `manual`, `reconcile`,
`test`), `requested_by uuid null`, `status text` CHECK in (`queued`, `running`, `ok`, `failed`, `skipped`),
`error_code text null`, `queued_at`, `started_at`, `finished_at`, `records_on_device int`, `users_on_device int`,
`fetched int`, `inserted int`, `quarantined int`, `days_recomputed int`, `clock_offset_s int`, `result jsonb null`
(test only: serial, model, firmware, platform, device_time, counts; no user data). Index (`device_id`, `queued_at desc`).

### attendance (new columns)
`check_in_source text not null default 'app'` CHECK in (app, device, manual); `check_out_source text null` (same CHECK);
`check_in_device_id uuid null fk devices`, `check_out_device_id uuid null fk devices`;
`app_check_in_at timestamptz null`, `app_check_out_at timestamptz null` (the phone punch times, kept so a recompute is
repeatable). Backfill: non-manual rows `app_* = check_in_at / check_out_at`; manual rows `check_in_source = 'manual'`,
`check_out_source = 'manual'` when they have a check-out; real app punches on manual rows keep `app_*` null (unknown).

### permissions and settings
- `device.view` (admin, hr): device list / status, unmatched users, punch log, sync history.
- `device.map` (admin, hr): map / ignore device users, Sync now.
- `device.manage` (admin): add / edit / delete / enable devices, comm key, test connection, accept identity.
- Settings (`/admin/settings`, validated): `device.sync_interval_minutes` 5 (2-60), `device.sync_window_start` "05:00",
  `device.sync_window_end` "23:30", `device.off_window_interval_minutes` 60 (15-240), `device.apply_window_days` 7 (0-31),
  `device.checkout_min_gap_minutes` 5 (1-120), `device.drift_tolerance_s` 120 (0-900), `device.drift_invalid_s` 43200,
  `device.future_tolerance_s` 300, `device.match_prefix` "" (<= 10 chars), `device.apply_on_on_duty_days` false,
  `device.use_punch_state` false, `device.push_on_punch` false, `device.punch_retention_days` 1095 (0 = keep, else >= 90),
  `device.sync_history_retention_days` 90.
- Code constants (not settings): connect timeout 5 s, per-packet read timeout 10 s, whole session cap 120 s, max payload
  16 MiB, failure threshold 5, circuit open 15 min doubling to 60 min.

## 4. Sync flow (worker job `devices.sync`)
1. Tick every minute (short lock like `tasks.reminders`); a device is due when `now - last_sync_at >= interval` (window
   interval inside `sync_window_start..end`, off-window interval outside), plus queued manual jobs. `devices.reconcile`
   daily 00:20 (before `auto_close` 00:30) syncs all devices and recomputes the last `apply_window_days` days for every
   mapped user.
2. Skip when disabled or `circuit_open_until > now` (sync row `skipped`). Per device: Redis lock `device-lock:{id}`
   (TTL 180 s) + an in-process asyncio lock; devices run concurrently (max 3), one connection per device, ever.
3. Connect (5 s timeout), CMD_CONNECT, CMD_AUTH with comm key when set. Read options (serial, model, firmware, platform).
   **Identity pin**: first success stores `serial_number`; a different serial later -> fail `DEVICE_IDENTITY_CHANGED`, no
   data read, alert; admin must "Accept new identity".
4. Read device time -> `clock_offset_s = device_time_as_utc - server_utc` (corrected by half the round trip).
5. Read record counts. If `log_count == last_log_count` and the last sync was ok, skip the log download. If it DECREASED
   (logs cleared by someone else), full read + audit `device.log_count_decreased` + alert (we never clear).
6. Read users when `user_count` changed or hourly. Upsert `device_users` (present flag, names, cards encrypted; PIN dropped);
   run auto-match for `unmatched` rows.
7. Read attendance log (chunked buffer read). Convert, quarantine, insert with `ON CONFLICT DO NOTHING`. Skip rows whose
   office date is before `ingest_from`. `ingest_from` may reach back to the punch retention (history import, decided 2026-11-07):
   punches older than the apply window are stored raw (no clock offset, exempt from the backdating check, outcome `not_applied`/
   `outside_window`, no attendance recompute); a later mapping refreshes their outcome rows only (`refresh_history_outcomes`).
8. CMD_EXIT. Collect affected (user, work_date) pairs and recompute each in its own transaction (section 5).
9. Update device status, `device_syncs` row, audit `device.sync` (counts only). Failure: `consecutive_failures + 1`; within
   the run retry twice (3 s, 10 s backoff); at threshold open the circuit and push `admin_alert` to `device.manage`
   holders (at most once per opening and once a day per device).
Error codes (job-level, stored in `last_error_code`): `DEVICE_UNREACHABLE`, `DEVICE_TIMEOUT`, `DEVICE_AUTH_FAILED`,
`DEVICE_BUSY` (another client such as eTimeTrackLite holds the connection; retried next tick, not counted toward the
circuit for the first 3 occurrences), `DEVICE_PROTOCOL_ERROR`, `DEVICE_IDENTITY_CHANGED`, `DEVICE_CLOCK_INVALID` (warning).

### Clock and timezone
- Device times are naive local times in `devices.timezone` (default office tz). `punch_at = localize(device_time) -
  offset_applied_s`. India has no DST; for other zones `fold=0` and the ambiguity is documented.
- `|offset| <= drift_tolerance_s`: apply 0, `clock_status = ok`. Up to `drift_invalid_s`: apply the offset measured in this
  sync when `clock_correction = auto` (stored per punch), status `drift`, daily alert. Above: status `invalid`, new punches
  `quarantine = clock_invalid`, alert. A device year < 2015 is `clock_invalid`; `punch_at > now + future_tolerance_s` is
  `future`. We never set the device clock (write command); the fix is on the device or via eTimeTrackLite.
- The offset is measured at sync time, so it is exact for recent punches; the first backfill of older punches uses the same
  offset (documented limitation).

### Matching
- Auto-match, in order: device_user_id equals employee_id (case-insensitive, trimmed); else numeric equality ignoring
  leading zeros after stripping `device.match_prefix` from the employee id (e.g. prefix `EMP`: `0101` <-> `EMP101`). Only
  active users; an ambiguous match (2+ candidates) stays `unmatched`.
- Manual map (`device.map`) overrides; `apply_to_all_devices` (default true) maps the same device_user_id on every device.
  Ignore = never applied. "Reset to auto" returns to auto-matching. Every change queues a recompute of that device user's
  punches in the apply window.

## 5. From punches to attendance (one pure function, idempotent)
`derive_day(user, work_date)` recomputes from scratch: app times (`app_check_in_at/out`), accepted device punches of all
mapped device users of that user, manual flag, shift, holiday, leave, permission, on duty, settings. Running it twice gives
the same row; it locks the attendance row `FOR UPDATE` and retries once on `IntegrityError` (concurrent phone check-in).

- **Work date of a punch**: day shift = office-local date. Night shift (end < start): a punch at or before shift end +
  4 h (`OVERNIGHT_BUFFER`) in the morning belongs to the previous date; otherwise its own date.
- **Not applied** (punch kept, outcome `not_applied` + reason): user inactive / deleted; no active shift (`no_shift`);
  approved full-day leave (`on_leave`); approved on-duty day unless `device.apply_on_on_duty_days` (`on_duty`); work_date
  older than today - `apply_window_days` (`outside_window`); month covered by a payroll run in status processed or
  published (`payroll_locked`); quarantined; mapping ignored / unmatched. HR uses manual attendance for these days.
- **Manual entry wins**: if `attendance.manual`, nothing changes (outcome reason `manual_entry`). New admin action
  "Use device punches" clears the manual flag and recomputes.
- **Check-in** = earliest of app check-in and first device punch. Source `device` / `app` (tie = `app`, keeps evidence).
  Device check-in sets `check_in_within_fence = true`, distance null, no selfie.
- **Check-out** = latest of app check-out and last device punch, where a device punch counts only if it is at least
  `checkout_min_gap_minutes` after the effective check-in. Punches within that gap of an earlier one are `duplicate`.
  In-between punches are `extra`. `device.use_punch_state` = false: in / out keys are ignored (stored and shown only).
- Status, `minutes_late`, worked minutes, half day and the late record are computed with the existing
  `minutes_late_for` / `working_day` / `attendance.half_day_minutes` rules (holiday / weekly off = never late; first-half
  half-day leave and covering permission = not late). Late record rebuilt.
- A check-out arriving for an `auto_closed` day clears `auto_closed`.
- A row that has no app punch, no manual entry and no remaining device punches (mapping removed) is deleted with its late
  record (audit `attendance.device_unapply`). App rows are never deleted.
- Phone flow: `POST /attendance/check-in` on a day that already has a device check-in still answers 409
  `ALREADY_CHECKED_IN` (the app hides the button). Phone check-out on a device day works with the usual rules; a later
  device punch can still move the check-out later.

## 6. ZK client choice
**Decision: minimal in-house, read-only, asyncio implementation** in `backend/app/integrations/zk/` (`protocol.py` codec,
`client.py`, `records.py` parsers). No new dependency (stdlib `asyncio`, `struct`).
- Why not pyzk (PyPI `pyzk`, last release 0.9, unmaintained for years): it is **GPL-2.0** (copyleft; we ship the backend
  image to customers under a commercial license), it is synchronous (blocking sockets inside our asyncio worker), and it
  exposes the full write API (`clear_attendance`, `clear_data`, `delete_user`, `set_user`, `set_time`, `disable_device`,
  `restart`, `unlock`) which we must never call. Wrapping it would not prove read-only behaviour. If ever adopted it must be
  pinned `pyzk==0.9` with legal sign-off; for now it may be used only on a developer laptop as an interop reference, never
  in the repo or image. The in-house code is written clean-room from the public protocol description; no GPL code copied.
- Scope: TCP (8-byte `0x5050 0x827D` + length prefix) and UDP framing, checksum, session / reply ids, comm-key scramble for
  CMD_AUTH, chunked buffer reads, packed device time decode, user record formats (28 / 72 byte) and attendance formats
  (8 / 16 / 40 byte).
- **Allow-list (the only commands that can leave the socket writer)**: CONNECT 1000, EXIT 1001, AUTH 1102, OPTIONS_RRQ 11
  (option names from a fixed list: `~SerialNumber`, `~DeviceName`, `~Platform`, `~ZKFPVersion`, `~OS`), GET_VERSION 1100,
  GET_TIME 201, GET_FREE_SIZES 50, PREPARE_BUFFER / DATA_WRRQ 1503 (inner command limited to USERTEMP_RRQ 9 with
  FCT_USER 5, and ATTLOG_RRQ 13), DATA_RDY 1504, FREE_DATA 1502. Anything else raises `ForbiddenCommand` before any byte is
  written. Template reads (DB_RRQ 7 / FCT_FINGERTMP, face) are explicitly NOT in the list. We do not disable the device
  during reads (punches made meanwhile arrive in the next sync).
- Hardening: every length is bounded (packet <= 64 KiB, total buffer <= 16 MiB, record count <= 200 000), strict timeouts,
  the device PIN field is skipped in the parser, raw packet dumps only with `ZK_DEBUG=1` outside production and redacted.
- Ops tool: `python -m scripts.zk_probe --device <id>|--ip <ip>` (read-only: info, time, counts; run in the worker
  container) to identify the unknown models before go-live.

## 7. API (contract section 21)
Devices CRUD, test, sync, sync history, identity accept; device users list / map / ignore / auto; punch log; employee
`GET /attendance/punches`; manual `use-device`; changes to `AttendanceRecord`, health, export and settings.

## 8. Security and privacy
- No biometrics: parser has no template code paths; test asserts no table / column / JSON key named like
  `template|finger|face|biometric`. Device PIN dropped. Card numbers encrypted, shown masked (last 4) to `device.view`, never
  exported, never in logs or audit.
- Comm key encrypted at rest, write-only in the API (`has_comm_key`), never logged; carried to the worker inside an
  encrypted (Fernet) Redis job payload with 120 s TTL for unsaved tests. Note: the ZK protocol is unencrypted and the comm
  key scramble is weak; risk accepted for a LAN segment; recommend the devices sit on their own VLAN and set a comm key on
  each device (the app cannot set it).
- Rogue-device / spoofing: identity pinning by serial, IP allow-list, firewall, punches never applied outside the window.
- Device admins (`is_device_admin`) are listed so HR can review who can change device data.
- Strings from the device (names, ids) are untrusted: length-limited, control characters stripped, export formula-escaped.
- Rate limits: Sync now 1 per device per 60 s (429), test connection 6 / min per admin, list endpoints 60 / min.
- Audit: `device.create|update|delete|enable|disable` (field names; `comm_key_changed` bool), `device.test`,
  `device.sync` (counts), `device.sync_failed` (error code), `device.circuit_open`, `device.identity_changed`,
  `device.identity_accepted`, `device.clock_drift`, `device.log_count_decreased`, `device_user.map|ignore|auto`
  (device_user_id + employee_id), `attendance.device_use` (use-device), `attendance.device_unapply`.
- DPDP: add a short in-app privacy notice (More -> Privacy) and README text: "Office attendance machines: Employee Connect
  receives only your device user number, name, card number and the time and method of each punch. Fingerprint or face data
  stays on the machine and is never copied to Employee Connect." The biometric enrolment on the machine itself is the
  company's existing process (consent outside this app). Retention: punches `device.punch_retention_days` (1095),
  sync history 90 days, device user names / cards of rows absent from the device for 90 days are nulled (job
  `devices.retention`, daily 04:00).

## 9. Flutter
- Console (web, admin): Admin -> **Attendance devices** with tabs Devices | Unmatched users | Punch log.
  - Devices list: name, location, IP, status chip (OK / Failing / Offline (circuit) / Disabled / Clock drift), last sync
    (relative), clock offset, unmatched count, actions Sync now / Sync all.
  - Add / Edit device: name, IP, port, TCP/UDP, comm key (write-only: keep / set / clear), location, timezone (default
    office), ingest from, clock correction, enabled. **Test connection** (works before saving) shows serial, model,
    firmware, device time, offset, user / record counts, or the error in plain words.
  - Device detail: status card, last error, identity (serial) with "Accept new identity", sync history table.
  - Unmatched users: device user id, name, devices, last punch, unapplied punches; Map to employee (search picker,
    "all devices" checkbox), Ignore. Matched / ignored filter with Change / Reset to auto.
  - Punch log: filters device, employee, date range (<= 92 days), outcome; read-only.
- Mobile admin (HR / admin, `device.view`): Devices status (read-only + Sync now for `device.map`), Unmatched users with
  mapping, Punch log. Device add / edit / test only in the console (keeps comm keys off phones).
- Manual attendance screen: shows the day's device punches and, on a manual day with punches, "Use device punches".
- Employee: Today card shows "Checked in 09:02 at Office device (Main entrance)"; history / calendar day detail gets a
  source badge (Phone / Device / HR) and the day's punch list (`GET /attendance/punches`). Settings screen: `device.*` keys.
- Privacy screen (More -> Privacy) with the notice above.

## 10. Test plan
- **Fake ZK server** `backend/tests/fakes/zk_server.py` (asyncio, TCP + UDP): configurable users, logs (8/16/40-byte
  formats), clock, comm key, serial, latency, busy, drop mid-transfer, malformed / oversized packets, wrong checksum.
  Records every command received; a fixture asserts after every test that only allow-listed commands were seen.
- Unit: codec golden bytes, checksum, comm-key scramble, time decode, record parsers (PIN dropped), allow-list refuses every
  known write / template command (CLEAR_ATTLOG 15, CLEAR_DATA 14, DELETE_USER 18, USER_WRQ 8, SET_TIME 202,
  DISABLEDEVICE 1003, ENABLEDEVICE 1002, RESTART 1004, POWEROFF 1005, UNLOCK 31, DB_RRQ 7, DELETE_USERTEMP 19, REFRESHDATA
  1013) and inner buffer commands other than 9/13; static test: the client module has exactly one socket write path.
- Sync: idempotent double sync, count-unchanged skip, count-decrease alert, ingest_from, quarantine (invalid / future),
  drift correction on / off, identity change, auth failure, busy, timeout, circuit breaker open / half-open / reset,
  one-connection-per-device lock, manual job queue and status, rate limits.
- derive_day matrix: device only, app only, both (earlier / later), duplicates and gap, single punch + auto_close then late
  check-out, night shift across midnight, holiday / weekly off, late with grace / permission / first-half leave, full-day
  leave, on duty (setting on / off), manual wins + use-device, mapping change / removal (row deleted only when device-only),
  outside window, payroll locked, inactive user, concurrent phone check-in.
- DB: migration 0030 upgrade / downgrade on PostgreSQL 16 (`test_migration_0030.py`), append-only trigger (UPDATE,
  TRUNCATE refused; DELETE refused under 90 days), CHECKs, unique keys, backfill of attendance sources.
- API: permission matrix for the three permissions, SSRF guard (CIDR, loopback, docker subnets), comm key never returned,
  card masked, contract shapes, export columns.
- Field test (with the customer, before enabling the schedule): `zk_probe` each device; tcpdump on port 4370 during a full
  sync to confirm only allow-listed commands; compare one day of punches with eTimeTrackLite; confirm eTimeTrackLite still
  downloads normally and its "delete after download" option is off.

## 11. Ordered work packages
**database-engineer**
1. Migration `0030_essl_devices`: tables and constraints of section 3, append-only trigger, attendance columns + backfill,
   permissions and role grants, `device.*` settings seed; update `database/schema.sql`, `database/seed.py`.
2. `test_migration_0030.py` (upgrade / downgrade / trigger / CHECKs / backfill).
3. Models `backend/app/models/devices.py` and the new attendance columns in `models/attendance.py`.

**backend-developer**
1. `app/integrations/zk/` read-only client with the allow-list; `tests/fakes/zk_server.py`; codec and parser tests.
2. `services/devices.py`: registry CRUD, SSRF guard, comm key handling, job queue (Redis), identity pinning.
3. `services/device_sync.py`: sync flow, clock handling, quarantine, matching, circuit breaker, alerts, audit.
4. `services/attendance.py`: extract `derive_day`; check-in / check-out write `app_check_*` and sources; manual endpoints
   set sources; `use-device`; record output fields; `GET /attendance/punches`.
5. Scheduler jobs `devices.sync` (every minute, short lock), `devices.reconcile` (00:20), `devices.retention` (04:00);
   system health `devices` block; export columns and `device_punches` type; settings validation.
6. Routers for section 21; `scripts/zk_probe.py`; deploy: netplan example, firewall script, worker fixed IP, `.env.example`
   (`DEVICE_ALLOWED_CIDRS`, `EGRESS_SUBNET`, `WORKER_EGRESS_IP`), README ops section.
7. Tests of section 10 (sync, derive_day matrix, API, permissions).

**flutter-developer**
1. Models / repositories for section 21 and the new `AttendanceRecord` fields.
2. Console: Attendance devices (list, form with test, detail, unmatched users, punch log).
3. Mobile admin: devices status, unmatched users mapping, punch log; manual attendance punches + "Use device punches".
4. Employee: Today / history source badges and punch list; Privacy screen; settings keys in admin settings.
5. Widget tests for the device form (comm key write-only), mapping flow and source badges.

**security-reviewer**
1. Prove read-only: allow-list review, fake-server command log, field tcpdump; confirm no template code path.
2. SSRF guard, firewall rules, worker-only connectivity, identity pinning, comm key handling (storage, logs, Redis payload).
3. Parser hardening (bounds, timeouts, malformed input), untrusted device strings in UI / export.
4. Privacy: no biometrics, PIN dropped, card masking, retention jobs, DPDP notice text; permission matrix; audit coverage.

**qa-tester**
1. Run the suites; end-to-end with the fake server through the console (add, test, sync, map, punch log, attendance).
2. Field checklist of section 10 with the real devices; regression of phone check-in / check-out, manual attendance,
   on duty, late, payroll days.

## 12. Open questions (defaults apply until answered)
1. eTimeTrackLite "delete logs from device after download": must be OFF, or punches can vanish before we poll. Default:
   assume off; the count-decrease alert detects it; if it cannot be turned off, set the interval to 2 min.
2. Device user id format vs employee id (e.g. `101` vs `EMP101`): default exact match, then `device.match_prefix` + leading
   zeros rule; the rest via the unmatched report.
3. Comm key on the devices: default none (0); recommended to set one on each device.
4. TCP or UDP: default TCP; switch per device if the model only speaks UDP.
5. Device timezone: default office timezone (Asia/Kolkata).
6. Device punches on an approved on-duty day: default NOT applied (`device.apply_on_on_duty_days` false).
7. Raw punch retention: default 1095 days (check the state Shops and Establishments register period).
8. In / out keys on the device: default ignored (first / last punch rule).
9. Apply window 7 days and payroll lock on processed / published runs: default as stated; older days via manual attendance.
10. Push to the employee on a device punch: default off (push not yet live in the app).
11. Quarantined punches: no "release" action in v1; HR uses manual attendance.
12. HR may run Sync now (`device.map`): default yes; adding / editing devices stays admin-only (console).
