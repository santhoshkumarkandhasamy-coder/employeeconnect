# Review 09: plan 11 (remote console, offboarding, company, licensing, passwords)

Date 2026-10-22. Reviewer: security-reviewer agent. No Critical findings; 1 High, 3 Medium, several Low/Info.

## High
- **H1** nginx `location /api/auth/web/` and `= /api/auth/web/login` set `Cookie`, so they lose the server-level `proxy_set_header` lines (X-Real-IP, X-Forwarded-For, Host, X-Request-ID, Forwarded, Connection). Clients can spoof their IP: bypass per-IP login limit, forge audit/session IPs, lock out office NAT. Fix: shared proxy-headers snippet included in both locations, then add Cookie. Regression test.

## Medium
- **M1** Anyone can lock any account (incl. admins) with 5 wrong passwords for 15 min. Fix: count lockout per (account, client IP), or back-off, or known-IP bypass; alert on privileged lockouts.
- **M2** Local wipe misses image_picker/file_picker cache copies (leave attachments, announcement files, logo picks). Fix: clear temp + app cache dirs and FilePicker temp files in `LocalWipe`.
- **M3** Offline-cache age limit only runs at cold start; null value means "off". Fix: also run on resume; null -> default 72h.

## Low / Info
- L1 web logout leaves access token valid up to 15 min (bump epoch or denylist jti).
- L2 forgot-password timing could enumerate IDs once FCM is on (push in background).
- L3 PUBLIC_ORIGIN validator accepts `http://localhost.attacker.tld` in prod.
- L4 CLI temp password never expires.
- L5 console server-side idle check is kept alive by 60 s polling; client timer is the real control (document).
- L6 cert pinning does not check hostname.
- L7 local deploy/.env is dev mode; never reuse for internet deployment.
- L8 invalid/deleted license falls back to 50 seats; VENDOR_PUBLIC_KEYS is empty (no license installable yet).
- L9 nginx-start.sh does not lower-case PUBLIC_HOSTNAME/PUBLIC_ORIGIN.
- L10 mobile change-password accepts a console bearer token and returns a mobile refresh token.

## Verified sound
Console cookie/CSRF design, offboarding ACCOUNT_INACTIVE and revocation, Ed25519 license verification and lock ordering, logo handling, permission seeding and endpoint checks, admin-reset passwords, nginx template validation, deploy mounts.
