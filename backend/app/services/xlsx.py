"""Reusable .xlsx writer for the export module (plan 10) and the food report.

Design (review 04, M1/M2):
* Row counts are taken with `SELECT count(*)` BEFORE anything is written; a sheet over its cap is refused up front.
* The workbook is built in a worker thread (rows are fetched asynchronously in batches, written by the thread), guarded by
  a process-wide semaphore (EXPORT_MAX_CONCURRENCY, default 2) and a hard time budget (EXPORT_TIMEOUT_S, default 60).
* openpyxl's write-only sheets stream into temp files. Those files are redirected into a private per-build directory
  (under EXPORT_TEMP_DIR) that is removed in `finally` on every path (success, error, timeout, cancellation); stale
  directories left by a killed process are swept at the start of each build.
* Every text cell is sanitised against spreadsheet formula injection and XML-illegal characters, and truncated.
* Headers: bold white on dark fill, frozen first row. Column widths come from the header plus the first WIDTH_SAMPLE rows
  (write-only sheets need widths before the first row is written), capped at MAX_WIDTH.
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet import _writer as _opx_writer

from ..core.config import get_settings
from ..core.errors import AppError

log = logging.getLogger("app.xlsx")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
WIDTH_SAMPLE = 500
MAX_WIDTH = 60
MIN_WIDTH = 10
SPOOL_BYTES = 8 * 1024 * 1024
BATCH = 1000
MAX_CELL_CHARS = 32_000  # Excel's hard limit is 32,767
SLOT_WAIT_S = 5  # how long a request waits for one of the build slots before answering 503
STALE_AFTER_S = 3600
DIR_PREFIX = "ec-export-"
# ASCII triggers plus the full-width forms (U+FF1D, U+FF0B, U+FF0D, U+FF20) that some spreadsheet locales normalise.
_FORMULA_START = ("=", "+", "-", "@", "＝", "＋", "－", "＠")
# C0 controls except TAB/LF/CR, lone surrogates, and the non-characters U+FFFE / U+FFFF (all illegal in XML 1.0)
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def sanitize_cell(value: Any) -> Any:
    """Text: strip XML-illegal characters, truncate to MAX_CELL_CHARS, and prefix an apostrophe when a spreadsheet could
    read it as a formula (the first NON-WHITESPACE character is a trigger; leading spaces do not hide it, and plain text that
    merely starts with a tab / newline is left alone). Cells are written as strings regardless. Non-strings pass through."""
    if isinstance(value, str):
        value = _ILLEGAL.sub("", value)
        if len(value) > MAX_CELL_CHARS:
            value = value[:MAX_CELL_CHARS]
        if value.lstrip().startswith(_FORMULA_START):
            return "'" + value
    return value


def _display_len(v: Any) -> int:
    if isinstance(v, dt.datetime):
        return 19
    if isinstance(v, dt.date):
        return 10
    return len(str(v)) if v is not None else 0


@dataclass
class SheetSpec:
    """One worksheet. `count` runs the same filtered query as `rows` with SELECT count(*)."""

    title: str
    headers: list[str]
    count: Callable[[], Awaitable[int]]
    rows: Callable[[], AsyncIterator[tuple]]
    max_rows: int


@dataclass
class BuiltWorkbook:
    file: IO[bytes]
    counts: dict[str, int]
    _dir: Path
    _closed: bool = field(default=False, repr=False)
    _token: object | None = field(default=None, repr=False)

    def close(self) -> None:
        """Idempotent: closes the file, removes the private build directory and frees the download slot."""
        if self._closed:
            return
        self._closed = True
        try:
            self.file.close()
        finally:
            shutil.rmtree(self._dir, ignore_errors=True)
            _unregister(self._dir)
            _release(self._token)


# ---------------------------------------------------------------- finished-but-undrained workbooks (review 08 L4)
_pending: dict[object, float] = {}
_pending_lock = threading.Lock()
PENDING_MAX_AGE_S = 900  # a slot not released after this long (client vanished, background task never ran) is reclaimed


def _reserve(limit: int) -> object:
    """Take one slot for a workbook that is being built or waits to be downloaded; 503 when `limit` slots are in use."""
    now = time.monotonic()
    with _pending_lock:
        for tok, t0 in list(_pending.items()):
            if now - t0 > PENDING_MAX_AGE_S:
                del _pending[tok]
        if len(_pending) >= limit:
            raise AppError(
                "TEMPORARILY_UNAVAILABLE", "Too many exports are waiting to be downloaded; try again shortly.",
                headers={"Retry-After": "15"},
            )  # fmt: skip
        tok = object()
        _pending[tok] = now
        return tok


def _release(token: object | None) -> None:
    if token is not None:
        with _pending_lock:
            _pending.pop(token, None)


def pending_downloads() -> int:
    with _pending_lock:
        return len(_pending)


# ---------------------------------------------------------------- temp-file redirection
_BUILD_DIR: contextvars.ContextVar[Path | None] = contextvars.ContextVar("xlsx_build_dir", default=None)
_orig_create_temporary_file = _opx_writer.create_temporary_file


def _create_temporary_file(suffix: str = "") -> str:
    """openpyxl hook: sheet temp files go to the private build directory of the calling build (context variable, which
    `asyncio.to_thread` propagates); anywhere else the original behaviour is kept."""
    d = _BUILD_DIR.get()
    if d is None:
        return _orig_create_temporary_file(suffix)
    fd, name = tempfile.mkstemp(suffix=suffix, prefix="openpyxl.", dir=d)
    os.close(fd)
    _opx_writer.ALL_TEMP_FILES.append(name)  # openpyxl's cleanup() removes the entry again after a successful save
    return name


_opx_writer.create_temporary_file = _create_temporary_file


_active_dirs: set[str] = set()  # build directories of THIS process that are still being written or streamed
_active_lock = threading.Lock()


def _register(d: Path) -> None:
    with _active_lock:
        _active_dirs.add(str(d))


def _unregister(d: Path) -> None:
    with _active_lock:
        _active_dirs.discard(str(d))


def _pid_alive(pid: int) -> bool:
    """False only when we are SURE the process is gone (so a live worker's directory is never removed)."""
    if os.name == "nt":  # os.kill(pid, 0) is not a probe on Windows
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: exists, owned by someone else
        return True
    return True


def _dir_pid(name: str) -> int | None:
    m = re.fullmatch(re.escape(DIR_PREFIX) + r"(\d+)-.+", name)
    return int(m.group(1)) if m else None


def sweep_stale(base: str | None, older_than_s: int = STALE_AFTER_S) -> int:
    """Remove build directories left behind by a killed process. Returns the number removed.

    Only directories that are ours to delete: `ec-export-*` real directories (no symlinks) owned by the current user. A
    directory named after a PID is removed when that process is dead, or when it is this process's own and no longer in use
    (not in the active set) and older than `older_than_s`. A directory of ANOTHER live worker is never touched (it may be
    streaming). Directories without a PID (older naming) are removed once older than `older_than_s`."""
    root = Path(base or tempfile.gettempdir())
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    cutoff = time.time() - older_than_s
    me, uid = os.getpid(), (os.getuid() if hasattr(os, "getuid") else None)
    for p in entries:
        try:
            if not p.name.startswith(DIR_PREFIX) or p.is_symlink() or not p.is_dir():
                continue
            st = p.stat()
            if uid is not None and st.st_uid != uid:
                continue
            pid = _dir_pid(p.name)
            old = st.st_mtime < cutoff
            if pid is None:
                ok = old
            elif pid == me:
                with _active_lock:
                    ok = old and str(p) not in _active_dirs
            else:
                ok = not _pid_alive(pid) or st.st_mtime < time.time() - 24 * older_than_s  # 2nd: recycled PID
            if ok:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def _prepare_base(base: str | None) -> str | None:
    """The dedicated export directory: created if missing and restricted to the service user (0700)."""
    if not base:
        return None
    p = Path(base)
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            if p.stat().st_uid == os.getuid():
                p.chmod(0o700)
        except OSError:
            log.warning("could not restrict %s to mode 0700", base)
    return base


# ---------------------------------------------------------------- the thread-side builder
class _Cancelled(Exception):
    pass


class _Builder:
    """All methods run in worker threads, one at a time (the lock also serialises `abort` against a zombie operation
    after a timeout). openpyxl objects are never touched from the event loop."""

    def __init__(self, workdir: Path):
        self.dir = workdir
        self.wb = Workbook(write_only=True)
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.sheets: dict[str, Any] = {}
        self.out: IO[bytes] | None = None

    def start_sheet(self, title: str, headers: list[str], sample: list[tuple]) -> None:
        with self.lock:
            self._check()
            _BUILD_DIR.set(self.dir)
            ws = self.wb.create_sheet(title=title[:31])
            self.sheets[title] = ws
            widths = [len(h) for h in headers]
            for row in sample:
                for i, v in enumerate(row):
                    widths[i] = max(widths[i], _display_len(v))
            for i, w in enumerate(widths):
                ws.column_dimensions[get_column_letter(i + 1)].width = max(MIN_WIDTH, min(MAX_WIDTH, w + 2))
            ws.freeze_panes = "A2"
            font, fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E78")
            head = []
            for h in headers:
                c = WriteOnlyCell(ws, value=h)
                c.font, c.fill, c.alignment = font, fill, Alignment(vertical="center")
                head.append(c)
            ws.append(head)

    def append(self, title: str, rows: list[tuple]) -> None:
        with self.lock:
            self._check()
            _BUILD_DIR.set(self.dir)
            ws = self.sheets[title]
            for row in rows:
                cells = []
                for v in row:
                    sv = sanitize_cell(v)
                    c = WriteOnlyCell(ws, value=sv)
                    if isinstance(sv, str):
                        c.data_type = "s"  # never a formula or an error code, whatever the text looks like
                    elif isinstance(v, dt.datetime):
                        c.number_format = "yyyy-mm-dd hh:mm"
                    elif isinstance(v, dt.date):
                        c.number_format = "yyyy-mm-dd"
                    cells.append(c)
                ws.append(cells)

    def finish(self) -> IO[bytes]:
        with self.lock:
            self._check()
            _BUILD_DIR.set(self.dir)
            out = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES, dir=self.dir)  # noqa: SIM115 - closed by BuiltWorkbook
            self.out = out
            self.wb.save(out)
            out.seek(0)
            return out

    def abort(self) -> None:
        """Stop and delete everything this build created. Safe to call repeatedly and after success."""
        self.cancelled.set()
        with self.lock:
            for ws in self.sheets.values():
                try:
                    rows = getattr(ws, "_rows", None)
                    if rows is not None:
                        rows.close()  # finishes <sheetData> and the xml stream, which releases the temp file handle
                    w = getattr(ws, "_writer", None)
                    if w is not None:
                        w.xf.close()
                except Exception:  # noqa: BLE001 - best effort; the directory is removed below regardless
                    pass
            if self.out is not None:
                try:
                    self.out.close()
                except Exception:  # noqa: BLE001
                    pass
            prefix = str(self.dir)
            _opx_writer.ALL_TEMP_FILES[:] = [p for p in _opx_writer.ALL_TEMP_FILES if not str(p).startswith(prefix)]
            shutil.rmtree(self.dir, ignore_errors=True)

    def _check(self) -> None:
        if self.cancelled.is_set():
            raise _Cancelled


# ---------------------------------------------------------------- orchestration
_sem: tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore] | None = None


def _semaphore(limit: int) -> asyncio.Semaphore:
    """One semaphore per (event loop, limit): asyncio primitives must not be shared across loops (tests create many)."""
    global _sem
    loop = asyncio.get_running_loop()
    if _sem is None or _sem[0] is not loop or _sem[1] != limit:
        _sem = (loop, limit, asyncio.Semaphore(limit))
    return _sem[2]


async def _fill(builder: _Builder, spec: SheetSpec) -> int:
    count = 0
    async with aclosing(spec.rows()) as rows:
        it = rows.__aiter__()
        sample: list[tuple] = []
        exhausted = False
        while len(sample) < WIDTH_SAMPLE:
            try:
                sample.append(await it.__anext__())
            except StopAsyncIteration:
                exhausted = True
                break
        await asyncio.to_thread(builder.start_sheet, spec.title, spec.headers, sample)
        batch = sample
        count = len(sample)
        if count > spec.max_rows:
            raise OverflowError(spec.title)
        while True:
            if len(batch) >= BATCH or (exhausted and batch):
                await asyncio.to_thread(builder.append, spec.title, batch)
                batch = []
            if exhausted:
                break
            try:
                row = await it.__anext__()
            except StopAsyncIteration:
                exhausted = True
                continue
            count += 1
            if count > spec.max_rows:
                raise OverflowError(spec.title)
            batch.append(row)
    return count


async def build_workbook(sheets: list[SheetSpec]) -> BuiltWorkbook:
    """Returns a rewound spooled .xlsx plus per-sheet row counts. The caller MUST close() the result (iter_file does).
    Raises OverflowError (-> 422 RANGE_TOO_LARGE) when a sheet is over its cap or the time budget is exhausted, and
    AppError 503 when all build slots stay busy."""
    cfg = get_settings()
    # 0. cap on workbooks that are built or finished-but-not-yet-downloaded (disk bound); released by BuiltWorkbook.close()
    token = _reserve(cfg.export_max_pending_downloads)
    workdir: Path | None = None
    try:
        # 1. bounded concurrency FIRST, so queued requests cannot pile up count queries against the database
        sem = _semaphore(cfg.export_max_concurrency)
        try:
            await asyncio.wait_for(sem.acquire(), SLOT_WAIT_S)
        except TimeoutError:
            raise AppError(
                "TEMPORARILY_UNAVAILABLE", "Export capacity is busy; try again shortly.", headers={"Retry-After": "10"}
            ) from None
        try:
            builder: _Builder | None = None
            counts: dict[str, int] = {}
            try:
                async with asyncio.timeout(cfg.export_timeout_s) as budget:  # counts AND writing share one budget
                    # 2. cheap pre-flight: refuse before a single byte is written
                    for spec in sheets:
                        if await spec.count() > spec.max_rows:
                            raise OverflowError(spec.title)
                    base = await asyncio.to_thread(_prepare_base, cfg.export_temp_dir)
                    await asyncio.to_thread(sweep_stale, base)
                    workdir = Path(tempfile.mkdtemp(prefix=f"{DIR_PREFIX}{os.getpid()}-", dir=base))
                    _register(workdir)
                    builder = _Builder(workdir)
                    for spec in sheets:
                        counts[spec.title] = await _fill(builder, spec)
                    out = await asyncio.to_thread(builder.finish)
            except BaseException as exc:
                if builder is not None:
                    await asyncio.shield(asyncio.to_thread(builder.abort))  # runs even when we were cancelled
                elif workdir is not None:
                    shutil.rmtree(workdir, ignore_errors=True)
                if workdir is not None:
                    _unregister(workdir)
                if isinstance(exc, TimeoutError) and budget.expired():
                    log.warning("export exceeded %ss and was aborted", cfg.export_timeout_s)
                    raise OverflowError("timeout") from None
                raise
            return BuiltWorkbook(file=out, counts=counts, _dir=workdir, _token=token)
        finally:
            sem.release()
    except BaseException:
        _release(token)
        raise


def iter_file(built: BuiltWorkbook, chunk: int = 64 * 1024) -> Iterator[bytes]:
    try:
        while data := built.file.read(chunk):
            yield data
    finally:
        built.close()
