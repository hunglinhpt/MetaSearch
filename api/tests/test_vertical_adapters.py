"""Vertical + API-key adapters: arxiv, wikipedia, serper, brave, stackoverflow, reddit."""

from __future__ import annotations

import httpx
import pytest

from app.retrieval.adapters.arxiv import ArxivAdapter
from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.brave import BraveAdapter
from app.retrieval.adapters.reddit import RedditAdapter
from app.retrieval.adapters.serper import SerperAdapter
from app.retrieval.adapters.stackoverflow import StackOverflowAdapter
from app.retrieval.adapters.wikipedia import WikipediaAdapter

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <title>Attention Is All You Need</title>
    <summary>The dominant sequence transduction models are based on
    recurrent networks. We propose the Transformer.</summary>
    <published>2023-08-02T17:59:57Z</published>
  </entry>
  <entry>
    <title>Broken entry without id</title>
    <summary>skipped</summary>
  </entry>
</feed>"""


@pytest.fixture
def patched_httpx(monkeypatch):
    original = httpx.AsyncClient

    def _patch(module_name: str, handler):
        module = __import__(module_name, fromlist=["httpx"])

        def make_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(module.httpx, "AsyncClient", make_client)

    return _patch


class TestArxiv:
    async def test_parses_atom_entries(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["search_query"] == "all:transformer"
            return httpx.Response(200, text=ARXIV_ATOM)

        adapter = ArxivAdapter()
        patched_httpx("app.retrieval.adapters.arxiv", handler)
        results = await adapter.search("transformer", max_results=10)
        assert len(results) == 1  # entry without id skipped
        assert results[0].title == "Attention Is All You Need"
        assert "Transformer" in results[0].snippet
        assert results[0].source_engines == ["arxiv"]
        assert results[0].published_at is not None

    async def test_http_error_raises(self, patched_httpx):
        patched_httpx("app.retrieval.adapters.arxiv", lambda r: httpx.Response(503, text="x"))
        with pytest.raises(AdapterError):
            await ArxivAdapter().search("x")


class TestWikipedia:
    async def test_parses_search_hits(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "en.wikipedia.org"
            assert request.url.params["list"] == "search"
            return httpx.Response(
                200,
                json={
                    "query": {
                        "search": [
                            {
                                "title": "Python (programming language)",
                                "pageid": 23862,
                                "snippet": 'A <span class="searchmatch">high-level</span> language',
                                "timestamp": "2024-01-01T00:00:00Z",
                            }
                        ]
                    }
                },
            )

        patched_httpx("app.retrieval.adapters.wikipedia", handler)
        results = await WikipediaAdapter().search("python")
        assert results[0].url == "https://en.wikipedia.org/wiki/Python_(programming_language)"
        assert "<span" not in results[0].snippet
        assert "high-level" in results[0].snippet

    async def test_lang_from_param(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "vi.wikipedia.org"
            return httpx.Response(200, json={"query": {"search": []}})

        patched_httpx("app.retrieval.adapters.wikipedia", handler)
        await WikipediaAdapter().search("trí tuệ", lang="vi")


class TestSerper:
    async def test_parses_organic(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-API-KEY"] == "sp-key"
            return httpx.Response(
                200,
                json={"organic": [{"title": "RAG", "link": "https://x.com/a", "snippet": "s"}]},
            )

        patched_httpx("app.retrieval.adapters.serper", handler)
        results = await SerperAdapter("sp-key").search("rag")
        assert results[0].url == "https://x.com/a"

    async def test_auth_not_retried(self, patched_httpx):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(401, json={})

        patched_httpx("app.retrieval.adapters.serper", handler)
        with pytest.raises(AdapterError):
            await SerperAdapter("bad").search("x")
        assert calls["n"] == 1


class TestBrave:
    async def test_parses_web_results(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-Subscription-Token"] == "br-key"
            return httpx.Response(
                200,
                json={
                    "web": {"results": [{"title": "t", "url": "https://b.com", "description": "d"}]}
                },
            )

        patched_httpx("app.retrieval.adapters.brave", handler)
        results = await BraveAdapter("br-key").search("x")
        assert results[0].url == "https://b.com"

    async def test_rate_limited_error(self, patched_httpx):
        patched_httpx("app.retrieval.adapters.brave", lambda r: httpx.Response(429, json={}))
        with pytest.raises(AdapterError, match="429"):
            await BraveAdapter("k").search("x")


class TestStackOverflow:
    async def test_parses_items(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "api.stackexchange.com" in request.url.host
            assert request.url.params["q"] == "python asyncio"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "title": "How to run &quot;asyncio&quot; in Python?",
                            "link": "https://stackoverflow.com/questions/12345/how-to-run-asyncio",
                            "score": 42,
                            "is_answered": True,
                            "answer_count": 5,
                            "tags": ["python", "asyncio"],
                            "creation_date": 1700000000,
                        }
                    ]
                },
            )

        patched_httpx("app.retrieval.adapters.stackoverflow", handler)
        results = await StackOverflowAdapter().search("python asyncio")
        assert len(results) == 1
        assert results[0].url == "https://stackoverflow.com/questions/12345/how-to-run-asyncio"
        assert results[0].title == 'How to run "asyncio" in Python?'
        assert "[Answered]" in results[0].snippet
        assert "Score: 42" in results[0].snippet
        assert results[0].adapter == "stackoverflow"
        assert results[0].published_at is not None

    async def test_api_error_raises(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error_id": 502, "error_name": "throttle_violation"})

        patched_httpx("app.retrieval.adapters.stackoverflow", handler)
        with pytest.raises(AdapterError, match="throttle_violation"):
            await StackOverflowAdapter().search("x")


class TestReddit:
    async def test_parses_children(self, patched_httpx):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "reddit.com" in request.url.host
            assert request.url.params["q"] == "machine learning"
            assert request.headers["User-Agent"].startswith("MetaSearch")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "children": [
                            {
                                "data": {
                                    "title": "Discussion on &amp; ML models",
                                    "permalink": "/r/MachineLearning/comments/xyz123/discussion/",
                                    "subreddit_name_prefixed": "r/MachineLearning",
                                    "score": 150,
                                    "num_comments": 30,
                                    "selftext": "Here is a breakdown of recent findings...",
                                    "created_utc": 1700000000,
                                }
                            }
                        ]
                    }
                },
            )

        patched_httpx("app.retrieval.adapters.reddit", handler)
        results = await RedditAdapter().search("machine learning")
        assert len(results) == 1
        assert (
            results[0].url == "https://www.reddit.com/r/MachineLearning/comments/xyz123/discussion/"
        )
        assert results[0].title == "Discussion on & ML models"
        assert "r/MachineLearning" in results[0].snippet
        assert "150 points" in results[0].snippet
        assert results[0].adapter == "reddit"
        assert results[0].published_at is not None

    async def test_http_error_raises(self, patched_httpx):
        patched_httpx(
            "app.retrieval.adapters.reddit", lambda r: httpx.Response(429, text="Too Many Requests")
        )
        with pytest.raises(AdapterError, match="429"):
            await RedditAdapter().search("x")


class TestCategoryFanout:
    async def test_verticals_only_join_their_category(self):
        from app.config import get_settings
        from app.orchestration.engine_store import InMemoryEngineStore
        from app.orchestration.pipeline import SearchParams, SearchPipeline
        from app.retrieval.models import EngineConfig
        from app.retrieval.registry import EngineRegistry

        configs = {
            "searxng": EngineConfig(id="searxng", name="s", category="general", enabled=True),
            "arxiv": EngineConfig(id="arxiv", name="a", category="academic", enabled=True),
            "wikipedia": EngineConfig(id="wikipedia", name="w", category="reference", enabled=True),
            "stackoverflow": EngineConfig(
                id="stackoverflow", name="so", category="developer", enabled=True
            ),
            "reddit": EngineConfig(id="reddit", name="rd", category="social", enabled=True),
        }
        adapters = {
            "searxng": object(),
            "arxiv": object(),
            "wikipedia": object(),
            "stackoverflow": object(),
            "reddit": object(),
        }
        registry = EngineRegistry(InMemoryEngineStore(configs), adapters)

        class SpyAdapter:
            def __init__(self, engine_id):
                self.id = engine_id
                self.calls = []

            async def search(self, query, **kwargs):
                self.calls.append(query)
                return []

        spy_searxng = SpyAdapter("s")
        spy_arxiv = SpyAdapter("a")
        spy_wiki = SpyAdapter("w")
        spy_so = SpyAdapter("so")
        spy_rd = SpyAdapter("rd")
        registry._adapters = {
            "searxng": spy_searxng,
            "arxiv": spy_arxiv,
            "wikipedia": spy_wiki,
            "stackoverflow": spy_so,
            "reddit": spy_rd,
        }
        pipeline = SearchPipeline(get_settings(), registry)

        # General-only request: verticals sit out.
        await pipeline.run(SearchParams(query="q"))
        assert (
            spy_arxiv.calls == []
            and spy_wiki.calls == []
            and spy_so.calls == []
            and spy_rd.calls == []
            and spy_searxng.calls
        )

        # Academic request: arxiv joins.
        await pipeline.run(SearchParams(query="q", categories=["academic"]))
        assert len(spy_arxiv.calls) == 1

        # Developer request: stackoverflow joins.
        await pipeline.run(SearchParams(query="q", categories=["developer"]))
        assert len(spy_so.calls) == 1

        # Social request: reddit joins.
        await pipeline.run(SearchParams(query="q", categories=["social"]))
        assert len(spy_rd.calls) == 1

        # Explicit engines list overrides categories.
        await pipeline.run(SearchParams(query="q", engines=["wikipedia"]))
        assert len(spy_wiki.calls) == 1
