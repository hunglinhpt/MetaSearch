"""Crossref adapter — keyless scholarly metadata API (docs/19 W1).

``https://api.crossref.org/works?query=...`` — free; polite pool via optional
``mailto`` (setting ``global_search_mailto``). Returns DOI + ``issued`` date.
Category: ``academic``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.retrieval.adapters._http import fetch_json
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.crossref.org/works"


def _parse_issued(value: Any) -> datetime | None:
    # Nested-field drift guard (audit v2.4 round 9): `issued` may arrive as a
    # non-dict or `date-parts[0]` as a scalar — a malformed record must be
    # skipped, never fail the whole adapter.
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
        return None
    try:
        ymd = [int(x) for x in [*parts[0], 1, 1]][:3]
        return datetime(ymd[0], ymd[1], ymd[2], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


class CrossrefAdapter:
    id = "crossref"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        mailto: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url
        self.mailto = mailto
        self._client = client

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 10,
        timeout_s: float = 5.0,
        **kwargs: Any,
    ) -> list[RawResult]:
        q = (query or "").strip()
        if not q:
            return []
        params: dict[str, Any] = {
            "query.bibliographic": q,
            "rows": str(min(max_results, 50)),
            "select": (
                "DOI,title,author,container-title,issued,type,publisher,is-referenced-by-count"
            ),
        }
        if freshness_days:
            from datetime import timedelta

            since = datetime.now(UTC).date() - timedelta(days=int(freshness_days))
            params["filter"] = f"from-issued-date:{since.isoformat()}"
        if self.mailto:
            params["mailto"] = self.mailto
        payload = await fetch_json(
            self.id, self.base_url, params=params, timeout_s=timeout_s, client=self._client
        )
        if not isinstance(payload, dict):
            return []
        message = payload.get("message")
        if not isinstance(message, dict):
            return []
        items = message.get("items") or []
        results: list[RawResult] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            doi = str(it.get("DOI") or "").lower()
            if not doi:
                continue
            titles = it.get("title") or []
            title = str(titles[0]).strip() if titles else ""
            if not title:
                continue
            authors = it.get("author") or []
            names = ", ".join(
                " ".join(filter(None, [a.get("given", ""), a.get("family", "")]))
                for a in authors[:3]
            )
            container = it.get("container-title") or []
            venue = str(container[0]) if container else ""
            snippet = f"{it.get('type') or 'work'} · {venue}".strip(" ·")
            if names:
                snippet = f"{names} — {snippet}" if snippet else names
            results.append(
                RawResult(
                    url=f"https://doi.org/{doi}",
                    title=title,
                    snippet=snippet,
                    adapter=self.id,
                    source_engines=["crossref"],
                    published_at=_parse_issued(it.get("issued")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
