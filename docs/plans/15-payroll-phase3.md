# 15 - Payroll phase 3: pay components feeding the run

Status: PROPOSED. Branch `feature/payroll-phase2` (or a new `feature/payroll-phase3` cut from it). Source: plan 14 phase 3.
Contract: `docs/api-contract.md` section 17 (single source of truth; this plan only explains it). Migrations `0022` (3a) and `0023` (3b).
All plan 12 rules still apply: money as 2-decimal strings / `Decimal`, every amount encrypted at rest, no SQL aggregates on money (D2),
audit every view/change without amounts, step-up (`current_password`) on sensitive writes, nobody acts on their own money (D9), published = locked (D4).
Nothing is deployed until the user approves.

## 1. Design decisions
| # | Decision |
|---|---|
| P1 | **One landing path.** Every component reaches a payslip as a line inside the encrypted `payslips.inputs_enc` JSON, in a NEW key `inputs.components` (system lines), next to the existing `inputs.adjustments` (manual lines). `_pay_inputs()` feeds both to `payroll_calc.calculate()` as `Adjustment`s. No payslip column, trigger or lock rule changes. |
| P2 | **Components are pulled, not typed.** `PUT /admin/payroll/payslips/{id}` cannot change `components` (read-only in AdminPayslip). They are (re)pulled from the source tables at run create, run refresh, run process, single payslip process and revise. `PUT` keeps the stored components (same as it keeps the structure). |
| P3 | **New system codes** `OVERTIME` (earning, ESI wage) and `LEAVE_ENCASH` (earning, not ESI wage) join `EARNING_ADJUSTMENTS` / `RESERVED_CODES`. They are NOT accepted in manual `adjustments` (422). Loans use existing `LOAN_EMI` / `ADVANCE_RECOVERY`, reimbursements `REIMBURSEMENT`, arrears `ARREARS` (negative arrears: `OTHER_DEDUCTION`), one-time items any of `BONUS, INCENTIVE, OTHER_EARNING, OTHER_DEDUCTION`. Register and statutory pick the codes up automatically (`payroll_register.py` groups by `EARNING_ADJUSTMENTS`; `payroll_statutory._esi_wage` uses `ESI_ADJUSTMENTS`). Published payslips are unaffected (no OVERTIME lines exist yet). |
| P4 | **Shared payable lifecycle** on every source row: `pending` (payable, waiting) -> `processing` (inside a processed payslip, `payslip_id` set) -> `paid` (payslip published, `payslip_id` = published copy). Reopen moves `processing` -> `pending`. Only `pending` rows (and the earlier per-kind stages) can be edited or cancelled. `paid` rows are immutable (DB trigger) and are carried into every later revision of that payslip; to reverse a paid amount HR adds an opposite one-time item in a later month. |
| P5 | **Pull rule** for payslip P (user U, period M): source rows of U with `period = M` and `status = 'pending'`, plus rows with `status = 'paid' AND payslip_id = P.supersedes_id`. Period-exact: a row whose month is published without a payslip for U stays `pending`, is flagged `PERIOD_CLOSED` in lists, and HR moves it (edit period / defer) or cancels it. Nothing catches up automatically. |
| P6 | **State changes are in the payroll transaction.** Process: lock run -> payslips -> source rows (`FOR UPDATE`, fixed table order, `ORDER BY id`), re-pull, recompute, then set `processing`. Publish (run or payslip): `processing` (payslip_id = P) -> `paid`; `paid` rows of the superseded copy get `payslip_id = P`. Reopen (run or payslip): back to `pending`, `payslip_id = NULL`. Held payslips stay draft, so their rows stay `pending`. A publish that finds a referenced source row in an unexpected state fails with 409 `COMPONENTS_CHANGED` (defence in depth). |
| P7 | **Global lock order** (extends plan 12): `payroll_runs` -> `payslips` -> `payroll_one_time_items` -> `payroll_loans` -> `payroll_loan_installments` -> `payroll_arrears` -> `leave_balances` -> `leave_encashments` -> `reimbursement_claims` -> `overtime_days`. Source-only actions lock only their own rows in this order (e.g. foreclose: loan -> installments; encashment cancel: balance -> encashment). |
| P8 | **Money history is append-only.** Every create / edit / state change of any source row appends to `payroll_item_events` (encrypted amount + previous amount, period + previous period, encrypted detail). No DELETE on any phase 3 table for `app_user` (cancel is a status). Same trigger pattern as `statutory_challan_events` (0021), key rotation allowed. |
| P9 | **Where a new row may land.** Target month must be: no run yet, a `draft` run, a `processed` run (lands after reopen + process; the response says `run_status`), or a `published` run in which U has an open draft revision. Otherwise 409 `PERIOD_LOCKED`. Months more than 12 ahead of today are refused (422 `INVALID_PERIOD`). |
| P10 | **Overtime** off by default (`payroll.overtime_enabled=false`), rate 2x the ordinary hourly rate of BASIC: `basic / overtime_month_days (26) / shift hours`. Per-employee eligibility flag (default false). Hours come from attendance; HR reviews every day row before it counts. Amount is computed inside `recompute()` from approved minutes and the structure the payslip adopts, so it always matches the payslip. |
| P11 | **Reimbursement approval** = reporting manager first (`verify`), then HR (`payroll.manage`) approves the final amount (<= claimed) and the payout month. If the employee has no active manager holding `request.approve_team`/`approve_all`, the claim goes straight to HR (`manager_step: "skipped"`). A manager who also holds `payroll.manage` may do both steps in one `approve` call (audited `combined:true`). Never on your own claim. |
| P12 | **Auto arrears** are proposals, never automatic money: a structure change that affects a month with a published payslip creates/refreshes an `ARREARS` proposal; HR accepts it into an open month (step-up). Arrears follow the current ARREARS treatment: **not PF wages, not ESI wages** (see open question Q1). |
| P13 | **Leave encashment** reserves the days in the leave balance at once (new `leave_balances.encashed`), amount = days x daily rate fixed at creation (`basic / payroll.encashment_divisor (30)`). HR-initiated (on the employee's request or for F&F in phase 4); no employee self-service in phase 3. |
| P14 | **Delivery** = push only (data messages, never amounts). No email / WhatsApp. |
| P15 | **Loans** are interest-free, EMI fixed, schedule generated at creation (last installment = remainder). Max tenure 60 months, max 3 active loans/advances per employee (settings). |

## 2. Database
Common column rules for every new table: `id uuid PK default gen_random_uuid()`, `user_id uuid NOT NULL FK users RESTRICT`, money as `*_enc bytea`
(model `EncryptedString`, value = 2-decimal string), periods `date` with `CHECK (extract(day from period)=1)` and `BETWEEN 2000-01-01 AND 2200-12-31`,
`payslip_id uuid NULL FK payslips RESTRICT` with `CHECK ((status IN ('processing','paid')) = (payslip_id IS NOT NULL))`, `version int NOT NULL DEFAULT 1 CHECK >= 1`,
`created_by / updated_by uuid NULL FK users`, `created_at / updated_at` + `set_updated_at` trigger, an index on every FK, `REVOKE DELETE, TRUNCATE FROM app_user`.
Trigger `payroll_item_paid_lock` (shared function) on every source table: UPDATE of a row whose OLD.status = 'paid' is refused unless only `payslip_id`, `updated_*`, `version`
change (revision carry-over) or it is a key rotation (`app.key_rotation='on'` and `payroll_strip_enc` equal). Labels / reasons follow plan 12 (1-40 / 3-200 chars, cleaned).

### 2.1 Migration 0022 (work package 3a)
**`payroll_item_events`** (append-only; 0021 pattern: trigger blocks UPDATE/DELETE/TRUNCATE except key rotation, `REVOKE UPDATE, DELETE, TRUNCATE`, column-level UPDATE on `*_enc` only)
`item_type text CHECK IN ('one_time','loan','loan_installment','arrears','encashment','reimbursement','overtime')`, `item_id uuid NOT NULL` (no FK, polymorphic),
`user_id FK`, `action text CHECK IN ('create','update','cancel','skip','reschedule','foreclose','close','propose','accept','dismiss','verify','approve','reject','process','reopen','pay','carry')`,
`status text NOT NULL`, `period date NULL`, `prev_period date NULL`, `amount_enc`, `prev_amount_enc`, `detail_enc bytea NULL` (encrypted JSON: reason, reference, minutes),
`payslip_id uuid NULL` (no FK), `actor_id uuid NULL FK users` (NULL = system), `created_at`. Index `(item_type, item_id, created_at)`, `(user_id, created_at)`.

**`payroll_import_batches`**: `period date`, `file_sha256 text CHECK ~ '^[0-9a-f]{64}$'`, `rows int CHECK BETWEEN 1 AND 500`, `created_by FK NOT NULL`, `created_at`,
`cancelled_at`, `cancelled_by`. `UNIQUE (file_sha256, period)`.

**`payroll_one_time_items`**: `period`, `code text CHECK IN ('BONUS','INCENTIVE','OTHER_EARNING','OTHER_DEDUCTION')`, `label_enc`, `amount_enc` (> 0, checked in service),
`status CHECK IN ('pending','processing','paid','cancelled')`, `batch_id FK payroll_import_batches NULL`, `payslip_id`, `cancel_reason_enc`, `version`, audit cols.
Index `(user_id, period, status)`, `(period, status)`, `(batch_id)`.

**`payroll_loans`**: `kind CHECK IN ('loan','advance')`, `reference text NOT NULL UNIQUE` (`L-0001`, sequence, shown to the employee), `principal_enc`, `emi_enc`,
`start_period`, `reason_enc`, `status CHECK IN ('active','closed','cancelled')`, `closed_at`, `close_mode CHECK IN (NULL,'recovered','foreclosed_payroll','foreclosed_outside','cancelled')`,
`version`, audit cols. Index `(user_id, status)`.

**`payroll_loan_installments`**: `loan_id FK RESTRICT`, `user_id` (denormalised for the pull query), `seq smallint CHECK BETWEEN 1 AND 120`, `period`, `amount_enc`,
`kind CHECK IN ('emi','foreclosure')`, `status CHECK IN ('pending','processing','paid','skipped','cancelled')`, `payslip_id`, `version`.
`UNIQUE (loan_id, seq)`; partial unique `(loan_id, period) WHERE status IN ('pending','processing','paid')` (one live installment per loan per month). Index `(user_id, period, status)`.

**`payroll_arrears`**: `period NULL` (target month, set on accept), `from_period`, `to_period` (published months covered), `trigger_structure_id FK salary_structures NULL ON DELETE SET NULL`,
`code CHECK IN ('ARREARS','OTHER_DEDUCTION')`, `amount_enc`, `breakdown_enc` (JSON per month: `{period, paid_structure_id, new_structure_id, paid_earnings, new_earnings, already_paid_arrears, diff, basic_diff}`),
`status CHECK IN ('proposed','pending','processing','paid','dismissed')`, `CHECK (status IN ('proposed','dismissed') OR period IS NOT NULL)`, `dismiss_reason_enc`, `payslip_id`, `version`.
Partial unique `(user_id) WHERE status = 'proposed'` (one open proposal per employee, recomputed in place).

**`leave_encashments`**: `leave_type_id FK`, `year int CHECK BETWEEN 2000 AND 2200`, `days numeric(4,1) CHECK (days > 0 AND days*2 = trunc(days*2))`, `daily_rate_enc`, `amount_enc`,
`period`, `source CHECK IN ('request','fnf')`, `reason_enc`, `status CHECK IN ('pending','processing','paid','cancelled')`, `payslip_id`, `version`. Index `(user_id, year)`, `(user_id, period, status)`.

**Changes**: `leave_balances.encashed numeric(4,1) NOT NULL DEFAULT 0 CHECK >= 0`; replace `ck_leave_balances_not_overdrawn` with `used + pending + encashed <= quota`.
`leave_types.encashable boolean NOT NULL DEFAULT false`, `encash_max_days_per_year numeric(4,1) NULL CHECK > 0`, `encash_min_balance numeric(4,1) NOT NULL DEFAULT 0 CHECK >= 0`.
Settings rows seeded (contract 17.9). Grants for `app_user` (SELECT, INSERT, UPDATE; no DELETE). Regenerate `database/schema.sql`.

### 2.2 Migration 0023 (work package 3b)
**`reimbursement_claims`**: `category text CHECK ~ '^[a-z][a-z0-9_]{1,19}$'`, `expense_date date`, `amount_enc` (claimed), `approved_amount_enc NULL`, `description_enc`,
`bill_file_id FK files RESTRICT NULL`, `status CHECK IN ('submitted','verified','approved','processing','paid','rejected','cancelled')`,
`manager_step CHECK IN ('pending','done','skipped')`, `verifier_id FK NULL`, `verified_at`, `approver_id FK NULL`, `decided_at`, `decision_comment_enc`, `period NULL`
(payout month, `CHECK (status NOT IN ('approved','processing','paid') OR (period IS NOT NULL AND approved_amount_enc IS NOT NULL))`), `payslip_id`, `version`.
Partial unique `(bill_file_id) WHERE status NOT IN ('rejected','cancelled')` (a bill backs one live claim). Index `(user_id, status)`, `(status, created_at)`, `(user_id, period, status)`.
`files.purpose` CHECK gains `'reimbursement_bill'`.

**`overtime_days`**: `work_date`, `period` (= first of work_date's month), `attendance_id FK attendance SET NULL`, `computed_minutes int CHECK BETWEEN 0 AND 1440`,
`approved_minutes int NULL CHECK BETWEEN 0 AND 1440`, `flags text[] NOT NULL DEFAULT '{}'` (`AUTO_CLOSED`, `SUSPICIOUS`, `OFF_DAY`, `ATTENDANCE_CHANGED`),
`status CHECK IN ('computed','approved','rejected','processing','paid')`, `CHECK (status IN ('computed','rejected') OR approved_minutes IS NOT NULL)`, `reviewer_id FK NULL`,
`reviewed_at`, `comment_enc`, `payslip_id` (rule adapted: `processing`/`paid` <-> payslip_id), `version`. `UNIQUE (user_id, work_date)`. Minutes are not money: plaintext.
For overtime the "pending" state is `approved` (P4 table in 17.0). `employee_payroll_details.overtime_eligible boolean NOT NULL DEFAULT false`.

## 3. Business rules and edge cases
**Common**
- Self rule: no create/edit/approve/cancel on rows of your own user (403 `FORBIDDEN`, audited `payroll.denied`). Bulk import rows for yourself are row errors.
- Users must be payroll-eligible for the target month (active or leaving inside it); deleted users -> 422.
- Item caps per employee-month: <= 20 one-time items, <= 40 component lines in total (else 409 `TOO_MANY_ITEMS`).
- `NEGATIVE_NET_PAY` stays a blocking error; HR fixes it by skipping an installment, moving an item or holding the payslip.
- New payslip warnings: `OVERTIME_UNREVIEWED` (computed rows exist for the month), `OVERTIME_CAPPED`, `ARREARS_PROPOSED` (open proposal), `MANUAL_COMPONENT_OVERLAP`
  (a manual adjustment uses a code that also has a system line, e.g. manual `LOAN_EMI` plus a loan installment: likely double counting).
- Variance report: new reason `COMPONENTS` when the system lines differ from the previous month.

**One-time items and bulk upload (3a)**
- Single create/edit/cancel; edit allowed only while `pending` (`expected_version`).
- Import: xlsx only, <= 1 MB, magic `PK\x03\x04`, <= 50 zip entries, <= 10 MB uncompressed and ratio <= 100 (checked from the zip directory BEFORE openpyxl), no
  `xl/vbaProject.bin`, no external links; openpyxl `read_only=True, data_only=True` in a worker thread with a 10 s timeout. Sheet 1 only, header row must match the template
  (`Employee ID, Code, Label, Amount`), <= 500 data rows, blank rows ignored. Formulas are refused (cell type formula or text starting with `= + - @`). The file is never stored.
- Dry run returns every row error and per-code counts/sums; commit re-sends the same file + `file_sha256` + `current_password` and is all-or-nothing in one transaction.
  Same file for the same period twice -> 409 `IMPORT_DUPLICATE`. A batch can be cancelled as a whole: pending rows are cancelled, others are reported.

**Loans and advances (3a)**
- `principal` 1.00..1000000.00, `emi` > 0 and <= principal, installments = ceil(principal / emi) <= `payroll.loan_max_tenure_months` (60). `start_period` open per P9.
- `advance` = recovered as `ADVANCE_RECOVERY`, `loan` = `LOAN_EMI`; label `"Loan L-0007 (3/12)"`.
- Skip (defer) one pending installment: it becomes `skipped`, a new one with the same amount is appended after the last. Not when `processing`/`paid`.
- Reschedule: new EMI from a month on; all pending installments from that month are `cancelled` and regenerated from the outstanding (principal - paid - processing).
- Foreclose: `payroll` = cancel pending installments, add one `foreclosure` installment = outstanding in an open month; `outside` = cancel pending, record `paid_on` + reference
  (encrypted, event only), loan `closed`. Refused while any installment is `processing` (409 `ITEM_LOCKED`).
- Cancel a loan only when nothing is processing or paid (409 `LOAN_HAS_RECOVERIES`). The loan closes itself (`recovered`) when the sum of paid installments = principal (at publish).
- Deactivated employees: schedule stays; phase 4 F&F recovers the outstanding. Until then lists flag `PERIOD_CLOSED` installments.

**Auto arrears (3a)**
- Trigger: create / update / delete of a structure whose `effective_from` <= the latest month with a published payslip for that employee; also `POST .../arrears/recalculate`.
- For each published month M from `effective_from`: live copy p; new = structure in force for M now; if new.id = p.structure_id -> 0. Else
  `diff = structure earnings(new, p.basis_days, p.paid_days, run.config rules) - structure earnings paid on p (BASIC, HRA, allowances lines of p.result) - ARREARS already paid for M`
  (from earlier paid arrears breakdowns). Rounding per run config. LOP of that month is honoured through `paid_days`.
- Total > 0 -> `ARREARS` earning; < 0 -> `OTHER_DEDUCTION` "Salary recovery (arrears)"; |total| < 1.00 -> no proposal (an existing proposal is dismissed by the system).
- One open proposal per employee, recomputed in place (version + 1, event `propose`). Accepted-but-pending rows are reset to `proposed` and recomputed when a later structure change
  alters them (event, push nothing). `processing`/`paid` rows are never touched; the next recompute subtracts them.
- Accept needs `expected_version` (409 `ARREARS_STALE` if recomputed meanwhile) and step-up.

**Leave encashment (3a)**
- Leave type must be `encashable`; `days` in 0.5 steps; `year` = current or previous year (the previous year's balance lapses otherwise, no carry-forward in v1).
- Available = quota - used - pending - encashed - `encash_min_balance`; also <= `encash_max_days_per_year` minus already encashed that year. Else 409 `INSUFFICIENT_BALANCE`.
- `daily_rate = monthly basic (or gross when payroll.encashment_basis = gross) of the structure in force for the payout month / payroll.encashment_divisor`, rounded to paise;
  amount = rnd(days x rate). No structure -> 422 `STRUCTURE_MISSING`.
- Create locks the balance row and adds `encashed`; cancel (pending only) gives the days back. `GET /leave/balance` and admin balances show `encashed`;
  `remaining = quota - used - pending - encashed`; `QUOTA_BELOW_USED` now compares with used + pending + encashed.

**Reimbursements (3b)**
- Categories from setting `payroll.reimbursement_categories` (<= 20: `{code, name, max_amount|null, bill_required}`), default travel, food, medical, phone, internet, other; all bill required.
- `expense_date` <= today and >= today - `payroll.reimbursement_max_age_days` (60). Amount > 0 and <= category `max_amount`. <= `payroll.reimbursement_max_claims_per_month` (30) claims per user per calendar month of submission.
- Bill: `POST /files` purpose `reimbursement_bill` (jpeg/png/pdf, <= 10 MB, encrypted at rest, own upload, single use). The same bytes (sha256) on another live claim -> 409 `DUPLICATE_BILL`.
- Employee cancels while `submitted` or `verified`. Manager `verify`/`reject` only while `submitted` and only direct reports (current manager at decision time; out of scope = 404).
  HR `approve` while `verified` (or `submitted` when `manager_step = skipped` or combined), sets `approved_amount` (<= claimed) and payout `period` (P9). HR `reject` while `submitted`/`verified`.
  HR can move the payout month while `approved`. Rejected/cancelled are final.
- The verifier and the final approver must differ unless the combined path applies (P11). Deactivation of the employee: `submitted` claims skip the manager step; nothing is auto-cancelled.
- Payslip line: one `REIMBURSEMENT` line per category per month, label `"Reimbursement - Travel"`, `sources` lists every claim.

**Overtime (3b)**
- Only when `payroll.overtime_enabled` and `overtime_eligible`. `compute` builds rows for the month from attendance rows with a check-out:
  working day: `worked_minutes - shift minutes` (half shift on an approved half-day leave); holiday / weekly off: all worked minutes (`payroll.overtime_offday_full`).
  Floor to `overtime_rounding_minutes` (30); below `overtime_min_minutes` (30) -> no row. Auto-closed days excluded unless `overtime_include_auto_closed`; suspicious days flagged.
  No shift -> no rows (the payslip already warns `NO_SHIFT`).
- Re-compute changes only `computed` rows; `approved`/`rejected` rows whose attendance changed get `ATTENDANCE_CHANGED` and keep their minutes.
- Review by `payroll.manage` (not self): approve (optionally fewer minutes than computed, never more), reject (comment). Bulk approve/reject up to 200 rows.
- Amount (in `recompute`): `hours = min(sum approved minutes, overtime_max_hours_month*60) / 60`; `hourly = basic / overtime_month_days / shift hours`;
  `OVERTIME = rnd(hours x hourly x overtime_multiplier)`. Uses the run snapshot of the settings.

## 4. Flutter screens and navigation
**Employee app (all roles)**
- Payslips screen becomes a tabbed "Pay" area: `Payslips | Loans | Claims` (Loans tab only if the user has at least one loan; Claims only with `reimbursement.submit`).
- Loans: list (reference, kind, outstanding, next installment) -> detail (principal, EMI, schedule with status `scheduled | deducted | deferred`). Read-only.
- Claims: list with status chips -> "New claim" form (category, expense date, amount, description, bill via camera / gallery / PDF picker, upload with progress) -> detail
  (status timeline, approved amount, payout month, "Cancel" while allowed, bill preview fetched with the bearer header into memory).
- My overtime (3b, only when enabled and eligible): month picker, day rows with minutes and status, no money.
- Manager: the existing Approvals screen gets a `Claims` segment (team claims in `submitted`): detail with bill, `Verify` / `Reject (comment)`.

**HR (app) and Admin (console) - shared widgets, new Payroll tab "Pay items"** with segments
`One-time | Loans | Arrears | Encashment | Claims | Overtime` (3b segments hidden until 3b ships; Overtime hidden when disabled).
- One-time: month picker, list, add/edit/cancel dialog, "Import" wizard (download template -> pick file -> dry-run report table -> confirm with password -> result), batches list with cancel.
- Loans: list + filters, create form (with live schedule preview computed locally, server is authoritative), detail with schedule and actions skip / reschedule / foreclose / cancel (password dialogs).
- Arrears: proposals list with per-month breakdown, accept (month + password) / dismiss; "Recalculate" on the employee payroll screen.
- Encashment: create form with server quote (`/encashment-quote`), list, cancel. Leave type admin form gains the three encashment fields.
- Claims (HR stage): queue `verified` + skipped, approve (amount, month) / reject, bill viewer, move payout month.
- Overtime: month, "Compute", per-employee grouping, approve/adjust/reject, bulk select.
- Payslip edit screen: read-only "Automatic lines" section listing `inputs.components` with a link to the source; new warning chips.
- Employee payroll details form: `overtime_eligible` toggle (3b).

## 5. Security considerations
- Encryption: every amount, label, reason, description, comment and breakdown in new tables is `*_enc`; extend `services/key_rotation.py` and `scripts/rotate_keys.py` to all of them.
- Audit (no amounts, no labels, no reasons): `payroll.item.*`, `payroll.import.dry_run|commit|cancel`, `payroll.loan.*`, `payroll.arrears.*`, `payroll.encashment.*`,
  `reimbursement.submit|cancel|verify|approve|reject|reschedule|view`, `payroll.overtime.compute|review`, `payroll.loan.view_own`. Allow-list new non-money keys in the redactor.
- Step-up: loan create / reschedule / foreclose / cancel, import commit, arrears accept, encashment create. Failed step-ups use the shared 5/min limiter and audit.
- Scope: manager stage only for current direct reports (404 outside scope, audited `reimbursement.denied`); bills readable by owner, the current manager while the claim is
  `submitted`, and `payroll.view_all` holders (`file.view_other` audit; cross-user). `cleanup_orphans` must treat bills referenced by claims as live.
- Bill uploads reuse `services/files.py` (magic bytes, image re-encode, PDF screen). Bills are never purged (financial record); `description` and comments are not purged either.
- XLSX import: zip-bomb and macro checks before parsing, size/row limits, worker thread with timeout and the xlsx build semaphore, formula refusal, file never written to disk.
- Concurrency: lock order P7; process/publish re-read sources `FOR UPDATE`; `paid` immutability and no-delete in the DB (trigger + revokes), append-only events.
- Pushes carry no amounts; employee loan/claim endpoints return 404 for anything not their own (audited probe).
- Rate limits fail closed on writes (section 17.0).

## 6. Test plan
- **Unit (pure)**: calc with `components` (OVERTIME ESI wage, LEAVE_ENCASH not, PT on new gross), `overtime_amount`, overtime minute derivation (working day, off day, half-day leave,
  rounding, min, cap, overnight shift), loan schedule (remainder, reschedule, foreclose), arrears diff (LOP months, multiple months, decrease, already-paid subtraction, < 1.00),
  encashment rate (basic / gross basis, divisor), xlsx parser (header, 500 rows, formulas, macro, zip bomb, ratio, entries, non-xlsx, timeout).
- **Lifecycle**: create/refresh pulls; PUT keeps components; process -> processing; reopen -> pending; publish -> paid; held payslip; deleted draft payslip; run delete;
  revision carries paid rows and pulls new pending ones; revision publish moves `payslip_id`; P5 period-closed rows; `COMPONENTS_CHANGED` guard.
- **Concurrency** (two sessions): cancel vs process, foreclose vs process, encash vs leave apply, approve claim vs process; no deadlock with P7 order.
- **DB**: migration 0022/0023 up/down/up; paid-row trigger; no DELETE/TRUNCATE for app_user; events append-only incl. key rotation exception; checks and partial uniques;
  leave balance constraint with `encashed`.
- **Security / API**: every endpoint's permission, self rule, scope 404s, step-up required/failed, rate limits incl. fail-closed, unknown fields 422, no amounts in audit/log/push,
  bill access matrix, duplicate bill, import replay `IMPORT_DUPLICATE`, `file_sha256` mismatch.
- **Integration**: register columns for OVERTIME/LEAVE_ENCASH, ESI statutory file includes OVERTIME, variance `COMPONENTS`, dashboard totals, payslip PDF lines, key rotation over new columns.
- **Flutter**: repository/model contract tests per new endpoint, widget tests for claim form (validation, upload errors), loan detail, import wizard states, approvals segment,
  automatic lines section; console smoke on the Pay items tab.

## 7. Work packages (each: contract -> DB -> backend -> Flutter/console -> qa-tester -> security-reviewer -> fixes; suites green before the next)
### 3a - admin side (migration 0022)
**database-engineer**
1. 0022: `payroll_item_events`, `payroll_import_batches`, `payroll_one_time_items`, `payroll_loans` (+ reference sequence), `payroll_loan_installments`, `payroll_arrears`,
   `leave_encashments`; shared `payroll_item_paid_lock` trigger; leave_balances / leave_types columns and replaced CHECK; settings seed; grants/revokes; downgrade.
2. ORM models, `schema.sql`, migration test (`test_migration_0022.py`).
**backend-developer**
3. Component engine: `services/payroll_components.py` (pull per payslip, state transitions, P5-P7), hook into `create_run`, `refresh_run`, `_recompute_open`, `process_payslip`,
   `revise`, `reopen_*`, `_publish_one`; `_pay_inputs` merges components; `admin_payslip_out` returns `inputs.components`; new warnings; calc codes (P3); `AdjustmentIn` refuses system codes;
   reserved-code check script `scripts/check_reserved_codes.py` (decrypts structures; run before deploy; fails if any allowance/fixed deduction uses OVERTIME or LEAVE_ENCASH).
4. One-time items CRUD + import (parser module `services/xlsx_import.py`) + template + batches.
5. Loans (schedule, skip, reschedule, foreclose, cancel, employee `/me/loans`), push `loan_created`.
6. Arrears engine (structure hooks in `payroll_structures.py`, recalculate, accept, dismiss).
7. Encashment (quote, create, cancel), leave balance `encashed` in leave service + section 4/9 outputs, leave type fields.
8. Settings validation for new keys; register / variance / key rotation updates; tests for all of section 6 (3a parts).
**flutter-developer**
9. Models/repositories for section 17 (3a); Pay items tab (One-time, Loans, Arrears, Encashment); import wizard; payslip automatic lines + warnings; leave type form fields;
   employee Loans tab; leave balance `encashed` display.
**security-reviewer** 10. Review 3a (locking, triggers, import parser, step-up, audit redaction, self rule). **qa-tester** 11. Contract + lifecycle + concurrency tests, Flutter suite.

### 3b - reimbursements and overtime (migration 0023)
**database-engineer** 1. 0023: `reimbursement_claims`, `overtime_days`, `files.purpose` CHECK, `employee_payroll_details.overtime_eligible`, `reimbursement.submit` permission seeded to all roles; downgrade; test.
**backend-developer**
2. File purpose `reimbursement_bill` (PURPOSES, GENERIC_PURPOSES, `authorize`, cross-user audit, `cleanup_orphans` refs).
3. Claims: employee endpoints, manager verify/reject, HR approve/reject/reschedule, pushes `reimbursement_submitted` / `reimbursement_decision`, pull into payslips.
4. Overtime: settings, compute job/endpoint, review endpoints, `/me/overtime`, calc `overtime_amount`, pull + cap warning; details `overtime_eligible`.
5. Tests (section 6, 3b parts).
**flutter-developer** 6. Claims tab + form + detail; manager Claims segment; HR Claims and Overtime segments; My overtime; details toggle.
**security-reviewer** 7. Review 3b (bill access matrix, scope, duplicate bills, OT data integrity). **qa-tester** 8. As 3a.

## 8. Open questions for the user (defaults are built if unanswered)
- **Q1 (needs the user)** PF / ESI on arrears and on bonus. Default: arrears and bonus are NOT PF or ESI wages (as today). Statutorily PF is usually due on arrears of basic and
  ESI on arrears paid in the month; doing that needs a basic-arrears split and the EPFO arrear ECR, which is not in this phase. Please confirm with your auditor.
- **Q2 (needs the user before enabling overtime)** OT base and divisor: default 2 x BASIC / 26 days / shift hours, OT counted as ESI wages, monthly cap 50 h. Verify against
  the Factories Act / Tamil Nadu Shops and Establishments rules that apply to the company.
- **Q3 (needs the user)** Encashment rate: default BASIC / 30. Alternatives: BASIC / 26, or gross / 30.
- Q4 Reimbursement: two steps (manager verify, then HR approve) as chosen; plan 14 also allowed "manager only, HR fallback". Confirm two steps.
- Q5 Loans are interest-free; no perquisite valuation (TDS stays manual). Employees cannot request encashment or loans in the app in phase 3.
