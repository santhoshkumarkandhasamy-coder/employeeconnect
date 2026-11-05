# 16 - Payroll phase 4: increments, structure import, CTC / gratuity, full and final settlement

Status: PROPOSED. Branch `feature/payroll-phase2` (or `feature/payroll-phase4` cut from it). Source: plan 14 phase 4 (items 12-15).
Contract: `docs/api-contract.md` section 18 (single source of truth; this plan only explains it). Migrations `0024` (4a) and `0025` (4b), split like 0022 / 0023.
All plan 12 / 15 rules still apply: money as 2-decimal strings / `Decimal`, every amount encrypted at rest, no SQL aggregates on money (D2), audit every view/change
without amounts, step-up (`current_password`) on sensitive writes, nobody acts on their own money (D9), published = locked (D4), component lifecycle P4-P9 of plan 15.
Nothing is deployed until the user approves.

## 1. Design decisions
| # | Decision |
|---|---|
| F1 | **Increments are maker-checker.** `draft -> submitted -> approved | rejected` (+ `cancelled`). Maker = `payroll.manage`; checker = NEW permission `payroll.approve` (admin, hr) + step-up, and must differ from the maker, the submitter and the employee (service check AND a DB CHECK). Approval creates (or replaces / removes) a `salary_structures` row in the same transaction. Kinds: `percent`, `amount` (new gross), `structure` (explicit, optionally `replaces_structure_id`), `remove` (delete an unused structure), `import` (from 15). |
| F2 | **Close the bypass.** New setting `payroll.structure_changes_need_approval` (default **true**; turning it off needs `role.assign` + step-up, like `publish_requires_second_person`). While on, direct `POST/PUT/DELETE /structures` is allowed only for an employee's FIRST structure (onboarding: no other structure exists, and for PUT/DELETE it is not in use); everything else is 409 `APPROVAL_REQUIRED` and goes through an increment. Off = today's behaviour. |
| F3 | **Increment approval is exact.** The checker approves the stored `proposed` structure, never a recomputation. The draft records `base_structure_id` + `base_updated_at`; approve fails 409 `INCREMENT_STALE` if the base was changed/deleted or another structure now sits between base and `effective_from`. 409 `LATER_STRUCTURE_EXISTS` if a structure is effective after `effective_from` (the increment would be overridden). |
| F4 | **Increment arithmetic.** `percent`: target gross = rupee-round(old gross x (1 + p/100)); `proportional` scales BASIC, HRA and every allowance half-up to the rupee and puts the remainder into `payroll.split_allowance_code` (SPECIAL) if present, else the last allowance, else BASIC; `split_from_gross` re-splits the target gross by the company rule (F9). Fixed deductions, flags and `tds_monthly` are copied (warning `TDS_REVIEW`). `0 < p <= payroll.increment_max_pct (50)`; decreases only via kind `structure` (warning `DECREASE`). |
| F5 | **Arrears interplay.** A backdated increment (effective month <= the employee's latest published month) is allowed within `payroll.increment_backdate_months` (12); its approval runs the existing `payroll_arrears.on_structure_change` hook in the same transaction, which creates/refreshes the ARREARS proposal (plan 15 P12). Nothing is paid automatically; HR accepts proposals (new bulk accept, contract 18.7). Warning `BACKDATED` on the draft. |
| F6 | **Bulk hike** = a `salary_increment_batches` row + one draft increment per selected employee (<= 500). Batch submit / approve / reject act on all rows of the batch in one transaction; rows that fail a check stay `submitted` and are reported (`failed[]`). Per-employee overrides of the percent are allowed while draft. |
| F7 | **Bulk structure import** reuses `services/xlsx_import.py` (container checks, worker thread, timeout, semaphore, formula refusal), generalised to named sheets with their own header / field list / row cap. Two sheets: `Structures` (one row per employee) and `Components` (allowances / fixed deductions). Dry run binds to commit by `HMAC(secret, actor | "structures" | sha256)` in Redis 15 min (same as 17.2). Commit = one transaction: employees without any structure get the structure created directly (`source = import`); employees with one get a `submitted` increment (kind `import`) in an import batch when F2 is on (else a structure directly). Same file twice -> 409 `IMPORT_DUPLICATE`. |
| F8 | **CTC calculator is pure** (no DB writes, no employee data): from gross or from CTC, using the current payroll settings (PF rate / ceiling, ESI threshold / rates, PT slabs, `employer_share_from_employee`) and the split rule. `from_ctc` searches the largest whole-rupee gross whose cost <= target separately below and above the ESI threshold (cost is monotonic inside each regime, not across it). |
| F9 | **Split-from-gross rule moves to the server**: settings `payroll.split_basic_pct` (50), `payroll.split_hra_pct_of_basic` (40), `payroll.split_allowance_code` (SPECIAL) / `_name`. Flutter `gross_split.dart` reads them instead of constants (local preview only; server authoritative). |
| F10 | **Gratuity is accrual only** (never a payslip line): 15/26 x last monthly basic x counted years, counted years = completed years + 1 when the remainder exceeds 6 months; eligible at >= `payroll.gratuity_min_years` (5) of service, or any service on `death` (Act s.4(1) proviso); capped at `payroll.gratuity_cap` (2,000,000.00); rupee half-up. Monthly accrual (CTC line) = basic x 15 / 26 / 12. Shown in the CTC calculator, a gratuity report (JSON + xlsx) and, for information, on the F&F statement ("paid separately, outside payroll"). Undiscounted estimate, NOT an actuarial (Ind AS 19 / AS 15) valuation. |
| F11 | **F&F is an orchestrator, not a new payslip source.** Approval writes rows into the EXISTING source tables, all tagged `fnf_id`: notice shortfall -> one-time item with NEW system code `NOTICE_RECOVERY`; other items -> one-time items (BONUS / INCENTIVE / OTHER_EARNING / OTHER_DEDUCTION); leave -> `leave_encashments` (`source = fnf`, reuse of `payroll_encashment` internals); loans -> existing foreclose internals (`payroll` = foreclosure installment in the settlement month, `outside` = closed at once). So process / reopen / publish / revise / PERIOD_CLOSED / caps / COMPONENTS_CHANGED work unchanged. |
| F12 | **Last working day (LWD) drives payroll, not deactivation.** F&F creation writes `employee_payroll_details.last_working_day`; `employment_window` ends at LWD when set (else deactivation date - 1 as today). HR may deactivate before or after LWD without changing pay. Cancel clears it. Statutory ESI "Last Working Day" and the dashboard leavers use LWD when set. |
| F13 | **Settlement month.** `settlement_period` >= LWD month and <= LWD month + 3. Same month: the final payslip is the employee's normal payslip of that month (revision if published). Later month: the employee is eligible for that run as a **settlement-only payslip** (employed/paid days 0, structure earnings 0, components only, warning `FNF_SETTLEMENT_ONLY`). Target month rule P9 applies (published month without an open revision = 409 `PERIOD_LOCKED`; HR revises first). |
| F14 | **F&F maker-checker and lifecycle.** `draft -> submitted -> approved -> settled`; reject = back to `draft` with a comment; `cancelled` from draft/submitted; `revoke` (approved -> draft) only while none of its rows is `processing`/`paid`: created items / encashments are cancelled (days back), foreclosure installments cancelled and the schedule regenerated with the loan's EMI from the settlement month (`outside` foreclosures are final and reported). `settled` is set in the publish transaction of the payslip that carries the settlement (`final_payslip_id`); a later revision moves `final_payslip_id` to the new copy. Settled rows are immutable (trigger). One live F&F per employee. |
| F15 | **Global lock order (extends P7)**: `payroll_runs -> users -> payslips -> salary_structures -> payroll_one_time_items -> payroll_loans -> payroll_loan_installments -> payroll_arrears -> leave_balances -> leave_encashments -> reimbursement_claims -> overtime_days -> fnf_settlements`. Increments are outside run processing: `salary_increment_batches -> salary_increments -> users -> salary_structures -> payroll_arrears`. F&F approve reads the F&F row unlocked, takes locks in the global order and locks the F&F row LAST, re-checking `version` (409 `VERSION_CONFLICT`). |
| F16 | **History is append-only.** Every increment / F&F state change appends to `payroll_item_events` (`item_type` gains `increment`, `fnf`; actions gain `submit`, `withdraw`, `settle`, `revoke`, `remove`; `amount_enc` = new gross / net estimate, `prev_amount_enc` = old gross). No DELETE grant on any new table. `GET /employees/{id}/salary-history` combines structures (with `source`) and increments. |
| F17 | **Employee visibility.** No employee push or view for increments (the company sends its own letter). F&F: `GET /me/fnf` + statement PDF once `settled` and only while the account is active (a deactivated user cannot sign in, so HR hands the PDF over); the final payslip is marked `is_final` in the normal payslip list. |

## 2. Database
Common rules of plan 15 section 2 (uuid PK, `*_enc` money/labels/reasons, period checks, `version`, audit columns + `set_updated_at`, FK indexes, `REVOKE DELETE, TRUNCATE FROM app_user`,
key rotation exception `app.key_rotation='on'` + `payroll_strip_enc` equality with `updated_at` unchanged, per commit f3cdaa5).

### 2.1 Migration 0024 (work package 4a)
**`salary_increment_batches`**: `kind CHECK IN ('percent','import')`, `effective_from date NULL` (percent: 1st of month; import: NULL), `percent_enc NULL`, `method CHECK IN (NULL,'proportional','split_from_gross')`,
`file_sha256 text NULL CHECK ~ '^[0-9a-f]{64}$'`, `rows int CHECK BETWEEN 1 AND 500`, `reason_enc`, `created_by NOT NULL`, `created_at`, `cancelled_at`, `cancelled_by`.
`CHECK ((kind='import') = (file_sha256 IS NOT NULL))`, partial unique `(file_sha256) WHERE kind='import'`.

**`salary_increments`**: `user_id`, `batch_id FK NULL RESTRICT`, `kind CHECK IN ('percent','amount','structure','remove','import')`, `effective_from date` (1st, 2000..2200),
`method NULL`, `percent_enc NULL`, `base_structure_id FK salary_structures ON DELETE SET NULL NULL`, `base_updated_at timestamptz NULL`, `replaces_structure_id FK salary_structures ON DELETE SET NULL NULL`,
`proposed_enc bytea NULL` (encrypted JSON = StructureIn without `effective_from`; NULL only for `remove`), `old_gross_enc NULL`, `new_gross_enc NULL`, `warnings text[] NOT NULL DEFAULT '{}'`,
`reason_enc`, `status CHECK IN ('draft','submitted','approved','rejected','cancelled')`, `submitted_by/at`, `decided_by/at`, `decision_comment_enc`, `structure_id FK salary_structures ON DELETE SET NULL NULL`, `version`, audit cols.
Checks: `created_by <> user_id`; `status <> 'approved' OR (decided_by IS NOT NULL AND decided_by <> user_id AND decided_by IS DISTINCT FROM created_by AND decided_by IS DISTINCT FROM submitted_by)`;
`(kind = 'remove') = (proposed_enc IS NULL)`. Partial unique `(user_id, effective_from) WHERE status IN ('draft','submitted')`. Index `(status, created_at)`, `(batch_id)`, `(user_id, created_at)`.
Trigger `salary_increments_lock`: OLD.status IN ('approved','rejected','cancelled') -> refuse unless only `structure_id`/`base_structure_id`/`replaces_structure_id` -> NULL (FK SET NULL) and `updated_*`, or key rotation.

**`salary_structures`**: add `source text NOT NULL DEFAULT 'manual' CHECK IN ('manual','increment','import')`, `increment_id uuid NULL FK salary_increments ON DELETE RESTRICT`
(added after `salary_increments` exists), `import_batch_id uuid NULL FK salary_increment_batches RESTRICT`. Existing `salary_structures_lock` unchanged (it already compares every column).

**`payroll_item_events`**: `item_type` CHECK + `increment`, `fnf`; `action` CHECK + `submit`, `withdraw`, `settle`, `revoke`, `remove` (drop and recreate the two CHECKs).
**Permission** `payroll.approve` (module `payroll`) granted to the `admin` and `hr` roles (pattern of 0023 line 186). Settings seeded (contract 18.11). Grants: SELECT, INSERT, UPDATE; no DELETE.
Regenerate `database/schema.sql`; `test_migration_0024.py`.

### 2.2 Migration 0025 (work package 4b)
**`fnf_settlements`**: `user_id`, `resignation_date date NULL`, `last_working_day date NOT NULL`, `exit_reason CHECK IN ('resignation','termination','retirement','death','contract_end','absconding','other')`,
`settlement_period date` (1st; `CHECK (settlement_period >= date_trunc('month', last_working_day))`), `notice_required_days smallint CHECK BETWEEN 0 AND 365`, `notice_waived bool NOT NULL DEFAULT false`,
`shortfall_days smallint NOT NULL DEFAULT 0 CHECK BETWEEN 0 AND 365` (effective, after waiver / override; days are not money), `plan_enc bytea NOT NULL` (encrypted JSON: override reason, encashment lines,
loan modes + outside `paid_on`/reference, other items with labels/amounts, `move_open_items`), `summary_enc NULL` (amounts snapshot at approval: notice rate/amount, line totals, net estimate, gratuity),
`remarks_enc NULL`, `status CHECK IN ('draft','submitted','approved','settled','cancelled')`, `submitted_by/at`, `decided_by/at`, `decision_comment_enc`, `final_payslip_id FK payslips RESTRICT NULL`, `version`, audit cols.
Checks: `resignation_date IS NULL OR resignation_date <= last_working_day`; `created_by <> user_id`; `status NOT IN ('approved','settled') OR (decided_by IS NOT NULL AND decided_by <> user_id AND decided_by IS DISTINCT FROM created_by AND decided_by IS DISTINCT FROM submitted_by)`;
`(status = 'settled') = (final_payslip_id IS NOT NULL)`. Partial unique `(user_id) WHERE status IN ('draft','submitted','approved')`. Index `(status, created_at)`, `(user_id)`, `(settlement_period, status)`.
Trigger `fnf_settlements_lock`: OLD.status IN ('settled','cancelled') -> only `final_payslip_id` (revision carry), `updated_*`, `version` may change, or key rotation.

**Changes**: `employee_payroll_details.last_working_day date NULL` (written only by the F&F service), `notice_period_days smallint NULL CHECK BETWEEN 0 AND 365`;
`payroll_one_time_items.code` CHECK + `NOTICE_RECOVERY`; `fnf_id uuid NULL FK fnf_settlements RESTRICT` + index on `payroll_one_time_items`, `leave_encashments`, `payroll_loan_installments`.
Settings seeded (18.11). Grants/revokes; downgrade; `test_migration_0025.py`; regenerate `schema.sql`.

## 3. Business rules and edge cases
**Increments (4a)**
- Employee must have a structure in force at `effective_from` (else 422 `STRUCTURE_MISSING`; first structures use `POST /structures`), not be deleted, have no F&F whose LWD month is before `effective_from` (422 `AFTER_EXIT`). Self rule for maker and checker.
- `effective_from` 1st of a month, <= 12 months ahead, >= current month - `increment_backdate_months` (422 `INVALID_PERIOD`). One live (draft/submitted) increment per employee-month (409 `INCREMENT_EXISTS`).
- Same date as an existing structure: only kind `structure` with `replaces_structure_id` of that structure (409 `STRUCTURE_EXISTS` otherwise). Replace / remove need the target not in use (409 `STRUCTURE_IN_USE`).
- Edit only in `draft` (`expected_version`); submit freezes; withdraw (submitted -> draft) by any `payroll.manage` holder; approve/reject only `submitted`. Reject is final (a new draft is needed).
- Warnings computed at save and re-checked at approve: `BACKDATED`, `TDS_REVIEW`, `DECREASE`, `LARGE_CHANGE` (> increment_max_pct for kinds amount/structure), `ESI_THRESHOLD_CROSSED`, `PF_CEILING_CROSSED`, `NO_CHANGE` (identical; submit refused 422).
- Processed (not published) runs: the new structure is not adopted until reopen + process (existing `STRUCTURE_CHANGED`); the approve response says `run_status` of the effective month.
- Batch approve: one transaction, rows in `id` order, per-row checks; failures listed and left `submitted`; response counts `arrears_proposed`.

**Structure import (4a)**
- Template sheets `Structures`: `Employee ID | Effective From (YYYY-MM) | Gross | Basic | HRA | TDS Monthly | PF | PF On Full Basic | ESI | PT` (flags Y/N, default Y/N/Y/Y);
  `Components`: `Employee ID | Type (allowance/deduction) | Code | Name | Amount | Prorate | ESI Wage`. Either `Gross` alone (split by F9, no component rows) or `Basic` (+HRA, components); both -> `GROSS_AND_PARTS`.
- Limits: Structures <= 500 rows, Components <= 2000 rows, <= 15 allowances / 10 deductions per employee, codes per 13.0 (reserved -> `RESERVED_CODE`), same file limits as 17.2.
- Row errors: `UNKNOWN_EMPLOYEE`, `DELETED`, `SELF`, `DUPLICATE_ROW`, `INVALID_PERIOD`, `INVALID_AMOUNT`, `INVALID_FLAG`, `INVALID_CODE`, `RESERVED_CODE`, `DUPLICATE_CODE`, `TOO_MANY_COMPONENTS`, `GROSS_AND_PARTS`,
  `ORPHAN_COMPONENT`, `STRUCTURE_EXISTS`, `LATER_STRUCTURE_EXISTS`, `INCREMENT_EXISTS`, `AFTER_EXIT`, `FORMULA_NOT_ALLOWED`. Warnings: `BACKDATED`, `NO_CHANGE`, `NEEDS_APPROVAL`, `ESI_THRESHOLD_CROSSED`, `TDS_REVIEW`.
- Commit re-validates under locks (`users` FOR UPDATE id order, then structures) and is all-or-nothing; a row error at commit -> 422 `IMPORT_INVALID`.

**CTC and gratuity (4a)**
- CTC monthly = gross + employer PF + employer ESI - recovered employer share (when `employer_share_from_employee`) + gratuity accrual (when `payroll.ctc_include_gratuity`). Same as the `_employer_cost` used by the CTC report, plus gratuity.
- PT: annual estimate from the slabs (half-yearly: 2 x slab(6 x gross); monthly: sum over months). Employee net estimate = gross - PF - ESI - recovered share - PT/12 (no TDS).
- Gratuity basis: setting `payroll.gratuity_wage_basis` `basic` (default) or `basic_or_half_gross` (Labour Code 50% rule, see Q1). No join date -> flag `NO_JOIN_DATE`, no amount.

**Full and final (4b)**
- Create: LWD >= join date, <= today + 180 days; `resignation_date` <= LWD; `notice_required_days` default = details.`notice_period_days` else `payroll.notice_period_days` (30). Writes LWD to payroll details (audit `payroll.details.update` fields `[last_working_day]`).
- Notice: served = LWD - resignation_date + 1 (calendar days); shortfall = max(0, required - served); only for `resignation` (other reasons 0); `waived` -> 0; override 0..required with reason (3-200).
  Amount = round(shortfall x daily_rate, 2), daily_rate = round(monthly gross (`payroll.notice_recovery_basis` = gross|basic) of the structure in force for the LWD month / `payroll.notice_recovery_divisor` (30), 2).
  Line `NOTICE_RECOVERY` "Notice period shortfall (12 d)" (deduction, not PF / ESI wage).
- Encashment lines: per encashable leave type, `days` <= the quote's `available_days` (existing 17.5 rules, year = LWD year or the previous year); priced at approval for the settlement month.
- Loans: every `active` loan must have a mode (`payroll` | `outside`) before submit (422 `LOAN_UNRESOLVED`); an installment in `processing` blocks approve (409 `ITEM_LOCKED`).
- Rows created by an F&F (`fnf_id` set) can only be changed through the F&F (edit / revoke); the 17.2 / 17.3 / 17.5 actions on them are 409 `FNF_MANAGED`.
- Other items <= 20 (17.2 rules). `move_open_items` (default true): pending one-time items / encashments, `pending` arrears and `approved` claims of the employee in a month other than the settlement month that will never be paid (month after LWD month, or `PERIOD_CLOSED`) are moved to the settlement month at approval (events `update`, audited counts).
- Checklist warnings (preview, submit and approve; none blocks except where stated): `ARREARS_PROPOSED`, `CLAIMS_OPEN`, `OVERTIME_UNREVIEWED`, `INCREMENT_PENDING`, `PAID_AFTER_EXIT` (a processed / published payslip
  exists for a month after the LWD month, or the LWD month was paid with more employed days than LWD allows: revise or add a deduction), `ACCOUNT_STILL_ACTIVE` (LWD passed), `NEGATIVE_NET_ESTIMATE`
  (blocks approve, 409 `FNF_NEGATIVE`: record a loan as paid outside or reduce items; a negative final payslip is not supported, Q5), `TDS_REVIEW`, `NO_JOIN_DATE`.
- Preview: estimate of the final payslip (structure for the window, LOP so far, all lines) clearly marked as an estimate; the published payslip is authoritative.
- Settlement on publish: `_publish_one` calls `fnf.on_published` after `mark_paid`; it locks the approved F&F of (user, settlement_period) last (F15) and sets `settled` + `final_payslip_id`. A revision publish of that payslip moves `final_payslip_id`. Reopen changes nothing (stays `approved`).
- Employee activate (section 9): 409 `FNF_OPEN` while an F&F is draft/submitted/approved; after `settled` reactivation clears `last_working_day` (audited). Deactivation does not touch payroll rows.
- Increments, structures and F&F: an increment effective after the LWD month is 422 `AFTER_EXIT`; F&F submit warns `INCREMENT_PENDING` for live increments.

## 4. Flutter screens and navigation
**HR (app) and Admin (console), Payroll area - shared widgets**
- New tab **Approvals** (only for `payroll.approve`): queue of submitted increments, increment batches and F&F settlements; detail with diff / summary, Approve (comment + password dialog) / Reject (comment). Badge count.
- **Increments** (Pay items area, segment): list (status, batch, month filters); "New increment" from the employee payroll screen (kind selector: percent / new gross / full structure (reuses the structure form) / remove), live diff table and warnings chips from the server response; Submit / Withdraw / Cancel.
- **Bulk hike wizard**: effective month, percent, method, scope (departments / employees, exclude) -> server creates the batch -> review table (old gross, new gross, delta, warnings; per-row percent override) -> Submit batch. Batch detail with totals and per-status counts.
- **Structure import wizard** (Employees screen action): download template -> pick file -> dry-run report (errors per sheet/row, warnings, create vs needs-approval counts) -> reason + password -> result.
- **Salary history** on the employee payroll screen: timeline of structures (source chip manual / increment / import, gross, change %) and open increments.
- **CTC calculator** (Payroll tools): mode toggle, amount, monthly/annual, flags -> breakdown table (split, employer PF / ESI, gratuity accrual, CTC, employee net estimate, flags). "Use as structure" pre-fills the structure / increment form. The structure form's "Split from gross" uses the server split settings.
- **Gratuity** report screen: as-of date, filters, table, xlsx export.
- **Exits (F&F)**: list (status, LWD, settlement month); "Record exit" from the employee screen (resignation date, LWD, reason, notice days, settlement month); F&F editor with sections Notice (computed shortfall, waive, override), Leave encashment (lines with server quote), Loans (mode per loan, outside payment fields), Other items, Open items (move toggle), Checklist (warnings), Preview totals, Gratuity info; Submit / Withdraw / Cancel / Revoke; statement PDF preview (DRAFT watermark until settled) and download; link to the final payslip.
- Payslip edit screen: chip "Final settlement" and `FNF_SETTLEMENT_ONLY` warning.
**Employee app**
- Pay area: payslip list marks the final payslip ("Final settlement"). A "Final settlement" card appears when `GET /me/fnf` returns one: status (in progress / settled), LWD, settlement month, "Download statement" when settled. No increment views.

## 5. Security considerations
- Maker-checker enforced three times: service (403 `SECOND_PERSON_REQUIRED`), DB CHECK on `decided_by`, and audit (`maker_id`, `checker_id`). `payroll.approve` is a separate permission so an organisation can give makers only `payroll.manage`.
- F2 removes the direct-structure bypass; turning it off is a `role.assign` + step-up action, audited and announced with an `admin_alert` push to `role.assign` holders.
- Step-up: increment approve, batch approve, import commit, arrears bulk accept, F&F approve and revoke, `payroll.structure_changes_need_approval` change. Failed step-ups share the 5/min limiter and are audited.
- Encryption: proposed structures, percents, gross figures, reasons, comments, F&F plan / summary / remarks are `*_enc`; key rotation discovers them from the models (`EncryptedString`/`EncryptedJSON`); the new lock triggers must accept rotation with `updated_at` unchanged.
- Audit (never amounts, percents, labels, reasons): `payroll.increment.create|update|submit|withdraw|approve|reject|cancel|view|list`, `payroll.increment_batch.*`, `payroll.structure_import.dry_run|commit`,
  `payroll.ctc.calculate` (count only), `payroll.gratuity.view|export`, `payroll.fnf.create|update|submit|withdraw|approve|reject|revoke|cancel|settle|view|list|preview|statement`, `payroll.fnf.view_own`, `payroll.arrears.bulk_accept`.
  Allow-list the new non-money keys (`kind`, `method`, `effective_from`, `rows`, `created`, `failed`, `exit_reason`, `settlement_period`, `last_working_day`, `shortfall_days`, `maker_id`, `checker_id`).
- Push payloads (approver queue, maker decision, `fnf_settled`) carry names / months only, never amounts or percents.
- Import: unchanged hardened parser path (zip-bomb, macro, external link, size/row caps, timeout, formula refusal, never stored); nginx exact location for `/api/admin/payroll/structures/import` with 1100k body.
- Statement PDF: same PDF slots / timeout / headers as payslips (`no-store`, `nosniff`, CSP), employee route 404 for anything not their own settled F&F (audited probe).
- Concurrency: F15 lock order; increment approve share-locks nothing in runs; F&F approve locks the run of the settlement month first (P9 check under lock).
- Known limitation L6 of plan 15 applies to the new triggers too.

## 6. Test plan
- **Unit (pure)**: increment arithmetic (proportional remainder, split_from_gross, rounding, max pct, decrease, no change), CTC from gross / from CTC (both ESI regimes, PF ceiling, full basic, recovery on/off, gratuity on/off, unreachable target), gratuity (service counting at 6 months boundary, 5-year rule, death, cap, basis setting), notice shortfall (reasons, waiver, override, divisor), settlement-only days, multi-sheet parser (headers, caps, formulas, orphan components, gross vs parts).
- **Lifecycle**: increment draft -> submit -> approve creates structure with `source`/`increment_id`; replace / remove; stale base; later structure; backdated approve creates an arrears proposal; batch approve partial failure; bulk arrears accept.
  F&F create sets LWD and run windows; approve writes tagged rows; process / reopen / publish settle; revision moves `final_payslip_id`; revoke restores loans / leave days; settlement-only payslip in a later month; PAID_AFTER_EXIT; activation rules.
- **Concurrency** (two sessions, PostgreSQL): two checkers approve the same increment; approve vs structure PUT; batch approve vs run process (structure share lock); F&F approve vs run process; F&F approve vs leave apply (balance lock); publish-settle vs revoke; no deadlock in F15 order.
- **PostgreSQL-only DB checks**: 0024/0025 up/down/up on PG 16; maker <> checker CHECKs reject a direct UPDATE; `salary_increments_lock` and `fnf_settlements_lock` (incl. FK `ON DELETE SET NULL` passing the lock, key rotation with `updated_at` unchanged, refusal otherwise);
  partial uniques (live increment per month, live F&F per user, import sha); `payroll_item_events` new item types/actions and still append-only; no DELETE/TRUNCATE for `app_user`; circular FK `salary_structures.increment_id` / `salary_increments.structure_id`; `scripts/rotate_keys.py --verify-only` over new columns.
- **Security / API**: permission matrix (manage vs approve vs view_all vs employee), self rule, second person (maker, submitter), step-up required / failed / limiter, rate limits fail-closed, unknown fields 422, no amounts in audit / log / push, F2 refusals, import `IMPORT_NOT_CHECKED` / `IMPORT_FILE_CHANGED` / `IMPORT_DUPLICATE`, employee F&F 404s.
- **Integration**: register / statutory with NOTICE_RECOVERY and settlement-only payslips, ESI "Last Working Day" from LWD, dashboard leavers and new alerts, gratuity xlsx, statement PDF content and watermark.
- **Flutter**: model / repository contract tests per endpoint; widget tests for increment form + diff, bulk wizard, import wizard states, approvals queue, CTC calculator, F&F editor sections and checklist, employee final settlement card; console smoke for Approvals, Increments, Exits.

## 7. Work packages (each: contract -> DB -> backend -> Flutter/console -> qa-tester -> security-reviewer -> fixes; suites green before the next)
### 4a - increments, structure import, CTC calculator, gratuity (migration 0024)
**database-engineer**
1. 0024: `salary_increment_batches`, `salary_increments` (+ lock trigger, checks, partial uniques), `salary_structures` columns + circular FK, `payroll_item_events` CHECK changes, `payroll.approve` permission, settings seed, grants/revokes, downgrade.
2. ORM models, `schema.sql`, `test_migration_0024.py` (PostgreSQL checks of section 6).
**backend-developer**
3. `services/payroll_increments.py`: arithmetic (pure), CRUD, submit / withdraw / approve / reject / cancel, structure create / replace / remove through the existing structure internals + arrears hook, events, warnings; F2 gate in `payroll_structures.py`; salary history.
4. Batches (bulk hike) and arrears bulk accept (`payroll_arrears.py`).
5. Generalise `xlsx_import.py` (named sheets, per-sheet header / fields / caps; 17.2 behaviour unchanged, its tests stay green); `services/payroll_structure_import.py` (template, dry run, HMAC binding, commit).
6. `services/payroll_ctc.py` (pure CTC + gratuity), endpoints, gratuity report + xlsx kind `gratuity`; split settings; settings validation; audit allow-list; push `payroll_approval`.
**flutter-developer**
7. Models / repositories for 18.2-18.7; Approvals tab; Increments segment + forms; bulk wizard; import wizard; salary history; CTC calculator; gratuity screen; `gross_split.dart` from settings.
**security-reviewer** 8. Review 4a (maker-checker bypasses incl. F2, import parser reuse, arithmetic, triggers, audit redaction). **qa-tester** 9. Contract + lifecycle + concurrency + Flutter suite.

### 4b - full and final settlement (migration 0025)
**database-engineer** 1. 0025: `fnf_settlements` (+ lock trigger, checks), details columns, `NOTICE_RECOVERY`, `fnf_id` columns, settings, downgrade, `test_migration_0025.py`.
**backend-developer**
2. `employment_window` / `_eligible` with LWD and settlement-only payslips (`payroll_days.py`, `payroll_runs.py`), statutory ESI LWD, dashboard leavers / alerts, activation rule in `employees.py`.
3. `services/payroll_fnf.py`: create / edit / preview / submit / withdraw / approve (orchestrates `payroll_items`, `payroll_encashment`, `payroll_loans` internals with `fnf_id`) / reject / revoke / cancel; `on_published` hook in `_publish_one`; `NOTICE_RECOVERY` in calc codes (deduction, system-only, reserved).
4. Statement PDF (`services/fnf_pdf.py` on the payslip PDF engine), `/me/fnf` endpoints, pushes, AdminPayslip `final_settlement` / Payslip `is_final`; tests.
**flutter-developer** 5. Exits list, Record exit, F&F editor, approvals entries, statement viewer, employee final settlement card, payslip chips.
**security-reviewer** 6. Review 4b (orchestration atomicity, revoke, lock order, employee access, PDF). **qa-tester** 7. As 4a.

## 8. Open questions for the user (defaults are built if unanswered; items marked LEGAL need the user / auditor)
- **Q1 LEGAL - gratuity.** Default: Payment of Gratuity Act formula, 15/26 x last basic x counted years, eligible at 5 completed years (death: no minimum), cap 20 lakh, accrual only, paid outside payroll.
  The Code on Social Security, 2020 (notified in force from 21 Nov 2025) changes "wages" (basic + DA, with excluded allowances above 50% of remuneration added back) and gives fixed-term employees gratuity after 1 year;
  courts have also read "5 years" as 4 years 240 days in some cases. Confirm the wage basis (setting `gratuity_wage_basis`), the eligibility rule and whether gratuity should be paid through the final payslip.
- **Q2 LEGAL - notice shortfall.** Default: monthly gross / 30 x shortfall calendar days, resignations only, waivable. Confirm basis (gross or basic), divisor and whether GST applies to notice-pay recovery (ask the auditor).
- **Q3 LEGAL - payment timeline.** The Code on Wages, 2019 s.17(2) requires wages to be paid within 2 working days of removal / resignation. The monthly run cycle (F13) may not meet that; an off-cycle settlement run is not in this phase. Confirm the practice.
- **Q4 LEGAL - leave encashment at exit.** Default: HR encashes up to the current balance per the 17.5 rules (no proration of the yearly quota for leavers), rate BASIC / 30. Tax exemption s.10(10AA) and TDS stay manual (`TDS_REVIEW`).
- **Q5** Negative settlement (employee owes the company): not supported on a payslip; default = blocked, HR records part recovery outside. Confirm.
- **Q6** Maker-checker for every structure change after the first (F2) is ON by default. A company with one HR person needs an admin as checker. Confirm.
- **Q7** Employees see their F&F statement in the app only while their account is still active; otherwise HR hands over the PDF. Confirm.
- **Q8** Increment limits: max 50% per increment, backdating up to 12 months, decreases only as a full-structure increment. No increment letters in this phase.
