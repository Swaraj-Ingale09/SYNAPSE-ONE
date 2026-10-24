"""SSRF defenses for outbound HTTP (OWASP SSRF Prevention Cheat Sheet).

Every outbound request must pass two gates:

1. ``validate_public_url`` — static validation of the URL itself (scheme,
   userinfo, port) plus DNS resolution with per-IP validation. All resolved
   addresses must be public; a hostname with even one private answer is
   rejected outright (defends against round-robin rebinding).
2. Per-hop revalidation — the fetcher disables automatic redirects and calls
   this module again for every redirect target, so a public site cannot bounce
   us into 169.254.169.254 or localhost.

Residual risk (documented honestly in ADR 0004): between validation and the
TLS connect, a hostile authoritative DNS server could re-resolve the name to a
private address (classic DNS rebinding TOCTOU). For a loopback-bound,
single-user local tool this is an acceptable residual; the mitigation layers
(block redirects, block userinfo, narrow port allowlist, fail-closed on any
resolution error) make exploitation require a deliberately hostile target the
user chose to fetch.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit


class EgressBlocked(ValueError):
    """The requested URL or address must not be fetched."""


# Explicit blocked networks (precise error messages; is_global subsumes most).
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT
        "127.0.0.0/8",
        "169.254.0.0/16",  # link-local incl. cloud metadata 169.254.169.254
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved + broadcast
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local v6
        "ff00::/8",  # multicast v6
        "2001:db8::/32",
    )
)

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})


def _blocked_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved:
        return "reserved"
    if ip.is_unspecified:
        return "unspecified"
    for network in _BLOCKED_NETWORKS:
        if ip in network:
            return f"blocked network {network}"
    if not ip.is_global:
        return "non-global"
    return None


def assert_safe_ip(ip_text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Raise EgressBlocked unless this literal address is publicly routable."""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError as exc:
        raise EgressBlocked(f"invalid IP address: {ip_text!r}") from exc
    reason = _blocked_reason(ip)
    if reason is not None:
        raise EgressBlocked(f"address {ip} is not publicly routable ({reason})")
    return ip


def validate_url_parts(parts: SplitResult) -> str:
    """Static URL checks: scheme, userinfo, port. Returns the hostname."""
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise EgressBlocked(f"scheme {parts.scheme!r} is not allowed (only http/https)")
    if not parts.hostname:
        raise EgressBlocked("URL has no hostname")
    if parts.username or parts.password:
        raise EgressBlocked("credentials embedded in URL are not allowed")
    if parts.port is not None and parts.port not in ALLOWED_PORTS:
        raise EgressBlocked(f"port {parts.port} is not allowed (only 80 and 443)")
    return parts.hostname


def resolve_and_validate(hostname: str, port: int) -> list[str]:
    """Resolve a hostname and require every answer to be publicly routable.

    Fails closed: any resolution error is a hard block, never a silent pass.
    """
    target_port = port
    try:
        answers = socket.getaddrinfo(hostname, target_port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressBlocked(f"cannot resolve host {hostname!r}: {exc}") from exc
    if not answers:
        raise EgressBlocked(f"host {hostname!r} resolved to no addresses")

    validated: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in answers:
        ip_text = sockaddr[0].split("%", 1)[0]  # strip IPv6 zone id
        assert_safe_ip(ip_text)  # raises on the FIRST unsafe answer
        validated.append(ip_text)
    return validated


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    parts: SplitResult
    hostname: str
    addresses: tuple[str, ...]


def validate_public_url(url: str) -> ValidatedTarget:
    """Full gate for an outbound URL: static checks + DNS + per-IP validation."""
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise EgressBlocked(f"malformed URL: {exc}") from exc
    hostname = validate_url_parts(parts)
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    addresses = tuple(resolve_and_validate(hostname, port))
    return ValidatedTarget(url=url, parts=parts, hostname=hostname, addresses=addresses)
