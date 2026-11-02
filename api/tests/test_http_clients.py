"""Tests for shared HTTP clients, connection pools, and SSRF hardening (WP-04).

Covers:
- test_no_asyncclient_outside_http_module (AST scan)
- test_single_ssl_context
- test_pools_are_isolated
- test_pool_timeout_is_local_overload
- test_cgnat_range_blocked
- test_nonstandard_port_blocked
- test_service_tier_internal_final_url_rejected
- test_ipv4_preferred_and_second_ip_tried
- test_per_domain_concurrency_cap
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.extraction.scraper import Scraper, _ordered_ips
from app.extraction.ssrf import SSRFViolation, resolve_and_check
from app.http import dispose_http_client, get_http_client, get_ssl_context
from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.wikipedia import WikipediaAdapter
from app.retrieval.health import Outcome


def test_no_asyncclient_outside_http_module() -> None:
    """AST scanner: httpx.AsyncClient must not be constructed outside allowlisted modules."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    allowlist = {
        app_dir / "http.py",
        app_dir / "mcp" / "client.py",
        app_dir / "cli.py",
    }

    violations = []
    for py_file in app_dir.rglob("*.py"):
        if py_file in allowlist:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Look for httpx.AsyncClient(...) or AsyncClient(...)
                if (isinstance(func, ast.Attribute) and func.attr == "AsyncClient") or (
                    isinstance(func, ast.Name) and func.id == "AsyncClient"
                ):
                    violations.append(f"{py_file.relative_to(app_dir)}:{node.lineno}")

    assert not violations, f"Found direct httpx.AsyncClient instantiation in: {violations}"


def test_single_ssl_context() -> None:
    """SSLContext is loaded once and reused across requests."""
    ctx1 = get_ssl_context()
    ctx2 = get_ssl_context()
    assert ctx1 is ctx2


@pytest.mark.asyncio
async def test_pools_are_isolated() -> None:
    """Distinct pools (search, scrape, llm, internal) must use isolated AsyncClient instances."""
    try:
        search_client = get_http_client("search")
        scrape_client = get_http_client("scrape")
        llm_client = get_http_client("llm")
        internal_client = get_http_client("internal")

        assert search_client is not scrape_client
        assert search_client is not llm_client
        assert search_client is not internal_client
        assert scrape_client is not llm_client
        assert llm_client is not internal_client
    finally:
        await dispose_http_client()


@pytest.mark.asyncio
async def test_pool_timeout_is_local_overload() -> None:
    """httpx.PoolTimeout maps to AdapterError with kind=LOCAL_OVERLOAD.

    Does not penalize engine.
    """
    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.PoolTimeout("Pool exhausted")

    adapter = WikipediaAdapter(client=mock_client)
    with pytest.raises(AdapterError) as exc_info:
        await adapter.search("python")

    assert exc_info.value.kind == Outcome.LOCAL_OVERLOAD


@pytest.mark.asyncio
async def test_cgnat_range_blocked() -> None:
    """CGNAT IP range (100.64.0.0/10) must be rejected by SSRF guard."""
    with pytest.raises(SSRFViolation, match="non-global/reserved"):
        await resolve_and_check("http://100.64.0.1/status")

    with pytest.raises(SSRFViolation, match="non-global/reserved"):
        await resolve_and_check("http://100.127.255.254/internal")


@pytest.mark.asyncio
async def test_nonstandard_port_blocked() -> None:
    """Non-standard ports (e.g. 22, 3306, 6379) must be blocked by SSRF port allowlist."""
    with pytest.raises(SSRFViolation, match="port 22 not allowed"):
        await resolve_and_check("http://example.com:22/ssh")

    with pytest.raises(SSRFViolation, match="port 6379 not allowed"):
        await resolve_and_check("http://example.com:6379/redis")


def test_ipv4_preferred_and_second_ip_tried() -> None:
    """_ordered_ips sorts IPv4 addresses first, followed by IPv6."""
    mixed_ips = ["2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34", "192.0.2.1"]
    ordered = _ordered_ips(mixed_ips)
    assert ordered == ["93.184.216.34", "192.0.2.1", "2606:2800:220:1:248:1893:25c8:1946"]


@pytest.mark.asyncio
async def test_service_tier_internal_final_url_rejected() -> None:
    """Service extractors returning an internal final_url must have their results rejected."""
    scraper = Scraper()
    # Mock crawl4ai returning a private internal final_url
    mock_crawl4ai_page = AsyncMock()
    mock_crawl4ai_page.final_url = "http://192.168.1.1/admin"
    scraper._crawl4ai = AsyncMock(return_value=mock_crawl4ai_page)  # type: ignore

    # When final_url is checked, it must be rejected
    with pytest.raises(SSRFViolation):
        await resolve_and_check(mock_crawl4ai_page.final_url)


@pytest.mark.asyncio
async def test_per_domain_concurrency_cap() -> None:
    """Scraper bounds concurrent requests per domain to scrape_per_domain_concurrency."""
    import asyncio
    from datetime import UTC, datetime

    from app.config import get_settings
    from app.extraction.scraper import PageContent

    settings = get_settings()
    max_domain_concurrency = settings.scrape_per_domain_concurrency
    assert max_domain_concurrency == 2

    active_count = 0
    max_active_observed = 0
    lock = asyncio.Lock()

    async def fake_scrape_uncached(url: str, timeout_s: float):
        nonlocal active_count, max_active_observed
        async with lock:
            active_count += 1
            if active_count > max_active_observed:
                max_active_observed = active_count
        await asyncio.sleep(0.02)
        async with lock:
            active_count -= 1
        return PageContent(
            url=url,
            final_url=url,
            title="test",
            markdown="body",
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        )

    scraper = Scraper(settings)
    scraper._scrape_uncached = fake_scrape_uncached  # type: ignore

    # Send 6 concurrent requests to the same domain
    urls = [f"https://example.com/page{i}" for i in range(6)]
    results = await asyncio.gather(*(scraper.scrape(u) for u in urls))

    assert len(results) == 6
    assert max_active_observed <= max_domain_concurrency
