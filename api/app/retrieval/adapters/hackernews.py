"""Hacker News adapter — keyless Algolia HN Search API (docs/19 W1).

``https://hn.algolia.com/api/v1/search?query=...&tags=story`` — no key, no auth,
returns stories with ``created_at`` (honest published_at) and points/comments.
Category: ``social`` (tech community discussions + link aggregation).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.retrieval.adapters._http import fetch_json
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://hn.algolia.com/api/v1/search"


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class HackerNewsAdapter:
    id = "hackernews"

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
        params: dict[str, Any] = {
            "query": q,
            "tags": "story",
            "hitsPerPage": str(min(max_results, 50)),
            "numericFilters": (
                f"created_at_i>{int(datetime.now(UTC).timestamp()) - freshness_days * 86400}"
                if freshness_days
                else None
            ),
        }
        params = {k: v for k, v in params.items() if v is not None}
        payload = await fetch_json(
            self.id,
            self.base_url,
            params=params,
            timeout_s=timeout_s,
            client=self._client,
        )
        hits = payload.get("hits") or []
        results: list[RawResult] = []
        for hit in hits:
            url = hit.get("url") or (
                f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                if hit.get("objectID")
                else None
            )
            title = str(hit.get("title") or "").strip()
            if not url or not title:
                continue
            results.append(
                RawResult(
                    url=str(url),
                    title=title,
                    snippet=(
                        f"{hit.get('points') or 0} points · "
                        f"{hit.get('num_comments') or 0} comments · "
                        f"by {hit.get('author') or 'unknown'}"
                    ),
                    adapter=self.id,
                    source_engines=["hackernews"],
                    published_at=_parse_ts(hit.get("created_at")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
