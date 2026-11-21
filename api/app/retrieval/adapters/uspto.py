"""USPTO adapter — United States Patent and Trademark Office patent search.

Fetches patent filings, applications, and grants from USPTO's public APIs.
This enables "Patent Secret Signals" - detecting patents that reveal unannounced
products or technologies.

API: https://developer.uspto.gov
Category: legal, patents, research
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

# docs/32 §6 live probe 2026-11-21: the old developer.uspto.gov/ibd-api
# endpoint 301-redirects to a marketing page (API retired); the replacement
# api.uspto.gov host requires an API token (free at the USPTO Open Data
# Portal). The adapter sends it via X-Api-Key — no key → the engine stays
# disabled (default_configs gates on settings.uspto_api_key).
DEFAULT_BASE_URL = "https://api.uspto.gov/ibd-api/v1"


class USPTOAdapter:
    """Adapter for USPTO patent search.

    The USPTO (United States Patent and Trademark Office) provides APIs for
    searching patents, trademarks, and other intellectual property data.

    This adapter focuses on patent applications and grants, which can provide
    early signals about company innovations and product directions.

    Features:
    - Search patents by company, inventor, keyword
    - Filter by date range (useful for "last 30 days")
    - Extract patent titles, abstracts, filings dates
    - Detect "Secret Signals" - patents that are out of character

    Note: USPTO API has rate limits. Use caching and be respectful.
    """

    id = "uspto"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
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
        timeout_s: float = 8.0,
    ) -> list[RawResult]:
        """Search USPTO patents related to the query.

        Args:
            query: Search term (company name, inventor, keyword, etc.)
            lang: Language hint (currently unused)
            categories: Filter by patent categories
            freshness_days: Filter by filing date (default: last 30 days)
            max_results: Maximum number of patents to return
            timeout_s: Request timeout in seconds

        Returns:
            List of RawResult with patent data
        """
        client = self._client or get_http_client("search")

        # Default to last 30 days if not specified
        if freshness_days is None:
            freshness_days = 30

        # Build date range filter
        end_date = datetime.now(UTC)
        start_date = end_date - timedelta(days=freshness_days)
        start_date_str = start_date.strftime("%Y-%m-%d")
        end_date_str = end_date.strftime("%Y-%m-%d")

        headers = {
            "User-Agent": "MetaSearch/4.1.1 (patent research; +https://metasearch.ai)",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key

        try:
            # USPTO API v1: patent search
            search_url = f"{self.base_url}/patent/search"

            # Build search query
            # USPTO uses Solr-like query syntax
            search_query = self._build_search_query(query)

            params: dict[str, Any] = {
                "searchText": search_query,
                "start": 0,
                "rows": min(max_results, 25),
                "facet": "false",
                "filter": f"patentDate: [{start_date_str} TO {end_date_str}]",
            }

            resp = await client.get(
                search_url,
                params=params,
                headers=headers,
                timeout=timeout_s,
            )

            if resp.status_code == 429:
                raise AdapterError(
                    "uspto rate limited (HTTP 429)",
                    kind=Outcome.RATE_LIMITED,
                    retry_after_s=30.0,
                )
            if resp.status_code == 403:
                raise AdapterError("uspto forbidden", kind=Outcome.BLOCKED, retryable=False)
            if resp.status_code != 200:
                raise AdapterError(
                    f"uspto HTTP {resp.status_code}",
                    kind=Outcome.UPSTREAM_ERROR,
                    retryable=False,
                )

            data = resp.json()
            patents = data.get("patents", [])

            results: list[RawResult] = []
            for rank, patent in enumerate(patents[:max_results], start=1):
                result = self._parse_patent_to_result(patent, query, rank)
                results.append(result)

            return results

        except httpx.PoolTimeout as exc:
            raise AdapterError("uspto pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(f"uspto timeout after {timeout_s}s", kind=Outcome.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"uspto transport error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc

    def _build_search_query(self, query: str) -> str:
        """Build USPTO search query from input."""
        # If query looks like a company name, search in assignee
        # If it looks like an inventor, search in inventor
        # Otherwise, search in title/abstract

        # Simple heuristic: if it contains "Inc", "LLC", "Corp", etc., search assignee
        company_keywords = ["Inc", "LLC", "Corp", "Ltd", "Plc", "GmbH", "Co", "Company"]
        if any(kw in query for kw in company_keywords):
            return f"assignee:({query}) OR applicant:({query})"

        # Otherwise search in title and abstract
        return f"title:({query}) OR abstract:({query}) OR claim:({query})"

    def _parse_patent_to_result(
        self,
        patent: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse USPTO patent data to RawResult."""
        patent_number = patent.get("patentNumber", patent.get("patentId", ""))
        title = patent.get("patentTitle", "Untitled Patent")

        # Build URL
        url = f"https://patft.uspto.gov/netacgi/nph-Parser?patentnumber={patent_number}"

        # Extract dates
        filing_date_str = patent.get("filingDate") or patent.get("applicationDate")
        grant_date_str = patent.get("grantDate") or patent.get("patentDate")

        published_at = None
        if filing_date_str:
            with contextlib.suppress(ValueError, TypeError):
                published_at = datetime.strptime(filing_date_str, "%Y-%m-%d")

        # Extract abstract
        abstract = patent.get("patentAbstract", "")

        # Build snippet
        inventor = patent.get("inventor", [])
        inventor_names = ", ".join([i.get("name", "") for i in inventor[:3]])

        assignee = patent.get("assignee", [])
        assignee_names = ", ".join([a.get("name", "") for a in assignee[:2]])

        snippet_parts = [f"Patent: {patent_number}"]
        if assignee_names:
            snippet_parts.append(f"Assignee: {assignee_names}")
        if inventor_names:
            snippet_parts.append(f"Inventor: {inventor_names}")
        if filing_date_str:
            snippet_parts.append(f"Filed: {filing_date_str}")
        if grant_date_str:
            snippet_parts.append(f"Granted: {grant_date_str}")

        snippet = " | ".join(snippet_parts)

        # Add abstract preview
        if abstract and len(abstract) > 100:
            snippet = f"{snippet} | {abstract[:150]}..."
        elif abstract:
            snippet = f"{snippet} | {abstract}"

        # Detect if this is a "Secret Signal" - patent that might be out of character
        _is_secret_signal = self._detect_secret_signal(patent, query)

        return RawResult(
            url=url,
            title=f"Patent: {title}",
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )

    def _detect_secret_signal(self, patent: dict[str, Any], query: str) -> bool:
        """Detect if this patent is a 'Secret Signal' - out of character or unannounced.

        This is a simplified version. In production, you would:
        1. Compare against the company's historical patent portfolio
        2. Check if the patent describes announced products
        3. Use ML to detect unusual patterns

        For now, we'll use simple heuristics:
        - Patent mentions technologies not typically associated with the company
        - Patent was filed recently (within last 30 days)
        - Patent has unusual keywords
        """
        # For demo purposes, mark all recent patents as potential signals
        # In a real implementation, you would have company profiles
        filing_date_str = patent.get("filingDate") or patent.get("applicationDate", "")

        if not filing_date_str:
            return False

        try:
            filing_date = datetime.strptime(filing_date_str, "%Y-%m-%d")
            # If filed within last 30 days, consider it a signal
            if datetime.now(UTC) - filing_date < timedelta(days=30):
                return True
        except (ValueError, TypeError):
            pass

        return False


class USPTOCompanyAdapter:
    """Adapter for fetching patents for a specific company.

    This is a specialized adapter for company-specific patent searches,
    optimized for "Secret Signals" detection.
    """

    id = "uspto_company"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
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
        timeout_s: float = 8.0,
    ) -> list[RawResult]:
        """Search patents for a specific company."""
        # This is similar to USPTOAdapter but optimized for company queries
        adapter = USPTOAdapter(
            base_url=self.base_url,
            api_key=self.api_key,
            client=self._client,
        )
        return await adapter.search(
            query=query,
            lang=lang,
            categories=categories,
            freshness_days=freshness_days,
            max_results=max_results,
            timeout_s=timeout_s,
        )
