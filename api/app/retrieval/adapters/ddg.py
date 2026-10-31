"""DuckDuckGo HTML adapter — keyless general search (Lite profile, docs/08).

No SearXNG? No API key? DDG's HTML endpoint still works at light rates.
Politeness: single attempt, per-process throttle, honest errors — the engine
health system auto-disables it if DDG starts blocking.

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
from app.retrieval.adapters.base import AdapterError
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

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL) -> None:
        self.base_url = base_url

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
        global _last_call
        params: dict[str, str] = {"q": query, "s": "0"}
        if lang:
            region = f"{lang}-{lang}" if len(lang) == 2 else lang
            params["kl"] = region
        if freshness_days:
            params["df"] = {1: "d", 7: "w", 30: "m"}.get(
                min(freshness_days, 30),
                freshness_days if freshness_days in ("d", "w", "m") else "y",
            )

        async with _lock:
            wait = _min_interval_s - (time.monotonic() - _last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_call = time.monotonic()
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=timeout_s,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; MetaSearchBot/1.0)"},
                ) as client:
                    resp = await client.post(self.base_url, data=params)
            except httpx.TimeoutException as exc:
                raise AdapterError(f"ddg timeout after {timeout_s}s") from exc
            except httpx.HTTPError as exc:
                raise AdapterError(f"ddg transport error: {type(exc).__name__}") from exc

        if resp.status_code in (202, 403, 429):
            # 202 = DDG's soft anomaly throttle; treat like an explicit block.
            raise AdapterError(f"ddg bot-blocked (HTTP {resp.status_code})", retryable=False)
        if resp.status_code != 200:
            raise AdapterError(f"ddg HTTP {resp.status_code}")
        if "anomaly" in resp.text[:2000].lower() or "captcha" in resp.text[:2000].lower():
            raise AdapterError("ddg challenge page (bot detection)", retryable=False)

        html = resp.text
        titles = _TITLE.findall(html)
        snippets = _SNIPPET.findall(html)
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
