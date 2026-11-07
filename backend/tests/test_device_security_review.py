"""Security review of the eSSL device integration: config guards (L1, L2), UDP hardening (L3), probe script (L10), removed debug
switch (Info). The behaviour of the sync, mapping and API fixes is tested next to the code they change (test_device_sync,
test_devices_api, test_migration_0030)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sys

import pytest
from pydantic import ValidationError

from app.core.config import SPECIAL_RANGES, Settings, get_settings
from app.integrations.zk import client as zk_client
from app.integrations.zk import protocol as p
from app.services import devices as devices_svc
from tests.fakes.zk_server import FakeZK, FakePunch, FakeUser

BASE = {"database_url": "postgresql+asyncpg://x:y@localhost/z", "jwt_secret": "k" * 40, "app_env": "dev"}


def make(**kw) -> Settings:
    return Settings(_env_file=None, **{**BASE, **kw})  # type: ignore[call-arg]


def problem(monkeypatch, ip, **settings):
    st = get_settings()
    for k, v in settings.items():
        monkeypatch.setattr(st, k, v)
    return devices_svc.address_problem(ip)


# ---------------------------------------------------------------- L1: ports
def test_l1_allowed_ports_default_and_validation():
    assert make().device_allowed_port_set == frozenset({4370})
    assert make(device_allowed_ports="4370, 4371").device_allowed_port_set == frozenset({4370, 4371})
    for bad in ("", "0", "70000", "abc", "4370,,x"):
        with pytest.raises(ValidationError):
            make(device_allowed_ports=bad)


def test_l1_port_problem(monkeypatch):
    monkeypatch.setattr(get_settings(), "device_allowed_ports", "4370")
    assert devices_svc.port_problem(4370) is None
    assert devices_svc.port_problem(22) and devices_svc.port_problem(6379)
    with pytest.raises(Exception) as exc:
        devices_svc.check_address("192.168.0.201", 22)
    assert getattr(exc.value, "code", None) == "DEVICE_ADDRESS_NOT_ALLOWED" and exc.value.details[0]["field"] == "port"
    devices_svc.check_address("192.168.0.201", 4370)
    devices_svc.check_address("192.168.0.201")  # no port given: only the address is judged


# ---------------------------------------------------------------- L2: always-blocked networks
def test_l2_special_ranges_are_blocked_even_if_somebody_allows_them(monkeypatch):
    allow = "192.168.0.0/24,100.64.0.0/10,198.18.0.0/15,192.0.2.0/24,203.0.113.0/24,240.0.0.0/4"
    for ip in ("100.64.1.1", "198.19.2.2", "192.0.2.10", "203.0.113.5", "250.1.1.1"):
        assert problem(monkeypatch, ip, device_allowed_cidrs=allow), ip
    assert all(n.prefixlen <= 32 for n in SPECIAL_RANGES)
    assert problem(monkeypatch, "192.168.0.201", device_allowed_cidrs="192.168.0.0/24") is None


def test_l2_net_edge_and_egress_networks_and_gateways_are_blocked(monkeypatch):
    st = {
        "device_allowed_cidrs": "192.168.0.0/22",
        "net_subnet": "192.168.1.0/28",
        "edge_subnet": "192.168.2.0/28",
        "egress_subnet": "192.168.3.0/28",
        "worker_egress_ip": "192.168.0.77",
        "device_blocked_ips": "192.168.0.33",
    }
    for ip in ("192.168.1.5", "192.168.2.5", "192.168.3.5", "192.168.1.1", "192.168.2.1", "192.168.3.1", "192.168.0.77", "192.168.0.33"):
        assert problem(monkeypatch, ip, **st), ip
    assert problem(monkeypatch, "192.168.0.201", **st) is None
    nets = [str(n) for n in make(**{k: v for k, v in st.items() if k != "device_allowed_cidrs"}, device_allowed_cidrs="192.168.0.0/24").device_blocked_networks]
    assert "192.168.1.1/32" in nets and "192.168.3.1/32" in nets  # the gateways are explicit


@pytest.mark.parametrize(
    "over",
    [
        {"net_subnet": "192.168.0.0/25"},
        {"edge_subnet": "192.168.0.128/25"},
        {"egress_subnet": "192.168.0.96/28"},
        {"device_blocked_ips": "192.168.0.0/24"},
        {"device_allowed_cidrs": "10.0.0.0/8,127.0.0.0/8"},
        {"device_allowed_cidrs": "192.168.0.0/24,172.17.0.0/16"},
        {"device_allowed_cidrs": "100.64.0.0/10"},
    ],
)
def test_l2_startup_is_refused_when_a_blocked_network_overlaps_the_allow_list(over):
    with pytest.raises(ValidationError, match="overlaps the blocked network"):
        make(**over)


def test_l2_a_single_host_may_be_carved_out_of_the_allow_list():
    s = make(device_blocked_ips="192.168.0.33", worker_egress_ip="172.29.250.10", egress_subnet="172.29.250.0/24", net_subnet="172.31.250.0/24")
    assert s.device_allowed_networks and s.device_blocked_networks


def test_l2_empty_blocked_ips_is_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.config"):
        make(device_blocked_ips="")
    assert any("DEVICE_BLOCKED_IPS is empty" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.config"):
        make(device_blocked_ips="192.168.0.33")
    assert not caplog.records


# ---------------------------------------------------------------- Info: zk_debug removed
def test_zk_debug_switch_is_gone():
    assert "zk_debug" not in Settings.model_fields


# ---------------------------------------------------------------- L3: UDP
def _proto(peer=("192.168.0.201", 4370)):
    return zk_client._UdpProtocol(peer)


def test_l3_udp_datagrams_must_come_from_the_registered_address_and_port():
    u = _proto()
    u.datagram_received(b"a", ("192.168.0.201", 4370))
    u.datagram_received(b"b", ("192.168.0.201", 40000))  # right host, wrong port
    u.datagram_received(b"c", ("192.168.0.99", 4370))  # wrong host
    assert u.queue.qsize() == 1


def test_l3_udp_queue_is_small_and_bounded():
    assert zk_client.UDP_QUEUE_MAX == 64
    u = _proto()
    for _ in range(500):
        u.datagram_received(b"x", ("192.168.0.201", 4370))
    assert u.queue.qsize() == 64
    u.datagram_received(b"y" * (p.MAX_PACKET + 65), ("192.168.0.201", 4370))
    assert u.queue.qsize() == 64


async def test_l3_a_large_log_still_reads_over_udp_with_the_small_queue():
    many = [FakePunch(str(100 + i % 7), dt.datetime(2026, 11, 10, 9, 0) + dt.timedelta(minutes=i), uid=i % 7) for i in range(3000)]
    f = FakeZK(transport="udp", users=[FakeUser(1, "101", "A")], punches=many)
    await f.start()
    try:
        async with zk_client.ZKClient(f.host, f.port, "udp", 0, connect_timeout=1, packet_timeout=2, session_timeout=20) as c:
            punches, _ = await c.get_attendance(3000)
        assert len(punches) == 3000
    finally:
        f.assert_only_allowed()
        await f.stop()


# ---------------------------------------------------------------- L10: the probe never takes the key from argv
def test_l10_probe_reads_the_key_from_env_stdin_or_prompt(monkeypatch):
    from scripts import zk_probe

    assert zk_probe.read_comm_key(False, False, {}) == 0
    assert zk_probe.read_comm_key(False, False, {"ZK_COMM_KEY": " 424242 "}) == 424242
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("777\nignored\n"))
    assert zk_probe.read_comm_key(False, True, {}) == 777
    monkeypatch.setattr(zk_probe.getpass, "getpass", lambda prompt: "123456")
    assert zk_probe.read_comm_key(True, False, {"ZK_COMM_KEY": "1"}) == 123456
    for bad in ("12ab", "-5", "1000000"):
        with pytest.raises(SystemExit):
            zk_probe.read_comm_key(False, False, {"ZK_COMM_KEY": bad})


def test_l10_probe_has_no_comm_key_argument(monkeypatch, capsys):
    from scripts import zk_probe

    for argv in (["zk_probe", "--ip", "192.168.0.201", "--comm-key", "1234"], ["zk_probe", "--ip", "192.168.0.201", "--comm-key=1234"]):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            zk_probe.main()
        assert exc.value.code == 2  # argparse refuses an unknown / ambiguous option
    assert "--comm-key " not in (zk_probe.__doc__ or "").replace("--comm-key-", "")
    capsys.readouterr()


def test_l10_probe_applies_the_port_guard(monkeypatch, capsys):
    from scripts import zk_probe

    monkeypatch.setattr(get_settings(), "device_allowed_ports", "4370")
    assert asyncio.run(zk_probe._probe("192.168.0.201", 22, "tcp", 0)) == 2
    assert "refused" in capsys.readouterr().out
