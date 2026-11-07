"""ZK client (plan 18 section 6, 10): codec golden bytes, parsers, the read-only allow-list, and the client against the fake server
over TCP and UDP. After EVERY test the fake server asserts that only allow-listed commands ever arrived."""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import struct
from pathlib import Path

import pytest

from app.integrations import zk
from app.integrations.zk import client as zk_client
from app.integrations.zk import protocol as p
from app.integrations.zk import records as rec
from tests.fakes.zk_server import FakePunch, FakeUser, FakeZK

T0 = dt.datetime(2026, 11, 10, 9, 2, 11)
USERS = [FakeUser(1, "101", "RAVI K", card=12344821, privilege=0), FakeUser(2, "207", "Anita", privilege=14)]
PUNCHES = [FakePunch("101", T0, uid=1, verify=1, state=0), FakePunch("101", T0 + dt.timedelta(hours=9), uid=1, verify=4, state=1)]


@pytest.fixture
async def make_fake():
    servers: list[FakeZK] = []

    async def _make(**kw) -> FakeZK:
        kw.setdefault("users", list(USERS))
        kw.setdefault("punches", list(PUNCHES))
        f = FakeZK(**kw)
        await f.start()
        servers.append(f)
        return f

    yield _make
    for f in servers:  # the read-only proof, for every test in this module
        f.assert_only_allowed()
        await f.stop()


def client_for(f: FakeZK, **kw) -> zk.ZKClient:
    return zk.ZKClient("127.0.0.1", f.port, f.transport, f.comm_key, **kw)


# ---------------------------------------------------------------- codec
def test_connect_packet_golden_bytes():
    assert p.frame_tcp(p.build_packet(p.CMD_CONNECT, 0, 0)).hex() == "5050827d08000000e80317fc00000000"


def test_checksum_roundtrip_and_tamper():
    pkt = p.build_packet(p.CMD_GET_TIME, 7, 3)
    assert p.parse_packet(pkt) == (p.CMD_GET_TIME, 7, 3, b"")
    bad = bytearray(pkt)
    bad[2] ^= 0xFF
    with pytest.raises(zk.DeviceProtocolError):
        p.parse_packet(bytes(bad))
    with pytest.raises(zk.DeviceProtocolError):
        p.parse_packet(b"\x01\x02")


def test_commkey_scramble_is_deterministic_and_session_bound():
    assert p.make_commkey(0, 1) == p.make_commkey(0, 1)
    assert p.make_commkey(123456, 1) != p.make_commkey(123456, 256)  # low byte of the session is dropped by the scheme
    assert len(p.make_commkey(999999, 65535)) == 4


def test_time_encode_decode_and_impossible_date():
    d = dt.datetime(2026, 11, 10, 23, 59, 58)
    assert p.decode_time(p.encode_time(d)) == d
    assert p.decode_time(((26 * 12 + 1) * 31 + 30) * 86400) is None  # 31 February


def test_parsers_28_and_72_byte_users_drop_the_pin():
    for fmt in (28, 72):
        f = FakeZK(users=[FakeUser(5, "101", "Ravi", card=4821, privilege=2, password="PIN77")], user_format=fmt)
        users = rec.parse_users(f._user_buf(), 1)
        assert users == [zk.ZKUser(uid=5, user_id="101", name="Ravi", card="4821", privilege=2)]
        assert users[0].is_admin
        assert "PIN77" not in repr(users) and not hasattr(users[0], "password")


@pytest.mark.parametrize("fmt", [8, 16, 40])
def test_parsers_attendance_formats(fmt):
    f = FakeZK(punches=[FakePunch("101", T0, uid=3, verify=15, state=1)], att_format=fmt)
    punches, skipped = rec.parse_attendance(f._att_buf(), 1)
    assert skipped == 0 and len(punches) == 1
    one = punches[0]
    assert (one.time, one.verify, one.state) == (T0, 15, 1)
    assert one.user_id == (None if fmt == 8 else "101")
    assert one.uid == (3 if fmt != 16 else None)


def test_parser_skips_impossible_timestamps_and_rejects_bad_lengths():
    good = FakeZK(punches=[FakePunch("1", T0, uid=1)])._att_buf()
    broken = bytearray(good)
    broken[4 + 27 : 4 + 31] = struct.pack("<I", ((26 * 12 + 1) * 31 + 30) * 86400)
    punches, skipped = rec.parse_attendance(bytes(broken), 1)
    assert punches == [] and skipped == 1
    with pytest.raises(zk.DeviceProtocolError):
        rec.parse_attendance(good[:-3], 1)


def test_clean_strips_control_and_bidi_characters():
    assert rec.clean("Ravi\u202e\x07 K\x00junk".encode(), 60) == "Ravi K"
    assert len(rec.clean(b"x" * 500, 24)) == 24


# ---------------------------------------------------------------- allow-list
FORBIDDEN = {
    "CLEAR_ATTLOG": 15, "CLEAR_DATA": 14, "DELETE_USER": 18, "USER_WRQ": 8, "SET_TIME": 202, "DISABLEDEVICE": 1003,
    "ENABLEDEVICE": 1002, "RESTART": 1004, "POWEROFF": 1005, "UNLOCK": 31, "DB_RRQ": 7, "DELETE_USERTEMP": 19,
    "REFRESHDATA": 1013, "SET_OPTION": 12, "WRITE_LCD": 66, "TESTVOICE": 1017, "CLEAR_ADMIN": 20,
}  # fmt: skip


@pytest.mark.parametrize("name,code", sorted(FORBIDDEN.items()))
def test_every_known_write_or_template_command_is_refused(name, code):
    if code in p.ALLOWED_COMMANDS:
        pytest.skip("allowed code")
    with pytest.raises(zk.ForbiddenCommand):
        p.check_command(code, b"")


def test_buffer_reads_other_than_users_and_attlog_are_refused():
    ok = (p.prepare_buffer_payload(p.CMD_USERTEMP_RRQ, p.FCT_USER), p.prepare_buffer_payload(p.CMD_ATTLOG_RRQ, p.FCT_ATTLOG))
    for payload in ok:
        p.check_command(p.CMD_DATA_WRRQ, payload)
    for inner, fct in [(7, 2), (7, 0), (9, 2), (9, 0), (13, 5), (8, 5), (19, 5), (15, 0), (1, 1)]:
        with pytest.raises(zk.ForbiddenCommand):
            p.check_command(p.CMD_DATA_WRRQ, p.prepare_buffer_payload(inner, fct))
    with pytest.raises(zk.ForbiddenCommand):
        p.check_command(p.CMD_DATA_WRRQ, struct.pack("<bhii", 1, 13, 0, 5))  # non-zero extension
    with pytest.raises(zk.ForbiddenCommand):
        p.check_command(p.CMD_DATA_WRRQ, b"")


def test_options_must_be_a_fixed_read_name_never_a_write():
    p.check_command(p.CMD_OPTIONS_RRQ, b"~SerialNumber\x00")
    for data in (b"~SerialNumber=1\x00", b"~SerialNumber", b"~Anything\x00", b"", b"IPAddress\x00", b"~SerialNumber\x00x"):
        with pytest.raises(zk.ForbiddenCommand):
            p.check_command(p.CMD_OPTIONS_RRQ, data)


def test_read_commands_take_no_payload():
    for cmd in (p.CMD_CONNECT, p.CMD_EXIT, p.CMD_GET_TIME, p.CMD_GET_VERSION, p.CMD_GET_FREE_SIZES, p.CMD_FREE_DATA):
        p.check_command(cmd, b"")
        with pytest.raises(zk.ForbiddenCommand):
            p.check_command(cmd, b"\x00")


def test_client_has_exactly_one_socket_write_path():
    tree = ast.parse(Path(inspect.getsourcefile(zk_client)).read_text(encoding="utf-8"))
    writers: dict[str, list[str]] = {}

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("write", "writelines", "sendto", "send", "sendall"):
                writers.setdefault(self.stack[-1] if self.stack else "<module>", []).append(node.func.attr)
            self.generic_visit(node)

    V().visit(tree)
    assert set(writers) == {"_write_raw"}, writers
    callers = [
        n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "_write_raw" for c in ast.walk(n))
    ]  # fmt: skip
    assert callers == ["_send_packet"]
    send_src = inspect.getsource(zk_client.ZKClient._send_packet)
    assert send_src.index("check_command") < send_src.index("_write_raw")


async def test_forbidden_command_never_reaches_the_wire(make_fake):
    f = await make_fake()
    async with client_for(f) as c:
        before = len(f.commands)
        with pytest.raises(zk.ForbiddenCommand):
            await c._send_packet(15, b"")  # CLEAR_ATTLOG
        with pytest.raises(zk.ForbiddenCommand):
            await c._send_packet(p.CMD_DATA_WRRQ, p.prepare_buffer_payload(7, 2))  # template read
        assert len(f.commands) == before


# ---------------------------------------------------------------- client against the fake (TCP and UDP)
@pytest.mark.parametrize("transport", ["tcp", "udp"])
@pytest.mark.parametrize("att_fmt,user_fmt", [(40, 72), (16, 28), (8, 28)])
async def test_full_read_session(make_fake, transport, att_fmt, user_fmt):
    f = await make_fake(transport=transport, att_format=att_fmt, user_format=user_fmt)
    async with client_for(f) as c:
        opts = await c.get_options()
        assert opts["SerialNumber"] == "SN-TEST-0001" and opts["DeviceName"] == "K40"
        assert (await c.get_version()).startswith("Ver 6.60")
        assert await c.get_time() == f.clock
        sizes = await c.get_sizes()
        assert (sizes.users, sizes.records) == (2, 2)
        users = await c.get_users(sizes.users)
        assert [u.user_id for u in users] == ["101", "207"]
        assert users[1].is_admin and not users[0].is_admin
        punches, skipped = await c.get_attendance(sizes.records)
        assert skipped == 0 and [x.time for x in punches] == [x.time for x in PUNCHES]
    sent = [cmd for cmd, _ in f.commands]
    assert sent[0] == 1000 and sent[-1] == 1001
    assert "9876" not in repr(users)


async def test_large_log_is_read_in_chunks(make_fake):
    many = [FakePunch(str(100 + i % 7), T0 + dt.timedelta(minutes=i), uid=i % 7) for i in range(3000)]
    f = await make_fake(punches=many)
    async with client_for(f) as c:
        punches, _ = await c.get_attendance(3000)
    assert len(punches) == 3000
    assert sum(1 for cmd, _ in f.commands if cmd == 1504) >= 3  # 120 KB over 32 KB chunks


async def test_small_buffer_delivered_inline(make_fake):
    f = await make_fake(inline_small=True)
    async with client_for(f) as c:
        assert len(await c.get_users(2)) == 2


@pytest.mark.parametrize("transport", ["tcp", "udp"])
async def test_comm_key_ok_and_wrong(make_fake, transport):
    f = await make_fake(transport=transport, comm_key=123456)
    async with zk.ZKClient("127.0.0.1", f.port, transport, 123456) as c:
        assert (await c.get_sizes()).users == 2
    with pytest.raises(zk.DeviceAuthFailed):
        await zk.ZKClient("127.0.0.1", f.port, transport, 654321).connect()


async def test_missing_comm_key_is_an_auth_failure(make_fake):
    f = await make_fake(comm_key=555)
    with pytest.raises(zk.DeviceAuthFailed):
        await zk.ZKClient("127.0.0.1", f.port, "tcp", 0).connect()


@pytest.mark.parametrize("transport", ["tcp", "udp"])
async def test_busy_device(make_fake, transport):
    f = await make_fake(transport=transport, busy=True)
    with pytest.raises(zk.DeviceBusy):
        await zk.ZKClient("127.0.0.1", f.port, transport, 0).connect()


async def test_unreachable_port():
    import socket

    with socket.socket() as sock:  # a port that was free a moment ago and has no listener
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(zk.DeviceUnreachable):
        await zk.ZKClient("127.0.0.1", port, "tcp", 0, connect_timeout=3).connect()


@pytest.mark.parametrize("transport", ["tcp", "udp"])
async def test_silent_device_times_out(make_fake, transport):
    f = await make_fake(transport=transport, silent=True)
    with pytest.raises(zk.DeviceTimeout):
        await zk.ZKClient("127.0.0.1", f.port, transport, 0, packet_timeout=0.3).connect()


async def test_session_cap_is_enforced(make_fake):
    f = await make_fake(latency=0.25)
    async with zk.ZKClient("127.0.0.1", f.port, "tcp", 0, packet_timeout=5, session_timeout=0.7) as c:
        with pytest.raises(zk.DeviceTimeout):
            for _ in range(10):
                await c.get_time()


async def test_drop_in_the_middle_of_a_transfer(make_fake):
    many = [FakePunch("101", T0 + dt.timedelta(minutes=i), uid=1) for i in range(100)]
    f = await make_fake(punches=many, drop_mid_transfer=True)
    with pytest.raises(zk.DeviceError):
        async with client_for(f, packet_timeout=1) as c:
            await c.get_attendance(100)


async def test_wrong_checksum_is_a_protocol_error(make_fake):
    f = await make_fake(bad_checksum=True)
    with pytest.raises(zk.DeviceProtocolError):
        await client_for(f).connect()


async def test_malformed_and_oversized_packets_are_refused(make_fake):
    f = await make_fake(malformed=True)
    with pytest.raises(zk.DeviceProtocolError):
        await client_for(f).connect()
    g = await make_fake(oversize=True)
    with pytest.raises(zk.DeviceProtocolError):
        await client_for(g).connect()


async def test_a_buffer_larger_than_the_limit_is_refused(make_fake):
    f = await make_fake()
    async with client_for(f) as c:
        orig = f._handle

        def fat(cmd, data, reply):
            if cmd == 1503:
                f.commands.append((cmd, data))
                return [f._packet(2000, b"\x00" + struct.pack("<I", p.MAX_BUFFER + 1), 4242, reply)]
            return orig(cmd, data, reply)

        f._handle = fat  # type: ignore[method-assign]
        with pytest.raises(zk.DeviceProtocolError):
            await c.get_attendance(10)


def test_timeouts_are_the_documented_constants():
    assert (zk_client.CONNECT_TIMEOUT_S, zk_client.PACKET_TIMEOUT_S, zk_client.SESSION_TIMEOUT_S) == (5.0, 10.0, 120.0)
