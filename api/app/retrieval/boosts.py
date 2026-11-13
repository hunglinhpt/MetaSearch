"""Per-API-key custom rerank boosts (docs/20 P0-6 — "Goggles-lite").

An operator can pin preferences for their own key: boost/demote a domain
(Brave Goggles style) or boost a keyword. Rules are applied in
``merger.merge`` AFTER the base score + RRF blend, as a multiplicative
(domain) or additive (keyword) factor, clamped so one rule can never
reorder the whole list. Rules are personal: they only ever apply to the
key that created them (tenancy by ``current_api_key_id``), never to other
users, and they are part of the search cache key so boosted and unboosted
answers never share a cache entry.

Storage: ``key_boosts`` table (alembic 0007) with the same graceful
degradation as every other store — DB down means NO boosts (fail-open to
neutral), never a request failure.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.retrieval.models import ScoredResult

logger = get_logger(__name__)

BOOST_KINDS = frozenset({"domain", "keyword", "url"})
# A single rule can move a result by at most this factor; the sum of all
# domain rules is clamped to the same band so one key cannot invert the
# ranking wholesale.
_MAX_DOMAIN_FACTOR = 2.0
_KEYWORD_STEP = 0.2  # each matching keyword rule adds weight * _KEYWORD_STEP


@dataclass(slots=True, frozen=True)
class BoostRule:
    kind: str  # "domain" | "keyword" | "url"
    target: str  # domain suffix ("vnexpress.net"), lowercase keyword/phrase, or canonical URL
    # domain/url: multiplier offset (-0.9..1.0); keyword: additive base
    weight: float


def _norm_domain(domain: str) -> str:
    d = (domain or "").strip().lower().rstrip(".")
    return d[4:] if d.startswith("www.") else d


def rules_signature(rules: list[BoostRule]) -> str:
    """Stable hash folded into the search cache key (per-key personalization
    must never leak across keys through a shared cache entry)."""
    if not rules:
        return ""
    h = hashlib.sha1(usedforsecurity=False)  # cache key, not security
    for r in sorted(rules, key=lambda r: (r.kind, r.target, r.weight)):
        h.update(f"{r.kind}|{r.target}|{r.weight:.4f}\n".encode())
    return h.hexdigest()[:16]


def apply_boosts(item: ScoredResult, query: str, rules: list[BoostRule]) -> int:
    """Apply personal boosts to ``item.score`` in place; returns how many rules hit."""
    if not rules or item.score <= 0.0:
        return 0
    applied = 0
    domain_factor = 1.0
    keyword_add = 0.0
    url_factor = 1.0
    q = query.lower()
    item_domain = _norm_domain(item.domain)
    for rule in rules:
        if rule.kind == "domain":
            t = _norm_domain(rule.target)
            if t and (item_domain == t or item_domain.endswith("." + t)):
                domain_factor += rule.weight
                applied += 1
        elif rule.kind == "keyword" and rule.target and rule.target in q:
            keyword_add += rule.weight
            applied += 1
        elif rule.kind == "url" and rule.target:
            # docs/20 P1-6: pinned Space sources — exact canonical URL match.
            from app.retrieval.canonical import canonicalize

            matched = False
            try:
                matched = canonicalize(item.url) == rule.target or item.canonical_url == rule.target
            except Exception:
                matched = False
            if matched:
                url_factor += rule.weight
                applied += 1
    if applied:
        domain_factor = max(1.0 - _MAX_DOMAIN_FACTOR, min(domain_factor, _MAX_DOMAIN_FACTOR))
        url_factor = max(1.0 - _MAX_DOMAIN_FACTOR, min(url_factor, _MAX_DOMAIN_FACTOR))
        item.score = max(0.0, item.score * domain_factor * url_factor + keyword_add * _KEYWORD_STEP)
        item.boosts_applied = applied
    return applied


# --- loading (DB with fail-open) ---------------------------------------------

_LOAD_TTL_S = 30.0
_cache: dict[str, tuple[float, list[BoostRule]]] = {}


async def load_boosts(owner: str, *, max_per_key: int = 50) -> list[BoostRule]:
    """Load one owner's rules. Any DB problem returns [] (neutral) — boosts are
    a preference layer, never a reason to fail a search."""
    now = time.monotonic()
    hit = _cache.get(owner)
    if hit is not None and now - hit[0] < _LOAD_TTL_S:
        return hit[1]
    rules: list[BoostRule] = []
    try:
        import asyncio

        from sqlalchemy import select

        from app.db.models import KeyBoost
        from app.db.session import get_sessionmaker

        # Bounded: a partitioned DB must never sit in the search critical path
        # (perf lesson from the engine store). Timeout -> no boosts this round.
        async def _read() -> list:
            async with get_sessionmaker()() as session:
                return (
                    (
                        await session.execute(
                            select(KeyBoost)
                            .where(KeyBoost.api_key_id == owner)
                            .order_by(KeyBoost.created_at.asc())
                        )
                    )
                    .scalars()
                    .all()
                )

        rows = await asyncio.wait_for(_read(), timeout=0.3)
        for row in rows[:max_per_key]:
            if row.kind in BOOST_KINDS and row.target:
                rules.append(
                    BoostRule(kind=row.kind, target=row.target.strip(), weight=float(row.weight))
                )
    except Exception as exc:
        logger.warning("boosts_load_failed", owner=owner, error=type(exc).__name__)
        rules = []
    _cache[owner] = (now, rules)
    if len(_cache) > 256:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    return rules


def invalidate(owner: str) -> None:
    _cache.pop(owner, None)
