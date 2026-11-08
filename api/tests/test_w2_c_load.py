"""Tests for Wave 2 Package C: Load, Budget, Admission & Gateway Resilience."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from app.api import deps
from app.config import get_settings
from app.core import admission
from app.core.admission import reset_admission
from app.core.auth import create_key, reset_rate_limit_windows
from app.core.budget import RequestBudget, get_current_budget, use_budget
from app.core.errors import ErrorCode, MetaSearchError
from app.db.bootstrap import ensure_schema
from app.db.session import dispose_engine
from app.llm.gateway import (
    LLMError,
    LLMGateway,
    StreamDelta,
    Usage,
    llm_breakers,
)
from app.main import app
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import SearchParams, SearchPipeline
from app.retrieval.health import EngineBreakerRegistry
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import HeuristicReranker


def _raw(url: str, title: str = "T", snippet: str = "S") -> RawResult:
    return RawResult(
        url=url, title=title, snippet=snippet, adapter="fake", source_engines=[], rank=0
    )


class _MockAdapter:
    def __init__(self, engine_id: str, results=None, delay: float = 0.0, fail: bool = False):
        self.id = engine_id
        self.results = results or []
        self.delay = delay
        self.fail = fail
        self.calls = 0
        self.max_inflight = 0
        self._inflight = 0

    async def search(self, query: str, **kwargs) -> list[RawResult]:
        self.calls += 1
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            if self.delay > 0:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError(f"Engine {self.id} failed intentionally")
            return list(self.results)
        finally:
            self._inflight -= 1


class _MockGateway:
    def __init__(self, delay: float = 0.0, text: str = "Answer [1]."):
        self.provider = "openai"
        self.default_quick_model = "test-model"
        self.delay = delay
        self.text = text
        self.calls = 0

    def with_override(self, override: Any) -> _MockGateway:
        return self

    async def stream(self, messages: list[dict[str, str]], **kwargs):
        self.calls += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        yield StreamDelta(text=self.text)
        yield StreamDelta(usage=Usage(input_tokens=10, output_tokens=10))


class _MockReg:
    class _Store:
        async def record_result(self, *a, **k):
            pass

    store = _Store()

    def __init__(self, adapters: list[_MockAdapter]) -> None:
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


def _install_stack(adapters, gateway=None):
    configs = {
        a.id: EngineConfig(id=a.id, name=a.id, category="general", enabled=True) for a in adapters
    }
    store = InMemoryEngineStore(configs)
    gw = gateway or _MockGateway()

    class TestScraper:
        async def scrape_many(self, urls, **kwargs):
            return []

    class TestEmbedder:
        enabled = False

    deps.get_engine_store = lambda: store
    deps.build_adapters = lambda s: {a.id: a for a in adapters}
    deps.get_scraper = lambda: TestScraper()
    deps.get_gateway = lambda: gw
    deps.get_embedder = lambda: TestEmbedder()
    deps.get_reranker = lambda: HeuristicReranker()
    deps.reset_pipelines()
    return gw


@pytest.fixture(autouse=True)
async def setup_w2_c_env(tmp_path, monkeypatch):
    db_file = tmp_path / "w2_c.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    monkeypatch.setenv("LLM_PROVIDER", "pollinations")
    get_settings.cache_clear()
    await dispose_engine()
    reset_admission()
    reset_rate_limit_windows()
    await ensure_schema()
    yield
    await dispose_engine()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_sse_inside_admission():
    """Verify streaming answers stay inside the admission limit and run in a budget."""
    adapter = _MockAdapter("fake", [_raw("https://example.com/1")])
    _install_stack([adapter])
    reset_admission()

    seen = {"inflight": 0, "max": 0, "budget": None}

    class Pipe:
        async def run(self, params, **kw):
            seen["inflight"] += 1
            seen["max"] = max(seen["max"], seen["inflight"])
            seen["budget"] = get_current_budget()
            try:
                await asyncio.sleep(0.1)
                yield ("done", {"answer_id": "test-ans"})
            finally:
                seen["inflight"] -= 1

    deps.get_answer_pipeline = lambda: Pipe()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        rs = await asyncio.gather(
            *(c.post("/v1/answer", json={"query": "q", "stream": True}) for _ in range(35))
        )

    codes = sorted({r.status_code for r in rs})
    assert seen["max"] <= 16, f"Peak inflight exceeded limit 16: {seen['max']}"
    assert seen["budget"] is not None, "Streaming answer did not run inside a budget"
    assert set(codes) <= {200, 503}, f"Unexpected status codes: {codes}"


@pytest.mark.asyncio
async def test_sse_slot_released_on_disconnect():
    """Verify that disconnecting from SSE immediately releases the admission slot."""
    reset_admission()
    adapter = _MockAdapter("fake", [_raw("https://example.com/1")])
    _install_stack([adapter])

    class Pipe:
        async def run(self, params, **kw):
            for i in range(10):
                await asyncio.sleep(0.04)
                yield ("token", {"t": f"tok{i}"})
            yield ("done", {"answer_id": "x"})

    deps.get_answer_pipeline = lambda: Pipe()

    sem = await admission.get_tier_semaphore("answer", 16)
    assert sem._value == 16

    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        c.stream("POST", "/v1/answer", json={"query": "q", "stream": True}) as resp,
    ):
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if "data:" in line:
                break

    await asyncio.sleep(0.05)
    assert sem._value == 16, f"Slot leaked on disconnect: {sem._value} != 16"


@pytest.mark.asyncio
async def test_per_key_limit_blocks_only_that_key(monkeypatch):
    """Verify per-key concurrency limits block only the offending key, not others."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    get_settings.cache_clear()
    reset_admission()
    reset_rate_limit_windows()

    adapter = _MockAdapter("fake", [_raw("https://example.com/1")], delay=0.3)
    _install_stack([adapter])

    small_key, _ = await create_key("small_tier", rate_limit_rpm=6000, max_concurrent=2)
    other_key, _ = await create_key("other_tier", rate_limit_rpm=6000, max_concurrent=5)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        hdr_small = {"Authorization": f"Bearer {small_key}"}
        hdr_other = {"Authorization": f"Bearer {other_key}"}

        burst = [
            asyncio.create_task(c.post("/v1/search", json={"query": f"q{i}"}, headers=hdr_small))
            for i in range(6)
        ]
        await asyncio.sleep(0.05)
        peak = adapter.max_inflight

        start = time.perf_counter()
        r_other = await c.post("/v1/search", json={"query": "from another key"}, headers=hdr_other)
        took_other = time.perf_counter() - start

        rs_small = await asyncio.gather(*burst)

    codes_small = [r.status_code for r in rs_small]
    assert peak <= 2, f"Adapter peak inflight {peak} exceeded small_key limit 2"
    assert set(codes_small) <= {200, 429, 503}, f"Unexpected codes for small key: {codes_small}"
    assert 429 in codes_small, f"Expected 429 for over-limit key, got {codes_small}"
    assert r_other.status_code == 200, f"Other key was blocked: {r_other.status_code}"
    assert took_other < 2.0, f"Other key took too long: {took_other:.2f}s"


@pytest.mark.asyncio
async def test_quorum_cut_keeps_breaker_closed(monkeypatch):
    """Verify that engines cancelled because quorum was met do not trip their breaker."""
    monkeypatch.setenv("SEARCH_SOFT_BUDGET_S", "0.08")
    get_settings.cache_clear()
    reset_admission()

    slow = _MockAdapter("slow", [_raw("https://slow.example/1")], delay=0.4)
    fast1 = _MockAdapter("f1", [_raw("https://f1.example/1")])
    fast2 = _MockAdapter("f2", [_raw("https://f2.example/1")])

    registry = EngineBreakerRegistry(threshold=3, timeout_threshold=5)
    pipe = SearchPipeline(
        get_settings(), _MockReg([slow, fast1, fast2]), cache=None, health_registry=registry
    )

    statuses = []
    for i in range(6):
        out = await pipe.run(SearchParams(query=f"quorum test {i}"))
        slow_st = next(s.status for s in out.statuses if s.engine == "slow")
        statuses.append(slow_st)
        assert slow_st == "cancelled", f"Expected cancelled status under quorum cut, got {slow_st}"

    breaker = registry.get("slow")
    assert breaker.state == "closed", f"Breaker was opened: {breaker.state}"
    assert "skipped_breaker" not in statuses, (
        f"Slow engine was marked as skipped_breaker: {statuses}"
    )


@pytest.mark.asyncio
async def test_retry_after_respects_budget():
    """Verify that Retry-After sleep respects budget and avoids sleeping after final attempt."""
    messages = [{"role": "user", "content": "hi"}]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "5"}, json={"error": "rate limited"})

    llm_breakers._breakers.clear()
    gw = LLMGateway(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        use_runtime_store=False,
    )

    start = time.perf_counter()
    with use_budget(RequestBudget(0.2)), pytest.raises(LLMError):
        async for _ in gw.stream(messages):
            pass
    took = time.perf_counter() - start
    assert took <= 0.6, f"Retry-After sleep exceeded request budget: {took:.2f}s > 0.6s"


@pytest.mark.asyncio
async def test_gateway_candidates_bounded_by_budget():
    """Verify that gateway retries across multiple candidates are strictly bounded by budget."""
    messages = [{"role": "user", "content": "hi"}]
    call_count = 0

    def slow_fail_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(500, json={"error": "down"})

    llm_breakers._breakers.clear()
    gw = LLMGateway(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(slow_fail_handler)),
        use_runtime_store=False,
    )

    start = time.perf_counter()
    with use_budget(RequestBudget(0.25)), pytest.raises(LLMError):
        async for _ in gw.stream(messages):
            pass
    took = time.perf_counter() - start
    assert took <= 0.7, f"Multi-candidate retry exceeded budget: {took:.2f}s"


@pytest.mark.asyncio
async def test_search_requires_admission():
    """Locks NAMED['C'] search admission: rejects with 503 when admission saturated."""
    reset_admission()
    _install_stack([_MockAdapter("fake", [_raw("https://example.com/1")])])

    # Saturate the search tier (limit 64)
    sem = await admission.get_tier_semaphore("search", 64)
    drain_tasks = [asyncio.create_task(sem.acquire()) for _ in range(64)]
    await asyncio.gather(*drain_tasks)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        start = time.perf_counter()
        r = await c.post("/v1/search", json={"query": "test"})
        took = time.perf_counter() - start

    # Release the permits
    for _ in range(64):
        sem.release()

    assert r.status_code == 503, f"Expected 503 OVERLOADED, got {r.status_code}"
    assert "Retry-After" in r.headers, "Missing Retry-After header on 503"
    assert took <= 2.3, f"Saturated admission did not fail fast: took {took:.2f}s"


@pytest.mark.asyncio
async def test_context_requires_admission():
    """Locks NAMED['C'] context admission: rejects with 503 when context saturated."""
    reset_admission()
    _install_stack([_MockAdapter("fake", [_raw("https://example.com/1")])])

    sem = await admission.get_tier_semaphore("context", 16)
    drain_tasks = [asyncio.create_task(sem.acquire()) for _ in range(16)]
    await asyncio.gather(*drain_tasks)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/v1/context", json={"query": "test", "k": 2})

    for _ in range(16):
        sem.release()

    assert r.status_code == 503, f"Expected 503 OVERLOADED for context, got {r.status_code}"
    assert "Retry-After" in r.headers, "Missing Retry-After header on 503"


@pytest.mark.asyncio
async def test_answer_costs_5_in_rate_limit(monkeypatch):
    """Locks NAMED['C'] answer cost: verify answer costs 5 against rate limit window."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    get_settings.cache_clear()
    reset_admission()
    reset_rate_limit_windows()
    _install_stack([_MockAdapter("fake", [_raw("https://example.com/1")])])

    # Key has limit of 6 RPM. Search costs 1 (so 6 searches allowed). Answer costs 5.
    key, _ = await create_key("tight_key", rate_limit_rpm=6, max_concurrent=5)
    headers = {"Authorization": f"Bearer {key}"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # First answer costs 5: succeeds (1 remaining)
        r1 = await c.post("/v1/answer", json={"query": "q1", "stream": False}, headers=headers)
        assert r1.status_code == 200, f"First answer failed: {r1.status_code}"

        # Second answer costs 5: 5 + 5 = 10 > 6 -> must fail with 429 RATE_LIMITED!
        # (If cost were 1 like search, 1 + 1 = 2 <= 6 and it would return 200)
        r2 = await c.post("/v1/answer", json={"query": "q2", "stream": False}, headers=headers)
        assert r2.status_code == 429, (
            f"Expected 429 RATE_LIMITED on 2nd answer, got {r2.status_code}"
        )


@pytest.mark.asyncio
async def test_deadline_exceeded_504():
    """Verify that when a request budget expires without results, 504 is returned."""
    reset_admission()
    adapter = _MockAdapter("failing", [_raw("https://example.com/1")], delay=1.2, fail=True)
    _install_stack([adapter])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/v1/search", json={"query": "test", "timeout_ms": 1000})
        assert r.status_code == 504, f"Expected 504 DEADLINE_EXCEEDED, got {r.status_code}"
        data = r.json()
        assert data.get("error", {}).get("code") == ErrorCode.DEADLINE_EXCEEDED.value


@pytest.mark.asyncio
async def test_malformed_retry_after_stays_llm_error():
    """Verify that a malformed Retry-After header does not crash and stays an LLMError."""
    messages = [{"role": "user", "content": "hi"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "1.2.3"}, json={})

    llm_breakers._breakers.clear()
    gw = LLMGateway(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        use_runtime_store=False,
    )

    with pytest.raises(LLMError) as exc_info:
        async for _ in gw.stream(messages):
            pass

    assert isinstance(exc_info.value, LLMError)
    assert not isinstance(exc_info.value, ValueError)


@pytest.mark.asyncio
async def test_upstream_timeout_counts_as_timeout(monkeypatch):
    """Verify that upstream httpx.ReadTimeout increments consecutive_timeouts on the breaker."""
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("CUSTOM_LLM_BASE_URL", "https://llm.test.invalid/v1")
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_API_KEY", "k")
    get_settings.cache_clear()

    messages = [{"role": "user", "content": "hi"}]

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    llm_breakers._breakers.clear()
    gw = LLMGateway(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)),
        use_runtime_store=False,
    )

    with pytest.raises(LLMError):
        async for _ in gw.stream(messages):
            pass

    counted = [b.consecutive_timeouts for b in llm_breakers._breakers.values()]
    assert bool(counted) and max(counted) >= 1, f"Breaker timeouts were not counted: {counted}"


@pytest.mark.asyncio
async def test_fast_failure_under_load():
    """Verify overflow requests fail fast with 503 and Retry-After <= 2.3s."""
    reset_admission()

    class HangPipe:
        async def run(self, params, **kw):
            await asyncio.sleep(10)
            yield ("done", {"answer_id": "x"})

    deps.get_answer_pipeline = lambda: HangPipe()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        held = [
            asyncio.create_task(c.post("/v1/answer", json={"query": "q", "stream": False}))
            for _ in range(16)
        ]
        await asyncio.sleep(0.1)

        start = time.perf_counter()
        r = await c.post("/v1/answer", json={"query": "overflow", "stream": False})
        took = time.perf_counter() - start

        for t in held:
            t.cancel()
        await asyncio.gather(*held, return_exceptions=True)

    assert r.status_code == 503, f"Expected 503, got {r.status_code}"
    assert bool(r.headers.get("retry-after")), "Missing Retry-After header"
    assert took <= 2.3, f"Overload rejection was too slow: took {took:.2f}s"


@pytest.mark.asyncio
async def test_answer_json_deadline_exceeded_504():
    """Verify non-streaming /v1/answer returns 504 DEADLINE_EXCEEDED when budget expires."""
    reset_admission()
    _install_stack([_MockAdapter("fake", [_raw("https://example.com/1")])])

    class DelayedEmptyPipe:
        async def run(self, params, **kw):
            await asyncio.sleep(1.05)
            if False:
                yield ("token", {})

    deps.get_answer_pipeline = lambda: DelayedEmptyPipe()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/v1/answer",
            json={"query": "test", "stream": False, "timeout_ms": 1000},
        )
    assert r.status_code == 504
    assert r.json().get("error", {}).get("code") == ErrorCode.DEADLINE_EXCEEDED.value


@pytest.mark.asyncio
async def test_reset_admission_clears_key_semaphores():
    """Verify reset_admission clears per-key semaphores."""
    await admission.get_key_semaphore("test_key", 5)
    assert "test_key" in admission._key_semaphores
    reset_admission()
    assert len(admission._key_semaphores) == 0


@pytest.mark.asyncio
async def test_acquire_admit_slot_rolls_back_key_semaphore_on_tier_timeout():
    """Verify that when tier admission times out, acquired key permit is rolled back."""
    reset_admission()
    from app.core.auth import AuthContext, current_auth_context

    key_ctx = AuthContext(
        key_id="rollback_key", name="test", role="user", rate_limit_rpm=100, max_concurrent=2
    )
    current_auth_context.set(key_ctx)

    sem = await admission.get_tier_semaphore("search", 64)
    drains = [asyncio.create_task(sem.acquire()) for _ in range(64)]
    await asyncio.gather(*drains)

    with pytest.raises(MetaSearchError) as exc_info:
        await admission.acquire_admit_slot("search", custom_wait_s=0.01)
    assert exc_info.value.status_code == 503

    key_sem = await admission.get_key_semaphore("rollback_key", 2)
    assert key_sem._value == 2, f"Key semaphore was not rolled back: {key_sem._value}"

    for _ in range(64):
        sem.release()


@pytest.mark.asyncio
async def test_authenticate_uses_cached_state_auth():
    """Verify authenticate_and_rate_limit sets current_auth_context from request.state.auth."""
    from app.core.auth import AuthContext, authenticate_and_rate_limit, current_auth_context

    dummy_auth = AuthContext(
        key_id="k_cached", name="Cached", role="user", rate_limit_rpm=10, max_concurrent=3
    )

    class DummyState:
        auth = dummy_auth

    class DummyReq:
        state = DummyState()
        headers = {}

    current_auth_context.set(None)
    ctx = await authenticate_and_rate_limit(DummyReq())
    assert ctx.key_id == "k_cached"
    assert current_auth_context.get() is dummy_auth


@pytest.mark.asyncio
async def test_gateway_stream_once_timeout_capped_by_budget():
    """Verify that _stream_once caps client.stream timeout to remaining budget."""
    from contextlib import asynccontextmanager

    from app.config import Settings

    gw = LLMGateway(Settings(llm_provider="pollinations"), use_runtime_store=False)
    target = gw.resolve_target()
    captured_timeout = None

    @asynccontextmanager
    async def mock_stream(*args, **kwargs):
        nonlocal captured_timeout
        captured_timeout = kwargs.get("timeout")
        yield httpx.Response(200, text="data: {}\n\n")

    dummy_client = type("Client", (), {"stream": mock_stream})()
    gw._client = dummy_client

    with use_budget(RequestBudget(0.35)):
        try:
            async for _ in gw._stream_once(
                target,
                [{"role": "user", "content": "hi"}],
                "openai",
                max_tokens=10,
                temperature=0.1,
            ):
                pass
        except Exception:
            pass

    assert captured_timeout is not None
    assert captured_timeout <= 0.4


@pytest.mark.asyncio
async def test_gateway_stream_once_raises_llm_timeout_error():
    """Verify that _stream_once converts httpx.TimeoutException into LLMTimeoutError."""
    from contextlib import asynccontextmanager

    from app.config import Settings
    from app.llm.gateway import LLMTimeoutError

    gw = LLMGateway(Settings(llm_provider="pollinations"), use_runtime_store=False)
    target = gw.resolve_target()

    @asynccontextmanager
    async def timeout_stream(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")
        yield

    dummy_client = type("Client", (), {"stream": timeout_stream})()
    gw._client = dummy_client

    with pytest.raises(LLMTimeoutError):
        async for _ in gw._stream_once(
            target, [{"role": "user", "content": "hi"}], "openai", max_tokens=10, temperature=0.1
        ):
            pass
