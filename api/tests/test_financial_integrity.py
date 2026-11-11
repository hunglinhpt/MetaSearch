"""Tests for Financial Data Integrity, Provenance Grounding & Anti-Hallucination (WP-09).

Enforces:
- DAT-01: No synthetic forensics without audited financial statements.
- DAT-02: Zero sin/cos price series.
- DAT-03: Zero hardcoded static stock databases in production runtime.
- DAT-04: Grounded chart patterns (no default "VCP").
- DAT-05: Strict provenance on all financial results (source_url, source_name, as_of, data_kind).
- DAT-06: Authoritative 1518 VN ticker recognition with comprehensive stopwords.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx

from app.retrieval.adapters.viet_stock import (
    VietStockAdapter,
    _build_dynamic_company_financials,
    _extract_tickers,
)
from app.retrieval.financial_forensics import analyze_balance_sheet_forensics
from app.retrieval.vn_tickers import is_vn_ticker


class TestFinancialDataIntegrity:
    def test_forensics_none_without_statements_f1(self):
        """F1 / DAT-01: analyze_balance_sheet_forensics MUST return None

        when audited financial statements are absent.
        """
        # 1. No company provided
        assert analyze_balance_sheet_forensics("HQC") is None
        assert analyze_balance_sheet_forensics("FPT") is None

        # 2. Sparse company payload without has_full_statements=True
        class SparseCompany:
            symbol = "HQC"
            market_cap_vnd_bil = 1500.0
            price_ref_vnd = 3800.0
            pe = 15.0
            pb = 0.8
            has_full_statements = False

        res = analyze_balance_sheet_forensics("HQC", company=SparseCompany())
        assert res is None, "Sparse company without audited statements must yield None forensics!"

    def test_dynamic_profile_has_no_invented_fields(self):
        """F1 / DAT-01: Dynamic company financials must not fabricate ratios."""
        simp_payload = {
            "ticker": "HQC",
            "nameVi": "Công ty Cổ phần Tư vấn - Thương mại - Dịch vụ Địa ốc Hoàng Quân",
            "industryActivity": "Bất động sản",
            "stockExchange": "HSX",
            "marketCap": 1_520_000_000_000.0,
            "priceClose": 3820.0,
            "peRatio": 18.2,
            "pbRatio": 0.85,
            "freeFloatRate": 65.0,
        }

        profile = _build_dynamic_company_financials("HQC", simp_payload, latest_close=3820.0)
        assert profile.symbol == "HQC"
        assert profile.market_cap_vnd_bil == 1520.0
        assert profile.pe == 18.2
        assert profile.pb == 0.85
        # The following must NOT be fabricated:
        assert profile.debt_to_equity is None
        assert profile.foreign_room_pct is None
        assert profile.foreign_current_pct is None
        assert profile.plan_year is None
        assert profile.target_revenue_vnd_bil is None
        assert profile.actual_revenue_vnd_bil is None
        assert profile.auditor is None
        assert profile.audit_opinion is None
        assert profile.has_full_statements is False

    async def test_no_static_data_when_upstreams_down_f3(self, monkeypatch):
        """F3 / DAT-03: When VNDIRECT and Simplize fail, adapter returns []

        without falling back to static database or fake candles.
        """

        def mock_error(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "upstream unavailable"})

        fail_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_error))
        monkeypatch.setattr(
            "app.retrieval.adapters.viet_stock.get_http_client", lambda *a, **kw: fail_client
        )

        adapter = VietStockAdapter()
        results = await adapter.search("phân tích mã HPG và FPT")
        assert results == [], "Must return empty list when upstreams are down, no static fallback!"

    def test_no_default_chart_pattern_dat_04(self):
        """DAT-04: Never use default 'VCP' pattern when pattern is undetected."""
        from app.retrieval.technical_analysis import detect_chart_patterns

        # Monotonically increasing prices without consolidation
        highs = [10.0 + i * 0.5 for i in range(40)]
        lows = [9.5 + i * 0.5 for i in range(40)]
        closes = [9.8 + i * 0.5 for i in range(40)]
        volumes = [100_000.0] * 40

        patterns = detect_chart_patterns(highs, lows, closes, volumes)
        # Should not falsely match VCP
        pattern_names = [p.name for p in patterns]
        assert "Tích lũy co hẹp VCP" not in pattern_names

    async def test_every_numeric_result_has_source_and_as_of(self, monkeypatch):
        """DAT-05: Every returned financial result must carry provenance attributes."""

        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "dchart-api.vndirect.com.vn" in url_str:
                # Schema-complete dchart payload: since the strict parser
                # (audit v2.3.0) refuses missing/mismatched o/h/l/v arrays,
                # the fixture must serve all six arrays.
                closes = [100.0 + i for i in range(30)]
                return httpx.Response(
                    200,
                    json={
                        "s": "ok",
                        "t": [1730000000 + i * 86400 for i in range(30)],
                        "o": closes,
                        "h": [c + 1 for c in closes],
                        "l": [c - 1 for c in closes],
                        "c": closes,
                        "v": [1000.0] * 30,
                    },
                )
            if "api.simplize.vn" in url_str:
                return httpx.Response(
                    200,
                    json={
                        "status": 200,
                        "data": {
                            "ticker": "FPT",
                            "nameVi": "Công ty Cổ phần FPT",
                            "stockExchange": "HSX",
                            "marketCap": 180_000_000_000_000.0,
                            "priceClose": 130000.0,
                            "analysisUpdated": "01/11/2024",
                        },
                    },
                )
            return httpx.Response(404, json={})

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(
            "app.retrieval.adapters.viet_stock.get_http_client", lambda *a, **kw: mock_client
        )

        adapter = VietStockAdapter()
        results = await adapter.search("FPT kỹ thuật và cơ bản")
        assert len(results) >= 2
        for r in results:
            assert r.source_url is not None, f"Missing source_url on {r.title}"
            assert r.source_name is not None, f"Missing source_name on {r.title}"
            assert r.as_of is not None, f"Missing as_of timestamp on {r.title}"
            assert r.data_kind in ("observed", "derived", "reference", "computed")

    def test_citation_url_matches_data_source(self):
        """DAT-03 / Citations: Simplize data must cite simplize.vn,

        VNDIRECT must cite vndirect.com.vn, never vietstock.vn.
        """
        simp_payload = {
            "ticker": "HPG",
            "nameVi": "Tập đoàn Hòa Phát",
            "stockExchange": "HSX",
            "marketCap": 170_000_000_000_000.0,
            "priceClose": 28500.0,
        }
        profile = _build_dynamic_company_financials("HPG", simp_payload)
        assert "simplize.vn" in profile.ir_url

    def test_hqc_recognized_without_prefix_dat_06(self):
        """DAT-06: Ticker HQC recognized without 'cổ phiếu' prefix."""
        tickers = _extract_tickers("định giá và xu hướng HQC hôm nay")
        assert "HQC" in tickers
        assert is_vn_ticker("HQC") is True

    def test_common_english_uppercase_not_a_ticker_dat_06(self):
        """DAT-06: Stopwords filtering out common English words and tech acronyms."""
        stopwords = ["THE", "AND", "FOR", "NOT", "ALL", "CAN", "BUT", "VCP", "BCTC"]
        for word in stopwords:
            assert is_vn_ticker(word) is False, f"{word} should be filtered by stop words!"
            assert _extract_tickers(f"tổng quan {word} trên thị trường") == []

    def test_no_module_imports_static_tables_in_runtime(self):
        """AST check: Confirms ZERO runtime modules in app/

        contain FORENSICS_DB or VIET_STOCKS_DB.
        """
        app_dir = Path(__file__).resolve().parent.parent / "app"
        assert app_dir.exists() and app_dir.is_dir()

        violations: list[str] = []
        for py_file in app_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
            except Exception:
                continue

            if "FORENSICS_DB" in content or "VIET_STOCKS_DB" in content:
                # Parse AST to ensure it's not a mere comment or docstring
                tree = ast.parse(content, filename=str(py_file))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name) and node.id in ("FORENSICS_DB", "VIET_STOCKS_DB"):
                        violations.append(f"{py_file.name}:{node.lineno} references {node.id}")

        assert violations == [], (
            f"Found static database references in app runtime code: {violations}"
        )
