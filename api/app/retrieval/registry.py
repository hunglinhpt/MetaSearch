"""Engine registry — pairs engine configs (hot-reloadable via EngineStore)
with their adapter instances.

Phase 2: configs live in an ``EngineStore`` (Postgres-backed with in-memory
fallback); ``PATCH /v1/engines/{id}`` takes effect on the next request without
a restart. Engines without an implemented adapter (serper, brave) resolve to
``adapter=None`` and are never fanned out.
"""

from __future__ import annotations

from app.config import Settings
from app.orchestration.engine_store import (
    DbEngineStore,
    EngineStore,
    InMemoryEngineStore,
)
from app.retrieval.adapters.arxiv import ArxivAdapter
from app.retrieval.adapters.base import SearchAdapter
from app.retrieval.adapters.brave import BraveAdapter
from app.retrieval.adapters.ddg import DuckDuckGoAdapter
from app.retrieval.adapters.github import GitHubAdapter
from app.retrieval.adapters.huggingface import HuggingFaceAdapter
from app.retrieval.adapters.pubmed import PubMedAdapter
from app.retrieval.adapters.reddit import RedditAdapter
from app.retrieval.adapters.searxng import SearXNGAdapter
from app.retrieval.adapters.sec_edgar import SecEdgarAdapter
from app.retrieval.adapters.semanticscholar import SemanticScholarAdapter
from app.retrieval.adapters.serper import SerperAdapter
from app.retrieval.adapters.stackoverflow import StackOverflowAdapter
from app.retrieval.adapters.tavily import TavilyAdapter
from app.retrieval.adapters.wikipedia import WikipediaAdapter
from app.retrieval.adapters.yahoo_finance import YahooFinanceAdapter
from app.retrieval.models import EngineConfig


class EngineRegistry:
    """Reads config snapshots from the store; adapters are fixed per process."""

    def __init__(self, store: EngineStore, adapters: dict[str, SearchAdapter]) -> None:
        self.store = store
        self._adapters = adapters

    async def enabled(self) -> list[tuple[EngineConfig, SearchAdapter]]:
        """Enabled engines that have a real adapter, sorted by weight desc."""
        snapshot = await self.store.snapshot()
        out = [
            (cfg, self._adapters[cfg.id])
            for cfg in snapshot.values()
            if cfg.enabled and cfg.id in self._adapters
        ]
        out.sort(key=lambda pair: -pair[0].weight)
        return out

    async def configs(self) -> dict[str, EngineConfig]:
        return await self.store.snapshot()

    async def all_entries(self) -> list[tuple[EngineConfig, SearchAdapter | None]]:
        snapshot = await self.store.snapshot()
        return [(cfg, self._adapters.get(cfg.id)) for cfg in snapshot.values()]

    async def get(self, engine_id: str) -> tuple[EngineConfig, SearchAdapter | None] | None:
        cfg = (await self.store.snapshot()).get(engine_id)
        if cfg is None:
            return None
        return cfg, self._adapters.get(engine_id)


def build_adapters(settings: Settings) -> dict[str, SearchAdapter]:
    """One adapter instance per implemented engine with credentials."""
    adapters: dict[str, SearchAdapter] = {
        "searxng": SearXNGAdapter(settings.searxng_url),
        "ddg": DuckDuckGoAdapter(),
        "arxiv": ArxivAdapter(),
        "wikipedia": WikipediaAdapter(),
        "stackoverflow": StackOverflowAdapter(),
        "reddit": RedditAdapter(),
        "github": GitHubAdapter(token=settings.github_token),
        "pubmed": PubMedAdapter(api_key=settings.pubmed_api_key),
        "huggingface": HuggingFaceAdapter(),
        "semanticscholar": SemanticScholarAdapter(api_key=settings.semanticscholar_api_key),
        "yahoo_finance": YahooFinanceAdapter(),
        "sec_edgar": SecEdgarAdapter(user_agent=settings.sec_edgar_user_agent),
    }
    if settings.tavily_api_key:
        adapters["tavily"] = TavilyAdapter(settings.tavily_api_key)
    if settings.serper_api_key:
        adapters["serper"] = SerperAdapter(settings.serper_api_key)
    if settings.brave_api_key:
        adapters["brave"] = BraveAdapter(settings.brave_api_key)
    return adapters


def build_registry(settings: Settings) -> EngineRegistry:
    """DB-backed store with in-memory fallback (used when Postgres is down)."""
    fallback = InMemoryEngineStore.from_settings(settings)
    store: EngineStore = DbEngineStore(settings, fallback)
    return EngineRegistry(store, build_adapters(settings))


__all__ = [
    "EngineRegistry",
    "build_adapters",
    "build_registry",
]
