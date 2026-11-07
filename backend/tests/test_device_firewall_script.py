"""deploy/firewall/devices.sh (security review M1): the generated rule set is exercised with stub `iptables` / `iptables-restore`
binaries, so no root and no kernel are needed. Skipped where no POSIX `sh` exists."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "deploy"
SCRIPT = ROOT / "firewall" / "devices.sh"
SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")

ENV = {"WORKER_EGRESS_IP": "172.29.250.10", "EGRESS_SUBNET": "172.29.250.0/24", "NET_SUBNET": "172.31.250.0/24"}


def run(tmp_path, env=None, *, restore_fails=False, chain_exists=True, jump_exists=False):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    rules = tmp_path / "restore.rules"
    log.write_text("")
    # a stub iptables: -n -L DOCKER-USER (exists?), -C (jump present?), -I (insert) are recorded
    (bindir / "iptables").write_text(
        "#!/bin/sh\n"
        'echo "iptables $*" >> "$STUB_LOG"\n'
        'case "$*" in\n'
        '  "-n -L DOCKER-USER") [ "$STUB_CHAIN" = 1 ] || exit 1 ;;\n'
        '  "-C DOCKER-USER -j EC-DEVICES") [ "$STUB_JUMP" = 1 ] || exit 1 ;;\n'
        "esac\nexit 0\n"
    )
    (bindir / "iptables-restore").write_text(
        'echo "iptables-restore $*" >> "$STUB_LOG"\ncat > "$STUB_RULES"\n[ "$STUB_RESTORE_FAILS" = 1 ] && exit 1\nexit 0\n'
    )
    for f in bindir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    full = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log), "STUB_RULES": str(rules), "STUB_CHAIN": "1" if chain_exists else "0",
        "STUB_JUMP": "1" if jump_exists else "0", "STUB_RESTORE_FAILS": "1" if restore_fails else "0", "TMPDIR": str(tmp_path),
        **(ENV if env is None else env),
    }  # fmt: skip
    p = subprocess.run([SH, str(SCRIPT)], env=full, capture_output=True, text=True, timeout=30)
    calls = log.read_text().splitlines()
    return p, calls, (rules.read_text().splitlines() if rules.exists() else [])


def test_first_rule_returns_established_and_related_traffic(tmp_path):
    p, _calls, rules = run(tmp_path)
    assert p.returncode == 0, p.stderr
    body = [r for r in rules if r.startswith("-A")]
    assert body[0] == "-A EC-DEVICES -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"


def test_worker_is_allowed_and_only_containers_are_dropped(tmp_path):
    _p, _c, rules = run(tmp_path)
    assert "-A EC-DEVICES -s 172.29.250.10 -d 192.168.0.0/24 -p tcp -m multiport --dports 4370 -m iprange --dst-range 192.168.0.201-192.168.0.203 -j ACCEPT" in rules
    assert "-A EC-DEVICES -s 172.29.250.10 -d 192.168.0.0/24 -p udp -m multiport --dports 4370 -m iprange --dst-range 192.168.0.201-192.168.0.203 -j ACCEPT" in rules
    drops = [r for r in rules if r.endswith("-j DROP")]
    assert drops == [
        "-A EC-DEVICES -s 172.29.250.0/24 -d 192.168.0.0/24 -j DROP",
        "-A EC-DEVICES -s 172.31.250.0/24 -d 192.168.0.0/24 -j DROP",
        "-A EC-DEVICES -i br-+ -d 192.168.0.0/24 -j DROP",
    ]
    # no blanket drop: every DROP names a container source and the LAN, so LAN clients' replies and other hosts are untouched
    assert not any(r == "-A EC-DEVICES -d 192.168.0.0/24 -j DROP" for r in rules)
    assert rules.index("-A EC-DEVICES -j RETURN") == len(rules) - 2 and rules[-1] == "COMMIT"
    allow_idx = max(i for i, r in enumerate(rules) if r.endswith("-j ACCEPT"))
    assert allow_idx < min(i for i, r in enumerate(rules) if r.endswith("-j DROP"))  # the worker is allowed before the drops


def test_chain_is_built_in_one_atomic_transaction_and_the_jump_comes_last(tmp_path):
    p, calls, rules = run(tmp_path)
    assert p.returncode == 0
    assert rules[0] == "*filter" and rules[1] == ":EC-DEVICES - [0:0]" and rules[-1] == "COMMIT"  # declared (= flushed) inside it
    restore = calls.index("iptables-restore --noflush")
    jump = calls.index("iptables -I DOCKER-USER 1 -j EC-DEVICES")
    assert restore < jump
    assert not any(c.startswith("iptables -F") or c.startswith("iptables -A") or c.startswith("iptables -N") for c in calls)


def test_a_failed_restore_never_installs_the_jump(tmp_path):
    p, calls, _rules = run(tmp_path, restore_fails=True)
    assert p.returncode != 0
    assert not any(" -I DOCKER-USER" in c for c in calls)  # no empty / half-built chain is ever hooked in
    assert not any(c.startswith("iptables -F") for c in calls)  # and the previous chain was never flushed


def test_the_jump_is_not_duplicated_on_a_second_run(tmp_path):
    _p, calls, _r = run(tmp_path, jump_exists=True)
    assert not any(c.startswith("iptables -I DOCKER-USER") for c in calls)


def test_missing_docker_chain_or_worker_ip_is_an_error(tmp_path):
    p, calls, _ = run(tmp_path, chain_exists=False)
    assert p.returncode != 0 and "start docker first" in p.stderr and "iptables-restore --noflush" not in calls
    p, _c, _r = run(tmp_path, env={})
    assert p.returncode != 0 and "WORKER_EGRESS_IP" in p.stderr


def test_ports_and_the_server_address_on_the_device_lan(tmp_path):
    _p, _c, rules = run(tmp_path, env={**ENV, "DEVICE_ALLOWED_PORTS": "4370,4371", "SERVER_DEVICE_IP": "192.168.0.33", "SERVER_DEVICE_ALLOW_PORTS": "443"})
    assert any("--dports 4370,4371" in r for r in rules)
    keep = "-A EC-DEVICES -p tcp -m conntrack --ctorigdst 192.168.0.33 --ctorigdstport 443 -j RETURN"
    deny = "-A EC-DEVICES -m conntrack --ctstate NEW --ctorigdst 192.168.0.33 -j DROP"
    assert keep in rules and deny in rules and rules.index(keep) < rules.index(deny)
    assert rules.index("-A EC-DEVICES -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN") < rules.index(deny)  # replies first


def test_systemd_unit_runs_after_docker():
    unit = (ROOT / "firewall" / "ec-devices-firewall.service").read_text()
    assert "After=docker.service" in unit and "Requires=docker.service" in unit and "PartOf=docker.service" in unit
    assert "ExecStart=/bin/sh /opt/employeeconnect/deploy/firewall/devices.sh" in unit
    assert "Type=oneshot" in unit and "WantedBy=multi-user.target" in unit
