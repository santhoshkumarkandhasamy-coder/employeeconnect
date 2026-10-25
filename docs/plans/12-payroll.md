# 12 - HR Payroll and employee Payslips

Status: PROPOSED (contract `docs/api-contract.md` section 13, proposed). Migration: `0018_payroll`.
Company: Aksan Capital Advisory Private Limited (India, ~50 employees). Single company per deployment (plan 11).
Goals: lightweight, correct, auditable. No tax engine, no statutory filing files, no bank integration.

## 0. Reuse (do not rebuild)
- RBAC `require_permission`, step-up `current_password` + `REAUTH_FAILED` (contract s.9), audit `core/audit.py`, Redis rate limiter (fail closed).
- Encryption: `EncryptedString` / `EncryptedJSON` (`core/crypto.py`, MultiFernet, `DATA_ENCRYPTION_KEYS`). `services/key_rotation.py` + `scripts/rotate_keys.py` MUST be extended to the new columns.
- Attendance (`attendance.status present|late|half_day`), holidays, shifts `working_days`, leave requests + `leave_types.is_paid` (seeded `LOP` type is unpaid), `users.join_date`, `users.deactivated_at`.
- Settings table (`settings`, JSONB) and validation style of `services/settings.py`.
- Excel: `services/xlsx.py` (sanitiser, formula-injection guard, temp dir, no-store headers).
- Push: `services/notifications.notify` (best effort, after commit).
- Branding: `company.name` + logo file (`files.read_plain`).
- Flutter: `screen_protect.dart` (FLAG_SECURE), `ExportStore` + `FileOpener` (private folder, deleted after share, wiped on logout/lock/start), `dio_client` + `guardApi`, admin widgets, console shell (`lib/console`).

## 1. Key decisions
| # | Decision |
|---|---|
| D1 | Money is stored and calculated as integer paise in Python `Decimal`, rounded half-up. Each line is rounded to the rupee by default (`payroll.rounding=rupee`). In JSON, money is a **string with 2 decimals** (`"25000.00"`), never a float. |
| D2 | **All amounts are encrypted at rest** (structure components, payslip inputs/results, bank/PAN/UAN/ESI numbers). Only non-monetary metadata is plaintext (status, period, days, warning codes, ids). Totals are computed in Python (50 employees, so this is cheap). There are no SQL aggregates on money. |
| D3 | A salary structure is effective-dated per **month**: `effective_from` is always the 1st. A mid-month change is paid through an `ARREARS` adjustment. The structure used for a month is the latest one with `effective_from <= period start`. |
| D4 | One run per month (`UNIQUE period`). Run status: `draft -> processed -> published` (`processed -> draft` via reopen). **Published = locked** (DB trigger). Corrections are made with a **payslip revision** (new row, `revision+1`, reason required). Publishing the revision supersedes the old one. Nothing is ever edited in place after publish. |
| D5 | PDFs are **generated on demand** from the stored, immutable payslip snapshot with **fpdf2** (pure Python, uses Pillow which we already have). A bundled DejaVuSans/Noto Sans TTF provides the Rupee glyph. Nothing is stored on disk, so there are no extra blobs to encrypt or purge, and revisions are always consistent. |
| D6 | The company name is snapshotted on the run at publish. The PDF uses the CURRENT logo (old logo files are deleted on replace). The company address and footer come from payroll settings. |
| D7 | Statutory rules are **settings, not code**, and are snapshotted into `payroll_runs.config` at run create/refresh, so later setting changes never alter an existing run. |
| D8 | TDS is a manual monthly amount: the structure default `tds_monthly` is copied into each payslip, where HR can edit it. There is no tax computation. |
| D9 | Scope: `payroll.manage` covers ALL employees (privilege rules of s.9 do NOT apply, because HR must pay managers/admins too). The one hard rule: **nobody can create/edit/revise/process/publish their own structure, details or payslip** (403 `FORBIDDEN`), same as no self-approval. Exception: a whole-run refresh/process/publish may include the caller's own (untouched) payslip, audited `self_included:true`, so a one-HR company can still publish. |
| D10 | The employee payslip **list carries no amounts**. Amounts appear only in the detail and the PDF, and both are audited. |
| D11 | HR uses the mobile app (the console is Admin-only per plan 11). The console gets the same payroll pages for Admin. |

## 2. Database (migration `0018_payroll`)

### 2.1 `salary_structures`
| Column | Type / constraint |
|---|---|
| id | uuid PK |
| user_id | uuid NOT NULL FK users RESTRICT |
| effective_from | date NOT NULL, `CHECK (extract(day from effective_from)=1)`, year 2000..2200 |
| components_enc | bytea NOT NULL (EncryptedJSON): `{basic, hra, allowances:[{code,name,amount,prorate,esi_wage}], fixed_deductions:[{code,name,amount}], tds_monthly}` (amounts as strings) |
| pf_applicable | boolean NOT NULL DEFAULT true |
| pf_on_full_basic | boolean NOT NULL DEFAULT false (true = ignore the PF wage ceiling) |
| esi_applicable | boolean NOT NULL DEFAULT true (still subject to the threshold) |
| pt_applicable | boolean NOT NULL DEFAULT true |
| created_by, updated_by | uuid FK users RESTRICT |
| created_at, updated_at | timestamptz + `set_updated_at` trigger |
`UNIQUE (user_id, effective_from)`; index `(user_id, effective_from DESC)`.

### 2.2 `employee_payroll_details` (1:1 with users, optional)
`user_id` uuid PK FK users RESTRICT; `designation` text NULL (<= 60); `payment_mode` text NOT NULL DEFAULT 'bank_transfer' CHECK IN ('bank_transfer','cheque','cash');
`include_in_payroll` boolean NOT NULL DEFAULT true; `bank_name_enc`, `bank_account_enc`, `bank_ifsc_enc`, `pan_enc`, `uan_enc`, `esi_ip_number_enc` bytea NULL (EncryptedString);
`updated_by` FK users; timestamps + trigger. Formats are validated in the app layer (encrypted, so no DB CHECK): PAN `^[A-Z]{5}[0-9]{4}[A-Z]$`, IFSC `^[A-Z]{4}0[A-Z0-9]{6}$`, account `^\d{9,18}$`, UAN `^\d{12}$`, ESI IP `^\d{10}$`.

### 2.3 `payroll_runs`
| Column | Type / constraint |
|---|---|
| id | uuid PK |
| period | date NOT NULL UNIQUE, `CHECK (extract(day from period)=1)` |
| status | text NOT NULL DEFAULT 'draft' CHECK IN ('draft','processed','published') |
| pay_date | date NULL |
| attendance_cutoff | date NOT NULL (absent days are only detected up to here) |
| config | jsonb NOT NULL (snapshot of all `payroll.*` settings, rates only, non-personal) |
| company_name | text NULL (snapshot at publish) |
| created_by, processed_by, published_by | uuid FK users RESTRICT |
| processed_at, published_at | timestamptz |
| created_at, updated_at | timestamptz + trigger |
`CHECK (status <> 'published' OR (published_at IS NOT NULL AND published_by IS NOT NULL))`.
Trigger `trg_payroll_runs_lock`: when `OLD.status='published'`, refuse UPDATE (except `updated_at`) and DELETE. DELETE is also refused unless `status='draft'` and no payslip of the run was ever published.

### 2.4 `payslips`
| Column | Type / constraint |
|---|---|
| id | uuid PK |
| run_id | uuid NOT NULL FK payroll_runs RESTRICT |
| user_id | uuid NOT NULL FK users RESTRICT |
| revision | smallint NOT NULL DEFAULT 1 CHECK (revision BETWEEN 1 AND 99) |
| status | text NOT NULL DEFAULT 'draft' CHECK IN ('draft','processed','published','superseded') |
| hold | boolean NOT NULL DEFAULT false (excluded from run publish) |
| structure_id | uuid NULL FK salary_structures RESTRICT |
| employee_snapshot | jsonb NOT NULL: `{employee_id, full_name, department, designation, join_date, payment_mode}` (no money, no ids of record) |
| identity_enc | bytea NULL (EncryptedJSON): `{pan, uan, esi_ip_number, bank_name, bank_account, bank_ifsc}` snapshot |
| basis_days, employed_days, lop_auto, paid_days | numeric(4,1) NOT NULL, `CHECK (0 <= paid_days <= basis_days)`, `lop_auto >= 0` |
| lop_override | numeric(4,1) NULL CHECK (lop_override >= 0 AND lop_override*2 = trunc(lop_override*2)) (half-day steps) |
| lop_override_reason_enc | bytea NULL; `CHECK (lop_override IS NULL OR lop_override_reason_enc IS NOT NULL)` |
| lop_breakdown | jsonb NOT NULL: `{unpaid_leave, absent, half_day_attendance, not_employed, pending_leave_days}` (day counts) |
| inputs_enc | bytea NOT NULL (EncryptedJSON): `{tds, pt_override, adjustments:[{code,label,amount}], note}` |
| result_enc | bytea NULL (EncryptedJSON): `{earnings[], deductions[], employer[], gross, total_deductions, net}`; `CHECK (status='draft' OR result_enc IS NOT NULL)` |
| warnings | text[] NOT NULL DEFAULT '{}' (codes only) |
| revision_reason_enc | bytea NULL; `CHECK (revision = 1 OR revision_reason_enc IS NOT NULL)` |
| supersedes_id | uuid NULL FK payslips RESTRICT |
| version | integer NOT NULL DEFAULT 1 (optimistic lock) |
| processed_at/by, published_at/by, superseded_at | timestamptz / uuid FK users |
| created_by, updated_by, created_at, updated_at | + trigger |
Indexes: `UNIQUE (run_id, user_id, revision)`; partial unique `(run_id, user_id) WHERE status IN ('draft','processed')` (one open copy); partial unique `(run_id, user_id) WHERE status='published'` (one live copy);
`(user_id, status)`; `(run_id, status)`; `structure_id`, `supersedes_id`.
Trigger `trg_payslips_lock`: `OLD.status='published'` allows only the transition to `superseded` (sets `superseded_at`); `OLD.status='superseded'` refuses any change; DELETE allowed only for `draft`.

### 2.5 Permissions, settings, grants
- Permissions (module `payroll`): `payroll.view_own` (all four system roles; the migration grants it to every existing role), `payroll.view_all` (admin, hr, read-only), `payroll.manage` (admin, hr), `payroll.publish` (admin, hr).
- Settings seeded (`payroll.*`, see section 4), edited only via `/admin/payroll/settings` and excluded from the generic `PUT /admin/settings`.
- Regenerate `schema.sql`. Grant the `app_user` role SELECT/INSERT/UPDATE/DELETE (the triggers do the locking).

## 3. API (summary; full JSON in contract section 13)
| Method | Path | Perm |
|---|---|---|
| GET | /payslips?year= | `payroll.view_own` |
| GET | /payslips/{id} | `payroll.view_own` (owner, published/superseded only) |
| GET | /payslips/{id}/pdf | same |
| GET/PUT | /admin/payroll/settings | GET `payroll.view_all`, PUT `payroll.manage` |
| GET | /admin/payroll/employees?q=&department_id=&has_structure=&page= | `payroll.view_all` |
| GET/PUT | /admin/payroll/employees/{user_id}/details | GET `payroll.view_all` (`?reveal=true` unmasks), PUT `payroll.manage` + step-up |
| GET/POST | /admin/payroll/employees/{user_id}/structures | GET `payroll.view_all`, POST `payroll.manage` |
| PUT/DELETE | /admin/payroll/structures/{id} | `payroll.manage` (only when not used by a processed/published payslip) |
| GET/POST | /admin/payroll/runs | GET `payroll.view_all`, POST `payroll.manage` |
| GET/DELETE | /admin/payroll/runs/{id} | GET `payroll.view_all`, DELETE `payroll.manage` (draft, never published) |
| POST | /admin/payroll/runs/{id}/refresh, /process, /reopen | `payroll.manage` |
| POST | /admin/payroll/runs/{id}/publish | `payroll.publish` + step-up |
| GET | /admin/payroll/runs/{id}/payslips?q=&status=&flag=&page= | `payroll.view_all` |
| GET/PUT/DELETE | /admin/payroll/payslips/{id} | GET `payroll.view_all`; PUT/DELETE `payroll.manage` (draft only) |
| POST | /admin/payroll/runs/{id}/employees/{user_id}/revise | `payroll.manage` |
| POST | /admin/payroll/payslips/{id}/process, /reopen | `payroll.manage` |
| POST | /admin/payroll/payslips/{id}/publish | `payroll.publish` + step-up |
| GET | /admin/payroll/payslips/{id}/pdf | `payroll.view_all` (non-published copies are watermarked "DRAFT") |
| POST | /admin/payroll/runs/{id}/register | `payroll.view_all` (+ step-up when `include_bank=true`, which also needs `payroll.manage`) -> xlsx |

## 4. Business rules

### 4.1 Settings (snapshotted into the run)
| Key | Default | Range |
|---|---|---|
| payroll.pay_days_basis | `calendar` | `calendar` (days in month) / `fixed_30` / `working_days` |
| payroll.lop_absent_days | true | bool: working day, no attendance, no approved leave = 1 LOP day |
| payroll.lop_half_day_attendance | true | bool: attendance `half_day` without approved half-day leave = 0.5 |
| payroll.pf_employee_rate_pct / pf_employer_rate_pct | 12 / 12 | 0..20, 2 decimals |
| payroll.pf_wage_ceiling | 15000 | 0..1000000 (0 = no ceiling) |
| payroll.esi_wage_threshold | 20000 | 0..1000000 |
| payroll.esi_employee_rate_pct / esi_employer_rate_pct | 0.75 / 3.25 | 0..10 |
| payroll.pt_frequency | `half_yearly` (Tamil Nadu; decision) | `monthly` / `half_yearly`: half_yearly = PT is deducted only in the months listed in the slab `months` (Mar, Sep) and the slab is looked up on the half-yearly gross (sum of the gross of the 6 months ending with the current one) |
| payroll.pt_slabs | Tamil Nadu half-yearly defaults: <=21,000 nil; 21,001-30,000 135; 30,001-45,000 315; 45,001-60,000 690; 60,001-75,000 1,025; >75,000 1,250, all with `months:[3,9]`. **VERIFY AGAINST CURRENT TN RULES before the first real run** | list (<= 20) of `{min_gross, max_gross|null, amount, months:[1..12]|null}`, non-overlapping ranges |
| payroll.rounding | `rupee` | `rupee` / `paise` |
| payroll.show_employer_contributions | false | bool (payslip only; the register always has them) |
| payroll.employer_share_from_employee | true | bool: the employer PF/ESI share is ALSO deducted from the employee (see 4.4 step 3a) |
| payroll.mask_pan | true | bool (payslip shows `XXXXX1234X`; the account is always masked to the last 4) |
| payroll.publish_requires_second_person | false | bool: publisher must differ from the last processor |
| payroll.company_address | "" | <= 300 chars |
| payroll.payslip_footer | "This is a computer-generated payslip and does not require a signature." | <= 200 chars |

### 4.2 Eligibility (run create / refresh)
An employee is included when `include_in_payroll`, `join_date` is NULL or <= period end, the employee was not deactivated/deleted before the period start, and a structure exists (else the payslip is created with error `STRUCTURE_MISSING`).
`hr`/`admin` users are included like anyone else. Deactivated mid-month employees are paid pro rata (full and final settlement is out of scope).

### 4.3 Days and LOP (office timezone, per calendar day)
- `employed` window = [max(period start, join_date), min(period end, deactivation date - 1)]. `not_employed` = basis days outside it (for `working_days` basis, only working days count).
- Working day = a shift `working_days` weekday and not an active holiday. Employees with no shift get warning `NO_SHIFT`, and absent detection is skipped (unpaid leave still counts).
- `unpaid_leave` = approved leave of `is_paid=false` types inside the employed window, on working days (half-day = 0.5).
- `absent` (when `lop_absent_days`) = working days <= `attendance_cutoff` with no attendance row and no approved leave. Days after the cutoff are assumed present.
- `half_day_attendance` (when enabled) = 0.5 per `half_day` attendance not covered by approved half-day leave.
- `pending_leave_days` = pending leave on absent days. It does not count, but raises warning `PENDING_LEAVE` (resolve the request, then refresh).
- `lop_auto` = unpaid_leave + absent + half_day_attendance; `lop = lop_override ?? lop_auto`; `paid_days = max(0, employed_days - lop)`; `basis_days` = 30 / days in month / working days in month.
- `attendance_cutoff` = min(period end, today) at create/refresh. Creating a run for a future month is refused (`INVALID_PERIOD`); past months are allowed.

### 4.4 Calculation (per payslip, recomputed on every draft save / refresh / process)
1. `factor = paid_days / basis_days`. Earned `BASIC`, `HRA` and every allowance with `prorate=true` = monthly x factor. `prorate=false` allowances are paid in full if paid_days > 0.
2. Adjustment earnings (not prorated): `BONUS`, `ARREARS`, `INCENTIVE`, `REIMBURSEMENT`, `OTHER_EARNING`. Gross = sum of earnings.
3. PF (if `pf_applicable`): `rate x min(earned BASIC, ceiling)`, or the full earned basic when `pf_on_full_basic` or ceiling = 0. Employer PF uses the same wage (informational).
3a. Employer share recovery (`payroll.employer_share_from_employee`, default true; business rule: employee deduction = employee share + employer share, for PF and ESI). When on, each PF/ESI line is split: `PF` "PF - employee share" + `PF_ER_REC` "PF - employer share (recovered)" (same amount as the employer PF, same wage and rounding), likewise `ESI` + `ESI_ER_REC`. Both lines are deductions, so net pay falls by both. The `employer` list (`PF_ER`, `ESI_ER`) still holds what the company remits to the authority. When off: only the employee lines (labels "Provident Fund" / "ESI"). Register: columns `PF`, `ESI` (employee share), `PF Employer Share Recovered`, `ESI Employer Share Recovered`, `Employer PF`, `Employer ESI` (remitted) and `Employer Cost (net of recovery)` = remitted - recovered (0 when on, so the company cost is not double counted). Run totals add `employer_recovered` and `employer_cost`; `employer_pf`/`employer_esi` stay the remitted amounts. `PF_ER_REC`/`ESI_ER_REC` are reserved codes.
4. ESI (if `esi_applicable` and eligible): the employee is eligible for the whole ESI contribution period (Apr-Sep, Oct-Mar) when the structure's monthly gross at the period's first month was <= threshold. ESI wage = earned structure earnings with `esi_wage=true` + adjustments `INCENTIVE`, `OTHER_EARNING` (excluding BONUS, ARREARS, REIMBURSEMENT). Rounded UP to the next rupee.
5. PT (if `pt_applicable`): the slab matching gross whose `months` is null or contains the month. `pt_override` (>= 0) replaces it.
6. TDS = `inputs.tds` (defaults to the structure's `tds_monthly`). Fixed deductions from the structure. Adjustment deductions: `ADVANCE_RECOVERY`, `LOAN_EMI`, `OTHER_DEDUCTION`.
7. Net = gross - total deductions. Net < 0 -> error `NEGATIVE_NET_PAY`. Net in words uses Indian numbering (lakh/crore).
- Limits: every amount 0.00..10,000,000.00, <= 2 decimals; <= 15 allowances, <= 10 fixed deductions, <= 10 adjustments; codes `^[A-Z][A-Z0-9_]{1,15}$`; reserved codes (BASIC, HRA, PF, ESI, PT, TDS, LOP and the adjustment codes) cannot be used as allowance/fixed-deduction codes; labels 1-40 chars, cleaned like other text.
- Errors (block process): `STRUCTURE_MISSING`, `NEGATIVE_NET_PAY`. Warnings (do not block): `NO_SHIFT`, `PENDING_LEAVE`, `LOP_OVERRIDDEN`, `ZERO_PAID_DAYS`, `NO_BANK_DETAILS` (payment_mode bank_transfer), `NO_PAN`, `STRUCTURE_CHANGED` (a newer structure was saved after this draft was computed).

### 4.5 Lifecycle
- **Create** (draft): snapshot config, compute every eligible employee's draft payslip. 409 `PAYROLL_RUN_EXISTS` for a duplicate month.
- **Draft edits**: `PUT /admin/payroll/payslips/{id}` with `expected_version` (409 `VERSION_CONFLICT` if stale). Allowed: lop_override + reason, tds, pt_override, adjustments, hold, note. The response is the recomputed payslip. Own payslip -> 403.
- **Refresh** (draft run only): re-snapshot settings, re-pull LOP/structures/identity, add newly eligible employees, keep manual inputs. Employees no longer eligible keep their draft with warning `NOT_ELIGIBLE` (HR deletes it).
- **Process**: recompute all non-held drafts in one transaction (run row `FOR UPDATE`). Any error -> 409 `PAYROLL_BLOCKED` with a list of `employee_id` + code, and nothing changes. Status becomes `processed` (held payslips stay draft).
- **Reopen**: processed -> draft (not after publish).
- **Publish** (step-up; optional maker-checker): every processed, non-held payslip -> published; run -> published + `company_name` snapshot. After commit, push `payslip_published` per employee. Idempotency: a second publish -> 409 `PAYROLL_LOCKED`.
- **After publish**: held payslips are edited, processed and published one by one (payslip-level endpoints). **Revise** (`reason` 3-200 chars) copies the live published payslip into a new draft `revision+1` (re-pulls LOP, keeps inputs), or creates revision 1 for an employee missing from the run. Publishing the revision sets the old one `superseded` in the same transaction and pushes `payslip_published` (title "Revised payslip available").
- Delete: a draft payslip (any time), or a whole draft run that never had a published payslip.
- Structure edit/delete: refused with 409 `STRUCTURE_IN_USE` once a processed/published payslip references it (create a new effective-dated structure instead). Draft payslips referencing it are recomputed on the next save/refresh and get warning `STRUCTURE_CHANGED`.

### 4.6 Edge cases
Mid-month join/exit; leave crossing month boundaries (only in-period days); unpaid leave on a holiday (0); LOP override above employed days (422); employee with zero paid days but fixed deductions (net < 0 -> blocked: HR waives via adjustment or hold); leave approved after publish (HR revises, or pays ARREARS next month);
attendance purged by retention (only coordinates/selfies are purged, rows stay); settings changed mid-run (snapshot, applied on refresh only); concurrent HR edits (version); double-tap publish; employee offboarded after publish (cannot log in; HR still downloads the PDF); soft-deleted users (payslips retained); key rotation (new columns included); a run for a month before an employee's first structure (`STRUCTURE_MISSING`).

## 5. Payslip PDF and register
- PDF (A4, fpdf2, embedded TTF): company logo + name + address, "Payslip for October 2026", employee block (ID, name, department, designation, DOJ, PAN masked per setting, UAN, bank masked last 4, payment mode), days (basis, LOP, paid), earnings and deductions tables side by side, employer contributions (if enabled), Gross / Deductions / **Net pay** + words, revision note ("Revision 2 - supersedes the payslip published on ..."), footer, generated-at timestamp.
  Non-published admin copies carry a "DRAFT - NOT VALID" watermark. PDF metadata contains no personal data beyond the title "Payslip 2026-10". No JavaScript, links or attachments. Build in a worker thread (`asyncio.to_thread`) under a 2-slot semaphore, 10 s timeout.
- Headers: `Content-Type: application/pdf`, `Content-Disposition: attachment; filename="payslip-<EMPID>-2026-10[-r2].pdf"` (built from sanitised enum/ids only), `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`.
- Register `.xlsx` (reuse `services/xlsx.py`): sheet `Register` (one row per live payslip: Employee ID, Name, Department, Designation, Status, Revision, Hold, Basis Days, LOP Days, Paid Days, BASIC, HRA, one column per allowance code (union, sorted), adjustment earnings, Gross, PF, ESI, PT, TDS, fixed deduction columns, adjustment deductions, Total Deductions, Net Pay, Employer PF, Employer ESI), sheet `Summary` (totals per column, counts), and with `include_bank=true` sheet `Bank` (Employee ID, Name, Bank, Account, IFSC, Net Pay, Payment Mode).
  Amounts are written as numeric cells. Formula-injection guard on text. Rate limit 5/min, fail closed.

## 6. Flutter
**Employee** (More -> "Payslips", shown when `payroll.view_own`):
- `PayslipsScreen`: year selector, month list (period, published date, "Revised" chip; no amounts), empty state "No payslips yet".
- `PayslipDetailScreen`: header, days, earnings/deductions, net pay + words, revision banner with link to the earlier revision, buttons **Download PDF** and **Share**. Both fetch bytes, save to `ExportStore` (private), hand them to `FileOpener`, and delete them after the share grace.
- FLAG_SECURE on both screens (`screen_protect.dart`). Data only in autoDispose Riverpod state: **never `json_cache`**, cleared on lock/logout. Push `payslip_published` (once FCM lands) opens the list.

**HR/Admin** (Admin home -> "Payroll", shown with `payroll.view_all`; write actions hidden without `payroll.manage` / `payroll.publish`):
- `PayrollHomeScreen` tabs: **Runs | Employees | Settings**.
- Runs: year list (period, status chip, employees, net total) + "New run" (month picker, pay date). `RunReviewScreen`: status, totals card, error/warning filters, employee rows (name, paid days, LOP, net, flags), actions by status (Refresh, Process, Reopen, Publish with step-up dialog, Register download with an optional "include bank details" step-up), per-row PDF preview.
- `PayslipEditScreen` (draft): LOP auto breakdown + override (half-day steps, reason), TDS, PT override, adjustments list (code picker, label, amount), hold toggle, note; Save shows the server-recomputed result. Published row: "Revise" (reason dialog). Held/revision rows: Process / Publish.
- Employees: list (structure status, gross, missing-details flags). `EmployeePayrollScreen`: structure history (effective-dated, "in use" lock), add/copy structure form (basic, HRA, allowance rows with prorate/ESI flags, fixed deductions, default TDS, PF/ESI/PT flags, live gross), details form (designation, payment mode, bank, IFSC, PAN, UAN, ESI IP; masked with a "Reveal" action that calls `?reveal=true`; save asks for step-up).
- Settings: rules form + PT slab editor (add/remove rows, month chips).
- All payroll admin screens use FLAG_SECURE. Money input uses a decimal keyboard and is sent as a string.
**Console** (Admin): pages Payroll Runs, Run Review, Payslip Edit, Employees/Structures, Payroll Settings, reusing the same repository/models. PDF and xlsx are saved as browser blob downloads (existing export pattern). The access token is never in a URL.

## 7. Security considerations
- Encryption at rest per D2. `DataDecryptionError` -> 503 `DATA_UNREADABLE` (per payslip, never a silent zero). Extend key rotation to all `*_enc` payroll columns.
- Audit (every view/change/publish, ids and field NAMES only, never amounts/bank/PAN): `payroll.settings.update` (rates before/after: not personal),
  `payroll.structure.view|create|update|delete`, `payroll.details.view` (`after.revealed`), `payroll.details.update` (`after.fields`), `payroll.run.create|refresh|process|reopen|publish|delete|view`,
  `payroll.payslip.view` (admin detail), `payroll.payslip.update` (`after.fields`), `payroll.payslip.revise|process|publish|delete`, `payroll.payslip.view_own`, `payroll.payslip.download` (owner or admin), `payroll.register.export` (`after.include_bank`, rows), `payroll.denied` (403/404 probes, outcome failure).
- Defence in depth in `core/audit.redact`: add substrings `salary`, `amount`, `bank`, `earning`, `deduction`, `gross`, `net_pay`, `tds`, and EXACT keys `pan`, `uan`, `ifsc` (a `pan` substring would also mask `company`).
- Logging: payroll services log ids/codes only. The 422 handler (`core/errors.py`) returns only `loc` + `msg` (no `input`); payroll custom validators must never put the submitted value into `msg`. nginx logs paths only (ids). No amounts in push title/body (`payslip_published`: "Payslip available" / "Your payslip for October 2026 is ready.").
- Access: the employee endpoints filter `user_id = caller AND status IN ('published','superseded')`. Anything else -> 404 (no existence leak). Admin endpoints are perm-gated. The self-edit rule applies to everything (D9). Publish and bank-detail changes/exports need step-up (5/min, fail closed).
- Rate limits (per user, Redis, fail closed for writes/exports): employee detail+pdf 30/min and 200/day; admin reads 120/min; drafts writes 60/min; run actions 10/min; register 5/min; details reveal 30/min and 200/day (more than 100/hour raises an `admin_alert` like sensitive views).
- Mobile: FLAG_SECURE, no disk cache, PDF only in the private ExportStore (deleted after share, wiped on lock/logout/start), share is the user's explicit choice. Console: blob URLs revoked after download.
- PDF: generated server side from trusted data. User text (labels, names, address) is drawn as plain text, never parsed. No external fetches.
- DB: triggers lock published data even against a buggy service. Backups are already encrypted.
- Retention: payroll records are kept (statutory 8 years). No purge job in v1.

## 8. Tasks (in order)
**database-engineer**
1. Migration `0018_payroll` (+ downgrade): 4 tables, checks, partial unique indexes, lock triggers (2.3/2.4), `set_updated_at` triggers, permissions + grants (`payroll.view_own` to every existing role), seeded `payroll.*` settings. Models in `app/models/payroll.py` using `EncryptedJSON`/`EncryptedString`.
2. Update `seed.py` (idempotent) and regenerate `schema.sql`. Extend `services/key_rotation.py` column list.
3. Tests: triggers refuse edit/delete of published/superseded rows and allow published -> superseded only.

**backend-developer**
1. `services/payroll_settings.py` (validation per 4.1, PT slab validator, snapshot builder); routes `GET/PUT /admin/payroll/settings`; exclude `payroll.*` from the generic settings PUT.
2. Structures + details services/routes (encryption, masking, reveal, step-up on details PUT, self rule, in-use rule).
3. `services/payroll_days.py` (4.3: working days, holidays, leave, attendance, join/exit) and `services/payroll_calc.py` (4.4, pure functions on Decimal, fully unit-tested).
4. Runs + payslips services/routes (4.5): create/refresh/process/reopen/publish/delete, draft PUT with version, revise, payslip-level process/reopen/publish, row locks (`FOR UPDATE` on the run), post-commit push `payslip_published`.
5. Employee routes `/payslips`, `/payslips/{id}`, `/payslips/{id}/pdf`.
6. `services/payslip_pdf.py` (fpdf2 + bundled font, watermark, semaphore/timeout); add `fpdf2` to `requirements.txt` (pin the version) and the TTF + its license under `backend/app/assets/fonts/`.
7. Register export (`services/payroll_register.py` on `xlsx.py`).
8. Audit actions + redaction additions (section 7), rate limits, error codes. Update contract section 13 from PROPOSED to final once implemented.

**flutter-developer**
1. `lib/features/payroll/` (data: repository on `dioProvider`; domain: models with money as `Decimal`-safe strings and an Indian number formatter; presentation: screens in section 6). No `json_cache`.
2. Employee screens + More entry + PDF download/share via `ExportStore`/`FileOpener`; FLAG_SECURE.
3. HR/Admin screens (runs, review, payslip edit, employees, structure form, details form, settings + PT slabs), step-up dialogs (reuse the existing ones), error code mapping (`PAYROLL_BLOCKED` list view, `VERSION_CONFLICT` reload prompt, `STRUCTURE_IN_USE`, `PAYROLL_LOCKED`).
4. Console pages (reuse widgets; blob downloads; no mobile plugins in the web build).
5. Push type `payslip_published` routing (when FCM lands).

**security-reviewer**
IDOR on `/payslips/{id}` (other user's, draft, held), admin-only routes for employees/managers, the self-edit rule on every write, step-up on publish/details/bank export, encryption of every money/identity column (inspect the DB), no amounts/PAN/bank in audit rows, logs, push payloads, validation errors or PDF metadata, trigger locking, PDF/xlsx headers + filename, formula injection in labels, mobile disk residue after share/lock/logout, console blob handling. Write `docs/reviews/12-payroll-security.md`.

**qa-tester**
Backend: calc unit tests (proration per basis, PF ceiling vs full basic, ESI threshold + contribution-period continuation + round-up, PT slabs + month overrides, rounding modes, negative net), LOP (half days, unpaid leave across months, holidays, no shift, cutoff, join/exit, pending leave warning, override), lifecycle (create duplicate, refresh keeps inputs, process blocked list, reopen, publish idempotency, maker-checker, held then single publish, revise -> supersede, structure in-use), RBAC/self rule/404s, audit rows present without amounts, PDF opens and contains the net pay, register columns/totals/bank sheet with step-up, rate limits.
Flutter: widget tests for list (no amounts), detail, edit form validation, money formatting (lakh grouping); integration: download + share + file deleted.

## 9. Open questions (defaults in brackets; nothing blocks the build)
1. DECIDED: office is in Tamil Nadu, PT half-yearly (Sep + Mar) with the default slabs in 4.1 (to be verified against current rules).
2. **Pay-days basis**: calendar days, fixed 30 or working days? [calendar]
3. DECIDED: a working day with no check-in and no approved leave is automatic LOP (default on, shown as a breakdown and warning; HR can override).
4. Is PF on basic only with the 15,000 ceiling right, or should the **Labour Codes (Nov 2025) "wages >= 50% of remuneration"** rule set the PF wage? [basic + ceiling; we could add a non-blocking `BASIC_BELOW_50PCT` warning]
5. Should HR see **Admin/director salaries**, or should some employees be payroll-restricted to Admin only? [no restriction; only the self rule]
6. **Maker-checker** on publish (a different person publishes)? [off; available as a setting]
7. **Password-protected PDF** (e.g. PAN or DOB as password)? [no; access is authenticated and sharing is the employee's choice]
8. Show **employer PF/ESI** on the employee payslip? [no; always in the register]
9. Out of scope for v1, confirm: full and final settlement, gratuity, leave encashment, bank-specific transfer file, PF ECR/ESI challan files, Form 16, historical payroll import, reimbursement claims workflow.
10. Should employees still see payslips after offboarding (would need an ex-employee portal)? [no; HR provides PDFs]
11. Is a **designation** field wanted on the employee master generally, or only on payroll details? [payroll details only]
