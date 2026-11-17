"""DuckDuckGo HTML adapter — keyless general search (Lite profile, docs/08).

No SearXNG? No API key? DDG's HTML endpoint still works at light rates.
Politeness: per-process throttle, honest errors — the engine health system
auto-disables it if DDG starts blocking, and fan-out routes to the healthy
engines meanwhile.

docs/29 ISSUE-1 hardening: UA rotation per call, ONE retry on transient
failures (bot-blocks are never retried), snippet regex accepts both the
legacy ``result__snippet`` inside ``</a>`` and block-level layouts so a DOM
change degrades snippets instead of silently zeroing whole result rows.

Response shape (html.duckduckgo.com/html/?q=...): result blocks
``result__body`` with ``result__a`` (title, href possibly a /l/?uddg= redirect),
``result__snippet``. The redirect wrapper is unwrapped back to the real URL.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

import httpx

from app.core.logging import get_logger
from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://html.duckduckgo.com/html/"
_TITLE = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
)
_SNIPPET = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_UDDG = re.compile(r"[?&]uddg=([^&]+)")
_BAD_HOSTS = ("ad_domain",)
# ISSUE-1: realistic desktop UA pool, rotated per call — a single static
# fingerprint let DDG throttle this adapter for the whole process.
_UA_POOL: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
)
_UA_INDEX = 0

# Politeness throttle: DDG blocks bursts; serialize requests with a gap.
_min_interval_s = 1.5
_last_call = 0.0
_lock = asyncio.Lock()


def _strip_html(text: str) -> str:
    return _WS.sub(" ", _TAG.sub("", text or "")).strip()


def _unwrap(href: str) -> str | None:
    """DDG wraps outbound links in /l/?uddg=<encoded-real-url>."""
    if href.startswith("//"):
        href = f"https:{href}"
    if "uddg=" in href:
        match = _UDDG.search(href)
        if not match:
            return None
        return unquote(match.group(1))
    if href.startswith("http"):
        return href
    return None


def _parse_published(snippet: str) -> datetime | None:
    match = re.search(
        r"(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4})", snippet
    )
    if not match:
        return None
    months = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }
    try:
        return datetime(
            int(match.group(3)), months[match.group(2)], int(match.group(1)), tzinfo=UTC
        )
    except (KeyError, ValueError):
        return None


class DuckDuckGoAdapter:
    id = "ddg"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url
        self._client = client

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 4.0,
    ) -> list[RawResult]:
        global _last_call, _UA_INDEX
        params: dict[str, str] = {"q": query, "s": "0"}
        if lang:
            region = f"{lang}-{lang}" if len(lang) == 2 else lang
            params["kl"] = region
        if freshness_days:
            if isinstance(freshness_days, str) and freshness_days in ("d", "w", "m", "y"):
                params["df"] = freshness_days
            elif isinstance(freshness_days, int):
                if freshness_days <= 1:
                    params["df"] = "d"
                elif freshness_days <= 7:
                    params["df"] = "w"
                elif freshness_days <= 31:
                    params["df"] = "m"
                else:
                    params["df"] = "y"

        async with _lock:
            now_mono = time.monotonic()
            wait = _min_interval_s - (now_mono - _last_call)
            _last_call = now_mono + max(wait, 0.0)
        if wait > 0:
            await asyncio.sleep(wait)
        headers = {
            "User-Agent": _UA_POOL[_UA_INDEX % len(_UA_POOL)],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        _UA_INDEX += 1
        client = self._client or get_http_client("search")
        # ISSUE-1: one jittered retry on TRANSIENT failures only — bot-blocks
        # (202/403/429/challenge) are hard failures and are never retried.
        import random as _random

        last_exc: AdapterError | None = None
        resp: httpx.Response | None = None
        for attempt in range(2):
            try:
                resp = await client.post(
                    self.base_url,
                    data=params,
                    headers={
                        **headers,
                        "User-Agent": _UA_POOL[(_UA_INDEX + attempt) % len(_UA_POOL)],
                    },
                    follow_redirects=True,
                    timeout=timeout_s,
                )
                break
            except httpx.HTTPError as exc:
                # NOTE: `X = AdapterError(...) from exc` is invalid syntax —
                # exception chaining is attached via __cause__ (ISSUE-1).
                if isinstance(exc, httpx.PoolTimeout):
                    outcome = Outcome.LOCAL_OVERLOAD
                elif isinstance(exc, httpx.TimeoutException):
                    outcome = Outcome.TIMEOUT
                else:
                    outcome = Outcome.UPSTREAM_ERROR
                last_exc = AdapterError(f"ddg transport error: {type(exc).__name__}", kind=outcome)
                last_exc.__cause__ = exc
                if attempt == 0:
                    await asyncio.sleep(_random.uniform(0.15, 0.4))  # nosec B311 - backoff timing
                    continue
        if resp is None:
            assert last_exc is not None  # nosec B101 - invariant guard
            raise last_exc

        if resp.status_code in (202, 403, 429):
            # 202 = DDG's soft anomaly throttle; treat like an explicit block.
            raise AdapterError(
                f"ddg bot-blocked (HTTP {resp.status_code})",
                kind=Outcome.BLOCKED if resp.status_code != 429 else Outcome.RATE_LIMITED,
                retryable=False,
            )
        if resp.status_code != 200:
            raise AdapterError(f"ddg HTTP {resp.status_code}", kind=Outcome.UPSTREAM_ERROR)
        if "anomaly" in resp.text[:2000].lower() or "captcha" in resp.text[:2000].lower():
            raise AdapterError("ddg challenge page (bot detection)", retryable=False)

        html = resp.text
        titles = _TITLE.findall(html)
        snippets = _SNIPPET.findall(html)
        if not titles and "result__a" not in html:
            # ISSUE-1: 200 with zero parsable rows is a DOM change, not honest
            # emptiness — surface it loudly (still Outcome.EMPTY so the
            # breaker does not cycle on layout drift).
            logger.warning("ddg_parse_zero_rows_dom_changed", query=query[:80])
        results: list[RawResult] = []
        seen: set[str] = set()
        for i, (href, title_html) in enumerate(titles):
            url = _unwrap(href.strip())
            if not url or url in seen or urlsplit(url).hostname in _BAD_HOSTS:
                continue
            seen.add(url)
            snippet = _strip_html(snippets[i]) if i < len(snippets) else ""
            results.append(
                RawResult(
                    url=url,
                    title=_strip_html(title_html),
                    snippet=snippet,
                    adapter=self.id,
                    source_engines=["duckduckgo"],
                    published_at=_parse_published(snippet),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        if not results:
            logger.info("ddg_empty_results", query=query[:80])
        return results
