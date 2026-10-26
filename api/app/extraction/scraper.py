"""Scrape pool: SSRF-guarded fetch with a 3-tier extraction fallback chain.

    trafilatura (in-process, ~50-200ms) -> crawl4ai service (JS pages)
      -> jina reader r.jina.ai (last resort, needs JINA_API_KEY)

Redirects are followed manually (max 3 hops, docs/03 §4) so every hop goes
through the SSRF guard — httpx auto-follow would bypass it. Failures populate a
process-local negative cache (TTL 1h) so dead URLs are not retried.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.extraction import ssrf
from app.extraction.robots import RobotsCache
from app.http import get_http_client

logger = get_logger(__name__)


@dataclass(slots=True)
class PageContent:
    url: str
    final_url: str
    title: str
    markdown: str
    extractor_used: str  # trafilatura | crawl4ai | jina
    fetched_at: datetime


@dataclass(slots=True)
class PageFailure:
    url: str
    status: str  # failed | blocked
    error: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Scraper:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._robots = RobotsCache(client)
        self._negative: dict[str, float] = {}  # url -> monotonic timestamp of failure
        self._lock = asyncio.Lock()

    # -- public API --------------------------------------------------------

    async def scrape_many(
        self,
        urls: list[str],
        *,
        timeout_s: float | None = None,
        progress: Any = None,
    ) -> list[PageContent | PageFailure]:
        timeout = self._settings.scrape_timeout_s if timeout_s is None else timeout_s
        sem = asyncio.Semaphore(self._settings.scrape_concurrency)

        async def one(url: str) -> PageContent | PageFailure:
            async with sem:
                return await self.scrape(url, timeout_s=timeout)

        results = await asyncio.gather(*(one(u) for u in urls))
        if progress is not None:
            for r in results:
                progress(r)
        return list(results)

    async def scrape(
        self, url: str, *, timeout_s: float | None = None
    ) -> PageContent | PageFailure:
        timeout = self._settings.scrape_timeout_s if timeout_s is None else timeout_s
        if self._is_negatively_cached(url):
            return PageFailure(url=url, status="failed", error="recently failed (negative cache)")
        try:
            page = await self._scrape_uncached(url, timeout)
        except ssrf.SSRFViolation as exc:
            logger.info("scrape_blocked_ssrf", url=url, reason=str(exc))
            return PageFailure(url=url, status="blocked", error=str(exc))
        except Exception as exc:
            logger.info("scrape_failed", url=url, error=type(exc).__name__)
            await self._mark_failed(url)
            return PageFailure(url=url, status="failed", error=type(exc).__name__)
        return page

    # -- internals ---------------------------------------------------------

    async def _scrape_uncached(self, url: str, timeout_s: float) -> PageContent:
        body, final_url = await self._guarded_fetch(url, timeout_s)
        markdown, title, extractor = extract_static(body)
        if markdown:
            return PageContent(
                url=url,
                final_url=final_url,
                title=title or url,
                markdown=markdown,
                extractor_used=extractor,
                fetched_at=_utcnow(),
            )

        page = await self._crawl4ai(url, timeout_s)
        if page is not None:
            return page

        page = await self._jina(url, timeout_s)
        if page is not None:
            return page

        raise RuntimeError("all extractors failed")

    async def _guarded_fetch(self, url: str, timeout_s: float) -> tuple[bytes, str]:
        """Fetch with SSRF check + robots.txt on every redirect hop.

        The body is streamed and capped at ``max_scrape_bytes`` so a huge page
        cannot balloon memory (docs/10-security-compliance.md).
        """
        current = url
        for _ in range(self._settings.max_redirects + 1):
            await ssrf.resolve_and_check(current)
            if not await self._robots.allowed(current, user_agent=self._settings.user_agent):
                raise ssrf.SSRFViolation(f"robots.txt disallows {current}")

            client = self._client or get_http_client()
            async with client.stream("GET", current, timeout=timeout_s) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        break
                    current = str(httpx.URL(current).join(location))
                    continue
                resp.raise_for_status()
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > self._settings.max_scrape_bytes:
                        raise RuntimeError(
                            f"page larger than {self._settings.max_scrape_bytes} bytes"
                        )
                return bytes(buf), str(resp.url)

        raise RuntimeError(f"too many redirects fetching {url}")

    async def _crawl4ai(self, url: str, timeout_s: float) -> PageContent | None:
        settings = self._settings
        headers: dict[str, str] = {}
        if settings.crawl4ai_api_token:
            headers["Authorization"] = f"Bearer {settings.crawl4ai_api_token}"
        try:
            client = self._client or get_http_client()
            resp = await client.post(
                f"{settings.crawl4ai_url.rstrip('/')}/md",
                json={"url": url},
                headers=headers,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.info("crawl4ai_unavailable", url=url, error=type(exc).__name__)
            return None

        markdown, title = _crawl4ai_markdown(payload)
        if not markdown:
            return None
        return PageContent(
            url=url,
            final_url=str(payload.get("url") or url),
            title=title or url,
            markdown=markdown,
            extractor_used="crawl4ai",
            fetched_at=_utcnow(),
        )

    async def _jina(self, url: str, timeout_s: float) -> PageContent | None:
        settings = self._settings
        if not settings.jina_api_key:
            return None
        headers = {"Authorization": f"Bearer {settings.jina_api_key}"}
        try:
            client = self._client or get_http_client()
            resp = await client.get(
                f"{settings.jina_reader_base_url.rstrip('/')}/{url}",
                headers=headers,
                timeout=timeout_s,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.info("jina_unavailable", url=url, error=type(exc).__name__)
            return None
        text = resp.text
        if not text:
            return None
        title = ""
        for line in text.splitlines():
            if line.startswith("Title:"):
                title = line[len("Title:") :].strip()
                break
        return PageContent(
            url=url,
            final_url=url,
            title=title or url,
            markdown=text,
            extractor_used="jina",
            fetched_at=_utcnow(),
        )

    # -- negative cache ------------------------------------------------------

    def _is_negatively_cached(self, url: str) -> bool:
        failed_at = self._negative.get(url)
        if failed_at is None:
            return False
        if time.monotonic() - failed_at > self._settings.negative_cache_ttl_s:
            self._negative.pop(url, None)
            return False
        return True

    async def _mark_failed(self, url: str) -> None:
        async with self._lock:
            self._negative[url] = time.monotonic()


def _crawl4ai_markdown(payload: dict[str, Any]) -> tuple[str, str]:
    """Normalize crawl4ai /md response (str or nested dict) to (markdown, title)."""
    if not isinstance(payload, dict) or not payload.get("success", True):
        return "", ""
    raw = payload.get("markdown")
    if isinstance(raw, str):
        markdown = raw
    elif isinstance(raw, dict):
        markdown = str(raw.get("fit_markdown") or raw.get("raw_markdown") or "")
    else:
        markdown = ""
    title = str(payload.get("title") or "")
    return markdown.strip(), title


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def extract_static(html: str | bytes) -> tuple[str, str, str]:
    """trafilatura extraction -> (markdown, title, extractor). Empty on failure."""
    if not html or len(html) < 200:  # too small to be a real page worth parsing
        return "", "", ""
    try:
        import trafilatura

        # trafilatura 2.x: bare_extraction(output_format="markdown") leaves .text
        # empty, so the markdown comes from extract() and the title from <title>.
        markdown = (
            trafilatura.extract(
                html,
                favor_recall=True,
                output_format="markdown",
                include_formatting=True,
                include_links=False,
                include_images=False,
            )
            or ""
        )
        # trafilatura handles bytes encoding itself; the title regex just needs
        # a lossy decode.
        text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
        title = ""
        match = _TITLE_RE.search(text)
        if match:
            title = match.group(1).strip()
    except Exception:
        return "", "", ""
    markdown = markdown.strip()
    return markdown, title, ("trafilatura" if markdown else "")
