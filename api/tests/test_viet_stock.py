"""Comprehensive tests for VietStockAdapter: HSX & HNX Listed Equities Deep Intelligence."""

from __future__ import annotations

from app.orchestration.research import detect_profile
from app.retrieval.adapters.viet_stock import (
    VietStockAdapter,
    _extract_tickers,
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

    async def test_search_fpt_earnings_plan(self):
        adapter = VietStockAdapter()
        results = await adapter.search("kế hoạch kinh doanh và kết quả BCTC FPT 2024")
        assert len(results) >= 2

        # 1. Primary FA Profile
        profile_res = results[0]
        assert "HSX:FPT" in profile_res.title
        assert "Công nghệ thông tin" in profile_res.snippet
        assert "P/E: 24.5" in profile_res.snippet
        assert "ROE: 28.2%" in profile_res.snippet
        assert "Vượt kế hoạch" in profile_res.snippet
        assert "EPS: 5,180 đ" in profile_res.snippet
        assert "11,120/10,875" in profile_res.snippet

        # 2. Plan & Earnings deep-dive
        plan_res = results[1]
        assert "KẾT QUẢ KINH DOANH" in plan_res.snippet
        assert "62,980" in plan_res.snippet or "61,890" in plan_res.snippet
        assert "101.8%" in plan_res.snippet

    async def test_search_hpg_dividend_history(self):
        adapter = VietStockAdapter()
        results = await adapter.search("lịch sử trả cổ tức HPG ngày GDKHQ tiền mặt cổ phiếu")
        assert len(results) >= 2

        div_results = [r for r in results if "CỔ TỨC" in r.snippet]
        assert len(div_results) >= 1
        div_res = div_results[0]
        assert "GDKHQ" in div_res.snippet
        assert "Dung Quất 2" in div_res.snippet or "cổ tức" in div_res.snippet.lower()
        assert "HPG" in div_res.title

    async def test_search_vcb_shareholders_and_deals(self):
        adapter = VietStockAdapter()
        results = await adapter.search(
            "cơ cấu cổ đông và giao dịch trao tay mua bán VCB khối ngoại"
        )
        assert len(results) >= 2

        deal_results = [
            r for r in results if "CƠ CẤU CỔ ĐÔNG" in r.snippet or "TRAO TAY" in r.snippet
        ]
        assert len(deal_results) >= 1
        d_res = deal_results[0]
        assert "Ngân hàng Nhà nước" in d_res.snippet or "Mizuho" in d_res.snippet
        assert "thỏa thuận" in d_res.snippet.lower()
        assert "room ngoại" in d_res.snippet.lower()

    async def test_search_hnx_idc_restructuring_and_esop(self):
        adapter = VietStockAdapter()
        results = await adapter.search("IDC sàn HNX tăng vốn quỹ đất KCN và cổ tức")
        assert len(results) >= 2

        top = results[0]
        assert "HNX:IDC" in top.title
        assert "Thủ phủ cổ tức tiền mặt sàn HNX" in top.snippet or "30-40%" in top.snippet
        assert "HNX" in top.title

    async def test_general_hsx_hnx_overview_query(self):
        adapter = VietStockAdapter()
        results = await adapter.search("danh sách các công ty niêm yết trên sàn hsx hnx")
        assert len(results) >= 5
        titles = [r.title for r in results]
        assert any("FPT" in t for t in titles)
        assert any("HPG" in t for t in titles)
        assert any("VCB" in t for t in titles)

    def test_research_profile_detection_viet_stock_keywords(self):
        assert detect_profile("Kế hoạch kinh doanh và cổ tức FPT 2025") == "financial"
        assert detect_profile("Giao dịch thỏa thuận trao tay cổ đông VCB") == "financial"
        assert detect_profile("Tăng vốn ESOP và phát hành quyền mua SSI") == "financial"
        assert detect_profile("Báo cáo tài chính BCTC sàn HNX IDC") == "financial"
        assert detect_profile("Tin FA và giải trình chênh lệch lợi nhuận HPG") == "financial"
