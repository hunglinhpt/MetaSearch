"""NewsAPI adapter - Global news aggregator.

Free news API with instant approval API keys.
Free tier: 100 requests per day.

Category: news, media, current events, research
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

# API documentation: https://newsapi.org/docs
NEWSAPI_BASE_URL = "https://newsapi.org/v2"

# Rate limit: 100 requests per day (free tier)


class NewsAPIAdapter:
    """Adapter for NewsAPI news aggregator."""

    id = "newsapi"

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
        """Search for news articles based on query.

        Supports:
        - Keyword search
        - Language filtering
        - Category filtering
        - Date filtering
        """
        results: list[RawResult] = []

        if not self.api_key:
            logger.warning("NewsAPI key not configured")
            return results

        try:
            # Build query parameters
            params = self._build_params(query, lang, categories, freshness_days, max_results)

            # Fetch articles
            articles = await self._fetch_articles(params, timeout_s)

            if articles:
                for idx, article in enumerate(articles[:max_results]):
                    result = self._parse_article_to_result(article, query, idx + 1)
                    results.append(result)

        except httpx.HTTPError as exc:
            logger.warning("NewsAPI HTTP error for query '%s': %s", query, exc)
            raise AdapterError(
                f"newsapi HTTP error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning("NewsAPI timeout for query '%s': %s", query, exc)
            raise AdapterError(
                f"newsapi timeout after {timeout_s}s",
                kind=Outcome.TIMEOUT,
            ) from exc
        except httpx.PoolTimeout as exc:
            logger.warning("NewsAPI pool timeout for query '%s': %s", query, exc)
            raise AdapterError(
                "newsapi pool timeout",
                kind=Outcome.LOCAL_OVERLOAD,
            ) from exc

        return results

    def _build_params(
        self,
        query: str,
        lang: str | None,
        categories: list[str] | None,
        freshness_days: int | None,
        max_results: int,
    ) -> dict[str, Any]:
        """Build query parameters for NewsAPI."""
        params: dict[str, Any] = {
            "q": query,
            "pageSize": min(max_results, 100),  # Max 100 per request
            "apiKey": self.api_key,
        }

        if lang:
            params["language"] = lang

        if categories:
            # NewsAPI uses category parameter (single value)
            # We'll use the first category or combine
            params["category"] = categories[0] if categories else None

        # Add date filter if freshness_days is specified
        if freshness_days is not None:
            from datetime import datetime, timedelta

            from_date = (datetime.utcnow() - timedelta(days=freshness_days)).strftime("%Y-%m-%d")
            params["from"] = from_date

        # Sort by published date (newest first)
        params["sortBy"] = "publishedAt"

        return params

    async def _fetch_articles(
        self, params: dict[str, Any], timeout_s: float
    ) -> list[dict[str, Any]]:
        """Fetch articles from NewsAPI."""
        url = f"{NEWSAPI_BASE_URL}/everything"

        async with get_http_client("scrape") as http_cli:
            try:
                headers = {"User-Agent": "MetaSearch/1.0"}
                resp = await http_cli.get(url, params=params, headers=headers, timeout=timeout_s)

                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        return data.get("articles", [])
                    else:
                        logger.debug("NewsAPI error: %s", data.get("message", "Unknown error"))
                        return []
                elif resp.status_code == 429:
                    # Rate limited
                    logger.warning("NewsAPI rate limit exceeded")
                    return []
                else:
                    logger.debug("NewsAPI HTTP %s: %s", resp.status_code, resp.text)
                    return []

            except Exception as exc:
                logger.debug("Error fetching from NewsAPI: %s", exc)
                return []

    def _parse_article_to_result(
        self,
        article: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse news article to RawResult."""
        title = article.get("title", "")
        description = article.get("description", "")
        url = article.get("url", "")
        published_at_str = article.get("publishedAt", "")
        source_name = article.get("source", {}).get("name", "Unknown")
        author = article.get("author", "")

        # Clean title
        title = f"News: {title}" if title else "News Article"

        # Build snippet
        snippet_parts = []
        if description:
            snippet_parts.append(description[:300])
        if source_name:
            snippet_parts.append(f"Source: {source_name}")
        if author:
            snippet_parts.append(f"Author: {author}")

        snippet = " | ".join(snippet_parts) if snippet_parts else ""

        # Parse published date
        published_at = None
        if published_at_str:
            with contextlib.suppress(ValueError):
                published_at = datetime.fromisoformat(published_at_str.replace("Z", "+00:00"))
            if published_at is None:
                with contextlib.suppress(ValueError):
                    published_at = datetime.strptime(published_at_str, "%Y-%m-%dT%H:%M:%SZ")

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
            source_name=source_name,
        )


class NewsAPITopHeadlinesAdapter:
    """Adapter for NewsAPI top headlines."""

    id = "newsapi_headlines"

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
        """Get top headlines."""
        results: list[RawResult] = []

        if not self.api_key:
            logger.warning("NewsAPI key not configured")
            return results

        try:
            # Build parameters for top headlines
            params: dict[str, Any] = {
                "pageSize": min(max_results, 100),
                "apiKey": self.api_key,
            }

            # Use category if specified
            if categories:
                params["category"] = categories[0] if categories else None

            # Use language if specified
            if lang:
                params["language"] = lang

            # Use country if language is a country code
            if lang and len(lang) == 2:
                params["country"] = lang

            # Fetch top headlines
            articles = await self._fetch_top_headlines(params, timeout_s)

            if articles:
                for idx, article in enumerate(articles[:max_results]):
                    result = self._parse_article_to_result(article, query, idx + 1)
                    results.append(result)

        except httpx.HTTPError as exc:
            logger.warning("NewsAPI headlines HTTP error: %s", exc)
            raise AdapterError(
                f"newsapi_headlines HTTP error: {type(exc).__name__}",
                kind=Outcome.UPSTREAM_ERROR,
            ) from exc
        except httpx.TimeoutException as exc:
            logger.warning("NewsAPI headlines timeout: %s", exc)
            raise AdapterError(
                f"newsapi_headlines timeout after {timeout_s}s",
                kind=Outcome.TIMEOUT,
            ) from exc

        return results

    async def _fetch_top_headlines(
        self, params: dict[str, Any], timeout_s: float
    ) -> list[dict[str, Any]]:
        """Fetch top headlines from NewsAPI."""
        url = f"{NEWSAPI_BASE_URL}/top-headlines"

        async with get_http_client("scrape") as http_cli:
            try:
                headers = {"User-Agent": "MetaSearch/1.0"}
                resp = await http_cli.get(url, params=params, headers=headers, timeout=timeout_s)

                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        return data.get("articles", [])
                    else:
                        msg = data.get("message", "Unknown error")
                        logger.debug("NewsAPI headlines error: %s", msg)
                        return []
                elif resp.status_code == 429:
                    logger.warning("NewsAPI headlines rate limit exceeded")
                    return []
                else:
                    logger.debug("NewsAPI headlines HTTP %s: %s", resp.status_code, resp.text)
                    return []

            except Exception as exc:
                logger.debug("Error fetching headlines: %s", exc)
                return []

    def _parse_article_to_result(
        self,
        article: dict[str, Any],
        query: str,
        rank: int,
    ) -> RawResult:
        """Parse news article to RawResult."""
        title = article.get("title", "")
        description = article.get("description", "")
        url = article.get("url", "")
        published_at_str = article.get("publishedAt", "")
        source_name = article.get("source", {}).get("name", "Unknown")

        title = f"Headline: {title}" if title else "Top Headline"

        snippet_parts = []
        if description:
            snippet_parts.append(description[:300])
        if source_name:
            snippet_parts.append(f"Source: {source_name}")

        snippet = " | ".join(snippet_parts) if snippet_parts else ""

        published_at = None
        if published_at_str:
            with contextlib.suppress(ValueError):
                published_at = datetime.fromisoformat(published_at_str.replace("Z", "+00:00"))
            if published_at is None:
                with contextlib.suppress(ValueError):
                    published_at = datetime.strptime(published_at_str, "%Y-%m-%dT%H:%M:%SZ")

        return RawResult(
            url=url,
            title=title,
            snippet=snippet,
            rank=rank,
            adapter=self.id,
            source_engines=[self.id],
            published_at=published_at,
            source_name=source_name,
        )
