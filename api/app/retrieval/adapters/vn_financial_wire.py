"""Vietnamese Financial Wire adapter — keyless live news, brokerage reports & market wire.

Combines three real-time verified Vietnamese equity intelligence streams when VN tickers
or Vietnamese financial context appear in the query:
1. Simplize equity news feed (``api.simplize.vn/api/company/news-event/list-news``).
2. Simplize brokerage analysis reports & target prices (``analysis-report/list``).
3. Cached live Vietnamese RSS/HTML financial wire (``events.fetch_market_news_wire``).
Category: ``financial``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.http import get_http_client
from app.retrieval.events import fetch_market_news_wire
from app.retrieval.models import RawResult
from app.retrieval.scoring import fold_diacritics, tokenize
from app.retrieval.vn_tickers import is_vn_ticker, load_vn_tickers

SIMPLIZE_NEWS_URL = "https://api.simplize.vn/api/company/news-event/list-news"
SIMPLIZE_REPORTS_URL = "https://api.simplize.vn/api/company/analysis-report/list"
_MAX_BYTES = 3 * 1024 * 1024

_VI_FIN_HINTS = (
    "cổ phiếu",
    "co phieu",
    "chứng khoán",
    "chung khoan",
    "báo cáo",
    "bao cao",
    "kết quả kinh doanh",
    "ket qua kinh doanh",
    "lợi nhuận",
    "loi nhuan",
    "doanh thu",
    "cổ tức",
    "co tuc",
    "vn-index",
    "vnindex",
    "hose",
    "hnx",
    "upcom",
    "ngân hàng",
    "ngan hang",
    "thị trường",
    "thi truong",
)


def _safe_http_url(url: str | None) -> str | None:
    if not url:
        return None
    cleaned = url.strip()
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return None
    if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
        return cleaned
    return None


def _parse_ddmmyyyy(raw: str | None) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _extract_vn_tickers(query: str) -> list[str]:
    catalog = load_vn_tickers()
    seen: list[str] = []
    # Match exact uppercase 3-letter tokens or lowercase tickers when VN context exists
    has_vi_context = any(h in fold_diacritics(query.lower()) for h in _VI_FIN_HINTS)
    for tok in re.findall(r"\$?([A-Za-z0-9]{3})\b", query):
        up = tok.upper()
        if up in seen or up not in catalog:
            continue
        if is_vn_ticker(up) and (tok.isupper() or has_vi_context):
            seen.append(up)
        if len(seen) >= 2:
            break
    return seen


class VnFinancialWireAdapter:
    """Keyless Vietnamese corporate news, brokerage reports & wire adapter."""

    id = "vn_wire"

    def __init__(
        self,
        *,
        news_url: str = SIMPLIZE_NEWS_URL,
        reports_url: str = SIMPLIZE_REPORTS_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.news_url = news_url
        self.reports_url = reports_url
        self._client = client

    async def _fetch_simplize_news(
        self, client: httpx.AsyncClient, ticker: str, *, timeout_s: float
    ) -> list[RawResult]:
        try:
            resp = await client.get(
                self.news_url,
                params={"ticker": ticker, "page": "0", "size": "6"},
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                follow_redirects=True,
                timeout=timeout_s,
            )
            if resp.status_code != 200 or len(resp.content or b"") > _MAX_BYTES:
                return []
            payload = resp.json()
        except Exception:
            return []
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        out: list[RawResult] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            title = str(r.get("title") or "").strip()
            slug = str(r.get("slug") or "").strip()
            if not title or not slug:
                continue
            url = f"https://simplize.vn/tin-tuc/{slug}"
            desc = str(r.get("aiContent") or r.get("description") or "").strip()
            src_name = str(r.get("sourceNameDisplay") or r.get("sourceName") or "Simplize").strip()
            ts_raw = r.get("ts")
            pub_dt: datetime | None = None
            if isinstance(ts_raw, (int, float)) and ts_raw > 0:
                sec = ts_raw / 1000.0 if ts_raw > 1e11 else float(ts_raw)
                try:
                    pub_dt = datetime.fromtimestamp(sec, tz=UTC)
                except (ValueError, OSError, OverflowError):
                    pub_dt = None
            elif isinstance(r.get("createdDate"), str):
                pub_dt = _parse_ddmmyyyy(r["createdDate"])
            snippet = f"[{src_name} · {ticker}] {desc[:360]}".strip()
            out.append(
                RawResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=pub_dt,
                    source_url=url,
                    source_name=src_name,
                    as_of=pub_dt,
                    data_kind="observed",
                    rank=len(out),
                )
            )
        return out

    async def _fetch_simplize_reports(
        self, client: httpx.AsyncClient, ticker: str, *, timeout_s: float
    ) -> list[RawResult]:
        try:
            resp = await client.get(
                self.reports_url,
                params={"ticker": ticker, "page": "0", "size": "5"},
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                follow_redirects=True,
                timeout=timeout_s,
            )
            if resp.status_code != 200 or len(resp.content or b"") > _MAX_BYTES:
                return []
            payload = resp.json()
        except Exception:
            return []
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        out: list[RawResult] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            title = str(r.get("title") or "").strip()
            pdf_url = _safe_http_url(str(r.get("attachedLink") or ""))
            if not title or not pdf_url:
                continue
            broker = str(r.get("source") or "Brokerage").strip()
            rec = str(r.get("recommend") or "").strip()
            tp = r.get("targetPrice")
            issue_dt = _parse_ddmmyyyy(r.get("issueDate"))
            parts = [f"[BCPT {broker} · {ticker}]"]
            if rec:
                parts.append(f"Khuyến nghị: {rec}")
            if isinstance(tp, (int, float)) and tp > 0:
                parts.append(f"Giá mục tiêu: {tp:,.0f} đ")
            if r.get("issueDate"):
                parts.append(f"Ngày: {r['issueDate']}")
            out.append(
                RawResult(
                    url=pdf_url,
                    title=f"[{broker}] {title}",
                    snippet=" · ".join(parts),
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=issue_dt,
                    source_url=pdf_url,
                    source_name=broker,
                    as_of=issue_dt,
                    data_kind="observed",
                    rank=len(out),
                )
            )
        return out

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 12,
        timeout_s: float = 4.0,
        **kwargs: Any,
    ) -> list[RawResult]:
        q = (query or "").strip()
        if not q:
            return []
        tickers = _extract_vn_tickers(q)
        folded_q = fold_diacritics(q.lower())
        is_vi_fin = bool(tickers) or any(h in folded_q for h in _VI_FIN_HINTS)
        if not is_vi_fin:
            return []

        client = self._client or get_http_client("search")
        coros: list[Any] = []
        for sym in tickers:
            coros.append(self._fetch_simplize_news(client, sym, timeout_s=min(timeout_s, 3.0)))
            coros.append(self._fetch_simplize_reports(client, sym, timeout_s=min(timeout_s, 3.0)))

        # Also query curated RSS/HTML wire from events.py when a live test client isn't isolated
        if self._client is None:
            coros.append(
                fetch_market_news_wire(
                    symbols=tickers or None,
                    lang="vi",
                    max_age_days=max(1, int(freshness_days or 14)),
                    max_items=min(max_results, 10),
                    timeout_s=min(timeout_s, 3.0),
                )
            )

        batches = await asyncio.gather(*coros, return_exceptions=True)
        cutoff_ts = (
            datetime.now(UTC).timestamp() - max(1, int(freshness_days)) * 86400
            if freshness_days
            else None
        )
        q_terms = set(tokenize(q))
        results: list[RawResult] = []
        seen_urls: set[str] = set()
        for batch in batches:
            if isinstance(batch, BaseException):
                continue
            if isinstance(batch, list):
                for item in batch:
                    if item.url in seen_urls:
                        continue
                    if (
                        cutoff_ts is not None
                        and item.published_at is not None
                        and item.published_at.timestamp() < cutoff_ts
                    ):
                        continue
                    seen_urls.add(item.url)
                    item.rank = len(results)
                    results.append(item)
            elif isinstance(batch, dict):
                for it in batch.get("items") or []:
                    if not isinstance(it, dict):
                        continue
                    u = _safe_http_url(it.get("url"))
                    title = str(it.get("title") or "").strip()
                    if not u or not title or u in seen_urls:
                        continue
                    if not tickers and q_terms:
                        doc_terms = set(tokenize(f"{title} {it.get('snippet') or ''}"))
                        if not (q_terms & doc_terms):
                            continue
                    seen_urls.add(u)
                    pub_dt = None
                    if isinstance(it.get("published_at"), str):
                        try:
                            pub_dt = datetime.fromisoformat(
                                it["published_at"].replace("Z", "+00:00")
                            )
                        except ValueError:
                            pub_dt = None
                    results.append(
                        RawResult(
                            url=u,
                            title=title,
                            snippet=str(it.get("snippet") or title)[:400],
                            adapter=self.id,
                            source_engines=[self.id],
                            published_at=pub_dt,
                            source_url=u,
                            source_name=str(it.get("source_id") or "") or None,
                            as_of=pub_dt,
                            data_kind="observed",
                            rank=len(results),
                        )
                    )
        return results[:max_results]
