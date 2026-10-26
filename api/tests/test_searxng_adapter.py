"""SearXNG adapter tests with httpx.MockTransport (no network)."""

from __future__ import annotations

import httpx
import pytest

from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.searxng import SearXNGAdapter

PAYLOAD = {
    "query": "postgres",
    "results": [
        {
            "url": "https://www.postgresql.org/docs/",
            "title": "PostgreSQL Documentation",
            "content": "The official PostgreSQL docs.",
            "engines": ["google", "bing"],
            "score": 12.5,
            "publishedDate": "2025-01-15T00:00:00Z",
        },
        {"url": "", "title": "no url result"},  # must be skipped
        {
            "url": "https://wiki.postgresql.org/",
            "title": "PostgreSQL wiki",
            "content": "Community wiki.",
            "engines": ["ddg"],
        },
    ],
}


def make_adapter(handler) -> SearXNGAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SearXNGAdapter("http://searxng.test", client=client)


async def test_parses_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        assert request.url.params["q"] == "postgres"
        return httpx.Response(200, json=PAYLOAD)

    results = await make_adapter(handler).search("postgres", max_results=15)
    assert len(results) == 2  # empty-url entry skipped
    assert results[0].source_engines == ["google", "bing"]
    assert results[0].adapter_score == 12.5
    assert results[0].published_at is not None
    assert results[1].published_at is None


async def test_sends_language_and_timerange():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["language"] == "vi"
        assert request.url.params["time_range"] == "week"
        return httpx.Response(200, json=PAYLOAD)

    await make_adapter(handler).search("postgres", lang="vi", freshness_days=7)


async def test_timeout_retries_once():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("boom", request=request)

    with pytest.raises(AdapterError):
        await make_adapter(handler).search("postgres", timeout_s=0.1)
    assert calls["n"] == 2  # initial + 1 retry


async def test_403_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")

    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).search("postgres")
    assert "403" in str(excinfo.value)
    assert calls["n"] == 1


async def test_http_error_raises_adapter_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(AdapterError):
        await make_adapter(handler).search("postgres")


async def test_max_results_respected():
    payload = {
        "results": [{"url": f"https://x.com/{i}", "title": "t", "content": "c"} for i in range(20)]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    results = await make_adapter(handler).search("postgres", max_results=5)
    assert len(results) == 5
