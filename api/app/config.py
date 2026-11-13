"""Application settings loaded from environment / .env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
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

    # --- server-side TTS (optional VoiceStudio backend, docs/17) -----------
    # Empty voicestudio_url = feature disabled (Web Speech API stays the default
    # in the browser). URL comes from env only, never from a request (SSRF).
    voicestudio_url: str = ""
    voicestudio_api_key: str = ""
    voicestudio_model: str = "omnivoice"
    # Empty = VoiceStudio server default voice/profile.
    voicestudio_voice: str = ""
    voicestudio_timeout_s: float = Field(default=30.0, gt=0)
    # Hard caps for /v1/tts/synthesize (request text and upstream audio bytes).
    tts_max_chars: int = Field(default=2000, ge=1)
    tts_max_audio_bytes: int = Field(default=8 * 1024 * 1024, ge=1)

    # --- v2.3.0: market compare/chart + watchlist + podcast (docs/18) --------
    # POST /v1/fa/compare caps (docs/18 B1).
    fa_compare_max_symbols: int = Field(default=5, ge=1, le=20)
    fa_compare_timeout_s: float = Field(default=8.0, gt=0)
    # POST /v1/market/ohlcv window (docs/18 B2); the feed itself serves ~240d.
    market_ohlcv_min_days: int = Field(default=30, ge=1)
    market_ohlcv_max_days: int = Field(default=365, ge=1)
    market_ohlcv_default_days: int = Field(default=90, ge=1)
    market_ohlcv_timeout_s: float = Field(default=5.0, gt=0)
    # Watchlist (docs/18 C): per-key item cap; alerts are poll-on-demand —
    # there is deliberately NO background poller burning search budget; the
    # client re-calls GET /v1/watchlist/alerts on its own schedule.
    watchlist_max_items: int = Field(default=20, ge=1, le=100)
    watchlist_alerts_k: int = Field(default=5, ge=1, le=10)
    watchlist_alerts_freshness_days: int = Field(default=1, ge=1, le=30)
    watchlist_alerts_timeout_ms: int = Field(default=8000, ge=500, le=30000)
    # Total wall-clock cap for one /alerts call (items fan out with bounded
    # concurrency underneath it) — a full watchlist can never hold an
    # admission slot for minutes.
    watchlist_alerts_total_timeout_s: float = Field(default=30.0, gt=0, le=300)
    watchlist_alerts_concurrency: int = Field(default=4, ge=1, le=10)
    # Podcast briefing (docs/18 D): script + audio file caps, retention.
    # Anchored to the api/ directory (not CWD) so audio never lands outside the
    # gitignored + guard-protected api/podcast_data/ runtime dir.
    podcast_data_dir: str = str(Path(__file__).resolve().parent.parent / "podcast_data")
    podcast_retention_days: int = Field(default=7, ge=1, le=90)
    podcast_max_chars: int = Field(default=4000, ge=200, le=20000)
    podcast_items_per_category: int = Field(default=3, ge=1, le=10)
    podcast_timeout_s: float = Field(default=120.0, gt=0)
    podcast_search_timeout_ms: int = Field(default=8000, ge=500, le=30000)
    # Polite-pool contact for keyless scholarly APIs (OpenAlex/Crossref, docs/19 W1).
    # Empty = omit mailto; both APIs still work, just without the polite-pool lane.
    global_search_mailto: str = ""
    # Optional public base URL for the RSS enclosure (behind a reverse proxy,
    # request.base_url would be the internal host). Empty = derive per request.
    public_base_url: str = ""

    # --- optional search providers (fallback when SearXNG is degraded) ----
    tavily_api_key: str = ""
    serper_api_key: str = ""
    brave_api_key: str = ""
    jina_api_key: str = ""
    github_token: str = ""
    pubmed_api_key: str = ""
    semanticscholar_api_key: str = ""
    sec_edgar_user_agent: str = "MetaSearch info@metasearch.ai"
    # FireAnt Bearer JWT (docs/15 §5.5). Empty = FireAnt source disabled.
    fa_fireant_token: str = ""

    # --- quality knobs (hardware-aware, see ADR-008) ----------------------
    # docs/20 P0-2: "bge" = BAAI/bge-reranker-v2-m3 cross-encoder, local ONNX
    # (int8 preferred), downloaded on first use when no local dir is given.
    reranker: Literal["heuristic", "onnx", "bge", "api"] = "heuristic"
    reranker_api_base_url: str = "https://api.jina.ai"
    # Directory holding model.onnx + tokenizer.json for RERANKER=onnx.
    onnx_reranker_dir: str = ""

    # Local dir for RERANKER=bge (model.onnx / model_int8.onnx + tokenizer.json).
    # Empty = snapshot_download from HF into model_cache_dir (lazy, optional dep).
    bge_reranker_dir: str = ""
    bge_reranker_hf_repo: str = "BAAI/bge-reranker-v2-m3"
    # Root for lazily downloaded models (empty = ~/.cache/metasearch/models).
    model_cache_dir: str = ""
    embedding_l2: Literal["off", "e5-small-int8", "bge-m3-api"] = "off"
    # OpenAI-compatible /embeddings endpoint (OpenAI, vLLM, TEI, Ollama).
    embedding_api_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "bge-m3"

    # --- v2.5 retrieval quality (docs/20 P0) --------------------------------
    # P0-1: weight of the RRF fusion signal in the merged score (0 = off).
    rrf_weight: float = 0.3
    # P0-3: multi-query expansion for deep/long queries (cheap LLM call,
    # fused via RRF). Variants only fan out to general engines to bound cost.
    query_expansion_enabled: bool = True
    query_expansion_max_variants: int = 2
    query_expansion_min_words: int = 6
    # P0-4: claim-level citation check (sentence entailment vs cited chunks).
    # Lexical overlap by default; a local ONNX NLI model is used when enabled
    # and loadable, otherwise we degrade to lexical — never fail the answer.
    claim_check_enabled: bool = True
    claim_entailment_threshold: float = 0.34
    citation_nli_enabled: bool = False
    citation_nli_dir: str = ""
    # P0-5: follow-up question suggestions after an answer.
    followups_enabled: bool = True
    followups_max: int = 3
    # P0-6: per-key custom rerank boosts (CRUD /v1/prefs/boosts).
    boosts_max_per_key: int = 50
    # P0-7: agent-friendly extras on /v1/search (contents/highlights/answer).
    search_extras_max_urls: int = 5
    search_extras_max_chars: int = 4000
    search_highlights_max: int = 2

    # --- v2.6 wave P1 (docs/20 §4, docs/22) ----------------------------------
    # P1-8: freshness decay half-lives in days. 30*ln2 ≈ 20.794 reproduces the
    # legacy exp(-age/30) curve exactly, so news behavior is unchanged by
    # default; non-news decays slower (45d half-life) so evergreen docs are
    # not punished like stale news, while genuinely old sources still sink.
    freshness_half_life_days_news: float = 20.794
    freshness_half_life_days_other: float = 45.0
    # P1-2: adaptive complexity router — heuristic auto-routes multi-hop /
    # comparative / long queries to deep fan-out without deep=true.
    adaptive_router_enabled: bool = True
    adaptive_router_min_words_deep: int = 14
    # P1-1: IRCoT research loop — reformulate sub-queries from evidence seen
    # so far, and gate early stopping on planner sub-question coverage.
    research_ircot_reformulate: bool = True
    research_coverage_gate_enabled: bool = True
    research_coverage_threshold: float = 0.6
    # P1-5: per-key memory (Mem0-lite): past queries + per-URL feedback used
    # as rerank boosts. DELETE /v1/memory wipes a key's memory (privacy).
    memory_enabled: bool = True
    memory_max_per_key: int = 500
    memory_feedback_boost: float = 0.15
    # P1-6: Spaces (named collections with pinned sources / domain scope).
    spaces_enabled: bool = True
    spaces_max_per_key: int = 20
    # P1-7: /v1/map + /v1/crawl site exploration bounds.
    map_max_urls: int = 200
    crawl_max_pages: int = 50
    crawl_max_depth: int = 2
    # P1-3: late chunking for long documents (segment-window approximation
    # of true late chunking on API embedding backends).
    late_chunking_enabled: bool = True
    late_chunking_doc_max_chars: int = 24_000
    late_chunking_segment_chars: int = 6_000
    late_chunking_segment_overlap_chars: int = 300
    # P1-4: hybrid dense+sparse (tsvector/BM25) chunk retrieval, RRF-fused.
    hybrid_search_enabled: bool = True
    hybrid_rrf_weight: float = 0.5

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
    # Graceful shutdown: how long to wait for answers already returned to reach the DB.
    shutdown_drain_s: float = 10.0
    # Lite (SQLite) runs retention inside the API; checked hourly, run at most daily.
    retention_enabled: bool = True
    retention_interval_s: float = 3600.0
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
    # Audit round 11: allow RFC1918/private-network scrapers (Prometheus in the
    # compose network) to read /metrics when auth is on. Off by default.
    metrics_allow_private: bool = False
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
            "rrf_weight": self.rrf_weight,
            "query_expansion": self.query_expansion_enabled,
            "claim_check": self.claim_check_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    ``METASEARCH_ENV_FILE`` names one more dotenv file, read after ``.env`` (the Windows
    service script passes its ``-EnvFile`` this way, so empty values and comments are
    parsed exactly as in ``.env``). Real environment variables still win.
    """
    extra = os.environ.get("METASEARCH_ENV_FILE", "").strip()
    if extra:
        return Settings(_env_file=(".env", extra))  # type: ignore[call-arg]
    return Settings()
