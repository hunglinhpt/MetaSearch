"""docs/36 A0/A0b: /v1/answer on a keyless, capped LLM provider.

Measured 2026-11-26 on the free Pollinations Classic API: HTTP 413
"16,000 characters max", HTTP 402 FREE_LIMIT "Retry after 40s", HTTP 429
"1 request at a time per IP". Before these fixes one 413 surfaced as a 500,
fed the breaker, and every later answer was a 503 "cooling down"."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

import app.llm.gateway as gw_mod
import app.orchestration.pipeline as pipeline_mod
from app.api import deps
from app.config import get_settings
from app.db.bootstrap import ensure_schema
from app.db.session import dispose_engine
from app.extraction.chunker import Chunk
from app.extraction.scraper import PageContent, PageFailure
from app.llm.gateway import LLMGateway, LLMStatusError, llm_breakers
from app.llm.providers import LLMOverride, parse_council_endpoint_specs
from app.main import app
from app.orchestration.engine_store import InMemoryEngineStore
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import HeuristicReranker

URLS = [f"https://a.example/{i}" for i in range(6)]
_SWAPPED = (
    "get_engine_store",
    "build_adapters",
    "get_scraper",
    "get_gateway",
    "get_embedder",
    "get_reranker",
)
_413 = {
    "status": 413,
    "error": {
        "code": "PAYLOAD_TOO_LARGE",
        "message": "Prompt too long for the free Classic API: 16,000 characters max",
    },
}


def _ok(text: str = "Postgres uses btree [1].") -> httpx.Response:
    body = f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _prompt_len(request: httpx.Request) -> int:
    body = json.loads(request.content)
    return sum(len(m["content"]) for m in body["messages"])


class _Adapter:
    id = "fake"

    async def search(self, query, **kwargs):
        return [
            RawResult(
                url=u,
                title=f"Postgres {i}",
                snippet="postgres btree",
                adapter="fake",
                source_engines=[],
                rank=i,
            )
            for i, u in enumerate(URLS)
        ]


def _install(handler) -> LLMGateway:
    store = InMemoryEngineStore(
        {"fake": EngineConfig(id="fake", name="fake", category="general", enabled=True)}
    )

    def page(u: str) -> PageContent:
        return PageContent(
            url=u,
            final_url=u,
            title="T",
            markdown="# T\n\n" + "postgres btree index pages " * 600,
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        )

    class Scraper:
        async def scrape_many(self, urls, **kwargs):
            return [
                page(u) if u in URLS else PageFailure(url=u, status="failed", error="x")
                for u in urls
            ]

    class Embedder:
        enabled = False

    gw = LLMGateway(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        use_runtime_store=False,
    )
    deps.get_engine_store = lambda: store
    deps.build_adapters = lambda s: {"fake": _Adapter()}
    deps.get_scraper = lambda: Scraper()
    deps.get_gateway = lambda: gw
    deps.get_embedder = lambda: Embedder()
    deps.get_reranker = lambda: HeuristicReranker()
    deps.reset_pipelines()
    return gw


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'd.db').as_posix()}")
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    monkeypatch.setenv("LLM_PROVIDER", "pollinations")
    monkeypatch.setenv("LLM_ENDPOINT_POLICY", "open")
    monkeypatch.setenv("ANSWER_CONTEXT_BUDGET", "20000")
    get_settings.cache_clear()
    await dispose_engine()
    await ensure_schema()
    llm_breakers._breakers.clear()
    gw_mod._learned_prompt_limits.clear()
    gw_mod._inflight_slots.clear()
    saved = {n: getattr(deps, n) for n in _SWAPPED}
    yield
    for n, v in saved.items():
        setattr(deps, n, v)
    deps.reset_pipelines()
    llm_breakers._breakers.clear()
    gw_mod._learned_prompt_limits.clear()
    await dispose_engine()
    get_settings.cache_clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30
    )


async def test_prompt_is_fitted_to_the_documented_cap():
    seen: list[int] = []

    def handler(request):
        seen.append(_prompt_len(request))
        return httpx.Response(413, json=_413) if seen[-1] > 16_000 else _ok()

    _install(handler)
    async with _client() as c:
        r = await c.post("/v1/answer", json={"query": f"postgres btree {uuid.uuid4().hex}"})
    assert r.status_code == 200, r.text
    assert seen and all(n <= 16_000 for n in seen)  # never sent an oversized prompt
    assert len(seen) == 1  # one-slot provider: no expansion, no follow-ups


async def test_413_is_request_scoped_and_answer_retries_with_half_the_context(monkeypatch):
    # No documented cap: the first prompt goes out whole and is rejected; the cap
    # is learned from the 413 body and the retry fits under it.
    import dataclasses

    from app.llm.providers import LLM_PROVIDER_PRESETS

    uncapped = dataclasses.replace(LLM_PROVIDER_PRESETS["pollinations"], max_prompt_chars=None)
    monkeypatch.setitem(LLM_PROVIDER_PRESETS, "pollinations", uncapped)
    seen: list[int] = []

    def handler(request):
        seen.append(_prompt_len(request))
        return httpx.Response(413, json=_413) if seen[-1] > 16_000 else _ok()

    _install(handler)
    async with _client() as c:
        r = await c.post("/v1/answer", json={"query": f"postgres btree {uuid.uuid4().hex}"})
    assert r.status_code == 200, r.text
    assert len(seen) == 2 and seen[0] > 16_000 >= seen[1]
    assert gw_mod._learned_prompt_limits["pollinations"] == 16_000  # parsed from the body
    breaker = llm_breakers.get("llm:pollinations")
    assert breaker.state == "closed" and breaker.consecutive_failures == 0
    # The learned cap applies up front next time: a single, fitted call.
    async with _client() as c:
        r2 = await c.post("/v1/answer", json={"query": f"postgres btree {uuid.uuid4().hex}"})
    assert r2.status_code == 200 and len(seen) == 3 and seen[2] <= 16_000


async def test_413_with_no_room_left_is_502_not_500():
    def handler(request):
        return httpx.Response(413, json={"error": "too large"})

    _install(handler)
    async with _client() as c:
        r = await c.post("/v1/answer", json={"query": f"postgres btree {uuid.uuid4().hex}"})
    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "upstream_error"
    assert llm_breakers.get("llm:pollinations").state == "closed"


async def test_free_limit_is_503_with_the_providers_wait():
    def handler(request):
        return httpx.Response(
            402,
            json={
                "error": {
                    "code": "FREE_LIMIT",
                    "message": "1 reply every 45 seconds per IP. Retry after 40s, or get a key",
                }
            },
        )

    _install(handler)
    async with _client() as c:
        r = await c.post("/v1/answer", json={"query": f"postgres btree {uuid.uuid4().hex}"})
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "overloaded"
    assert int(r.headers["retry-after"]) == 40


async def test_one_slot_provider_queues_instead_of_overlapping():
    active = 0
    peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return _ok("ok")

    gw = LLMGateway(
        get_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        request_override=LLMOverride(provider="pollinations"),
        use_runtime_store=False,
    )

    async def one():
        async for _ in gw.stream([{"role": "user", "content": "hi"}]):
            pass

    await asyncio.gather(*(one() for _ in range(3)))
    assert peak == 1 and gw.is_scarce()


def test_status_parsing():
    assert gw_mod._parse_retry_after(None, "Retry after 10s, or get") == 10.0
    assert gw_mod._parse_retry_after("7", "Retry after 10s") == 7.0
    assert gw_mod._parse_retry_after(None, "nothing") is None
    assert gw_mod._parse_limit_chars("16,000 characters max (about 4,000 tokens)") == 16_000
    assert gw_mod._parse_limit_chars("12 characters max") is None
    e = LLMStatusError("p", 503, retry_after_s=10)
    assert e.quota and e.code == "overloaded"
    assert LLMStatusError("p", 500).code == "upstream_error"
    assert LLMStatusError("p", 413).payload_too_large and not LLMStatusError("p", 413).retryable


def _chunk(i: int, n: int) -> tuple[int, Chunk]:
    return i, Chunk(
        id=f"c{i}",
        document_url=f"https://x/{i}",
        position=1,
        heading_path="",
        content="w" * n,
        word_count=1,
    )


def test_fit_prompt_drops_lowest_ranked_suffix_then_clips():
    def build(ctx):
        return [{"role": "user", "content": "".join(c.content for _, c in ctx)}]

    inc = [_chunk(i, 1000) for i in range(5)]
    kept, msgs = pipeline_mod._fit_prompt(inc, build, 3000)
    assert [i for i, _ in kept] == [0, 1] and gw_mod.prompt_chars(msgs) <= 2850
    kept, msgs = pipeline_mod._fit_prompt([_chunk(0, 9000)], build, 3000)
    assert len(kept) == 1 and gw_mod.prompt_chars(msgs) <= 2850
    kept, _ = pipeline_mod._fit_prompt(inc, build, None)
    assert kept == inc


def test_council_auto_panel_names_only_served_pollinations_models():
    panel = parse_council_endpoint_specs("", get_settings())
    models = [ep.model for ep in panel if ep.provider == "pollinations"]
    assert models == ["openai-fast"]


async def test_reranker_fail_open_is_reported():
    # docs/36 §17 (XERJ `_rerank.applied:false`): a reranker that fails open is
    # visible to the caller instead of silently becoming the heuristic order.
    from app.retrieval.reranker import RerankerUnavailable

    class _Broken:
        name = "bge"

        async def rerank(self, query, scored):
            raise RerankerUnavailable("model not downloaded")

    _install(lambda r: _ok())
    deps.get_reranker = lambda: _Broken()
    deps.reset_pipelines()
    async with _client() as c:
        r = await c.post("/v1/answer", json={"query": f"postgres btree {uuid.uuid4().hex}"})
    assert r.status_code == 200, r.text
    assert r.json()["rerank"] == {
        "reranker": "bge",
        "applied": False,
        "reason": "model not downloaded",
    }

    deps.get_reranker = lambda: HeuristicReranker()
    deps.reset_pipelines()
    async with _client() as c:
        r = await c.post("/v1/answer", json={"query": f"postgres btree {uuid.uuid4().hex}"})
    assert r.json()["rerank"]["applied"] is True


def test_provider_slots_never_leak_across_event_loops():
    # A slot leaked by an abandoned stream in one loop must not block the next loop
    # (keying by id(loop) once did: a new loop can reuse the old loop's address).
    async def leak():
        sem = gw_mod._slot("pollinations")
        await sem.acquire()
        return sem

    async def fresh():
        return gw_mod._slot("pollinations")

    leaked = asyncio.run(leak())
    current = asyncio.run(fresh())
    assert current is not leaked and not current.locked()
