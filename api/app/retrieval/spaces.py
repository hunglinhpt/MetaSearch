"""Spaces: named collections with domain scope + pinned sources (docs/20 P1-6, v2.6).

A Space (Perplexity-Spaces-lite) lets a key scope search to a set of domains
(``restrict`` drops out-of-scope results via the existing post-cache domain
filter, so it never pollutes the shared search cache) and pin specific URLs
(those become ``BoostRule(kind="url")`` entries folded into ``boosts_sig`` —
again per-key cache-safe).

All DB access is bounded (0.3s) and fail-open: a sick DB means "no space"
(neutral), never a failed search.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from app.core.logging import get_logger
from app.retrieval.boosts import BoostRule
from app.retrieval.canonical import canonicalize

if TYPE_CHECKING:
    from app.db.models import Space

logger = get_logger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_LOAD_TTL_S = 30.0
_space_cache: dict[tuple[str, str], tuple[float, Any]] = {}

PINNED_BOOST_WEIGHT = 0.5


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug[:60] or "space"


def _unique_slug(base: str, existing: set[str]) -> str:
    slug = base
    i = 2
    while slug in existing:
        slug = f"{base}-{i}"
        i += 1
    return slug


async def get_space(owner: str, slug: str) -> Space | None:
    """Load one space by (owner, slug). Fail-open None on any DB problem."""
    key = (owner, slug)
    now = time.monotonic()
    hit = _space_cache.get(key)
    if hit is not None and now - hit[0] < _LOAD_TTL_S:
        return hit[1]
    space = None
    try:
        from sqlalchemy import select

        from app.db.models import Space
        from app.db.session import get_sessionmaker

        async def _read():
            async with get_sessionmaker()() as session:
                return (
                    await session.execute(
                        select(Space).where(Space.api_key_id == owner, Space.slug == slug)
                    )
                ).scalar_one_or_none()

        space = await asyncio.wait_for(_read(), timeout=0.3)
    except Exception as exc:
        # Audit Pass 1 P3: a transient DB failure is NOT "no space" — it must
        # not be negative-cached 30s (that 404s an existing space during a
        # blip, colliding with the fail-closed scope semantics). Return None
        # uncached; a healthy lookup still caches both ways.
        logger.warning("space_load_failed", owner=owner, slug=slug, error=type(exc).__name__)
        return None
    _space_cache[key] = (now, space)
    if len(_space_cache) > 256:
        oldest = min(_space_cache, key=lambda k: _space_cache[k][0])
        _space_cache.pop(oldest, None)
    return space


async def load_space_boosts(owner: str, slug: str, *, settings: Any) -> list[BoostRule]:
    """Pinned URLs of the space become url-boost rules (per-key, cache-keyed)."""
    if not getattr(settings, "spaces_enabled", True):
        return []
    space = await get_space(owner, slug)
    if space is None:
        return []
    rules: list[BoostRule] = []
    for raw in space.pinned_urls or []:
        canon = None
        try:
            canon = canonicalize(raw)
        except Exception:
            canon = None
        if canon:
            rules.append(BoostRule(kind="url", target=canon, weight=PINNED_BOOST_WEIGHT))
    return rules


def space_domain_scope(space: Space) -> list[str]:
    """Normalized domain suffixes for the restrict filter."""
    out: list[str] = []
    for d in space.domains or []:
        d = (d or "").strip().lower().rstrip(".")
        if d.startswith("www."):
            d = d[4:]
        if d and d not in out:
            out.append(d)
    return out


def invalidate(owner: str) -> None:
    for key in [k for k in _space_cache if k[0] == owner]:
        _space_cache.pop(key, None)


def domain_of_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host
