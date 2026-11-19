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
from app.retrieval.scoring import fold_diacritics

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Peer auto-discovery
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PeerGroup:
    """Peers of a target ticker, ranked by identity-token overlap."""

    target: str
    peers: list[dict[str, Any]] = field(default_factory=list)


def discover_peers(
    tickers_meta: dict[str, dict[str, Any]],
    target: str,
    *,
    max_peers: int = 5,
    max_df_fraction: float = 0.01,
) -> PeerGroup:
    """Discover peers of ``target`` by NAME-IDENTITY overlap over the VN
    ticker registry (parent-affiliate and brand matches), deterministic and
    honest: the registry carries no industry/sector field, so this surfaces
    companies sharing a RARE identity token (e.g. "BIDV" links the bank to
    its securities arm) — not sector clusters. A generic token like "phát"
    (df 95/1518 in the real registry) can never bridge a bank and a steel
    maker; tokens rarer than ``max_df_fraction`` of the registry qualify,
    weighted by inverse document frequency.

    ``tickers_meta`` maps symbol → {company_name, short_name, exchange}.
    Diacritics are folded so "Hòa Phát" and "Hoa Phat" tokenize alike.
    """
    target_meta = tickers_meta.get(target)
    if not target_meta:
        return PeerGroup(target=target)

    total = len(tickers_meta)
    # Floor of 2 keeps tiny registries (tests, single-sector subsets) usable:
    # a token shared by exactly 2 of 4 entries is still an identity link there,
    # while on the real registry (1518) the fraction term dominates (max_df 15).
    max_df = max(2, int(total * max_df_fraction))
    target_exchange = (target_meta.get("exchange") or "").upper()
    target_tokens = _identity_tokens(target_meta)

    if not target_tokens:
        return PeerGroup(target=target)

    # One DF pass over the registry, tokenized identically to the target.
    df: dict[str, int] = {}
    tokenized: dict[str, frozenset[str]] = {}
    for sym, meta in tickers_meta.items():
        toks = _identity_tokens(meta)
        tokenized[sym] = toks
        for t in toks:
            df[t] = df.get(t, 0) + 1

    peers: list[tuple[float, str, str]] = []
    for sym, toks in tokenized.items():
        if sym == target or sym.startswith("^"):
            continue
        meta = tickers_meta[sym]
        exchange = (meta.get("exchange") or "").upper()
        if target_exchange and exchange != target_exchange:
            continue
        shared = target_tokens & toks
        qualifying = [t for t in shared if df.get(t, 0) <= max_df]
        if not qualifying:
            continue
        # Rare shared token = strong identity link; sum of inverse DF weights.
        score = sum(1.0 / df[t] for t in qualifying)
        peers.append((score, sym, ", ".join(sorted(qualifying))[:80]))

    peers.sort(key=lambda x: (-x[0], x[1]))
    return PeerGroup(
        target=target,
        peers=[
            {
                "symbol": sym,
                "shared_context": shared,
                "score": round(score, 4),
            }
            for score, sym, shared in peers[:max_peers]
        ],
    )


def _identity_tokens(meta: dict[str, Any]) -> frozenset[str]:
    """Folded, stop-worded tokens from company_name + short_name merged."""
    _STOP: frozenset[str] = frozenset(
        {
            # Generic Vietnamese corporate vocabulary (FOLDED — "công"→"cong").
            "cong",
            "ty",
            "co",
            "phan",
            "tnhh",
            "ctcp",
            "tmcp",
            "tong",
            "tap",
            "doan",
            "va",
            "viet",
            "nam",
            "vietnam",
            "vn",
            "cua",
            "chung",
            "khoan",
            "dich",
            "vu",
            "thuong",
            "mai",
            "dau",
            "tu",
            "kinh",
            "doanh",
            "ngan",
            "hang",
            # English corporate vocabulary.
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
    tokens: set[str] = set()
    for src in (meta.get("company_name") or "", meta.get("short_name") or ""):
        for tok in fold_diacritics(src.lower()).split():
            tok = tok.strip(".,-()\"'")
            if len(tok) >= 3 and tok not in _STOP:
                tokens.add(tok)
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

    # Filter ONCE over bars having BOTH close and volume so the two series
    # always stay index-aligned (a mid-series bar missing one field must not
    # desynchronize RSI's window from the volume window).
    usable = [b for b in bars if b.get("close") and b.get("volume") is not None]
    closes = [float(b["close"]) for b in usable]
    volumes = [float(b["volume"] or 0) for b in usable]
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

    # Closing-high proximity (within 2% of the 20-day CLOSING high AND above
    # the mid-range — a flat sideways series must not fire this every day;
    # computed on closes, hence the explicit CLOSE naming)
    if len(closes) >= 20:
        period_high = max(closes[-20:])
        period_low = min(closes[-20:])
        midpoint = (period_high + period_low) / 2
        if period_high > 0 and latest_close >= period_high * 0.98 and latest_close > midpoint:
            out.append(
                AlertEvaluation(
                    ticker=ticker,
                    condition="NEAR_20D_CLOSE_HIGH",
                    triggered=True,
                    current_value=latest_close,
                    detail=f"Within 2% of 20-day closing high ({period_high:.2f})",
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
        parsed = datetime.fromisoformat(event_date)
        # Date-only strings are naive → UTC. Already-tz-aware inputs convert
        # (replace would silently re-label the wall clock).
        event_dt = parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None

    if len(bars) < 3:
        return None

    # Bar timestamps: VNDIRECT dchart (the only OHLCV feed wired here) emits
    # 00:00-UTC epochs whose UTC date == VN trading date (verified 2026-11-20,
    # 86400-multiples with weekend gaps). If another feed ever shifts to
    # VN-local-midnight epochs, the anchor below would pick one trading day
    # late — normalize through the feed's own convention before extending.
    dated_bars = [b for b in bars if b.get("time") is not None and float(b["time"]) > 0]
    if not dated_bars:
        return None

    # Sort by time ascending, then pick the last bar on or before the event.
    dated_bars.sort(key=lambda b: float(b["time"]))
    event_bar_idx = None
    for i, b in enumerate(dated_bars):
        bar_day = datetime.fromtimestamp(float(b["time"]), tz=UTC).date()
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

    # Honest horizon: when fewer forward bars than the window exist, the
    # "{window}d return" would actually be a shorter-horizon return — null it
    # (bars_used keeps the true span) instead of mislabeling.
    bars_spanned = end_idx - event_bar_idx
    ret_window = (
        round((close_end - close_at) / close_at * 100, 2) if bars_spanned >= window_days else None
    )

    return {
        "ticker": ticker,
        "event_date": event_date,
        "price_at_event": round(close_at, 2),
        "return_1d_pct": ret_1d,
        f"return_{window_days}d_pct": ret_window,
        "volume_ratio_1d": vol_ratio,
        "bars_used": bars_spanned,
    }
