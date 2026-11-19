"""Wave 3 tests: v4.0 Deep Research v2 & Council Bias Fix (offline, deterministic)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from app.config import get_settings
from app.extraction.chunker import Chunk
from app.extraction.scraper import PageContent
from app.llm.council import DreamRSICouncilEngine, PanelistDraft
from app.llm.gateway import LLMError, StreamDelta, Usage
from app.llm.providers import LLMOverride
from app.main import create_app
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import SearchPipeline
from app.orchestration.research import (
    ContextPack,
    ResearchParams,
    ResearchPipeline,
    _plan_coverage,
    detect_profile,
)
from app.orchestration.research_jobs import ResearchJob, ResearchJobStore
from app.retrieval.domain_intelligence import classify_domain_intelligence
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.registry import EngineRegistry


class _FakeAdapter:
    def __init__(self, results: list[RawResult]) -> None:
        self.id = "searxng"
        self.results = results
        self.queries: list[str] = []

    async def search(self, query: str, **_kw: Any) -> list[RawResult]:
        self.queries.append(query)
        return self.results


class _FakeScraper:
    def __init__(self, pages: dict[str, PageContent]) -> None:
        self.pages = pages
        self.calls: list[list[str]] = []

    async def scrape_many(self, urls: list[str], **_kw: Any) -> list[PageContent]:
        self.calls.append(list(urls))
        return [self.pages[u] for u in urls if u in self.pages]


class _ScriptedGateway:
    def __init__(
        self, *, fail_outline: bool = False, multi_batch_extracts: list[dict] | None = None
    ):
        self.fail_outline = fail_outline
        self.extract_replies = list(multi_batch_extracts or [])
        self.prompts_seen: list[str] = []

    async def complete(self, messages, *, model, max_tokens=None, task="intent"):
        self.prompts_seen.append(str(messages[-1]["content"]))
        if task == "planner":
            return (
                json.dumps(
                    {
                        "sub_queries": [
                            {"query": "fpt revenue growth", "purpose": "revenue"},
                            {"query": "fpt valuation pe", "purpose": "valuation"},
                        ],
                        "categories": ["financial", "general"],
                    }
                ),
                Usage(15, 10),
            )
        if task == "outline":
            if self.fail_outline:
                raise LLMError("outline offline")
            return (
                json.dumps(
                    {
                        "perspectives": ["Fundamentals", "Quant & Risk"],
                        "sections": [
                            "Executive Summary",
                            "Revenue & Growth",
                            "Risks & Counterarguments",
                            "References",
                        ],
                    }
                ),
                Usage(12, 8),
            )
        if task == "extractor":
            reply = (
                self.extract_replies.pop(0)
                if self.extract_replies
                else {"learnings": [], "gaps": [], "conflicts": []}
            )
            return json.dumps(reply), Usage(20, 10)
        raise LLMError(f"unexpected {task}")

    async def stream(self, messages, *, model, max_tokens=None):
        yield StreamDelta(text="## Executive Summary\nFPT revenue rose 20% [1]. ")
        yield StreamDelta(text="Peers note margin compression risk [2].\n")
        yield StreamDelta(usage=Usage(80, 30))


def _make_page(url: str, title: str, text: str) -> PageContent:
    return PageContent(
        url=url,
        final_url=url,
        title=title,
        markdown=text,
        extractor_used="trafilatura",
        fetched_at=datetime.now(UTC),
    )


class TestResearchUpgrades:
    def test_profile_and_coverage_diacritic_folding(self):
        assert detect_profile("phan tich co phieu FPT dinh gia") == "financial"
        assert detect_profile("so sanh docker vs podman khac nhau") == "comparative"
        assert detect_profile("Thủ đô của nước Pháp là gì") == "general"

        class _Lr:
            def __init__(self, fact: str) -> None:
                self.fact = fact

        plan = [("thuế thu nhập cá nhân", "quy định"), ("tăng trưởng doanh thu thép", "sản lượng")]
        cov = _plan_coverage(plan, [_Lr("quy dinh thue thu nhap ca nhan moi nhat")])
        assert 0.4 <= cov < 1.0
        cov_full = _plan_coverage(
            plan,
            [_Lr("quy dinh thue thu nhap ca nhan va tang truong doanh thu thep san luong")],
        )
        assert cov_full == 1.0

    def test_context_pack_accepts_up_to_6_chunks_per_doc(self):
        pack = ContextPack()
        chunks = [
            Chunk(
                id=f"c{i}" * 16,
                document_url="https://example.com/long",
                position=i,
                heading_path=f"S{i}",
                content=f"chunk content {i} " * 10,
                word_count=30,
            )
            for i in range(8)
        ]
        accepted = pack.merge(chunks, [{"url": "https://example.com/long", "title": "Long"}])
        assert len(accepted) == 6
        assert len(pack.chunks) == 6

    async def test_prescrape_dedup_multibatch_extract_storm_devils_advocate_and_verification(
        self, monkeypatch
    ):
        adapter = _FakeAdapter(
            [
                RawResult(
                    url="https://a.com/doc1",
                    title="Doc 1",
                    snippet="alpha revenue growth 20%",
                    adapter="searxng",
                    source_engines=["google"],
                    rank=0,
                ),
                RawResult(
                    url="https://b.com/doc2",
                    title="Doc 2",
                    snippet="alpha margin pressure risks",
                    adapter="searxng",
                    source_engines=["bing"],
                    rank=1,
                ),
            ]
        )
        registry = EngineRegistry(
            InMemoryEngineStore(
                {"searxng": EngineConfig(id="searxng", name="s", category="general", enabled=True)}
            ),
            {"searxng": adapter},
        )
        search = SearchPipeline(get_settings(), registry)
        huge_block = "word " * 1400  # forces 2+ extractor batches when 2+ chunks merged
        pages = {
            "https://a.com/doc1": _make_page(
                "https://a.com/doc1", "Doc1", f"# Revenue\n\nrevenue growth 20% {huge_block}"
            ),
            "https://b.com/doc2": _make_page(
                "https://b.com/doc2", "Doc2", f"# Risks\n\nmargin compression risk {huge_block}"
            ),
        }
        scraper = _FakeScraper(pages)
        # Note: out-of-batch citation_id=99 must be filtered out; valid batch IDs preserved
        gw = _ScriptedGateway(
            multi_batch_extracts=[
                {
                    "learnings": [
                        {"fact": "revenue grew 20%", "citation_id": 1},
                        {"fact": "hallucinated id", "citation_id": 99},
                    ],
                    "gaps": ["competitor margin"],
                    "conflicts": [
                        {
                            "topic": "Growth sustainability",
                            "claim_a": "20% growth continues",
                            "citation_a": 1,
                            "claim_b": "margins compressing",
                            "citation_b": 2,
                        }
                    ],
                }
            ]
        )

        # Ensure flag_injection is invoked cleanly during report stage
        import app.orchestration.research as res_mod

        inj_called: list[int] = []
        orig_flag = res_mod.flag_injection

        def _spy_flag(included):
            inj_called.append(len(included))
            return orig_flag(included)

        monkeypatch.setattr(res_mod, "flag_injection", _spy_flag)

        pipe = ResearchPipeline(get_settings(), search, scraper, gw)
        events = [
            ev
            async for ev in pipe.run(
                ResearchParams(
                    query="alpha beta market architecture risks?",
                    breadth=2,
                    depth=2,
                    profile="general",
                )
            )
        ]

        names = [n for n, _ in events]
        assert names[0] == "status"
        assert "plan" in names and "outline" in names
        assert "verification" in names
        assert names[-1] == "done"

        # Pre-scrape dedup: iteration 0 scrapes doc1 & doc2; iteration 1 must NOT re-scrape them
        assert len(scraper.calls) == 1
        assert set(scraper.calls[0]) == {"https://a.com/doc1", "https://b.com/doc2"}

        # Devil's Advocate counter-thesis ran on iteration 1
        assert any("risks" in q.lower() or "counterarguments" in q.lower() for q in adapter.queries)

        # Hallucinated citation_id=99 was rejected
        first_learnings = next(d for n, d in events if n == "learnings")["learnings"]
        assert "hallucinated id" not in first_learnings
        assert "revenue grew 20%" in first_learnings

        # flag_injection ran & FACT verification emitted clean metrics
        assert inj_called and inj_called[0] >= 2
        verif = next(d for n, d in events if n == "verification")
        assert verif["citation_count"] == 2
        assert verif["numeric_fidelity"] == 1.0
        assert verif["citation_coverage"] == 1.0
        assert verif["conflicts_count"] == 1

    async def test_stage0_financial_tools_and_outline_fallback(self, monkeypatch):
        import app.retrieval.events as events_mod
        import app.retrieval.sentiment_quant as sq_mod

        async def _fake_wire(**_kw):
            return {
                "items": [
                    {
                        "url": "https://cafef.vn/fpt-wire.chn",
                        "title": "FPT Q4 outlook",
                        "summary": "FPT wins major AI infrastructure contract",
                        "source": "CafeF",
                    }
                ]
            }

        async def _fake_events(**_kw):
            return {
                "events": [
                    {
                        "symbol": "FPT",
                        "event_type": "CASH_DIVIDEND",
                        "title": "Tạm ứng cổ tức tiền mặt 10%",
                        "ex_date": "2026-12-01",
                    }
                ]
            }

        async def _fake_quant(sym, **_kw):
            return [], "VNDIRECT", f"https://dchart.vndirect.com.vn/?symbol={sym}", None, None

        monkeypatch.setattr(events_mod, "fetch_market_news_wire", _fake_wire)
        monkeypatch.setattr(events_mod, "fetch_market_events", _fake_events)
        monkeypatch.setattr(sq_mod, "fetch_sentiment_quant_context", _fake_quant)

        adapter = _FakeAdapter(
            [
                RawResult(
                    url="https://vietstock.vn/fpt-1",
                    title="FPT report",
                    snippet="FPT revenue growth and AI backlog",
                    adapter="searxng",
                    source_engines=["google"],
                    rank=0,
                )
            ]
        )
        registry = EngineRegistry(
            InMemoryEngineStore(
                {"searxng": EngineConfig(id="searxng", name="s", category="general", enabled=True)}
            ),
            {"searxng": adapter},
        )
        search = SearchPipeline(get_settings(), registry)
        gw = _ScriptedGateway(fail_outline=True)
        pipe = ResearchPipeline(get_settings(), search, _FakeScraper({}), gw)

        events = [
            ev
            async for ev in pipe.run(
                ResearchParams(query="Phân tích cổ phiếu FPT 2026", breadth=2, depth=1)
            )
        ]
        tool_ev = next(d for n, d in events if n == "tool_results")
        tool_names = {t["tool"] for t in tool_ev["tools"]}
        assert {"market_news_wire", "market_events"} <= tool_names

        outline_ev = next(d for n, d in events if n == "outline")
        assert outline_ev["profile"] == "financial"
        assert len(outline_ev["sections"]) >= 4

    async def test_stage0_handles_live_dataclass_wire_and_events(self, monkeypatch):
        from types import SimpleNamespace

        import app.retrieval.events as events_mod
        import app.retrieval.sentiment_quant as sq_mod
        from app.retrieval.events import CorporateEventRecord, ParsedNewsItem

        async def _fake_wire(**_kw):
            return {
                "items": [
                    ParsedNewsItem(
                        title="FPT wins global cloud transformation deal",
                        url="https://cafef.vn/fpt-dc-wire.chn",
                        source_id="cafef_rss",
                        snippet="FPT international IT revenue climbs 31% YoY.",
                        published_at=datetime.now(UTC),
                        symbols=["FPT"],
                    )
                ]
            }

        async def _fake_events(**_kw):
            return {
                "events": [
                    CorporateEventRecord(
                        event_id="ev-fpt-dc-1",
                        symbol="FPT",
                        exchange="HOSE",
                        event_type="dividend_calendar",
                        event_subtype="cash_dividend",
                        severity="info",
                        risk_level=1,
                        title="FPT cash dividend 10%",
                        ex_date="2026-12-01",
                    )
                ],
                "risk_alerts": [],
            }

        async def _fake_quant(sym, **_kw):
            return (
                [SimpleNamespace(close=135.5)],
                "VNDIRECT",
                f"https://dchart.vndirect.com.vn/?symbol={sym}",
                "2026-11-19",
                None,
            )

        monkeypatch.setattr(events_mod, "fetch_market_news_wire", _fake_wire)
        monkeypatch.setattr(events_mod, "fetch_market_events", _fake_events)
        monkeypatch.setattr(sq_mod, "fetch_sentiment_quant_context", _fake_quant)

        adapter = _FakeAdapter(
            [
                RawResult(
                    url="https://vietstock.vn/fpt-dc-1",
                    title="FPT report",
                    snippet="FPT revenue growth and AI backlog",
                    adapter="searxng",
                    source_engines=["google"],
                    rank=0,
                )
            ]
        )
        registry = EngineRegistry(
            InMemoryEngineStore(
                {"searxng": EngineConfig(id="searxng", name="s", category="general", enabled=True)}
            ),
            {"searxng": adapter},
        )
        search = SearchPipeline(get_settings(), registry)
        gw = _ScriptedGateway(fail_outline=True)
        pipe = ResearchPipeline(get_settings(), search, _FakeScraper({}), gw)

        events = [
            ev
            async for ev in pipe.run(
                ResearchParams(query="Phân tích cổ phiếu FPT 2026", breadth=2, depth=1)
            )
        ]
        tool_ev = next(d for n, d in events if n == "tool_results")
        tool_names = {t["tool"] for t in tool_ev["tools"]}
        assert {"market_news_wire", "market_events", "financial_quant"} <= tool_names


class TestCouncilBiasFixes:
    async def test_dual_reviewer_shuffle_self_exclusion_and_8000_context(self):
        captured: list[dict[str, Any]] = []

        class _FakeGW:
            def with_override(self, ov):
                class _Bound:
                    provider = ov.provider

                    async def stream(self, messages, *, model=None):
                        captured.append(
                            {
                                "provider": ov.provider,
                                "model": model,
                                "user": messages[-1]["content"],
                            }
                        )
                        # Deliberately inflate own draft (Candidate A on openai)
                        scores = (
                            [
                                {
                                    "candidate_id": "Candidate A",
                                    "score": 9.9,
                                    "strengths": "self",
                                    "flaws": "",
                                },
                                {
                                    "candidate_id": "Candidate B",
                                    "score": 7.0,
                                    "strengths": "ok",
                                    "flaws": "",
                                },
                            ]
                            if ov.provider == "openai"
                            else [
                                {
                                    "candidate_id": "Candidate A",
                                    "score": 5.0,
                                    "strengths": "weak",
                                    "flaws": "gap",
                                },
                                {
                                    "candidate_id": "Candidate B",
                                    "score": 8.0,
                                    "strengths": "solid",
                                    "flaws": "",
                                },
                            ]
                        )
                        yield StreamDelta(text=json.dumps({"reviews": scores}))

                return _Bound()

        engine = DreamRSICouncilEngine(get_settings(), _FakeGW())
        ok_drafts = [
            PanelistDraft(
                candidate_id="Candidate A",
                provider="openai",
                model="gpt-4o-mini",
                answer_md="Draft A content [1]",
                valid_citations=[1],
                invalid_citations=[],
                citation_precision=1.0,
                quantitative_density=0.5,
                source_coverage=0.5,
                deterministic_score=7.0,
                peer_score=7.0,
                composite_score=7.0,
                latency_ms=10,
            ),
            PanelistDraft(
                candidate_id="Candidate B",
                provider="groq",
                model="llama-3.3-70b-versatile",
                answer_md="Draft B content [1]",
                valid_citations=[1],
                invalid_citations=[],
                citation_precision=1.0,
                quantitative_density=0.5,
                source_coverage=0.5,
                deterministic_score=7.0,
                peer_score=7.0,
                composite_score=7.0,
                latency_ms=10,
            ),
        ]
        panel = [
            LLMOverride(provider="openai", model="gpt-4o-mini"),
            LLMOverride(provider="groq", model="llama-3.3-70b-versatile"),
        ]
        long_context = "SRC_LONG " * 900  # ~8100 chars > old 4000 cap
        pack = classify_domain_intelligence("test question")
        reviews = await engine._run_anonymous_cross_review(
            "test question", long_context, ok_drafts, panel, pack
        )

        # Two reviewers executed concurrently and saw >4000 chars of source context
        assert len(captured) == 2
        assert all(len(c["user"]) > 5000 for c in captured)
        # Candidate order differed across the 2 reviewers to prevent position bias
        pos_orders = [
            (c["user"].index("=== Candidate A ==="), c["user"].index("=== Candidate B ==="))
            for c in captured
        ]
        assert (pos_orders[0][0] < pos_orders[0][1]) != (pos_orders[1][0] < pos_orders[1][1])

        # Self-reviews are excluded: Candidate A gets prov_b's 5.0 (NOT prov_a's self-9.9),
        # and Candidate B gets prov_a's 7.0 (NOT prov_b's self-8.0).
        by_cid = {d.candidate_id: d for d in ok_drafts}
        assert by_cid["Candidate A"].peer_score == 5.0
        assert by_cid["Candidate B"].peer_score == 7.0
        assert sum(1 for r in reviews if r.get("self_review_excluded")) == 2


class TestResearchRoutesAndPersistence:
    async def test_cancel_history_sse_resume_and_db_metadata_restore(self, tmp_path, monkeypatch):
        from app.api.v1 import research as research_api
        from app.core.auth import reset_rate_limit_windows
        from app.db.bootstrap import ensure_schema
        from app.db.session import dispose_engine
        from app.orchestration.persist import _persist_report
        from app.orchestration.research_jobs import reset_job_store

        db_path = tmp_path / "research_v40.db"
        monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db_path}")
        reset_job_store()
        reset_rate_limit_windows()
        await dispose_engine()
        assert await ensure_schema() is True

        try:
            # Persisted report carries rich metadata + citation titles
            rid = uuid.uuid4()
            await _persist_report(
                research_id=rid,
                question="Restored research question",
                lang="en",
                content_md="# Heading\nClaim [1].",
                model="gpt-4o",
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.01,
                latency_ms=420,
                citations=[(1, "https://example.com/1", "Example Title", None)],
                truncated=False,
                metadata={
                    "profile": "financial",
                    "learnings": ["L1 insight"],
                    "outline": ["Executive Summary", "References"],
                    "conflicts": [
                        {
                            "topic": "T",
                            "claim_a": "A",
                            "citation_a": 1,
                            "claim_b": "B",
                            "citation_b": 1,
                        }
                    ],
                    "verification": {"numeric_fidelity": 1.0, "citation_coverage": 1.0},
                    "citation_titles": {"1": "Example Title"},
                },
            )

            fresh_store = ResearchJobStore()
            restored = await fresh_store.get(str(rid))
            assert restored.result["learnings"] == ["L1 insight"]
            assert restored.result["outline"] == ["Executive Summary", "References"]
            assert restored.result["verification"]["numeric_fidelity"] == 1.0
            assert restored.result["citations"][0]["title"] == "Example Title"

            listed = await fresh_store.list_jobs(is_admin=True, limit=10)
            assert any(j["research_id"] == str(rid) for j in listed)

            # Active job creation + cancel + after_seq SSE stream via TestClient
            async def _slow_run_job(job: ResearchJob, _params: Any) -> None:
                job.status = "running"
                job.push(("status", {"stage": "planning"}))
                job.push(("outline", {"sections": ["A", "B"], "profile": "general"}))
                job.notify_sync()
                await asyncio.sleep(30)

            monkeypatch.setattr(research_api, "run_job", _slow_run_job)
            reset_job_store()

            with TestClient(create_app()) as client:
                post_r = client.post("/v1/research", json={"query": "cancel test query"})
                assert post_r.status_code == 202
                new_id = post_r.json()["research_id"]

                hist_r = client.get("/v1/research?limit=10")
                assert hist_r.status_code == 200
                ids = {j["research_id"] for j in hist_r.json()["jobs"]}
                assert new_id in ids and str(rid) in ids

                cancel_r = client.post(f"/v1/research/{new_id}/cancel")
                assert cancel_r.status_code == 200
                assert cancel_r.json()["status"] == "cancelled"

                # Resuming after_seq=1 skips seq=1 (status) & yields seq=2 (outline)
                sse_r = client.get(f"/v1/research/{new_id}/events?after_seq=1")
                assert sse_r.status_code == 200
                assert "id: 2" in sse_r.text or "outline" in sse_r.text
                assert "status" not in sse_r.text.split("event: outline")[0]
        finally:
            reset_job_store()
            await dispose_engine()
