#!/usr/bin/env bash
# DEV / pilot only: generate a self-signed TLS certificate for nginx.
#   ./gen-selfsigned-cert.sh <hostname-or-ip> [extra-SAN ...]   e.g. hr.company.local 192.168.1.20
# Writes deploy/certs/fullchain.pem + privkey.pem (key chmod 600, EC P-256, 825 days) and prints the SPKI pin for CERT_PINS.
# For production use a certificate from your internal CA or a public CA instead (docs/DEPLOY.md).
set -euo pipefail
[[ $# -ge 1 ]] || { echo "usage: $0 <hostname-or-ip> [extra-SAN ...]" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${CERT_DIR:-$HERE/../certs}"
mkdir -p "$OUT"
[[ ! -e "$OUT/privkey.pem" || "${FORCE:-0}" == 1 ]] || { echo "$OUT/privkey.pem exists; set FORCE=1 to overwrite" >&2; exit 1; }

SAN=""
for h in "$@"; do
  if [[ "$h" =~ ^[0-9.]+$ || "$h" == *:* ]]; then t="IP"; else t="DNS"; fi
  SAN+="${SAN:+,}${t}:${h}"
done

umask 077
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 \
  -keyout "$OUT/privkey.pem" -out "$OUT/fullchain.pem" \
  -subj "/CN=$1/O=Employee Connect (self-signed)" \
  -addext "subjectAltName=$SAN" -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=serverAuth"
# nginx runs as uid 101 and must read the key (bind mount keeps host permissions)
chmod 644 "$OUT/fullchain.pem"; chmod 640 "$OUT/privkey.pem"
chgrp 101 "$OUT/privkey.pem" 2>/dev/null || chmod 644 "$OUT/privkey.pem"   # no root: fall back to world-readable (dev only)

echo "wrote $OUT/fullchain.pem and $OUT/privkey.pem"
echo "SPKI pin (add to CERT_PINS):"
"$HERE/spki-pin.sh" "$OUT/fullchain.pem"
