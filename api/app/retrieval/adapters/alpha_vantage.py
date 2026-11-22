"""Alpha Vantage adapter - Free financial market data.

Free stock market data API with instant approval API keys.
Free tier: 5 requests per minute, 500 requests per day.

Category: finance, stocks, market, research
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

# API documentation: https://www.alpha-vantage.co/documentation/
ALPHA_VANTAGE_BASE_URL = "https://www.alpha-vantage.co/query"

# Rate limit: 5 requests per minute, 500 per day (free tier)
# Premium: 30 requests per minute, 1.2 million per year


class AlphaVantageAdapter:
    """Adapter for Alpha Vantage financial data."""

    id = "alpha_vantage"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or ""

    @property
    def requires_key(self) -> bool:
        return not self.api_key

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
        """Search for financial data based on query.

        Supports:
        - Stock symbols (e.g., "AAPL", "MSFT")
        - Company names (e.g., "Apple", "Microsoft")
        - Market data queries
        """
        results: list[RawResult] = []

        if not self.api_key:
            logger.warning("Alpha Vantage API key not configured")
            return results

        try:
            # Parse query to extract symbol and function
            symbol, function = self._parse_query(query)

            if not symbol:
                return results

            # Fetch data based on function
            if function == "overview":
                data = await self._fetch_company_overview(symbol, timeout_s)
                if data:
                    result = self._parse_overview_to_result(data, query, 1)
                    results.append(result)

            elif function == "quote":
                data = await self._fetch_quote(symbol, timeout_s)
                if data:
                    result = self._parse_quote_to_result(data, query, 1)
                    results.append(result)

            elif function == "earnings":
                data = await self._fetch_earnings(symbol, timeout_s)
                if data:
                    result = self._parse_earnings_to_result(data, query, 1)
                    results.append(result)

            elif function == "news":
                news_data = await self._fetch_news(symbol, max_results, timeout_s)
                if news_data:
                    for idx, news_item in enumerate(news_data[:max_results]):
                        result = self._parse_news_to_result(news_item, query, idx + 1)
                        results.append(result)

            else:
                # Default: fetch overview
                data = await self._fetch_company_overview(symbol, timeout_s)
                if data:
                    result = self._parse_overview_to_result(data, query, 1)
                    results.append(result)

        except httpx.HTTPError as exc:
            logger.warning("Alpha Vantage HTTP error for query '%s': %s", query, exc)
            raise AdapterError(
                f"alpha_vantage HTTP error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning("Alpha Vantage timeout for query '%s': %s", query, exc)
            raise AdapterError(
                f"alpha_vantage timeout after {timeout_s}s",
                kind=Outcome.TIMEOUT,
            ) from exc
        except httpx.PoolTimeout as exc:
            logger.warning("Alpha Vantage pool timeout for query '%s': %s", query, exc)
            raise AdapterError(
                "alpha_vantage pool timeout",
                kind=Outcome.LOCAL_OVERLOAD,
            ) from exc

        return results

    def _parse_query(self, query: str) -> tuple[str, str]:
        """Parse query to extract symbol and function."""
        query_lower = query.lower()

        # Default function
        function = "overview"

        # Check for specific functions
        if any(word in query_lower for word in ["news", "article", "headline"]):
            function = "news"
        elif any(word in query_lower for word in ["price", "quote", "stock", "market"]):
            function = "quote"
        elif any(word in query_lower for word in ["earnings", "financial", "income"]):
            function = "earnings"

        # Try to extract symbol (ticker)
        # Pattern: 1-5 uppercase letters, possibly with numbers
        import re

        symbol_pattern = r"\b([A-Z]{1,5}(?:\.[A-Z]{1,5})?)\b"
        match = re.search(symbol_pattern, query)
        symbol = match.group(1) if match else self._company_to_symbol(query)

        return symbol, function

    def _company_to_symbol(self, query: str) -> str:
        """Map company name to stock symbol (simplified)."""
        company_map = {
            "apple": "AAPL",
            "microsoft": "MSFT",
            "google": "GOOGL",
            "alphabet": "GOOGL",
            "amazon": "AMZN",
            "meta": "META",
            "facebook": "META",
            "tesla": "TSLA",
            "nvidia": "NVDA",
            "netflix": "NFLX",
            "twitter": "X",
        }
        query_lower = query.lower()
        for name, symbol in company_map.items():
            if name in query_lower:
                return symbol
        return ""

    async def _fetch_company_overview(self, symbol: str, timeout_s: float) -> dict[str, Any] | None:
        """Fetch company overview data."""
        url = f"{ALPHA_VANTAGE_BASE_URL}?function=OVERVIEW&symbol={symbol}&apikey={self.api_key}"

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, timeout=timeout_s)

                if resp.status_code == 200:
                    data = resp.json()
                    # Alpha Vantage reports failures in-band via Note /
                    # Information / Error keys (rate limit, demo key cap,
                    # invalid key) — never treat those as company data.
                    if any(k in data for k in ("Note", "Information", "Error")):
                        logger.debug(
                            "Alpha Vantage rate limit or error: %s",
                            next(
                                (
                                    str(data[k])
                                    for k in ("Note", "Information", "Error")
                                    if k in data
                                ),
                                "unknown",
                            ),
                        )
                        return None
                    return data
                else:
                    logger.debug("Alpha Vantage HTTP %s: %s", resp.status_code, resp.text)

            except Exception as exc:
                logger.debug("Error fetching company overview: %s", exc)

        return None

    async def _fetch_quote(self, symbol: str, timeout_s: float) -> dict[str, Any] | None:
        """Fetch real-time quote data."""
        url = (
            f"{ALPHA_VANTAGE_BASE_URL}?function=GLOBAL_QUOTE&symbol={symbol}&apikey={self.api_key}"
        )

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, timeout=timeout_s)

                if resp.status_code == 200:
                    data = resp.json()
                    if "Global Quote" in data:
                        return data["Global Quote"]
                    if "Note" in data:
                        logger.debug("Alpha Vantage quote error: %s", data.get("Note"))
                        return None
                    return data
                else:
                    logger.debug("Alpha Vantage HTTP %s: %s", resp.status_code, resp.text)

            except Exception as exc:
                logger.debug("Error fetching quote: %s", exc)

        return None

    async def _fetch_earnings(self, symbol: str, timeout_s: float) -> dict[str, Any] | None:
        """Fetch earnings data."""
        url = f"{ALPHA_VANTAGE_BASE_URL}?function=EARNINGS&symbol={symbol}&apikey={self.api_key}"

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, timeout=timeout_s)

                if resp.status_code == 200:
                    data = resp.json()
                    if "annualEarnings" in data or "quarterlyEarnings" in data:
                        return data
                    if "Note" in data:
                        logger.debug("Alpha Vantage earnings error: %s", data.get("Note"))
                        return None
                    return data
                else:
                    logger.debug("Alpha Vantage HTTP %s: %s", resp.status_code, resp.text)

            except Exception as exc:
                logger.debug("Error fetching earnings: %s", exc)

        return None

    async def _fetch_news(self, symbol: str, limit: int, timeout_s: float) -> list[dict[str, Any]]:
        """Fetch news articles for a symbol."""
        url = (
            f"{ALPHA_VANTAGE_BASE_URL}?function=NEWS_SENTIMENT"
            f"&symbol={symbol}&limit={min(limit, 100)}&apikey={self.api_key}"
        )

        async with get_http_client("scrape") as http_cli:
            try:
                resp = await http_cli.get(url, timeout=timeout_s)

                if resp.status_code == 200:
                    data = resp.json()
                    if "feed" in data:
                        return data["feed"][:limit]
                    if "Note" in data:
                        logger.debug("Alpha Vantage news error: %s", data.get("Note"))
                        return []
                    return []
                else:
                    logger.debug("Alpha Vantage HTTP %s: %s", resp.status_code, resp.text)

            except Exception as exc:
                logger.debug("Error fetching news: %s", exc)

        return []

    def _parse_overview_to_result(
        self,
        data: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse company overview to RawResult."""
        symbol = data.get("Symbol", "")
        name = data.get("Name", "")
        description = data.get("Description", "")
        sector = data.get("Sector", "")
        industry = data.get("Industry", "")
        exchange = data.get("Exchange", "")
        market_cap = data.get("MarketCapitalization", "")

        title = f"Company: {name} ({symbol})"

        snippet_parts = []
        if sector:
            snippet_parts.append(f"Sector: {sector}")
        if industry:
            snippet_parts.append(f"Industry: {industry}")
        if exchange:
            snippet_parts.append(f"Exchange: {exchange}")
        if market_cap:
            snippet_parts.append(f"Market Cap: {market_cap}")

        snippet = " | ".join(snippet_parts) if snippet_parts else description[:200]

        url = f"https://www.alpha-vantage.co/query?function=OVERVIEW&symbol={symbol}"

        published_at = None
        if "FiscalYearEnding" in data:
            with contextlib.suppress(ValueError):
                published_at = datetime.strptime(data["FiscalYearEnding"], "%Y-%m-%d")

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )

    def _parse_quote_to_result(
        self,
        data: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse quote data to RawResult."""
        symbol = data.get("01. symbol", "")
        price = data.get("05. price", "")
        change = data.get("09. change", "")
        change_percent = data.get("10. change percent", "")
        volume = data.get("06. volume", "")
        latest_trading_day = data.get("07. latest trading day", "")

        title = f"Stock: {symbol} - ${price}"

        snippet_parts = []
        if change:
            sign = "+" if str(change).startswith("+") or float(change) >= 0 else ""
            snippet_parts.append(f"Change: {sign}{change} ({change_percent})")
        if volume:
            snippet_parts.append(f"Volume: {volume}")
        if latest_trading_day:
            snippet_parts.append(f"Date: {latest_trading_day}")

        snippet = " | ".join(snippet_parts)

        url = f"https://www.alpha-vantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}"

        published_at = None
        if latest_trading_day:
            with contextlib.suppress(ValueError):
                published_at = datetime.strptime(latest_trading_day, "%Y-%m-%d")

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )

    def _parse_news_to_result(
        self,
        news_item: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse news item to RawResult."""
        title = news_item.get("title", "")
        url = news_item.get("url", "")
        summary = news_item.get("summary", "")
        time_published = news_item.get("time_published", "")

        title = f"News: {title}" if title else "Financial News"
        snippet = f"{summary[:200]}..." if summary else ""

        published_at = None
        if time_published:
            with contextlib.suppress(ValueError):
                published_at = datetime.fromisoformat(time_published.replace("Z", "+00:00"))
            if not published_at:
                with contextlib.suppress(ValueError):
                    published_at = datetime.strptime(time_published, "%Y%m%dT%H%M%S")

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )

    def _parse_earnings_to_result(
        self,
        data: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse earnings data to RawResult."""
        symbol = data.get("symbol", "")

        # Get latest annual earnings
        annual_earnings = data.get("annualEarnings", [])
        latest_annual = annual_earnings[0] if annual_earnings else {}

        fiscal_date = latest_annual.get("fiscalDateEnding", "")
        reported_eps = latest_annual.get("reportedEPS", "")

        title = f"Earnings: {symbol}"
        snippet_parts = []
        if fiscal_date:
            snippet_parts.append(f"Period: {fiscal_date}")
        if reported_eps:
            snippet_parts.append(f"EPS: ${reported_eps}")

        snippet = " | ".join(snippet_parts)

        url = f"https://www.alpha-vantage.co/query?function=EARNINGS&symbol={symbol}"

        published_at = None
        if fiscal_date:
            with contextlib.suppress(ValueError):
                published_at = datetime.strptime(fiscal_date, "%Y-%m-%d")

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
        )
