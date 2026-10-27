"""Tavily adapter — the designated fallback engine when SearXNG is degraded
(docs/12-roadmap-plan.md rủi ro #1). Enabled automatically when TAVILY_API_KEY
is set.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.retrieval.adapters.base import AdapterError
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.tavily.com"


def _parse_published(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class TavilyAdapter:
    id = "tavily"

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 3.0,
    ) -> list[RawResult]:
        payload: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        if categories and "news" in categories:
            payload["topic"] = "news"
        if freshness_days:
            payload["days"] = min(freshness_days, 365)

        last_error: AdapterError | None = None
        for _attempt in (1, 2):  # roadmap Phase 1 semantics: 1 retry
            try:
                resp = await self._post(payload, timeout_s)
            except AdapterError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
                continue
            break
        else:  # both attempts failed with retryable errors
            raise last_error or AdapterError("tavily failed")

        results: list[RawResult] = []
        for i, item in enumerate(resp.get("results") or []):
            url = item.get("url")
            if not url:
                continue
            results.append(
                RawResult(
                    url=str(url),
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("content") or ""),
                    adapter=self.id,
                    source_engines=["tavily"],
                    adapter_score=_as_float(item.get("score")),
                    published_at=_parse_published(item.get("published_date")),
                    rank=i,
                )
            )
            if len(results) >= max_results:
                break
        return results

    async def _post(self, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
                resp = await client.post(
                    f"{self.base_url}/search",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise AdapterError(f"tavily timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"tavily transport error: {type(exc).__name__}") from exc

        if resp.status_code in (401, 403):
            raise AdapterError(f"tavily auth failed (HTTP {resp.status_code})", retryable=False)
        if resp.status_code == 429:
            raise AdapterError("tavily rate limited (HTTP 429)", retryable=False)
        try:
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPStatusError as exc:
            raise AdapterError(f"tavily HTTP {exc.response.status_code}") from exc
        except ValueError as exc:
            raise AdapterError("tavily returned non-JSON body") from exc
        if not isinstance(body, dict):
            raise AdapterError("tavily returned non-object JSON")
        return body


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
