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
from typing import TYPE_CHECKING, Any

import httpx

from app.config import Settings, get_settings
from app.core.logging import get_logger
from app.extraction import ssrf
from app.extraction.pinning import build_pinned_request
from app.extraction.robots import RobotsCache
from app.http import get_http_client

if TYPE_CHECKING:
    from app.cache import CacheClient

logger = get_logger(__name__)

_NEGATIVE_CACHE_MAX = 4096


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


CHALLENGE_PATTERNS = (
    "cf-turnstile",
    "challenges.cloudflare.com",
    "attention required! | cloudflare",
    "<title>just a moment...</title>",
    "cloudflare ray id",
    "enable javascript and cookies to continue",
    "g-recaptcha",
    "hcaptcha",
    "verify you are human",
    "verifying you are human",
    "checking your browser before accessing",
    "security check to access",
    "please turn javascript on and reload the page",
    "datadome",
    "perimeterx",
    "bot detection",
    "are you a human",
    "incapsula_resource",
)


def is_challenge_page(content: str | bytes) -> bool:
    """Detect Cloudflare Turnstile, CAPTCHA, or anti-bot challenge interstitial."""
    if not content:
        return False
    if isinstance(content, bytes):
        text = content[:15000].decode("utf-8", "replace").lower()
    else:
        text = content[:15000].lower()
    return any(p in text for p in CHALLENGE_PATTERNS)


class Scraper:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        cache: CacheClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client
        self._robots = RobotsCache(client)
        self._cache = cache
        # Process-local negative cache: fallback when Redis is unavailable and
        # a fast path when it is up (multi-worker installs share via Redis).
        self._negative: dict[str, float] = {}
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
        if await self._is_negatively_cached(url):
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
        await self._cache_page(page)
        return page

    # -- internals ---------------------------------------------------------

    async def _scrape_uncached(self, url: str, timeout_s: float) -> PageContent:
        cached = await self._load_page_cache(url)
        if cached is not None:
            return cached

        body: bytes | None = None
        final_url = url
        try:
            body, final_url = await self._guarded_fetch(url, timeout_s)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (403, 429, 503):
                # Anti-bot challenge / Cloudflare WAF: escalate to proxy tier
                logger.info(
                    "direct_fetch_challenge_escalating", url=url, status=exc.response.status_code
                )
            else:
                raise
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            # Network-level bot drop or TLS reset: escalate to proxy tier
            logger.info("direct_fetch_network_drop_escalating", url=url, error=type(exc).__name__)

        if body:
            if is_challenge_page(body):
                logger.info("direct_fetch_captcha_detected_escalating", url=url)
            else:
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

        # Ensure the target URL passed to service extractors passed SSRF check
        target_url = final_url or url
        await ssrf.resolve_and_check(target_url)

        page = await self._crawl4ai(target_url, timeout_s, original_url=url)
        if page is not None and not is_challenge_page(page.markdown):
            return page

        page = await self._jina(target_url, timeout_s, original_url=url)
        if page is not None and not is_challenge_page(page.markdown):
            return page

        raise RuntimeError("all extractors failed or blocked by anti-bot challenge")

    async def _guarded_fetch(self, url: str, timeout_s: float) -> tuple[bytes, str]:
        """Fetch with SSRF check, IP pinning and robots.txt on every hop.

        Each hop: resolve + validate DNS -> connect to the validated IP
        (Host/SNI preserved, closing the rebinding window) -> stream the body
        capped at ``max_scrape_bytes`` (docs/10-security-compliance.md).
        """
        current = url
        for _ in range(self._settings.max_redirects + 1):
            ips = await ssrf.resolve_and_check(current)
            if not await self._robots.allowed(current, user_agent=self._settings.user_agent):
                raise ssrf.SSRFViolation(f"robots.txt disallows {current}")

            pinned_url, pin_headers, extensions = build_pinned_request(current, sorted(ips)[0])
            client = self._client or get_http_client()
            stealth_headers = {
                **pin_headers,
                "Connection": "close",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }
            async with client.stream(
                "GET", pinned_url, headers=stealth_headers, extensions=extensions, timeout=timeout_s
            ) as resp:
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
                # ``current`` (original form) beats resp.url: the response URL
                # carries the pinned IP host.
                return bytes(buf), current

        raise RuntimeError(f"too many redirects fetching {url}")

    async def _crawl4ai(
        self, url: str, timeout_s: float, *, original_url: str | None = None
    ) -> PageContent | None:
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
        req_url = original_url or url
        return PageContent(
            url=req_url,
            final_url=str(payload.get("url") or url),
            title=title or req_url,
            markdown=self._cap(markdown),
            extractor_used="crawl4ai",
            fetched_at=_utcnow(),
        )

    def _cap(self, text: str) -> str:
        """Guard the service tiers (crawl4ai/jina) against oversized bodies —
        the direct fetch enforces the same limit while streaming."""
        return text[: self._settings.max_scrape_bytes]

    async def _jina(
        self, url: str, timeout_s: float, *, original_url: str | None = None
    ) -> PageContent | None:
        settings = self._settings
        # Keyless r.jina.ai (~20 rpm) keeps the Lite profile functional without
        # crawl4ai; an API key simply raises the rate ceiling.
        if not settings.jina_api_key and not settings.jina_keyless:
            return None
        headers = (
            {"Authorization": f"Bearer {settings.jina_api_key}"} if settings.jina_api_key else {}
        )
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
        req_url = original_url or url
        return PageContent(
            url=req_url,
            final_url=url,
            title=title or req_url,
            markdown=self._cap(text),
            extractor_used="jina",
            fetched_at=_utcnow(),
        )

    # -- negative cache + page cache ------------------------------------------

    async def _is_negatively_cached(self, url: str) -> bool:
        from app.cache import negative_cache_key

        if self._cache is not None and await self._cache.has_flag(negative_cache_key(url)):
            return True
        failed_at = self._negative.get(url)
        if failed_at is None:
            return False
        if time.monotonic() - failed_at > self._settings.negative_cache_ttl_s:
            self._negative.pop(url, None)
            return False
        return True

    async def _mark_failed(self, url: str) -> None:
        from app.cache import negative_cache_key

        now = time.monotonic()
        async with self._lock:
            if len(self._negative) >= _NEGATIVE_CACHE_MAX:
                expired = [
                    k
                    for k, ts in self._negative.items()
                    if now - ts > self._settings.negative_cache_ttl_s
                ]
                for k in expired:
                    self._negative.pop(k, None)
                if len(self._negative) >= _NEGATIVE_CACHE_MAX:
                    self._negative.pop(next(iter(self._negative)), None)
            self._negative[url] = now
        if self._cache is not None:
            await self._cache.set_flag(
                negative_cache_key(url), ttl_s=int(self._settings.negative_cache_ttl_s)
            )

    async def _load_page_cache(self, url: str) -> PageContent | None:
        from app.cache import page_cache_key

        if self._cache is None:
            return None
        payload = await self._cache.get_json(page_cache_key(url))
        if not payload:
            return None
        return PageContent(
            url=url,
            final_url=str(payload.get("final_url") or url),
            title=str(payload.get("title") or url),
            markdown=str(payload.get("markdown") or ""),
            extractor_used=str(payload.get("extractor_used") or "cache"),
            fetched_at=_utcnow(),
        )

    async def _cache_page(self, page: PageContent) -> None:
        from app.cache import page_cache_key

        if self._cache is None or not page.markdown:
            return
        await self._cache.set_json(
            page_cache_key(page.url),
            {
                "final_url": page.final_url,
                "title": page.title,
                "markdown": page.markdown,
                "extractor_used": page.extractor_used,
            },
            ttl_s=self._settings.cache_page_ttl_s,
        )


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
