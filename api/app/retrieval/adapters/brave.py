"""Brave Search API adapter. Category ``general``, needs subscription token."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.retrieval.adapters.base import AdapterError
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.search.brave.com/res"


def _parse_age(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class BraveAdapter:
    id = "brave"

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
        params: dict[str, str] = {"q": query, "count": str(min(max_results, 20))}
        if lang:
            params["search_lang"] = lang
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
                resp = await client.get(
                    f"{self.base_url}/v1/web/search",
                    params=params,
                    headers={
                        "X-Subscription-Token": self.api_key,
                        "Accept": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise AdapterError(f"brave timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"brave transport error: {type(exc).__name__}") from exc

        if resp.status_code in (401, 403):
            raise AdapterError(f"brave auth failed (HTTP {resp.status_code})", retryable=False)
        if resp.status_code == 429:
            raise AdapterError("brave rate limited (HTTP 429)", retryable=False)
        if resp.status_code != 200:
            raise AdapterError(f"brave HTTP {resp.status_code}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise AdapterError("brave returned non-JSON body") from exc

        results: list[RawResult] = []
        for item in (body.get("web") or {}).get("results") or []:
            url = item.get("url")
            if not url:
                continue
            results.append(
                RawResult(
                    url=str(url),
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("description") or ""),
                    adapter=self.id,
                    source_engines=["brave"],
                    published_at=_parse_age(item.get("age")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
