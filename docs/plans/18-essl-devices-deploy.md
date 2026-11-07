# Plan 18 deploy: eSSL / ZK attendance devices

Branch `feature/essl-devices` (migration 0030). Nothing here has been run on production yet. Background and design: `18-essl-devices.md`, `docs/DEPLOY.md` section 7.

## 0. Rule that never changes: the machines' logs are never deleted

- The application sends only read commands. The allow-list is `app/integrations/zk/protocol.py` (connect, auth, exit, options, version, time, free sizes, buffer read). Clear-log, delete-user, set-time and restart are not in it, and a test fails if one is added. A packet capture of a real session on 2026-11-07 showed only those commands.
- The retention job (`devices.retention`) prunes **our database copy** only, never the machine.
- **Do not clear a machine's memory by hand** from its menu or from any software, until the records are confirmed safely in our database and in a backup.
- eTimeTrackLite: its option "delete logs from device after download" must stay **OFF**. This needs a person to check (not done yet). If it is ON, eTimeTrackLite wipes the logs we have not read yet, and our count-drop alert fires.

Machines (verified 2026-11-07): `.201` A6FE201561197 (36 users, 29,228 records), `.202` NFZ8260505793 (3 users, 800 records), `.203` NFZ8251103508 (17 users, 7,417 records). All answer without a communication key (key 0). Punch layouts verified on all three by `scripts/zk_dryrun.py`.

## 1. Gates before starting

| Gate | Who | Status |
|---|---|---|
| Backend suite green (2207 passed) and Flutter device tests green (71) | done | OK, re-run after any further change |
| Migration 0030 up, down and up again on Postgres 16; append-only trigger checked as `app_user` | done | OK |
| Real-device read of users and punches on all 3 machines | done | OK |
| eTimeTrackLite "delete logs after download" is OFF | user | **tested 2026-11-07**: the user downloaded the logs from eTimeTrackLite (twice) and reported the record counts unchanged (user's report; the counts were not pasted, and the setting screen itself was not viewed). Machines covered: all three (`.202` confirmed by the user afterwards). Optional: also view the setting screen |
| Employee codes vs machine user ids: compared 2026-11-07, see section 1.1 | user | **open**, the user will change the codes by hand |
| "Ingest from" date per machine. Decided 2026-11-07: **keep the previous punch records**, so use the machine's oldest date (`.201` 2024-03-25, `.203` 2025-08-29, `.202` 2026-08-07). History is stored as raw records only and never rewrites attendance | user | **decided**, enter the dates when adding the machines |
| A quiet time window agreed (the egress network change can cause a short outage, see 4.4) | user | **open** |

### 1.1 Employee codes vs machine user ids (checked 2026-11-07, read-only)

The app has 5 users: `ACA001`, `ACA002` (inactive and deleted, never matches), `ACA003`, `ACA004`, `ADMIN001`. The machines hold 44 distinct ids:

| Machine | Ids on the device | Match an app employee |
|---|---|---|
| `.202` | `ACA001`, `ACA028`, `ACA035` | `ACA001` only |
| `.201` | 36 ids, `E001` to `E092` | none |
| `.203` | 17 ids (12 also on `.201`, plus `E019`, `E075`, `E080`, `E081`, `E094`) | none |

41 distinct `E…` ids across `.201` and `.203`. Matching rule (`device_matching.py`, active and not deleted users only): exact employee ID, case-insensitive; else numeric equality when the device id is all digits (after removing `device.match_prefix`). `E003` is not all digits, so only an exact code `E003` matches it.

Until employees with those codes exist, punches are stored and shown as unmatched, never lost, and no attendance is written for them. The user changes the codes by hand after this process. Either way works:
1. **Rename or create the employees with the device ids** (`E003`, `ACA028`, ...). The next sync or the 00:20 recompute then matches them by exact ID.
2. **Leave the codes and map in the console** (Unmatched users, per machine or for all machines).

Do this before relying on device attendance. `ACA003` and `ACA004` are on no machine, and `ACA028` and `ACA035` (on `.202`) have no employee yet. Nothing in the database was changed by the check.

## 2. Prepare (no production change)

1. Push the branch and open a PR to `main`. Merge only after the open gates above are answered.
2. On the VM find the compose folder: `docker inspect employeeconnect-api-1 --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'`. Every `docker compose` command below runs there.
3. Check `deploy/.env` on the VM: `DEVICE_ALLOWED_CIDRS=192.168.0.0/24`, `DEVICE_BLOCKED_IPS=192.168.0.33`, `WORKER_EGRESS_IP` and `EGRESS_SUBNET` set or left at the defaults. Compose already defaults `DEVICE_BLOCKED_IPS` to `192.168.0.33`. The warning seen during the probes came from the throwaway test container, which had no such setting. Only make sure `.env` does not set it to empty.
4. The VM already has the permanent second address `192.168.0.33/24` (netplan, 2026-11-06). Confirm with `ip -4 addr show | grep 192.168.0.33`.

## 3. Backup

```
docker compose run --rm backup once
docker compose exec backup ls -la /backups | tail -3
```
Copy the newest `.dump.gpg` and its `.sha256` to the off-server location. Note the file name here before continuing.

## 4. Release

1. `git fetch && git checkout main && git pull`, so the VM has the merged code.
2. `docker compose build --pull`.
3. `docker compose up -d`. This runs `migrate` (0030, as `app_owner`), then `dbrole`, then recreates `api` and `worker`.
4. **Egress network:** the worker now has a fixed address on the `egress` network. If `up` complains about the network's configuration, run `docker compose down` (never `-v`; that would delete the database volume) and then `docker compose up -d`. Expect about a minute without service, so do it in the agreed quiet window.
5. `docker compose run --rm seed` to add the new permissions and `device.*` settings.
6. `docker compose ps` (migrate and dbrole `Exited (0)`, the rest healthy) and `docker compose logs --tail=50 api worker`. Look for `device` errors and for any `DEVICE_BLOCKED_IPS` warning.

## 5. Firewall (only the worker may reach the machines)

```
sudo cp deploy/firewall/ec-devices-firewall.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ec-devices-firewall.service
systemctl status ec-devices-firewall.service        # active (exited)
```
Adjust the paths in the unit if the repository is not at `/opt/employeeconnect`. Verify, with the worker and the api each trying port 4370 on `.202`:
```
docker compose exec worker python -c "import socket;socket.create_connection(('192.168.0.202',4370),5);print('worker ok')"
docker compose exec api    python -c "import socket;socket.create_connection(('192.168.0.202',4370),5);print('api reached (BAD)')"
```
The worker must print `worker ok`. The api must fail with a timeout or refusal. If the api connects, stop and fix the firewall before adding devices.

## 6. Console and mobile app

1. Console: `cd frontend && flutter build web --release --target lib/main_console.dart --base-href /console/ --no-web-resources-cdn --pwa-strategy none`, then copy `build/web/*` into `deploy/console/` (no restart needed).
2. Android: build the release APK as for the previous releases and distribute it. Employees get "My punches" and the privacy notice.

## 7. Add the machines (one at a time, `.202` first)

In the console (Admin, Attendance devices):
1. Name, IP, port 4370, TCP, no communication key, the "ingest from" date agreed in the gates, expected serial number.
2. **Test connection**, then save, then **Sync now**.
3. Check: the device shows the serial and clock status, the punch log fills, "Unmatched users" lists anyone whose id has no employee. Until the codes are changed (1.1), expect all of `.201` and `.203` and two of the three on `.202` to appear there. `.202` has a clock about 6 minutes slow, so expect an automatic offset to be applied and shown.
4. Map unmatched users or fix employee codes, then check that attendance appears for a day you can verify by hand.
5. Repeat for `.203`, then `.201`. `.201` has the largest log; the first sync reads about 29k records and may take a minute or two.

## 8. Watch

- `GET /admin/system/health` shows a `devices` block. Check it after the first scheduled syncs (every 5 minutes between 05:00 and 23:30, hourly outside).
- On the VM, during one full sync: `sudo tcpdump -ni any host 192.168.0.202 and port 4370` must show only the allow-listed commands (1000, 1001, 1102, 11, 1100, 201, 50, 1503, 1504, 1502).
- Compare one day of punches with eTimeTrackLite for the same people.
- Watch the next morning: the `devices.reconcile` run at 00:20 and the `devices.retention` run at 04:00 must finish without errors.

## 9. Rollback

The machines are never changed, so rolling back affects only our side.
1. In the console, disable the three devices (or `docker compose stop worker`). Syncing stops at once.
2. To go back to the previous release: set the previous `APP_VERSION` image tag and `docker compose up -d`. If the migration must be undone, stop `api` and `worker` and restore the pre-release backup (`docs/DEPLOY.md` section 3.5). Alembic downgrades are not relied on in production, although 0030's downgrade was verified on a scratch database.
3. Firewall: `sudo systemctl disable --now ec-devices-firewall.service` and remove the `EC-DEVICES` chain if the old setup must be restored.

## 10. Recommended after go-live (not blocking)

- Set a communication key on each machine and enter it in the console (the ZK protocol is unencrypted).
- Put the machines on their own VLAN.
- Decide whether to turn on `device.use_punch_state` (`.201` and `.203` record in/out; `.202` does not). Default stays off.
- Firebase push for punches stays off until the Firebase files are provided (`device.push_on_punch=false`).
