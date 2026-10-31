from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.db.bootstrap import ensure_schema
from app.db.models import Document
from app.db.session import dispose_engine, get_sessionmaker
from app.extraction.scraper import PageContent, PageFailure
from app.orchestration.simulator import ReplaySimulator
from app.retrieval.canonical import canonicalize
from app.retrieval.models import RawResult


@pytest.fixture
async def sqlite_db(tmp_path, monkeypatch):
    db_path = tmp_path / "simulator_test.db"
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db_path}")
    await dispose_engine()
    assert await ensure_schema() is True
    yield
    await dispose_engine()


@pytest.mark.asyncio
async def test_replay_simulator_search(sqlite_db):
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        url1 = "https://docs.python.org/3/library/asyncio.html"
        url2 = "https://www.postgresql.org/docs/current/indexes.html"
        doc1 = Document(
            id=uuid.uuid4(),
            canonical_url=url1,
            url_hash=hashlib.sha256(url1.encode()).hexdigest(),
            title="Python Asyncio Documentation",
            content_md=(
                "Asyncio is a library to write concurrent code using the async/await syntax."
            ),
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        )
        doc2 = Document(
            id=uuid.uuid4(),
            canonical_url=url2,
            url_hash=hashlib.sha256(url2.encode()).hexdigest(),
            title="PostgreSQL Indexes",
            content_md="Indexes are a common way to enhance database performance in PostgreSQL.",
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        )
        session.add_all([doc1, doc2])
        await session.commit()

    sim = ReplaySimulator(sessionmaker=sessionmaker)

    # Search matching postgresql
    outcome = await sim.search("postgresql database performance", k=5)
    assert outcome.query == "postgresql database performance"
    assert outcome.replayed_from_db >= 1
    assert len(outcome.results) >= 1
    top = outcome.results[0]
    assert "postgresql.org" in top.domain
    assert top.score > 0
    assert "indexes" in top.snippet.lower() or "performance" in top.snippet.lower()

    # Search with no match
    no_match = await sim.search("quantum teleportation quantum computing", k=5)
    assert no_match.replayed_from_db == 0
    assert len(no_match.results) == 0


@pytest.mark.asyncio
async def test_replay_simulator_scrape(sqlite_db):
    sessionmaker = get_sessionmaker()
    target_url = "https://fastapi.tiangolo.com/tutorial/"
    async with sessionmaker() as session:
        doc = Document(
            id=uuid.uuid4(),
            canonical_url=canonicalize(target_url),
            url_hash=hashlib.sha256(canonicalize(target_url).encode()).hexdigest(),
            title="FastAPI Tutorial",
            content_md="# FastAPI Tutorial\nLearn FastAPI step by step.",
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        )
        session.add(doc)
        await session.commit()

    sim = ReplaySimulator(sessionmaker=sessionmaker)

    # Scrape hit
    page = await sim.scrape(target_url)
    assert isinstance(page, PageContent)
    assert page.title == "FastAPI Tutorial"
    assert page.markdown.startswith("# FastAPI Tutorial")
    assert page.extractor_used == "replay:trafilatura"

    # Scrape miss
    fail = await sim.scrape("https://nonexistent-site-404.com/unknown")
    assert isinstance(fail, PageFailure)
    assert fail.status == "failed"
    assert "not found" in fail.error.lower()


@pytest.mark.asyncio
async def test_replay_simulator_seed_from_results(sqlite_db):
    sessionmaker = get_sessionmaker()
    known_url = "https://example.com/known-article"
    async with sessionmaker() as session:
        doc = Document(
            id=uuid.uuid4(),
            canonical_url=canonicalize(known_url),
            url_hash=hashlib.sha256(canonicalize(known_url).encode()).hexdigest(),
            title="Known Article",
            content_md="Known content for testing.",
            extractor_used="trafilatura",
            fetched_at=datetime.now(UTC),
        )
        session.add(doc)
        await session.commit()

    sim = ReplaySimulator(sessionmaker=sessionmaker)
    raws = [
        RawResult(
            url=known_url,
            title="Known",
            snippet="Known snippet",
            source_engines=["test"],
            adapter="test",
            rank=1,
        ),
        RawResult(
            url="https://example.com/missing-article",
            title="Missing",
            snippet="Missing snippet",
            source_engines=["test"],
            adapter="test",
            rank=2,
        ),
    ]

    count = await sim.seed_from_results(raws)
    assert count == 1
