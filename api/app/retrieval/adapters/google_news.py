"""Google News RSS adapter — real-time news across finance, technology, society & world events.

Uses keyless Google News RSS search endpoints:
- https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}
Provides minute-by-minute news coverage with zero CAPTCHA and auto language detection.
Category: ``news``.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from defusedxml import ElementTree as DET

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

_DEFAULT_NEWS_URL = "https://news.google.com/rss/search"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_snippet(raw: str) -> str:
    cleaned = _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(raw or ""))).strip()
    return cleaned


class GoogleNewsAdapter:
    """Zero-key, zero-CAPTCHA adapter for Google News RSS."""

    id = "google_news"

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_NEWS_URL,
        default_hl: str = "vi",
        default_gl: str = "VN",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url
        self.default_hl = default_hl
        self.default_gl = default_gl
        self._client = client

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 10,
        timeout_s: float = 3.0,
        **kwargs: Any,
    ) -> list[RawResult]:
        q = (query or "").strip()
        if not q:
            return []

        # Determine locale: respect explicit lang="vi" or Vietnamese markers in query
        is_vietnamese = (lang or "").lower().startswith("vi") or bool(
            re.search(
                r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]",
                q,
                re.IGNORECASE,
            )
            or any(
                w in q.lower()
                for w in [
                    "chứng khoán",
                    "cổ phiếu",
                    "việt nam",
                    "vn-index",
                    "kinh tế",
                    "hôm nay",
                    "tin tức",
                ]
            )
        )

        hl = "vi" if is_vietnamese else "en-US"
        gl = "VN" if is_vietnamese else "US"
        ceid = "VN:vi" if is_vietnamese else "US:en"

        params = {
            "q": q,
            "hl": hl,
            "gl": gl,
            "ceid": ceid,
        }

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        }

        client = self._client or get_http_client("search")
        try:
            resp = await client.get(
                self.base_url,
                params=params,
                headers=headers,
                follow_redirects=True,
                timeout=timeout_s,
            )
        except httpx.PoolTimeout as exc:
            raise AdapterError("google_news pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"google_news_timeout: Timeout after {timeout_s}s", kind=Outcome.TIMEOUT
            ) from exc
        except httpx.RequestError as exc:
            raise AdapterError(
                f"google_news_network_error: {exc}", kind=Outcome.UPSTREAM_ERROR
            ) from exc

        if resp.status_code == 429:
            raise AdapterError("google_news rate limited", kind=Outcome.RATE_LIMITED)
        if resp.status_code == 403:
            raise AdapterError("google_news forbidden", kind=Outcome.BLOCKED, retryable=False)
        if resp.status_code != 200:
            raise AdapterError(
                f"google_news_upstream_error: Google News RSS returned status {resp.status_code}",
                kind=Outcome.UPSTREAM_ERROR,
            )

        try:
            root = DET.fromstring(resp.text)
        except Exception as exc:
            raise AdapterError(f"google_news_parse_error: XML parse error: {exc}") from exc

        items = root.findall("./channel/item")
        results: list[RawResult] = []

        for item in items[: max(max_results, 1)]:
            title_elem = item.find("title")
            link_elem = item.find("link")
            desc_elem = item.find("description")
            pub_elem = item.find("pubDate")
            source_elem = item.find("source")

            raw_title = title_elem.text if title_elem is not None and title_elem.text else ""
            title = _clean_snippet(raw_title)

            raw_link = link_elem.text if link_elem is not None and link_elem.text else ""
            link = raw_link.strip()

            raw_desc = desc_elem.text if desc_elem is not None and desc_elem.text else ""
            snippet = _clean_snippet(raw_desc) or title

            source_name = ""
            if source_elem is not None and source_elem.text:
                source_name = source_elem.text.strip()
            elif " - " in title:
                source_name = title.rsplit(" - ", 1)[-1].strip()

            if source_name and not snippet.startswith(f"[{source_name}]"):
                snippet = f"[{source_name}] {snippet}"

            published_at: datetime | None = None
            if pub_elem is not None and pub_elem.text:
                try:
                    # RFC 822 format: e.g. 'Sun, 01 Nov 2026 03:30:00 GMT'
                    dt = datetime.strptime(pub_elem.text.strip(), "%a, %d %b %Y %H:%M:%S %Z")
                    published_at = dt.replace(tzinfo=UTC)
                except ValueError:
                    published_at = None

            if link and title:
                results.append(
                    RawResult(
                        url=link,
                        title=title,
                        snippet=snippet,
                        adapter="google_news",
                        source_engines=["google_news"],
                        rank=len(results) + 1,
                        published_at=published_at,
                    )
                )

        return results
