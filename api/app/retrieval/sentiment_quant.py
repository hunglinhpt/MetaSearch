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
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeGuard
from urllib.parse import quote

from app.http import get_http_client
from app.retrieval.adapters.viet_stock import (
    _fetch_simplize_summary,
    _safe_float,
    fetch_vndirect_ohlcv_series,
)
from app.retrieval.adapters.yahoo_finance import DEFAULT_CHART_URL as YAHOO_CHART_URL
from app.retrieval.fa_sources.base import ensure_symbol
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
            "return_20d_pct": None,
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
        # High volume amplifies price direction cleanly (Vietstock price-volume confirmation)
        signed_surge = _clamp((vol_ratio_20d - 1.0) * (1.0 if ret_1d >= 0.0 else -1.0), -1.5, 1.5)
        flow_parts.append(_scale_to_100(signed_surge, -1.5, 1.5))
    breadth_flow_quant_score = round(sum(flow_parts) / len(flow_parts), 2) if flow_parts else None

    volatility_quant_score: float | None = None
    if vol_ratio_20_60 is not None:
        # Analog to CNN VIX vs 50D MA: >1.35 fear (low), <0.80 calm (high)
        volatility_quant_score = _scale_to_100(vol_ratio_20_60, 0.70, 1.45, invert=True)

    return {
        "available": True,
        "bars_count": n,
        "source_name": source_name,
        "source_url": source_url,
        "as_of": as_of,
        "last_close": round(last_close, 4),
        "return_1d_pct": ret_1d,
        "return_20d_pct": ret_20d,
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

    all_proxy_scores = breadth_scores + vol_opt_scores + smart_parts + crowd_parts
    bofa_indicator: float | None = None
    if len(all_proxy_scores) >= 2:
        bofa_indicator = round(sum(all_proxy_scores) / len(all_proxy_scores) / 10.0, 2)

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
            return parse_yahoo_chart_bars(resp.json())
    except Exception as exc:
        logger.debug("Yahoo Finance chart fetch failed for %s: %s", symbol, exc)
    return [], None


async def fetch_live_market_proxies(
    ticker: str,
    *,
    is_vietnam: bool,
    timeout_s: float = 3.0,
    client: Any = None,
    yahoo_chart_url: str = YAHOO_CHART_URL,
) -> MarketProxiesInput | None:
    """Fetch real CBOE ^VIX / ^VIX3M volatility term structure for global equities (fail-open)."""
    sym = ensure_symbol(ticker)
    if is_vietnam or sym is None:
        return None
    eff_timeout = max(0.8, min(6.0, float(timeout_s)))
    (vix_bars, _), (vix3m_bars, _) = await asyncio.gather(
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
    )
    vix_val = round(vix_bars[-1].close, 2) if vix_bars and vix_bars[-1].close > 0 else None
    vix_ma50 = (
        round(sum(b.close for b in vix_bars[-50:]) / 50.0, 2) if len(vix_bars) >= 50 else None
    )
    vix3m_val = round(vix3m_bars[-1].close, 2) if vix3m_bars and vix3m_bars[-1].close > 0 else None
    if vix_val is None and vix_ma50 is None and vix3m_val is None:
        return None
    return MarketProxiesInput(
        vix=vix_val,
        vix_ma50=vix_ma50,
        vix_3m=vix3m_val,
        source_note=f"Yahoo Finance (^VIX, ^VIX3M) for {sym}",
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
    sym = ensure_symbol(ticker)
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
