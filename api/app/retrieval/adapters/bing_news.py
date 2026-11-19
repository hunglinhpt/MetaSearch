"""Bing News RSS adapter — keyless bilingual real-time news with direct publisher URLs.

Queries ``https://www.bing.com/news/search?q={q}&format=rss&mkt={mkt}`` and unwraps
``apiclick.aspx?url=...`` redirect wrappers back to real publisher URLs so downstream
ranking, domain authority, and article scraping operate on actual origins.
Category: ``news``.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from defusedxml import ElementTree as DET

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://www.bing.com/news/search"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_VI_CHARS_RE = re.compile(
    r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]",
    re.IGNORECASE,
)


def _clean_text(raw: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(raw or ""))).strip()


def _unwrap_bing_url(href: str) -> str | None:
    """Unwrap Bing News apiclick redirect wrappers back to a validated http(s) URL."""
    raw = (href or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.netloc.lower().endswith("bing.com") and "apiclick.aspx" in parsed.path.lower():
        qs = parse_qs(parsed.query)
        candidates = qs.get("url") or qs.get("u") or []
        target = candidates[0] if candidates else None
        if target:
            raw = unquote(target).strip()
            try:
                parsed = urlsplit(raw)
            except ValueError:
                return None
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return raw
    return None


def _parse_pubdate(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class BingNewsAdapter:
    """Keyless Bing News RSS search adapter."""

    id = "bing_news"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url
        self._client = client

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 4.0,
        **kwargs: Any,
    ) -> list[RawResult]:
        q = (query or "").strip()
        if not q:
            return []

        raw_lang = (lang or "").split("-")[0].lower().strip()
        if raw_lang == "vi":
            mkt = "vi-VN"
        elif raw_lang == "en":
            mkt = "en-US"
        else:
            is_vi = bool(_VI_CHARS_RE.search(q)) or any(
                w in q.lower()
                for w in ("chứng khoán", "cổ phiếu", "vn-index", "kinh doanh", "viet nam")
            )
            mkt = "vi-VN" if is_vi else "en-US"

        params: dict[str, str] = {
            "q": q,
            "format": "rss",
            "mkt": mkt,
        }
        if freshness_days:
            try:
                days = int(freshness_days)
            except (TypeError, ValueError):
                days = 0
            if days <= 1:
                params["qft"] = 'interval="4"'
            elif days <= 7:
                params["qft"] = 'interval="7"'
            elif days <= 31:
                params["qft"] = 'interval="9"'

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
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
            raise AdapterError("bing_news pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"bing_news timeout after {timeout_s}s", kind=Outcome.TIMEOUT
            ) from exc
        except httpx.RequestError as exc:
            raise AdapterError(
                f"bing_news transport error: {type(exc).__name__}", kind=Outcome.UPSTREAM_ERROR
            ) from exc

        if resp.status_code == 429:
            raise AdapterError("bing_news rate limited", kind=Outcome.RATE_LIMITED, retryable=False)
        if resp.status_code == 403:
            raise AdapterError("bing_news forbidden", kind=Outcome.BLOCKED, retryable=False)
        if resp.status_code != 200:
            raise AdapterError(
                f"bing_news HTTP {resp.status_code}",
                kind=Outcome.UPSTREAM_ERROR,
                retryable=resp.status_code >= 500,
            )
        if len(resp.content or b"") > 5 * 1024 * 1024:
            raise AdapterError("bing_news payload too large", kind=Outcome.UPSTREAM_ERROR)

        try:
            root = DET.fromstring(resp.text)
        except Exception as exc:
            raise AdapterError(f"bing_news XML parse error: {exc}") from exc

        items = root.findall("./channel/item")
        results: list[RawResult] = []
        seen: set[str] = set()
        for item in items[: max(1, min(max_results, 30)) * 2]:
            title = _clean_text(item.findtext("title") or "")
            real_url = _unwrap_bing_url(item.findtext("link") or "")
            if not title or not real_url or real_url in seen:
                continue
            seen.add(real_url)
            desc = _clean_text(item.findtext("description") or "")
            pub_dt = _parse_pubdate(item.findtext("pubDate") or "")
            source_name = ""
            for child in item:
                if child.tag.lower().endswith("source") and child.text:
                    source_name = child.text.strip()
                    break
            snippet = desc or title
            if source_name and not snippet.startswith(f"[{source_name}]"):
                snippet = f"[{source_name}] {snippet}"
            results.append(
                RawResult(
                    url=real_url,
                    title=title,
                    snippet=snippet,
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=pub_dt,
                    source_url=real_url,
                    source_name=source_name or None,
                    as_of=pub_dt,
                    data_kind="observed",
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
