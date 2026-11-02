"""Comprehensive verification suite for Waves 1-5 Architectural & Quantitative Upgrades."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.extraction.chunker import Chunk
from app.orchestration.pipeline import infer_categories_from_query
from app.orchestration.research import ContextPack, detect_profile
from app.retrieval.adapters.fengshui import (
    FengShuiAdapter,
    _calc_cung_phi_nam,
    _calc_cung_phi_nu,
)
from app.retrieval.adapters.stooq import _extract_stooq_symbol
from app.retrieval.adapters.viet_stock import VietStockAdapter, _extract_tickers
from app.retrieval.adapters.wikipedia import WikipediaAdapter
from app.retrieval.financial_forensics import (
    analyze_balance_sheet_forensics,
    calc_altman_z_score,
    calc_beneish_m_score,
    calc_piotroski_f_score,
    format_forensics_markdown,
)
from app.retrieval.merger import diversity_cap
from app.retrieval.models import ScoredResult
from app.retrieval.scoring import domain_authority, snippet_quality
from app.retrieval.technical_analysis import (
    calc_fibonacci_retracement,
    calc_geometric_trendlines,
    calc_ichimoku_cloud,
    compute_custom_indicators,
    format_technical_analysis_markdown,
    parse_custom_indicator_params,
)


class TestWave1InfrastructureAndSecurity:
    async def test_mcp_server_tools_delegate_without_recursion(self, monkeypatch):
        import sys
        import types

        from app.mcp import client as mcp_client
        from app.mcp import server as mcp_server

        tools: dict[str, object] = {}

        class FakeFastMCP:
            def __init__(self, name: str):
                self.name = name

            def tool(self):
                def decorator(fn):
                    tools[fn.__name__] = fn
                    return fn

                return decorator

        fake_mod = types.ModuleType("mcp.server.fastmcp")
        fake_mod.FastMCP = FakeFastMCP  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
        monkeypatch.setitem(sys.modules, "mcp.server", types.ModuleType("mcp.server"))
        monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake_mod)

        called: dict[str, object] = {}

        async def fake_web_search(
            query: str,
            *,
            lang=None,
            k: int = 10,
            engines=None,
            categories=None,
            freshness_days=None,
        ) -> str:
            called["web_search"] = (query, k)
            return "ok-results"

        monkeypatch.setattr(mcp_client, "web_search", fake_web_search)
        mcp_server.build_server()
        res = await tools["web_search"]("fpt", k=5)  # type: ignore[misc]
        assert res == "ok-results"
        assert called["web_search"] == ("fpt", 5)

    def test_wikipedia_lang_regex_prevents_ssrf_injection(self):
        adapter = WikipediaAdapter()
        assert adapter.id == "wikipedia"
        from app.retrieval.adapters.wikipedia import _LANG_RE

        assert _LANG_RE.match("vi")
        assert _LANG_RE.match("en")
        assert not _LANG_RE.match("evil.com/pwn?")
        assert not _LANG_RE.match("127.0.0.1")


class TestWave2RetrievalAndResearch:
    def test_smart_category_routing_infers_verticals_when_general(self):
        # Generic query should not trigger vertical categories
        assert infer_categories_from_query("q") == set()
        assert infer_categories_from_query("how to bake bread") == set()

        # Financial queries auto-route to financial
        assert "financial" in infer_categories_from_query("phân tích BCTC cổ phiếu FPT")
        assert "financial" in infer_categories_from_query("$NVDA Fibonacci retracement")

        # Fengshui queries auto-route to fengshui
        assert "fengshui" in infer_categories_from_query("phong thủy bát trạch hướng nhà 1990")

        # Developer queries auto-route to developer
        assert "developer" in infer_categories_from_query("FastAPI Python exception traceback")

        # Academic/Medical queries auto-route to academic
        assert "academic" in infer_categories_from_query("pubmed clinical trial crispr")

    def test_snippet_quality_rewards_structured_quantitative_markdown(self):
        quant_md = (
            "[HSX:FPT] **Vốn hóa**: 185,500 tỷ đ | P/E: 24.5 | ROE: 28.2%\n"
            "- Kế hoạch doanh thu: 62,980 tỷ đ\n"
            "- Lợi nhuận sau thuế tăng trưởng 21.4% " + ("x " * 120)
        )
        assert snippet_quality(quant_md) == 0.95
        # Plain unstructured wall of text > 1500 chars still decays
        plain_long = "a" * 1600
        assert snippet_quality(plain_long) < 0.5
        # Trusted domains populated
        assert domain_authority("finance.vietstock.vn") == 0.90
        assert domain_authority("stooq.com") == 0.90
        assert domain_authority("unknown-random-site.xyz") == 0.5

    def test_diversity_cap_allows_4_for_quantitative_vertical_adapters(self):
        vs_hits = [
            ScoredResult(
                url=f"https://finance.vietstock.vn/FPT/p{i}.htm",
                canonical_url=f"https://finance.vietstock.vn/FPT/p{i}.htm",
                domain="finance.vietstock.vn",
                title=f"FPT Part {i}",
                snippet="...",
                best_rank=i,
                adapters=["viet_stock"],
                engines=["viet_stock"],
            )
            for i in range(1, 7)
        ]
        other_hits = [
            ScoredResult(
                url=f"https://site{j}.com/a",
                canonical_url=f"https://site{j}.com/a",
                domain=f"site{j}.com",
                title=f"Site {j}",
                snippet="...",
                best_rank=10 + j,
                adapters=["ddg"],
                engines=["ddg"],
            )
            for j in range(1, 10)
        ]
        capped = diversity_cap(vs_hits + other_hits, max_per_domain=2, limit=10)
        assert len(capped) == 10
        assert sum(1 for r in capped if r.domain == "finance.vietstock.vn") == 4

    def test_detect_profile_honors_all_8_explicit_profiles_and_auto(self):
        for p in (
            "general",
            "academic",
            "financial",
            "health",
            "developer",
            "news",
            "comparative",
            "fengshui",
        ):
            assert detect_profile("any query", p) == p

        assert detect_profile("phân tích ichimoku và pháp y bctc HPG", "auto") == "financial"
        assert detect_profile("xem hướng nhà tuổi canh ngọ 1990", "auto") == "fengshui"

    def test_context_pack_merge_keeps_up_to_3_chunks_per_new_url(self):
        pack = ContextPack()
        src = [{"url": "https://example.com/a", "title": "A", "domain": "example.com"}]
        chunks = [
            Chunk(
                id=f"c{i}",
                document_url="https://example.com/a",
                position=i,
                heading_path=None,
                content=f"chunk {i}",
                word_count=10,
            )
            for i in range(5)
        ]
        added = pack.merge(chunks, src)
        assert len(added) == 3
        assert len(pack.chunks) == 3  # Up to 3 chunks from the new URL
        assert len(pack.sources) == 1

        # Second merge with same URL is deduplicated
        added_again = pack.merge(chunks, src)
        assert added_again == []
        assert len(pack.chunks) == 3


class TestWave3FinancialForensicsAndTechnicals:
    def test_banking_securities_and_real_estate_forensics(self):
        vcb = analyze_balance_sheet_forensics("VCB")
        assert vcb is not None
        assert vcb.industry_type == "banking"
        assert vcb.car_pct == 11.8
        assert vcb.npl_ratio_pct == 0.98
        assert vcb.llr_pct == 235.0
        assert (
            round(vcb.ebit_vnd_bil / vcb.interest_expense_vnd_bil, 2) == vcb.interest_coverage_ratio
        )
        vcb_md = format_forensics_markdown(vcb)
        assert "CAMELS" in vcb_md
        assert "CAR" in vcb_md

        ssi = analyze_balance_sheet_forensics("SSI")
        assert ssi is not None
        assert ssi.industry_type == "securities"
        assert ssi.margin_loans_to_equity_pct == 82.5
        assert ssi.financial_safety_ratio_pct == 415.0
        ssi_md = format_forensics_markdown(ssi)
        assert "Margin / VCSH" in ssi_md

        idc = analyze_balance_sheet_forensics("IDC")
        assert idc is not None
        assert idc.industry_type == "real_estate"
        assert idc.unearned_revenue_and_advances_vnd_bil == 5_650.0
        idc_md = format_forensics_markdown(idc)
        assert "Người mua trả tiền trước" in idc_md

    def test_dynamic_forensics_math_and_distress_scores(self):
        @dataclass
        class DummyCompany:
            plan_year: int = 2024
            industry: str = "Sản xuất công nghiệp"
            actual_revenue_vnd_bil: float = 12_000.0
            actual_profit_vnd_bil: float = 1_500.0
            debt_to_equity: float = 0.6
            market_cap_vnd_bil: float = 24_000.0
            pb: float = 2.0
            roe_pct: float = 18.0
            roa_pct: float = 10.0

        f = analyze_balance_sheet_forensics("XYZ", DummyCompany())
        assert f is not None
        # Book Equity = 24,000 / 2.0 = 12,000; Total Debt = 12,000 * 0.6 = 7,200
        assert f.total_debt_vnd_bil == 7_200.0
        assert f.interest_coverage_ratio == round(f.ebit_vnd_bil / f.interest_expense_vnd_bil, 1)
        assert 0 <= f.piotroski_f_score <= 9

        z_val = calc_altman_z_score(
            working_capital=3000,
            retained_earnings=4000,
            ebit=2000,
            market_cap=20000,
            total_liabilities=5000,
            revenue=15000,
            total_assets=12000,
        )
        assert z_val > 2.99
        m_val = calc_beneish_m_score()
        assert m_val < -1.78
        f_val = calc_piotroski_f_score()
        assert f_val == 9

    def test_custom_indicator_query_parser_and_true_ema_macd(self):
        params = parse_custom_indicator_params(
            "Phân tích FPT với RSI 9, MACD 8 21 5, Bollinger 15 2.5, ATR 10, Stoch 14 3"
        )
        assert params["rsi_period"] == 9
        assert params["macd_fast"] == 8
        assert params["macd_slow"] == 21
        assert params["macd_signal"] == 5
        assert params["bb_period"] == 15
        assert params["bb_std"] == 2.5
        assert params["atr_period"] == 10
        assert params["stoch_k"] == 14
        assert params["stoch_d"] == 3

        closes = [100.0 + i * 0.9 + (1.2 if i % 3 == 0 else -0.5) for i in range(60)]
        highs = [c + 1.8 for c in closes]
        lows = [c - 1.5 for c in closes]
        vols = [1_000_000.0] * 60

        ind = compute_custom_indicators(closes, vols, highs=highs, lows=lows, **params)
        assert "macd" in ind
        assert "signal_line" in ind["macd"]
        assert "histogram" in ind["macd"]
        assert ind["rsi"]["period"] == 9
        assert 0.0 <= ind["rsi"]["value"] <= 100.0
        assert ind["atr"]["period"] == 10
        assert ind["atr"]["trailing_stop_2x"] < closes[-1]
        assert "stochastic" in ind
        assert "ema_ribbon" in ind

        fibo = calc_fibonacci_retracement(highs, lows, closes[-1])
        assert fibo.nearest_support < fibo.nearest_resistance
        assert fibo.fib_1272 > fibo.fib_0
        assert fibo.fib_2618 > fibo.fib_1618

        ichi = calc_ichimoku_cloud(highs, lows, closes)
        assert "Chikou" in ichi.chikou_signal

        trend = calc_geometric_trendlines(highs, lows, closes)
        assert trend.regression_upper > trend.regression_lower
        assert 0.0 <= trend.r_squared <= 1.0

        md = format_technical_analysis_markdown("FPT", closes[-1], fibo, ichi, trend, [], ind)
        assert "Wilder's RSI (9)" in md
        assert "MACD (8,21,5)" in md
        assert "ATR Trailing Stop" in md


class TestWave4MultiEngineAdapters:
    async def test_viet_stock_word_boundary_and_unique_trajectories(self, monkeypatch):
        # Must NOT match 'ssi' in 'discussion' or 'idc' in 'idcard'
        assert _extract_tickers("online discussion about idcard") == []
        # Must match real tickers and Vietnamese company names
        assert _extract_tickers("phân tích cổ phiếu FPT và Techcombank") == ["TCB", "FPT"]

        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "dchart-api.vndirect.com.vn" in url_str:
                sym = "FPT" if "FPT" in url_str else "HPG"
                base_p = 125.0 if sym == "FPT" else 28.0
                t_base = 1730000000
                t_list = [t_base + i * 86400 for i in range(60)]
                c_list = [base_p + (i * 0.1) for i in range(60)]
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
            return httpx.Response(404, json={})

        original = httpx.AsyncClient

        def make_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", make_client)

        adapter = VietStockAdapter()
        fpt_res = await adapter.search("phân tích kỹ thuật FPT RSI 9")
        hpg_res = await adapter.search("phân tích kỹ thuật HPG")
        assert fpt_res and hpg_res
        fpt_tech = next(r for r in fpt_res if "KỸ THUẬT NÂNG CAO" in r.snippet)
        hpg_tech = next(r for r in hpg_res if "KỸ THUẬT NÂNG CAO" in r.snippet)
        assert "RSI(9)" in fpt_tech.snippet
        # Ensure FPT and HPG do not share an identical slope & Fibo support
        assert fpt_tech.snippet != hpg_tech.snippet

    async def test_stooq_rejects_english_substrings_and_acronyms(self):
        assert _extract_stooq_symbol("metasearch architecture") is None
        assert _extract_stooq_symbol("aws lambda function") is None
        assert _extract_stooq_symbol("organic soil quality") is None
        assert _extract_stooq_symbol("What is AI API and RSI in Python?") is None
        assert _extract_stooq_symbol("phân tích cổ phiếu FPT") is None

        assert _extract_stooq_symbol("$NVDA price") == "nvda.us"
        assert _extract_stooq_symbol("giá vàng hôm nay") == "xauusd"
        assert _extract_stooq_symbol("chỉ số S&P 500") == "^spx"
        assert _extract_stooq_symbol("tỷ giá USD/VND") == "usdvnd"

    async def test_fengshui_universal_cung_phi_and_can_chi_lookup(self):
        # Verify 1990 (Canh Ngọ): Male = Khảm, Female = Cấn
        assert _calc_cung_phi_nam(1990) == "Khảm"
        assert _calc_cung_phi_nu(1990) == "Cấn"
        # Verify 2000 (Canh Thìn): Male = Ly, Female = Càn
        assert _calc_cung_phi_nam(2000) == "Ly"
        assert _calc_cung_phi_nu(2000) == "Càn"
        # Verify 2024 (Giáp Thìn): Male = Chấn, Female = Chấn
        assert _calc_cung_phi_nam(2024) == "Chấn"
        assert _calc_cung_phi_nu(2024) == "Chấn"

        adapter = FengShuiAdapter()
        # Lookup by Can Chi name without 4-digit year
        res = await adapter.search("xem phong thủy tuổi Canh Ngọ hợp hướng nào")
        assert len(res) >= 1
        assert "Canh Ngọ" in res[0].snippet
        assert "Tam hợp:" in res[0].snippet
        assert "Tứ hành xung:" in res[0].snippet
