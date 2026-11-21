"""docs/32 Lite upgrades — Bing Web RSS adapter, Last30Days engine configs,
and crawl4ai local fast-fail cooldown for the Lite extraction chain.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.extraction.scraper import _is_local_crawl4ai
from app.orchestration.engine_store import ADAPTER_PATHS, default_configs
from app.retrieval.adapters.bing_web import BingWebSearchAdapter

_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<title>Bing: test</title>
<item><title>Result One &amp; More</title><link>https://example.org/one</link>
<description>First result snippet</description>
<pubDate>Fri, 20 Nov 2026 00:00:00 GMT</pubDate></item>
<item><title>Wrapped</title><link>https://www.bing.com/apiclick.aspx?url=https%3A%2F%2Freal.example%2Ftwo</link>
<description>Wrapped result</description></item>
<item><title>Dupe</title><link>https://example.org/one</link><description>dupe</description></item>
<item><title>No link</title><description>broken</description></item>
</channel></rss>"""


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_bing_web_rss_happy_path_unwrap_dedupe():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "format=rss" in str(request.url)
        assert request.url.params["q"] == "vietnam market"
        return httpx.Response(200, text=_RSS)

    results = await BingWebSearchAdapter(client=_client(handler)).search(
        "vietnam market", max_results=10
    )
    assert [r.title for r in results] == ["Result One & More", "Wrapped"]
    assert results[0].url == "https://example.org/one"
    # apiclick wrapper unwrapped to the real publisher URL
    assert results[1].url == "https://real.example/two"
    assert results[0].published_at is not None
    assert {r.adapter for r in results} == {"bing_web"}


@pytest.mark.asyncio
async def test_bing_web_blocked_raises():
    from app.retrieval.adapters.base import AdapterError
    from app.retrieval.health import Outcome

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(AdapterError) as exc_info:
        await BingWebSearchAdapter(client=_client(handler)).search("test")
    assert exc_info.value.kind == Outcome.BLOCKED


def test_new_engine_configs_present_and_enabled():
    cfgs = default_configs(get_settings())
    for engine in ("bing_web", "polymarket", "stocktwits", "careers"):
        assert engine in cfgs, f"{engine} missing from default_configs"
        assert cfgs[engine].enabled, f"{engine} must be enabled by default (keyless)"
        assert engine in ADAPTER_PATHS
    # USPTO retired the keyless endpoint (301 probe 2026-11-21): the new
    # api.uspto.gov host requires a token — engines stay OFF without a key
    # (no dead upstream calls in the default fan-out).
    assert cfgs["uspto"].enabled == bool(getattr(get_settings(), "uspto_api_key", ""))
    assert cfgs["uspto"].requires_key is True
    assert cfgs["uspto_company"].requires_key is True
    # The query-blind trending adapters must NOT join the default fan-out.
    assert "polymarket_trending" not in cfgs
    assert "stocktwits_trending" not in cfgs


def test_is_local_crawl4ai():
    assert _is_local_crawl4ai("http://127.0.0.1:11235")
    assert _is_local_crawl4ai("http://localhost:11235")
    assert not _is_local_crawl4ai("http://crawl4ai:11222")  # docker hostname
    assert not _is_local_crawl4ai("http://crawl4ai.internal:11222")


@pytest.mark.asyncio
async def test_crawl4ai_local_refused_cooldown_300s(monkeypatch):
    """docs/32 Lite: a refused LOCAL crawl4ai means the tier is absent —
    the scraper must cool down 300s (not 60s) so later requests skip it."""
    from app.config import get_settings
    from app.extraction.scraper import Scraper

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    settings = get_settings()
    scraper = Scraper(settings, client=_client(handler))
    scraper._crawl4ai_down_until = 0.0
    monkeypatch.setattr(settings, "crawl4ai_url", "http://127.0.0.1:11235", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = await scraper._crawl4ai("https://example.com/page", 5.0)
    assert result is None
    remaining = scraper._crawl4ai_down_until
    import time

    assert remaining > time.monotonic() + 200, "local refused must cool down ~300s"
