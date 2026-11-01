"""Universal Multi-Domain Deep Intelligence Architecture.

Designs MetaSearch so that deep, essence-level analysis ("hiểu bản chất thay vì chỉ
đọc chỉ số bề mặt") is pluggable across ANY vertical domain, with **Financial & Equities
Forensics (`financial`)** serving as the full-depth **Flagship Pilot** (`flagship_pilot`)
and 7 additional specialized domain packs (`biomedical_health`, `technology_ai`,
`academic_research`, `legal_macro_policy`, `eastern_metaphysics`, `news_geopolitics`,
`general_discovery`) ready for cross-domain synthesis and Council verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.retrieval.technical_analysis import parse_custom_indicator_params


@dataclass(frozen=True, slots=True)
class DomainIntelligencePack:
    """Specification of a pluggable deep domain intelligence module."""

    id: str
    name: str
    maturity: str  # flagship_pilot | active_vertical | extensible_pack
    categories: list[str]
    preferred_engines: list[str]
    essence_dimensions: list[str]
    verification_rubric: list[str]
    council_critic_prompt: str
    description: str
    sample_queries: list[str] = field(default_factory=list)


DOMAIN_INTELLIGENCE_REGISTRY: dict[str, DomainIntelligencePack] = {
    "financial": DomainIntelligencePack(
        id="financial",
        name="Tài Chính, Pháp Y BCTC & Kỹ Thuật Định Lượng (Flagship Deep Pilot)",
        maturity="flagship_pilot",
        categories=["financial", "news", "general"],
        preferred_engines=[
            "viet_stock",
            "stooq",
            "yahoo_finance",
            "sec_edgar",
            "google_news",
            "searxng",
            "ddg",
        ],
        essence_dimensions=[
            "Industry-Specific BCTC Forensics: Banking CAMELS (CAR, NPL, Group-2 Debt, LLR, NIM, CASA, LDR, CIR), Securities (Margin/Equity vs 200% cap, FVTPL/HTM/AFS), Real Estate/Industrial Park (Unbilled Bookings / Người mua trả tiền trước, Inventory/Assets), Manufacturing (Debt structure, DSO, ICR)",
            "Earnings Quality & Distress Models: Altman Z-Score, Beneish M-Score (-2.22 threshold), Piotroski F-Score (0-9), CFO / Net Income divergence",
            "Quantitative Technicals: Fibonacci Retracement & Golden Pocket (61.8%-65%) + Extensions (127.2%, 261.8%), Ichimoku Kinko Hyo (Tenkan/Kijun/Kumo/Chikou), True EMA MACD, Wilder's RSI/ATR, Stochastic, EMA Ribbon (20/50/200)",
            "Geometric Trend & Pattern Recognition: Linear Regression Channel (±2σ, R²), 8 Price/Candlestick Patterns, Custom Indicator Query Params",
            "Corporate Governance & Capital Flows: Business Plan vs Actuals, Cash/Stock Dividends, Foreign Room, Block/Insider Trades, Audited Filings (HSX/HNX/SEC 10-K)",
        ],
        verification_rubric=[
            "Does the analysis distinguish surface valuation multiples (P/E, ROE) from cash-flow quality (CFO/NI, Beneish M-Score, DSO/NPL)?",
            "Are industry-specific balance-sheet mechanics applied (e.g. CAMELS for banks vs Unbilled Bookings for real estate)?",
            "Are technical support/resistance levels grounded in exact Fibonacci, Ichimoku, or Regression numbers from context [n]?",
            "Are all financial figures and ratios explicitly backed by valid source citations [n]?",
        ],
        council_critic_prompt=(
            "Audit each candidate as an Institutional Quantitative & Forensic Financial Analyst. "
            "Penalize superficial reading of P/E or ROE without balance-sheet forensics (CAMELS, "
            "Altman Z, Beneish M, Piotroski F, CFO/NI) or ungrounded technical levels."
        ),
        description=(
            "Flagship pilot domain with full end-to-end BCTC forensic decomposition and "
            "quantitative technical analysis for Vietnam (HSX/HNX/UPCOM) and global markets."
        ),
        sample_queries=[
            "Phân tích pháp y BCTC CAMELS ngân hàng VCB và kỹ thuật Fibonacci Ichimoku cổ phiếu FPT RSI 9",
            "Đánh giá của để dành người mua trả tiền trước IDC và cấu trúc nợ HPG",
            "Compare $NVDA and $AAPL SEC 10-K filings, Altman Z-score and technical trend",
        ],
    ),
    "biomedical_health": DomainIntelligencePack(
        id="biomedical_health",
        name="Y Sinh, Dược Học & Lâm Sàng Dựa Trên Bằng Chứng (Evidence-Based Medicine)",
        maturity="active_vertical",
        categories=["academic", "news", "general"],
        preferred_engines=["pubmed", "semanticscholar", "arxiv", "wikipedia", "searxng", "ddg"],
        essence_dimensions=[
            "PICO Clinical Framing: Population/Patient profile, Intervention/Exposure, Comparator/Placebo, Primary & Secondary Outcomes",
            "Evidence Hierarchy Grading: Systematic Review / Cochrane Meta-Analysis > Phase III RCT > Prospective Cohort > Case-Control > In-vitro/Preclinical",
            "Mechanism of Action & Pharmacokinetics: Molecular target, signaling pathway, bioavailability, half-life, drug-drug interactions",
            "Safety, Contraindications & Statistical Rigor: Hazard Ratio (HR), Odds Ratio (OR), 95% Confidence Intervals, NNT/NNH, and mandatory clinical disclaimers",
        ],
        verification_rubric=[
            "Does the synthesis separate peer-reviewed clinical trial evidence (PubMed/PMC) from anecdotal claims?",
            "Are study design, sample size, or biological mechanisms explained clearly with [n] citations?",
            "Are safety precautions, contraindications, and medical disclaimers explicitly stated?",
        ],
        council_critic_prompt=(
            "Audit each candidate as a Clinical Evidence & Biomedical Reviewer. "
            "Verify PICO clarity, evidence hierarchy (Meta-analysis/RCT vs preclinical), "
            "and presence of safety disclaimers."
        ),
        description=(
            "Deep biomedical and clinical research domain powered by PubMed E-utilities, "
            "Semantic Scholar, and PICO evidence grading."
        ),
        sample_queries=[
            "Mechanism of action and Phase III clinical trial outcomes of GLP-1 receptor agonists",
            "Tác dụng sinh học và bằng chứng lâm sàng về kiểm soát đường huyết HbA1c",
        ],
    ),
    "technology_ai": DomainIntelligencePack(
        id="technology_ai",
        name="Kiến Trúc Phần Mềm, AI Engineering & Bảo Mật Hệ Thống",
        maturity="active_vertical",
        categories=["developer", "academic", "general"],
        preferred_engines=[
            "github",
            "stackoverflow",
            "huggingface",
            "arxiv",
            "reddit",
            "searxng",
            "ddg",
        ],
        essence_dimensions=[
            "System Architecture & Bottlenecks: Concurrency model, memory layout, I/O vs CPU bound, distributed failure modes",
            "Algorithmic & Computational Complexity: Time complexity O(·), space complexity, latency percentiles (p50/p95/p99), throughput",
            "Security & Attack Surface: OWASP / CWE vulnerabilities, SSRF/injection vectors, sandboxing, authentication & zero-trust boundaries",
            "Production Trade-offs & Reproducibility: Version compatibility, operational overhead, verified code patterns",
        ],
        verification_rubric=[
            "Does the response explain the underlying architectural root cause rather than just copying a surface snippet?",
            "Are performance trade-offs (latency, memory, scalability) and edge cases addressed?",
            "Are API names, library versions, and benchmark figures grounded in citations [n]?",
        ],
        council_critic_prompt=(
            "Audit each candidate as a Principal Systems Architect & Security Engineer. "
            "Check root-cause accuracy, algorithmic trade-offs, and code/version correctness."
        ),
        description=(
            "Deep software engineering, AI/ML model hubs, and distributed systems domain "
            "grounded in GitHub, StackOverflow, HuggingFace, and arXiv."
        ),
        sample_queries=[
            "Compare pgvector HNSW vs IVFFlat indexing memory and recall trade-offs in PostgreSQL",
            "Kiến trúc Dream-RSI và LLM Council cross-review giảm hallucination như thế nào?",
        ],
    ),
    "academic_research": DomainIntelligencePack(
        id="academic_research",
        name="Nghiên Cứu Khoa Học Cơ Bản & Học Thuật Liên Ngành",
        maturity="active_vertical",
        categories=["academic", "reference", "general"],
        preferred_engines=["arxiv", "semanticscholar", "pubmed", "wikipedia", "searxng", "ddg"],
        essence_dimensions=[
            "Theoretical First Principles: Mathematical formulation, axioms, conservation laws, or formal proofs",
            "Experimental & Dataset Validity: Benchmark construction, ablation studies, baseline fairness, reproducibility",
            "Cross-Paper Consensus vs Contradictions: Seminal papers vs modern SOTA critiques, citation lineage",
            "Open Problems & Future Directions: Unresolved edge cases, scaling laws, theoretical bounds",
        ],
        verification_rubric=[
            "Are theoretical claims and empirical numbers accurately attributed to primary literature [n]?",
            "Does the synthesis highlight methodology limitations and ablation insights?",
        ],
        council_critic_prompt=(
            "Audit each candidate as a Peer Reviewer for a top-tier scientific journal. "
            "Reward methodological rigor, ablation analysis, and exact citation grounding."
        ),
        description=(
            "Cross-disciplinary scientific literature discovery and methodological critique "
            "across arXiv, Semantic Scholar, PubMed, and Wikipedia."
        ),
        sample_queries=[
            "Mathematical formulation and scaling laws of Mixture-of-Experts transformers",
        ],
    ),
    "legal_macro_policy": DomainIntelligencePack(
        id="legal_macro_policy",
        name="Kinh Tế Vĩ Mô, Chính Sách Tiền Tệ & Pháp Lý Doanh Nghiệp",
        maturity="extensible_pack",
        categories=["financial", "news", "general"],
        preferred_engines=["stooq", "sec_edgar", "google_news", "wikipedia", "searxng", "ddg"],
        essence_dimensions=[
            "Macroeconomic Transmission Channels: Central bank policy rates (Fed/SBV), FX reserves, bond yields, CPI inflation, GDP components",
            "Regulatory & Legal Hierarchy: Statutory caps, disclosure mandates, cross-border tariffs, compliance timelines",
            "Second-Order Sectoral Impacts: How macro/policy shifts propagate to banking liquidity, manufacturing exports, and asset prices",
        ],
        verification_rubric=[
            "Does the analysis trace causal transmission mechanisms (e.g. bond yields -> FX -> credit growth) with data [n]?",
            "Are regulatory rules or macro indicators cited accurately with dates/periods?",
        ],
        council_critic_prompt=(
            "Audit each candidate as a Macroeconomic Strategist & Regulatory Counsel. "
            "Check causal transmission chains and date-specific macro data accuracy."
        ),
        description=(
            "Macroeconomic indicators, central bank policy transmission, and corporate "
            "regulatory analysis."
        ),
        sample_queries=[
            "Tác động của tỷ giá USD/VND, lợi suất trái phiếu Mỹ và giá vàng XAUUSD tới vĩ mô",
        ],
    ),
    "eastern_metaphysics": DomainIntelligencePack(
        id="eastern_metaphysics",
        name="Phong Thủy Học, Bát Trạch, Can Chi & Kiến Trúc Phương Đông",
        maturity="active_vertical",
        categories=["fengshui", "reference", "general"],
        preferred_engines=["fengshui", "wikipedia", "searxng", "ddg"],
        essence_dimensions=[
            "Can Chi & Ngũ Hành Nạp Âm: Thiên Can, Địa Chi, 60 Hoa Giáp, Quy luật Tương Sinh / Tương Khắc / Phản Khắc",
            "Bát Trạch Cung Phi Vạn Niên: Đông Tứ Mệnh vs Tây Tứ Mệnh, 4 Cát Tinh (Sinh Khí, Thiên Y, Diên Niên, Phục Vị) & 4 Hung Tinh",
            "Tam Hợp / Tứ Hành Xung & Ứng Dụng Kiến Trúc Khoa Học: Bố cục không gian, ánh sáng, thông gió và tâm lý học môi trường",
        ],
        verification_rubric=[
            "Are Can Chi, Cung Phi, and Ngũ Hành calculated deterministically rather than guessed?",
            "Does the advice balance traditional Eastern geomancy with scientific spatial ergonomics?",
        ],
        council_critic_prompt=(
            "Audit each candidate for exact Can Chi / Ngũ Hành / Bát Trạch calculation accuracy "
            "and practical architectural applicability."
        ),
        description=(
            "Deterministic Eastern metaphysics, Can Chi, Ngũ Hành, and Bát Trạch spatial analysis."
        ),
        sample_queries=[
            "Phân tích phong thủy Bát Trạch, hướng bàn làm việc và màu sắc hợp tuổi Canh Ngọ 1990",
        ],
    ),
    "news_geopolitics": DomainIntelligencePack(
        id="news_geopolitics",
        name="Tin Tức Thời Sự, Địa Chính Trị & Kiểm Chứng Đa Nguồn",
        maturity="active_vertical",
        categories=["news", "general"],
        preferred_engines=["google_news", "reddit", "wikipedia", "searxng", "ddg"],
        essence_dimensions=[
            "Chronological Event Timeline: Primary trigger, verified timestamps, official statements vs secondary commentary",
            "Multi-Source Cross-Verification: Consensus across independent newsrooms, identification of media bias or unconfirmed rumors",
            "Strategic & Societal Implications: Short-term market/public reaction vs structural long-term impact",
        ],
        verification_rubric=[
            "Are event dates, actors, and official statements verified across multiple sources [n]?",
            "Does the synthesis clearly distinguish confirmed facts from evolving speculation?",
        ],
        council_critic_prompt=(
            "Audit each candidate as an Investigative Journalism Editor. "
            "Verify chronological accuracy, source independence, and neutrality."
        ),
        description=(
            "Real-time multi-source news verification, timeline reconstruction, and "
            "geopolitical impact synthesis."
        ),
        sample_queries=[
            "Tin tức thời sự công nghệ AI và kinh tế thế giới nổi bật hôm nay",
        ],
    ),
    "general_discovery": DomainIntelligencePack(
        id="general_discovery",
        name=" Đa Lĩnh Vực Tổng Hợp (Universal Multi-Perspective Synthesis)",
        maturity="active_vertical",
        categories=["general", "reference", "social"],
        preferred_engines=["searxng", "ddg", "wikipedia", "reddit"],
        essence_dimensions=[
            "Core Conceptual Essence: First-principles definition and structural mechanism",
            "Empirical Data & Quantitative Evidence: Measurable statistics and real-world benchmarks",
            "Multi-Angle Perspectives: Pros/cons, trade-offs, and contrasting viewpoints",
        ],
        verification_rubric=[
            "Every factual assertion carries an accurate [n] citation to the numbered context.",
            "The answer addresses both core concepts and practical nuances in the user's language.",
        ],
        council_critic_prompt=(
            "Audit each candidate for factual accuracy, citation faithfulness [n], "
            "clarity, and multi-perspective completeness."
        ),
        description="Universal multi-engine discovery and cross-verified synthesis.",
        sample_queries=[
            "So sánh toàn diện các phương pháp tối ưu hóa hệ thống tìm kiếm phân tán",
        ],
    ),
}


def classify_domain_intelligence(
    query: str,
    requested_domain: str = "auto",
) -> DomainIntelligencePack:
    """Detect the optimal ``DomainIntelligencePack`` for ``query``.

    Supports explicit selection or automatic multi-domain routing, with
    ``financial`` prioritized when financial/forensic/technical signals appear.
    """
    if requested_domain != "auto" and requested_domain in DOMAIN_INTELLIGENCE_REGISTRY:
        return DOMAIN_INTELLIGENCE_REGISTRY[requested_domain]

    from app.orchestration.research import detect_profile

    profile = detect_profile(query, "auto")
    mapping = {
        "financial": "financial",
        "health": "biomedical_health",
        "developer": "technology_ai",
        "academic": "academic_research",
        "fengshui": "eastern_metaphysics",
        "news": "news_geopolitics",
        "comparative": "technology_ai",
        "general": "general_discovery",
    }
    q_lower = query.lower()
    biomed_terms = (
        "lâm sàng",
        "rct",
        "clinical trial",
        "pubmed",
        "fda",
        "tiểu đường",
        "ung thư",
        "dược lý",
        "tác dụng phụ",
        "phác đồ",
        "vaccine",
        "bệnh nhân",
    )
    if any(t in q_lower for t in biomed_terms):
        return DOMAIN_INTELLIGENCE_REGISTRY["biomedical_health"]

    macro_legal_terms = (
        "lãi suất điều hành",
        "chính sách tiền tệ",
        "tỷ giá",
        "lạm phát cpi",
        "nhnn",
        "luật doanh nghiệp",
        "nghị định",
        "thông tư",
        "fed rate",
        "monetary policy",
        "tariff",
    )
    if any(t in q_lower for t in macro_legal_terms) and "bctc" not in q_lower:
        return DOMAIN_INTELLIGENCE_REGISTRY["legal_macro_policy"]

    tech_sec_terms = (
        "distributed consensus",
        "raft",
        "paxos",
        "owasp",
        "cve",
        "lỗ hổng bảo mật",
        "kiến trúc hệ thống",
        "kubernetes",
        "microservice",
    )
    if any(t in q_lower for t in tech_sec_terms):
        return DOMAIN_INTELLIGENCE_REGISTRY["technology_ai"]

    pack_id = mapping.get(profile, "general_discovery")
    return DOMAIN_INTELLIGENCE_REGISTRY[pack_id]


def build_domain_analysis_preview(
    query: str,
    requested_domain: str = "auto",
) -> dict[str, Any]:
    """Inspect how MetaSearch decomposes a query using its Universal Domain Intelligence
    architecture (including custom technical indicator parsing when in the financial pilot).
    """
    from app.llm.prompts import SYSTEM_PROMPT, build_domain_system_prompt

    pack = classify_domain_intelligence(query, requested_domain=requested_domain)
    custom_indicators = parse_custom_indicator_params(query) if pack.id == "financial" else {}
    sys_prompt = build_domain_system_prompt(SYSTEM_PROMPT, query, requested_domain=pack.id)
    return {
        "query": query,
        "detected_domain": pack.id,
        "domain_name": pack.name,
        "maturity": pack.maturity,
        "categories": pack.categories,
        "preferred_engines": pack.preferred_engines,
        "essence_dimensions": pack.essence_dimensions,
        "verification_rubric": pack.verification_rubric,
        "custom_indicator_params": custom_indicators,
        "system_prompt": sys_prompt,
    }
