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
    # SEC-03: loopback by default; 0.0.0.0 now requires AUTH_ENABLED=true
    # (startup guard in main.py).
    api_host: str = "127.0.0.1"
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
    llm_provider: Literal[
        "openai",
        "anthropic",
        "openrouter",
        "pollinations",
        "gemini",
        "groq",
        "nvidia_nim",
        "cerebras",
        "sambanova",
        "mistral",
        "github_models",
        "cloudflare",
        "cohere",
        "chutes",
        "siliconflow",
        "hyperbolic",
        "huggingface",
        "ollama",
        "custom",
        "none",
    ] = "none"
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    # OpenRouter Free Router (https://openrouter.ai/openrouter/free) & multi-provider support
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_site_url: str = "https://github.com/hunglinhpt/MetaSearch"
    openrouter_app_name: str = "MetaSearch AI"
    openrouter_fallback_models: str = (
        "openrouter/free,"
        "meta-llama/llama-3.3-70b-instruct:free,"
        "deepseek/deepseek-chat-v3-0324:free,"
        "google/gemini-2.0-flash-exp:free,"
        "qwen/qwen-2.5-72b-instruct:free"
    )
    # Reputable Free LLM Endpoints (curated from nejib1/Free-LLM & awesome-freellm-apis)
    pollinations_base_url: str = "https://text.pollinations.ai/openai"
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    nvidia_nim_api_key: str = ""
    nvidia_nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    cerebras_api_key: str = ""
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    sambanova_api_key: str = ""
    sambanova_base_url: str = "https://api.sambanova.ai/v1"
    mistral_api_key: str = ""
    mistral_base_url: str = "https://api.mistral.ai/v1"
    github_models_base_url: str = "https://models.inference.ai.azure.com"
    cloudflare_api_key: str = ""
    cloudflare_account_id: str = ""
    cloudflare_base_url: str = ""
    cohere_api_key: str = ""
    cohere_base_url: str = "https://api.cohere.ai/compatibility/v1"
    chutes_api_key: str = ""
    chutes_base_url: str = "https://llm.chutes.ai/v1"
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    hyperbolic_api_key: str = ""
    hyperbolic_base_url: str = "https://api.hyperbolic.xyz/v1"
    hf_token: str = ""
    hf_base_url: str = "https://router.huggingface.co/v1"
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    # Custom / External System LLM Integration (LiteLLM, vLLM, Kong, Azure, internal proxy)
    custom_llm_protocol: Literal["openai", "anthropic"] = "openai"
    custom_llm_base_url: str = ""
    custom_llm_api_key: str = ""
    custom_llm_headers: str = ""
    # BYO (Bring-Your-Own) LLM Configuration & Security Guards
    llm_byo_enabled: bool = False
    llm_byo_allowed_hosts: str = ""
    llm_private_hosts: str = "127.0.0.1,localhost,ollama"
    # Automatic failover provider (e.g. "pollinations" or "openrouter") when primary hits 429/5xx
    llm_fallback_provider: str = ""
    # Multi-Endpoint LLM Council + Cross-Review + Dream-RSI settings
    llm_council_enabled: bool = False
    llm_council_endpoints: str = "pollinations:openai,pollinations:mistral,pollinations:llama"
    llm_dream_rsi_enabled: bool = True
    llm_quick_model: str = "gpt-4o-mini"
    llm_report_model: str = "gpt-4o"
    llm_temperature: float = 0.2
    llm_answer_max_tokens: int = 1024
    # WP-08: total upstream LLM calls per request (failover candidates included).
    llm_max_attempts: int = 3
    # WP-08: Deep Research report streaming token cap (DAT-07: was 1024 silently).
    research_report_max_tokens: int = 4096
    # WP-08: overall council wall-clock budget per run (seconds).
    council_budget_s: float = 90.0
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
    cache_local_max_bytes: int = 64 * 1024 * 1024
    cache_partial_ttl_s: int = 120
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
    # An LLM call cut short by the request budget only counts against the provider's breaker
    # when the provider had at least this long to answer; shorter cuts are our own deadline.
    llm_breaker_min_timeout_s: float = 5.0
    # Hard ceiling for one streamed answer, first byte to last, so a slow drip cannot hold
    # an admission slot forever.
    answer_stream_max_s: float = 120.0
    searxng_timeout_s: float = 3.0
    # Overall budget for the fan-out stage before falling back to partial results.
    search_fanout_budget_s: float = 5.0
    search_soft_budget_s: float = 1.5
    search_quorum_min_engines: int = 2
    scrape_min_pages: int = 3
    scrape_soft_budget_s: float = 2.5
    scrape_url_budget_s: float = 4.0
    scrape_max_urls: int = 8
    scrape_timeout_s: float = 8.0
    scrape_concurrency: int = 6
    scrape_global_concurrency: int = 24
    scrape_per_domain_concurrency: int = 2
    tls_trust_store: Literal["certifi", "system"] = "certifi"
    extract_concurrency: int = 2
    max_extract_bytes: int = 2 * 1024 * 1024
    max_page_markdown_chars: int = 200_000
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
    # Local-only escape hatch to reach admin endpoints without a key; startup
    # guard refuses it on non-loopback binds.
    dev_anon_admin: bool = False

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

    # --- Super-Agent Autonomous Engine (WP-10: Quarantined by default) ---
    superagent_enabled: bool = False

    # --- Automated Daily Briefings / Blog Scanner ---
    scheduler_enabled: bool = False
    scheduler_interval_hours: float = 24.0
    # Notification dispatcher (Telegram, Webhook, Discord)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""
    discord_webhook_url: str = ""

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
