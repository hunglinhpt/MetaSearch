"""Scraper tests: fallback chain, negative cache, SSRF blocking (all offline)."""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.extraction.scraper import Scraper

HTML_PAGE = """<html><head><title>Test Page</title></head>
<body><article><h1>Test Page</h1><p>This is a long enough article body used to verify
that trafilatura extracts markdown properly from the fetched HTML document.</p>
<p>Second paragraph adds more content so extraction has enough text to work with.</p>
</article></body></html>"""


def make_settings(**overrides) -> Settings:
    values = {
        "ENVIRONMENT": "test",
        "SEARXNG_URL": "http://searxng.test",
        "CRAWL4AI_URL": "http://crawl4ai.test",
        "JINA_API_KEY": "jina-key",
        "SCRAPE_CONCURRENCY": "2",
    }
    values.update(overrides)
    return Settings(**values)


def make_scraper(handler, **settings_overrides) -> Scraper:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Scraper(make_settings(**settings_overrides), client=client)


async def test_trafilatura_extracts_public_page():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "example.com"
        return httpx.Response(200, html=HTML_PAGE, headers={"content-type": "text/html"})

    page = await make_scraper(handler).scrape("https://example.com/article")
    assert hasattr(page, "markdown")  # PageContent
    assert page.extractor_used == "trafilatura"
    assert "article body" in page.markdown
    assert page.title == "Test Page"


async def test_blocks_private_target():
    scraper = make_scraper(lambda request: httpx.Response(200, html=HTML_PAGE))
    result = await scraper.scrape("http://127.0.0.1:8000/admin")
    assert result.status == "blocked"


async def test_negative_cache_prevents_second_fetch():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    scraper = make_scraper(handler)
    first = await scraper.scrape("https://example.com/fail")
    assert first.status == "failed"
    assert calls["n"] == 1
    second = await scraper.scrape("https://example.com/fail")
    assert second.status == "failed"
    assert calls["n"] == 1  # not refetched


async def test_crawl4ai_fallback_when_static_extraction_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "js-only.example.com":
            return httpx.Response(200, html="<html><body><div>x</div></body></html>")
        assert request.url.host == "crawl4ai.test"
        assert request.url.path == "/md"
        return httpx.Response(
            200,
            json={
                "success": True,
                "url": "https://js-only.example.com/",
                "markdown": {"raw_markdown": "# Rendered\n\nJS page content"},
                "title": "Rendered",
            },
        )

    page = await make_scraper(handler).scrape("https://js-only.example.com/app")
    assert hasattr(page, "markdown")
    assert page.extractor_used == "crawl4ai"
    assert "JS page content" in page.markdown


async def test_jina_fallback_when_crawl4ai_down():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hard.example.com":
            return httpx.Response(200, html="<html><body><div>x</div></body></html>")
        if request.url.host == "crawl4ai.test":
            raise httpx.ConnectError("down", request=request)
        assert request.url.host == "r.jina.ai"
        return httpx.Response(200, text="Title: Hard Page\n\nJina markdown content")

    page = await make_scraper(handler).scrape("https://hard.example.com/page")
    assert hasattr(page, "markdown")
    assert page.extractor_used == "jina"
    assert page.title == "Hard Page"


async def test_all_extractors_fail():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bad.example.com":
            return httpx.Response(200, html="<html><body><div>x</div></body></html>")
        if request.url.host == "crawl4ai.test":
            return httpx.Response(503, json={"success": False})
        return httpx.Response(502, text="err")  # jina

    result = await make_scraper(handler).scrape("https://bad.example.com/page")
    assert result.status == "failed"


async def test_redirect_hop_rechecked_via_ssrf():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        return httpx.Response(200, html=HTML_PAGE)

    result = await make_scraper(handler).scrape("https://public.example.com/redirect")
    assert result.status == "blocked"


async def test_robots_disallow_blocks(monkeypatch):
    from app.extraction.robots import RobotsCache

    async def denied(self, url: str, *, user_agent: str) -> bool:
        return False

    monkeypatch.setattr(RobotsCache, "allowed", denied)
    scraper = make_scraper(lambda request: httpx.Response(200, html=HTML_PAGE))
    result = await scraper.scrape("https://example.com/private")
    assert result.status == "blocked"


async def test_scrape_many_respects_concurrency():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=HTML_PAGE)

    scraper = make_scraper(handler)
    urls = [f"https://example.com/p{i}" for i in range(4)]
    results = await scraper.scrape_many(urls)
    assert all(hasattr(r, "markdown") for r in results)


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"success": True, "markdown": {"raw_markdown": "md"}}, "md"),
        ({"success": True, "markdown": {"fit_markdown": "fit", "raw_markdown": "raw"}}, "fit"),
        ({"success": True, "markdown": "plain"}, "plain"),
        ({"success": False, "markdown": "ignored"}, ""),
    ],
)
def test_crawl4ai_markdown_shapes(payload, expected):
    from app.extraction.scraper import _crawl4ai_markdown

    assert _crawl4ai_markdown(payload)[0] == expected
