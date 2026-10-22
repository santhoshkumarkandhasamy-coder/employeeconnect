#!/usr/bin/env python
"""VENDOR-SIDE, run offline. Issues a signed Employee Connect license token (ECL1.<payload>.<signature>).

    python issue_license.py --key vendor_private.key --deployment-id <uuid from the customer console/CLI> \\
        --license-id L-2026-0042 --licensed-to "Acme Pvt Ltd" --max-users 50 \\
        [--expires 2027-10-22] [--grace-days 14] [--on-expiry block_new_users|read_only] [--issued-at 2026-10-22T00:00:00Z]

No --expires = the license never expires. The customer installs the printed token with Admin > License (PUT /admin/license)
or `python -m scripts.create_admin license install FILE`. The token is bound to the deployment id, so it cannot be copied to
another installation. A later license for the same customer must have a later --issued-at than the installed one.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import sys
import uuid

from cryptography.hazmat.primitives import serialization

PREFIX = "ECL1"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _ts(value: str) -> str:
    d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return d.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, help="vendor private key (PEM)")
    ap.add_argument("--deployment-id", required=True)
    ap.add_argument("--license-id", required=True)
    ap.add_argument("--licensed-to", required=True)
    ap.add_argument("--max-users", required=True, type=int)
    ap.add_argument("--expires", help="ISO date/time; omit for a license that never expires")
    ap.add_argument("--grace-days", type=int, default=14)
    ap.add_argument("--on-expiry", choices=["block_new_users", "read_only"], default="block_new_users")
    ap.add_argument("--issued-at", help="default: now")
    ap.add_argument("--out", help="write the token to this file instead of stdout")
    a = ap.parse_args()

    payload = {
        "license_id": a.license_id,
        "licensed_to": a.licensed_to,
        "deployment_id": str(uuid.UUID(a.deployment_id)),
        "max_users": a.max_users,
        "issued_at": _ts(a.issued_at) if a.issued_at else dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": _ts(a.expires) if a.expires else None,
        "grace_days": a.grace_days,
        "on_expiry": a.on_expiry,
    }
    with open(a.key, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(key.sign(f"{PREFIX}.{body}".encode()))  # type: ignore[union-attr]
    token = f"{PREFIX}.{body}.{sig}"
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        print(f"license written to {a.out}")
    else:
        print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
