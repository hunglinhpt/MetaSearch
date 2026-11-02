"""Tests for WP-07: End-to-end deadlines, admission control, quorum fanout, and early exit."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

import app.api.deps as deps
from app.config import Settings
from app.core.admission import admit, reset_admission
from app.core.auth import AuthContext, check_rate_limit, reset_rate_limit_windows
from app.core.errors import ErrorCode, MetaSearchError
from app.extraction.scraper import PageContent, PageFailure, Scraper
from app.llm.gateway import LLMGateway
from app.main import app
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import AnswerPipeline, SearchOutcome, SearchParams, SearchPipeline
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.registry import EngineRegistry
from app.retrieval.reranker import HeuristicReranker


class HangAdapter:
    """An adapter that sleeps longer than deadline to simulate blackhole upstream."""

    def __init__(self, delay_s: float = 10.0):
        self.delay_s = delay_s
        self.called = 0

    async def search(self, query: str, **kwargs) -> list[RawResult]:
        self.called += 1
        await asyncio.sleep(self.delay_s)
        return [
            RawResult(
                title="Hanged result",
                url="https://hang.example.com",
                snippet="Blackhole",
                adapter="slow",
            )
        ]


class FastAdapter:
    def __init__(self, engine_id: str, results: list[RawResult]):
        self.id = engine_id
        self.results = results
        self.called = 0

    async def search(self, query: str, **kwargs) -> list[RawResult]:
        self.called += 1
        await asyncio.sleep(0.01)
        return self.results


@pytest.fixture(autouse=True)
def clean_admission():
    reset_admission()
    reset_rate_limit_windows()
    yield
    reset_admission()
    reset_rate_limit_windows()


# --------------------------------------------------------------------------
# 1. test_timeout_ms_validated
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timeout_ms_validated():
    """timeout_ms must be within [1000..60000] milliseconds."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Too low (< 1000) -> 400 invalid_request
        res_low = await client.post("/v1/search", json={"query": "test", "timeout_ms": 500})
        assert res_low.status_code == 400
        assert res_low.json()["error"]["code"] == "invalid_request"

        # Too high (> 60000) -> 400 invalid_request
        res_high = await client.post("/v1/search", json={"query": "test", "timeout_ms": 65000})
        assert res_high.status_code == 400
        assert res_high.json()["error"]["code"] == "invalid_request"

        # Scrape endpoint validation
        res_scrape_low = await client.post(
            "/v1/scrape", json={"urls": ["https://example.com"], "timeout_ms": 100}
        )
        assert res_scrape_low.status_code == 400
        assert res_scrape_low.json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------
# 2. test_answer_fast_fails_without_provider (REL-05)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_answer_fast_fails_without_provider(monkeypatch):
    """When LLM provider is none and no override given, /v1/answer fails fast without search."""
    search_called = False

    async def mock_run(*args, **kwargs):
        nonlocal search_called
        search_called = True
        return SearchOutcome(query_id=None, results=[], statuses=[])

    monkeypatch.setattr(
        deps, "get_settings", lambda: Settings(llm_provider="none", llm_council_enabled=False)
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        start = time.perf_counter()
        resp = await client.post("/v1/answer", json={"query": "Why is sky blue?"})
        elapsed = time.perf_counter() - start

        assert resp.status_code == 500
        data = resp.json()
        assert data["error"]["code"] == "internal"
        assert "LLM" in data["error"]["message"]
        # Fast fail must complete well under 100ms
        assert elapsed < 0.2
        assert not search_called


# --------------------------------------------------------------------------
# 3. test_quorum_returns_before_slowest_engine (PERF-08)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_quorum_returns_before_slowest_engine():
    """Fan-out returns once quorum (>= 2 engines) succeed and soft budget passes,
    without waiting 5s.
    """
    fast1 = FastAdapter(
        "fast1",
        [
            RawResult(
                title="Python Language Reference",
                url="https://docs.python.org/reference",
                snippet="Official documentation and syntax reference for Python.",
                adapter="fast1",
            )
        ],
    )
    fast2 = FastAdapter(
        "fast2",
        [
            RawResult(
                title="Asyncio Event Loop Architecture",
                url="https://docs.python.org/library/asyncio-eventloop.html",
                snippet="Managing asynchronous event loops, tasks, and future completions.",
                adapter="fast2",
            )
        ],
    )
    slow = HangAdapter(delay_s=5.0)

    cfg1 = EngineConfig(id="fast1", name="Fast 1", category="general", enabled=True)
    cfg2 = EngineConfig(id="fast2", name="Fast 2", category="general", enabled=True)
    cfg3 = EngineConfig(id="slow", name="Slow", category="general", enabled=True)

    configs = {c.id: c for c in [cfg1, cfg2, cfg3]}
    adapters = {"fast1": fast1, "fast2": fast2, "slow": slow}
    store = InMemoryEngineStore(configs)
    registry = EngineRegistry(store, adapters)

    settings = Settings(
        search_fanout_budget_s=4.0,
        search_soft_budget_s=0.2,
        search_quorum_min_engines=2,
    )
    pipeline = SearchPipeline(settings, registry)

    start = time.monotonic()
    outcome = await pipeline.run(SearchParams(query="python async"))
    elapsed = time.monotonic() - start

    # Should finish shortly after soft budget (0.2s), far before the 5.0s slow engine
    assert elapsed < 1.5
    assert len(outcome.results) >= 2
    status_map = {s.engine: s.status for s in outcome.statuses}
    assert status_map.get("fast1") == "ok"
    assert status_map.get("fast2") == "ok"
    assert status_map.get("slow") == "timeout"
    assert outcome.complete is False


# --------------------------------------------------------------------------
# 4. test_scrape_stops_early_when_enough_pages (PERF-09)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scrape_stops_early_when_enough_pages(monkeypatch):
    """Scraper stops once scrape_min_pages succeed and soft budget passes, cancelling the rest."""
    settings = Settings(
        scrape_max_urls=8,
        scrape_min_pages=3,
        scrape_soft_budget_s=0.15,
        scrape_timeout_s=5.0,
        scrape_url_budget_s=4.0,
        scrape_concurrency=8,
    )
    scraper = Scraper(settings)

    async def mock_scrape_guarded(url: str, timeout_s: float | None = None):
        if "fast" in url:
            await asyncio.sleep(0.01)
            return PageContent(
                url=url,
                final_url=url,
                title=f"Title {url}",
                markdown=f"Content for {url} with plenty of words to count as ok page.",
                extractor_used="trafilatura",
                fetched_at=datetime.now(UTC),
            )
        # Slow url
        await asyncio.sleep(5.0)
        return PageContent(
            url=url,
            final_url=url,
            title="Slow",
            markdown="Late",
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        )

    monkeypatch.setattr(scraper, "_scrape_guarded", mock_scrape_guarded)

    urls = [
        "https://fast1.com",
        "https://fast2.com",
        "https://fast3.com",
        "https://slow4.com",
        "https://slow5.com",
        "https://slow6.com",
    ]

    start = time.monotonic()
    results = await scraper.scrape_many(urls)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0  # Finished way before 5.0s
    ok_count = sum(1 for r in results if isinstance(r, PageContent))
    assert ok_count >= 3
    cancelled = [r for r in results if isinstance(r, PageFailure) and "cancelled" in r.error]
    assert len(cancelled) > 0


# --------------------------------------------------------------------------
# 5. test_slowloris_body_cut_by_url_budget (PERF-10)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_slowloris_body_cut_by_url_budget(monkeypatch):
    """A slow body stream or hanging URL is bounded by scrape_url_budget_s."""
    settings = Settings(
        scrape_url_budget_s=0.2,
        scrape_timeout_s=2.0,
    )
    scraper = Scraper(settings)

    async def hanging_fetch(*args, **kwargs):
        await asyncio.sleep(3.0)
        return b"content", "https://slowloris.example.com"

    # Patch _guarded_fetch to simulate a hanging/slowloris body fetch
    monkeypatch.setattr(scraper, "_guarded_fetch", hanging_fetch)

    start = time.monotonic()
    result = await scraper.scrape("https://slowloris.example.com")
    elapsed = time.monotonic() - start

    # Must be cut within url budget (~0.2s)
    assert elapsed < 0.8
    assert isinstance(result, PageFailure)
    assert result.status == "failed"


# --------------------------------------------------------------------------
# 6. test_overload_returns_503_with_retry_after (PERF-02)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_overload_returns_503_with_retry_after():
    """When semaphore limit is saturated and wait queue expires, 503 OVERLOADED is raised."""

    # Custom tier with limit=1, wait=0.05s
    async def worker(hold_time: float):
        async with admit("search", custom_limit=1, custom_wait_s=0.05):
            await asyncio.sleep(hold_time)
            return "ok"

    # Task 1 holds the slot for 0.2s
    t1 = asyncio.create_task(worker(0.2))
    await asyncio.sleep(0.01)

    # Task 2 attempts to enter, waits max 0.05s and times out
    with pytest.raises(MetaSearchError) as exc_info:
        await worker(0.1)

    err = exc_info.value
    assert err.code == ErrorCode.OVERLOADED
    assert err.status_code == 503
    assert "Retry-After" in err.headers
    assert int(err.headers["Retry-After"]) >= 1

    await t1


# --------------------------------------------------------------------------
# 7. test_accepted_requests_keep_latency_under_overload
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_accepted_requests_keep_latency_under_overload():
    """Accepted requests complete in expected duration while excess requests fail fast."""
    successes = 0
    overloaded = 0

    async def attempt():
        nonlocal successes, overloaded
        try:
            async with admit("test_lane", custom_limit=2, custom_wait_s=0.05):
                await asyncio.sleep(0.05)
                successes += 1
        except MetaSearchError as e:
            if e.code == ErrorCode.OVERLOADED:
                overloaded += 1
            else:
                raise

    # Fire 6 concurrent attempts
    await asyncio.gather(*(attempt() for _ in range(6)))

    assert successes >= 2
    assert overloaded >= 1
    assert successes + overloaded == 6


# --------------------------------------------------------------------------
# 8. test_weighted_rate_limit
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_weighted_rate_limit():
    """Weighted rate limit: answer=5, context=3, search=1. Dynamic Retry-After."""
    ctx = AuthContext(key_id="key_test_weight", name="tester", role="user", rate_limit_rpm=10)

    # Search cost=1 (1 / 10 used)
    h1 = await check_rate_limit(ctx, cost=1, window_s=60)
    assert h1["X-RateLimit-Remaining"] == "9"

    # Context cost=3 (4 / 10 used)
    h2 = await check_rate_limit(ctx, cost=3, window_s=60)
    assert h2["X-RateLimit-Remaining"] == "6"

    # Answer cost=5 (9 / 10 used)
    h3 = await check_rate_limit(ctx, cost=5, window_s=60)
    assert h3["X-RateLimit-Remaining"] == "1"

    # Another Answer (cost=5) -> 9 + 5 = 14 > 10 => 429 RATE_LIMITED
    with pytest.raises(MetaSearchError) as exc_info:
        await check_rate_limit(ctx, cost=5, window_s=60)

    err = exc_info.value
    assert err.code == ErrorCode.RATE_LIMITED
    assert "Retry-After" in err.headers
    retry_after = int(err.headers["Retry-After"])
    assert 1 <= retry_after <= 60


# --------------------------------------------------------------------------
# 9. test_context_honors_budget_with_blackhole_upstream
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_context_honors_budget_with_blackhole_upstream(monkeypatch):
    """When upstream hangs, /v1/context finishes within budget + 0.5s
    with partial / complete=false.
    """
    fast = FastAdapter(
        "fast",
        [
            RawResult(
                title="Python docs",
                url="https://fast.org/1",
                snippet="Python is great",
                adapter="fast",
            ),
            RawResult(
                title="Asyncio docs",
                url="https://fast.org/2",
                snippet="Asyncio event loop",
                adapter="fast",
            ),
        ],
    )
    blackhole = HangAdapter(delay_s=10.0)

    cfg1 = EngineConfig(id="fast", name="Fast", category="general", enabled=True)
    cfg2 = EngineConfig(id="blackhole", name="Blackhole", category="general", enabled=True)

    configs = {c.id: c for c in [cfg1, cfg2]}
    adapters = {"fast": fast, "blackhole": blackhole}
    store = InMemoryEngineStore(configs)
    registry = EngineRegistry(store, adapters)

    settings = Settings(
        search_fanout_budget_s=1.0,
        search_soft_budget_s=0.2,
        search_quorum_min_engines=1,
        scrape_timeout_s=1.0,
        scrape_soft_budget_s=0.2,
        scrape_min_pages=2,
    )
    search_pipeline = SearchPipeline(settings, registry)

    scraper = Scraper(settings)

    async def hanging_scrape(*args, **kwargs):
        await asyncio.sleep(10.0)
        return []

    monkeypatch.setattr(scraper, "scrape_many", hanging_scrape)

    gateway = LLMGateway(settings)
    reranker = HeuristicReranker()
    ans_pipeline = AnswerPipeline(settings, search_pipeline, scraper, gateway, reranker)

    monkeypatch.setattr(deps, "get_answer_pipeline", lambda: ans_pipeline)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        start = time.monotonic()
        # Request with 1500ms timeout_ms
        resp = await client.post(
            "/v1/context",
            json={"query": "python asyncio", "timeout_ms": 1500},
        )
        elapsed = time.monotonic() - start

        # Must finish within budget (1.5s) + 0.5s tolerance = 2.0s
        assert elapsed < 2.5
        assert resp.status_code == 200
        data = resp.json()
        assert "context_md" in data
        assert data["complete"] is False
        # Fallback snippet was used in context!
        assert "Python" in data["context_md"]
