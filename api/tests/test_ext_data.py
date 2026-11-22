"""External Data Integrations (docs/32) — unit tests with httpx.MockTransport
plus key-gating/403-hint tests for the endpoints. Live upstream verification
is done separately via scripts (real network), never inside the unit suite.
"""

from __future__ import annotations

from types import SimpleNamespace

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
from app.retrieval.ext_data._base import TtlCache, ext_settings_keys, json_get


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
                    "market_cap_change_percentage_24h_usd": -3.1,
                }
            },
        )

    from app.retrieval.ext_data import crypto as cg

    cg._global_cache.clear()
    data = await fetch_crypto_global(client=_client(handler))
    assert data is not None
    # CoinGecko /global reports PERCENT values already — passed through as-is
    # (live smoke 2026-11-21: 57.76% BTC dominance, not a fraction).
    assert data["btc_dominance_pct"] == 52.5
    assert data["market_cap_change_24h_pct"] == -3.1
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


# --- shared base plumbing --------------------------------------------------------


def test_ttl_cache_expiry_and_eviction(monkeypatch):
    from app.retrieval.ext_data import _base

    now = {"t": 1000.0}
    monkeypatch.setattr(_base.time, "monotonic", lambda: now["t"])  # deterministic clock
    # beyond ttl -> get is a miss and the entry is dropped
    expiring = TtlCache(ttl_s=5.0, max_entries=4)
    expiring.put("a", 1)
    now["t"] += 10.0
    assert expiring.get("a") is None
    # bounded: inserting beyond max_entries evicts the oldest entry
    bounded = TtlCache(ttl_s=100.0, max_entries=2)
    bounded.put("a", 1)
    now["t"] += 1.0
    bounded.put("b", 2)
    now["t"] += 1.0
    bounded.put("c", 3)
    assert bounded.get("a") is None  # oldest evicted
    assert bounded.get("b") == 2
    assert bounded.get("c") == 3


def test_json_get_walks_defensively():
    assert json_get({"a": {"b": {"c": 5}}}, "a", "b", "c") == 5
    assert json_get({"a": 5}, "a", "b") is None  # non-dict hop
    assert json_get(None, "a") is None
    assert json_get({"a": None}, "a") is None


def test_ext_settings_keys_collects_registered_env_values():
    settings = SimpleNamespace(
        coingecko_api_key="CG-1",
        openweather_api_key="",
        newsapi_key="n",
        alpha_vantage_api_key="av",
        twelvedata_api_key="",
        fmp_api_key="fmp",
        ipinfo_token="ip",
        exchangerate_api_key="",
    )
    keys = ext_settings_keys(settings)
    assert keys["coingecko_api_key"] == "CG-1"
    assert keys["newsapi_key"] == "n"
    assert keys["openweather_api_key"] == ""
    # missing attributes degrade to empty strings
    assert ext_settings_keys(SimpleNamespace())["fmp_api_key"] == ""


# --- crypto sanitizers + edge cases ----------------------------------------------


def test_crypto_sanitize_coin_ids_and_vs_currency():
    from app.retrieval.ext_data import crypto as cg

    assert cg.sanitize_coin_ids(["Bitcoin", " BTC ", "BAD!", "", "x" * 100]) == [
        "bitcoin",
        "btc",
    ]  # "btc" is itself a valid CoinGecko slug
    # caps at 25 ids and dedupes
    ids = [f"coin-{i}" for i in range(30)] + ["coin-0"]
    assert len(cg.sanitize_coin_ids(ids)) == 25
    assert cg.sanitize_vs_currency("USD") == "usd"
    assert cg.sanitize_vs_currency("NO-T-A-CY") == "usd"  # >8 chars falls back to usd


def test_crypto_key_tag_deterministic():
    from app.retrieval.ext_data.crypto import _key_tag

    assert _key_tag("") == ""
    assert _key_tag("abc") == _key_tag("abc")
    assert _key_tag("abc") != _key_tag("xyz")


@pytest.mark.asyncio
async def test_crypto_prices_invalid_ids_returns_none():
    from app.retrieval.ext_data import crypto as cg

    cg._prices_cache.clear()
    assert await fetch_crypto_prices(["BAD_ID!"], client=_client(lambda r: _resp(200, {}))) is None


@pytest.mark.asyncio
async def test_crypto_prices_unknown_coin_returns_none():
    from app.retrieval.ext_data import crypto as cg

    cg._prices_cache.clear()
    # upstream answered but has no node for the requested id
    assert await fetch_crypto_prices(["darkcoin"], client=_client(lambda r: _resp(200, {}))) is None


@pytest.mark.asyncio
async def test_crypto_prices_cache_hit_flags():
    from app.retrieval.ext_data import crypto as cg

    cg._prices_cache.clear()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _resp(200, {"bitcoin": {"usd": 1.0}})

    first = await fetch_crypto_prices(["bitcoin"], client=_client(handler))
    assert first is not None and first["cached"] is False
    second = await fetch_crypto_prices(["bitcoin"], client=_client(handler))
    assert second is not None and second["cached"] is True
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_crypto_global_bad_payloads_return_none():
    from app.retrieval.ext_data import crypto as cg

    cg._global_cache.clear()
    assert await fetch_crypto_global(client=_client(lambda r: _resp(200, {"data": None}))) is None
    cg._global_cache.clear()
    assert await fetch_crypto_global(client=_client(lambda r: _resp(200, []))) is None


# --- fx sanitizers + edge cases --------------------------------------------------


def test_fx_sanitize_helpers():
    from app.retrieval.ext_data import fx as fx_mod

    assert fx_mod.sanitize_currency("usd") == "USD"
    assert fx_mod.sanitize_currency("DOLLAR") is None
    # 30 distinct 3-letter codes (letters only) + a duplicate -> capped at 20
    syms = [chr(65 + i // 26) + chr(65 + i % 26) + "X" for i in range(30)] + ["AAX"]
    assert len(fx_mod.sanitize_symbols(syms)) == 20  # capped + deduped


@pytest.mark.asyncio
async def test_fx_frankfurter_dead_er_alive():
    from app.retrieval.ext_data import fx as fx_mod

    fx_mod._fx_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if "frankfurter" in str(request.url):
            return httpx.Response(500, text="down")
        return _resp(200, {"result": "success", "rates": {"EUR": 0.9}})

    data = await fetch_fx_rates("USD", ["EUR"], client=_client(handler))
    assert data is not None
    assert data["sources_used"] == ["ExchangeRate-API"]
    assert data["rates"]["EUR"]["source_name"] == "ExchangeRate-API"


@pytest.mark.asyncio
async def test_fx_invalid_symbol_filtered_from_request():
    from app.retrieval.ext_data import fx as fx_mod

    fx_mod._fx_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if "frankfurter" in str(request.url):
            return _resp(200, {"base": "USD", "date": "2026-11-20", "rates": {"EUR": 0.9}})
        return _resp(200, {"result": "success", "rates": {"EUR": 0.9}})

    data = await fetch_fx_rates("USD", ["NOPE!", "EUR"], client=_client(handler))
    assert data is not None
    assert data["requested_symbols"] == ["EUR"]
    assert "NOPE" not in data["rates"]


# --- weather edge cases ----------------------------------------------------------


@pytest.mark.asyncio
async def test_weather_latlon_skips_geocoding():
    from app.retrieval.ext_data import weather as wx

    wx.weather_cache.clear()
    wx.geocode_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "geocoding-api" not in str(request.url)  # explicit coords skip geocode
        return _resp(
            200,
            {
                "timezone": "UTC",
                "current": {"temperature_2m": 20.0, "weather_code": 0, "time": "t"},
                "daily": {},
            },
        )

    data = await fetch_weather(lat=21.03456, lon=105.87654, client=_client(handler))
    assert data is not None
    assert data["location"] == {"lat": 21.0346, "lon": 105.8765}  # rounded to 4dp


@pytest.mark.asyncio
async def test_weather_invalid_inputs_rejected():
    assert (
        await fetch_weather(lat=999.0, lon=10.0, client=_client(lambda r: _resp(200, {}))) is None
    )
    assert await fetch_weather(city="<script>", client=_client(lambda r: _resp(200, {}))) is None


@pytest.mark.asyncio
async def test_weather_geocode_failure_returns_none():
    from app.retrieval.ext_data import weather as wx

    wx.weather_cache.clear()
    wx.geocode_cache.clear()
    assert (
        await fetch_weather(city="Nowhere", client=_client(lambda r: httpx.Response(500))) is None
    )


@pytest.mark.asyncio
async def test_weather_geocode_without_results_returns_none():
    from app.retrieval.ext_data import weather as wx

    wx.weather_cache.clear()
    wx.geocode_cache.clear()
    assert (
        await fetch_weather(city="Nowhere", client=_client(lambda r: _resp(200, {"results": []})))
        is None
    )


@pytest.mark.asyncio
async def test_weather_owm_dead_open_meteo_alive_still_succeeds():
    from app.retrieval.ext_data import weather as wx

    wx.weather_cache.clear()
    wx.geocode_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if "openweathermap.org" in str(request.url):
            return httpx.Response(503, text="down")
        return _resp(
            200,
            {
                "timezone": "UTC",
                "current": {"temperature_2m": 20.0, "weather_code": 45, "time": "t"},
                "daily": {},
            },
        )

    data = await fetch_weather(
        lat=1.0, lon=2.0, days=1, openweather_api_key="OWM", client=_client(handler)
    )
    assert data is not None
    assert data["sources_used"] == ["Open-Meteo"]
    assert data["open_meteo"]["current"]["condition"] == "Fog"  # WMO 45
    assert data["openweather_current"] is None


@pytest.mark.asyncio
async def test_weather_days_clamped_to_seven():
    from app.retrieval.ext_data import weather as wx

    wx.weather_cache.clear()
    wx.geocode_cache.clear()
    seen_days: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_days.append(request.url.params["forecast_days"])
        return _resp(200, {"timezone": "UTC", "current": {"temperature_2m": 1.0}, "daily": {}})

    await fetch_weather(lat=1.0, lon=2.0, days=99, client=_client(handler))
    assert seen_days == ["7"]


def test_weather_wmo_text_and_helpers():
    from app.retrieval.ext_data.weather import _at, _num, _wmo_text

    assert _wmo_text(0) == "Clear sky"
    assert _wmo_text("not-a-code") is None
    assert _at([1, 2], 5) is None  # out-of-range index
    assert _num("bad") is None
    assert _num(float("inf")) is None


# --- ipgeo edge cases ------------------------------------------------------------


@pytest.mark.asyncio
async def test_ipgeo_with_ipinfo_token_adds_block():
    from app.retrieval.ext_data import ipgeo as ig

    ig.ipgeo_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if "ipwho.is" in str(request.url):
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
                    "flag": {"emoji": "AU"},
                },
            )
        assert "ipinfo.io" in str(request.url)
        assert request.url.params["token"] == "TOK"
        return _resp(
            200,
            {
                "city": "Brisbane",
                "region": "Queensland",
                "country": "AU",
                "loc": "-27.47,153.02",
                "org": "AS13335 Cloudflare",
                "timezone": "Australia/Brisbane",
            },
        )

    data = await fetch_ipgeo("1.1.1.1", ipinfo_token="TOK", client=_client(handler))
    assert data is not None
    assert data["sources_used"] == ["ipwho.is", "IPinfo"]
    assert data["ipwho"]["flag_emoji"] == "AU"
    assert data["ipinfo"]["org"] == "AS13335 Cloudflare"


@pytest.mark.asyncio
async def test_ipgeo_ipwho_dead_but_ipinfo_alive():
    from app.retrieval.ext_data import ipgeo as ig

    ig.ipgeo_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if "ipwho.is" in str(request.url):
            return httpx.Response(503, text="down")
        return _resp(200, {"city": "Brisbane", "country": "AU"})

    data = await fetch_ipgeo("1.1.1.1", ipinfo_token="TOK", client=_client(handler))
    assert data is not None
    assert data["ipwho"] is None
    assert data["sources_used"] == ["IPinfo"]


@pytest.mark.asyncio
async def test_ipgeo_ipwho_failure_flag_counts_as_dead():
    from app.retrieval.ext_data import ipgeo as ig

    ig.ipgeo_cache.clear()
    # success:False payload (e.g. private range) -> treated as a dead source
    assert (
        await fetch_ipgeo(
            "192.168.0.1",
            client=_client(lambda r: _resp(200, {"success": False})),
        )
        is None
    )


@pytest.mark.asyncio
async def test_ipgeo_ipinfo_error_payload_counts_as_dead():
    from app.retrieval.ext_data import ipgeo as ig

    ig.ipgeo_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if "ipwho.is" in str(request.url):
            return httpx.Response(500)
        return _resp(200, {"error": {"title": "token invalid"}})

    assert await fetch_ipgeo("1.1.1.1", ipinfo_token="TOK", client=_client(handler)) is None


def test_ipgeo_sanitize_ipv6():
    from app.retrieval.ext_data.ipgeo import sanitize_ip

    assert sanitize_ip("2001:DB8::1") == "2001:db8::1"  # lowercased canonical form
    assert sanitize_ip(" 8.8.8.8 ") == "8.8.8.8"
    assert sanitize_ip("nope") is None


# --- news edge cases -------------------------------------------------------------


@pytest.mark.asyncio
async def test_news_top_headlines_defaults_to_us():
    from app.retrieval.ext_data import news_ext as ne

    ne.news_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "top-headlines" in str(request.url)
        assert request.url.params["country"] == "us"  # bounded default, never unbounded
        return _resp(200, {"status": "ok", "articles": []})

    # no articles -> None (nothing to serve)
    assert await fetch_external_news(api_key="NKEY", client=_client(handler)) is None


@pytest.mark.asyncio
async def test_news_invalid_country_and_category_dropped():
    from app.retrieval.ext_data import news_ext as ne

    ne.news_cache.clear()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _resp(200, {"status": "ok", "articles": []})

    await fetch_external_news(
        country="MOON", category="fashion", api_key="NKEY", client=_client(handler)
    )
    params = seen[0].url.params
    assert "country" not in params or params["country"] == "us"
    assert "category" not in params


@pytest.mark.asyncio
async def test_news_page_size_clamped():
    from app.retrieval.ext_data import news_ext as ne

    ne.news_cache.clear()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _resp(200, {"status": "ok", "articles": []})

    await fetch_external_news(q="x", page_size=999, api_key="NKEY", client=_client(handler))
    assert seen[0].url.params["pageSize"] == "50"


@pytest.mark.asyncio
async def test_news_upstream_failures_return_none():
    from app.retrieval.ext_data import news_ext as ne

    ne.news_cache.clear()
    assert (
        await fetch_external_news(q="x", api_key="N", client=_client(lambda r: httpx.Response(500)))
        is None
    )
    ne.news_cache.clear()
    assert (
        await fetch_external_news(
            q="x", api_key="N", client=_client(lambda r: _resp(200, {"status": "error"}))
        )
        is None
    )


@pytest.mark.asyncio
async def test_news_cache_hit_flag():
    from app.retrieval.ext_data import news_ext as ne

    ne.news_cache.clear()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _resp(200, {"status": "ok", "articles": [{"title": "T", "url": "https://a/x"}]})

    first = await fetch_external_news(q="x", api_key="N", client=_client(handler))
    assert first is not None and first["cached"] is False
    second = await fetch_external_news(q="x", api_key="N", client=_client(handler))
    assert second is not None and second["cached"] is True
    assert calls["n"] == 1


# --- markets_global edge cases ---------------------------------------------------


def test_sanitize_ticker_rules():
    from app.retrieval.ext_data.markets_global import sanitize_ticker

    assert sanitize_ticker(" aapl ") == "AAPL"
    assert sanitize_ticker("BRK-B") == "BRK-B"
    assert sanitize_ticker("") is None
    assert sanitize_ticker("A" * 16) is None  # over the 15-char bound
    assert sanitize_ticker("AAA!!!") is None


@pytest.mark.asyncio
async def test_global_quote_dedupes_and_converts_yahoo_dash():
    from app.retrieval.ext_data import markets_global as mg

    mg._quote_cache.clear()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _resp(200, {"chart": {"result": [{"meta": {"regularMarketPrice": 100.0}}]}})

    data = await fetch_global_quote([" aapl ", "BRK.B", "aapl", "!!!"], client=_client(handler))
    assert data is not None
    # deduped + invalid dropped; Yahoo ticker uses dashes (BRK.B -> BRK-B)
    assert [s["symbol"] for s in data["symbols"]] == ["AAPL", "BRK.B"]
    assert any("chart/BRK-B" in url for url in seen)


@pytest.mark.asyncio
async def test_global_quote_no_valid_symbols_returns_none():
    from app.retrieval.ext_data import markets_global as mg

    mg._quote_cache.clear()
    assert await fetch_global_quote(["!!!", ""], client=_client(lambda r: _resp(200, {}))) is None


@pytest.mark.asyncio
async def test_global_quote_alphavantage_block_and_td_failure():
    from app.retrieval.ext_data import markets_global as mg

    mg._quote_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "twelvedata.com" in url:
            return httpx.Response(500, text="down")  # dead source -> omitted
        if "alphavantage.co" in url:
            return _resp(
                200,
                {
                    "Global Quote": {
                        "05. price": "10.5",
                        "09. change": "0.5",
                        "10. change percent": "5.00%",
                        "06. volume": "1000",
                        "07. latest trading day": "2026-11-20",
                    }
                },
            )
        return _resp(200, {"chart": {"result": [{"meta": {"regularMarketPrice": 100.0}}]}})

    data = await fetch_global_quote(
        ["AAPL"],
        alpha_vantage_api_key="AV",
        twelvedata_api_key="TD",
        client=_client(handler),
    )
    assert data is not None
    quotes = data["symbols"][0]["quotes"]
    assert {q["source_name"] for q in quotes} == {"Yahoo Finance", "Alpha Vantage"}
    av_block = next(q for q in quotes if q["source_name"] == "Alpha Vantage")
    assert av_block["price"] == 10.5
    # "5.00%" carries a percent sign -> not float-parseable -> None (honest gap)
    assert av_block["change_pct"] is None
    assert av_block["volume"] == 1000.0
    assert av_block["latest_trading_day"] == "2026-11-20"


@pytest.mark.asyncio
async def test_global_quote_cache_hit_flag():
    from app.retrieval.ext_data import markets_global as mg

    mg._quote_cache.clear()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _resp(200, {"chart": {"result": [{"meta": {"regularMarketPrice": 1.0}}]}})

    first = await fetch_global_quote(["AAPL"], client=_client(handler))
    assert first is not None and first["cached"] is False
    second = await fetch_global_quote(["AAPL"], client=_client(handler))
    assert second is not None and second["cached"] is True
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fundamentals_invalid_statement_and_ticker():
    assert (
        await fetch_fundamentals(
            "AAPL",
            statement=" nonsense ",
            fmp_api_key="K",
            client=_client(lambda r: _resp(200, [])),
        )
        is None
    )
    assert (
        await fetch_fundamentals("!!!", fmp_api_key="K", client=_client(lambda r: _resp(200, [])))
        is None
    )


@pytest.mark.asyncio
async def test_fundamentals_upstream_failures():
    from app.retrieval.ext_data import markets_global as mg

    mg._fundamentals_cache.clear()
    assert (
        await fetch_fundamentals(
            "AAPL", fmp_api_key="K", client=_client(lambda r: httpx.Response(404))
        )
        is None
    )
    mg._fundamentals_cache.clear()
    # empty statement list -> None
    assert (
        await fetch_fundamentals("AAPL", fmp_api_key="K", client=_client(lambda r: _resp(200, [])))
        is None
    )


@pytest.mark.asyncio
async def test_fundamentals_balance_sheet_fields():
    from app.retrieval.ext_data import markets_global as mg

    mg._fundamentals_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "balance-sheet-statement" in str(request.url)
        return _resp(
            200,
            [
                {
                    "date": "2025-12-31",
                    "period": "FY",
                    "reportedCurrency": "USD",
                    "totalAssets": 500.0,
                    "totalLiabilities": 200.0,
                    "totalStockholdersEquity": 300.0,
                }
            ],
        )

    data = await fetch_fundamentals(
        "AAPL", statement="balance-sheet-statement", fmp_api_key="K", client=_client(handler)
    )
    assert data is not None
    assert data["statement"] == "balance-sheet-statement"
    assert data["periods"][0]["total_assets"] == 500.0
    assert data["periods"][0]["total_equity"] == 300.0
    assert data["periods"][0]["revenue"] is None  # not present on the balance sheet
