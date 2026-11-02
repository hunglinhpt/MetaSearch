"""Tavily adapter tests with httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.tavily import TavilyAdapter

PAYLOAD = {
    "results": [
        {
            "title": "PostgreSQL docs",
            "url": "https://postgresql.org/docs",
            "content": "The official docs.",
            "score": 0.87,
            "published_date": "2025-03-01T00:00:00Z",
        },
        {"title": "No url", "content": "skipped"},
    ]
}


@pytest.fixture
def adapter():
    """TavilyAdapter whose HTTP requests go through a MockTransport."""

    def _make(handler) -> TavilyAdapter:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return TavilyAdapter("tvly-key", client=client)

    return _make


async def test_parses_results(adapter):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["query"] == "postgres"
        assert request.headers["authorization"] == "Bearer tvly-key"
        return httpx.Response(200, json=PAYLOAD)

    results = await adapter(handler).search("postgres", max_results=10)
    assert len(results) == 1
    assert results[0].adapter == "tavily"
    assert results[0].source_engines == ["tavily"]
    assert results[0].adapter_score == 0.87
    assert results[0].published_at is not None


async def test_news_topic_and_days(adapter):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["topic"] == "news"
        assert body["days"] == 7
        return httpx.Response(200, json=PAYLOAD)

    await adapter(handler).search("postgres", categories=["news"], freshness_days=7)


async def test_auth_error_not_retried(adapter):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"detail": "bad key"})

    with pytest.raises(AdapterError):
        await adapter(handler).search("postgres")
    assert calls["n"] == 1


async def test_timeout_retried_once(adapter):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("boom", request=request)

    with pytest.raises(AdapterError):
        await adapter(handler).search("postgres", timeout_s=0.1)
    assert calls["n"] == 2


async def test_non_json_body_raises(adapter):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(AdapterError):
        await adapter(handler).search("postgres")
