"""Tests for Wave 2 Package B: Data Integrity, Single-Flight & Client Resilience."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import app.extraction.ssrf as ssrf
from app.api import deps
from app.cache import CacheClient, _local_cache
from app.config import get_settings
from app.core.budget import RequestBudget, use_budget
from app.db.bootstrap import ensure_schema
from app.extraction.chunker import chunk_markdown
from app.extraction.scraper import PageContent, PageFailure, Scraper
from app.llm.gateway import StreamDelta, Usage
from app.main import app
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import SearchParams, SearchPipeline
from app.retrieval.health import EngineBreakerRegistry, Outcome
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import HeuristicReranker


def _raw(url: str, title: str = "T", snippet: str = "S") -> RawResult:
    return RawResult(
        url=url, title=title, snippet=snippet, adapter="fake", source_engines=[], rank=0
    )


def _page(url: str, title: str = "T") -> PageContent:
    return PageContent(
        url=url,
        final_url=url,
        title=title,
        markdown=f"# {title}\n\nContent for {url} with informative details about PostgreSQL.\n",
        extractor_used="trafilatura",
        fetched_at=datetime.now(UTC),
    )


class _TestAdapter:
    def __init__(self, engine_id: str, results=None, delay: float = 0.0, fail: bool = False):
        self.id = engine_id
        self.results = results or []
        self.delay = delay
        self.fail = fail
        self.calls = 0

    async def search(self, query, **kwargs):
        self.calls += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError(f"Engine {self.id} failed intentionally")
        return list(self.results)


class _TestGateway:
    def __init__(
        self, delay: float = 0.0, finish: str = "stop", text: str = "Postgres is relational [1]."
    ):
        self.provider = "openai"
        self.default_quick_model = "test-model"
        self.delay = delay
        self.finish = finish
        self.text = text
        self.calls = 0

    def with_override(self, override):
        return self

    async def stream(self, messages, **kwargs):
        self.calls += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        half = len(self.text) // 2
        yield StreamDelta(text=self.text[:half])
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        yield StreamDelta(text=self.text[half:], finish_reason=self.finish)
        yield StreamDelta(usage=Usage(input_tokens=10, output_tokens=10))


class _Reg:
    class _Store:
        async def record_result(self, *a, **k):
            pass

    store = _Store()

    def __init__(self, adapters) -> None:
        self.adapters = adapters

    async def enabled(self):
        return [
            (
                EngineConfig(
                    id=a.id,
                    name=a.id,
                    category="general",
                    weight=1.0,
                    enabled=True,
                    timeout_ms=2000,
                    max_results=10,
                ),
                a,
            )
            for a in self.adapters
        ]

    async def configs(self):
        return {c.id: c for c, _ in await self.enabled()}


def _install_stack(adapters, pages=None, gateway=None, cache=None):
    _local_cache.clear()
    configs = {
        a.id: EngineConfig(id=a.id, name=a.id, category="general", enabled=True) for a in adapters
    }
    store = InMemoryEngineStore(configs)
    gw = gateway or _TestGateway()
    c_client = cache or CacheClient(None)
    pages_map = pages or {}

    class TestScraper:
        async def scrape_many(self, urls, **kwargs):
            return [
                pages_map.get(u) or PageFailure(url=u, status="failed", error="not found")
                for u in urls
            ]

    class TestEmbedder:
        enabled = False

    deps.get_engine_store = lambda: store
    deps.build_adapters = lambda s: {a.id: a for a in adapters}
    deps.get_cache = lambda: c_client
    deps.get_scraper = lambda: TestScraper()
    deps.get_gateway = lambda: gw
    deps.get_embedder = lambda: TestEmbedder()
    deps.get_reranker = lambda: HeuristicReranker()
    deps.reset_pipelines()
    return gw


@pytest.fixture(autouse=True)
async def setup_w2_b_env(tmp_path, monkeypatch):
    db_file = tmp_path / "w2_b.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    monkeypatch.setenv("DEV_ANON_ADMIN", "true")
    get_settings.cache_clear()
    from app.db.session import dispose_engine

    await dispose_engine()
    await ensure_schema()
    _local_cache.clear()
    yield
    await dispose_engine()


@pytest.mark.asyncio
async def test_false_empty_is_incomplete():
    """Empty results plus breaker-skipped engine must return complete=False."""
    s = get_settings()
    tripped = EngineBreakerRegistry(threshold=2)
    tripped.record("tavily", Outcome.UPSTREAM_ERROR)
    tripped.record("tavily", Outcome.UPSTREAM_ERROR)

    adapters = [_TestAdapter("ddg", []), _TestAdapter("tavily", [])]
    pipe = SearchPipeline(s, _Reg(adapters), cache=CacheClient(None), health_registry=tripped)

    outcome = await pipe.run(SearchParams(query="q empty test"))
    assert len(outcome.results) == 0
    assert outcome.complete is False


@pytest.mark.asyncio
async def test_results_plus_breaker_incomplete():
    """Results plus a breaker-skipped engine must return complete=False."""
    s = get_settings()
    tripped = EngineBreakerRegistry(threshold=2)
    tripped.record("tavily", Outcome.UPSTREAM_ERROR)
    tripped.record("tavily", Outcome.UPSTREAM_ERROR)

    adapters = [_TestAdapter("ddg", [_raw("https://a.example/1")]), _TestAdapter("tavily", [])]
    pipe = SearchPipeline(s, _Reg(adapters), cache=CacheClient(None), health_registry=tripped)

    outcome = await pipe.run(SearchParams(query="q partial test"))
    assert len(outcome.results) == 1
    assert outcome.complete is False


@pytest.mark.asyncio
async def test_cached_partial_stays_incomplete():
    """Partial search stored in cache must retain complete=False on replay."""
    s = get_settings()
    cache = CacheClient(None)
    adapters = [
        _TestAdapter("ddg", [_raw("https://a.example/c1")]),
        _TestAdapter("serper", fail=True),
    ]
    pipe = SearchPipeline(
        s, _Reg(adapters), cache=cache, health_registry=EngineBreakerRegistry(threshold=9)
    )

    first = await pipe.run(SearchParams(query="q cached partial"))
    assert first.complete is False
    assert first.cached is False

    second = await pipe.run(SearchParams(query="q cached partial"))
    assert second.complete is False
    assert second.cached is True
    assert second.age_s is not None
    assert second.age_s >= 0


@pytest.mark.asyncio
async def test_partial_results_get_short_ttl():
    """Partial search results must receive short TTL (cache_partial_ttl_s)."""
    s = get_settings()
    seen: dict[str, int] = {}
    cache = CacheClient(None)
    real_set = cache.set_json

    async def spy(k, v, *, ttl_s):
        seen["ttl"] = ttl_s
        await real_set(k, v, ttl_s=ttl_s)

    cache.set_json = spy
    tripped = EngineBreakerRegistry(threshold=1)
    tripped.record("tavily", Outcome.UPSTREAM_ERROR)

    adapters = [_TestAdapter("ddg", [_raw("https://a.example/ttl")]), _TestAdapter("tavily", [])]
    pipe = SearchPipeline(s, _Reg(adapters), cache=cache, health_registry=tripped)

    await pipe.run(SearchParams(query="q test ttl"))
    assert seen.get("ttl", 9999) <= s.cache_partial_ttl_s


@pytest.mark.asyncio
async def test_implicitly_financial_search_short_ttl():
    """Implicitly financial queries must receive TTL 300 or less even if category is general."""
    s = get_settings()
    seen: dict[str, int] = {}
    cache = CacheClient(None)
    real_set = cache.set_json

    async def spy(k, v, *, ttl_s):
        seen["ttl"] = ttl_s
        await real_set(k, v, ttl_s=ttl_s)

    cache.set_json = spy
    adapters = [_TestAdapter("ddg", [_raw("https://a.example/f")])]
    pipe = SearchPipeline(s, _Reg(adapters), cache=cache, health_registry=EngineBreakerRegistry())

    await pipe.run(SearchParams(query="giá cổ phiếu FPT hôm nay"))
    assert seen.get("ttl", 9999) <= 300


@pytest.mark.asyncio
async def test_malformed_url_does_not_fail_search_or_answer():
    """Malformed URL in adapter result must be dropped gracefully without failing request."""
    good_url = "https://good.example/1"
    bad_url = "http://[bad"
    adapters = [_TestAdapter("fake", [_raw(good_url), _raw(bad_url)])]
    _install_stack(adapters, pages={good_url: _page(good_url)})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/v1/search", json={"query": "postgres test"})
        assert resp.status_code == 200
        data = resp.json()
        urls = [r["url"] for r in data["results"]]
        assert good_url in urls
        assert bad_url not in urls

        ans_resp = await c.post("/v1/answer", json={"query": "postgres test", "stream": False})
        assert ans_resp.status_code == 200
        assert "Postgres" in ans_resp.json()["answer_md"]


@pytest.mark.asyncio
async def test_cached_search_returns_age_s():
    """Cached search response must include cached=True and non-negative age_s."""
    good_url = "https://good.example/cache"
    adapters = [_TestAdapter("fake", [_raw(good_url)])]
    _install_stack(adapters, pages={good_url: _page(good_url)})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r1 = await c.post("/v1/search", json={"query": "test age cache"})
        assert r1.status_code == 200
        assert r1.json().get("cached") is False

        r2 = await c.post("/v1/search", json={"query": "test age cache"})
        assert r2.status_code == 200
        body = r2.json()
        assert body.get("cached") is True
        assert "age_s" in body
        assert isinstance(body["age_s"], int)
        assert body["age_s"] >= 0


@pytest.mark.asyncio
async def test_truncated_never_replayed_complete():
    """Truncated answer (finish_reason=length) must never be cached as complete."""
    good_url = "https://good.example/trunc"
    gw = _TestGateway(finish="length", text="Incomplete truncated answer")
    _install_stack(
        [_TestAdapter("fake", [_raw(good_url)])], pages={good_url: _page(good_url)}, gateway=gw
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r1 = (await c.post("/v1/answer", json={"query": "postgres cut", "stream": False})).json()
        assert r1.get("status") == "truncated"

        r2 = (await c.post("/v1/answer", json={"query": "postgres cut", "stream": False})).json()
        assert r2.get("status") == "truncated"
        assert gw.calls == 2


@pytest.mark.asyncio
async def test_answer_from_partial_results_stays_incomplete():
    """Answer synthesized from partial engine failure must remain complete=False across calls."""
    good_url = "https://good.example/partial"
    adapters = [_TestAdapter("fake", [_raw(good_url)]), _TestAdapter("dead", fail=True)]
    gw = _install_stack(adapters, pages={good_url: _page(good_url)})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        a = (await c.post("/v1/answer", json={"query": "postgres partial", "stream": False})).json()
        assert a.get("complete") is False

        b = (await c.post("/v1/answer", json={"query": "postgres partial", "stream": False})).json()
        assert b.get("complete") is False
        assert gw.calls == 2


@pytest.mark.asyncio
async def test_implicitly_financial_answer_not_cached():
    """Implicitly financial answer must not be cached without max_age_s."""
    good_url = "https://good.example/fin"
    adapters = [_TestAdapter("fake", [_raw(good_url)])]
    gw = _install_stack(adapters, pages={good_url: _page(good_url)})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        await c.post("/v1/answer", json={"query": "giá cổ phiếu FPT hôm nay", "stream": False})
        await c.post("/v1/answer", json={"query": "giá cổ phiếu FPT hôm nay", "stream": False})
        assert gw.calls == 2


@pytest.mark.asyncio
async def test_stampede_one_llm_call():
    """Concurrent identical answer requests must coalesce into 1 search and 1 LLM call."""
    good_url = "https://good.example/storm"
    adapter = _TestAdapter("fake", [_raw(good_url)], delay=0.05)
    gw = _TestGateway(delay=0.15)
    _install_stack([adapter], pages={good_url: _page(good_url)}, gateway=gw)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        rs = await asyncio.gather(
            *(
                c.post("/v1/answer", json={"query": "postgres storm", "stream": False})
                for _ in range(4)
            )
        )
        assert all(r.status_code == 200 for r in rs)
        assert all("Postgres" in r.json().get("answer_md", "") for r in rs)
        assert gw.calls == 1
        assert adapter.calls == 1


@pytest.mark.asyncio
async def test_first_caller_streams_tokens_under_singleflight():
    """First caller must receive tokens in real-time stream under single flight without delay."""
    good_url = "https://good.example/stream"
    adapter = _TestAdapter("fake", [_raw(good_url)], delay=0.02)
    gw = _TestGateway(delay=0.05, text="Postgres is relational database [1].")
    _install_stack([adapter], pages={good_url: _page(good_url)}, gateway=gw)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/v1/answer", json={"query": "postgres stream test", "stream": True})
        assert resp.status_code == 200
        events = []
        tokens = []
        for line in resp.text.splitlines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            elif line.startswith("data:") and '"t"' in line:
                tokens.append(json.loads(line.split("data:", 1)[1])["t"])

        assert events[0] == "status"
        assert "sources" in events
        assert "token" in events
        assert len(tokens) >= 2
        assert "".join(tokens).startswith("Postgres is relational")
        assert events[-2:] == ["citations", "done"]


@pytest.mark.asyncio
async def test_singleflight_survives_first_waiter_cancel():
    """When leader cancels, subsequent waiters must survive and get the complete result."""
    good_url = "https://good.example/cancel"
    adapter = _TestAdapter("fake", [_raw(good_url)], delay=0.05)
    gw = _TestGateway(delay=0.25)
    _install_stack([adapter], pages={good_url: _page(good_url)}, gateway=gw)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        first = asyncio.create_task(
            c.post("/v1/answer", json={"query": "postgres cancel", "stream": False})
        )
        await asyncio.sleep(0.08)
        other = asyncio.create_task(
            c.post("/v1/answer", json={"query": "postgres cancel", "stream": False})
        )
        await asyncio.sleep(0.08)
        first.cancel()
        resp = await other
        assert resp.status_code == 200
        assert "Postgres" in resp.json().get("answer_md", "")
        assert gw.calls == 1


@pytest.mark.asyncio
async def test_different_questions_not_merged():
    """Distinct questions must not be coalesced."""
    good_url = "https://good.example/diff"
    adapter = _TestAdapter("fake", [_raw(good_url)])
    gw = _TestGateway(delay=0.05)
    _install_stack([adapter], pages={good_url: _page(good_url)}, gateway=gw)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        rs = await asyncio.gather(
            *(
                c.post("/v1/answer", json={"query": f"postgres distinct {i}", "stream": False})
                for i in range(2)
            )
        )
        assert all(r.status_code == 200 for r in rs)
        assert gw.calls == 2


@pytest.mark.asyncio
async def test_budget_cut_not_negative_cached():
    """Local budget timeout must not mark page as negatively cached."""
    _local_cache.clear()

    async def slow(request):
        await asyncio.sleep(1.0)
        return httpx.Response(
            200,
            text="<html><body>" + "<p>content paragraph</p>" * 50 + "</body></html>",
            headers={"content-type": "text/html"},
        )

    scraper = Scraper(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(slow)),
        cache=CacheClient(None),
    )

    async def allowed(*a, **k):
        return True

    async def resolved(url):
        return frozenset({"93.184.216.34"})

    scraper._robots.allowed = allowed
    orig_resolve = ssrf.resolve_and_check
    ssrf.resolve_and_check = resolved
    target_url = "https://budget.example/page"
    try:
        with use_budget(RequestBudget(0.12)):
            res = await scraper.scrape(target_url)
            assert isinstance(res, PageFailure)
        poisoned = await scraper._is_negatively_cached(target_url)
        assert poisoned is False
    finally:
        ssrf.resolve_and_check = orig_resolve


def test_chunk_markdown_with_malformed_url():
    """chunk_markdown must safely handle malformed URL without throwing ValueError."""
    chunks = chunk_markdown(
        "# Title\n\nSome content here.", document_url="http://[bad", source_title="Title"
    )
    assert len(chunks) >= 1
    assert chunks[0].document_url == "http://[bad"


def test_client_metasearch(monkeypatch):
    """MetaSearch python client must meet all resilience and contract requirements."""
    client_path = (
        Path(__file__).resolve().parents[2] / "clients" / "python" / "metasearch_client.py"
    )
    if not client_path.exists():
        client_path = (
            Path(__file__).resolve().parents[1] / "clients" / "python" / "metasearch_client.py"
        )
    spec = importlib.util.spec_from_file_location("msc", client_path)
    assert spec is not None and spec.loader is not None
    msc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(msc)

    monkeypatch.setattr(msc.time, "sleep", lambda s: None)  # restored after the test

    class FakeResponse:
        def __init__(self, code, body, headers=None):
            self.status_code = code
            self._body = body
            self.headers = headers or {}
            self.text = json.dumps(body)

        def json(self):
            return self._body

    def run_client(*responses):
        idx = {"n": 0}

        def post(*a, **k):
            r = responses[min(idx["n"], len(responses) - 1)]
            idx["n"] += 1
            return r

        msc.requests.post = post
        try:
            return msc.search("http://127.0.0.1:1", "q"), idx["n"]
        except Exception as exc:
            return f"RAISES {type(exc).__name__}", idx["n"]

    err = {"error": {"code": "x", "message": "x", "request_id": "req-123"}}

    # 1. Keeps results when complete=False
    got, _ = run_client(FakeResponse(200, {"complete": False, "results": [{"url": "u"}]}))
    assert got == [{"url": "u"}]

    # 2. Returns [] only on true empty
    got, _ = run_client(FakeResponse(200, {"complete": True, "results": []}))
    assert got == []

    # 3. Returns None for empty and incomplete
    got, _ = run_client(FakeResponse(200, {"complete": False, "results": []}))
    assert got is None

    # 4. Returns None on 401 and 500
    for code in (401, 500):
        got, _ = run_client(FakeResponse(code, err))
        assert got is None

    # 5. Survives HTTP date in Retry-After without raising ValueError
    got, _ = run_client(FakeResponse(429, err, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    assert not str(got).startswith("RAISES")

    # 6. Retries 503 exactly once
    got, n = run_client(
        FakeResponse(503, err, {"Retry-After": "0"}),
        FakeResponse(200, {"complete": True, "results": [{"url": "u"}]}),
    )
    assert got == [{"url": "u"}]
    assert n == 2

    # 7. Gives up on 429 after one retry
    got, n = run_client(FakeResponse(429, err, {"Retry-After": "0"}))
    assert got is None
    assert n == 2

    # 8. Request ID tracking
    assert msc.get_last_request_id() is not None


@pytest.mark.asyncio
async def test_scraper_exception_when_budget_exhausted():
    """Scraper must not negative cache when an exception happens while budget is exhausted."""
    import app.extraction.ssrf as ssrf
    from app.cache import CacheClient, _local_cache
    from app.config import get_settings
    from app.core.budget import RequestBudget, use_budget
    from app.extraction.scraper import PageFailure, Scraper

    _local_cache.clear()

    async def fail_transport(request):
        raise RuntimeError("simulated network connection drop")

    scraper = Scraper(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(fail_transport)),
        cache=CacheClient(None),
    )

    async def allowed(*a, **k):
        return True

    async def resolved(url):
        return frozenset({"93.184.216.34"})

    scraper._robots.allowed = allowed
    orig_resolve = ssrf.resolve_and_check
    ssrf.resolve_and_check = resolved
    target_url = "https://budget-exhausted.example/page"
    try:
        import time

        b = RequestBudget(0.1, start_time=time.monotonic() - 1.0)
        assert b.is_expired()
        with use_budget(b):
            res = await scraper.scrape(target_url)
            assert isinstance(res, PageFailure)
        poisoned = await scraper._is_negatively_cached(target_url)
        assert poisoned is False
    finally:
        ssrf.resolve_and_check = orig_resolve


def test_score_chunks_sync_malformed_page_url():
    """_score_chunks_sync must safely handle malformed page url without raising ValueError."""
    from datetime import UTC, datetime

    from app.extraction.scraper import PageContent
    from app.orchestration.pipeline import _score_chunks_sync

    page = PageContent(
        url="http://[bad",
        final_url="http://[bad",
        title="Title",
        markdown="This is page text with sufficient tokens to chunk properly.",
        extractor_used="trafilatura",
        fetched_at=datetime.now(UTC),
    )
    chunks = _score_chunks_sync("query", [], [page])
    assert isinstance(chunks, list)


@pytest.mark.asyncio
async def test_answer_pipeline_replay_preserves_status_complete_and_council():
    """_replay must preserve payload status, complete, and council events."""
    from app.cache import CacheClient
    from app.config import get_settings
    from app.orchestration.pipeline import AnswerPipeline

    pipe = AnswerPipeline.__new__(AnswerPipeline)
    pipe.settings = get_settings()
    pipe.cache = CacheClient(None)
    payload = {
        "status": "truncated",
        "complete": False,
        "council": {"verdict": "consensus", "summary": "test"},
        "answer_md": "some markdown answer text",
        "citations": [],
        "sources": [],
    }
    events = [ev async for ev in pipe._replay(payload)]
    ev_dict = dict(events)
    assert "council" in ev_dict
    assert ev_dict["council"] == {"verdict": "consensus", "summary": "test"}
    done = ev_dict["done"]
    assert done["status"] == "truncated"
    assert done["complete"] is False


@pytest.mark.asyncio
async def test_answer_pipeline_cache_store_payload():
    """_cache_store must store status complete and complete True."""
    import uuid
    from typing import Any

    from app.cache import CacheClient
    from app.config import get_settings
    from app.orchestration.pipeline import AnswerPipeline, SearchParams

    cache = CacheClient(None)
    saved: dict[str, Any] = {}

    async def fake_set(k, v, **kwargs):
        saved.update(v)

    cache.set_json = fake_set
    pipe = AnswerPipeline.__new__(AnswerPipeline)
    pipe.settings = get_settings()
    pipe.cache = cache
    await pipe._cache_store(
        SearchParams(query="python programming basics"),
        model="fake-model",
        answer_id=uuid.uuid4(),
        answer_md="informative answer",
        citations=[],
        sources=[],
    )
    assert saved.get("status") == "complete"
    assert saved.get("complete") is True


def test_client_retry_after_parsed_and_slept(monkeypatch):
    """Client must pass parsed Retry-After to sleep for 503 instead of hardcoded 1s."""
    client_path = (
        Path(__file__).resolve().parents[2] / "clients" / "python" / "metasearch_client.py"
    )
    if not client_path.exists():
        client_path = (
            Path(__file__).resolve().parents[1] / "clients" / "python" / "metasearch_client.py"
        )
    spec = importlib.util.spec_from_file_location("msc2", client_path)
    assert spec is not None and spec.loader is not None
    msc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(msc)

    sleep_calls = []
    monkeypatch.setattr(msc.time, "sleep", lambda s: sleep_calls.append(s))  # restored

    class R:
        def __init__(self, code, body, headers=None):
            self.status_code, self._body, self.headers, self.text = (
                code,
                body,
                headers or {},
                json.dumps(body),
            )

        def json(self):
            return self._body

    seen = {"n": 0}
    responses = [
        R(503, {"error": {"code": "x"}}, {"Retry-After": "3.5"}),
        R(200, {"complete": True, "results": [{"url": "http://ok"}]}),
    ]

    def post(*a, **k):
        r = responses[min(seen["n"], len(responses) - 1)]
        seen["n"] += 1
        return r

    msc.requests.post = post
    res = msc.search("http://127.0.0.1", "q")
    assert res == [{"url": "http://ok"}]
    assert sleep_calls == [3.5]


def test_client_raises_for_non_json_error():
    """Client must preserve resp.text as message when response is non-JSON error."""
    client_path = (
        Path(__file__).resolve().parents[2] / "clients" / "python" / "metasearch_client.py"
    )
    if not client_path.exists():
        client_path = (
            Path(__file__).resolve().parents[1] / "clients" / "python" / "metasearch_client.py"
        )
    spec = importlib.util.spec_from_file_location("msc3", client_path)
    assert spec is not None and spec.loader is not None
    msc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(msc)

    class NonJsonResp:
        status_code = 403
        text = "Raw Custom Forbidden"
        headers = {"x-request-id": "req-custom-403"}

        def json(self):
            raise ValueError("not json")

    msc.requests.post = lambda *a, **k: NonJsonResp()
    with pytest.raises(msc.MetaSearchError) as exc_info:
        msc.search("http://127.0.0.1", "q")
    exc = exc_info.value
    assert exc.status_code == 403
    assert exc.message == "Raw Custom Forbidden"
    assert exc.request_id == "req-custom-403"


@pytest.mark.asyncio
async def test_upstream_timeout_without_budget_is_negative_cached():
    """A timeout with no request budget is the upstream's fault and stays negative cached.

    Plan 11 section 4 item 6: only our own budget cut skips the negative cache (audit 15).
    """
    import app.extraction.ssrf as ssrf
    from app.cache import CacheClient, _local_cache
    from app.config import get_settings
    from app.extraction.scraper import PageFailure, Scraper

    _local_cache.clear()

    async def timeout_transport(request):
        raise TimeoutError("local socket read timeout")

    scraper = Scraper(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(timeout_transport)),
        cache=CacheClient(None),
    )

    async def allowed(*a, **k):
        return True

    async def resolved(url):
        return frozenset({"93.184.216.34"})

    scraper._robots.allowed = allowed
    orig_resolve = ssrf.resolve_and_check
    ssrf.resolve_and_check = resolved
    target_url = "https://timeout-no-budget.example/page"
    try:
        res = await scraper.scrape(target_url)
        assert isinstance(res, PageFailure)
        assert res.error == "TimeoutError"
        poisoned = await scraper._is_negatively_cached(target_url)
        assert poisoned is True
    finally:
        ssrf.resolve_and_check = orig_resolve


# -- audit 15 (readiness audit 15) -------------------------


class _LongGateway(_TestGateway):
    """First token at once, then the rest of a long answer after ``tail`` seconds."""

    def __init__(self, tail: float, finish: str = "stop") -> None:
        super().__init__(finish=finish)
        self.tail = tail

    async def stream(self, messages, **kwargs):
        self.calls += 1
        yield StreamDelta(text="Postgres is relational [1].")
        await asyncio.sleep(self.tail)
        yield StreamDelta(text=" More detail.", finish_reason=self.finish)
        yield StreamDelta(usage=Usage(input_tokens=10, output_tokens=10))


async def test_follower_gets_long_answer_past_first_token_budget():
    """The answer budget counts to the first token; a joined caller must not give up early."""
    good_url = "https://good.example/long"
    gw = _LongGateway(tail=2.5)
    _install_stack([_TestAdapter("fake", [_raw(good_url)])], {good_url: _page(good_url)}, gw)
    body = {"query": "postgres long answer", "stream": False, "timeout_ms": 2000}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        rs = await asyncio.gather(*(c.post("/v1/answer", json=body) for _ in range(2)))
    assert [r.status_code for r in rs] == [200, 200]
    assert all("More detail" in r.json()["answer_md"] for r in rs)
    assert gw.calls == 1


async def test_follower_streams_tokens_before_the_answer_ends():
    """A caller that joins a running answer gets tokens live, not only after the end."""
    from app.orchestration.pipeline import SearchParams as SP

    good_url = "https://good.example/live"
    gw = _LongGateway(tail=0.6)
    _install_stack([_TestAdapter("fake", [_raw(good_url)])], {good_url: _page(good_url)}, gw)
    pipe = deps.get_answer_pipeline()
    started = asyncio.get_running_loop().time()
    first_token_at: dict[str, float] = {}

    async def consume(tag: str) -> None:
        async for name, _ in pipe.run(SP(query="postgres live")):
            if name == "token" and tag not in first_token_at:
                first_token_at[tag] = asyncio.get_running_loop().time() - started

    leader = asyncio.create_task(consume("leader"))
    await asyncio.sleep(0.05)
    await asyncio.gather(leader, consume("follower"))
    assert gw.calls == 1
    assert first_token_at["follower"] < 0.5  # before the 0.6s tail finished


async def test_reader_that_stops_early_leaves_no_stale_answer():
    """aclose() before the end must not leave a finished flight that the next caller replays."""
    from app.orchestration.pipeline import SearchParams as SP

    good_url = "https://good.example/early"
    gw = _LongGateway(tail=0.3, finish="length")
    _install_stack([_TestAdapter("fake", [_raw(good_url)])], {good_url: _page(good_url)}, gw)
    pipe = deps.get_answer_pipeline()
    gen = pipe.run(SP(query="postgres stop early"))
    async for name, _ in gen:
        if name == "token":
            break
    await gen.aclose()
    await asyncio.sleep(0.5)
    assert pipe._flights == {}
    done = None
    async for name, data in pipe.run(SP(query="postgres stop early")):
        if name == "done":
            done = data
    assert gw.calls == 2
    assert done is not None and done.get("cached") is False
    assert pipe._flights == {}


async def test_last_reader_leaving_cancels_the_shared_answer():
    """When every caller has gone, the LLM call is cancelled and the flight is removed."""
    from app.orchestration.pipeline import SearchParams as SP

    good_url = "https://good.example/gone"
    gw = _LongGateway(tail=5.0)
    _install_stack([_TestAdapter("fake", [_raw(good_url)])], {good_url: _page(good_url)}, gw)
    pipe = deps.get_answer_pipeline()

    async def consume() -> None:
        async for _ in pipe.run(SP(query="postgres gone")):
            pass

    tasks = [asyncio.create_task(consume()) for _ in range(2)]
    await asyncio.sleep(0.3)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0.05)
    assert pipe._flights == {}
    assert gw.calls == 1


async def test_follower_idle_deadline_uses_error_model(monkeypatch):
    """A joined caller whose shared answer goes silent gets DEADLINE_EXCEEDED, not a crash."""
    import app.orchestration.pipeline as pl
    from app.core.errors import ErrorCode, MetaSearchError

    monkeypatch.setattr(pl, "_FLIGHT_IDLE_TIMEOUT_S", 0.2)
    good_url = "https://good.example/silent"
    gw = _LongGateway(tail=1.0)
    _install_stack([_TestAdapter("fake", [_raw(good_url)])], {good_url: _page(good_url)}, gw)
    pipe = deps.get_answer_pipeline()

    async def consume() -> str:
        async for _ in pipe.run(pl.SearchParams(query="postgres silent")):
            pass
        return "ok"

    leader = asyncio.create_task(consume())
    await asyncio.sleep(0.3)  # the leader already has its first token
    with pytest.raises(MetaSearchError) as err:
        await consume()
    assert err.value.code is ErrorCode.DEADLINE_EXCEEDED
    assert await leader == "ok"


async def test_shared_answer_reaches_every_callers_on_event():
    """Mid-flight events (engine_done and the like) reach every caller, not only the first."""
    from app.orchestration.pipeline import SearchParams as SP

    good_url = "https://good.example/events"
    gw = _LongGateway(tail=0.2)
    _install_stack(
        [_TestAdapter("fake", [_raw(good_url)], delay=0.1)], {good_url: _page(good_url)}, gw
    )
    pipe = deps.get_answer_pipeline()
    seen: dict[str, list[str]] = {"a": [], "b": []}

    async def consume(tag: str) -> None:
        async for _ in pipe.run(
            SP(query="postgres events"), on_event=lambda e: seen[tag].append(e[0])
        ):
            pass

    first = asyncio.create_task(consume("a"))
    await asyncio.sleep(0.02)
    await asyncio.gather(first, consume("b"))
    assert gw.calls == 1
    assert seen["a"] and seen["a"] == seen["b"]


async def test_answer_cache_looked_up_once_per_miss(monkeypatch):
    """run() looks the answer cache up; the shared producer must not look it up again."""
    from app.orchestration.pipeline import AnswerPipeline
    from app.orchestration.pipeline import SearchParams as SP

    good_url = "https://good.example/once"
    _install_stack([_TestAdapter("fake", [_raw(good_url)])], {good_url: _page(good_url)})
    calls = 0
    real = AnswerPipeline._cache_hit

    async def counting(self, params, model):
        nonlocal calls
        calls += 1
        return await real(self, params, model)

    monkeypatch.setattr(AnswerPipeline, "_cache_hit", counting)
    async for _ in deps.get_answer_pipeline().run(SP(query="postgres once")):
        pass
    assert calls == 1


async def test_complete_answer_is_replayed_from_cache():
    """A complete answer asked again is served from the cache under the effective model."""
    good_url = "https://good.example/replay"
    gw = _TestGateway()
    _install_stack([_TestAdapter("fake", [_raw(good_url)])], {good_url: _page(good_url)}, gw)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        body = {"query": "postgres replay", "stream": False}
        first = (await c.post("/v1/answer", json=body)).json()
        second = (await c.post("/v1/answer", json=body)).json()
    assert first["status"] == "complete" and first["cached"] is False
    assert second["cached"] is True and second["answer_md"] == first["answer_md"]
    assert gw.calls == 1
