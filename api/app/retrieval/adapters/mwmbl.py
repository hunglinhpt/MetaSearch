"""Mwmbl adapter — keyless general web index (docs/36 S1).

Mwmbl (https://mwmbl.org) is a non-profit, open-source (AGPL) web search engine
with its own community-crawled index and a public JSON API — the giants-first
answer to "Lite has no working general web engine once DuckDuckGo bot-blocks the
egress" (docs/36 §15: ddg 1/37 successes 19-25/11).

Probe 2026-11-26 (4 queries EN/VI): 200 in 1.2-1.8 s, 31-79 rows each, Vietnamese
queries return vnexpress/thuvienphapluat pages. Response: a JSON list of
``{"url", "title": [{"value", "is_bold"}], "extract": [...], "source"}``. The
canonical path is ``/search/`` (``/search`` answers 301); redirects are NOT
followed (shared ``fetch_json`` policy), so the URL must stay canonical.

Politeness: one request at a time per process with a small gap — the API is run
by volunteers and we identify ourselves honestly via the shared User-Agent.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.retrieval.adapters._http import fetch_json
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.mwmbl.org/search/"
_MIN_INTERVAL_S = 0.25
_lock = asyncio.Lock()
_last_call = 0.0


def _join(parts: Any) -> str:
    if isinstance(parts, str):
        return parts.strip()
    if not isinstance(parts, list):
        return ""
    text = "".join(str(p.get("value") or "") for p in parts if isinstance(p, dict))
    return " ".join(text.split())


class MwmblAdapter:
    id = "mwmbl"

    def __init__(
        self, *, base_url: str = DEFAULT_BASE_URL, client: httpx.AsyncClient | None = None
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
        global _last_call
        q = " ".join((query or "").split())[:300]
        if not q:
            return []
        async with _lock:
            wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_call = time.monotonic()
        data = await fetch_json(
            self.id, self.base_url, params={"s": q}, timeout_s=timeout_s, client=self._client
        )
        if not isinstance(data, list):
            raise AdapterError("mwmbl returned a non-list payload", kind=Outcome.UPSTREAM_ERROR)
        results: list[RawResult] = []
        seen: set[str] = set()
        for row in data:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname or url in seen:
                continue
            seen.add(url)
            title = _join(row.get("title")) or parts.hostname
            results.append(
                RawResult(
                    url=url,
                    title=title[:300],
                    snippet=_join(row.get("extract"))[:500],
                    adapter=self.id,
                    source_engines=["mwmbl"],
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
