Build a complete, production-ready office employee mobile application called "Employee Connect".

TECH STACK
- Mobile: Flutter (Android + iOS), Riverpod for state, Dio for HTTP, flutter_secure_storage,
  local_auth (biometrics), geolocator (attendance geo-fence), firebase_messaging (push only).
- Backend: Python FastAPI, SQLAlchemy + Alembic, PostgreSQL, Redis, openpyxl for Excel export.
- Deployment: Docker Compose (postgres, redis, api, nginx with HTTPS) on our own on-premise
  server. ALL data must be stored on our server; do not use any third-party cloud database.

AUTHENTICATION & SECURITY
- Login with Employee ID + password only; no self sign-up (Admin creates accounts).
- Force password change on first login; strong password policy.
- Argon2id password hashing; JWT access token (15 min) + rotating refresh token stored hashed
  in DB and revocable; logout and logout-all-devices.
- Lock account after 5 failed attempts; rate limit login endpoint with Redis.
- Store tokens in flutter_secure_storage; optional biometric unlock; auto logout on inactivity.
- HTTPS only with SSL certificate pinning; root/jailbreak detection; block screenshots on
  sensitive screens; obfuscate release builds.
- Server-side RBAC on every endpoint with roles Admin, HR, Manager, Employee and granular
  permissions. Encrypt sensitive personal fields at rest.
- Audit log for logins and every admin create/update/delete action.

FEATURES
1. Attendance: check-in/check-out with time, GPS and optional selfie; office geo-fence
   validation; calendar + history; auto "late" marking from shift start + grace period.
2. Leave & Permission:
   - Balance tab: remaining leave per leave type and remaining permission hours/count.
   - Request tab: apply leave (type, dates, half-day, reason, attachment) and permission
     (date, from/to time, reason).
   - My Requests: status Pending/Approved/Rejected/Cancelled; cancel pending requests.
   - Manager/Admin approval flow with comments; balances update on approval; push
     notifications both ways.
3. Late details: list of late arrivals with minutes late, monthly summary.
4. Food enrollment: ONLY a simple "Need food?" Yes/No per date (no meal types, no veg/non-veg).
   Employee can set Yes/No for today and upcoming dates and change it before a configurable
   cut-off time; Admin sees the daily count and list of employees who need food; Excel export.
5. Announcements: Admin creates announcements (title, body, image/PDF, priority, target
   departments); push notification; read/unread tracking.
6. Personal profile: photo, DOB, wedding anniversary, personal & official email, phone,
   emergency contact, blood group, address. Birthday/anniversary feed on home.
7. Admin module (role-based, inside the app): full add/remove/modify rights for employees,
   roles & permissions, departments, shifts, holidays, leave types & quotas, permission limits,
   food cut-off time, announcements; approve/reject requests; unlock accounts; reset
   passwords; deactivate users; view audit logs.
8. Excel export: one "Export All" button that downloads a single .xlsx with separate sheets
   for Attendance, Leave, Late and Permission, filterable by date range, department and
   employee; also individual exports and a Food enrollment export.

DELIVERABLES
- frontend/: Flutter mobile app (details below).
- backend/: FastAPI project with models, Pydantic schemas, routers, services, pytest tests
  for auth, RBAC, leave balance logic, food enrollment and export.
- database/: PostgreSQL schema via Alembic migrations, schema.sql, seed script for the first
  Admin + default leave types/roles, backup and restore scripts.
- frontend/ details: Flutter project organised by feature (auth, home, attendance, leave, late, food,
  announcements, profile, admin), clean UI with bottom navigation, light/dark theme,
  form validation, loading/error states, offline-friendly caching.
- deploy/: docker-compose.yml, nginx.conf with TLS, .env.example, daily encrypted DB backup
  script.
- Update README.md with setup, environment variables, API list and build/release steps.
- Use the sub-agents in .claude/agents/ (project-architect, database-engineer,
  backend-developer, flutter-developer, security-reviewer, qa-tester, devops-engineer):
  architect plans first, then database -> backend -> frontend per feature, then security
  review and tests before moving to the next feature.
Write clean, well-structured code and implement each feature fully end to end.
