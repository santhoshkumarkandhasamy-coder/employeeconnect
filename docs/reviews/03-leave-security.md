# Security review 03 - Leave and Permission (backend, database, Flutter)

Reviewer: security-reviewer (read-only). Scope: `backend/app/routers/leave.py`, `services/leave.py`, `schemas/leave.py`,
`services/notifications.py`, `services/fcm.py`, `jobs/scheduler.py` (leave rollover), `services/employees.py` (balance
creation), `services/files.py` (leave_attachment authorisation only), `models/leave.py`,
`database/alembic/versions/0003_leave_permission.py`, `tests/test_leave.py`, `frontend/lib/features/leave/**`,
`frontend/lib/core/cache/json_cache.dart`, and the contract in `docs/api-contract.md` sections 4 and 11.
Attendance/files code that another agent is editing at the same time was not reviewed. Tests were not run.

Counts: Critical 0 | High 0 | Medium 4 | Low 10 | Info/contract 6.

---

## A. What I checked and found sound

- IDOR on own requests: `get_mine` and `cancel` load the row and return 404 for non-owners (`services/leave.py:610-614`, `624-626`). `list_mine` always filters on `actor.id`.
- Approval scope: `_scope_check` (`services/leave.py:662-669`) blocks self-approval for everyone, `approve_all` = any user, `approve_team` = current `manager_id` only. `get_principal` (`core/rbac.py:58-69`) reloads the user, status and role permissions on every request, so a deactivated approver or a role change takes effect immediately.
- Races and balance integrity: request rows are locked `FOR UPDATE` before the status check (`_load_locked`, `:618-620`). Approve/reject/cancel lock request then balance, and apply locks user then balance, so lock order is consistent and double-approve or cancel-vs-approve cannot both succeed. `_move` refuses negative values, and the DB CHECKs `used,pending >= 0` and `used + pending <= quota` plus the two GiST EXCLUDE constraints (`0003_leave_permission.py:44-46, 74, 102`) are a real safety net.
- Year boundary: one request = one calendar year, and the balance year is `from_date.year` everywhere (apply, decide, cancel).
- Attachment binding: `attachment_file_id` must be the caller's own `leave_attachment` file (`:413-416`). Files are magic-byte checked, re-encoded or PDF-screened, and encrypted at rest.
- Mass assignment: the schemas declare explicit fields and the service copies only those fields. `status`, `user_id`, `days` and `approver_id` cannot be injected.
- Push payload: only `type`, `id`, a title and a body of the form "NAME requested leave" / "Your leave request was approved". No reason, comment or dates. Tokens are not logged.
- Logs and audit contain no reason text. Reject audit stores `has_comment` only.
- Client cache: `reason`, `decision_comment` and `attachment_url` are scrubbed before writing (`json_cache.dart:113-118`). Keys are scoped per user id, the cache is wiped on logout and on lock, and approval lists are never cached.
- Rollover is idempotent: the unique `(user, type, year)` constraint, savepoint insert, Redis job lock and `max_instances=1` together prevent double rows.

---

## B. Findings

### MEDIUM

**M1. Leave and permission reasons (often medical) are stored in plaintext, with no retention**
- `backend/app/models/leave.py:85` (`leave_requests.reason`), `:89` (`decision_comment`), `:115`/`:118` (permission). Migration `0003_leave_permission.py:59, 91`.
- Impact: profile PII, locations and selfies are column-encrypted (`EncryptedString`/`EncryptedJSON`), but free-text such as "chemotherapy follow-up" or "pregnancy scan" sits in clear in the DB, WAL and every `pg_dump` backup (`database/scripts/backup.sh`). A DB read (SQLi elsewhere, leaked backup, read-only DBA) exposes health data. Nothing ever purges reasons or attachments of old, rejected or cancelled requests.
- Fix: map `reason` and `decision_comment` to `EncryptedString` (columns `reason_enc`, `decision_comment_enc`) with a data migration, and include them in `scripts.rotate_keys`. Add a retention job, for example `leave.reason_retention_days` (default 2 years after `to_date`), that nulls reasons and deletes attachment files of closed requests.

**M2. Attachment and max-consecutive rules can be bypassed by splitting leave into adjacent requests**
- `backend/app/services/leave.py:400-412` evaluates `max_consecutive_days` and `requires_attachment_after_days` per request only. The overlap check (`:419-430`) and the EXCLUDE constraint allow back-to-back ranges.
- Attack: sick leave needs a certificate above 2 days. The employee files Mon-Tue (2 days) and Wed-Thu (2 days) as two requests, so no attachment is needed. In the same way, a type with `max_consecutive_days=5` can be chained indefinitely. The client's year split (`request_tab.dart:223-266`) is a legitimate source of the same pattern.
- Fix: before the checks, merge the new range with the user's pending or approved requests of the same type that touch it, including gaps made only of weekly-offs or holidays, and apply both rules to the merged block. If an attachment is needed, accept it when any request in the block has one. Add tests for both bypasses.

**M3. No rate limiting on apply, cancel or decide, and push fan-out amplifies it**
- `backend/app/routers/leave.py:32-60, 95-114`. Only `/files` and attendance call `rate_limit.enforce`.
- Attack: a script loops apply then cancel. Each apply writes 2 audit rows and sends a push to the manager, or, when the user has no active manager, to **every** `request.approve_all` holder (`services/leave.py:363-378, 466-473`). That spams all HR/admin phones and grows the audit log without bound. Lock contention on the user and balance rows is a minor DoS.
- Fix: call `rate_limit.enforce` on apply and cancel (both kinds) with per-user keys, for example 10 per minute and 50 per day, plus a looser per-approver limit on decide. Optionally coalesce `new_request` pushes per approver, for example at most one per requester per 10 minutes.

**M4. Leave-attachment read access is not bound to a request**
- `backend/app/services/files.py:266-276`: any `request.view_team` holder can read **every** `leave_attachment` file whose owner currently reports to them. `request.view_all` holders can read every such file.
- Impact: a manager can read medical documents an employee uploaded but never submitted (orphans live up to 24h, `cleanup_orphans`), documents from requests the employee cancelled, and documents from before the manager took over the team. Because the file id is not tied to a request, the same blob can also be attached to several requests (it already is with the year split). Access is audited (`file.view_other`), which limits the impact.
- Fix: for `leave_attachment`, authorise through the request: EXISTS a `leave_requests` row with this `attachment_file_id` that the actor may see under `scope_conditions`, and deny unattached files to everyone except the owner. Optionally enforce one request per file with a unique partial index on `leave_requests(attachment_file_id) WHERE attachment_file_id IS NOT NULL`, and let the client reuse the file across split parts only through an explicit server-side copy.

### LOW

**L1. Far-future dates cause a 500, and there is no horizon on future leave**
- `services/leave.py:431` calls `get_balance` (`:119-133`). For `from_date` in year 2201 or later, the insert violates `ck_leave_balances_year`. The `except IntegrityError` treats this as "a concurrent request created it", and `scalar_one()` then raises `NoResultFound`, which returns 500 and a stack trace in the logs. Nothing limits how far ahead leave can be booked either: an employee can pre-book years whose quotas HR has not set yet.
- Fix: in `apply_leave`, reject `from_date > today + leave.max_advance_days` (for example 365) with `INVALID_DATE_RANGE`. In `get_balance`, re-raise when the follow-up select finds nothing.

**L2. Requests of deactivated or deleted employees stay pending and remain approvable**
- `services/leave.py:675-702` never checks `requester.status` or `deleted_at`. `services/employees.py:313-335, 374-395` does not touch the user's pending requests.
- Impact: approval queues fill with stale items, `pending` balance stays reserved, and an approver can still move balances of a terminated user.
- Fix: in `set_active(False)` and `delete_employee`, cancel the user's pending leave and permission requests (restoring `pending`) and audit it as `leave.auto_cancel`. In `decide`, return 409 `NOT_PENDING` (or a new `REQUESTER_INACTIVE`) for inactive requesters.

**L3. Denied approval attempts are not audited**
- `services/leave.py:662-669, 679`: self-approval and out-of-scope `FORBIDDEN` raise with no audit row, while `files.authorize` does audit denials. A manager probing other teams' request ids leaves no trace.
- Fix: write `request.decide_denied` (outcome failure, entity id, reason self or scope) before raising, committed like `file.access_denied`.

**L4. The approve endpoint reveals that a request exists and locks rows before authorisation**
- `services/leave.py:675-679`: an unknown id returns 404, a foreign id returns 403. The row is also locked `FOR UPDATE` before the scope check. Ids are UUIDv4, so the impact is minimal.
- Fix: return 404 for out-of-scope requests (as for files), or run the scope check on an unlocked read first.

**L5. Pro-rata quota follows `join_date` only at row creation**
- `services/leave.py:93-106`, `services/employees.py:251, 283-288`. An employee created with `join_date = null` gets the full quota for the current year. Setting or correcting `join_date` later (via `update_employee`) never recomputes rows that already exist. A past `join_date` creates a row only for that old year. The audit trail shows the join_date change but not the quota effect.
- Fix: when `join_date` changes and a balance row for the join year has `used = pending = 0`, recompute the quota and audit it, otherwise flag it for HR. Require `join_date` on create.

**L6. Gaps in permission and leave interplay**
- `services/leave.py:485`: `date == today` with times already past is accepted, so permission can be requested after the fact. Today that does not retroactively change `minutes_late` (computed at check-in), but any later recomputation would turn it into a lateness waiver.
- `:511-523`: only **approved full-day** leave blocks permission. A pending leave, or a half-day leave covering the same session, does not. `decide` for leave (`:689-702`) never checks approved permissions on the same dates, so both can be approved for one day.
- `:489`: for overnight shifts (`end_time <= start_time`) the shift-hours check is skipped entirely.
- Fix: require `from_time` after the current office-time-zone time when `date == today` (or allow a small configurable grace), check permission against pending and approved leave including the half-day session window (first = shift start to midpoint), check approved permissions when approving leave, and handle overnight windows explicitly.

**L7. New-request push can reach a non-approver, and real approvers get nothing**
- `services/leave.py:363-368`: if `manager_id` points to an active user **without** `request.approve_team`, only that user is notified, with the requester's name. They cannot act on it, the `approve_all` holders are not told, and the request stalls.
- Fix: in `_approvers`, accept the manager only if their role holds `request.approve_team` or `approve_all`. Otherwise fall back to the `approve_all` list.

**L8. GET /leave/balance writes balance rows for any year from 2000 to 2200**
- `services/leave.py:298-306`: each call with a new year inserts one row per leave type and commits. That is bounded (201 years x types per user), but it is a write on a GET and pollutes old years with full quotas.
- Fix: create rows only for the previous, current and next year, and return computed or empty data for other years without inserting.

**L9. Year rollover runs as one transaction with a 1-hour misfire window**
- `jobs/scheduler.py:96, 102`, `services/leave.py:154-164`: all users run in one transaction, so one bad row (for example a type with an invalid quota) rolls back the whole run. A worker down for more than 1h on Jan 1 skips the job. Lazy creation covers both cases functionally, so there is no data loss. The `created` count in the audit overstates when rows already existed through a race.
- Fix: commit per user or in batches, log failures per user, and also run the job on worker start-up when the current year's rows are missing.

**L10. Tests are missing for the security-relevant paths**
- `backend/tests/test_leave.py` has no tests for: concurrent approve vs cancel and double approve (only `test_rapid_submits_never_overdraw`), a cross-user attachment id on apply, a manager reading a non-report's attachment, the far-future year (L1), the push payload containing no reason, out-of-scope decide, and the split bypass (M2).
- Fix: add these, including a two-session concurrency test with `asyncio.gather` on approve and cancel that asserts exactly one wins and the balance stays consistent.

### INFO / CONTRACT MISMATCHES

1. `apply_leave` with an unknown `leave_type_id` returns 404 `NOT_FOUND` (`services/leave.py:386-387`). That is not in the contract error list, and the client maps it to "This request could not be found." (`leave_logic.dart:149-150`). Return 422 `VALIDATION_ERROR` with field `leave_type_id`, or document it.
2. Cancelling an **approved permission** is gated by the setting `leave.allow_cancel_approved` (`services/leave.py:632-636`). The contract table says permission cancel is "pending only". Document it or add `permission.allow_cancel_approved`.
3. The approvals UI shows only "Attached" and cannot open the document (`request_widgets.dart:142`), so approvers cannot verify certificates in-app. When adding a viewer, fetch with the Bearer header into memory (no temp file, or delete it right away), and never use `url_launcher` with a token in the URL.
4. Year split, partial failure: `request_tab.dart:278-283` sets `_file = null`, so retrying the remaining part fails with `ATTACHMENT_REQUIRED` until the user picks the file again. Keep `fileId` for the retry. The client also attaches one `fileId` to several requests (see M4).
5. `leave_providers.dart:13` uses `DateTime.now().year` (device time zone) while the server uses the office time zone. Around New Year the client can show the wrong year's balance. The client never shows next year's balance even when the user is booking January.
6. The client defines `RATE_LIMITED` (`leave_logic.dart:151`), but the backend never emits it for leave (see M3). Field names, status values, `INSUFFICIENT_BALANCE` with `details[0].field` = remaining, `PERMISSION_LIMIT_EXCEEDED` with count or minutes, page_size up to 100 and push `type` routing all match the contract.

---

## C. Summary

Critical 0, High 0, Medium 4, Low 10, Info 6.

Top issues:
1. M1: medical reasons and decision comments in plaintext with no retention. Encrypt them and add a purge job.
2. M2: the attachment and consecutive-day rules can be dodged by filing adjacent split requests.
3. M4: the leave-attachment ACL is owner/manager-based, not request-based, so managers can read unsubmitted or historical medical files.
4. M3: no rate limit on apply, cancel or decide, and the push fan-out to every HR user amplifies spam.
5. L1/L2: a far-future date gives a 500, and deactivated users' pending requests stay approvable.
