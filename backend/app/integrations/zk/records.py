"""Parsers for the user table and the attendance log. There is NO template / fingerprint / face code path here.

The device PIN (password) field of a user record is skipped with pad bytes: it is never decoded, stored or logged.
"""

from __future__ import annotations

import datetime as dt
import re
import struct
from dataclasses import dataclass

from .errors import DeviceProtocolError
from .protocol import MAX_RECORDS, decode_time

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩​-‏﻿]")
USER_FORMATS = {
    # uid u16, privilege u8, PIN (skipped), name, card u32, pad, group, timezone, numeric user id
    28: struct.Struct("<HB5x8sIxBhI"),
    # uid u16, privilege u8, PIN (skipped), name, card u32, pad, group (skipped), pad, user id text
    72: struct.Struct("<HB8x24sIx7sx24s"),
}
ATT_FORMATS = {
    8: struct.Struct("<HBB4s"),  # uid, verify, state, time
    16: struct.Struct("<I4sBB2xI"),  # numeric user id, time, verify, state, (reserved), workcode
    40: struct.Struct("<H24sBB4s8x"),  # uid, user id text, verify, state, time
}


@dataclass(frozen=True)
class ZKUser:
    uid: int
    user_id: str
    name: str
    card: str | None
    privilege: int

    @property
    def is_admin(self) -> bool:
        return (self.privilege & 0x0E) != 0


@dataclass(frozen=True)
class ZKPunch:
    uid: int | None  # internal slot; the only identifier of the 8-byte format
    user_id: str | None  # None in the 8-byte format until resolved through the user list
    time: dt.datetime  # naive device clock
    state: int
    verify: int


@dataclass(frozen=True)
class ZKSizes:
    users: int
    records: int


def clean(raw: bytes, limit: int) -> str:
    """Untrusted device text: cut at NUL, decode, drop control / bidi characters, bounded."""
    text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
    return _CTRL.sub("", text).strip()[:limit]


def parse_sizes(data: bytes) -> ZKSizes:
    if len(data) < 48:
        raise DeviceProtocolError("short free-sizes reply")
    fields = struct.unpack_from("<12i", data)
    users, records = fields[4], fields[8]
    if not (0 <= users <= MAX_RECORDS and 0 <= records <= MAX_RECORDS):
        raise DeviceProtocolError("implausible record counts")
    return ZKSizes(users=users, records=records)


def _pick_size(total: int, count: int, sizes) -> int:
    """Record size from the buffer length: the candidate that divides it and best matches the announced record count."""
    fits = [s for s in sizes if total % s == 0]
    if not fits:
        raise DeviceProtocolError("record size not recognised")
    return min(fits, key=lambda s: abs(total // s - count))


def _split(buf: bytes, count: int, sizes) -> tuple[int, bytes]:
    if len(buf) < 4:
        return 0, b""
    (total,) = struct.unpack_from("<I", buf)
    body = buf[4:]
    if total != len(body):
        raise DeviceProtocolError("buffer length mismatch")
    if total == 0:
        return 0, b""
    size = _pick_size(total, count, sizes)
    if total // size > MAX_RECORDS:
        raise DeviceProtocolError("too many records")
    return size, body


def parse_users(buf: bytes, count: int) -> list[ZKUser]:
    size, body = _split(buf, count, USER_FORMATS)
    out: list[ZKUser] = []
    for off in range(0, len(body), size or 1):
        chunk = body[off : off + size]
        if size == 28:
            uid, priv, name, card, _grp, _tz, numeric_id = USER_FORMATS[28].unpack(chunk)
            user_id = str(numeric_id)
        else:
            uid, priv, name, card, _grp, uid_text = USER_FORMATS[72].unpack(chunk)
            user_id = clean(uid_text, 24)
        if not user_id:
            continue
        out.append(ZKUser(uid=uid, user_id=user_id, name=clean(name, 60), card=str(card) if card else None, privilege=priv))
    return out


def parse_attendance(buf: bytes, count: int) -> tuple[list[ZKPunch], int]:
    """-> (punches, skipped). A record with an impossible timestamp is skipped (counted), never guessed."""
    size, body = _split(buf, count, ATT_FORMATS)
    out: list[ZKPunch] = []
    skipped = 0
    for off in range(0, len(body), size or 1):
        chunk = body[off : off + size]
        uid: int | None
        user_id: str | None
        if size == 8:
            uid, verify, state, t = ATT_FORMATS[8].unpack(chunk)
            user_id = None
        elif size == 16:
            num, t, verify, state, _work = ATT_FORMATS[16].unpack(chunk)
            uid, user_id = None, str(num)
        else:
            uid, uid_text, verify, state, t, *_ = (*ATT_FORMATS[40].unpack(chunk),)
            user_id = clean(uid_text, 24) or None
        when = decode_time(struct.unpack("<I", t)[0] if isinstance(t, bytes) else t)
        if when is None:
            skipped += 1
            continue
        out.append(ZKPunch(uid=uid, user_id=user_id, time=when, state=state, verify=verify))
    return out, skipped
