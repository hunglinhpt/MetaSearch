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
from app.tts.voicestudio import VoiceStudioClient


@lru_cache(maxsize=1)
def get_cache() -> CacheClient:
    return CacheClient.from_settings(get_settings())


@lru_cache(maxsize=1)
def get_tts_client() -> VoiceStudioClient:
    return VoiceStudioClient(get_settings())


def get_session_factory():
    """DB session factory as a patchable dependency (queries endpoint)."""
    from app.db.session import get_sessionmaker

    return get_sessionmaker()


@lru_cache(maxsize=1)
def get_engine_store() -> EngineStore:
    """DB-backed store with in-memory fallback; registry and endpoints share it.

    ``ENGINE_STORE_BACKEND=memory`` skips Postgres entirely (dev boxes and
    perf runs without a database).
    """
    from app.orchestration.engine_store import DbEngineStore

    settings = get_settings()
    fallback = InMemoryEngineStore.from_settings(settings)
    if settings.engine_store_backend == "memory":
        return fallback
    return DbEngineStore(settings, fallback)


@lru_cache(maxsize=1)
def get_health_registry():
    from app.retrieval.health import EngineBreakerRegistry

    return EngineBreakerRegistry(threshold=get_settings().engine_auto_disable_threshold)


@lru_cache(maxsize=1)
def get_health_writer():
    from app.retrieval.health import EngineHealthWriter

    return EngineHealthWriter(sessionmaker_provider=get_session_factory)


@lru_cache(maxsize=1)
def get_registry() -> EngineRegistry:
    return EngineRegistry(get_engine_store(), build_adapters(get_settings()))


@lru_cache(maxsize=1)
def get_scraper() -> Scraper:
    return Scraper(get_settings(), cache=get_cache())


@lru_cache(maxsize=1)
def get_gateway() -> LLMGateway:
    from app.http import get_http_client

    return LLMGateway(get_settings(), client=get_http_client("llm"))


@lru_cache(maxsize=1)
def get_embedder() -> EmbeddingClient:
    return EmbeddingClient(get_settings())


@lru_cache(maxsize=1)
def get_reranker():
    return build_reranker(get_settings())


@lru_cache(maxsize=1)
def get_search_pipeline() -> SearchPipeline:
    return SearchPipeline(
        get_settings(),
        get_registry(),
        cache=get_cache(),
        health_registry=get_health_registry(),
        health_writer=get_health_writer(),
    )


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


@lru_cache(maxsize=1)
def get_scanner():
    from app.orchestration.scheduler import AutomatedScanner

    return AutomatedScanner(get_registry())


def reset_pipelines() -> None:
    """Drop cached providers (used by tests and by settings changes)."""
    from app.llm.council import get_dream_rsi_store
    from app.llm.providers import get_runtime_llm_store

    get_runtime_llm_store().reset()
    get_dream_rsi_store().reset()
    for provider in (
        get_scanner,
        get_answer_pipeline,
        get_search_pipeline,
        get_health_registry,
        get_health_writer,
        get_reranker,
        get_embedder,
        get_gateway,
        get_scraper,
        get_registry,
        get_engine_store,
        get_cache,
        get_tts_client,
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
    "get_health_registry",
    "get_health_writer",
    "get_registry",
    "get_reranker",
    "get_scanner",
    "get_scraper",
    "get_search_pipeline",
    "reset_pipelines",
]
