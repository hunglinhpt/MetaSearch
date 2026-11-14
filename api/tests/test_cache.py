"""Cache layer tests: CacheClient semantics, key builders, payload codecs."""

from __future__ import annotations

from datetime import UTC, datetime

from app.cache import (
    CacheClient,
    answer_cache_key,
    deserialize_results,
    negative_cache_key,
    page_cache_key,
    search_cache_key,
    serialize_results,
)
from app.retrieval.models import ScoredResult
from app.retrieval.scoring import tokenize


class FakeRedis:
    """Minimal redis.asyncio double (JSON strings in a dict)."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.fail = False

    async def get(self, key: str):
        if self.fail:
            raise ConnectionError("redis down")
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        if self.fail:
            raise ConnectionError("redis down")
        self.data[key] = value
        return True

    async def exists(self, key: str):
        if self.fail:
            raise ConnectionError("redis down")
        return 1 if key in self.data else 0


class TestCacheClient:
    async def test_roundtrip_and_miss(self):
        client = CacheClient(FakeRedis())
        assert await client.get_json("k") is None
        await client.set_json("k", {"a": 1}, ttl_s=60)
        assert await client.get_json("k") == {"a": 1}

    async def test_dead_redis_is_a_miss_not_a_crash(self):
        # Audit round 15: the redis-down marker has a 30s TTL and leaks across
        # tests via the module global — reset it so this test always exercises
        # the fake's raise path (full-suite order-dependent flake). The shared
        # module-level _local_cache leaks the same way (an earlier test's
        # set_json lands there while the marker is up), so clear it too.
        import app.cache as _cache_mod

        _cache_mod._redis_down_until = 0.0
        _cache_mod._local_cache.clear()
        fake = FakeRedis()
        fake.fail = True
        client = CacheClient(fake)
        assert await client.get_json("k") is None
        await client.set_json("k", {"a": 1}, ttl_s=60)  # swallowed
        assert await client.has_flag("f") is False
        await client.set_flag("f", ttl_s=60)  # swallowed

    async def test_disabled_client_is_noop(self):
        client = CacheClient(FakeRedis(), enabled=False)
        assert not client.enabled
        await client.set_json("k", {"a": 1}, ttl_s=60)
        assert await client.get_json("k") is None

    async def test_flags(self):
        client = CacheClient(FakeRedis())
        assert await client.has_flag("nc:x") is False
        await client.set_flag("nc:x", ttl_s=10)
        assert await client.has_flag("nc:x") is True


class TestKeys:
    def test_search_key_varies_with_params(self):
        from app.orchestration.pipeline import SearchParams

        base = search_cache_key("postgres", SearchParams(query="postgres"))
        assert base.startswith("sr:")
        other_k = search_cache_key("postgres", SearchParams(query="postgres", k=5))
        assert base != other_k
        same = search_cache_key("postgres", SearchParams(query="postgres"))
        assert base == same

    def test_other_keys(self):
        assert answer_cache_key("q", model="m", lang="vi").startswith("an:")
        assert page_cache_key("https://x.com").startswith("pg:")
        assert negative_cache_key("https://x.com").startswith("nc:")
        # negative + page keys must not collide for the same URL
        assert page_cache_key("https://x.com") != negative_cache_key("https://x.com")


class TestCodecs:
    def test_result_serialization_roundtrip(self):
        result = ScoredResult(
            url="https://x.com/a",
            canonical_url="https://x.com/a",
            title="T",
            snippet="S",
            domain="x.com",
            adapters=["searxng"],
            engines=["google"],
            consensus=1,
            best_rank=0,
            published_at=datetime(2025, 1, 1, tzinfo=UTC),
            score=0.5,
        )
        rebuilt = deserialize_results(serialize_results([result]))
        assert rebuilt[0].url == result.url
        assert rebuilt[0].published_at == result.published_at
        assert rebuilt[0].score == result.score

    def test_stopwords_still_normalized_via_tokenize(self):
        assert "và" not in tokenize("PostgreSQL và MySQL")
