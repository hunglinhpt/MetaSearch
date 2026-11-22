"""Real search() behavior tests for the public-API adapters (docs/32 §6).

Every network touch goes through httpx.MockTransport — no live calls. The
adapters that fetch via the shared pool are tested by monkeypatching the
module-level ``get_http_client`` with a fresh MockTransport-backed client per
call (the idiom from tests/test_expansion_and_digests.py).
"""

from __future__ import annotations

import httpx
import pytest

from app.retrieval.adapters.alpha_vantage import AlphaVantageAdapter
from app.retrieval.adapters.coingecko import CoinGeckoAdapter
from app.retrieval.adapters.newsapi import NewsAPIAdapter, NewsAPITopHeadlinesAdapter
from app.retrieval.adapters.openweather import OpenWeatherMapAdapter


def _handler_client(handler):
    """Fresh client per call — adapters using ``async with get_http_client()``
    close the client on exit, so a shared instance would break multi-fetch flows."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def patched_pool(monkeypatch):
    """Patch ``get_http_client`` inside the given adapter module so every call
    gets a fresh MockTransport client."""

    def _patch(module_name: str, handler) -> None:
        module = __import__(module_name, fromlist=["get_http_client"])
        monkeypatch.setattr(module, "get_http_client", lambda *a, **kw: _handler_client(handler))

    return _patch


# --- CoinGecko -------------------------------------------------------------------


class TestCoinGeckoAdapter:
    async def test_price_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "simple/price" in str(request.url)
            assert request.url.params["ids"] == "bitcoin"
            return httpx.Response(
                200,
                json={
                    "bitcoin": {
                        "usd": 50000.0,
                        "eur": 46000.0,
                        "btc": 0.85,
                        "usd_market_cap": 9.8e11,
                        "usd_24h_change": 1.75,
                        "last_updated_at": 1760000000,
                    }
                },
            )

        patched_pool("app.retrieval.adapters.coingecko", handler)
        adapter = CoinGeckoAdapter()
        results = await adapter.search("bitcoin price usd")
        assert len(results) == 1
        assert results[0].adapter == "coingecko"
        assert "Price: BITCOIN" in results[0].title
        assert "USD: $50,000.00" in results[0].snippet
        assert "24h: +1.75%" in results[0].snippet
        assert results[0].published_at is not None

    async def test_market_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "coins/markets" in str(request.url)
            assert request.url.params["ids"] == "ethereum"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "ethereum",
                        "name": "Ethereum",
                        "symbol": "eth",
                        "current_price": 2500.5,
                        "market_cap": 3.0e11,
                        "market_cap_rank": 2,
                        "total_volume": 1.5e10,
                        "price_change_percentage_24h": -2.4,
                        "last_updated": "2026-11-20T10:00:00Z",
                    }
                ],
            )

        patched_pool("app.retrieval.adapters.coingecko", handler)
        results = await CoinGeckoAdapter().search("ethereum")
        assert len(results) == 1
        assert "Market: Ethereum (ETH)" in results[0].title
        assert "24h: -2.40%" in results[0].snippet
        assert "Rank: #2" in results[0].snippet

    async def test_info_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/coins/bitcoin" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "id": "bitcoin",
                    "name": "Bitcoin",
                    "symbol": "btc",
                    "description": {"en": "Bitcoin is a decentralized digital currency."},
                    "categories": ["Cryptocurrency", "Meme"],
                    "market_cap_rank": 1,
                    "links": {"homepage": ["https://bitcoin.org", ""]},
                },
            )

        patched_pool("app.retrieval.adapters.coingecko", handler)
        results = await CoinGeckoAdapter().search("bitcoin info")
        assert len(results) == 1
        assert results[0].title == "Coin: Bitcoin (BTC)"
        assert results[0].url == "https://bitcoin.org"
        assert "Categories: Cryptocurrency, Meme" in results[0].snippet

    async def test_trending_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "search/trending" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "coins": [
                        {"item": {"id": "pepe", "name": "Pepe", "symbol": "pepe"}, "score": 1},
                        {"item": {"id": "dogwifcoin", "name": "Wif", "symbol": "wif"}, "score": 2},
                    ]
                },
            )

        patched_pool("app.retrieval.adapters.coingecko", handler)
        results = await CoinGeckoAdapter().search("trending coins")
        assert len(results) == 2
        assert results[0].title.startswith("Trending: Pepe (pepe)")

    async def test_list_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "coins/list" in str(request.url)
            return httpx.Response(
                200,
                json=[
                    {"id": "bitcoin", "name": "Bitcoin", "symbol": "btc"},
                    {"id": "ethereum", "name": "Ethereum", "symbol": "eth"},
                ],
            )

        patched_pool("app.retrieval.adapters.coingecko", handler)
        results = await CoinGeckoAdapter().search("list all coins", max_results=1)
        assert len(results) == 1
        assert results[0].title == "Coin: Bitcoin (btc)"
        assert results[0].snippet == "ID: bitcoin"

    async def test_upstream_error_status_yields_no_results(self, patched_pool):
        patched_pool(
            "app.retrieval.adapters.coingecko",
            lambda request: httpx.Response(429, text="rate limited"),
        )
        assert await CoinGeckoAdapter().search("bitcoin") == []

    async def test_transport_error_degrades_to_empty(self, patched_pool):
        # Each fetch wraps its own request in try/except Exception -> None, so
        # transport failures degrade to empty results instead of raising.
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        patched_pool("app.retrieval.adapters.coingecko", handler)
        assert await CoinGeckoAdapter().search("bitcoin") == []


# --- Alpha Vantage ---------------------------------------------------------------


class TestAlphaVantageAdapter:
    async def test_missing_api_key_returns_empty(self, patched_pool):
        patched_pool(
            "app.retrieval.adapters.alpha_vantage",
            lambda request: pytest.fail("network call must not happen without a key"),
        )
        assert await AlphaVantageAdapter(api_key="").search("AAPL") == []

    async def test_quote_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "function=GLOBAL_QUOTE" in str(request.url)
            assert "symbol=AAPL" in str(request.url)
            assert "apikey=AVKEY" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "Global Quote": {
                        "01. symbol": "AAPL",
                        "05. price": "232.50",
                        "09. change": "1.25",
                        "10. change percent": "+0.54%",
                        "06. volume": "51000000",
                        "07. latest trading day": "2026-11-20",
                    }
                },
            )

        patched_pool("app.retrieval.adapters.alpha_vantage", handler)
        results = await AlphaVantageAdapter(api_key="AVKEY").search("AAPL stock quote")
        assert len(results) == 1
        assert results[0].title == "Stock: AAPL - $232.50"
        assert "Change: +1.25 (+0.54%)" in results[0].snippet
        assert results[0].published_at is not None

    async def test_overview_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "function=OVERVIEW" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "Symbol": "MSFT",
                    "Name": "Microsoft Corporation",
                    "Sector": "Technology",
                    "Industry": "Software",
                    "Exchange": "NASDAQ",
                    "MarketCapitalization": "3100000000000",
                    "FiscalYearEnding": "2026-06-30",
                },
            )

        patched_pool("app.retrieval.adapters.alpha_vantage", handler)
        results = await AlphaVantageAdapter(api_key="AVKEY").search("MSFT")
        assert len(results) == 1
        assert results[0].title == "Company: Microsoft Corporation (MSFT)"
        assert "Sector: Technology" in results[0].snippet
        assert results[0].published_at is not None

    async def test_earnings_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "function=EARNINGS" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "symbol": "NVDA",
                    "annualEarnings": [
                        {"fiscalDateEnding": "2025-12-31", "reportedEPS": "6.08"},
                        {"fiscalDateEnding": "2024-12-31", "reportedEPS": "5.20"},
                    ],
                },
            )

        patched_pool("app.retrieval.adapters.alpha_vantage", handler)
        results = await AlphaVantageAdapter(api_key="AVKEY").search("NVDA earnings")
        assert len(results) == 1
        assert results[0].title == "Earnings: NVDA"
        assert "EPS: $6.08" in results[0].snippet

    async def test_news_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "function=NEWS_SENTIMENT" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "feed": [
                        {
                            "title": "Chip demand surges",
                            "url": "https://finance.example/nvda-1",
                            "summary": "Data center revenue tripled year over year.",
                            "time_published": "20261120T120000",
                        }
                    ]
                },
            )

        patched_pool("app.retrieval.adapters.alpha_vantage", handler)
        results = await AlphaVantageAdapter(api_key="AVKEY").search("NVDA news")
        assert len(results) == 1
        assert results[0].title == "News: Chip demand surges"
        assert results[0].url == "https://finance.example/nvda-1"
        assert results[0].published_at is not None

    async def test_rate_limit_note_returns_empty(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "Note": "Thank you for using Alpha Vantage! Please consider the premium "
                    "plans if you need more requests per day."
                },
            )

        patched_pool("app.retrieval.adapters.alpha_vantage", handler)
        assert await AlphaVantageAdapter(api_key="AVKEY").search("MSFT") == []

    async def test_unknown_company_returns_empty_without_call(self, patched_pool):
        patched_pool(
            "app.retrieval.adapters.alpha_vantage",
            lambda request: pytest.fail("no symbol extraction should mean no network call"),
        )
        assert await AlphaVantageAdapter(api_key="AVKEY").search("flibbertigibbet") == []


# --- OpenWeatherMap --------------------------------------------------------------


class TestOpenWeatherAdapter:
    async def test_missing_api_key_returns_empty(self, patched_pool):
        patched_pool(
            "app.retrieval.adapters.openweather",
            lambda request: pytest.fail("network call must not happen without a key"),
        )
        assert await OpenWeatherMapAdapter(api_key="").search("Hanoi") == []

    async def test_current_weather_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "data/2.5/weather" in str(request.url)
            assert request.url.params["q"] == "Hanoi"
            assert request.url.params["appid"] == "OWMKEY"
            return httpx.Response(
                200,
                json={
                    "name": "Hanoi",
                    "sys": {"country": "VN"},
                    "main": {"temp": 24.5, "feels_like": 26.0, "humidity": 70},
                    "weather": [{"description": "clear sky"}],
                    "wind": {"speed": 3.2},
                    "dt": 1760000000,
                },
            )

        patched_pool("app.retrieval.adapters.openweather", handler)
        results = await OpenWeatherMapAdapter(api_key="OWMKEY").search("Hanoi", lang="vi")
        assert len(results) == 1
        assert results[0].title == "Weather: Hanoi, VN"
        assert "Temp: 24.5°C" in results[0].snippet
        assert "Condition: clear sky" in results[0].snippet
        assert results[0].published_at is not None

    async def test_forecast_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "data/2.5/forecast" in str(request.url)
            return httpx.Response(
                200,
                json={
                    "list": [
                        {
                            "dt": 1760000000,
                            "main": {"temp": 25.0, "feels_like": 27.0, "humidity": 68},
                            "weather": [{"description": "scattered clouds"}],
                            "dt_txt": "2026-11-21 12:00:00",
                        },
                        {
                            "dt": 1760010800,
                            "main": {"temp": 21.0, "feels_like": 22.0, "humidity": 80},
                            "weather": [{"description": "light rain"}],
                            "dt_txt": "2026-11-21 15:00:00",
                        },
                    ]
                },
            )

        patched_pool("app.retrieval.adapters.openweather", handler)
        results = await OpenWeatherMapAdapter(api_key="OWMKEY").search("Hanoi forecast")
        assert len(results) == 2
        # Location is the raw query (no coords/ZIP present) — app behavior.
        assert results[0].title.startswith("Forecast: Hanoi forecast at 2026-11-21")
        assert "Condition: light rain" in results[1].snippet

    async def test_location_not_found_returns_empty(self, patched_pool):
        patched_pool(
            "app.retrieval.adapters.openweather",
            lambda request: httpx.Response(404, json={"message": "city not found"}),
        )
        assert await OpenWeatherMapAdapter(api_key="OWMKEY").search("Nowhereville") == []

    async def test_coordinate_query(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["q"] == "21.03,105.85"
            return httpx.Response(
                200,
                json={
                    "name": "Hanoi",
                    "main": {"temp": 24.0},
                    "weather": [{"description": "sunny"}],
                },
            )

        patched_pool("app.retrieval.adapters.openweather", handler)
        results = await OpenWeatherMapAdapter(api_key="OWMKEY").search("lat:21.03,lon:105.85")
        assert len(results) == 1


# --- NewsAPI ---------------------------------------------------------------------


class TestNewsAPIAdapter:
    async def test_missing_api_key_returns_empty(self, patched_pool):
        patched_pool(
            "app.retrieval.adapters.newsapi",
            lambda request: pytest.fail("network call must not happen without a key"),
        )
        assert await NewsAPIAdapter(api_key="").search("metasearch") == []

    async def test_everything_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "everything" in str(request.url)
            assert request.url.params["apiKey"] == "NKEY"
            assert request.url.params["sortBy"] == "publishedAt"
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "totalResults": 2,
                    "articles": [
                        {
                            "title": "MetaSearch releases v4",
                            "description": "A big release with briefings.",
                            "url": "https://news.example/ms4",
                            "publishedAt": "2026-11-20T10:00:00Z",
                            "source": {"name": "Example Daily"},
                            "author": "Jane Reporter",
                        },
                        {"title": "no url article", "url": ""},
                    ],
                },
            )

        patched_pool("app.retrieval.adapters.newsapi", handler)
        results = await NewsAPIAdapter(api_key="NKEY").search("metasearch", max_results=10)
        # url-less article is still parsed into a RawResult (parser does not filter)
        assert len(results) == 2
        assert results[0].title == "News: MetaSearch releases v4"
        assert results[0].url == "https://news.example/ms4"
        assert results[0].source_name == "Example Daily"
        assert "Author: Jane Reporter" in results[0].snippet
        assert results[0].published_at is not None

    async def test_error_status_payload_returns_empty(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "error", "message": "apiKeyInvalid"})

        patched_pool("app.retrieval.adapters.newsapi", handler)
        assert await NewsAPIAdapter(api_key="NKEY").search("metasearch") == []

    async def test_rate_limited_returns_empty(self, patched_pool):
        patched_pool(
            "app.retrieval.adapters.newsapi",
            lambda request: httpx.Response(429, text="rate limited"),
        )
        assert await NewsAPIAdapter(api_key="NKEY").search("metasearch") == []

    async def test_headlines_happy_path(self, patched_pool):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "top-headlines" in str(request.url)
            assert request.url.params["country"] == "vn"
            assert request.url.params["category"] == "business"
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "articles": [
                        {
                            "title": "VN-Index tăng điểm",
                            "description": "Thị trường phục hồi.",
                            "url": "https://news.example/vn-index",
                            "publishedAt": "2026-11-20T09:00:00Z",
                            "source": {"name": "CafeF"},
                        }
                    ],
                },
            )

        patched_pool("app.retrieval.adapters.newsapi", handler)
        results = await NewsAPITopHeadlinesAdapter(api_key="NKEY").search(
            "market", lang="vn", categories=["business"]
        )
        assert len(results) == 1
        assert results[0].title == "Headline: VN-Index tăng điểm"
        assert results[0].source_name == "CafeF"
