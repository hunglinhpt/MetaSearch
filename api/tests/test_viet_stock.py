"""Comprehensive tests for VietStockAdapter: HSX & HNX Listed Equities Deep Intelligence."""

from __future__ import annotations

import httpx
import pytest

from app.orchestration.research import detect_profile
from app.retrieval.adapters.viet_stock import (
    VietStockAdapter,
    _extract_tickers,
)


@pytest.fixture
def mock_viet_stock_network(monkeypatch):
    """Provides grounded mocked responses for VNDIRECT and Simplize APIs."""

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        # VNDIRECT dchart API mock
        if "dchart-api.vndirect.com.vn" in url_str:
            t_base = 1730000000
            t_list = [t_base + i * 86400 for i in range(60)]
            c_list = [120.0 + (i * 0.15) for i in range(60)]
            return httpx.Response(
                200,
                json={
                    "s": "ok",
                    "t": t_list,
                    "o": [c * 0.995 for c in c_list],
                    "h": [c * 1.015 for c in c_list],
                    "l": [c * 0.985 for c in c_list],
                    "c": c_list,
                    "v": [2_500_000.0] * 60,
                },
            )
        # Simplize API mock
        if "api.simplize.vn" in url_str:
            sym = url_str.split("/")[-1].upper()
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "data": {
                        "ticker": sym,
                        "nameVi": f"Công ty Cổ phần {sym}",
                        "industryActivity": "Công nghệ thông tin",
                        "stockExchange": "HSX",
                        "marketCap": 185_500_000_000_000.0,
                        "outstandingSharesValue": 1_460_400_000.0,
                        "priceClose": 127000.0,
                        "peRatio": 24.5,
                        "pbRatio": 5.8,
                        "epsRatio": 5180.0,
                        "roe": 28.2,
                        "roa": 14.1,
                        "freeFloatRate": 49.0,
                        "analysisUpdated": "31/10/2024",
                    },
                },
            )
        return httpx.Response(404, json={})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "app.retrieval.adapters.viet_stock.get_http_client", lambda *a, **kw: mock_client
    )


class TestVietStockAdapter:
    async def test_empty_query_returns_empty(self):
        adapter = VietStockAdapter()
        assert await adapter.search("   ") == []

    def test_ticker_extraction_uppercase_and_keywords(self):
        # By ticker
        assert "FPT" in _extract_tickers("Phân tích mã FPT hôm nay")
        assert "HPG" in _extract_tickers("Cổ phiếu HPG ngành thép")
        assert "IDC" in _extract_tickers("Sàn HNX mã IDC")

        # By Vietnamese name
        assert "HPG" in _extract_tickers("Kế hoạch lợi nhuận Tập đoàn Hòa Phát")
        assert "VNM" in _extract_tickers("Chính sách cổ tức công ty Vinamilk")
        assert "MWG" in _extract_tickers("Doanh thu chuỗi Bách Hóa Xanh và Thế Giới Di Động")
        assert "PVS" in _extract_tickers("Dịch vụ kỹ thuật dầu khí PTSC")
        assert "DGC" in _extract_tickers("Nhà máy Hóa chất Đức Giang")

    def test_stop_words_filtering(self):
        # Ensure common Vietnamese and financial acronyms are not matched as tickers
        assert _extract_tickers("xem báo cáo tài chính BCTC và chỉ số ROE ROA") == []
        assert _extract_tickers("cổ tức tiền mặt hôm nay cho người dân") == []
        assert _extract_tickers("phân tích mô hình VCP MACD RSI") == []
        assert _extract_tickers("thị trường chứng khoán Việt Nam hôm nay") == []

    async def test_search_fpt_earnings_plan(self, mock_viet_stock_network):
        adapter = VietStockAdapter()
        results = await adapter.search("kế hoạch kinh doanh và kết quả BCTC FPT 2024")
        assert len(results) >= 2

        # 1. Primary Grounded FA Profile
        profile_res = results[0]
        assert "HSX:FPT" in profile_res.title
        assert "Công nghệ thông tin" in profile_res.snippet
        assert "P/E: 24.5" in profile_res.snippet
        assert "ROE: 28.2%" in profile_res.snippet
        assert profile_res.source_name == "Simplize"
        assert profile_res.source_url == "https://simplize.vn/co-phieu/FPT"
        assert profile_res.published_at is not None
        assert profile_res.published_at.year in (2024, 2025, 2026)

        # 2. Grounded VNDIRECT EOD Technicals
        tech_res = results[1]
        assert "KỸ THUẬT NÂNG CAO" in tech_res.snippet
        assert tech_res.source_name == "VNDIRECT"

    async def test_search_fpt_technical_analysis_with_real_candles(self, mock_viet_stock_network):
        adapter = VietStockAdapter()
        results = await adapter.search("phân tích kỹ thuật FPT RSI MACD")
        tech_items = [r for r in results if "KỸ THUẬT NÂNG CAO" in r.snippet]
        assert len(tech_items) == 1
        t_res = tech_items[0]
        assert "Real VNDIRECT TradingView EOD" in t_res.snippet
        assert "Fibo Retracement" in t_res.snippet
        assert "Mây Ichimoku" in t_res.snippet
        assert t_res.published_at is not None
        assert t_res.published_at.year in (2024, 2025, 2026)
        assert t_res.source_name == "VNDIRECT"

    async def test_search_hpg_dividend_history(self, mock_viet_stock_network):
        adapter = VietStockAdapter()
        results = await adapter.search("lịch sử trả cổ tức HPG ngày GDKHQ tiền mặt cổ phiếu")
        assert len(results) >= 2

        # Verify results are grounded to HPG without inventing fake corporate stories
        hpg_results = [r for r in results if "HPG" in r.title]
        assert len(hpg_results) >= 1
        top_res = hpg_results[0]
        assert top_res.source_name in ("Simplize", "VNDIRECT")
        assert not any("Dung Quất 2" in r.snippet for r in results)

    async def test_search_vcb_shareholders_and_deals(self, mock_viet_stock_network):
        adapter = VietStockAdapter()
        results = await adapter.search(
            "cơ cấu cổ đông và giao dịch trao tay mua bán VCB khối ngoại"
        )
        assert len(results) >= 2

        # Verify results are grounded to VCB without hallucinated block trades
        vcb_results = [r for r in results if "VCB" in r.title]
        assert len(vcb_results) >= 1
        top_res = vcb_results[0]
        assert top_res.source_name in ("Simplize", "VNDIRECT")
        assert not any("Mizuho" in r.snippet for r in results)

    async def test_search_hnx_idc_restructuring_and_esop(self, mock_viet_stock_network):
        adapter = VietStockAdapter()
        results = await adapter.search("IDC sàn HNX tăng vốn quỹ đất KCN và cổ tức")
        assert len(results) >= 2

        # Query mentions both IDC and index HNX
        idc_results = [r for r in results if "IDC" in r.title]
        assert len(idc_results) >= 1
        assert idc_results[0].source_name in ("Simplize", "VNDIRECT")

    async def test_general_market_query_not_hijacked(self):
        adapter = VietStockAdapter()
        # General queries without specific ticker must NOT be hijacked with arbitrary stocks
        assert await adapter.search("danh sách các công ty niêm yết trên sàn hsx hnx") == []
        assert await adapter.search("thị trường chứng khoán hôm nay thế nào") == []
        assert await adapter.search("hướng dẫn đầu tư cổ phiếu cho người mới") == []

    async def test_offline_graceful_degradation_no_synthetic_candles(self, monkeypatch):
        # Simulate network failure / 500 error on all APIs
        def fail_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "offline"})

        fail_client = httpx.AsyncClient(transport=httpx.MockTransport(fail_handler))
        monkeypatch.setattr(
            "app.retrieval.adapters.viet_stock.get_http_client", lambda *a, **kw: fail_client
        )

        adapter = VietStockAdapter()
        results = await adapter.search("phân tích kỹ thuật FPT")
        # Should NOT contain synthetic sin/cos technical series when offline
        tech_items = [r for r in results if "KỸ THUẬT NÂNG CAO" in r.snippet]
        assert tech_items == []

    def test_research_profile_detection_viet_stock_keywords(self):
        assert detect_profile("Kế hoạch kinh doanh và cổ tức FPT 2025") == "financial"
        assert detect_profile("Giao dịch thỏa thuận trao tay cổ đông VCB") == "financial"
        assert detect_profile("Tăng vốn ESOP và phát hành quyền mua SSI") == "financial"
        assert detect_profile("Báo cáo tài chính BCTC sàn HNX IDC") == "financial"
        assert detect_profile("Tin FA và giải trình chênh lệch lợi nhuận HPG") == "financial"
