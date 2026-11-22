"""Stage-2 public API adapters (docs Giai Đoạn 2) — full behaviour tests.

Raises coverage for the adapters previously only exercised by instantiation
tests in test_last30days_adapters.py (CI coverage floor dropped to 82% after
they landed): query routing, fetch calls, response parsing, error/limit
handling — all offline via httpx.MockTransport, following the
patched_pool pattern in test_last30days_search.py.
"""

from __future__ import annotations

import httpx
import pytest

from app.retrieval.adapters.alpha_vantage import AlphaVantageAdapter
from app.retrieval.adapters.coingecko import CoinGeckoAdapter
from app.retrieval.adapters.newsapi import NewsAPIAdapter, NewsAPITopHeadlinesAdapter
from app.retrieval.adapters.openweather import OpenWeatherMapAdapter


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def patch_pool(monkeypatch):
    """Patch get_http_client inside an adapter module with a fresh mock client."""

    def _patch(module_name: str, handler) -> None:
        module = __import__(module_name, fromlist=["get_http_client"])
        monkeypatch.setattr(module, "get_http_client", lambda *a, **kw: _mock(handler))

    return _patch


# =========================================================================
# Alpha Vantage
# =========================================================================


_OVERVIEW = {
    "Symbol": "AAPL",
    "Name": "Apple Inc",
    "Description": "Designs smartphones and personal computers.",
    "Sector": "Technology",
    "Industry": "Consumer Electronics",
    "Exchange": "NASDAQ",
    "MarketCapitalization": "3000000000000",
    "FiscalYearEnding": "2026-09-30",
}

_GLOBAL_QUOTE = {
    "Global Quote": {
        "01. symbol": "AAPL",
        "05. price": "150.2500",
        "06. volume": "81234500",
        "07. latest trading day": "2026-11-21",
        "09. change": "-0.4800",
        "10. change percent": "-0.3178%",
    }
}

_EARNINGS = {
    "symbol": "AAPL",
    "annualEarnings": [
        {"fiscalDateEnding": "2026-09-30", "reportedEPS": "2.34"},
        {"fiscalDateEnding": "2025-09-30", "reportedEPS": "2.02"},
    ],
}

_NEWS_FEED = {
    "feed": [
        {
            "title": "Apple ships new M6 chip",
            "url": "https://example.com/m6",
            "summary": "The M6 focuses on on-device inference.",
            "time_published": "20261122T101500",
        },
        {
            "title": "Apple services revenue grows",
            "url": "https://example.com/svc",
            "summary": "Services hit a new record.",
            "time_published": "2026-11-21T08:00:00Z",
        },
    ]
}


class TestAlphaVantage:
    async def test_overview_route_and_parse(self, patch_pool):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            assert "function=OVERVIEW" in str(request.url)
            assert "symbol=AAPL" in str(request.url)
            return httpx.Response(200, json=_OVERVIEW)

        patch_pool("app.retrieval.adapters.alpha_vantage", handler)
        results = await AlphaVantageAdapter(api_key="k").search(
            "Apple company overview", max_results=5
        )
        assert len(results) == 1
        r = results[0]
        assert r.title == "Company: Apple Inc (AAPL)"
        assert r.url.endswith("symbol=AAPL")
        assert "Sector: Technology" in r.snippet
        assert "Market Cap: 3000000000000" in r.snippet
        assert r.published_at is not None
        assert r.adapter == "alpha_vantage"

    async def test_quote_route_and_parse(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "function=GLOBAL_QUOTE" in str(request.url)
            return httpx.Response(200, json=_GLOBAL_QUOTE)

        patch_pool("app.retrieval.adapters.alpha_vantage", handler)
        results = await AlphaVantageAdapter(api_key="k").search("AAPL stock price", max_results=5)
        assert len(results) == 1
        r = results[0]
        assert r.title == "Stock: AAPL - $150.2500"
        assert "Change: -0.4800 (-0.3178%)" in r.snippet
        assert "Volume: 81234500" in r.snippet
        assert r.published_at is not None

    async def test_earnings_route_and_parse(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "function=EARNINGS" in str(request.url)
            return httpx.Response(200, json=_EARNINGS)

        patch_pool("app.retrieval.adapters.alpha_vantage", handler)
        results = await AlphaVantageAdapter(api_key="k").search(
            "AAPL earnings report", max_results=5
        )
        assert len(results) == 1
        r = results[0]
        assert r.title == "Earnings: AAPL"
        assert "Period: 2026-09-30" in r.snippet
        assert "EPS: $2.34" in r.snippet

    async def test_news_route_parses_two_formats(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "function=NEWS_SENTIMENT" in str(request.url)
            return httpx.Response(200, json=_NEWS_FEED)

        patch_pool("app.retrieval.adapters.alpha_vantage", handler)
        results = await AlphaVantageAdapter(api_key="k").search("AAPL news", max_results=5)
        assert len(results) == 2
        assert results[0].title == "News: Apple ships new M6 chip"
        assert results[0].published_at is not None  # YYYYmmddTHHMMSS format
        assert results[1].title == "News: Apple services revenue grows"
        assert results[1].published_at is not None  # ISO format

    async def test_missing_key_returns_empty(self):
        results = await AlphaVantageAdapter().search("Apple", max_results=5)
        assert results == []

    async def test_rate_limit_note_yields_empty(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"Note": "API rate limit reached"})

        patch_pool("app.retrieval.adapters.alpha_vantage", handler)
        results = await AlphaVantageAdapter(api_key="k").search("AAPL", max_results=5)
        assert results == []

    async def test_unmapped_query_returns_empty(self, patch_pool):
        # No ticker and no known company → symbol "" → graceful no-op.
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={})

        patch_pool("app.retrieval.adapters.alpha_vantage", handler)
        assert await AlphaVantageAdapter(api_key="k").search("random words", max_results=5) == []
        assert seen == []


# =========================================================================
# NewsAPI
# =========================================================================


_ARTICLES = {
    "status": "ok",
    "totalResults": 2,
    "articles": [
        {
            "source": {"id": "techcrunch", "name": "TechCrunch"},
            "author": "J. Doe",
            "title": "Open-source AI search heats up",
            "description": "Meta search engines gain traction.",
            "url": "https://techcrunch.example/ai-search",
            "publishedAt": "2026-11-22T09:30:00Z",
            "content": "Content body",
        },
        {
            "source": {"id": None, "name": "VnExpress"},
            "author": None,
            "title": "MetaSearch phát hành bản 4.4",
            "description": "Bản tin dịch vụ.",
            "url": "https://vnexpress.example/44",
            "publishedAt": "2026-11-22T02:00:00Z",
            "content": "Nội dung",
        },
    ],
}

_HEADLINES = {
    "status": "ok",
    "totalResults": 1,
    "articles": [
        {
            "source": {"id": "reuters", "name": "Reuters"},
            "title": "Markets rally on rate hopes",
            "description": "Global equities up.",
            "url": "https://reuters.example/rally",
            "publishedAt": "2026-11-22T07:00:00Z",
        }
    ],
}


class TestNewsAPI:
    async def test_everything_route_and_parse(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/v2/everything" in str(request.url)
            assert request.url.params["q"] == "AI market"
            assert request.url.params["apiKey"] == "k"
            return httpx.Response(200, json=_ARTICLES)

        patch_pool("app.retrieval.adapters.newsapi", handler)
        results = await NewsAPIAdapter(api_key="k").search("AI market", max_results=5)
        assert len(results) == 2
        assert results[0].title == "News: Open-source AI search heats up"
        assert results[0].url == "https://techcrunch.example/ai-search"
        assert results[0].published_at is not None
        assert "Source: TechCrunch" in results[0].snippet

    async def test_everything_passes_freshness_filter(self, patch_pool):
        seen_params = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.update(request.url.params)
            return httpx.Response(200, json=_ARTICLES)

        patch_pool("app.retrieval.adapters.newsapi", handler)
        await NewsAPIAdapter(api_key="k").search("AI market", freshness_days=3, max_results=5)
        assert "from" in seen_params  # date window forwarded to upstream

    async def test_headlines_route_and_parse(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/v2/top-headlines" in str(request.url)
            return httpx.Response(200, json=_HEADLINES)

        patch_pool("app.retrieval.adapters.newsapi", handler)
        results = await NewsAPITopHeadlinesAdapter(api_key="k").search(
            "business headlines", max_results=5
        )
        assert len(results) == 1
        assert results[0].title == "Headline: Markets rally on rate hopes"
        assert results[0].published_at is not None

    async def test_missing_key_returns_empty(self):
        assert await NewsAPIAdapter().search("AI", max_results=5) == []
        assert await NewsAPITopHeadlinesAdapter().search("AI", max_results=5) == []

    async def test_upstream_error_payload_returns_empty(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "error", "message": "apiKeyInvalid"})

        patch_pool("app.retrieval.adapters.newsapi", handler)
        assert await NewsAPIAdapter(api_key="k").search("AI", max_results=5) == []

    async def test_rate_limited_429_returns_empty(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"message": "rateLimited"})

        patch_pool("app.retrieval.adapters.newsapi", handler)
        assert await NewsAPIAdapter(api_key="k").search("AI", max_results=5) == []


# =========================================================================
# OpenWeatherMap
# =========================================================================


_CURRENT = {
    "name": "Hanoi",
    "sys": {"country": "VN"},
    "main": {"temp": 24.5, "feels_like": 26.1, "humidity": 78},
    "weather": [{"description": "scattered clouds", "icon": "03d"}],
    "wind": {"speed": 3.5, "deg": 120},
    "visibility": 10000,
    "dt": 1763802000,
}

_FORECAST = {
    "city": {"name": "Hanoi", "country": "VN"},
    "list": [
        {
            "dt": 1763802000,
            "dt_txt": "2026-11-22 09:00:00",
            "main": {"temp": 23.0, "feels_like": 24.0, "humidity": 80},
            "weather": [{"description": "clear sky"}],
            "wind": {"speed": 2.0},
        },
        {
            "dt": 1763812800,
            "dt_txt": "2026-11-22 12:00:00",
            "main": {"temp": 28.0, "feels_like": 29.5, "humidity": 65},
            "weather": [{"description": "few clouds"}],
            "wind": {"speed": 3.0},
        },
    ],
}


class TestOpenWeatherMap:
    async def test_current_route_and_parse(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/data/2.5/weather" in str(request.url)
            assert request.url.params["q"] == "Hanoi"
            assert request.url.params["appid"] == "k"
            return httpx.Response(200, json=_CURRENT)

        patch_pool("app.retrieval.adapters.openweather", handler)
        results = await OpenWeatherMapAdapter(api_key="k").search("Hanoi", max_results=5)
        assert len(results) == 1
        r = results[0]
        assert r.title == "Weather: Hanoi, VN"
        assert "Temp: 24.5°C" in r.snippet
        assert "Humidity: 78%" in r.snippet
        assert r.published_at is not None
        assert r.url.startswith("https://openweathermap.org/city/")

    async def test_forecast_route_and_parse(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/data/2.5/forecast" in str(request.url)
            return httpx.Response(200, json=_FORECAST)

        patch_pool("app.retrieval.adapters.openweather", handler)
        results = await OpenWeatherMapAdapter(api_key="k").search("Hanoi forecast", max_results=5)
        assert len(results) == 2
        # The adapter keeps the full query as the location label.
        assert results[0].title.startswith("Forecast: Hanoi forecast at ")
        assert "Condition: clear sky" in results[0].snippet
        assert results[1].published_at is not None

    async def test_missing_key_returns_empty(self):
        assert await OpenWeatherMapAdapter().search("Hanoi", max_results=5) == []

    async def test_api_error_returns_empty(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "Invalid API key"})

        patch_pool("app.retrieval.adapters.openweather", handler)
        assert await OpenWeatherMapAdapter(api_key="k").search("Hanoi", max_results=5) == []


# =========================================================================
# CoinGecko (keyless-friendly)
# =========================================================================


_SIMPLE_PRICE = {
    "bitcoin": {
        "usd": 95000.5,
        "eur": 87000.25,
        "btc": 1.0,
        "usd_market_cap": 1_880_000_000_000,
        "usd_24h_vol": 55_000_000_000,
        "usd_24h_change": 2.34,
        "last_updated_at": 1763802000,
    }
}

_MARKETS = [
    {
        "id": "bitcoin",
        "name": "Bitcoin",
        "symbol": "btc",
        "current_price": 95000.5,
        "market_cap": 1_880_000_000_000,
        "market_cap_rank": 1,
        "price_change_percentage_24h": 2.34,
        "total_volume": 55_000_000_000,
        "last_updated": "2026-11-22 09:30:00",
    }
]

_TRENDING = {
    "coins": [
        {
            "item": {"id": "pepe", "name": "Pepe", "symbol": "PEPE"},
            "price_btc": 0.00000012,
            "score": 1,
        },
        {
            "item": {"id": "wif", "name": "dogwifhat", "symbol": "WIF"},
            "price_btc": 0.0000021,
            "score": 2,
        },
    ]
}


class TestCoinGecko:
    async def test_market_route_and_parse(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/coins/markets" in str(request.url)
            assert request.url.params["ids"] == "bitcoin"
            return httpx.Response(200, json=_MARKETS)

        patch_pool("app.retrieval.adapters.coingecko", handler)
        results = await CoinGeckoAdapter().search("bitcoin", max_results=5)
        assert len(results) == 1
        r = results[0]
        assert r.title == "Market: Bitcoin (BTC) - $95,000.50"
        assert "24h: +2.34%" in r.snippet
        assert "Market Cap: $1,880,000,000,000" in r.snippet
        assert r.url == "https://www.coingecko.com/en/coins/bitcoin"
        assert r.published_at is not None

    async def test_simple_price_route_and_parse(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/simple/price" in str(request.url)
            return httpx.Response(200, json=_SIMPLE_PRICE)

        patch_pool("app.retrieval.adapters.coingecko", handler)
        results = await CoinGeckoAdapter().search("price of bitcoin usd", max_results=5)
        assert len(results) == 1
        r = results[0]
        assert r.title == "Price: BITCOIN - $95,000.50"
        assert "USD: $95,000.50" in r.snippet
        assert "24h: +2.34%" in r.snippet
        assert r.published_at is not None

    async def test_trending_route_and_parse(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/search/trending" in str(request.url)
            return httpx.Response(200, json=_TRENDING)

        patch_pool("app.retrieval.adapters.coingecko", handler)
        results = await CoinGeckoAdapter().search("trending coins", max_results=5)
        assert len(results) == 2
        assert results[0].title == "Trending: Pepe (PEPE) - Score: 1"
        assert results[0].url == "https://www.coingecko.com/en/coins/pepe"

    async def test_coin_info_route_and_parse(self, patch_pool):
        coin_detail = {
            "id": "solana",
            "name": "Solana",
            "symbol": "sol",
            "description": {"en": "High performance L1 blockchain."},
            "categories": ["Layer 1 (L1)", "Smart Contract Platform"],
            "links": {"homepage": ["https://solana.com"]},
            "market_cap_rank": 5,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert "/coins/solana" in str(request.url)
            return httpx.Response(200, json=coin_detail)

        patch_pool("app.retrieval.adapters.coingecko", handler)
        results = await CoinGeckoAdapter().search("solana info details", max_results=5)
        assert len(results) == 1
        r = results[0]
        assert r.title == "Coin: Solana (SOL)"
        assert r.url == "https://solana.com"
        assert "Categories: Layer 1 (L1), Smart Contract Platform" in r.snippet
        assert "Rank: #5" in r.snippet

    async def test_demo_key_header_sent_when_configured(self, patch_pool):
        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return httpx.Response(200, json=_MARKETS)

        patch_pool("app.retrieval.adapters.coingecko", handler)
        await CoinGeckoAdapter(api_key="demo-abc").search("bitcoin", max_results=5)
        assert seen_headers.get("x-cg-demo-api-key") == "demo-abc"

    async def test_coin_list_route_bounds(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/coins/list" in str(request.url)
            return httpx.Response(
                200,
                json=[{"id": "bitcoin", "name": "Bitcoin", "symbol": "btc"}] * 30,
            )

        patch_pool("app.retrieval.adapters.coingecko", handler)
        results = await CoinGeckoAdapter().search("list all coins", max_results=5)
        assert len(results) == 5  # max_results bound respected

    async def test_rate_limited_returns_empty(self, patch_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"status": 429})

        patch_pool("app.retrieval.adapters.coingecko", handler)
        assert await CoinGeckoAdapter().search("bitcoin", max_results=5) == []
