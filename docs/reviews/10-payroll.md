# Review 10 - Payroll / payslips (2026-10-25)

Read-only code review by security-reviewer. No Critical findings. Employee isolation, RBAC, re-auth, encryption, audit redaction, PDF and Excel handling all hold.

## High
- H1. Lock triggers in 0018 (`payslips_lock`) block `key_rotation.py` on published/superseded rows, so rotation aborts and an old key can never be retired. Fix: rotation sets `SET LOCAL app.key_rotation='on'`; trigger allows changes limited to `*_enc` columns when set; add a test rotating published + superseded payslips.

## Medium
- M1. A `payroll.manage` user can switch off `publish_requires_second_person` (no re-auth) then publish alone; the rule also checks only `processed_by`. Fix: enforce if either the run copy or the current setting is on; require admin permission + re-auth to change it; block the last editor from publishing.
- M2. Admin payslip view/PDF return full UAN/ESI/PAN without the reveal controls (limits, alert, audit). Fix: mask in admin JSON/PDF, or count against the reveal limit and audit it.

## Low
- L1. Structure "in use" check races with run processing; delete clears `structure_id` on processed payslips. Fix: share-lock structures in process/publish; limit the clearing to drafts.
- L2. Employee sees `superseded_by_id` of an unpublished draft revision. Filter successor lookup to published/superseded.
- L3. PDF timeout does not stop the worker thread; release the slot from a done-callback; cap logo size.
- L4. PDF endpoints fail open on rate limit when Redis is down; use fail-closed.
- L5. Flutter details form fields lack enableSuggestions/autocorrect/enableIMEPersonalizedLearning = false.
- L6. DB locks narrower than plan (processed payslips, structures, superseded without successor). Optional hardening.
- Info: a user can change global settings then publish a run including their own payslip (audited). Consider flagging.
