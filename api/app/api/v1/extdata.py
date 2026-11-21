"""External data integrations endpoints (docs/32).

GET /v1/market/crypto, /v1/market/crypto/global — CoinGecko (keyless)
GET /v1/market/fx                        — Frankfurter + ExchangeRate-API
GET /v1/weather                          — Open-Meteo (keyless) + OWM upgrade
GET /v1/news/external                    — NewsAPI (key-gated)
GET /v1/market/global-quote              — Yahoo keyless + TwelveData/AV upgrades
GET /v1/market/fundamentals              — FMP statements (key-gated)
GET /v1/net/ipgeo                        — ipwho.is keyless + IPinfo upgrade

All sources carry explicit provenance (source_name/source_url/as_of); a
missing key or dead upstream is reported (503/empty block) — never fabricated
(docs/32 §1). Free-tier defense: bounded TTL caches + single-flight live in
the fetchers themselves.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.v1.schemas import (
    ExtCryptoGlobalResponse,
    ExtCryptoResponse,
    ExtFundamentalsResponse,
    ExtFxRateOut,
    ExtFxResponse,
    ExtGlobalQuoteResponse,
    ExtIpGeoResponse,
    ExtNewsResponse,
    ExtWeatherResponse,
)
from app.config import get_settings
from app.core.errors import ErrorCode, MetaSearchError
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

router = APIRouter(tags=["ext-data"])

_NO_KEY_HINTS = {
    "news": "set NEWSAPI_KEY in the environment (free key: newsapi.org). "
    "MetaSearch's own google_news engine stays keyless.",
    "fmp": "set FMP_API_KEY in the environment (free key: financialmodelingprep.com).",
}


def _sanitize_ip_query(raw: str) -> str | None:
    import ipaddress

    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return None


@router.get("/market/crypto", response_model=ExtCryptoResponse)
async def market_crypto(
    ids: str = Query(min_length=1, max_length=400),
    vs_currency: str = Query(default="usd", min_length=2, max_length=8),
) -> ExtCryptoResponse:
    """Crypto spot prices + 24h change/volume/market-cap from CoinGecko
    (keyless; COINGECKO_API_KEY upgrades quota). Comma-separated ids, e.g.
    `bitcoin,ethereum,solana`."""
    settings = get_settings()
    coin_ids = [part for part in ids.split(",") if part.strip()][:25]
    result = await fetch_crypto_prices(
        coin_ids,
        vs_currency=vs_currency,
        api_key=settings.coingecko_api_key,
    )
    if result is None:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            "no crypto data from CoinGecko for the given ids",
            status_code=503,
            details={"ids": ids[:200]},
        )
    return ExtCryptoResponse(
        vs_currency=result["vs_currency"],
        coins=result["coins"],
        source_name=result["source_name"],
        source_url=result["source_url"],
        cached=bool(result.get("cached")),
    )


@router.get("/market/crypto/global", response_model=ExtCryptoGlobalResponse)
async def market_crypto_global() -> ExtCryptoGlobalResponse:
    settings = get_settings()
    result = await fetch_crypto_global(api_key=settings.coingecko_api_key)
    if result is None:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            "no global crypto stats from CoinGecko",
            status_code=503,
        )
    return ExtCryptoGlobalResponse(
        active_cryptocurrencies=result.get("active_cryptocurrencies"),
        markets=result.get("markets"),
        total_market_cap_usd=result.get("total_market_cap_usd"),
        total_volume_24h_usd=result.get("total_volume_24h_usd"),
        btc_dominance_pct=result.get("btc_dominance_pct"),
        eth_dominance_pct=result.get("eth_dominance_pct"),
        market_cap_change_24h_pct=result.get("market_cap_change_24h_pct"),
        source_name=result["source_name"],
        source_url=result["source_url"],
    )


@router.get("/market/fx", response_model=ExtFxResponse)
async def market_fx(
    base: str = Query(default="USD", min_length=3, max_length=3),
    symbols: str = Query(default="", max_length=120),
) -> ExtFxResponse:
    """FX rates for `base` (e.g. USD) merged from Frankfurter (ECB, official
    prints) + ExchangeRate-API open endpoint — both keyless; per-currency
    provenance included. `symbols` = comma-separated codes (VND,EUR,...)."""
    settings = get_settings()
    symbol_list = [s for s in symbols.split(",") if s.strip()][:20]
    result = await fetch_fx_rates(
        base, symbol_list, exchangerate_api_key=settings.exchangerate_api_key
    )
    if result is None:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            "no FX rates available from any source",
            status_code=503,
            details={"base": base},
        )
    return ExtFxResponse(
        base=result["base"],
        rates=[
            ExtFxRateOut(
                currency=code,
                rate=block["rate"],
                source_name=block["source_name"],
                source_url=block["source_url"],
                as_of=str(block.get("as_of") or ""),
            )
            for code, block in result["rates"].items()
        ],
        sources_used=result["sources_used"],
        requested_symbols=result["requested_symbols"],
        as_of=result.get("as_of") or "",
        cached=bool(result.get("cached")),
    )


@router.get("/weather", response_model=ExtWeatherResponse)
async def weather(
    q: str | None = Query(default=None, min_length=1, max_length=80),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lon: float | None = Query(default=None, ge=-180, le=180),
    days: int = Query(default=3, ge=1, le=7),
) -> ExtWeatherResponse:
    """Weather by city name (geocoded) or lat/lon: Open-Meteo keyless
    forecast; OPENWEATHER_API_KEY adds a current-conditions block."""
    if q is None and lat is None and lon is None:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            "provide a city name (q) or lat+lon",
            status_code=400,
        )
    settings = get_settings()
    result = await fetch_weather(
        city=q,
        lat=lat,
        lon=lon,
        days=days,
        openweather_api_key=settings.openweather_api_key,
    )
    if result is None:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            "no weather data from any source for the given location",
            status_code=503,
            details={"q": q[:80] if q else None},
        )
    return ExtWeatherResponse(
        location=result.get("location"),
        open_meteo=result.get("open_meteo"),
        openweather_current=result.get("openweather_current"),
        sources_used=result["sources_used"],
        cached=bool(result.get("cached")),
    )


@router.get("/news/external", response_model=ExtNewsResponse)
async def news_external(
    q: str | None = Query(default=None, min_length=1, max_length=120),
    country: str | None = Query(default=None, min_length=2, max_length=2),
    category: str | None = Query(default=None, min_length=3, max_length=20),
    page_size: int = Query(default=20, ge=1, le=50),
    days: int = Query(default=3, ge=1, le=7),
) -> ExtNewsResponse:
    """Global news via NewsAPI (top-headlines, or everything matching `q`).
    Key-gated: without NEWSAPI_KEY the endpoint explains the setup (the
    keyless google_news engine remains available)."""
    settings = get_settings()
    api_key = settings.newsapi_key
    if not api_key:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            _NO_KEY_HINTS["news"],
            status_code=503,
            details={"source": "NewsAPI", "key_env": "NEWSAPI_KEY"},
        )
    result = await fetch_external_news(
        q=q,
        country=country,
        category=category,
        page_size=page_size,
        days=days,
        api_key=api_key,
    )
    if result is None:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            "no news from NewsAPI for the given filters",
            status_code=503,
            details={"q": q[:120] if q else None},
        )
    return ExtNewsResponse(
        endpoint=result["endpoint"],
        items=result["items"],
        total_results=result["total_results"],
        source_name=result["source_name"],
        source_url=result["source_url"],
        cached=bool(result.get("cached")),
    )


@router.get("/market/global-quote", response_model=ExtGlobalQuoteResponse)
async def market_global_quote(
    symbols: str = Query(min_length=1, max_length=120),
) -> ExtGlobalQuoteResponse:
    """Global ticker quotes (US/international): Yahoo Finance keyless
    primary; TWELVEDATA_API_KEY / ALPHA_VANTAGE_API_KEY add per-source
    blocks. `symbols` = comma-separated (AAPL,MSFT,BRK.B), max 5."""
    settings = get_settings()
    symbol_list = [s for s in symbols.split(",") if s.strip()][:5]
    result = await fetch_global_quote(
        symbol_list,
        alpha_vantage_api_key=settings.alpha_vantage_api_key,
        twelvedata_api_key=settings.twelvedata_api_key,
    )
    if result is None:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            "no valid symbols provided",
            status_code=400,
        )
    return ExtGlobalQuoteResponse(
        symbols=result["symbols"],
        sources_used=result["sources_used"],
        cached=bool(result.get("cached")),
    )


@router.get("/market/fundamentals", response_model=ExtFundamentalsResponse)
async def market_fundamentals(
    symbol: str = Query(min_length=1, max_length=15),
    statement: str = Query(
        default="income-statement",
        pattern="^(income-statement|balance-sheet-statement|cash-flow-statement)$",
    ),
) -> ExtFundamentalsResponse:
    """Financial statements via Financial Modeling Prep (key-gated: 503
    without FMP_API_KEY). Up to 4 recent periods."""
    settings = get_settings()
    api_key = settings.fmp_api_key
    if not api_key:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            _NO_KEY_HINTS["fmp"],
            status_code=503,
            details={"source": "Financial Modeling Prep", "key_env": "FMP_API_KEY"},
        )
    result = await fetch_fundamentals(symbol, statement=statement, fmp_api_key=api_key)
    if result is None:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            "no fundamentals from FMP for the given symbol/statement",
            status_code=503,
            details={"symbol": symbol[:15], "statement": statement[:32]},
        )
    return ExtFundamentalsResponse(
        symbol=result["symbol"],
        statement=result["statement"],
        periods=result["periods"],
        source_name=result["source_name"],
        source_url=result["source_url"],
        cached=bool(result.get("cached")),
    )


@router.get("/net/ipgeo", response_model=ExtIpGeoResponse)
async def net_ipgeo(
    ip: str = Query(min_length=3, max_length=45),
) -> ExtIpGeoResponse:
    """IP geolocation: ipwho.is keyless; IPINFO_TOKEN adds an IPinfo block."""
    settings = get_settings()
    clean_ip = _sanitize_ip_query(ip)
    if clean_ip is None:
        raise MetaSearchError(ErrorCode.INVALID_REQUEST, f"invalid ip: {ip!r}", status_code=400)
    result = await fetch_ipgeo(clean_ip, ipinfo_token=settings.ipinfo_token)
    if result is None:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            "no IP geolocation data from any source",
            status_code=503,
            details={"ip": clean_ip[:45]},
        )
    return ExtIpGeoResponse(
        ip=result["ip"],
        ipwho=result.get("ipwho"),
        ipinfo=result.get("ipinfo"),
        sources_used=result["sources_used"],
        cached=bool(result.get("cached")),
    )
