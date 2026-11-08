"""Best-effort persistence (docs/03-search-pipeline.md stage 11).

Writes never fail the request: every function swallows and logs exceptions.
Called fire-and-forget from the pipelines; used by /v1/search and /v1/answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.auth import current_api_key_id
from app.core.logging import get_logger
from app.db.models import Answer, Citation, Document, Query, SearchResult
from app.db.models import Chunk as ChunkRow
from app.db.session import get_sessionmaker
from app.extraction.chunker import Chunk
from app.extraction.scraper import PageContent
from app.retrieval.canonical import canonicalize
from app.retrieval.models import ScoredResult

logger = get_logger(__name__)

# Hold strong references to fire-and-forget tasks so Python's GC never collects
# an in-flight coroutine before it finishes.
_background_tasks: set[asyncio.Task[Any]] = set()


def _dialect_insert(session: Any):
    """Postgres and SQLite both support ON CONFLICT — pick the right dialect insert."""
    if session.bind is not None and session.bind.dialect.name == "sqlite":
        return sqlite_insert
    return pg_insert


async def _run(label: str, coro: Any) -> None:
    try:
        await coro
    except Exception as exc:
        logger.warning("persist_failed", label=label, error=type(exc).__name__)


def _spawn_task(label: str, coro: Any) -> asyncio.Task[None] | None:
    """Hand one DB write to the bounded persist queue.

    The queue is started by the app lifespan. Without it (scripts, some tests) the write
    falls back to a tracked task, as before WP-13.
    """
    from app.orchestration.persist_queue import PersistItem, get_persist_queue

    queue = get_persist_queue()
    if queue.running:
        queue.enqueue(PersistItem(label, _run, (label, coro)))
        return None
    task = asyncio.create_task(_run(label, coro))
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


async def wait_background(timeout_s: float) -> None:
    """Wait for fallback write tasks still running; used on shutdown."""
    pending = [t for t in list(_background_tasks) if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout_s)


def _on_task_done(task: asyncio.Task[Any]) -> None:
    _background_tasks.discard(task)
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.warning("persist_task_failed", error=type(exc).__name__)


def _url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()  # nosec B324 - dedup key, not security


async def _persist_query(
    query_id: uuid.UUID,
    raw_text: str,
    normalized_text: str,
    lang: str | None,
    params: dict[str, Any],
) -> None:
    async with get_sessionmaker()() as session:
        insert = _dialect_insert(session)
        await session.execute(
            insert(Query)
            .values(
                id=query_id,
                raw_text=raw_text,
                normalized_text=normalized_text,
                lang=lang,
                params=params,
                # FIX1: record the owning key (None while auth is disabled or
                # WP-11 has not set the context yet — never fabricated).
                api_key_id=current_api_key_id.get(),
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.commit()


async def _persist_search_results(query_id: uuid.UUID, results: list[ScoredResult]) -> None:
    async with get_sessionmaker()() as session:
        for rank, item in enumerate(results, start=1):
            session.add(
                SearchResult(
                    query_id=query_id,
                    rank=rank,
                    score=item.score,
                    consensus=item.consensus,
                    found_by=item.adapters,
                    raw_snippet=item.snippet,
                )
            )
        await session.commit()


async def _persist_search_all(
    query_id: uuid.UUID,
    raw_text: str,
    normalized_text: str,
    lang: str | None,
    params: dict[str, Any],
    results: list[ScoredResult],
) -> None:
    """Persist Query + SearchResults atomically in one session to avoid FK races."""
    async with get_sessionmaker()() as session:
        insert = _dialect_insert(session)
        await session.execute(
            insert(Query)
            .values(
                id=query_id,
                raw_text=raw_text,
                normalized_text=normalized_text,
                lang=lang,
                params=params,
                # FIX1: record the owning key (same rationale as _persist_query).
                api_key_id=current_api_key_id.get(),
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.flush()
        for rank, item in enumerate(results, start=1):
            session.add(
                SearchResult(
                    query_id=query_id,
                    rank=rank,
                    score=item.score,
                    consensus=item.consensus,
                    found_by=item.adapters,
                    raw_snippet=item.snippet,
                )
            )
        await session.commit()


async def _persist_documents(pages: list[PageContent], chunks: list[Chunk]) -> None:
    """Insert scraped documents + their chunks (skip URLs already stored)."""
    async with get_sessionmaker()() as session:
        insert = _dialect_insert(session)
        doc_ids: dict[str, uuid.UUID] = {}
        new_doc_ids: set[uuid.UUID] = set()
        for page in pages:
            raw_target = page.final_url or page.url
            canon = canonicalize(raw_target) or raw_target
            domain = (urlsplit(canon).hostname or "").lower()
            row = (
                await session.execute(
                    insert(Document)
                    .values(
                        id=uuid.uuid4(),
                        canonical_url=canon,
                        url_hash=_url_hash(canon),
                        domain=domain,
                        title=page.title,
                        content_md=page.markdown,
                        content_hash=_url_hash(page.markdown),
                        fetched_at=page.fetched_at,
                        fetch_status="ok",
                        extractor_used=page.extractor_used,
                    )
                    .on_conflict_do_nothing(index_elements=["canonical_url"])
                    .returning(Document.id)
                )
            ).first()
            if row is None:
                # Already stored by an earlier request; reuse its id.
                existing = (
                    await session.execute(
                        select(Document.id).where(Document.canonical_url == canon)
                    )
                ).first()
                if existing is None:
                    continue
                doc_id: uuid.UUID | None = existing[0]
            else:
                doc_id = row[0]
                if doc_id is not None:
                    new_doc_ids.add(doc_id)
            if doc_id is not None:
                # Index under requested, final, and canonical URL so chunk lookup
                # always matches even when redirects or tracking params existed.
                doc_ids.setdefault(page.url, doc_id)
                doc_ids.setdefault(raw_target, doc_id)
                doc_ids.setdefault(canon, doc_id)

        for chunk in chunks:
            doc_id = doc_ids.get(chunk.document_url) or doc_ids.get(
                canonicalize(chunk.document_url)
            )
            if doc_id is None or doc_id not in new_doc_ids:
                continue
            try:
                chunk_uuid = uuid.UUID(chunk.id)
            except ValueError:
                chunk_uuid = uuid.uuid4()
            await session.execute(
                insert(ChunkRow)
                .values(
                    id=chunk_uuid,
                    document_id=doc_id,
                    position=chunk.position,
                    heading_path=chunk.heading_path,
                    content=chunk.content,
                    token_count=chunk.word_count,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
        await session.commit()


async def _resolve_valid_chunk_ids(
    session: Any, citations: list[tuple[int, str, str, str | None]]
) -> set[uuid.UUID]:
    """Return the subset of citation chunk UUIDs that actually exist in chunks table."""
    candidate_ids: list[uuid.UUID] = []
    for _n, _url, _title, cid in citations:
        if cid:
            try:
                candidate_ids.append(uuid.UUID(str(cid)))
            except ValueError:
                continue
    if not candidate_ids:
        return set()
    rows = (
        (await session.execute(select(ChunkRow.id).where(ChunkRow.id.in_(candidate_ids))))
        .scalars()
        .all()
    )
    return set(rows)


async def _persist_answer(
    answer_id: uuid.UUID,
    query_id: uuid.UUID,
    content_md: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    citations: list[tuple[int, str, str, str | None]],
    status: str = "complete",
) -> None:
    """``citations``: (n, url, title, chunk_id-or-None)."""
    async with get_sessionmaker()() as session:
        existing_chunks = await _resolve_valid_chunk_ids(session, citations)
        session.add(
            Answer(
                id=answer_id,
                api_key_id=current_api_key_id.get(),
                query_id=query_id,
                kind="quick",
                content_md=content_md,
                model=model,
                input_tokens=input_tokens or None,
                output_tokens=output_tokens or None,
                cost_usd=cost_usd or None,
                latency_ms=latency_ms,
                status=status,
            )
        )
        for n, url, _title, chunk_id in citations:
            parsed_cid: uuid.UUID | None = None
            if chunk_id:
                try:
                    candidate = uuid.UUID(str(chunk_id))
                    if candidate in existing_chunks:
                        parsed_cid = candidate
                except ValueError:
                    parsed_cid = None
            session.add(
                Citation(
                    answer_id=answer_id,
                    n=n,
                    url=url,
                    chunk_id=parsed_cid,
                    valid=True,
                )
            )
        await session.commit()


def persist_search(
    query_id: uuid.UUID,
    raw_text: str,
    normalized_text: str,
    lang: str | None,
    params: dict[str, Any],
    results: list[ScoredResult],
) -> None:
    _spawn_task(
        "search",
        _persist_search_all(query_id, raw_text, normalized_text, lang, params, results),
    )


async def persist_answer(
    answer_id: uuid.UUID,
    query_id: uuid.UUID,
    content_md: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    citations: list[tuple[int, str, str, str | None]],
    status: str = "complete",
) -> None:
    """Awaited by the answer pipeline after the ``done`` event so the
    semantic-cache write (which references the answers FK) runs safely."""
    await _run(
        "answer",
        _persist_answer(
            answer_id,
            query_id,
            content_md,
            model,
            input_tokens,
            output_tokens,
            cost_usd,
            latency_ms,
            citations,
        ),
    )


def persist_pages(pages: list[PageContent], chunks: list[Chunk]) -> None:
    _spawn_task("documents", _persist_documents(pages, chunks))


async def persist_pages_async(pages: list[PageContent], chunks: list[Chunk]) -> None:
    """Awaitable variant of ``persist_pages`` so answer/research pipelines can
    ensure chunks exist before writing ``citations.chunk_id`` foreign keys."""
    await _run("documents", _persist_documents(pages, chunks))


async def _persist_report(
    research_id: uuid.UUID,
    question: str,
    lang: str | None,
    content_md: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    citations: list[tuple[int, str, str, str | None]],
    truncated: bool,
) -> None:
    """Persist the research report as an Answer(kind="research") row."""
    answer_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        insert = _dialect_insert(session)
        await session.execute(
            insert(Query)
            .values(
                id=research_id,
                raw_text=question,
                normalized_text=question,
                lang=lang,
                params={"kind": "research", "truncated": truncated},
                api_key_id=current_api_key_id.get(),
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.flush()
        existing_chunks = await _resolve_valid_chunk_ids(session, citations)
        session.add(
            Answer(
                id=answer_id,
                api_key_id=current_api_key_id.get(),
                query_id=research_id,
                kind="research",
                content_md=content_md,
                model=model,
                input_tokens=input_tokens or None,
                output_tokens=output_tokens or None,
                cost_usd=cost_usd or None,
                latency_ms=latency_ms,
                status="truncated" if truncated else "complete",
            )
        )
        for n, url, _title, chunk_id in citations:
            parsed_cid: uuid.UUID | None = None
            if chunk_id:
                try:
                    candidate = uuid.UUID(str(chunk_id))
                    if candidate in existing_chunks:
                        parsed_cid = candidate
                except ValueError:
                    parsed_cid = None
            session.add(
                Citation(
                    answer_id=answer_id,
                    n=n,
                    url=url,
                    chunk_id=parsed_cid,
                    valid=True,
                )
            )
        await session.commit()


def persist_report(
    research_id: uuid.UUID,
    question: str,
    lang: str | None,
    content_md: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    citations: list[tuple[int, str, str, str | None]],
    truncated: bool,
) -> None:
    """Fire-and-forget report persistence (called from the research task)."""
    _spawn_task(
        "report",
        _persist_report(
            research_id,
            question,
            lang,
            content_md,
            model,
            input_tokens,
            output_tokens,
            cost_usd,
            latency_ms,
            citations,
            truncated,
        ),
    )


async def load_citations(answer_id: uuid.UUID) -> list[dict[str, Any]]:
    """Citations for one answer (used by the L2 semantic-cache replay)."""
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(Citation).where(Citation.answer_id == answer_id).order_by(Citation.n)
                )
            )
            .scalars()
            .all()
        )
    return [
        {"n": row.n, "url": row.url or "", "title": "", "chunk_id": str(row.chunk_id or "")}
        for row in rows
    ]
