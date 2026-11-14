"""Multi-signal scoring (docs/03-search-pipeline.md §3, "Scoring v1").

    score = 0.35 x keyword_relevance        BM25-lite over title + snippet
          + 0.25 x consensus                log2(1 + n_engines) / log2(1 + max_engines)
          + 0.15 x engine_weight            weighted average of adapter weights
          + 0.10 x freshness                exp(-age_days / 30), 0.3 when unknown
          + 0.10 x domain_authority         curated institutional trust table (0.5 default)
          + 0.05 x snippet_quality          length & structure trapezoid

All signals are normalized to 0..1 so weights stay meaningful as new signals
(SimHash, vector similarity, ONNX rerank) land in Phase 2.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, datetime

from app.retrieval.models import EngineConfig, ScoredResult

WEIGHTS = {
    "keyword": 0.35,
    "consensus": 0.25,
    "engine_weight": 0.15,
    "freshness_news": 0.10,
    "freshness_other": 0.05,
    "domain_authority": 0.10,
    "snippet": 0.05,
}

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)

# A tiny Vietnamese + English stopword list - BM25-lite only needs the obvious ones.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "at",
        "as",
        "by",
        "from",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "vào",
        "của",
        "và",
        # Pass 1 P2#1: folded forms of base VI stopwords — the stopword match
        # runs on the ORIGINAL token, so unaccented queries ("va", "khong")
        # kept these as scored terms and deflated their scores vs accented
        # twins. Only collision-free forms are added; meaningful unaccented
        # nouns (co/la/cua/mot/do/nam/sao/da...) stay deliberately excluded.
        "va",
        "khong",
        "duoc",
        "nhung",
        "tren",
        "duoi",
        "cac",
        "neu",
        "hoặc",
        "là",
        "cái",
        "gì",
        "thì",
        "có",
        "không",
        "được",
        "những",
        "các",
        "một",
        "nhưng",
        "nếu",
        "khi",
        "để",
        "cho",
        "trong",
        "ngoài",
        "về",
        "trên",
        "dưới",
    ]
)

_TRUSTED_DOMAINS: dict[str, float] = {
    # Financial & Regulatory
    "sec.gov": 0.95,
    "finance.yahoo.com": 0.90,
    "stooq.com": 0.90,
    "iboard.ssi.com.vn": 0.92,
    "ssi.com.vn": 0.90,
    "s.cafef.vn": 0.88,
    "cafef.vn": 0.85,
    "finance.vietstock.vn": 0.90,
    "vietstock.vn": 0.88,
    "hsx.vn": 0.95,
    "hnx.vn": 0.95,
    "fireant.vn": 0.85,
    "simplize.vn": 0.85,
    # Academic & Bio-Medical
    "arxiv.org": 0.95,
    "pubmed.ncbi.nlm.nih.gov": 0.95,
    "ncbi.nlm.nih.gov": 0.95,
    "semanticscholar.org": 0.92,
    # Developer & AI
    "github.com": 0.90,
    "stackoverflow.com": 0.90,
    "huggingface.co": 0.90,
    "docs.python.org": 0.92,
    "postgresql.org": 0.92,
    # Reference & Knowledge
    "en.wikipedia.org": 0.85,
    "vi.wikipedia.org": 0.85,
    "wikipedia.org": 0.85,
    "metasearch.local": 0.88,
}


def fold_diacritics(text: str) -> str:
    """docs/20 P2-3 (vi-first): strip Vietnamese diacritics so "hòa" matches
    "hoa", "đánh giá" matches "danh gia" — users type both forms interchangeably."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    # đ/Đ has no decomposition — map it explicitly.
    return stripped.replace("đ", "d").replace("Đ", "D")


# Extended Vietnamese stopwords (P2-3): single-word function words, listed in
# BOTH accented and unaccented form, matched against the ORIGINAL token (see
# tokenize). Entries that collide with meaningful unaccented nouns are
# deliberately left out ("sao"=ngôi sao, "do"=đô, "day"=dây, "ma"=ma/mã,
# "bac"=bắc, "nam"=năm) — folding the stopword list would make "cổ"->"co" hit
# the folded "có"->"co" and swallow the meaningful word (docs/24 §8 P3 fix).
_EXTRA_VI_STOPWORDS = frozenset(
    [
        "ai",
        "nay",
        "kia",
        "theo",
        "rất",
        "rat",
        "nhiều",
        "nhieu",
        "ít",
        "it",
        "hơn",
        "hon",
        "nhất",
        "nhat",
        "cùng",
        "cung",
        "đã",
        "da",
        "đang",
        "dang",
        "sẽ",
        "se",
        "vẫn",
        "van",
        "như",
        "nhu",
        "giống",
        "giong",
        "khác",
        "khac",
    ]
)


# Matched on the ORIGINAL (pre-fold) token — never on the folded form.
_STOPWORDS_ALL = frozenset(_STOPWORDS | _EXTRA_VI_STOPWORDS)


def tokenize(text: str, *, fold: bool = True) -> list[str]:
    """Lowercase NFC tokens, stopwords removed.

    docs/20 P2-3: Vietnamese diacritics are folded by default (``fold=True``)
    so accented and unaccented queries match the same documents. The stopword
    check runs on the ORIGINAL token BEFORE folding: folding the stopword list
    instead would collide ("cổ" and "có" both fold to "co") and silently drop
    meaningful words like "cổ phiếu" or "thị trường" (docs/24 §8 P3 fix).
    """
    normalized = unicodedata.normalize("NFC", text).lower()
    tokens = [t for t in _TOKEN_RE.findall(normalized) if len(t) > 1 and t not in _STOPWORDS_ALL]
    if fold:
        return [fold_diacritics(t) for t in tokens]
    return tokens


def keyword_relevance(query_terms: list[str], title: str, snippet: str) -> float:
    """Share of query terms matched, title worth 2x snippet (BM25-lite proxy).

    Tokens prefix-match terms of >= 4 characters so "postgres" hits
    "postgresql" — a cheap stand-in for stemming.
    """
    if not query_terms:
        return 0.0
    title_tokens = set(tokenize(title))
    snippet_tokens = set(tokenize(snippet))

    def _hits(term: str, tokens: set[str]) -> bool:
        if term in tokens:
            return True
        return len(term) >= 4 and any(t.startswith(term) for t in tokens)

    matched = 0
    for term in query_terms:
        weight = 2 if _hits(term, title_tokens) else 0
        weight += 1 if _hits(term, snippet_tokens) else 0
        matched += min(weight, 2)
    return matched / (2 * len(query_terms))


def consensus_score(n_engines: int, max_engines: int) -> float:
    if max_engines <= 1:
        return 1.0 if n_engines >= 1 else 0.0
    return math.log2(1 + n_engines) / math.log2(1 + max_engines)


def engine_weight_score(adapters: list[str], registry: dict[str, EngineConfig]) -> float:
    weights = [registry[a].weight for a in adapters if a in registry]
    if not weights:
        return 0.0
    return min(sum(weights) / len(weights) / 2.0, 1.0)


def freshness(
    published_at: datetime | None,
    now: datetime | None = None,
    *,
    half_life_days: float | None = None,
) -> float:
    """Exponential decay of the freshness signal.

    docs/20 P1-8: ``half_life_days`` switches to a true half-life curve
    ``0.5 ** (age / half_life)``; ``None`` keeps the legacy ``exp(-age/30)``
    (which is exactly half-life ``30 * ln2 ≈ 20.794`` days). News and
    non-news results can therefore decay at different rates — stale
    non-news sources are downranked automatically but gently.
    """
    if published_at is None:
        return 0.3
    now = now or datetime.now(UTC)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    age_days = max((now - published_at).total_seconds() / 86400, 0.0)
    if half_life_days is not None and half_life_days > 0:
        return 0.5 ** (age_days / half_life_days)
    return math.exp(-age_days / 30.0)


def snippet_quality(snippet: str) -> float:
    """Trapezoid over snippet length: best around 60-320 chars; structured
    markdown reports (tables/bold metrics) retain high quality up to 2000 chars."""
    stripped = snippet.strip()
    n = len(stripped)
    if n == 0:
        return 0.0
    if n < 60:
        return n / 60.0
    if n <= 320:
        return 1.0
    if "|" in stripped or "**" in stripped or "\n-" in stripped:
        return 0.95
    return max(1.0 - (n - 320) / 1200.0, 0.35)


def domain_authority(domain: str) -> float:
    """Curated trust table wins; otherwise the P2-7 reputation tier base."""
    from app.retrieval.tiers import tier_score

    clean = domain.lower().removeprefix("www.")
    if clean in _TRUSTED_DOMAINS or domain.lower() in _TRUSTED_DOMAINS:
        return _TRUSTED_DOMAINS.get(clean, _TRUSTED_DOMAINS.get(domain.lower(), 0.5))
    return tier_score(domain)


def apply_signals(
    item: ScoredResult,
    *,
    query: str,
    registry: dict[str, EngineConfig],
    max_engines: int,
    now: datetime | None = None,
    news: bool = False,
    # docs/20 P1-8: configurable half-lives (days); None = legacy exp(-age/30).
    news_half_life_days: float | None = None,
    other_half_life_days: float | None = None,
) -> float:
    """Fill the signal fields of ``item`` and return the combined score."""
    query_terms = tokenize(query)
    kw = keyword_relevance(query_terms, item.title, item.snippet)
    cons = consensus_score(item.consensus, max_engines)
    ew = engine_weight_score(item.adapters, registry)
    half_life = news_half_life_days if news else other_half_life_days
    fresh = (
        0.0
        if item.data_kind == "computed"
        else freshness(item.published_at, now, half_life_days=half_life)
    )
    da = domain_authority(item.domain)
    sq = snippet_quality(item.snippet)
    fresh_weight = WEIGHTS["freshness_news"] if news else WEIGHTS["freshness_other"]

    item.keyword_relevance = kw
    item.consensus_score = cons
    item.engine_weight_score = ew
    item.freshness = fresh
    item.domain_authority = da
    item.snippet_quality = sq
    from app.retrieval.tiers import domain_tier

    item.domain_tier = domain_tier(item.domain)

    return (
        WEIGHTS["keyword"] * kw
        + WEIGHTS["consensus"] * cons
        + WEIGHTS["engine_weight"] * ew
        + fresh_weight * fresh
        + WEIGHTS["domain_authority"] * da
        + WEIGHTS["snippet"] * sq
    )


def chunk_relevance(query: str, content: str) -> float:
    """Keyword overlap for chunk-level heuristic rerank (0..1)."""
    return keyword_relevance(tokenize(query), content, "")


def heuristic_chunk_score(query: str, content: str, result_score: float, position: int) -> float:
    """0.7 x source-result score (decayed by chunk position) + 0.3 x keyword overlap."""
    decay = 1.0 / (1.0 + 0.1 * max(position - 1, 0))
    return 0.7 * result_score * decay + 0.3 * chunk_relevance(query, content)
