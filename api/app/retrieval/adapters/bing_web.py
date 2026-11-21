"""Bing Web Search RSS adapter — keyless general web results (docs/32 Lite).

Queries ``https://www.bing.com/search?q={q}&format=rss&mkt={mkt}`` — the RSS
face of Bing web search — giving Lite (no SearXNG) a SECOND independent
general-web index next to DuckDuckGo. Reuses the bing_news unwrapping helpers
for redirect wrappers; verified keyless live 2026-11-21. Category: ``general``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from defusedxml import ElementTree as DET

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.bing_news import (
    _VI_CHARS_RE,
    _clean_text,
    _parse_pubdate,
    _unwrap_bing_url,
)
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://www.bing.com/search"


class BingWebSearchAdapter:
    """Keyless Bing WEB search via the RSS endpoint (no SearXNG needed)."""

    id = "bing_web"

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
        max_results: int = 12,
        timeout_s: float = 4.0,
        **kwargs: Any,
    ) -> list[RawResult]:
        q = (query or "").strip()
        if not q:
            return []

        raw_lang = (lang or "").split("-")[0].lower().strip()
        if raw_lang == "vi":
            mkt = "vi-VN"
        elif raw_lang == "en":
            mkt = "en-US"
        else:
            is_vi = bool(_VI_CHARS_RE.search(q))
            mkt = "vi-VN" if is_vi else "en-US"

        params: dict[str, str] = {"q": q, "format": "rss", "mkt": mkt, "count": "20"}
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        }
        client = self._client or get_http_client("search")
        try:
            resp = await client.get(
                self.base_url,
                params=params,
                headers=headers,
                follow_redirects=True,
                timeout=timeout_s,
            )
        except httpx.PoolTimeout as exc:
            raise AdapterError("bing_web pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"bing_web timeout after {timeout_s}s", kind=Outcome.TIMEOUT
            ) from exc
        except httpx.RequestError as exc:
            raise AdapterError(
                f"bing_web transport error: {type(exc).__name__}", kind=Outcome.UPSTREAM_ERROR
            ) from exc

        if resp.status_code == 429:
            raise AdapterError("bing_web rate limited", kind=Outcome.RATE_LIMITED, retryable=False)
        if resp.status_code == 403:
            raise AdapterError("bing_web forbidden", kind=Outcome.BLOCKED, retryable=False)
        if resp.status_code != 200:
            raise AdapterError(
                f"bing_web HTTP {resp.status_code}",
                kind=Outcome.UPSTREAM_ERROR,
                retryable=resp.status_code >= 500,
            )
        if len(resp.content or b"") > 5 * 1024 * 1024:
            raise AdapterError("bing_web payload too large", kind=Outcome.UPSTREAM_ERROR)

        try:
            root = DET.fromstring(resp.text)
        except Exception as exc:
            raise AdapterError(f"bing_web XML parse error: {exc}") from exc

        items = root.findall("./channel/item")
        results: list[RawResult] = []
        seen: set[str] = set()
        for item in items[: max(1, min(max_results, 20)) * 2]:
            title = _clean_text(item.findtext("title") or "")
            real_url = _unwrap_bing_url(item.findtext("link") or "")
            if not title or not real_url or real_url in seen:
                continue
            seen.add(real_url)
            snippet = _clean_text(item.findtext("description") or "") or title
            pub_dt: datetime | None = _parse_pubdate(item.findtext("pubDate") or "")
            results.append(
                RawResult(
                    url=real_url,
                    title=title,
                    snippet=snippet,
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=pub_dt,
                    source_url=real_url,
                    as_of=pub_dt,
                    data_kind="observed",
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
