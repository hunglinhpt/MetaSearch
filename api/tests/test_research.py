"""Deep Research pipeline + job store tests (fakes, no network/LLM)."""

from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.extraction.chunker import Chunk
from app.extraction.scraper import PageContent
from app.llm.gateway import LLMError, StreamDelta, Usage
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import SearchPipeline
from app.orchestration.research import ContextPack, ResearchParams, ResearchPipeline
from app.orchestration.research_jobs import ResearchJob, ResearchJobStore
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.registry import EngineRegistry


class FakeAdapter:
    def __init__(self, engine_id, results):
        self.id = engine_id
        self.results = results

    async def search(self, query, **kwargs):
        return self.results


def raw(url, rank=0):
    return RawResult(
        url=url,
        title=f"T{rank}",
        snippet="postgres replication streaming",
        adapter="searxng",
        source_engines=["google"],
        rank=rank,
    )


class FakeGateway:
    """Scripted LLM: plan -> extract -> extract... -> report streaming."""

    def __init__(self, plan=None, extracts=None, fail_planner=False):
        self.plan = plan or {
            "sub_queries": [
                {"query": "q overview", "purpose": "definition"},
                {"query": "q numbers", "purpose": "data"},
            ],
            "categories": ["general"],
        }
        self.extracts = list(extracts or [])
        self.fail_planner = fail_planner
        self.calls = 0

    async def complete(self, messages, *, model, max_tokens=None, task="intent"):
        self.calls += 1
        if self.fail_planner and task == "planner":
            raise LLMError("offline")
        if task == "planner":
            return json.dumps(self.plan), Usage(10, 5)
        if task == "extractor":
            payload = self.extracts.pop(0) if self.extracts else {"learnings": [], "gaps": []}
            return json.dumps(payload), Usage(20, 8)
        raise LLMError(f"unexpected complete task {task}")

    async def stream(self, messages, *, model, max_tokens=None):
        yield StreamDelta(text="Report body [1].")
        yield StreamDelta(text=" Conclusion [2].")
        yield StreamDelta(usage=Usage(100, 40))


class FakeScraper:
    def __init__(self, pages):
        self.pages = pages

    async def scrape_many(self, urls, **kwargs):
        return [self.pages.get(u) for u in urls if self.pages.get(u)]


def page(url, title, md):
    from datetime import UTC, datetime

    return PageContent(
        url=url,
        final_url=url,
        title=title,
        markdown=md,
        extractor_used="trafilatura",
        fetched_at=datetime.now(UTC),
    )


PAGE_MD = "# R\n\nStreaming replication ships wal records to a standby.\n"


def build_pipeline(monkeypatch, gateway):

    adapter = FakeAdapter("searxng", [raw("https://a.com/1", 0), raw("https://b.com/2", 1)])
    registry = EngineRegistry(
        InMemoryEngineStore(
            {"searxng": EngineConfig(id="searxng", name="s", category="general", enabled=True)}
        ),
        {"searxng": adapter},
    )
    search = SearchPipeline(get_settings(), registry)
    pages = {
        "https://a.com/1": page("https://a.com/1", "One", PAGE_MD),
        "https://b.com/2": page("https://b.com/2", "Two", PAGE_MD),
    }
    return ResearchPipeline(get_settings(), search, FakeScraper(pages), gateway)


class TestResearchPipeline:
    async def test_full_flow_events_and_result(self, monkeypatch):
        gateway = FakeGateway(
            extracts=[
                {
                    "learnings": [{"fact": "wal ships to standby", "citation_id": 1}],
                    "gaps": ["pricing?"],
                },
                {"learnings": [{"fact": "done", "citation_id": 2}], "gaps": []},
            ]
        )
        pipeline = build_pipeline(monkeypatch, gateway)
        events = [
            e async for e in pipeline.run(ResearchParams(query="postgres replication", depth=3))
        ]

        names = [n for n, _ in events]
        assert names[0] == "status"  # planning
        assert "plan" in names
        # v2.6 P1-1 coverage gate: gaps are empty after iteration 2 but the
        # planner sub-questions are not covered by the learnings yet, so the
        # loop probes once more up to depth=3.
        assert names.count("iteration_start") == 3
        assert "learnings" in names
        assert names[-1] == "done"
        done = events[-1][1]
        assert done["status"] == "complete"
        assert done["truncated"] is False
        assert done["usage"]["input_tokens"] > 0

    async def test_planner_fallback_heuristic(self, monkeypatch):
        gateway = FakeGateway(fail_planner=True, extracts=[{"learnings": [], "gaps": []}])
        pipeline = build_pipeline(monkeypatch, gateway)
        events = [e async for e in pipeline.run(ResearchParams(query="general topic", depth=1))]
        plan = next(data for name, data in events if name == "plan")
        assert len(plan["sub_queries"]) == 4  # aspect-templated fallback
        assert plan["sub_queries"][0]["purpose"] == "overview and definition"

    async def test_truncated_on_budget(self, monkeypatch):
        gateway = FakeGateway(
            extracts=[
                {"learnings": [], "gaps": ["still more?"]},
                {"learnings": [], "gaps": ["more again?"]},
                {"learnings": [], "gaps": ["and more?"]},
            ]
        )
        pipeline = build_pipeline(monkeypatch, gateway)
        events = [e async for e in pipeline.run(ResearchParams(query="q", depth=2, max_time_s=60))]
        done = events[-1][1]
        # depth=2 with persistent gaps -> report written from what exists
        assert done["status"] == "complete"
        assert "Report body" in "".join(d["t"] for n, d in events if n == "report_token")

    async def test_report_citations_validated(self, monkeypatch):
        gateway = FakeGateway(extracts=[{"learnings": [], "gaps": []}])
        pipeline = build_pipeline(monkeypatch, gateway)
        events = [e async for e in pipeline.run(ResearchParams(query="q", depth=1))]
        citations = next(d for n, d in events if n == "citations")["citations"]
        assert citations, "report cites [1]/[2] which must resolve"
        assert all(c["url"] for c in citations)


class TestContextPack:
    def test_merge_dedups_sources_and_chunks(self):
        pack = ContextPack()
        c1 = Chunk(
            id="a" * 32,
            document_url="https://x.com",
            position=1,
            heading_path="",
            content="c1",
            word_count=2,
        )
        c2 = Chunk(
            id="b" * 32,
            document_url="https://x.com",
            position=2,
            heading_path="",
            content="c2",
            word_count=2,
        )
        accepted = pack.merge([c1], [{"url": "https://x.com", "title": "T"}])
        assert [ch.id for ch in accepted] == [c1.id]
        accepted2 = pack.merge([c2], [{"url": "https://x.com", "title": "T"}])
        assert accepted2 == []  # duplicate chunk URL dropped
        assert len(pack.chunks) == 1
        assert len(pack.sources) == 1


class TestJobStore:
    async def test_create_get_and_cap(self):
        store = ResearchJobStore()
        job = await store.create("q")
        assert (await store.get(str(job.id))) is job
        assert (await store.get(str(job.id))).status == "queued"

    async def test_get_invalid_and_missing(self):
        store = ResearchJobStore()
        from app.core.errors import MetaSearchError

        with pytest.raises(MetaSearchError):
            await store.get("not-a-uuid")
        with pytest.raises(MetaSearchError):
            await store.get("00000000-0000-0000-0000-000000000000")

    async def test_events_wait_returns_buffered(self):
        job = ResearchJob(id=__import__("uuid").uuid4(), query="q")
        job.push(("status", {"stage": "planning"}))
        index, event = await job.wait_event(0, 0.1)
        assert index == 1
        assert event[0] == "status"
        index, event = await job.wait_event(index, 0.05)
        assert event is None  # nothing new; timeout

    async def test_restore_from_db(self, tmp_path, monkeypatch):
        import uuid

        from app.config import get_settings
        from app.db.bootstrap import ensure_schema
        from app.db.models import Answer, Query
        from app.db.session import dispose_engine, get_sessionmaker

        db_path = tmp_path / "restore.db"
        monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db_path}")
        await dispose_engine()
        assert await ensure_schema() is True

        job_id = uuid.uuid4()
        answer_id = uuid.uuid4()

        try:
            async with get_sessionmaker()() as session:
                session.add(
                    Query(
                        id=job_id,
                        raw_text="test restore query",
                        normalized_text="test restore query",
                    )
                )
                session.add(
                    Answer(
                        id=answer_id,
                        query_id=job_id,
                        kind="research",
                        content_md="# Restored Report",
                        status="complete",
                        model="gpt-4o",
                    )
                )
                await session.commit()

            store = ResearchJobStore()
            restored = await store.get(str(job_id))
            assert restored is not None
            assert restored.id == job_id
            assert restored.query == "test restore query"
            assert restored.status == "done"
            assert restored.result["report_md"] == "# Restored Report"
        finally:
            await dispose_engine()


class TestHarnessProfiles:
    def test_detect_profile_keywords(self):
        from app.orchestration.research import detect_profile

        assert detect_profile("Postgres vs MySQL performance") == "comparative"
        assert detect_profile("So sánh Docker và Podman") == "comparative"
        assert detect_profile("How to fix Python asyncio exception") == "developer"
        assert detect_profile("Deep learning transformer attention arxiv paper") == "academic"
        assert detect_profile("Thủ đô của nước Pháp là gì") == "general"

    def test_detect_profile_explicit_override(self):
        from app.orchestration.research import detect_profile

        assert detect_profile("python error", requested="academic") == "academic"
        assert detect_profile("some query", requested="comparative") == "comparative"

    async def test_pipeline_plan_event_carries_profile(self, monkeypatch):
        gateway = FakeGateway(extracts=[{"learnings": [], "gaps": []}])
        pipeline = build_pipeline(monkeypatch, gateway)

        events = [
            e
            async for e in pipeline.run(
                ResearchParams(query="python asyncio bug fix", profile="auto", depth=1)
            )
        ]
        plan = next(data for name, data in events if name == "plan")
        assert plan["profile"] == "developer"

        events_academic = [
            e
            async for e in pipeline.run(
                ResearchParams(query="some topic", profile="academic", depth=1)
            )
        ]
        plan_acad = next(data for name, data in events_academic if name == "plan")
        assert plan_acad["profile"] == "academic"
