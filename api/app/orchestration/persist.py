"""Best-effort persistence (docs/03-search-pipeline.md stage 11).

Writes never fail the request: every function swallows and logs exceptions.
Called fire-and-forget from the pipelines; used by /v1/search and /v1/answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.logging import get_logger
from app.db.models import Answer, Citation, Document, Query, SearchResult
from app.db.models import Chunk as ChunkRow
from app.db.session import get_sessionmaker
from app.extraction.chunker import Chunk
from app.extraction.scraper import PageContent
from app.retrieval.models import ScoredResult

logger = get_logger(__name__)


async def _run(label: str, coro: Any) -> None:
    try:
        await coro
    except Exception as exc:
        logger.warning("persist_failed", label=label, error=type(exc).__name__)


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
        session.add(
            Query(
                id=query_id,
                raw_text=raw_text,
                normalized_text=normalized_text,
                lang=lang,
                params=params,
            )
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


async def _persist_documents(pages: list[PageContent], chunks: list[Chunk]) -> None:
    """Insert scraped documents + their chunks (skip URLs already stored)."""
    async with get_sessionmaker()() as session:
        doc_ids: dict[str, uuid.UUID] = {}
        for page in pages:
            row = (
                await session.execute(
                    pg_insert(Document)
                    .values(
                        id=uuid.uuid4(),
                        canonical_url=page.final_url or page.url,
                        url_hash=_url_hash(page.final_url or page.url),
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
                        select(Document.id).where(
                            Document.canonical_url == (page.final_url or page.url)
                        )
                    )
                ).first()
                if existing is None:
                    continue
                doc_id: uuid.UUID | None = existing[0]
            else:
                doc_id = row[0]
            if doc_id is not None:
                # Index under both the requested and final URL so chunk lookup
                # still matches when a redirect changed the address.
                doc_ids.setdefault(page.url, doc_id)
                doc_ids.setdefault(page.final_url or page.url, doc_id)

        for chunk in chunks:
            doc_id = doc_ids.get(chunk.document_url)
            if doc_id is None:
                continue
            session.add(
                ChunkRow(
                    id=uuid.UUID(chunk.id),
                    document_id=doc_id,
                    position=chunk.position,
                    heading_path=chunk.heading_path,
                    content=chunk.content,
                    token_count=chunk.word_count,
                )
            )
        await session.commit()


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
) -> None:
    """``citations``: (n, url, title, chunk_id-or-None)."""
    async with get_sessionmaker()() as session:
        session.add(
            Answer(
                id=answer_id,
                query_id=query_id,
                kind="quick",
                content_md=content_md,
                model=model,
                input_tokens=input_tokens or None,
                output_tokens=output_tokens or None,
                cost_usd=cost_usd or None,
                latency_ms=latency_ms,
                status="complete",
            )
        )
        for n, url, _title, chunk_id in citations:
            session.add(
                Citation(
                    answer_id=answer_id,
                    n=n,
                    url=url,
                    chunk_id=uuid.UUID(chunk_id) if chunk_id else None,
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
    task = asyncio.create_task(
        _run(
            "search",
            asyncio.gather(
                _persist_query(query_id, raw_text, normalized_text, lang, params),
                _persist_search_results(query_id, results),
            ),
        )
    )
    task.add_done_callback(_log_task_exception)


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
    task = asyncio.create_task(_run("documents", _persist_documents(pages, chunks)))
    task.add_done_callback(_log_task_exception)


def _log_task_exception(task: asyncio.Task) -> None:
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.warning("persist_task_failed", error=type(exc).__name__)


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
        session.add(
            Query(
                id=research_id,
                raw_text=question,
                normalized_text=question,
                lang=lang,
                params={"kind": "research", "truncated": truncated},
            )
        )
        session.add(
            Answer(
                id=answer_id,
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
            session.add(
                Citation(
                    answer_id=answer_id,
                    n=n,
                    url=url,
                    chunk_id=uuid.UUID(chunk_id) if chunk_id else None,
                    valid=True,
                )
            )
        await session.commit()


async def persist_report(
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
    task = asyncio.create_task(
        _run(
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
    )
    task.add_done_callback(_log_task_exception)


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
