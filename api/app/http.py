"""Process-wide ``httpx.AsyncClient`` instances shared by adapters, scraper, LLM and internal jobs.

Multiple named pools keep connections isolated (WP-04):
- "search": adapters querying search backends (SearXNG, Tavily, Wikipedia, etc.)
- "scrape": web scraper pinned fetching (Connection: close, keepalive 0)
- "llm": LLM gateway streaming calls
- "internal": background tasks, embeddings, rerankers, health probes
"""

from __future__ import annotations

import contextlib
import ssl
from typing import Literal

import httpx

from app.config import get_settings

PoolName = Literal["search", "scrape", "llm", "internal"]

_LIMITS: dict[PoolName, httpx.Limits] = {
    "search": httpx.Limits(max_connections=64, max_keepalive_connections=32),
    "scrape": httpx.Limits(max_connections=48, max_keepalive_connections=0),
    "llm": httpx.Limits(max_connections=32, max_keepalive_connections=16),
    "internal": httpx.Limits(max_connections=16, max_keepalive_connections=8),
}

_ssl_context: ssl.SSLContext | None = None
_pools: dict[PoolName, httpx.AsyncClient] = {}


def get_ssl_context() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is None:
        settings = get_settings()
        trust_store = getattr(settings, "tls_trust_store", "certifi")
        if trust_store == "system":
            try:
                import truststore

                _ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except (ImportError, AttributeError):
                _ssl_context = ssl.create_default_context()
        else:
            try:
                import certifi

                _ssl_context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                _ssl_context = ssl.create_default_context()
        # Verify must always be enabled. Never verify=False.
        _ssl_context.verify_mode = ssl.CERT_REQUIRED
        _ssl_context.check_hostname = True
    return _ssl_context


def get_http_client(pool: PoolName = "internal") -> httpx.AsyncClient:
    global _pools
    client = _pools.get(pool)
    is_closed = getattr(client, "is_closed", False)
    if client is None or is_closed:
        settings = get_settings()
        limits = _LIMITS.get(pool, _LIMITS["internal"])
        ctx = get_ssl_context()
        # pool=1.0 ensures fast failure on pool exhaustion
        timeout = httpx.Timeout(settings.request_timeout_s, pool=1.0)
        _pools[pool] = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            follow_redirects=False,
            timeout=timeout,
            limits=limits,
            verify=ctx,
        )
    return _pools[pool]


async def dispose_http_client() -> None:
    global _pools
    for client in list(_pools.values()):
        if getattr(client, "is_closed", False) is False and hasattr(client, "aclose"):
            with contextlib.suppress(Exception):
                await client.aclose()
    _pools.clear()
