"""Tests for Financial Forensics, Advanced Technical Analysis & Stooq Adapter."""

from __future__ import annotations

import httpx
import pytest

from app.retrieval.adapters.stooq import StooqFinanceAdapter
from app.retrieval.adapters.viet_stock import VietStockAdapter
from app.retrieval.financial_forensics import (
    analyze_balance_sheet_forensics,
    format_forensics_markdown,
)
from app.retrieval.technical_analysis import (
    calc_fibonacci_retracement,
    calc_geometric_trendlines,
    calc_ichimoku_cloud,
    compute_custom_indicators,
    detect_chart_patterns,
    format_technical_analysis_markdown,
)


class TestFinancialForensics:
    def test_forensics_data_fpt(self):
        f = analyze_balance_sheet_forensics("FPT")
        assert f is not None
        assert f.symbol == "FPT"
        assert f.piotroski_f_score >= 8
        assert f.altman_z_score > 2.99  # Safe zone
        assert f.net_debt_vnd_bil < 0  # Net cash position
        assert f.cfo_to_net_income_ratio >= 1.0  # Quality earnings
        assert f.days_sales_outstanding == 65.0
        assert f.forensic_rating == "Vững Mạnh Tuyệt Đối"

    def test_forensics_data_hpg(self):
        f = analyze_balance_sheet_forensics("HPG")
        assert f is not None
        assert f.receivables_to_revenue_pct < 10.0  # Superior cash collection
        assert f.days_sales_outstanding < 35.0
        assert f.net_debt_to_ebitda < 3.0  # Safe leverage
        assert f.operating_cash_flow_vnd_bil > 15_000.0

    def test_format_forensics_markdown(self):
        f = analyze_balance_sheet_forensics("VNM")
        assert f is not None
        md = format_forensics_markdown(f)
        assert "Thẩm Định Pháp Y Báo Cáo Tài Chính" in md
        assert "Cấu Trúc Nợ & Khả Năng Thanh Toán" in md
        assert "Chất Lượng Khoản Phải Thu" in md
        assert "Altman Z-Score" in md
        assert "Piotroski F-Score" in md


class TestTechnicalAnalysis:
    def test_fibonacci_retracement_calculation(self):
        highs = [100.0, 110.0, 120.0, 130.0, 150.0]
        lows = [80.0, 85.0, 90.0, 95.0, 100.0]
        curr_price = 125.0

        fibo = calc_fibonacci_retracement(highs, lows, curr_price)
        assert fibo.swing_high == 150.0
        assert fibo.swing_low == 80.0
        assert fibo.fib_500 == 150.0 - 0.5 * 70.0  # 115.0
        assert fibo.fib_618 == pytest.approx(150.0 - 0.618 * 70.0, 0.01)
        assert fibo.nearest_support <= curr_price
        assert fibo.nearest_resistance >= curr_price

    def test_ichimoku_cloud_calculation(self):
        highs = [100.0 + i for i in range(60)]
        lows = [90.0 + i for i in range(60)]
        closes = [95.0 + i for i in range(60)]

        ichimoku = calc_ichimoku_cloud(highs, lows, closes)
        assert ichimoku.tenkan_sen > 0
        assert ichimoku.kijun_sen > 0
        assert ichimoku.senkou_span_a > 0
        assert ichimoku.senkou_span_b > 0
        # In strictly upward series, price is above the cloud
        assert "Bullish Cloud" in ichimoku.cloud_state

    def test_geometric_trendlines(self):
        highs = [100.0 + i * 2.0 for i in range(30)]
        lows = [95.0 + i * 2.0 for i in range(30)]
        closes = [98.0 + i * 2.0 for i in range(30)]

        trend = calc_geometric_trendlines(highs, lows, closes)
        assert trend.channel_slope_deg > 0
        assert "Kênh Dốc Tăng Trưởng" in trend.trend_type
        assert trend.breakout_trigger_price >= max(highs)

    def test_detect_candlestick_hammer(self):
        # Create a hammer pattern on the last candle: long lower shadow
        highs = [100.0] * 20
        lows = [95.0] * 19 + [75.0]  # Deep drop then recovery
        closes = [98.0] * 19 + [99.0]  # Closed near the high

        patterns = detect_chart_patterns(highs, lows, closes, [1000] * 20)
        hammer = [p for p in patterns if "Hammer" in p.name]
        assert len(hammer) >= 1
        assert hammer[0].stop_loss == 75.0

    def test_compute_custom_indicators(self):
        closes = [100.0 + (i % 5) for i in range(30)]
        volumes = [10_000] * 30

        customs = compute_custom_indicators(
            closes, volumes, macd_fast=8, macd_slow=17, bb_period=15, bb_std=2.5
        )
        assert "bollinger_bands" in customs
        assert customs["bollinger_bands"]["period"] == 15
        assert customs["bollinger_bands"]["upper"] > customs["bollinger_bands"]["lower"]
        assert "macd" in customs

    def test_format_technical_analysis_markdown(self):
        highs = [100.0, 120.0, 140.0]
        lows = [80.0, 90.0, 100.0]
        closes = [90.0, 110.0, 130.0]

        fibo = calc_fibonacci_retracement(highs, lows, 130.0)
        ichimoku = calc_ichimoku_cloud(highs, lows, closes)
        trend = calc_geometric_trendlines(highs, lows, closes)
        patterns = detect_chart_patterns(highs, lows, closes, [1000] * 3)

        md = format_technical_analysis_markdown("FPT", 130.0, fibo, ichimoku, trend, patterns)
        assert "Phân Tích Kỹ Thuật Nâng Cao" in md
        assert "Fibonacci Retracement" in md
        assert "Ichimoku Kinko Hyo" in md
        assert "Mẫu Hình Kỹ Thuật" in md


class TestStooqAdapter:
    async def test_empty_query(self):
        adapter = StooqFinanceAdapter()
        assert await adapter.search("   ") == []

    async def test_parses_stooq_csv_data(self, monkeypatch):
        csv_sample = """Date,Open,High,Low,Close,Volume
2024-10-25,230.0,233.0,229.0,231.5,45000000
2024-10-28,232.0,234.5,231.0,233.0,42000000
2024-10-29,233.5,235.0,232.0,234.0,40000000
2024-10-30,234.0,236.0,233.0,235.5,48000000
2024-10-31,235.0,238.0,234.0,237.5,52000000
"""

        def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=csv_sample)

        original = httpx.AsyncClient

        def make_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(mock_handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", make_client)

        adapter = StooqFinanceAdapter()
        results = await adapter.search("NVDA")
        assert len(results) >= 1
        res = results[0]
        assert "NVDA.US" in res.title
        assert "237.50" in res.snippet
        assert "Fibo Retracement" in res.snippet
        assert "Ichimoku" in res.snippet
        assert res.adapter == "stooq"


class TestVietStockDeepForensicsAndTechnicals:
    async def test_search_viet_stock_forensics_query(self):
        adapter = VietStockAdapter()
        results = await adapter.search("thẩm định pháp y BCTC chất lượng phải thu cấu trúc nợ FPT")
        assert len(results) >= 1

        forensic_items = [r for r in results if "PHÁP Y BCTC" in r.snippet]
        assert len(forensic_items) >= 1
        f_res = forensic_items[0]
        assert "F-Score: 8/9" in f_res.snippet
        assert "Altman Z-Score" in f_res.snippet
        assert "ICR" in f_res.snippet
        assert "DSO" in f_res.snippet
        assert "CFO" in f_res.snippet

    async def test_search_viet_stock_technical_query(self):
        adapter = VietStockAdapter()
        results = await adapter.search("fibonacci retracement và mây ichimoku HPG")
        assert len(results) >= 1

        tech_items = [r for r in results if "FIBONACCI" in r.snippet]
        assert len(tech_items) >= 1
        t_res = tech_items[0]
        assert "Golden Pocket" in t_res.snippet
        assert "Mây Ichimoku" in t_res.snippet
        assert "Hỗ trợ" in t_res.snippet
        assert "Kháng cự" in t_res.snippet
