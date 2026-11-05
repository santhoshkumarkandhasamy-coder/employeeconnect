"""Bank payment file `generic_neft_csv` (plan 17 B5, contract 19.5). Pure functions: the file is rendered from STORED, approved
lines only, never from a recomputation, so every re-download is byte-identical.

Every field is bank-safe by construction (ASCII, no comma, quote, CR/LF or leading = + - @), which makes CSV / formula injection
impossible without any quoting; a field that is not safe raises ValueError and nothing is written.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass

HEADER = "Sr No,Payment Type,Debit Account No,Beneficiary Name,Beneficiary Account No,IFSC,Amount,Value Date,Narration,Reference"
ACCOUNT_RE = re.compile(r"^\d{9,18}$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
AMOUNT_RE = re.compile(r"^\d{1,8}\.\d{2}$")
_NOT_NAME = re.compile(r"[^A-Z0-9 .]")
_FIELD = re.compile(r"^[A-Z0-9][A-Z0-9 ./\-]*$")  # no comma, quote, CR/LF; cannot start with = + - @
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
NAME_MAX = 35


def bank_safe_name(raw: str | None) -> str:
    """Upper-cased, accents folded to ASCII, only A-Z 0-9 . and space, spaces collapsed, at most 35 characters. Empty = unusable."""
    folded = unicodedata.normalize("NFKD", raw or "").encode("ascii", "ignore").decode("ascii").upper()
    cleaned = re.sub(r" {2,}", " ", _NOT_NAME.sub("", folded)).strip()
    cleaned = cleaned.lstrip(". ")[:NAME_MAX].strip()
    return cleaned if re.search(r"[A-Z0-9]", cleaned) else ""


@dataclass(frozen=True)
class FileLine:
    seq: int
    name: str  # the stored beneficiary name (cleaned here)
    account: str
    ifsc: str
    amount: str
    delta: bool


def narration(period: dt.date, delta: bool) -> str:
    return f"SALARY {'REV ' if delta else ''}{MONTHS[period.month - 1]} {period.year}"


def _safe(value: str, what: str) -> str:
    if not _FIELD.match(value):
        raise ValueError(f"{what} is not bank-safe")
    return value


def render(
    *, reference: str, payment_type: str, debit_account: str, value_date: dt.date, period: dt.date, lines: list[FileLine]
) -> bytes:
    if not ACCOUNT_RE.match(debit_account):
        raise ValueError("debit account is not valid")
    rows = [HEADER]
    for ln in sorted(lines, key=lambda x: x.seq):
        name = bank_safe_name(ln.name)
        if not name:
            raise ValueError("beneficiary name is empty")
        if not ACCOUNT_RE.match(ln.account) or not IFSC_RE.match(ln.ifsc) or not AMOUNT_RE.match(ln.amount):
            raise ValueError("line fields are not valid")
        fields = [
            str(ln.seq), _safe(payment_type, "payment type"), debit_account, _safe(name, "name"), ln.account, ln.ifsc, ln.amount,
            value_date.strftime("%d/%m/%Y"), _safe(narration(period, ln.delta), "narration"),
            _safe(f"{reference}-{ln.seq:04d}", "reference"),
        ]  # fmt: skip
        rows.append(",".join(fields))
    return ("\r\n".join(rows) + "\r\n").encode("ascii")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
