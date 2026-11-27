"""Adult-content filter for merged results (docs/36 W2).

Only SearXNG receives ``safesearch=1``; the keyless Lite engines have no safe
search at all, and Bing's RSS face returns *nothing* with ``adlt=strict``. The
26/11 eval caught ``bing_web`` answering "hướng dẫn dùng FastAPI dependency
injection" (mkt vi-VN) with ten porn sites. This filter runs after merge, for
every engine.

Precision first: a host is flagged only on unambiguous markers, so
``sussex.ac.uk``, ``essex.gov.uk`` or a "sexual health" clinic page stay. Titles
are checked for a short list of explicit phrases (EN + VI).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Unambiguous adult brands / host fragments (matched inside the hostname).
_HOST_MARKERS = re.compile(
    r"(porn|xxx|xnxx|xvideos|xhamster|stripchat|chaturbate|redtube|youporn|brazzers|"
    r"hentai|onlyfans|camsoda|bongacams|spankbang|eporner|"
    r"phimsex|clipsex|thichsex|xemsex|sexviet|sexhay|phim18|mejav|javhd|javfree|"
    r"xemloz|topgai|cliphot)",
    re.IGNORECASE,
)
# Explicit title phrases. Word-bounded; Vietnamese phrases match as written.
_TITLE_MARKERS = re.compile(
    r"(\bporn\w*|\bxxx\b|\bhentai\b|\bnsfw\b|\bjav\b|phim sex|clip sex|phim người lớn|"
    r"phim cấp 3|\bsex (video|tape|cam)s?\b|\bnude (video|pic)s?\b)",
    re.IGNORECASE,
)
# Titles count only on sources without an established tier: reputable outlets
# report ON these topics ("bắt nhóm phát tán clip sex").
_TITLE_CHECK_TIERS = frozenset({"unknown", "low_trust"})


def is_adult(url: str, title: str = "", *, domain: str | None = None) -> bool:
    """``domain`` = publisher domain when the URL is a wrapper (Google News)."""
    host = (urlsplit(url).hostname or "").lower()
    if host and _HOST_MARKERS.search(host):
        return True
    if domain and _HOST_MARKERS.search(domain.lower()):
        return True
    if not title or not _TITLE_MARKERS.search(title):
        return False
    from app.retrieval.tiers import domain_tier

    return domain_tier(domain or host) in _TITLE_CHECK_TIERS
