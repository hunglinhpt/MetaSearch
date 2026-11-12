"""SearXNG adapter — the primary meta-search giant (docs/03-search-pipeline.md §2).

One JSON call aggregates 70+ upstream engines; each result carries an
``engines`` list which becomes our consensus signal. Timeout 3s, 1 retry on
transport errors (per docs/12-roadmap-plan.md Phase 1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.logging import get_logger
from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

logger = get_logger(__name__)

# freshness_days → SearXNG time_range bucket.
_TIME_RANGES: tuple[tuple[int, str], ...] = ((1, "day"), (7, "week"), (30, "month"), (365, "year"))

# MetaSearch category → SearXNG category (docs/19 W2). SearXNG would 400 on an
# unknown category name (e.g. our "financial"), so map explicitly; anything
# unmapped falls back to "general" (dedup keeps one entry).
_CATEGORY_MAP: dict[str, str] = {
    "general": "general",
    "news": "news",
    "social": "social media",
    "developer": "it",
    "academic": "science",
    "reference": "general",
    "financial": "general",
    "fengshui": "general",
    "images": "images",
    "videos": "videos",
}


def map_categories(categories: list[str] | None) -> list[str]:
    """Translate MetaSearch categories to SearXNG category names, deduped."""
    if not categories:
        return ["general"]
    out: list[str] = []
    for c in categories:
        mapped = _CATEGORY_MAP.get(c.strip().lower(), "general")
        if mapped not in out:
            out.append(mapped)
    return out or ["general"]


def _time_range(freshness_days: int | None) -> str | None:
    if not freshness_days or freshness_days <= 0:
        return None
    for ceiling, bucket in _TIME_RANGES:
        if freshness_days <= ceiling:
            return bucket
    return None


def _parse_published(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class SearXNGAdapter:
    id = "searxng"

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def _fetch(self, params: dict[str, str], timeout_s: float) -> dict[str, Any]:
        client = self._client or get_http_client("search")
        try:
            resp = await client.get(
                f"{self.base_url}/search",
                params=params,
                headers={"Accept": "application/json"},
                timeout=timeout_s,
                follow_redirects=True,  # configured internal service, not user input
            )
            if resp.status_code == 403:
                # SearXNG bot protection — usually means `formats: json` missing
                # from settings.yml or the limiter blocking us. Config error, not
                # transient, so no retry.
                raise AdapterError(
                    "searxng returned 403 (json format disabled or limiter)",
                    kind=Outcome.BLOCKED,
                    retryable=False,
                )
            if resp.status_code == 429:
                raise AdapterError("searxng rate limited", kind=Outcome.RATE_LIMITED)
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                raise AdapterError("searxng returned non-object JSON", kind=Outcome.UPSTREAM_ERROR)
            return payload
        except httpx.PoolTimeout as exc:
            raise AdapterError("searxng pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(f"searxng timeout after {timeout_s}s", kind=Outcome.TIMEOUT) from exc
        except httpx.HTTPStatusError as exc:
            raise AdapterError(
                f"searxng HTTP {exc.response.status_code}", kind=Outcome.UPSTREAM_ERROR
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"searxng transport error: {type(exc).__name__}", kind=Outcome.UPSTREAM_ERROR
            ) from exc

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
        params: dict[str, str] = {
            "q": query,
            "format": "json",
            "safesearch": "1",
        }
        if lang:
            params["language"] = lang
        # Map to SearXNG's own category names (docs/19 W2) — never send our
        # internal ids like "financial", which SearXNG does not know.
        params["categories"] = ",".join(map_categories(categories))
        time_range = _time_range(freshness_days)
        if time_range:
            params["time_range"] = time_range

        try:
            payload = await self._fetch(params, timeout_s)
        except AdapterError as first:
            if not first.retryable:
                raise
            # Roadmap Phase 1: exactly one retry on transient failures.
            try:
                payload = await self._fetch(params, timeout_s)
            except AdapterError:
                raise first from None

        results: list[RawResult] = []
        for i, item in enumerate(payload.get("results") or []):
            url = item.get("url")
            if not url:
                continue
            results.append(
                RawResult(
                    url=str(url),
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("content") or ""),
                    adapter=self.id,
                    source_engines=[str(e) for e in (item.get("engines") or [])],
                    adapter_score=_as_float(item.get("score")),
                    published_at=_parse_published(item.get("publishedDate")),
                    rank=i,
                )
            )
            if len(results) >= max_results:
                break
        return results


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def probe(base_url: str, timeout_s: float = 2.0) -> bool:
    """Cheap readiness probe used by /readyz."""
    try:
        client = get_http_client("internal")
        resp = await client.get(f"{base_url.rstrip('/')}/healthz", timeout=timeout_s)
        return resp.status_code < 500
    except Exception:
        return False
