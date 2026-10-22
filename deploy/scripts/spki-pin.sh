#!/usr/bin/env bash
# Print the base64 SHA-256 of the SubjectPublicKeyInfo (the value the mobile app expects in CERT_PINS).
#   spki-pin.sh cert.pem            first certificate in the file (leaf)
#   spki-pin.sh --n 2 chain.pem     N-th certificate in a bundle (2 = usually the intermediate)
#   spki-pin.sh --key private.pem   pin of a private key (use for the OFFLINE backup key)
#   spki-pin.sh --host hr.company.local[:443]   fetch the live leaf cert over TLS
#   spki-pin.sh --gen-backup out.key            create a spare EC key, save it, print its pin
set -euo pipefail
der_pin() { openssl dgst -sha256 -binary | openssl base64 -A; echo; }
N=1
case "${1:-}" in
  --n)    N="$2"; shift 2 ;;
  --key)  openssl pkey -in "$2" -pubout -outform der | der_pin; exit ;;
  --host) h="$2"; [[ "$h" == *:* ]] || h="$h:443"
          openssl s_client -connect "$h" -servername "${h%%:*}" </dev/null 2>/dev/null | openssl x509 -pubkey -noout \
            | openssl pkey -pubin -outform der | der_pin; exit ;;
  --gen-backup) umask 077; openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out "$2"
          echo "spare key saved to $2 - store it OFFLINE; pin:" >&2
          openssl pkey -in "$2" -pubout -outform der | der_pin; exit ;;
esac
F="${1:?usage: spki-pin.sh [--n N] <cert.pem> | --key <key.pem> | --host <host[:port]> | --gen-backup <file>}"
awk -v n="$N" '/BEGIN CERTIFICATE/{c++} c==n{print} /END CERTIFICATE/&&c==n{exit}' "$F" \
  | openssl x509 -pubkey -noout | openssl pkey -pubin -outform der | der_pin
