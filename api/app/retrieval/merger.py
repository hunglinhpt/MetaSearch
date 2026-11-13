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
from app.retrieval.scoring import apply_signals
from app.retrieval.simhash import is_near_duplicate, simhash64

logger = get_logger(__name__)

_DEEP_VERTICAL_ADAPTERS = frozenset({"viet_stock", "stooq", "fengshui", "sec_edgar"})

# docs/20 P0-1: Reciprocal Rank Fusion constant (standard RRF k=60).
RRF_K = 60


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

    merged: list[ScoredResult] = []
    group_rrf: dict[str, float] = {}
    for canonical_url, group in groups.items():
        # Deterministic primary even when ranks tie (fan-out arrival order varies).
        group.sort(key=lambda r: (r.rank, r.url))
        # RRF (docs/20 P0-1): sum over EVERY raw occurrence (multi-query fan-out
        # included) of adapter_weight / (k + rank). Engine weight comes from the
        # registry so an operator-tuned engine counts more in the fusion too.
        rrf = 0.0
        for raw in group:
            cfg = registry.get(raw.adapter)
            w = cfg.weight if cfg is not None else 1.0
            rrf += w / (RRF_K + raw.rank + 1)
        group_rrf[canonical_url] = rrf
        primary = group[0]
        adapters = sorted({r.adapter for r in group})
        engines = sorted({e for r in group for e in r.source_engines}) or list(adapters)
        merged.append(
            ScoredResult(
                url=primary.url,
                canonical_url=canonical_url,
                title=primary.title,
                snippet=max((r.snippet for r in group), key=len, default=""),
                domain=(urlsplit(primary.url).hostname or "").lower(),
                adapters=adapters,
                engines=engines,
                consensus=len(engines),
                best_rank=primary.rank,
                adapter_score=primary.adapter_score,
                published_at=next((r.published_at for r in group if r.published_at), None),
                source_url=primary.source_url,
                source_name=primary.source_name,
                as_of=next((r.as_of for r in group if r.as_of), primary.as_of),
                data_kind=primary.data_kind,
            )
        )

    max_engines = max((item.consensus for item in merged), default=1)
    max_rrf = max(group_rrf.values(), default=0.0)
    rw = min(max(rrf_weight, 0.0), 1.0)
    for item in merged:
        base = apply_signals(
            item,
            query=query,
            registry=registry,
            max_engines=max_engines,
            now=now,
            news=news,
            news_half_life_days=news_half_life_days,
            other_half_life_days=other_half_life_days,
        )
        item.rrf_score = (group_rrf[item.canonical_url] / max_rrf) if max_rrf > 0 else 0.0
        item.score = (1.0 - rw) * base + rw * item.rrf_score
        if boosts:
            apply_boosts(item, query, boosts)

    merged.sort(key=lambda r: (-r.score, r.best_rank, r.canonical_url))
    return _drop_near_duplicates(merged)


def _drop_near_duplicates(results: list[ScoredResult]) -> list[ScoredResult]:
    """Fold SimHash near-duplicates into their best-ranked twin.

    Mirrors and syndicated copies share content but not canonical URLs; their
    engine signals are merged into the kept result and the copy is dropped.
    """
    kept: list[ScoredResult] = []
    kept_hashes: list[int] = []
    for item in results:
        fingerprint = simhash64(f"{item.title} {item.snippet}")
        twin = next(
            (
                i
                for i, kept_hash in enumerate(kept_hashes)
                if is_near_duplicate(fingerprint, kept_hash)
            ),
            None,
        )
        if twin is None:
            kept.append(item)
            kept_hashes.append(fingerprint)
            continue
        target = kept[twin]
        target.engines = sorted(set(target.engines) | set(item.engines))
        target.adapters = sorted(set(target.adapters) | set(item.adapters))
        target.consensus = len(target.engines)
        if not target.published_at and item.published_at:
            target.published_at = item.published_at
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
