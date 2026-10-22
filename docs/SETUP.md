# Employee Connect - Developer Setup

Product spec: `README.md`. API source of truth: `docs/api-contract.md`. Per-feature plans: `docs/plans/`. Security reviews: `docs/reviews/`.

| Part | Where | Details |
|---|---|---|
| Backend (FastAPI) | `backend/` | below |
| Database + migrations (Alembic, latest in `database/alembic/versions`) | `database/` | below |
| Mobile app (Flutter) | `frontend/` | `frontend/README.md`, `frontend/PLATFORM_SETUP.md` |
| Server deployment (Docker Compose, nginx, backups) | `deploy/` | `docs/DEPLOY.md` |

## Backend: run the tests

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -r requirements-migrate.txt   # migrate file adds alembic
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```
Tests use SQLite and fakeredis, so no Postgres or Redis is needed. They do not exercise `FOR UPDATE`, partial indexes, `ON CONFLICT`, citext or the audit append-only trigger; verify those against a real Postgres (`docs/DEPLOY.md`).

## Migrations

Run from the environment that has `DATA_ENCRYPTION_KEYS` set (migrations 0009 and 0013 encrypt existing rows in place). Take a backup first and rehearse on a copy of production data. `scripts/gen_schema.sh` cannot run as written; use `python -c "from alembic.config import main; main(argv=[...])"`.

## Environment variables

`backend/.env.example` lists every backend setting with comments (`backend/app/core/config.py` is the source). `deploy/.env.example` lists what compose passes through. Notable ones: `DATA_ENCRYPTION_KEYS`, `JWT_SECRET`, `AUDIT_HMAC_KEY`, `TEMP_PASSWORD_HOURS`, `LEAVE_*` rate limits, `EXPORT_*` limits (`EXPORT_TEMP_DIR` should be a dedicated size-limited volume), `FCM_CREDENTIALS_FILE`, `TRUSTED_PROXIES`.

## Mobile app

Install Flutter, then in `frontend/`: `flutter pub get`, `flutter analyze`, `flutter test`. Release builds require https and at least 2 certificate pins (`CERT_PINS`, see `deploy/scripts/spki-pin.sh`). Firebase (FCM) config is required for push and is not in the repo. The `android/` and `ios/` platform folders are generated with `flutter create` (see `frontend/PLATFORM_SETUP.md`).

## Known gaps

- Dart code has never been compiled or tested (Flutter was not installed when it was written).
- Postgres, Redis and nginx have not run end to end (Docker was off).
- Push: no `firebase_messaging` client wiring yet (no push receiver; tokens are registered only).

## Local development (Docker Desktop, DEV ONLY)
Runs the real stack (postgres, redis, api, worker) on your PC with the API published over plain http. `deploy/docker-compose.dev.yml`
is an override, so the production layout is untouched. nginx and backup are not started (no TLS cert or backup key needed);
postgres and redis stay unpublished. `deploy/.env` holds disposable DEV secrets with `APP_ENV=dev` (enables `/docs`; never use it in
production). It is gitignored; to recreate it copy `deploy/.env.example`, fill the secrets with `deploy/scripts/gen-secrets.sh` and add `DEV_API_PORT`.

Run in PowerShell from `D:\employeeconnect\deploy` (alias once: `function dc { docker compose -p ecdev -f docker-compose.yml -f docker-compose.dev.yml @args }`):
```powershell
dc up -d --build api worker                         # start (migrate + dbrole run automatically)
dc run --rm -e SEED_SKIP_ADMIN=1 seed               # first time only: roles, permissions, leave types, settings
dc exec -T -e ADMIN_PASSWORD='<temp pw>' api python -m scripts.create_admin create ADMIN001 "Dev Administrator"
dc ps                                               # api must be healthy
dc logs -f api                                      # logs
dc stop                                             # stop, keep data
dc down -v                                          # RESET: delete containers AND the ecdev database/uploads volumes
```
Optional demo data (admin password change, shift, manager MGR001 with reports EMP001/EMP002, leave balances):
`DEV_ADMIN_INITIAL_PW=<temp pw> DEV_ADMIN_PW=<new pw> DEV_DEMO_PW=<pw> python deploy/scripts/dev_demo.py http://127.0.0.1:<port>`.

- API URL: `http://127.0.0.1:%DEV_API_PORT%` (`DEV_API_PORT` in `deploy/.env`, default 8000; `/docs` for Swagger). Port 8000 may be taken by another
  project: `docker ps`, then set `DEV_API_PORT=8010`. Android emulator: `http://10.0.2.2:<port>/api`.
- Phone on the same Wi-Fi: set `DEV_BIND=0.0.0.0` in `deploy/.env`, `dc up -d api`, allow the port in Windows Firewall for the Private profile
  (`New-NetFirewallRule -DisplayName ecdev -Direction Inbound -Protocol TCP -LocalPort 8010 -Action Allow -Profile Private`), and use
  `http://<PC LAN IP>:<port>/api`. Revert to `127.0.0.1` afterwards. A debug-build app can use http; release builds need https.
- `must_change_password`: users created by `create_admin` or the admin API (and after `reset`) log in with `must_change_password: true`; until they
  `POST /api/auth/change-password` the other endpoints are refused (exact status not verified here). The demo script already did this, so those users log in directly.
- Locked out or forgot: `dc exec -it api python -m scripts.create_admin unlock|reset ADMIN001`.
- Behind nginx (production) a 413 or 429 comes back as an HTML page, not JSON; the dev stack has no nginx, so the app will not see that locally.
- The app's own rate limits still apply (login: `LOGIN_RATE_EID` per employee, raised to 20 in the dev `.env`).
