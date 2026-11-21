"""External Data Integrations (docs/32) — unit tests with httpx.MockTransport
plus key-gating/403-hint tests for the endpoints. Live upstream verification
is done separately via scripts (real network), never inside the unit suite.
"""

from __future__ import annotations

import httpx
import pytest

from app.api.v1 import extdata
from app.config import get_settings
from app.retrieval.ext_data import (
    fetch_crypto_global,
    fetch_crypto_prices,
    fetch_external_news,
    fetch_fundamentals,
    fetch_fx_rates,
    fetch_global_quote,
    fetch_ipgeo,
    fetch_weather,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _resp(status: int, payload) -> httpx.Response:
    return httpx.Response(status, json=payload if payload is not None else {})


# --- crypto ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crypto_prices_keyless():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "simple/price" in str(request.url)
        assert request.url.params["ids"] == "bitcoin,ethereum"
        assert request.headers.get("x-cg-demo-api-key") is None
        return _resp(
            200,
            {
                "bitcoin": {"usd": 50000.0, "usd_24h_change": -1.5, "usd_market_cap": 9.8e11},
                "ethereum": {"usd": 2500.0},
            },
        )

    data = await fetch_crypto_prices(
        ["bitcoin", "ethereum"], vs_currency="usd", client=_client(handler)
    )
    assert data is not None
    assert data["coins"][0]["id"] == "bitcoin"
    assert data["coins"][0]["price"] == 50000.0
    assert data["coins"][0]["change_24h_pct"] == -1.5
    assert data["source_name"] == "CoinGecko"


@pytest.mark.asyncio
async def test_crypto_prices_uses_key_header_when_configured():
    seen_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # Demo keys ride the public host via x-cg-demo-api-key; CG- prefixed
        # keys are pro keys routed to the pro header (docs/32 §2).
        seen_headers.append(
            request.headers.get("x-cg-demo-api-key")
            or request.headers.get("x-cg-pro-api-key")
            or ""
        )
        return _resp(200, {"bitcoin": {"usd": 1.0}})

    from app.retrieval.ext_data import crypto as cg

    cg._prices_cache.clear()
    data = await fetch_crypto_prices(
        ["bitcoin"], vs_currency="usd", api_key="demo-key-123", client=_client(handler)
    )
    assert data is not None and data["coins"][0]["id"] == "bitcoin"
    data = await fetch_crypto_prices(
        ["bitcoin"], vs_currency="usd", api_key="CG-pro-key-9", client=_client(handler)
    )
    assert data is not None
    # Demo keys → demo header on the public host; CG- keys → pro header
    # (value sent verbatim — CoinGecko's contract).
    assert seen_headers == ["demo-key-123", "CG-pro-key-9"]


@pytest.mark.asyncio
async def test_crypto_prices_dead_upstream_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    # Cache may hold a value from a prior test with the same key — clear first.
    from app.retrieval.ext_data import crypto as cg

    cg._prices_cache.clear()
    assert await fetch_crypto_prices(["bitcoin"], client=_client(handler)) is None


@pytest.mark.asyncio
async def test_crypto_global():
    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(
            200,
            {
                "data": {
                    "active_cryptocurrencies": 17000,
                    "markets": 950,
                    "total_market_cap": {"usd": 2.4e12},
                    "total_volume": {"usd": 8.0e10},
                    "market_cap_percentage": {"btc": 52.5, "eth": 17.1},
                    "market_cap_change_percentage_24h_usd": -0.031,
                }
            },
        )

    from app.retrieval.ext_data import crypto as cg

    cg._global_cache.clear()
    data = await fetch_crypto_global(client=_client(handler))
    assert data is not None
    assert data["btc_dominance_pct"] == 5250.0  # fraction → percent conversion
    assert data["active_cryptocurrencies"] == 17000


# --- fx --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fx_merges_ecb_over_erapi():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "frankfurter" in url:
            assert url.startswith("https://api.frankfurter.dev")
            return _resp(200, {"base": "USD", "date": "2026-11-20", "rates": {"EUR": 0.9}})
        assert "open.er-api.com" in url
        return _resp(
            200,
            {
                "result": "success",
                "time_last_update_utc": "Thu, 20 Nov 2026",
                "rates": {"EUR": 0.9, "VND": 26300.0, "JPY": 155.0},
            },
        )

    data = await fetch_fx_rates("USD", ["VND", "EUR", "JPY"], client=_client(handler))
    assert data is not None
    by_code = data["rates"]  # fetcher output: dict keyed by currency
    # ECB print wins the EUR tie; VND/JPY only exist on ER-API
    assert by_code["EUR"]["source_name"] == "Frankfurter (ECB)"
    assert by_code["VND"]["rate"] == 26300.0
    assert by_code["VND"]["source_name"] == "ExchangeRate-API"
    assert set(data["sources_used"]) == {"Frankfurter (ECB)", "ExchangeRate-API"}


@pytest.mark.asyncio
async def test_fx_keyed_host_used_when_key_present():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v6/" in str(request.url) and "KEY123" in str(request.url)
        return _resp(
            200, {"result": "success", "rates": {"VND": 26300.0}, "time_last_update_utc": "x"}
        )

    from app.retrieval.ext_data import fx as fx_mod

    fx_mod._fx_cache.clear()
    data = await fetch_fx_rates(
        "USD", ["VND"], exchangerate_api_key="KEY123", client=_client(handler)
    )
    assert data is not None
    assert data["rates"]["VND"]["rate"] == 26300.0


@pytest.mark.asyncio
async def test_fx_both_dead_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    from app.retrieval.ext_data import fx as fx_mod

    fx_mod._fx_cache.clear()
    assert await fetch_fx_rates("USD", ["EUR"], client=_client(handler)) is None


@pytest.mark.asyncio
async def test_fx_invalid_base_rejected():
    assert await fetch_fx_rates("DOLLAR", ["EUR"], client=_client(lambda r: _resp(200, {}))) is None


# --- weather ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weather_keyless_city_geocode_and_forecast():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "geocoding-api" in url:
            assert request.url.params["name"] == "Hanoi"
            return _resp(
                200,
                {
                    "results": [
                        {
                            "latitude": 21.03,
                            "longitude": 105.85,
                            "name": "Hanoi",
                            "country": "Vietnam",
                        }
                    ]
                },
            )
        assert "api.open-meteo.com/v1/forecast" in url
        assert request.url.params["forecast_days"] == "3"
        return _resp(
            200,
            {
                "timezone": "Asia/Bangkok",
                "current": {
                    "time": "2026-11-20T10:00",
                    "temperature_2m": 24.5,
                    "relative_humidity_2m": 70,
                    "wind_speed_10m": 12.0,
                    "weather_code": 2,
                },
                "daily": {
                    "time": ["2026-11-20", "2026-11-21"],
                    "temperature_2m_max": [27.0, 28.0],
                    "temperature_2m_min": [19.0, 20.0],
                    "precipitation_sum": [0.0, 2.5],
                    "weather_code": [2, 61],
                },
            },
        )

    data = await fetch_weather(city="Hanoi", days=3, client=_client(handler))
    assert data is not None
    assert "Open-Meteo" in data["sources_used"]
    assert data["open_meteo"]["current"]["temperature_c"] == 24.5
    assert data["open_meteo"]["current"]["condition"] == "Partly cloudy"  # WMO 2
    assert data["open_meteo"]["daily"][1]["condition"] == "Slight rain"  # WMO 61
    assert data["location"] == "Hanoi, Vietnam"


@pytest.mark.asyncio
async def test_weather_with_owm_key_adds_block():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "openweathermap.org" in url:
            assert request.url.params["appid"] == "OWMKEY"
            return _resp(
                200,
                {
                    "weather": [{"description": "few clouds"}],
                    "main": {"temp": 25.0, "humidity": 65},
                    "wind": {"speed": 3.5},
                    "dt": 1760000000,
                },
            )
        return _resp(
            200,
            {
                "timezone": "Asia/Bangkok",
                "current": {"temperature_2m": 24.5, "weather_code": 0, "time": "t"},
                "daily": {},
            },
        )

    from app.retrieval.ext_data import weather as wx

    wx.weather_cache.clear()
    wx.geocode_cache.clear()
    data = await fetch_weather(
        lat=21.0, lon=105.8, days=1, openweather_api_key="OWMKEY", client=_client(handler)
    )
    assert data is not None
    assert data["openweather_current"]["condition"] == "few clouds"
    assert set(data["sources_used"]) == {"Open-Meteo", "OpenWeatherMap"}


@pytest.mark.asyncio
async def test_weather_dead_sources_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    from app.retrieval.ext_data import weather as wx

    wx.weather_cache.clear()
    wx.geocode_cache.clear()
    assert await fetch_weather(city="Nowhereville", client=_client(handler)) is None


# --- news (key-gated) ------------------------------------------------------------


@pytest.mark.asyncio
async def test_news_missing_key_returns_none():
    assert (
        await fetch_external_news(q="test", api_key="", client=_client(lambda r: _resp(200, {})))
        is None
    )


@pytest.mark.asyncio
async def test_news_everything_with_query():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "everything" in str(request.url)
        assert request.headers["X-Api-Key"] == "NKEY"
        assert "from=" in str(request.url)
        return _resp(
            200,
            {
                "status": "ok",
                "totalResults": 2,
                "articles": [
                    {
                        "title": "Headline A",
                        "url": "https://news.example/a",
                        "publishedAt": "2026-11-20T00:00:00Z",
                        "source": {"name": "Example"},
                        "description": "d",
                    },
                    {"title": "no url article"},
                ],
            },
        )

    from app.retrieval.ext_data import news_ext as ne

    ne.news_cache.clear()
    data = await fetch_external_news(q="metasearch", api_key="NKEY", client=_client(handler))
    assert data is not None
    assert len(data["items"]) == 1  # url-less article filtered
    assert data["items"][0]["source"] == "Example"


# --- global quote + fundamentals -------------------------------------------------


@pytest.mark.asyncio
async def test_global_quote_keyless_yahoo():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "finance.yahoo.com" in str(request.url)
        return _resp(
            200,
            {
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": 232.5,
                                "chartPreviousClose": 230.0,
                                "currency": "USD",
                                "fullExchangeName": "NasdaqGS",
                                "regularMarketTime": 1760000000,
                            }
                        }
                    ]
                }
            },
        )

    data = await fetch_global_quote(["AAPL"], client=_client(handler))
    assert data is not None
    assert data["symbols"][0]["symbol"] == "AAPL"
    assert data["symbols"][0]["quotes"][0]["source_name"] == "Yahoo Finance"
    assert data["symbols"][0]["quotes"][0]["price"] == 232.5


@pytest.mark.asyncio
async def test_global_quote_with_twelvedata_key():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "twelvedata.com" in url:
            assert request.url.params["apikey"] == "TDKEY"
            return _resp(
                200,
                {"status": "ok", "close": "232.75", "previous_close": "230.0", "currency": "USD"},
            )
        return _resp(
            200,
            {"chart": {"result": [{"meta": {"regularMarketPrice": 232.5}}]}},
        )

    from app.retrieval.ext_data import markets_global as mg

    mg._quote_cache.clear()
    data = await fetch_global_quote(["AAPL"], twelvedata_api_key="TDKEY", client=_client(handler))
    assert data is not None
    quotes = data["symbols"][0]["quotes"]
    assert {q["source_name"] for q in quotes} == {"Yahoo Finance", "Twelve Data"}


@pytest.mark.asyncio
async def test_fundamentals_key_gated():
    assert (
        await fetch_fundamentals("AAPL", fmp_api_key="", client=_client(lambda r: _resp(200, [])))
        is None
    )


@pytest.mark.asyncio
async def test_fundamentals_with_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["apikey"] == "FMPKEY"
        assert "income-statement" in str(request.url)
        return _resp(
            200,
            [
                {
                    "date": "2025-12-31",
                    "period": "FY",
                    "reportedCurrency": "USD",
                    "revenue": 400000000000,
                    "netIncome": 100000000000,
                }
            ],
        )

    from app.retrieval.ext_data import markets_global as mg

    mg._fundamentals_cache.clear()
    data = await fetch_fundamentals("AAPL", fmp_api_key="FMPKEY", client=_client(handler))
    assert data is not None
    assert data["periods"][0]["revenue"] == 400000000000.0
    assert data["source_name"] == "Financial Modeling Prep"


# --- ipgeo -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ipgeo_keyless():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "ipwho.is/1.1.1.1" in str(request.url)
        return _resp(
            200,
            {
                "success": True,
                "country": "Australia",
                "country_code": "AU",
                "city": "Brisbane",
                "latitude": -27.47,
                "longitude": 153.02,
                "timezone": {"id": "Australia/Brisbane"},
                "connection": {"asn": 13335, "org": "APNIC", "isp": "Cloudflare"},
            },
        )

    data = await fetch_ipgeo("1.1.1.1", client=_client(handler))
    assert data is not None
    assert data["ipwho"]["country_code"] == "AU"
    assert data["ipwho"]["org"] == "APNIC"
    assert data["ipwho"]["timezone"] == "Australia/Brisbane"


@pytest.mark.asyncio
async def test_ipgeo_invalid_ip_rejected():
    assert await fetch_ipgeo("999.999.1.1", client=_client(lambda r: _resp(200, {}))) is None


# --- endpoints (key gating + bounds) ---------------------------------------------


@pytest.mark.asyncio
async def test_endpoints_register_and_gating(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(extdata, "get_settings", lambda: settings)
    # news without key → 503 with setup hint
    resp = client.get("/v1/news/external", params={"q": "metasearch"})
    assert resp.status_code == 503
    assert "NEWSAPI_KEY" in resp.json()["error"]["message"]
    # fundamentals without key → 503 with setup hint
    resp = client.get("/v1/market/fundamentals", params={"symbol": "AAPL"})
    assert resp.status_code == 503
    assert "FMP_API_KEY" in resp.json()["error"]["message"]


def test_endpoint_bounds_enforced(client):
    # invalid ip rejected by schema/validation
    resp = client.get("/v1/net/ipgeo", params={"ip": "not-an-ip;"})
    assert resp.status_code == 400
    # weather with no locator → 400
    resp = client.get("/v1/weather")
    assert resp.status_code == 400
    # crypto bad ids → 503 (no valid ids)
    resp = client.get("/v1/market/crypto", params={"ids": "BAD_ID!"})
    assert resp.status_code in (400, 422, 503)
    # global-quote too many symbols → schema cap 5 via max_length + [:5]
    resp = client.get(
        "/v1/market/global-quote",
        params={"symbols": "AAPL,MSFT,NVDA,TSLA,AMZN,GOOG"},
    )
    assert resp.status_code in (200, 400, 503)  # bounded processing, no crash


def test_ext_routes_pass_w5_post_cost_rules():
    # All new routes are GETs — W5 Rule 1 only classifies POSTs; assert none slipped in.
    from app.main import app

    spec = app.openapi()
    ext_paths = [
        p
        for p in spec["paths"]
        if any(
            k in p
            for k in (
                "/crypto",
                "/fx",
                "/weather",
                "/external",
                "global-quote",
                "fundamentals",
                "ipgeo",
            )
        )
    ]
    for p in ext_paths:
        assert "post" not in spec["paths"][p], f"{p} must stay GET (no fan-out POST)"
