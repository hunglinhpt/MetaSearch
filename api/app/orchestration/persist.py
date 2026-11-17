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

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.auth import current_api_key_id
from app.core.logging import get_logger
from app.db.models import Answer, Citation, Document, Query, SearchResult
from app.db.models import Chunk as ChunkRow
from app.db.session import get_sessionmaker, sqlite_write_lock
from app.extraction.chunker import Chunk, segment_long_document
from app.extraction.scraper import PageContent
from app.retrieval.canonical import canonicalize
from app.retrieval.models import ScoredResult

logger = get_logger(__name__)

# docs/20 P1-3: optional embedder used to fill Chunk.embedding at persist time
# (late chunking: long docs are embedded per segment, chunks inherit their
# segment's vector). Set once at startup via configure_embedder().
_embedder: Any = None


def configure_embedder(embedder: Any) -> None:
    global _embedder
    _embedder = embedder


async def _doc_chunk_embeddings(
    page: PageContent, doc_chunks: list[Chunk]
) -> dict[int, list[float]]:
    """Map chunk position -> embedding vector for one document.

    Long documents (late chunking): embed the segment windows of the whole
    document once (bounded batch), then each chunk inherits the vector of the
    segment that contains it — the chunk vector therefore carries document
    context. Short documents: embed the chunks directly. Any failure returns
    {} (fail-open: the row is still stored, just without a vector)."""
    from app.config import get_settings

    settings = get_settings()
    embedder = (
        _embedder if (_embedder is not None and getattr(_embedder, "enabled", False)) else None
    )
    # docs/20 P2-1: without the L2 embedder, a configured vision/multimodal API
    # (jina-v4 / NeMo NIM) can still produce chunk vectors for PDF pages.
    use_vision = embedder is None
    if use_vision:
        from app.retrieval.vision import vision_embed_available

        if not vision_embed_available(settings) or not doc_chunks:
            return {}
    elif not doc_chunks:
        return {}
    try:
        md = page.markdown or ""
        if settings.late_chunking_enabled and len(md) > settings.late_chunking_doc_max_chars:
            segments = segment_long_document(
                md,
                segment_chars=settings.late_chunking_segment_chars,
                overlap_chars=settings.late_chunking_segment_overlap_chars,
            )[:12]
            if use_vision:
                from app.retrieval.vision import embed_texts_via_vision_api

                vectors = await asyncio.wait_for(
                    embed_texts_via_vision_api(settings, segments), timeout=15.0
                )
            else:
                embedder_ref = embedder
                if embedder_ref is None:  # mypy: guarded by use_vision above
                    return {}
                vectors = await asyncio.wait_for(embedder_ref.embed_many(segments), timeout=10.0)
            out: dict[int, list[float]] = {}
            for chunk in doc_chunks[:64]:
                probe = chunk.content[:200]
                for seg, vec in zip(segments, vectors, strict=True):
                    if probe and probe in seg:
                        out[chunk.position] = vec
                        break
            return out
        texts = [c.content for c in doc_chunks[:32]]
        if use_vision:
            from app.retrieval.vision import embed_texts_via_vision_api

            vectors = await asyncio.wait_for(
                embed_texts_via_vision_api(settings, texts), timeout=15.0
            )
        else:
            embedder_ref = embedder
            if embedder_ref is None:  # mypy: guarded by use_vision above
                return {}
            vectors = await asyncio.wait_for(embedder_ref.embed_many(texts), timeout=10.0)
        return {c.position: v for c, v in zip(doc_chunks[:32], vectors, strict=True)}
    except Exception as exc:
        logger.warning("persist_embed_failed", url=page.url, error=type(exc).__name__)
        return {}


# Hold strong references to fire-and-forget tasks so Python's GC never collects
# an in-flight coroutine before it finishes.
_background_tasks: set[asyncio.Task[Any]] = set()
_pending_coros: dict[asyncio.Task[Any], Any] = {}


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


def spawn_task(label: str, coro: Any) -> None:
    """Public wrapper: hand one background write to the bounded persist queue."""
    _spawn_task(label, coro)


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
    _pending_coros[task] = coro
    task.add_done_callback(_on_task_done)
    return task


async def wait_background(timeout_s: float) -> None:
    """Wait for fallback write tasks still running; used on shutdown."""
    pending = [t for t in list(_background_tasks) if not t.done()]
    if pending:
        _, still_pending = await asyncio.wait(pending, timeout=timeout_s)
        for t in still_pending:
            t.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)


def _on_task_done(task: asyncio.Task[Any]) -> None:
    _background_tasks.discard(task)
    _pending_coros.pop(task, None)
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.warning("persist_task_failed", error=type(exc).__name__)


def discard_background_tasks() -> None:
    """Cancel fallback write tasks and close un-started coroutines on test teardown."""
    import contextlib

    for task in list(_background_tasks):
        coro = _pending_coros.pop(task, None)
        if not task.done():
            task.cancel()
        coro_live = coro is not None and asyncio.iscoroutine(coro)
        if coro_live and not getattr(coro, "cr_running", False):
            with contextlib.suppress(RuntimeError):
                coro.close()
    _background_tasks.clear()
    _pending_coros.clear()


def _url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()  # nosec B324 - dedup key, not security


async def _persist_query(
    query_id: uuid.UUID,
    raw_text: str,
    normalized_text: str,
    lang: str | None,
    params: dict[str, Any],
) -> None:
    async with sqlite_write_lock(), get_sessionmaker()() as session:
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
    async with sqlite_write_lock(), get_sessionmaker()() as session:
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
    owner: str | None = None,
) -> None:
    """Persist Query + SearchResults atomically in one session to avoid FK races."""
    async with sqlite_write_lock(), get_sessionmaker()() as session:
        insert = _dialect_insert(session)
        await session.execute(
            insert(Query)
            .values(
                id=query_id,
                raw_text=raw_text,
                normalized_text=normalized_text,
                lang=lang,
                params=params,
                api_key_id=owner if owner is not None else current_api_key_id.get(),
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
    """Insert scraped documents + their chunks (skip URLs already stored).

    docs/29 P1-4 (audit findings #4/#10): the embedding fan-out (up to
    10-15 s of awaited HTTP per page) runs BEFORE the SQLite write lock —
    the global write lock must never serialize every background writer
    behind one page's embed calls. Chunk rows are written as one
    executemany statement on SQLite (per-row round trips were the
    persist-queue throughput ceiling); Postgres keeps per-row executes
    because its tsv expression is not executemany-safe.
    """
    # Pre-plan: one entry per distinct canonical URL (duplicate canonicals
    # would race the executemany conflict target).
    plan: list[tuple[PageContent, str, str, str, uuid.UUID]] = []
    seen_canons: set[str] = set()
    for page in pages:
        raw_target = page.final_url or page.url
        # PASS 2 P2-5 (audit B#16 parity): canonicalize() raises on malformed
        # urls — one bad URL must not abort the whole persist batch.
        try:
            canon = canonicalize(raw_target) or raw_target
        except Exception:
            canon = raw_target
        if canon in seen_canons:
            continue
        seen_canons.add(canon)
        domain = (urlsplit(canon).hostname or "").lower()
        plan.append((page, raw_target, canon, domain, uuid.uuid4()))
    hashes = {_url_hash(canon) for (_, _, canon, _, _) in plan}

    # Read-only pre-pass (no write lock) to resolve already-stored docs.
    existing_ids: dict[str, uuid.UUID] = {}
    try:
        async with get_sessionmaker()() as session:
            rows = await session.execute(
                select(Document.id, Document.url_hash).where(Document.url_hash.in_(hashes))
            )
            for found_id, found_hash in rows.all():
                existing_ids[found_hash] = found_id
    except Exception as exc:
        logger.warning("persist_docs_prefetch_failed", error=type(exc).__name__)

    doc_ids: dict[str, uuid.UUID] = {}
    new_doc_ids: set[uuid.UUID] = set()
    doc_values: list[dict[str, Any]] = []
    for page, raw_target, canon, domain, fresh_id in plan:
        url_hash = _url_hash(canon)
        doc_id: uuid.UUID | None = existing_ids.get(url_hash)
        if doc_id is None:
            doc_id = fresh_id
            new_doc_ids.add(doc_id)
            doc_values.append(
                {
                    "id": doc_id,
                    "canonical_url": canon,
                    "url_hash": url_hash,
                    "domain": domain,
                    "title": page.title,
                    "content_md": page.markdown,
                    "content_hash": _url_hash(page.markdown),
                    "fetched_at": page.fetched_at,
                    "fetch_status": "ok",
                    "extractor_used": page.extractor_used,
                }
            )
        # Index under requested, final, and canonical URL so chunk lookup
        # always matches even when redirects or tracking params existed.
        doc_ids.setdefault(page.url, doc_id)
        doc_ids.setdefault(raw_target, doc_id)
        doc_ids.setdefault(canon, doc_id)

    # Embedding fan-out OUTSIDE the write lock (audit finding #4).
    embeddings_by_doc: dict[uuid.UUID, dict[int, list[float]]] = {}
    from app.config import get_settings as _gs
    from app.retrieval.vision import vision_embed_available as _vea

    _can_embed = (_embedder is not None and getattr(_embedder, "enabled", False)) or _vea(_gs())
    if _can_embed:
        for page, _raw, _canon, _dom, _fresh in plan:
            embed_doc_id: uuid.UUID | None = doc_ids.get(page.url)
            if embed_doc_id is None or embed_doc_id not in new_doc_ids:
                continue
            doc_chunks = []
            for c in chunks:
                if c.document_url == page.url:
                    doc_chunks.append(c)
                    continue
                try:
                    if canonicalize(c.document_url) == canonicalize(page.url):
                        doc_chunks.append(c)
                except Exception:  # nosec B112 - skip malformed url, keep batch
                    continue
            embeddings_by_doc[embed_doc_id] = await _doc_chunk_embeddings(page, doc_chunks)

    async with sqlite_write_lock(), get_sessionmaker()() as session:
        insert = _dialect_insert(session)
        if doc_values:
            await session.execute(
                insert(Document)
                .values(doc_values)
                .on_conflict_do_nothing(index_elements=["canonical_url"])
            )
        # docs/29 PASS 2 P1-1: the pre-read SELECT ran OUTSIDE the write lock
        # (and may have failed open with an empty map) — a concurrent writer
        # could own the canonical_url already. Re-resolve ownership from
        # ACTUAL DB state inside the lock, remapping our planned fresh ids
        # onto the stored row ids, before any chunk is written.
        stored: dict[str, uuid.UUID] = {}
        if hashes:
            rows = await session.execute(
                select(Document.id, Document.url_hash).where(Document.url_hash.in_(hashes))
            )
            for stored_id, stored_hash in rows.all():
                stored[stored_hash] = stored_id
        if stored:
            remapped_candidates: set[uuid.UUID] = set()
            for page, raw_target, canon, _domain, fresh_id in plan:
                url_hash = _url_hash(canon)
                real_id = stored.get(url_hash)
                if real_id is None:
                    # our executemany just landed this row under fresh_id
                    real_id = fresh_id
                elif fresh_id in new_doc_ids and real_id != fresh_id:
                    # a concurrent writer (or prefetch miss) owns it — abandon fresh_id
                    new_doc_ids.discard(fresh_id)
                    remapped_candidates.add(real_id)
                    if fresh_id in embeddings_by_doc and real_id not in embeddings_by_doc:
                        embeddings_by_doc[real_id] = embeddings_by_doc.pop(fresh_id)
                    else:
                        embeddings_by_doc.pop(fresh_id, None)
                doc_ids[page.url] = real_id
                doc_ids[raw_target] = real_id
                doc_ids[canon] = real_id
            if remapped_candidates:
                existing_chunk_docs = await session.execute(
                    select(ChunkRow.document_id)
                    .where(ChunkRow.document_id.in_(remapped_candidates))
                    .distinct()
                )
                already_has_chunks = {row[0] for row in existing_chunk_docs.all()}
                for cand_id in remapped_candidates:
                    if cand_id not in already_has_chunks:
                        new_doc_ids.add(cand_id)
        bind_dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
        is_pg = bind_dialect == "postgresql"
        chunk_values: list[dict[str, Any]] = []
        for chunk in chunks:
            chunk_doc_id: uuid.UUID | None = doc_ids.get(chunk.document_url)
            if chunk_doc_id is None:
                try:
                    chunk_doc_id = doc_ids.get(canonicalize(chunk.document_url))
                except Exception:
                    chunk_doc_id = None  # malformed chunk url — skip this chunk
            if chunk_doc_id is None or chunk_doc_id not in new_doc_ids:
                continue
            try:
                chunk_uuid = uuid.UUID(chunk.id)
            except ValueError:
                chunk_uuid = uuid.uuid4()
            chunk_values.append(
                {
                    "id": chunk_uuid,
                    "document_id": chunk_doc_id,
                    "position": chunk.position,
                    "heading_path": chunk.heading_path,
                    "content": chunk.content,
                    "token_count": chunk.word_count,
                    # docs/20 P1-4: sparse side of the hybrid index — real
                    # tsvector on Postgres, raw text on SQLite (scored in
                    # Python by retrieval/hybrid).
                    "tsv": func.to_tsvector("simple", chunk.content) if is_pg else chunk.content,
                    "embedding": embeddings_by_doc.get(chunk_doc_id, {}).get(chunk.position),
                }
            )
        if chunk_values:
            if is_pg:
                chunk_stmt = insert(ChunkRow).on_conflict_do_nothing(index_elements=["id"])
                for row_values in chunk_values:
                    await session.execute(chunk_stmt.values(**row_values))
            else:
                await session.execute(
                    insert(ChunkRow).on_conflict_do_nothing(index_elements=["id"]),
                    chunk_values,
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
    async with sqlite_write_lock(), get_sessionmaker()() as session:
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
    # Audit Pass 2 P1: snapshot the owner HERE (producer context) — the persist
    # queue consumer runs in the startup context where current_api_key_id is
    # None, silently nulling attribution for every keyed request.
    owner = current_api_key_id.get()
    _spawn_task(
        "search",
        _persist_search_all(query_id, raw_text, normalized_text, lang, params, results, owner),
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
            status=status,
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
    owner: str | None = None,
) -> None:
    """Persist the research report as an Answer(kind="research") row."""
    answer_id = uuid.uuid4()
    async with sqlite_write_lock(), get_sessionmaker()() as session:
        insert = _dialect_insert(session)
        await session.execute(
            insert(Query)
            .values(
                id=research_id,
                raw_text=question,
                normalized_text=question,
                lang=lang,
                params={"kind": "research", "truncated": truncated},
                api_key_id=owner if owner is not None else current_api_key_id.get(),
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.flush()
        existing_chunks = await _resolve_valid_chunk_ids(session, citations)
        session.add(
            Answer(
                id=answer_id,
                api_key_id=owner if owner is not None else current_api_key_id.get(),
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
    owner = current_api_key_id.get()  # producer-context snapshot (Pass 2 P1)
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
            owner,
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
