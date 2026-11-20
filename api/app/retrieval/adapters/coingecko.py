"""CoinGecko adapter - Cryptocurrency data.

Free cryptocurrency API.
Free tier: 10-30 calls per minute (recommended: 10-50 calls/min).
No API key required, but recommended for higher limits.

Category: crypto, finance, blockchain, research
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

logger = logging.getLogger(__name__)

# API documentation: https://www.coingecko.com/en/api
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

# Rate limit: 10-30 calls per minute (free tier)
# With API key: 500 calls per minute


class CoinGeckoAdapter:
    """Adapter for CoinGecko cryptocurrency data."""

    id = "coingecko"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or ""

    @property
    def requires_key(self) -> bool:
        return False  # API key is optional

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
        """Search for cryptocurrency data based on query.

        Supports:
        - Coin names (e.g., "bitcoin", "ethereum")
        - Coin symbols (e.g., "btc", "eth")
        - Market data
        - Historical data
        - Trending coins
        """
        results: list[RawResult] = []

        try:
            # Parse query to determine coin and function
            coin_id, function = self._parse_query(query)

            if function == "info":
                data = await self._fetch_coin_info(coin_id, lang, timeout_s)
                if data:
                    result = self._parse_coin_info_to_result(data, query, 1)
                    results.append(result)

            elif function == "market":
                data = await self._fetch_market_data(coin_id, timeout_s)
                if data:
                    result = self._parse_market_data_to_result(data, query, 1)
                    results.append(result)

            elif function == "price":
                # Get price for specific coin
                data = await self._fetch_simple_price(coin_id, timeout_s)
                if data and coin_id in data:
                    price_data = data[coin_id]
                    result = self._parse_price_to_result(price_data, coin_id, query, 1)
                    results.append(result)

            elif function == "trending":
                data = await self._fetch_trending(max_results, timeout_s)
                if data:
                    for idx, coin in enumerate(data.get("coins", [])[:max_results]):
                        result = self._parse_trending_coin_to_result(coin, query, idx + 1)
                        results.append(result)

            elif function == "list":
                coin_list = await self._fetch_coin_list(max_results, timeout_s)
                if coin_list:
                    for idx, coin in enumerate(coin_list[:max_results]):
                        result = self._parse_coin_list_item_to_result(coin, query, idx + 1)
                        results.append(result)

            else:
                # Default: try to get market data
                data = await self._fetch_market_data(coin_id, timeout_s)
                if data:
                    result = self._parse_market_data_to_result(data, query, 1)
                    results.append(result)

        except httpx.HTTPError as exc:
            logger.warning("CoinGecko HTTP error for query '%s': %s", query, exc)
            raise AdapterError(
                f"coingecko HTTP error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning("CoinGecko timeout for query '%s': %s", query, exc)
            raise AdapterError(
                f"coingecko timeout after {timeout_s}s",
                kind=Outcome.TIMEOUT,
            ) from exc
        except httpx.PoolTimeout as exc:
            logger.warning("CoinGecko pool timeout for query '%s': %s", query, exc)
            raise AdapterError(
                "coingecko pool timeout",
                kind=Outcome.LOCAL_OVERLOAD,
            ) from exc

        return results

    def _parse_query(self, query: str) -> tuple[str, str]:
        """Parse query to extract coin ID and function."""
        query_lower = query.lower()

        # Default function
        function = "market"

        # Check for specific functions
        if any(word in query_lower for word in ["trending", "hot", "popular"]):
            function = "trending"
        elif any(word in query_lower for word in ["list", "all", "coins"]):
            function = "list"
        elif any(word in query_lower for word in ["price", "usd", "market"]):
            function = "price"
        elif any(word in query_lower for word in ["info", "details", "about"]):
            function = "info"

        # Try to extract coin ID
        coin_id = self._extract_coin_id(query)

        return coin_id, function

    def _extract_coin_id(self, query: str) -> str:
        """Extract coin ID from query."""
        import re

        query_lower = query.lower()

        # Common coin mappings
        coin_map = {
            "bitcoin": "bitcoin",
            "btc": "bitcoin",
            "ethereum": "ethereum",
            "eth": "ethereum",
            "tether": "tether",
            "usdt": "tether",
            "binance coin": "binancecoin",
            "bnb": "binancecoin",
            "solana": "solana",
            "sol": "solana",
            "cardano": "cardano",
            "ada": "cardano",
            "xrp": "ripple",
            "ripple": "ripple",
            "dogecoin": "dogecoin",
            "doge": "dogecoin",
            "polkadot": "polkadot",
            "dot": "polkadot",
            "litecoin": "litecoin",
            "ltc": "litecoin",
        }

        for name, coin_id in coin_map.items():
            if name in query_lower:
                return coin_id

        # Try to find coin symbol (3-4 letters)
        symbol_pattern = r"\b([a-z]{3,4})\b"
        match = re.search(symbol_pattern, query_lower)
        if match:
            symbol = match.group(1)
            # Map common symbols
            symbol_map = {"btc": "bitcoin", "eth": "ethereum", "usdt": "tether"}
            return symbol_map.get(symbol, symbol)

        # Default: use query as coin ID
        return query.strip().lower()

    async def _fetch_coin_info(
        self, coin_id: str, lang: str | None, timeout_s: float
    ) -> dict[str, Any] | None:
        """Fetch coin information."""
        if not coin_id:
            return None

        url = f"{COINGECKO_BASE_URL}/coins/{coin_id}"

        params: dict[str, Any] = {}
        if lang:
            params[" localization"] = lang  # Note: space is intentional

        headers = {}
        if self.api_key:
            headers["x-cg-demo-api-key"] = self.api_key

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, params=params, headers=headers, timeout=timeout_s)

                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 404:
                    logger.debug("Coin not found: %s", coin_id)
                    return None
                elif resp.status_code == 429:
                    logger.warning("CoinGecko rate limit exceeded")
                    return None
                else:
                    logger.debug("CoinGecko HTTP %s: %s", resp.status_code, resp.text)
                    return None

            except Exception as exc:
                logger.debug("Error fetching coin info: %s", exc)
                return None

    async def _fetch_market_data(self, coin_id: str, timeout_s: float) -> dict[str, Any] | None:
        """Fetch market data for a coin."""
        if not coin_id:
            return None

        url = f"{COINGECKO_BASE_URL}/coins/markets"

        params: dict[str, str | int | bool] = {
            "vs_currency": "usd",
            "ids": coin_id,
            "order": "market_cap_desc",
            "per_page": 1,
            "page": 1,
            "sparkline": False,
        }

        headers = {}
        if self.api_key:
            headers["x-cg-demo-api-key"] = self.api_key

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, params=params, headers=headers, timeout=timeout_s)

                if resp.status_code == 200:
                    data = resp.json()
                    return data[0] if data else None
                elif resp.status_code == 429:
                    logger.warning("CoinGecko market rate limit exceeded")
                    return None
                else:
                    logger.debug("CoinGecko market HTTP %s: %s", resp.status_code, resp.text)
                    return None

            except Exception as exc:
                logger.debug("Error fetching market data: %s", exc)
                return None

    async def _fetch_simple_price(self, coin_id: str, timeout_s: float) -> dict[str, Any] | None:
        """Fetch simple price data for multiple coins."""
        if not coin_id:
            return None

        url = f"{COINGECKO_BASE_URL}/simple/price"

        params: dict[str, str | bool] = {
            "ids": coin_id,
            "vs_currencies": "usd,eur,btc",
            "include_market_cap": True,
            "include_24hr_vol": True,
            "include_24hr_change": True,
            "include_last_updated_at": True,
        }

        headers = {}
        if self.api_key:
            headers["x-cg-demo-api-key"] = self.api_key

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, params=params, headers=headers, timeout=timeout_s)

                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    logger.warning("CoinGecko price rate limit exceeded")
                    return None
                else:
                    logger.debug("CoinGecko price HTTP %s: %s", resp.status_code, resp.text)
                    return None

            except Exception as exc:
                logger.debug("Error fetching price: %s", exc)
                return None

    async def _fetch_trending(self, limit: int, timeout_s: float) -> dict[str, Any] | None:
        """Fetch trending cryptocurrencies."""
        url = f"{COINGECKO_BASE_URL}/search/trending"

        headers = {}
        if self.api_key:
            headers["x-cg-demo-api-key"] = self.api_key

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, headers=headers, timeout=timeout_s)

                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    logger.warning("CoinGecko trending rate limit exceeded")
                    return None
                else:
                    logger.debug("CoinGecko trending HTTP %s: %s", resp.status_code, resp.text)
                    return None

            except Exception as exc:
                logger.debug("Error fetching trending: %s", exc)
                return None

    async def _fetch_coin_list(self, limit: int, timeout_s: float) -> list[dict[str, Any]]:
        """Fetch list of all coins."""
        url = f"{COINGECKO_BASE_URL}/coins/list"

        params = {
            "include_platform": False,
        }

        headers = {}
        if self.api_key:
            headers["x-cg-demo-api-key"] = self.api_key

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, params=params, headers=headers, timeout=timeout_s)

                if resp.status_code == 200:
                    return resp.json()[:limit]
                elif resp.status_code == 429:
                    logger.warning("CoinGecko list rate limit exceeded")
                    return []
                else:
                    logger.debug("CoinGecko list HTTP %s: %s", resp.status_code, resp.text)
                    return []

            except Exception as exc:
                logger.debug("Error fetching coin list: %s", exc)
                return []

    def _parse_coin_info_to_result(
        self,
        data: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse coin info to RawResult."""
        name = data.get("name", "")
        symbol = data.get("symbol", "").upper()
        description = data.get("description", {}).get("en", "")
        categories = data.get("categories", [])
        homepage = data.get("links", {}).get("homepage", [""])[0]
        market_cap_rank = data.get("market_cap_rank", 0)

        title = f"Coin: {name} ({symbol})"

        snippet_parts = []
        if description:
            snippet_parts.append(description[:300])
        if categories:
            snippet_parts.append(f"Categories: {', '.join(categories[:3])}")
        if market_cap_rank:
            snippet_parts.append(f"Rank: #{market_cap_rank}")

        snippet = " | ".join(snippet_parts) if snippet_parts else ""

        coin_id = data.get("id", "")
        url = homepage if homepage else f"https://www.coingecko.com/en/coins/{coin_id}"

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=None,
        )

    def _parse_market_data_to_result(
        self,
        data: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse market data to RawResult."""
        name = data.get("name", "")
        symbol = data.get("symbol", "").upper()
        current_price = data.get("current_price", 0)
        market_cap = data.get("market_cap", 0)
        market_cap_rank = data.get("market_cap_rank", 0)
        price_change_24h = data.get("price_change_percentage_24h", 0)
        volume_24h = data.get("total_volume", 0)
        last_updated = data.get("last_updated", "")

        title = f"Market: {name} ({symbol}) - ${current_price:,.2f}"

        snippet_parts = []
        if price_change_24h is not None:
            sign = "+" if price_change_24h >= 0 else ""
            snippet_parts.append(f"24h: {sign}{price_change_24h:.2f}%")
        if market_cap:
            snippet_parts.append(f"Market Cap: ${market_cap:,.0f}")
        if volume_24h:
            snippet_parts.append(f"Volume: ${volume_24h:,.0f}")
        if market_cap_rank:
            snippet_parts.append(f"Rank: #{market_cap_rank}")

        snippet = " | ".join(snippet_parts)

        url = f"https://www.coingecko.com/en/coins/{data.get('id', '')}"

        published_at = None
        if last_updated:
            with contextlib.suppress(ValueError, TypeError):
                published_at = datetime.fromisoformat(
                    last_updated.replace("T", " ").replace("Z", "+00:00")
                )

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )

    def _parse_price_to_result(
        self,
        price_data: dict[str, Any],
        coin_id: str,
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse price data to RawResult."""
        usd_price = price_data.get("usd", 0)
        eur_price = price_data.get("eur", 0)
        btc_price = price_data.get("btc", 0)
        market_cap = price_data.get("usd_market_cap", 0)
        price_change_24h = price_data.get("usd_24h_change", 0)
        last_updated = price_data.get("last_updated_at", 0)

        title = f"Price: {coin_id.upper()} - ${usd_price:,.2f}"

        snippet_parts = []
        if usd_price:
            snippet_parts.append(f"USD: ${usd_price:,.2f}")
        if eur_price:
            snippet_parts.append(f"EUR: €{eur_price:,.2f}")
        if btc_price:
            snippet_parts.append(f"BTC: {btc_price:,.8f}")
        if price_change_24h is not None:
            sign = "+" if price_change_24h >= 0 else ""
            snippet_parts.append(f"24h: {sign}{price_change_24h:.2f}%")
        if market_cap:
            snippet_parts.append(f"Cap: ${market_cap:,.0f}")

        snippet = " | ".join(snippet_parts)

        url = f"https://www.coingecko.com/en/coins/{coin_id}"

        published_at = None
        if last_updated:
            with contextlib.suppress(ValueError, TypeError):
                published_at = datetime.fromtimestamp(last_updated)

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )

    def _parse_trending_coin_to_result(
        self,
        coin: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse trending coin to RawResult."""
        item = coin.get("item", {})
        name = item.get("name", "")
        symbol = item.get("symbol", "")
        price_btc = coin.get("price_btc", 0)
        score = coin.get("score", 0)

        title = f"Trending: {name} ({symbol}) - Score: {score}"
        snippet = f"Price: {price_btc} BTC" if price_btc else ""

        url = f"https://www.coingecko.com/en/coins/{item.get('id', '')}"

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=None,
        )

    def _parse_coin_list_item_to_result(
        self,
        coin: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse coin list item to RawResult."""
        name = coin.get("name", "")
        symbol = coin.get("symbol", "")
        coin_id = coin.get("id", "")

        title = f"Coin: {name} ({symbol})"
        snippet = f"ID: {coin_id}"

        url = f"https://www.coingecko.com/en/coins/{coin_id}"

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=None,
        )
