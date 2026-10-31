"""Deep Research pipeline (docs/04-agents-orchestration.md).

Planner (LLM, JSON sub-queries) -> Executor (pure async: search + scrape +
rerank into a ContextPack) -> Extractor (LLM: learnings + gaps) -> loop within
budget -> Synthesizer (streaming report with [n] citations).

Every stage emits events consumed by both the SSE endpoint and the MCP layer:
plan, iteration_start, engine_done, learnings, report_token, done.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.config import Settings
from app.core.logging import get_logger
from app.extraction.chunker import Chunk
from app.extraction.scraper import PageContent, Scraper
from app.llm.citations import validate_citations
from app.llm.gateway import LLMError, LLMGateway, Usage
from app.llm.prompts import SYSTEM_PROMPT
from app.orchestration import persist
from app.orchestration.pipeline import (
    Event,
    OnEvent,
    SearchParams,
    SearchPipeline,
    normalize_query,
)
from app.retrieval.scoring import heuristic_chunk_score

logger = get_logger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_MAX_SUB_QUERIES = 5
_EXTRACTOR_MAX_CHARS = 12_000


@dataclass(slots=True)
class Learning:
    fact: str
    citation_id: int  # chunk number in the merged ContextPack


@dataclass(slots=True)
class ContextPack:
    """Merged research context: reranked chunks + sources + learnings."""

    chunks: list[Chunk] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    learnings: list[Learning] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    _seen_urls: set[str] = field(default_factory=set)
    _seen_chunk_urls: set[str] = field(default_factory=set)

    def merge(self, chunks: list[Chunk], sources: list[dict]) -> list[Chunk]:
        """Dedup sources by URL; chunks dedup against already-merged chunk URLs.

        Returns the accepted chunks so the caller can number citations
        consistently even when duplicates are dropped.
        """
        for source in sources:
            if source["url"] not in self._seen_urls:
                self._seen_urls.add(source["url"])
                self.sources.append(source)
        accepted: list[Chunk] = []
        for chunk in chunks:
            if chunk.document_url in self._seen_chunk_urls:
                continue
            self._seen_chunk_urls.add(chunk.document_url)
            self.chunks.append(chunk)
            accepted.append(chunk)
        return accepted

    def next_citation_number(self) -> int:
        return len(self.chunks)


# -- prompts ---------------------------------------------------------------


def _planner_messages(question: str, breadth: int, lang: str | None) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a research planner. Decompose the question into "
                f"{breadth} distinct web-search sub-queries covering different "
                "aspects: definitions, quantitative data, opposing views, real "
                "cases. No duplicates. Respond with STRICT JSON only: "
                '{"sub_queries": [{"query": "...", "purpose": "..."}], '
                '"categories": ["general"]} '
                "(categories optionally include 'academic' or 'reference')."
            ),
        },
        {"role": "user", "content": f"Question: {question}"},
    ]


def _extractor_messages(question: str, chunk_text: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a research extractor. From the numbered sources below, "
                "list facts relevant to the question and what remains "
                "unanswered. STRICT JSON only: "
                '{"learnings": [{"fact": "...", "citation_id": <number>}], '
                '"gaps": ["..."]}. citation_id must be one of the source numbers.'
            ),
        },
        {
            "role": "user",
            "content": f"Question: {question}\n\nSources:\n\n{chunk_text}",
        },
    ]


def _render_pack(chunks: list[Chunk], start: int = 1) -> str:
    blocks = []
    for offset, chunk in enumerate(chunks):
        blocks.append(f"[{start + offset}] {chunk.document_url}\n{chunk.content}")
    return "\n\n".join(blocks)


def _report_messages(
    question: str, learnings: list[Learning], context_text: str
) -> list[dict[str, str]]:
    learnings_text = "\n".join(f"- {item.fact} [{item.citation_id}]" for item in learnings) or "-"
    return [
        {
            "role": "system",
            "content": (
                SYSTEM_PROMPT + "\nStructure the report: Executive summary -> Analysis per "
                "aspect -> Comparison -> Conclusion -> References. Be thorough."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\nKnown learnings:\n{learnings_text}\n\n"
                f"Sources:\n\n{context_text}\n\nWrite the report."
            ),
        },
    ]


# -- pipeline ----------------------------------------------------------------


@dataclass(slots=True)
class ResearchParams:
    query: str
    lang: str | None = None
    breadth: int = 4  # sub-queries per plan
    depth: int = 3  # max iterations
    max_time_s: int | None = None
    max_tokens: int | None = None
    k: int = 6  # results per sub-query


class ResearchPipeline:
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
        self, params: ResearchParams, *, on_event: OnEvent = None
    ) -> AsyncIterator[Event]:
        started = time.perf_counter()
        deadline = params.max_time_s or self.settings.research_max_time_s
        question = normalize_query(params.query)
        breadth = max(2, min(params.breadth, _MAX_SUB_QUERIES))
        depth = max(1, min(params.depth, self.settings.research_max_iterations))
        usage_total = Usage()

        yield ("status", {"stage": "planning", "detail": question})
        sub_queries, categories = await self._plan(question, breadth, params.lang)
        # Yielded only (no on_event): run_job forwards generator events itself,
        # a callback here would duplicate the event in the SSE stream.
        yield (
            "plan",
            {
                "sub_queries": [{"query": q, "purpose": purpose} for q, purpose in sub_queries],
                "categories": categories,
            },
        )

        pack = ContextPack()
        truncated = False
        for iteration in range(depth):
            if time.perf_counter() - started > deadline:
                truncated = True
                break
            yield ("iteration_start", {"iteration": iteration + 1, "max_iterations": depth})
            if iteration > 0 and pack.gaps:
                sub_queries = [(gap, "gap follow-up") for gap in pack.gaps[:breadth]]

            queries = [q for q, _purpose in sub_queries]
            outcome = await self.search.run(
                SearchParams(
                    query=" ".join(queries),
                    lang=params.lang,
                    k=params.k * max(len(queries), 1),
                    categories=categories,
                    engines=None,
                ),
                on_event=on_event,
            )
            if not outcome.results:
                break

            pages = await self.scraper.scrape_many([r.url for r in outcome.results])
            ok_pages = [p for p in pages if isinstance(p, PageContent)]
            chunk_start = pack.next_citation_number()  # last chunk number so far
            new_chunks = self._chunks(question, outcome.results, ok_pages, offset=chunk_start)
            sources = [
                {"url": r.url, "title": r.title, "domain": r.domain, "score": round(r.score, 4)}
                for r in outcome.results
            ]
            accepted = pack.merge(new_chunks, sources)

            learnings, gaps, usage = await self._extract(
                question, _render_pack(accepted, start=chunk_start + 1)
            )
            usage_total.input_tokens += usage.input_tokens
            usage_total.output_tokens += usage.output_tokens
            pack.learnings.extend(learnings)
            pack.gaps = gaps
            # Yielded only — same duplicate-prevention as the plan event.
            yield (
                "learnings",
                {
                    "learnings": [item.fact for item in learnings],
                    "gaps": gaps,
                    "iteration": iteration + 1,
                },
            )
            if not gaps:
                break

        yield ("status", {"stage": "writing_report", "detail": f"{len(pack.chunks)} chunks"})
        report_id = uuid.uuid4()
        answer_parts: list[str] = []
        citations_payload: list[dict] = []
        try:
            included = pack.chunks[:60]
            messages = _report_messages(question, pack.learnings, _render_pack(included))
            async for delta in self.gateway.stream(messages, model=self.settings.llm_report_model):
                if delta.text:
                    answer_parts.append(delta.text)
                    yield ("report_token", {"t": delta.text})
                if delta.usage:
                    usage_total.input_tokens += delta.usage.input_tokens
                    usage_total.output_tokens += delta.usage.output_tokens
        except LLMError as exc:
            logger.warning("research_report_llm_failed", error=str(exc)[:200])
            yield ("error", {"code": "internal", "message": str(exc)[:200]})
            return
        record_usage_safe(self.settings.llm_report_model, usage_total)

        check = validate_citations("".join(answer_parts), list(enumerate(included, start=1)))
        citations_payload = [
            {"n": c.n, "url": c.url, "title": c.title, "chunk_id": c.chunk_id}
            for c in check.citations
        ]
        latency_ms = int((time.perf_counter() - started) * 1000)
        yield (
            "citations",
            {"citations": citations_payload, "invalid_markers": check.invalid_markers},
        )
        yield (
            "done",
            {
                "research_id": str(report_id),
                "latency_ms": latency_ms,
                "status": "complete",
                "truncated": truncated,
                "iterations": depth if truncated else None,
                "model": self.settings.llm_report_model,
                "usage": {
                    "input_tokens": usage_total.input_tokens,
                    "output_tokens": usage_total.output_tokens,
                    "cost_usd": round(usage_total.cost_usd(self.settings.llm_report_model), 6),
                },
            },
        )

        persist.persist_report(
            research_id=report_id,
            question=params.query,
            lang=params.lang,
            content_md=check.text,
            model=self.settings.llm_report_model,
            input_tokens=usage_total.input_tokens,
            output_tokens=usage_total.output_tokens,
            cost_usd=usage_total.cost_usd(self.settings.llm_report_model),
            latency_ms=latency_ms,
            citations=[(c.n, c.url, c.title, c.chunk_id) for c in check.citations],
            truncated=truncated,
        )

    # -- stages --------------------------------------------------------------

    async def _plan(
        self, question: str, breadth: int, lang: str | None
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Planner LLM call; heuristic fallback keeps research alive offline."""
        try:
            text, _usage = await self.gateway.complete(
                _planner_messages(question, breadth, lang),
                model=self.settings.llm_quick_model,
                task="planner",
            )
            parsed = _extract_json(text)
            sub_queries = [
                (str(item["query"]), str(item.get("purpose") or ""))
                for item in parsed.get("sub_queries", [])
                if item.get("query")
            ][:_MAX_SUB_QUERIES]
            categories = [str(c) for c in parsed.get("categories", []) if c] or ["general"]
            if sub_queries:
                return sub_queries, categories
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            logger.warning("research_planner_fallback", error=type(exc).__name__)
        # Heuristic fallback: aspect-templated sub-queries.
        aspects = [
            "overview and definition",
            "data and numbers",
            "criticism and trade-offs",
            "practical examples",
        ]
        return (
            [(f"{question} — {aspect}", aspect) for aspect in aspects[:breadth]],
            ["general"],
        )

    async def _extract(
        self, question: str, chunk_text: str
    ) -> tuple[list[Learning], list[str], Usage]:
        capped = chunk_text[:_EXTRACTOR_MAX_CHARS]
        try:
            text, usage = await self.gateway.complete(
                _extractor_messages(question, capped),
                model=self.settings.llm_quick_model,
                task="extractor",
            )
            parsed = _extract_json(text)
            valid_ids = {int(m.group(1)) for m in re.finditer(r"\[(\d+)\]", chunk_text)}
            learnings = [
                Learning(fact=str(item["fact"]), citation_id=int(item.get("citation_id") or 0))
                for item in parsed.get("learnings", [])
                if item.get("fact") and int(item.get("citation_id") or 0) in valid_ids
            ]
            gaps = [str(g) for g in parsed.get("gaps", []) if g][:8]
            return learnings, gaps, usage
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            logger.warning("research_extract_fallback", error=type(exc).__name__)
            return [], [], Usage()

    def _chunks(
        self,
        question: str,
        results: list,
        pages: list[PageContent],
        *,
        offset: int,
    ) -> list[Chunk]:
        from app.extraction.chunker import chunk_markdown

        scored: list[tuple[float, Chunk]] = []
        score_of = {r.url: r.score for r in results}
        for page in pages:
            page_chunks = chunk_markdown(
                page.markdown, document_url=page.url, source_title=page.title
            )
            for chunk in page_chunks:
                chunk.position += offset
                score = heuristic_chunk_score(
                    question, chunk.content, score_of.get(page.url, 0.0), chunk.position
                )
                scored.append((score, chunk))
        scored.sort(key=lambda pair: -pair[0])
        return [chunk for _, chunk in scored]


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply."""
    match = _JSON_BLOCK.search(text)
    if not match:
        raise ValueError("no JSON object in planner/extractor reply")
    return json.loads(match.group(0))


def record_usage_safe(model: str, usage: Usage) -> None:
    from app.llm.gateway import record_usage

    try:
        record_usage(model, usage)
    except Exception as exc:  # metrics must never break a request
        logger.debug("record_usage_failed", model=model, error=type(exc).__name__)
