"""SSRF guard v1 (docs/03-search-pipeline.md §4, docs/10-security-compliance.md).

Checks before any outbound fetch:
1. Only http/https; no userinfo in the URL.
2. Only allowed ports (80, 443, 8080, 8443).
3. Resolve DNS and reject private/loopback/link-local/reserved/metadata IPs,
   including CGNAT 100.64.0.0/10 and IPv4-mapped IPv6 addresses.
4. Returns the validated IP set so callers can log it; connection pinning and
   redirect re-check hardening land in Phase 4 (docs/12-roadmap-plan.md).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_PORTS: set[int] = {80, 443, 8080, 8443}


class SSRFViolation(Exception):
    """The URL targets a forbidden host and must not be fetched."""


def _assert_safe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_reserved
    ):
        raise SSRFViolation(f"refusing to fetch non-global/reserved address {ip}")


async def resolve_and_check(url: str) -> frozenset[str]:
    """Validate ``url`` and return its resolved (safe) IP addresses.

    Raises ``SSRFViolation`` for anything that must not leave the box.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SSRFViolation(f"scheme {parts.scheme!r} not allowed")
    if parts.username or parts.password:
        raise SSRFViolation("userinfo in URL not allowed")
    hostname = parts.hostname
    if not hostname:
        raise SSRFViolation("URL has no hostname")

    port = parts.port
    if port is not None and port not in ALLOWED_PORTS:
        raise SSRFViolation(f"port {port} not allowed (only {sorted(ALLOWED_PORTS)})")

    # A literal IP in the hostname skips DNS.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        _assert_safe_ip(literal)
        return frozenset({str(literal)})

    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(hostname, parts.port or None, proto=socket.IPPROTO_TCP),
            timeout=5.0,
        )
    except TimeoutError as exc:
        raise SSRFViolation(f"DNS resolution timeout for {hostname}") from exc
    except socket.gaierror as exc:
        raise SSRFViolation(f"DNS resolution failed for {hostname}") from exc

    addresses: set[str] = set()
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:  # pragma: no cover - getaddrinfo returns IP literals
            continue
        _assert_safe_ip(ip)
        addresses.add(str(ip))

    if not addresses:
        raise SSRFViolation(f"no addresses resolved for {hostname}")
    return frozenset(addresses)
