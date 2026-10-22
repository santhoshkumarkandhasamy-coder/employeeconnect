"""Background jobs. Run as ONE process:  python -m app.jobs.scheduler

Jobs (office timezone = `office.timezone` setting at start-up):
  attendance.auto_close_open_records   daily 00:30
  files.cleanup_orphans                hourly at :15
  attendance.retention                 daily 03:30   (nulls old punch coordinates, deletes old selfies; settings
                                                     attendance.location_retention_days / attendance.selfie_retention_days)
  leave.year_rollover                  Jan 1 00:10   (creates the new year's balance rows; no carry-forward in v1; per-user
                                                     commits; also run once at worker start-up as a catch-up, it is idempotent)
  announcements.retention              daily 03:50   (unlinks image/PDF of announcements deleted more than
                                                     ANNOUNCEMENT_DELETED_RETENTION_DAYS ago, 0 = off; the hourly orphan sweep then
                                                     removes the blobs)
  license.expiry_alert                 daily 08:00   (grace / expired license -> admin_alert push to license.manage holders)
  worker.heartbeat                     every 60 s    (Redis ec:worker:heartbeat, read by GET /admin/system/health)
  leave.retention                      daily 03:45   (setting leave.reason_retention_days, 0 = off: nulls reason/comment of
                                                     old decided requests, deletes their unreferenced attachment files)

Single worker only: `max_instances=1` + `coalesce` stop a job overlapping itself, and a best-effort Redis lock
(SET NX EX) stops a second accidentally started worker from running the same job twice. SIGTERM/SIGINT stop the
scheduler, wait for running jobs and dispose the DB engine.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..core.clock import utcnow
from ..core.config import get_settings
from ..core.db import dispose_engine, get_sessionmaker
from ..core.redis import close_redis, get_redis
from ..services import announcements as ann_svc
from ..services import attendance as att
from ..services import files as files_svc
from ..services import leave as leave_svc
from ..services import license as license_svc
from ..services.system_health import HEARTBEAT_KEY

log = logging.getLogger("app.jobs")
LOCK_TTL_S = 15 * 60


# ---------------------------------------------------------------- job bodies (each opens its own session)
async def job_auto_close() -> int:
    async with get_sessionmaker()() as s:
        return await att.auto_close_open_records(s)


async def job_cleanup_orphans() -> int:
    async with get_sessionmaker()() as s:
        return await files_svc.cleanup_orphans(s)


async def job_attendance_retention() -> int:
    async with get_sessionmaker()() as s:
        out = await att.purge_pii(s)
        return out["locations"] + out["selfies"]


async def job_announcement_retention() -> int:
    async with get_sessionmaker()() as s:
        return await ann_svc.purge_deleted_files(s)


async def job_leave_rollover() -> int:
    async with get_sessionmaker()() as s:
        year = utcnow().astimezone(att.office_tz(await att.load_settings(s))).year
        return await leave_svc.rollover_balances(s, year)


async def job_leave_retention() -> int:
    async with get_sessionmaker()() as s:
        out = await leave_svc.purge_old_text(s)
        return out["leave"] + out["permission"]


async def job_license_alert() -> int:
    async with get_sessionmaker()() as s:
        return await license_svc.alert_expiry(s)


async def job_heartbeat() -> None:
    """Not lock-guarded and never raises: it only proves this worker is alive."""
    try:
        redis = await get_redis()
        await redis.set(HEARTBEAT_KEY, utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), ex=600)
    except Exception:  # noqa: BLE001
        log.warning("worker heartbeat could not be written")


async def _guarded(name: str, fn: Callable[[], Awaitable[int]], lock_ttl_s: int = LOCK_TTL_S) -> None:
    try:
        redis = await get_redis()
        if not await redis.set(f"job-lock:{name}", "1", nx=True, ex=lock_ttl_s):
            log.warning("job %s skipped: another worker holds the lock", name)
            return
    except Exception:  # noqa: BLE001 - Redis trouble must not stop the jobs
        log.warning("job lock unavailable for %s; running without it", name)
    started = utcnow()
    try:
        result = await fn()
        log.info("job %s ok result=%s in %.1fs", name, result, (utcnow() - started).total_seconds())
    except Exception:  # noqa: BLE001
        log.exception("job %s failed", name)


def build_scheduler(tz: str) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=tz, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})
    jobs = [
        ("attendance.auto_close_open_records", job_auto_close, CronTrigger(hour=0, minute=30, timezone=tz)),
        ("files.cleanup_orphans", job_cleanup_orphans, CronTrigger(minute=15, timezone=tz)),
        ("attendance.retention", job_attendance_retention, CronTrigger(hour=3, minute=30, timezone=tz)),
        ("leave.year_rollover", job_leave_rollover, CronTrigger(month=1, day=1, hour=0, minute=10, timezone=tz)),
        ("leave.retention", job_leave_retention, CronTrigger(hour=3, minute=45, timezone=tz)),
        ("announcements.retention", job_announcement_retention, CronTrigger(hour=3, minute=50, timezone=tz)),
        ("license.expiry_alert", job_license_alert, CronTrigger(hour=8, minute=0, timezone=tz)),
    ]
    for name, fn, trig in jobs:
        sched.add_job(_guarded, trig, args=[name, fn], id=name, name=name, replace_existing=True)
    sched.add_job(
        job_heartbeat, IntervalTrigger(seconds=60), id="worker.heartbeat", name="worker.heartbeat", replace_existing=True
    )
    return sched


async def run() -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    async with get_sessionmaker()() as s:
        tz = str(att.office_tz(await att.load_settings(s)))
    sched = build_scheduler(tz)
    sched.start()
    await job_heartbeat()  # visible immediately, not only after the first minute
    # catch-up: a worker that was down on Jan 1 still gets the current year's rows (idempotent; lazy creation covers the rest)
    startup = asyncio.create_task(_guarded("leave.year_rollover", job_leave_rollover))
    log.info("scheduler started (tz=%s, jobs=%s)", tz, [j.id for j in sched.get_jobs()])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    await stop.wait()
    log.info("shutting down")
    startup.cancel()
    sched.shutdown(wait=True)
    await close_redis()
    await dispose_engine()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
