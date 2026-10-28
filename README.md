# Employee Connect — Office Employee Mobile App

A secure mobile application for office employees to manage attendance, leave, late
arrivals, permissions, food enrollment, announcements and personal profile data.
Admins have full control over users, roles and data, and can
export reports to Excel. **All data is stored on the company's own server (on-premise /
self-hosted)** — no third-party cloud database.

> Developer setup, tests and deployment: see [docs/SETUP.md](docs/SETUP.md) and [docs/DEPLOY.md](docs/DEPLOY.md).

---

## 1. Features

### 1.1 Authentication & Security
- Login with **Employee ID + Password** (no public sign-up; accounts are created by Admin).
- First-login forced password change; password policy (min 8 chars, upper, lower, digit, symbol).
- Optional **biometric unlock** (fingerprint / Face ID) after first successful login.
- Account lockout after 5 failed attempts (unlock by Admin or after 15 minutes).
- "Forgot password" → reset request goes to Admin / OTP to registered email.
- Auto logout after inactivity; logout from all devices.

### 1.2 Attendance
- Check-in / Check-out with timestamp, GPS location and optional selfie.
- Geo-fence validation (check-in only allowed within office radius, configurable).
- Daily, weekly and monthly attendance history and calendar view.
- Automatic **late** marking based on shift start time + grace period.

### 1.3 Leave, Late & Permission
- **Balances tab**: shows remaining leave by type (Casual, Sick, Earned, etc.) and permission hours/count remaining for the month.
- **Request tab**: apply for leave (type, from/to date, half-day, reason, attachment) and permission (date, from/to time, reason).
- Request status: Pending → Approved / Rejected / Cancelled, with approver comments.
- Late details: list of late arrivals with minutes late per day/month.
- Push notification to manager/Admin on new request and to employee on decision.

### 1.3a On Duty (client locations)
- Admin adds **client locations** with their **geo coordinates** (latitude, longitude) and an allowed radius in metres
  (the app can fill the coordinates from the admin's current position at the site).
- Employee requests **On Duty** for one or more dates and chooses the client location; it is approved like leave.
- On an approved on-duty day, check-in / check-out is accepted **only when the employee's location matches the client
  location** (inside its radius). Punching at the office is refused that day.
- On-duty requests are included in the Excel export (sheet "On Duty"), and the Attendance sheet shows the client location.

### 1.4 Food Enrollment
- Simple daily choice: **"Need food today?" → Yes / No** (no meal type or veg/non-veg options).
- Employee can mark Yes/No for upcoming dates (e.g. the whole week) and change it before the cut-off.
- Enrollment cut-off time (e.g. previous day 6 PM or same day 10 AM), configurable by Admin.
- Admin view: daily count of employees who need food, with names; export to Excel.

### 1.5 Announcements
- Admin posts general announcements (title, message, optional image/PDF, priority).
- Target all employees or specific departments.
- Push notification on new announcement; read/unread status.

### 1.6 Personal Profile
- Employee stores: Full name, photo, **date of birth, wedding anniversary**, personal & official **email**, **phone number**, emergency contact, blood group, address.
- Birthday & anniversary reminders/wishes feed on the home screen.
- Sensitive fields encrypted at rest; employee can edit own profile, Admin can edit all.

### 1.7 Admin Panel (inside the app, role-based)
- Full rights: **add, remove, modify** employees, departments, shifts, holidays, leave types, leave quotas, permission limits, food cut-off time, announcements.
- Assign roles and permissions (Admin, HR, Manager, Employee) — granular rights.
- Approve / reject leave and permission requests.
- Unlock accounts, reset passwords, deactivate employees.
- View audit logs (who changed what, when).
- **Export to Excel (.xlsx)**: one click to export all **Attendance, Leave, Late, Permission and On Duty** details (each as a separate sheet), filtered by date range / department / employee. Food enrollment report also exportable.

---

## 2. Recommended Tech Stack

| Layer | Part | Technology |
|---|---|---|
| **Frontend** | Mobile app | **Flutter** (single codebase for Android & iOS) |
| Frontend | State management | Riverpod |
| **Backend** | REST API | **Python FastAPI** — REST + JSON |
| **Database** | Main data store | **PostgreSQL** (self-hosted on company server) |
| Cache / rate limit | Redis |
| File storage | Server local disk / MinIO (self-hosted) for photos & attachments |
| Push notifications | Firebase Cloud Messaging (only for notification delivery, no data storage) |
| Excel export | `openpyxl` (Python) / `exceljs` (Node) |
| Deployment | Docker + Docker Compose behind **Nginx** with HTTPS (TLS 1.2+) |

---

## 3. Security Design

- **Passwords** hashed with **Argon2id / bcrypt** — never stored in plain text.
- **JWT access token** (short-lived, 15 min) + **refresh token** (rotating, stored hashed on server, revocable).
- Tokens stored on device in **Flutter Secure Storage** (Android Keystore / iOS Keychain).
- **HTTPS only**, with **SSL certificate pinning** in the app.
- **Role-Based Access Control (RBAC)** enforced on every API endpoint on the server side.
- Rate limiting on login and sensitive endpoints; account lockout.
- Encryption at rest for personal data (DOB, phone, address).
- Input validation, parameterized queries (ORM), protection against SQL injection, XSS, CSRF.
- Root / jailbreak detection, screenshot blocking on sensitive screens, code obfuscation.
- **Audit log** of all logins and admin actions.
- Location data retention policy (attendance punch coordinates and selfies auto-purged, default 90 days).
- Daily encrypted database backups on the server.

---

## 4. Database Tables (high level)

`users`, `roles`, `permissions`, `role_permissions`, `departments`, `shifts`,
`employee_profiles`, `attendance`, `leave_types`, `leave_balances`, `leave_requests`,
`permission_requests`, `late_records`, `holidays`, `food_enrollments` (employee_id, date, need_food),
`announcements`, `announcement_reads`,
`refresh_tokens`, `device_tokens`, `audit_logs`, `settings`.

---

## 5. Main API Endpoints (sample)

```
POST   /api/auth/login                 POST /api/auth/refresh      POST /api/auth/logout
POST   /api/auth/change-password
GET    /api/me/profile                 PUT  /api/me/profile
POST   /api/attendance/check-in        POST /api/attendance/check-out
GET    /api/attendance?from=&to=
GET    /api/leave/balance              POST /api/leave/requests    GET /api/leave/requests
POST   /api/permission/requests        GET  /api/permission/requests
GET    /api/late
PUT    /api/food/enrollment  {date, need_food: true|false}           GET /api/food/my-enrollments
GET    /api/announcements
# Admin
CRUD   /api/admin/employees | roles | departments | shifts | holidays | leave-types | announcements
PUT    /api/admin/requests/{id}/approve | reject
GET    /api/admin/export?type=all|attendance|leave|late|permission|food&from=&to=&dept=
```

---

## 6. App Screens

1. Splash → Login (Employee ID, Password) → Change password (first login)
2. Home dashboard (today's attendance, balances, announcements, birthdays)
3. Attendance (check-in/out, calendar, history)
4. Leave & Permission (Balance tab | Request tab | My Requests tab)
5. Late details
6. Food enrollment
7. Announcements
8. Profile (personal data)
9. Admin: Employees, Roles & Rights, Approvals, Masters, Announcements, Reports & Excel Export, Audit Logs

---

## 7. Project Structure

```
employee-app/
├── frontend/           # Flutter mobile app (lib/features/<feature>/...)
├── backend/            # FastAPI app: models, routers, services
├── database/           # Alembic migrations, schema.sql, seed data, backup/restore scripts
├── deploy/             # docker-compose.yml, nginx.conf, .env.example
├── .claude/agents/     # Claude Code sub-agents used to build this project
└── README.md
```

---

## 8. Setup (once code is generated)

```bash
# Backend (on the office server)
cd employee-app/deploy
cp .env.example .env          # set DB password, JWT secrets, encryption key
docker compose up -d          # starts postgres, redis, api, nginx
docker compose exec api alembic upgrade head
docker compose exec api python -m app.scripts.create_admin   # first Admin user

# Mobile
cd employee-app/frontend
flutter pub get
flutter run --dart-define=API_BASE_URL=https://your-office-server/api
```

---

## 9. Claude Code Agents

The `.claude/agents/` folder contains sub-agents that Claude Code uses to build the project.
Copy the folder into the root of your new repository.

| Agent | Responsibility |
|---|---|
| `project-architect` | Plans the work, defines API contracts between frontend and backend, splits tasks to other agents |
| `database-engineer` | PostgreSQL schema, Alembic migrations, indexes, seed data, backup/restore |
| `backend-developer` | FastAPI endpoints, business rules (leave balance, late, food cut-off), Excel export |
| `flutter-developer` | Flutter screens, state, API client, secure storage, location for attendance |
| `security-reviewer` | Reviews auth, RBAC, encryption, OWASP Mobile/API Top 10 issues (read-only) |
| `qa-tester` | Writes and runs backend pytest and Flutter widget/integration tests |
| `devops-engineer` | Docker Compose, Nginx + TLS, env config, deployment to the office server |

Usage example in Claude Code: *"Use the project-architect agent to plan the leave module, then
have database-engineer, backend-developer and flutter-developer implement it, and finish with
security-reviewer and qa-tester."*

---

## 10. Prompt to Generate This Application

Copy the prompt below into Claude Code (or another AI coding assistant):

```text
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
```
