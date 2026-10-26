"""Process-wide ``httpx.AsyncClient`` shared by adapters, scraper and LLM gateway.

One pooled client keeps connections warm to SearXNG / crawl4ai and avoids
exhausting sockets under fan-out. Created lazily; the app lifespan disposes it.
"""

from __future__ import annotations

import httpx

from app.config import get_settings

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        settings = get_settings()
        _client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            follow_redirects=False,
            timeout=httpx.Timeout(settings.request_timeout_s),
            limits=httpx.Limits(
                max_connections=40,
                max_keepalive_connections=20,
            ),
        )
    return _client


async def dispose_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
