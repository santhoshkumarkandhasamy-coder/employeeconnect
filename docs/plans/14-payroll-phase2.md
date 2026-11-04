# 14 - Payroll phase 2 (admin features)

Status: PROPOSED. Branch `feature/payroll-phase2`. Builds on plan 12 (payroll, migration 0018) and its rules: money as integer paise / 2-decimal strings,
amounts encrypted at rest, audit every view/change, step-up (`current_password`) on sensitive writes, no self-approval (D9), published = locked.
Admin gets every feature in the console; HR gets the app screens (D11). Each phase ships with its own migration (0020+), contract section, security review and tests.
Nothing is deployed until the user approves.

## Phase 1 - Dashboard, reports, calendar (read-only, no money tables)
| # | Feature | Notes |
|---|---|---|
| 16 | Payroll dashboard | Month picker. Tiles: employees paid, gross, deductions, net, employer cost, holds/errors. Department-wise cost, 12-month trend, joiners/leavers, next pay date. Computed in Python from decrypted payslips (D2), cached per run. |
| 17 | Variance report | Month vs previous month per employee: new, left, structure change, LOP change, net delta, reason codes. |
| 18 | Reports (xlsx via `services/xlsx.py`) | Salary register by department, bank advice, CTC, headcount cost, LOP report. Bank advice needs `payroll.manage` + step-up (same as register `include_bank`). |
| 20 | Payroll calendar and reminders | Settings: cutoff, process, pay dates. Push/dashboard alert when the run is not processed by the process date. |
Perms: `payroll.view_all`. Endpoints `/admin/payroll/dashboard`, `/variance`, `/reports/{kind}`.

## Phase 2 - Statutory reports (A1-A3, A5)
- PF ECR text file (EPFO format, UAN, wages, EPF/EPS/EDLI split), ESI contribution xlsx/csv, PT summary and challan data (TN half-yearly).
- Statutory summary: dues per month for PF, ESI, PT, TDS; table `statutory_challans` (type, period, amount_enc, paid_on, reference_enc, status) to track paid/unpaid.
- Only published runs; employees missing UAN/ESI IP/PAN are listed as blockers. Format specs must be verified by the user against current EPFO/ESIC portals.

## Phase 3 - Pay components feeding the run
| # | Feature | Design |
|---|---|---|
| 7 | Loans and advances | `payroll_loans` (principal, EMI, start month, status) + `loan_installments`; installment due in the month is auto-added as a deduction at refresh; outstanding balance; foreclose. |
| 6 | Reimbursements | Employee submits claim + bill (existing file upload rules), approver (not self) approves, approved amount added as earning in the chosen month. |
| 8 | Bonus / one-time earnings | `payroll_one_time_items` per employee/month, bulk upload xlsx. |
| 9 | Overtime | Rate and eligibility settings; hours from attendance punches; reviewed before inclusion. |
| 10 | Auto arrears | On a structure with effective date inside a paid month, compute the difference and propose an `ARREARS` adjustment. |
| 11 | Leave encashment | Setting per leave type; balance x daily rate; added from F&F or on request. |
All amounts reach the payslip through the existing `adjustments` mechanism with new codes, so the lock and revision rules are unchanged.

## Phase 4 - Exit, increments, import, CTC
- 13 Full and final: `fnf_settlements` (last day, notice shortfall, leave encashment, loan recovery, other items) -> final payslip + statement PDF.
- 14 Increments: draft -> approve (second person) -> creates the new `salary_structures` row; bulk percentage hike; history view.
- 15 Bulk structure import: xlsx template download, dry-run validation report, then commit in one transaction.
- 12 CTC calculator and gratuity accrual: pure calculation from gross/CTC with the "split from gross" rules; accrual shown as a report line, not a payslip item.

## Phase 5 - Controls and delivery
- 19 Maker-checker UI for publish (setting already exists): request, second person approves, audit.
- 21 Bank payment file (NEFT bulk CSV, generic format) and payslip delivery by push (email/WhatsApp only if the user supplies a provider).
- 22 Employee investment declarations and Form 16 download.
- 4 TDS Section 192 engine (old/new regime, declarations, proofs, 24Q/Form 16 data). Last and highest risk; the user must verify slabs and rules each financial year. Until built, TDS stays manual.

## Cross-cutting
- Extend `services/key_rotation.py` and `scripts/rotate_keys.py` to every new encrypted column.
- Regenerate `database/schema.sql`; grants for `app_user`; contract sections 15.x in `docs/api-contract.md`.
- Per phase: contract -> migration -> backend -> console and Flutter -> qa-tester -> security-reviewer -> fixes. Backend suite and Flutter suite must be green before the next phase.
- Open questions for the user (defaults chosen if unanswered): overtime rate (default 2x basic hourly rate, off until enabled); reimbursement approver (reporting manager, HR fallback); bank file format (generic CSV); email/WhatsApp provider (none, push only).
