# 03 - Leave and Permission

Contract: api-contract section 4. Tables: `leave_types`, `leave_balances`, `leave_requests`, `permission_requests` (migration 0003), `holidays` (exists), `files`.

## Leave rules
- Balance rows per (user, leave_type, year), created by: admin create employee (all active types, pro-rata optional O1), Jan 1 rollover job, and lazily on first request. `remaining = quota - used - pending`; DB CHECK blocks negatives.
- Days = count of shift working days in range excluding holidays; half day = 0.5 (only same-day, only if type allows).
- Apply: validate type active, dates (not before today - `leave.backdate_days`, to>=from, not spanning year boundary in v1 -> split by client? v1: reject `INVALID_DATE_RANGE` with message O2), days>0, balance sufficient, no overlap with pending/approved (EXCLUDE + friendly pre-check), attachment required if `days > requires_attachment_after_days` (e.g. sick > 2 days).
- On submit: `pending += days` (same transaction, `SELECT ... FOR UPDATE` on balance row). On approve: `pending -= days; used += days`. On reject/cancel (pending): `pending -= days`. All with CHECK as safety net.
- Cancel allowed only while pending (spec). Cancelling approved leave: not in v1 (O3); Admin can reject->no, instead adjust via leave-balances endpoint.
- Approval: Manager (team) or HR/Admin; approver != requester; if requester has no manager, HR/Admin only. Reject requires comment. Re-validate balance and overlap at approval. Approving writes audit `request.approve`.
- Approved leave suppresses absence/late for those dates (used by attendance, late).
- Unpaid/LOP type: `annual_quota` large or flagged unlimited (O4).

## Permission rules (short absence, e.g. leave office early)
- Request: date (today or future, up to `leave.backdate_days` back disabled), from/to inside shift hours, `minutes` = difference, <= `permission.max_minutes_per_request`.
- Monthly limits per calendar month of request_date: count (pending+approved) <= `monthly_count_limit` AND minutes <= `monthly_minutes_limit`; else `PERMISSION_LIMIT_EXCEEDED`. Rejected/cancelled do not count. Balance endpoint derives used from approved, pending shown separately.
- No overlap with another permission (EXCLUDE) and not on a day covered by approved full-day leave (409 `OVERLAPPING_REQUEST`).
- Same approval flow, cancel only while pending.

## Notifications
New request -> push to the approver(s): manager, or all HR/Admin if none; decision -> push to employee. Failure never blocks.

## Edge cases
Concurrent submits by same user (row lock + EXCLUDE); two approvers acting simultaneously (`NOT_PENDING` for loser, status check with row lock); leave type deactivated while pending (still approvable); employee changes manager (pending requests visible to new manager by current `manager_id`); holiday added after request (days stay as computed, recalculation not done; O5); quota lowered below used+pending blocked; request touching weekends only -> `ZERO_WORKING_DAYS`.

## Flutter (features/leave)
Tabs: Balances (cards per leave type with remaining/quota, permission count/minutes this month), Request (segmented Leave | Permission; forms with type picker, date range picker, half-day switch + session, reason, attachment picker; live day count and balance preview), My Requests (filter chips by status, detail sheet with approver comment, Cancel on pending). Manager/HR: Approvals screen (in admin/approvals): pending list, detail, approve/reject with comment. Pull-to-refresh, cached balances.

## Security
Server-side scope on approvals (team vs all); attachment access limited to owner + approvers; file type checks; audit approve/reject; no self-approval.

## Tasks
1. database-engineer: migration 0003 incl. btree_gist EXCLUDE constraints, CHECKs, seed default leave types; index (status), (user_id, from_date).
2. backend-developer: working-day calculator (shared with attendance), balance service with locking, leave + permission services, approvals endpoints with scope, rollover job, push hooks, create balances on employee creation.
3. flutter-developer: screens above, repositories, approvals UI.
4. security-reviewer: IDOR on request ids, approval scope, attachments.
5. qa-tester: balance math (apply/approve/reject/cancel), insufficient balance, overlap, half-day, limits at boundary, race tests (two approvals), RBAC matrix.

Open: O1 pro-rata for mid-year joiners; O2 requests crossing year end; O3 cancel approved leave; O4 unlimited/LOP type; O5 holiday recalculation.
