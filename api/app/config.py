"""Application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Every value can be overridden with an environment variable of the same
    (case-insensitive) name, e.g. ``SEARXNG_URL``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- application -----------------------------------------------------
    app_name: str = "MetaSearch AI"
    environment: Literal["dev", "prod", "test"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    # Binds all interfaces inside the container; docker-compose publishes the
    # port on 127.0.0.1 only, so the host is not exposed.
    api_host: str = "0.0.0.0"  # nosec B104
    api_port: int = 8000

    # --- giants (upstream services) --------------------------------------
    searxng_url: str = "http://searxng:8080"
    crawl4ai_url: str = "http://crawl4ai:11235"
    crawl4ai_api_token: str = ""

    # --- storage ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://metasearch:metasearch@postgres:5432/metasearch"
    redis_url: str = "redis://redis:6379/0"

    # --- LLM gateway -----------------------------------------------------
    llm_provider: Literal["openai", "anthropic", "none"] = "none"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_quick_model: str = "gpt-4o-mini"
    llm_report_model: str = "gpt-4o"

    # --- optional search providers (fallback when SearXNG is degraded) ----
    tavily_api_key: str = ""
    serper_api_key: str = ""
    brave_api_key: str = ""
    jina_api_key: str = ""

    # --- quality knobs (hardware-aware, see ADR-008) ----------------------
    reranker: Literal["heuristic", "onnx", "api"] = "heuristic"
    embedding_l2: Literal["off", "e5-small-int8", "bge-m3-api"] = "off"

    # --- timeouts / limits ------------------------------------------------
    request_timeout_s: float = 30.0
    searxng_timeout_s: float = 3.0
    scrape_timeout_s: float = 8.0
    scrape_concurrency: int = 8
    max_scrape_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 3
    user_agent: str = "MetaSearchBot/1.0 (+https://metasearch.example/bot)"
    rate_limit_rpm: int = 60
    readiness_timeout_s: float = 2.0

    # --- deep research budgets -------------------------------------------
    research_max_iterations: int = 3
    research_max_tokens: int = 150_000
    research_max_time_s: int = 180

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    def giants(self) -> dict[str, str]:
        """Public description of the upstream giants in use."""
        return {
            "searxng_url": self.searxng_url,
            "crawl4ai_url": self.crawl4ai_url,
            "reranker": self.reranker,
            "embedding_l2": self.embedding_l2,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
