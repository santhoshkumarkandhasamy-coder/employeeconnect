"""Asyncio READ-ONLY ZK client (TCP default, UDP optional). One connection per instance.

Safety properties (tested): every outgoing packet passes `protocol.check_command` in `_send_packet` (the only caller of
`_write_raw`, the only place bytes reach a socket); timeouts: connect 5 s, each packet 10 s, whole session 120 s; every length
read from the device is bounded before memory is allocated.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import struct
import time

from . import protocol as p
from .errors import DeviceAuthFailed, DeviceBusy, DeviceError, DeviceProtocolError, DeviceTimeout, DeviceUnreachable
from .records import ZKPunch, ZKSizes, ZKUser, clean, parse_attendance, parse_sizes, parse_users

log = logging.getLogger("app.zk")
CONNECT_TIMEOUT_S = 5.0
PACKET_TIMEOUT_S = 10.0
SESSION_TIMEOUT_S = 120.0
CHUNK_TCP = 0x8000
CHUNK_UDP = 0x4000
UDP_QUEUE_MAX = 64  # datagrams waiting for the reader; a flood from the LAN is dropped instead of buffered (review L3)


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, peer: tuple[str, int]):
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.peer = peer
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        # only the registered device (address AND port) is believed; the queue is small and bounded
        if (addr[0], addr[1]) == (self.peer[0], self.peer[1]) and len(data) <= p.MAX_PACKET + 64 and self.queue.qsize() < UDP_QUEUE_MAX:
            self.queue.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        self.queue.put_nowait(None)

    def connection_lost(self, exc) -> None:
        self.queue.put_nowait(None)


class ZKClient:
    def __init__(
        self,
        host: str,
        port: int = 4370,
        transport: str = "tcp",
        comm_key: int = 0,
        *,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        packet_timeout: float = PACKET_TIMEOUT_S,
        session_timeout: float = SESSION_TIMEOUT_S,
    ):
        if transport not in ("tcp", "udp"):
            raise ValueError("transport must be tcp or udp")
        self.host, self.port, self.transport, self.comm_key = host, port, transport, comm_key
        self._connect_timeout, self._packet_timeout, self._session_timeout = connect_timeout, packet_timeout, session_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._udp: _UdpProtocol | None = None
        self._session = 0
        self._reply = 0
        self._deadline = 0.0
        self._open = False
        self._handshake = False

    # ------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> ZKClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + self._session_timeout
        try:
            if self.transport == "tcp":
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), self._connect_timeout
                )
            else:
                tr, proto = await asyncio.wait_for(
                    loop.create_datagram_endpoint(
                        lambda: _UdpProtocol((self.host, self.port)), remote_addr=(self.host, self.port)
                    ),
                    self._connect_timeout,
                )
                self._udp = proto
                self._udp.transport = tr  # type: ignore[assignment]
        except TimeoutError:
            raise DeviceTimeout(f"connect timed out after {self._connect_timeout:g}s") from None
        except OSError as exc:
            raise DeviceUnreachable(f"cannot connect: {exc.__class__.__name__}") from None
        self._open = True
        self._handshake = True
        try:
            cmd, sid, data = await self._request(p.CMD_CONNECT, b"", tolerate_error=True)
            if cmd == p.CMD_ACK_ERROR:
                raise DeviceBusy("device refused the connection (another client is connected)")
            self._session = sid
            if cmd == p.CMD_ACK_UNAUTH:
                await self._authenticate()
            elif cmd != p.CMD_ACK_OK:
                raise DeviceProtocolError(f"unexpected reply {cmd} to connect")
        except BaseException:
            await self._abort()
            raise
        finally:
            self._handshake = False

    async def _authenticate(self) -> None:
        cmd, _sid, _data = await self._request(p.CMD_AUTH, p.make_commkey(self.comm_key, self._session), tolerate_error=True)
        if cmd != p.CMD_ACK_OK:
            raise DeviceAuthFailed("the device rejected the communication key")

    async def close(self) -> None:
        """Polite CMD_EXIT, then close the socket. Never raises."""
        if self._open and self._session is not None:
            with contextlib.suppress(Exception):
                self._deadline = max(self._deadline, asyncio.get_running_loop().time() + 2.0)
                await asyncio.wait_for(self._request(p.CMD_EXIT, b"", tolerate_error=True), 2.0)
        await self._abort()

    async def _abort(self) -> None:
        self._open = False
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), 1.0)
        if self._udp is not None and self._udp.transport is not None:
            self._udp.transport.close()
        self._reader = self._writer = self._udp = None

    # ------------------------------------------------------------ the one write path
    def _write_raw(self, data: bytes) -> None:
        """The ONLY place bytes reach a socket. Reachable only through `_send_packet` (allow-list gate)."""
        if self._writer is not None:
            self._writer.write(data)
        elif self._udp is not None and self._udp.transport is not None:
            self._udp.transport.sendto(data)  # type: ignore[attr-defined]
        else:
            raise DeviceUnreachable("not connected")

    async def _send_packet(self, command: int, data: bytes) -> None:
        p.check_command(command, data)  # raises ForbiddenCommand before any byte is written
        packet = p.build_packet(command, self._session, self._reply, data)
        self._reply = (self._reply + 1) & 0xFFFF
        self._write_raw(p.frame_tcp(packet) if self.transport == "tcp" else packet)
        if self._writer is not None:
            await self._guard(self._writer.drain())

    # ------------------------------------------------------------ receive
    def _remaining(self) -> float:
        left = self._deadline - asyncio.get_running_loop().time()
        if left <= 0:
            raise DeviceTimeout("session time limit reached")
        return min(self._packet_timeout, left)

    async def _guard(self, coro):
        try:
            timeout = self._remaining()
        except DeviceTimeout:
            coro.close()
            raise
        try:
            return await asyncio.wait_for(coro, timeout)
        except TimeoutError:
            raise DeviceTimeout("the device did not answer in time") from None
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            # a device that is serving another client typically drops the new connection during the handshake
            if self._handshake:
                raise DeviceBusy("connection closed by the device") from None
            raise DeviceProtocolError("connection closed by the device") from None

    async def _recv_packet(self) -> tuple[int, int, bytes]:
        if self._reader is not None:
            head = await self._guard(self._reader.readexactly(8))
            if head[:4] != p.TCP_MAGIC:
                raise DeviceProtocolError("bad TCP framing")
            (length,) = struct.unpack_from("<I", head, 4)
            if length < p.HEADER.size or length > p.MAX_PACKET + p.HEADER.size:
                raise DeviceProtocolError("packet length out of bounds")
            raw = await self._guard(self._reader.readexactly(length))
        elif self._udp is not None:
            got = await self._guard(self._udp.queue.get())
            if got is None:
                raise DeviceUnreachable("udp error")
            raw = got
        else:
            raise DeviceUnreachable("not connected")
        cmd, sid, _rid, data = p.parse_packet(raw)
        return cmd, sid, data

    async def _request(self, command: int, data: bytes = b"", *, tolerate_error: bool = False) -> tuple[int, int, bytes]:
        await self._send_packet(command, data)
        cmd, sid, body = await self._recv_packet()
        if self._session and sid != self._session and command != p.CMD_CONNECT:
            raise DeviceProtocolError("session id mismatch")
        if cmd == p.CMD_ACK_UNAUTH and command != p.CMD_CONNECT and not tolerate_error:
            raise DeviceAuthFailed("the device wants authentication")
        if cmd == p.CMD_ACK_ERROR and not tolerate_error:
            raise DeviceProtocolError(f"device answered an error to command {command}")
        return cmd, sid, body

    # ------------------------------------------------------------ read-only API
    async def get_options(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in ("~SerialNumber", "~DeviceName", "~Platform", "~ZKFPVersion", "~OS"):
            cmd, _s, data = await self._request(p.CMD_OPTIONS_RRQ, name.encode() + b"\x00", tolerate_error=True)
            if cmd not in (p.CMD_ACK_OK, p.CMD_ACK_DATA):
                continue  # not every model has every option
            text = clean(data, 120)
            out[name.lstrip("~")] = text.split("=", 1)[1] if "=" in text else text
        return out

    async def get_version(self) -> str:
        cmd, _s, data = await self._request(p.CMD_GET_VERSION)
        if cmd not in (p.CMD_ACK_OK, p.CMD_ACK_DATA):
            raise DeviceProtocolError("version not available")
        return clean(data, 80)

    async def get_time(self) -> dt.datetime | None:
        """Naive device clock, or None when the device reports an impossible date."""
        cmd, _s, data = await self._request(p.CMD_GET_TIME)
        if cmd not in (p.CMD_ACK_OK, p.CMD_ACK_DATA) or len(data) < 4:
            raise DeviceProtocolError("device time not available")
        return p.decode_time(struct.unpack_from("<I", data)[0])

    async def get_sizes(self) -> ZKSizes:
        cmd, _s, data = await self._request(p.CMD_GET_FREE_SIZES)
        if cmd not in (p.CMD_ACK_OK, p.CMD_ACK_DATA):
            raise DeviceProtocolError("record counts not available")
        return parse_sizes(data)

    async def get_users(self, expected: int) -> list[ZKUser]:
        buf = await self._read_buffer(p.CMD_USERTEMP_RRQ, p.FCT_USER)
        return parse_users(buf, expected)

    async def get_attendance(self, expected: int) -> tuple[list[ZKPunch], int]:
        buf = await self._read_buffer(p.CMD_ATTLOG_RRQ, p.FCT_ATTLOG)
        return parse_attendance(buf, expected)

    # ------------------------------------------------------------ buffered table read
    async def _read_buffer(self, inner: int, fct: int) -> bytes:
        cmd, _s, data = await self._request(p.CMD_DATA_WRRQ, p.prepare_buffer_payload(inner, fct))
        try:
            if cmd == p.CMD_DATA:  # small table: delivered directly
                return self._bounded(data)
            if cmd != p.CMD_ACK_OK or len(data) < 5:
                raise DeviceProtocolError(f"unexpected reply {cmd} to buffer request")
            (size,) = struct.unpack_from("<I", data, 1)
            if size > p.MAX_BUFFER:
                raise DeviceProtocolError("buffer larger than the safety limit")
            chunk = CHUNK_TCP if self.transport == "tcp" else CHUNK_UDP
            parts: list[bytes] = []
            got = 0
            while got < size:
                want = min(chunk, size - got)
                piece = await self._read_chunk(got, want)
                if not piece:
                    raise DeviceProtocolError("empty chunk")
                parts.append(piece)
                got += len(piece)
            return b"".join(parts)
        finally:
            with contextlib.suppress(DeviceError):
                await self._request(p.CMD_FREE_DATA, b"", tolerate_error=True)

    def _bounded(self, data: bytes) -> bytes:
        if len(data) > p.MAX_BUFFER:
            raise DeviceProtocolError("buffer larger than the safety limit")
        return data

    async def _read_chunk(self, start: int, size: int) -> bytes:
        cmd, _s, data = await self._request(p.CMD_DATA_RDY, p.chunk_payload(start, size))
        if cmd == p.CMD_ACK_OK or (cmd == p.CMD_DATA and len(data) == size):
            return data  # delivered inline
        if cmd == p.CMD_DATA:
            parts = [data]
            got = len(data)
        elif cmd == p.CMD_PREPARE_DATA:
            if len(data) < 4:
                raise DeviceProtocolError("short prepare-data reply")
            (announced,) = struct.unpack_from("<I", data)
            if announced != size:
                raise DeviceProtocolError("device announced a different chunk size")
            parts, got = [], 0
        else:
            raise DeviceProtocolError(f"unexpected reply {cmd} to chunk read")
        started = time.monotonic()
        while got < size:
            c, _sid, body = await self._recv_packet()
            if c != p.CMD_DATA:
                raise DeviceProtocolError("data stream interrupted")
            parts.append(body)
            got += len(body)
            if got > size or time.monotonic() - started > self._session_timeout:
                raise DeviceProtocolError("chunk longer than announced")
        c, _sid, _body = await self._recv_packet()  # trailing ACK_OK keeps the stream in step
        if c != p.CMD_ACK_OK:
            raise DeviceProtocolError("chunk not acknowledged")
        return b"".join(parts)
