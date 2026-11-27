"""Polymarket adapter — prediction markets data for real-world event probabilities.

Fetches active markets, odds, and volume from Polymarket's Gamma API.
This provides "wisdom of crowds" signals backed by real money.

API: https://gamma-api.polymarket.com
Category: financial, prediction
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"


_TERM_RE = re.compile(r"\w{3,}", re.UNICODE)


def _query_terms(query: str) -> set[str]:
    return {t.lower() for t in _TERM_RE.findall(query or "")}


def _json_list(raw: Any) -> list[Any]:
    """Gamma encodes ``outcomes``/``outcomePrices`` as JSON strings."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
    return []


def _parse_dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp would break freshness math against aware datetimes.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _event_to_result(event: Any, terms: set[str], *, adapter_id: str) -> RawResult | None:
    """One open event -> one result. Odds/volume only as the API states them."""
    if not isinstance(event, dict) or event.get("closed") or event.get("archived"):
        return None
    title = str(event.get("title") or "").strip()
    slug = str(event.get("slug") or "").strip()
    if not title or not slug or not re.fullmatch(r"[A-Za-z0-9-]{1,200}", slug):
        return None
    # Gamma search is fuzzy ("xyzzy nothing" -> "Nothing Ever Happens"): keep an
    # event only when a query term appears in its title or description.
    haystack = f"{title} {event.get('description') or ''}".lower()
    if terms and not any(t in haystack for t in terms):
        return None
    parts: list[str] = []
    raw_markets = event.get("markets")
    markets: list[Any] = raw_markets if isinstance(raw_markets, list) else []
    open_markets = [m for m in markets if isinstance(m, dict) and not m.get("closed")]
    for market in open_markets[:3]:
        names = _json_list(market.get("outcomes"))
        prices = _json_list(market.get("outcomePrices"))
        if not names or len(names) != len(prices):
            continue
        try:
            odds = ", ".join(
                f"{n}: {float(p) * 100:.0f}%" for n, p in zip(names, prices, strict=True)
            )
        except (TypeError, ValueError):
            continue
        question = str(market.get("question") or "").strip()
        parts.append(f"{question} ({odds})" if question else odds)
    volume = event.get("volume")
    snippet = f"Polymarket: {title}"
    if isinstance(volume, (int, float)) and volume >= 0:
        snippet += f" | Volume: ${volume:,.0f}"
    if parts:
        snippet += " | " + " ; ".join(parts)
    end = _parse_dt(event.get("endDate"))
    if end is not None:
        snippet += f" | Resolves: {end.date().isoformat()}"
    return RawResult(
        url=f"https://polymarket.com/event/{slug}",
        title=f"Polymarket: {title}",
        snippet=snippet[:600],
        rank=0,
        adapter=adapter_id,
        source_engines=[adapter_id],
        published_at=_parse_dt(event.get("createdAt")) or _parse_dt(event.get("startDate")),
    )


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
            # docs/36 S4: ``/markets/search`` now answers 422 for every query
            # (probe 2026-11-26, 4/4). The current Gamma search is
            # ``/public-search`` and returns events with their markets inline —
            # one request instead of 1 + N detail calls.
            resp = await client.get(
                f"{self.base_url}/public-search",
                params={"q": query, "limit_per_type": str(min(max_results, 20))},
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
            try:
                payload = resp.json()
            except ValueError as exc:
                raise AdapterError("polymarket returned non-JSON body") from exc
            events = payload.get("events") if isinstance(payload, dict) else None
            if not isinstance(events, list):
                return []
            terms = _query_terms(query)
            results: list[RawResult] = []
            for event in events:
                result = _event_to_result(event, terms, adapter_id=self.id)
                if result is not None:
                    result.rank = len(results)
                    results.append(result)
                if len(results) >= max_results:
                    break
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
