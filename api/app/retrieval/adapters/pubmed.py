"""PubMed / NCBI adapter — vertical engine for academic & biomedical research.

Uses the official NCBI E-Utilities JSON API (esearch + esummary).
Keyless quota: 3 requests/sec; optional NCBI api_key extends to 10 requests/sec.
Category: ``academic``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from app.retrieval.adapters.base import AdapterError
from app.retrieval.models import RawResult

DEFAULT_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
DEFAULT_ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    return _WS.sub(" ", _TAG.sub("", text or "")).strip()


def _parse_pubmed_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    # sortpubdate format: "2023/08/01 00:00"
    match = re.search(r"(\d{4})/(\d{2})/(\d{2})", date_str)
    if match:
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=UTC,
            )
        except (ValueError, TypeError):
            pass
    # fallback to just year
    year_match = re.search(r"\b(19\d\d|20\d\d)\b", date_str)
    if year_match:
        try:
            return datetime(int(year_match.group(1)), 1, 1, tzinfo=UTC)
        except (ValueError, TypeError):
            pass
    return None


class PubMedAdapter:
    id = "pubmed"

    def __init__(
        self,
        *,
        search_url: str = DEFAULT_ESEARCH_URL,
        summary_url: str = DEFAULT_ESUMMARY_URL,
        api_key: str = "",
    ) -> None:
        self.search_url = search_url
        self.summary_url = summary_url
        self.api_key = api_key

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 5.0,
    ) -> list[RawResult]:
        search_params = {
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": str(min(max_results, 20)),
            "sort": "relevance",
        }
        if self.api_key:
            search_params["api_key"] = self.api_key

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 MetaSearch/0.7.0"
            ),
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
                s_resp = await client.get(self.search_url, params=search_params, headers=headers)
                if s_resp.status_code != 200:
                    raise AdapterError(f"pubmed search HTTP {s_resp.status_code}", retryable=False)
                s_data = s_resp.json()
                id_list = s_data.get("esearchresult", {}).get("idlist", [])
                if not id_list:
                    return []

                summary_params = {
                    "db": "pubmed",
                    "id": ",".join(id_list),
                    "retmode": "json",
                }
                if self.api_key:
                    summary_params["api_key"] = self.api_key

                sum_resp = await client.get(
                    self.summary_url, params=summary_params, headers=headers
                )
                if sum_resp.status_code != 200:
                    raise AdapterError(f"pubmed summary HTTP {sum_resp.status_code}")
                sum_data = sum_resp.json()
        except httpx.TimeoutException as exc:
            raise AdapterError(f"pubmed timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"pubmed transport error: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise AdapterError("pubmed returned non-JSON body") from exc

        result_dict = sum_data.get("result", {})
        results: list[RawResult] = []

        for rank, pmid in enumerate(id_list, start=1):
            doc = result_dict.get(pmid)
            if not doc or not isinstance(doc, dict):
                continue

            raw_title = doc.get("title", "")
            title = _strip_html(raw_title).rstrip(".")
            source = doc.get("source", "PubMed")
            pubdate = doc.get("pubdate", "")
            authors = [a.get("name", "") for a in doc.get("authors", []) if a.get("name")]
            authors_str = (
                ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
                if authors
                else "Unknown"
            )

            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            snippet = f"[{source} {pubdate}] Authors: {authors_str} · PMID: {pmid}"
            published_at = _parse_pubmed_date(doc.get("sortpubdate") or pubdate)

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
