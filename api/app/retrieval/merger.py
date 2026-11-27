"""Merge adapter outputs: canonicalize, exact-dedupe, near-dup SimHash, consensus,
diversity cap.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit

from app.core.logging import get_logger
from app.retrieval.boosts import BoostRule, apply_boosts
from app.retrieval.canonical import canonicalize
from app.retrieval.models import EngineConfig, RawResult, ScoredResult
from app.retrieval.relevance_gate import off_topic_flags
from app.retrieval.scoring import apply_signals, compute_batch_idf, tokenize, tokenize_dual
from app.retrieval.simhash import is_near_duplicate, simhash64

logger = get_logger(__name__)

_DEEP_VERTICAL_ADAPTERS = frozenset({"viet_stock", "stooq", "fengshui", "sec_edgar", "vn_wire"})
_WRAPPER_HOSTS = frozenset({"news.google.com", "www.bing.com", "bing.com"})

# docs/20 P0-1: Reciprocal Rank Fusion constant (standard RRF k=60).
RRF_K = 60


def _effective_domain(url: str, source_url: str | None) -> str:
    """Resolve real publisher host when a news aggregator wraps the link."""
    host = (urlsplit(url).hostname or "").lower()
    if host in _WRAPPER_HOSTS and source_url:
        src_host = (urlsplit(source_url).hostname or "").lower()
        if src_host:
            return src_host
    return host


def _title_jaccard(tokens_a: set[str], tokens_b: set[str]) -> float:
    if len(tokens_a) < 4 or len(tokens_b) < 4:
        return 0.0
    union = len(tokens_a | tokens_b)
    return len(tokens_a & tokens_b) / union if union else 0.0


def merge(
    raws: list[RawResult],
    *,
    query: str,
    registry: dict[str, EngineConfig],
    now: datetime | None = None,
    news: bool = False,
    # docs/20 P0-1: weight of the RRF signal in the final blend (0 = off).
    rrf_weight: float = 0.3,
    # docs/20 P0-6: per-key personal boost rules, applied after the blend.
    boosts: list[BoostRule] | None = None,
    # docs/20 P1-8: freshness half-lives in days (None = legacy exp(-age/30)).
    news_half_life_days: float | None = None,
    other_half_life_days: float | None = None,
) -> list[ScoredResult]:
    """Deduplicate ``raws`` by canonical URL, score groups, sort desc."""
    groups: dict[str, list[RawResult]] = {}
    for raw in raws:
        try:
            canon = canonicalize(raw.url)
        except Exception as exc:
            logger.warning("merger_malformed_url_dropped", url=raw.url, error=str(exc))
            continue
        groups.setdefault(canon, []).append(raw)

    # Normalize per-adapter minimum rank so 0-based and 1-based adapters rank fairly.
    adapter_min_rank: dict[str, int] = {}
    for raw in raws:
        prev = adapter_min_rank.get(raw.adapter)
        if prev is None or raw.rank < prev:
            adapter_min_rank[raw.adapter] = raw.rank

    merged: list[ScoredResult] = []
    group_rrf: dict[str, float] = {}
    for canonical_url, group in groups.items():
        # Deterministic primary even when ranks tie (fan-out arrival order varies).
        group.sort(key=lambda r: (r.rank, r.url))
        # RRF (docs/20 P0-1): sum over EVERY raw occurrence (multi-query fan-out
        # included) of adapter_weight / (k + normalized_rank + 1).
        rrf = 0.0
        for raw in group:
            cfg = registry.get(raw.adapter)
            w = cfg.weight if cfg is not None else 1.0
            norm_rank = max(0, raw.rank - adapter_min_rank.get(raw.adapter, 0))
            rrf += w / (RRF_K + norm_rank + 1)
        group_rrf[canonical_url] = rrf
        primary = group[0]
        adapters = sorted({r.adapter for r in group})
        engines = sorted({e for r in group for e in r.source_engines}) or list(adapters)
        resolved_source_url = primary.source_url or next(
            (r.source_url for r in group if r.source_url), None
        )
        resolved_source_name = primary.source_name or next(
            (r.source_name for r in group if r.source_name), None
        )
        merged.append(
            ScoredResult(
                url=primary.url,
                canonical_url=canonical_url,
                title=primary.title,
                snippet=max((r.snippet for r in group), key=len, default=""),
                domain=_effective_domain(primary.url, resolved_source_url),
                adapters=adapters,
                engines=engines,
                consensus=len(engines),
                best_rank=primary.rank,
                adapter_score=max(
                    (r.adapter_score for r in group if r.adapter_score is not None),
                    default=primary.adapter_score,
                ),
                published_at=next((r.published_at for r in group if r.published_at), None),
                source_url=resolved_source_url,
                source_name=resolved_source_name,
                as_of=next((r.as_of for r in group if r.as_of), primary.as_of),
                data_kind=primary.data_kind,
            )
        )

    max_engines = max((item.consensus for item in merged), default=1)
    max_rrf = max(group_rrf.values(), default=0.0)
    rw = min(max(rrf_weight, 0.0), 1.0)
    query_terms, accented_terms = tokenize_dual(query)
    batch_idf = compute_batch_idf(query_terms, [(m.title, m.snippet) for m in merged])
    for item in merged:
        base = apply_signals(
            item,
            query=query,
            query_terms=query_terms,
            accented_terms=accented_terms,
            idf=batch_idf,
            registry=registry,
            max_engines=max_engines,
            now=now,
            news=news,
            news_half_life_days=news_half_life_days,
            other_half_life_days=other_half_life_days,
        )
        item.rrf_score = (group_rrf[item.canonical_url] / max_rrf) if max_rrf > 0 else 0.0
        adapter_bonus = (
            min(max(float(item.adapter_score), 0.0), 1.0) * 0.03
            if item.adapter_score is not None
            else 0.0
        )
        raw_score = (1.0 - rw) * base + rw * item.rrf_score + adapter_bonus
        if len(query_terms) >= 2 and item.keyword_relevance == 0.0 and item.data_kind != "computed":
            raw_score *= 0.45
        item.score = min(raw_score, 1.0)
        if boosts:
            apply_boosts(item, query, boosts)

    # docs/36 §20 Q1: boilerplate matches ("…how does it differ from…") rank below
    # every on-topic result instead of filling the page. Same factor as the
    # zero-relevance penalty above; never applied twice.
    for item, off in zip(merged, off_topic_flags(query, merged), strict=True):
        if off and item.keyword_relevance > 0.0 and item.data_kind != "computed":
            item.score *= 0.45

    merged.sort(key=lambda r: (-r.score, r.best_rank, r.canonical_url))
    return _drop_near_duplicates(merged)


def _drop_near_duplicates(results: list[ScoredResult]) -> list[ScoredResult]:
    """Fold SimHash or near-identical syndicated title duplicates into their
    best-ranked twin, preserving engine corroboration and re-sorting cleanly.
    """
    kept: list[ScoredResult] = []
    kept_hashes: list[int] = []
    kept_title_tokens: list[set[str]] = []
    merged_any = False
    for item in results:
        fingerprint = simhash64(f"{item.title} {item.snippet}")
        title_toks = set(tokenize(item.title))
        twin = next(
            (
                i
                for i, (kept_hash, kept_tt) in enumerate(
                    zip(kept_hashes, kept_title_tokens, strict=True)
                )
                if is_near_duplicate(fingerprint, kept_hash)
                or _title_jaccard(title_toks, kept_tt) >= 0.72
            ),
            None,
        )
        if twin is None:
            kept.append(item)
            kept_hashes.append(fingerprint)
            kept_title_tokens.append(title_toks)
            continue
        target = kept[twin]
        prev_consensus = target.consensus
        target.engines = sorted(set(target.engines) | set(item.engines))
        target.adapters = sorted(set(target.adapters) | set(item.adapters))
        target.consensus = len(target.engines)
        if not target.published_at and item.published_at:
            target.published_at = item.published_at
        if len(item.snippet) > len(target.snippet):
            target.snippet = item.snippet
        if target.consensus > prev_consensus:
            target.score = min(target.score + 0.02 * (target.consensus - prev_consensus), 1.0)
            merged_any = True
    if merged_any:
        kept.sort(key=lambda r: (-r.score, r.best_rank, r.canonical_url))
    return kept


def diversity_cap(
    results: list[ScoredResult],
    *,
    max_per_domain: int = 2,
    limit: int = 10,
) -> list[ScoredResult]:
    """Keep at most ``max_per_domain`` results per domain within the top ``limit``.

    Multi-aspect quantitative reports from deep vertical adapters (e.g. VietStock
    BCTC Forensics + Technical Analysis + Dividends) are granted up to 4 slots per
    domain so multi-facet or multi-ticker queries do not lose core reports.
    Over-cap results fall back to a reserve pool that backfills any remaining slots.
    """
    picked: list[ScoredResult] = []
    reserve: list[ScoredResult] = []
    per_domain: Counter[str] = Counter()

    for item in results:
        if len(picked) >= limit:
            reserve.append(item)
            continue
        domain_cap = (
            max(max_per_domain, 4)
            if any(a in _DEEP_VERTICAL_ADAPTERS for a in item.adapters)
            else max_per_domain
        )
        if per_domain[item.domain] >= domain_cap:
            reserve.append(item)
            continue
        per_domain[item.domain] += 1
        picked.append(item)

    if len(picked) < limit:
        picked.extend(reserve[: limit - len(picked)])
    return picked[:limit]
