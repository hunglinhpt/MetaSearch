"""Wave 4 tests: v4.0 Evaluation Suite fixes & golden benchmarks (all offline)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.eval_metrics import (
    aggregate,
    aggregate_by_slice,
    citation_precision_rate,
    hit_at_k,
    latency_percentiles,
    ndcg_at_k,
    numeric_fidelity_rate,
)
from app.eval_research import evaluate_research_golden
from app.eval_sentiment import evaluate_sentiment_golden
from app.extraction.chunker import Chunk
from app.retrieval.merger import merge
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import HeuristicReranker

_EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"


class TestEvalMetricsV40:
    def test_ndcg_at_k_never_exceeds_one_with_duplicate_domain_matches(self):
        # Multiple URLs matching the single relevant substring "arxiv.org"
        urls = [
            "https://arxiv.org/abs/1",
            "https://arxiv.org/abs/2",
            "https://arxiv.org/abs/3",
            "https://github.com/org/repo",
        ]
        score = ndcg_at_k(urls, ["arxiv.org"], k=10)
        assert score == 1.0
        assert score <= 1.0

        # Two relevant substrings across 4 URLs -> deduplicated gain matching <= 1.0
        urls_mixed = [
            "https://arxiv.org/1",
            "https://arxiv.org/2",
            "https://other.org/3",
            "https://github.com/4",
        ]
        mixed_score = ndcg_at_k(urls_mixed, ["arxiv.org", "github.com"], k=10)
        assert 0.0 < mixed_score < 1.0

    def test_numeric_fidelity_citation_precision_latency_and_slices(self):
        chunks = {1: "FPT grew revenue by 19.5% to 15,200 billion VND."}
        assert numeric_fidelity_rate("FPT revenue grew 19.5% [1].", chunks) == 1.0
        assert numeric_fidelity_rate("FPT revenue jumped 99.9% [1].", chunks) == 0.0
        assert citation_precision_rate("Claim [1] and invalid [9].", {1, 2}) == 0.5

        lat = latency_percentiles([100, 200, 300, 400, 1000])
        assert lat["p50_ms"] == 300.0
        assert lat["p95_ms"] == 1000.0
        assert lat["p99_ms"] == 1000.0

        by_slice = aggregate_by_slice(
            [
                {"slice": "vi:financial", "hit_at_5": 1.0, "ndcg_at_10": 0.9},
                {"slice": "vi:financial", "hit_at_5": 0.5, "ndcg_at_10": 0.7},
                {"slice": "en:developer", "hit_at_5": 1.0, "ndcg_at_10": 1.0},
            ]
        )
        assert by_slice["vi:financial"]["hit_at_5"] == 0.75
        assert by_slice["en:developer"]["n_queries"] == 1
        assert aggregate([{"hit_at_5": 1.0}])["n_queries"] == 1


class TestGoldenBenchmarksOffline:
    def test_golden_v3_vi_hygiene(self):
        data = json.loads((_EVALS_DIR / "golden_v3_vi.json").read_text(encoding="utf-8"))
        queries = data["queries"]
        assert len(queries) == 25
        assert "25 VI queries" in data["description"]
        for item in queries:
            q = item["query"]
            assert "(không dấu)" not in q
            # Zero unintended CJK characters
            assert not re.search(r"[\u4e00-\u9fff]", q), f"CJK char in {q!r}"
            for dom in item["relevant"]:
                assert dom == dom.lower()

    def test_sentiment_golden_benchmark_passes(self):
        report = evaluate_sentiment_golden(_EVALS_DIR / "sentiment_golden.json")
        assert report["n_cases"] >= 60
        assert report["macro_f1"] >= 0.85, report
        assert report["collision_rejection_rate"] == 1.0, report
        assert report["sentfin_attribution_accuracy"] == 1.0, report
        assert report["passed"] is True

    async def test_search_offline_ir_benchmark_and_reranker_diversity(self):
        data = json.loads((_EVALS_DIR / "search_offline_ir.json").read_text(encoding="utf-8"))
        registry = {
            "openalex": EngineConfig(
                id="openalex", name="OpenAlex", category="academic", weight=1.0
            ),
            "crossref": EngineConfig(
                id="crossref", name="Crossref", category="academic", weight=0.95
            ),
            "searxng": EngineConfig(id="searxng", name="SearXNG", category="general", weight=1.0),
            "google_news": EngineConfig(
                id="google_news", name="Google News", category="news", weight=1.0
            ),
            "vn_wire": EngineConfig(id="vn_wire", name="VN Wire", category="news", weight=1.0),
            "sec_edgar": EngineConfig(id="sec_edgar", name="SEC", category="financial", weight=1.1),
            "bing_news": EngineConfig(
                id="bing_news", name="Bing News", category="news", weight=0.95
            ),
            "github": EngineConfig(id="github", name="GitHub", category="developer", weight=1.0),
            "ddg": EngineConfig(id="ddg", name="DDG", category="general", weight=0.7),
        }
        hits5: list[float] = []
        ndcgs: list[float] = []
        for q in data["queries"]:
            raws = [
                RawResult(
                    url=c["url"],
                    title=c["title"],
                    snippet=c["snippet"],
                    adapter=c["adapter"],
                    source_engines=c["source_engines"],
                    rank=c["rank"],
                    source_url=c.get("source_url"),
                )
                for c in q["candidates"]
            ]
            ranked = merge(raws, query=q["query"], registry=registry, news=bool(q.get("news")))
            urls = [r.source_url or r.url for r in ranked]
            # For wrapper links (news.google.com), check effective domain too
            eff_urls = [f"https://{r.domain}/" if r.domain else r.url for r in ranked]
            combined_urls = [
                u if any(rel in u for rel in q["relevant"]) else eu
                for u, eu in zip(urls, eff_urls, strict=True)
            ]
            hits5.append(hit_at_k(combined_urls, q["relevant"], 5))
            ndcgs.append(ndcg_at_k(combined_urls, q["relevant"], 10))
        assert sum(hits5) / len(hits5) >= 0.9
        assert sum(ndcgs) / len(ndcgs) >= 0.8

        # HeuristicReranker enforces <= 4 chunks per URL & <= 24 total chunks
        sample_chunks = [
            (
                1.0 - i * 0.01,
                Chunk(
                    id=f"c{i}" * 16,
                    document_url="https://dominant.com/doc" if i < 10 else f"https://alt{i}.com/p",
                    position=i,
                    heading_path="",
                    content=f"PostgreSQL replication streaming WAL throughput benchmark {i}",
                    word_count=8,
                ),
            )
            for i in range(30)
        ]
        reranked = await HeuristicReranker().rerank(
            "PostgreSQL replication streaming", sample_chunks
        )
        assert len(reranked) <= 24
        dom_count = sum(1 for c in reranked if c.document_url == "https://dominant.com/doc")
        assert dom_count <= 4

    def test_research_golden_benchmark_passes(self):
        report = evaluate_research_golden(_EVALS_DIR / "research_golden.json")
        assert report["passed"] is True, report
        assert report["summary"]["fact_score"] >= 0.85
