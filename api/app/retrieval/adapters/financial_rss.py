"""Global & VN Tier-2 Financial RSS adapter — fast cached direct press feeds.

Fetches verified RSS feeds (VnExpress Kinh Doanh, VnEconomy Chứng Khoán, CNBC Finance,
MarketWatch Top Stories, and per-ticker Yahoo Finance RSS when a US/global ticker is
detected) with a process-local 10-minute TTL cache so press headlines arrive even when
generic web search engines throttle or block.
Category: ``news``.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from defusedxml import ElementTree as DET

from app.http import get_http_client
from app.retrieval.models import RawResult
from app.retrieval.scoring import tokenize

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_UPPER_SYM_RE = re.compile(r"(?:\$|\b)([A-Z]{2,5})\b")
_RSS_CACHE_TTL_S = 600.0
_FEED_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}

DEFAULT_FEEDS: tuple[tuple[str, str, str], ...] = (
    ("vnexpress_biz", "https://vnexpress.net/rss/kinh-doanh.rss", "vi"),
    ("vneconomy_ck", "https://vneconomy.vn/chung-khoan.rss", "vi"),
    (
        "cnbc_finance",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
        "en",
    ),
    ("marketwatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "en"),
)


def _clean(raw: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(raw or ""))).strip()


def _parse_dt(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, OverflowError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def _fetch_feed(
    client: httpx.AsyncClient, url: str, *, timeout_s: float, use_cache: bool
) -> list[dict[str, Any]]:
    now = time.monotonic()
    if use_cache:
        hit = _FEED_CACHE.get(url)
        if hit is not None and (now - hit[0]) < _RSS_CACHE_TTL_S:
            return hit[1]
    try:
        resp = await client.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
            },
            follow_redirects=True,
            timeout=timeout_s,
        )
        if resp.status_code != 200 or len(resp.content or b"") > 4 * 1024 * 1024:
            return []
        root = DET.fromstring(resp.text)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for item in root.findall("./channel/item")[:40]:
        title = _clean(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        desc = _clean(item.findtext("description") or "")
        pub_dt = _parse_dt(item.findtext("pubDate") or "")
        if title and link.startswith("http"):
            rows.append(
                {
                    "title": title,
                    "url": link,
                    "snippet": desc or title,
                    "published_at": pub_dt,
                }
            )
    if use_cache and len(_FEED_CACHE) < 64:
        _FEED_CACHE[url] = (now, rows)
    return rows


class GlobalFinancialRssAdapter:
    """Cached keyless Tier-2 financial & business RSS adapter."""

    id = "fin_rss"

    def __init__(
        self,
        *,
        feeds: tuple[tuple[str, str, str], ...] = DEFAULT_FEEDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.feeds = feeds
        self._client = client

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 10,
        timeout_s: float = 3.5,
        **kwargs: Any,
    ) -> list[RawResult]:
        q = (query or "").strip()
        if not q:
            return []
        q_terms = set(tokenize(q))
        if not q_terms:
            return []
        raw_lang = (lang or "").split("-")[0].lower().strip()
        target_feeds = [
            (sid, url)
            for sid, url, f_lang in self.feeds
            if not raw_lang or raw_lang in ("auto", "all") or f_lang == raw_lang
        ]
        # Add per-ticker Yahoo Finance RSS if query mentions a global equity ticker
        skip_syms = {"CEO", "CFO", "ETF", "SEC", "FED", "IPO", "GDP", "CPI", "Q1", "Q2", "Q3", "Q4"}
        sym_cands = [s for s in _UPPER_SYM_RE.findall(q) if s not in skip_syms]
        if sym_cands and self._client is None and raw_lang != "vi":
            from app.retrieval.vn_tickers import is_vn_ticker

            for s in sym_cands[:1]:
                if not is_vn_ticker(s):
                    target_feeds.append(
                        (
                            f"yahoo_rss_{s}",
                            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={s}&region=US&lang=en-US",
                        )
                    )

        client = self._client or get_http_client("search")
        use_cache = self._client is None
        batches = await asyncio.gather(
            *(
                _fetch_feed(client, u, timeout_s=min(timeout_s, 3.0), use_cache=use_cache)
                for _, u in target_feeds
            ),
            return_exceptions=True,
        )
        cutoff_ts = (
            datetime.now(UTC).timestamp() - max(1, int(freshness_days)) * 86400
            if freshness_days
            else None
        )
        scored_rows: list[tuple[int, dict[str, Any], str]] = []
        seen: set[str] = set()
        for (sid, _u), rows in zip(target_feeds, batches, strict=False):
            if isinstance(rows, BaseException):
                continue
            for row in rows:
                u = row["url"]
                if u in seen:
                    continue
                pub_dt = row.get("published_at")
                if cutoff_ts is not None and pub_dt is not None and pub_dt.timestamp() < cutoff_ts:
                    continue
                doc_terms = set(tokenize(f"{row['title']} {row['snippet']}"))
                overlap = len(q_terms & doc_terms)
                if overlap == 0:
                    continue
                seen.add(u)
                scored_rows.append((overlap, row, sid))

        scored_rows.sort(
            key=lambda x: (
                -x[0],
                -(x[1]["published_at"].timestamp() if x[1].get("published_at") else 0.0),
            )
        )
        out: list[RawResult] = []
        for _overlap, row, sid in scored_rows[:max_results]:
            out.append(
                RawResult(
                    url=row["url"],
                    title=row["title"],
                    snippet=f"[{sid}] {row['snippet'][:360]}",
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=row.get("published_at"),
                    source_url=row["url"],
                    source_name=sid,
                    as_of=row.get("published_at"),
                    data_kind="observed",
                    rank=len(out),
                )
            )
        return out
