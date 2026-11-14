"""Two-tier cache (docs/03-search-pipeline.md §7) + negative cache.

    L1 Redis exact:  v2:sr: search results · v2:an: answers · v2:pg: scraped pages
    L2 pgvector:     semantic answer cache, cosine > 0.92 (answer only)
    negative cache:  v2:nc: recently-failed URLs (Redis, local LRU as fallback)

Every cache operation degrades gracefully: Redis down -> circuit breaker trips 30s,
immediate in-process fallback with zero stall. Writes happen safely from the pipelines.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

import redis.asyncio as aioredis

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.retrieval.models import ScoredResult

logger = get_logger(__name__)


def sha1_key(*parts: Any) -> str:
    joined = "|".join(str(p) for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()  # nosec B324 - cache key, not security


_redis_client: aioredis.Redis | None = None
_redis_down_until: float = 0.0


def is_redis_available() -> bool:
    """Return True if Redis is not in circuit-breaker open state."""
    global _redis_down_until
    return time.monotonic() >= _redis_down_until


def mark_redis_down(duration_s: float = 30.0) -> None:
    """Trip the Redis circuit breaker for duration_s seconds."""
    global _redis_down_until
    _redis_down_until = time.monotonic() + duration_s
    logger.warning("redis_circuit_breaker_tripped", duration_s=duration_s)


def reset_redis_breaker() -> None:
    """Reset the Redis circuit breaker (for tests)."""
    global _redis_down_until
    _redis_down_until = 0.0


def get_redis() -> aioredis.Redis | None:
    """Shared Redis client, or None when unavailable or down (never raises)."""
    global _redis_client
    if not is_redis_available():
        return None
    url = (get_settings().redis_url or "").strip()
    if not url:
        return None
    if _redis_client is None:
        try:
            _redis_client = aioredis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=0.25,
                socket_timeout=0.5,
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("redis_init_failed", error=type(exc).__name__)
            mark_redis_down()
            return None
    return _redis_client


async def dispose_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        with contextlib.suppress(Exception):
            await _redis_client.aclose()
    _redis_client = None
    reset_redis_breaker()


class _LocalTTLCache:
    """Bounded in-process TTL + LRU cache with byte-level eviction."""

    def __init__(self, max_bytes: int = 64 * 1024 * 1024) -> None:
        self._data: OrderedDict[str, tuple[float, int, Any]] = OrderedDict()
        self._max_bytes = max_bytes
        self._curr_bytes = 0

    def _estimate_size(self, key: str, value: Any) -> int:
        size = len(key.encode("utf-8")) + 64
        if isinstance(value, (str, bytes)):
            size += len(value)
        elif isinstance(value, (int, float, bool)):
            size += 16
        else:
            try:
                size += len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            except Exception:
                size += 512
        return size

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, size_bytes, value = entry
        if time.monotonic() > expires_at:
            self._curr_bytes -= size_bytes
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any, ttl_s: int | float) -> None:
        if key in self._data:
            _, old_size, _ = self._data.pop(key)
            self._curr_bytes -= old_size

        entry_size = self._estimate_size(key, value)
        if entry_size > self._max_bytes:
            return

        now = time.monotonic()
        expired_keys = [k for k, (exp, size, _) in self._data.items() if now > exp]
        for k in expired_keys:
            _, sz, _ = self._data.pop(k)
            self._curr_bytes -= sz

        while self._data and (self._curr_bytes + entry_size > self._max_bytes):
            _, (_, sz, _) = self._data.popitem(last=False)
            self._curr_bytes -= sz

        self._data[key] = (now + ttl_s, entry_size, value)
        self._curr_bytes += entry_size

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def clear(self) -> None:
        self._data.clear()
        self._curr_bytes = 0


_local_cache = _LocalTTLCache()


class CacheClient:
    """Thin JSON/flag wrapper: Redis first, in-process TTL cache fallback."""

    def __init__(self, client: Any = None, *, enabled: bool = True) -> None:
        self._client = client
        self.enabled = enabled

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> CacheClient:
        s = settings or get_settings()
        if not s.cache_enabled:
            return cls(None, enabled=False)
        url = (s.redis_url or "").strip()
        if not url:
            return cls(None)
        if settings is not None:
            try:
                c = aioredis.from_url(
                    url,
                    decode_responses=True,
                    socket_connect_timeout=0.25,
                    socket_timeout=0.5,
                )
                return cls(c)
            except Exception:
                mark_redis_down()
                return cls(None)
        return cls(get_redis())

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            with contextlib.suppress(Exception):
                await self._client.aclose()

    async def get_json(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        if self._client is not None and is_redis_available():
            try:
                raw = await self._client.get(key)
                if raw is not None:
                    return json.loads(raw)
            except Exception as exc:
                logger.warning(
                    "redis_get_failed_fallback_local", key=key[:24], error=type(exc).__name__
                )
                mark_redis_down()
        # In-process fallback
        value = _local_cache.get(key)
        return value

    async def set_json(self, key: str, value: Any, *, ttl_s: int) -> None:
        if not self.enabled:
            return
        written_to_redis = False
        if self._client is not None and is_redis_available():
            # Serialize OUTSIDE the transport try: a json.dumps TypeError is a
            # caller bug, not a Redis outage — it must not trip the shared
            # breaker for 30s (audit round 10).
            try:
                payload = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                logger.warning("cache_serialize_failed", key=key[:24], error=type(exc).__name__)
                return
            try:
                await self._client.set(key, payload, ex=ttl_s)
                written_to_redis = True
            except Exception as exc:
                logger.warning(
                    "redis_set_failed_fallback_local", key=key[:24], error=type(exc).__name__
                )
                mark_redis_down()
        if not written_to_redis:
            _local_cache.set(key, value, ttl_s)

    async def has_flag(self, key: str) -> bool:
        if not self.enabled:
            return False
        if self._client is not None and is_redis_available():
            try:
                return bool(await self._client.exists(key))
            except Exception:
                mark_redis_down()
        return _local_cache.exists(key)

    async def set_flag(self, key: str, *, ttl_s: int) -> None:
        if not self.enabled:
            return
        written = False
        if self._client is not None and is_redis_available():
            try:
                await self._client.set(key, "1", ex=ttl_s)
                written = True
            except Exception:
                mark_redis_down()
        if not written:
            _local_cache.set(key, 1, ttl_s)


# --- key builders (v2 with complete feature coverage) -----------------------


def search_cache_key(query: str, params: Any, *, news: bool = False) -> str:
    engines_part = sorted(params.engines) if getattr(params, "engines", None) else []
    h = sha1_key(
        query,
        params.lang,
        params.k,
        sorted(params.categories),
        params.freshness_days,
        engines_part,
        news,
        getattr(params, "deep", False),
        # v2.5: per-key boosts + expansion variants change the result set,
        # so their signatures must be part of the key (no cross-key leakage).
        getattr(params, "boosts_sig", ""),
        getattr(params, "expansion_sig", ""),
        # v3.0 P2-4: interleaving variant must not share a cache entry.
        getattr(params, "interleave_sig", ""),
    )
    return f"sr:v3:{h}"


def answer_cache_key(
    query: str,
    *,
    model: str,
    lang: str | None,
    k: int = 10,
    categories: list[str] | None = None,
    freshness_days: int | None = None,
    engines: list[str] | None = None,
    news: bool = False,
    domain: str | None = None,
    deep: bool = False,
    personalization: str = "",
) -> str:
    """Answer key covers the search params that shape the context (docs/03 §7).

    v2.5: ``personalization`` folds the per-key boosts signature in, so a boosted
    key never replays another key's cached answer."""
    engines_part = sorted(engines) if engines else []
    h = sha1_key(
        query,
        model,
        lang,
        k,
        sorted(categories or []),
        freshness_days,
        engines_part,
        news,
        domain,
        deep,
        personalization,
    )
    return f"an:v3:{h}"


def page_cache_key(url: str) -> str:
    return f"pg:v2:{sha1_key(url)}"


def negative_cache_key(url: str) -> str:
    return f"nc:v2:{sha1_key(url)}"


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
            "source_url": r.source_url,
            "source_name": r.source_name,
            "as_of": r.as_of.isoformat() if r.as_of else None,
            "data_kind": r.data_kind,
            "domain_tier": r.domain_tier,
        }
        for r in results
    ]


def deserialize_results(payload: list[dict[str, Any]]) -> list[ScoredResult]:
    from datetime import datetime

    out: list[ScoredResult] = []
    for item in payload:
        published = item.get("published_at")
        as_of = item.get("as_of")
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
                source_url=item.get("source_url"),
                source_name=item.get("source_name"),
                as_of=datetime.fromisoformat(as_of) if as_of else None,
                data_kind=item.get("data_kind"),
                domain_tier=item.get("domain_tier") or "unknown",
            )
        )
    return out
