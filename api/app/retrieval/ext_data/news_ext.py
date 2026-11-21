"""NewsAPI external news (docs/32 §5).

Key-gated: NewsAPI has no keyless tier (the "demo" key serves fixed example
articles — using it would fabricate news, which is forbidden). With
``NEWSAPI_API_KEY`` configured, top-headlines/everything are served with
bounded page sizes and explicit provenance; without a key the endpoint
reports a 503 with setup guidance (MetaSearch's own google_news engine
remains the keyless news path).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.core.singleflight import SingleFlight
from app.http import get_http_client
from app.retrieval.ext_data._base import DEFAULT_TIMEOUT_S, TtlCache

logger = get_logger(__name__)

_NEWSAPI_URL = "https://newsapi.org/v2/{endpoint}"

_Q_RE = re.compile(r"^[\w\s.,'\-():/&+]{1,120}$")
_COUNTRIES = frozenset({"vn", "us", "gb", "jp", "kr", "cn", "de", "fr", "sg", "au", "in", "ca"})
_CATEGORIES = frozenset(
    {"business", "entertainment", "general", "health", "science", "sports", "technology"}
)

news_cache = TtlCache(ttl_s=300.0, max_entries=64)
news_flight = SingleFlight()


def sanitize_query(raw: str | None) -> str | None:
    if raw is None:
        return None
    q = raw.strip()
    return q if q and _Q_RE.fullmatch(q) else None


async def fetch_external_news(
    *,
    q: str | None = None,
    country: str | None = None,
    category: str | None = None,
    page_size: int = 20,
    days: int = 3,
    api_key: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: Any = None,
) -> dict[str, Any] | None:
    """Top headlines (no query) or everything-matching (query). Returns None
    when the key is missing or the upstream fails — callers must distinguish
    via the ``api_key`` argument (missing key → 503 setup hint)."""
    if not api_key:
        return None
    clean_q = sanitize_query(q)
    clean_country = country.strip().lower() if country else None
    if clean_country and clean_country not in _COUNTRIES:
        clean_country = None
    clean_category = category.strip().lower() if category else None
    if clean_category and clean_category not in _CATEGORIES:
        clean_category = None
    page_size = max(1, min(50, page_size))
    days = max(1, min(7, days))

    cache_key = f"news:{clean_q}:{clean_country}:{clean_category}:{page_size}:{days}"
    cached = news_cache.get(cache_key)
    if cached is not None:
        return dict(cached, cached=True)

    async def _load() -> dict[str, Any] | None:
        params: dict[str, Any] = {"pageSize": page_size, "page": 1}
        if clean_q:
            endpoint = "everything"
            params["q"] = clean_q
            params["from"] = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
            params["sortBy"] = "publishedAt"
        else:
            endpoint = "top-headlines"
            if clean_country:
                params["country"] = clean_country
            if clean_category:
                params["category"] = clean_category
        if endpoint == "top-headlines" and not clean_country and not clean_category:
            params["country"] = "us"  # bounded default, never an unbounded call

        try:
            http_cli = client or get_http_client("search")
            resp = await http_cli.get(
                _NEWSAPI_URL.format(endpoint=endpoint),
                params=params,
                headers={"X-Api-Key": api_key},
                timeout=timeout_s,
            )
            if resp.status_code != 200:
                logger.warning("ext_newsapi_failed", status=resp.status_code)
                return None
            data = resp.json()
        except Exception as exc:
            logger.warning("ext_newsapi_error", error=type(exc).__name__)
            return None
        if not isinstance(data, dict) or data.get("status") != "ok":
            return None
        articles = data.get("articles") or []
        if not isinstance(articles, list):
            return None
        items: list[dict[str, Any]] = []
        for art in articles[:page_size]:
            if not isinstance(art, dict):
                continue
            title = str(art.get("title") or "").strip()
            url = str(art.get("url") or "").strip()
            if not title or not url.startswith(("http://", "https://")):
                continue
            items.append(
                {
                    "title": title[:200],
                    "url": url[:500],
                    "source": str((art.get("source") or {}).get("name") or "")[:80],
                    "published_at": str(art.get("publishedAt") or "")[:32],
                    "description": str(art.get("description") or "")[:300],
                    "image_url": str(art.get("urlToImage") or "")[:500] or None,
                }
            )
        if not items:
            return None
        return {
            "endpoint": endpoint,
            "items": items,
            "total_results": min(int(data.get("totalResults") or len(items)), 1000),
            "source_name": "NewsAPI",
            "source_url": "https://newsapi.org",
        }

    result = await news_flight.run(cache_key, _load)
    if result is None:
        return None
    news_cache.put(cache_key, result)
    return dict(result, cached=False)
