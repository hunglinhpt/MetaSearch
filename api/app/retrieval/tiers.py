"""Domain reputation tiers (docs/20 P2-7 — Kagi/Brave-style source tiers).

Each domain maps to a tier with a base authority score; ``scoring.domain_authority``
uses it (the curated ``_TRUSTED_DOMAINS`` table still wins when present, so an
operator-tuned trust value is never overridden by a tier default). Tiers are
also exposed on every search result (``domain_tier``) so agents/UI can filter
or display source quality — "official/primary" vs aggregator vs UGC.

Tiers (higher = more authoritative):
- ``official``   0.97  government / regulator / exchange / central bank
- ``primary``    0.93  issuer filings, original publisher, first-party data
- ``reference``  0.88  encyclopedias, standards bodies, official docs
- ``research``   0.90  journals / preprint servers / scholarly indexes
- ``aggregator`` 0.72  reputable press aggregators & mainstream media
- ``community``  0.60  forums, Q&A, UGC platforms (useful, needs corroboration)
- ``unknown``    0.50  default — never guessed higher
"""

from __future__ import annotations

TIER_SCORES: dict[str, float] = {
    "official": 0.97,
    "primary": 0.93,
    "research": 0.90,
    "reference": 0.88,
    "aggregator": 0.72,
    "community": 0.60,
    "unknown": 0.50,
}

_DOMAIN_TIERS: dict[str, str] = {
    # official / regulator
    "sec.gov": "official",
    "hsx.vn": "official",
    "hnx.vn": "official",
    "sbv.gov.vn": "official",
    "mof.gov.vn": "official",
    "gov.vn": "official",
    "federalreserve.gov": "official",
    "ecb.europa.eu": "official",
    "imf.org": "official",
    "worldbank.org": "official",
    "thuvienphapluat.vn": "official",
    "luatvietnam.vn": "official",
    # primary / first-party data
    "stooq.com": "primary",
    "finance.yahoo.com": "primary",
    "iboard.ssi.com.vn": "primary",
    # research
    "arxiv.org": "research",
    "pubmed.ncbi.nlm.nih.gov": "research",
    "ncbi.nlm.nih.gov": "research",
    "semanticscholar.org": "research",
    "openalex.org": "research",
    "crossref.org": "research",
    "europepmc.org": "research",
    "gdeltproject.org": "research",
    # reference
    "wikipedia.org": "reference",
    "postgresql.org": "reference",
    "docs.python.org": "reference",
    "ietf.org": "reference",
    # aggregator / mainstream press
    "vnexpress.net": "aggregator",
    "dantri.com.vn": "aggregator",
    "tuoitre.vn": "aggregator",
    "thanhnien.vn": "aggregator",
    "cafef.vn": "aggregator",
    "vietstock.vn": "aggregator",
    "reuters.com": "aggregator",
    "bloomberg.com": "aggregator",
    "tinhte.vn": "aggregator",
    "genk.vn": "aggregator",
    # community / UGC
    "github.com": "community",
    "stackoverflow.com": "community",
    "reddit.com": "community",
    "news.ycombinator.com": "community",
    "x.com": "community",
    "twitter.com": "community",
    "facebook.com": "community",
    "viblo.asia": "community",
    "medium.com": "community",
}


def _norm(domain: str) -> str:
    d = (domain or "").strip().lower().rstrip(".")
    return d[4:] if d.startswith("www.") else d


def domain_tier(domain: str) -> str:
    """Tier name for a domain (suffix match on the registry, else 'unknown')."""
    d = _norm(domain)
    if d in _DOMAIN_TIERS:
        return _DOMAIN_TIERS[d]
    for known, tier in _DOMAIN_TIERS.items():
        if d.endswith("." + known):
            return tier
    return "unknown"


def tier_score(domain: str) -> float:
    return TIER_SCORES[domain_tier(domain)]
