"""Search + answer pipelines (docs/03-search-pipeline.md, 11 stages).

The answer pipeline is a single async generator of ``(event, data)`` tuples so
/v1/answer can serve both JSON (consume + reduce) and SSE (forward) from the
same code path. One slow engine never fails the request: partial results win.
"""

from __future__ import annotations

import asyncio
import time
import unicodedata
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from app.config import Settings
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.core.metrics import ENGINE_CALLS, ENGINE_LATENCY
from app.extraction.chunker import Chunk, chunk_markdown
from app.extraction.scraper import PageContent, Scraper
from app.llm.citations import validate_citations
from app.llm.gateway import LLMError, LLMGateway, Usage, record_usage
from app.llm.prompts import build_context, build_messages
from app.orchestration import persist
from app.retrieval.adapters.base import SearchAdapter
from app.retrieval.merger import diversity_cap, merge
from app.retrieval.models import EngineConfig, EngineStatus, RawResult, ScoredResult
from app.retrieval.registry import EngineRegistry
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


def normalize_query(raw: str) -> str:
    """NFC normalize, trim, collapse inner whitespace."""
    normalized = unicodedata.normalize("NFC", raw)
    return " ".join(normalized.split())


class SearchPipeline:
    """Fan-out -> merge -> score -> diversity cap (stages 5-7)."""

    def __init__(self, settings: Settings, registry: EngineRegistry) -> None:
        self.settings = settings
        self.registry = registry

    async def run(self, params: SearchParams, on_event: OnEvent = None) -> SearchOutcome:
        started = time.perf_counter()
        query_id = uuid.uuid4()
        query = normalize_query(params.query)

        raws, statuses = await self._fanout(query, params, on_event)
        results = merge(raws, query=query, registry=self.registry.configs(), news=params.news)
        results = diversity_cap(results, max_per_domain=2, limit=params.k)

        latency_ms = int((time.perf_counter() - started) * 1000)
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

    async def _fanout(
        self, query: str, params: SearchParams, on_event: OnEvent
    ) -> tuple[list[RawResult], list[EngineStatus]]:
        candidates = self.registry.enabled()
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
                if on_event:
                    on_event(("engine_done", {"engine": cfg.id, "results": 0, "ms": ms}))
                return []
            ms = int((time.perf_counter() - started) * 1000)
            statuses.append(EngineStatus(engine=cfg.id, status="ok", results=len(results), ms=ms))
            ENGINE_CALLS.labels(engine=cfg.id, outcome="ok").inc()
            ENGINE_LATENCY.labels(engine=cfg.id).observe(ms / 1000)
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
                task.cancel()
            # Let cancellations settle so no task leaks past the request.
            await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            exc = task.exception()
            if exc is None:
                raws.extend(task.result())
        return raws, statuses


class AnswerPipeline:
    """Search -> scrape -> chunk -> heuristic rerank -> LLM synthesize (stages 8-11)."""

    def __init__(
        self,
        settings: Settings,
        search: SearchPipeline,
        scraper: Scraper,
        gateway: LLMGateway,
    ) -> None:
        self.settings = settings
        self.search = search
        self.scraper = scraper
        self.gateway = gateway

    async def run(
        self, params: SearchParams, *, model: str | None = None, on_event: OnEvent = None
    ) -> AsyncIterator[Event]:
        started = time.perf_counter()
        model = model or self.settings.llm_quick_model

        yield ("status", {"stage": "searching", "detail": params.query})
        outcome = await self.search.run(params, on_event=on_event)
        if not outcome.results:
            raise _no_results(outcome.statuses)

        yield (
            "sources",
            {
                "sources": [
                    {"url": r.url, "title": r.title, "domain": r.domain, "score": round(r.score, 4)}
                    for r in outcome.results
                ]
            },
        )

        yield ("status", {"stage": "scraping", "detail": f"{len(outcome.results)} pages"})
        pages = await self.scraper.scrape_many([r.url for r in outcome.results])
        ok_pages = [p for p in pages if isinstance(p, PageContent)]

        chunks = self._build_chunks(normalize_query(params.query), outcome.results, ok_pages)
        if not chunks:
            raise MetaSearchError(
                ErrorCode.NO_RESULTS,
                "Search returned results but none could be scraped for synthesis",
                details={"engines_status": _status_map(outcome.statuses)},
            )
        yield ("status", {"stage": "synthesizing", "detail": f"{len(chunks)} chunks"})

        included = build_context(chunks, budget_words=self.settings.answer_context_words)
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

        yield (
            "citations",
            {
                "citations": [
                    {"n": c.n, "url": c.url, "title": c.title, "chunk_id": c.chunk_id}
                    for c in check.citations
                ],
                "invalid_markers": check.invalid_markers,
            },
        )
        yield (
            "done",
            {
                "answer_id": str(answer_id),
                "latency_ms": latency_ms,
                "status": "complete",
                "model": model,
            },
        )

        persist.persist_pages(ok_pages, chunks)
        persist.persist_answer(
            answer_id=answer_id,
            query_id=outcome.query_id,
            content_md=check.text,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd(model),
            latency_ms=latency_ms,
            citations=[(c.n, c.url, c.title, c.chunk_id) for c in check.citations],
        )

    def _build_chunks(
        self, query: str, results: list[ScoredResult], pages: list[PageContent]
    ) -> list[Chunk]:
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
        return [chunk for _, chunk in scored_chunks]


def _no_results(statuses: list[EngineStatus]) -> MetaSearchError:
    return MetaSearchError(
        ErrorCode.NO_RESULTS,
        "All engines failed or returned no results",
        details={"engines_status": _status_map(statuses)},
    )


def _status_map(statuses: list[EngineStatus]) -> dict[str, str]:
    return {s.engine: s.status for s in statuses}
