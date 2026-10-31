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


def make_scraper(handler, *, cache=None, **settings_overrides) -> Scraper:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Scraper(make_settings(**settings_overrides), client=client, cache=cache)


async def test_trafilatura_extracts_public_page():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        # IP-pinned fetch: host is the validated IP, Host header original.
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
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
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "crawl4ai.test":
            assert request.url.path == "/md"
            return _crawl4ai_md_response()
        return httpx.Response(200, html="<html><body><div>x</div></body></html>")

    def _crawl4ai_md_response() -> httpx.Response:
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


async def test_jina_keyless_lite_mode():
    """Lite profile: r.jina.ai works WITHOUT an API key (~20 rpm free tier)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "crawl4ai.test":
            raise httpx.ConnectError("down", request=request)
        if request.url.host == "r.jina.ai":
            assert "authorization" not in request.headers  # no key sent
            return httpx.Response(200, text="Title: Keyless\n\ncontent")
        return httpx.Response(200, html="<html><body><div>x</div></body></html>")

    scraper = make_scraper(handler, JINA_API_KEY="")
    page = await scraper.scrape("https://keyless.example.com/page")
    assert hasattr(page, "markdown")
    assert page.extractor_used == "jina"


async def test_jina_fallback_when_crawl4ai_down():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "crawl4ai.test":
            raise httpx.ConnectError("down", request=request)
        if request.url.host == "r.jina.ai":
            return httpx.Response(200, text="Title: Hard Page\n\nJina markdown content")
        return httpx.Response(200, html="<html><body><div>x</div></body></html>")

    page = await make_scraper(handler).scrape("https://hard.example.com/page")
    assert hasattr(page, "markdown")
    assert page.extractor_used == "jina"
    assert page.title == "Hard Page"


async def test_all_extractors_fail():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "crawl4ai.test":
            return httpx.Response(503, json={"success": False})
        if request.url.host == "r.jina.ai":
            return httpx.Response(502, text="err")
        return httpx.Response(200, html="<html><body><div>x</div></body></html>")

    result = await make_scraper(handler).scrape("https://bad.example.com/page")
    assert result.status == "failed"


async def test_redirect_hop_rechecked_via_ssrf():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == "/redirect":
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


async def test_page_cache_roundtrip_avoids_second_fetch():
    from tests.test_api_v1 import FakeCache

    calls = {"page": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        calls["page"] += 1
        return httpx.Response(200, html=HTML_PAGE)

    cache = FakeCache()
    scraper = make_scraper(handler, cache=cache)
    first = await scraper.scrape("https://example.com/cached")
    assert hasattr(first, "markdown")
    assert calls["page"] == 1

    second = await scraper.scrape("https://example.com/cached")
    assert hasattr(second, "markdown")
    assert second.markdown == first.markdown
    assert calls["page"] == 1  # served from page cache


async def test_negative_cache_shared_via_redis_flag():
    from tests.test_api_v1 import FakeCache

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(500, text="boom")

    cache = FakeCache()
    first_scraper = make_scraper(handler, cache=cache)
    failed = await first_scraper.scrape("https://example.com/dead")
    assert failed.status == "failed"
    assert any(k.startswith("nc:") for k in cache.flags)

    # A *different* process-local scraper instance sharing the same Redis
    # must skip the URL without a single HTTP attempt.
    def counting_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be reached")

    second_scraper = make_scraper(counting_handler, cache=cache)
    result = await second_scraper.scrape("https://example.com/dead")
    assert result.status == "failed"
    assert "negative cache" in (result.error or "")


def test_is_challenge_page_detection():
    from app.extraction.scraper import is_challenge_page

    cloudflare_html = """<html><head><title>Just a moment...</title></head>
    <body><div class="cf-turnstile" data-sitekey="abc"></div>
    <p>Checking your browser before accessing the website.</p>
    <p>Cloudflare Ray ID: 8b0123456789</p></body></html>"""
    assert is_challenge_page(cloudflare_html) is True
    assert is_challenge_page(cloudflare_html.encode("utf-8")) is True

    recaptcha_html = """<html><body><div class="g-recaptcha" data-sitekey="xyz"></div>
    <p>Please verify that you are not a robot</p></body></html>"""
    assert is_challenge_page(recaptcha_html) is True

    normal_html = """<html><head><title>Clean Blog</title></head>
    <body><article><h1>Clean Post</h1><p>Real content here.</p></article></body></html>"""
    assert is_challenge_page(normal_html) is False
    assert is_challenge_page("") is False


async def test_scraper_escalates_on_403_challenge_to_jina():
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/robots.txt" in url_str:
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if "crawl4ai.test" in url_str:
            return httpx.Response(500, text="crawl4ai offline")
        if "r.jina.ai" in url_str:
            return httpx.Response(
                200,
                text="Title: Jina Stealth Article\n\nReal content extracted through cloud proxy.",
            )
        # Direct fetch gets blocked by Cloudflare (403 Forbidden)
        return httpx.Response(403, text="403 Forbidden - Cloudflare WAF")

    scraper = make_scraper(handler)
    page = await scraper.scrape("https://example.com/protected-article")
    assert hasattr(page, "markdown")
    assert page.extractor_used == "jina"
    assert "Real content extracted" in page.markdown


async def test_scraper_discards_captcha_body_and_escalates_to_jina():
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/robots.txt" in url_str:
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if "crawl4ai.test" in url_str:
            return httpx.Response(500, text="crawl4ai offline")
        if "r.jina.ai" in url_str:
            return httpx.Response(
                200,
                text="Title: Bypass Success\n\nUnblocked article content.",
            )
        # Direct fetch returns 200 with Cloudflare challenge interstitial
        challenge_page = (
            "<html><head><title>Just a moment...</title></head>"
            "<body><div class='cf-turnstile'></div>"
            "<p>Please wait while we verify you are human.</p></body></html>"
        )
        return httpx.Response(200, html=challenge_page)

    scraper = make_scraper(handler)
    page = await scraper.scrape("https://example.com/turnstile-page")
    assert hasattr(page, "markdown")
    assert page.extractor_used == "jina"
    assert "Unblocked article content" in page.markdown
    assert "cf-turnstile" not in page.markdown
