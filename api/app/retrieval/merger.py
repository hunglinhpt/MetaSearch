"""Merge adapter outputs: canonicalize, exact-dedupe, consensus, diversity cap.

Near-duplicate SimHash filtering arrives in Phase 2 (docs/12-roadmap-plan.md).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit

from app.retrieval.canonical import canonicalize
from app.retrieval.models import EngineConfig, RawResult, ScoredResult
from app.retrieval.scoring import apply_signals


def merge(
    raws: list[RawResult],
    *,
    query: str,
    registry: dict[str, EngineConfig],
    now: datetime | None = None,
    news: bool = False,
) -> list[ScoredResult]:
    """Deduplicate ``raws`` by canonical URL, score groups, sort desc."""
    groups: dict[str, list[RawResult]] = {}
    for raw in raws:
        groups.setdefault(canonicalize(raw.url), []).append(raw)

    merged: list[ScoredResult] = []
    for canonical_url, group in groups.items():
        # Deterministic primary even when ranks tie (fan-out arrival order varies).
        group.sort(key=lambda r: (r.rank, r.url))
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
            )
        )

    max_engines = max((item.consensus for item in merged), default=1)
    for item in merged:
        item.score = apply_signals(
            item, query=query, registry=registry, max_engines=max_engines, now=now, news=news
        )

    merged.sort(key=lambda r: (-r.score, r.best_rank, r.canonical_url))
    return merged


def diversity_cap(
    results: list[ScoredResult],
    *,
    max_per_domain: int = 2,
    limit: int = 10,
) -> list[ScoredResult]:
    """Keep at most ``max_per_domain`` results per domain within the top ``limit``.

    Over-cap results are not dropped: they fall back to a reserve pool that
    backfills any remaining slots after the first pass.
    """
    picked: list[ScoredResult] = []
    reserve: list[ScoredResult] = []
    per_domain: Counter[str] = Counter()

    for item in results:
        if len(picked) >= limit:
            reserve.append(item)
            continue
        if per_domain[item.domain] >= max_per_domain:
            reserve.append(item)
            continue
        per_domain[item.domain] += 1
        picked.append(item)

    if len(picked) < limit:
        picked.extend(reserve[: limit - len(picked)])
    return picked[:limit]
