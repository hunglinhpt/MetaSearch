"""arXiv adapter — vertical engine for academic queries (docs/03 §2).

Uses the public Atom API (export.arxiv.org). No API key. Enabled engines of
category ``academic`` join the fan-out only when the request asks for that
category (docs/01 FR-3).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
from defusedxml import ElementTree as DET

from app.retrieval.adapters.base import AdapterError
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def _parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class ArxivAdapter:
    id = "arxiv"

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 3.0,
    ) -> list[RawResult]:
        params: dict[str, str] = {
            "search_query": f"all:{query}",
            "start": "0",
            "max_results": str(min(max_results, 30)),
            "sortBy": "relevance",
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
                resp = await client.get(self.base_url, params=params)
        except httpx.TimeoutException as exc:
            raise AdapterError(f"arxiv timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"arxiv transport error: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise AdapterError(f"arxiv HTTP {resp.status_code}", retryable=False)

        try:
            root = DET.fromstring(resp.text)
        except Exception as exc:  # ParseError or forbidden-entity raise
            raise AdapterError("arxiv returned malformed XML") from exc

        results: list[RawResult] = []
        for entry in root.findall(f"{_ATOM}entry"):
            url = self._abs_url(entry)
            if not url:
                continue
            results.append(
                RawResult(
                    url=url,
                    title=_clean(self._text(entry, "title")),
                    snippet=_clean(self._text(entry, "summary"))[:600],
                    adapter=self.id,
                    source_engines=["arxiv"],
                    published_at=_parse_published(self._text(entry, "published")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results

    def _text(self, entry: DET.Element, tag: str) -> str:
        node = entry.find(f"{_ATOM}{tag}")
        return str(node.text or "") if node is not None else ""

    def _abs_url(self, entry: DET.Element) -> str | None:
        id_node = entry.find(f"{_ATOM}id")
        if id_node is not None and str(id_node.text or "").startswith("http"):
            return str(id_node.text).strip()
        for link in entry.findall(f"{_ATOM}link"):
            href = link.get("href") or ""
            if "/abs/" in href:
                return href
        return None
