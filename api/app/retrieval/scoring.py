"""Multi-signal scoring (docs/03-search-pipeline.md §3, "Scoring v1").

    score = 0.35 x keyword_relevance        BM25-lite over title + snippet
          + 0.25 x consensus                log2(1 + n_engines) / log2(1 + max_engines)
          + 0.15 x engine_weight            weighted average of adapter weights
          + 0.10 x freshness                exp(-age_days / 30), 0.3 when unknown
          + 0.10 x domain_authority         neutral 0.5 until a trust table exists
          + 0.05 x snippet_quality          length-shaped trapezoid

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

_TRUSTED_DOMAINS: dict[str, float] = {}


def tokenize(text: str) -> list[str]:
    """Lowercase NFC tokens, stopwords removed."""
    normalized = unicodedata.normalize("NFC", text).lower()
    return [t for t in _TOKEN_RE.findall(normalized) if len(t) > 1 and t not in _STOPWORDS]


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


def freshness(published_at: datetime | None, now: datetime | None = None) -> float:
    if published_at is None:
        return 0.3
    now = now or datetime.now(UTC)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    age_days = max((now - published_at).total_seconds() / 86400, 0.0)
    return math.exp(-age_days / 30.0)


def snippet_quality(snippet: str) -> float:
    """Trapezoid over snippet length: best around 60-320 chars."""
    n = len(snippet.strip())
    if n == 0:
        return 0.0
    if n < 60:
        return n / 60.0
    if n <= 320:
        return 1.0
    return max(1.0 - (n - 320) / 680.0, 0.2)


def domain_authority(domain: str) -> float:
    return _TRUSTED_DOMAINS.get(domain.lower(), 0.5)


def apply_signals(
    item: ScoredResult,
    *,
    query: str,
    registry: dict[str, EngineConfig],
    max_engines: int,
    now: datetime | None = None,
    news: bool = False,
) -> float:
    """Fill the signal fields of ``item`` and return the combined score."""
    query_terms = tokenize(query)
    kw = keyword_relevance(query_terms, item.title, item.snippet)
    cons = consensus_score(item.consensus, max_engines)
    ew = engine_weight_score(item.adapters, registry)
    fresh = freshness(item.published_at, now)
    da = domain_authority(item.domain)
    sq = snippet_quality(item.snippet)
    fresh_weight = WEIGHTS["freshness_news"] if news else WEIGHTS["freshness_other"]

    item.keyword_relevance = kw
    item.consensus_score = cons
    item.engine_weight_score = ew
    item.freshness = fresh
    item.domain_authority = da
    item.snippet_quality = sq

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
