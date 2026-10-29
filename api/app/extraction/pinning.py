"""IP pinning: connect to a validated address instead of re-resolving DNS.

Closes the DNS-rebinding window between ``ssrf.resolve_and_check()`` and the
actual TCP connect (docs/10-security-compliance.md §SSRF): the URL host is
swapped for the validated IP while the ``Host`` header and — for TLS — the
httpcore ``sni_hostname`` extension keep the original identity, so virtual
hosts and certificate verification keep working. Redirects are re-resolved
and re-pinned hop by hop by the scraper.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


class PinningError(Exception):
    """The URL cannot be pinned (unsupported scheme, missing host)."""


def build_pinned_request(url: str, ip: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return ``(pinned_url, extra_headers, extensions)`` for one validated IP.

    - ``pinned_url``: same scheme/path/query, host replaced by the IP literal
      (bracketed for IPv6).
    - ``extra_headers``: ``Host`` set to the original ``host[:port]``.
    - ``extensions``: ``sni_hostname`` for https so TLS SNI + certificate
      verification use the original hostname (httpcore >= 0.16).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise PinningError(f"cannot pin scheme {parts.scheme!r}")
    if not parts.hostname:
        raise PinningError("URL has no hostname")
    if not ip:
        raise PinningError("no validated IP to pin")

    try:
        port = parts.port
    except ValueError:
        port = None
    host = f"[{ip}]" if ":" in ip else ip
    if port is not None:
        host = f"{host}:{port}"
    pinned = urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
    headers = {"Host": parts.netloc}
    extensions: dict[str, str] = {}
    if parts.scheme == "https":
        extensions["sni_hostname"] = parts.hostname
    return pinned, headers, extensions
