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
    # Comma-separated origins allowed to call the API from a browser (Web UI).
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # --- giants (upstream services) --------------------------------------
    # Lite profile (no Docker): SEARXNG_ENABLED=false switches the general
    # search to the keyless DuckDuckGo adapter + verticals.
    searxng_enabled: bool = True
    searxng_url: str = "http://searxng:8080"
    # Port must match deploy/docker-compose.yml (crawl4ai publishes 11222).
    crawl4ai_url: str = "http://crawl4ai:11222"
    crawl4ai_api_token: str = ""
    jina_reader_base_url: str = "https://r.jina.ai"
    # Jina reader works keyless at low rates (~20 rpm) — the Lite profile
    # relies on it as the JS-rendering tier without crawl4ai.
    jina_keyless: bool = True

    # --- storage ---------------------------------------------------------
    database_url: str = "postgresql+asyncpg://metasearch:metasearch@postgres:5432/metasearch"
    redis_url: str = "redis://redis:6379/0"
    # Bounded so a saturated pool fails fast instead of the 30s default.
    db_pool_timeout_s: float = 5.0

    # --- LLM gateway -----------------------------------------------------
    llm_provider: Literal["openai", "anthropic", "none"] = "none"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    llm_quick_model: str = "gpt-4o-mini"
    llm_report_model: str = "gpt-4o"
    llm_temperature: float = 0.2
    llm_answer_max_tokens: int = 1024
    # Context budget for /v1/answer: units depend on CONTEXT_TOKENIZER
    # (words by default; real tokens with CONTEXT_TOKENIZER=tiktoken).
    answer_context_budget: int = 6000

    # --- optional search providers (fallback when SearXNG is degraded) ----
    tavily_api_key: str = ""
    serper_api_key: str = ""
    brave_api_key: str = ""
    jina_api_key: str = ""
    github_token: str = ""
    pubmed_api_key: str = ""
    semanticscholar_api_key: str = ""
    sec_edgar_user_agent: str = "MetaSearch info@metasearch.ai"

    # --- quality knobs (hardware-aware, see ADR-008) ----------------------
    reranker: Literal["heuristic", "onnx", "api"] = "heuristic"
    reranker_api_base_url: str = "https://api.jina.ai"
    # Directory holding model.onnx + tokenizer.json for RERANKER=onnx.
    onnx_reranker_dir: str = ""
    embedding_l2: Literal["off", "e5-small-int8", "bge-m3-api"] = "off"
    # OpenAI-compatible /embeddings endpoint (OpenAI, vLLM, TEI, Ollama).
    embedding_api_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "bge-m3"

    # --- Phase 2: cache + engine management -------------------------------
    cache_enabled: bool = True
    cache_search_ttl_s: int = 1800  # docs/03 §7: 15-60m, news 15m
    cache_news_ttl_s: int = 900
    cache_answer_ttl_s: int = 7200  # docs/03 §7: 1-6h
    cache_page_ttl_s: int = 43200  # docs/03 §7: 6-24h
    # pgvector semantic cache similarity threshold (answer only).
    semantic_cache_threshold: float = 0.92
    # Engines table is polled this often so PATCH lands without restart.
    registry_refresh_s: float = 5.0
    # "db" reads engine config from Postgres (fallback to memory when down);
    # "memory" skips the DB entirely (dev without Postgres, perf runs).
    engine_store_backend: Literal["db", "memory"] = "db"
    # Any single engine-store DB operation waits at most this long before
    # falling back to the in-memory store — a partitioned Postgres must never
    # sit in the search critical path (perf finding: 3s connect waits).
    engine_store_db_timeout_s: float = 0.5
    engine_auto_disable_threshold: int = 3
    engine_recovery_probe_s: int = 300
    # Context budget tokenizer: words (proxy) or tiktoken when installed.
    context_tokenizer: Literal["words", "tiktoken"] = "words"

    # --- timeouts / limits ------------------------------------------------
    request_timeout_s: float = 30.0
    searxng_timeout_s: float = 3.0
    # Overall budget for the fan-out stage before falling back to partial results.
    search_fanout_budget_s: float = 5.0
    scrape_timeout_s: float = 8.0
    scrape_concurrency: int = 6
    max_scrape_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 3
    user_agent: str = "MetaSearchBot/1.0 (+https://metasearch.example/bot)"
    rate_limit_rpm: int = 60
    readiness_timeout_s: float = 2.0
    # Failed URLs are not retried for this long (negative cache, process-local).
    negative_cache_ttl_s: float = 3600.0
    robots_cache_ttl_s: float = 1800.0
    robots_timeout_s: float = 3.0

    # --- deep research budgets -------------------------------------------
    research_max_iterations: int = 3
    research_max_tokens: int = 150_000
    research_max_time_s: int = 180
    research_max_concurrent: int = 5  # docs/06 §8: 5 jobs/user; single-tenant

    # --- Phase 3: auth -----------------------------------------------------
    # Off by default: local dev + Web UI stay open. Set AUTH_ENABLED=true on
    # exposed deployments; then every /v1 request needs
    # `Authorization: Bearer msk_...` (create keys: metasearch keys create).
    auth_enabled: bool = False

    # --- Phase 4: optional observability + MCP ------------------------------
    # OTel needs `pip install '.[otel]'` plus standard OTEL_* env vars.
    otel_enabled: bool = False
    # Mount the MCP streamable-HTTP server at /mcp on this API process
    # (stdio mode runs separately via `python -m app.mcp`).
    mcp_enabled: bool = False
    # Langfuse LLM tracing (optional, httpx-based): all three must be set.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

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
