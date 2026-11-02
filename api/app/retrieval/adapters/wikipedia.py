"""Wikipedia adapter — vertical engine for reference lookups (docs/01 FR-3).

MediaWiki ``list=search`` API, no key. Language follows the request ``lang``
(defaulting to English). Category ``reference``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from app.http import get_http_client
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import Outcome
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://{lang}.wikipedia.org/w/api.php"
WIKIPEDIA_USER_AGENT = (
    "MetaSearch/1.3.1 (https://github.com/hunglinhpt/MetaSearch; contact: admin@metasearch.local)"
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_LANG_RE = re.compile(r"^[a-z]{2,12}$")


def _strip_html(text: str) -> str:
    return _WS.sub(" ", _TAG.sub("", text or "")).strip()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class WikipediaAdapter:
    id = "wikipedia"

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
        timeout_s: float = 3.0,
    ) -> list[RawResult]:
        raw_lang = (lang or "en").split("-")[0].lower().strip()
        language = raw_lang if _LANG_RE.match(raw_lang) else "en"
        params: dict[str, str] = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "utf8": "1",
            "srlimit": str(min(max_results, 25)),
        }
        client = self._client or get_http_client("search")
        try:
            resp = await client.get(
                self.base_url.format(lang=language),
                params=params,
                headers={"User-Agent": WIKIPEDIA_USER_AGENT},
                follow_redirects=True,
                timeout=timeout_s,
            )
        except httpx.PoolTimeout as exc:
            raise AdapterError("wikipedia pool timeout", kind=Outcome.LOCAL_OVERLOAD) from exc
        except httpx.TimeoutException as exc:
            raise AdapterError(
                f"wikipedia timeout after {timeout_s}s", kind=Outcome.TIMEOUT
            ) from exc
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"wikipedia transport error: {type(exc).__name__}", kind=Outcome.UPSTREAM_ERROR
            ) from exc
        if resp.status_code == 429:
            raise AdapterError("wikipedia rate limited", kind=Outcome.RATE_LIMITED)
        if resp.status_code == 403:
            raise AdapterError("wikipedia access forbidden", kind=Outcome.BLOCKED, retryable=False)
        if resp.status_code != 200:
            raise AdapterError(
                f"wikipedia HTTP {resp.status_code}", kind=Outcome.UPSTREAM_ERROR, retryable=False
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise AdapterError("wikipedia returned non-JSON body") from exc
        hits = (payload.get("query") or {}).get("search") or []

        results: list[RawResult] = []
        for hit in hits:
            title = str(hit.get("title") or "")
            pageid = hit.get("pageid")
            if not title or pageid is None:
                continue
            results.append(
                RawResult(
                    url=(
                        f"https://{language}.wikipedia.org/wiki/"
                        f"{quote(title.replace(' ', '_'), safe='()')}"
                    ),
                    title=title,
                    snippet=_strip_html(str(hit.get("snippet") or "")),
                    adapter=self.id,
                    source_engines=[f"wikipedia-{language}"],
                    published_at=_parse_timestamp(hit.get("timestamp")),
                    rank=len(results),
                )
            )
            if len(results) >= max_results:
                break
        return results
