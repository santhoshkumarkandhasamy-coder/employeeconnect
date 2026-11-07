#!/bin/sh
# Host firewall for the attendance devices (plan 18). Run as root after docker is up and at boot (see ec-devices-firewall.service).
#
#   RETURN  conntrack RELATED,ESTABLISHED          replies to anybody (a LAN client of nginx, a device answering the worker)
#   ALLOW   WORKER_EGRESS_IP -> DEVICE_RANGE       tcp/udp on DEVICE_PORTS (default 4370)
#   DROP    every OTHER CONTAINER -> DEVICE_LAN    containers = EGRESS_SUBNET / NET_SUBNET / EDGE_SUBNET, else any docker bridge (-i br-+)
#   DROP    NEW connections to SERVER_DEVICE_IP    (optional) the VM's own address on the device LAN, e.g. 192.168.0.33: a published
#                                                  docker port (80/443) is also reachable there and bypasses ufw INPUT. Ports in
#                                                  SERVER_DEVICE_ALLOW_PORTS (e.g. 443) stay open.
#   RETURN  everything else                        traffic of other hosts is not ours to judge
#
# The chain is built in ONE atomic iptables-restore transaction: a failure leaves the previous chain untouched, and the jump from
# DOCKER-USER is only inserted once a complete chain exists, so there is never an empty chain or a half-built one.
# Variables come from deploy/.env: WORKER_EGRESS_IP (required), DEVICE_LAN (default 192.168.0.0/24),
# DEVICE_RANGE (default 192.168.0.201-192.168.0.203), DEVICE_PORTS (default 4370), EGRESS_SUBNET (default 172.29.250.0/24),
# NET_SUBNET and EDGE_SUBNET (optional extra container networks), SERVER_DEVICE_IP / SERVER_DEVICE_ALLOW_PORTS (optional, see above).
set -eu
WORKER_EGRESS_IP="${WORKER_EGRESS_IP:?set WORKER_EGRESS_IP}"
DEVICE_LAN="${DEVICE_LAN:-192.168.0.0/24}"
DEVICE_RANGE="${DEVICE_RANGE:-192.168.0.201-192.168.0.203}"
DEVICE_PORTS="${DEVICE_PORTS:-${DEVICE_ALLOWED_PORTS:-4370}}"
EGRESS_SUBNET="${EGRESS_SUBNET:-172.29.250.0/24}"
NET_SUBNET="${NET_SUBNET:-}"
EDGE_SUBNET="${EDGE_SUBNET:-}"
SERVER_DEVICE_IP="${SERVER_DEVICE_IP:-}"
SERVER_DEVICE_ALLOW_PORTS="${SERVER_DEVICE_ALLOW_PORTS:-}"
CHAIN=EC-DEVICES
RULES="$(mktemp)"
trap 'rm -f "$RULES"' EXIT

# DOCKER-USER must exist (docker creates it at start); do not guess a chain into existence
iptables -n -L DOCKER-USER >/dev/null 2>&1 || { echo "DOCKER-USER chain not found: start docker first" >&2; exit 1; }

# which sources count as "a container"
SOURCES=""
for net in "$EGRESS_SUBNET" "$NET_SUBNET" "$EDGE_SUBNET"; do
  [ -n "$net" ] && SOURCES="$SOURCES $net"
done

{
  echo "*filter"
  echo ":$CHAIN - [0:0]"                       # declaring the chain flushes it inside the same transaction
  echo "-A $CHAIN -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
  for proto in tcp udp; do
    echo "-A $CHAIN -s $WORKER_EGRESS_IP -d $DEVICE_LAN -p $proto -m multiport --dports $DEVICE_PORTS -m iprange --dst-range $DEVICE_RANGE -j ACCEPT"
  done
  for src in $SOURCES; do
    echo "-A $CHAIN -s $src -d $DEVICE_LAN -j DROP"
  done
  echo "-A $CHAIN -i br-+ -d $DEVICE_LAN -j DROP"   # any other docker bridge network (compose creates br-<id> interfaces)
  if [ -n "$SERVER_DEVICE_IP" ]; then
    for port in $(echo "$SERVER_DEVICE_ALLOW_PORTS" | tr ',' ' '); do
      echo "-A $CHAIN -p tcp -m conntrack --ctorigdst $SERVER_DEVICE_IP --ctorigdstport $port -j RETURN"
    done
    echo "-A $CHAIN -m conntrack --ctstate NEW --ctorigdst $SERVER_DEVICE_IP -j DROP"
  fi
  echo "-A $CHAIN -j RETURN"
  echo "COMMIT"
} >"$RULES"

# --noflush: leave every other chain alone; only $CHAIN is replaced, atomically
iptables-restore --noflush <"$RULES"

# the jump goes in last and only once (a complete chain exists by now)
iptables -C DOCKER-USER -j "$CHAIN" 2>/dev/null || iptables -I DOCKER-USER 1 -j "$CHAIN"
echo "devices firewall applied"
