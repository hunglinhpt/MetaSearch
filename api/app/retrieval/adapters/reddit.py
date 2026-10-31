"""Reddit adapter — vertical engine for social/community discussions (docs/01 FR-3).

Uses Reddit public JSON search API (reddit.com/search.json). No API key required.
Category: ``social``.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

import httpx

from app.retrieval.adapters.base import AdapterError
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://www.reddit.com/search.json"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean_text(text: str) -> str:
    cleaned = _TAG.sub("", text or "")
    return _WS.sub(" ", html.unescape(cleaned)).strip()


class RedditAdapter:
    id = "reddit"

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
        params: dict[str, str] = {
            "q": query,
            "sort": "relevance",
            "limit": str(min(max_results, 25)),
            "type": "link",
        }

        # Reddit strictly requires a distinct, informative User-Agent
        headers = {
            "User-Agent": "MetaSearch/0.6.0 (community research; +https://metasearch.ai)",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
                resp = await client.get(self.base_url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise AdapterError(f"reddit timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"reddit transport error: {type(exc).__name__}") from exc

        if resp.status_code != 200:
            raise AdapterError(f"reddit HTTP {resp.status_code}", retryable=False)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise AdapterError("reddit returned non-JSON body") from exc

        children = (payload.get("data") or {}).get("children") or []
        results: list[RawResult] = []

        for rank, child in enumerate(children, start=1):
            data = child.get("data") or {}
            permalink = data.get("permalink")
            url = f"https://www.reddit.com{permalink}" if permalink else data.get("url")
            if not url or not url.startswith("http"):
                continue

            title = _clean_text(data.get("title", ""))
            subreddit = (
                data.get("subreddit_name_prefixed") or f"r/{data.get('subreddit', 'unknown')}"
            )
            score = data.get("score", 0)
            num_comments = data.get("num_comments", 0)
            selftext = _clean_text(data.get("selftext", ""))

            snippet = f"{subreddit} · {score} points · {num_comments} comments"
            if selftext:
                snippet = f"{snippet} — {selftext[:250]}"

            published_at = None
            created_utc = data.get("created_utc")
            if created_utc:
                try:
                    published_at = datetime.fromtimestamp(created_utc, tz=UTC)
                except (ValueError, OSError):
                    published_at = None

            results.append(
                RawResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                    rank=rank,
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=published_at,
                )
            )

        return results
