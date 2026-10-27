"""Dependency providers for v1 endpoints (overridable in tests)."""

from __future__ import annotations

from functools import lru_cache

from app.cache import CacheClient
from app.config import Settings, get_settings
from app.extraction.scraper import Scraper
from app.llm.embeddings import EmbeddingClient
from app.llm.gateway import LLMGateway
from app.orchestration.engine_store import EngineStore, InMemoryEngineStore
from app.orchestration.pipeline import AnswerPipeline, SearchPipeline
from app.retrieval.registry import EngineRegistry, build_adapters
from app.retrieval.reranker import build_reranker


@lru_cache(maxsize=1)
def get_cache() -> CacheClient:
    return CacheClient.from_settings(get_settings())


def get_session_factory():
    """DB session factory as a patchable dependency (queries endpoint)."""
    from app.db.session import get_sessionmaker

    return get_sessionmaker()


@lru_cache(maxsize=1)
def get_engine_store() -> EngineStore:
    """DB-backed store with in-memory fallback; registry and endpoints share it."""
    from app.orchestration.engine_store import DbEngineStore

    settings = get_settings()
    return DbEngineStore(settings, InMemoryEngineStore.from_settings(settings))


@lru_cache(maxsize=1)
def get_registry() -> EngineRegistry:
    return EngineRegistry(get_engine_store(), build_adapters(get_settings()))


@lru_cache(maxsize=1)
def get_scraper() -> Scraper:
    return Scraper(get_settings(), cache=get_cache())


@lru_cache(maxsize=1)
def get_gateway() -> LLMGateway:
    return LLMGateway(get_settings())


@lru_cache(maxsize=1)
def get_embedder() -> EmbeddingClient:
    return EmbeddingClient(get_settings())


@lru_cache(maxsize=1)
def get_reranker():
    return build_reranker(get_settings())


@lru_cache(maxsize=1)
def get_search_pipeline() -> SearchPipeline:
    return SearchPipeline(get_settings(), get_registry(), cache=get_cache())


@lru_cache(maxsize=1)
def get_answer_pipeline() -> AnswerPipeline:
    settings: Settings = get_settings()
    return AnswerPipeline(
        settings,
        get_search_pipeline(),
        get_scraper(),
        get_gateway(),
        get_reranker(),
        cache=get_cache(),
        embedder=get_embedder(),
    )


def reset_pipelines() -> None:
    """Drop cached providers (used by tests and by settings changes)."""
    for provider in (
        get_answer_pipeline,
        get_search_pipeline,
        get_reranker,
        get_embedder,
        get_gateway,
        get_scraper,
        get_registry,
        get_engine_store,
        get_cache,
    ):
        # Patched doubles in tests are plain functions without a cache.
        clear = getattr(provider, "cache_clear", None)
        if clear is not None:
            clear()


__all__ = [
    "Settings",
    "build_adapters",
    "get_answer_pipeline",
    "get_cache",
    "get_embedder",
    "get_engine_store",
    "get_gateway",
    "get_registry",
    "get_reranker",
    "get_scraper",
    "get_search_pipeline",
    "reset_pipelines",
]
