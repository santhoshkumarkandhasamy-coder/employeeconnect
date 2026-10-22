"""Client-IP resolution. X-Real-IP / X-Forwarded-For are honoured ONLY when the direct peer is in
TRUSTED_PROXIES (CIDR list). nginx must OVERWRITE the header (`proxy_set_header X-Real-IP $remote_addr;`,
never `$proxy_add_x_forwarded_for` appended onto a client-supplied value) and port 8000 must not be published."""

from __future__ import annotations

import ipaddress

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _in(ip: str, nets: list[Network]) -> bool:
    addr = ipaddress.ip_address(ip)
    return any(addr in n for n in nets)


def resolve_client_ip(peer: str | None, x_real_ip: str | None, x_forwarded_for: str | None, trusted: list[Network]) -> str | None:
    peer_ip = parse_ip(peer)
    if peer_ip is None or not trusted or not _in(peer_ip, trusted):
        return peer_ip  # untrusted peer: forwarded headers are attacker-controlled, ignore them
    real = parse_ip(x_real_ip)
    if real:
        return real
    if x_forwarded_for:  # right-most entry that is not one of our own proxies
        for part in reversed(x_forwarded_for.split(",")):
            ip = parse_ip(part)
            if ip is None:
                return peer_ip  # malformed chain: fall back to the proxy address
            if not _in(ip, trusted):
                return ip
    return peer_ip


def rate_limit_key(ip: str | None) -> str | None:
    """Rate-limit bucket for an IP: IPv6 collapses to its /64 so rotating host bits does not bypass limits."""
    if not ip:
        return None
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)
