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
    "low_trust": 0.25,
}

_DOMAIN_TIERS: dict[str, str] = {
    # official / regulator / exchange / central bank
    "sec.gov": "official",
    "edgar.sec.gov": "official",
    "hsx.vn": "official",
    "hose.vn": "official",
    "hnx.vn": "official",
    "ssc.gov.vn": "official",
    "sbv.gov.vn": "official",
    "mof.gov.vn": "official",
    "gso.gov.vn": "official",
    "gov.vn": "official",
    "chinhphu.vn": "official",
    "quochoi.vn": "official",
    "federalreserve.gov": "official",
    "fred.stlouisfed.org": "official",
    "bls.gov": "official",
    "bea.gov": "official",
    "treasury.gov": "official",
    "cboe.com": "official",
    "finra.org": "official",
    "ecb.europa.eu": "official",
    "imf.org": "official",
    "worldbank.org": "official",
    "thuvienphapluat.vn": "official",
    "luatvietnam.vn": "official",
    # primary / first-party market data & brokerage platforms
    "stooq.com": "primary",
    "finance.yahoo.com": "primary",
    "iboard.ssi.com.vn": "primary",
    "ssi.com.vn": "primary",
    "vndirect.com.vn": "primary",
    "dnse.com.vn": "primary",
    "simplize.vn": "primary",
    "fireant.vn": "primary",
    "fiintrade.vn": "primary",
    "wichart.vn": "primary",
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
    # aggregator / mainstream & financial press
    "vnexpress.net": "aggregator",
    "vneconomy.vn": "aggregator",
    "dantri.com.vn": "aggregator",
    "tuoitre.vn": "aggregator",
    "thanhnien.vn": "aggregator",
    "cafef.vn": "aggregator",
    "cafebiz.vn": "aggregator",
    "vietstock.vn": "aggregator",
    "tinnhanhchungkhoan.vn": "aggregator",
    "baodautu.vn": "aggregator",
    "nguoiquansat.vn": "aggregator",
    "thesaigontimes.vn": "aggregator",
    "vietnamplus.vn": "aggregator",
    "reuters.com": "aggregator",
    "bloomberg.com": "aggregator",
    "ft.com": "aggregator",
    "wsj.com": "aggregator",
    "cnbc.com": "aggregator",
    "marketwatch.com": "aggregator",
    "seekingalpha.com": "aggregator",
    "investing.com": "aggregator",
    "barrons.com": "aggregator",
    "bbc.com": "aggregator",
    "apnews.com": "aggregator",
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
    "f319.com": "community",
    "f247.com": "community",
    "stocktwits.com": "community",
    "bsky.app": "community",
    "quora.com": "community",
    # low-trust / SEO farms / paywalled doc dumps
    "pinterest.com": "low_trust",
    "slideshare.net": "low_trust",
    "scribd.com": "low_trust",
    "123docz.net": "low_trust",
    "tailieu.vn": "low_trust",
    "studocu.com": "low_trust",
}

_OFFICIAL_SUFFIXES = (".gov", ".gov.vn", ".gov.uk", ".gov.au", ".gov.sg", ".mil")
_RESEARCH_SUFFIXES = (".edu", ".edu.vn", ".ac.uk", ".ac.jp")


def _norm(domain: str) -> str:
    d = (domain or "").strip().lower().rstrip(".")
    return d[4:] if d.startswith("www.") else d


def domain_tier(domain: str) -> str:
    """Tier name for a domain (registry match, TLD rule, else 'unknown')."""
    d = _norm(domain)
    if not d:
        return "unknown"
    if d in _DOMAIN_TIERS:
        return _DOMAIN_TIERS[d]
    for known, tier in _DOMAIN_TIERS.items():
        if d.endswith("." + known):
            return tier
    if d.endswith(_OFFICIAL_SUFFIXES):
        return "official"
    if d.endswith(_RESEARCH_SUFFIXES):
        return "research"
    return "unknown"


def tier_score(domain: str) -> float:
    return TIER_SCORES[domain_tier(domain)]
