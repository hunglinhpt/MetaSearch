"""GitHub repository search adapter — vertical engine for developer queries.

Uses the official GitHub Search API (api.github.com/search/repositories).
Keyless quota: 10 requests/min; optional GITHUB_TOKEN extends to 30 requests/min.
Category: ``developer``.
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from app.retrieval.adapters.base import AdapterError
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://api.github.com/search/repositories"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    return _WS.sub(" ", _TAG.sub("", text or "")).strip()


def _parse_iso(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class GitHubAdapter:
    id = "github"

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, token: str = "") -> None:
        self.base_url = base_url
        self.token = token

    async def search(
        self,
        query: str,
        *,
        lang: str | None = None,
        categories: list[str] | None = None,
        freshness_days: int | None = None,
        max_results: int = 15,
        timeout_s: float = 4.0,
    ) -> list[RawResult]:
        if not query.strip():
            return []
        params: dict[str, str] = {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": str(min(max_results, 30)),
        }
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 MetaSearch/0.7.0"
            ),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
                resp = await client.get(self.base_url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise AdapterError(f"github timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"github transport error: {type(exc).__name__}") from exc

        if resp.status_code == 403:
            raise AdapterError("github rate limit exceeded (HTTP 403)", retryable=False)
        if resp.status_code != 200:
            raise AdapterError(f"github HTTP {resp.status_code}", retryable=False)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise AdapterError("github returned non-JSON body") from exc

        items = payload.get("items") or []
        results: list[RawResult] = []

        for item in items:
            url = item.get("html_url")
            if not url or not url.startswith("http"):
                continue

            name = item.get("full_name") or item.get("name") or "repository"
            desc = _strip_html(item.get("description") or "")
            language = item.get("language") or "Code"
            stars = item.get("stargazers_count") or 0
            forks = item.get("forks_count") or 0

            snippet = f"[{language} ★{stars:,} ⑂{forks:,}] {desc}".strip()
            published_at = _parse_iso(item.get("updated_at") or item.get("pushed_at"))

            results.append(
                RawResult(
                    url=url,
                    title=name,
                    snippet=snippet,
                    rank=len(results) + 1,
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=published_at,
                )
            )

        return results
