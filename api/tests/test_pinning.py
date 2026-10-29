"""Pin-IP connector tests: URL rewriting, Host/SNI preservation, live flow."""

from __future__ import annotations

import httpx
import pytest

from app.extraction.pinning import PinningError, build_pinned_request
from tests.test_scraper import HTML_PAGE, make_scraper


class TestBuildPinnedRequest:
    def test_http_pin_rewrites_host_keeps_path(self):
        pinned, headers, extensions = build_pinned_request(
            "http://example.com/page?a=1", "93.184.216.34"
        )
        assert pinned == "http://93.184.216.34/page?a=1"
        assert headers == {"Host": "example.com"}
        assert extensions == {}

    def test_https_pin_sets_sni_extension(self):
        pinned, headers, extensions = build_pinned_request(
            "https://example.com/docs", "93.184.216.34"
        )
        assert pinned == "https://93.184.216.34/docs"
        assert headers == {"Host": "example.com"}
        assert extensions == {"sni_hostname": "example.com"}

    def test_port_preserved_in_pin_and_host_header(self):
        pinned, headers, _ = build_pinned_request("http://example.com:8080/x", "1.2.3.4")
        assert pinned == "http://1.2.3.4:8080/x"
        assert headers == {"Host": "example.com:8080"}

    def test_ipv6_pinned_bracketed(self):
        pinned, _, extensions = build_pinned_request(
            "https://example.com/", "2606:2800:220:1:248:1893:25c8:1946"
        )
        assert pinned.startswith("https://[2606:2800:220:1:248:1893:25c8:1946]/")
        assert extensions["sni_hostname"] == "example.com"

    def test_rejects_bad_inputs(self):
        with pytest.raises(PinningError):
            build_pinned_request("ftp://example.com/", "1.2.3.4")
        with pytest.raises(PinningError):
            build_pinned_request("http://example.com/", "")


class TestScraperPinning:
    async def test_fetch_connects_via_validated_ip(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /")
            seen["host"] = request.url.host
            seen["header_host"] = request.headers.get("host")
            return httpx.Response(200, html=HTML_PAGE, headers={"content-type": "text/html"})

        page = await make_scraper(handler).scrape("https://example.com/article")
        assert hasattr(page, "markdown")
        # conftest fake_dns resolves every name to this public IP: the request
        # must go to the pinned IP, with the original host in the Host header.
        assert seen["host"] == "93.184.216.34"
        assert seen["header_host"] == "example.com"

    async def test_final_url_is_original_form_not_pinned(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /")
            return httpx.Response(200, html=HTML_PAGE)

        page = await make_scraper(handler).scrape("https://example.com/article")
        assert hasattr(page, "markdown")
        assert page.final_url == "https://example.com/article"
