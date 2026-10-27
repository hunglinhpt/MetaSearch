"""Endpoint + pipeline tests with fakes injected through app.api.deps."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

import app.api.deps as deps
from app.config import get_settings
from app.core.errors import MetaSearchError
from app.extraction.scraper import PageContent, PageFailure
from app.llm.gateway import LLMError, StreamDelta, Usage
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import SearchParams, SearchPipeline
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.registry import EngineRegistry
from app.retrieval.reranker import HeuristicReranker


class FakeAdapter:
    def __init__(self, engine_id: str, results: list[RawResult], fail: bool = False):
        self.id = engine_id
        self.results = results
        self.fail = fail

    async def search(self, query, **kwargs) -> list[RawResult]:
        if self.fail:
            raise RuntimeError("engine exploded")
        return self.results


class FakeScraper:
    def __init__(self, pages: dict[str, PageContent]):
        self.pages = pages

    async def scrape_many(self, urls, **kwargs):
        return [
            self.pages.get(u) or PageFailure(url=u, status="failed", error="not found")
            for u in urls
        ]


class FakeGateway:
    provider = "openai"

    def __init__(self):
        self.captured_messages = None

    async def stream(self, messages, *, model, max_tokens=None):
        if self.provider == "none":
            raise LLMError("LLM provider is 'none': set LLM_PROVIDER plus an API key")
        self.captured_messages = messages
        yield StreamDelta(text="Postgres is relational [1]. MySQL too [2].")
        yield StreamDelta(text=" Both are [2] SQL databases.")
        yield StreamDelta(usage=Usage(input_tokens=50, output_tokens=12))


class FakeEmbedder:
    enabled = False

    async def embed(self, text):
        raise LLMError("embeddings disabled in tests")


class FakeCache:
    """In-memory CacheClient double with write recording."""

    def __init__(self, store: dict[str, Any] | None = None):
        self.enabled = True
        self.store: dict[str, Any] = store or {}
        self.flags: set[str] = set()
        self.written: list[str] = []

    async def get_json(self, key: str):
        return self.store.get(key)

    async def set_json(self, key: str, value: Any, *, ttl_s: int) -> None:
        self.store[key] = value
        self.written.append(key)

    async def has_flag(self, key: str) -> bool:
        return key in self.flags

    async def set_flag(self, key: str, *, ttl_s: int) -> None:
        self.flags.add(key)


@pytest.fixture
def stack(monkeypatch):
    """Patch deps with a controllable fake stack; returns the fakes holder."""

    class Stack:
        def __init__(self):
            self.adapters: list[FakeAdapter] = []
            self.pages: dict[str, PageContent] = {}
            self.gateway = FakeGateway()
            self.cache = FakeCache()

        def install(self):
            configs = {
                a.id: EngineConfig(id=a.id, name=a.id, category="general", enabled=True)
                for a in self.adapters
            }
            store = InMemoryEngineStore(configs)
            monkeypatch.setattr(deps, "get_engine_store", lambda: store)
            monkeypatch.setattr(
                deps, "build_adapters", lambda settings: {a.id: a for a in self.adapters}
            )
            monkeypatch.setattr(deps, "get_cache", lambda: self.cache)
            monkeypatch.setattr(deps, "get_scraper", lambda: FakeScraper(self.pages))
            monkeypatch.setattr(deps, "get_gateway", lambda: self.gateway)
            monkeypatch.setattr(deps, "get_embedder", lambda: FakeEmbedder())
            monkeypatch.setattr(deps, "get_reranker", lambda: HeuristicReranker())
            deps.reset_pipelines()

    return Stack()


def raw(
    url, engines=None, rank=0, title="PostgreSQL guide", snippet="postgres tutorial", adapter="fake"
):
    return RawResult(
        url=url,
        title=title,
        snippet=snippet,
        adapter=adapter,
        source_engines=engines or [],
        rank=rank,
    )


def fake_registry(*adapters: FakeAdapter) -> EngineRegistry:
    configs = {
        a.id: EngineConfig(id=a.id, name=a.id, category="general", enabled=True) for a in adapters
    }
    return EngineRegistry(InMemoryEngineStore(configs), {a.id: a for a in adapters})


def _page(url: str, title: str, md: str) -> PageContent:
    return PageContent(
        url=url,
        final_url=url,
        title=title,
        markdown=md,
        extractor_used="trafilatura",
        fetched_at=datetime.now(UTC),
    )


PAGE_MD = "# Overview\n\nPostgres is an object-relational database with strong SQL support.\n"


class TestSearchEndpoint:
    def test_search_merges_and_ranks(self, client, stack):
        stack.adapters = [
            FakeAdapter(
                "searxng",
                [
                    raw(
                        "https://a.example.com/1",
                        adapter="searxng",
                        engines=["google", "bing"],
                        rank=0,
                        title="PostgreSQL guide",
                        snippet="postgres tutorial",
                    ),
                    raw(
                        "https://b.example.com/2",
                        adapter="searxng",
                        engines=["ddg"],
                        rank=1,
                        title="Cooking pasta",
                        snippet="boil water add salt",
                    ),
                ],
            ),
            FakeAdapter(
                "tavily",
                [
                    raw(
                        "https://a.example.com/1?utm_source=x",
                        adapter="tavily",
                        engines=["ddg"],
                        rank=0,
                    ),
                ],
            ),
            FakeAdapter("broken", [], fail=True),
        ]
        stack.install()
        resp = client.post("/v1/search", json={"query": "postgres tutorial", "k": 5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["engines_used"] == ["searxng", "tavily"]
        assert data["engines_failed"] == ["broken"]
        assert data["results"][0]["url"] == "https://a.example.com/1"
        assert data["results"][0]["consensus"] == 3
        assert data["results"][0]["found_by"] == ["searxng", "tavily"]
        assert data["results"][0]["engines"] == ["bing", "ddg", "google"]
        assert data["cached"] is False
        assert data["latency_ms"] >= 0

    def test_search_cache_hit_replays(self, client, stack):
        stack.adapters = [FakeAdapter("searxng", [raw("https://a.com/1", rank=0)])]
        stack.install()
        # Warm the cache through a real request first.
        client.post("/v1/search", json={"query": "postgres"})
        assert stack.cache.written, "search result should be cached"
        second = client.post("/v1/search", json={"query": "postgres"})
        assert second.status_code == 200
        assert second.json()["cached"] is True
        assert second.json()["results"][0]["url"] == "https://a.com/1"

    def test_search_no_results_is_503(self, client, stack):
        stack.adapters = [FakeAdapter("searxng", [])]
        stack.install()
        resp = client.post("/v1/search", json={"query": "nothing to find"})
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "no_results"
        assert resp.json()["error"]["details"]["engines_status"]["searxng"] == "ok"

    def test_search_validation_error(self, client, stack):
        stack.install()
        resp = client.post("/v1/search", json={"query": ""})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"


class TestAnswerEndpoint:
    def _install(self, stack):
        stack.adapters = [
            FakeAdapter(
                "searxng",
                [
                    raw(
                        "https://one.example.com/a",
                        adapter="searxng",
                        engines=["google"],
                        rank=0,
                        title="PostgreSQL overview",
                        snippet="postgres object relational database",
                    ),
                    raw(
                        "https://two.example.com/b",
                        adapter="searxng",
                        engines=["bing"],
                        rank=1,
                        title="MySQL versus PostgreSQL",
                        snippet="mysql comparison with postgres",
                    ),
                ],
            )
        ]
        stack.pages = {
            "https://one.example.com/a": _page("https://one.example.com/a", "One", PAGE_MD),
            "https://two.example.com/b": _page("https://two.example.com/b", "Two", PAGE_MD),
        }
        stack.install()

    def test_answer_json(self, client, stack):
        self._install(stack)
        resp = client.post("/v1/answer", json={"query": "what is postgres", "stream": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "complete"
        assert data["cached"] is False
        assert "Postgres is relational [1]." in data["answer_md"]
        assert {c["n"] for c in data["citations"]} == {1, 2}
        assert data["citations"][0]["url"] == "https://one.example.com/a"
        assert len(data["sources"]) == 2
        assert data["model"] == get_settings().llm_quick_model

    def test_answer_sse_event_sequence(self, client, stack):
        self._install(stack)
        resp = client.post("/v1/answer", json={"query": "what is postgres", "stream": True})
        assert resp.status_code == 200
        events = []
        for line in resp.text.splitlines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
        assert events[0] == "status"
        assert "engine_done" in events
        assert "sources" in events
        assert events.count("token") == 2
        assert events[-2:] == ["citations", "done"]

        tokens = [
            json.loads(line.split("data:", 1)[1])
            for line in resp.text.splitlines()
            if line.startswith("data:") and '"t"' in line
        ]
        assert "".join(t["t"] for t in tokens).startswith("Postgres is relational")

    def test_answer_cache_replay(self, client, stack):
        self._install(stack)
        client.post("/v1/answer", json={"query": "what is postgres"})
        assert any(k.startswith("an:") for k in stack.cache.written)
        replay = client.post("/v1/answer", json={"query": "what is postgres"})
        assert replay.status_code == 200
        data = replay.json()
        assert data["cached"] is True
        assert "Postgres is relational [1]." in data["answer_md"]
        assert {c["n"] for c in data["citations"]} == {1, 2}

    def test_answer_requires_llm(self, client, stack):
        self._install(stack)
        stack.gateway.provider = "none"
        resp = client.post("/v1/answer", json={"query": "what is postgres"})
        assert resp.status_code == 500
        assert "LLM" in resp.json()["error"]["message"]


class TestScrapeEndpoint:
    def test_scrape_mixed_results(self, client, stack):
        stack.pages = {
            "https://ok.example.com/a": _page("https://ok.example.com/a", "OK", "# h\nbody")
        }
        stack.install()
        resp = client.post(
            "/v1/scrape", json={"urls": ["https://ok.example.com/a", "https://bad.example.com/x"]}
        )
        assert resp.status_code == 200
        items = resp.json()
        assert items[0]["status"] == "ok"
        assert items[0]["extractor_used"] == "trafilatura"
        assert items[1]["status"] == "failed"


class TestEnginesEndpoint:
    def test_lists_all_engines_with_health(self, client, stack):
        stack.adapters = [FakeAdapter("searxng", [])]
        stack.install()
        resp = client.get("/v1/engines")
        assert resp.status_code == 200
        engines = {e["id"]: e for e in resp.json()}
        assert engines["searxng"]["enabled"] is True
        assert engines["searxng"]["adapter_ready"] is True
        assert engines["searxng"]["consecutive_failures"] == 0

    def test_patch_engine_hot_config(self, client, stack):
        stack.adapters = [FakeAdapter("searxng", [raw("https://a.com/1", rank=0)])]
        stack.install()
        resp = client.patch("/v1/engines/searxng", json={"weight": 2.5, "timeout_ms": 1500})
        assert resp.status_code == 200
        data = resp.json()
        assert data["weight"] == 2.5
        assert data["timeout_ms"] == 1500

        # Disable then verify it no longer fans out.
        client.patch("/v1/engines/searxng", json={"enabled": False})
        resp2 = client.post("/v1/search", json={"query": "postgres"})
        assert resp2.status_code == 503  # no engines left

        # Re-enable via PATCH.
        client.patch("/v1/engines/searxng", json={"enabled": True})
        resp3 = client.post("/v1/search", json={"query": "postgres"})
        assert resp3.status_code == 200

    def test_patch_unknown_engine_404(self, client, stack):
        stack.install()
        resp = client.patch("/v1/engines/nope", json={"weight": 1.0})
        assert resp.status_code == 404

    def test_health_endpoint(self, client, stack):
        stack.adapters = [FakeAdapter("searxng", [raw("https://a.com/1", rank=0)])]
        stack.install()
        search_resp = client.post("/v1/search", json={"query": "postgres"})
        assert search_resp.status_code == 200
        resp = client.get("/v1/engines/health")
        assert resp.status_code == 200
        rows = {r["engine"]: r for r in resp.json()}
        assert rows["searxng"]["calls"] >= 1
        assert rows["searxng"]["successes"] >= 1


class TestSearchPipelineFanout:
    async def test_partial_failure_keeps_partial_results(self):
        ok = FakeAdapter("ok", [raw("https://a.com/1", rank=0)])
        bad = FakeAdapter("bad", [], fail=True)
        pipeline = SearchPipeline(get_settings(), fake_registry(ok, bad))
        outcome = await pipeline.run(SearchParams(query="postgres"))
        assert [r.url for r in outcome.results] == ["https://a.com/1"]
        statuses = {s.engine: s.status for s in outcome.statuses}
        assert statuses == {"ok": "ok", "bad": "error"}

    async def test_engine_filter(self):
        ok = FakeAdapter("ok", [raw("https://a.com/1", rank=0)])
        other = FakeAdapter("other", [raw("https://b.com/2", rank=0)])
        pipeline = SearchPipeline(get_settings(), fake_registry(ok, other))
        outcome = await pipeline.run(SearchParams(query="postgres", engines=["other"]))
        assert [r.url for r in outcome.results] == ["https://b.com/2"]

    async def test_unknown_engine_filter_raises_no_results(self):
        ok = FakeAdapter("ok", [raw("https://a.com/1", rank=0)])
        pipeline = SearchPipeline(get_settings(), fake_registry(ok))
        with pytest.raises(MetaSearchError):
            await pipeline.run(SearchParams(query="postgres", engines=["missing"]))

    async def test_auto_disable_after_consecutive_failures(self):
        bad = FakeAdapter("bad", [], fail=True)
        registry = fake_registry(bad)
        pipeline = SearchPipeline(get_settings(), registry)
        # Each failing call is recorded; empty results never raise here.
        for _ in range(3):
            outcome = await pipeline.run(SearchParams(query="postgres"))
            assert outcome.results == []
        snapshot = await registry.store.snapshot()
        assert snapshot["bad"].enabled is False
        assert "auto:" in (snapshot["bad"].disabled_reason or "")
        # With the only engine auto-disabled the next fan-out raises no_results.
        with pytest.raises(MetaSearchError):
            await pipeline.run(SearchParams(query="postgres"))

    async def test_auto_disabled_engine_not_fanned_out(self):
        bad = FakeAdapter("bad", [], fail=True)
        ok = FakeAdapter("ok", [raw("https://a.com/1", rank=0)])
        registry = fake_registry(bad, ok)
        pipeline = SearchPipeline(get_settings(), registry)
        for _ in range(3):
            await pipeline.run(SearchParams(query="postgres"))
        # bad is now auto-disabled; the next search only fans out to ok.
        outcome = await pipeline.run(SearchParams(query="postgres"))
        assert [r.url for r in outcome.results] == ["https://a.com/1"]
        assert all(s.engine != "bad" for s in outcome.statuses)
