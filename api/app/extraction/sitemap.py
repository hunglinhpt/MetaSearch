"""Sitemap parsing + link discovery for /v1/map and /v1/crawl (docs/20 P1-7).

All fetching goes through ``Scraper._guarded_fetch`` — SSRF resolve+check, IP
pinning and robots.txt on every hop (docs/10 §SSRF). Only http(s) URLs on
the same host as the root are followed, so a hostile sitemap cannot turn
the crawler into an SSRF pivot to internal addresses (those would also be
rejected by ``resolve_and_check``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

# defusedxml is a pinned dependency: sitemaps are untrusted remote XML, so we
# never parse them with the stdlib ElementTree (bandit B405/B314).
from defusedxml.ElementTree import ParseError as _XMLParseError
from defusedxml.ElementTree import fromstring as _xml_fromstring

from app.core.logging import get_logger

logger = get_logger(__name__)

_MAX_SITEMAPS = 8
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\w/])https?://[^\s)\]>\"']+", re.IGNORECASE)


@dataclass
class MapResult:
    root: str
    urls: list[str] = field(default_factory=list)
    source: str = "html"  # "sitemap" | "html" | "sitemap+html"
    truncated: bool = False


def _normalize(url: str) -> str:
    """Drop the fragment and trailing slash noise; keep scheme+host+path+query."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def same_host(url: str, root: str) -> bool:
    a = urlsplit(url).hostname or ""
    b = urlsplit(root).hostname or ""
    a, b = a.lower(), b.lower()
    return a == b or a.endswith("." + b)


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls) from a sitemap XML document.

    Namespace-agnostic (matches on local names) so both sitemapindex and
    urlset documents parse.
    """
    urls: list[str] = []
    sitemaps: list[str] = []
    try:
        root = _xml_fromstring(xml_text)
    except _XMLParseError:
        return [], []
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1].lower()
        text = (elem.text or "").strip()
        if not text:
            continue
        if tag == "loc":
            # Parent decides: sitemapindex -> sitemap, urlset -> page. We can't
            # always see the parent here, so classify by heuristic below.
            urls.append(text)
        elif tag == "sitemap":
            sitemaps.append(text)
    # Heuristic split: a <urlset> document lists pages in <url><loc>; a
    # <sitemapindex> lists sitemaps in <sitemap><loc>. Re-walk with parents.
    pages: list[str] = []
    children: list[str] = []
    for parent in root.iter():
        ptag = parent.tag.rsplit("}", 1)[-1].lower()
        for child in parent:
            ctag = child.tag.rsplit("}", 1)[-1].lower()
            if ctag != "loc":
                continue
            text = (child.text or "").strip()
            if not text:
                continue
            if ptag == "url":
                pages.append(text)
            elif ptag == "sitemap":
                children.append(text)
    return (pages or urls), children or sitemaps


def extract_links(text: str, base_url: str, *, markdown: bool = False) -> list[str]:
    """Collect same-host http(s) links from HTML (href) or markdown text."""
    found: list[str] = []
    candidates: list[str] = []
    if markdown:
        candidates.extend(_MD_LINK_RE.findall(text))
        candidates.extend(_BARE_URL_RE.findall(text))
    else:
        candidates.extend(_HREF_RE.findall(text))
    seen: set[str] = set()
    for raw in candidates:
        absolute = urljoin(base_url, raw.strip())
        norm = _normalize(absolute)
        if not norm or norm in seen or not same_host(norm, base_url):
            continue
        seen.add(norm)
        found.append(norm)
    return found


async def _fetch_text(scraper, url: str, timeout_s: float) -> tuple[str, str]:
    """Fetch via the scraper's guarded fetch; returns (body_text, final_url)."""
    body, final_url = await scraper._guarded_fetch(url, timeout_s)
    return body.decode("utf-8", errors="replace"), final_url


async def map_site(
    scraper,
    url: str,
    *,
    max_urls: int = 100,
    timeout_s: float = 10.0,
    include_html_links: bool = True,
) -> MapResult:
    """Discover URLs under ``url``: sitemap.xml first, HTML links as fallback
    (or complement). Bounded by ``max_urls`` and ``_MAX_SITEMAPS`` documents."""
    root = _normalize(url)
    if not root:
        return MapResult(root=url, urls=[], source="html")
    collected: list[str] = []
    seen: set[str] = {root}
    source_parts: list[str] = []

    sitemap_candidates = [urljoin(root, "/sitemap.xml")]
    visited_sitemaps: set[str] = set()
    queue = list(sitemap_candidates)
    while queue and len(visited_sitemaps) < _MAX_SITEMAPS:
        sm = queue.pop(0)
        norm_sm = _normalize(sm)
        if not norm_sm or norm_sm in visited_sitemaps or not same_host(norm_sm, root):
            continue
        visited_sitemaps.add(norm_sm)
        try:
            text, _final = await _fetch_text(scraper, norm_sm, timeout_s)
        except Exception as exc:
            logger.info("sitemap_fetch_failed", url=norm_sm, error=type(exc).__name__)
            continue
        if "<urlset" not in text and "<sitemapindex" not in text:
            continue
        pages, children = parse_sitemap(text)
        source_parts.append("sitemap")
        for p in pages:
            np = _normalize(p)
            if np and same_host(np, root) and np not in seen:
                seen.add(np)
                collected.append(np)
        for c in children:
            nc = _normalize(c)
            if nc and same_host(nc, root):
                queue.append(nc)

    if include_html_links:
        try:
            html, final_root = await _fetch_text(scraper, root, timeout_s)
            for link in extract_links(html, final_root or root, markdown=False):
                if link not in seen:
                    seen.add(link)
                    collected.append(link)
            source_parts.append("html")
        except Exception as exc:
            logger.info("map_root_html_failed", url=root, error=type(exc).__name__)

    truncated = len(collected) > max_urls
    return MapResult(
        root=root,
        urls=collected[:max_urls],
        source="+".join(dict.fromkeys(source_parts)) or "html",
        truncated=truncated,
    )
