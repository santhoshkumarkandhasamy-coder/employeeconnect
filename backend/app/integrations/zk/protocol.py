"""Wire format, checksum, comm-key scramble and the command ALLOW-LIST (clean-room, from the public protocol description).

Packet = command u16, checksum u16, session id u16, reply id u16, data. TCP prefixes `50 50 82 7D` + u32 length of the packet.
`check_command` is the single gate every outgoing packet passes (see client.ZKClient._send_packet): anything that is not an
allowed read command raises ForbiddenCommand before a byte is written.
"""

from __future__ import annotations

import struct

from .errors import DeviceProtocolError, ForbiddenCommand

TCP_MAGIC = b"\x50\x50\x82\x7d"
HEADER = struct.Struct("<HHHH")
MAX_PACKET = 64 * 1024  # one packet (header + data)
MAX_BUFFER = 16 * 1024 * 1024  # a whole user / attendance buffer
MAX_RECORDS = 200_000

# ---- commands we send (the ONLY ones)
CMD_CONNECT = 1000
CMD_EXIT = 1001
CMD_AUTH = 1102
CMD_OPTIONS_RRQ = 11
CMD_GET_VERSION = 1100
CMD_GET_TIME = 201
CMD_GET_FREE_SIZES = 50
CMD_DATA_WRRQ = 1503  # "prepare buffer": announces which table we want to read (inner command below)
CMD_DATA_RDY = 1504  # read a chunk of the prepared buffer
CMD_FREE_DATA = 1502

# inner commands of CMD_DATA_WRRQ
CMD_USERTEMP_RRQ = 9  # the user table (NOT templates; templates are DB_RRQ 7 / FCT_FINGERTMP, which we never use)
CMD_ATTLOG_RRQ = 13
FCT_USER = 5
FCT_ATTLOG = 0

# ---- replies
CMD_ACK_OK = 2000
CMD_ACK_ERROR = 2001
CMD_ACK_DATA = 2002
CMD_ACK_UNAUTH = 2005
CMD_PREPARE_DATA = 1500
CMD_DATA = 1501

OPTION_NAMES = frozenset({"~SerialNumber", "~DeviceName", "~Platform", "~ZKFPVersion", "~OS"})
ALLOWED_COMMANDS = frozenset(
    {
        CMD_CONNECT,
        CMD_EXIT,
        CMD_AUTH,
        CMD_OPTIONS_RRQ,
        CMD_GET_VERSION,
        CMD_GET_TIME,
        CMD_GET_FREE_SIZES,
        CMD_DATA_WRRQ,
        CMD_DATA_RDY,
        CMD_FREE_DATA,
    }
)
_ALLOWED_BUFFERS = frozenset({(CMD_USERTEMP_RRQ, FCT_USER), (CMD_ATTLOG_RRQ, FCT_ATTLOG)})
_NO_DATA = frozenset({CMD_CONNECT, CMD_EXIT, CMD_GET_VERSION, CMD_GET_TIME, CMD_GET_FREE_SIZES, CMD_FREE_DATA})
_PREPARE = struct.Struct("<bhii")
_CHUNK = struct.Struct("<ii")


def check_command(command: int, data: bytes) -> None:
    """Raise ForbiddenCommand unless (command, data) is an allowed read-only request. Pure; called before every write."""
    if command not in ALLOWED_COMMANDS:
        raise ForbiddenCommand(f"command {command} is not on the read-only allow-list")
    if command in _NO_DATA:
        if data:
            raise ForbiddenCommand(f"command {command} takes no data")
    elif command == CMD_AUTH:
        if len(data) != 4:
            raise ForbiddenCommand("auth payload must be 4 bytes")
    elif command == CMD_OPTIONS_RRQ:
        name = data[:-1].decode("ascii", "replace") if data.endswith(b"\x00") else None
        if name not in OPTION_NAMES:
            raise ForbiddenCommand("option name is not on the read-only list")  # also blocks "name=value" writes
    elif command == CMD_DATA_WRRQ:
        if len(data) != _PREPARE.size:
            raise ForbiddenCommand("prepare-buffer payload has the wrong size")
        flag, inner, fct, ext = _PREPARE.unpack(data)
        if flag != 1 or ext != 0 or (inner, fct) not in _ALLOWED_BUFFERS:
            raise ForbiddenCommand(f"buffer read {inner}/{fct} is not allowed")
    elif command == CMD_DATA_RDY:
        if len(data) != _CHUNK.size:
            raise ForbiddenCommand("read-chunk payload has the wrong size")
        start, size = _CHUNK.unpack(data)
        if start < 0 or size <= 0 or size > MAX_PACKET or start > MAX_BUFFER:
            raise ForbiddenCommand("read-chunk range is out of bounds")


def prepare_buffer_payload(inner: int, fct: int) -> bytes:
    return _PREPARE.pack(1, inner, fct, 0)


def chunk_payload(start: int, size: int) -> bytes:
    return _CHUNK.pack(start, size)


def checksum(packet_without_checksum: bytes) -> int:
    """16-bit one's-complement checksum over little-endian words (checksum field zeroed)."""
    b = packet_without_checksum
    total = 0
    for i in range(0, len(b) - 1, 2):
        total += b[i] | (b[i + 1] << 8)
    if len(b) % 2:
        total += b[-1]
    while total > 0xFFFF:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _checksum_variant(packet_without_checksum: bytes) -> int:
    """Some firmware computes 0xFFFE - (sum mod 0xFFFF); both forms are accepted on receive."""
    b = packet_without_checksum
    total = 0
    for i in range(0, len(b) - 1, 2):
        total += b[i] | (b[i + 1] << 8)
    if len(b) % 2:
        total += b[-1]
    return (0xFFFE - total % 0xFFFF) & 0xFFFF


def build_packet(command: int, session_id: int, reply_id: int, data: bytes = b"") -> bytes:
    body = HEADER.pack(command, 0, session_id, reply_id) + data
    return HEADER.pack(command, checksum(body), session_id, reply_id) + data


def frame_tcp(packet: bytes) -> bytes:
    return TCP_MAGIC + struct.pack("<I", len(packet)) + packet


def parse_packet(packet: bytes, *, verify: bool = True) -> tuple[int, int, int, bytes]:
    """-> (command, session_id, reply_id, data). Raises DeviceProtocolError on a short packet or a wrong checksum."""
    if len(packet) < HEADER.size:
        raise DeviceProtocolError("short packet")
    if len(packet) > MAX_PACKET + HEADER.size:
        raise DeviceProtocolError("packet too large")
    command, csum, session_id, reply_id = HEADER.unpack_from(packet)
    if verify:
        zeroed = HEADER.pack(command, 0, session_id, reply_id) + packet[HEADER.size :]
        if csum not in (checksum(zeroed), _checksum_variant(zeroed)):
            raise DeviceProtocolError("bad checksum")
    return command, session_id, reply_id, packet[HEADER.size :]


def make_commkey(key: int, session_id: int, ticks: int = 50) -> bytes:
    """Scramble the numeric communication key with the session id for CMD_AUTH (weak by design of the protocol)."""
    if not 0 <= key <= 0xFFFFFFFF:
        raise ValueError("comm key out of range")
    k = 0
    for i in range(32):  # bit-reverse the key
        k = (k << 1) | ((key >> i) & 1)
    k = (k + session_id) & 0xFFFFFFFF
    b = bytearray(struct.pack("<I", k))
    for i, c in enumerate(b"ZKSO"):
        b[i] ^= c
    h0, h1 = struct.unpack("<HH", bytes(b))
    b = bytearray(struct.pack("<HH", h1, h0))
    t = ticks & 0xFF
    b[0] ^= t
    b[1] ^= t
    b[2] = t
    b[3] ^= t
    return bytes(b)


def decode_time(value: int):
    """Packed device time (seconds since 2000, 31-day months) -> naive datetime, or None when it is not a real date."""
    import datetime as dt

    t = value
    second = t % 60
    t //= 60
    minute = t % 60
    t //= 60
    hour = t % 24
    t //= 24
    day = t % 31 + 1
    t //= 31
    month = t % 12 + 1
    t //= 12
    year = t + 2000
    try:
        return dt.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def encode_time(d) -> int:
    return (((d.year - 2000) * 12 + (d.month - 1)) * 31 + (d.day - 1)) * 86400 + d.hour * 3600 + d.minute * 60 + d.second
