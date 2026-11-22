"""Persist plumbing round 2 (docs/20 P1-3 late chunking, WP-13 task tracking):
embedder-backed chunk vectors, background task lifecycle, error swallowing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.extraction.chunker import Chunk
from app.extraction.scraper import PageContent
from app.orchestration import persist


def _page(md: str = "short content") -> PageContent:
    return PageContent(
        url="https://a.example/doc",
        final_url="https://a.example/doc",
        title="Doc",
        markdown=md,
        extractor_used="trafilatura",
        fetched_at=datetime.now(UTC),
    )


def _chunk(content: str, position: int) -> Chunk:
    return Chunk(
        id=f"c{position}",
        document_url="https://a.example/doc",
        position=position,
        heading_path="",
        content=content,
        word_count=len(content.split()),
    )


class FakeEmbedder:
    enabled = True

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[list[str]] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self._fail:
            raise RuntimeError("embedder down")
        return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.fixture(autouse=True)
def _reset_embedder():
    yield
    persist.configure_embedder(None)


@pytest.mark.asyncio
async def test_doc_chunk_embeddings_short_doc_direct(monkeypatch):
    embedder = FakeEmbedder()
    persist.configure_embedder(embedder)
    chunks = [_chunk(f"chunk text {i}", i) for i in range(1, 4)]
    out = await persist._doc_chunk_embeddings(_page("tiny"), chunks)
    assert set(out) == {1, 2, 3}
    assert out[1] == [0.1, 0.2, 0.3]
    assert embedder.calls and len(embedder.calls[0]) == 3


@pytest.mark.asyncio
async def test_doc_chunk_embeddings_late_chunking_segment_inheritance(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "late_chunking_enabled", True)
    monkeypatch.setattr(settings, "late_chunking_doc_max_chars", 50)
    monkeypatch.setattr(settings, "late_chunking_segment_chars", 40)
    monkeypatch.setattr(settings, "late_chunking_segment_overlap_chars", 5)
    embedder = FakeEmbedder()
    persist.configure_embedder(embedder)
    long_md = " ".join(f"segment {i} unique words alpha beta gamma" for i in range(5))
    chunks = [_chunk(f"segment {i} unique words", i) for i in range(1, 4)]
    out = await persist._doc_chunk_embeddings(_page(long_md), chunks)
    assert out, "each chunk must inherit its containing segment's vector"
    assert all(v == [0.1, 0.2, 0.3] for v in out.values())


@pytest.mark.asyncio
async def test_doc_chunk_embeddings_fail_open(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    # no embedder, no vision API configured -> {}
    monkeypatch.setattr(settings, "vision_embed_api_base_url", "")
    assert await persist._doc_chunk_embeddings(_page(), [_chunk("x", 1)]) == {}
    # embedder errors are swallowed -> {}
    persist.configure_embedder(FakeEmbedder(fail=True))
    assert await persist._doc_chunk_embeddings(_page(), [_chunk("x", 1)]) == {}


# --- background task lifecycle -----------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_task_fallback_tracks_and_runs():
    done: list[str] = []
    task = persist._spawn_task("t1", asyncio.sleep(0, result=None))
    assert task is not None
    # attach a real effect via a second task
    persist._spawn_task("t2", _record(done))
    await persist.wait_background(timeout_s=2.0)
    assert done == ["ran"]
    assert persist._background_tasks == set()
    # wait_background on empty set is a no-op
    await persist.wait_background(timeout_s=0.01)


async def _record(done: list[str]) -> None:
    await asyncio.sleep(0)
    done.append("ran")


@pytest.mark.asyncio
async def test_run_swallows_exceptions():
    async def boom() -> None:
        raise RuntimeError("nope")

    await persist._run("label", boom())  # must not raise


@pytest.mark.asyncio
async def test_discard_background_tasks_cancels_pending():
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(30)

    task = persist._spawn_task("slow", slow())
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=1.0)
    persist.discard_background_tasks()
    await asyncio.sleep(0)  # let the cancel request land
    # discard() both cancels the task and closes the coroutine (double-kill);
    # whichever wins, the task must be finished and the registry cleared.
    assert task.done()
    assert persist._background_tasks == set()


@pytest.mark.asyncio
async def test_spawn_task_unstarted_coroutine_closed_without_warning():
    # create a coroutine that is never awaited; discard must close it cleanly
    async def never() -> None:
        await asyncio.sleep(0)

    coro = never()
    task = asyncio.create_task(asyncio.sleep(0))  # dummy tracked task
    persist._background_tasks.add(task)
    persist._pending_coros[task] = coro
    persist.discard_background_tasks()
    import inspect

    assert inspect.getcoroutinestate(coro) == "CORO_CLOSED"


# --- dialect insert helper ---------------------------------------------------------


def test_dialect_insert_picks_sqlite_for_sqlite_session():
    class FakeDialect:
        name = "sqlite"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        bind = FakeBind()

    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    assert persist._dialect_insert(FakeSession()) is sqlite_insert
