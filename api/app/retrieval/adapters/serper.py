"""Serper.dev adapter (Google SERP API). Category ``general``, needs key."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://google.serper.dev"


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class SerperAdapter:
    id = "serper"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client

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
        payload: dict[str, Any] = {"q": query, "num": min(max_results, 30)}
        if lang:
            payload["hl"] = lang
        client = self._client or get_http_client("search")
        try:
            resp = await client.post(
                f"{self.base_url}/search",
                json=payload,
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                follow_redirects=True,
                timeout=timeout_s,
            )
        except httpx.PoolTimeout as exc:
            raise AdapterError("serper pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(f"serper timeout after {timeout_s}s", kind=Outcome.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"serper transport error: {type(exc).__name__}", kind=Outcome.UPSTREAM_ERROR
            ) from exc

        if resp.status_code in (401, 403):
            raise AdapterError(
                f"serper auth failed (HTTP {resp.status_code})",
                kind=Outcome.BLOCKED,
                retryable=False,
            )
        if resp.status_code == 429:
            raise AdapterError(
                "serper rate limited (HTTP 429)", kind=Outcome.RATE_LIMITED, retryable=False
            )
        if resp.status_code != 200:
            raise AdapterError(f"serper HTTP {resp.status_code}", kind=Outcome.UPSTREAM_ERROR)

        try:
            body = resp.json()
        except ValueError as exc:
            raise AdapterError("serper returned non-JSON body") from exc

        results: list[RawResult] = []
        for item in body.get("organic") or []:
            link = item.get("link")
            if not link:
                continue
            results.append(
                RawResult(
                    url=str(link),
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("snippet") or ""),
                    adapter=self.id,
                    source_engines=["serper"],
                    published_at=_parse_date(item.get("date")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
