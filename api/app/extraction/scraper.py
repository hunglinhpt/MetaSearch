"""Scrape pool: SSRF-guarded fetch with a 3-tier extraction fallback chain.

    trafilatura (in-process, ~50-200ms) -> crawl4ai service (JS pages)
      -> jina reader r.jina.ai (last resort, needs JINA_API_KEY)

Redirects are followed manually (max 3 hops, docs/03 §4) so every hop goes
through the SSRF guard — httpx auto-follow would bypass it. Failures populate a
process-local negative cache (TTL 1h) so dead URLs are not retried.
"""

from __future__ import annotations

import asyncio
import collections.abc
import ipaddress
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings, get_settings
from app.core.budget import get_current_budget
from app.core.logging import get_logger
from app.core.metrics import SCRAPE_TOTAL
from app.core.offload import run_cpu
from app.core.singleflight import SingleFlight
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


def _ordered_ips(ips: collections.abc.Iterable[str]) -> list[str]:
    """Sort IP addresses putting IPv4 first, then IPv6, preserving order within each family."""
    v4: list[str] = []
    v6: list[str] = []
    for ip_str in ips:
        ip = ipaddress.ip_address(ip_str)
        if ip.version == 4:
            v4.append(ip_str)
        else:
            v6.append(ip_str)
    return v4 + v6


# Same margin as the escalation check in _scrape_uncached: a timeout with less
# request budget left than this is our own cut.
_BUDGET_CUT_MARGIN_S = 0.5


def _request_budget_cut() -> bool:
    """True when the current request's own budget, not the page, ended the scrape."""
    budget = get_current_budget()
    return budget is not None and budget.remaining() <= _BUDGET_CUT_MARGIN_S


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
        self._single_flight = SingleFlight()
        self._lock = asyncio.Lock()
        self._global_sem = asyncio.Semaphore(self._settings.scrape_global_concurrency)
        self._domain_sems: dict[str, asyncio.Semaphore] = {}
        self._domain_sem_lock = asyncio.Lock()

    async def _get_domain_sem(self, domain: str) -> asyncio.Semaphore:
        async with self._domain_sem_lock:
            sem = self._domain_sems.get(domain)
            if sem is None:
                sem = asyncio.Semaphore(self._settings.scrape_per_domain_concurrency)
                self._domain_sems[domain] = sem
            return sem

    # -- public API --------------------------------------------------------

    async def scrape_many(
        self,
        urls: list[str],
        *,
        timeout_s: float | None = None,
        progress: Any = None,
    ) -> list[PageContent | PageFailure]:
        capped_urls = urls[: self._settings.scrape_max_urls]
        if not capped_urls:
            return []

        from app.core.budget import get_current_budget

        budget = get_current_budget()
        stage_budget = (
            budget.stage("scrape", self._settings.scrape_timeout_s)
            if budget
            else (self._settings.scrape_timeout_s if timeout_s is None else timeout_s)
        )
        soft_budget = min(self._settings.scrape_soft_budget_s, stage_budget)
        min_pages = self._settings.scrape_min_pages

        sem = asyncio.Semaphore(self._settings.scrape_concurrency)

        async def one(url: str) -> PageContent | PageFailure:
            async with sem:
                return await self.scrape(url, timeout_s=self._settings.scrape_url_budget_s)

        task_map = {asyncio.create_task(one(u)): u for u in capped_urls}
        pending = set(task_map.keys())
        results: list[PageContent | PageFailure] = []
        ok_pages = 0
        start_mono = time.monotonic()

        while pending:
            elapsed = time.monotonic() - start_mono
            remaining_stage = stage_budget - elapsed
            if remaining_stage <= 0.0:
                break
            if elapsed >= soft_budget and ok_pages >= min_pages:
                break

            step_timeout = min(
                remaining_stage,
                max(0.01, soft_budget - elapsed) if ok_pages < min_pages else 0.05,
            )
            done_batch, pending = await asyncio.wait(
                pending, timeout=step_timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for t in done_batch:
                try:
                    res = t.result()
                    results.append(res)
                    if isinstance(res, PageContent) and bool(res.markdown):
                        ok_pages += 1
                    if progress is not None:
                        progress(res)
                except Exception as exc:
                    results.append(PageFailure(url=task_map[t], status="failed", error=str(exc)))

        if pending:
            for t in pending:
                t.cancel()
                u = task_map[t]
                results.append(
                    PageFailure(
                        url=u, status="failed", error="scrape cancelled (early exit / budget)"
                    )
                )
            await asyncio.gather(*pending, return_exceptions=True)

        return results

    async def scrape(
        self, url: str, *, timeout_s: float | None = None
    ) -> PageContent | PageFailure:
        result = await self._single_flight.run(
            url, lambda: self._scrape_guarded(url, timeout_s=timeout_s)
        )
        if isinstance(result, PageContent):
            SCRAPE_TOTAL.labels(tier=result.extractor_used or "unknown", outcome="ok").inc()
        else:
            SCRAPE_TOTAL.labels(tier="none", outcome=result.status).inc()
        return result

    async def _scrape_guarded(
        self, url: str, *, timeout_s: float | None = None
    ) -> PageContent | PageFailure:
        timeout = self._settings.scrape_timeout_s if timeout_s is None else timeout_s
        domain = (urlsplit(url).hostname or "").lower()
        domain_sem = await self._get_domain_sem(domain)
        async with self._global_sem, domain_sem:
            if await self._is_negatively_cached(url):
                return PageFailure(
                    url=url, status="failed", error="recently failed (negative cache)"
                )
            try:
                page = await self._scrape_uncached(url, timeout)
            except ssrf.SSRFViolation as exc:
                logger.info("scrape_blocked_ssrf", url=url, reason=str(exc))
                await self._mark_failed(url, ttl_s=3600.0)
                return PageFailure(url=url, status="blocked", error=str(exc))
            except (httpx.PoolTimeout, asyncio.CancelledError) as exc:
                # P17: Local overload, pool exhaustion or cancellation MUST NOT be negative cached!
                logger.info(
                    "scrape_local_overload_not_negative_cached", url=url, error=type(exc).__name__
                )
                return PageFailure(url=url, status="failed", error=type(exc).__name__)
            except Exception as exc:
                # Our own request budget running out says nothing about the page, so it is
                # not negative cached. A page slower than its own per-URL limit (TimeoutError)
                # while the request still had time left is the upstream's fault and is cached.
                if _request_budget_cut():
                    logger.info("scrape_budget_exhausted_not_negative_cached", url=url)
                    return PageFailure(url=url, status="failed", error="budget_exhausted")
                logger.info("scrape_failed", url=url, error=type(exc).__name__)
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if (
                    status_code in (404, 410)
                    or "robots" in str(exc).lower()
                    or "too large" in str(exc).lower()
                ):
                    ttl_s = 3600.0
                else:
                    ttl_s = 300.0
                await self._mark_failed(url, ttl_s=ttl_s)
                return PageFailure(url=url, status="failed", error=type(exc).__name__)

            if (
                isinstance(page, PageContent)
                and len(page.markdown) > self._settings.max_page_markdown_chars
            ):
                page = PageContent(
                    url=page.url,
                    final_url=page.final_url,
                    title=page.title,
                    markdown=page.markdown[: self._settings.max_page_markdown_chars],
                    extractor_used=page.extractor_used,
                    fetched_at=page.fetched_at,
                )
            await self._cache_page(page)
            return page

    # -- internals ---------------------------------------------------------

    async def _scrape_uncached(self, url: str, timeout_s: float) -> PageContent:
        cached = await self._load_page_cache(url)
        if cached is not None:
            return cached

        from app.core.budget import get_current_budget

        budget = get_current_budget()
        url_budget = min(self._settings.scrape_url_budget_s, timeout_s)
        if budget is not None:
            url_budget = min(url_budget, budget.remaining())
        url_budget = max(0.1, url_budget)

        async with asyncio.timeout(url_budget):
            body: bytes | None = None
            final_url = url
            try:
                body, final_url = await self._guarded_fetch(url, url_budget)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 429, 503):
                    # Anti-bot challenge / Cloudflare WAF: escalate to proxy tier
                    logger.info(
                        "direct_fetch_challenge_escalating",
                        url=url,
                        status=exc.response.status_code,
                    )
                else:
                    raise
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                # Network-level bot drop or TLS reset: escalate to proxy tier
                logger.info(
                    "direct_fetch_network_drop_escalating", url=url, error=type(exc).__name__
                )

            if body:
                if is_challenge_page(body):
                    logger.info("direct_fetch_captcha_detected_escalating", url=url)
                else:
                    if len(body) > self._settings.max_extract_bytes:
                        logger.info(
                            "direct_fetch_body_too_large_truncating",
                            url=url,
                            size=len(body),
                            max_bytes=self._settings.max_extract_bytes,
                        )
                        body = body[: self._settings.max_extract_bytes]
                    markdown, title, extractor = await run_cpu(extract_static, body)
                    if markdown:
                        if len(markdown) > self._settings.max_page_markdown_chars:
                            markdown = markdown[: self._settings.max_page_markdown_chars]
                        return PageContent(
                            url=url,
                            final_url=final_url,
                            title=title or url,
                            markdown=markdown,
                            extractor_used=extractor,
                            fetched_at=_utcnow(),
                        )

            # Check remaining budget before escalating to crawl4ai / Jina
            if budget is not None and budget.remaining() <= 0.5:
                raise TimeoutError("exhausted budget before extractor escalation")

            # Ensure the target URL passed to service extractors passed SSRF check
            target_url = final_url or url
        await ssrf.resolve_and_check(target_url)

        page = await self._crawl4ai(target_url, timeout_s, original_url=url)
        if page is not None and not is_challenge_page(page.markdown):
            try:
                await ssrf.resolve_and_check(page.final_url)
                return page
            except ssrf.SSRFViolation as exc:
                logger.warning(
                    "crawl4ai_rejected_internal_final_url",
                    url=url,
                    final_url=page.final_url,
                    error=str(exc),
                )

        page = await self._jina(target_url, timeout_s, original_url=url)
        if page is not None and not is_challenge_page(page.markdown):
            try:
                await ssrf.resolve_and_check(page.final_url)
                return page
            except ssrf.SSRFViolation as exc:
                logger.warning(
                    "jina_rejected_internal_final_url",
                    url=url,
                    final_url=page.final_url,
                    error=str(exc),
                )

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

            ordered = _ordered_ips(ips)
            client = self._client or get_http_client("scrape")
            # D2: the crawler says who it is, the same name robots.txt was checked against.
            stealth_headers = {
                "Connection": "close",
                "User-Agent": self._settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            last_conn_exc: Exception | None = None
            redirect_target: str | None = None
            body_result: bytes | None = None

            for ip in ordered:
                pinned_url, pin_headers, extensions = build_pinned_request(current, ip)
                merged_headers = {**stealth_headers, **pin_headers}
                hop_timeout = httpx.Timeout(timeout_s, connect=2.0)
                try:
                    async with client.stream(
                        "GET",
                        pinned_url,
                        headers=merged_headers,
                        extensions=extensions,
                        timeout=hop_timeout,
                    ) as resp:
                        if resp.is_redirect:
                            location = resp.headers.get("location")
                            if location:
                                redirect_target = str(httpx.URL(current).join(location))
                            break
                        resp.raise_for_status()
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes():
                            buf.extend(chunk)
                            if len(buf) > self._settings.max_scrape_bytes:
                                raise RuntimeError(
                                    f"page larger than {self._settings.max_scrape_bytes} bytes"
                                )
                        body_result = bytes(buf)
                        break
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    last_conn_exc = exc
                    logger.info("guarded_fetch_ip_failed_next", ip=ip, url=current, error=str(exc))
                    continue

            if body_result is not None:
                return body_result, current

            if redirect_target is not None:
                current = redirect_target
                continue

            if last_conn_exc is not None:
                raise last_conn_exc
            raise RuntimeError(f"could not fetch {current} from any resolved IP")

        raise RuntimeError(f"too many redirects fetching {url}")

    async def _crawl4ai(
        self, url: str, timeout_s: float, *, original_url: str | None = None
    ) -> PageContent | None:
        settings = self._settings
        headers: dict[str, str] = {}
        if settings.crawl4ai_api_token:
            headers["Authorization"] = f"Bearer {settings.crawl4ai_api_token}"
        try:
            client = self._client or get_http_client("scrape")
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
        final_url = str(payload.get("url") or url)
        try:
            await ssrf.resolve_and_check(final_url)
        except ssrf.SSRFViolation as exc:
            logger.warning(
                "crawl4ai_rejected_internal_final_url",
                url=url,
                final_url=final_url,
                error=str(exc),
            )
            return None
        return PageContent(
            url=req_url,
            final_url=final_url,
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
            client = self._client or get_http_client("scrape")
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

    async def _mark_failed(self, url: str, *, ttl_s: float | None = None) -> None:
        from app.cache import negative_cache_key

        effective_ttl = ttl_s if ttl_s is not None else self._settings.negative_cache_ttl_s
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
            await self._cache.set_flag(negative_cache_key(url), ttl_s=int(effective_ttl))

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


def extract_static(
    html: str | bytes, *, max_extract_bytes: int | None = None
) -> tuple[str, str, str]:
    """trafilatura extraction -> (markdown, title, extractor). Empty on failure."""
    if not html or len(html) < 200:  # too small to be a real page worth parsing
        return "", "", ""
    if max_extract_bytes is None:
        try:
            max_extract_bytes = get_settings().max_extract_bytes
        except Exception:
            max_extract_bytes = 2 * 1024 * 1024
    if (isinstance(html, bytes) and len(html) > max_extract_bytes) or (
        isinstance(html, str) and len(html.encode("utf-8", "replace")) > max_extract_bytes
    ):
        html = html[:max_extract_bytes]
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
