"""Yahoo Finance adapter — vertical engine for real-time market data, stocks & financial news.

Uses keyless Yahoo Finance public endpoints:
- Search & Autocomplete: query2.finance.yahoo.com/v1/finance/search
- Chart Quote Data: query1.finance.yahoo.com/v8/finance/chart/{symbol}
Supports US equities, Vietnamese equities (.VN), crypto, currencies and global indices.
Category: ``financial``.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult
from app.retrieval.technical_analysis import (
    calc_fibonacci_retracement,
    calc_ichimoku_cloud,
    calc_wilder_rsi,
)

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
DEFAULT_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
DEFAULT_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,6}(?:[.\-][A-Za-z0-9]{1,4})?)")
_DOTTED_SYM_RE = re.compile(r"\b([A-Z]{1,5}\.(?:VN|HK|L|TO|DE|A|B)|[A-Z]{2,5}-[A-Z]{1,4})\b")
_UPPER_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")
_YAHOO_NOISE_WORDS = frozenset(
    {
        "STOCK",
        "STOCKS",
        "SHARE",
        "SHARES",
        "QUOTE",
        "QUOTES",
        "PRICE",
        "PRICES",
        "OUTLOOK",
        "EARNINGS",
        "NEWS",
        "SENTIMENT",
        "ANALYSIS",
        "FORECAST",
        "BUY",
        "SELL",
        "REPORT",
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "YOY",
        "QOQ",
        "EPS",
        "ETF",
        "SEC",
        "USD",
        "VND",
    }
)


def _extract_yahoo_lookup_query(raw_query: str) -> tuple[str, str | None]:
    """Return (search_query, primary_ticker_hint) safe for Yahoo autocomplete."""
    q = (raw_query or "").strip()
    m_cash = _CASHTAG_RE.search(q)
    if m_cash:
        sym = m_cash.group(1).upper()
        return sym, sym
    m_dot = _DOTTED_SYM_RE.search(q)
    if m_dot:
        sym = m_dot.group(1).upper()
        return sym, sym
    uppers = [t for t in _UPPER_TICKER_RE.findall(q) if t not in _YAHOO_NOISE_WORDS]
    if uppers:
        sym = uppers[0]
        from app.retrieval.vn_tickers import is_vn_ticker

        if is_vn_ticker(sym) and any(ord(c) > 127 for c in q):
            return f"{sym}.VN", f"{sym}.VN"
        return sym, sym
    stripped = re.sub(
        r"\b(stock|stocks|share|shares|quote|quotes|cổ phiếu|chứng khoán|kết quả kinh doanh|"
        r"triển vọng|phân tích|price|prices|earnings|outlook|forecast|news|sentiment)\b",
        " ",
        q,
        flags=re.IGNORECASE,
    )
    tokens = [w for w in _WS.sub(" ", stripped).strip().split() if len(w) > 1][:3]
    clean = " ".join(tokens) or q.lstrip("$")
    return clean, None


def _strip_html(text: str) -> str:
    return _WS.sub(" ", _TAG.sub("", text or "")).strip()


def _calc_sma(prices: list[float], period: int) -> float | None:
    if len(prices) < period or period <= 0:
        return None
    return sum(prices[-period:]) / period


def _calc_rsi(prices: list[float], period: int = 14) -> float | None:
    return calc_wilder_rsi(prices, period)


def _calc_technical_summary(
    closes: list[float],
    volumes: list[float],
    current_price: float | None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> str:
    valid_closes = [float(c) for c in closes if c is not None and isinstance(c, (int, float))]
    valid_vols = [float(v) for v in volumes if v is not None and isinstance(v, (int, float))]
    if not valid_closes:
        return ""

    parts: list[str] = []
    rsi = _calc_rsi(valid_closes, 14)
    if rsi is not None:
        state = "Overbought" if rsi >= 70 else ("Oversold" if rsi <= 30 else "Neutral")
        parts.append(f"RSI(14)={rsi:.1f} ({state})")

    sma20 = _calc_sma(valid_closes, 20)
    if sma20 is not None:
        parts.append(f"SMA20={sma20:,.2f}")

    sma50 = _calc_sma(valid_closes, 50)
    if sma50 is not None:
        parts.append(f"SMA50={sma50:,.2f}")

    if current_price is not None and sma20 is not None and sma50 is not None:
        if current_price >= sma20 >= sma50:
            parts.append("Trend: Bullish ↑")
        elif current_price <= sma20 <= sma50:
            parts.append("Trend: Bearish ↓")
        else:
            parts.append("Trend: Neutral/Consolidating")

    v_h = [float(h) for h in (highs or []) if h is not None and isinstance(h, (int, float))]
    v_l = [float(lv) for lv in (lows or []) if lv is not None and isinstance(lv, (int, float))]
    if v_h and v_l and current_price is not None:
        fibo = calc_fibonacci_retracement(v_h, v_l, float(current_price))
        ichi = calc_ichimoku_cloud(v_h, v_l, valid_closes)
        parts.append(
            f"Fibo Sup/Res: {fibo.nearest_support:,.2f}/{fibo.nearest_resistance:,.2f} "
            f"(Golden Pocket {fibo.golden_pocket_range})"
        )
        parts.append(f"Ichimoku: {ichi.tk_cross}")

    if len(valid_vols) >= 20 and valid_vols[-1] > 0:
        avg_vol20 = sum(valid_vols[-20:]) / 20
        if avg_vol20 > 0:
            ratio = valid_vols[-1] / avg_vol20
            parts.append(f"Vol={ratio:.1f}x 20D")

    if not parts:
        return ""
    return " · Techs: " + " · ".join(parts)


class YahooFinanceAdapter:
    id = "yahoo_finance"

    def __init__(
        self,
        *,
        search_url: str = DEFAULT_SEARCH_URL,
        chart_url: str = DEFAULT_CHART_URL,
        rss_url: str = DEFAULT_RSS_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.search_url = search_url
        self.chart_url = chart_url
        self.rss_url = rss_url
        self._client = client

    async def _fetch_ticker_rss(
        self,
        client: httpx.AsyncClient,
        symbol: str,
        *,
        freshness_days: int | None,
        max_items: int,
        timeout_s: float,
    ) -> list[RawResult]:
        from defusedxml import ElementTree as DET

        try:
            resp = await client.get(
                self.rss_url,
                params={"s": symbol, "region": "US", "lang": "en-US"},
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=True,
                timeout=min(timeout_s, 2.5),
            )
            if resp.status_code != 200 or len(resp.content or b"") > 2 * 1024 * 1024:
                return []
            root = DET.fromstring(resp.text)
        except Exception:
            return []
        cutoff_ts = (
            datetime.now(UTC).timestamp() - max(1, int(freshness_days)) * 86400
            if freshness_days
            else None
        )
        out: list[RawResult] = []
        for item in root.findall("./channel/item")[: max_items * 2]:
            title = _strip_html(item.findtext("title") or "")
            link = (item.findtext("link") or "").strip()
            desc = _strip_html(item.findtext("description") or "")
            pub_raw = (item.findtext("pubDate") or "").strip()
            if not title or not link.startswith("http"):
                continue
            pub_dt: datetime | None = None
            if pub_raw:
                from email.utils import parsedate_to_datetime

                try:
                    pub_dt = parsedate_to_datetime(pub_raw).astimezone(UTC)
                except (TypeError, ValueError, OverflowError):
                    pub_dt = None
            if cutoff_ts is not None and pub_dt is not None and pub_dt.timestamp() < cutoff_ts:
                continue
            out.append(
                RawResult(
                    url=link,
                    title=title,
                    snippet=desc or f"[Yahoo Finance RSS · {symbol}] {title}",
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=pub_dt,
                    source_url=link,
                    source_name="Yahoo Finance",
                    as_of=pub_dt,
                    data_kind="observed",
                    rank=len(out) + 1,
                )
            )
            if len(out) >= max_items:
                break
        return out

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 4.0,
    ) -> list[RawResult]:
        clean_q = query.strip().lstrip("$")
        if not clean_q:
            return []

        effective_q, sym_hint = _extract_yahoo_lookup_query(query)
        search_params = {
            "q": effective_q,
            "quotesCount": str(min(max_results, 6)),
            "newsCount": str(min(max_results, 8)),
            "enableFuzzyQuery": "false" if sym_hint else "true",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 MetaSearch/0.8.0"
            ),
            "Accept": "application/json",
        }

        client = self._client or get_http_client("search")
        try:
            resp = await client.get(
                self.search_url,
                params=search_params,
                headers=headers,
                follow_redirects=True,
                timeout=timeout_s,
            )
            if resp.status_code == 429:
                raise AdapterError("yahoo_finance rate limited", kind=Outcome.RATE_LIMITED)
            if resp.status_code == 403:
                raise AdapterError("yahoo_finance forbidden", kind=Outcome.BLOCKED, retryable=False)
            if resp.status_code != 200:
                raise AdapterError(
                    f"yahoo_finance HTTP {resp.status_code}",
                    kind=Outcome.UPSTREAM_ERROR,
                    retryable=False,
                )
            payload = resp.json()
        except httpx.PoolTimeout as exc:
            raise AdapterError("yahoo_finance pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"yahoo_finance timeout after {timeout_s}s", kind=Outcome.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"yahoo_finance transport error: {type(exc).__name__}", kind=Outcome.UPSTREAM_ERROR
            ) from exc
        except ValueError as exc:
            raise AdapterError("yahoo_finance returned non-JSON body") from exc

        quotes = payload.get("quotes") or [] if isinstance(payload, dict) else []
        news_items = payload.get("news") or [] if isinstance(payload, dict) else []
        results: list[RawResult] = []

        # 1. Process Quotes (Equities, ETFs, Indices, Crypto)
        primary_symbols: list[str] = []
        for q in quotes:
            if not isinstance(q, dict):
                continue
            sym = q.get("symbol")
            if sym:
                primary_symbols.append(sym)

        # Attempt to fetch price & technical chart info for primary symbols
        price_meta: dict[str, dict] = {}
        price_tech: dict[str, str] = {}
        if primary_symbols:
            top_sym = primary_symbols[0]
            try:
                c_resp = await client.get(
                    f"{self.chart_url}/{top_sym}",
                    params={"interval": "1d", "range": "3mo"},
                    headers=headers,
                    follow_redirects=True,
                    timeout=min(timeout_s, 2.5),
                )
                if c_resp.status_code == 200:
                    c_data = c_resp.json()
                    chart_res = c_data.get("chart", {}).get("result") or []
                    if chart_res:
                        meta = chart_res[0].get("meta") or {}
                        price_meta[top_sym] = meta
                        indicators = chart_res[0].get("indicators") or {}
                        quote_list = indicators.get("quote") or [{}]
                        q_item = quote_list[0] if quote_list else {}
                        closes = q_item.get("close") or []
                        vols = q_item.get("volume") or []
                        highs = q_item.get("high") or []
                        lows = q_item.get("low") or []
                        cur_price = meta.get("regularMarketPrice")
                        tech_str = _calc_technical_summary(
                            closes, vols, cur_price, highs=highs, lows=lows
                        )
                        if tech_str:
                            price_tech[top_sym] = tech_str
            except Exception as exc:
                logger.debug("Yahoo Finance chart enrichment skipped: %s", exc)

        for q in quotes:
            if not isinstance(q, dict):
                continue
            symbol = q.get("symbol")
            if not symbol:
                continue

            name = q.get("longname") or q.get("shortname") or symbol
            exchange = q.get("exchDisp") or q.get("exchange") or "Market"
            quote_type = q.get("typeDisp") or q.get("quoteType") or "Equity"
            sector = q.get("sectorDisp") or q.get("sector") or ""
            industry = q.get("industryDisp") or q.get("industry") or ""

            url = f"https://finance.yahoo.com/quote/{symbol}/"

            # Check if we have live chart price metadata for this symbol
            meta = price_meta.get(symbol)
            if meta:
                price = meta.get("regularMarketPrice")
                currency = meta.get("currency") or "USD"
                prev_close = meta.get("chartPreviousClose")
                hi_52 = meta.get("fiftyTwoWeekHigh")
                lo_52 = meta.get("fiftyTwoWeekLow")

                price_str = f"{price:,.2f} {currency}" if price is not None else "N/A"
                change_str = ""
                if price is not None and prev_close:
                    diff = price - prev_close
                    pct = (diff / prev_close) * 100
                    sign = "+" if diff >= 0 else ""
                    change_str = f" ({sign}{pct:.2f}%)"

                range_str = (
                    f" · 52w Range: {lo_52:,.2f} - {hi_52:,.2f}"
                    if lo_52 is not None and hi_52 is not None
                    else ""
                )
                tech_str = price_tech.get(symbol, "")

                snippet = (
                    f"[{symbol} · {quote_type}] Price: {price_str}{change_str}{range_str}"
                    f"{tech_str} · Exch: {exchange} · Sector: {sector or industry or 'General'}"
                )
            else:
                extra_sec = f" · Sector: {sector}" if sector else ""
                snippet = (
                    f"[{symbol} · {quote_type}] {name} · Exchange: {exchange}{extra_sec}".strip()
                )

            market_time = None
            raw_time = q.get("regularMarketTime")
            if raw_time:
                try:
                    market_time = datetime.fromtimestamp(raw_time, tz=UTC)
                except (ValueError, OSError):
                    market_time = None

            results.append(
                RawResult(
                    url=url,
                    title=f"{symbol} - {name} ({exchange})",
                    snippet=snippet,
                    rank=len(results) + 1,
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=market_time,
                    source_url=url,
                    source_name="Yahoo Finance",
                    as_of=market_time,
                    data_kind="observed",
                )
            )

        cutoff_news_ts = (
            datetime.now(UTC).timestamp() - max(1, int(freshness_days)) * 86400
            if freshness_days
            else None
        )
        seen_urls = {r.url for r in results}
        # 2. Process Financial News
        for item in news_items:
            if not isinstance(item, dict):
                continue
            link = item.get("link")
            if not link or not link.startswith("http") or link in seen_urls:
                continue

            title = _strip_html(item.get("title") or "")
            if not title:
                continue

            publisher = item.get("publisher") or "Market News"
            related = ", ".join(item.get("relatedTickers") or [])
            related_str = f" · Related: {related}" if related else ""

            snippet = f"[{publisher}] {title}{related_str}"

            published_at = None
            pub_time = item.get("providerPublishTime")
            if pub_time:
                try:
                    published_at = datetime.fromtimestamp(pub_time, tz=UTC)
                except (ValueError, OSError):
                    published_at = None
            if (
                cutoff_news_ts is not None
                and published_at is not None
                and published_at.timestamp() < cutoff_news_ts
            ):
                continue

            seen_urls.add(link)
            results.append(
                RawResult(
                    url=link,
                    title=title,
                    snippet=snippet,
                    rank=len(results) + 1,
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=published_at,
                    source_url=link,
                    source_name=publisher,
                    as_of=published_at,
                    data_kind="observed",
                )
            )

        rss_sym = sym_hint or (primary_symbols[0] if primary_symbols else None)
        if rss_sym and len(results) < max_results and self._client is None:
            rss_hits = await self._fetch_ticker_rss(
                client,
                rss_sym,
                freshness_days=freshness_days,
                max_items=max(2, max_results - len(results)),
                timeout_s=timeout_s,
            )
            for rh in rss_hits:
                if rh.url not in seen_urls:
                    seen_urls.add(rh.url)
                    rh.rank = len(results) + 1
                    results.append(rh)

        return results[:max_results]
