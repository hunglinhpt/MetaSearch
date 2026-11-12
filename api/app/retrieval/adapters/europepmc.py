"""Europe PMC adapter — keyless biomedical literature API (docs/19 W1).

``https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=...&format=json``
— free, no key, full-text open access subset. Category: ``academic``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from app.retrieval.adapters._http import fetch_json
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC)


class EuropePmcAdapter:
    id = "europepmc"

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
        timeout_s: float = 5.0,
        **kwargs: Any,
    ) -> list[RawResult]:
        q = (query or "").strip()
        if not q:
            return []
        full_q = q
        if freshness_days:
            from datetime import date, timedelta

            since = date.today() - timedelta(days=int(freshness_days))
            full_q = f"({q}) AND FIRST_PDATE:[{since.isoformat()} TO 3000-12-31]"
        params: dict[str, Any] = {
            "query": full_q,
            "format": "json",
            "pageSize": str(min(max_results, 100)),
            "resultType": "core",
        }
        payload = await fetch_json(
            self.id, self.base_url, params=params, timeout_s=timeout_s, client=self._client
        )
        hits = (payload.get("resultList") or {}).get("result") or []
        results: list[RawResult] = []
        for h in hits:
            pmid = h.get("pmid") or ""
            pmcid = h.get("pmcid") or ""
            doi = h.get("doi") or ""
            if pmcid:
                url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
            elif doi:
                url = f"https://doi.org/{doi}"
            elif pmid:
                url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            else:
                continue
            title = str(h.get("title") or "").strip()
            if not title:
                continue
            journal = h.get("journalTitle") or ""
            snippet = f"{h.get('pubYear') or ''} · {journal}".strip(" ·")
            if h.get("openAccessFlag") == "Y":
                snippet = f"open access · {snippet}" if snippet else "open access"
            results.append(
                RawResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                    adapter=self.id,
                    source_engines=["europepmc"],
                    published_at=_parse_date(h.get("firstPublicationDate")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
