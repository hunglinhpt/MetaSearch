"""Endpoint + pipeline tests with fakes injected through app.api.deps."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import app.api.deps as deps
from app.config import get_settings
from app.core.errors import MetaSearchError
from app.extraction.scraper import PageContent, PageFailure
from app.llm.gateway import LLMError, StreamDelta, Usage
from app.orchestration.pipeline import SearchParams, SearchPipeline
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.registry import EngineRegistry


class FakeAdapter:
    def __init__(self, engine_id: str, results: list[RawResult], fail: bool = False):
        self.id = engine_id
        self.results = results
        self.fail = fail

    async def search(self, query, **kwargs) -> list[RawResult]:
        if self.fail:
            raise RuntimeError("engine exploded")
        return self.results


def fake_registry(*adapters: FakeAdapter) -> EngineRegistry:
    entries = [(EngineConfig(id=a.id, name=a.id, category="general"), a) for a in adapters]
    return EngineRegistry(entries)


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


@pytest.fixture
def stack(monkeypatch):
    """Patch deps with a controllable fake stack; returns the fakes dict."""

    class Stack:
        def __init__(self):
            self.adapters: list[FakeAdapter] = []
            self.pages: dict[str, PageContent] = {}
            self.gateway = FakeGateway()

        def install(self):
            monkeypatch.setattr(
                deps.EngineRegistry,
                "from_settings",
                classmethod(lambda cls, settings: fake_registry(*self.adapters)),
            )
            monkeypatch.setattr(deps, "Scraper", lambda settings: FakeScraper(self.pages))
            monkeypatch.setattr(deps, "LLMGateway", lambda settings: self.gateway)
            deps.reset_pipelines()

    return Stack()


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
        assert data["latency_ms"] >= 0

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
                    raw("https://one.example.com/a", engines=["google"], rank=0),
                    raw("https://two.example.com/b", engines=["bing"], rank=1),
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

        # spot check SSE payload shape
        tokens = [
            json.loads(line.split("data:", 1)[1])
            for line in resp.text.splitlines()
            if line.startswith("data:") and '"t"' in line
        ]
        assert "".join(t["t"] for t in tokens).startswith("Postgres is relational")

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
    def test_lists_all_engines(self, client):
        resp = client.get("/v1/engines")
        assert resp.status_code == 200
        engines = {e["id"]: e for e in resp.json()}
        assert engines["searxng"]["enabled"] is True
        assert engines["searxng"]["adapter_ready"] is True
        assert engines["tavily"]["requires_key"] is True


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
