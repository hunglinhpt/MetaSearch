"""Stooq Finance Adapter — Keyless Global Equities, Commodities, Forex & Macro Yields.

Fetches institutional-grade OHLCV historical and real-time market data from Stooq:
- US & Global Equities (AAPL.US, NVDA.US, MSFT.US, TSLA.US)
- Global Benchmark Indices (^SPX S&P 500, ^DJI Dow Jones, ^NDQ Nasdaq, ^DAX, ^NKX Nikkei)
- Macro Yields & Commodities (US10Y, Gold XAU/USD, Crude Oil WTI, USD/VND)
- Computes on-the-fly Fibonacci Retracement, Ichimoku Cloud, and Pattern Recognition.

Category: ``financial``. Zero API keys required, zero-CAPTCHA.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.retrieval.models import RawResult
from app.retrieval.technical_analysis import (
    calc_fibonacci_retracement,
    calc_geometric_trendlines,
    calc_ichimoku_cloud,
    compute_custom_indicators,
    detect_chart_patterns,
    format_technical_analysis_markdown,
    parse_custom_indicator_params,
)

logger = logging.getLogger(__name__)

DEFAULT_STOOQ_URL = "https://stooq.com/q/d/l/"

_GLOBAL_SYMBOL_MAP: dict[str, str] = {
    # Benchmark Indices
    "s&p 500": "^spx",
    "sp500": "^spx",
    "^spx": "^spx",
    "dow jones": "^dji",
    "dji": "^dji",
    "^dji": "^dji",
    "nasdaq": "^ndq",
    "^ndq": "^ndq",
    "nikkei": "^nkx",
    "^nkx": "^nkx",
    "dax": "^dax",
    "^dax": "^dax",
    # Commodities, Forex, Macro Yields
    "giá vàng": "xauusd",
    "xau/usd": "xauusd",
    "xauusd": "xauusd",
    "gold price": "xauusd",
    "gold": "xauusd",
    "dầu thô": "cl.f",
    "giá dầu": "cl.f",
    "crude oil": "cl.f",
    "wti": "cl.f",
    "cl.f": "cl.f",
    "trái phiếu mỹ 10 năm": "10yus.b",
    "trái phiếu mỹ": "10yus.b",
    "lợi suất trái phiếu mỹ": "10yus.b",
    "us10y": "10yus.b",
    "10yus.b": "10yus.b",
    "usdvnd": "usdvnd",
    "tỷ giá usd/vnd": "usdvnd",
    "tỷ giá usd": "usdvnd",
    "usd/vnd": "usdvnd",
    # Global Equities & Tech Megacaps
    "apple": "aapl.us",
    "aapl": "aapl.us",
    "nvidia": "nvda.us",
    "nvda": "nvda.us",
    "microsoft": "msft.us",
    "msft": "msft.us",
    "tesla": "tsla.us",
    "tsla": "tsla.us",
    "alphabet": "googl.us",
    "googl": "googl.us",
    "goog": "goog.us",
    "amazon": "amzn.us",
    "amzn": "amzn.us",
    "meta platforms": "meta.us",
    "facebook": "meta.us",
    "amd": "amd.us",
    "intel": "intc.us",
    "intc": "intc.us",
    "palantir": "pltr.us",
    "pltr": "pltr.us",
    "taiwan semi": "tsm.us",
    "tsmc": "tsm.us",
    "tsm": "tsm.us",
    "broadcom": "avgo.us",
    "avgo": "avgo.us",
    "netflix": "nflx.us",
    "nflx": "nflx.us",
    "alibaba": "baba.us",
    "baba": "baba.us",
    "berkshire": "brk-b.us",
    "jpmorgan": "jpm.us",
    "jpm": "jpm.us",
    "coinbase": "coin.us",
    "microstrategy": "mstr.us",
    "mstr": "mstr.us",
}

_STOP_WORDS_GLOBAL = {
    # Vietnamese words & stock tickers (handled by viet_stock)
    "PHAN",
    "TICH",
    "KY",
    "THUAT",
    "GIA",
    "CO",
    "PHIEU",
    "THI",
    "TRUONG",
    "FIBO",
    "FIBONACCI",
    "ICHIMOKU",
    "MAY",
    "BCTC",
    "TAI",
    "CHINH",
    "DOANH",
    "THU",
    "LOI",
    "NHUAN",
    "HOM",
    "NAY",
    "FPT",
    "HPG",
    "VCB",
    "VNM",
    "SSI",
    "MWG",
    "IDC",
    "PVS",
    "DGC",
    "TCB",
    "MBB",
    "ACB",
    "VHM",
    "VIC",
    "VRE",
    "MSN",
    "GAS",
    "PNJ",
    "VND",
    "VCI",
    "KBC",
    "STB",
    "CTG",
    "BID",
    "VPB",
    "SHB",
    "HDB",
    "LPB",
    "VIB",
    "TPB",
    "GVR",
    "SAB",
    "PLX",
    "POW",
    "BVH",
    "HSX",
    "HNX",
    "HOSE",
    "UPCOM",
    "VN30",
    "VNI",
    # Common English words & Technical/IT/Finance/Medical Acronyms
    "STOCK",
    "PRICE",
    "CHART",
    "ANALYSIS",
    "TODAY",
    "THE",
    "FOR",
    "AND",
    "WITH",
    "FROM",
    "WHAT",
    "WHEN",
    "WHERE",
    "WHICH",
    "HOW",
    "WHY",
    "WHO",
    "ARE",
    "WAS",
    "WERE",
    "THIS",
    "THAT",
    "HAVE",
    "HAS",
    "HAD",
    "NOT",
    "BUT",
    "CAN",
    "ALL",
    "NEW",
    "TOP",
    "BEST",
    "USD",
    "EUR",
    "JPY",
    "GBP",
    "AI",
    "API",
    "SQL",
    "LLM",
    "RAG",
    "MCP",
    "SSE",
    "HTTP",
    "HTTPS",
    "JSON",
    "HTML",
    "CSS",
    "URL",
    "URI",
    "CPU",
    "GPU",
    "RAM",
    "DOM",
    "JWT",
    "TLS",
    "SSL",
    "DNS",
    "TCP",
    "UDP",
    "SSH",
    "CLI",
    "GUI",
    "SDK",
    "IDE",
    "ORM",
    "AWS",
    "GCP",
    "RSI",
    "MACD",
    "EMA",
    "SMA",
    "ATR",
    "ADX",
    "OBV",
    "VCP",
    "ROE",
    "ROA",
    "EPS",
    "BVPS",
    "EBIT",
    "EBITDA",
    "CFO",
    "FCF",
    "NPL",
    "CAR",
    "NIM",
    "CASA",
    "LDR",
    "CIR",
    "DSO",
    "GDP",
    "CPI",
    "PCE",
    "FDI",
    "FED",
    "ECB",
    "BOJ",
    "SBV",
    "IMF",
    "WTO",
    "ETF",
    "IPO",
    "ESOP",
    "CEO",
    "CTO",
    "COO",
    "SEC",
    "FDA",
    "NIH",
    "DNA",
    "RNA",
    "PCR",
    "MRI",
    "HIV",
    " Crispr",
    "COVID",
    "SARS",
}

_TICKER_REGEX = re.compile(r"\b([A-Z]{2,5})(?:\.US)?\b")
_FINANCE_CONTEXT_WORDS = (
    "stock",
    "share",
    "ticker",
    "price",
    "chart",
    "earnings",
    "nasdaq",
    "nyse",
    "wall street",
    "cổ phiếu",
    "chứng khoán",
    "giá",
    "phân tích kỹ thuật",
    "fibonacci",
    "ichimoku",
    "rsi",
    "macd",
    "bollinger",
)


def _extract_stooq_symbol(query: str) -> str | None:
    """Extracts a valid Stooq ticker symbol from a query using strict word boundaries."""
    clean_q = query.strip().lower()
    if not clean_q:
        return None

    # 1. Explicit cashtags like $AMD, $NVDA, $GOOGL, $META
    for tok in query.split():
        if tok.startswith("$") and 2 <= len(tok) <= 6:
            sym = tok[1:].strip(".,;:!?()").lower()
            if sym.isalnum() and sym.upper() not in _STOP_WORDS_GLOBAL:
                return f"{sym}.us"

    # 2. Match longest key from global symbol map using strict word boundaries
    for kw in sorted(_GLOBAL_SYMBOL_MAP.keys(), key=len, reverse=True):
        if kw.startswith("^"):
            if re.search(rf"(?:^|\s){re.escape(kw)}(?:\b|\s|$)", clean_q):
                return _GLOBAL_SYMBOL_MAP[kw]
        elif re.search(rf"\b{re.escape(kw)}\b", clean_q):
            # Disambiguate "vàng" / "gold" / "meta" if used in non-financial context
            return _GLOBAL_SYMBOL_MAP[kw]

    # 3. Explicit .US suffix in query (e.g. "COST.US", "uber.us")
    m_us = re.search(r"\b([a-zA-Z]{1,5})\.us\b", query, re.IGNORECASE)
    if m_us:
        return f"{m_us.group(1).lower()}.us"

    # 4. Look for explicit uppercase ticker tokens ONLY when financial context is present
    has_fin_context = any(w in clean_q for w in _FINANCE_CONTEXT_WORDS)
    if has_fin_context:
        for m in _TICKER_REGEX.finditer(query):
            candidate = m.group(1).upper()
            if candidate not in _STOP_WORDS_GLOBAL and 2 <= len(candidate) <= 5:
                return f"{candidate.lower()}.us"

    # 5. Fallback for broad US / Global market queries
    market_triggers = (
        "chứng khoán mỹ",
        "thị trường mỹ",
        "global market",
        "wall street",
        "us stock",
        "chỉ số s&p",
    )
    if any(t in clean_q for t in market_triggers):
        return "^spx"

    return None


class StooqFinanceAdapter:
    id = "stooq"

    def __init__(self, base_url: str = DEFAULT_STOOQ_URL) -> None:
        self.base_url = base_url

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 10,
        timeout_s: float = 4.0,
        **kwargs: Any,
    ) -> list[RawResult]:
        symbol = _extract_stooq_symbol(query)
        if not symbol:
            return []

        custom_params = parse_custom_indicator_params(query)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 MetaSearch/0.9.0"
            )
        }
        params = {"s": symbol, "i": "d"}

        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        volumes: list[float] = []
        latest_date: str | None = None

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
                resp = await client.get(self.base_url, params=params, headers=headers)
                if resp.status_code == 200 and resp.text and "Date" in resp.text[:100]:
                    reader = csv.DictReader(io.StringIO(resp.text))
                    rows = sorted(
                        (r for r in reader if r.get("Date")),
                        key=lambda item: str(item.get("Date", "")),
                    )
                    for r in rows[-120:]:
                        try:
                            c = float(r.get("Close", 0.0))
                            o_val = float(r.get("Open", c) or c)
                            h = float(r.get("High", c) or c)
                            l_val = float(r.get("Low", c) or c)
                            v = float(r.get("Volume", 0.0) or 0.0)
                            if c > 0:
                                opens.append(o_val)
                                closes.append(c)
                                highs.append(h)
                                lows.append(l_val)
                                volumes.append(v)
                                latest_date = str(r.get("Date", "")).strip() or latest_date
                        except (ValueError, TypeError):
                            continue
        except Exception as exc:
            logger.debug("stooq_fetch_failed: %s", exc)

        # Fail gracefully without synthesizing hallucinated data
        if not closes or len(closes) < 5:
            logger.debug("stooq_insufficient_or_missing_data for %s (bars=%d)", symbol, len(closes))
            return []

        pub_dt: datetime | None = None
        if latest_date:
            try:
                pub_dt = datetime.strptime(latest_date, "%Y-%m-%d").replace(tzinfo=UTC)
            except Exception:
                pub_dt = None

        curr_price = closes[-1]
        prev_price = closes[-2] if len(closes) >= 2 else curr_price
        pct_change = ((curr_price - prev_price) / prev_price) * 100 if prev_price else 0.0
        sign = "+" if pct_change >= 0 else ""
        source_mode = "Live Stooq Real EOD"

        # Perform quantitative computations on recent 90-session swing window
        swing_h = highs[-90:] if len(highs) >= 90 else highs
        swing_l = lows[-90:] if len(lows) >= 90 else lows
        fibo = calc_fibonacci_retracement(swing_h, swing_l, curr_price)
        ichimoku = calc_ichimoku_cloud(highs, lows, closes)
        trendline = calc_geometric_trendlines(highs, lows, closes)
        patterns = detect_chart_patterns(highs, lows, closes, volumes, opens=opens)
        customs = compute_custom_indicators(
            closes, volumes, highs=highs, lows=lows, **custom_params
        )

        pat_name = patterns[0].name if patterns else "Đang tích lũy"
        rsi_info = customs.get("rsi", {})
        macd_info = customs.get("macd", {})
        extra_ind = ""
        if rsi_info and macd_info:
            extra_ind = (
                f" · RSI({rsi_info['period']})={rsi_info['value']:.1f} · "
                f"MACD={macd_info['macd_line']:,.2f} (Signal={macd_info['signal_line']:,.2f}, "
                f"Hist={macd_info['histogram']:+,.2f})"
            )

        date_str = latest_date or "EOD"
        tech_snippet = (
            f"[{symbol.upper()} · {source_mode} · {date_str}] "
            f"Giá: {curr_price:,.2f} ({sign}{pct_change:.2f}%) · "
            f"Xu hướng: {trendline.trend_type} ({trendline.channel_slope_deg}°, "
            f"R²={trendline.r_squared:.2f}) · "
            f"Fibo Retracement: Hỗ trợ {fibo.nearest_support:,.2f}, "
            f"Kháng cự {fibo.nearest_resistance:,.2f}, "
            f"Golden Pocket (61.8%-65.0%): {fibo.golden_pocket_range} · "
            f"Mây Ichimoku: {ichimoku.cloud_state} ({ichimoku.tk_cross})"
            f"{extra_ind} · Mẫu hình: {pat_name}."
        )

        rich_md = format_technical_analysis_markdown(
            symbol.upper(), curr_price, fibo, ichimoku, trendline, patterns, customs
        )

        return [
            RawResult(
                url=f"https://stooq.com/q/?s={symbol}",
                title=f"{symbol.upper()} - Dữ Liệu Thị Trường Toàn Cầu & Fibonacci (Stooq)",
                snippet=tech_snippet,
                rank=1,
                adapter=self.id,
                source_engines=[self.id],
                published_at=pub_dt,
            ),
            RawResult(
                url=f"https://stooq.com/q/a/?s={symbol}",
                title=f"{symbol.upper()} - Phân Tích Hình Học, Mây Ichimoku & Mẫu Hình (Stooq)",
                snippet=f"Chi tiết kỹ thuật {symbol.upper()}:\n{rich_md}",
                rank=2,
                adapter=self.id,
                source_engines=[self.id],
                published_at=pub_dt,
            ),
        ][:max_results]
