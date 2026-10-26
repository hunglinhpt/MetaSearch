"""Engine registry — which engines exist, which are enabled (Phase 1: env-based).

Phase 2 swaps the source for the ``engines`` table + Redis cache with
``PATCH /v1/engines/{id}`` hot config; the read interface below stays the same.
"""

from __future__ import annotations

from app.config import Settings
from app.retrieval.adapters.base import SearchAdapter
from app.retrieval.adapters.searxng import SearXNGAdapter
from app.retrieval.models import EngineConfig


class EngineRegistry:
    """Immutable snapshot of engine configs + their adapter instances."""

    def __init__(self, entries: list[tuple[EngineConfig, SearchAdapter]]) -> None:
        # Preserve declaration order; dict keeps insertion order.
        self._entries: dict[str, tuple[EngineConfig, SearchAdapter]] = {
            cfg.id: (cfg, adapter) for cfg, adapter in entries
        }

    @classmethod
    def from_settings(cls, settings: Settings) -> EngineRegistry:
        """Build the Phase 1 registry.

        SearXNG is always enabled (self-hosted, no key). API-key engines
        (Tavily/Serper/Brave) appear as disabled entries when their key is
        missing so /v1/engines shows what *can* be turned on.
        """
        entries: list[tuple[EngineConfig, SearchAdapter]] = []

        searxng_cfg = EngineConfig(
            id="searxng",
            name="SearXNG (70+ engines)",
            category="general",
            weight=1.0,
            enabled=True,
            timeout_ms=int(settings.searxng_timeout_s * 1000),
            max_results=15,
            requires_key=False,
        )
        entries.append((searxng_cfg, SearXNGAdapter(settings.searxng_url)))

        # Phase 2+ adapters. Declared but disabled without keys; adapter classes
        # land together with the DB-backed hot config.
        for engine_id, name, key, requires in (
            ("tavily", "Tavily API", settings.tavily_api_key, True),
            ("serper", "Serper.dev (Google)", settings.serper_api_key, True),
            ("brave", "Brave Search API", settings.brave_api_key, True),
        ):
            entries.append(
                (
                    EngineConfig(
                        id=engine_id,
                        name=name,
                        category="general",
                        enabled=bool(key),
                        requires_key=requires,
                    ),
                    _NullAdapter(engine_id),
                )
            )
        return cls(entries)

    def configs(self) -> dict[str, EngineConfig]:
        return {engine_id: cfg for engine_id, (cfg, _) in self._entries.items()}

    def all_entries(self) -> list[tuple[EngineConfig, SearchAdapter]]:
        """Every registered engine, enabled or not (declaration order)."""
        return list(self._entries.values())

    def enabled(self) -> list[tuple[EngineConfig, SearchAdapter]]:
        """Enabled engines that have a real adapter, in declaration order."""
        return [
            (cfg, adapter)
            for cfg, adapter in self._entries.values()
            if cfg.enabled and not getattr(adapter, "is_null", False)
        ]

    def get(self, engine_id: str) -> tuple[EngineConfig, SearchAdapter] | None:
        return self._entries.get(engine_id)


class _NullAdapter:
    """Placeholder for engines whose adapter class is not implemented yet."""

    is_null = True

    def __init__(self, engine_id: str) -> None:
        self.id = engine_id

    async def search(self, *args: object, **kwargs: object) -> list:  # pragma: no cover
        raise NotImplementedError(f"adapter for {self.id} lands in Phase 2")
