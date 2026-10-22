"""Vendor PUBLIC keys used to verify license tokens (Ed25519). Public keys only: the private key and the issuing tool
(`tools/license/issue_license.py`) stay with the vendor, offline, and never ship in the image or in `deploy/`.

Each entry is the raw 32-byte public key, base64url without padding (printed by `tools/license/gen_vendor_key.py`).
To rotate: ADD the new key and keep the old one until every license signed with it has been replaced.
While the list is empty no license can be installed (`LICENSE_INVALID`) and the deployment stays on the built-in
unlicensed default (UNLICENSED_MAX_USERS seats, no expiry).
"""

VENDOR_PUBLIC_KEYS: list[str] = []
