# 07 - Personal Profile

Contract: api-contract section 2 (profile, photo, home feed, celebrations). Tables: `employee_profiles`, `files` (exist from 0001); no new migration expected.

## Rules
- One profile row per user, created empty with the user. Fields per contract; sensitive ones encrypted (`EncryptedString`, see foundation section 7).
- Employee edits own: DOB, anniversary, personal email, phone, emergency contact, blood group, address, photo. Read-only for self: full name, official email, department, role, employee_id. HR/Admin edit everything via `PUT /admin/employees/{id}` (audited as `profile.update_admin`; before/after masked).
- Validation: phone E.164-ish (`^\+?[0-9]{8,15}$`), emails valid, DOB in past and age 16-100, anniversary in past, blood group enum, address <= 300 chars, emergency contact needs name+phone together.
- Photo: jpeg/png <= 5 MB, server re-encodes to max 512px, strips EXIF, old photo file deleted after replace. Served via `/files/{id}` to any authenticated user (name/photo are directory info) - decision O1.
- Home feed (`/home`): composes today's attendance, balances, unread count, pending requests count, food today, celebrations. Each part tolerates failure (returns null for that part).
- Celebrations: birthdays and anniversaries within next N days (default 7) for active users, month/day only, plus "today" list; computed by decrypting in memory, cached in Redis per day; Feb 29 birthdays celebrated on Feb 28 in non-leap years. Users can opt out of being shown (`show_celebrations` bool, default true) - add column in 0001 or small migration (O2).
- Other users never see anyone's DOB year, phone or address via this API; managers/HR with `employee.view_sensitive` see them in admin employee detail only.

## Edge cases
Partial updates (PUT merges provided keys; explicit null clears); concurrent edits (last write wins); decrypt failure after key rotation (return null, log error, never 500); very large address; deactivated users excluded from feed; users with no DOB just skipped.

## Flutter (features/profile, features/home)
Profile screen: avatar with change-photo (camera/gallery, crop), sections Personal, Contact, Emergency, Address, Settings (biometric, logout, logout all, theme). Edit mode with form validation and dirty-state guard. FLAG_SECURE on profile. Home dashboard: attendance card, balances, announcements preview, food card, birthday/anniversary carousel with wish message. Cached profile.

## Security
Encryption at rest, masked audit, no sensitive values in logs, photo access auth, screenshot blocking on profile, rate limit on uploads.

## Tasks
1. database-engineer: confirm `employee_profiles` columns (+ `show_celebrations`), no plaintext DOB column.
2. backend-developer: crypto types (if not done), profile service/router, photo pipeline (Pillow), celebrations service + Redis cache, `/home` aggregator (stub parts for modules not built yet).
3. flutter-developer: profile screens, home dashboard, celebrations carousel.
4. security-reviewer: verify ciphertext in DB, response filtering, photo handling.
5. qa-tester: encryption round-trip (DB value != plaintext), validation table, Feb 29, field-level permissions, home aggregation partial failure.

Open: O1 photo visibility to all employees; O2 celebration opt-out; O3 whether HR may see personal phone/address (proposed yes, audited).
