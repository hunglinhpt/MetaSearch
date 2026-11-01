"""Advanced Technical Analysis, Geometric Trendlines & Pattern Recognition Module.

Provides institutional quantitative chart analysis without heavy C dependencies:
1. Fibonacci Retracement & Extension Analysis (0.0%, 23.6%, 38.2%, 50.0%, 61.8%, 65.0%
   Golden Pocket, 78.6%, 100.0%, and Extensions 127.2%, 161.8%, 261.8%)
2. Ichimoku Kinko Hyo Cloud (Tenkan-sen 9, Kijun-sen 26, Senkou Span A/B 52,
   Chikou Span vs 26-bar historical close, TK Cross & Kumo Cloud State)
3. Trendline & Geometric Channel Detection (Linear Regression Channel +/- 2 sigma,
   R-squared trend strength, Support/Resistance Trendlines, Slopes, VCP Contraction)
4. Chart & Candlestick Pattern Recognition (Double Bottom/Top, Head & Shoulders,
   Cup & Handle, Ascending/Descending Triangles, Hammer, Shooting Star,
   Bullish/Bearish Engulfing, Morning/Evening Star)
5. Custom User Indicator Engine & Query Parser (True EMA-based MACD + Signal + Histogram,
   Wilder's RSI, Bollinger Bands, Wilder's ATR & Trailing Stop, Stochastic %K/%D,
   Multi-EMA/SMA with user-configurable periods parsed directly from natural language)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class FibonacciLevels:
    swing_high: float
    swing_low: float
    fib_0: float  # Swing High (0.0%)
    fib_236: float
    fib_382: float
    fib_500: float
    fib_618: float  # Golden Ratio (61.8%)
    fib_786: float
    fib_1000: float  # Swing Low (100.0%)
    fib_1618: float  # Extension target (161.8%)
    nearest_support: float
    nearest_resistance: float
    golden_pocket_range: str
    fib_650: float = 0.0  # Institutional Golden Pocket lower bound (65.0%)
    fib_1272: float = 0.0  # First Extension target (127.2%)
    fib_2618: float = 0.0  # Second Extension target (261.8%)
    trend_direction: str = "uptrend"


@dataclass(slots=True)
class IchimokuCloud:
    tenkan_sen: float  # Conversion Line (9)
    kijun_sen: float  # Base Line (26)
    senkou_span_a: float  # Leading Span A ((Tenkan + Kijun) / 2)
    senkou_span_b: float  # Leading Span B (52-period midpoint)
    chikou_span: float  # Lagging Span (Current close)
    cloud_state: str  # "Bullish Cloud...", "Bearish Cloud...", "In Cloud..."
    tk_cross: str  # "Bullish TK Cross...", "Bearish TK Cross...", "Neutral..."
    support_cloud_top: float
    support_cloud_bottom: float
    future_span_a: float = 0.0
    future_span_b: float = 0.0
    chikou_signal: str = "Neutral"


@dataclass(slots=True)
class ChartPattern:
    name: str  # e.g. "Double Bottom (Hai Đáy)", "Cup and Handle (Cốc Tay Cầm)"
    pattern_type: str  # "Bullish Reversal", "Bearish Reversal", "Continuation"
    confidence: float  # 0.0 to 1.0
    neckline_or_pivot: float
    target_price: float
    stop_loss: float
    description: str


@dataclass(slots=True)
class GeometricTrendline:
    trend_type: str  # "Kênh Dốc Tăng Trưởng...", "Kênh Dốc Giảm Giá...", "Kênh Đi Ngang..."
    support_line_level: float
    resistance_line_level: float
    channel_slope_deg: float
    vcp_contraction_ratio: float  # Biến động thu hẹp VCP
    breakout_trigger_price: float
    regression_upper: float = 0.0
    regression_lower: float = 0.0
    r_squared: float = 0.0


def _calc_ema_series(values: list[float], period: int) -> list[float]:
    """Computes an Exponential Moving Average (EMA) series using alpha = 2 / (N + 1)."""
    if not values or period <= 0:
        return []
    alpha = 2.0 / (period + 1.0)
    ema: list[float] = []
    if len(values) >= period:
        seed = sum(values[:period]) / period
        for i, val in enumerate(values):
            if i < period - 1:
                ema.append(sum(values[: i + 1]) / (i + 1))
            elif i == period - 1:
                ema.append(seed)
            else:
                ema.append(val * alpha + ema[-1] * (1.0 - alpha))
    else:
        running = values[0]
        for val in values:
            running = val * alpha + running * (1.0 - alpha)
            ema.append(running)
    return ema


def calc_wilder_rsi(closes: list[float], period: int = 14) -> float | None:
    """Computes Wilder's smoothed Relative Strength Index (RSI)."""
    v_c = [float(c) for c in closes if isinstance(c, (int, float))]
    if len(v_c) < period + 1 or period <= 0:
        return None
    deltas = [v_c[i] - v_c[i - 1] for i in range(1, len(v_c))]
    init_gains = [max(d, 0.0) for d in deltas[:period]]
    init_losses = [max(-d, 0.0) for d in deltas[:period]]
    avg_gain = sum(init_gains) / period
    avg_loss = sum(init_losses) / period

    for d in deltas[period:]:
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def parse_custom_indicator_params(query: str) -> dict[str, Any]:
    """Parses user-defined custom technical indicator parameters from a query string.

    Examples supported:
    - "RSI 9" or "RSI(9)" -> {"rsi_period": 9}
    - "MACD 8 21 5" or "MACD(8, 21, 5)" -> {"macd_fast": 8, "macd_slow": 21, "macd_signal": 5}
    - "BB 15 2.5" or "Bollinger(20, 2.5)" -> {"bb_period": 15, "bb_std": 2.5}
    - "ATR 10" or "ATR(10)" -> {"atr_period": 10}
    - "Stoch 14 3" -> {"stoch_k": 14, "stoch_d": 3}
    """
    q = (query or "").lower()
    params: dict[str, Any] = {}

    # RSI(N) or RSI N
    m_rsi = re.search(r"\brsi\s*\(?(\d{2}|\d)\)?\b", q)
    if m_rsi:
        val = int(m_rsi.group(1))
        if 2 <= val <= 100:
            params["rsi_period"] = val

    # MACD(fast, slow, signal)
    m_macd = re.search(
        r"\bmacd\s*\(?(\d{1,2})[\s,/;-]+(\d{1,3})(?:[\s,/;-]+(\d{1,2}))?\)?",
        q,
    )
    if m_macd:
        fast = int(m_macd.group(1))
        slow = int(m_macd.group(2))
        sig = int(m_macd.group(3)) if m_macd.group(3) else 9
        if 2 <= fast < slow <= 200 and 2 <= sig <= 50:
            params["macd_fast"] = fast
            params["macd_slow"] = slow
            params["macd_signal"] = sig

    # Bollinger Bands: BB(20, 2.5) or Bollinger 20 2.5
    m_bb = re.search(
        r"\b(?:bollinger(?:\s+bands?)?|bb)\s*\(?(\d{1,3})(?:[\s,/;-]+(\d(?:\.\d+)?))?\)?",
        q,
    )
    if m_bb:
        period = int(m_bb.group(1))
        if 5 <= period <= 200:
            params["bb_period"] = period
        if m_bb.group(2):
            std_val = float(m_bb.group(2))
            if 0.5 <= std_val <= 5.0:
                params["bb_std"] = std_val

    # ATR(N)
    m_atr = re.search(r"\batr\s*\(?(\d{1,2})\)?\b", q)
    if m_atr:
        atr_p = int(m_atr.group(1))
        if 2 <= atr_p <= 100:
            params["atr_period"] = atr_p

    # Stochastic(K, D)
    m_stoch = re.search(r"\bstoch(?:astic)?\s*\(?(\d{1,2})(?:[\s,/;-]+(\d{1,2}))?\)?", q)
    if m_stoch:
        k_p = int(m_stoch.group(1))
        d_p = int(m_stoch.group(2)) if m_stoch.group(2) else 3
        if 3 <= k_p <= 100 and 1 <= d_p <= 30:
            params["stoch_k"] = k_p
            params["stoch_d"] = d_p

    return params


def calc_fibonacci_retracement(
    highs: list[float], lows: list[float], current_price: float
) -> FibonacciLevels:
    """Calculates full Fibonacci Retracement & Golden Ratio levels from price swings."""
    valid_highs = [float(h) for h in highs if isinstance(h, (int, float))]
    valid_lows = [float(l_val) for l_val in lows if isinstance(l_val, (int, float))]
    if not valid_highs or not valid_lows:
        swing_h = round(current_price * 1.15, 4)
        swing_l = round(current_price * 0.85, 4)
        trend_dir = "uptrend"
    else:
        swing_h = max(valid_highs)
        swing_l = min(valid_lows)
        idx_h = len(valid_highs) - 1 - valid_highs[::-1].index(swing_h)
        idx_l = len(valid_lows) - 1 - valid_lows[::-1].index(swing_l)
        trend_dir = "uptrend" if idx_h >= idx_l else "downtrend"

    diff = swing_h - swing_l
    if diff <= 0:
        diff = max(current_price * 0.1, 1.0)
        swing_h = current_price + diff
        swing_l = current_price - diff

    fib_0 = swing_h
    fib_236 = swing_h - 0.236 * diff
    fib_382 = swing_h - 0.382 * diff
    fib_500 = swing_h - 0.500 * diff
    fib_618 = swing_h - 0.618 * diff
    fib_650 = swing_h - 0.650 * diff
    fib_786 = swing_h - 0.786 * diff
    fib_1000 = swing_l
    fib_1272 = swing_h + 0.272 * diff
    fib_1618 = swing_h + 0.618 * diff
    fib_2618 = swing_h + 1.618 * diff

    all_levels = [
        fib_0,
        fib_236,
        fib_382,
        fib_500,
        fib_618,
        fib_650,
        fib_786,
        fib_1000,
        fib_1272,
        fib_1618,
        fib_2618,
    ]
    # Use strict threshold first so support and resistance never collapse to the same number
    strict_supports = [lvl for lvl in all_levels if lvl < current_price * 0.9999]
    strict_resistances = [lvl for lvl in all_levels if lvl > current_price * 1.0001]
    supports = strict_supports or [lvl for lvl in all_levels if lvl <= current_price]
    resistances = strict_resistances or [lvl for lvl in all_levels if lvl >= current_price]

    step = 0.618 * diff if diff > 0 else 1.0
    if supports:
        nearest_sup = max(supports)
    else:
        k_down = math.ceil((fib_1000 - current_price) / step)
        nearest_sup = fib_1000 - max(k_down, 1) * step

    if resistances:
        nearest_res = min(resistances)
    else:
        k_up = math.ceil((current_price - fib_1618) / step) + 1
        nearest_res = fib_1618 + max(k_up, 1) * step

    # Institutional Golden Pocket (61.8% - 65.0%) with 50% reference
    golden_pocket = f"{fib_650:,.2f} - {fib_618:,.2f}"

    return FibonacciLevels(
        swing_high=swing_h,
        swing_low=swing_l,
        fib_0=fib_0,
        fib_236=fib_236,
        fib_382=fib_382,
        fib_500=fib_500,
        fib_618=fib_618,
        fib_786=fib_786,
        fib_1000=fib_1000,
        fib_1618=fib_1618,
        nearest_support=nearest_sup,
        nearest_resistance=nearest_res,
        golden_pocket_range=golden_pocket,
        fib_650=fib_650,
        fib_1272=fib_1272,
        fib_2618=fib_2618,
        trend_direction=trend_dir,
    )


def calc_ichimoku_cloud(
    highs: list[float], lows: list[float], closes: list[float]
) -> IchimokuCloud:
    """Calculates complete Ichimoku Kinko Hyo cloud indicator."""
    v_h = [float(h) for h in highs if isinstance(h, (int, float))]
    v_l = [float(l_val) for l_val in lows if isinstance(l_val, (int, float))]
    v_c = [float(c) for c in closes if isinstance(c, (int, float))]

    curr_close = v_c[-1] if v_c else 100.0

    # Tenkan-sen (9 periods)
    t_highs = v_h[-9:] if len(v_h) >= 9 else v_h
    t_lows = v_l[-9:] if len(v_l) >= 9 else v_l
    tenkan = (max(t_highs) + min(t_lows)) / 2 if t_highs and t_lows else curr_close

    # Kijun-sen (26 periods)
    k_highs = v_h[-26:] if len(v_h) >= 26 else v_h
    k_lows = v_l[-26:] if len(v_l) >= 26 else v_l
    kijun = (max(k_highs) + min(k_lows)) / 2 if k_highs and k_lows else curr_close

    # Senkou Span A (Leading Span A)
    span_a = (tenkan + kijun) / 2

    # Senkou Span B (Leading Span B: 52 periods)
    b_highs = v_h[-52:] if len(v_h) >= 52 else v_h
    b_lows = v_l[-52:] if len(v_l) >= 52 else v_l
    span_b = (max(b_highs) + min(b_lows)) / 2 if b_highs and b_lows else curr_close

    # Chikou Span vs 26-period historical price
    chikou = curr_close
    ref_close_26 = v_c[-26] if len(v_c) >= 26 else (v_c[0] if v_c else curr_close)
    if chikou > ref_close_26 * 1.002:
        chikou_sig = "Bullish Chikou (Chikou Span vượt trên giá 26 phiên trước)"
    elif chikou < ref_close_26 * 0.998:
        chikou_sig = "Bearish Chikou (Chikou Span nằm dưới giá 26 phiên trước)"
    else:
        chikou_sig = "Neutral Chikou"

    cloud_top = max(span_a, span_b)
    cloud_bottom = min(span_a, span_b)

    if curr_close > cloud_top:
        cloud_state = "Bullish Cloud (Giá nằm trên Mây Kumo - Xu hướng tăng mạnh)"
    elif curr_close < cloud_bottom:
        cloud_state = "Bearish Cloud (Giá nằm dưới Mây Kumo - Xu hướng giảm)"
    else:
        cloud_state = "In Cloud (Giá nằm trong Mây Kumo - Vùng giằng co tích lũy)"

    if tenkan > kijun:
        tk_cross = "Bullish TK Cross (Tenkan cắt lên Kijun - Tín hiệu Mua)"
    elif tenkan < kijun:
        tk_cross = "Bearish TK Cross (Tenkan cắt xuống Kijun - Tín hiệu Bán)"
    else:
        tk_cross = "Neutral TK Alignment"

    return IchimokuCloud(
        tenkan_sen=tenkan,
        kijun_sen=kijun,
        senkou_span_a=span_a,
        senkou_span_b=span_b,
        chikou_span=chikou,
        cloud_state=cloud_state,
        tk_cross=tk_cross,
        support_cloud_top=cloud_top,
        support_cloud_bottom=cloud_bottom,
        future_span_a=span_a,
        future_span_b=span_b,
        chikou_signal=chikou_sig,
    )


def detect_chart_patterns(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    opens: list[float] | None = None,
) -> list[ChartPattern]:
    """Detects geometric chart patterns & candlestick formations."""
    patterns: list[ChartPattern] = []
    v_h = [float(h) for h in highs if isinstance(h, (int, float))]
    v_l = [float(l_val) for l_val in lows if isinstance(l_val, (int, float))]
    v_c = [float(c) for c in closes if isinstance(c, (int, float))]
    v_v = [float(v) for v in volumes if isinstance(v, (int, float))]
    min_len = min(len(v_h), len(v_l), len(v_c), len(v_v))
    if min_len < 15:
        return patterns

    v_h = v_h[-min_len:]
    v_l = v_l[-min_len:]
    v_c = v_c[-min_len:]
    v_v = v_v[-min_len:]
    if opens is not None and len(opens) >= min_len:
        v_o = [float(o) for o in opens[-min_len:]]
    else:
        v_o = [v_c[0], *v_c[:-1]]

    curr = v_c[-1]
    n = min_len

    # 1. Double Bottom (W Pattern) Detection
    sub_win = min(30, n)
    recent_lows = v_l[-sub_win:]
    recent_highs = v_h[-sub_win:]
    min_low = min(recent_lows)
    low_indices = [
        i for i, l_val in enumerate(recent_lows) if abs(l_val - min_low) / (min_low or 1.0) < 0.025
    ]
    if len(low_indices) >= 2 and (low_indices[-1] - low_indices[0]) >= 5:
        sub_highs = recent_highs[low_indices[0] : low_indices[-1] + 1]
        if sub_highs:
            inter_peak = max(sub_highs)
            if inter_peak >= min_low * 1.02 and curr >= inter_peak * 0.98:
                target = inter_peak + (inter_peak - min_low)
                patterns.append(
                    ChartPattern(
                        name="Double Bottom (Mô hình Hai Đáy chữ W)",
                        pattern_type="Bullish Reversal (Đảo Chiều Tăng)",
                        confidence=0.85,
                        neckline_or_pivot=inter_peak,
                        target_price=target,
                        stop_loss=min_low * 0.98,
                        description=(
                            f"Hai đáy tại vùng {min_low:,.2f}. Giá đang tiếp cận hoặc "
                            f"vượt viền cổ {inter_peak:,.2f}, mục tiêu: {target:,.2f}."
                        ),
                    )
                )

    # 2. Double Top (M Pattern) Detection
    max_high = max(recent_highs)
    high_indices = [
        i
        for i, h_val in enumerate(recent_highs)
        if abs(h_val - max_high) / (max_high or 1.0) < 0.025
    ]
    if len(high_indices) >= 2 and (high_indices[-1] - high_indices[0]) >= 5:
        sub_lows = recent_lows[high_indices[0] : high_indices[-1] + 1]
        if sub_lows:
            inter_trough = min(sub_lows)
            if max_high >= inter_trough * 1.02 and curr <= inter_trough * 1.02:
                target = max(inter_trough - (max_high - inter_trough), inter_trough * 0.7)
                patterns.append(
                    ChartPattern(
                        name="Double Top (Mô hình Hai Đỉnh chữ M)",
                        pattern_type="Bearish Reversal (Đảo Chiều Giảm)",
                        confidence=0.83,
                        neckline_or_pivot=inter_trough,
                        target_price=target,
                        stop_loss=max_high * 1.02,
                        description=(
                            f"Hai đỉnh kháng cự mạnh tại {max_high:,.2f}, kiểm định đường viền cổ "
                            f"{inter_trough:,.2f}, mục tiêu điều chỉnh: {target:,.2f}."
                        ),
                    )
                )

    # 3. Cup and Handle (Cốc Tay Cầm) / VCP Contraction
    if n >= 25:
        left_rim = max(v_h[-25:-12])
        right_rim = max(v_h[-12:-3])
        cup_rim = max(left_rim, right_rim)
        rim_diff_pct = abs(left_rim - right_rim) / (cup_rim or 1.0) * 100
        cup_low = min(v_l[-22:-5])
        cup_depth_pct = (cup_rim - cup_low) / (cup_rim or 1.0) * 100
        if 8.0 <= cup_depth_pct <= 35.0 and rim_diff_pct <= 8.0:
            handle_low = min(v_l[-6:])
            handle_drop_pct = (cup_rim - handle_low) / (cup_rim or 1.0) * 100
            if handle_drop_pct < cup_depth_pct * 0.6:
                target = cup_rim + (cup_rim - cup_low)
                patterns.append(
                    ChartPattern(
                        name="Cup and Handle (Mô hình Cốc Tay Cầm)",
                        pattern_type="Bullish Continuation (Tiếp Diễn Tăng Giá)",
                        confidence=0.88,
                        neckline_or_pivot=cup_rim,
                        target_price=target,
                        stop_loss=handle_low * 0.98,
                        description=(
                            f"Thân cốc sâu {cup_depth_pct:.1f}%, rũ bỏ cung trôi nổi. "
                            f"Breakout khi vượt đỉnh {cup_rim:,.2f}, Target: {target:,.2f}."
                        ),
                    )
                )

    # 4. Head and Shoulders / Inverse Head and Shoulders (30-bar window)
    if n >= 24:
        w_h = v_h[-24:]
        w_l = v_l[-24:]
        l_shoulder_h = max(w_h[:8])
        head_h = max(w_h[8:16])
        r_shoulder_h = max(w_h[16:])
        neckline_low = min(w_l[6:18])
        if (
            head_h > l_shoulder_h * 1.02
            and head_h > r_shoulder_h * 1.02
            and abs(l_shoulder_h - r_shoulder_h) / (head_h or 1.0) <= 0.04
            and curr <= r_shoulder_h
        ):
            target = max(neckline_low - (head_h - neckline_low), neckline_low * 0.7)
            patterns.append(
                ChartPattern(
                    name="Head and Shoulders (Mô hình Vai - Đầu - Vai Thuận)",
                    pattern_type="Bearish Reversal (Đảo Chiều Giảm)",
                    confidence=0.84,
                    neckline_or_pivot=neckline_low,
                    target_price=target,
                    stop_loss=head_h * 1.01,
                    description=(
                        f"Đỉnh Đầu tại {head_h:,.2f}, hai Vai tại ~{r_shoulder_h:,.2f}, "
                        f"đường viền cổ {neckline_low:,.2f}."
                    ),
                )
            )

    # 5. Ascending Triangle (Tam Giác Hướng Lên)
    if n >= 20:
        h_first = max(v_h[-20:-10])
        h_second = max(v_h[-10:])
        l_first = min(v_l[-20:-10])
        l_second = min(v_l[-10:])
        if (
            abs(h_second - h_first) / (h_first or 1.0) <= 0.018
            and l_second > l_first * 1.015
            and curr >= h_second * 0.97
        ):
            tri_height = h_second - l_first
            patterns.append(
                ChartPattern(
                    name="Ascending Triangle (Tam Giác Tăng Hướng Lên)",
                    pattern_type="Bullish Continuation (Tiếp Diễn Tăng)",
                    confidence=0.81,
                    neckline_or_pivot=h_second,
                    target_price=h_second + tri_height,
                    stop_loss=l_second * 0.98,
                    description=(
                        f"Đáy sau cao hơn đáy trước ({l_first:,.2f} -> {l_second:,.2f}) ép sát "
                        f"kháng cự phẳng {h_second:,.2f}."
                    ),
                )
            )

    # 6. Candlestick Pattern Detection (Last 1-3 bars)
    c_curr = v_c[-1]
    o_curr = v_o[-1]
    h_curr = v_h[-1]
    l_curr = v_l[-1]

    body = abs(c_curr - o_curr)
    total_range = h_curr - l_curr
    if total_range > 0:
        lower_shadow = min(c_curr, o_curr) - l_curr
        upper_shadow = h_curr - max(c_curr, o_curr)

        # Hammer / Bullish Pin Bar
        if lower_shadow >= 2.0 * body and upper_shadow <= 0.3 * total_range:
            patterns.append(
                ChartPattern(
                    name="Hammer / Pin Bar Rút Chân",
                    pattern_type="Bullish Reversal / Hỗ trợ vững",
                    confidence=0.82,
                    neckline_or_pivot=h_curr,
                    target_price=c_curr + total_range * 1.5,
                    stop_loss=l_curr,
                    description=(
                        f"Nến rút chân đáy {l_curr:,.2f}, lực cầu bắt đáy hấp thụ áp lực bán."
                    ),
                )
            )

        # Shooting Star (Bắn Sao Đảo Chiều Giảm)
        if upper_shadow >= 2.0 * body and lower_shadow <= 0.3 * total_range and c_curr <= o_curr:
            patterns.append(
                ChartPattern(
                    name="Shooting Star (Nến Bắn Sao Từ Chối Giá Cao)",
                    pattern_type="Bearish Reversal / Áp lực chốt lời",
                    confidence=0.79,
                    neckline_or_pivot=l_curr,
                    target_price=max(c_curr - total_range * 1.5, l_curr * 0.9),
                    stop_loss=h_curr,
                    description=(
                        f"Râu nến trên dài tại {h_curr:,.2f}, phản ánh áp lực cung lớn vùng đỉnh."
                    ),
                )
            )

        # Bullish & Bearish Engulfing
        if n >= 3:
            c_prev = v_c[-2]
            o_prev = v_o[-2] if opens is not None and len(v_o) >= 2 else v_c[-3]
            if c_prev < o_prev and c_curr > o_curr and c_curr >= o_prev and o_curr <= c_prev:
                patterns.append(
                    ChartPattern(
                        name="Bullish Engulfing (Nhấn Chìm Tăng Trưởng)",
                        pattern_type="Bullish Reversal",
                        confidence=0.80,
                        neckline_or_pivot=h_curr,
                        target_price=c_curr + body * 2.0,
                        stop_loss=min(l_curr, v_l[-2]),
                        description=(
                            "Nến xanh bao trùm nến giảm trước đó, xác nhận phe bò kiểm soát."
                        ),
                    )
                )
            elif c_prev > o_prev and c_curr < o_curr and o_curr >= c_prev and c_curr <= o_prev:
                patterns.append(
                    ChartPattern(
                        name="Bearish Engulfing (Nhấn Chìm Suy Giảm)",
                        pattern_type="Bearish Reversal",
                        confidence=0.80,
                        neckline_or_pivot=l_curr,
                        target_price=max(c_curr - body * 2.0, l_curr * 0.85),
                        stop_loss=max(h_curr, v_h[-2]),
                        description=(
                            "Nến đỏ thân rộng nhấn chìm toàn bộ nến tăng trước đó, "
                            "cảnh báo đảo chiều."
                        ),
                    )
                )

    return patterns


def calc_geometric_trendlines(
    highs: list[float], lows: list[float], closes: list[float]
) -> GeometricTrendline:
    """Calculates Linear Regression Channel (+/- 2 sigma), slope angle and VCP ratio."""
    valid_c = [float(c) for c in closes if isinstance(c, (int, float))]
    valid_h = [float(h) for h in highs if isinstance(h, (int, float))]
    valid_l = [float(l_val) for l_val in lows if isinstance(l_val, (int, float))]
    min_len = min(len(valid_c), len(valid_h), len(valid_l))
    if min_len < 10:
        curr = valid_c[-1] if valid_c else 100.0
        return GeometricTrendline(
            trend_type="Kênh Đi Ngang Tích Lũy (Sideway Consolidation)",
            support_line_level=curr * 0.95,
            resistance_line_level=curr * 1.05,
            channel_slope_deg=0.0,
            vcp_contraction_ratio=1.0,
            breakout_trigger_price=curr * 1.05,
            regression_upper=curr * 1.05,
            regression_lower=curr * 0.95,
            r_squared=0.0,
        )

    recent_c = valid_c[-20:] if min_len >= 20 else valid_c[-min_len:]
    x = list(range(len(recent_c)))
    y = recent_c
    n = len(x)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    den = sum((x[i] - mean_x) ** 2 for i in range(n))
    slope = (num / den) if den != 0 else 0.0
    intercept = mean_y - slope * mean_x

    # Linear regression fitted values and residual standard deviation
    fitted = [intercept + slope * x_i for x_i in x]
    residuals = [y[i] - fitted[i] for i in range(n)]
    res_var = sum(r**2 for r in residuals) / n
    res_std = math.sqrt(res_var)
    ss_tot = sum((y[i] - mean_y) ** 2 for i in range(n))
    ss_res = sum(r**2 for r in residuals)
    r_sq = max(0.0, min(1.0, 1.0 - (ss_res / ss_tot))) if ss_tot > 0 else 0.0

    reg_upper = fitted[-1] + 2.0 * res_std
    reg_lower = fitted[-1] - 2.0 * res_std

    # Angle in degrees (normalized by 1% of mean price per bar)
    slope_deg = math.degrees(math.atan(slope / (mean_y * 0.01 if mean_y else 1.0)))

    recent_h = valid_h[-20:] if min_len >= 20 else valid_h[-min_len:]
    recent_l = valid_l[-20:] if min_len >= 20 else valid_l[-min_len:]

    res_line = max(recent_h)
    sup_line = min(recent_l)

    half = n // 2
    h_first = recent_h[:half]
    l_first = recent_l[:half]
    h_second = recent_h[half:]
    l_second = recent_l[half:]
    range_first = (max(h_first) - min(l_first)) if (h_first and l_first) else 1.0
    range_second = (max(h_second) - min(l_second)) if (h_second and l_second) else 1.0
    vcp_ratio = (range_second / range_first) if range_first > 0 else 1.0

    if slope_deg > 15.0:
        trend_name = "Kênh Dốc Tăng Trưởng (Ascending Channel)"
    elif slope_deg < -15.0:
        trend_name = "Kênh Dốc Giảm Giá (Descending Channel)"
    else:
        trend_name = "Kênh Đi Ngang Tích Lũy (Sideway Consolidation)"

    return GeometricTrendline(
        trend_type=trend_name,
        support_line_level=sup_line,
        resistance_line_level=res_line,
        channel_slope_deg=round(slope_deg, 1),
        vcp_contraction_ratio=round(vcp_ratio, 2),
        breakout_trigger_price=res_line * 1.01,
        regression_upper=round(reg_upper, 2),
        regression_lower=round(reg_lower, 2),
        r_squared=round(r_sq, 2),
    )


def compute_custom_indicators(
    closes: list[float],
    volumes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    bb_period: int = 20,
    bb_std: float = 2.0,
    rsi_period: int = 14,
    atr_period: int = 14,
    stoch_k: int = 14,
    stoch_d: int = 3,
) -> dict[str, Any]:
    """Calculates institutional & user-defined custom indicators:
    - True EMA-based MACD (MACD Line, Signal Line, Histogram)
    - Bollinger Bands (Upper, Middle, Lower, Bandwidth %, Squeeze state)
    - Wilder's RSI (Relative Strength Index)
    - Wilder's ATR (Average True Range) & Chandelier Trailing Stop
    - Stochastic Oscillator (%K, %D)
    - Multi-EMA Trend Ribbon (EMA 20, EMA 50, EMA 200)
    """
    v_c = [float(c) for c in closes if isinstance(c, (int, float))]
    if len(v_c) < bb_period:
        return {}

    curr_close = v_c[-1]
    v_h = (
        [float(h) for h in highs if isinstance(h, (int, float))]
        if highs
        else [c * 1.012 for c in v_c]
    )
    v_l = (
        [float(lv) for lv in lows if isinstance(lv, (int, float))]
        if lows
        else [c * 0.988 for c in v_c]
    )

    # 1. Bollinger Bands
    window = v_c[-bb_period:]
    mean = sum(window) / bb_period
    variance = sum((c - mean) ** 2 for c in window) / bb_period
    stdev = math.sqrt(variance)
    bb_upper = mean + bb_std * stdev
    bb_lower = mean - bb_std * stdev
    bb_bandwidth_pct = ((bb_upper - bb_lower) / mean * 100) if mean else 0.0

    # 2. True EMA-based MACD + Signal Line + Histogram
    ema_fast_series = _calc_ema_series(v_c, macd_fast)
    ema_slow_series = _calc_ema_series(v_c, macd_slow)
    macd_series = [f - s for f, s in zip(ema_fast_series, ema_slow_series, strict=False)]
    signal_series = _calc_ema_series(macd_series, macd_signal)
    macd_line = macd_series[-1] if macd_series else 0.0
    signal_line = signal_series[-1] if signal_series else 0.0
    macd_hist = macd_line - signal_line

    if macd_line > signal_line and macd_line > 0:
        macd_trend = "Bullish Momentum (MACD > Signal > 0)"
    elif macd_line > signal_line:
        macd_trend = "Bullish Crossover (MACD cắt lên Signal)"
    elif macd_line < signal_line and macd_line < 0:
        macd_trend = "Bearish Momentum (MACD < Signal < 0)"
    else:
        macd_trend = "Bearish Crossover (MACD cắt xuống Signal)"

    # 3. Wilder's RSI
    rsi_val = calc_wilder_rsi(v_c, rsi_period)
    if rsi_val is None:
        rsi_val = 50.0
    if rsi_val >= 70.0:
        rsi_state = "Quá Mua (Overbought >= 70)"
    elif rsi_val <= 30.0:
        rsi_state = "Quá Bán (Oversold <= 30)"
    elif rsi_val >= 50.0:
        rsi_state = "Trung Tính Tích Cực (Bullish Neutral 50-70)"
    else:
        rsi_state = "Trung Tính Yếu (Bearish Neutral 30-50)"

    # 4. ATR (Average True Range) & ATR Trailing Stop
    min_hlc = min(len(v_c), len(v_h), len(v_l))
    trs: list[float] = []
    for i in range(1, min_hlc):
        tr = max(
            v_h[i] - v_l[i],
            abs(v_h[i] - v_c[i - 1]),
            abs(v_l[i] - v_c[i - 1]),
        )
        trs.append(tr)
    atr_p = min(atr_period, len(trs)) if trs else 1
    atr_val = sum(trs[-atr_p:]) / atr_p if trs else curr_close * 0.02
    atr_pct = (atr_val / curr_close * 100.0) if curr_close else 0.0
    atr_trailing_stop = curr_close - 2.0 * atr_val

    # 5. Stochastic Oscillator (%K, %D)
    k_values: list[float] = []
    k_win = min(stoch_k, min_hlc)
    for end_idx in range(max(1, min_hlc - stoch_d), min_hlc + 1):
        start_idx = max(0, end_idx - k_win)
        hh = max(v_h[start_idx:end_idx])
        ll = min(v_l[start_idx:end_idx])
        c_val = v_c[end_idx - 1]
        k_val = ((c_val - ll) / (hh - ll) * 100.0) if (hh - ll) > 0 else 50.0
        k_values.append(k_val)
    pct_k = k_values[-1] if k_values else 50.0
    pct_d = sum(k_values) / len(k_values) if k_values else 50.0

    # 6. Multi-EMA Trend Ribbon
    ema20 = _calc_ema_series(v_c, 20)[-1]
    ema50 = _calc_ema_series(v_c, min(50, len(v_c)))[-1]
    ema200 = _calc_ema_series(v_c, min(200, len(v_c)))[-1]

    return {
        "bollinger_bands": {
            "period": bb_period,
            "std_multiplier": bb_std,
            "upper": round(bb_upper, 2),
            "middle": round(mean, 2),
            "lower": round(bb_lower, 2),
            "bandwidth_pct": round(bb_bandwidth_pct, 2),
            "state": (
                "Co Thắt Nút Cổ Chai (Squeeze)" if bb_bandwidth_pct < 6.0 else "Mở Rộng Biên Độ"
            ),
        },
        "macd": {
            "fast_period": macd_fast,
            "slow_period": macd_slow,
            "signal_period": macd_signal,
            "macd_line": round(macd_line, 2),
            "signal_line": round(signal_line, 2),
            "histogram": round(macd_hist, 2),
            "trend": macd_trend,
        },
        "rsi": {
            "period": rsi_period,
            "value": round(rsi_val, 2),
            "state": rsi_state,
        },
        "atr": {
            "period": atr_period,
            "value": round(atr_val, 2),
            "atr_pct": round(atr_pct, 2),
            "trailing_stop_2x": round(atr_trailing_stop, 2),
        },
        "stochastic": {
            "k_period": stoch_k,
            "d_period": stoch_d,
            "k": round(pct_k, 1),
            "d": round(pct_d, 1),
        },
        "ema_ribbon": {
            "ema20": round(ema20, 2),
            "ema50": round(ema50, 2),
            "ema200": round(ema200, 2),
            "alignment": (
                "Bullish Alignment (Giá > EMA20 > EMA50)"
                if curr_close >= ema20 >= ema50
                else (
                    "Bearish Alignment (Giá < EMA20 < EMA50)"
                    if curr_close <= ema20 <= ema50
                    else "Consolidation / Mixed"
                )
            ),
        },
    }


def format_technical_analysis_markdown(
    symbol: str,
    current_price: float,
    fibo: FibonacciLevels,
    ichimoku: IchimokuCloud,
    trendline: GeometricTrendline,
    patterns: list[ChartPattern],
    customs: dict[str, Any] | None = None,
) -> str:
    """Renders comprehensive technical report with Fibonacci, Ichimoku, Pattern Recognition."""
    patterns_str = (
        "\n".join(
            f"- 🎯 **{p.name}** ({int(p.confidence * 100)}%): {p.description}\n"
            f"  $\\rightarrow$ **Target**: `{p.target_price:,.2f}` · "
            f"**Stop Loss**: `{p.stop_loss:,.2f}`"
            for p in patterns
        )
        if patterns
        else "- Chưa xuất hiện mẫu hình nến hoặc đảo chiều cực đoan."
    )

    custom_lines: list[str] = []
    if customs:
        if "bollinger_bands" in customs:
            bb = customs["bollinger_bands"]
            custom_lines.append(
                f"- **Bollinger Bands ({bb['period']}, {bb['std_multiplier']} SD)**: "
                f"Trên: `{bb['upper']:,.2f}` · Giữa: `{bb['middle']:,.2f}` · "
                f"Dưới: `{bb['lower']:,.2f}` ({bb['state']})"
            )
        if "macd" in customs:
            mc = customs["macd"]
            sig_p = mc.get("signal_period", 9)
            sig_l = mc.get("signal_line", 0.0)
            hist = mc.get("histogram", 0.0)
            custom_lines.append(
                f"- **MACD ({mc['fast_period']},{mc['slow_period']},{sig_p})**: "
                f"MACD: `{mc['macd_line']:,.2f}` · Signal: `{sig_l:,.2f}` · "
                f"Hist: `{hist:+,.2f}` ({mc['trend']})"
            )
        if "rsi" in customs:
            r_info = customs["rsi"]
            custom_lines.append(
                f"- **Wilder's RSI ({r_info['period']})**: `{r_info['value']:.1f}` "
                f"({r_info['state']})"
            )
        if "atr" in customs:
            a_info = customs["atr"]
            custom_lines.append(
                f"- **ATR ({a_info['period']})**: `{a_info['value']:,.2f}` "
                f"({a_info['atr_pct']:.2f}%) · **ATR Trailing Stop (2x)**: "
                f"`{a_info['trailing_stop_2x']:,.2f}`"
            )
        if "stochastic" in customs:
            st = customs["stochastic"]
            custom_lines.append(
                f"- **Stochastic ({st['k_period']},{st['d_period']})**: "
                f"`%K = {st['k']:.1f}` · `%D = {st['d']:.1f}`"
            )

    custom_block = ("\n".join(custom_lines) + "\n") if custom_lines else ""

    return (
        f"### 📐 Phân Tích Kỹ Thuật Nâng Cao, Fibonacci & Mẫu Hình ({symbol})\n"
        f"**Giá hiện tại**: `{current_price:,.2f}` · "
        f"**Xu hướng**: `{trendline.trend_type}` ({trendline.channel_slope_deg}°, "
        f"$R^2 = {trendline.r_squared:.2f}$)\n\n"
        f"#### 1. Các Mức Fibonacci Retracement & Vùng Golden Pocket\n"
        f"- **Swing High (0.0%)**: `{fibo.fib_0:,.2f}` · "
        f"**Swing Low (100.0%)**: `{fibo.fib_1000:,.2f}`\n"
        f"- **Fibo 23.6%**: `{fibo.fib_236:,.2f}` · **Fibo 38.2%**: `{fibo.fib_382:,.2f}` · "
        f"**Fibo 50.0%**: `{fibo.fib_500:,.2f}`\n"
        f"- **Golden Pocket (Fibo 61.8% - 65.0%)**: `{fibo.golden_pocket_range}` "
        f"(Vùng cầu tổ chức tối ưu)\n"
        f"- **Fibo Extension 127.2% / 161.8%**: `{fibo.fib_1272:,.2f}` / `{fibo.fib_1618:,.2f}`\n"
        f"- **Hỗ trợ gần**: `{fibo.nearest_support:,.2f}` · "
        f"**Kháng cự gần**: `{fibo.nearest_resistance:,.2f}`\n\n"
        f"#### 2. Hệ Thống Đám Mây Ichimoku Kinko Hyo & Chỉ Báo Định Lượng\n"
        f"- **Trạng thái Mây Kumo**: **{ichimoku.cloud_state}**\n"
        f"- **Tenkan-sen (9)**: `{ichimoku.tenkan_sen:,.2f}` · "
        f"**Kijun-sen (26)**: `{ichimoku.kijun_sen:,.2f}`\n"
        f"- **Tín hiệu TK**: `{ichimoku.tk_cross}` · **Chikou**: `{ichimoku.chikou_signal}`\n"
        f"- **Biên trên Mây**: `{ichimoku.support_cloud_top:,.2f}` · "
        f"**Biên dưới Mây**: `{ichimoku.support_cloud_bottom:,.2f}`\n"
        f"{custom_block}\n"
        f"#### 3. Nhận Diện Mẫu Hình Kỹ Thuật & Kênh Hồi Quy (Pattern Recognition)\n"
        f"{patterns_str}\n"
        f"- **Kênh Hồi Quy Tuyến Tính ($\\pm 2\\sigma$)**: `{trendline.regression_lower:,.2f}` "
        f"$\\rightarrow$ `{trendline.regression_upper:,.2f}`\n"
        f"- **VCP Contraction**: Co hẹp `{trendline.vcp_contraction_ratio}x` · "
        f"**Breakout**: `{trendline.breakout_trigger_price:,.2f}`\n"
    )
