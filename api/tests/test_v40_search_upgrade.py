"""Wave 1 tests: v4.0 Search & Retrieval overhaul (adapters, scoring, merger, router,
pipeline, reranker, scraper, chunker, SEC-07 prompts, citations). Zero live network calls.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings
from app.core.auth import current_api_key_id
from app.extraction.chunker import Chunk, chunk_markdown
from app.extraction.scraper import PageContent, Scraper
from app.llm.citations import check_claims, extract_numeric_tokens, verify_numeric_fidelity
from app.llm.prompts import build_context, render_context
from app.orchestration.pipeline import (
    SearchParams,
    SearchPipeline,
    infer_categories_from_query,
)
from app.retrieval.adapters.bing_news import BingNewsAdapter
from app.retrieval.adapters.fengshui import FengShuiAdapter
from app.retrieval.adapters.financial_rss import GlobalFinancialRssAdapter
from app.retrieval.adapters.vn_financial_wire import VnFinancialWireAdapter
from app.retrieval.merger import merge
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import BGEReranker, HeuristicReranker, ONNXReranker, RerankerUnavailable
from app.retrieval.router import classify_complexity, extract_implicit_freshness_days
from app.retrieval.scoring import (
    compute_batch_idf,
    consensus_score,
    heuristic_chunk_score,
    keyword_relevance,
    tokenize,
)
from app.retrieval.tiers import TIER_SCORES, domain_tier


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestWave1Adapters:
    async def test_bing_news_unwraps_publisher_url_and_sets_mkt(self):
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            rss = """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0" xmlns:News="http://www.bing.com/news">
              <channel>
                <item>
                  <title>FPT tăng trưởng doanh thu phần mềm nước ngoài 28%</title>
                  <link>https://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3A%2F%2Fvnexpress.net%2Ffpt-tang-truong-28-4800001.html</link>
                  <description>Lợi nhuận trước thuế FPT đạt kỷ lục mới.</description>
                  <pubDate>Thu, 19 Nov 2026 08:00:00 GMT</pubDate>
                  <News:Source>VnExpress</News:Source>
                </item>
              </channel>
            </rss>"""
            return httpx.Response(200, text=rss, headers={"content-type": "application/xml"})

        adapter = BingNewsAdapter(client=_mock_client(handler))
        out = await adapter.search("cổ phiếu FPT", lang="vi", freshness_days=7, max_results=5)
        assert "mkt=vi-VN" in captured["url"]
        assert "interval%3D%227%22" in captured["url"]
        assert len(out) == 1
        assert out[0].url == "https://vnexpress.net/fpt-tang-truong-28-4800001.html"
        assert out[0].source_name == "VnExpress"
        assert out[0].adapter == "bing_news"

    async def test_vn_financial_wire_parses_simplize_and_reports(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            u = str(request.url)
            if "list-news" in u:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": 9101,
                                "title": "FPT mở rộng nhà máy AI tại Nhật Bản",
                                "slug": "fpt-mo-rong-ai",
                                "publishedAt": 1763452800,
                                "sourceName": "Simplize",
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 445,
                            "title": "Báo cáo phân tích FPT: Khuyến nghị MUA",
                            "source": "SSI Research",
                            "recommend": "BUY",
                            "targetPrice": 165000,
                            "issueDate": "2026-11-17",
                            "attachedLink": "https://simplize.vn/reports/fpt-ssi-445.pdf",
                        }
                    ]
                },
            )

        adapter = VnFinancialWireAdapter(client=_mock_client(handler))
        out = await adapter.search("phân tích cổ phiếu FPT", max_results=10)
        urls = [r.url for r in out]
        assert any("simplize.vn/tin-tuc/" in u for u in urls)
        assert any("fpt-ssi-445.pdf" in u for u in urls)
        assert any("SSI Research" in r.title or "MUA" in r.snippet for r in out)

        async def _fake_wire(**_kwargs):
            return {
                "items": [
                    {
                        "url": "https://cafef.vn/fpt-chia-co-tuc.chn",
                        "title": "FPT sắp chốt quyền trả cổ tức tiền mặt",
                        "summary": "Cổ đông FPT nhận cổ tức đợt 1/2026.",
                        "published_at": "2026-11-18T10:00:00Z",
                        "source_name": "CafeF",
                    }
                ]
            }

        monkeypatch.setattr(
            "app.retrieval.adapters.vn_financial_wire.fetch_market_news_wire", _fake_wire
        )
        adapter_live = VnFinancialWireAdapter(
            news_url="http://invalid.invalid", reports_url="http://invalid.invalid"
        )
        out_wire = await adapter_live.search("phân tích cổ phiếu FPT", max_results=5)
        assert any("cafef.vn" in r.url for r in out_wire)

    async def test_financial_rss_parses_feeds_with_mock_client(self):
        def handler(request: httpx.Request) -> httpx.Response:
            rss = """<?xml version="1.0" encoding="utf-8"?>
            <rss version="2.0"><channel>
              <item>
                <title>NVIDIA AI datacenter revenue surges 90% in latest quarter</title>
                <link>https://feeds.finance.yahoo.com/rss/article-nvda</link>
                <description>Strong GPU demand drives record margin for NVDA.</description>
                <pubDate>Thu, 19 Nov 2026 07:00:00 GMT</pubDate>
              </item>
            </channel></rss>"""
            return httpx.Response(200, text=rss, headers={"content-type": "application/xml"})

        adapter = GlobalFinancialRssAdapter(client=_mock_client(handler))
        out = await adapter.search("NVDA AI revenue", lang="en", max_results=5)
        assert len(out) >= 1
        assert "NVDA" in out[0].title or "NVIDIA" in out[0].title

    async def test_fengshui_gate_rejects_unrelated_queries(self):
        adapter = FengShuiAdapter()
        assert await adapter.search("tình hình chiến tranh Trung Đông") == []
        assert await adapter.search("FPT P/E valuation and AI revenue") == []
        hits = await adapter.search("hướng nhà hợp tuổi 1990")
        assert len(hits) >= 1


class TestScoringTiersAndMerger:
    def test_tokenize_keeps_ai_and_uppercase_it(self):
        assert tokenize("tác động của AI và ngành IT") == ["tac", "dong", "ai", "nganh", "it"]
        assert "it" not in tokenize("it is easy to use")

    def test_consensus_requires_multi_engine_agreement(self):
        assert consensus_score(1, 8) == 0.0
        assert consensus_score(1, 1) == 0.0
        assert 0.0 < consensus_score(2, 8) < 0.5
        assert consensus_score(3, 3) == 1.0

    def test_idf_and_bigram_prefer_exact_phrase_over_scattered_words(self):
        terms = tokenize("ai search engine")
        docs = [
            ("Unrelated AI guide", "general machine learning news"),
            ("Engine notes", "random ai benchmark and engine details scattered"),
            ("AI search engine architecture", "Deep look at AI search engine ranking"),
        ]
        idf = compute_batch_idf(terms, docs)
        exact = keyword_relevance(terms, docs[2][0], docs[2][1], idf=idf)
        scatter = keyword_relevance(terms, docs[1][0], docs[1][1], idf=idf)
        assert exact > scatter

    def test_domain_tiers_extended_and_tld_rules(self):
        assert domain_tier("hose.vn") == "official"
        assert domain_tier("agency.gov.uk") == "official"
        assert domain_tier("university.edu.vn") == "research"
        assert domain_tier("pinterest.com") == "low_trust"
        assert TIER_SCORES["official"] > TIER_SCORES["low_trust"]

    def test_merger_resolves_wrapper_domain_dedups_titles_and_demotes_offtopic(self):
        reg = {
            "google_news": EngineConfig(id="google_news", name="GN", category="news", weight=1.2),
            "bing_news": EngineConfig(id="bing_news", name="BN", category="news", weight=1.25),
        }
        now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
        merged = merge(
            [
                RawResult(
                    url="https://news.google.com/rss/articles/CBMiAAA",
                    title="FPT bứt phá lợi nhuận quý 3 nhờ mảng AI và chuyển đổi số",
                    snippet="FPT công bố kết quả kinh doanh tích cực.",
                    adapter="google_news",
                    source_engines=["google_news"],
                    source_url="https://vnexpress.net/fpt-loi-nhuan-quy-3.html",
                    published_at=now - timedelta(hours=2),
                    rank=0,
                ),
                RawResult(
                    url="https://cafef.vn/fpt-but-pha-loi-nhuan-q3.chn",
                    title="FPT bứt phá lợi nhuận quý 3 nhờ mảng AI và chuyển đổi số!",
                    snippet="Báo cáo chi tiết tăng trưởng doanh thu FPT.",
                    adapter="bing_news",
                    source_engines=["bing_news"],
                    published_at=now - timedelta(hours=3),
                    rank=1,
                ),
                RawResult(
                    url="https://spam.example/cooking",
                    title="Chocolate cake baking recipes",
                    snippet="Sugar and butter cooking tutorial",
                    adapter="bing_news",
                    source_engines=["bing_news"],
                    rank=0,
                ),
            ],
            query="FPT lợi nhuận AI",
            registry=reg,
            now=now,
            news=True,
        )
        assert len(merged) == 2
        assert merged[0].domain in {"vnexpress.net", "cafef.vn"}
        assert merged[0].consensus == 2
        assert merged[1].domain == "spam.example"
        assert merged[0].score > merged[1].score * 1.5

        solo = merge(
            [
                RawResult(
                    url="https://news.google.com/rss/articles/CBMiSolo",
                    title="FPT lợi nhuận AI",
                    snippet="Doanh thu FPT mảng AI tăng mạnh.",
                    adapter="google_news",
                    source_engines=["google_news"],
                    source_url="https://vnexpress.net/fpt-ai.html",
                    published_at=now - timedelta(hours=1),
                    rank=0,
                )
            ],
            query="FPT lợi nhuận AI",
            registry=reg,
            now=now,
            news=True,
        )
        assert solo[0].domain == "vnexpress.net"

    def test_heuristic_chunk_score_heading_bonus_and_soft_decay(self):
        early_off = heuristic_chunk_score("lợi nhuận FPT", "bảng phụ lục khác", 0.8, 1)
        deep_match = heuristic_chunk_score(
            "lợi nhuận FPT",
            "lợi nhuận FPT quý 3 tăng 24%",
            0.8,
            12,
            heading_path="Kết quả lợi nhuận FPT",
        )
        assert deep_match > early_off


class TestRouterPipelineAndExtraction:
    def test_category_inference_and_freshness_cues(self):
        assert "reference" in infer_categories_from_query("Altman Z-Score là gì")
        assert "developer" in infer_categories_from_query("thuật toán Dijkstra và FastAPI")
        assert "financial" not in infer_categories_from_query("cho tôi hỏi cách nấu phở bò")
        assert "financial" in infer_categories_from_query("định giá cổ phiếu FPT")
        assert extract_implicit_freshness_days("tin tức chứng khoán hôm nay") == 2
        assert extract_implicit_freshness_days("tin mới nhất về VCB") == 14
        assert classify_complexity("định giá và triển vọng cổ phiếu FPT") == "deep"

    async def test_lite_fallback_includes_news_and_wiki_when_searxng_disabled(self):
        class _DummyAdapter:
            def __init__(self, eid: str) -> None:
                self.id = eid

            async def search(self, query: str, **_kwargs):
                return [
                    RawResult(
                        url=f"https://example.org/{self.id}",
                        title=f"{query} result from {self.id}",
                        snippet="Relevant AI content",
                        adapter=self.id,
                        source_engines=[self.id],
                        rank=0,
                    )
                ]

        configs = {
            "ddg": EngineConfig(id="ddg", name="DDG", category="general", enabled=True),
            "bing_news": EngineConfig(id="bing_news", name="BN", category="news", enabled=True),
            "wikipedia": EngineConfig(
                id="wikipedia", name="Wiki", category="reference", enabled=True
            ),
            "fengshui": EngineConfig(id="fengshui", name="FS", category="fengshui", enabled=True),
        }

        class _Store:
            async def configs(self):
                return configs

            async def record_result(self, *_a, **_kw):
                pass

        class _Registry:
            store = _Store()

            async def enabled(self):
                return [(cfg, _DummyAdapter(eid)) for eid, cfg in configs.items()]

            async def configs(self):
                return configs

        settings = Settings(
            ENVIRONMENT="test", SEARXNG_ENABLED=False, SKILL_ORCHESTRATION_ENABLED=True
        )
        pipe = SearchPipeline(settings, _Registry())
        tok = current_api_key_id.set("test-key")
        try:
            outcome = await pipe.run(SearchParams(query="AI search overview", expansion=False))
        finally:
            current_api_key_id.reset(tok)
        used = {s.engine for s in outcome.statuses}
        assert "bing_news" in used
        assert "wikipedia" in used
        assert "fengshui" not in used

    async def test_hybrid_reranker_bm25_mmr_and_onnx_truncation(self, tmp_path):
        chunks = [
            Chunk(
                id=f"c{i}",
                document_url=f"https://site{i % 2}.com/doc",
                position=i + 1,
                heading_path="",
                content=f"unrelated filler topic paragraph number {i}",
                word_count=6,
                source_title="Doc",
            )
            for i in range(6)
        ]
        target = Chunk(
            id="c-hit",
            document_url="https://primary.com/fpt",
            position=1,
            heading_path="FPT AI",
            content="FPT AI datacenter revenue grew 28 percent year over year",
            word_count=10,
            source_title="FPT Report",
        )
        scored = [(0.9, c) for c in chunks] + [(0.6, target)]
        ranked = await HeuristicReranker(max_chunks=24, max_per_url=4).rerank(
            "FPT AI revenue", scored
        )
        assert ranked[0].id == "c-hit"

        model_dir = tmp_path / "bge"
        (model_dir / "onnx").mkdir(parents=True)
        (model_dir / "onnx" / "model_int8.onnx").write_bytes(b"stub")
        s = Settings(ENVIRONMENT="test", RERANKER="bge", BGE_RERANKER_DIR=str(model_dir))
        assert (
            BGEReranker._pick_file(s, str(model_dir)).replace("\\", "/") == "onnx/model_int8.onnx"
        )

        with pytest.raises(RerankerUnavailable):
            await ONNXReranker(model_dir="").rerank("q", scored[:1])

    async def test_scraper_preserves_partial_timeout_and_strips_jina_preamble(self):
        scraper = Scraper(Settings(ENVIRONMENT="test", CRAWL4AI_ENABLED=False))

        async def _fake_scrape(url: str, *, timeout_s: float | None = None):
            if "slow" in url:
                await asyncio.sleep(5.0)
            return PageContent(
                url=url,
                final_url=url,
                title="Fast Page",
                markdown="Fast markdown body",
                extractor_used="trafilatura",
                fetched_at=datetime.now(UTC),
            )

        scraper.scrape = _fake_scrape  # type: ignore[method-assign]
        pages = await scraper.scrape_many(
            ["https://fast.example/1", "https://slow.example/2"],
            timeout_s=0.15,
        )
        assert any(isinstance(p, PageContent) and p.url == "https://fast.example/1" for p in pages)
        assert any(p.url == "https://slow.example/2" and p.status == "failed" for p in pages)

    def test_table_chunking_preserves_header_and_prompts_sec07_delimiters(self):
        rows = "\n".join(f"| VCB_{i} | {10 + i}.5% | 3.2x | Buy |" for i in range(180))
        md = f"# Financials\n\n| Ticker | ROE | P/B | Rating |\n|---|---|---|---| \n{rows}"
        chunks = chunk_markdown(md, document_url="https://example.com/fin", source_title="VN Banks")
        assert len(chunks) >= 2
        assert chunks[1].content.startswith("| Ticker | ROE |")

        included = build_context(chunks, budget=5000, max_per_url=3)
        rendered = render_context(included)
        assert "VN Banks (https://example.com/fin)" in rendered
        assert "<<<SRC_" in rendered
        assert "<<<END_SRC_" in rendered

    def test_numeric_fidelity_and_answer_json_cleans_invalid_citations(self):
        chunk = Chunk(
            id="c1",
            document_url="https://example.com/q3",
            position=1,
            heading_path="",
            content="FPT Q3 revenue reached 15,920 tỷ đồng, growing 24.8% YoY.",
            word_count=10,
            source_title="FPT Q3",
        )
        good_text = "FPT achieved 24.8% YoY revenue growth at 15,920 tỷ [1]."
        bad_text = "FPT achieved 99.9% margin with 88,888 tỷ [1]."
        fid_good, unv_good = verify_numeric_fidelity(good_text, [(1, chunk)])
        fid_bad, unv_bad = verify_numeric_fidelity(bad_text, [(1, chunk)])
        assert fid_good == 1.0 and unv_good == []
        assert fid_bad == 0.0 and len(unv_bad) == 2
        assert "24.8%" in extract_numeric_tokens(good_text)
        c_check = check_claims(bad_text, [(1, chunk)])
        assert c_check.numeric_fidelity == 0.0
