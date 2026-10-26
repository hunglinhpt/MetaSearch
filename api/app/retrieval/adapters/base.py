"""Adapter contract for search engines (docs/03-search-pipeline.md §2)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.retrieval.models import RawResult


class AdapterError(Exception):
    """An adapter failed; the pipeline degrades gracefully instead of failing."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class SearchAdapter(Protocol):
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
        """Return results for ``query`` or raise ``AdapterError``."""
