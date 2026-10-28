# Employee Connect - Deployment Guide (on-premise, Docker Compose)

Everything lives in `deploy/`. Run all commands from `deploy/` on the server (Linux with Docker Engine + Compose v2).

```
Internet/LAN -> :80/:443 nginx (also serves the static admin console at /console/) --(internal net)--> api:8000 --> postgres, redis
                                                   worker  --> postgres, redis
                                                   backup  --> postgres  (nightly encrypted dump + uploads archive -> volume "backups"; uploads mounted read-only)
```
- Only nginx publishes ports. `api` uses `expose:` (never `ports:`); postgres/redis are on an `internal: true` network.
- `api`/`worker` additionally join an outbound-only `egress` network for FCM push / SMTP.
- Containers run non-root, `cap_drop: ALL`, `no-new-privileges`, read-only root fs (tmpfs `/tmp`), with CPU/memory/pids limits.
- Volumes: `pgdata` (database), `uploads` (`/data/files` in api/worker, read-only `/data` in backup), `redisdata`, `backups`.

## 1. First deploy

### 1.1 Prepare the host
1. Install Docker Engine and the Compose plugin. Give the server a stable name/IP (DNS record such as `hr.company.local`).
2. Network and firewall (section 6): choose port-forward or VPS relay, set `PUBLIC_HOSTNAME`/`PUBLIC_ORIGIN`, allow only 80/443; nothing else inbound except SSH from admin hosts.
3. Get the repo onto the server (`git clone` / copy) and `cd deploy`.

### 1.2 Configure
```bash
cp .env.example .env && chmod 600 .env
./scripts/gen-secrets.sh            # prints POSTGRES_PASSWORD, APP_DB_PASSWORD, REDIS_PASSWORD, JWT_SECRET, AUDIT_HMAC_KEY, DATA_ENCRYPTION_KEYS
```
Paste the printed lines into `.env` (replace the empty ones). Review the rest of `.env`:
- `APP_ENV=prod` (no `/docs`, `CORS_ORIGINS` must be empty).
- `NET_SUBNET` / `NGINX_IP`: leave unless they clash with your LAN. Compose derives `TRUSTED_PROXIES=<NGINX_IP>/32`, so the api only believes `X-Real-IP` from nginx.
- `DATABASE_URL` is built for the NON-owner role `app_user`; `OWNER_DATABASE_URL` (role `app_owner`) is used only by migrations/seed/backups.
- `DATA_ENCRYPTION_KEYS`: comma list of Fernet keys, newest first. **Back this value up somewhere other than the DB backups**; without it encrypted personal/location data is unrecoverable. Alternative generator: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`.
- `JWT_SECRET` alternative: `python -c "import secrets;print(secrets.token_urlsafe(48))"`.
- Set `OFFICE_LAT/LNG/RADIUS_M/OFFICE_TIMEZONE` if you want them seeded (also editable later in the app).
- FCM: place the Firebase service-account JSON at `secrets/api/fcm.json`, `sudo chown 10001 secrets/api/fcm.json && chmod 400 secrets/api/fcm.json`, and set `FCM_CREDENTIALS_FILE=/run/app-secrets/fcm.json`. `secrets/api/` is mounted read-only into api/worker, so the key is never baked into an image or passed as an env value.
- FCM push also needs the `firebase-admin` package, which is deliberately not in `requirements.txt`. Set `FIREBASE_ADMIN_VERSION=<tested version>` in `.env` (build arg of `deploy/api.Dockerfile`, used for api and worker) and run `docker compose build api worker && docker compose up -d`. Left empty, the app runs and push is skipped with a log warning.
- The migrate/seed image installs from the pinned `backend/requirements-migrate.txt` (includes `requirements.txt`); bump pins there, not in the Dockerfile.
- Excel export: `EXPORT_TEMP_DIR` is **mandatory** in prod (the api refuses to start without it). Compose sets `/exports` and mounts it as a private tmpfs for the `api` service only (mode 0700, uid 10001, size `EXPORT_TMPFS_SIZE`, default 256m; it counts against the api memory limit, raise `deploy.resources.limits.memory` if you raise it). Do not point it at a shared directory such as `/tmp`. Per-user daily export quotas (`EXPORT_DAILY_MAX_*`) and the `app.export` WARNING log lines (large exports, heavy daily use) are described in `deploy/.env.example`; alert on that logger. Reasons/comments in exports need the `export.sensitive` permission (migration 0015 grants it to admin and hr).
- Uploads: files are stored in volume `uploads` at `/data/files`. `MAX_UPLOAD_MB` (default 10) and nginx must agree: nginx allows 12 MB (`MAX_UPLOAD_MB` + ~2 MB multipart overhead) only on `location = /api/files` (`POST /api/files`), and 6 MB on `location = /api/me/profile/photo` (profile photo, backend cap 5 MB); every other `/api/` route accepts 256 KB bodies. Both upload locations share the `upload` rate zone and the 8-connection `uploadsrv` cap. If you change `MAX_UPLOAD_MB`, edit `client_max_body_size` in that location and reload nginx. Spooled upload bodies go to a dedicated 128 MB tmpfs (`/var/body_temp`), sized for the 8-concurrent-upload cap.

### 1.3 TLS certificate
Pick one (both produce `certs/fullchain.pem` and `certs/privkey.pem`, which nginx mounts read-only):

**Dev / pilot, self-signed**
```bash
./scripts/gen-selfsigned-cert.sh hr.company.local 192.168.1.20     # first arg = CN, all args become SANs
```
It prints the SPKI pin. The phone must trust it (or, with pinning on, accept it by pin only; see 1.3.2).

**Production, internal CA or public CA**
1. Create a key + CSR (keep the key on the server only):
   `openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -keyout certs/privkey.pem -out hr.csr -subj "/CN=hr.company.local" -addext "subjectAltName=DNS:hr.company.local"`
2. Have the CA sign `hr.csr`. Save the leaf **followed by the intermediate(s)** as `certs/fullchain.pem` (not the root).
3. `chmod 640 certs/privkey.pem && sudo chgrp 101 certs/privkey.pem` (nginx runs as uid 101).
4. Renewal: replace the two files, then `docker compose exec nginx nginx -c /tmp/nginx.conf -s reload` (Let's Encrypt: section 6.6, keep the same key). If you keep the same key, the pin does not change; otherwise ship an app update first (below).

**1.3.2 SPKI pins for the app's `CERT_PINS`**
The app pins `base64(sha256(SubjectPublicKeyInfo))` and requires at least two pins in release builds (current + backup).
```bash
./scripts/spki-pin.sh certs/fullchain.pem            # leaf pin (changes if you generate a new key)
./scripts/spki-pin.sh --n 2 certs/fullchain.pem      # intermediate CA pin (survives leaf renewals under the same CA)
./scripts/spki-pin.sh --host hr.company.local        # pin of what the server actually serves
./scripts/spki-pin.sh --gen-backup spare.key         # NEW offline spare key: store spare.key offline, use its pin as the backup pin
```
Build the app with `--dart-define=API_BASE_URL=https://hr.company.local/api --dart-define=CERT_PINS=<pin1>,<pin2>`.
Rotation rule: before the leaf key changes, ship an app release that already contains the new key's pin (the spare). Pinning disables the OS trust store, so a self-signed cert works in the app with no device install.

### 1.4 Start
```bash
# Ports: nginx publishes HTTP_PORT (80) and HTTPS_PORT (443) from .env. Other apps on the same server (e.g. the RFS
# Generator on 8501) do not clash; change them only if 80/443 are taken, and then add the port to PUBLIC_ORIGIN,
# the router port-forward and the app's server address (https://host:PORT).
docker compose up -d --build
docker compose ps                    # migrate/dbrole show "Exited (0)"; others "healthy"
```
Startup order is automatic: postgres -> `migrate` (alembic upgrade head as `app_owner`) -> `dbrole` (`ALTER ROLE app_user LOGIN PASSWORD ...`, replaces the manual step) -> api/worker -> nginx.

### 1.5 Seed and first Admin
`database/seed.py` is idempotent: it loads roles, the permission catalogue, leave types, default shift/department and settings. **Recommended first-admin path: seed without an admin, then create the admin with the CLI**, so no admin password appears in `.env`, shell history or a process environment (the CLI prompts for it):
```bash
docker compose run --rm -e SEED_SKIP_ADMIN=1 seed          # same effect: `... seed python database/seed.py --no-admin`
docker compose exec -it api python -m scripts.create_admin create ADMIN001 "Full Name" --email it@company.example --company-name "Acme Pvt Ltd"   # prompts for password
```
`--company-name` (optional) sets the company name stored with the installation (shown in the console and on the license).
`SEED_SKIP_ADMIN=1|true|yes` (or `--no-admin`) makes the seed never touch users. `create_admin` needs the seeded roles, so the seed always comes first. The admin must change the password at first login.

Legacy alternative (the seed creates the admin; the password is visible in the process environment, so prefer the above):
```bash
docker compose run --rm -e ADMIN_EMPLOYEE_ID=ADMIN001 -e ADMIN_PASSWORD='Use-A-Long-Temp-Passw0rd!' -e ADMIN_EMAIL=it@company.example seed
```
Further/break-glass admin operations use the same CLI in the running api container (it audits every action as `cli`):
```bash
docker compose exec -it api python -m scripts.create_admin unlock ADMIN001      # clear a lockout
docker compose exec -it api python -m scripts.create_admin reset  ADMIN001      # new temp password, printed once
```
On upgrades `docker compose run --rm seed` needs no admin variables once an admin exists.

### 1.5.1 License
Each installation needs a vendor-signed license (Ed25519; the token `ECL1.<payload>.<signature>` is bound to this installation's deployment id, so it cannot be moved to another server). The vendor tools live in `tools/license/` and are run offline on the vendor's machine, never on the customer server:
- `gen_vendor_key.py [--out vendor_private.key]` generates the signing key pair once. Keep the private key offline and backed up; never commit it or copy it into `deploy/`. Paste the printed public key into `backend/app/core/license_keys.py` (`VENDOR_PUBLIC_KEYS`) before building the image. To rotate, add the new public key and keep the old one until all licenses signed with it are replaced.
- `issue_license.py --key vendor_private.key --deployment-id <id> --license-id L-2026-0042 --licensed-to "Acme Pvt Ltd" --max-users 50 [--expires 2027-10-22] [--grace-days 14] [--on-expiry block_new_users|read_only] [--issued-at ...]` prints the token. No `--expires` = never expires. A replacement license must have a later `--issued-at` than the installed one.

Customer side (get the deployment id first, send it to the vendor, then install the returned token saved as a file):
```bash
docker compose exec -it api python -m scripts.create_admin license show             # status, seats, deployment id
docker compose exec -T api sh -c 'cat > /tmp/license.txt' < license.txt           # /tmp is a writable tmpfs (docker cp does not work on tmpfs)
docker compose exec -it api python -m scripts.create_admin license install /tmp/license.txt
```
The CLI verifies the signature against the built-in public key before storing. Alternatively paste the token in the console under Admin > License. `create_admin create` refuses to add users beyond the licensed seats.

### 1.6 Verify
```bash
curl -ksI https://hr.company.local/api/auth/login | head -1       # 405/422 = reached the api through nginx
curl -sI  http://hr.company.local/ | head -3                      # 301 -> https
curl -k --max-time 3 https://hr.company.local/docs -o /dev/null -w '%{http_code}\n'   # 404
nc -zv <server> 8000 5432 6379                                    # all must be refused from another machine
docker compose logs api | grep -i "peer\|proxy" | head            # client IPs are real addresses, not the nginx IP
```
Check the audit log shows your workstation IP, not `172.31.250.10`; if it shows nginx's IP, `TRUSTED_PROXIES`/`NGINX_IP` is wrong.
Limits (nginx.conf): login 300 req/min per IP (burst 100), other `/api/auth/*` 600/min (burst 100), general API 20 req/s (burst 60), `POST /api/files` 120/min per IP (burst 20, max 8 concurrent, 12 MB body), all other bodies 256 KB.

**Login rate limiting and office NAT.** Staff behind one office NAT share one public IP, so per-IP limits must not be the main control:
- Primary, per employee (app, Redis): `LOGIN_RATE_EID` (10 attempts per `LOGIN_RATE_WINDOW_SECONDS`, 600) plus account lockout (`LOCKOUT_THRESHOLD`/`LOCKOUT_MINUTES`). Keep these tight; they stop password guessing against one account regardless of source IP.
- Secondary, per IP: app `LOGIN_RATE_IP` (default 300 per window) and the nginx zones `login`/`authapi` (`rate=300r/m`, `600r/m`). They only catch floods and credential stuffing across many accounts. Size `LOGIN_RATE_IP` to roughly 1.5 x head-count per window and keep the nginx rate in the same ballpark. nginx cannot key on the employee id (it is in the JSON body), which is why the per-employee limit lives in the app.
- If staff still see 429 in the morning rush, first check the audit log shows real client IPs (not the nginx IP), then raise `LOGIN_RATE_IP` and the nginx `rate`/`burst` together, then `docker compose up -d` (and `docker compose up -d nginx` for nginx.conf edits).

If a load balancer sits in front of nginx, add `set_real_ip_from <LB>; real_ip_header X-Forwarded-For; real_ip_recursive on;` in the `http {}` block of `nginx.conf`. Keep `TRUSTED_PROXIES` equal to the nginx container only.

## 2. Upgrade
```bash
git pull                                  # or copy the new release
docker compose build --pull
docker compose run --rm backup once       # fresh backup first (needs the backup key config)
docker compose up -d                      # re-runs migrate -> dbrole, recreates changed containers
docker compose run --rm seed              # re-sync permissions/defaults (no ADMIN_* needed once an admin exists)
docker compose ps && docker compose logs --tail=50 api worker
```
Pin releases with `APP_VERSION=1.2.0` in `.env` (build and tag together). Rollback: restore the previous image tag, and if a migration ran, restore the pre-upgrade backup (section 3) - Alembic downgrades are not relied on in prod.
Rotate `DATA_ENCRYPTION_KEYS` by prepending a new key (keep old ones), `up -d`, then run `docker compose exec api python -m scripts.rotate_keys` (use `--dry-run` first; it re-encrypts every encrypted column and file blob and verifies against the new key alone), and drop the old key only when it reports `failed: 0`.

## 3. Backups and restore

### 3.1 What runs
The `backup` container also archives the uploads volume nightly (3.6). It runs `database/scripts/backup.sh` daily at `BACKUP_TIME_UTC` (default 02:00 UTC) and once at start if none is younger than 24 h: `pg_dump -Fc | gpg` into `/backups` (volume `backups`) as `employeeconnect_<stamp>.dump.gpg` + `.sha256`; files older than `BACKUP_RETENTION_DAYS` (14) are deleted. No plaintext dump touches disk. The container goes unhealthy if the newest DB dump or uploads archive is older than 26 h (`docker compose ps`; wire this to your monitoring). The container runs as root with only `DAC_READ_SEARCH` + `DAC_OVERRIDE` (all other caps dropped, uploads mounted read-only) because the app stores uploads as uid 10001 with mode 0600/0700, which uid 999 cannot read. Archives are written as `*.partial` and renamed only on success.

### 3.2 Choose the encryption mode (required before `up`; backup refuses to run unencrypted)
**A. Public key (recommended)** - the private key stays off the server (HR/IT safe, offline token).
```bash
gpg --quick-generate-key "EC Backup <backup@company.example>" ed25519 cert 5y   # on a SEPARATE machine; add an encryption subkey:
gpg --quick-add-key <FPR> cv25519 encr 5y
gpg --armor --export backup@company.example > backup_public.asc
gpg --armor --export-secret-keys backup@company.example > backup_private.asc    # store offline, 2 copies, passphrase-protected
```
Copy `backup_public.asc` to `deploy/secrets/backup/`, then `sudo chown 999 deploy/secrets/backup/backup_public.asc && chmod 400 ...` and set in `.env`: `BACKUP_GPG_RECIPIENT=backup@company.example`, `BACKUP_GPG_PUBLIC_KEY_FILE=/run/backup-secrets/backup_public.asc`.

**B. Symmetric passphrase** - simpler, but the passphrase sits on the same host as the backups.
`openssl rand -base64 48 > secrets/backup/backup_passphrase; sudo chown 999 secrets/backup/backup_passphrase; chmod 400 secrets/backup/backup_passphrase`; set `BACKUP_PASSPHRASE_FILE=/run/backup-secrets/backup_passphrase`. Keep a copy of the passphrase offline.

Then `docker compose up -d backup`, and run one now: `docker compose run --rm backup once` (or `docker compose exec backup backup.sh`).

### 3.3 Off-server copies (do this; a backup on the same disk is not a backup)
The `backups` volume is local. Copy it nightly to another machine/tape, e.g. host cron:
`docker run --rm -v employeeconnect_backups:/b:ro -v /mnt/nas/ec:/out alpine sh -c 'cp -n /b/employeeconnect_* /out/'`
Also store `.env` secrets (esp. `DATA_ENCRYPTION_KEYS`, `JWT_SECRET`) in your password vault. Files are encrypted, so the NAS needs no extra trust.

### 3.4 Restore drill (do quarterly, and before go-live)
Restore is non-destructive by default: it creates a **new** database `employeeconnect_restore_<stamp>`.
1. Pick a file: `docker compose run --rm backup ls -lh /backups`.
2. Restore. Mode B: `docker compose run --rm backup restore.sh /backups/employeeconnect_<stamp>.dump.gpg`.
   Mode A: the private key is not on the server; either decrypt elsewhere (`gpg -d file | pg_restore ...` against a scratch Postgres) or temporarily mount the private key: copy `backup_private.asc` to `/tmp` of a one-off container (`docker compose run --rm -v $PWD/restore-key:/rk:ro backup bash -c 'gpg --batch --import /rk/backup_private.asc && restore.sh /backups/<file>'`) and delete `restore-key/` afterwards.
3. The script verifies the checksum, restores, re-applies `app_user` grants and prints `users=N audit_logs=N alembic=<rev>`. Compare with production (`docker compose exec postgres psql -U app_owner -d employeeconnect -Atc "select count(*) from users"`) and the alembic revision.
4. Point-test the data: start a scratch api against the restored DB (copy the stack with `DATABASE_URL` pointing at it) or query a few rows; confirm encrypted fields decrypt with your `DATA_ENCRYPTION_KEYS`.
5. Drop the drill DB: `docker compose exec postgres psql -U app_owner -d postgres -c 'DROP DATABASE "employeeconnect_restore_<stamp>"'`.
6. Do the same drill for uploads (3.6), then record date, backup file, elapsed time (RTO) and data age (RPO <= 24 h) in your ops log.

### 3.5 Disaster restore into production
`docker compose stop api worker nginx`, then `docker compose run --rm backup restore.sh <file> --target-db employeeconnect --yes` (drops and recreates objects), then `docker compose up -d` (roles persist in the restored DB; `dbrole` re-sets the app_user password). If the whole host is lost: fresh install (section 1) with the **same** `.env` secrets, restore into the empty DB with the command above after `migrate` has run, and restore the `uploads` volume from your file copy.
The uploads archive is separate from the DB dump (3.6): restore it as well, otherwise file references in the database point at missing files.

### 3.6 Uploads backup and restore
`database/scripts/backup-uploads.sh` is baked into the `backup` image. The `uploads` volume is mounted **read-only** at `/data` in that container, so a backup bug cannot alter user files. It runs nightly at `BACKUP_UPLOADS_TIME_UTC` (default 02:30 UTC, after the DB dump) and once at start if none is younger than 24 h, writing `employeeconnect_uploads_<stamp>.tgz.gpg` (+ `.sha256`) to the `backups` volume with the same GPG settings and `BACKUP_RETENTION_DAYS` as the DB dumps. The off-server copy in 3.3 (`employeeconnect_*`) covers these files. Run one now: `docker compose run --rm backup once-uploads`. The archive is of the volume root, which is `/data/files` inside api/worker.

Restore into a scratch directory first (mode B shown; mode A needs the private key as in 3.4 step 2):
```bash
mkdir restore-out
docker compose run --rm -v $PWD/restore-out:/out backup bash -c 'cd /backups && sha256sum -c employeeconnect_uploads_<stamp>.tgz.gpg.sha256 && gpg --batch --pinentry-mode loopback --passphrase-file "$BACKUP_PASSPHRASE_FILE" -d employeeconnect_uploads_<stamp>.tgz.gpg | tar xzf - -C /out'
```
Disaster restore into production: `docker compose stop api worker`, then copy into the live volume with a one-off container that mounts it read-write (the backup service is read-only on purpose), then `docker compose up -d`:
```bash
docker run --rm -v employeeconnect_uploads:/data -v $PWD/restore-out:/in:ro alpine sh -c 'cp -a /in/. /data/ && chown -R 10001 /data'
```

### 3.7 Status file, health endpoint and TLS expiry
After every successful run `backup.sh` / `backup-uploads.sh` write `/status/backup.json` (`db_last_success_at`, `uploads_last_success_at`, UTC) into the `status` volume, which the `backup` service mounts read-write and the `api` mounts read-only (`STATUS_DIR`). The api health endpoint reports `backup.ok` false (overall `degraded`) when either timestamp is missing or older than its threshold; right after first deploy it stays degraded until both a DB and an uploads backup have succeeded. A failed run leaves the old timestamps untouched.

The api also mounts only `certs/fullchain.pem` (read-only, never the private key) at `TLS_CERT_FILE=/certs/fullchain.pem` to report certificate expiry (degraded below 14 days). A single-file bind mount keeps pointing at the old file if renewal replaces it by rename, so after renewing run `docker compose restart api` as well as the nginx reload. `APP_VERSION` from `.env` is passed to the api and reported as `app_version`; set it to the release you deploy (it is also the image tag).

## 4. Day-2 operations
- Logs: `docker compose logs -f nginx api worker` (json-file, 10 MB x 5 per container). nginx logs omit query strings.
- Update base images: `docker compose pull postgres redis nginx && docker compose up -d` (postgres major upgrades need dump/restore; image is pinned to 16).
- Disk: watch `docker system df -v` for `pgdata`, `uploads`, `backups`. nginx upload spooling is capped by its 128 MB tmpfs and cannot fill the host.
- Redis holds rate-limit and lockout counters only; losing it is harmless.

## 5. Troubleshooting
| Symptom | Likely cause |
|---|---|
| `docker compose` says `set X` | missing value in `.env` (required: passwords, JWT_SECRET, DATA_ENCRYPTION_KEYS, NET_SUBNET, NGINX_IP, DB URLs) |
| api exits at start: JWT/CORS error | prod rejects placeholder-looking JWT secrets and non-empty `CORS_ORIGINS` |
| `worker` restart-looping | backend has no `app.jobs.scheduler` module yet |
| Everyone gets 429 on login | all clients share one IP: check `TRUSTED_PROXIES` / `NGINX_IP`, or an upstream LB masking IPs; or raise limits |
| nginx unhealthy | missing/unreadable `certs/fullchain.pem` / `privkey.pem` (uid 101 must read the key) |
| `migrate` fails: `DATA_ENCRYPTION_KEYS must be set` | migrations 0009/0013 encrypt existing data; compose passes the key to `migrate`, so set it in `.env` |
| `migrate` fails on extensions/roles | `POSTGRES_USER` must be a superuser (the default bootstrap user is) |

## 6. Internet exposure, remote access and firewall
Staff use the app on mobile data and admins use the web console from anywhere, so the server must be reachable from the Internet through ONE host name (`PUBLIC_HOSTNAME`, e.g. `hr.company.com`, a record in the company's own domain). Only nginx is exposed; postgres, redis and the api have no published ports.

### 6.1 Choose a path
| | A. Port-forward (PRIMARY: the office has a public IPv4) | D. VPS relay over WireGuard (fallback: CGNAT / no inbound port) |
|---|---|---|
| Inbound ports at the office | 443 (and 80 only for HTTP-01 renewal) | none |
| Third party in the data path | none | a small rented VPS that only forwards ciphertext (TLS still ends on our nginx) |
| Certificate pinning | unchanged | unchanged |
| Extra | static IP or DDNS | VPS (~USD 5/month) to patch, +10-30 ms |

Not recommended: Cloudflare Tunnel (Cloudflare decrypts all traffic and its rotating edge certificate breaks the app's key pinning); record it as an accepted risk if ever chosen. A staff-wide VPN is too heavy for every employee.
If the ISP ever changes the line, check: compare the WAN address on the router with `curl ifconfig.me`. If they differ, or the WAN address is in 100.64.0.0/10 or a private range, you are behind CGNAT: use D.
Both paths use the same `.env` values, certificate and nginx; D only adds `docker-compose.relay.yml`.

### 6.2 `.env` values (all paths)
`PUBLIC_HOSTNAME=hr.company.com`, `PUBLIC_ORIGIN=https://hr.company.com`, optional `CONSOLE_ALLOW_CIDRS` (6.8). nginx renders `nginx.conf` at start (`scripts/nginx-start.sh`), serves only that Host/SNI and refuses everything else (6.7). Apply changes with `docker compose up -d`.

### 6.3 DNS, DDNS and split DNS
- Public: `A hr.company.com -> office public IP` (option D: the VPS public IP). TTL 300.
- Dynamic IP (option A): use the router's DDNS client, or a `ddclient` job that updates the record through your DNS provider's API. If the provider can proxy (Cloudflare), set the record to DNS-only (grey cloud).
- Split-horizon DNS: the office DNS/router resolves `hr.company.com` to the server's LAN IP, so office Wi-Fi works without NAT hairpinning and keeps working when the Internet is down.
- Never point other host names at the server: they are refused.

### 6.4 Option A: port-forward steps
1. Give the server a fixed LAN IP (DHCP reservation).
2. Router: forward WAN TCP 443 -> server:443 (add WAN 80 -> server:80 only if you use HTTP-01, 6.6). Nothing else. Disable UPnP and remote administration, change the router admin password, keep the firmware current.
3. Add the DNS records (6.3). Test from mobile data (Wi-Fi off): `curl -I https://hr.company.com/api/public/company`.

### 6.5 Fallback only: VPS relay over WireGuard (CGNAT / no inbound port)
This office has a public IP, so use 6.4. Keep this as a fallback if the ISP ever moves the line behind CGNAT. The server dials OUT to a small VPS (WireGuard, e.g. VPS `10.8.0.1`, server `10.8.0.2`); HAProxy on the VPS forwards raw TCP 443 (no TLS termination) into the tunnel with PROXY protocol, so TLS, pinning and client IPs stay as in option A.
- VPS: `apt install wireguard haproxy`, a `wg0` with `Address=10.8.0.1/24`, `ListenPort=51820` and the server as peer (`AllowedIPs=10.8.0.2/32`); its firewall allows only SSH (admin IP), UDP 51820, TCP 443. HAProxy: `frontend :443 mode tcp` -> `backend server 10.8.0.2:8444 send-proxy-v2 check`.
- Server: `wg0` with `Address=10.8.0.2/24`, peer = VPS (`Endpoint=<vps>:51820`, `AllowedIPs=10.8.0.1/32`, `PersistentKeepalive=25`). No router port-forward.
- `.env`: `WG_BIND_IP=10.8.0.2`, `RELAY_PEER_IP=10.8.0.1`; start with `docker compose -f docker-compose.yml -f docker-compose.relay.yml up -d` (nginx also listens on `10.8.0.2:8444`, PROXY protocol trusted only from `RELAY_PEER_IP`). Use DNS-01 for the certificate (6.6). Verify audit-log IPs are real client addresses, not `10.8.0.1`.

### 6.6 Let's Encrypt with a stable key (required for pinning)
The app pins the leaf public key, so every renewal MUST reuse the same private key: `certbot --reuse-key` (acme.sh: do not use `--always-force-new-key`).
```bash
# HTTP-01 (option A): nginx serves deploy/acme/ at /.well-known/acme-challenge/ on port 80
certbot certonly --webroot -w /opt/employeeconnect/deploy/acme -d hr.company.com --key-type ecdsa --reuse-key
# DNS-01 (option D, or no port 80): use your DNS plugin, e.g. certbot-dns-cloudflare
certbot certonly --dns-cloudflare --dns-cloudflare-credentials /root/.cf.ini -d hr.company.com --key-type ecdsa --reuse-key
```
Deploy hook `/etc/letsencrypt/renewal-hooks/deploy/employeeconnect.sh` (chmod +x):
```bash
#!/bin/sh
set -e
D=/opt/employeeconnect/deploy
cp /etc/letsencrypt/live/hr.company.com/fullchain.pem $D/certs/fullchain.pem
cp /etc/letsencrypt/live/hr.company.com/privkey.pem  $D/certs/privkey.pem
chgrp 101 $D/certs/privkey.pem && chmod 640 $D/certs/privkey.pem
docker compose -f $D/docker-compose.yml exec -T nginx nginx -c /tmp/nginx.conf -s reload
```
Pin workflow: after the first issue run `./scripts/spki-pin.sh certs/fullchain.pem` (current) and `./scripts/spki-pin.sh --gen-backup spare.key` (offline spare) and build the app with both in `CERT_PINS`. After the first renewal confirm `./scripts/spki-pin.sh --host hr.company.com` prints the SAME pin (proves `--reuse-key` works). Never delete `/etc/letsencrypt` (it holds the key). To rotate the key: ship an app with the spare pin first, then issue with `--new-key`. Pinning Let's Encrypt intermediates is not supported by the app (leaf only) and they rotate anyway. The browser console uses the normal trust store, which is why a public certificate is required. nginx does not staple OCSP (Let's Encrypt retired it).

### 6.7 Firewall and hardening
- Expose ONLY 443 (+80 if HTTP-01). Never 22, 5432, 6379, 8000. SSH only from the LAN/admin VPN, key-only. Keep the host patched (unattended security updates); put the volumes on an encrypted disk if the server is physically shared.
- Docker publishes ports through iptables and bypasses ufw/firewalld INPUT rules, so filter in `DOCKER-USER`. For Internet exposure allow 80/443 and drop other new traffic to published containers (the chain sees traffic after port mapping, so it matches the container ports 8080/8443):
```bash
sudo iptables -I DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
sudo iptables -A DOCKER-USER -i eth0 -p tcp -m multiport --dports 8080,8443 -j RETURN
sudo iptables -A DOCKER-USER -i eth0 -j DROP    # eth0 = WAN/LAN interface; persist with iptables-persistent/nftables
```
  Option D: the 8444 listener is published on the WireGuard address only (`wg0`), which this `eth0` rule does not touch. The compose file publishes no other port, so an external scan must show only 80/443 (option D: nothing at the office).
- Unknown host rejection (built in): nginx refuses the TLS handshake for any SNI other than `PUBLIC_HOSTNAME`, closes (444) plain-HTTP requests for other hosts and Host/SNI mismatches (421), and exposes no `/docs`. Bare-IP scanners learn nothing. Verify: `curl -vk https://<public-ip>/` fails the handshake; `curl -k --resolve hr.company.com:443:<ip> https://hr.company.com/docs` returns 404.
- Optional: fail2ban on the nginx log for bursts of 401/429/444 (`docker compose logs nginx`), and an external uptime check on `https://hr.company.com/api/public/company`.
- Rate limits (nginx, per client IP): login 300/min, other `/api/auth/*` 600/min, `/api/auth/web/login` 10/min (burst 5), `/api/public/*` 120/min, general API 20 r/s, uploads 120/min. Per-employee limits and lockout in the app remain the primary brute-force control; with Internet exposure, lower `LOGIN_RATE_IP` and the `login`/`authapi` rates as far as your office NAT head-count allows.

### 6.8 Admin console (Admin role only, reachable from the Internet)
- Served by nginx at `https://hr.company.com/console/` from `deploy/console/` (read-only mount, empty until built; see `deploy/console/README.md`). Build: `cd frontend && flutter build web --release --target lib/main_console.dart --base-href /console/ --no-web-resources-cdn --pwa-strategy none`, copy `build/web/*` into `deploy/console/`. No restart needed.
- Browser auth uses `/api/auth/web/*` only: the refresh token is an HttpOnly, Secure, SameSite=Strict cookie; nginx forwards cookies to that path only, requires `Origin == PUBLIC_ORIGIN` and rate-limits login to 10/min per IP. Only users holding `console.access` can log in (grant it to Admin only).
- `/console/` has its own strict CSP (self only, no CDN, no framing), `no-cache`, HSTS and the shared security headers. Flutter web needs `script-src 'wasm-unsafe-eval'`; `base-uri` is `'self'` (not `'none'`) because Flutter's index.html carries a `<base>` tag.
- Optional allow-list toggle (off by default): set `CONSOLE_ALLOW_CIDRS=203.0.113.0/24,192.168.1.0/24` and `docker compose up -d` to limit `/console/` and `/api/auth/web/*` to those networks; the mobile API is never affected. With it empty the console is open to the Internet, so use long admin passwords and review the audit log.
- Future work, recommended before relying on Internet-reachable admin access: two-factor authentication (TOTP) for console logins. Not implemented; today's controls are password + lockout + rate limits + short web sessions.
- MDM: a lost or leaving employee's phone is handled by server-side deactivation (the app wipes itself on `ACCOUNT_INACTIVE`); remote uninstall needs company-managed devices (Android Enterprise/MDM) and is out of scope.

### 6.9 Reload
After replacing certificates: `docker compose exec nginx nginx -c /tmp/nginx.conf -s reload`. After editing `nginx.conf` or `.env`: `docker compose up -d nginx` (the container runs `nginx -t` first and exits with the error in `docker compose logs nginx`).
