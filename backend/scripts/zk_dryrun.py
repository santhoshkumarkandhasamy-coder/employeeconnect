#!/usr/bin/env python
"""Read-only dry run of a full device read (plan 18): users + punch log, nothing is written anywhere (no database, no Redis).

    python -m scripts.zk_dryrun --ip 192.168.0.202 [--port 4370] [--transport tcp|udp] [--comm-key-prompt | --comm-key-stdin]

Checks that the user table and the punch log parse on this firmware: the record counts match what the device reports, user ids
are sane, every punch belongs to a known user, times are plausible. Prints only counts and ranges, no names, PINs or cards.
It sends only the allow-listed read commands (the user table and the attendance log; never templates).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import sys

from app.integrations.zk import DeviceError, ZKClient
from app.services import devices as devices_svc

from .zk_probe import read_comm_key


def _range(values: list[dt.datetime]) -> str:
    return f"{min(values):%Y-%m-%d %H:%M:%S} .. {max(values):%Y-%m-%d %H:%M:%S}" if values else "-"


async def _dryrun(ip: str, port: int, transport: str, key: int) -> int:
    try:
        devices_svc.check_address(ip, port)
    except Exception as exc:  # noqa: BLE001
        print(f"refused: {getattr(exc, 'message', exc)}")
        return 2
    problems: list[str] = []
    try:
        async with ZKClient(ip, port, transport, key) as zk:
            sizes = await zk.get_sizes()
            now_dev = await zk.get_time()
            users = await zk.get_users(sizes.users)
            punches, skipped = await zk.get_attendance(sizes.records)
    except DeviceError as exc:
        print(f"{exc.code}: {exc}")
        return 1

    print(f"device reports   : {sizes.users} users, {sizes.records} records")
    print(f"parsed           : {len(users)} users, {len(punches)} punches, {skipped} skipped/invalid records")
    if len(users) != sizes.users:
        problems.append(f"user count {len(users)} != reported {sizes.users}")
    if len(punches) + skipped != sizes.records:
        problems.append(f"punches+skipped {len(punches) + skipped} != reported {sizes.records}")

    ids = [u.user_id for u in users]
    print(f"user ids         : {len(set(ids))} distinct, length {min(map(len, ids), default=0)}..{max(map(len, ids), default=0)}, "
          f"numeric {sum(i.isdigit() for i in ids)}, empty {sum(not i for i in ids)}")
    print(f"user names       : {sum(bool(u.name) for u in users)} with a name, {sum(u.card is not None for u in users)} with a card, "
          f"{sum(u.is_admin for u in users)} admin")
    if len(set(ids)) != len(ids):
        problems.append("duplicate user ids in the user table")
    if any(not i for i in ids):
        problems.append("empty user id")

    known = set(ids)
    by_uid = {u.uid: u.user_id for u in users}  # the 8-byte punch format carries only the internal uid
    unknown = [p for p in punches if (p.user_id or by_uid.get(p.uid)) not in known]
    print(f"punch users      : {len({p.user_id or by_uid.get(p.uid) for p in punches})} distinct; punches of users not in the user table: {len(unknown)}")
    times = [p.time for p in punches]
    print(f"punch time range : {_range(times)} (device clock; device now {now_dev})")
    if now_dev is not None and times and max(times) > now_dev + dt.timedelta(minutes=10):
        problems.append("a punch is later than the device clock now")
    if len(set((p.user_id or by_uid.get(p.uid), p.time) for p in punches)) != len(punches):
        problems.append("duplicate (user, time) punches")
    print(f"punch state      : {dict(sorted(collections.Counter(p.state for p in punches).items()))}")
    print(f"punch verify     : {dict(sorted(collections.Counter(p.verify for p in punches).items()))}")
    per_day = collections.Counter(p.time.date() for p in punches)
    if per_day:
        print(f"punches per day  : max {max(per_day.values())}, days with punches {len(per_day)}")

    print("RESULT           : " + ("OK" if not problems else "PROBLEMS"))
    for pr in problems:
        print(f"  - {pr}")
    return 0 if not problems else 3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ip", required=True)
    ap.add_argument("--port", type=int, default=4370)
    ap.add_argument("--transport", choices=("tcp", "udp"), default="tcp")
    k = ap.add_mutually_exclusive_group()
    k.add_argument("--comm-key-prompt", action="store_true")
    k.add_argument("--comm-key-stdin", action="store_true")
    a = ap.parse_args()
    return asyncio.run(_dryrun(a.ip, a.port, a.transport, read_comm_key(a.comm_key_prompt, a.comm_key_stdin)))


if __name__ == "__main__":
    sys.exit(main())
