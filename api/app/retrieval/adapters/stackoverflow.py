"""Stack Overflow adapter — vertical engine for developer queries (docs/01 FR-3).

Uses the public StackExchange 2.3 API (api.stackexchange.com), no API key required
(keyless quota: 300 requests/day per IP; optional key extends to 10k/day).
Category: ``developer``.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.stackexchange.com/2.3/search/advanced"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    cleaned = _TAG.sub("", text or "")
    return _WS.sub(" ", html.unescape(cleaned)).strip()


class StackOverflowAdapter:
    id = "stackoverflow"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
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
        params: dict[str, str] = {
            "site": "stackoverflow",
            "q": query,
            "order": "desc",
            "sort": "relevance",
            "pagesize": str(min(max_results, 30)),
        }
        if self.api_key:
            params["key"] = self.api_key

        headers = {
            "User-Agent": "MetaSearch/0.6.0 (developer research; +https://metasearch.ai)",
            "Accept": "application/json",
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
            raise AdapterError("stackoverflow pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"stackoverflow timeout after {timeout_s}s", kind=Outcome.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"stackoverflow transport error: {type(exc).__name__}", kind=Outcome.UPSTREAM_ERROR
            ) from exc

        if resp.status_code == 429:
            raise AdapterError("stackoverflow rate limited", kind=Outcome.RATE_LIMITED)
        if resp.status_code == 403:
            raise AdapterError("stackoverflow forbidden", kind=Outcome.BLOCKED, retryable=False)
        if resp.status_code != 200:
            raise AdapterError(
                f"stackoverflow HTTP {resp.status_code}",
                kind=Outcome.UPSTREAM_ERROR,
                retryable=False,
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise AdapterError("stackoverflow returned non-JSON body") from exc

        if "error_id" in payload:
            err_msg = payload.get("error_message") or payload.get("error_name") or "API error"
            raise AdapterError(f"stackoverflow API error: {err_msg}", retryable=False)

        items = payload.get("items") or []
        results: list[RawResult] = []

        for rank, item in enumerate(items, start=1):
            url = item.get("link")
            if not url or not url.startswith("http"):
                continue

            raw_title = item.get("title", "")
            title = _strip_html(raw_title)

            tags = item.get("tags") or []
            score = item.get("score", 0)
            is_answered = bool(item.get("is_answered", False))
            answer_count = item.get("answer_count", 0)
            status_text = "Answered" if is_answered else "Unanswered"
            tags_text = ", ".join(tags[:5]) if tags else "none"

            snippet = f"[{status_text}] Score: {score} ({answer_count} answers) · Tags: {tags_text}"

            published_at = None
            creation_date = item.get("creation_date")
            if creation_date:
                try:
                    published_at = datetime.fromtimestamp(creation_date, tz=UTC)
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
