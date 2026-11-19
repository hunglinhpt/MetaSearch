"""docs/30 BP-1 (thread carry) + BP-4 (citation streaming) tests.

Covers: _load_thread_context owner isolation + fail-open, the pipeline
thread-rewrite hop (event payload + retrieval actually uses the rewrite),
and the citation_map SSE event that precedes the token stream.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import tests.test_api_v1 as _tav
from app.api import deps
from app.api.v1.answer import _load_thread_context
from app.core.auth import current_api_key_id
from app.db.models import Answer, Base, Query
from app.llm.gateway import Usage
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import SearchParams
from app.retrieval.models import EngineConfig
from app.retrieval.reranker import HeuristicReranker
from tests.test_api_v1 import (
    FakeAdapter,
    _page,
    raw,
)


@pytest.fixture
def stack(monkeypatch):
    """Same patch stack as test_api_v1's `stack` fixture (copy, not call —
    fixtures cannot be invoked directly). Patches deps with fakes and
    returns a holder with .adapters/.pages/.gateway/.cache/.install()."""

    class Stack:
        def __init__(self):
            self.adapters: list[FakeAdapter] = []
            self.pages: dict = {}
            self.gateway = _tav.FakeGateway()
            self.cache = _tav.FakeCache()

        def install(self):
            configs = {
                a.id: EngineConfig(id=a.id, name=a.id, category="general", enabled=True)
                for a in self.adapters
            }
            store = InMemoryEngineStore(configs)
            monkeypatch.setattr(deps, "get_engine_store", lambda: store)
            monkeypatch.setattr(
                deps, "build_adapters", lambda settings: {a.id: a for a in self.adapters}
            )
            monkeypatch.setattr(deps, "get_cache", lambda: self.cache)
            monkeypatch.setattr(deps, "get_scraper", lambda: _tav.FakeScraper(self.pages))
            monkeypatch.setattr(deps, "get_gateway", lambda: self.gateway)
            monkeypatch.setattr(deps, "get_embedder", lambda: _tav.FakeEmbedder())
            monkeypatch.setattr(deps, "get_reranker", lambda: HeuristicReranker())
            deps.reset_pipelines()

    return Stack()


@pytest.fixture
def mem_db(monkeypatch):
    """Sync fixture: engine + factory patched into deps. Tables are created
    inside each test (async, same event loop) to avoid aiosqlite loop binding."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(deps, "get_session_factory", lambda: factory)
    return engine, factory


async def _seed_answer(
    factory, owner: str, q_text: str = "VNM là gì?", a_md: str = "VNM là Vinamilk [1]."
) -> str:
    async with factory() as session:
        q = Query(raw_text=q_text, api_key_id=owner)
        session.add(q)
        await session.flush()
        a = Answer(
            query_id=q.id,
            api_key_id=owner,
            kind="answer",
            content_md=a_md,
            model="fake",
            status="complete",
        )
        session.add(a)
        await session.commit()
        return str(a.id)


# --- BP-1: _load_thread_context -------------------------------------------------


class TestLoadThreadContext:
    async def test_returns_prior_question_and_answer(self, mem_db):
        engine, factory = mem_db
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        owner = "key-owner-1"
        answer_id = await _seed_answer(factory, owner)
        current_api_key_id.set(owner)

        ctx = await _load_thread_context(answer_id)
        assert ctx is not None
        assert ctx["prior_answer_id"] == answer_id
        assert ctx["prior_question"] == "VNM là gì?"
        assert "Vinamilk" in ctx["prior_answer_md"]
        assert len(ctx["prior_answer_md"]) <= 2000

    async def test_owner_isolation(self, mem_db):
        """Another owner's answer id must never leak content (Rule 3)."""
        engine, factory = mem_db
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed_answer(factory, "owner-a")
        current_api_key_id.set("owner-b")

        ctx = await _load_thread_context(str(await _seed_answer(factory, "owner-a")))
        assert ctx is None  # not owned → treated as not found

    async def test_garbage_thread_id_fails_open(self, mem_db):
        engine, _factory = mem_db
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        current_api_key_id.set("owner-a")

        ctx = await _load_thread_context("not-a-uuid")
        assert ctx is None  # invalid id never raises — thread carry is best-effort


# --- BP-1: pipeline thread rewrite ----------------------------------------------


class _RewriteGateway:
    """Records every complete() call (task-tagged); streams a fixed token."""

    provider = "openai"

    def __init__(self):
        self.complete_calls: list[tuple[str, list]] = []

    async def complete(self, messages, *, model=None, max_tokens=None, task="intent"):
        self.complete_calls.append((task, list(messages)))
        return "VNM Vinamilk cổ tức 2026", Usage(input_tokens=10, output_tokens=8)

    async def stream(self, messages, *, model, max_tokens=None):
        yield _FakeDelta(text="Cổ tức VNM 2026 [1].")
        yield _FakeDelta(usage=Usage(input_tokens=5, output_tokens=3))


class _FakeDelta:
    def __init__(self, text=None, usage=None):
        self.text = text
        self.usage = usage
        self.finish_reason = "stop" if usage is not None else None


class TestThreadRewrite:
    async def test_rewrite_event_and_query_used(self, stack, monkeypatch):
        """Follow-up query is rewritten standalone using the prior turn, and
        the thread_rewrite event reports original vs rewritten correctly."""
        s = stack
        s.adapters.append(
            FakeAdapter(
                "fake",
                [
                    raw(
                        "https://vnm.example/dividend",
                        title="VNM dividend",
                        snippet="VNM dividend 2026",
                    )
                ],
            )
        )
        s.pages["https://vnm.example/dividend"] = _page(
            "https://vnm.example/dividend", "VNM dividend", "# VNM\nDividend 2026 announced."
        )
        s.install()
        pipeline = deps.get_answer_pipeline()
        gateway = _RewriteGateway()
        monkeypatch.setattr(pipeline, "gateway", gateway)

        events: list[tuple[str, dict]] = []
        agen = pipeline.run(
            SearchParams(query="còn cổ tức thì sao"),
            thread_context={
                "prior_question": "VNM là gì?",
                "prior_answer_md": "VNM là Vinamilk [1].",
            },
        )
        async for ev in agen:
            events.append(ev)

        names = [name for name, _data in events]
        assert "thread_rewrite" in names
        _name, data = next(e for e in events if e[0] == "thread_rewrite")
        assert data["original"] == "còn cổ tức thì sao"
        assert data["rewritten"] == "VNM Vinamilk cổ tức 2026"
        # The rewrite prompt carried both turns (recorded as the thread_rewrite task)
        rewrite_calls = [msgs for task, msgs in gateway.complete_calls if task == "thread_rewrite"]
        assert rewrite_calls, "gateway.complete must be called with task='thread_rewrite'"
        prompt_text = rewrite_calls[0][0]["content"]
        assert "VNM là gì?" in prompt_text and "còn cổ tức thì sao" in prompt_text

    async def test_rewrite_fail_open(self, stack, monkeypatch):
        """A broken rewrite call must not break the answer flow."""

        class _BrokenRewriteGateway(_RewriteGateway):
            async def complete(self, messages, **kw):
                raise RuntimeError("rewrite provider down")

        s = stack
        s.adapters.append(FakeAdapter("fake", [raw("https://x.example/", title="x", snippet="x")]))
        s.pages["https://x.example/"] = _page("https://x.example/", "x", "# x\ncontent")
        s.install()
        pipeline = deps.get_answer_pipeline()
        monkeypatch.setattr(pipeline, "gateway", _BrokenRewriteGateway())

        names: list[str] = []
        async for ev in pipeline.run(
            SearchParams(query="follow-up question"),
            thread_context={"prior_question": "prior q", "prior_answer_md": "prior a"},
        ):
            names.append(ev[0])
        assert "thread_rewrite" not in names  # rewrite failed silently
        assert "done" in names  # the answer still completed


# --- BP-4: citation_map precedes the token stream -------------------------------


def _parse_sse_events(body: str) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    # sse-starlette frames use CRLF; normalize first (same rule as lib/api.ts).
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    for frame in body.split("\n\n"):
        name = None
        data_lines: list[str] = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if name and data_lines:
            try:
                out.append((name, json.loads("\n".join(data_lines))))
            except json.JSONDecodeError:
                continue
    return out


class TestCitationMapSse:
    def test_citation_map_precedes_tokens(self, client, stack):
        s = stack
        s.adapters.append(
            FakeAdapter(
                "fake",
                [raw("https://db.example/postgres", title="Postgres guide", snippet="pg")],
            )
        )
        s.pages["https://db.example/postgres"] = _page(
            "https://db.example/postgres",
            "Postgres guide",
            "# Postgres\ntutorial text about relational databases",
        )
        s.install()
        resp = client.post("/v1/answer", json={"query": "postgres", "stream": True})
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        names = [name for name, _ in events]
        assert "citation_map" in names, f"citation_map missing from {names}"
        assert "token" in names
        assert names.index("citation_map") < names.index("token"), (
            "citation_map must precede the token stream (BP-4)"
        )
        _, data = next(e for e in events if e[0] == "citation_map")
        assert isinstance(data["map"], list)
        for entry in data["map"]:
            assert set(entry) >= {"n", "url", "title", "chunk_id"}
            assert entry["n"] >= 1

    def test_citation_map_absent_when_no_chunks(self, client, stack):
        """No retrieved documents → no dangling citation_map."""
        s = stack
        s.install()
        resp = client.post("/v1/answer", json={"query": "noresults", "stream": True})
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        assert all(name != "citation_map" for name, _ in events)
