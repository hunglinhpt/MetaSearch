"""robots.txt compliance for the scraper (docs/10-security-compliance.md).

Fetches and caches robots.txt per host for ``robots_cache_ttl_s``. Per RFC 9309,
an unreachable robots.txt means "allow" — we never block on robots fetch errors,
only on an explicit disallow for our user agent group.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.config import get_settings

# stdlib RobotFileParser does network I/O synchronously via urlopen; we feed it
# the body ourselves instead so everything stays inside our httpx client.


@dataclass(slots=True)
class _RobotsEntry:
    parser: RobotFileParser | None
    fetched_at: float


class RobotsCache:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._cache: dict[str, _RobotsEntry] = {}
        self._client = client

    async def allowed(self, url: str, *, user_agent: str) -> bool:
        settings = get_settings()
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        entry = self._cache.get(origin)
        now = time.monotonic()
        if entry is None or now - entry.fetched_at > settings.robots_cache_ttl_s:
            entry = _RobotsEntry(parser=await self._fetch(origin), fetched_at=now)
            self._cache[origin] = entry
        if entry.parser is None:
            return True
        return entry.parser.can_fetch(user_agent, url)

    async def _fetch(self, origin: str) -> RobotFileParser | None:
        """Fetch robots.txt pinned to a validated IP (no redirects followed:
        a 3xx is treated as "no robots" — best-effort compliance, and pinned
        fetching keeps the SSRF guard intact)."""
        settings = get_settings()
        client = self._client
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(follow_redirects=False, timeout=settings.robots_timeout_s)
        try:
            from app.extraction import ssrf
            from app.extraction.pinning import build_pinned_request

            url = f"{origin}/robots.txt"
            ips = await ssrf.resolve_and_check(url)
            pinned_url, headers, extensions = build_pinned_request(url, sorted(ips)[0])
            resp = await client.get(url=pinned_url, headers=headers, extensions=extensions)
            if resp.status_code != 200 or not resp.content:
                return None
            parser = RobotFileParser()
            parser.parse(resp.text.splitlines())
            return parser
        except Exception:
            # Unreachable robots → assume allow (RFC 9309 §2.4).
            return None
        finally:
            if owns:
                await client.aclose()

    def clear(self) -> None:
        self._cache.clear()
