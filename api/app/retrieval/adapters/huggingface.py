"""HuggingFace Hub adapter — vertical engine for AI models, datasets & open research.

Uses the official HuggingFace Hub Models API (huggingface.co/api/models).
Keyless public API, zero CAPTCHA.
Category: ``developer``.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from app.retrieval.adapters.base import AdapterError
from app.retrieval.models import RawResult

DEFAULT_BASE_URL = "https://huggingface.co/api/models"


def _parse_iso(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class HuggingFaceAdapter:
    id = "huggingface"

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL) -> None:
        self.base_url = base_url

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
        params = {
            "search": query,
            "limit": str(min(max_results, 30)),
            "full": "false",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 MetaSearch/0.7.0"
            ),
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
                resp = await client.get(self.base_url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise AdapterError(f"huggingface timeout after {timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise AdapterError(f"huggingface transport error: {type(exc).__name__}") from exc

        if resp.status_code != 200:
            raise AdapterError(f"huggingface HTTP {resp.status_code}", retryable=False)

        try:
            items = resp.json()
        except ValueError as exc:
            raise AdapterError("huggingface returned non-JSON body") from exc

        if not isinstance(items, list):
            return []

        results: list[RawResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            model_id = item.get("id") or item.get("_id")
            if not model_id:
                continue

            url = f"https://huggingface.co/{model_id}"
            downloads = item.get("downloads") or 0
            likes = item.get("likes") or 0
            pipeline_tag = item.get("pipeline_tag") or "model"
            author = model_id.split("/")[0] if "/" in model_id else "community"

            snippet = (
                f"[HuggingFace {pipeline_tag}] Author: {author} · "
                f"Downloads: {downloads:,} · Likes: {likes:,}"
            )
            published_at = _parse_iso(item.get("lastModified"))

            results.append(
                RawResult(
                    url=url,
                    title=model_id,
                    snippet=snippet,
                    rank=len(results) + 1,
                    adapter=self.id,
                    source_engines=[self.id],
                    published_at=published_at,
                )
            )

        return results
