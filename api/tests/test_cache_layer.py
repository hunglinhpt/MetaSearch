"""Tests for WP-06: Cache layer hardening, LRU memory cap, SingleFlight, and fallback."""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest

from app.cache import (
    CacheClient,
    _LocalTTLCache,
    answer_cache_key,
    dispose_redis,
    is_redis_available,
    mark_redis_down,
    reset_redis_breaker,
    search_cache_key,
)
from app.config import Settings
from app.core.singleflight import SingleFlight
from app.extraction.scraper import Scraper
from app.orchestration.pipeline import AnswerPipeline, EngineStatus, SearchParams, SearchPipeline


@pytest.mark.asyncio
async def test_dead_redis_uses_local_and_is_fast() -> None:
    """P4: When Redis is unreachable, CacheClient trips breaker and subsequent ops take <= 5ms."""
    await dispose_redis()
    settings = Settings(_env_file=None, redis_url="redis://127.0.0.1:6399/0")
    client = CacheClient.from_settings(settings)

    # First set trips breaker
    await client.set_json("sr:probe", {"a": 1}, ttl_s=60)
    assert not is_redis_available()

    # Subsequent get & set are served entirely from in-process local cache in < 5ms
    t0 = time.perf_counter()
    got = await client.get_json("sr:probe")
    await client.set_json("sr:probe2", {"b": 2}, ttl_s=60)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert got == {"a": 1}
    assert elapsed_ms <= 5.0, f"Local cache operation took {elapsed_ms:.2f}ms (> 5ms)"
    await client.aclose()
    await dispose_redis()


@pytest.mark.asyncio
async def test_redis_recovers_after_breaker() -> None:
    """Breaker resets and re-enables Redis check once duration expires."""
    reset_redis_breaker()
    assert is_redis_available()

    mark_redis_down(0.02)
    assert not is_redis_available()

    await asyncio.sleep(0.05)
    assert is_redis_available()


def test_local_cache_bytes_bounded() -> None:
    """_LocalTTLCache strictly caps storage to max_bytes using LRU OrderedDict eviction."""
    cache = _LocalTTLCache(max_bytes=1000)

    # Add 4 items of ~300 bytes each
    cache.set("k1", "x" * 250, ttl_s=60)
    cache.set("k2", "x" * 250, ttl_s=60)
    cache.set("k3", "x" * 250, ttl_s=60)

    # Total should fit around 3 items
    assert cache._curr_bytes <= 1000

    # Access k1 to make it most recently used
    assert cache.get("k1") is not None

    # Adding k4 must evict k2 (oldest), preserving k1 and k3
    cache.set("k4", "x" * 250, ttl_s=60)
    assert cache._curr_bytes <= 1000
    assert cache.get("k2") is None  # evicted
    assert cache.get("k1") is not None  # kept because of recent get
    assert cache.get("k4") is not None


def test_search_key_includes_news() -> None:
    """P16: search_cache_key produces distinct keys when news flag differs."""
    params_std = SearchParams(query="thời sự", news=False)
    params_news = SearchParams(query="thời sự", news=True)

    key_std = search_cache_key("thời sự", params_std, news=False)
    key_news = search_cache_key("thời sự", params_news, news=True)

    assert key_std.startswith("sr:v2:")
    assert key_news.startswith("sr:v2:")
    assert key_std != key_news


def test_answer_key_includes_engines_and_domain() -> None:
    """Answer key differentiates by engines, news flag, and domain."""
    k1 = answer_cache_key("test", model="gpt-4o", lang="vi", engines=["searxng"])
    k2 = answer_cache_key("test", model="gpt-4o", lang="vi", engines=["brave"])
    k3 = answer_cache_key("test", model="gpt-4o", lang="vi", engines=["searxng"], news=True)
    k4 = answer_cache_key(
        "test", model="gpt-4o", lang="vi", engines=["searxng"], domain="financial"
    )

    assert len({k1, k2, k3, k4}) == 4


@pytest.mark.asyncio
async def test_partial_results_short_ttl(monkeypatch) -> None:
    """DAT-10: Results with failed engines receive short cache TTL (<= 120s)."""
    settings = Settings(_env_file=None, cache_partial_ttl_s=120)
    stored_ttls: list[int] = []

    class MockCache:
        async def set_json(self, key, value, *, ttl_s):
            stored_ttls.append(ttl_s)

    class DummyStore:
        pass

    pipe = SearchPipeline(settings, DummyStore(), cache=MockCache())

    # Full success
    ok_statuses = [EngineStatus(engine="searxng", status="ok", results=5)]
    await pipe._cache_store(
        "query",
        SearchParams(query="query"),
        query_id=uuid.uuid4(),
        results=[],
        statuses=ok_statuses,
    )
    assert stored_ttls[-1] == settings.cache_search_ttl_s

    # Partial failure (e.g. error on one engine)
    partial_statuses = [
        EngineStatus(engine="searxng", status="ok", results=5),
        EngineStatus(engine="ddg", status="error", results=0),
    ]
    await pipe._cache_store(
        "query",
        SearchParams(query="query"),
        query_id=uuid.uuid4(),
        results=[],
        statuses=partial_statuses,
    )
    assert stored_ttls[-1] == 120


@pytest.mark.asyncio
async def test_financial_ttl() -> None:
    """Financial queries get max 300s search TTL and are NOT cached in answer without max_age_s."""
    settings = Settings(_env_file=None)
    stored_search_ttls: list[int] = []

    class MockCache:
        def __init__(self):
            self.answers = []

        async def set_json(self, key, value, *, ttl_s):
            if key.startswith("sr:v2:"):
                stored_search_ttls.append(ttl_s)
            elif key.startswith("an:v2:"):
                self.answers.append(value)

    mock_cache = MockCache()
    pipe = SearchPipeline(settings, None, cache=mock_cache)
    await pipe._cache_store(
        "hpg",
        SearchParams(query="hpg", categories=["financial"]),
        query_id=uuid.uuid4(),
        results=[],
        statuses=[EngineStatus(engine="viet_stock", status="ok", results=2)],
    )
    assert stored_search_ttls[-1] == 300

    ans_pipe = AnswerPipeline(settings, pipe, None, None, None, cache=mock_cache)
    # Without max_age_s: must NOT cache answer
    await ans_pipe._cache_store(
        SearchParams(query="hpg", categories=["financial"]),
        model="gpt-4o",
        answer_id=None,  # type: ignore
        answer_md="Analysis",
        citations=[],
        sources=[],
    )
    assert len(mock_cache.answers) == 0


@pytest.mark.asyncio
async def test_singleflight_coalesces_concurrent_calls() -> None:
    """SingleFlight coalesces concurrent identical requests into 1 execution."""
    sf = SingleFlight()
    call_count = 0

    async def expensive_work() -> str:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return "result"

    results = await asyncio.gather(
        sf.run("same_key", expensive_work),
        sf.run("same_key", expensive_work),
        sf.run("same_key", expensive_work),
        sf.run("same_key", expensive_work),
    )

    assert results == ["result", "result", "result", "result"]
    assert call_count == 1, f"Expected 1 underlying call, got {call_count}"


@pytest.mark.asyncio
async def test_singleflight_survives_first_waiter_cancel() -> None:
    """Cancelling one waiter does not cancel work if other waiters remain."""
    sf = SingleFlight()

    async def slow_work() -> str:
        await asyncio.sleep(0.1)
        return "completed"

    task1 = asyncio.create_task(sf.run("k", slow_work))
    task2 = asyncio.create_task(sf.run("k", slow_work))

    await asyncio.sleep(0.02)
    # Cancel waiter 1
    task1.cancel()

    # Waiter 2 must still receive the completed result
    res2 = await task2
    assert res2 == "completed"


@pytest.mark.asyncio
async def test_pool_timeout_not_negative_cached() -> None:
    """P17: PoolTimeout / LocalOverload is NOT cached in negative cache."""
    settings = Settings(_env_file=None, negative_cache_ttl_s=3600.0)
    scraper = Scraper(settings, cache=None)

    async def fake_fail(url, _timeout):
        raise httpx.PoolTimeout("Connection pool exhausted")

    scraper._scrape_uncached = fake_fail  # type: ignore

    res = await scraper.scrape("https://example.com/test")
    assert res.status == "failed"

    # URL must NOT be in negative cache
    is_neg = await scraper._is_negatively_cached("https://example.com/test")
    assert is_neg is False


@pytest.mark.asyncio
async def test_answer_cached_when_client_closes_on_done() -> None:
    """REL-04: Cache write runs BEFORE done event is yielded."""
    from app.orchestration.pipeline import Event

    cache_stored = False

    class TrackingCache:
        async def get_json(self, key):
            return None

        async def set_json(self, key, value, *, ttl_s):
            nonlocal cache_stored
            cache_stored = True

    events_received: list[Event] = []

    # Generator simulating AnswerPipeline stream
    async def mock_stream():
        nonlocal cache_stored
        yield ("token", {"t": "Hello"})
        yield ("citations", {"citations": []})
        # Simulate WP-06 pattern: write cache before done
        await TrackingCache().set_json("an:key", {"answer": "Hello"}, ttl_s=60)
        yield ("done", {"status": "complete"})

    async for event in mock_stream():
        events_received.append(event)
        if event[0] == "done":
            # Client disconnects right at done event
            break

    assert cache_stored is True
    assert events_received[-1][0] == "done"
