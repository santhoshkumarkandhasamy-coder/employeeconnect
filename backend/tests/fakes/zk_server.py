"""Fake ZK attendance machine (asyncio, TCP and UDP) for tests. Implements the READ side only and RECORDS EVERY COMMAND it
receives, so a test can prove that nothing but allow-listed commands ever arrived.

Written independently of app.integrations.zk (own framing, checksum, record encoders) so the client is checked against a second
implementation. Fault switches: busy, silent, latency, drop_mid_transfer, bad_checksum, malformed, oversize, inline_small.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import struct
from dataclasses import dataclass, field

# the independent copy of the allow-list the tests assert against
ALLOWED = {1000, 1001, 1102, 11, 1100, 201, 50, 1503, 1504, 1502}
ALLOWED_BUFFERS = {(9, 5), (13, 0)}
ALLOWED_OPTIONS = {b"~SerialNumber\x00", b"~DeviceName\x00", b"~Platform\x00", b"~ZKFPVersion\x00", b"~OS\x00"}
SESSION_ID = 4242


@dataclass
class FakeUser:
    uid: int
    user_id: str
    name: str
    card: int = 0
    privilege: int = 0
    password: str = "9876"  # must never reach the app


@dataclass
class FakePunch:
    user_id: str
    time: dt.datetime  # naive device clock
    uid: int = 0
    verify: int = 1
    state: int = 0


def _csum(b: bytes) -> int:
    total = 0
    for i in range(0, len(b) - 1, 2):
        total += b[i] | (b[i + 1] << 8)
    if len(b) % 2:
        total += b[-1]
    while total > 0xFFFF:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _enc_time(d: dt.datetime) -> int:
    return (((d.year - 2000) * 12 + d.month - 1) * 31 + d.day - 1) * 86400 + d.hour * 3600 + d.minute * 60 + d.second


def _scramble(key: int, session: int) -> bytes:
    k = int(f"{key:032b}"[::-1], 2)
    k = (k + session) & 0xFFFFFFFF
    b = bytearray(struct.pack("<I", k))
    for i, c in enumerate(b"ZKSO"):
        b[i] ^= c
    h0, h1 = struct.unpack("<HH", bytes(b))
    b = bytearray(struct.pack("<HH", h1, h0))
    t = 50
    b[0] ^= t
    b[1] ^= t
    b[2] = t
    b[3] ^= t
    return bytes(b)


@dataclass
class FakeZK:
    transport: str = "tcp"
    users: list[FakeUser] = field(default_factory=list)
    punches: list[FakePunch] = field(default_factory=list)
    att_format: int = 40  # 8 | 16 | 40
    user_format: int = 72  # 28 | 72
    clock: dt.datetime = field(default_factory=lambda: dt.datetime(2026, 11, 10, 9, 0, 0))
    clock_fn: object = None  # callable returning the device clock (follows real time in sync tests)
    comm_key: int = 0
    serial: str = "SN-TEST-0001"
    model: str = "K40"
    platform: str = "ZMM220_TFT"
    version: str = "Ver 6.60 Apr  1 2020"
    latency: float = 0.0
    busy: bool = False
    silent: bool = False
    drop_mid_transfer: bool = False
    bad_checksum: bool = False
    malformed: bool = False
    oversize: bool = False
    inline_small: bool = False
    commands: list[tuple[int, bytes]] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 0
    _server: asyncio.AbstractServer | None = None
    _udp: asyncio.DatagramTransport | None = None
    _authed: bool = False
    _buffer: bytes = b""

    # ------------------------------------------------------------ record encoders
    def _user_buf(self) -> bytes:
        body = b""
        for u in self.users:
            name = u.name.encode()
            if self.user_format == 28:
                body += struct.pack(
                    "<HB5s8sIxBhI", u.uid, u.privilege, u.password.encode(), name[:8], u.card, 1, 0, int(u.user_id)
                )
            else:
                body += struct.pack(
                    "<HB8s24sIx7sx24s", u.uid, u.privilege, u.password.encode(), name, u.card, b"1", u.user_id.encode()
                )
        return struct.pack("<I", len(body)) + body

    def _att_buf(self) -> bytes:
        body = b""
        for p in self.punches:
            t = struct.pack("<I", _enc_time(p.time))
            if self.att_format == 8:
                body += struct.pack("<HB4sB", p.uid, p.verify, t, p.state)
            elif self.att_format == 16:
                body += struct.pack("<I4sBB2xI", int(p.user_id), t, p.verify, p.state, 0)
            else:
                body += struct.pack("<H24sB4sB8x", p.uid, p.user_id.encode(), p.verify, t, p.state)
        return struct.pack("<I", len(body)) + body

    def now(self) -> dt.datetime:
        return self.clock_fn() if callable(self.clock_fn) else self.clock

    # ------------------------------------------------------------ protocol
    def _packet(self, cmd: int, data: bytes = b"", session: int = SESSION_ID, reply: int = 0) -> bytes:
        head = struct.pack("<HHHH", cmd, 0, session, reply)
        c = _csum(head + data)
        if self.bad_checksum:
            c ^= 0x5A5A
        return struct.pack("<HHHH", cmd, c, session, reply) + data

    def _handle(self, cmd: int, data: bytes, reply: int) -> list[bytes]:
        self.commands.append((cmd, data))
        ok = lambda d=b"": [self._packet(2000, d, SESSION_ID, reply)]  # noqa: E731
        if cmd == 1000:
            if self.busy:
                return [self._packet(2001, b"", 0, reply)]
            self._authed = self.comm_key == 0
            return [self._packet(2005 if self.comm_key else 2000, b"", SESSION_ID, reply)]
        if cmd == 1102:
            if data == _scramble(self.comm_key, SESSION_ID):
                self._authed = True
                return ok()
            return [self._packet(2005, b"", SESSION_ID, reply)]
        if not self._authed:
            return [self._packet(2005, b"", SESSION_ID, reply)]
        if cmd == 1001:
            return ok()
        if cmd == 11:
            key = data.rstrip(b"\x00").decode()
            val = {"~SerialNumber": self.serial, "~DeviceName": self.model, "~Platform": self.platform}.get(key)
            if val is None:
                return [self._packet(2001, b"", SESSION_ID, reply)]
            return ok(f"{key}={val}".encode() + b"\x00")
        if cmd == 1100:
            return ok(self.version.encode() + b"\x00")
        if cmd == 201:
            return ok(struct.pack("<I", _enc_time(self.now())))
        if cmd == 50:
            fields = [0] * 20
            fields[4], fields[8] = len(self.users), len(self.punches)
            return ok(struct.pack("<20i", *fields))
        if cmd == 1503:
            _flag, inner, _fct, _ext = struct.unpack("<bhii", data)
            self._buffer = self._user_buf() if inner == 9 else self._att_buf()
            if self.inline_small and len(self._buffer) < 4096:
                return [self._packet(1501, self._buffer, SESSION_ID, reply)]
            return ok(b"\x00" + struct.pack("<I", len(self._buffer)) + b"\x00" * 3)
        if cmd == 1504:
            start, size = struct.unpack("<ii", data)
            chunk = self._buffer[start : start + size]
            out = [self._packet(1500, struct.pack("<I", len(chunk)), SESSION_ID, reply)]
            pieces = [chunk[i : i + 1000] for i in range(0, len(chunk), 1000)]
            if self.drop_mid_transfer:
                pieces = pieces[: max(1, len(pieces) // 2)]
            out += [self._packet(1501, p, SESSION_ID, reply) for p in pieces]
            if not self.drop_mid_transfer:
                out.append(self._packet(2000, b"", SESSION_ID, reply))
            return out
        if cmd == 1502:
            return ok()
        return [self._packet(2001, b"", SESSION_ID, reply)]  # anything else: refused (and recorded)

    # ------------------------------------------------------------ TCP
    async def _tcp_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = await reader.readexactly(8)
                (length,) = struct.unpack_from("<I", head, 4)
                raw = await reader.readexactly(length)
                cmd, _c, _s, reply = struct.unpack_from("<HHHH", raw)
                if self.silent:
                    self.commands.append((cmd, raw[8:]))
                    continue
                if self.latency:
                    await asyncio.sleep(self.latency)
                if self.malformed:
                    self.commands.append((cmd, raw[8:]))
                    writer.write(b"\xde\xad\xbe\xef" + b"\x00" * 12)
                    await writer.drain()
                    continue
                if self.oversize:
                    self.commands.append((cmd, raw[8:]))
                    writer.write(b"\x50\x50\x82\x7d" + struct.pack("<I", 50_000_000))
                    await writer.drain()
                    continue
                if cmd == 1000 and self.busy:
                    self.commands.append((cmd, raw[8:]))
                    writer.close()  # a device serving another client drops the newcomer
                    return
                replies = self._handle(cmd, raw[8:], reply)
                for i, pkt in enumerate(replies):
                    writer.write(b"\x50\x50\x82\x7d" + struct.pack("<I", len(pkt)) + pkt)
                    await writer.drain()
                    if self.drop_mid_transfer and i == len(replies) - 1 and cmd == 1504:
                        writer.close()
                        return
                if cmd == 1001:
                    writer.close()
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    # ------------------------------------------------------------ UDP
    def _udp_protocol(self):
        outer = self

        class P(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                outer._udp = transport

            def datagram_received(self, data, addr):
                cmd, _c, _s, reply = struct.unpack_from("<HHHH", data)
                if outer.silent:
                    outer.commands.append((cmd, data[8:]))
                    return
                if outer.busy and cmd == 1000:
                    outer.commands.append((cmd, data[8:]))
                    outer._udp.sendto(outer._packet(2001, b"", 0, reply), addr)
                    return
                for pkt in outer._handle(cmd, data[8:], reply):
                    outer._udp.sendto(pkt, addr)

        return P()

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        if self.transport == "tcp":
            self._server = await asyncio.start_server(self._tcp_conn, self.host, 0)
            self.port = self._server.sockets[0].getsockname()[1]
        else:
            tr, _ = await loop.create_datagram_endpoint(self._udp_protocol, local_addr=(self.host, 0))
            self.port = tr.get_extra_info("sockname")[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 1.0)
        if self._udp is not None:
            self._udp.close()

    # ------------------------------------------------------------ assertions
    def assert_only_allowed(self) -> None:
        for cmd, data in self.commands:
            assert cmd in ALLOWED, f"forbidden command {cmd} reached the device"
            if cmd == 1503:
                _f, inner, fct, ext = struct.unpack("<bhii", data)
                assert (inner, fct) in ALLOWED_BUFFERS and ext == 0, f"forbidden buffer read {inner}/{fct}"
            if cmd == 11:
                assert data in ALLOWED_OPTIONS, f"forbidden option {data!r}"
