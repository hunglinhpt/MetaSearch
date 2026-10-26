"""URL canonicalization for dedup (docs/03-search-pipeline.md §3).

Order of operations: strip tracking params → drop fragment → drop ``www.`` →
lowercase host → sort query params.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking params removed before comparing URLs. Anything not listed keeps its
# original position in the query string.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "yclid",
    "twclid",
    "ref",
    "referrer",
    "igshid",
    "mc_cid",
    "mc_eid",
}


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def canonicalize(url: str) -> str:
    """Return a canonical form of ``url`` usable as a dedup key."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "http"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _is_tracking(k)))
    path = parts.path or "/"
    # Keep non-default ports so e.g. :8080 mirrors are not conflated.
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host

    return urlunsplit((scheme, netloc, path.rstrip("/") or "/", query, ""))
