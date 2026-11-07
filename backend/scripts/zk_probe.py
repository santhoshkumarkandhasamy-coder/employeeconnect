#!/usr/bin/env python
"""Read-only identify script for attendance machines (plan 18). Run inside the worker container before go-live:

    python -m scripts.zk_probe --ip 192.168.0.201 [--port 4370] [--transport tcp|udp] [--comm-key-prompt | --comm-key-stdin]
    python -m scripts.zk_probe --device <uuid>        # uses the registered device (comm key from the database)

The communication key is never taken from the command line (it would show in `ps` and shell history): type it at the prompt
(--comm-key-prompt), pipe it in (--comm-key-stdin) or export ZK_COMM_KEY. No key means 0.

Prints serial, model, platform, firmware, device clock, user and record counts. It sends only allow-listed read commands,
never reads templates, and prints no names or PINs.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import getpass
import os
import sys
import uuid

from app.integrations.zk import DeviceError, ZKClient
from app.services import devices as devices_svc


def read_comm_key(prompt: bool, stdin: bool, env: dict[str, str] | None = None) -> int:
    """Comm key from the prompt, stdin or ZK_COMM_KEY (in that order of preference); 0 when none is given. Never argv."""
    env = os.environ if env is None else env
    raw = ""
    if prompt:
        raw = getpass.getpass("Communication key (hidden, empty = 0): ")
    elif stdin:
        raw = sys.stdin.readline()
    else:
        raw = env.get("ZK_COMM_KEY", "")
    raw = raw.strip()
    if not raw:
        return 0
    if not raw.isdigit() or not 0 <= int(raw) <= 999999:
        raise SystemExit("the communication key must be a number from 0 to 999999")
    return int(raw)


async def _probe(ip: str, port: int, transport: str, key: int) -> int:
    try:
        devices_svc.check_address(ip, port)
    except Exception as exc:  # noqa: BLE001
        print(f"refused: {getattr(exc, 'message', exc)}")
        return 2
    try:
        async with ZKClient(ip, port, transport, key) as zk:
            opts = await zk.get_options()
            version = await zk.get_version()
            now = dt.datetime.now(dt.UTC)
            dev_time = await zk.get_time()
            sizes = await zk.get_sizes()
            print(f"serial   : {opts.get('SerialNumber')}")
            print(f"model    : {opts.get('DeviceName')}")
            print(f"platform : {opts.get('Platform')}")
            print(f"firmware : {version}")
            print(f"clock    : {dev_time} (device local; server UTC {now:%Y-%m-%d %H:%M:%S})")
            print(f"users    : {sizes.users}")
            print(f"records  : {sizes.records}")
    except DeviceError as exc:
        print(f"{exc.code}: {exc}")
        return 1
    return 0


async def _device(device_id: str) -> int:
    from app.core.db import dispose_engine, get_sessionmaker
    from app.models import Device

    async with get_sessionmaker()() as s:
        d = await s.get(Device, uuid.UUID(device_id))
        if d is None:
            print("unknown device")
            return 2
        args = (str(d.ip).split("/")[0], d.port, d.transport, int(d.comm_key or 0))
    await dispose_engine()
    return await _probe(*args)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ip")
    g.add_argument("--device")
    ap.add_argument("--port", type=int, default=4370)
    ap.add_argument("--transport", choices=("tcp", "udp"), default="tcp")
    k = ap.add_mutually_exclusive_group()
    k.add_argument("--comm-key-prompt", action="store_true", help="ask for the communication key (hidden input)")
    k.add_argument("--comm-key-stdin", action="store_true", help="read the communication key from the first line of stdin")
    a = ap.parse_args()
    if a.device:
        return asyncio.run(_device(a.device))
    return asyncio.run(_probe(a.ip, a.port, a.transport, read_comm_key(a.comm_key_prompt, a.comm_key_stdin)))


if __name__ == "__main__":
    sys.exit(main())
