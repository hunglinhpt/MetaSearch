"""Stack Exchange adapter — keyless official API (docs/19 W1).

``https://api.stackexchange.com/2.3/search/advanced`` with ``site`` param (default
``stackoverflow``; any public SE site works: mathoverflow, serverfault, superuser,
askubuntu...). No key (300 req/day anonymous, gzip). Category: ``developer``
(forums Q&A — "diễn đàn" theo yêu cầu global).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.retrieval.adapters._http import fetch_json
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.stackexchange.com/2.3/search/advanced"


def _site_from_query(query: str, default: str = "stackoverflow") -> tuple[str, str]:
    """Allow ``site:mathoverflow`` in the query to pick the SE site; returns
    (cleaned_query, site). Site is strictly validated — it goes into a URL path."""
    import re

    m = re.search(r"(?:^|\s)site:([a-z0-9.]+)", query, re.IGNORECASE)
    if not m:
        return query, default
    site = m.group(1).lower()
    if not re.fullmatch(r"[a-z0-9]+(\.[a-z0-9]+)*", site):
        return query, default
    cleaned = (query[: m.start()] + query[m.end() :]).strip()
    return cleaned or query, site


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return dt


class StackExchangeAdapter:
    id = "stackexchange"

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
        max_results: int = 10,
        timeout_s: float = 4.0,
        **kwargs: Any,
    ) -> list[RawResult]:
        q = (query or "").strip()
        if not q:
            return []
        q, site = _site_from_query(q)
        if not q:
            return []
        params: dict[str, Any] = {
            "site": site,
            "q": q,
            "pagesize": str(min(max_results, 100)),
            "sort": "relevance",
            "order": "desc",
            "filter": "default",
        }
        if freshness_days:
            from time import time

            params["fromdate"] = str(int(time()) - freshness_days * 86400)
        payload = await fetch_json(
            self.id,
            self.base_url,
            params=params,
            timeout_s=timeout_s,
            client=self._client,
            headers={"Accept-Encoding": "gzip"},
        )
        items = payload.get("items") or []
        results: list[RawResult] = []
        for it in items:
            link = it.get("link")
            title = str(it.get("title") or "").strip()
            if not link or not title:
                continue
            tags = ", ".join(it.get("tags") or [])
            snippet = f"score {it.get('score') or 0} · answers {it.get('answer_count') or 0}"
            if tags:
                snippet += f" · [{tags}]"
            results.append(
                RawResult(
                    url=str(link),
                    title=title,
                    snippet=snippet,
                    adapter=self.id,
                    source_engines=[f"stackexchange-{site}"],
                    published_at=_parse_ts(it.get("creation_date")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
