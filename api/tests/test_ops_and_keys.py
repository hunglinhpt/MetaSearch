"""Worker recovery probe + misc endpoint guards + cache key semantics."""

from __future__ import annotations

import httpx

from app.cache import answer_cache_key, search_ttl_s
from app.config import get_settings
from app.worker import _probe_adapter


class TestProbeAdapter:
    async def test_searxng_probe_via_healthz(self, monkeypatch):

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/healthz"
            return httpx.Response(200)

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr("app.worker.get_http_client", lambda *a, **kw: mock_client)
        ok = await _probe_adapter("searxng")
        assert ok is True

    async def test_unknown_engine_fails_probe(self):
        assert await _probe_adapter("ghost") is False

    async def test_adapter_error_fails_probe(self, monkeypatch):
        import app.api.deps as deps

        class ExplodingAdapter:
            id = "searxng"

            async def search(self, *args, **kwargs):
                raise RuntimeError("down")

        monkeypatch.setattr(deps, "build_adapters", lambda s: {"searxng": ExplodingAdapter()})
        assert await _probe_adapter("searxng") is False

    async def test_tavily_probe_uses_search(self, monkeypatch):
        class ProbeAdapter:
            id = "tavily"

            def __init__(self):
                self.calls = []

            async def search(self, query, **kwargs):
                self.calls.append((query, kwargs))
                return [{"url": "https://x"}]

        adapter = ProbeAdapter()
        import app.api.deps as deps

        monkeypatch.setattr(deps, "build_adapters", lambda s: {"tavily": adapter})
        assert await _probe_adapter("tavily") is True
        assert adapter.calls[0][1]["max_results"] == 1


class TestQueriesEndpointGuards:
    def test_invalid_uuid_is_400(self, client):
        resp = client.get("/v1/queries/not-a-uuid")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"


class TestCacheKeySemantics:
    def test_search_ttl_news_branch(self):
        settings = get_settings()
        assert search_ttl_s(settings, news=True) == settings.cache_news_ttl_s
        assert search_ttl_s(settings, news=False) == settings.cache_search_ttl_s
        assert search_ttl_s(settings, news=True) < search_ttl_s(settings, news=False)

    def test_answer_key_varies_with_search_params(self):
        a = answer_cache_key("q", model="m", lang="vi", k=10)
        b = answer_cache_key("q", model="m", lang="vi", k=5)
        c = answer_cache_key("q", model="m", lang="vi", k=10, freshness_days=7)
        assert len({a, b, c}) == 3

    def test_answer_key_stable_for_same_params(self):
        a = answer_cache_key("q", model="m", lang="vi", k=10, categories=["general"])
        b = answer_cache_key("q", model="m", lang="vi", k=10, categories=["general"])
        assert a == b
