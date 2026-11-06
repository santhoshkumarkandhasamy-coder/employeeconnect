#!/usr/bin/env python
"""Read-only identify script for attendance machines (plan 18). Run inside the worker container before go-live:

    python -m scripts.zk_probe --ip 192.168.0.201 [--port 4370] [--transport tcp|udp] [--comm-key 0]
    python -m scripts.zk_probe --device <uuid>        # uses the registered device (comm key from the database)

Prints serial, model, platform, firmware, device clock, user and record counts. It sends only allow-listed read commands,
never reads templates, and prints no names or PINs.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import uuid

from app.integrations.zk import DeviceError, ZKClient
from app.services import devices as devices_svc


async def _probe(ip: str, port: int, transport: str, key: int) -> int:
    try:
        devices_svc.check_address(ip)
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
    ap.add_argument("--comm-key", type=int, default=0)
    a = ap.parse_args()
    if a.device:
        return asyncio.run(_device(a.device))
    return asyncio.run(_probe(a.ip, a.port, a.transport, a.comm_key))


if __name__ == "__main__":
    sys.exit(main())
