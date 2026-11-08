"""Wave 3 package B: writes are bounded, survive shutdown, expire, and migrate on SQLite."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event, func, select

import app.db.session as session_mod
from app.api import deps
from app.config import get_settings
from app.db.bootstrap import ensure_schema
from app.db.models import Answer, Chunk, Citation, Document, Query, SearchResult
from app.db.session import dispose_engine, get_engine, get_sessionmaker
from app.extraction.scraper import PageContent, PageFailure
from app.llm.gateway import StreamDelta, Usage
from app.main import app
from app.orchestration import persist_queue
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.retention import BATCH_SIZE, run_retention
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.reranker import HeuristicReranker

API = Path(__file__).resolve().parents[1]
URL = "https://a.example/1"
_SWAPPED = (
    "get_engine_store",
    "build_adapters",
    "get_scraper",
    "get_gateway",
    "get_embedder",
    "get_reranker",
)


class _Adapter:
    id = "fake"

    async def search(self, query, **kwargs):
        return [
            RawResult(
                url=URL,
                title="Postgres",
                snippet="postgres btree",
                adapter="fake",
                source_engines=[],
                rank=0,
            )
        ]


class _Gateway:
    provider = "openai"
    default_quick_model = "m"

    def with_override(self, override):
        return self

    async def stream(self, messages, **kwargs):
        yield StreamDelta(text="Postgres [1].", finish_reason="stop")
        yield StreamDelta(usage=Usage(input_tokens=1, output_tokens=1))


def _install() -> None:
    store = InMemoryEngineStore(
        {"fake": EngineConfig(id="fake", name="fake", category="general", enabled=True)}
    )
    page = PageContent(
        url=URL,
        final_url=URL,
        title="T",
        markdown="# T\n\n" + "postgres btree " * 80,
        extractor_used="trafilatura",
        fetched_at=datetime.now(UTC),
    )

    class Scraper:
        async def scrape_many(self, urls, **kwargs):
            return [
                page if u == URL else PageFailure(url=u, status="failed", error="x") for u in urls
            ]

    class Embedder:
        enabled = False

    gw = _Gateway()
    deps.get_engine_store = lambda: store
    deps.build_adapters = lambda s: {"fake": _Adapter()}
    deps.get_scraper = lambda: Scraper()
    deps.get_gateway = lambda: gw
    deps.get_embedder = lambda: Embedder()
    deps.get_reranker = lambda: HeuristicReranker()
    deps.reset_pipelines()


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'b.db').as_posix()}")
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    get_settings.cache_clear()
    await dispose_engine()
    await ensure_schema()
    await persist_queue.reset_persist_queue()
    saved = {n: getattr(deps, n) for n in _SWAPPED}
    yield
    for n, v in saved.items():
        setattr(deps, n, v)
    deps.reset_pipelines()
    await persist_queue.reset_persist_queue()
    await dispose_engine()
    get_settings.cache_clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30
    )


def _slow_commits(monkeypatch, seconds: float) -> None:
    real = session_mod.get_sessionmaker

    def slow_maker():
        maker = real()

        def factory(*a, **k):
            s = maker(*a, **k)
            orig = s.commit

            async def commit():
                await asyncio.sleep(seconds)
                return await orig()

            s.commit = commit
            return s

        return factory

    import app.orchestration.persist as persist_mod

    monkeypatch.setattr(session_mod, "get_sessionmaker", slow_maker)
    monkeypatch.setattr(persist_mod, "get_sessionmaker", slow_maker)


async def _count(model) -> int:
    async with get_sessionmaker()() as s:
        return int((await s.execute(select(func.count()).select_from(model))).scalar_one())


async def test_persist_goes_through_queue(monkeypatch):
    _install()
    _slow_commits(monkeypatch, 2.0)
    async with app.router.lifespan_context(app):
        base = len(asyncio.all_tasks())
        async with _client() as c:
            for i in range(60):
                assert (
                    await c.post("/v1/search", json={"query": f"postgres q {i}"})
                ).status_code == 200
        assert len(asyncio.all_tasks()) - base <= 10, (
            "one task per write means the queue is bypassed"
        )


async def test_queue_full_drops_not_blocks():
    queue = persist_queue.PersistQueue(maxsize=3)
    ran = []

    async def write(n):
        ran.append(n)

    coros = []
    for n in range(10):
        coro = write(n)
        coros.append(coro)
        queue.enqueue(persist_queue.PersistItem("t", lambda c: c, (coro,)))
    assert queue.dropped == 7
    assert await queue.drain(budget_s=1) == 3
    assert ran == [7, 8, 9], "the newest writes are kept"
    assert all(c.cr_frame is None for c in coros), "dropped writes are closed, not leaked"


async def test_shutdown_drains_answers(monkeypatch):
    _install()
    _slow_commits(monkeypatch, 0.3)
    async with app.router.lifespan_context(app), _client() as c:
        r = await c.post("/v1/answer", json={"query": "postgres shutdown"})
    assert r.status_code == 200
    me = asyncio.current_task()
    rest = [t for t in asyncio.all_tasks() if t is not me]
    for t in rest:
        t.cancel()
    await asyncio.gather(*rest, return_exceptions=True)
    assert await _count(Answer) == 1


async def _seed(old: datetime):
    async with get_sessionmaker()() as s:
        q_old, q_new = Query(raw_text="old", created_at=old), Query(raw_text="new")
        s.add_all([q_old, q_new])
        await s.flush()
        a_old = Answer(query_id=q_old.id, kind="quick", content_md="old", created_at=old)
        a_new = Answer(query_id=q_new.id, kind="quick", content_md="new")
        doc = Document(
            canonical_url=f"https://d.example/{uuid.uuid4().hex}",
            url_hash=uuid.uuid4().hex,
            domain="d.example",
        )
        s.add_all([a_old, a_new, doc])
        await s.flush()
        chunk = Chunk(document_id=doc.id, position=1, content="old content", created_at=old)
        s.add(chunk)
        await s.flush()
        s.add_all(
            [
                Citation(answer_id=a_old.id, n=1, url="https://o.example", chunk_id=chunk.id),
                Citation(answer_id=a_new.id, n=1, url="https://n.example", chunk_id=chunk.id),
            ]
        )
        await s.commit()
        return q_old.id, a_new.id, chunk.id


async def test_retention_handles_citations():
    old = datetime.now(UTC) - timedelta(days=400)
    q_old, a_new, chunk_id = await _seed(old)
    await run_retention()
    async with get_sessionmaker()() as s:
        answers = {a.id for a in (await s.execute(select(Answer))).scalars()}
        cites = (await s.execute(select(Citation))).scalars().all()
        queries = {q.id for q in (await s.execute(select(Query))).scalars()}
        chunk = await s.get(Chunk, chunk_id)
    assert answers == {a_new}
    assert [c.answer_id for c in cites] == [a_new]
    assert q_old not in queries
    assert chunk is not None and chunk.content == "", "cited chunk rows stay, their content goes"


async def test_retention_deletes_in_batches():
    old = datetime.now(UTC) - timedelta(days=400)
    async with get_sessionmaker()() as s:
        q = Query(raw_text="old", created_at=old)
        s.add(q)
        await s.flush()
        s.add_all(
            [SearchResult(query_id=q.id, rank=i, created_at=old) for i in range(2 * BATCH_SIZE + 5)]
        )
        await s.commit()
    biggest = {"rows": 0, "deletes": 0}

    def after(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("DELETE FROM SEARCH_RESULTS"):
            biggest["deletes"] += 1
            biggest["rows"] = max(biggest["rows"], cursor.rowcount or 0)

    event.listen(get_engine().sync_engine, "after_cursor_execute", after)
    counts = await run_retention()
    assert counts["search_results"] == 2 * BATCH_SIZE + 5
    assert biggest["rows"] <= BATCH_SIZE and biggest["deletes"] >= 3
    assert await _count(SearchResult) == 0


async def test_retention_dry_run_changes_nothing():
    await _seed(datetime.now(UTC) - timedelta(days=400))
    counts = await run_retention(dry_run=True)
    assert counts["answers"] == 1 and counts["citations"] == 1
    assert await _count(Answer) == 2


def test_retention_cli_dry_run(tmp_path, monkeypatch):
    from app.cli import main

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'cli.db').as_posix()}")
    get_settings.cache_clear()
    asyncio.run(ensure_schema())
    assert main(["retention", "--dry-run"]) == 0


def test_migrations_match_models(tmp_path, monkeypatch):
    from alembic.config import Config

    from alembic import command
    from app.db import models  # noqa: F401
    from app.db.base import Base

    db = tmp_path / "mig.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db.as_posix()}")
    monkeypatch.chdir(API)
    get_settings.cache_clear()
    cfg = Config(str(API / "alembic.ini"))
    command.upgrade(cfg, "head")
    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(Base.metadata.tables) <= tables
    for name, table in Base.metadata.tables.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({name})")}
        assert {c.name for c in table.columns} <= have, name
    con.close()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


async def test_sqlite_pragmas_per_connection():
    for _ in range(2):
        async with get_engine().connect() as conn:
            assert (
                str((await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()).lower() == "wal"
            )
            assert int((await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()) >= 1000
        await dispose_engine()


async def test_wal_set_on_every_new_connection(tmp_path, monkeypatch):
    # A brand-new file that ensure_schema never touched: only the per-connection pragma sets WAL.
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'fresh.db').as_posix()}")
    get_settings.cache_clear()
    await dispose_engine()
    async with get_engine().connect() as conn:
        assert str((await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()).lower() == "wal"
    await dispose_engine()


async def test_shutdown_waits_for_fallback_writes():
    from app.orchestration import persist

    done = []

    async def slow_write():
        await asyncio.sleep(0.3)
        done.append(1)

    assert persist._spawn_task("t", slow_write()) is not None  # queue not running: fallback task
    await persist.wait_background(2.0)
    assert done == [1]


async def test_retention_runs_at_most_daily(tmp_path, monkeypatch):
    from app.orchestration import retention

    calls = []

    async def fake_run(**kwargs):
        calls.append(1)
        return {}

    monkeypatch.setattr(retention, "run_retention", fake_run)
    marker = tmp_path / "last.retention"
    assert await retention.run_if_due(last_run_path=str(marker)) == {}
    assert marker.exists()
    assert await retention.run_if_due(last_run_path=str(marker)) is None
    marker.write_text("not a number")
    assert await retention.run_if_due(last_run_path=str(marker)) == {}
    assert len(calls) == 2


async def test_retention_loop_keeps_going_after_errors(monkeypatch):
    from app.orchestration import retention

    calls = []

    async def flaky(**kwargs):
        calls.append(1)
        raise RuntimeError("db busy")

    monkeypatch.setattr(retention, "run_if_due", flaky)
    task = asyncio.create_task(retention.retention_loop(interval_s=0.05, last_run_path=None))
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 3


async def test_lite_runs_retention_in_the_api_and_stops_it(monkeypatch):
    from app.orchestration import retention

    async def quiet(**kwargs):
        return None

    monkeypatch.setattr(retention, "run_if_due", quiet)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    get_settings.cache_clear()
    async with app.router.lifespan_context(app):
        names = {t.get_name() for t in asyncio.all_tasks() if not t.done()}
        assert "retention-loop" in names
    await asyncio.sleep(0)
    assert not any(t.get_name() == "retention-loop" and not t.done() for t in asyncio.all_tasks())
