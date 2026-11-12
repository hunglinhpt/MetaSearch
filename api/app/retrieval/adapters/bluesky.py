"""Bluesky adapter — keyless public AppView API (docs/19 W1).

``https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts`` — public, no auth
for public posts (rate-limited per IP). Returns posts with ``createdAt``.
Category: ``social``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.retrieval.adapters._http import fetch_json
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"


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


class BlueskyAdapter:
    id = "bluesky"

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
        params: dict[str, Any] = {"q": q, "limit": str(min(max_results, 100))}
        if freshness_days:
            since = datetime.now(UTC).timestamp() - freshness_days * 86400
            params["since"] = (
                datetime.fromtimestamp(since, tz=UTC).isoformat().replace("+00:00", "Z")
            )
        payload = await fetch_json(
            self.id, self.base_url, params=params, timeout_s=timeout_s, client=self._client
        )
        posts = (payload.get("posts") or []) if isinstance(payload, dict) else []
        results: list[RawResult] = []
        for post in posts:
            if not isinstance(post, dict):
                continue
            record = post.get("record") or {}
            author_obj = post.get("author")
            record = record if isinstance(record, dict) else {}
            author_obj = author_obj if isinstance(author_obj, dict) else {}
            author = author_obj.get("handle") or "unknown"
            text = str(record.get("text") or "").strip()
            uri = post.get("uri") or ""
            # at://did:.../app.bsky.feed.post/<rkey> -> https://bsky.app/profile/<handle>/post/<rkey>
            rkey = uri.rsplit("/", 1)[-1] if uri else ""
            handle = author_obj.get("handle") or ""
            # Audit v2.4 round 9: without handle+rkey there is NO honest URL —
            # dropping the post beats a fabricated homepage link that the
            # merger would collapse every drifted post onto.
            if not (handle and rkey) or not text:
                continue
            url = f"https://bsky.app/profile/{handle}/post/{rkey}"
            results.append(
                RawResult(
                    url=url,
                    title=f"@{author} on Bluesky",
                    snippet=text[:500],
                    adapter=self.id,
                    source_engines=["bluesky"],
                    published_at=_parse_ts(record.get("createdAt")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
