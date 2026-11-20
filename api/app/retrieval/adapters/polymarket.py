"""Polymarket adapter — prediction markets data for real-world event probabilities.

Fetches active markets, odds, and volume from Polymarket's Gamma API.
This provides "wisdom of crowds" signals backed by real money.

API: https://gamma-api.polymarket.com
Category: financial, prediction
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"


class PolymarketAdapter:
    """Adapter for Polymarket prediction markets.

    Polymarket allows users to bet on real-world events, creating a market-based
    probability system. This adapter fetches active markets related to a query
    and returns them as search results with odds and volume information.

    Features:
    - Search markets by keyword/query
    - Get odds for each outcome
    - Volume and liquidity data
    - Recent activity signals

    Note: Polymarket API is rate-limited. Use sparingly and cache results.
    """

    id = "polymarket"

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
        timeout_s: float = 5.0,
    ) -> list[RawResult]:
        """Search Polymarket for markets related to the query.

        Args:
            query: Search term (e.g., "Apple acquisition", "Bitcoin ETF approval")
            lang: Language hint (currently unused)
            categories: Filter by market categories
            freshness_days: Filter by market creation date
            max_results: Maximum number of markets to return
            timeout_s: Request timeout in seconds

        Returns:
            List of RawResult with market data including odds and volume
        """
        # Build request headers
        headers = {
            "User-Agent": "MetaSearch/4.1.1 (prediction market research; +https://metasearch.ai)",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        client = self._client or get_http_client("search")

        try:
            # Step 1: Search for markets matching the query
            search_url = f"{self.base_url}/markets/search"
            params: dict[str, Any] = {
                "query": query,
                "limit": str(min(max_results, 20)),
            }

            resp = await client.get(
                search_url,
                params=params,
                headers=headers,
                timeout=timeout_s,
            )

            if resp.status_code == 429:
                raise AdapterError(
                    "polymarket rate limited (HTTP 429)",
                    kind=Outcome.RATE_LIMITED,
                    retry_after_s=10.0,
                )
            if resp.status_code == 403:
                raise AdapterError("polymarket forbidden", kind=Outcome.BLOCKED, retryable=False)
            if resp.status_code != 200:
                raise AdapterError(
                    f"polymarket HTTP {resp.status_code}",
                    kind=Outcome.UPSTREAM_ERROR,
                    retryable=False,
                )

            search_data = resp.json()
            market_ids = [m["id"] for m in search_data.get("markets", [])[:max_results]]

            if not market_ids:
                return []

            # Step 2: Fetch detailed data for each market
            results: list[RawResult] = []
            for market_id in market_ids:
                market_data = await self._fetch_market_details(
                    client, market_id, headers, timeout_s
                )
                if market_data:
                    result = self._parse_market_to_result(market_data, query)
                    results.append(result)

            return results

        except httpx.PoolTimeout as exc:
            raise AdapterError("polymarket pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"polymarket timeout after {timeout_s}s", kind=Outcome.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"polymarket transport error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc

    async def _fetch_market_details(
        self,
        client: httpx.AsyncClient,
        market_id: str,
        headers: dict[str, str],
        timeout_s: float,
    ) -> dict[str, Any] | None:
        """Fetch detailed data for a specific market."""
        url = f"{self.base_url}/markets/{market_id}"

        try:
            resp = await client.get(
                url,
                headers=headers,
                timeout=timeout_s,
            )

            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    def _parse_market_to_result(
        self,
        market_data: dict[str, Any],
        query: str,
    ) -> RawResult:
        """Convert Polymarket market data to a RawResult."""
        # Extract basic info
        market_id = market_data.get("id", "")
        question = market_data.get("question", "")
        url = market_data.get("url") or f"https://polymarket.com/event/{market_id}"

        # Extract creation time
        created_at = market_data.get("createdAt")
        published_at = None
        if created_at:
            with contextlib.suppress(ValueError, TypeError):
                published_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        # Extract outcomes and calculate odds
        outcomes = market_data.get("outcomes", [])
        total_volume = sum(o.get("volumeUsd", 0) for o in outcomes)

        # Build snippet with top outcomes
        outcome_strings = []
        for outcome in outcomes[:3]:  # Top 3 outcomes
            name = outcome.get("name", "Unknown")
            probability = outcome.get("probability", 0) * 100
            volume = outcome.get("volumeUsd", 0)
            outcome_strings.append(f"{name}: {probability:.0f}% ({volume:,.0f} USD)")

        snippet = (
            f"Polymarket: {question} | Volume: ${total_volume:,.0f} | {', '.join(outcome_strings)}"
        )

        # Try to find the most relevant outcome based on query
        best_outcome = None
        best_score = 0.0
        for outcome in outcomes:
            score = self._similarity_score(query.lower(), outcome.get("name", "").lower())
            if score > best_score:
                best_score = score
                best_outcome = outcome

        # If we have a good match, highlight it
        if best_outcome and best_score > 0.5:
            probability = best_outcome.get("probability", 0) * 100
            snippet = (
                f"Polymarket: {question} | "
                f"{best_outcome.get('name', 'Result')}: {probability:.0f}% "
                f"| Total: ${total_volume:,.0f}"
            )

        return RawResult(
            url=url,
            title=f"Polymarket: {question}",
            snippet=snippet,
            rank=0,  # Will be set by merger
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )

    def _similarity_score(self, query: str, text: str) -> float:
        """Simple similarity score between query and text."""
        if not query or not text:
            return 0.0

        query_words = set(query.split())
        text_words = set(text.split())

        if not query_words:
            return 0.0

        return len(query_words & text_words) / len(query_words)


class PolymarketTrendingAdapter:
    """Adapter for fetching trending Polymarket markets.

    Returns currently active and trending markets without a specific query.
    Useful for discovering what topics people are betting on.
    """

    id = "polymarket_trending"

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
        timeout_s: float = 5.0,
    ) -> list[RawResult]:
        """Fetch trending markets from Polymarket."""
        headers = {
            "User-Agent": "MetaSearch/4.1.1 (trending prediction markets; +https://metasearch.ai)",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        client = self._client or get_http_client("search")

        try:
            # Fetch trending markets
            url = f"{self.base_url}/markets/trending"
            params: dict[str, Any] = {
                "limit": str(min(max_results, 20)),
            }

            resp = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout_s,
            )

            if resp.status_code == 429:
                raise AdapterError(
                    "polymarket rate limited (HTTP 429)",
                    kind=Outcome.RATE_LIMITED,
                    retry_after_s=10.0,
                )
            if resp.status_code != 200:
                raise AdapterError(
                    f"polymarket HTTP {resp.status_code}",
                    kind=Outcome.UPSTREAM_ERROR,
                    retryable=False,
                )

            data = resp.json()
            markets = data.get("markets", [])

            results: list[RawResult] = []
            for rank, market in enumerate(markets[:max_results], start=1):
                result = self._parse_trending_market(market, rank)
                results.append(result)

            return results

        except httpx.PoolTimeout as exc:
            raise AdapterError("polymarket pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"polymarket timeout after {timeout_s}s", kind=Outcome.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"polymarket transport error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc

    def _parse_trending_market(self, market: dict[str, Any], rank: int) -> RawResult:
        """Parse trending market data to RawResult."""
        market_id = market.get("id", "")
        question = market.get("question", "")
        url = market.get("url") or f"https://polymarket.com/event/{market_id}"

        created_at = market.get("createdAt")
        published_at = None
        if created_at:
            with contextlib.suppress(ValueError, TypeError):
                published_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        outcomes = market.get("outcomes", [])
        total_volume = sum(o.get("volumeUsd", 0) for o in outcomes)

        # Build snippet
        outcome_strings = []
        for outcome in outcomes[:2]:
            name = outcome.get("name", "Unknown")
            probability = outcome.get("probability", 0) * 100
            outcome_strings.append(f"{name}: {probability:.0f}%")

        snippet = (
            f"Trending: {question} | Volume: ${total_volume:,.0f} | {', '.join(outcome_strings)}"
        )

        return RawResult(
            url=url,
            title=f"Trending: {question}",
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )
