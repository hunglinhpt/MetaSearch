"""Deep Research pipeline (docs/04-agents-orchestration.md).

Planner (LLM, JSON sub-queries) -> Executor (pure async: search + scrape +
rerank into a ContextPack) -> Extractor (LLM: learnings + gaps) -> loop within
budget -> Synthesizer (streaming report with [n] citations).

Harness²-inspired adaptive scaffolding (Google Research pattern):
Dynamic domain profiles (developer, academic, comparative, financial, news,
fengshui, health, general) tailor planning directives, search categories,
extraction depth, and report structure.

Every stage emits events consumed by both the SSE endpoint and the MCP layer:
plan, iteration_start, engine_done, learnings, report_token, done.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.config import Settings
from app.core.logging import get_logger
from app.core.offload import run_cpu
from app.extraction.chunker import Chunk, chunk_markdown
from app.extraction.scraper import PageContent, Scraper
from app.llm.citations import validate_citations
from app.llm.gateway import LLMError, LLMGateway, Usage
from app.llm.prompts import SYSTEM_PROMPT
from app.llm.providers import LLMOverride
from app.orchestration import persist
from app.orchestration.pipeline import (
    Event,
    OnEvent,
    SearchParams,
    SearchPipeline,
    normalize_query,
)
from app.retrieval.canonical import canonicalize
from app.retrieval.models import ScoredResult
from app.retrieval.scoring import heuristic_chunk_score, tokenize

logger = get_logger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_MAX_SUB_QUERIES = 5
_EXTRACTOR_MAX_CHARS = 12_000
_MAX_CHUNKS_PER_DOC_IN_MERGE = 3
_QUANT_ADAPTERS = frozenset({"viet_stock", "stooq", "yahoo_finance", "sec_edgar", "fengshui"})


def _reformulate_query(
    question: str, gap: str, learnings: list[Learning], *, max_facts: int = 3
) -> str:
    """docs/20 P1-1 (IRCoT): reformulate the next hop from the evidence already
    gathered — the gap plus the established facts, so the search is conditioned
    on what we know instead of re-asking the same thing (entity-linking hop)."""
    facts = " ".join(lr.fact for lr in learnings[-max_facts:]).strip()
    if facts:
        return f"{question} — {gap}. Given what is already established ({facts[:500]}), find:"
    return f"{question} — {gap}"


def _plan_coverage(
    plan_queries: list[tuple[str, str]], learnings: list[Learning], *, threshold: float = 0.25
) -> float:
    """docs/20 P1-1 coverage gate: fraction of planner sub-questions whose key
    terms are already reflected in the extracted learnings (lexical proxy for
    "answered"). 1.0 when there is nothing to cover."""
    if not plan_queries:
        return 1.0
    facts_text = " ".join(lr.fact.lower() for lr in learnings)
    covered = 0
    for query, purpose in plan_queries:
        terms = [t for t in tokenize(f"{query} {purpose}") if len(t) > 3]
        if not terms:
            # Pass 1 P2: acronym-only sub-queries ("P/E P/B ROE EPS") have no
            # checkable terms — skipping them as "covered" could report 1.0
            # coverage on ZERO evidence. The threshold math below decides.
            continue
        hits = sum(1 for t in terms if t in facts_text)
        if hits / len(terms) >= threshold:
            covered += 1
    return covered / len(plan_queries)


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
    _seen_chunk_keys: set[tuple[str, int]] = field(default_factory=set)

    def merge(self, chunks: list[Chunk], sources: list[dict]) -> list[Chunk]:
        """Dedup sources by URL; accept up to 3 chunks per new document URL in a pass,
        then mark those document URLs seen so subsequent iterations do not re-ingest them."""
        for source in sources:
            if source["url"] not in self._seen_urls:
                self._seen_urls.add(source["url"])
                self.sources.append(source)
        already_seen_urls = set(self._seen_chunk_urls)
        newly_seen_urls: set[str] = set()
        per_doc_count: Counter[str] = Counter()
        accepted: list[Chunk] = []
        for chunk in chunks:
            if chunk.document_url in already_seen_urls:
                continue
            key = (chunk.document_url, chunk.position)
            if key in self._seen_chunk_keys:
                continue
            if per_doc_count[chunk.document_url] >= _MAX_CHUNKS_PER_DOC_IN_MERGE:
                continue
            self._seen_chunk_keys.add(key)
            per_doc_count[chunk.document_url] += 1
            newly_seen_urls.add(chunk.document_url)
            self.chunks.append(chunk)
            accepted.append(chunk)
        self._seen_chunk_urls.update(newly_seen_urls)
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
            "Decompose into rigorous institutional financial, forensic & technical sub-queries: "
            "(1) business plans vs actual results (revenue, net profit, % plan achieved, YoY), "
            "(2) balance sheet forensics & quality of earnings (Altman Z-Score, Beneish M-Score, "
            "Piotroski F-Score, CFO/Net Income, DSO, ICR, or Banking CAMELS/NIM/CASA/NPL/LLR), "
            "(3) quantitative technical analysis (Fibonacci Retracement & Golden Pocket 61.8%-65%, "
            "Ichimoku Cloud, MACD/RSI/ATR/Bollinger, Linear Regression Trend & Chart Patterns), "
            "(4) valuation multiples (P/E, P/B, EPS, ROE, ROA) & dividend policy (cash/stock), "
            "(5) shareholder structure, foreign room, insider/block trades & regulatory filings. "
            "Categories must include 'financial', 'general' and 'news'."
        ),
        extractor_instructions=(
            "Extract concrete financial metrics (revenue, profit, P/E, ROE), forensic scores "
            "(Altman Z, Beneish M, Piotroski F, CFO/NI, DSO, ICR, NPL, NIM, CASA), technical "
            "levels (Fibonacci 38.2%/50%/61.8%, Ichimoku Tenkan/Kijun/Kumo, RSI, MACD, Regression "
            "Slope), dividend ex-dates, and block/insider trades with full citations."
        ),
        report_structure=(
            "Structure the report: Executive Summary & Corporate Profile -> "
            "Business Plans vs Actual Financial Performance -> "
            "Institutional Financial Forensics & Balance Sheet Quality (Altman Z / Beneish M / "
            "Piotroski F / CAMELS / Debt & Cashflow Structure) -> "
            "Quantitative Technical Analysis (Fibonacci, Ichimoku Cloud, Indicators & Patterns) -> "
            "Dividends, Capital Restructuring & Insider/Block Trades -> "
            "Audited Filings, Risks & Strategic Investment Outlook -> References."
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
    if requested != "auto" and requested in HARNESS_PROFILES:
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
        "kế hoạch kinh doanh",
        "kết quả kinh doanh",
        "bctc",
        "cổ phần",
        "cơ cấu cổ đông",
        "cổ đông lớn",
        "trao tay",
        "thỏa thuận",
        "giao dịch nội bộ",
        "tăng vốn",
        "esop",
        "tin fa",
        "nghị quyết hđqt",
        "đại hội cổ đông",
        "giải trình",
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
        "fibonacci",
        "fibo",
        "ichimoku",
        "kumo",
        "tenkan",
        "kijun",
        "rsi",
        "macd",
        "bollinger",
        "atr",
        "stochastic",
        "altman",
        "piotroski",
        "beneish",
        "camels",
        "nim",
        "casa",
        "npl",
        "dso",
        "icr",
        "pháp y",
        "mẫu hình",
        "hỗ trợ",
        "kháng cự",
        "trendline",
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
    profile: str = "auto"  # auto | general | developer | academic | comparative | financial | ...
    research_id: uuid.UUID | None = None
    llm_override: LLMOverride | None = None


def _chunks_sync(
    question: str,
    results: list[ScoredResult],
    pages: list[PageContent],
    *,
    offset: int,
) -> list[Chunk]:
    scored: list[tuple[float, Chunk]] = []
    res_by_url: dict[str, ScoredResult] = {}
    for r in results:
        res_by_url[r.url] = r
        res_by_url.setdefault(r.canonical_url, r)

    scraped_urls: set[str] = set()
    for page in pages:
        scraped_urls.add(page.url)
        scraped_urls.add(page.final_url)
        scraped_urls.add(canonicalize(page.url))
        matched_res = res_by_url.get(page.url) or res_by_url.get(canonicalize(page.url))
        result_score = matched_res.score if matched_res else 0.0
        md_body = page.markdown
        if (
            matched_res is not None
            and matched_res.snippet
            and any(a in _QUANT_ADAPTERS for a in matched_res.adapters)
            and matched_res.snippet[:80] not in md_body
        ):
            md_body = f"{matched_res.snippet}\n\n{md_body}"
        page_chunks = chunk_markdown(md_body, document_url=page.url, source_title=page.title)
        for chunk in page_chunks:
            # Score using within-document position BEFORE adding offset
            score = heuristic_chunk_score(question, chunk.content, result_score, chunk.position)
            chunk.position += offset
            scored.append((score, chunk))

    for r in results:
        if r.url in scraped_urls or r.canonical_url in scraped_urls:
            continue
        snippet_text = (r.snippet or "").strip()
        if not snippet_text:
            continue
        fallback_md = f"# {r.title}\n\n{snippet_text}" if r.title else snippet_text
        fallback_chunks = chunk_markdown(
            fallback_md, document_url=r.url, source_title=r.title or r.url
        )
        for chunk in fallback_chunks:
            score = heuristic_chunk_score(question, chunk.content, r.score, chunk.position)
            chunk.position += offset
            scored.append((score, chunk))

    scored.sort(key=lambda pair: -pair[0])
    return [chunk for _, chunk in scored]


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
        token_budget = params.max_tokens or self.settings.research_max_tokens
        question = normalize_query(params.query)
        breadth = max(2, min(params.breadth, _MAX_SUB_QUERIES))
        depth = max(1, min(params.depth, self.settings.research_max_iterations))
        usage_total = Usage()

        gateway = (
            self.gateway.with_override(params.llm_override)
            if params.llm_override is not None and hasattr(self.gateway, "with_override")
            else self.gateway
        )
        quick_model = getattr(gateway, "default_quick_model", self.settings.llm_quick_model)
        report_model = getattr(gateway, "default_report_model", self.settings.llm_report_model)
        if params.llm_override and params.llm_override.model:
            quick_model = params.llm_override.model
            report_model = params.llm_override.model

        profile_name = detect_profile(question, params.profile)
        harness = HARNESS_PROFILES.get(profile_name, HARNESS_PROFILES["general"])

        yield (
            "status",
            {"stage": "planning", "detail": f"{question} [profile: {harness.profile}]"},
        )
        sub_queries, categories, plan_usage = await self._plan(
            question, breadth, params.lang, harness, gateway=gateway, model=quick_model
        )
        usage_total.input_tokens += plan_usage.input_tokens
        usage_total.output_tokens += plan_usage.output_tokens
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
        plan_queries = list(sub_queries)  # kept for the P1-1 coverage gate
        coverage = 0.0
        for iteration in range(depth):
            if (
                time.perf_counter() - started > deadline
                or (usage_total.input_tokens + usage_total.output_tokens) >= token_budget
            ):
                truncated = True
                break
            yield ("iteration_start", {"iteration": iteration + 1, "max_iterations": depth})
            if iteration > 0 and pack.gaps:
                if self.settings.research_ircot_reformulate:
                    # docs/20 P1-1 (IRCoT): reformulate each gap hop conditioned
                    # on the learnings extracted so far (entity-linking hop).
                    sub_queries = [
                        (
                            _reformulate_query(question, gap, pack.learnings),
                            "gap follow-up (IRCoT)",
                        )
                        for gap in pack.gaps[:breadth]
                    ]
                else:
                    # Harness² contrastive gap follow-up: probe explicitly on unanswered points
                    sub_queries = [
                        (f"{gap} evidence analysis", "gap follow-up") for gap in pack.gaps[:breadth]
                    ]

            queries = [q for q, _purpose in sub_queries] or [question]
            outcomes = await asyncio.gather(
                *(
                    self.search.run(
                        SearchParams(
                            query=sub_q,
                            lang=params.lang,
                            k=params.k,
                            categories=categories,
                            engines=None,
                            record_memory=False,  # synthetic sub-query, not a user query
                            # Pass 1 P1: IRCoT reformulations are always long ->
                            # the adaptive router would escalate EVERY sub-query
                            # to deep (all-engine fan-out + quorum bypass + LLM
                            # expansion), amplifying a depth-3 x breadth-4 run
                            # into ~12 deep searches. Sub-queries stay shallow
                            # and un-expanded; the planner already targets them.
                            skip_router=True,
                            expansion=False,
                        ),
                        on_event=on_event,
                    )
                    for sub_q in queries
                ),
                return_exceptions=True,
            )
            deduped_results: dict[str, ScoredResult] = {}
            for out in outcomes:
                if isinstance(out, Exception):
                    continue
                for r in out.results:
                    existing = deduped_results.get(r.canonical_url)
                    if existing is None or r.score > existing.score:
                        deduped_results[r.canonical_url] = r

            merged_results = sorted(deduped_results.values(), key=lambda r: -r.score)[
                : params.k * max(len(queries), 1)
            ]
            if not merged_results:
                break

            pages = await self.scraper.scrape_many([r.url for r in merged_results])
            ok_pages = [p for p in pages if isinstance(p, PageContent)]
            chunk_start = pack.next_citation_number()  # last chunk number so far
            new_chunks = await self._chunks(question, merged_results, ok_pages, offset=chunk_start)
            sources = [
                {"url": r.url, "title": r.title, "domain": r.domain, "score": round(r.score, 4)}
                for r in merged_results
            ]
            accepted = pack.merge(new_chunks, sources)
            if ok_pages and accepted:
                persist.persist_pages(ok_pages, accepted)

            learnings, gaps, usage = await self._extract(
                question,
                _render_pack(accepted, start=chunk_start + 1),
                harness,
                gateway=gateway,
                model=quick_model,
            )
            usage_total.input_tokens += usage.input_tokens
            usage_total.output_tokens += usage.output_tokens
            pack.learnings.extend(learnings)
            pack.gaps = gaps
            if self.settings.research_coverage_gate_enabled:
                coverage = _plan_coverage(plan_queries, pack.learnings)
            # Yielded only — same duplicate-prevention as the plan event.
            yield (
                "learnings",
                {
                    "learnings": [item.fact for item in learnings],
                    "gaps": gaps,
                    "iteration": iteration + 1,
                    "coverage": round(coverage, 4),
                },
            )
            # docs/20 P1-1 coverage gate: stop only when there are no open gaps
            # AND the planner's sub-questions are covered by the evidence.
            coverage_ok = (
                not self.settings.research_coverage_gate_enabled
                or coverage >= self.settings.research_coverage_threshold
            )
            if not gaps and coverage_ok:
                break
            if not gaps and not coverage_ok:
                # Pass 1 P2: no new gaps but the coverage gate is unmet — the
                # identical plan re-run would find nothing new (duplicated
                # facts add no terms). Re-queue the UNCOVERED sub-queries so
                # the next iteration actually probes the missing evidence.
                # Per-query lexical overlap uses the _plan_coverage default
                # (0.25); the aggregate gate threshold (0.6) is a DIFFERENT
                # metric — passing it here re-queued nearly every query
                # (audit Pass 2 P2).
                plan_q = [q for q in plan_queries if _plan_coverage([q], pack.learnings) < 0.25]
                if plan_q:
                    sub_queries = plan_q
                # else: nothing left to probe — accept and let the report note it

        if not pack.chunks:
            # Pass 1 P2: zero grounded chunks — an ungrounded report would be
            # hallucination-prone. Fail TYPED: no_results when the searches
            # genuinely found nothing; budget_truncated when the loop stopped
            # before any search could run (Pass 2 P2#9).
            if truncated:
                yield (
                    "error",
                    {
                        "code": "invalid_request",
                        "message": (
                            "Nghiên cứu dừng do hết ngân sách/thời gian trước khi thu thập nguồn"
                        ),
                    },
                )
            else:
                yield (
                    "error",
                    {
                        "code": "no_results",
                        "message": "Nghiên cứu thất bại: không thu thập được nguồn nào",
                    },
                )
            return
        yield ("status", {"stage": "writing_report", "detail": f"{len(pack.chunks)} chunks"})
        report_id = params.research_id or uuid.uuid4()
        answer_parts: list[str] = []
        try:
            included = pack.chunks[:60]
            messages = _report_messages(
                question, pack.learnings, _render_pack(included), harness=harness
            )
            report_truncated = False
            async for delta in gateway.stream(
                messages,
                model=report_model,
                max_tokens=self.settings.research_report_max_tokens,
            ):
                if delta.text:
                    answer_parts.append(delta.text)
                    yield ("report_token", {"t": delta.text})
                if delta.finish_reason == "length":
                    report_truncated = True  # DAT-07: cut-off is not "complete"
                if delta.usage:
                    usage_total.input_tokens += delta.usage.input_tokens
                    usage_total.output_tokens += delta.usage.output_tokens
        except LLMError as exc:
            logger.warning("research_report_llm_failed", error=str(exc)[:200])
            yield ("error", {"code": "internal", "message": str(exc)[:200]})
            return
        record_usage_safe(report_model, usage_total)

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
                "status": "truncated" if (truncated or report_truncated) else "complete",
                "truncated": truncated or report_truncated,
                "iterations": depth if (truncated or report_truncated) else None,
                "coverage": round(coverage, 4),
                "model": report_model,
                "profile": harness.profile,
                "usage": {
                    "input_tokens": usage_total.input_tokens,
                    "output_tokens": usage_total.output_tokens,
                    "cost_usd": round(usage_total.cost_usd(report_model), 6),
                },
            },
        )

        persist.persist_report(
            research_id=report_id,
            question=params.query,
            lang=params.lang,
            content_md=check.text,
            model=report_model,
            input_tokens=usage_total.input_tokens,
            output_tokens=usage_total.output_tokens,
            cost_usd=usage_total.cost_usd(report_model),
            latency_ms=latency_ms,
            citations=[(c.n, c.url, c.title, c.chunk_id) for c in check.citations],
            truncated=truncated or report_truncated,
        )

    # -- stages --------------------------------------------------------------

    async def _plan(
        self,
        question: str,
        breadth: int,
        lang: str | None,
        harness: ResearchHarness | None = None,
        *,
        gateway: LLMGateway | None = None,
        model: str | None = None,
    ) -> tuple[list[tuple[str, str]], list[str], Usage]:
        """Planner LLM call with domain-adaptive harness; heuristic fallback."""
        h = harness or HARNESS_PROFILES["general"]
        gw = gateway or self.gateway
        eff_model = model or self.settings.llm_quick_model
        try:
            text, usage = await gw.complete(
                _planner_messages(question, breadth, lang, harness=h),
                model=eff_model,
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
                return sub_queries, categories, usage
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
            Usage(),
        )

    async def _extract(
        self,
        question: str,
        chunk_text: str,
        harness: ResearchHarness | None = None,
        *,
        gateway: LLMGateway | None = None,
        model: str | None = None,
    ) -> tuple[list[Learning], list[str], Usage]:
        capped = chunk_text[:_EXTRACTOR_MAX_CHARS]
        gw = gateway or self.gateway
        eff_model = model or self.settings.llm_quick_model
        try:
            text, usage = await gw.complete(
                _extractor_messages(question, capped, harness=harness),
                model=eff_model,
                task="extractor",
            )
            parsed = _extract_json(text)
            valid_ids = {int(m.group(1)) for m in re.finditer(r"\[(\d+)\]", chunk_text)}
            learnings = [
                Learning(fact=str(item["fact"]), citation_id=int(item.get("citation_id") or 0))
                for item in parsed.get("learnings", [])
                if item.get("fact") and int(item.get("citation_id") or 0) in valid_ids
            ]
            gaps = [
                str(g)
                for g in parsed.get("gaps", [])
                if g and str(g).strip()  # whitespace-only gaps produce junk reformulations
            ][:8]
            return learnings, gaps, usage
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            logger.warning("research_extract_fallback", error=type(exc).__name__)
            return [], [], Usage()

    async def _chunks(
        self,
        question: str,
        results: list[ScoredResult],
        pages: list[PageContent],
        *,
        offset: int,
    ) -> list[Chunk]:
        return await run_cpu(_chunks_sync, question, results, pages, offset=offset)


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
