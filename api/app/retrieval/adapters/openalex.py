"""OpenAlex adapter — keyless scholarly works API (docs/19 W1).

``https://api.openalex.org/works?search=...`` — free, no key; the polite pool just
asks for a ``mailto`` (optional setting ``global_search_mailto``). Supports
``filter=language:{lang}`` when the request carries ``lang``. Category: ``academic``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.retrieval.adapters._http import fetch_json
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.openalex.org/works"
_LANG_RE = re.compile(r"^[a-z]{2}$")


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC)


class OpenAlexAdapter:
    id = "openalex"

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
        raw_lang = (lang or "").split("-")[0].lower()
        filters = []
        if raw_lang and _LANG_RE.match(raw_lang):
            filters.append(f"language:{raw_lang}")
        if freshness_days:
            from datetime import timedelta

            since = datetime.now(UTC).date() - timedelta(days=int(freshness_days))
            filters.append(f"from_publication_date:{since.isoformat()}")
        params: dict[str, Any] = {
            "search": q,
            "per-page": str(min(max_results, 50)),
            "sort": "relevance_score:desc",
        }
        if filters:
            params["filter"] = ",".join(filters)
        if self.mailto:
            params["mailto"] = self.mailto
        payload = await fetch_json(
            self.id, self.base_url, params=params, timeout_s=timeout_s, client=self._client
        )
        if not isinstance(payload, dict):
            return []
        works = payload.get("results") or []
        results: list[RawResult] = []
        for w in works:
            if not isinstance(w, dict):
                continue
            wid = w.get("id") or ""
            doi = w.get("doi") or ""
            url = doi or (f"https://openalex.org/{wid}" if wid else "")
            title = str(w.get("title") or "").strip()
            if not url or not title:
                continue
            authors = (w.get("authorships") or [])[:3]
            names = ", ".join((a.get("author") or {}).get("display_name", "") for a in authors)
            venue = ((w.get("primary_location") or {}).get("source") or {}).get(
                "display_name"
            ) or ""
            snippet = f"{w.get('publication_year') or ''} · {venue}".strip(" ·")
            if names:
                snippet = f"{names} — {snippet}" if snippet else names
            snippet = f"cited_by {w.get('cited_by_count') or 0} · {snippet}"
            results.append(
                RawResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                    adapter=self.id,
                    source_engines=["openalex"],
                    published_at=_parse_date(w.get("publication_date")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
