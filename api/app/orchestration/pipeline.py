"""Search + answer pipelines (docs/03-search-pipeline.md, 11 stages).

The answer pipeline is a single async generator of ``(event, data)`` tuples so
/v1/answer can serve both JSON (consume + reduce) and SSE (forward) from the
same code path. One slow engine never fails the request: partial results win.

Phase 2 adds: L1/L2 cache lookups, per-engine health recording, the rerank
stage and cached-answer replay.
"""

from __future__ import annotations

import asyncio
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.cache import (
    CacheClient,
    answer_cache_key,
    deserialize_results,
    search_cache_key,
    search_ttl_s,
    serialize_results,
)
from app.config import Settings
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.core.metrics import ENGINE_CALLS, ENGINE_LATENCY
from app.db.base import EMBEDDING_DIM
from app.db.models import Answer, SemanticCache
from app.db.session import get_sessionmaker
from app.extraction.chunker import Chunk, chunk_markdown
from app.extraction.scraper import PageContent, Scraper
from app.llm.citations import validate_citations
from app.llm.embeddings import EmbeddingClient, EmbeddingError
from app.llm.gateway import LLMError, LLMGateway, Usage, record_usage
from app.llm.prompts import build_context, build_messages
from app.llm.tokens import make_counter
from app.orchestration import persist
from app.retrieval.adapters.base import SearchAdapter
from app.retrieval.merger import diversity_cap, merge
from app.retrieval.models import EngineConfig, EngineStatus, RawResult, ScoredResult
from app.retrieval.reranker import Reranker, RerankerUnavailable, heuristic_order
from app.retrieval.scoring import heuristic_chunk_score

logger = get_logger(__name__)

Event = tuple[str, dict]
OnEvent = Callable[[Event], None] | None


@dataclass(slots=True)
class SearchParams:
    query: str
    lang: str | None = None
    k: int = 10
    engines: list[str] | None = None
    categories: list[str] = field(default_factory=lambda: ["general"])
    freshness_days: int | None = None
    include_snippets: bool = True
    news: bool = False


@dataclass(slots=True)
class SearchOutcome:
    query_id: uuid.UUID
    results: list[ScoredResult]
    statuses: list[EngineStatus]
    latency_ms: int
    cached: bool = False


def normalize_query(raw: str) -> str:
    """NFC normalize, trim, collapse inner whitespace."""
    normalized = unicodedata.normalize("NFC", raw)
    return " ".join(normalized.split())


class SearchPipeline:
    """Cache check -> fan-out -> merge -> score -> diversity cap (stages 3-7)."""

    def __init__(self, settings: Settings, registry, cache: CacheClient | None = None) -> None:
        self.settings = settings
        self.registry = registry
        self.cache = cache

    async def run(self, params: SearchParams, on_event: OnEvent = None) -> SearchOutcome:
        started = time.perf_counter()
        query_id = uuid.uuid4()
        query = normalize_query(params.query)

        cached_payload = await self._cache_lookup(query, params)
        if cached_payload is not None:
            return SearchOutcome(
                query_id=uuid.UUID(cached_payload["query_id"]),
                results=deserialize_results(cached_payload["results"]),
                statuses=[
                    EngineStatus(
                        engine=s["engine"], status=s["status"], results=s.get("results", 0)
                    )
                    for s in cached_payload["statuses"]
                ],
                latency_ms=int((time.perf_counter() - started) * 1000),
                cached=True,
            )

        raws, statuses = await self._fanout(query, params, on_event)
        configs = await self.registry.configs()
        results = merge(raws, query=query, registry=configs, news=params.news)
        results = diversity_cap(results, max_per_domain=2, limit=params.k)

        latency_ms = int((time.perf_counter() - started) * 1000)
        if results and self.cache is not None:
            await self._cache_store(query, params, query_id, results, statuses)
        persist.persist_search(
            query_id=query_id,
            raw_text=params.query,
            normalized_text=query,
            lang=params.lang,
            params={
                "k": params.k,
                "categories": params.categories,
                "freshness_days": params.freshness_days,
                "engines": params.engines,
            },
            results=results,
        )
        return SearchOutcome(
            query_id=query_id, results=results, statuses=statuses, latency_ms=latency_ms
        )

    async def _cache_lookup(self, query: str, params: SearchParams) -> dict | None:
        if self.cache is None:
            return None
        payload = await self.cache.get_json(search_cache_key(query, params))
        if not payload or not payload.get("results"):
            return None
        return payload

    async def _cache_store(
        self,
        query: str,
        params: SearchParams,
        query_id: uuid.UUID,
        results: list[ScoredResult],
        statuses: list[EngineStatus],
    ) -> None:
        if self.cache is None:
            return
        await self.cache.set_json(
            search_cache_key(query, params),
            {
                "query_id": str(query_id),
                "results": serialize_results(results),
                "statuses": [
                    {"engine": s.engine, "status": s.status, "results": s.results} for s in statuses
                ],
            },
            ttl_s=search_ttl_s(self.settings, news=params.news),
        )

    async def _fanout(
        self, query: str, params: SearchParams, on_event: OnEvent
    ) -> tuple[list[RawResult], list[EngineStatus]]:
        candidates = await self.registry.enabled()
        if params.engines:
            wanted = set(params.engines)
            candidates = [(cfg, ad) for cfg, ad in candidates if cfg.id in wanted]

        if not candidates:
            raise MetaSearchError(
                ErrorCode.NO_RESULTS,
                "No search engine is enabled for this request",
                details={"requested_engines": params.engines or []},
            )

        raws: list[RawResult] = []
        statuses: list[EngineStatus] = []

        async def call_engine(cfg: EngineConfig, adapter: SearchAdapter) -> list[RawResult]:
            started = time.perf_counter()
            try:
                results = await adapter.search(
                    query,
                    lang=params.lang,
                    categories=params.categories,
                    freshness_days=params.freshness_days,
                    max_results=cfg.max_results,
                    timeout_s=cfg.timeout_ms / 1000,
                )
            except Exception as exc:
                ms = int((time.perf_counter() - started) * 1000)
                statuses.append(
                    EngineStatus(engine=cfg.id, status="error", ms=ms, error=str(exc)[:200])
                )
                ENGINE_CALLS.labels(engine=cfg.id, outcome="error").inc()
                logger.info("engine_failed", engine=cfg.id, error=str(exc)[:200])
                await self.registry.store.record_result(cfg.id, ok=False, latency_ms=ms)
                if on_event:
                    on_event(("engine_done", {"engine": cfg.id, "results": 0, "ms": ms}))
                return []
            ms = int((time.perf_counter() - started) * 1000)
            statuses.append(EngineStatus(engine=cfg.id, status="ok", results=len(results), ms=ms))
            ENGINE_CALLS.labels(engine=cfg.id, outcome="ok").inc()
            ENGINE_LATENCY.labels(engine=cfg.id).observe(ms / 1000)
            await self.registry.store.record_result(cfg.id, ok=True, latency_ms=ms)
            if on_event:
                on_event(("engine_done", {"engine": cfg.id, "results": len(results), "ms": ms}))
            return results

        tasks = {
            asyncio.create_task(call_engine(cfg, adapter)): cfg.id for cfg, adapter in candidates
        }
        done, pending = await asyncio.wait(tasks, timeout=self.settings.search_fanout_budget_s)
        if pending:
            for task in pending:
                statuses.append(EngineStatus(engine=tasks[task], status="timeout"))
                ENGINE_CALLS.labels(engine=tasks[task], outcome="timeout").inc()
                await self.registry.store.record_result(tasks[task], ok=False, latency_ms=0)
                task.cancel()
            # Let cancellations settle so no task leaks past the request.
            await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            exc = task.exception()
            if exc is None:
                raws.extend(task.result())
        return raws, statuses


class AnswerPipeline:
    """Cache -> search -> scrape -> chunk -> rerank -> synthesize (stages 8-11)."""

    def __init__(
        self,
        settings: Settings,
        search: SearchPipeline,
        scraper: Scraper,
        gateway: LLMGateway,
        reranker: Reranker,
        cache: CacheClient | None = None,
        embedder: EmbeddingClient | None = None,
    ) -> None:
        self.settings = settings
        self.search = search
        self.scraper = scraper
        self.gateway = gateway
        self.reranker = reranker
        self.cache = cache
        self.embedder = embedder

    async def run(
        self, params: SearchParams, *, model: str | None = None, on_event: OnEvent = None
    ) -> AsyncIterator[Event]:
        started = time.perf_counter()
        model = model or self.settings.llm_quick_model

        replay = await self._cache_hit(params, model)
        if replay is not None:
            async for event in replay:
                yield event
            return

        yield ("status", {"stage": "searching", "detail": params.query})
        outcome = await self.search.run(params, on_event=on_event)
        if not outcome.results:
            raise _no_results(outcome.statuses)

        sources = [
            {"url": r.url, "title": r.title, "domain": r.domain, "score": round(r.score, 4)}
            for r in outcome.results
        ]
        yield ("sources", {"sources": sources})

        yield ("status", {"stage": "scraping", "detail": f"{len(outcome.results)} pages"})
        pages = await self.scraper.scrape_many([r.url for r in outcome.results])
        ok_pages = [p for p in pages if isinstance(p, PageContent)]

        scored_chunks = self._score_chunks(normalize_query(params.query), outcome.results, ok_pages)
        if not scored_chunks:
            raise MetaSearchError(
                ErrorCode.NO_RESULTS,
                "Search returned results but none could be scraped for synthesis",
                details={"engines_status": _status_map(outcome.statuses)},
            )

        chunks = await self._rerank(normalize_query(params.query), scored_chunks)
        yield ("status", {"stage": "synthesizing", "detail": f"{len(chunks)} chunks"})

        included = build_context(
            chunks,
            budget=self.settings.answer_context_budget,
            count=make_counter(self.settings),
        )
        messages = build_messages(normalize_query(params.query), included)

        answer_id = uuid.uuid4()
        text_parts: list[str] = []
        usage = Usage()
        try:
            async for delta in self.gateway.stream(messages, model=model):
                if delta.text:
                    text_parts.append(delta.text)
                    yield ("token", {"t": delta.text})
                if delta.usage:
                    usage.input_tokens += delta.usage.input_tokens
                    usage.output_tokens += delta.usage.output_tokens
        except LLMError as exc:
            logger.warning("llm_stream_failed", error=str(exc)[:200])
            yield ("error", {"code": "internal", "message": str(exc)[:200]})
            return
        record_usage(model, usage)

        check = validate_citations("".join(text_parts), included)
        latency_ms = int((time.perf_counter() - started) * 1000)
        citations_payload = [
            {"n": c.n, "url": c.url, "title": c.title, "chunk_id": c.chunk_id}
            for c in check.citations
        ]

        yield (
            "citations",
            {"citations": citations_payload, "invalid_markers": check.invalid_markers},
        )
        yield (
            "done",
            {
                "answer_id": str(answer_id),
                "latency_ms": latency_ms,
                "status": "complete",
                "model": model,
                "cached": False,
            },
        )

        persist.persist_pages(ok_pages, chunks)
        if self.cache is not None:
            await self._cache_store(
                params, model, answer_id, check.text, citations_payload, sources
            )
        # DB persist + semantic-cache write run in one background task, in this
        # order: the semantic row references the answers row (FK), and the
        # client must not wait on a slow/unreachable Postgres (done event is
        # already delivered above).
        finalize = asyncio.create_task(
            self._finalize_answer(
                answer_id=answer_id,
                query_id=outcome.query_id,
                content_md=check.text,
                model=model,
                usage=usage,
                latency_ms=latency_ms,
                citations=[(c.n, c.url, c.title, c.chunk_id) for c in check.citations],
                query=normalize_query(params.query),
            )
        )
        finalize.add_done_callback(_log_background)

    async def _finalize_answer(
        self,
        *,
        answer_id: uuid.UUID,
        query_id: uuid.UUID,
        content_md: str,
        model: str,
        usage: Usage,
        latency_ms: int,
        citations: list[tuple[int, str, str, str | None]],
        query: str,
    ) -> None:
        await persist.persist_answer(
            answer_id=answer_id,
            query_id=query_id,
            content_md=content_md,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd(model),
            latency_ms=latency_ms,
            citations=citations,
        )
        if self.embedder is not None and self.embedder.enabled:
            await self._semantic_cache_store(query, answer_id, model)

    # -- rerank ------------------------------------------------------------

    async def _rerank(self, query: str, scored_chunks: list[tuple[float, Chunk]]) -> list[Chunk]:
        try:
            return await self.reranker.rerank(query, scored_chunks)
        except RerankerUnavailable as exc:
            logger.warning("rerank_fallback_heuristic", reason=str(exc)[:120])
            return heuristic_order(scored_chunks)

    def _score_chunks(
        self, query: str, results: list[ScoredResult], pages: list[PageContent]
    ) -> list[tuple[float, Chunk]]:
        scored_chunks: list[tuple[float, Chunk]] = []
        score_of = {r.url: r.score for r in results}
        for page in pages:
            result_score = score_of.get(page.url, 0.0)
            page_chunks = chunk_markdown(
                page.markdown, document_url=page.url, source_title=page.title
            )
            for chunk in page_chunks:
                score = heuristic_chunk_score(query, chunk.content, result_score, chunk.position)
                scored_chunks.append((score, chunk))
        scored_chunks.sort(key=lambda pair: -pair[0])
        return scored_chunks

    # -- answer cache (L1 + L2) ---------------------------------------------

    async def _cache_hit(self, params: SearchParams, model: str) -> AsyncIterator[Event] | None:
        """Return a replay generator when L1 or L2 has a fresh answer."""
        query = normalize_query(params.query)
        if self.cache is not None:
            payload = await self.cache.get_json(
                answer_cache_key(
                    query,
                    model=model,
                    lang=params.lang,
                    k=params.k,
                    categories=params.categories,
                    freshness_days=params.freshness_days,
                )
            )
            if payload and payload.get("answer_md"):
                return self._replay(payload)
        payload = await self._semantic_lookup(query)
        if payload is not None:
            return self._replay(payload)
        return None

    def _replay(self, payload: dict) -> AsyncIterator[Event]:
        async def _gen() -> AsyncIterator[Event]:
            yield ("sources", {"sources": payload.get("sources") or []})
            text = str(payload.get("answer_md") or "")
            for i in range(0, len(text), 24):
                yield ("token", {"t": text[i : i + 24]})
            yield (
                "citations",
                {"citations": payload.get("citations") or [], "invalid_markers": []},
            )
            yield (
                "done",
                {
                    "answer_id": str(payload.get("answer_id") or uuid.uuid4()),
                    "latency_ms": 0,
                    "status": "complete",
                    "model": str(payload.get("model") or ""),
                    "cached": True,
                },
            )

        return _gen()

    async def _cache_store(
        self,
        params: SearchParams,
        model: str,
        answer_id: uuid.UUID,
        answer_md: str,
        citations: list[dict],
        sources: list[dict],
    ) -> None:
        if self.cache is None or not answer_md:
            return
        await self.cache.set_json(
            answer_cache_key(
                normalize_query(params.query),
                model=model,
                lang=params.lang,
                k=params.k,
                categories=params.categories,
                freshness_days=params.freshness_days,
            ),
            {
                "answer_id": str(answer_id),
                "answer_md": answer_md,
                "citations": citations,
                "sources": sources,
                "model": model,
            },
            ttl_s=self.settings.cache_answer_ttl_s,
        )

    async def _semantic_lookup(self, query: str) -> dict | None:
        """L2: pgvector cosine > threshold on the semantic_cache table."""
        if self.embedder is None or not self.embedder.enabled:
            return None
        try:
            vector = await self.embedder.embed(query)
        except EmbeddingError as exc:
            logger.warning("semantic_lookup_skipped", reason=str(exc)[:120])
            return None
        max_distance = 1.0 - self.settings.semantic_cache_threshold
        try:
            async with get_sessionmaker()() as session:
                row = (
                    await session.execute(
                        select(SemanticCache.answer_id, Answer.content_md, Answer.model)
                        .join(Answer, Answer.id == SemanticCache.answer_id)
                        .where(
                            SemanticCache.query_embedding.cosine_distance(vector) <= max_distance,
                            SemanticCache.expires_at > func.now(),
                        )
                        .order_by(SemanticCache.query_embedding.cosine_distance(vector))
                        .limit(1)
                    )
                ).first()
            if row is None or not row[1]:
                return None
        except Exception as exc:
            logger.warning("semantic_lookup_failed", error=type(exc).__name__)
            return None
        answer_id, content_md, model = row
        if self.cache is not None:
            cached = await self.cache.get_json(
                answer_cache_key(query, model=model or "", lang=None)
            )  # L2 replay: original search params unknown, default key shape
            if cached and cached.get("answer_md"):
                return cached
        # No L1 payload for this answer: rebuild citations from the DB so the
        # replayed answer still carries its sources.
        citations = await persist.load_citations(answer_id)
        return {
            "answer_id": str(answer_id),
            "answer_md": content_md,
            "citations": citations,
            "sources": [
                {"url": c["url"], "title": c["title"], "domain": _domain(c["url"])}
                for c in citations
            ],
            "model": model or "",
        }

    async def _semantic_cache_store(self, query: str, answer_id: uuid.UUID, model: str) -> None:
        if self.embedder is None or not self.embedder.enabled:
            return
        try:
            vector = await self.embedder.embed(query)
            if len(vector) != EMBEDDING_DIM:
                return
            async with get_sessionmaker()() as session:
                session.add(
                    SemanticCache(
                        query_text=query,
                        query_embedding=vector,
                        answer_id=answer_id,
                        model=model,
                        expires_at=datetime.now(UTC) + timedelta(days=7),
                    )
                )
                await session.commit()
        except Exception as exc:
            logger.warning("semantic_cache_write_failed", error=type(exc).__name__)


def _log_background(task: asyncio.Task) -> None:
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.warning("background_task_failed", error=type(exc).__name__)


def _no_results(statuses: list[EngineStatus]) -> MetaSearchError:
    return MetaSearchError(
        ErrorCode.NO_RESULTS,
        "All engines failed or returned no results",
        details={"engines_status": _status_map(statuses)},
    )


def _status_map(statuses: list[EngineStatus]) -> dict[str, str]:
    return {s.engine: s.status for s in statuses}


def _domain(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()
