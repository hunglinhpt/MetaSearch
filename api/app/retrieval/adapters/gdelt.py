"""GDELT DOC 2.0 adapter — global news/events in 100+ languages (docs/19 W1).

``https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=artlist&format=json``
— free, no key, updates every 15 minutes. ``freshness_days`` maps to GDELT
``startdatetime``/``enddatetime`` (YYYYMMDDHHMMSS). ``lang`` appends a
``sourcelang:{name}`` clause so English requests return English-language sources.
Category: ``news``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.retrieval.adapters._http import fetch_json
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

_LANG_MAP = {
    "en": "english",
    "vi": "vietnamese",
    "zh": "chinese",
    "ja": "japanese",
    "ko": "korean",
    "fr": "french",
    "de": "german",
    "es": "spanish",
    "pt": "portuguese",
    "ru": "russian",
    "ar": "arabic",
    "hi": "hindi",
    "th": "thai",
    "id": "indonesian",
}


def _parse_seendate(value: Any) -> datetime | None:
    # GDELT returns YYYYMMDDHHMMSS (UTC)
    s = str(value or "")
    if len(s) != 14 or not s.isdigit():
        return None
    try:
        return datetime(
            int(s[0:4]),
            int(s[4:6]),
            int(s[6:8]),
            int(s[8:10]),
            int(s[10:12]),
            int(s[12:14]),
            tzinfo=UTC,
        )
    except ValueError:
        return None


class GdeltAdapter:
    id = "gdelt"

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
        timeout_s: float = 5.0,
        **kwargs: Any,
    ) -> list[RawResult]:
        q = (query or "").strip()
        if not q:
            return []
        raw_lang = (lang or "").split("-")[0].lower()
        gdelt_lang = _LANG_MAP.get(raw_lang)
        if gdelt_lang:
            q = f"{q} sourcelang:{gdelt_lang}"
        now = datetime.now(UTC)
        params: dict[str, Any] = {
            "query": q,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(min(max_results, 250)),
            "sort": "datetimedesc",
        }
        if freshness_days:
            start = now - timedelta(days=int(freshness_days))
            params["startdatetime"] = start.strftime("%Y%m%d%H%M%S")
            params["enddatetime"] = now.strftime("%Y%m%d%H%M%S")
        payload = await fetch_json(
            self.id, self.base_url, params=params, timeout_s=timeout_s, client=self._client
        )
        articles = payload.get("articles") or []
        results: list[RawResult] = []
        for art in articles:
            url = art.get("url")
            title = str(art.get("title") or "").strip()
            if not url or not title:
                continue
            domain = art.get("domain") or ""
            results.append(
                RawResult(
                    url=str(url),
                    title=title,
                    snippet=f"{domain} · {art.get('language') or ''}".strip(" ·"),
                    adapter=self.id,
                    source_engines=["gdelt", domain] if domain else ["gdelt"],
                    published_at=_parse_seendate(art.get("seendate")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
