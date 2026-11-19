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
import hashlib
import json
import re
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.core.admission import admit
from app.core.auth import current_api_key_id
from app.core.budget import RequestBudget, use_budget
from app.core.logging import get_logger
from app.core.offload import run_cpu
from app.extraction.chunker import Chunk, chunk_markdown
from app.extraction.scraper import PageContent, Scraper
from app.llm.citations import check_claims, split_sentences, validate_citations
from app.llm.gateway import LLMError, LLMGateway, Usage
from app.llm.prompts import SYSTEM_PROMPT, _random_delimiter, flag_injection
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
from app.retrieval.reranker import HeuristicReranker, RerankerUnavailable, build_reranker
from app.retrieval.scoring import fold_diacritics, heuristic_chunk_score, tokenize

logger = get_logger(__name__)

_MAX_SUB_QUERIES = 5
_EXTRACTOR_MAX_CHARS = 12_000
_MAX_EXTRACT_BATCHES = 3
_MAX_CHUNKS_PER_DOC_IN_MERGE = 6
_MAX_REPORT_CHUNKS = 40
_QUANT_ADAPTERS = frozenset({"viet_stock", "stooq", "yahoo_finance", "sec_edgar", "fengshui"})
_NON_TICKER_UPPER = frozenset(
    {
        "CEO",
        "CFO",
        "COO",
        "CTO",
        "IPO",
        "ROE",
        "ROA",
        "ROS",
        "ROC",
        "EPS",
        "ETF",
        "GDP",
        "CPI",
        "PPI",
        "USD",
        "VND",
        "EUR",
        "API",
        "LLM",
        "SQL",
        "RAG",
        "USA",
        "HOSE",
        "HSX",
        "HNX",
        "SEC",
        "FED",
        "BCTC",
        "NIM",
        "NPL",
        "CASA",
        "DSO",
        "ICR",
        "ATR",
        "RSI",
        "MACD",
        "MA50",
        "MA200",
        "YOY",
        "QOQ",
        "VNI",
        "VN30",
    }
)
_VI_CHAR_RE = re.compile(
    r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.IGNORECASE
)
_CITE_INLINE_RE = re.compile(r"\[\d{1,3}\]")

RESEARCH_REPORT_SYSTEM_PROMPT = (
    f"{SYSTEM_PROMPT}\n"
    "9. DEEP RESEARCH RIGOR:\n"
    "   - Follow the planned section outline cleanly using Markdown headings.\n"
    "   - Use Markdown tables for comparative attributes, financial ratios, and timelines.\n"
    "   - Explicitly reconcile or highlight conflicting evidence across sources.\n"
    "   - Always include a dedicated Risks & Counterarguments section examining downside risks, "
    "opposing views, or evidence gaps.\n"
    "   - Never fabricate metrics, dates, or citations beyond the provided [1..N] sources."
)


def _reformulate_query(
    question: str, gap: str, learnings: list[Learning], *, max_facts: int = 3
) -> str:
    """docs/20 P1-1 (IRCoT) + docs/30 B1 fix: build a SHORT KEYWORD hop query
    from the gap text (entity + 3-6 distinctive keywords + recency framing).
    The old prose form ("question — gap. Given what is already established
    (500 chars of facts), find:") produced near-zero-relevance search results
    because web engines rank keyword queries, not prose paragraphs."""
    # Extract the entity (ticker or company name) so every hop keeps it.
    entity = question.strip()
    # Distinctive gap keywords: > 3 chars, not generic, at most 6.
    _GENERIC_HOP_WORDS = frozenset(
        {
            "given",
            "what",
            "already",
            "established",
            "find",
            "more",
            "about",
            "the",
            "and",
            "for",
            "that",
            "this",
            "with",
            "from",
            "are",
            "was",
            "cần",
            "tìm",
            "thêm",
            "về",
            "và",
            "của",
            "cho",
            "được",
            "là",
        }
    )
    gap_tokens = [t for t in gap.split() if len(t) > 3 and t.lower() not in _GENERIC_HOP_WORDS]
    keywords = " ".join(gap_tokens[:6])
    if keywords:
        return f"{entity} {keywords} mới nhất"
    return f"{entity} {gap.strip()[:60]} mới nhất"


def _plan_coverage(
    plan_queries: list[tuple[str, str]], learnings: list[Learning], *, threshold: float = 0.25
) -> float:
    """docs/20 P1-1 coverage gate: fraction of planner sub-questions whose key
    terms are already reflected in the extracted learnings (lexical proxy for
    "answered", diacritic-folded for VI parity). 1.0 when there is nothing to cover."""
    if not plan_queries:
        return 1.0
    facts_raw = " ".join(lr.fact.lower() for lr in learnings)
    facts_folded = fold_diacritics(facts_raw)
    facts_tokens = set(tokenize(facts_raw, fold=True))
    covered = 0
    for query, purpose in plan_queries:
        terms = [t for t in tokenize(f"{query} {purpose}", fold=True) if len(t) > 3]
        if not terms:
            # Pass 1 P2: acronym-only sub-queries ("P/E P/B ROE EPS") have no
            # checkable terms — skipping them as "covered" could report 1.0
            # coverage on ZERO evidence. The threshold math below decides.
            continue
        hits = sum(1 for t in terms if (t in facts_tokens or t in facts_folded))
        if hits / len(terms) >= threshold:
            covered += 1
    return covered / len(plan_queries)


@dataclass(slots=True)
class Learning:
    fact: str
    citation_id: int  # chunk number in the merged ContextPack


@dataclass(slots=True)
class ContextPack:
    """Merged research context: reranked chunks + sources + learnings + conflicts."""

    chunks: list[Chunk] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    learnings: list[Learning] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    tool_summaries: list[dict[str, Any]] = field(default_factory=list)
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
    """Classify the query into a domain scaffolding profile (Harness² pattern).

    Matches both accented and diacritic-folded Vietnamese forms so unaccented
    queries route to the same specialist domain harness as accented ones.
    """
    if requested != "auto" and requested in HARNESS_PROFILES:
        return requested
    q = query.lower()
    qf = fold_diacritics(q)

    def _has(kw: str) -> bool:
        return kw in q or fold_diacritics(kw) in qf

    comp_keywords = (
        "so sánh",
        "versus",
        " vs ",
        "compare",
        "comparison",
        "khác nhau",
        "ưu nhược điểm",
        "doi chieu",
        "nen chon",
    )
    if any(_has(k) for k in comp_keywords):
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
        "phan tich co phieu",
        "nhan dinh thi truong",
        "khoi ngoai",
        "tu doanh",
        "trien vong nganh",
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
    if any(_has(k) for k in fin_keywords) or re.search(r"\$[a-z]{1,6}\b", q):
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
    if any(_has(k) for k in fengshui_keywords):
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
    if any(_has(k) for k in health_keywords):
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
    if any(_has(k) for k in news_keywords):
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
    if any(_has(k) for k in dev_keywords):
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
    if any(_has(k) for k in academic_keywords):
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
                f"Decompose the question into {breadth} distinct web-search sub-queries.\n"
                "CRAFT RULES (docs/30 A1):\n"
                "- Each sub_query MUST be <= 12 words, keyword-style (NOT a full question).\n"
                "- Each sub_query MUST contain the main entity/ticker/company name VERBATIM.\n"
                "- Write sub_queries in the SAME LANGUAGE as the question (Vietnamese "
                "question → Vietnamese sub-queries).\n"
                "- Vary the facet across queries: fundamentals, flows/liquidity, valuation, "
                "risks, recent news.\n"
                "- Include one recency-framed query "
                "(e.g. 'tin tức FPT mới nhất 7 ngày').\n"
                "- NEVER produce English sub-queries for a Vietnamese question.\n"
                "Example: question 'FPT có nên mua không?' → sub_queries: "
                '["FPT lợi nhuận tăng trưởng mới nhất", "FPT định giá P/E P/B hiện tại", '
                '"FPT khối ngoại mua ròng dòng tiền", "FPT rủi ro thách thức cạnh tranh"]\n'
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
                "From the numbered sources below, extract facts relevant to the question.\n"
                "EXTRACTION RULES (docs/30 A2):\n"
                "- Extract 3-8 ATOMIC facts, each ONE sentence, self-contained.\n"
                "- Preserve every number with its unit/currency and as-of date VERBATIM.\n"
                "- Prefer quantitative statements over adjectives ('doanh thu 5.200 tỷ đồng' "
                "not 'doanh thu lớn').\n"
                "- Never merge two facts into one sentence.\n"
                "- ALWAYS include 1-3 gaps: what could NOT be verified or answered from "
                "these sources (required — do not omit the gaps array).\n"
                "STRICT JSON only: "
                '{"learnings": [{"fact": "...", "citation_id": <number>}], '
                '"conflicts": [{"topic": "...", "claim_a": "...", "citation_a": <number>, '
                '"claim_b": "...", "citation_b": <number>}], '
                '"gaps": ["..."]}. citation_id must be one of the source numbers in this batch.'
            ),
        },
        {
            "role": "user",
            "content": f"Question: {question}\n\nSources:\n\n{chunk_text}",
        },
    ]


def _outline_messages(
    question: str,
    harness: ResearchHarness | None = None,
) -> list[dict[str, str]]:
    h = harness or HARNESS_PROFILES["general"]
    return [
        {
            "role": "system",
            "content": (
                f"You are a STORM-style research architect ({h.profile} profile). "
                "Identify 2-4 distinct analytical perspectives (stakeholders/lenses) and "
                "5-7 clean report section headings tailored to the question and target domain: "
                f"{h.report_structure}. Always include a risks/counterarguments section and a "
                "references section. Respond in the same language as the question with STRICT JSON "
                'only: {"perspectives": ["..."], "sections": ["..."]}.'
            ),
        },
        {"role": "user", "content": f"Question: {question}"},
    ]


def _default_outline(question: str, harness: ResearchHarness) -> dict[str, Any]:
    raw_struct = re.sub(r"^Structure the report:\s*", "", harness.report_structure).rstrip(".")
    sections = [s.strip() for s in raw_struct.split("->") if s.strip()]
    is_vi = bool(_VI_CHAR_RE.search(question))
    risk_heading = "Rủi ro & Phản biện" if is_vi else "Risks & Counterarguments"
    if sections and not any(
        k in s.lower()
        for s in sections
        for k in ("risk", "limit", "rủi ro", "phản biện", "challenge")
    ):
        insert_at = max(1, len(sections) - 1)
        sections.insert(insert_at, risk_heading)
    perspectives = {
        "financial": [
            "Fundamental & Cash-Flow Forensic",
            "Valuation & Smart-Money Flows",
            "Downside & Governance Risks",
        ],
        "comparative": [
            "Architecture & Capabilities",
            "Benchmark & Cost Trade-Offs",
            "Migration & Adoption Risk",
        ],
        "developer": [
            "Architecture & Semantics",
            "Production Trade-Offs & Pitfalls",
            "Operations & Security",
        ],
        "academic": [
            "Theoretical Basis",
            "Empirical SOTA & Methodology",
            "Limitations & Counter-Evidence",
        ],
    }.get(
        harness.profile,
        ["Core Fundamentals", "Quantitative & Empirical Evidence", "Counterarguments & Risks"],
    )
    return {"perspectives": perspectives, "sections": sections[:8], "profile": harness.profile}


def _render_pack(chunks: list[Chunk], start: int = 1) -> str:
    """Numbered source blocks wrapped in per-request random delimiters
    (SEC-07 parity with prompts.render_context - docs/29 P1-1: web
    content is untrusted and must never sit bare next to instructions)."""
    blocks = []
    for offset, chunk in enumerate(chunks):
        header = f"[{start + offset}] {chunk.document_url}"
        # docs/30 A3: source title so the LLM knows what each source is.
        src_title = f" — {chunk.source_title}" if chunk.source_title else ""
        header += src_title
        section = f"(section: {chunk.heading_path})" + ("\n" if chunk.heading_path else "")
        begin, end_delim = _random_delimiter(start + offset), _random_delimiter(start + offset)
        body = "\n<<<" + begin + ">>>\n" + section + chunk.content + "\n<<<" + end_delim + ">>>"
        blocks.append(header + body)
    return "\n\n".join(blocks)


def _report_messages(
    question: str,
    learnings: list[Learning],
    context_text: str,
    harness: ResearchHarness | None = None,
    *,
    outline: dict[str, Any] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    h = harness or HARNESS_PROFILES["general"]
    learnings_text = "\n".join(f"- {item.fact} [{item.citation_id}]" for item in learnings) or "-"
    outline_block = ""
    if outline and outline.get("sections"):
        sec_lines = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(outline["sections"]))
        persp_line = ", ".join(str(p) for p in (outline.get("perspectives") or []))
        outline_block = f"\nPlanned Outline:\n{sec_lines}\n" + (
            f"Analytical Perspectives: {persp_line}\n" if persp_line else ""
        )
    conflict_block = ""
    if conflicts:
        c_lines = []
        for c in conflicts[:8]:
            ca = f"[{c['citation_a']}]" if c.get("citation_a") else ""
            cb = f"[{c['citation_b']}]" if c.get("citation_b") else ""
            c_lines.append(
                f"- {c.get('topic', 'Conflict')}: {c.get('claim_a', '')} {ca} vs "
                f"{c.get('claim_b', '')} {cb}"
            )
        conflict_block = (
            "\nDetected Source Conflicts (must address explicitly):\n" + "\n".join(c_lines) + "\n"
        )
    return [
        {
            "role": "system",
            "content": (
                f"{RESEARCH_REPORT_SYSTEM_PROMPT}\n{h.report_structure} "
                "Be thorough and cite [n] for every claim."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question: {question}{outline_block}{conflict_block}\n"
                f"Known learnings:\n{learnings_text}\n\n"
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
    space: str | None = None
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
        # docs/29 P3-2 (audit B#16): canonicalize() raises on malformed URLs
        # (e.g. bad IPv6 literals) — guard it like pipeline.py does so one
        # malformed page cannot abort the whole research iteration.
        try:
            canon_url = canonicalize(page.url)
        except Exception:
            canon_url = page.url
        scraped_urls.add(canon_url)
        matched_res = res_by_url.get(page.url) or res_by_url.get(canon_url)
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


def _extract_tickers_from_question(question: str) -> list[str]:
    """Extract conservative equity ticker symbols from a financial research question."""
    from app.retrieval.vn_tickers import load_vn_tickers

    vn_map = load_vn_tickers()
    found: list[str] = []
    for sym in re.findall(r"\$([A-Z]{2,5}(?:\.[A-Z])?)\b", question):
        if sym not in found:
            found.append(sym)
    for tok in re.findall(r"\b([A-Z]{3,4})\b", question):
        if tok in _NON_TICKER_UPPER:
            continue
        if tok in vn_map and tok not in found:
            found.append(tok)
    return found[:4]


def _make_tool_chunk(url: str, title: str, heading: str, content: str, pos: int) -> Chunk:
    digest = hashlib.sha256(f"{url}:{pos}:{title}".encode("utf-8", "replace")).hexdigest()[:32]
    return Chunk(
        id=digest,
        document_url=url,
        position=pos,
        heading_path=heading,
        content=content[:2000],
        word_count=len(content.split()),
        source_title=title,
    )


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
        budget = RequestBudget(
            total_timeout_s=float(deadline),
            stage_caps={"search": min(8.0, float(deadline)), "scrape": min(8.0, float(deadline))},
        )

        async with admit("research"):
            with use_budget(budget):
                yield (
                    "status",
                    {"stage": "planning", "detail": f"{question} [profile: {harness.profile}]"},
                )

                pack = ContextPack()
                stage0_tools = await self._run_stage0_tools(
                    question, harness.profile, params, pack, budget
                )
                if stage0_tools:
                    pack.tool_summaries.extend(stage0_tools)
                    yield ("tool_results", {"tools": stage0_tools})

                sub_queries, categories, plan_usage = await self._plan(
                    question, breadth, params.lang, harness, gateway=gateway, model=quick_model
                )
                usage_total.input_tokens += plan_usage.input_tokens
                usage_total.output_tokens += plan_usage.output_tokens
                outline_data, outline_usage = await self._generate_outline(
                    question, harness, gateway=gateway, model=quick_model
                )
                usage_total.input_tokens += outline_usage.input_tokens
                usage_total.output_tokens += outline_usage.output_tokens

                # Yielded only (no on_event): run_job forwards generator events itself
                yield (
                    "plan",
                    {
                        "profile": harness.profile,
                        "sub_queries": [
                            {"query": q, "purpose": purpose} for q, purpose in sub_queries
                        ],
                        "categories": categories,
                    },
                )
                yield ("outline", outline_data)

                truncated = False
                plan_queries = list(sub_queries)  # kept for the P1-1 coverage gate
                coverage = 0.0
                executed_queries: set[str] = set()
                devil_ran = False
                is_vi_q = bool(_VI_CHAR_RE.search(question)) or (params.lang or "").startswith("vi")

                for iteration in range(depth):
                    if (
                        time.perf_counter() - started > deadline
                        or (usage_total.input_tokens + usage_total.output_tokens) >= token_budget
                    ):
                        truncated = True
                        break
                    yield ("iteration_start", {"iteration": iteration + 1, "max_iterations": depth})
                    if iteration > 0 and pack.gaps:
                        next_subs: list[tuple[str, str]] = []
                        for gap in pack.gaps[:breadth]:
                            cand = (
                                _reformulate_query(question, gap, pack.learnings)
                                if self.settings.research_ircot_reformulate
                                else f"{gap} evidence analysis"
                            )
                            purpose = (
                                "gap follow-up (IRCoT)"
                                if self.settings.research_ircot_reformulate
                                else "gap follow-up"
                            )
                            if fold_diacritics(cand.lower()) in executed_queries:
                                cand = f"{cand} (update #{iteration + 1})"
                            next_subs.append((cand, purpose))
                        sub_queries = next_subs

                    if (
                        iteration >= 1
                        and not devil_ran
                        and harness.profile in ("financial", "comparative", "academic", "general")
                    ):
                        devil_ran = True
                        counter_q = (
                            f"{question} rủi ro phản biện hạn chế"
                            if is_vi_q
                            else f"{question} risks limitations counterarguments"
                        )
                        if len(sub_queries) < breadth:
                            sub_queries = [*sub_queries, (counter_q, "counter-thesis / risks")]
                        elif sub_queries:
                            sub_queries = [*sub_queries[:-1], (counter_q, "counter-thesis / risks")]

                    queries = [q for q, _purpose in sub_queries] or [question]
                    for q_item in queries:
                        executed_queries.add(fold_diacritics(q_item.lower()))

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
                        if isinstance(out, BaseException):
                            continue
                        for r in out.results:
                            existing = deduped_results.get(r.canonical_url)
                            if existing is None or r.score > existing.score:
                                deduped_results[r.canonical_url] = r

                    merged_results = sorted(deduped_results.values(), key=lambda r: -r.score)[
                        : params.k * max(len(queries), 1)
                    ]
                    if not merged_results:
                        if not pack.chunks:
                            break
                        continue

                    # docs/30 B2: scrape cap scales with query count (was fixed 6)
                    scrape_cap = min(15, max(6, 3 * len(plan_queries)))
                    to_scrape = [
                        r.url for r in merged_results if r.url not in pack._seen_chunk_urls
                    ][:scrape_cap]
                    pages = (
                        await self.scraper.scrape_many(
                            to_scrape, max_pages=scrape_cap, timeout_s=12.0
                        )
                        if to_scrape
                        else []
                    )
                    ok_pages = [p for p in pages if isinstance(p, PageContent)]
                    chunk_start = pack.next_citation_number()  # last chunk number so far
                    new_chunks = await self._chunks(
                        question, merged_results, ok_pages, offset=chunk_start
                    )
                    sources = [
                        {
                            "url": r.url,
                            "title": r.title,
                            "domain": r.domain,
                            "score": round(r.score, 4),
                        }
                        for r in merged_results
                    ]
                    accepted = pack.merge(new_chunks, sources)
                    if ok_pages and accepted:
                        persist.persist_pages(ok_pages, accepted, owner=current_api_key_id.get())

                    iter_learnings: list[Learning] = []
                    iter_gaps: list[str] = []
                    if accepted:
                        batches = self._batch_chunks(accepted, start=chunk_start + 1)
                        for b_start, batch in batches[:_MAX_EXTRACT_BATCHES]:
                            valid_ids = set(range(b_start, b_start + len(batch)))
                            (
                                b_learnings,
                                b_gaps,
                                b_conflicts,
                                usage,
                            ) = await self._extract_with_conflicts(
                                question,
                                _render_pack(batch, start=b_start),
                                harness,
                                valid_ids=valid_ids,
                                gateway=gateway,
                                model=quick_model,
                            )
                            usage_total.input_tokens += usage.input_tokens
                            usage_total.output_tokens += usage.output_tokens
                            iter_learnings.extend(b_learnings)
                            for g in b_gaps:
                                if g not in iter_gaps:
                                    iter_gaps.append(g)
                            for conf in b_conflicts:
                                if len(pack.conflicts) < 12:
                                    pack.conflicts.append(conf)
                    iter_gaps = iter_gaps[:8]
                    pack.learnings.extend(iter_learnings)
                    # docs/30 E1: accumulate gaps (not replace) — the extractor
                    # may not repeat earlier gaps but unresolved ones persist.
                    for g in iter_gaps:
                        if g not in pack.gaps:
                            pack.gaps.append(g)
                    if self.settings.research_coverage_gate_enabled:
                        coverage = _plan_coverage(plan_queries, pack.learnings)
                    # Yielded only — same duplicate-prevention as the plan event.
                    yield (
                        "learnings",
                        {
                            "learnings": [item.fact for item in iter_learnings],
                            "gaps": iter_gaps,
                            "iteration": iteration + 1,
                            "coverage": round(coverage, 4),
                        },
                    )
                    coverage_ok = (
                        not self.settings.research_coverage_gate_enabled
                        or coverage >= self.settings.research_coverage_threshold
                    )
                    # docs/30 E1: the old `if not iter_gaps and coverage_ok: break`
                    # exited after ONE iteration in the common case (extractor
                    # omitted gaps). Now: never break before iteration 2, and
                    # only break when coverage is met AND no new gaps appeared.
                    if not iter_gaps and coverage_ok and iteration >= 1:
                        break
                    if not iter_gaps and not coverage_ok and iteration >= 1:
                        plan_q = [
                            q for q in plan_queries if _plan_coverage([q], pack.learnings) < 0.25
                        ]
                        if plan_q:
                            refined: list[tuple[str, str]] = []
                            for q_txt, purp in plan_q:
                                folded = fold_diacritics(q_txt.lower())
                                if folded in executed_queries:
                                    refined.append((f"{q_txt} evidence analysis", purp))
                                else:
                                    refined.append((q_txt, purp))
                            sub_queries = refined

                if not pack.chunks:
                    if truncated:
                        yield (
                            "error",
                            {
                                "code": "invalid_request",
                                "message": (
                                    "Nghiên cứu dừng do hết ngân sách/thời gian "
                                    "trước khi thu thập nguồn"
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

                yield (
                    "status",
                    {"stage": "writing_report", "detail": f"{len(pack.chunks)} chunks"},
                )
                included = await self._rerank_chunks(question, pack.chunks, gateway=gateway)
                if not included:
                    included = list(pack.chunks[:_MAX_REPORT_CHUNKS])

                # Map original 1-based ContextPack chunk positions -> contiguous 1..M IDs
                new_idx_by_key = {
                    (ch.document_url, ch.position): idx for idx, ch in enumerate(included, start=1)
                }
                old_to_new: dict[int, int] = {}
                for old_idx, ch in enumerate(pack.chunks, start=1):
                    mapped = new_idx_by_key.get((ch.document_url, ch.position))
                    if mapped is not None:
                        old_to_new[old_idx] = mapped

                remapped_learnings: list[Learning] = []
                for lr in pack.learnings:
                    mapped_id = old_to_new.get(lr.citation_id)
                    if mapped_id is not None:
                        remapped_learnings.append(Learning(fact=lr.fact, citation_id=mapped_id))
                remapped_conflicts: list[dict[str, Any]] = []
                for c in pack.conflicts:
                    ca = old_to_new.get(c.get("citation_a") or 0)
                    cb = old_to_new.get(c.get("citation_b") or 0)
                    remapped_conflicts.append({**c, "citation_a": ca, "citation_b": cb})

                numbered_included = list(enumerate(included, start=1))
                flag_injection(numbered_included)

                report_id = params.research_id or uuid.uuid4()
                answer_parts: list[str] = []
                try:
                    messages = _report_messages(
                        question,
                        remapped_learnings,
                        _render_pack(included),
                        harness=harness,
                        outline=outline_data,
                        conflicts=remapped_conflicts,
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

                check = validate_citations("".join(answer_parts), numbered_included)
                claim_check = check_claims(check.text, numbered_included)
                substantive = [
                    s
                    for s in split_sentences(check.text)
                    if len(s.strip()) > 20 and not s.lstrip().startswith("#")
                ]
                cited_sentences = sum(1 for s in substantive if _CITE_INLINE_RE.search(s))
                citation_coverage = (
                    (cited_sentences / len(substantive))
                    if substantive
                    else (1.0 if check.citations else 0.0)
                )
                verification_payload = {
                    "citation_count": len(check.citations),
                    "invalid_markers": list(check.invalid_markers),
                    "unsupported_markers": list(claim_check.unsupported_markers),
                    "flagged_sentences": list(claim_check.flagged_sentences[:5]),
                    "numeric_fidelity": float(claim_check.numeric_fidelity),
                    "unverified_numbers": list(claim_check.unverified_numbers[:10]),
                    "citation_coverage": round(citation_coverage, 4),
                    "conflicts_count": len(remapped_conflicts),
                }

                citations_payload = [
                    {"n": c.n, "url": c.url, "title": c.title, "chunk_id": c.chunk_id}
                    for c in check.citations
                ]
                latency_ms = int((time.perf_counter() - started) * 1000)
                yield (
                    "citations",
                    {"citations": citations_payload, "invalid_markers": check.invalid_markers},
                )
                yield ("verification", verification_payload)
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
                        "outline": outline_data.get("sections", []),
                        "conflicts": remapped_conflicts,
                        "verification": verification_payload,
                        "tools": stage0_tools,
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
                    metadata={
                        "profile": harness.profile,
                        "learnings": [lr.fact for lr in remapped_learnings],
                        "gaps": pack.gaps,
                        "conflicts": remapped_conflicts,
                        "outline": outline_data.get("sections", []),
                        "perspectives": outline_data.get("perspectives", []),
                        "verification": verification_payload,
                        "tools": stage0_tools,
                        "citation_titles": {str(c.n): c.title for c in check.citations if c.title},
                    },
                )

    # -- stages --------------------------------------------------------------

    def _batch_chunks(self, chunks: list[Chunk], *, start: int) -> list[tuple[int, list[Chunk]]]:
        """Split chunks into character-bounded extractor batches preserving exact [n] IDs."""
        batches: list[tuple[int, list[Chunk]]] = []
        cur: list[Chunk] = []
        cur_start = start
        cur_chars = 0
        for idx, ch in enumerate(chunks):
            est = len(ch.content) + len(ch.document_url) + 80
            if cur and cur_chars + est > _EXTRACTOR_MAX_CHARS:
                batches.append((cur_start, cur))
                cur = []
                cur_start = start + idx
                cur_chars = 0
            cur.append(ch)
            cur_chars += est
        if cur:
            batches.append((cur_start, cur))
        return batches

    async def _run_stage0_tools(
        self,
        question: str,
        profile_name: str,
        params: ResearchParams,
        pack: ContextPack,
        budget: RequestBudget,
    ) -> list[dict[str, Any]]:
        """Fail-open bounded Stage 0 specialist tool mesh (Spaces RAG/KG + Finance live tools)."""
        summaries: list[dict[str, Any]] = []
        tool_timeout = min(5.5, max(1.0, budget.remaining() * 0.3))

        async def _space_tool() -> list[tuple[Chunk, dict[str, str], dict[str, Any]]]:
            if not params.space:
                return []
            from app.db.session import get_sessionmaker
            from app.retrieval.graph import query_graph
            from app.retrieval.hybrid import hybrid_chunk_search
            from app.retrieval.spaces import get_space

            owner = current_api_key_id.get() or "anonymous"
            space = await get_space(owner, params.space)
            if space is None:
                return []
            out: list[tuple[Chunk, dict[str, Any], dict[str, Any]]] = []
            async with get_sessionmaker()() as session:
                h_task = hybrid_chunk_search(
                    session,
                    question,
                    k=6,
                    domains=list(space.domains or []) or None,
                    urls=list(space.pinned_urls or []) or None,
                    owner=current_api_key_id.get(),
                )
                g_task = query_graph(session, space, question)
                h_res, g_res = await asyncio.gather(h_task, g_task, return_exceptions=True)
            h_items = h_res if isinstance(h_res, list) else []
            for pos, row in enumerate(h_items):
                u = str(row.get("url") or f"space://{params.space}/{pos}")
                txt = str(row.get("content") or "").strip()
                if txt:
                    ch = _make_tool_chunk(
                        u,
                        str(row.get("title") or params.space),
                        str(row.get("heading_path") or f"Space:{params.space}"),
                        txt,
                        pos,
                    )
                    out.append(
                        (
                            ch,
                            {
                                "url": u,
                                "title": str(row.get("title") or u),
                                "domain": str(row.get("domain") or "space"),
                                "score": 0.9,
                            },
                            {},
                        )
                    )
            if isinstance(g_res, dict) and g_res.get("context"):
                g_url = f"space://{params.space}/graph"
                g_ch = _make_tool_chunk(
                    g_url,
                    f"Knowledge Graph: {params.space}",
                    "Knowledge Graph",
                    str(g_res["context"]),
                    0,
                )
                out.append(
                    (
                        g_ch,
                        {
                            "url": g_url,
                            "title": g_ch.source_title,
                            "domain": "space.kg",
                            "score": 0.92,
                        },
                        {},
                    )
                )
            if out:
                summaries.append(
                    {
                        "tool": "space_rag",
                        "space": params.space,
                        "chunks": len(out),
                    }
                )
            return out

        async def _finance_tools() -> list[tuple[Chunk, dict[str, Any], dict[str, Any]]]:
            if profile_name != "financial":
                return []
            tickers = _extract_tickers_from_question(question)
            if not tickers:
                return []
            from app.api import deps
            from app.retrieval.events import (
                fetch_market_events,
                fetch_market_news_wire,
                record_to_dict,
            )
            from app.retrieval.sentiment_quant import fetch_sentiment_quant_context
            from app.retrieval.vn_tickers import load_vn_tickers

            vn_set = load_vn_tickers()
            vn_tickers = [t for t in tickers if t in vn_set]
            tasks: list[Any] = [
                fetch_market_news_wire(
                    symbols=tickers,
                    lang="vi" if vn_tickers else "en",
                    max_age_days=14,
                    max_items=6,
                    timeout_s=3.5,
                )
            ]
            if vn_tickers:
                tasks.append(fetch_market_events(symbols=vn_tickers, timeout_s=3.5))
            fa_agg = deps.get_fa_aggregator()
            for sym in tickers[:2]:
                tasks.append(
                    fetch_sentiment_quant_context(
                        sym,
                        is_vietnam=(sym in vn_set),
                        fa_aggregator=fa_agg,
                        timeout_s=3.5,
                    )
                )
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)
            out: list[tuple[Chunk, dict[str, Any], dict[str, Any]]] = []
            wire_res = raw_results[0]
            if isinstance(wire_res, dict):
                wire_items = wire_res.get("items") or []
                for pos, raw_item in enumerate(wire_items[:5]):
                    item = record_to_dict(raw_item)
                    u = str(item.get("url") or "")
                    t = str(item.get("title") or "")
                    s = str(item.get("snippet") or item.get("summary") or t)
                    if u and s:
                        ch = _make_tool_chunk(u, t or u, "Market News Wire", f"{t}\n{s}", pos)
                        out.append(
                            (
                                ch,
                                {
                                    "url": u,
                                    "title": t,
                                    "domain": str(
                                        item.get("source_id") or item.get("source") or "news"
                                    ),
                                    "score": 0.85,
                                },
                                {},
                            )
                        )
                if wire_items:
                    summaries.append({"tool": "market_news_wire", "items": len(wire_items[:5])})

            idx_offset = 1
            if vn_tickers:
                ev_res = raw_results[1] if len(raw_results) > 1 else None
                idx_offset = 2
                if isinstance(ev_res, dict) and ev_res.get("events"):
                    ev_dicts = [record_to_dict(e) for e in (ev_res.get("events") or [])[:6]]
                    ev_lines = [
                        f"- {e.get('symbol')}: {e.get('event_type')} | {e.get('title')} ("
                        f"{e.get('ex_date') or e.get('event_date') or e.get('announced_at') or ''})"
                        for e in ev_dicts
                    ]
                    u = "https://cafef.vn/lich-su-kien.chn"
                    ch = _make_tool_chunk(
                        u,
                        f"Corporate Events ({', '.join(vn_tickers)})",
                        "Corporate Events",
                        "\n".join(ev_lines),
                        0,
                    )
                    out.append(
                        (
                            ch,
                            {
                                "url": u,
                                "title": ch.source_title,
                                "domain": "cafef.vn",
                                "score": 0.88,
                            },
                            {},
                        )
                    )
                    summaries.append({"tool": "market_events", "events": len(ev_lines)})

            quant_hits = 0
            for t_idx, sym in enumerate(tickers[:2]):
                q_res = (
                    raw_results[idx_offset + t_idx]
                    if len(raw_results) > idx_offset + t_idx
                    else None
                )
                if not isinstance(q_res, tuple) or len(q_res) != 5:
                    continue
                bars, src_name, src_url, as_of, fa_snap = q_res
                lines: list[str] = []
                if bars:
                    last_c = bars[-1].close
                    lines.append(
                        f"{sym} latest close={last_c} "
                        f"(bars={len(bars)}, source={src_name}, as_of={as_of})"
                    )
                if fa_snap is not None:
                    metrics = [
                        f"{k}={v}"
                        for k, v in (
                            ("P/E", fa_snap.pe),
                            ("P/B", fa_snap.pb),
                            ("ROE", fa_snap.roe),
                            ("RevGrowthYoY", fa_snap.revenue_growth_yoy),
                            ("ProfitGrowthYoY", fa_snap.profit_growth_yoy),
                            ("MarketCap", fa_snap.market_cap),
                        )
                        if v is not None
                    ]
                    if metrics:
                        lines.append(f"{sym} fundamentals ({fa_snap.source}): {', '.join(metrics)}")
                if lines:
                    u = src_url or f"https://dchart.vndirect.com.vn/?symbol={sym}"
                    ch = _make_tool_chunk(
                        u,
                        f"{sym} Quant & FA Snapshot",
                        "Quantitative Market Data",
                        "\n".join(lines),
                        0,
                    )
                    out.append(
                        (
                            ch,
                            {
                                "url": u,
                                "title": ch.source_title,
                                "domain": src_name or "market",
                                "score": 0.9,
                            },
                            {},
                        )
                    )
                    quant_hits += 1
            if quant_hits:
                summaries.append({"tool": "financial_quant", "symbols": tickers[:2]})
            return out

        try:
            collected = await asyncio.wait_for(
                asyncio.gather(_space_tool(), _finance_tools(), return_exceptions=True),
                timeout=tool_timeout,
            )
            chunks_to_merge: list[Chunk] = []
            sources_to_merge: list[dict[str, Any]] = []
            offset = pack.next_citation_number()
            for part in collected:
                if not isinstance(part, list):
                    continue
                for ch, src, _meta in part:
                    ch.position = offset
                    offset += 1
                    chunks_to_merge.append(ch)
                    sources_to_merge.append(src)
            if chunks_to_merge:
                pack.merge(chunks_to_merge, sources_to_merge)
        except Exception as exc:
            logger.debug("research_stage0_tools_skipped", error=type(exc).__name__)
        return summaries

    async def _generate_outline(
        self,
        question: str,
        harness: ResearchHarness,
        *,
        gateway: LLMGateway,
        model: str,
    ) -> tuple[dict[str, Any], Usage]:
        """STORM-style perspective + outline synthesis with deterministic fail-open."""
        fallback = _default_outline(question, harness)
        try:
            text, usage = await gateway.complete(
                _outline_messages(question, harness=harness),
                model=model,
                task="outline",
            )
            parsed = _extract_json(text)
            sections = [str(s).strip() for s in parsed.get("sections", []) if str(s).strip()][:8]
            perspectives = [
                str(p).strip() for p in parsed.get("perspectives", []) if str(p).strip()
            ][:5]
            if sections:
                return {
                    "perspectives": perspectives or fallback["perspectives"],
                    "sections": sections,
                    "profile": harness.profile,
                }, usage
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            logger.debug("research_outline_fallback", error=type(exc).__name__)
        return fallback, Usage()

    async def _rerank_chunks(
        self,
        question: str,
        chunks: list[Chunk],
        *,
        gateway: LLMGateway,
    ) -> list[Chunk]:
        if len(chunks) <= 1:
            return list(chunks)
        scored = [
            (heuristic_chunk_score(question, ch.content, 0.5, ch.position), ch) for ch in chunks
        ]
        reranker = build_reranker(self.settings, gateway=gateway)
        if getattr(reranker, "name", "heuristic") == "heuristic":
            reranker = HeuristicReranker(max_chunks=_MAX_REPORT_CHUNKS, max_per_url=6)
        try:
            reranked = await reranker.rerank(question, scored)
            return (
                reranked[:_MAX_REPORT_CHUNKS]
                if reranked
                else [c for _, c in scored[:_MAX_REPORT_CHUNKS]]
            )
        except RerankerUnavailable:
            return await HeuristicReranker(max_chunks=_MAX_REPORT_CHUNKS, max_per_url=6).rerank(
                question, scored
            )

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
        line_ids = {int(m.group(1)) for m in re.finditer(r"^\[(\d+)\]", chunk_text, re.MULTILINE)}
        valid_ids = line_ids or {int(m.group(1)) for m in re.finditer(r"\[(\d+)\]", chunk_text)}
        learnings, gaps, _conflicts, usage = await self._extract_with_conflicts(
            question,
            chunk_text[:_EXTRACTOR_MAX_CHARS],
            harness,
            valid_ids=valid_ids,
            gateway=gateway,
            model=model,
        )
        return learnings, gaps, usage

    async def _extract_with_conflicts(
        self,
        question: str,
        chunk_text: str,
        harness: ResearchHarness | None = None,
        *,
        valid_ids: set[int],
        gateway: LLMGateway | None = None,
        model: str | None = None,
    ) -> tuple[list[Learning], list[str], list[dict[str, Any]], Usage]:
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
            learnings = [
                Learning(fact=str(item["fact"]), citation_id=int(item.get("citation_id") or 0))
                for item in parsed.get("learnings", [])
                if isinstance(item, dict)
                and item.get("fact")
                and int(item.get("citation_id") or 0) in valid_ids
            ]
            gaps = [str(g) for g in parsed.get("gaps", []) if g and str(g).strip()][:8]
            conflicts: list[dict[str, Any]] = []
            for raw_c in parsed.get("conflicts", []):
                if not isinstance(raw_c, dict) or not raw_c.get("topic"):
                    continue
                try:
                    ca = int(raw_c.get("citation_a") or 0)
                    cb = int(raw_c.get("citation_b") or 0)
                except (ValueError, TypeError):
                    continue
                if (
                    ca in valid_ids
                    and cb in valid_ids
                    and raw_c.get("claim_a")
                    and raw_c.get("claim_b")
                ):
                    conflicts.append(
                        {
                            "topic": str(raw_c["topic"])[:140],
                            "claim_a": str(raw_c["claim_a"])[:260],
                            "citation_a": ca,
                            "claim_b": str(raw_c["claim_b"])[:260],
                            "citation_b": cb,
                        }
                    )
            return learnings, gaps, conflicts, usage
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            logger.warning("research_extract_fallback", error=type(exc).__name__)
            return [], [], [], Usage()

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
    from app.core.json_utils import extract_json_object

    parsed = extract_json_object(text)
    if parsed is None:
        raise ValueError("no JSON object in planner/extractor reply")
    return parsed


def record_usage_safe(model: str, usage: Usage) -> None:
    from app.llm.gateway import record_usage

    try:
        record_usage(model, usage)
    except Exception as exc:  # metrics must never break a request
        logger.debug("record_usage_failed", model=model, error=type(exc).__name__)
