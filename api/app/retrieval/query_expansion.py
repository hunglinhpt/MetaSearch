"""Multi-query expansion (docs/20 P0-3, v2.5.0).

For deep or long queries, one cheap LLM call paraphrases the query into 1-2
variants; each variant fans out to the GENERAL engines only (bounded cost)
and all raw hits are fused by RRF in merger.merge — the standard
multi-query + RRF retriever (HyDE-adjacent recall boost).

Failure semantics: any LLM error, disabled setting, or short query returns []
and the search proceeds with the original query alone. Expansion never fails
a request and never runs when no LLM is configured.
"""

from __future__ import annotations

import re

from app.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_EXPAND_SYSTEM = (
    "You rewrite search queries. Given a search query, output up to {n} alternative "
    "paraphrases that would retrieve the same information from a web search engine. "
    "Keep the language of the query. One per line, no numbering, no quotes, no commentary."
)

_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def parse_variants(text: str, original: str, *, max_variants: int) -> list[str]:
    """Extract clean variant lines; dedupe case-insensitively, drop the original."""
    out: list[str] = []
    seen = {original.strip().lower()}
    for line in (text or "").splitlines():
        v = _LINE_RE.sub("", line).strip().strip('"“”')
        if not v or len(v) < 3 or len(v) > 500:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
        if len(out) >= max_variants:
            break
    return out


async def expand_query(
    gateway,
    query: str,
    *,
    lang: str | None,
    settings: Settings,
    force: bool = False,
) -> list[str]:
    """Return up to ``query_expansion_max_variants`` paraphrases ([] on any failure).

    ``force=True`` (deep mode) expands even short queries; otherwise only
    queries with at least ``query_expansion_min_words`` words are expanded.
    """
    if not settings.query_expansion_enabled:
        return []
    words = len(query.split())
    if not force and words < settings.query_expansion_min_words:
        return []
    provider = getattr(gateway, "provider", "none")
    if provider == "none":
        return []
    try:
        text, _usage = await gateway.complete(
            [
                {
                    "role": "system",
                    "content": _EXPAND_SYSTEM.format(n=settings.query_expansion_max_variants),
                },
                {"role": "user", "content": query + (f"\nlang: {lang}" if lang else "")},
            ],
            max_tokens=160,
            task="expand",
        )
    except Exception as exc:
        logger.info("query_expansion_failed", error=type(exc).__name__)
        return []
    return parse_variants(text, query, max_variants=settings.query_expansion_max_variants)


def expansion_signature(query: str, variants: list[str]) -> str:
    """Folded into the search cache key so expanded and plain searches never
    share a cache entry (the variant set changes the result set)."""
    import hashlib

    if not variants:
        return ""
    h = hashlib.sha1(usedforsecurity=False)  # cache key, not security
    h.update(query.encode("utf-8"))
    for v in variants:
        h.update(b"\n")
        h.update(v.encode("utf-8"))
    return h.hexdigest()[:16]
