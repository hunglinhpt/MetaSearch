"""Two-tier cache (docs/03-search-pipeline.md §7) + negative cache.

    L1 Redis exact:  sr: search results · an: answers · pg: scraped pages
    L2 pgvector:     semantic answer cache, cosine > 0.92 (answer only)
    negative cache:  nc: recently-failed URLs (Redis, local dict as fallback)

Every cache operation degrades gracefully: Redis down -> miss, no crash.
Writes happen fire-and-forget from the pipelines.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import redis.asyncio as aioredis

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.metrics import CACHE_EVENTS
from app.retrieval.models import ScoredResult

logger = get_logger(__name__)


def sha1_key(*parts: Any) -> str:
    joined = "|".join(str(p) for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()  # nosec B324 - cache key, not security


_redis_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis | None:
    """Shared Redis client, or None when unavailable (never raises)."""
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        try:
            _redis_client = aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
        except Exception as exc:  # pragma: no cover - bad URL shape only
            logger.warning("redis_init_failed", error=type(exc).__name__)
            return None
    return _redis_client


async def dispose_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
    _redis_client = None


class CacheClient:
    """Thin JSON/flag wrapper over Redis; a dead Redis is just a cache miss."""

    def __init__(self, client: Any = None, *, enabled: bool = True) -> None:
        self._client = client
        self.enabled = enabled and client is not None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> CacheClient:
        settings = settings or get_settings()
        if not settings.cache_enabled:
            return cls(None, enabled=False)
        return cls(get_redis())

    async def get_json(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        try:
            raw = await self._client.get(key)
        except Exception:
            return None
        if raw is None:
            CACHE_EVENTS.labels(layer="L1", outcome="miss").inc()
            return None
        CACHE_EVENTS.labels(layer="L1", outcome="hit").inc()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def set_json(self, key: str, value: Any, *, ttl_s: int) -> None:
        if not self.enabled:
            return
        try:
            await self._client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_s)
        except Exception as exc:
            logger.warning("cache_set_failed", key=key[:24], error=type(exc).__name__)

    async def has_flag(self, key: str) -> bool:
        if not self.enabled:
            return False
        try:
            return bool(await self._client.exists(key))
        except Exception:
            return False

    async def set_flag(self, key: str, *, ttl_s: int) -> None:
        if not self.enabled:
            return
        try:
            await self._client.set(key, "1", ex=ttl_s)
        except Exception as exc:
            logger.warning("cache_flag_failed", key=key[:24], error=type(exc).__name__)


# --- key builders -----------------------------------------------------------


def search_cache_key(query: str, params: Any) -> str:
    return f"sr:{sha1_key(query, params.lang, params.k, sorted(params.categories), params.freshness_days, params.engines)}"  # noqa: E501


def answer_cache_key(
    query: str,
    *,
    model: str,
    lang: str | None,
    k: int = 10,
    categories: list[str] | None = None,
    freshness_days: int | None = None,
) -> str:
    """Answer key covers the search params that shape the context (docs/03 §7)."""
    return f"an:{sha1_key(query, model, lang, k, sorted(categories or []), freshness_days)}"


def page_cache_key(url: str) -> str:
    return f"pg:{sha1_key(url)}"


def negative_cache_key(url: str) -> str:
    return f"nc:{sha1_key(url)}"


def search_ttl_s(settings: Settings, *, news: bool) -> int:
    return settings.cache_news_ttl_s if news else settings.cache_search_ttl_s


# --- payload codecs (L1 stores domain objects as plain dicts) ----------------


def serialize_results(results: list[ScoredResult]) -> list[dict[str, Any]]:
    return [
        {
            "url": r.url,
            "canonical_url": r.canonical_url,
            "title": r.title,
            "snippet": r.snippet,
            "domain": r.domain,
            "adapters": r.adapters,
            "engines": r.engines,
            "consensus": r.consensus,
            "best_rank": r.best_rank,
            "adapter_score": r.adapter_score,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "score": r.score,
        }
        for r in results
    ]


def deserialize_results(payload: list[dict[str, Any]]) -> list[ScoredResult]:
    from datetime import datetime

    out: list[ScoredResult] = []
    for item in payload:
        published = item.get("published_at")
        out.append(
            ScoredResult(
                url=item["url"],
                canonical_url=item["canonical_url"],
                title=item["title"],
                snippet=item["snippet"],
                domain=item["domain"],
                adapters=list(item.get("adapters") or []),
                engines=list(item.get("engines") or []),
                consensus=int(item.get("consensus") or 0),
                best_rank=int(item.get("best_rank") or 0),
                adapter_score=item.get("adapter_score"),
                published_at=datetime.fromisoformat(published) if published else None,
                score=float(item.get("score") or 0.0),
            )
        )
    return out
