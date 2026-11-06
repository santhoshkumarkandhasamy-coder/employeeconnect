#!/bin/sh
# Idempotent host firewall for the attendance devices (plan 18). Run as root after docker is up and at boot.
#   ALLOW  WORKER_EGRESS_IP -> DEVICE_RANGE, tcp/udp 4370
#   DROP   every other container -> DEVICE_LAN
# Variables come from deploy/.env: WORKER_EGRESS_IP (required), DEVICE_LAN (default 192.168.0.0/24),
# DEVICE_RANGE (default 192.168.0.201-192.168.0.203).
set -eu
WORKER_EGRESS_IP="${WORKER_EGRESS_IP:?set WORKER_EGRESS_IP}"
DEVICE_LAN="${DEVICE_LAN:-192.168.0.0/24}"
DEVICE_RANGE="${DEVICE_RANGE:-192.168.0.201-192.168.0.203}"
CHAIN=EC-DEVICES
iptables -N "$CHAIN" 2>/dev/null || true
iptables -F "$CHAIN"
for proto in tcp udp; do
  iptables -A "$CHAIN" -s "$WORKER_EGRESS_IP" -p "$proto" --dport 4370 -m iprange --dst-range "$DEVICE_RANGE" -j ACCEPT
done
iptables -A "$CHAIN" -d "$DEVICE_LAN" -j DROP
iptables -A "$CHAIN" -j RETURN
iptables -C DOCKER-USER -j "$CHAIN" 2>/dev/null || iptables -I DOCKER-USER 1 -j "$CHAIN"
echo "devices firewall applied"
