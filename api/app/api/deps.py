"""Dependency providers for v1 endpoints (overridable in tests)."""

from __future__ import annotations

from functools import lru_cache

from app.config import Settings, get_settings
from app.extraction.scraper import Scraper
from app.llm.gateway import LLMGateway
from app.orchestration.pipeline import AnswerPipeline, SearchPipeline
from app.retrieval.registry import EngineRegistry


@lru_cache(maxsize=1)
def get_registry() -> EngineRegistry:
    return EngineRegistry.from_settings(get_settings())


@lru_cache(maxsize=1)
def get_scraper() -> Scraper:
    return Scraper(get_settings())


@lru_cache(maxsize=1)
def get_gateway() -> LLMGateway:
    return LLMGateway(get_settings())


@lru_cache(maxsize=1)
def get_search_pipeline() -> SearchPipeline:
    return SearchPipeline(get_settings(), get_registry())


@lru_cache(maxsize=1)
def get_answer_pipeline() -> AnswerPipeline:
    settings: Settings = get_settings()
    return AnswerPipeline(settings, get_search_pipeline(), get_scraper(), get_gateway())


def reset_pipelines() -> None:
    """Drop cached pipelines (used by tests and by settings changes)."""
    get_answer_pipeline.cache_clear()
    get_search_pipeline.cache_clear()
    get_gateway.cache_clear()
    get_scraper.cache_clear()
    get_registry.cache_clear()
