#!/bin/sh
# nginx container entrypoint (POSIX sh, runs as uid 101 on a read-only root fs).
# Renders /etc/nginx/nginx.conf.tpl -> /tmp/nginx.conf from environment variables, then runs nginx in the foreground.
#   PUBLIC_HOSTNAME     required, e.g. hr.company.com (the ONLY Host/SNI that is served)
#   PUBLIC_ORIGIN       required, e.g. https://hr.company.com (browser Origin accepted by /api/auth/web/*)
#   CONSOLE_ALLOW_CIDRS optional, comma separated IPs/CIDRs allowed to reach /console/ and /api/auth/web/ (empty = everyone)
#   RELAY_PEER_IP       optional, WireGuard IP of the VPS relay (option D); enables the PROXY-protocol listener on 8444
#   HTTPS_PORT          optional, host port published for HTTPS (default 443); used in the http -> https redirect
# Reload after a config change:  docker compose exec nginx nginx -c /tmp/nginx.conf -s reload   (edits to .env need `up -d`).
set -eu

# lower-cased: browsers send a lower-case Host / Origin, and nginx compares $host / $http_origin case-sensitively here
HOST=$(printf '%s' "${PUBLIC_HOSTNAME:-}" | tr 'A-Z' 'a-z')
ORIGIN=$(printf '%s' "${PUBLIC_ORIGIN:-}" | tr 'A-Z' 'a-z')
CIDRS="${CONSOLE_ALLOW_CIDRS:-}"
RELAY="${RELAY_PEER_IP:-}"
HTTPS_PORT="${HTTPS_PORT:-443}"

die() { echo "nginx-start: $*" >&2; exit 1; }

case "$HOST" in ''|*[!A-Za-z0-9.-]*|.*|*..*|-*) die "PUBLIC_HOSTNAME must be a plain host name such as hr.company.com" ;; esac
case "$ORIGIN" in
  https://*) ;;
  *) die "PUBLIC_ORIGIN must start with https://" ;;
esac
case "${ORIGIN#https://}" in ''|*[!A-Za-z0-9.:-]*) die "PUBLIC_ORIGIN must be https://host[:port] with no path" ;; esac
case "$RELAY" in *[!0-9.:a-fA-F]*) die "RELAY_PEER_IP must be an IP address" ;; esac
case "$HTTPS_PORT" in ''|*[!0-9]*) die "HTTPS_PORT must be a port number" ;; esac
PORT_SUFFIX=""
[ "$HTTPS_PORT" = "443" ] || PORT_SUFFIX=":$HTTPS_PORT"

# console allow-list -> allow ...; deny all;   (values are validated, never interpolated blindly)
: > /tmp/ec-console-allow.conf
if [ -n "$CIDRS" ]; then
  OLDIFS="$IFS"; IFS=','
  for c in $CIDRS; do
    c=$(printf '%s' "$c" | tr -d ' ')
    [ -n "$c" ] || continue
    case "$c" in *[!0-9.:a-fA-F/]*) die "CONSOLE_ALLOW_CIDRS: bad entry '$c'" ;; esac
    echo "allow $c;" >> /tmp/ec-console-allow.conf
  done
  IFS="$OLDIFS"
  echo "deny all;" >> /tmp/ec-console-allow.conf
fi

# optional PROXY-protocol listener for the VPS relay (option D). Not published unless docker-compose.relay.yml is used.
: > /tmp/ec-relay-reject.conf
: > /tmp/ec-relay-app.conf
if [ -n "$RELAY" ]; then
  echo "listen 8444 ssl default_server proxy_protocol;" > /tmp/ec-relay-reject.conf
  {
    echo "listen 8444 ssl proxy_protocol;"
    echo "set_real_ip_from $RELAY;"
    echo "real_ip_header proxy_protocol;"
  } > /tmp/ec-relay-app.conf
fi

sed -e "s|@@PUBLIC_HOSTNAME@@|$HOST|g" -e "s|@@PUBLIC_ORIGIN@@|$ORIGIN|g" -e "s|@@HTTPS_PORT_SUFFIX@@|$PORT_SUFFIX|g" \
    /etc/nginx/nginx.conf.tpl > /tmp/nginx.conf

nginx -t -c /tmp/nginx.conf
exec nginx -c /tmp/nginx.conf -g 'daemon off;'
