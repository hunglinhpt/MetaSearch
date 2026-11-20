"""StockTwits adapter — trader sentiment and discussions for stocks and crypto.

Fetches messages, sentiment, and engagement from StockTwits.
This provides real-time trader community insights.

API: https://api.stocktwits.com (unofficial, via web scraping)
Category: financial, social
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.stocktwits.com"

# Regex to extract symbol from query (e.g., "AAPL", "TSLA", "BTC-USD")
SYMBOL_PATTERN = re.compile(r"\b([A-Z]{1,5}(?:-[A-Z]{1,5})?)\b")


class StockTwitsAdapter:
    """Adapter for StockTwits trader sentiment.

    StockTwits is a social network for traders and investors.
    This adapter fetches recent messages, sentiment, and engagement
    for stocks and cryptocurrencies.

    Features:
    - Fetch messages for a symbol
    - Extract sentiment (bullish/bearish)
    - Get engagement metrics (likes, replies)
    - Real-time trader discussions

    Note: StockTwits API is unofficial. Uses web scraping as fallback.
    """

    id = "stocktwits"

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
        """Search StockTwits for messages related to the query.

        Args:
            query: Search term (stock symbol like "AAPL", "TSLA", "BTC-USD")
            lang: Language hint (currently unused)
            categories: Filter by categories
            freshness_days: Filter by message date
            max_results: Maximum number of messages to return
            timeout_s: Request timeout in seconds

        Returns:
            List of RawResult with StockTwits messages
        """
        # Try to extract symbol from query
        symbols = self._extract_symbols(query)

        if not symbols:
            # If no symbol found, return empty results
            # StockTwits requires a symbol
            return []

        client = self._client or get_http_client("search")
        headers = {
            "User-Agent": "MetaSearch/4.1.1 (trader sentiment; +https://metasearch.ai)",
            "Accept": "application/json",
        }

        results: list[RawResult] = []

        for symbol in symbols[:3]:  # Try up to 3 symbols
            try:
                messages = await self._fetch_messages_for_symbol(
                    client, symbol, headers, max_results, timeout_s
                )
                for rank, message in enumerate(messages, start=1):
                    result = self._parse_message_to_result(message, symbol, query, rank)
                    results.append(result)

                # If we got results, stop after first symbol
                if messages:
                    break
            except AdapterError:
                continue

        return results

    def _extract_symbols(self, query: str) -> list[str]:
        """Extract potential stock/crypto symbols from query."""
        # Look for common symbol patterns
        matches = SYMBOL_PATTERN.findall(query)

        # Filter out common false positives
        common_words = {"AND", "OR", "NOT", "THE", "FOR", "FROM", "TO", "IN", "ON", "AT", "BY"}
        symbols = [m for m in matches if m not in common_words]

        return symbols

    async def _fetch_messages_for_symbol(
        self,
        client: httpx.AsyncClient,
        symbol: str,
        headers: dict[str, str],
        max_results: int,
        timeout_s: float,
    ) -> list[dict[str, Any]]:
        """Fetch recent messages for a symbol from StockTwits."""
        # StockTwits API endpoint for messages
        url = f"{self.base_url}/api/2/streams/symbol/{symbol}.json"

        try:
            params: dict[str, Any] = {
                "filter": "all",
                "limit": min(max_results, 30),
            }

            resp = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout_s,
            )

            if resp.status_code == 429:
                raise AdapterError(
                    "stocktwits rate limited (HTTP 429)",
                    kind=Outcome.RATE_LIMITED,
                    retry_after_s=10.0,
                )
            if resp.status_code == 404:
                # Symbol not found, try with different format
                return []
            if resp.status_code != 200:
                raise AdapterError(
                    f"stocktwits HTTP {resp.status_code}",
                    kind=Outcome.UPSTREAM_ERROR,
                    retryable=False,
                )

            data = resp.json()
            messages = data.get("messages", [])

            return messages

        except httpx.PoolTimeout as exc:
            raise AdapterError("stocktwits pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"stocktwits timeout after {timeout_s}s", kind=Outcome.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"stocktwits transport error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc

    def _parse_message_to_result(
        self,
        message: dict[str, Any],
        symbol: str,
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse StockTwits message to RawResult."""
        message_id = message.get("id", "")
        body = message.get("body", "")

        # Extract sentiment
        sentiment = message.get("sentiment", {})
        sentiment_basic = sentiment.get("basic", "neutral")
        sentiment_score = sentiment.get("score", 0)

        # Extract engagement
        likes = message.get("likes", {}).get("total", 0)
        replies = message.get("conversation", {}).get("total_replies", 0)

        # Build URL
        url = f"https://stocktwits.com/{symbol}/message/{message_id}"

        # Extract timestamp
        created_at = message.get("created_at")
        published_at = None
        if created_at:
            try:
                # StockTwits uses Unix timestamp in milliseconds
                timestamp = int(created_at) / 1000
                published_at = datetime.fromtimestamp(timestamp, tz=UTC)
            except (ValueError, TypeError):
                pass

        # Extract user info
        user = message.get("user", {})
        username = user.get("username", "anonymous")

        # Build snippet
        sentiment_emoji = self._get_sentiment_emoji(sentiment_basic, sentiment_score)
        snippet = f"StockTwits: {symbol} | {sentiment_emoji} {username}: {body[:150]}..."

        if likes > 0 or replies > 0:
            snippet += f" | {likes} likes, {replies} replies"

        return RawResult(
            url=url,
            title=f"StockTwits: {symbol}",
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )

    def _get_sentiment_emoji(self, basic: str, score: float) -> str:
        """Get emoji representation of sentiment."""
        if basic == "bullish" or score > 0.3:
            return "🟢"
        elif basic == "bearish" or score < -0.3:
            return "🔴"
        else:
            return "⚪"


class StockTwitsTrendingAdapter:
    """Adapter for fetching trending stocks from StockTwits."""

    id = "stocktwits_trending"

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
        """Fetch trending stocks from StockTwits."""
        client = self._client or get_http_client("search")
        headers = {
            "User-Agent": "MetaSearch/4.1.1 (trending stocks; +https://metasearch.ai)",
            "Accept": "application/json",
        }

        try:
            url = f"{self.base_url}/api/2/trending/symbols.json"
            params: dict[str, Any] = {
                "limit": min(max_results, 30),
            }

            resp = await client.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout_s,
            )

            if resp.status_code != 200:
                return []

            data = resp.json()
            symbols = data.get("symbols", [])

            results: list[RawResult] = []
            for rank, symbol_data in enumerate(symbols[:max_results], start=1):
                result = self._parse_trending_symbol(symbol_data, rank)
                results.append(result)

            return results

        except Exception:
            return []

    def _parse_trending_symbol(self, symbol_data: dict[str, Any], rank: int) -> RawResult:
        """Parse trending symbol data to RawResult."""
        symbol = symbol_data.get("symbol", "")
        title = symbol_data.get("title", symbol)

        # Extract metrics
        messages = symbol_data.get("messages", 0)
        followers = symbol_data.get("followers", 0)

        # Build URL
        url = f"https://stocktwits.com/{symbol}"

        snippet = f"Trending: {symbol} ({title}) | {messages} messages | {followers} followers"

        return RawResult(
            url=url,
            title=f"Trending: {symbol}",
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=None,
        )
