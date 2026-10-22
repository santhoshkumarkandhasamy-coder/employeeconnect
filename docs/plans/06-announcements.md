# 06 - Announcements

Contract: api-contract section 7. Tables: `announcements`, `announcement_departments`, `announcement_reads` (migration 0005), `files`, `device_tokens`.

## Rules
- Admin/HR (`announcement.manage`) create/update/soft-delete. Fields: title (<=150), body (<=5000, plain text; client renders as text, no HTML), priority normal/important/urgent, optional image (jpeg/png) and PDF (each <= 10 MB, uploaded via `/files` first), `target_all` or >=1 department, optional `expires_at`.
- Visibility for an employee: not deleted, published, not expired, and (`target_all` or their department in targets). Users without department only see target_all.
- Edit after publish allowed (updates `updated_at`; no re-push unless `renotify=true`). Changing targets does not delete existing reads.
- Push on create: to device tokens of targeted active users, data payload (type=announcement, id, title) via FCM; urgent priority uses high-priority channel. Done in a background task, failures logged; invalid tokens removed.
- Read tracking: `POST /announcements/{id}/read` idempotent insert; list returns `is_read`; unread-count for badge; admin sees read_count/target_count and reader list.
- Ordering: urgent first among unexpired? No - ordered by `published_at desc`, urgent pinned at top on client (O1).
- Scheduled publish not in v1.

## Edge cases
User moves department (visibility follows current department); announcement deleted while a push is in flight; large target sets (batch FCM 500/token call); expired announcements remain in admin list; opening a deleted/untargeted announcement id -> 404 (not 403); attachment access allowed only to eligible users.

## Flutter
Employee: Announcements list (unread dot, priority chip, pull to refresh, infinite scroll), detail (image, PDF open via downloaded temp file), badge on More/Home. Deep link from push opens detail. Admin (admin/announcements): list, create/edit form (title, body, priority, department multi-select, image/PDF pickers), read stats screen, delete with confirm.

## Security
Plain-text body only (XSS irrelevant but stripped control chars), file type sniffing, authorisation on `/files/{id}` derived from announcement targeting, audit create/update/delete.

## Tasks
1. database-engineer: migration 0005; indexes (published_at), (announcement_id) reads.
2. backend-developer: files authorisation hook for announcement files, service with visibility query, FCM sender (`notifications/fcm.py`, reused later), admin CRUD, reads stats.
3. flutter-developer: FCM setup (permissions, token registration, foreground/background handlers, deep links), screens.
4. security-reviewer: targeting enforcement, file access, push payload content.
5. qa-tester: visibility matrix by department, read idempotency, unread counts, RBAC on admin endpoints, push payload test with mocked FCM.

Open: O1 pinning urgent items; O2 scheduled publish (v2); O3 comments/likes (out of scope).
