"""Semantic Scholar adapter — vertical engine for academic papers and citations.

Uses the official Semantic Scholar Academic Graph API (api.semanticscholar.org).
Keyless quota: 100 requests/5min; optional API key extends limits.
Category: ``academic``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from app.retrieval.adapters.base import AdapterError
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    return _WS.sub(" ", _TAG.sub("", text or "")).strip()


class SemanticScholarAdapter:
    id = "semanticscholar"

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, api_key: str = "") -> None:
        self.base_url = base_url
        self.api_key = api_key

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
        params = {
            "query": query,
            "limit": str(min(max_results, 25)),
            "fields": "title,url,abstract,year,citationCount,authors",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 MetaSearch/0.7.0"
            ),
            "Accept": "application/json",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
                resp = await client.get(self.base_url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise AdapterError(f"semanticscholar timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"semanticscholar transport error: {type(exc).__name__}") from exc

        if resp.status_code == 429:
            raise AdapterError("semanticscholar rate limited (HTTP 429)", retryable=False)
        if resp.status_code != 200:
            raise AdapterError(f"semanticscholar HTTP {resp.status_code}", retryable=False)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise AdapterError("semanticscholar returned non-JSON body") from exc

        items = payload.get("data") or []
        results: list[RawResult] = []

        for rank, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue

            title = _strip_html(item.get("title") or "")
            if not title:
                continue

            paper_id = item.get("paperId")
            url = item.get("url") or (
                f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else ""
            )
            if not url:
                continue

            year = item.get("year")
            citations = item.get("citationCount", 0)
            authors = [a.get("name", "") for a in item.get("authors", []) if a.get("name")]
            authors_str = (
                ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
                if authors
                else "Unknown"
            )
            abstract = _strip_html(item.get("abstract") or "")[:200]

            snippet = f"[{year or 'Academic'}] Citations: {citations:,} · Authors: {authors_str}"
            if abstract:
                snippet = f"{snippet} · {abstract}"

            published_at = datetime(int(year), 1, 1, tzinfo=UTC) if year else None

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
