"""Safe reader for the one-time item bulk upload (plan 15 s.3, contract 17.2).

Defence in depth, in this order, all BEFORE any value is trusted:
  1. size (<= 1 MB) and magic bytes (`PK\\x03\\x04`)
  2. the zip directory only (no decompression): <= 50 entries, <= 10 MB declared uncompressed, per-entry and total ratio
     <= 100, no path tricks, no `xl/vbaProject.bin` / macro sheets / ActiveX / embeddings / external links
  3. openpyxl (`read_only=True`) in a worker thread with a 10 s timeout; the thread keeps its build slot until it really ends
  4. sheet 1 only, the header must match the template, <= 500 data rows, blank rows ignored
  5. a cell that is a formula, or text starting with `= + - @` (also full-width forms), is flagged and the row is refused

The parser returns raw cells; business validation (employees, codes, amounts) lives in `payroll_items`. The file is never
written to disk. Row numbers are the spreadsheet's own (the header is row 1).

`data_only=False` is deliberate (plan said True): with True, openpyxl returns the cached value of a formula cell and the
formula would be invisible, so it could never be refused.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..core.config import get_settings
from ..core.errors import AppError
from . import xlsx

log = logging.getLogger("app.xlsx_import")

MAX_BYTES = 1024 * 1024
MAX_ENTRIES = 50
MAX_UNCOMPRESSED = 10 * 1024 * 1024
MAX_RATIO = 100
MAX_ROWS = 500
MAX_SCAN_ROWS = 2000  # physical rows read (blank rows included) before the sheet is called too long
PARSE_TIMEOUT_S = 10
HEADER = ("Employee ID", "Code", "Label", "Amount")
FIELDS = ("employee_id", "code", "label", "amount")
FORMULA_START = ("=", "+", "-", "@", "＝", "＋", "－", "＠")
_FORBIDDEN_PARTS = (
    "xl/vbaproject.bin", "xl/vbadata.xml", "xl/macrosheets/", "xl/dialogsheets/", "xl/activex/", "xl/embeddings/",
    "xl/externallinks/", "customui/",
)  # fmt: skip
_REQUIRED = ("[Content_Types].xml", "xl/workbook.xml")


@dataclass
class ParsedRow:
    row: int  # spreadsheet row number
    values: dict[str, Any] = field(default_factory=dict)  # FIELDS -> str | Decimal | None (as read)
    formula: set[str] = field(default_factory=set)  # fields whose cell is a formula or text that looks like one


def _not_xlsx(msg: str = "The file is not a valid .xlsx workbook") -> AppError:
    return AppError("FILE_TYPE_NOT_ALLOWED", msg)


def check_container(data: bytes) -> None:
    """Size, magic bytes and zip-directory checks. Raises AppError; never decompresses anything."""
    if len(data) > MAX_BYTES:
        raise AppError("FILE_TOO_LARGE", "File exceeds the 1 MB limit")
    if data[:4] != b"PK\x03\x04":
        raise _not_xlsx()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError, NotImplementedError):
        raise _not_xlsx() from None
    if len(infos) > MAX_ENTRIES:
        raise _not_xlsx("The workbook has too many parts")
    total = 0
    names = []
    for i in infos:
        name = i.filename.replace("\\", "/")
        low = name.lower()
        if name.startswith("/") or ".." in name.split("/") or ":" in name.split("/")[0]:
            raise _not_xlsx()
        if any(low.startswith(p) or low == p.rstrip("/") for p in _FORBIDDEN_PARTS):
            raise _not_xlsx("Macros, embedded objects and external links are not allowed")
        if i.flag_bits & 0x1:  # encrypted entry
            raise _not_xlsx()
        total += i.file_size
        if total > MAX_UNCOMPRESSED:
            raise _not_xlsx("The workbook is too large when unpacked")
        if i.file_size and (i.compress_size == 0 or i.file_size / i.compress_size > MAX_RATIO):
            raise _not_xlsx("The workbook is not accepted (compression ratio)")
        names.append(name)
    if len(data) and total / len(data) > MAX_RATIO:
        raise _not_xlsx("The workbook is not accepted (compression ratio)")
    if any(r not in names for r in _REQUIRED):
        raise _not_xlsx()


async def read_upload(upload: Any) -> bytes:
    """Read an UploadFile up to MAX_BYTES (FILE_TOO_LARGE beyond); the bytes stay in memory, nothing touches the disk."""
    buf = bytearray()
    while chunk := await upload.read(64 * 1024):
        buf += chunk
        if len(buf) > MAX_BYTES:
            raise AppError("FILE_TOO_LARGE", "File exceeds the 1 MB limit")
    return bytes(buf)


def _looks_like_formula(text: str) -> bool:
    return text.lstrip().startswith(FORMULA_START)


def _cell(c: Any) -> tuple[Any, bool]:
    """(value, is_formula). Empty strings and None are 'no value'."""
    v = getattr(c, "value", c)
    if getattr(c, "data_type", None) == "f":
        return v, True
    if isinstance(v, str):
        v = v.strip()
        if v == "":
            return None, False
        return v, _looks_like_formula(v)
    return v, False


def _parse_sync(data: bytes) -> list[ParsedRow]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    except Exception:  # noqa: BLE001 - any parser failure means "not a usable workbook"
        raise _not_xlsx() from None
    try:
        if not wb.worksheets:
            raise _not_xlsx()
        ws = wb.worksheets[0]  # sheet 1 only
        out: list[ParsedRow] = []
        header_seen = False
        scanned = 0
        for n, cells in enumerate(ws.iter_rows(max_col=6), start=1):
            scanned += 1
            if scanned > MAX_SCAN_ROWS:
                raise _too_many_rows()
            vals = [_cell(c) for c in cells]
            vals += [(None, False)] * (6 - len(vals))
            if not header_seen:
                header_seen = True
                got = tuple(str(v).strip().lower() if v is not None else "" for v, _ in vals[:4])
                if got != tuple(h.lower() for h in HEADER) or any(v is not None for v, _ in vals[4:]):
                    raise AppError(
                        "IMPORT_INVALID", "The header row does not match the template",
                        details=[{"field": "file", "message": "header must be: " + ", ".join(HEADER)}],
                    )  # fmt: skip
                continue
            main = vals[:4]
            if all(v is None for v, _ in main):
                continue  # blank row
            if len(out) >= MAX_ROWS:
                raise _too_many_rows()
            pr = ParsedRow(row=n)
            for name, (v, is_formula) in zip(FIELDS, main, strict=True):
                pr.values[name] = v
                if is_formula:
                    pr.formula.add(name)
            out.append(pr)
        if not header_seen:
            raise AppError(
                "IMPORT_INVALID", "The sheet is empty", details=[{"field": "file", "message": "header row missing"}]
            )  # fmt: skip
        return out
    finally:
        wb.close()


def _too_many_rows() -> AppError:
    return AppError(
        "IMPORT_INVALID", f"The sheet has more than {MAX_ROWS} data rows",
        details=[{"field": "file", "message": f"at most {MAX_ROWS} rows"}],
    )  # fmt: skip


async def parse(data: bytes) -> list[ParsedRow]:
    """Container checks, then the openpyxl read in a worker thread (10 s) under the shared workbook semaphore. A thread that
    outlives the timeout keeps its semaphore slot until it ends, so stuck parses cannot pile up."""
    check_container(data)
    sem = xlsx._semaphore(get_settings().export_max_concurrency)
    try:
        await asyncio.wait_for(sem.acquire(), xlsx.SLOT_WAIT_S)
    except TimeoutError:
        raise AppError(
            "TEMPORARILY_UNAVAILABLE", "The importer is busy; try again shortly.", headers={"Retry-After": "10"}
        ) from None
    task = asyncio.ensure_future(asyncio.to_thread(_parse_sync, data))
    task.add_done_callback(lambda _t: sem.release())
    try:
        return await asyncio.wait_for(asyncio.shield(task), PARSE_TIMEOUT_S)
    except TimeoutError:
        log.warning("xlsx import parse exceeded %ss", PARSE_TIMEOUT_S)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)  # retrieve, never log the content
        raise AppError(
            "TEMPORARILY_UNAVAILABLE", "The file took too long to read; try a smaller file.", headers={"Retry-After": "30"}
        ) from None


_MONEY_TEXT = re.compile(r"^\d{1,8}(\.\d{1,2})?$")


def amount_of(value: Any) -> Decimal | None:
    """The cell as a Decimal amount (<= 2 decimals) or None when it is not a plain number. Booleans are not numbers."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            d = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        except Exception:  # noqa: BLE001
            return None
    elif isinstance(value, str) and _MONEY_TEXT.match(value):
        d = Decimal(value)
    else:
        return None
    if not d.is_finite() or d != d.quantize(Decimal("0.01")):
        return None
    return d.quantize(Decimal("0.01"))
