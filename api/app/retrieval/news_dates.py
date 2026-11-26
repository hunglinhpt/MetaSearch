"""Publication-date recovery for undated wire items (docs/35 M3).

HTML listing sources (section pages) give a title + link but no date; such
items were returned with ``published_at=null`` and many consumers drop them.
The article page itself almost always states its date. Order of trust:

1. ``<meta property="article:published_time">`` (and the common equivalents),
2. JSON-LD ``datePublished`` (schema.org NewsArticle, incl. ``@graph``),
3. a date embedded in the article URL (day precision, midnight Vietnam time).

Never inferred from the crawl time: no evidence -> ``None`` (the item stays
``date_status="undated"``). Pure functions; the bounded fetch lives in
``retrieval.events``.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

VN_TZ = timezone(timedelta(hours=7))

_META_KEYS = frozenset(
    {
        "article:published_time",
        "og:article:published_time",
        "article:published",
        "datepublished",
        "pubdate",
        "publishdate",
        "publish-date",
        "publish_date",
        "dc.date.issued",
        "dc.date",
        "parsely-pub-date",
        "sailthru.date",
        "cxenseparse:recs:publishtime",
    }
)
_LDJSON_RE = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_URL_DATE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})(?:/|$)"),
    re.compile(r"/(20\d{2})-(\d{2})-(\d{2})(?:[/_-]|$)"),
    re.compile(r"[/_-](20\d{2})(\d{2})(\d{2})(?:\d{4,})?(?=[._/-]|$)"),
)
_VN_DMY_RE = re.compile(
    r"^\s*(\d{1,2})/(\d{1,2})/(20\d{2})(?:[\s,-]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*$"
)
_MAX_HTML_SCAN = 400_000
_MAX_LD_BLOCKS = 20
_MAX_LD_CHARS = 200_000


def _plausible(dt: datetime, now: datetime) -> bool:
    return datetime(2000, 1, 1, tzinfo=UTC) <= dt <= now + timedelta(days=1)


def parse_published(raw: Any, *, naive_tz: timezone = VN_TZ) -> datetime | None:
    """ISO-8601 / ``dd/mm/yyyy [HH:MM]`` -> aware UTC datetime. A value
    without an offset is read as ``naive_tz`` (Vietnamese publishers state
    local time)."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    iso = f"{s[:-1]}+00:00" if s.endswith(("Z", "z")) else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        m = _VN_DMY_RE.match(s)
        if not m:
            return None
        d, mo, y, hh, mm, ss = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=naive_tz)
    return dt.astimezone(UTC)


class _MetaScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []
        self.time_datetimes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key in _META_KEYS and a.get("content"):
                self.values.append(a["content"])
        elif (
            tag == "time" and a.get("itemprop", "").lower() == "datepublished" and a.get("datetime")
        ):
            self.time_datetimes.append(a["datetime"])


def _walk_date_published(node: Any, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if isinstance(node, dict):
        val = node.get("datePublished")
        if isinstance(val, str) and val.strip():
            return val
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
            found = _walk_date_published(node.get(key), depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node[:50]:
            found = _walk_date_published(item, depth + 1)
            if found:
                return found
    return None


def published_from_html(
    html: str, *, now: datetime | None = None
) -> tuple[datetime | None, str | None]:
    """``(published_utc, source)`` with source ``article_meta`` | ``json_ld``."""
    ref = now or datetime.now(UTC)
    text = (html or "")[:_MAX_HTML_SCAN]
    if not text:
        return None, None
    scanner = _MetaScanner()
    with contextlib.suppress(Exception):  # malformed markup: fall through to JSON-LD
        scanner.feed(text)
    for raw in (*scanner.values, *scanner.time_datetimes):
        dt = parse_published(raw)
        if dt is not None and _plausible(dt, ref):
            return dt, "article_meta"
    for block in _LDJSON_RE.findall(text)[:_MAX_LD_BLOCKS]:
        if len(block) > _MAX_LD_CHARS:
            continue
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        dt = parse_published(_walk_date_published(data))
        if dt is not None and _plausible(dt, ref):
            return dt, "json_ld"
    return None, None


def published_from_url(url: str | None, *, now: datetime | None = None) -> datetime | None:
    """Day-precision date embedded in the URL path (midnight Vietnam time)."""
    if not url:
        return None
    ref = now or datetime.now(UTC)
    path = url.split("?", 1)[0].split("#", 1)[0]
    for pat in _URL_DATE_RES:
        m = pat.search(path)
        if not m:
            continue
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=VN_TZ)
        except ValueError:
            continue
        utc = dt.astimezone(UTC)
        if _plausible(utc, ref):
            return utc
    return None
