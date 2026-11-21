"""Global market quotes & fundamentals (docs/32 §6).

Quotes: Yahoo Finance chart API is the KEYLESS primary (already the project's
global OHLCV feed); Alpha Vantage (``ALPHA_VANTAGE_API_KEY``) and Twelve Data
(``TWELVEDATA_API_KEY``) are optional per-source upgrades — each source's
quote is returned in its own provenance-bearing block (never blended into a
single "truth"). Fundamentals (financial statements) come from Financial
Modeling Prep and are strictly key-gated.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.core.logging import get_logger
from app.core.singleflight import SingleFlight
from app.http import get_http_client
from app.retrieval.ext_data._base import DEFAULT_TIMEOUT_S, TtlCache, json_get
from app.retrieval.ext_data.crypto import _key_tag

logger = get_logger(__name__)

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_AV_URL = "https://www.alphavantage.co/query"
_TD_URL = "https://api.twelvedata.com/quote"
_FMP_URL = "https://financialmodelingprep.com/api/v3"

# US/global tickers: letters, dots (BRK.B), dashes and digits.
_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,15}$")

_quote_cache = TtlCache(ttl_s=60.0, max_entries=64)
_fundamentals_cache = TtlCache(ttl_s=3600.0, max_entries=32)
_quote_flight = SingleFlight()
_fundamentals_flight = SingleFlight()


def sanitize_ticker(raw: str) -> str | None:
    t = raw.strip().upper()
    return t if t and _TICKER_RE.fullmatch(t) else None


async def fetch_global_quote(
    symbols: list[str],
    *,
    alpha_vantage_api_key: str = "",
    twelvedata_api_key: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: Any = None,
) -> dict[str, Any] | None:
    """Per-symbol quotes from every configured source, each in its own
    provenance block. Returns None only when no source yields anything for
    a given symbol (that symbol then appears with empty ``quotes``)."""
    clean: list[str] = []
    for raw in symbols[:5]:
        t = sanitize_ticker(raw)
        if t and t not in clean:
            clean.append(t)
    if not clean:
        return None

    cache_key = (
        f"quote:{','.join(clean)}:{_key_tag(alpha_vantage_api_key)}:{_key_tag(twelvedata_api_key)}"
    )
    cached = _quote_cache.get(cache_key)
    if cached is not None:
        return dict(cached, cached=True)

    async def _load() -> dict[str, Any] | None:
        results: list[dict[str, Any]] = []
        sources_used: list[str] = ["Yahoo Finance (keyless)"]

        async def _one(sym: str) -> dict[str, Any]:
            tasks: list[Any] = [_fetch_yahoo_quote(sym, timeout_s=timeout_s, client=client)]
            if twelvedata_api_key:
                tasks.append(
                    _fetch_twelvedata_quote(
                        sym, api_key=twelvedata_api_key, timeout_s=timeout_s, client=client
                    )
                )
            if alpha_vantage_api_key:
                tasks.append(
                    _fetch_alphavantage_quote(
                        sym, api_key=alpha_vantage_api_key, timeout_s=timeout_s, client=client
                    )
                )
            blocks = await asyncio.gather(*tasks)
            quotes = [b for b in blocks if b is not None]
            return {"symbol": sym, "quotes": quotes}

        results = list(await asyncio.gather(*(_one(s) for s in clean)))
        return {
            "symbols": results,
            "sources_used": sources_used,
        }

    result = await _quote_flight.run(cache_key, _load)
    if result is None:
        return None
    _quote_cache.put(cache_key, result)
    return dict(result, cached=False)


async def fetch_fundamentals(
    symbol: str,
    *,
    statement: str = "income-statement",
    fmp_api_key: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client: Any = None,
) -> dict[str, Any] | None:
    """Financial statements from FMP — strictly key-gated (no keyless tier)."""
    if not fmp_api_key:
        return None
    clean = sanitize_ticker(symbol)
    if clean is None:
        return None
    if statement not in ("income-statement", "balance-sheet-statement", "cash-flow-statement"):
        return None
    cache_key = f"fmp:{clean}:{statement}"
    cached = _fundamentals_cache.get(cache_key)
    if cached is not None:
        return dict(cached, cached=True)

    async def _load() -> dict[str, Any] | None:
        try:
            http_cli = client or get_http_client("search")
            resp = await http_cli.get(
                f"{_FMP_URL}/{statement}/{clean}",
                params={"apikey": fmp_api_key, "limit": 4},
                timeout=timeout_s,
            )
            if resp.status_code != 200:
                logger.warning("ext_fmp_failed", status=resp.status_code)
                return None
            data = resp.json()
        except Exception as exc:
            logger.warning("ext_fmp_error", error=type(exc).__name__)
            return None
        if not isinstance(data, list) or not data:
            return None
        periods: list[dict[str, Any]] = []
        for row in data[:4]:
            if not isinstance(row, dict):
                continue
            periods.append(
                {
                    "date": str(row.get("date") or "")[:32],
                    "fiscal_period": str(row.get("period") or "")[:16],
                    "currency": str(row.get("reportedCurrency") or "")[:8],
                    "revenue": _num(row.get("revenue")),
                    "net_income": _num(row.get("netIncome")),
                    "gross_profit": _num(row.get("grossProfit")),
                    "operating_income": _num(row.get("operatingIncome")),
                    "total_assets": _num(row.get("totalAssets")),
                    "total_liabilities": _num(row.get("totalLiabilities")),
                    "total_equity": _num(row.get("totalStockholdersEquity")),
                    "operating_cash_flow": _num(row.get("operatingCashFlow")),
                    "free_cash_flow": _num(row.get("freeCashFlow")),
                }
            )
        if not periods:
            return None
        return {
            "symbol": clean,
            "statement": statement,
            "periods": periods,
            "source_name": "Financial Modeling Prep",
            "source_url": "https://site.financialmodelingprep.com",
        }

    result = await _fundamentals_flight.run(cache_key, _load)
    if result is None:
        return None
    _fundamentals_cache.put(cache_key, result)
    return dict(result, cached=False)


async def _fetch_yahoo_quote(
    sym: str, *, timeout_s: float, client: Any = None
) -> dict[str, Any] | None:
    try:
        http_cli = client or get_http_client("search")
        # Yahoo ticker format uses DASHES for share classes (BRK-B), while
        # AV/FMP use dots (BRK.B) — convert per-source, keep the canonical
        # dotted form in our output (verified live 2026-11-21: BRK.B → empty
        # on Yahoo until converted).
        resp = await http_cli.get(
            _YAHOO_CHART_URL.format(symbol=sym.replace(".", "-")),
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout_s,
        )
        if resp.status_code != 200:
            logger.warning("ext_yahoo_quote_failed", symbol=sym, status=resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("ext_yahoo_quote_error", symbol=sym, error=type(exc).__name__)
        return None
    result = json_get(data, "chart", "result")
    if not isinstance(result, list) or not result:
        return None
    meta = result[0].get("meta") if isinstance(result[0], dict) else None
    if not isinstance(meta, dict):
        return None
    price = meta.get("regularMarketPrice")
    if price is None:
        return None
    return {
        "source_name": "Yahoo Finance",
        "source_url": f"https://finance.yahoo.com/quote/{sym}",
        "price": _num(price),
        "previous_close": _num(meta.get("chartPreviousClose")),
        "currency": str(meta.get("currency") or "")[:8],
        "exchange": str(meta.get("fullExchangeName") or "")[:40],
        "market_time": _num(meta.get("regularMarketTime")),
    }


async def _fetch_twelvedata_quote(
    sym: str,
    *,
    api_key: str,
    timeout_s: float,
    client: Any = None,
) -> dict[str, Any] | None:
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(
            _TD_URL,
            params={"symbol": sym, "apikey": api_key},
            timeout=timeout_s,
        )
        if resp.status_code != 200:
            logger.warning("ext_td_quote_failed", symbol=sym, status=resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("ext_td_quote_error", symbol=sym, error=type(exc).__name__)
        return None
    if not isinstance(data, dict) or data.get("status") == "error":
        return None
    close = data.get("close")
    if close is None:
        return None
    return {
        "source_name": "Twelve Data",
        "source_url": "https://twelvedata.com",
        "price": _num(close),
        "previous_close": _num(data.get("previous_close")),
        "currency": str(data.get("currency") or "")[:8],
        "exchange": str(data.get("exchange") or "")[:40],
        "market_time": _num(data.get("timestamp")),
    }


async def _fetch_alphavantage_quote(
    sym: str,
    *,
    api_key: str,
    timeout_s: float,
    client: Any = None,
) -> dict[str, Any] | None:
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(
            _AV_URL,
            params={"function": "GLOBAL_QUOTE", "symbol": sym, "apikey": api_key},
            timeout=timeout_s,
        )
        if resp.status_code != 200:
            logger.warning("ext_av_quote_failed", symbol=sym, status=resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("ext_av_quote_error", symbol=sym, error=type(exc).__name__)
        return None
    node = json_get(data, "Global Quote")
    if not isinstance(node, dict) or not node:
        return None
    # Keys are numeric-suffixed ("05. price") — read defensively.
    price = _av_field(node, "price")
    if price is None:
        return None
    return {
        "source_name": "Alpha Vantage",
        "source_url": "https://www.alphavantage.co",
        "price": price,
        "previous_close": _av_field(node, "previous close"),
        "currency": None,  # AV GLOBAL_QUOTE does not state currency
        "change_pct": _av_field(node, "change percent"),
        "volume": _av_field(node, "volume"),
        "latest_trading_day": str(node.get("07. latest trading day") or "")[:16] or None,
    }


def _av_field(node: dict[str, Any], label: str) -> float | None:
    for key, value in node.items():
        if label in str(key).lower():
            return _num(value)
    return None


def _num(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(value, 8)
