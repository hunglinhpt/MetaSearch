"""viet_stock search adapter.

Dedicated search engine adapter for Vietnamese equities on HSX and HNX.
Fetches real daily OHLCV candles from VNDIRECT TradingView dchart API and
grounded fundamentals from Simplize API.

Strict Data Integrity (WP-09 / DAT-01..DAT-04):
- 0% synthetic or sin/cos prices.
- 0% hardcoded static databases in runtime.
- Never invents accounting ratios or forensic scores without audited statements.
- Exact provenance (source_url, source_name, as_of, data_kind) on every result.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.http import get_http_client
from app.retrieval.fa_sources import (
    MAX_RESPONSE_BYTES,
    AggregatedFinancials,
    FAAggregator,
    SourceOHLCV,
    build_default_aggregator,
)
from app.retrieval.fa_sources.base import ensure_symbol
from app.retrieval.financial_forensics import (
    analyze_balance_sheet_forensics,
)
from app.retrieval.models import RawResult
from app.retrieval.technical_analysis import (
    calc_fibonacci_retracement,
    calc_geometric_trendlines,
    calc_ichimoku_cloud,
    compute_custom_indicators,
    detect_chart_patterns,
    parse_custom_indicator_params,
)
from app.retrieval.vn_tickers import (
    _VIET_STOCK_STOP_WORDS,
    is_vn_ticker,
    load_vn_tickers,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CompanyFinancials:
    """Core financial and operational indicators for a listed Vietnamese company.

    Fields are Optional (None) when not directly grounded in authoritative disclosures,
    preventing any synthetic or hallucinated ratios (WP-09 / DAT-01..DAT-04).
    """

    symbol: str
    name: str
    exchange: str
    industry: str
    market_cap_vnd_bil: float
    charter_capital_vnd_bil: float | None = None
    shares_outstanding_mil: float | None = None
    price_ref_vnd: float = 0.0
    pe: float | None = None
    pb: float | None = None
    eps_vnd: float | None = None
    roe_pct: float | None = None
    roa_pct: float | None = None
    debt_to_equity: float | None = None
    foreign_room_pct: float | None = None
    foreign_current_pct: float | None = None
    plan_year: int | None = None
    target_revenue_vnd_bil: float | None = None
    actual_revenue_vnd_bil: float | None = None
    target_profit_vnd_bil: float | None = None
    actual_profit_vnd_bil: float | None = None
    revenue_completion_pct: float | None = None
    profit_completion_pct: float | None = None
    yoy_profit_growth_pct: float | None = None
    plan_status: str | None = None
    dividend_policy: str | None = None
    recent_dividends: list[dict[str, Any]] | None = None
    major_shareholders: list[dict[str, Any]] | None = None
    free_float_pct: float | None = None
    recent_deals: list[dict[str, Any]] | None = None
    block_trade_summary: str | None = None
    restructuring_events: list[dict[str, Any]] | None = None
    auditor: str | None = None
    audit_opinion: str | None = None
    core_catalysts: str | None = None
    ir_url: str | None = None
    has_full_statements: bool = False


_TICKER_REGEX = re.compile(r"\b([A-Z0-9]{3})\b")

_QUERY_KEYWORD_MAP: dict[str, str] = {
    "hòa phát": "HPG",
    "hoa phat": "HPG",
    "fpt telecom": "FPT",
    "tập đoàn fpt": "FPT",
    "vinamilk": "VNM",
    "vietcombank": "VCB",
    "ngân hàng quân đội": "MBB",
    "mbbank": "MBB",
    "techcombank": "TCB",
    "vingroup": "VIC",
    "vinhomes": "VHM",
    "vinpearl": "VIC",
    "thế giới di động": "MWG",
    "the gioi di dong": "MWG",
    "bách hóa xanh": "MWG",
    "chứng khoán ssi": "SSI",
    "masan": "MSN",
    "hóa chất đức giang": "DGC",
    "đức giang": "DGC",
    "dầu khí việt nam": "PVS",
    "tổng cty ptsc": "PVS",
    "ptsc": "PVS",
    "idico": "IDC",
    "kinh bắc": "KBC",
    "sacombank": "STB",
    "vietinbank": "CTG",
    "bidv": "BID",
    "vndirect": "VND",
    "chứng khoán bản việt": "VCI",
    "vietcap": "VCI",
    "địa ốc hoàng quân": "HQC",
    "hoàng quân": "HQC",
    "vnindex": "VNINDEX",
    "vn-index": "VNINDEX",
    "vn30": "VN30",
    "hnx-index": "HNX",
    "hnx": "HNX",
    "upcom": "UPCOM",
}


def _extract_tickers(query: str) -> list[str]:
    """Finds referenced Vietnamese stock tickers and indices using strict boundaries

    and authoritative 1518 ticker registry (DAT-06).
    """
    q_lower = query.lower()
    detected: list[str] = []

    # 1. Match company names, indices, and mapped keywords
    for kw in sorted(_QUERY_KEYWORD_MAP.keys(), key=len, reverse=True):
        sym = _QUERY_KEYWORD_MAP[kw]
        if sym not in detected and re.search(rf"\b{re.escape(kw)}\b", q_lower):
            detected.append(sym)

    # 2. Match tickers with explicit prefix: $FPT, mã HPG, cổ phiếu VNM, cp SSI, mã HQC
    explicit_pat = re.compile(
        r"(?:\$|(?:mã|cổ phiếu|cp|stock)\s+)([A-Za-z0-9_]{3,8})", re.IGNORECASE
    )
    for m in explicit_pat.finditer(query):
        sym = m.group(1).upper()
        if (
            sym not in _VIET_STOCK_STOP_WORDS
            and sym not in detected
            and (is_vn_ticker(sym) or sym in ("VNINDEX", "VN30", "HNX", "UPCOM"))
        ):
            detected.append(sym)

    # 3. Match 3-character ticker symbols verified against official 1518 ticker registry
    for m in _TICKER_REGEX.finditer(query):
        sym = m.group(1).upper()
        if sym not in _VIET_STOCK_STOP_WORDS and sym not in detected and is_vn_ticker(sym):
            detected.append(sym)

    return detected


async def _fetch_vndirect_ohlcv(
    symbol: str,
    base_url: str = "https://dchart-api.vndirect.com.vn/dchart/history",
    timeout_s: float = 3.5,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[float], list[float], list[float], list[float], list[float], int | None] | None:
    """Fetches real historical daily OHLCV candles from VNDIRECT TradingView dchart API.

    Returns:
        (opens, highs, lows, closes, volumes, last_timestamp) or None.
    """
    sym = symbol.upper()
    now_ts = int(time.time())
    from_ts = now_ts - 240 * 86400  # ~240 days window

    url = f"{base_url}?resolution=D&symbol={sym}&from={from_ts}&to={now_ts}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }

    try:
        http_client = client or get_http_client("search")
        resp = await http_client.get(url, headers=headers, follow_redirects=True, timeout=timeout_s)
        if resp.status_code != 200:
            logger.debug("vndirect_status_%s: %s", resp.status_code, sym)
            return None
        if len(resp.content or b"") > MAX_RESPONSE_BYTES:
            # Same 5MB cap as the FA sources — a hostile/huge body must not
            # stall the event loop inside resp.json().
            logger.debug("vndirect_payload_too_large: %s", sym)
            return None
        series = _parse_dchart_series(resp.json(), sym)
        if series is None:
            return None
        return (
            series["opens"],
            series["highs"],
            series["lows"],
            series["closes"],
            series["volumes"],
            series["last_ts"],
        )
    except Exception as exc:
        logger.debug("vndirect_fetch_failed for %s: %s", sym, exc)
        return None


def _parse_dchart_series(data: Any, sym: str) -> dict[str, Any] | None:
    """Validate + normalize one TradingView dchart payload (shared by all VNDIRECT paths).

    Returns {"times", "opens", "highs", "lows", "closes", "volumes", "last_ts"}
    or None. Never invents values: schema drift, negative/non-finite candles
    and too-short series are refused.
    """
    if not isinstance(data, dict) or data.get("s") != "ok":
        return None

    t_list = data.get("t", [])
    c_list = data.get("c", [])
    o_list = data.get("o", [])
    h_list = data.get("h", [])
    l_list = data.get("l", [])
    v_list = data.get("v", [])

    if not c_list or len(c_list) < 5:
        return None

    # STRICT arrays (same rule as the SSI source): a missing or mismatched
    # o/h/l/v is schema drift — refuse the series rather than fabricating
    # closes-for-opens or zero volumes (zero volumes poison OBV/MFI, and
    # since docs/18 B2 this parser also feeds the candlestick chart directly).
    n = len(c_list)
    if any(len(raw) != n for raw in (t_list, o_list, h_list, l_list, v_list)):
        logger.debug("vndirect_series_schema_drift: %s array_length", sym)
        return None

    is_index = sym in ("VNINDEX", "VN30", "HNX", "HNX-INDEX", "UPCOM")
    # Live-probed 2026-11-11: VNDIRECT serves a /1000-scaled feed for stocks
    # (HPG last close 21.25 = 21,250 VND native). The probe is the LAST close:
    # a stock whose price crossed 1,000,000 VND stays unscaled (heuristic
    # threshold documented here — a feed >= 1000 is treated as native VND).
    # The multiplier probe reads the LAST raw close — guard it: a non-numeric
    # tail must refuse the series (return None), never raise out of the parser.
    try:
        last_close = float(c_list[-1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(last_close) or last_close < 0:
        return None
    multiplier = 1000.0 if not is_index and last_close < 1000.0 else 1.0
    if multiplier != 1.0:
        logger.debug("vndirect_price_multiplier_applied: %s x1000", sym)

    def scaled(raw: list, *, is_price: bool) -> list[float] | None:
        # Mirror of the SSI guard on EVERY array: json.loads accepts bare
        # NaN/Infinity, and a non-finite or negative value would poison ratios
        # and charts downstream (or 500 the API via allow_nan=False).
        out: list[float] = []
        for x in raw:
            try:
                v = float(x)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(v) or v < 0:
                return None
            out.append(round(v * multiplier, 2) if is_price else round(v, 0))
        return out

    opens = scaled(o_list, is_price=True)
    highs = scaled(h_list, is_price=True)
    lows = scaled(l_list, is_price=True)
    closes = scaled(c_list, is_price=True)
    volumes = scaled(v_list, is_price=False)
    if opens is None or highs is None or lows is None or closes is None or volumes is None:
        logger.debug("vndirect_bad_value: %s non_finite_or_negative", sym)
        return None
    try:
        times = [int(t) for t in t_list]
    except (TypeError, ValueError):
        return None
    # Audit round 12: absurd timestamps overflow datetime.fromtimestamp
    # downstream (OSError → 500 on /v1/market/ohlcv) — bound to sane years.
    if any(t <= 0 or t > 4102444800 for t in times):  # ≤ 2100-01-01
        logger.debug("vndirect_ts_out_of_range: %s", sym)
        return None
    last_ts = times[-1] if times else None
    if any(times[i] >= times[i + 1] for i in range(len(times) - 1)):
        # Charts require strictly ascending time; duplicate/backwards
        # timestamps would corrupt every candle's day.
        logger.debug("vndirect_series_schema_drift: %s times_not_ascending", sym)
        return None

    return {
        "times": times,
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "volumes": volumes,
        "last_ts": last_ts,
    }


async def fetch_vndirect_ohlcv_series(
    symbol: str,
    *,
    days: int = 90,
    base_url: str = "https://dchart-api.vndirect.com.vn/dchart/history",
    timeout_s: float = 3.5,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Full daily OHLCV series with per-candle timestamps (chart endpoint, docs/18 B2).

    Same feed, guards and /1000 multiplier as the search path — one parser
    (``_parse_dchart_series``), so the chart can never diverge from snippets.
    Returns the normalized series dict, or None on any fault.
    """
    # Defense in depth: the symbol is interpolated into the URL query, so it
    # must pass the same plain-code guard as every other adapter path even
    # though the only caller (market.py) already validates it.
    sym = ensure_symbol(symbol)
    if sym is None:
        return None
    now_ts = int(time.time())
    from_ts = now_ts - max(1, days) * 86400
    url = f"{base_url}?resolution=D&symbol={sym}&from={from_ts}&to={now_ts}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    try:
        http_client = client or get_http_client("search")
        resp = await http_client.get(url, headers=headers, follow_redirects=True, timeout=timeout_s)
        if resp.status_code != 200:
            logger.debug("vndirect_status_%s: %s", resp.status_code, sym)
            return None
        if len(resp.content or b"") > MAX_RESPONSE_BYTES:
            logger.debug("vndirect_payload_too_large: %s", sym)
            return None
        return _parse_dchart_series(resp.json(), sym)
    except Exception as exc:
        logger.debug("vndirect_fetch_failed for %s: %s", sym, exc)
        return None


async def _fetch_simplize_summary(
    symbol: str,
    base_url: str = "https://api.simplize.vn/api/company/summary",
    timeout_s: float = 3.5,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Fetches real fundamentals and company overview from Simplize API."""
    sym = symbol.upper()
    url = f"{base_url}/{sym}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    try:
        http_client = client or get_http_client("search")
        resp = await http_client.get(url, headers=headers, follow_redirects=True, timeout=timeout_s)
        if resp.status_code != 200:
            logger.debug("simplize_status_%s: %s", resp.status_code, sym)
            return None
        if len(resp.content or b"") > MAX_RESPONSE_BYTES:
            logger.debug("simplize_payload_too_large: %s", sym)
            return None
        body = resp.json()
        if (
            isinstance(body, dict)
            and body.get("status") == 200
            and isinstance(body.get("data"), dict)
        ):
            return body["data"]
        return None
    except Exception as exc:
        logger.debug("simplize_fetch_failed for %s: %s", sym, exc)
        return None


def _build_dynamic_company_financials(
    sym: str,
    simp: dict[str, Any],
    latest_close: float = 0.0,
) -> CompanyFinancials:
    """Builds a grounded CompanyFinancials instance dynamically from live Simplize data.

    Fields without authoritative disclosures remain None (WP-09 / DAT-01..DAT-04).
    """
    mc_raw = simp.get("marketCap")
    mc_vnd_bil = float(mc_raw) / 1e9 if mc_raw is not None else 0.0

    pe_raw = simp.get("peRatio")
    pe = float(pe_raw) if pe_raw is not None else None

    pb_raw = simp.get("pbRatio")
    pb = float(pb_raw) if pb_raw is not None else None

    eps_raw = simp.get("epsRatio")
    eps = float(eps_raw) if eps_raw is not None else None

    roe_raw = simp.get("roe")
    roe = float(roe_raw) if roe_raw is not None else None

    roa_raw = simp.get("roa")
    roa = float(roa_raw) if roa_raw is not None else None

    sh_raw = simp.get("outstandingSharesValue")
    sh_out = float(sh_raw) / 1e6 if sh_raw is not None else None

    p_ref = float(simp.get("priceClose") or simp.get("priceReferrance") or latest_close or 0.0)
    ind = str(
        simp.get("industryActivity") or simp.get("bcEconomicSectorName") or "Doanh nghiệp niêm yết"
    )
    name = str(simp.get("nameVi") or simp.get("name") or f"Công ty Cổ phần {sym}")
    exch = str(simp.get("stockExchange") or "HSX")

    ff_raw = simp.get("freeFloatRate")
    ff_rate = float(ff_raw) if ff_raw is not None else None

    div_raw = simp.get("dividendYieldCurrent")
    div_yield = float(div_raw) if div_raw is not None else None

    growth_raw = simp.get("netIncome5yGrowth")
    growth_5y = float(growth_raw) if growth_raw is not None else None

    core_cat = f"{ind}"
    if pe is not None and pb is not None:
        core_cat += f", định giá P/E {pe:.1f}x, P/B {pb:.2f}x."

    return CompanyFinancials(
        symbol=sym,
        name=name,
        exchange=exch,
        industry=ind,
        market_cap_vnd_bil=round(mc_vnd_bil, 1),
        charter_capital_vnd_bil=round(sh_out * 10.0, 1) if sh_out is not None else None,
        shares_outstanding_mil=round(sh_out, 1) if sh_out is not None else None,
        price_ref_vnd=p_ref,
        pe=round(pe, 2) if pe is not None else None,
        pb=round(pb, 2) if pb is not None else None,
        eps_vnd=round(eps, 0) if eps is not None else None,
        roe_pct=round(roe, 2) if roe is not None else None,
        roa_pct=round(roa, 2) if roa is not None else None,
        debt_to_equity=None,  # Not present in summary payload
        foreign_room_pct=None,
        foreign_current_pct=None,
        plan_year=None,
        target_revenue_vnd_bil=None,
        actual_revenue_vnd_bil=None,
        target_profit_vnd_bil=None,
        actual_profit_vnd_bil=None,
        revenue_completion_pct=None,
        profit_completion_pct=None,
        yoy_profit_growth_pct=round(growth_5y, 1) if growth_5y is not None else None,
        plan_status=None,
        dividend_policy=(f"Tỷ suất cổ tức hiện tại: {div_yield:.1f}%" if div_yield else None),
        recent_dividends=None,
        major_shareholders=None,
        free_float_pct=ff_rate,
        recent_deals=None,
        block_trade_summary=None,
        restructuring_events=None,
        auditor=None,
        audit_opinion=None,
        core_catalysts=core_cat,
        ir_url=str(simp.get("website") or f"https://simplize.vn/co-phieu/{sym}"),
        has_full_statements=False,
    )


def _build_financials_from_aggregated(
    sym: str,
    agg: AggregatedFinancials,
    latest_close: float = 0.0,
) -> CompanyFinancials | None:
    """Builds a grounded CompanyFinancials from multi-source aggregated fields.

    docs/15 §8.2: name/exchange come from the DAT-06 ticker registry (real
    records, never invented); payloads without provenance URLs are refused;
    forensics stays off (has_full_statements=False) because scattered ratios
    are not audited statements.
    """
    if not agg.fields or not agg.source_urls:
        return None

    rec = load_vn_tickers().get(sym) or {}
    name = str(rec.get("company_name") or "").strip() or f"Công ty Cổ phần {sym}"
    exchange = str(rec.get("exchange") or "").strip() or "VN"

    g = agg.get
    pe = g("pe")
    pb = g("pb")
    eps = g("eps_vnd")
    roe = g("roe_pct")
    roa = g("roa_pct")
    de = g("debt_to_equity")
    profit_growth = g("profit_growth_pct")
    revenue_growth = g("revenue_growth_pct")
    div_yield = g("dividend_yield_pct")

    # Unit detector built from two OTHER grounded numbers: implied EPS =
    # price / P/E. A >3x disagreement in EITHER direction means the payload's
    # EPS unit is not VND (thousand-VND -> down-100x display, wrong scale ->
    # up-1000x) — drop it with a warning instead of displaying a wrong figure.
    # Pure arithmetic, nothing invented.
    # Scale caveat: latest_close inherits the CANDLE feed's unit (VNDIRECT's
    # /1000-scaled feed is confirmed and x1000-corrected; an unverified
    # fallback feed would skew implied EPS by the same factor — the gate errs
    # toward dropping, never fabricating).
    if eps is not None and pe is not None and pe > 0 and latest_close > 0:
        implied_eps = latest_close / pe
        if implied_eps > 0:
            eps_ratio = max(eps, implied_eps) / min(eps, implied_eps)
            if eps_ratio > 3.0:
                # ONLY a real disagreement drops the field — consistent data
                # (ratio <= 3) must survive (audit round 10 P2: the previous
                # indentation dropped eps whenever pe+close existed).
                logger.warning(
                    "fa_suspect_unit: %s eps_vnd=%s vs implied=%.1f — dropped",
                    sym,
                    eps,
                    implied_eps,
                )
                eps = None

    n_validated = sum(1 for fv in agg.fields.values() if fv.validated)
    # Advertised metric count excludes the inert FireAnt balance-sheet anchors
    # (total_assets/equity/debt merge for provenance but render nothing).
    n_metrics = sum(1 for k in agg.fields if not k.startswith("total_"))
    n_sources = len(agg.sources_used)
    source_label = (
        f"Nguồn FA đa nguồn: {', '.join(agg.sources_used)}"
        if n_sources >= 2
        else f"Nguồn FA: {', '.join(agg.sources_used)} (đơn nguồn)"
    )
    cross_label = (
        f"{n_validated} chỉ số đối chiếu chéo"
        if n_validated > 0
        else "chưa đối chiếu chéo (mỗi chỉ số từ một nguồn)"
    )
    per_source_dates = sorted(
        {(fv.source, fv.as_of) for fv in agg.fields.values() if fv.as_of is not None},
        key=lambda pair: pair[0],
    )
    dates_label = (
        " · mốc dữ liệu: "
        + "; ".join(f"{src} {dt.date().isoformat()}" for src, dt in per_source_dates)
        if per_source_dates
        else ""
    )
    catalysts = f"{source_label} · {n_metrics} chỉ số có mốc nguồn, {cross_label}.{dates_label}"
    if pe is not None and pb is not None:
        catalysts += f" Định giá P/E {pe:.1f}x, P/B {pb:.2f}x."
    if profit_growth is not None:
        catalysts += f" Tăng trưởng LNNT: {profit_growth:.1f}%."
    if revenue_growth is not None:
        catalysts += f" Tăng trưởng doanh thu: {revenue_growth:.1f}%."

    return CompanyFinancials(
        symbol=sym,
        name=name,
        exchange=exchange,
        industry="Doanh nghiệp niêm yết",  # registry has no industry column
        market_cap_vnd_bil=round(g("market_cap_vnd_bil") or 0.0, 1),
        price_ref_vnd=latest_close,
        pe=round(pe, 2) if pe is not None else None,
        pb=round(pb, 2) if pb is not None else None,
        eps_vnd=round(eps, 0) if eps is not None else None,
        roe_pct=round(roe, 2) if roe is not None else None,
        roa_pct=round(roa, 2) if roa is not None else None,
        debt_to_equity=round(de, 2) if de is not None else None,
        free_float_pct=g("free_float_pct"),
        yoy_profit_growth_pct=(round(profit_growth, 1) if profit_growth is not None else None),
        dividend_policy=(f"Tỷ suất cổ tức hiện tại: {div_yield:.1f}%" if div_yield else None),
        core_catalysts=catalysts,
        ir_url=None,  # no grounded IR page in this path — None beats invention
        has_full_statements=False,
    )


def _ohlcv_tuple(
    src: SourceOHLCV,
) -> tuple[list[float], list[float], list[float], list[float], list[float], int | None]:
    """Adapt a SourceOHLCV to the legacy (o, h, l, c, v, last_ts) tuple shape."""
    return (src.opens, src.highs, src.lows, src.closes, src.volumes, src.last_ts)


class VietStockAdapter:
    """Dedicated search engine adapter for Vietnamese equities on HSX and HNX."""

    id = "viet_stock"

    def __init__(
        self,
        vndirect_url: str = "https://dchart-api.vndirect.com.vn/dchart/history",
        simplize_url: str = "https://api.simplize.vn/api/company/summary",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.vndirect_url = vndirect_url
        self.simplize_url = simplize_url
        self._client = client
        self._fa_agg: FAAggregator | None = None

    def _fa_aggregator(self) -> FAAggregator:
        """Multi-source FA fallback, built once per adapter.

        The adapter is instantiated once per registry build, so per-source
        breakers (fa:ssi/tcbs/fireant) persist across requests just like
        engine-level breakers.
        """
        if self._fa_agg is None:
            self._fa_agg = build_default_aggregator(client=self._client)
        return self._fa_agg

    async def _fetch_ohlcv_fallback(self, sym: str, timeout_s: float) -> SourceOHLCV | None:
        """VNDIRECT down -> fan out to direct OHLCV sources (SSI first)."""
        try:
            src = await self._fa_aggregator().fetch_ohlcv(sym, timeout_s=timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("fa_ohlcv_fallback_failed for %s: %s", sym, exc)
            return None
        return src

    async def _fetch_financials_fallback(
        self, sym: str, timeout_s: float
    ) -> AggregatedFinancials | None:
        """Simplize down -> cross-source fan-out (TCBS, FireAnt, ...)."""
        try:
            agg = await self._fa_aggregator().fetch_financials(sym, timeout_s=timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("fa_financials_fallback_failed for %s: %s", sym, exc)
            return None
        if agg is None or not agg.fields:
            return None
        return agg

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 3.5,
        **kwargs: Any,
    ) -> list[RawResult]:
        clean_q = query.strip()
        if not clean_q:
            return []

        tickers = _extract_tickers(clean_q)
        q_lower = clean_q.lower()
        custom_params = parse_custom_indicator_params(clean_q)

        # Strict: Do NOT hijack generic market queries if no specific ticker or index was requested!
        if not tickers:
            return []

        results: list[RawResult] = []
        rank = 1

        for sym in tickers[:4]:
            # Fetch real candles from VNDIRECT and fundamentals from Simplize concurrently
            primary_t0 = time.monotonic()
            vnd_task = _fetch_vndirect_ohlcv(
                sym,
                base_url=self.vndirect_url,
                timeout_s=timeout_s,
                client=self._client,
            )
            simp_task = _fetch_simplize_summary(
                sym,
                base_url=self.simplize_url,
                timeout_s=timeout_s,
                client=self._client,
            )
            ohlcv_res, simp_data = await asyncio.gather(vnd_task, simp_task, return_exceptions=True)
            primary_elapsed = time.monotonic() - primary_t0

            if isinstance(ohlcv_res, Exception):
                logger.debug("vndirect_gather_exception: %s", ohlcv_res)
                ohlcv_res = None
            if isinstance(simp_data, Exception):
                logger.debug("simplize_gather_exception: %s", simp_data)
                simp_data = None

            # Provenance for every candle served: primary VNDIRECT, else the
            # winning fallback source (docs/15 §8.2). Defaults MUST come before
            # the fallback block, which overrides them on success.
            ohlcv_source_name = "VNDIRECT"
            ohlcv_url = f"https://dchart.vndirect.com.vn/?symbol={sym}"

            # Multi-source fallback (docs/15 §8.2): a dead primary leg fans
            # out to the direct FA sources CONCURRENTLY — never two sequential
            # round-trips on the request budget. The fallback only gets the
            # REMAINING leg budget (primaries already spent part of the 3.5s);
            # a fallback that cannot start inside it is skipped entirely, so
            # the pipeline's fan-out cancel never discards a late fallback
            # result or counts the engine as TIMEOUT. Indices only need OHLCV.
            is_index = sym in ("VNINDEX", "VN30", "HNX", "HNX-INDEX", "UPCOM")
            agg_fin: AggregatedFinancials | None = None
            need_ohlcv_fb = ohlcv_res is None
            need_fa_fb = simp_data is None and not is_index
            remaining = timeout_s - primary_elapsed
            if need_ohlcv_fb or need_fa_fb:
                if remaining < 0.8:
                    # 0.8s floor (breaker fairness): a thinner window would
                    # nearly always time out a HEALTHY fallback source and
                    # record a non-representative TIMEOUT against its breaker
                    # (audits N3 / SA+Finance round 2).
                    logger.debug(
                        "fa_fallback_skipped_no_budget: %s remaining=%.2fs", sym, remaining
                    )
                else:
                    fb_timeout = min(timeout_s, remaining - 0.15)
                    fallback_coros: dict[str, Any] = {}
                    if need_ohlcv_fb:
                        fallback_coros["ohlcv"] = self._fetch_ohlcv_fallback(sym, fb_timeout)
                    if need_fa_fb:
                        fallback_coros["fa"] = self._fetch_financials_fallback(sym, fb_timeout)
                    keys = list(fallback_coros)
                    values = await asyncio.gather(*fallback_coros.values())
                    for key, value in zip(keys, values, strict=True):
                        if key == "ohlcv" and value is not None:
                            ohlcv_res = _ohlcv_tuple(value)
                            ohlcv_source_name = value.source_id.upper()
                            ohlcv_url = value.source_url
                        elif key == "fa" and value is not None:
                            agg_fin = value

            # Feed cross-check (log-only, both sides grounded): VNDIRECT's
            # x1000 heuristic vs Simplize's reference price must agree within
            # 50% — a wider gap means one feed changed its scale.
            if ohlcv_res and simp_data:
                simp_price = simp_data.get("priceClose") or simp_data.get("priceReferrance")
                if simp_price:
                    last_close = ohlcv_res[3][-1]
                    try:
                        gap = abs(last_close - float(simp_price)) / float(simp_price)
                    except (TypeError, ValueError, ZeroDivisionError):
                        gap = 0.0
                    if gap > 0.5:
                        logger.warning(
                            "fa_price_mismatch: %s candle_last=%s simplize_price=%s",
                            sym,
                            last_close,
                            simp_price,
                        )

            # 1. Handle Market Index (e.g. VNINDEX, VN30, HNX)
            if is_index:
                if ohlcv_res:
                    opens, highs, lows, closes, volumes, last_ts = ohlcv_res
                    idx_pub_dt = datetime.fromtimestamp(last_ts, tz=UTC) if last_ts else None
                    curr_val = closes[-1]
                    prev_val = closes[-2] if len(closes) >= 2 else curr_val
                    pct_chg = ((curr_val - prev_val) / prev_val) * 100 if prev_val else 0.0
                    sign = "+" if pct_chg >= 0 else ""

                    fibo = calc_fibonacci_retracement(highs[-90:], lows[-90:], curr_val)
                    ichimoku = calc_ichimoku_cloud(highs, lows, closes)
                    trendline = calc_geometric_trendlines(highs, lows, closes)
                    patterns = detect_chart_patterns(highs, lows, closes, volumes, opens=opens)
                    customs = compute_custom_indicators(
                        closes, volumes, highs=highs, lows=lows, **custom_params
                    )
                    pat_name = patterns[0].name if patterns else "Không phát hiện mẫu hình"

                    date_str = idx_pub_dt.strftime("%Y-%m-%d") if idx_pub_dt else "EOD"
                    idx_snippet = (
                        f"[{sym} · Real Market Index OHLCV · {date_str}] "
                        f"Điểm số: {curr_val:,.2f} ({sign}{pct_chg:.2f}%) · "
                        f"Xu hướng kênh: {trendline.trend_type} ({trendline.channel_slope_deg}°, "
                        f"R²={trendline.r_squared:.2f}) · "
                        f"Fibo Retracement: Hỗ trợ {fibo.nearest_support:,.2f}, "
                        f"Kháng cự {fibo.nearest_resistance:,.2f}, "
                        f"Golden Pocket (61.8%-65.0%): {fibo.golden_pocket_range} · "
                        f"Mây Ichimoku: {ichimoku.cloud_state} ({ichimoku.tk_cross}) · "
                        f"Mẫu hình: {pat_name}."
                    )
                    results.append(
                        RawResult(
                            url=ohlcv_url,
                            title=(
                                f"Chỉ Số {sym} - Dữ Liệu Giao Dịch & Phân Tích Kỹ Thuật Thị Trường"
                            ),
                            snippet=idx_snippet,
                            rank=rank,
                            adapter=self.id,
                            source_engines=[self.id],
                            published_at=idx_pub_dt,
                            source_url=ohlcv_url,
                            source_name=ohlcv_source_name,
                            as_of=idx_pub_dt,
                            data_kind="observed",
                        )
                    )
                    rank += 1
                continue

            # 2. Check if stock can be dynamically built from live data
            stock: CompanyFinancials | None = None
            latest_c = ohlcv_res[3][-1] if ohlcv_res else 0.0
            if simp_data:
                stock = _build_dynamic_company_financials(sym, simp_data, latest_c)
            elif agg_fin is not None:
                stock = _build_financials_from_aggregated(sym, agg_fin, latest_c)

            if not stock and not ohlcv_res:
                # No grounded data available -> fail gracefully without synthesizing fake data
                continue

            # Determine timestamps with strict grounding
            candle_pub_dt: datetime | None = None
            if ohlcv_res and ohlcv_res[5]:
                candle_pub_dt = datetime.fromtimestamp(ohlcv_res[5], tz=UTC)

            fa_pub_dt: datetime | None = None
            if simp_data and simp_data.get("analysisUpdated"):
                try:
                    fa_pub_dt = datetime.strptime(
                        str(simp_data["analysisUpdated"]).strip(), "%d/%m/%Y"
                    ).replace(tzinfo=UTC)
                except Exception:
                    fa_pub_dt = None
            if not fa_pub_dt:
                fa_pub_dt = candle_pub_dt
            # Aggregated path: published_at and as_of are DIFFERENT claims —
            # publish-side date when a payload carries one (never the period
            # end), period-end anchors as_of. When no publish-side key exists,
            # published_at keeps the grounded CANDLE date (a real market
            # timestamp, clearly not the period end) rather than None.
            fa_as_of = fa_pub_dt
            if agg_fin is not None:
                if agg_fin.published_at is not None:
                    fa_pub_dt = agg_fin.published_at
                if agg_fin.as_of is not None:
                    fa_as_of = agg_fin.as_of

            # Provenance-aware result metadata: Simplize when it served, else
            # the winning FA source(s) (docs/15 §8.2).
            fa_url = f"https://simplize.vn/co-phieu/{sym}"
            fa_source_name = "Simplize"
            if agg_fin is not None:
                if agg_fin.source_urls:
                    fa_url = agg_fin.source_urls[0]
                fa_source_name = "FA-Aggregator(" + "+".join(agg_fin.sources_used) + ")"

            # Intent classification
            is_forensic = any(
                w in q_lower
                for w in (
                    "pháp y",
                    "bản chất",
                    "phải thu",
                    "cấu trúc nợ",
                    "nợ vay",
                    "nợ ròng",
                    "dso",
                    "dòng tiền",
                    "cfo",
                    "f-score",
                    "z-score",
                    "m-score",
                    "camels",
                    "car",
                    "npl",
                    "casa",
                    "nim",
                    "margin",
                    "thao túng",
                    "thanh toán",
                )
            )
            is_tech_adv = any(
                w in q_lower
                for w in (
                    "fibonacci",
                    "fibo",
                    "ichimoku",
                    "mây",
                    "mẫu hình",
                    "hai đáy",
                    "hai đỉnh",
                    "cốc tay cầm",
                    "trendline",
                    "hình học",
                    "pattern",
                    "kỹ thuật",
                    "rsi",
                    "macd",
                    "bollinger",
                    "atr",
                    "stoch",
                )
            )

            # 1. Primary Comprehensive FA Profile Result (only if grounded stock data exists)
            if stock:
                fa_parts = [f"[{stock.exchange}:{stock.symbol} · {stock.industry}]"]
                if stock.market_cap_vnd_bil > 0:
                    fa_parts.append(f"Vốn hóa: {stock.market_cap_vnd_bil:,.0f} tỷ đ")
                if stock.pe is not None:
                    fa_parts.append(f"P/E: {stock.pe:.1f}")
                if stock.pb is not None:
                    fa_parts.append(f"P/B: {stock.pb:.2f}")
                if stock.eps_vnd is not None:
                    fa_parts.append(f"EPS: {stock.eps_vnd:,.0f} đ")
                if stock.roe_pct is not None:
                    fa_parts.append(f"ROE: {stock.roe_pct:.1f}%")
                if stock.roa_pct is not None:
                    fa_parts.append(f"ROA: {stock.roa_pct:.1f}%")
                if stock.debt_to_equity is not None:
                    fa_parts.append(f"D/E: {stock.debt_to_equity:.2f}")
                if stock.free_float_pct is not None:
                    fa_parts.append(f"Free-float: {stock.free_float_pct:.1f}%")
                if stock.dividend_policy:
                    fa_parts.append(stock.dividend_policy)
                if stock.core_catalysts:
                    fa_parts.append(stock.core_catalysts)

                main_snippet = " · ".join(fa_parts)

                results.append(
                    RawResult(
                        url=fa_url,
                        title=(
                            f"[{stock.exchange}:{stock.symbol}] {stock.name} - "
                            "Báo Cáo Phân Tích Cơ Bản (FA) & Chỉ Số Định Giá"
                        ),
                        snippet=main_snippet,
                        rank=rank,
                        adapter=self.id,
                        source_engines=[self.id],
                        published_at=fa_pub_dt,
                        source_url=fa_url,
                        source_name=fa_source_name,
                        as_of=fa_as_of,
                        data_kind="observed",
                    )
                )
                rank += 1

            # 2. Deep Dive: Forensic Inspection (Strict: only if full statements exist)
            if stock and stock.has_full_statements:
                forensic = analyze_balance_sheet_forensics(stock.symbol, stock)
                if forensic and (is_forensic or len(tickers) <= 2):
                    sector_extra = ""
                    if forensic.industry_type == "banking" and forensic.car_pct > 0:
                        sector_extra = (
                            f"CAMELS Ngân hàng: CAR {forensic.car_pct:.1f}% · "
                            f"NPL (Nợ xấu) {forensic.npl_ratio_pct:.2f}% · "
                            f"Nợ nhóm 2 {forensic.group2_loan_ratio_pct:.2f}% · "
                            f"Bao phủ nợ xấu (LLR) {forensic.llr_pct:.0f}% · "
                            f"NIM {forensic.nim_pct:.2f}% · CASA {forensic.casa_pct:.1f}% · "
                        )
                    elif (
                        forensic.industry_type == "securities"
                        and forensic.margin_loans_to_equity_pct > 0
                    ):
                        sector_extra = (
                            f"Pháp y CTCK: Margin/VCSH {forensic.margin_loans_to_equity_pct:.1f}% "
                            f"(trần 200%) · Tự doanh FVTPL {forensic.fvtpl_vnd_bil:,.0f} tỷ đ · "
                            f"HTM {forensic.htm_vnd_bil:,.0f} tỷ đ · "
                            f"An toàn tài chính {forensic.financial_safety_ratio_pct:.0f}% · "
                        )
                    elif (
                        forensic.industry_type == "real_estate"
                        and forensic.unearned_revenue_and_advances_vnd_bil > 0
                    ):
                        sector_extra = (
                            f"Pháp y BĐS/KCN: Người mua trả tiền trước & DT chưa thực hiện "
                            f"{forensic.unearned_revenue_and_advances_vnd_bil:,.0f} tỷ đ · "
                            f"Tồn kho/TTS {forensic.inventory_to_assets_pct:.1f}% · "
                        )

                    forensic_snippet = (
                        f"[{stock.symbol} THẨM ĐỊNH PHÁP Y BCTC - {forensic.forensic_rating}] "
                        f"Piotroski F-Score: {forensic.piotroski_f_score}/9 · "
                        f"Altman Z-Score: {forensic.altman_z_score:.2f} · "
                        f"Beneish M-Score: {forensic.beneish_m_score:.2f} · "
                        f"{sector_extra}"
                        f"Nợ vay ròng: {forensic.net_debt_vnd_bil:,.0f} tỷ đ · "
                        f"Hệ số ICR: {forensic.interest_coverage_ratio:.1f}x · "
                        f"Phải thu: {forensic.short_term_receivables_vnd_bil:,.0f} tỷ đ "
                        f"({forensic.receivables_to_revenue_pct:.1f}% DThu, "
                        f"DSO {forensic.days_sales_outstanding:.1f} ngày) · "
                        f"Dòng tiền CFO: {forensic.operating_cash_flow_vnd_bil:,.0f} tỷ đ "
                        f"({forensic.cfo_to_net_income_ratio:.2f}x LNST). "
                        f"Bản chất: {forensic.forensic_summary}"
                    )
                    results.append(
                        RawResult(
                            url=fa_url,
                            title=(
                                f"{stock.symbol} - Thẩm Định Pháp Y BCTC: Cấu Trúc Nợ, "
                                "Chất Lượng Phải Thu & Dòng Tiền Thật"
                            ),
                            snippet=forensic_snippet,
                            rank=rank,
                            adapter=self.id,
                            source_engines=[self.id],
                            published_at=fa_pub_dt,
                            source_url=fa_url,
                            source_name=fa_source_name,
                            as_of=fa_as_of,
                            data_kind="derived",
                        )
                    )
                    rank += 1

            # 3. Deep Dive: Advanced Technicals on REAL OHLCV data from VNDIRECT
            if ohlcv_res and (is_tech_adv or len(tickers) <= 2):
                opens, highs, lows, closes, volumes, last_ts = ohlcv_res
                real_curr_p = closes[-1]
                prev_p = closes[-2] if len(closes) >= 2 else real_curr_p
                pct_chg = ((real_curr_p - prev_p) / prev_p) * 100 if prev_p else 0.0
                sign = "+" if pct_chg >= 0 else ""

                fibo = calc_fibonacci_retracement(highs[-90:], lows[-90:], real_curr_p)
                ichimoku = calc_ichimoku_cloud(highs, lows, closes)
                trendline = calc_geometric_trendlines(highs, lows, closes)
                patterns = detect_chart_patterns(highs, lows, closes, volumes, opens=opens)
                customs = compute_custom_indicators(
                    closes,
                    volumes,
                    highs=highs,
                    lows=lows,
                    **custom_params,
                )
                pat_name = patterns[0].name if patterns else "Không phát hiện mẫu hình"
                rsi_info = customs.get("rsi", {})
                macd_info = customs.get("macd", {})
                atr_info = customs.get("atr", {})
                ind_summary = ""
                if rsi_info and macd_info:
                    ind_summary = (
                        f" · RSI({rsi_info['period']}): {rsi_info['value']:.1f} · "
                        f"MACD({macd_info['fast_period']},{macd_info['slow_period']},"
                        f"{macd_info['signal_period']}): {macd_info['macd_line']:,.1f} "
                        f"(Signal {macd_info['signal_line']:,.1f}, "
                        f"Hist {macd_info['histogram']:+,.1f}) · "
                        f"ATR Stop: {atr_info.get('trailing_stop_2x', real_curr_p * 0.95):,.0f} đ"
                    )

                date_label = candle_pub_dt.strftime("%Y-%m-%d") if candle_pub_dt else "EOD"
                sym_label = stock.symbol if stock else sym
                tech_snippet = (
                    f"[{sym_label} KỸ THUẬT NÂNG CAO & FIBONACCI · "
                    f"Real {ohlcv_source_name} TradingView EOD · {date_label}] "
                    f"Giá: {real_curr_p:,.0f} đ ({sign}{pct_chg:.2f}%) · "
                    f"Cấu trúc: {trendline.trend_type} ({trendline.channel_slope_deg}°, "
                    f"R²={trendline.r_squared:.2f}) · "
                    f"Fibo Retracement: Hỗ trợ {fibo.nearest_support:,.0f} đ, "
                    f"Kháng cự {fibo.nearest_resistance:,.0f} đ, "
                    f"Golden Pocket (61.8%-65.0%): {fibo.golden_pocket_range}, "
                    f"Fibo Ext 161.8%: {fibo.fib_1618:,.0f} đ · "
                    f"Mây Ichimoku: {ichimoku.cloud_state} ({ichimoku.tk_cross})"
                    f"{ind_summary} · Mẫu hình: {pat_name}."
                )
                results.append(
                    RawResult(
                        url=ohlcv_url,
                        title=(
                            f"{sym_label} - Phân Tích Kỹ Thuật Nâng Cao, Fibonacci, "
                            f"Mây Ichimoku & Mẫu Hình Hình Học ({ohlcv_source_name})"
                        ),
                        snippet=tech_snippet,
                        rank=rank,
                        adapter=self.id,
                        source_engines=[self.id],
                        published_at=candle_pub_dt,
                        source_url=ohlcv_url,
                        source_name=ohlcv_source_name,
                        as_of=candle_pub_dt,
                        data_kind="observed",
                    )
                )
                rank += 1

        return results[:max_results]
