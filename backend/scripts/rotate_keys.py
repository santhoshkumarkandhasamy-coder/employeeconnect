#!/usr/bin/env python
"""Re-encrypt all application-encrypted columns and file blobs under the newest DATA_ENCRYPTION_KEYS key.

    python -m scripts.rotate_keys                 # rotate, then verify against the primary key only
    python -m scripts.rotate_keys --dry-run       # count what would change, write nothing
    python -m scripts.rotate_keys --verify-only   # exit 1 unless everything decrypts with the primary key alone

Steps: 1) prepend the new key (keep old ones)  2) deploy  3) run this  4) drop the old key once verify reports 0 failed.
Back up the database and the file volume first. Safe to re-run (resumable).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.core.crypto import validate_keys
from app.core.db import dispose_engine, get_sessionmaker
from app.services import key_rotation


async def _run(dry_run: bool, verify_only: bool) -> int:
    validate_keys()
    code = 0
    async with get_sessionmaker()() as session:
        if not verify_only:
            stats = await key_rotation.rotate_all(session, dry_run=dry_run)
            print(f"rotate{' (dry run)' if dry_run else ''}: {stats}")
            if stats["undecryptable"] or stats["missing_blobs"]:
                print("WARNING: some values could not be rotated; do NOT drop the old keys", file=sys.stderr)
                code = 1
        if not dry_run:
            v = await key_rotation.verify_all(session)
            print(f"verify (primary key only): {v}")
            if v["failed"]:
                code = 1
    await dispose_engine()
    return code


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level="INFO")
    sys.exit(asyncio.run(_run(args.dry_run, args.verify_only)))


if __name__ == "__main__":
    main()
