"""Deep Research pipeline (docs/04-agents-orchestration.md).

Planner (LLM, JSON sub-queries) -> Executor (pure async: search + scrape +
rerank into a ContextPack) -> Extractor (LLM: learnings + gaps) -> loop within
budget -> Synthesizer (streaming report with [n] citations).

Harness²-inspired adaptive scaffolding (Google Research pattern):
Dynamic domain profiles (developer, academic, comparative, general) tailor
planning directives, search categories, extraction depth, and report structure.

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
        """Dedup sources by URL; chunks dedup against already-merged chunk URLs."""
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


# -- Harness² domain scaffolding profiles -----------------------------------


@dataclass(slots=True)
class ResearchHarness:
    """Domain scaffolding profile inspired by Harness² (docs/04 §Agents)."""

    profile: str
    default_categories: list[str]
    planner_instructions: str
    extractor_instructions: str
    report_structure: str


HARNESS_PROFILES: dict[str, ResearchHarness] = {
    "developer": ResearchHarness(
        profile="developer",
        default_categories=["developer", "general"],
        planner_instructions=(
            "Decompose into distinct developer-focused sub-queries covering: "
            "(1) root cause / architecture, (2) verified code solutions & API usage, "
            "(3) trade-offs & version pitfalls, (4) practical integration examples. "
            "Categories must include 'developer' and 'general'."
        ),
        extractor_instructions=(
            "Extract concrete technical facts, code syntax, library versions, "
            "and identify technical gaps or unverified edge cases."
        ),
        report_structure=(
            "Structure the report: Executive Summary -> Technical Root Cause / Architecture -> "
            "Verified Solutions & Code Examples -> Performance & Version Pitfalls -> "
            "Best Practices & Conclusion -> References."
        ),
    ),
    "academic": ResearchHarness(
        profile="academic",
        default_categories=["academic", "reference", "general"],
        planner_instructions=(
            "Decompose into rigorous academic sub-queries covering: "
            "(1) foundational theory / original papers, (2) algorithmic formulation, "
            "(3) empirical benchmark numbers & SOTA results, (4) limitations & open questions. "
            "Categories must include 'academic', 'reference', and 'general'."
        ),
        extractor_instructions=(
            "Extract empirical findings, metrics, experimental methodologies, and theoretical gaps."
        ),
        report_structure=(
            "Structure the report: Abstract / Executive Summary -> Theoretical Foundations -> "
            "Empirical Evidence & Benchmark Analysis -> Limitations & Open Challenges -> "
            "Conclusion -> Academic References."
        ),
    ),
    "comparative": ResearchHarness(
        profile="comparative",
        default_categories=["general", "developer", "social"],
        planner_instructions=(
            "Decompose into side-by-side comparative sub-queries covering: "
            "(1) core architectural differences, (2) direct performance & benchmark metrics, "
            "(3) ecosystem, tooling & community adoption, (4) real-world migration trade-offs. "
            "Categories should include 'general', 'developer', or 'social'."
        ),
        extractor_instructions=(
            "Extract side-by-side metric comparisons, pros/cons for each option, "
            "and unanswered comparative points."
        ),
        report_structure=(
            "Structure the report: Executive Summary -> Feature Matrix & Comparison Table -> "
            "Performance & Benchmark Deep Dive -> Ecosystem & Developer Experience -> "
            "Decision Guide & Trade-offs -> References."
        ),
    ),
    "financial": ResearchHarness(
        profile="financial",
        default_categories=["financial", "general", "news"],
        planner_instructions=(
            "Decompose into rigorous financial, equities & market sub-queries covering: "
            "(1) core valuation & price metrics (P/E, P/B, EV/EBITDA, market cap, dividend yield), "
            "(2) historical revenue, margins & cash flow growth (quarterly & annual filings), "
            "(3) regulatory filings & audited disclosures (SEC 10-K/10-Q, debt maturities, risks), "
            "(4) competitive moat & analyst consensus (price targets, market share, catalysts). "
            "Categories must include 'financial' and 'general'."
        ),
        extractor_instructions=(
            "Extract concrete financial figures, stock prices, revenue numbers, operating margins, "
            "valuation multiples, debt levels, and audited disclosures with citations."
        ),
        report_structure=(
            "Structure the report: Executive Summary & Valuation Snapshot -> "
            "Business Overview & Competitive Moat -> "
            "Financial Performance (Revenue, Margins & Cash Flow) -> "
            "Audited Filings & Key Risk Factors (Debt, Regulatory, Macro) -> "
            "Forward Outlook & Analyst Consensus -> References."
        ),
    ),
    "news": ResearchHarness(
        profile="news",
        default_categories=["news", "general"],
        planner_instructions=(
            "Focus on real-time news developments, verifiable timeline of events, primary sources, "
            "and factual consensus. Categories must include 'news' and 'general'."
        ),
        extractor_instructions=(
            "Extract latest news facts, quotes from verified officials, event dates, locations, "
            "and emerging consequences with citations."
        ),
        report_structure=(
            "Structure the report: Executive Summary & Latest Headlines -> "
            "Timeline of Events -> Key Developments & Perspectives -> "
            "Broader Impact (Societal, Economic or Tech) -> References."
        ),
    ),
    "fengshui": ResearchHarness(
        profile="fengshui",
        default_categories=["fengshui", "general"],
        planner_instructions=(
            "Focus on authentic Eastern geomancy, I Ching principles, Bat Trach, Can Chi, and "
            "architectural spatial harmony. Avoid superstitious claims. "
            "Categories must include 'fengshui' and 'general'."
        ),
        extractor_instructions=(
            "Extract Can Chi, Ngu Hanh elements, Bat Trach directions (Sinh Khi, Dien Nien, etc.), "
            "spatial placement rules, and harmony recommendations."
        ),
        report_structure=(
            "Structure the report: Cung Menh & Can Chi Tong Quan -> "
            "Bat Trach & Huong Cat Hung -> Bo Tri Khong Gian (Ban Lam Viec / Nha O) -> "
            "Mau Sac & Yeu To Tuong Sinh -> Khuyen Nghi Khoa Hoc & Can Bang -> References."
        ),
    ),
    "health": ResearchHarness(
        profile="health",
        default_categories=["academic", "news", "general"],
        planner_instructions=(
            "Focus on evidence-based medicine, peer-reviewed clinical research, "
            "and verified health advice. Explicitly include medical disclaimers. "
            "Categories must include 'academic' and 'news'."
        ),
        extractor_instructions=(
            "Extract clinical outcomes, mechanisms of action, evidence levels, "
            "and cautionary notes."
        ),
        report_structure=(
            "Structure the report: Clinical Overview -> Scientific Evidence & Mechanisms -> "
            "Preventive & Lifestyle Recommendations -> Medical Disclaimers & Risks -> References."
        ),
    ),
    "general": ResearchHarness(
        profile="general",
        default_categories=["general"],
        planner_instructions=(
            "Decompose into distinct web-search sub-queries covering: "
            "definitions, quantitative data, opposing views, and real cases. "
            "No duplicates."
        ),
        extractor_instructions=(
            "Extract relevant facts with source citations and note what remains unanswered."
        ),
        report_structure=(
            "Structure the report: Executive Summary -> Key Aspects & Findings -> "
            "Comparative Insights & Trade-offs -> Conclusion & Recommendations -> References."
        ),
    ),
}


def detect_profile(query: str, requested: str = "auto") -> str:
    """Classify the query into a domain scaffolding profile (Harness² pattern)."""
    if requested in ("developer", "academic", "comparative", "financial", "general"):
        return requested
    q = query.lower()
    comp_keywords = (
        "so sánh",
        "versus",
        " vs ",
        "compare",
        "comparison",
        "khác nhau",
        "ưu nhược điểm",
    )
    if any(k in q for k in comp_keywords):
        return "comparative"
    fin_keywords = (
        "cổ phiếu",
        "chứng khoán",
        "báo cáo tài chính",
        "tài chính",
        "cổ tức",
        "thị giá",
        "doanh thu",
        "lợi nhuận",
        "biên lợi nhuận",
        "dòng tiền",
        "vốn hóa",
        "p/e",
        "p/b",
        "eps",
        "ebitda",
        "vn-index",
        "vnindex",
        "hose",
        "hnx",
        "upcom",
        "stock",
        "stocks",
        "equity",
        "equities",
        "valuation",
        "earnings",
        "dividend",
        "sec edgar",
        "10-k",
        "10-q",
        "8-k",
        "market cap",
        "yahoo finance",
        "trái phiếu",
        "định giá",
    )
    if any(k in q for k in fin_keywords) or re.search(r"\$[a-z]{1,6}\b", q):
        return "financial"

    fengshui_keywords = (
        "phong thủy",
        "bát trạch",
        "can chi",
        "ngũ hành",
        "tử vi",
        "cung mệnh",
        "hướng nhà",
        "bàn làm việc",
        "mệnh kim",
        "mệnh mộc",
        "mệnh thủy",
        "mệnh hỏa",
        "mệnh thổ",
        "sinh khí",
        "thiên y",
        "diên niên",
        "phục vị",
        "tam hợp",
        "tứ hành xung",
        "tuổi làm ăn",
        "hướng xuất hành",
    )
    if any(k in q for k in fengshui_keywords):
        return "fengshui"

    health_keywords = (
        "sức khỏe",
        "bệnh",
        "triệu chứng",
        "thuốc",
        "điều trị",
        "dinh dưỡng",
        "ung thư",
        "tiểu đường",
        "huyết áp",
        "symptom",
        "treatment",
        "clinical",
        "disease",
        "therapy",
        "vaccine",
        "y học",
    )
    if any(k in q for k in health_keywords):
        return "health"

    news_keywords = (
        "tin tức",
        "thời sự",
        "hôm nay",
        "mới nhất",
        "breaking news",
        "sự kiện",
        "vừa qua",
        "bản tin",
    )
    if any(k in q for k in news_keywords):
        return "news"
    dev_keywords = (
        "python",
        "code",
        "bug",
        "error",
        "exception",
        "fastapi",
        "docker",
        "npm",
        "typescript",
        "javascript",
        "sql",
        "git",
        "linux",
        "function",
        "class",
        "react",
        "asyncio",
        "postgres",
        "mysql",
        "redis",
        "kubernetes",
        "c++",
        "rust",
        "github",
        "repo",
        "huggingface",
    )
    if any(k in q for k in dev_keywords):
        return "developer"
    academic_keywords = (
        "arxiv",
        "paper",
        "theorem",
        "hypothesis",
        "dataset",
        "deep learning",
        "transformer",
        "neural",
        "empirical",
        "pubmed",
        "ncbi",
        "clinical",
        "biology",
        "biomedical",
        "citation",
    )
    if any(k in q for k in academic_keywords):
        return "academic"
    return "general"


# -- prompts ---------------------------------------------------------------


def _planner_messages(
    question: str,
    breadth: int,
    lang: str | None,
    harness: ResearchHarness | None = None,
) -> list[dict[str, str]]:
    h = harness or HARNESS_PROFILES["general"]
    cat_json = json.dumps(h.default_categories)
    return [
        {
            "role": "system",
            "content": (
                f"You are a research planner for the {h.profile} domain. "
                f"{h.planner_instructions} "
                f"Decompose the question into {breadth} distinct web-search sub-queries. "
                "Respond with STRICT JSON only: "
                '{"sub_queries": [{"query": "...", "purpose": "..."}], '
                f'"categories": {cat_json}}}'
            ),
        },
        {"role": "user", "content": f"Question: {question}"},
    ]


def _extractor_messages(
    question: str,
    chunk_text: str,
    harness: ResearchHarness | None = None,
) -> list[dict[str, str]]:
    h = harness or HARNESS_PROFILES["general"]
    return [
        {
            "role": "system",
            "content": (
                f"You are a research extractor ({h.profile} profile). "
                f"{h.extractor_instructions} "
                "From the numbered sources below, list facts relevant to the question "
                "and what remains unanswered. STRICT JSON only: "
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
    question: str,
    learnings: list[Learning],
    context_text: str,
    harness: ResearchHarness | None = None,
) -> list[dict[str, str]]:
    h = harness or HARNESS_PROFILES["general"]
    learnings_text = "\n".join(f"- {item.fact} [{item.citation_id}]" for item in learnings) or "-"
    return [
        {
            "role": "system",
            "content": (
                f"{SYSTEM_PROMPT}\n{h.report_structure} Be thorough and cite [n] for every claim."
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
    profile: str = "auto"  # auto | general | developer | academic | comparative


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

        profile_name = detect_profile(question, params.profile)
        harness = HARNESS_PROFILES.get(profile_name, HARNESS_PROFILES["general"])

        yield (
            "status",
            {"stage": "planning", "detail": f"{question} [profile: {harness.profile}]"},
        )
        sub_queries, categories = await self._plan(question, breadth, params.lang, harness)
        # Yielded only (no on_event): run_job forwards generator events itself
        yield (
            "plan",
            {
                "profile": harness.profile,
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
                # Harness² contrastive gap follow-up: probe explicitly on unanswered points
                sub_queries = [
                    (f"{gap} evidence analysis", "gap follow-up") for gap in pack.gaps[:breadth]
                ]

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
                question, _render_pack(accepted, start=chunk_start + 1), harness
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
        try:
            included = pack.chunks[:60]
            messages = _report_messages(
                question, pack.learnings, _render_pack(included), harness=harness
            )
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
                "profile": harness.profile,
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
        self,
        question: str,
        breadth: int,
        lang: str | None,
        harness: ResearchHarness | None = None,
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Planner LLM call with domain-adaptive harness; heuristic fallback."""
        h = harness or HARNESS_PROFILES["general"]
        try:
            text, _usage = await self.gateway.complete(
                _planner_messages(question, breadth, lang, harness=h),
                model=self.settings.llm_quick_model,
                task="planner",
            )
            parsed = _extract_json(text)
            sub_queries = [
                (str(item["query"]), str(item.get("purpose") or ""))
                for item in parsed.get("sub_queries", [])
                if item.get("query")
            ][:_MAX_SUB_QUERIES]
            categories = [str(c) for c in parsed.get("categories", []) if c] or (
                h.default_categories
            )
            if sub_queries:
                return sub_queries, categories
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            logger.warning("research_planner_fallback", error=type(exc).__name__)

        # Heuristic fallback: domain-aware aspects
        if h.profile == "general":
            aspects = [
                "overview and definition",
                "quantitative data and statistics",
                "opposing viewpoints and critique",
                "real-world examples and case studies",
            ]
        else:
            aspects = [
                f"core overview and concepts ({h.profile})",
                "quantitative metrics and benchmarks",
                "critical trade-offs and edge cases",
                "practical real-world applications",
            ]
        return (
            [(f"{question} — {aspect}", aspect) for aspect in aspects[:breadth]],
            h.default_categories,
        )

    async def _extract(
        self,
        question: str,
        chunk_text: str,
        harness: ResearchHarness | None = None,
    ) -> tuple[list[Learning], list[str], Usage]:
        capped = chunk_text[:_EXTRACTOR_MAX_CHARS]
        try:
            text, usage = await self.gateway.complete(
                _extractor_messages(question, capped, harness=harness),
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
