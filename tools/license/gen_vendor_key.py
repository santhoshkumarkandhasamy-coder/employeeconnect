#!/usr/bin/env python
"""VENDOR-SIDE, run offline. Generates the Ed25519 key pair used to sign Employee Connect licenses.

    python gen_vendor_key.py [--out vendor_private.key]

* Writes the PRIVATE key (PEM, mode 600) to --out. Keep it offline and backed up; NEVER commit it or copy it to a server
  or into deploy/. Anyone holding it can mint licenses.
* Prints the PUBLIC key (raw 32 bytes, base64url). Paste it into backend/app/core/license_keys.py (VENDOR_PUBLIC_KEYS).
  To rotate, add the new public key and keep the old one until every license signed with it has been replaced.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="vendor_private.key")
    args = ap.parse_args()
    if os.path.exists(args.out):
        print(f"refusing to overwrite {args.out}", file=sys.stderr)
        return 1
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print(f"private key written to {args.out} (keep it offline)")
    print("public key (add to backend/app/core/license_keys.py VENDOR_PUBLIC_KEYS):")
    print(base64.urlsafe_b64encode(pub).rstrip(b"=").decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
