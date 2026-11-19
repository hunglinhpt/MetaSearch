"""Quantitative sentiment formulas & zero-fabrication market telemetry.

Implements mathematically grounded formulas from institutional market-sentiment
standards without synthesizing synthetic candles or proxy estimates:
- CNN Fear & Greed 7-pillar technical & volatility conventions (MA125, 52W H/L, vol ratio)
- Investopedia / Technical Money Flow: Wilder RSI(14), MFI(14), Chaikin Money Flow CMF(21)
- Surveys & Institutional Positioning: AAII Bull-Bear Spread, II Bull/Bear Ratio, NAAIM
- Market Breadth: Arms Index (TRIN), McClellan Ratio-Adjusted Net Advances (RANA / NYMO), High-Low %
- Options, Dark Pool & Leverage: CBOE Put/Call, VIX/VIX3M Term Structure, SKEW, DIX, FINRA Margin
- Academic & Quant NLP: Loughran-McDonald sublinear TF-IDF weighting, Tetlock Media Pessimism,
  Consensus Polarization Index, and Context Analytics / StockTwits S-Score.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeGuard
from urllib.parse import quote

from app.http import get_http_client
from app.retrieval.adapters.viet_stock import (
    _fetch_simplize_summary,
    _safe_float,
    fetch_vndirect_ohlcv_series,
)
from app.retrieval.adapters.yahoo_finance import DEFAULT_CHART_URL as YAHOO_CHART_URL
from app.retrieval.fa_sources.base import (
    BROWSER_HEADERS,
    MAX_RESPONSE_BYTES,
    ensure_global_symbol,
    ensure_symbol,
)
from app.retrieval.technical_analysis import _calc_ema_series, calc_wilder_rsi

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OhlcvBarInput:
    """Single OHLCV bar sourced from a real market feed (VNDIRECT, SSI, Yahoo)."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class MarketProxiesInput:
    """Optional external breadth, options, dark-pool, survey, and flow proxies."""

    # Surveys
    aaii_bullish_pct: float | None = None
    aaii_bearish_pct: float | None = None
    aaii_neutral_pct: float | None = None
    ii_bullish_pct: float | None = None
    ii_bearish_pct: float | None = None
    naaim_exposure_pct: float | None = None
    # Breadth
    advancing_issues: int | None = None
    declining_issues: int | None = None
    advancing_volume: float | None = None
    declining_volume: float | None = None
    new_52w_highs: int | None = None
    new_52w_lows: int | None = None
    pct_above_ma50: float | None = None
    pct_above_ma200: float | None = None
    rana_series: tuple[float, ...] | None = None
    # Options, volatility, credit, dark pool, margin
    cboe_put_call_equity: float | None = None
    cboe_put_call_total: float | None = None
    vix: float | None = None
    vix_3m: float | None = None
    vix_ma50: float | None = None
    cboe_skew: float | None = None
    off_exchange_short_dollar_vol: float | None = None
    off_exchange_total_dollar_vol: float | None = None
    dix_pct: float | None = None
    gex_billions: float | None = None
    margin_debt_curr: float | None = None
    margin_debt_prev_year: float | None = None
    margin_debt_yoy_pct: float | None = None
    junk_yield_pct: float | None = None
    ig_yield_pct: float | None = None
    credit_spread_bps: float | None = None
    # VN institutional flows & social/insider baselines
    vn_foreign_net_value_bil: float | None = None
    vn_prop_net_value_bil: float | None = None
    social_weighted_24h: float | None = None
    social_mean_20d: float | None = None
    social_std_20d: float | None = None
    insider_acquired_shares: float | None = None
    insider_disposed_shares: float | None = None
    source_note: str | None = None


@dataclass(frozen=True, slots=True)
class FundamentalSnapshotInput:
    """Optional verified FA ratios from FAAggregator (TCBS/FireAnt/Simplize/SEC)."""

    pe: float | None = None
    pb: float | None = None
    roe: float | None = None
    revenue_growth_yoy: float | None = None
    profit_growth_yoy: float | None = None
    debt_to_equity: float | None = None
    market_cap: float | None = None
    source: str | None = None
    source_url: str | None = None
    as_of: str | None = None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _is_pos_finite(value: float | None) -> TypeGuard[float]:
    return value is not None and math.isfinite(value) and value > 0.0


def _is_nonneg_finite(value: float | None) -> TypeGuard[float]:
    return value is not None and math.isfinite(value) and value >= 0.0


def _scale_to_100(value: float, low_val: float, high_val: float, *, invert: bool = False) -> float:
    """Linearly map [low_val, high_val] onto [0.0, 100.0] (or inverted)."""
    if high_val <= low_val:
        return 50.0
    ratio = _clamp((value - low_val) / (high_val - low_val), 0.0, 1.0)
    score = (1.0 - ratio) * 100.0 if invert else ratio * 100.0
    return round(score, 2)


def _normalize_bars(bars: list[OhlcvBarInput] | None) -> list[OhlcvBarInput]:
    if not bars:
        return []
    dedup: dict[int, OhlcvBarInput] = {}
    for b in bars:
        if (
            b.time > 0
            and math.isfinite(b.open)
            and math.isfinite(b.high)
            and math.isfinite(b.low)
            and math.isfinite(b.close)
            and math.isfinite(b.volume)
            and b.close > 0.0
            and b.high >= b.low
            and b.volume >= 0.0
        ):
            dedup[b.time] = b
    return [dedup[t] for t in sorted(dedup)]


def compute_mfi(bars: list[OhlcvBarInput], period: int = 14) -> float | None:
    """Money Flow Index (Quong & Soudack) over ``period`` bars."""
    if len(bars) < period + 1 or period < 2:
        return None
    window = bars[-(period + 1) :]
    tps = [(b.high + b.low + b.close) / 3.0 for b in window]
    pos_mf = 0.0
    neg_mf = 0.0
    for idx in range(1, len(window)):
        rmf = tps[idx] * window[idx].volume
        if tps[idx] > tps[idx - 1]:
            pos_mf += rmf
        elif tps[idx] < tps[idx - 1]:
            neg_mf += rmf
    if pos_mf == 0.0 and neg_mf == 0.0:
        return None
    if neg_mf == 0.0:
        return 100.0
    ratio = pos_mf / neg_mf
    return round(100.0 - (100.0 / (1.0 + ratio)), 2)


def compute_cmf(bars: list[OhlcvBarInput], period: int = 21) -> float | None:
    """Chaikin Money Flow (Marc Chaikin) in [-1.0, +1.0] over ``period`` bars."""
    if len(bars) < period or period < 2:
        return None
    window = bars[-period:]
    sum_mfv = 0.0
    sum_vol = 0.0
    for b in window:
        span = b.high - b.low
        mfm = ((2.0 * b.close - b.high - b.low) / span) if span > 0.0 else 0.0
        sum_mfv += mfm * b.volume
        sum_vol += b.volume
    if sum_vol <= 0.0:
        return None
    return round(_clamp(sum_mfv / sum_vol, -1.0, 1.0), 4)


def compute_realized_vol_annualized(closes: list[float], window: int) -> float | None:
    """Annualized log-return volatility (%) over ``window`` trading returns."""
    if len(closes) < window + 1 or window < 2:
        return None
    tail = closes[-(window + 1) :]
    rets: list[float] = []
    for i in range(1, len(tail)):
        if tail[i - 1] > 0.0 and tail[i] > 0.0:
            rets.append(math.log(tail[i] / tail[i - 1]))
    if len(rets) < window:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return round(math.sqrt(max(0.0, var)) * math.sqrt(252.0) * 100.0, 2)


def compute_ohlcv_sentiment_metrics(
    bars: list[OhlcvBarInput] | None,
    *,
    source_name: str | None = None,
    source_url: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Derive CNN/Investopedia/Vietstock quantitative indicators strictly from real bars."""
    valid = _normalize_bars(bars)
    if not valid:
        return {
            "available": False,
            "bars_count": 0,
            "source_name": source_name,
            "source_url": source_url,
            "as_of": as_of,
            "last_close": None,
            "return_1d_pct": None,
            "return_5d_pct": None,
            "return_20d_pct": None,
            "return_5d_z": None,
            "return_20d_z": None,
            "pct_vs_ma50": None,
            "pct_vs_ma125": None,
            "pct_vs_ma200": None,
            "range_pos_52w_pct": None,
            "rsi_14": None,
            "mfi_14": None,
            "cmf_21": None,
            "volume_ratio_20d": None,
            "realized_vol_20d_pct": None,
            "realized_vol_60d_pct": None,
            "vol_ratio_20_60": None,
            "momentum_quant_score": None,
            "breadth_flow_quant_score": None,
            "volatility_quant_score": None,
        }

    closes = [b.close for b in valid]
    last_close = closes[-1]
    n = len(valid)

    ret_1d = round((last_close / closes[-2] - 1.0) * 100.0, 2) if n >= 2 else None
    ret_5d = round((last_close / closes[-6] - 1.0) * 100.0, 2) if n >= 6 else None
    ret_20d = round((last_close / closes[-21] - 1.0) * 100.0, 2) if n >= 21 else None

    def _pct_vs_ma(win: int) -> float | None:
        if n < win:
            return None
        ma = sum(closes[-win:]) / win
        return round((last_close / ma - 1.0) * 100.0, 2) if ma > 0.0 else None

    pct_ma50 = _pct_vs_ma(50)
    pct_ma125 = _pct_vs_ma(125)
    pct_ma200 = _pct_vs_ma(200)

    range_pos_52w: float | None = None
    if n >= 20:
        w252 = valid[-min(252, n) :]
        hi = max(b.high for b in w252)
        lo = min(b.low for b in w252)
        if hi > lo:
            range_pos_52w = round(_clamp((last_close - lo) / (hi - lo) * 100.0, 0.0, 100.0), 2)

    rsi_14 = calc_wilder_rsi(closes, 14)
    if rsi_14 is not None:
        rsi_14 = round(rsi_14, 2)
    mfi_14 = compute_mfi(valid, 14)
    cmf_21 = compute_cmf(valid, 21)

    vol_ratio_20d: float | None = None
    if n >= 21:
        prior_vols = [b.volume for b in valid[-21:-1]]
        avg_vol = sum(prior_vols) / len(prior_vols)
        if avg_vol > 0.0:
            vol_ratio_20d = round(valid[-1].volume / avg_vol, 2)

    rvol_20 = compute_realized_vol_annualized(closes, 20)
    rvol_60 = compute_realized_vol_annualized(closes, 60)
    vol_ratio_20_60 = (
        round(rvol_20 / rvol_60, 2) if (rvol_20 is not None and rvol_60 and rvol_60 > 0.0) else None
    )

    # Quantitative 0..100 sub-scores (only averaged from actually populated inputs)
    mom_parts: list[float] = []
    if pct_ma125 is not None:
        mom_parts.append(_scale_to_100(pct_ma125, -15.0, 15.0))
    elif pct_ma50 is not None:
        mom_parts.append(_scale_to_100(pct_ma50, -10.0, 10.0))
    if pct_ma200 is not None:
        mom_parts.append(_scale_to_100(pct_ma200, -20.0, 20.0))
    if rsi_14 is not None:
        mom_parts.append(_clamp(rsi_14, 0.0, 100.0))
    if ret_20d is not None:
        mom_parts.append(_scale_to_100(ret_20d, -12.0, 12.0))
    momentum_quant_score = round(sum(mom_parts) / len(mom_parts), 2) if mom_parts else None

    flow_parts: list[float] = []
    if range_pos_52w is not None:
        flow_parts.append(range_pos_52w)
    if mfi_14 is not None:
        flow_parts.append(mfi_14)
    if cmf_21 is not None:
        flow_parts.append(_scale_to_100(cmf_21, -0.35, 0.35))
    if vol_ratio_20d is not None and ret_1d is not None:
        # Only above-average volume excess amplifies direction cleanly (down days stay <= 0)
        vol_excess = max(0.0, vol_ratio_20d - 1.0)
        dir_sign = 1.0 if ret_1d > 0.0 else (-1.0 if ret_1d < 0.0 else 0.0)
        signed_surge = _clamp(vol_excess * dir_sign, -1.5, 1.5)
        flow_parts.append(_scale_to_100(signed_surge, -1.5, 1.5))
    breadth_flow_quant_score = round(sum(flow_parts) / len(flow_parts), 2) if flow_parts else None

    volatility_quant_score: float | None = None
    if vol_ratio_20_60 is not None:
        # Analog to CNN VIX vs 50D MA: >1.35 fear (low), <0.80 calm (high)
        volatility_quant_score = _scale_to_100(vol_ratio_20_60, 0.70, 1.45, invert=True)

    # Volatility-normalized return z-scores using daily vol derived from 20d annualized vol
    daily_vol_pct = (rvol_20 / math.sqrt(252.0)) if (rvol_20 is not None and rvol_20 > 0) else None
    ret_5d_z = (
        round(ret_5d / max(daily_vol_pct * math.sqrt(5.0), 0.5), 2)
        if (ret_5d is not None and daily_vol_pct is not None)
        else None
    )
    ret_20d_z = (
        round(ret_20d / max(daily_vol_pct * math.sqrt(20.0), 1.0), 2)
        if (ret_20d is not None and daily_vol_pct is not None)
        else None
    )

    return {
        "available": True,
        "bars_count": n,
        "source_name": source_name,
        "source_url": source_url,
        "as_of": as_of,
        "last_close": round(last_close, 4),
        "return_1d_pct": ret_1d,
        "return_5d_pct": ret_5d,
        "return_20d_pct": ret_20d,
        "return_5d_z": ret_5d_z,
        "return_20d_z": ret_20d_z,
        "pct_vs_ma50": pct_ma50,
        "pct_vs_ma125": pct_ma125,
        "pct_vs_ma200": pct_ma200,
        "range_pos_52w_pct": range_pos_52w,
        "rsi_14": rsi_14,
        "mfi_14": mfi_14,
        "cmf_21": cmf_21,
        "volume_ratio_20d": vol_ratio_20d,
        "realized_vol_20d_pct": rvol_20,
        "realized_vol_60d_pct": rvol_60,
        "vol_ratio_20_60": vol_ratio_20_60,
        "momentum_quant_score": momentum_quant_score,
        "breadth_flow_quant_score": breadth_flow_quant_score,
        "volatility_quant_score": volatility_quant_score,
    }


def compute_market_proxy_metrics(proxies: MarketProxiesInput | None) -> dict[str, Any]:
    """Compute exact market breadth, survey, options, dark-pool & leverage metrics."""
    if proxies is None:
        return {"available": False}

    # 1. AAII Bull-Bear Spread
    aaii_spread: float | None = None
    if (
        proxies.aaii_bullish_pct is not None
        and proxies.aaii_bearish_pct is not None
        and math.isfinite(proxies.aaii_bullish_pct)
        and math.isfinite(proxies.aaii_bearish_pct)
    ):
        aaii_spread = round(proxies.aaii_bullish_pct - proxies.aaii_bearish_pct, 2)

    # 2. Investors Intelligence Bull/Bear Ratio
    ii_ratio: float | None = None
    if _is_pos_finite(proxies.ii_bullish_pct) and _is_pos_finite(proxies.ii_bearish_pct):
        ii_ratio = round(float(proxies.ii_bullish_pct) / float(proxies.ii_bearish_pct), 2)

    # 3. NAAIM Exposure
    naaim = (
        round(float(proxies.naaim_exposure_pct), 2)
        if proxies.naaim_exposure_pct is not None and math.isfinite(proxies.naaim_exposure_pct)
        else None
    )

    # 4. Arms Index (TRIN) & Ratio-Adjusted Net Advances (RANA)
    trin: float | None = None
    if (
        proxies.advancing_issues is not None
        and proxies.declining_issues is not None
        and proxies.declining_issues > 0
        and _is_pos_finite(proxies.advancing_volume)
        and _is_pos_finite(proxies.declining_volume)
    ):
        ad_ratio = proxies.advancing_issues / proxies.declining_issues
        vol_ratio = float(proxies.advancing_volume) / float(proxies.declining_volume)
        if vol_ratio > 0.0:
            trin = round(ad_ratio / vol_ratio, 3)

    rana: float | None = None
    if proxies.advancing_issues is not None and proxies.declining_issues is not None:
        total_issues = proxies.advancing_issues + proxies.declining_issues
        if total_issues > 0:
            rana = round(
                ((proxies.advancing_issues - proxies.declining_issues) / total_issues) * 1000.0, 2
            )

    # McClellan Oscillator (NYMO) when >= 39 historical RANA observations are supplied
    mcclellan_osc: float | None = None
    if proxies.rana_series and len(proxies.rana_series) >= 39:
        series = [float(x) for x in proxies.rana_series if math.isfinite(x)]
        if len(series) >= 39:
            ema19 = _calc_ema_series(series, 19)
            ema39 = _calc_ema_series(series, 39)
            if ema19 and ema39:
                mcclellan_osc = round(ema19[-1] - ema39[-1], 2)

    # 5. High-Low Breadth %
    hl_breadth: float | None = None
    if proxies.new_52w_highs is not None and proxies.new_52w_lows is not None:
        hl_sum = proxies.new_52w_highs + proxies.new_52w_lows
        if hl_sum > 0:
            hl_breadth = round((proxies.new_52w_highs / hl_sum) * 100.0, 2)

    # 6. Volatility term structure & options
    vix_term_ratio: float | None = None
    vix_term_regime: str | None = None
    if _is_pos_finite(proxies.vix) and _is_pos_finite(proxies.vix_3m):
        vix_term_ratio = round(float(proxies.vix) / float(proxies.vix_3m), 3)
        if vix_term_ratio > 1.0:
            vix_term_regime = "BACKWARDATION_STRESS"
        elif vix_term_ratio >= 0.90:
            vix_term_regime = "ELEVATED_CONTANGO"
        else:
            vix_term_regime = "CALM_CONTANGO"

    vix_vs_ma50_pct: float | None = None
    if _is_pos_finite(proxies.vix) and _is_pos_finite(proxies.vix_ma50):
        vix_vs_ma50_pct = round((float(proxies.vix) / float(proxies.vix_ma50) - 1.0) * 100.0, 2)

    put_call = (
        round(float(proxies.cboe_put_call_equity), 3)
        if _is_nonneg_finite(proxies.cboe_put_call_equity)
        else (
            round(float(proxies.cboe_put_call_total), 3)
            if _is_nonneg_finite(proxies.cboe_put_call_total)
            else None
        )
    )
    cboe_skew = round(float(proxies.cboe_skew), 2) if _is_pos_finite(proxies.cboe_skew) else None

    # 7. SqueezeMetrics DIX & GEX
    dix_pct: float | None = None
    if proxies.dix_pct is not None and math.isfinite(proxies.dix_pct):
        dix_pct = round(_clamp(float(proxies.dix_pct), 0.0, 100.0), 2)
    elif _is_nonneg_finite(proxies.off_exchange_short_dollar_vol) and _is_pos_finite(
        proxies.off_exchange_total_dollar_vol
    ):
        dix_pct = round(
            _clamp(
                (
                    float(proxies.off_exchange_short_dollar_vol)
                    / float(proxies.off_exchange_total_dollar_vol)
                )
                * 100.0,
                0.0,
                100.0,
            ),
            2,
        )

    gex_bil = (
        round(float(proxies.gex_billions), 3)
        if proxies.gex_billions is not None and math.isfinite(proxies.gex_billions)
        else None
    )

    # 8. FINRA Margin Debt YoY% & Credit Spread
    margin_yoy: float | None = None
    if proxies.margin_debt_yoy_pct is not None and math.isfinite(proxies.margin_debt_yoy_pct):
        margin_yoy = round(float(proxies.margin_debt_yoy_pct), 2)
    elif _is_nonneg_finite(proxies.margin_debt_curr) and _is_pos_finite(
        proxies.margin_debt_prev_year
    ):
        margin_yoy = round(
            ((float(proxies.margin_debt_curr) / float(proxies.margin_debt_prev_year)) - 1.0)
            * 100.0,
            2,
        )

    credit_bps: float | None = None
    if proxies.credit_spread_bps is not None and math.isfinite(proxies.credit_spread_bps):
        credit_bps = round(float(proxies.credit_spread_bps), 1)
    elif (
        proxies.junk_yield_pct is not None
        and proxies.ig_yield_pct is not None
        and math.isfinite(proxies.junk_yield_pct)
        and math.isfinite(proxies.ig_yield_pct)
    ):
        credit_bps = round((float(proxies.junk_yield_pct) - float(proxies.ig_yield_pct)) * 100.0, 1)

    # 9. Context Analytics / StockTwits S-Score & Form 4 / MSPR Insider Ratio
    s_score: float | None = None
    if (
        proxies.social_weighted_24h is not None
        and proxies.social_mean_20d is not None
        and _is_pos_finite(proxies.social_std_20d)
        and math.isfinite(proxies.social_weighted_24h)
        and math.isfinite(proxies.social_mean_20d)
    ):
        s_score = round(
            (float(proxies.social_weighted_24h) - float(proxies.social_mean_20d))
            / float(proxies.social_std_20d),
            3,
        )

    mspr_derived: float | None = None
    if _is_nonneg_finite(proxies.insider_acquired_shares) and _is_nonneg_finite(
        proxies.insider_disposed_shares
    ):
        acq = float(proxies.insider_acquired_shares)
        disp = float(proxies.insider_disposed_shares)
        if acq + disp > 0.0:
            mspr_derived = round((acq - disp) / (acq + disp), 3)

    # Map proxy groups onto standardized 0..100 sentiment scores (higher = greedier / more bullish)
    breadth_scores: list[float] = []
    if hl_breadth is not None:
        breadth_scores.append(hl_breadth)
    if proxies.pct_above_ma50 is not None and math.isfinite(proxies.pct_above_ma50):
        breadth_scores.append(_clamp(float(proxies.pct_above_ma50), 0.0, 100.0))
    if proxies.pct_above_ma200 is not None and math.isfinite(proxies.pct_above_ma200):
        breadth_scores.append(_clamp(float(proxies.pct_above_ma200), 0.0, 100.0))
    if rana is not None:
        breadth_scores.append(_scale_to_100(rana, -600.0, 600.0))
    if trin is not None:
        # TRIN < 0.7 bullish, > 1.8 heavy selling
        breadth_scores.append(_scale_to_100(trin, 0.50, 2.00, invert=True))

    vol_opt_scores: list[float] = []
    if vix_term_ratio is not None:
        vol_opt_scores.append(_scale_to_100(vix_term_ratio, 0.80, 1.15, invert=True))
    if vix_vs_ma50_pct is not None:
        vol_opt_scores.append(_scale_to_100(vix_vs_ma50_pct, -25.0, 45.0, invert=True))
    elif _is_pos_finite(proxies.vix):
        vol_opt_scores.append(_scale_to_100(float(proxies.vix), 12.0, 38.0, invert=True))
    if put_call is not None:
        # High put/call (> 1.1) = fear -> low score; low (< 0.5) = greed -> high score
        vol_opt_scores.append(_scale_to_100(put_call, 0.45, 1.15, invert=True))
    if credit_bps is not None:
        # Widening junk spread = fear
        vol_opt_scores.append(_scale_to_100(credit_bps, 250.0, 700.0, invert=True))

    smart_parts: list[float] = []
    if dix_pct is not None:
        smart_parts.append(_scale_to_100(dix_pct, 36.0, 50.0))
    if gex_bil is not None:
        smart_parts.append(_scale_to_100(gex_bil, -5.0, 8.0))
    if proxies.vn_foreign_net_value_bil is not None and math.isfinite(
        proxies.vn_foreign_net_value_bil
    ):
        smart_parts.append(_scale_to_100(float(proxies.vn_foreign_net_value_bil), -1000.0, 1000.0))
    if proxies.vn_prop_net_value_bil is not None and math.isfinite(proxies.vn_prop_net_value_bil):
        smart_parts.append(_scale_to_100(float(proxies.vn_prop_net_value_bil), -500.0, 500.0))
    if mspr_derived is not None:
        smart_parts.append(_scale_to_100(mspr_derived, -1.0, 1.0))

    crowd_parts: list[float] = []
    if aaii_spread is not None:
        crowd_parts.append(_scale_to_100(aaii_spread, -30.0, 40.0))
    if ii_ratio is not None:
        crowd_parts.append(_scale_to_100(ii_ratio, 0.70, 3.30))
    if naaim is not None:
        crowd_parts.append(_scale_to_100(naaim, 15.0, 105.0))
    if s_score is not None:
        crowd_parts.append(_scale_to_100(s_score, -2.5, 2.5))
    if margin_yoy is not None:
        crowd_parts.append(_scale_to_100(margin_yoy, -20.0, 35.0))

    # Require >= 2 distinct institutional signal groups so VIX alone cannot emit a BofA score.
    group_means = [
        sum(g) / len(g) for g in (breadth_scores, vol_opt_scores, smart_parts, crowd_parts) if g
    ]
    bofa_indicator: float | None = None
    if len(group_means) >= 2:
        bofa_indicator = round(sum(group_means) / len(group_means) / 10.0, 2)

    has_any = any(
        v is not None
        for v in (
            aaii_spread,
            ii_ratio,
            naaim,
            trin,
            rana,
            mcclellan_osc,
            hl_breadth,
            vix_term_ratio,
            vix_vs_ma50_pct,
            put_call,
            cboe_skew,
            dix_pct,
            gex_bil,
            margin_yoy,
            credit_bps,
            s_score,
            mspr_derived,
            proxies.pct_above_ma50,
            proxies.pct_above_ma200,
            proxies.vn_foreign_net_value_bil,
            proxies.vn_prop_net_value_bil,
        )
    )

    return {
        "available": has_any,
        "aaii_bull_bear_spread": aaii_spread,
        "ii_bull_bear_ratio": ii_ratio,
        "naaim_exposure_pct": naaim,
        "arms_trin": trin,
        "mcclellan_rana": rana,
        "mcclellan_oscillator": mcclellan_osc,
        "high_low_breadth_pct": hl_breadth,
        "pct_above_ma50": proxies.pct_above_ma50,
        "pct_above_ma200": proxies.pct_above_ma200,
        "vix_term_ratio": vix_term_ratio,
        "vix_term_regime": vix_term_regime,
        "vix_vs_ma50_pct": vix_vs_ma50_pct,
        "cboe_put_call_ratio": put_call,
        "cboe_skew": cboe_skew,
        "dix_pct": dix_pct,
        "gex_billions": gex_bil,
        "margin_debt_yoy_pct": margin_yoy,
        "credit_spread_bps": credit_bps,
        "vn_foreign_net_value_bil": proxies.vn_foreign_net_value_bil,
        "vn_prop_net_value_bil": proxies.vn_prop_net_value_bil,
        "social_s_score": s_score,
        "insider_mspr": mspr_derived,
        "bofa_bull_bear_analog_0_10": bofa_indicator,
        "breadth_proxy_score": round(sum(breadth_scores) / len(breadth_scores), 2)
        if breadth_scores
        else None,
        "volatility_options_proxy_score": (
            round(sum(vol_opt_scores) / len(vol_opt_scores), 2) if vol_opt_scores else None
        ),
        "smart_money_proxy_score": round(sum(smart_parts) / len(smart_parts), 2)
        if smart_parts
        else None,
        "crowd_positioning_proxy_score": round(sum(crowd_parts) / len(crowd_parts), 2)
        if crowd_parts
        else None,
        "source_note": proxies.source_note,
    }


def compute_tfidf_idf_map(doc_term_lists: list[list[str]]) -> dict[str, float]:
    """Loughran-McDonald smooth IDF weights across >=3 documents.

    High-frequency boilerplate terms repeated in nearly every hit are dampened
    toward 0.50x while rare specific financial catalyst terms remain near 1.0x.
    """
    n_docs = len(doc_term_lists)
    if n_docs < 3:
        return {}
    df: dict[str, int] = {}
    for terms in doc_term_lists:
        for t in set(terms):
            df[t] = df.get(t, 0) + 1
    denom = 1.0 + math.log((n_docs + 1.0) / 2.0)
    weights: dict[str, float] = {}
    for term, count in df.items():
        raw_idf = 1.0 + math.log((n_docs + 1.0) / (count + 1.0))
        weights[term] = round(_clamp(raw_idf / denom, 0.50, 1.25), 4)
    return weights


def compute_tetlock_pessimism_ratio(
    weighted_scores: list[tuple[float, float]],
) -> float:
    """Tetlock (2007) weighted media pessimism ratio in [0.0, 1.0].

    ``weighted_scores`` contains ``(effective_weight, score_in_-1_to_+1)`` for
    institutional/media hits.
    """
    total_w = sum(max(0.0, w) for w, _ in weighted_scores)
    if total_w <= 0.0:
        return 0.0
    neg_mass = sum(max(0.0, w) * abs(s) for w, s in weighted_scores if s < -0.15)
    return round(_clamp(neg_mass / total_w, 0.0, 1.0), 4)


def compute_consensus_polarization(
    weighted_scores: list[tuple[float, float]],
) -> float:
    """Market disagreement index in [0.0, 1.0]: 0 = unanimous, 1 = balanced bull/bear split."""
    abs_mass = sum(max(0.0, w) * abs(s) for w, s in weighted_scores)
    if abs_mass <= 0.0:
        return 0.0
    net_mass = abs(sum(max(0.0, w) * s for w, s in weighted_scores))
    return round(_clamp(1.0 - (net_mass / abs_mass), 0.0, 1.0), 4)


def compute_source_diversity_entropy(domains: list[str]) -> float:
    """Normalized Shannon entropy [0.0, 1.0] across source domains."""
    clean = [d for d in domains if d]
    n = len(clean)
    if n <= 1:
        return 0.0
    counts: dict[str, int] = {}
    for d in clean:
        counts[d] = counts.get(d, 0) + 1
    if len(counts) <= 1:
        return 0.0
    entropy = 0.0
    for c in counts.values():
        p = c / n
        entropy -= p * math.log(p)
    max_ent = math.log(len(counts))
    return round(_clamp(entropy / max_ent if max_ent > 0.0 else 0.0, 0.0, 1.0), 4)


def bars_from_ohlcv_series(
    times: list[int],
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
) -> tuple[list[OhlcvBarInput], str | None]:
    """Build sorted valid OhlcvBarInput items from parallel series arrays (never synthesized)."""
    n = min(len(times), len(opens), len(highs), len(lows), len(closes), len(volumes))
    raw_bars: list[OhlcvBarInput] = []
    for i in range(n):
        t = times[i]
        o, h, l_val, c, v = opens[i], highs[i], lows[i], closes[i], volumes[i]
        if (
            isinstance(t, int)
            and not isinstance(t, bool)
            and t > 0
            and c is not None
            and o is not None
            and h is not None
            and l_val is not None
            and v is not None
        ):
            raw_bars.append(
                OhlcvBarInput(
                    time=t,
                    open=float(o),
                    high=float(h),
                    low=float(l_val),
                    close=float(c),
                    volume=float(v),
                )
            )
    valid = _normalize_bars(raw_bars)
    as_of = datetime.fromtimestamp(valid[-1].time, tz=UTC).isoformat() if valid else None
    return valid, as_of


def parse_yahoo_chart_bars(payload: dict[str, Any]) -> tuple[list[OhlcvBarInput], str | None]:
    """Extract real daily OHLCV bars from a Yahoo Finance v8 chart JSON payload."""
    results = ((payload or {}).get("chart") or {}).get("result") or []
    if not results or not isinstance(results[0], dict):
        return [], None
    first = results[0]
    timestamps = first.get("timestamp") or []
    quotes_list = (first.get("indicators") or {}).get("quote") or []
    if not timestamps or not quotes_list or not isinstance(quotes_list[0], dict):
        return [], None
    q = quotes_list[0]
    opens = q.get("open") or []
    highs = q.get("high") or []
    lows = q.get("low") or []
    closes = q.get("close") or []
    volumes = q.get("volume") or []
    n = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
    raw_bars: list[OhlcvBarInput] = []
    for i in range(n):
        ts, o, h, l_val, c = (
            timestamps[i],
            opens[i],
            highs[i],
            lows[i],
            closes[i],
        )
        v = volumes[i] if i < len(volumes) else 0.0
        if ts is None or o is None or h is None or l_val is None or c is None:
            continue
        try:
            ts_int = int(ts)
            f_o, f_h, f_l, f_c, f_v = (
                float(o),
                float(h),
                float(l_val),
                float(c),
                float(v or 0.0),
            )
        except (TypeError, ValueError):
            continue
        if (
            ts_int <= 0
            or f_c <= 0.0
            or not all(math.isfinite(x) for x in (f_o, f_h, f_l, f_c, f_v))
        ):
            continue
        raw_bars.append(
            OhlcvBarInput(
                time=ts_int,
                open=f_o,
                high=f_h,
                low=f_l,
                close=f_c,
                volume=f_v,
            )
        )
    valid = _normalize_bars(raw_bars)
    as_of = datetime.fromtimestamp(valid[-1].time, tz=UTC).isoformat() if valid else None
    return valid, as_of


async def _fetch_yahoo_chart_series(
    symbol: str,
    *,
    range_str: str,
    timeout_s: float,
    client: Any = None,
    yahoo_chart_url: str = YAHOO_CHART_URL,
) -> tuple[list[OhlcvBarInput], str | None]:
    """Fetch daily OHLCV bars from Yahoo Finance v8 chart JSON (fail-open)."""
    try:
        http_cli = client or get_http_client("search")
        url = f"{yahoo_chart_url.rstrip('/')}/{quote(symbol, safe='')}"
        resp = await http_cli.get(
            url,
            params={"range": range_str, "interval": "1d"},
            timeout=timeout_s,
        )
        if resp.status_code == 200:
            raw_bytes = resp.content or b""
            if len(raw_bytes) > MAX_RESPONSE_BYTES:
                logger.warning(
                    "Yahoo Finance chart payload too large for %s (%d bytes)",
                    symbol,
                    len(raw_bytes),
                )
                return [], None
            return parse_yahoo_chart_bars(resp.json())
    except Exception as exc:
        logger.debug("Yahoo Finance chart fetch failed for %s: %s", symbol, exc)
    return [], None


_CNN_FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_CBOE_PCR_TMPL = "https://cdn.cboe.com/data/us/options/market_statistics/daily/{day}_daily_options"
_FRED_OAS_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2"
_CAFEF_HISTORY_TMPL = "https://cafef.vn/du-lieu/Ajax/PageNew/DataHistory/{endpoint}.ashx"
_SSI_STOCK_TMPL = "https://iboard-query.ssi.com.vn/stock/{symbol}"
_SSI_HOSE_MAIN_URL = "https://iboard-query.ssi.com.vn/stock/exchange/hose?boardId=MAIN"
_DNSE_OHLCV_URL = "https://services.entrade.com.vn/chart-api/v2/ohlcs/stock"
_SIMPLIZE_NEWS_URL = "https://api.simplize.vn/api/company/news-event/list-news"
_SIMPLIZE_REPORTS_URL = "https://api.simplize.vn/api/company/analysis-report/list"

_BREADTH_CACHE: tuple[float, dict[str, Any]] | None = None
_BREADTH_LOCK: asyncio.Lock | None = None
_VNINDEX_CACHE: tuple[float, dict[str, Any]] | None = None
_VNINDEX_LOCK: asyncio.Lock | None = None


async def fetch_cnn_fear_greed(
    *,
    timeout_s: float = 4.0,
    client: Any = None,
    url: str = _CNN_FG_URL,
) -> dict[str, Any] | None:
    """Fetch official CNN Fear & Greed JSON with historical comparisons & sub-indicators."""
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(
            url,
            headers={**BROWSER_HEADERS, "Referer": "https://edition.cnn.com/"},
            timeout=timeout_s,
        )
        if resp.status_code != 200 or len(resp.content or b"") > MAX_RESPONSE_BYTES:
            return None
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    fg = data.get("fear_and_greed")
    if not isinstance(fg, dict):
        return None
    score = _safe_float(fg.get("score"))
    if score is None:
        return None
    subs: dict[str, dict[str, Any]] = {}
    for key in (
        "market_momentum_sp500",
        "stock_price_strength",
        "stock_price_breadth",
        "put_call_options",
        "market_volatility_vix",
        "safe_haven_demand",
        "junk_bond_demand",
    ):
        node = data.get(key)
        if isinstance(node, dict):
            sub_score = _safe_float(node.get("score"))
            if sub_score is not None:
                subs[key] = {"score": round(sub_score, 2), "rating": str(node.get("rating") or "")}
    return {
        "score": round(score, 2),
        "rating": str(fg.get("rating") or ""),
        "previous_close": _safe_float(fg.get("previous_close")),
        "previous_1_week": _safe_float(fg.get("previous_1_week")),
        "previous_1_month": _safe_float(fg.get("previous_1_month")),
        "previous_1_year": _safe_float(fg.get("previous_1_year")),
        "as_of": str(fg.get("timestamp") or "") or None,
        "sub_indicators": subs,
        "source_name": "CNN Business Fear & Greed",
        "source_url": "https://edition.cnn.com/markets/fear-and-greed",
    }


async def fetch_cboe_daily_pcr(
    *,
    timeout_s: float = 4.0,
    client: Any = None,
    url_template: str = _CBOE_PCR_TMPL,
    ref_date: datetime | None = None,
) -> dict[str, Any] | None:
    """Fetch CBOE daily Equity/Total/Index Put-Call Ratio JSON stepping back up to 5 days."""
    base_dt = (ref_date or datetime.now(UTC)).date()
    http_cli = client or get_http_client("search")
    for offset in range(6):
        day_str = (base_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        target = url_template.format(day=day_str)
        try:
            resp = await http_cli.get(target, headers=BROWSER_HEADERS, timeout=timeout_s)
            if resp.status_code != 200 or len(resp.content or b"") > MAX_RESPONSE_BYTES:
                continue
            payload = resp.json()
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        ratios = payload.get("ratios")
        if not isinstance(ratios, list):
            continue
        total_pcr: float | None = None
        equity_pcr: float | None = None
        index_pcr: float | None = None
        for row in ratios:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").upper()
            val = _safe_float(row.get("value"))
            if val is None or val <= 0:
                continue
            if "EQUITY" in name and equity_pcr is None:
                equity_pcr = round(val, 3)
            elif "INDEX" in name and index_pcr is None:
                index_pcr = round(val, 3)
            elif "TOTAL" in name and total_pcr is None:
                total_pcr = round(val, 3)
        if any(v is not None for v in (total_pcr, equity_pcr, index_pcr)):
            return {
                "total_put_call_ratio": total_pcr,
                "equity_put_call_ratio": equity_pcr,
                "index_put_call_ratio": index_pcr,
                "as_of": day_str,
                "source_name": "CBOE Daily Options Statistics",
                "source_url": target,
            }
        if client is not None:
            break
    return None


async def fetch_fred_hy_oas(
    *,
    timeout_s: float = 4.0,
    client: Any = None,
    url: str = _FRED_OAS_URL,
) -> dict[str, Any] | None:
    """Fetch ICE BofA US High Yield Index OAS (BAMLH0A0HYM2) from FRED CSV."""
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(url, headers=BROWSER_HEADERS, timeout=timeout_s)
        if resp.status_code != 200 or len(resp.content or b"") > MAX_RESPONSE_BYTES:
            return None
        text = resp.text
    except Exception:
        return None
    if "BAMLH0A0HYM2" not in text:
        return None
    last_date: str | None = None
    last_val: float | None = None
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) < 2:
            continue
        dt_s, val_s = row[0].strip(), row[1].strip()
        if not dt_s or dt_s.startswith("observation") or dt_s.startswith("DATE"):
            continue
        v = _safe_float(val_s)
        if v is not None and v >= 0:
            last_date = dt_s
            last_val = v
    if last_val is None:
        return None
    return {
        "credit_spread_bps": round(last_val * 100.0, 1),
        "as_of": last_date,
        "source_name": "FRED (ICE BofA HY OAS)",
        "source_url": "https://fred.stlouisfed.org/series/BAMLH0A0HYM2",
    }


async def fetch_live_market_proxies(
    ticker: str,
    *,
    is_vietnam: bool,
    timeout_s: float = 3.0,
    client: Any = None,
    yahoo_chart_url: str = YAHOO_CHART_URL,
) -> MarketProxiesInput | None:
    """Fetch real CBOE ^VIX/^VIX3M/^SKEW, SPY momentum, CBOE PCR & FRED HY OAS (fail-open)."""
    sym = ensure_global_symbol(ticker)
    if is_vietnam or sym is None:
        return None
    eff_timeout = max(0.8, min(6.0, float(timeout_s)))
    res = await asyncio.gather(
        _fetch_yahoo_chart_series(
            "^VIX",
            range_str="3mo",
            timeout_s=eff_timeout,
            client=client,
            yahoo_chart_url=yahoo_chart_url,
        ),
        _fetch_yahoo_chart_series(
            "^VIX3M",
            range_str="5d",
            timeout_s=eff_timeout,
            client=client,
            yahoo_chart_url=yahoo_chart_url,
        ),
        _fetch_yahoo_chart_series(
            "^SKEW",
            range_str="5d",
            timeout_s=eff_timeout,
            client=client,
            yahoo_chart_url=yahoo_chart_url,
        ),
        fetch_cboe_daily_pcr(timeout_s=eff_timeout, client=client),
        fetch_fred_hy_oas(timeout_s=eff_timeout, client=client),
        return_exceptions=True,
    )
    vix_bars = res[0][0] if not isinstance(res[0], BaseException) else []
    vix3m_bars = res[1][0] if not isinstance(res[1], BaseException) else []
    skew_bars = res[2][0] if not isinstance(res[2], BaseException) else []
    pcr_data = res[3] if not isinstance(res[3], BaseException) else None
    fred_data = res[4] if not isinstance(res[4], BaseException) else None

    vix_val = round(vix_bars[-1].close, 2) if vix_bars and vix_bars[-1].close > 0 else None
    vix_ma50 = (
        round(sum(b.close for b in vix_bars[-50:]) / 50.0, 2) if len(vix_bars) >= 50 else None
    )
    vix3m_val = round(vix3m_bars[-1].close, 2) if vix3m_bars and vix3m_bars[-1].close > 0 else None
    # Guard against shared-payload test mocks returning 20.x VIX values on ^SKEW
    skew_raw = round(skew_bars[-1].close, 2) if skew_bars and skew_bars[-1].close >= 80.0 else None

    cboe_eq = pcr_data.get("equity_put_call_ratio") if isinstance(pcr_data, dict) else None
    cboe_tot = pcr_data.get("total_put_call_ratio") if isinstance(pcr_data, dict) else None
    hy_bps = fred_data.get("credit_spread_bps") if isinstance(fred_data, dict) else None

    if all(v is None for v in (vix_val, vix_ma50, vix3m_val, skew_raw, cboe_eq, cboe_tot, hy_bps)):
        return None
    parts = ["Yahoo Finance (^VIX, ^VIX3M)"]
    if skew_raw is not None:
        parts.append("^SKEW")
    if isinstance(pcr_data, dict):
        parts.append("CBOE PCR")
    if isinstance(fred_data, dict):
        parts.append("FRED HY OAS")
    return MarketProxiesInput(
        vix=vix_val,
        vix_ma50=vix_ma50,
        vix_3m=vix3m_val,
        cboe_skew=skew_raw,
        cboe_put_call_equity=cboe_eq,
        cboe_put_call_total=cboe_tot,
        credit_spread_bps=hy_bps,
        source_note=f"{', '.join(parts)} for {sym}",
    )


async def fetch_sentiment_quant_context(
    ticker: str,
    *,
    is_vietnam: bool,
    need_ohlcv: bool = True,
    fa_aggregator: Any = None,
    timeout_s: float = 3.5,
    client: Any = None,
    vndirect_base_url: str = "https://dchart-api.vndirect.com.vn/dchart/history",
    yahoo_chart_url: str = YAHOO_CHART_URL,
) -> tuple[
    list[OhlcvBarInput],
    str | None,
    str | None,
    str | None,
    FundamentalSnapshotInput | None,
]:
    """Fetch real OHLCV & FA fundamentals concurrently when requested (fail-open)."""
    sym = ensure_symbol(ticker) if is_vietnam else ensure_global_symbol(ticker)
    if sym is None:
        return [], None, None, None, None
    ticker = sym
    eff_timeout = max(0.8, min(8.0, float(timeout_s)))

    async def _fetch_ohlcv() -> tuple[list[OhlcvBarInput], str | None, str | None, str | None]:
        if not need_ohlcv:
            return [], None, None, None
        if is_vietnam:
            try:
                series = await fetch_vndirect_ohlcv_series(
                    ticker,
                    days=365,
                    base_url=vndirect_base_url,
                    timeout_s=eff_timeout,
                    client=client,
                )
                if series and series.get("closes"):
                    bars, as_of = bars_from_ohlcv_series(
                        series.get("times") or [],
                        series.get("opens") or [],
                        series.get("highs") or [],
                        series.get("lows") or [],
                        series.get("closes") or [],
                        series.get("volumes") or [],
                    )
                    if bars:
                        return (
                            bars,
                            "VNDIRECT",
                            f"https://dchart.vndirect.com.vn/?symbol={ticker}",
                            as_of,
                        )
            except Exception as exc:
                logger.debug("VNDIRECT OHLCV fetch failed for %s: %s", ticker, exc)
            if fa_aggregator is not None:
                try:
                    src = await fa_aggregator.fetch_ohlcv(ticker, timeout_s=eff_timeout)
                    if src is not None and getattr(src, "closes", None):
                        bars, as_of = bars_from_ohlcv_series(
                            getattr(src, "times", None) or [],
                            src.opens,
                            src.highs,
                            src.lows,
                            src.closes,
                            src.volumes,
                        )
                        if bars:
                            return (
                                bars,
                                str(getattr(src, "source_id", "") or "fa-aggregator").upper(),
                                str(getattr(src, "source_url", "") or ""),
                                as_of,
                            )
                except Exception as exc:
                    logger.debug("Fallback FA OHLCV fetch failed for %s: %s", ticker, exc)
            dnse_bars, dnse_as_of = await fetch_dnse_ohlcv_series(
                ticker, days=365, timeout_s=eff_timeout, client=client
            )
            if dnse_bars:
                return (
                    dnse_bars,
                    "DNSE",
                    f"https://www.dnse.com.vn/senses/co-phieu-{quote(ticker, safe='')}",
                    dnse_as_of,
                )
            return [], None, None, None

        # Global equity daily series via Yahoo Finance v8 chart JSON
        bars, as_of = await _fetch_yahoo_chart_series(
            ticker,
            range_str="1y",
            timeout_s=eff_timeout,
            client=client,
            yahoo_chart_url=yahoo_chart_url,
        )
        if bars:
            return (
                bars,
                "Yahoo Finance",
                f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}",
                as_of,
            )
        return [], None, None, None

    async def _fetch_fa() -> FundamentalSnapshotInput | None:
        if not is_vietnam:
            return None
        if fa_aggregator is not None:
            try:
                agg = await fa_aggregator.fetch_financials(ticker, timeout_s=eff_timeout)
                if agg is not None and getattr(agg, "fields", None):
                    pe = agg.get("pe")
                    pb = agg.get("pb")
                    # PASS-2 audit P3-17: `or` swallows legitimate 0.0 values —
                    # explicit is-not-None chains per key family.
                    roe = agg.get("roe") if agg.get("roe") is not None else agg.get("roe_pct")
                    rev_g = (
                        agg.get("revenue_growth_yoy")
                        if agg.get("revenue_growth_yoy") is not None
                        else agg.get("revenue_growth_pct")
                    )
                    prof_g = next(
                        (
                            v
                            for v in (
                                agg.get("profit_growth_yoy"),
                                agg.get("profit_growth_pct"),
                                agg.get("net_profit_growth_pct"),
                            )
                            if v is not None
                        ),
                        None,
                    )
                    dte = agg.get("debt_to_equity")
                    mcap = (
                        agg.get("market_cap")
                        if agg.get("market_cap") is not None
                        else agg.get("market_cap_vnd_bil")
                    )
                    if not all(v is None for v in (pe, pb, roe, rev_g, prof_g, dte, mcap)):
                        sources_used = getattr(agg, "sources_used", []) or []
                        source_urls = getattr(agg, "source_urls", []) or []
                        as_of_dt = getattr(agg, "as_of", None)
                        return FundamentalSnapshotInput(
                            pe=pe,
                            pb=pb,
                            roe=roe,
                            revenue_growth_yoy=rev_g,
                            profit_growth_yoy=prof_g,
                            debt_to_equity=dte,
                            market_cap=mcap,
                            source=", ".join(sources_used) if sources_used else None,
                            source_url=source_urls[0] if source_urls else None,
                            as_of=as_of_dt.isoformat() if as_of_dt is not None else None,
                        )
            except Exception as exc:
                logger.debug("FA aggregator snapshot failed for %s: %s", ticker, exc)

        # Fallback to direct keyless Simplize summary API when aggregator lacks VN ratios
        try:
            simp = await _fetch_simplize_summary(ticker, timeout_s=eff_timeout, client=client)
            if simp:
                pe = _safe_float(simp.get("peRatio"))
                pb = _safe_float(simp.get("pbRatio"))
                roe = _safe_float(simp.get("roe"))
                mc_raw = _safe_float(simp.get("marketCap"))
                mcap = round(mc_raw / 1e9, 2) if mc_raw is not None and mc_raw > 0 else None
                if any(v is not None for v in (pe, pb, roe, mcap)):
                    return FundamentalSnapshotInput(
                        pe=round(pe, 2) if pe is not None else None,
                        pb=round(pb, 2) if pb is not None else None,
                        roe=round(roe, 2) if roe is not None else None,
                        market_cap=mcap,
                        source="Simplize",
                        source_url=f"https://simplize.vn/co-phieu/{quote(ticker.upper(), safe='')}",
                        as_of=None,
                    )
        except Exception as exc:
            logger.debug("Simplize FA fallback failed for %s: %s", ticker, exc)
        return None

    (bars, src_name, src_url, as_of), fa_snap = await asyncio.gather(
        _fetch_ohlcv(),
        _fetch_fa(),
    )
    return bars, src_name, src_url, as_of, fa_snap


def _to_bil_vnd(raw_val: float | None) -> float | None:
    if raw_val is None or not math.isfinite(raw_val):
        return None
    # CafeF Ajax DataHistory reports monetary net value in raw VND (e.g. 2.5e10 = 25 billion).
    if abs(raw_val) >= 1e6:
        return round(raw_val / 1e9, 3)
    return round(raw_val, 3)


def _extract_cafef_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    node = payload.get("Data")
    if isinstance(node, list):
        return [r for r in node if isinstance(r, dict)]
    if isinstance(node, dict):
        inner = node.get("Data")
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
        if isinstance(inner, dict) and isinstance(inner.get("ListDataTuDoanh"), list):
            return [r for r in inner["ListDataTuDoanh"] if isinstance(r, dict)]
        if isinstance(node.get("ListDataTuDoanh"), list):
            return [r for r in node["ListDataTuDoanh"] if isinstance(r, dict)]
    return []


def _cafef_num(val: Any) -> float | None:
    if isinstance(val, (int, float)) and math.isfinite(val):
        return float(val)
    if isinstance(val, str):
        cleaned = val.strip().replace(",", "").replace(" ", "")
        if not cleaned or cleaned in ("-", "--", "N/A"):
            return None
        return _safe_float(cleaned)
    return None


async def fetch_dnse_ohlcv_series(
    symbol: str,
    *,
    days: int = 365,
    timeout_s: float = 4.0,
    client: Any = None,
    base_url: str = _DNSE_OHLCV_URL,
) -> tuple[list[OhlcvBarInput], str | None]:
    """Fetch daily OHLCV bars from DNSE/Entrade chart API (backup VN feed)."""
    sym = ensure_symbol(symbol)
    if sym is None:
        return [], None
    now_ts = int(datetime.now(UTC).timestamp())
    start_ts = now_ts - max(10, int(days)) * 86400
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(
            base_url,
            params={
                "symbol": sym,
                "resolution": "1D",
                "from": str(start_ts),
                "to": str(now_ts),
            },
            headers=BROWSER_HEADERS,
            timeout=timeout_s,
        )
        if resp.status_code != 200 or len(resp.content or b"") > MAX_RESPONSE_BYTES:
            return [], None
        data = resp.json()
    except Exception:
        return [], None
    if not isinstance(data, dict) or not data.get("c"):
        return [], None
    return bars_from_ohlcv_series(
        data.get("t") or [],
        data.get("o") or [],
        data.get("h") or [],
        data.get("l") or [],
        data.get("c") or [],
        data.get("v") or [],
    )


async def fetch_cafef_vn_flows(
    symbol: str,
    *,
    timeout_s: float = 4.0,
    client: Any = None,
    url_template: str = _CAFEF_HISTORY_TMPL,
) -> dict[str, Any]:
    """Fetch verified CafeF Foreign/Prop flows & Insider Form-4 analog MSPR."""
    sym = ensure_symbol(symbol)
    if sym is None:
        return {}
    http_cli = client or get_http_client("search")

    async def _get(endpoint: str) -> list[dict[str, Any]]:
        try:
            resp = await http_cli.get(
                url_template.format(endpoint=endpoint),
                params={
                    "Symbol": sym,
                    "StartDate": "",
                    "EndDate": "",
                    "PageIndex": 1,
                    "PageSize": 20,
                },
                headers={**BROWSER_HEADERS, "Referer": "https://cafef.vn/"},
                timeout=timeout_s,
            )
            if resp.status_code != 200 or len(resp.content or b"") > MAX_RESPONSE_BYTES:
                return []
            return _extract_cafef_rows(resp.json())
        except Exception:
            return []

    fk_rows, td_rows, cd_rows = await asyncio.gather(
        _get("GDKhoiNgoai"),
        _get("GDTuDoanh"),
        _get("GDCoDong"),
    )

    out: dict[str, Any] = {"sources": []}
    if fk_rows:
        out["sources"].append("CafeF GDKhoiNgoai")
        vals = [
            v
            for v in (
                _to_bil_vnd(_cafef_num(r.get("GiaTriMuaRong") or r.get("GiaTriRong")))
                for r in fk_rows[:20]
            )
            if v is not None
        ]
        vols = [
            v
            for v in (
                _cafef_num(r.get("KhoiLuongMuaRong") or r.get("KhoiLuongRong"))
                for r in fk_rows[:20]
            )
            if v is not None
        ]
        if vals:
            out["foreign_net_val_5d_bil"] = round(sum(vals[:5]), 3)
            out["foreign_net_val_20d_bil"] = round(sum(vals[:20]), 3)
        if vols:
            out["foreign_net_vol_5d"] = round(sum(vols[:5]), 0)
            out["foreign_net_vol_20d"] = round(sum(vols[:20]), 0)
        as_of_fk = str(fk_rows[0].get("Ngay") or "").strip() or None
        if as_of_fk:
            out["foreign_as_of"] = as_of_fk

    if td_rows:
        out["sources"].append("CafeF GDTuDoanh")
        td_vals: list[float] = []
        for r in td_rows[:20]:
            net_v = _cafef_num(r.get("GiaTriMuaRong") or r.get("GiaTriRong") or r.get("MuaRong"))
            if net_v is None:
                buy_v = _cafef_num(r.get("KLTDMua") or r.get("GiaTriMua"))
                sell_v = _cafef_num(r.get("KLTDBan") or r.get("GiaTriBan"))
                if buy_v is not None and sell_v is not None:
                    net_v = buy_v - sell_v
            conv = _to_bil_vnd(net_v)
            if conv is not None:
                td_vals.append(conv)
        if td_vals:
            out["prop_net_val_5d_bil"] = round(sum(td_vals[:5]), 3)
            out["prop_net_val_20d_bil"] = round(sum(td_vals[:20]), 3)

    if cd_rows:
        out["sources"].append("CafeF GDCoDong")
        buy_shares = 0.0
        sell_shares = 0.0
        tx_count = 0
        for r in cd_rows[:20]:
            loai = str(
                r.get("LoaiGiaoDich") or r.get("HanhDong") or r.get("TransactionType") or ""
            ).lower()
            qty = _cafef_num(
                r.get("SoLuongCoPhieuThucHien")
                or r.get("KhoiLuongThucHien")
                or r.get("SoLuongCoPhieuDangKy")
            )
            if qty is None or qty <= 0:
                continue
            if any(k in loai for k in ("mua", "buy", "nhận")):
                buy_shares += qty
                tx_count += 1
            elif any(k in loai for k in ("bán", "ban", "sell", "chuyển nhượng")):
                sell_shares += qty
                tx_count += 1
        if buy_shares + sell_shares > 0:
            ratio = (buy_shares - sell_shares) / (buy_shares + sell_shares)
            out["insider_buy_shares"] = round(buy_shares, 0)
            out["insider_sell_shares"] = round(sell_shares, 0)
            out["insider_tx_count"] = tx_count
            out["insider_mspr"] = round(ratio * 100.0, 2)
            out["insider_mspr_ratio"] = round(ratio, 3)
    return out


async def fetch_ssi_iboard_orderflow(
    symbol: str,
    *,
    timeout_s: float = 4.0,
    client: Any = None,
    url_template: str = _SSI_STOCK_TMPL,
) -> dict[str, Any]:
    """Fetch active buy/sell order-flow (stockBUVol/stockSDVol) & foreign room from SSI iBoard."""
    sym = ensure_symbol(symbol)
    if sym is None:
        return {}
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(
            url_template.format(symbol=quote(sym, safe="")),
            headers={**BROWSER_HEADERS, "Referer": "https://iboard.ssi.com.vn/"},
            timeout=timeout_s,
        )
        if resp.status_code != 200 or len(resp.content or b"") > MAX_RESPONSE_BYTES:
            return {}
        payload = resp.json()
    except Exception:
        return {}
    node = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(node, list) and node:
        node = node[0]
    if not isinstance(node, dict):
        return {}

    bu = _safe_float(node.get("stockBUVol") or node.get("buVol")) or 0.0
    sd = _safe_float(node.get("stockSDVol") or node.get("sdVol")) or 0.0
    total_act = bu + sd
    active_buy_ratio = round(bu / total_act, 4) if total_act > 0 else None

    f_buy = _safe_float(node.get("buyForeignQtty"))
    f_sell = _safe_float(node.get("sellForeignQtty"))
    total_room = _safe_float(node.get("totalRoom"))
    cur_room = _safe_float(node.get("currentRoom"))
    room_pct = (
        round((cur_room / total_room) * 100.0, 2)
        if (total_room is not None and total_room > 0 and cur_room is not None and cur_room >= 0)
        else None
    )
    matched = _safe_float(node.get("matchedPrice"))
    return {
        "active_buy_vol": round(bu, 0) if total_act > 0 else None,
        "active_sell_vol": round(sd, 0) if total_act > 0 else None,
        "active_buy_ratio": active_buy_ratio,
        "foreign_buy_vol_session": f_buy,
        "foreign_sell_vol_session": f_sell,
        "foreign_remaining_room_pct": room_pct,
        "matched_price": matched,
        "source_name": "SSI iBoard",
        "source_url": f"https://iboard.ssi.com.vn/chung-khoan/{quote(sym, safe='')}",
    }


async def fetch_ssi_hose_breadth(
    *,
    timeout_s: float = 4.5,
    client: Any = None,
    url: str = _SSI_HOSE_MAIN_URL,
) -> dict[str, Any] | None:
    """Compute real HOSE exchange market breadth (Arms TRIN & McClellan RANA) over ~400 stocks."""
    global _BREADTH_CACHE, _BREADTH_LOCK
    now_mono = time.monotonic()
    if client is None and _BREADTH_CACHE is not None and (now_mono - _BREADTH_CACHE[0]) < 300.0:
        return dict(_BREADTH_CACHE[1])

    if client is None:
        if _BREADTH_LOCK is None:
            _BREADTH_LOCK = asyncio.Lock()
        async with _BREADTH_LOCK:
            now_mono = time.monotonic()
            if _BREADTH_CACHE is not None and (now_mono - _BREADTH_CACHE[0]) < 300.0:
                return dict(_BREADTH_CACHE[1])
            parsed = await _fetch_ssi_hose_breadth_uncached(
                timeout_s=timeout_s, client=None, url=url
            )
            if parsed is not None:
                _BREADTH_CACHE = (time.monotonic(), parsed)
            return dict(parsed) if parsed is not None else None
    return await _fetch_ssi_hose_breadth_uncached(timeout_s=timeout_s, client=client, url=url)


async def _fetch_ssi_hose_breadth_uncached(
    *,
    timeout_s: float,
    client: Any,
    url: str,
) -> dict[str, Any] | None:
    try:
        http_cli = client or get_http_client("search")
        resp = await http_cli.get(
            url,
            headers={**BROWSER_HEADERS, "Referer": "https://iboard.ssi.com.vn/"},
            timeout=timeout_s,
        )
        if resp.status_code != 200 or len(resp.content or b"") > max(
            MAX_RESPONSE_BYTES, 8 * 1024 * 1024
        ):
            return None
        payload = resp.json()
    except Exception:
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return None

    adv = dec = unch = ceil_cnt = floor_cnt = 0
    adv_vol = dec_vol = 0.0
    for item in rows:
        if not isinstance(item, dict):
            continue
        ref = _safe_float(item.get("refPrice"))
        px = _safe_float(item.get("matchedPrice")) or _safe_float(item.get("closePrice"))
        vol = _safe_float(item.get("nmTotalTradedQty")) or 0.0
        ceil_p = _safe_float(item.get("ceiling"))
        floor_p = _safe_float(item.get("floor"))
        if ref is None or px is None or ref <= 0 or px <= 0:
            continue
        if ceil_p is not None and px >= ceil_p:
            ceil_cnt += 1
        if floor_p is not None and px <= floor_p:
            floor_cnt += 1
        if px > ref:
            adv += 1
            adv_vol += max(0.0, vol)
        elif px < ref:
            dec += 1
            dec_vol += max(0.0, vol)
        else:
            unch += 1

    if adv + dec + unch == 0:
        return None
    trin: float | None = None
    if adv > 0 and dec > 0 and adv_vol > 0 and dec_vol > 0:
        trin = round((adv / dec) / (adv_vol / dec_vol), 3)
    rana = round(1000.0 * (adv - dec) / (adv + dec), 2) if (adv + dec) > 0 else None
    return {
        "exchange": "HOSE",
        "total_issues": adv + dec + unch,
        "advancing_issues": adv,
        "declining_issues": dec,
        "unchanged_issues": unch,
        "ceiling_count": ceil_cnt,
        "floor_count": floor_cnt,
        "advancing_volume": round(adv_vol, 0),
        "declining_volume": round(dec_vol, 0),
        "arms_trin": trin,
        "mcclellan_rana": rana,
        "as_of": datetime.now(UTC).isoformat(),
        "source_name": "SSI iBoard HOSE MAIN",
        "source_url": "https://iboard.ssi.com.vn/bang-gia/hose",
    }


async def fetch_vn_benchmark(
    *,
    timeout_s: float = 4.0,
    client: Any = None,
    vndirect_base_url: str = "https://dchart-api.vndirect.com.vn/dchart/history",
) -> dict[str, Any] | None:
    """Fetch VNINDEX 1Y real benchmark returns (5-min cached)."""
    global _VNINDEX_CACHE, _VNINDEX_LOCK
    now_mono = time.monotonic()
    if client is None and _VNINDEX_CACHE is not None and (now_mono - _VNINDEX_CACHE[0]) < 300.0:
        return dict(_VNINDEX_CACHE[1])

    async def _load() -> dict[str, Any] | None:
        try:
            series = await fetch_vndirect_ohlcv_series(
                "VNINDEX",
                days=60,
                base_url=vndirect_base_url,
                timeout_s=timeout_s,
                client=client,
            )
        except Exception:
            return None
        if not series or not series.get("closes"):
            return None
        bars, as_of = bars_from_ohlcv_series(
            series.get("times") or [],
            series.get("opens") or [],
            series.get("highs") or [],
            series.get("lows") or [],
            series.get("closes") or [],
            series.get("volumes") or [],
        )
        if len(bars) < 2:
            return None
        closes = [b.close for b in bars]
        last = closes[-1]
        return {
            "index": "VNINDEX",
            "last_close": round(last, 2),
            "ret_1d_pct": round((last / closes[-2] - 1.0) * 100.0, 2) if len(closes) >= 2 else None,
            "ret_5d_pct": round((last / closes[-6] - 1.0) * 100.0, 2) if len(closes) >= 6 else None,
            "ret_20d_pct": (
                round((last / closes[-21] - 1.0) * 100.0, 2) if len(closes) >= 21 else None
            ),
            "as_of": as_of,
            "source_name": "VNDIRECT VNINDEX",
            "source_url": "https://dchart.vndirect.com.vn/?symbol=VNINDEX",
        }

    if client is None:
        if _VNINDEX_LOCK is None:
            _VNINDEX_LOCK = asyncio.Lock()
        async with _VNINDEX_LOCK:
            now_mono = time.monotonic()
            if _VNINDEX_CACHE is not None and (now_mono - _VNINDEX_CACHE[0]) < 300.0:
                return dict(_VNINDEX_CACHE[1])
            parsed = await _load()
            if parsed is not None:
                _VNINDEX_CACHE = (time.monotonic(), parsed)
            return dict(parsed) if parsed is not None else None
    return await _load()


async def fetch_simplize_extended(
    symbol: str,
    *,
    spot_price: float | None = None,
    timeout_s: float = 4.0,
    client: Any = None,
    news_url: str = _SIMPLIZE_NEWS_URL,
    reports_url: str = _SIMPLIZE_REPORTS_URL,
) -> dict[str, Any]:
    """Fetch Simplize corporate news + brokerage BCPT analyst consensus/target price."""
    sym = ensure_symbol(symbol)
    if sym is None:
        return {}
    http_cli = client or get_http_client("search")

    async def _get_json(u: str, size: int) -> list[dict[str, Any]]:
        try:
            resp = await http_cli.get(
                u,
                params={"ticker": sym, "page": "0", "size": str(size)},
                headers=BROWSER_HEADERS,
                timeout=timeout_s,
            )
            if resp.status_code != 200 or len(resp.content or b"") > MAX_RESPONSE_BYTES:
                return []
            payload = resp.json()
        except Exception:
            return []
        rows = payload.get("data") if isinstance(payload, dict) else None
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    news_rows, report_rows = await asyncio.gather(
        _get_json(news_url, 8),
        _get_json(reports_url, 12),
    )

    news_hits: list[dict[str, Any]] = []
    for r in news_rows:
        title = str(r.get("title") or "").strip()
        slug = str(r.get("slug") or "").strip()
        if not title or not slug:
            continue
        ts_raw = r.get("ts")
        pub_iso: str | None = None
        if isinstance(ts_raw, (int, float)) and ts_raw > 0:
            sec = ts_raw / 1000.0 if ts_raw > 1e11 else float(ts_raw)
            try:
                pub_iso = datetime.fromtimestamp(sec, tz=UTC).isoformat()
            except (ValueError, OSError, OverflowError):
                pub_iso = None
        news_hits.append(
            {
                "url": f"https://simplize.vn/tin-tuc/{slug}",
                "title": title,
                "snippet": str(r.get("aiContent") or r.get("description") or "").strip()[:400],
                "published_at": pub_iso,
                "source_name": str(r.get("sourceNameDisplay") or r.get("sourceName") or "Simplize"),
            }
        )

    analyst_consensus: dict[str, Any] | None = None
    if report_rows:
        buy = hold = sell = 0
        targets: list[float] = []
        latest_reports: list[dict[str, Any]] = []
        for r in report_rows:
            rec_raw = str(r.get("recommend") or "").strip()
            rec_up = rec_raw.upper()
            if any(k in rec_up for k in ("BUY", "MUA", "OUTPERFORM", "KHẢ QUAN", "TÍCH CỰC")):
                buy += 1
            elif any(k in rec_up for k in ("SELL", "BÁN", "UNDERPERFORM", "TIÊU CỰC", "KÉM")):
                sell += 1
            elif rec_up:
                hold += 1
            tp = _safe_float(r.get("targetPrice"))
            # Guard against thousand-VND vs raw-VND: VN stocks below 1000 mean x1000 if spot > 1000
            if tp is not None and tp > 0:
                if spot_price is not None and spot_price > 1000 and tp < 1000:
                    tp = tp * 1000.0
                elif spot_price is not None and spot_price < 1000 and tp > 1000:
                    tp = tp / 1000.0
                targets.append(tp)
            if len(latest_reports) < 5:
                latest_reports.append(
                    {
                        "source": str(r.get("source") or ""),
                        "title": str(r.get("title") or "")[:180],
                        "recommend": rec_raw or None,
                        "target_price": round(tp, 0) if tp is not None else None,
                        "issue_date": r.get("issueDate"),
                        "url": str(r.get("attachedLink") or "") or None,
                    }
                )
        total_rated = buy + hold + sell
        avg_tp = round(sum(targets) / len(targets), 1) if targets else None
        med_tp = round(statistics.median(targets), 1) if targets else None
        upside = (
            round(((avg_tp / spot_price) - 1.0) * 100.0, 2)
            if (avg_tp is not None and spot_price is not None and spot_price > 0)
            else None
        )
        rating_label = "NEUTRAL"
        if total_rated > 0:
            net_rec = (buy - sell) / total_rated
            if net_rec >= 0.4:
                rating_label = "BUY"
            elif net_rec <= -0.3:
                rating_label = "SELL"
            else:
                rating_label = "HOLD"
        analyst_consensus = {
            "report_count": len(report_rows),
            "buy_count": buy,
            "hold_count": hold,
            "sell_count": sell,
            "consensus_rating": rating_label if total_rated > 0 else None,
            "avg_target_price": avg_tp,
            "median_target_price": med_tp,
            "upside_pct": upside,
            "latest_reports": latest_reports,
            "source_name": "Simplize Brokerage Consensus",
            "source_url": f"https://simplize.vn/co-phieu/{quote(sym, safe='')}/bao-cao-phan-tich",
        }
    return {"news_hits": news_hits, "analyst_consensus": analyst_consensus}


async def fetch_institutional_sentiment_extras(
    ticker: str,
    *,
    is_vietnam: bool,
    spot_price: float | None = None,
    timeout_s: float = 4.5,
    client: Any = None,
) -> dict[str, Any]:
    """Concurrently collect verified keyless institutional flows, breadth, analyst & CNN F&G."""
    eff_t = max(0.8, min(7.0, float(timeout_s)))
    cnn_task = fetch_cnn_fear_greed(timeout_s=eff_t, client=client)
    if is_vietnam:
        res = await asyncio.gather(
            cnn_task,
            fetch_cafef_vn_flows(ticker, timeout_s=eff_t, client=client),
            fetch_ssi_iboard_orderflow(ticker, timeout_s=eff_t, client=client),
            fetch_ssi_hose_breadth(timeout_s=eff_t, client=client),
            fetch_vn_benchmark(timeout_s=eff_t, client=client),
            fetch_simplize_extended(ticker, spot_price=spot_price, timeout_s=eff_t, client=client),
            return_exceptions=True,
        )
        cnn = res[0] if isinstance(res[0], dict) else None
        cafef = res[1] if isinstance(res[1], dict) else {}
        ssi_flow = res[2] if isinstance(res[2], dict) else {}
        hose_breadth = res[3] if isinstance(res[3], dict) else None
        vn_bench = res[4] if isinstance(res[4], dict) else None
        simp_ext = res[5] if isinstance(res[5], dict) else {}

        flows = {**cafef, **ssi_flow}
        if vn_bench:
            flows["vnindex_benchmark"] = vn_bench
        if not any(
            k in flows
            for k in (
                "foreign_net_val_5d_bil",
                "prop_net_val_5d_bil",
                "active_buy_ratio",
                "insider_mspr",
                "vnindex_benchmark",
            )
        ):
            flows = {}
        return {
            "institutional_flows": flows or None,
            "market_breadth": hose_breadth,
            "analyst_consensus": simp_ext.get("analyst_consensus"),
            "cnn_fear_greed": cnn,
            "direct_news_hits": simp_ext.get("news_hits") or [],
        }

    # Global equity: CNN Fear & Greed + CBOE PCR + FRED HY OAS (when not already fetched)
    global_res: tuple[Any, ...] = await asyncio.gather(
        cnn_task,
        fetch_cboe_daily_pcr(timeout_s=eff_t, client=client),
        fetch_fred_hy_oas(timeout_s=eff_t, client=client),
        return_exceptions=True,
    )
    cnn_d = global_res[0] if isinstance(global_res[0], dict) else None
    pcr_d = global_res[1] if isinstance(global_res[1], dict) else None
    fred_d = global_res[2] if isinstance(global_res[2], dict) else None
    breadth: dict[str, Any] | None = None
    if cnn_d or pcr_d or fred_d:
        breadth = {
            "exchange": "US_MACRO",
            "cnn_fear_greed_score": cnn_d.get("score") if cnn_d else None,
            "cnn_fear_greed_rating": cnn_d.get("rating") if cnn_d else None,
            "cboe_equity_put_call_ratio": pcr_d.get("equity_put_call_ratio") if pcr_d else None,
            "cboe_total_put_call_ratio": pcr_d.get("total_put_call_ratio") if pcr_d else None,
            "fred_hy_oas_bps": fred_d.get("credit_spread_bps") if fred_d else None,
            "source_name": "CNN F&G / CBOE / FRED",
        }
    return {
        "institutional_flows": None,
        "market_breadth": breadth,
        "analyst_consensus": None,
        "cnn_fear_greed": cnn_d,
        "direct_news_hits": [],
    }


async def record_sentiment_snapshot(
    *,
    ticker: str,
    market: str,
    composite_score: float,
    text_score: float | None,
    quant_score: float | None,
    social_volume: int,
    mention_count: int,
    regime: str,
    confidence: float,
    as_of: datetime | None = None,
) -> None:
    """Persist one daily sentiment snapshot per (ticker, UTC day) under sqlite_write_lock."""
    try:
        from sqlalchemy import select

        from app.config import get_settings
        from app.db.models import SentimentSnapshot
        from app.db.session import get_sessionmaker, sqlite_write_lock

        cfg = get_settings()
        if cfg.environment == "test" and not str(cfg.database_url).lower().startswith("sqlite"):
            return

        now = (as_of or datetime.now(UTC)).astimezone(UTC)
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)

        async def _write() -> None:
            async with sqlite_write_lock(), get_sessionmaker()() as session:
                existing = (
                    await session.execute(
                        select(SentimentSnapshot).where(
                            SentimentSnapshot.ticker == ticker,
                            SentimentSnapshot.snapshot_date == day_start,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    existing.composite_score = float(composite_score)
                    existing.text_score = float(text_score) if text_score is not None else None
                    existing.quant_score = float(quant_score) if quant_score is not None else None
                    existing.social_volume = int(social_volume)
                    existing.mention_count = int(mention_count)
                    existing.regime = regime
                    existing.confidence = float(confidence)
                else:
                    session.add(
                        SentimentSnapshot(
                            ticker=ticker,
                            market=market,
                            snapshot_date=day_start,
                            composite_score=float(composite_score),
                            text_score=float(text_score) if text_score is not None else None,
                            quant_score=float(quant_score) if quant_score is not None else None,
                            social_volume=int(social_volume),
                            mention_count=int(mention_count),
                            regime=regime,
                            confidence=float(confidence),
                        )
                    )
                await session.commit()

        await asyncio.wait_for(_write(), timeout=1.5)
    except Exception as exc:
        logger.debug("record_sentiment_snapshot failed for %s: %s", ticker, exc)


async def load_sentiment_history_s_score(
    ticker: str,
    *,
    current_score: float,
    current_volume: int,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Compute rolling 20-day S-Score and Social-Volume Z-score from persisted daily rows."""
    try:
        from sqlalchemy import select

        from app.config import get_settings
        from app.db.models import SentimentSnapshot
        from app.db.session import get_sessionmaker

        cfg = get_settings()
        if cfg.environment == "test" and not str(cfg.database_url).lower().startswith("sqlite"):
            return {"available": False, "history_days": 0, "s_score": None, "sv_score": None}

        now = (as_of or datetime.now(UTC)).astimezone(UTC)
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)

        async def _read() -> list[Any]:
            async with get_sessionmaker()() as session:
                return list(
                    (
                        await session.execute(
                            select(SentimentSnapshot)
                            .where(
                                SentimentSnapshot.ticker == ticker,
                                SentimentSnapshot.snapshot_date < day_start,
                            )
                            .order_by(SentimentSnapshot.snapshot_date.desc())
                            .limit(20)
                        )
                    )
                    .scalars()
                    .all()
                )

        rows = await asyncio.wait_for(_read(), timeout=1.5)
    except Exception:
        return {"available": False, "history_days": 0, "s_score": None, "sv_score": None}

    n = len(rows)
    if n < 5:
        return {"available": False, "history_days": n, "s_score": None, "sv_score": None}

    scores = [float(r.composite_score) for r in rows]
    vols = [float(r.social_volume) for r in rows]
    mean_s = sum(scores) / n
    std_s = math.sqrt(sum((x - mean_s) ** 2 for x in scores) / n)
    s_score = round((current_score - mean_s) / std_s, 3) if std_s > 1e-4 else 0.0

    mean_v = sum(vols) / n
    std_v = math.sqrt(sum((v - mean_v) ** 2 for v in vols) / n)
    sv_score = round((float(current_volume) - mean_v) / std_v, 3) if std_v > 1e-4 else None
    return {
        "available": True,
        "history_days": n,
        "s_score": s_score,
        "sv_score": sv_score,
        "mean_20d_score": round(mean_s, 4),
    }
