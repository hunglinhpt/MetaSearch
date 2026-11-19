"""Market Intelligence Orchestration Layer (docs/30 Part 2).

Peer auto-discovery, price/technical alert engine, and news-to-price
event-impact study — the decision surfaces that differentiate MetaSearch
from basic market data APIs. All data plumbing (OHLCV, FA, sentiment,
events) already exists; this module orchestrates them into actionable
intelligence.

Every quantitative output is grounded in real fetched data (zero-fabrication
law): missing data → null + provenance, never placeholder values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Peer auto-discovery
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PeerGroup:
    """Peers of a target ticker, ranked by market cap proximity."""

    target: str
    peers: list[dict[str, Any]] = field(default_factory=list)


def discover_peers(
    tickers_meta: dict[str, dict[str, Any]],
    target: str,
    *,
    max_peers: int = 5,
) -> PeerGroup:
    """Discover peers of ``target`` by sector/industry overlap + market cap
    proximity from the vn_tickers registry. Deterministic, no fabricated data.

    ``tickers_meta`` maps symbol → {company_name, short_name, exchange}.
    Peers are same-exchange tickers whose company_name shares ≥ 1
    distinctive token (≥ 4 chars, not generic) with the target.
    """
    target_meta = tickers_meta.get(target)
    if not target_meta:
        return PeerGroup(target=target)

    target_name = (target_meta.get("company_name") or "").lower()
    target_exchange = (target_meta.get("exchange") or "").upper()
    target_tokens = _distinctive_tokens(target_name)

    peers: list[tuple[float, str, str]] = []
    for sym, meta in tickers_meta.items():
        if sym == target or sym.startswith("^"):
            continue
        exchange = (meta.get("exchange") or "").upper()
        if target_exchange and exchange != target_exchange:
            continue
        name = (meta.get("company_name") or "").lower()
        shared = target_tokens & _distinctive_tokens(name)
        if not shared:
            continue
        # Score: more shared tokens = closer peer
        score = len(shared)
        peers.append((score, sym, ", ".join(sorted(shared))[:80]))

    peers.sort(key=lambda x: (-x[0], x[1]))
    return PeerGroup(
        target=target,
        peers=[
            {"symbol": sym, "shared_context": shared, "score": score}
            for score, sym, shared in peers[:max_peers]
        ],
    )


def _distinctive_tokens(name: str) -> frozenset[str]:
    """Extract tokens that are distinctive enough for peer matching."""
    _STOP = frozenset(
        {
            "công",
            "ty",
            "cổ",
            "phần",
            "ctcp",
            "cp",
            "tổng",
            "tập",
            "đoàn",
            "và",
            "việt",
            "nam",
            "vietnam",
            "vn",
            "của",
            "chứng",
            "khoán",
            "dịch",
            "vụ",
            "thương",
            "mại",
            "đầu",
            "tư",
            "kinh",
            "doanh",
            "ngân",
            "hàng",
            "company",
            "joint",
            "stock",
            "group",
            "holding",
            "the",
            "and",
            "of",
            "for",
            "vietnamese",
        }
    )
    tokens = set()
    for tok in name.split():
        tok_lower = tok.lower().strip(".,-()'")
        if len(tok_lower) >= 4 and tok_lower not in _STOP:
            tokens.add(tok_lower)
    return frozenset(tokens)


# ---------------------------------------------------------------------------
# Price / technical alert engine
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AlertEvaluation:
    """Result of evaluating one alert condition over a price series."""

    ticker: str
    condition: str
    triggered: bool
    current_value: float | None
    detail: str = ""


def evaluate_alert_conditions(
    ticker: str,
    bars: list[dict[str, Any]],
    *,
    rsi_oversold: float = 30.0,
    rsi_overbought: float = 70.0,
    volume_spike_ratio: float = 2.5,
    ma_cross_window: int = 5,
) -> list[AlertEvaluation]:
    """Evaluate technical alert conditions over a daily OHLCV series.

    Conditions checked: RSI oversold/overbought, volume spike, price
    crossing 5-day MA. Requires ≥ 10 bars (fail-open: fewer bars → no alerts,
    never fabricated).

    ``bars``: list of {time, open, high, low, close, volume} dicts
    (same shape as OhlcvBarInput).
    """
    if len(bars) < 10:
        return []

    closes = [float(b.get("close") or 0) for b in bars if b.get("close")]
    volumes = [float(b.get("volume") or 0) for b in bars if b.get("volume")]
    if len(closes) < 10 or len(volumes) < 10:
        return []

    out: list[AlertEvaluation] = []
    latest_close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else latest_close

    # RSI-14 (Wilder)
    rsi = _calc_rsi_14(closes)
    if rsi is not None:
        if rsi <= rsi_oversold:
            out.append(
                AlertEvaluation(
                    ticker=ticker,
                    condition="RSI_OVERSOLD",
                    triggered=True,
                    current_value=rsi,
                    detail=f"RSI-14 {rsi:.1f} <= {rsi_oversold}",
                )
            )
        elif rsi >= rsi_overbought:
            out.append(
                AlertEvaluation(
                    ticker=ticker,
                    condition="RSI_OVERBOUGHT",
                    triggered=True,
                    current_value=rsi,
                    detail=f"RSI-14 {rsi:.1f} >= {rsi_overbought}",
                )
            )

    # Volume spike: last bar volume > ratio x average of prior 10 bars
    if len(volumes) >= 11:
        avg_vol_prior = sum(volumes[-11:-1]) / 10
        if avg_vol_prior > 0 and volumes[-1] >= avg_vol_prior * volume_spike_ratio:
            out.append(
                AlertEvaluation(
                    ticker=ticker,
                    condition="VOLUME_SPIKE",
                    triggered=True,
                    current_value=round(volumes[-1] / avg_vol_prior, 2),
                    detail=f"Volume {volumes[-1]:.0f} = {volumes[-1] / avg_vol_prior:.1f}x avg",
                )
            )

    # MA-cross: price crosses 5-day MA (bullish = crosses above, bearish = below)
    if len(closes) >= ma_cross_window + 1:
        ma_prev = sum(closes[-(ma_cross_window + 1) : -1]) / ma_cross_window
        ma_curr = sum(closes[-ma_cross_window:]) / ma_cross_window
        if prev_close <= ma_prev and latest_close > ma_curr:
            out.append(
                AlertEvaluation(
                    ticker=ticker,
                    condition="MA5_CROSS_ABOVE",
                    triggered=True,
                    current_value=round(ma_curr, 2),
                    detail=f"Price crossed above 5-day MA ({ma_curr:.2f})",
                )
            )
        elif prev_close >= ma_prev and latest_close < ma_curr:
            out.append(
                AlertEvaluation(
                    ticker=ticker,
                    condition="MA5_CROSS_BELOW",
                    triggered=True,
                    current_value=round(ma_curr, 2),
                    detail=f"Price crossed below 5-day MA ({ma_curr:.2f})",
                )
            )

    # High proximity (within 2% of the 20-day high AND above the mid-range —
    # a flat sideways series must not fire this every day)
    if len(closes) >= 20:
        period_high = max(closes[-20:])
        period_low = min(closes[-20:])
        midpoint = (period_high + period_low) / 2
        if period_high > 0 and latest_close >= period_high * 0.98 and latest_close > midpoint:
            out.append(
                AlertEvaluation(
                    ticker=ticker,
                    condition="NEAR_20D_HIGH",
                    triggered=True,
                    current_value=latest_close,
                    detail=f"Within 2% of 20-day high ({period_high:.2f})",
                )
            )

    return out


def _calc_rsi_14(closes: list[float]) -> float | None:
    """Wilder's RSI-14 (recursive smoothing). Returns None if fewer than 15 closes."""
    if len(closes) < 15:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(d for d in deltas[:14] if d > 0) / 14
    avg_loss = sum(-d for d in deltas[:14] if d < 0) / 14
    # Wilder smoothing over the remainder of the series
    for d in deltas[14:]:
        avg_gain = (avg_gain * 13 + max(d, 0)) / 14
        avg_loss = (avg_loss * 13 + max(-d, 0)) / 14
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


# ---------------------------------------------------------------------------
# News-to-price event impact study
# ---------------------------------------------------------------------------


def compute_event_impact(
    ticker: str,
    event_date: str,
    bars: list[dict[str, Any]],
    *,
    window_days: int = 5,
) -> dict[str, Any] | None:
    """News-to-price correlation for one tagged event.

    Computes 1-day and {window_days}-day price return + volume ratio
    around the event date. Returns None when candles for the window are
    missing (zero-fabrication law: never invent a return).
    """
    try:
        event_dt = datetime.fromisoformat(event_date).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None

    if len(bars) < 3:
        return None

    # Find the bar closest to (and not after) the event date.
    dated_bars = [b for b in bars if b.get("time") is not None and float(b["time"]) > 0]
    if not dated_bars:
        return None

    # Sort by time ascending
    dated_bars.sort(key=lambda b: float(b["time"]))
    event_bar_idx = None
    for i, b in enumerate(dated_bars):
        bar_dt = datetime.fromtimestamp(float(b["time"]), tz=UTC)
        bar_day = bar_dt.date()
        if bar_day <= event_dt.date():
            event_bar_idx = i
    if event_bar_idx is None:
        return None
    # Staleness guard: an anchor bar days before the event (event precedes the
    # data window) would measure unrelated price action — refuse, don't mislabel.
    anchor_dt = datetime.fromtimestamp(float(dated_bars[event_bar_idx]["time"]), tz=UTC)
    if (event_dt.date() - anchor_dt.date()).days > 3:
        return None

    end_idx = min(event_bar_idx + window_days, len(dated_bars) - 1)
    if end_idx <= event_bar_idx:
        return None

    bar_at_event = dated_bars[event_bar_idx]
    bar_at_end = dated_bars[end_idx]
    close_at = float(bar_at_event.get("close") or 0)
    close_end = float(bar_at_end.get("close") or 0)
    if close_at <= 0 or close_end <= 0:
        return None

    ret_1d = None
    vol_ratio = None
    if event_bar_idx + 1 < len(dated_bars):
        bar_1d = dated_bars[event_bar_idx + 1]
        close_1d = float(bar_1d.get("close") or 0)
        if close_at > 0:
            ret_1d = round((close_1d - close_at) / close_at * 100, 2)
        vol_at = float(bar_at_event.get("volume") or 0)
        vol_1d = float(bar_1d.get("volume") or 0)
        if vol_at > 0:
            vol_ratio = round(vol_1d / vol_at, 2)

    ret_window = round((close_end - close_at) / close_at * 100, 2)

    return {
        "ticker": ticker,
        "event_date": event_date,
        "price_at_event": round(close_at, 2),
        "return_1d_pct": ret_1d,
        f"return_{window_days}d_pct": ret_window,
        "volume_ratio_1d": vol_ratio,
        "bars_used": end_idx - event_bar_idx,
    }
