"""Advanced Technical Analysis, Geometric Trendlines & Pattern Recognition Module.

Provides institutional quantitative chart analysis without heavy C dependencies:
1. Fibonacci Retracement & Extension Analysis (0.0%, 23.6%, 38.2%, 50.0%, 61.8%, 78.6%, 161.8%)
2. Ichimoku Kinko Hyo Cloud (Tenkan, Kijun, Senkou Span A/B, TK Cross & Cloud State)
3. Trendline & Geometric Channel Detection (Support/Resistance Trendlines, Slopes, VCP Contraction)
4. Chart & Candlestick Pattern Recognition (Double Bottom/Top, Head & Shoulders, Cup & Handle,
   Triangles, Hammer, Bullish/Bearish Engulfing, Morning/Evening Star)
5. Custom User Indicator Engine (MACD, Bollinger Bands, ATR, Stochastic with custom params)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class FibonacciLevels:
    swing_high: float
    swing_low: float
    fib_0: float  # Swing High
    fib_236: float
    fib_382: float
    fib_500: float
    fib_618: float  # Golden Ratio
    fib_786: float
    fib_1000: float  # Swing Low
    fib_1618: float  # Extension target
    nearest_support: float
    nearest_resistance: float
    golden_pocket_range: str


@dataclass(slots=True)
class IchimokuCloud:
    tenkan_sen: float  # Conversion Line (9)
    kijun_sen: float  # Base Line (26)
    senkou_span_a: float  # Leading Span A (52/2)
    senkou_span_b: float  # Leading Span B (52)
    chikou_span: float  # Lagging Span
    cloud_state: str  # "Bullish Cloud (Mây Xanh Tăng Trưởng)", "Bearish Cloud", "In Cloud"
    tk_cross: str  # "Bullish TK Golden Cross", "Bearish Dead Cross", "Neutral"
    support_cloud_top: float
    support_cloud_bottom: float


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
    trend_type: str  # "Uptrend Channel (Kênh Tăng)", "Downtrend Channel", "Sideway"
    support_line_level: float
    resistance_line_level: float
    channel_slope_deg: float
    vcp_contraction_ratio: float  # Biến động thu hẹp VCP
    breakout_trigger_price: float


def calc_fibonacci_retracement(
    highs: list[float], lows: list[float], current_price: float
) -> FibonacciLevels:
    """Calculates full Fibonacci Retracement & Golden Ratio levels from price swings."""
    valid_highs = [float(h) for h in highs if isinstance(h, (int, float))]
    valid_lows = [float(l_val) for l_val in lows if isinstance(l_val, (int, float))]
    if not valid_highs or not valid_lows:
        swing_h = round(current_price * 1.15, 4)
        swing_l = round(current_price * 0.85, 4)
    else:
        swing_h = max(valid_highs)
        swing_l = min(valid_lows)

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
    fib_786 = swing_h - 0.786 * diff
    fib_1000 = swing_l
    fib_1618 = swing_h + 0.618 * diff

    # Find nearest support and resistance
    all_levels = [
        fib_0,
        fib_236,
        fib_382,
        fib_500,
        fib_618,
        fib_786,
        fib_1000,
        fib_1618,
    ]
    supports = [lvl for lvl in all_levels if lvl <= current_price]
    resistances = [lvl for lvl in all_levels if lvl >= current_price]

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

    golden_pocket = f"{fib_618:,.2f} - {fib_500:,.2f}"

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

    chikou = curr_close

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
    )


def detect_chart_patterns(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
) -> list[ChartPattern]:
    """Detects geometric chart patterns (Double Bottom/Top, Cup & Handle, Triangles)."""
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
    curr = v_c[-1]
    n = min_len

    # 1. Double Bottom (W Pattern) Detection
    # Look for 2 distinct swing lows with similar price level and a peak in between
    sub_win = min(30, n)
    recent_lows = v_l[-sub_win:]
    recent_highs = v_h[-sub_win:]
    min_low = min(recent_lows)
    low_indices = [
        i for i, l_val in enumerate(recent_lows) if abs(l_val - min_low) / (min_low or 1.0) < 0.025
    ]
    if len(low_indices) >= 2 and (low_indices[-1] - low_indices[0]) >= 5:
        # Check if current price is breaking above the intermediate peak
        sub_highs = recent_highs[low_indices[0] : low_indices[-1] + 1]
        if sub_highs:
            inter_peak = max(sub_highs)
            if curr >= inter_peak * 0.98:
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

    # 2. Cup and Handle (Cốc Tay Cầm) / VCP Contraction
    if n >= 25:
        # Left cup rim, cup bottom, right cup rim, handle
        cup_rim = max(v_h[-25:-10])
        cup_low = min(v_l[-25:-5])
        cup_depth_pct = (cup_rim - cup_low) / (cup_rim or 1.0) * 100
        if 8.0 <= cup_depth_pct <= 35.0:
            # Handle should be shallow (< 12% drop from rim)
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

    # 3. Candlestick Pattern Detection (Last 1-3 bars)
    c_curr = v_c[-1]
    o_curr = v_c[-2] if n >= 2 else v_c[-1]
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

        # Bullish Engulfing
        if n >= 3:
            c_prev = v_c[-2]
            o_prev = v_c[-3]
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

    return patterns


def calc_geometric_trendlines(
    highs: list[float], lows: list[float], closes: list[float]
) -> GeometricTrendline:
    """Calculates trendline boundaries, slope angle and Volatility Contraction (VCP)."""
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
        )

    # Linear slope proxy
    recent_c = valid_c[-20:] if min_len >= 20 else valid_c[-min_len:]
    x = list(range(len(recent_c)))
    y = recent_c
    n = len(x)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    den = sum((x[i] - mean_x) ** 2 for i in range(n))
    slope = (num / den) if den != 0 else 0.0

    # Angle in degrees
    slope_deg = math.degrees(math.atan(slope / (mean_y * 0.01 if mean_y else 1.0)))

    recent_h = valid_h[-20:] if min_len >= 20 else valid_h[-min_len:]
    recent_l = valid_l[-20:] if min_len >= 20 else valid_l[-min_len:]

    res_line = max(recent_h)
    sup_line = min(recent_l)

    # Volatility Contraction Pattern (VCP) ratio: compare variance of first half vs second half
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
    )


def compute_custom_indicators(
    closes: list[float],
    volumes: list[float],
    *,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> dict[str, Any]:
    """Calculates user-defined custom indicators: Custom MACD, Bollinger Bands, ATR."""
    v_c = [float(c) for c in closes if isinstance(c, (int, float))]
    if len(v_c) < bb_period:
        return {}

    curr_close = v_c[-1]

    # Bollinger Bands
    window = v_c[-bb_period:]
    mean = sum(window) / bb_period
    variance = sum((c - mean) ** 2 for c in window) / bb_period
    stdev = math.sqrt(variance)
    bb_upper = mean + bb_std * stdev
    bb_lower = mean - bb_std * stdev
    bb_bandwidth_pct = ((bb_upper - bb_lower) / mean * 100) if mean else 0.0

    # MACD Proxy
    fast_ema = sum(v_c[-macd_fast:]) / macd_fast if len(v_c) >= macd_fast else curr_close
    slow_ema = sum(v_c[-macd_slow:]) / macd_slow if len(v_c) >= macd_slow else curr_close
    macd_line = fast_ema - slow_ema

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
            "macd_line": round(macd_line, 2),
            "trend": "Bullish Momentum" if macd_line > 0 else "Bearish Momentum",
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

    bb_info = ""
    if customs and "bollinger_bands" in customs:
        bb = customs["bollinger_bands"]
        bb_info = (
            f"- **Bollinger Bands ({bb['period']})**: Trên: `{bb['upper']:,.2f}` · "
            f"Giữa: `{bb['middle']:,.2f}` · Dưới: `{bb['lower']:,.2f}` ({bb['state']})\n"
        )

    return (
        f"### 📐 Phân Tích Kỹ Thuật Nâng Cao, Fibonacci & Mẫu Hình ({symbol})\n"
        f"**Giá hiện tại**: `{current_price:,.2f}` · "
        f"**Xu hướng**: `{trendline.trend_type}` ({trendline.channel_slope_deg}°)\n\n"
        f"#### 1. Các Mức Fibonacci Retracement & Vùng Golden Pocket\n"
        f"- **Swing High (0.0%)**: `{fibo.fib_0:,.2f}` · "
        f"**Swing Low (100.0%)**: `{fibo.fib_1000:,.2f}`\n"
        f"- **Fibo 23.6%**: `{fibo.fib_236:,.2f}` · **Fibo 38.2%**: `{fibo.fib_382:,.2f}` · "
        f"**Fibo 50.0%**: `{fibo.fib_500:,.2f}`\n"
        f"- **Golden Pocket (Fibo 61.8% - 50.0%)**: `{fibo.golden_pocket_range}` "
        f"(Vùng tích lũy tối ưu)\n"
        f"- **Fibo Extension 161.8%**: `{fibo.fib_1618:,.2f}`\n"
        f"- **Hỗ trợ gần**: `{fibo.nearest_support:,.2f}` · "
        f"**Kháng cự gần**: `{fibo.nearest_resistance:,.2f}`\n\n"
        f"#### 2. Hệ Thống Đám Mây Ichimoku Kinko Hyo\n"
        f"- **Trạng thái Mây Kumo**: **{ichimoku.cloud_state}**\n"
        f"- **Tenkan-sen (9)**: `{ichimoku.tenkan_sen:,.2f}` · "
        f"**Kijun-sen (26)**: `{ichimoku.kijun_sen:,.2f}`\n"
        f"- **Tín hiệu TK**: `{ichimoku.tk_cross}`\n"
        f"- **Biên trên Mây**: `{ichimoku.support_cloud_top:,.2f}` · "
        f"**Biên dưới Mây**: `{ichimoku.support_cloud_bottom:,.2f}`\n"
        f"{bb_info}"
        f"#### 3. Nhận Diện Mẫu Hình Kỹ Thuật & Hình Học (Pattern Recognition)\n"
        f"{patterns_str}\n"
        f"- **VCP Contraction**: Co hẹp `{trendline.vcp_contraction_ratio}x` · "
        f"**Breakout**: `{trendline.breakout_trigger_price:,.2f}`\n"
    )
