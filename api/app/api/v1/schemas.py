"""Pydantic request/response models for v1 (docs/06-api-spec.md)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.llm.providers import LLMOverride


class TtsSynthesizeRequest(BaseModel):
    # Server-side TTS proxy (docs/17). The tighter runtime cap is
    # settings.tts_max_chars; this bound only keeps the request sane.
    text: str = Field(min_length=1, max_length=20_000)
    voice: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    response_format: Literal["mp3", "wav", "opus", "flac", "pcm"] = "mp3"


# --- v2.3.0 market endpoints (docs/18 B1/B2) ----------------------------------


class FaCompareRequest(BaseModel):
    """POST /v1/fa/compare — cross-source fundamentals for 1..N symbols."""

    # Item-level bound (audit 9): ensure_symbol rejects >15 chars anyway —
    # failing early at the schema costs one 400 instead of N parse work.
    symbols: list[Annotated[str, StringConstraints(max_length=15)]] = Field(
        min_length=1, max_length=20
    )
    # Per-item bound: ensure_symbol rejects >15 chars anyway — failing
    # early at the schema costs one 400 instead of N parse work (audit 9).
    timeout_ms: int | None = Field(default=None, ge=500, le=30000)
    format: Literal["json", "csv"] = "json"


class FaFieldOut(BaseModel):
    """One merged field with full provenance — never an invented number."""

    value: float
    source: str
    as_of: str | None = None
    validated: bool = False


class FaSymbolResult(BaseModel):
    symbol: str
    name: str | None = None
    exchange: str | None = None
    fields: dict[str, FaFieldOut] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    sources_used: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    as_of: str | None = None
    published_at: str | None = None


class FaCompareResponse(BaseModel):
    results: list[FaSymbolResult]
    latency_ms: int = 0


class MarketOhlcvRequest(BaseModel):
    """POST /v1/market/ohlcv — daily candles for the chart (docs/18 B2)."""

    symbol: str = Field(min_length=1, max_length=15)
    days: int | None = Field(default=None, ge=1, le=3650)
    # The effective window is the server-side clamp to [market_ohlcv_min/max_days];
    # le=3650 keeps the published contract bounded (review F-07) while clamping
    # out-of-range integer inputs preserves the clamping contract.
    timeout_ms: int | None = Field(default=None, ge=500, le=30000)

    @field_validator("days", mode="before")
    @classmethod
    def _clamp_days(cls, value: Any) -> Any:
        if isinstance(value, int) and not isinstance(value, bool):
            return max(1, min(3650, value))
        return value


class OhlcvCandle(BaseModel):
    # Field names match lightweight-charts CandlestickData so the UI can
    # pass them through without remapping.
    time: int  # epoch seconds — the feed's own timestamp, never synthesized
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketOhlcvResponse(BaseModel):
    symbol: str
    candles: list[OhlcvCandle]
    source_name: str
    source_url: str
    as_of: str | None = None  # ISO of the last candle's timestamp
    days: int
    latency_ms: int = 0


# --- Omni-Channel Bilingual Equity Sentiment Engine (VI / EN) ------------------


class MarketSentimentDocInput(BaseModel):
    """Pre-retrieved document item passed to bypass Phase 1 search fan-out."""

    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=500)
    snippet: str = Field(default="", max_length=4000)
    content: str = Field(default="", max_length=8_000)
    published_at: str | None = Field(default=None, max_length=64)
    lang: Literal["VI", "EN"] | None = None


class MarketOhlcvBarInput(BaseModel):
    """Bounded OHLCV candle supplied directly in request payloads."""

    time: int = Field(ge=1, le=4_102_444_800)
    open: float = Field(gt=0.0, le=1e12)
    high: float = Field(gt=0.0, le=1e12)
    low: float = Field(gt=0.0, le=1e12)
    close: float = Field(gt=0.0, le=1e12)
    volume: float = Field(ge=0.0, le=1e15)


class MarketProxiesSchemaInput(BaseModel):
    """Optional external market breadth, survey, options, dark-pool & flow proxies."""

    aaii_bullish_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    aaii_bearish_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    aaii_neutral_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    ii_bullish_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    ii_bearish_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    naaim_exposure_pct: float | None = Field(default=None, ge=-200.0, le=200.0)
    advancing_issues: int | None = Field(default=None, ge=0, le=100_000)
    declining_issues: int | None = Field(default=None, ge=0, le=100_000)
    advancing_volume: float | None = Field(default=None, ge=0.0, le=1e16)
    declining_volume: float | None = Field(default=None, ge=0.0, le=1e16)
    new_52w_highs: int | None = Field(default=None, ge=0, le=100_000)
    new_52w_lows: int | None = Field(default=None, ge=0, le=100_000)
    pct_above_ma50: float | None = Field(default=None, ge=0.0, le=100.0)
    pct_above_ma200: float | None = Field(default=None, ge=0.0, le=100.0)
    rana_series: list[Annotated[float, Field(ge=-1000.0, le=1000.0)]] | None = Field(
        default=None, max_length=252
    )
    cboe_put_call_equity: float | None = Field(default=None, ge=0.0, le=20.0)
    cboe_put_call_total: float | None = Field(default=None, ge=0.0, le=20.0)
    vix: float | None = Field(default=None, gt=0.0, le=200.0)
    vix_3m: float | None = Field(default=None, gt=0.0, le=200.0)
    vix_ma50: float | None = Field(default=None, gt=0.0, le=200.0)
    cboe_skew: float | None = Field(default=None, ge=50.0, le=300.0)
    off_exchange_short_dollar_vol: float | None = Field(default=None, ge=0.0, le=1e16)
    off_exchange_total_dollar_vol: float | None = Field(default=None, ge=0.0, le=1e16)
    dix_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    gex_billions: float | None = Field(default=None, ge=-500.0, le=500.0)
    margin_debt_curr: float | None = Field(default=None, ge=0.0, le=1e15)
    margin_debt_prev_year: float | None = Field(default=None, gt=0.0, le=1e15)
    margin_debt_yoy_pct: float | None = Field(default=None, ge=-100.0, le=1000.0)
    junk_yield_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    ig_yield_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    credit_spread_bps: float | None = Field(default=None, ge=-500.0, le=10_000.0)
    vn_foreign_net_value_bil: float | None = Field(default=None, ge=-100_000.0, le=100_000.0)
    vn_prop_net_value_bil: float | None = Field(default=None, ge=-100_000.0, le=100_000.0)
    social_weighted_24h: float | None = Field(default=None, ge=-1e9, le=1e9)
    social_mean_20d: float | None = Field(default=None, ge=-1e9, le=1e9)
    social_std_20d: float | None = Field(default=None, gt=0.0, le=1e9)
    insider_acquired_shares: float | None = Field(default=None, ge=0.0, le=1e13)
    insider_disposed_shares: float | None = Field(default=None, ge=0.0, le=1e13)
    source_note: str | None = Field(default=None, max_length=300)


class MarketSentimentRequest(BaseModel):
    """POST /v1/market/sentiment — Omni-Channel Bilingual Equity Sentiment Engine."""

    ticker: str = Field(min_length=1, max_length=15)
    # PASS-2 audit P1-2: free-text but operator-safe — quotes / OR / site:
    # must never reach the fan-out query grammar (search-operator injection).
    company_name: str | None = Field(
        default=None,
        max_length=120,
        pattern=r"^[\w\s.,&'\-]+$",
    )
    exchange: Literal["HOSE", "HSX", "HNX", "UPCOM", "NASDAQ", "NYSE", "OTHER"] | None = None
    k: int = Field(default=5, ge=1, le=20)
    max_queries: int = Field(default=10, ge=1, le=22)
    # PASS-2 P1-4: 50 x 8,000 chars stays inside the 1 MiB body cap
    retrieved_documents: list[MarketSentimentDocInput] | None = Field(default=None, max_length=50)
    ohlcv_bars: list[MarketOhlcvBarInput] | None = Field(default=None, max_length=500)
    market_proxies: MarketProxiesSchemaInput | None = None
    fetch_market_data: bool = False
    timeout_ms: int | None = Field(default=None, ge=1_000, le=60_000)


class SentimentEntityOut(BaseModel):
    ticker: str
    resolved_name: str
    exchange: Literal["HOSE", "HNX", "UPCOM", "NASDAQ", "NYSE", "OTHER"]
    timestamp_utc: str


class SentimentSummaryOut(BaseModel):
    composite_score: float
    institutional_vs_retail_spread: float
    label: Literal["STRONG_BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONG_BEARISH"]
    confidence: float
    market_regime: Literal[
        "STEALTH_ACCUMULATION",
        "MOMENTUM_MARKUP",
        "EUPHORIC_DISTRIBUTION_TRAP",
        "PANIC_CAPITULATION",
        "EVENT_DRIVEN_BINARY",
        "LOW_SIGNAL_NOISE",
    ]
    momentum_24h: Literal["IMPROVING", "STABLE", "DETERIORATING", "UNKNOWN"]


class OfficialMediaTelemetryOut(BaseModel):
    weighted_score: float
    hit_count: int
    key_catalysts: list[str] = Field(default_factory=list)


class VnSocialTelemetryOut(BaseModel):
    weighted_score: float
    platforms_covered: list[str] = Field(default_factory=list)
    hit_count: int
    crowd_narrative_vi: str
    trending_slang_vi: list[str] = Field(default_factory=list)


class GlobalSocialTelemetryOut(BaseModel):
    weighted_score: float
    platforms_covered: list[str] = Field(default_factory=list)
    hit_count: int
    crowd_narrative_en: str
    trending_tags_en: list[str] = Field(default_factory=list)


class SentimentNetworkTelemetryOut(BaseModel):
    official_and_media: OfficialMediaTelemetryOut
    vn_social_ecosystem: VnSocialTelemetryOut
    global_social_ecosystem: GlobalSocialTelemetryOut


class SocialIntegrityAuditOut(BaseModel):
    manipulation_or_seeding_detected: bool
    unverified_rumor_count: int
    audit_notes: str


class VerifiedDriverOut(BaseModel):
    rank: int
    headline_or_post_summary: str
    lang: Literal["VI", "EN"]
    platform_or_domain: str
    tier: Literal["TIER_1", "TIER_2", "TIER_3", "TIER_4"]
    freshness: Literal["BREAKING_6H", "INTRADAY_24H", "SHORT_3D", "WEEKLY_7D", "STALE"]
    effective_weight: float
    polarity: Literal["POSITIVE", "NEGATIVE", "NEUTRAL"]
    evidence_quote: str
    url: str | None = None


class SentimentPillarChecklistItemOut(BaseModel):
    pillar_id: str
    name: str
    standard_ref: str
    score_0_100: int
    status: Literal["BULLISH", "BEARISH", "NEUTRAL", "WARNING", "INSUFFICIENT_DATA"]
    evidence_count: int


class SentimentNlpTelemetryOut(BaseModel):
    tfidf_corpus_weighting: bool = False
    corpus_doc_count: int = 0
    tetlock_media_pessimism: float = 0.0
    consensus_polarization: float = 0.0
    source_diversity_entropy: float = 0.0


class OhlcvSentimentMetricsOut(BaseModel):
    available: bool = False
    bars_count: int = 0
    source_name: str | None = None
    source_url: str | None = None
    as_of: str | None = None
    last_close: float | None = None
    return_1d_pct: float | None = None
    return_20d_pct: float | None = None
    pct_vs_ma50: float | None = None
    pct_vs_ma125: float | None = None
    pct_vs_ma200: float | None = None
    range_pos_52w_pct: float | None = None
    rsi_14: float | None = None
    mfi_14: float | None = None
    cmf_21: float | None = None
    volume_ratio_20d: float | None = None
    realized_vol_20d_pct: float | None = None
    realized_vol_60d_pct: float | None = None
    vol_ratio_20_60: float | None = None
    momentum_quant_score: float | None = None
    breadth_flow_quant_score: float | None = None
    volatility_quant_score: float | None = None


class MarketProxyMetricsOut(BaseModel):
    available: bool = False
    aaii_bull_bear_spread: float | None = None
    ii_bull_bear_ratio: float | None = None
    naaim_exposure_pct: float | None = None
    arms_trin: float | None = None
    mcclellan_rana: float | None = None
    mcclellan_oscillator: float | None = None
    high_low_breadth_pct: float | None = None
    pct_above_ma50: float | None = None
    pct_above_ma200: float | None = None
    vix_term_ratio: float | None = None
    vix_term_regime: str | None = None
    vix_vs_ma50_pct: float | None = None
    cboe_put_call_ratio: float | None = None
    cboe_skew: float | None = None
    dix_pct: float | None = None
    gex_billions: float | None = None
    margin_debt_yoy_pct: float | None = None
    credit_spread_bps: float | None = None
    vn_foreign_net_value_bil: float | None = None
    vn_prop_net_value_bil: float | None = None
    social_s_score: float | None = None
    insider_mspr: float | None = None
    bofa_bull_bear_analog_0_10: float | None = None
    breadth_proxy_score: float | None = None
    volatility_options_proxy_score: float | None = None
    smart_money_proxy_score: float | None = None
    crowd_positioning_proxy_score: float | None = None
    source_note: str | None = None


class FundamentalMetricsOut(BaseModel):
    pe: float | None = None
    pb: float | None = None
    roe: float | None = None
    revenue_growth_yoy: float | None = None
    profit_growth_yoy: float | None = None
    debt_to_equity: float | None = None
    market_cap: float | None = None
    source: str | None = None
    source_url: str | None = None
    as_of: str | None = None


class SentimentQuantTelemetryOut(BaseModel):
    ohlcv: OhlcvSentimentMetricsOut | None = None
    market_proxies: MarketProxyMetricsOut | None = None
    fundamentals: FundamentalMetricsOut | None = None


class SentimentBenchmarkStandardsOut(BaseModel):
    # PASS-2 audit P2-5: nullable when the corresponding input class is
    # absent — docs/27 zero-fabrication (never emit placeholder 50/NEUTRAL).
    fear_greed_index: int | None = None
    fear_greed_zone: Literal[
        "EXTREME_FEAR", "FEAR", "NEUTRAL", "GREED", "EXTREME_GREED", "INSUFFICIENT_DATA"
    ]
    vietstock_psychology_state: Literal[
        "BI_QUAN_QUA_MUC",
        "THAN_TRONG_SO_HAI",
        "TRUNG_LAP_PHAN_VAN",
        "LAC_QUAN_HUNG_PHAN",
        "HUNG_PHAN_QUA_MUC",
        "THIEU_DU_LIEU",
    ]
    smart_money_confidence: float | None = None
    dumb_money_confidence: float | None = None
    smart_dumb_spread: float | None = None
    contrarian_signal: Literal[
        "CONTRARIAN_BULLISH_OPPORTUNITY",
        "CONTRARIAN_BEARISH_WARNING",
        "TREND_ALIGNED",
        "NEUTRAL",
    ]
    buzz_velocity: Literal["VIRAL_SURGE", "ELEVATED", "NORMAL", "DORMANT"]
    insider_signal: Literal["NET_BUYING", "NET_SELLING", "MIXED", "NONE"]
    uncertainty_and_litigation_score: float
    nlp_telemetry: SentimentNlpTelemetryOut | None = None
    quantitative_telemetry: SentimentQuantTelemetryOut | None = None
    seven_pillar_checklist: list[SentimentPillarChecklistItemOut] = Field(default_factory=list)


class MarketSentimentResponse(BaseModel):
    module: Literal["omni_channel_equity_sentiment_v2"] = "omni_channel_equity_sentiment_v2"
    entity: SentimentEntityOut
    sentiment_summary: SentimentSummaryOut
    benchmark_standards: SentimentBenchmarkStandardsOut
    network_telemetry: SentimentNetworkTelemetryOut
    social_integrity_audit: SocialIntegrityAuditOut
    top_verified_drivers: list[VerifiedDriverOut] = Field(default_factory=list)
    risk_circuit_breakers: list[str] = Field(default_factory=list)


# --- v2.3.0 watchlist (docs/18 C) ----------------------------------------------


class WatchlistCreateRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    label: str | None = Field(default=None, max_length=120)


class WatchlistItemOut(BaseModel):
    id: str
    query: str
    label: str | None = None
    created_at: str | None = None
    last_checked_at: str | None = None
    last_hash: str | None = None


class WatchlistListResponse(BaseModel):
    items: list[WatchlistItemOut] = Field(default_factory=list)
    # "db" | "memory" — when the DB is down the watchlist degrades to a bounded
    # in-memory store and the UI says so instead of pretending persistence.
    storage: str = "db"


class WatchlistAlertItem(BaseModel):
    id: str
    query: str
    label: str | None = None
    baseline_set: bool = False  # first check only records the baseline
    new_items: list[dict[str, Any]] = Field(default_factory=list)
    checked_at: str | None = None
    error: str | None = None


class WatchlistAlertsResponse(BaseModel):
    items: list[WatchlistAlertItem]
    total_new: int = 0
    latency_ms: int = 0
    storage: str = "db"


# --- v2.5.0 per-key boosts (docs/20 P0-6) ---------------------------------------


class BoostCreateRequest(BaseModel):
    kind: Literal["domain", "keyword"]
    target: str = Field(min_length=1, max_length=200)
    # domain: multiplier offset (-1 demote .. +1 boost); keyword: additive base.
    weight: float = Field(default=0.5, ge=-1.0, le=1.0)


class BoostOut(BaseModel):
    id: str
    kind: str
    target: str
    weight: float
    created_at: str | None = None


class BoostListResponse(BaseModel):
    items: list[BoostOut] = Field(default_factory=list)
    storage: str = "db"  # "db" | "memory" (DB down -> neutral empty list)


# --- v2.3.0 podcast briefing (docs/18 D) ----------------------------------------


class PodcastBriefingStatus(BaseModel):
    date: str
    status: str  # pending | running | ready | error
    error: str | None = None
    chars: int = 0
    audio_bytes: int = 0
    created_at: str | None = None


class ResponseFormat(BaseModel):
    """docs/20 P2-6: request a structured JSON answer over the search results.

    Defined BEFORE SearchRequest so the `response_format` forward reference
    resolves in this module's namespace (subclasses in other modules rebuild
    the parent with their own namespace — an unresolved ref there breaks
    OpenAPI generation).
    """

    type: Literal["json_schema"] = "json_schema"
    # Pass 1 P2#8: cap the SERIALIZED schema (a multi-MB schema is parsed +
    # validated inline on the loop before any LLM call).
    json_schema: dict[str, Any] = Field(min_length=1)

    @field_validator("json_schema")
    @classmethod
    def _validate_schema_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json as _json

        raw = _json.dumps(value, ensure_ascii=False)
        if len(raw) > 32_000:
            raise ValueError(f"json_schema too large: {len(raw)} chars (cap 32000)")
        depth = 0
        max_depth = 0
        for ch in raw:
            if ch in "{[":
                depth += 1
                max_depth = max(max_depth, depth)
            elif ch in "}]":
                depth -= 1
        if max_depth > 32:
            raise ValueError(f"json_schema nesting too deep: {max_depth} (cap 32)")
        return value

    # Optional human-readable name (Exa-style); echoed back in the response.
    name: str = Field(default="result", max_length=100)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    lang: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    k: int = Field(default=10, ge=1, le=50)
    # Review F-07: bounded lists of bounded identifiers (engine ids / category names).
    engines: list[Annotated[str, StringConstraints(max_length=64)]] | None = Field(
        default=None, max_length=50
    )
    categories: list[Annotated[str, StringConstraints(max_length=64)]] = Field(
        default_factory=lambda: ["general"], max_length=20
    )
    freshness_days: int | None = Field(default=None, ge=1, le=3650)
    include_snippets: bool = True
    news: bool = False
    timeout_ms: int | None = Field(default=None, ge=1_000, le=60_000)
    # Keep or drop results by host; "example.com" also matches its subdomains. A filter that
    # removes every result returns complete=false, not a confirmed empty answer.
    include_domains: list[Annotated[str, StringConstraints(max_length=253)]] | None = Field(
        default=None, max_length=50
    )
    exclude_domains: list[Annotated[str, StringConstraints(max_length=253)]] | None = Field(
        default=None, max_length=50
    )
    # docs/19 W4: deep=True fans out to ALL enabled engines (category filter is
    # ignored) so the answer is built from the widest possible result set.
    deep: bool = False
    # docs/20 P0-7: agent-friendly extras computed on the top results —
    # "contents" (scraped markdown, bounded), "highlights" (extractive top
    # sentences), "answer" (one short instant answer, response-level).
    # Named `extras` (not `include`) because ContextRequest overrides `include`.
    extras: list[Literal["contents", "highlights", "answer"]] | None = Field(
        default=None, max_length=3
    )
    # docs/20 P1-6: search within one of the caller's Spaces (slug). Domain
    # scope is applied post-cache; pinned URLs become per-key boosts.
    space: str | None = Field(default=None, max_length=100)
    # docs/20 P2-6: structured output — Exa-style response_format. When set,
    # the response carries a validated `structured` object extracted from the
    # top results (null + errors are reported, never fabricated).
    response_format: ResponseFormat | None = None


class CitationItem(BaseModel):
    n: int
    url: str
    title: str = ""
    chunk_id: str = ""


class AnswerRequest(SearchRequest):
    stream: bool = False
    # DAT-10 valve (docs/18): bounds acceptable answer staleness; presence
    # allows financial answers to be cached with ttl = min(answer_ttl, this).
    max_age_s: int | None = Field(default=None, ge=30, le=86_400)
    model: str | None = Field(default=None, max_length=150)
    llm: LLMOverride | None = None
    council_mode: bool = False
    council_endpoints: (
        list[LLMOverride | Annotated[str, StringConstraints(max_length=150)]] | None
    ) = Field(default=None, max_length=6)
    domain: str = Field(default="auto", max_length=60)


class ScrapeRequest(BaseModel):
    urls: list[Annotated[str, StringConstraints(max_length=2000)]] = Field(
        min_length=1, max_length=10
    )
    timeout_s: float = Field(default=8.0, ge=1, le=30)
    max_chars: int = Field(default=200_000, ge=1, le=1_000_000)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=60_000)


class ScrapeItem(BaseModel):
    url: str
    status: str  # ok | failed | blocked
    markdown: str = ""
    title: str = ""
    extractor_used: str = ""
    error: str | None = None
    truncated: bool = False


class SearchResponse(BaseModel):
    query_id: str
    results: list[dict[str, Any]]
    # docs/20 P0-7: instant answer over the top results (present only when
    # requested via extras=["answer"] and an LLM is configured).
    answer: str | None = None
    # docs/20 P2-6: structured object extracted from the top results when
    # response_format was given; null when extraction/validation failed
    # (errors in `structured_errors`).
    structured: dict[str, Any] | None = None
    structured_errors: list[str] = Field(default_factory=list)
    # docs/20 P2-4: online interleaving assignment for this query ("a"|"b"),
    # present only when INTERLEAVE_ENABLED=true.
    interleave: dict[str, Any] | None = None
    engines_used: list[str]
    engines_failed: list[str]
    engines_status: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int
    cached: bool = False
    complete: bool = True
    age_s: int | None = None


class AnswerResponse(BaseModel):
    answer_id: str
    answer_md: str
    citations: list[CitationItem] = Field(default_factory=list)
    # docs/20 P0-5: suggested follow-up questions (empty when disabled/unavailable).
    follow_ups: list[str] = Field(default_factory=list)
    # docs/20 P0-4: citation markers whose sentence is not supported by the
    # cited chunks (claim-level check). Reported, never hidden.
    unsupported_markers: list[int] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    model: str = ""
    latency_ms: int = 0
    status: str = "complete"
    cached: bool = False
    complete: bool = True
    council: dict[str, Any] | None = None
    skill: dict[str, Any] | None = None
    compression: dict[str, Any] | None = None


def new_id() -> str:
    return str(uuid.uuid4())


# --- v2.6 wave P1 (docs/20 §4, docs/22) -----------------------------------------


class MemoryCreateRequest(BaseModel):
    """docs/20 P1-5 + Claude-Mem: record a memory entry for the current key.

    kind="query" remembers a past query; kind="feedback" attaches a 👍/👎
    (weight ±1) to a result URL and turns into a rerank boost;
    kind="observation" stores a structured episodic memory payload.
    """

    kind: Literal["query", "feedback", "observation"]
    text: str = Field(default="", max_length=4000)
    url: str | None = Field(default=None, max_length=2000)
    weight: float = Field(default=0.0, ge=-1.0, le=1.0)
    # docs/20 P2-4: feedback may carry {"variant": "a"|"b", "query_id": ...}
    # so /v1/ops/interleave can aggregate per-variant 👍/👎.
    # Pass 1 P2#3: payload is bounded (≤1KB serialized) and `variant` is
    # constrained to "a"|"b" — unbounded payloads were echoed back as
    # arbitrary aggregation keys in /v1/ops/interleave.
    payload: dict[str, Any] | None = None

    @field_validator("payload")
    @classmethod
    def _validate_payload(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        import json as _json

        if value is None:
            return None
        variant = value.get("variant")
        if variant is not None and variant not in ("a", "b"):
            raise ValueError("payload.variant must be 'a' or 'b'")
        if len(_json.dumps(value, ensure_ascii=False)) > 1024:
            raise ValueError("payload too large (cap 1KB serialized)")
        return value


class MemoryOut(BaseModel):
    id: str
    kind: str
    text: str = ""
    url: str | None = None
    weight: float = 0.0
    payload: dict[str, Any] | None = None
    created_at: str | None = None


class MemoryListResponse(BaseModel):
    items: list[MemoryOut] = Field(default_factory=list)
    storage: str = "db"


class MemoryIndexItem(BaseModel):
    id: str
    kind: str
    title: str = ""
    snippet: str = ""
    score: float = 0.0
    created_at: str | None = None


class MemoryIndexResponse(BaseModel):
    query: str = ""
    items: list[MemoryIndexItem] = Field(default_factory=list)
    storage: str = "db"


class MemoryTimelineItem(BaseModel):
    id: str
    kind: str
    title: str = ""
    snippet: str = ""
    is_anchor: bool = False
    created_at: str | None = None


class MemoryTimelineResponse(BaseModel):
    anchor_id: str | None = None
    anchor_found: bool = False
    items: list[MemoryTimelineItem] = Field(default_factory=list)
    storage: str = "db"


class MemoryObservationsRequest(BaseModel):
    ids: list[Annotated[str, StringConstraints(max_length=64)]] = Field(min_length=1, max_length=50)


class MemoryObservationDetail(BaseModel):
    id: str
    kind: str
    title: str = ""
    summary: str = ""
    facts: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    domain_id: str = ""
    text: str = ""
    url: str | None = None
    weight: float = 0.0
    payload: dict[str, Any] | None = None
    created_at: str | None = None


class MemoryObservationsResponse(BaseModel):
    items: list[MemoryObservationDetail] = Field(default_factory=list)
    storage: str = "db"


def _validate_space_domains(value: list[str]) -> list[str]:
    import re as _re

    out = []
    for d in value:
        d = d.strip().lower().rstrip(".").removeprefix("www.")
        if not d or not _re.fullmatch(r"[a-z0-9.-]+", d) or ".." in d:
            raise ValueError(f"invalid domain: {d!r}")
        out.append(d)
    return out


def _validate_space_pinned_urls(value: list[str]) -> list[str]:
    from urllib.parse import urlsplit

    for u in value:
        parts = urlsplit(u.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"pinned URL must be http(s): {u!r}")
    return value


_SpaceDomainStr = Annotated[str, StringConstraints(max_length=253)]
_SpaceUrlStr = Annotated[str, StringConstraints(max_length=2000)]
_SpaceTermStr = Annotated[str, StringConstraints(max_length=200)]


class SpaceCreateRequest(BaseModel):
    """docs/20 P1-6: create a Space (named collection) for the current key."""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    # Pass 3 P2: per-item bounds + charset — a multi-MB "domain" was persisted
    # and echoed; good.com@evil.com would redirect the space crawl off-scope.
    domains: list[_SpaceDomainStr] = Field(default_factory=list, max_length=50)
    pinned_urls: list[_SpaceUrlStr] = Field(default_factory=list, max_length=100)
    query_terms: list[_SpaceTermStr] = Field(default_factory=list, max_length=50)
    restrict: bool = True

    @field_validator("domains")
    @classmethod
    def _validate_domains(cls, value: list[str]) -> list[str]:
        return _validate_space_domains(value)

    @field_validator("pinned_urls")
    @classmethod
    def _validate_pinned_urls(cls, value: list[str]) -> list[str]:
        return _validate_space_pinned_urls(value)


class SpaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    domains: list[_SpaceDomainStr] | None = Field(default=None, max_length=50)
    pinned_urls: list[_SpaceUrlStr] | None = Field(default=None, max_length=100)
    query_terms: list[_SpaceTermStr] | None = Field(default=None, max_length=50)
    restrict: bool | None = None

    @field_validator("domains")
    @classmethod
    def _validate_domains(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_space_domains(value)

    @field_validator("pinned_urls")
    @classmethod
    def _validate_pinned_urls(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_space_pinned_urls(value)


class SpaceOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str = ""
    domains: list[str] = Field(default_factory=list)
    pinned_urls: list[str] = Field(default_factory=list)
    query_terms: list[str] = Field(default_factory=list)
    restrict: bool = True
    last_crawl_at: str | None = None
    created_at: str | None = None


class SpaceListResponse(BaseModel):
    items: list[SpaceOut] = Field(default_factory=list)
    storage: str = "db"


class MapRequest(BaseModel):
    """docs/20 P1-7: discover URLs of a site (sitemap first, HTML links fallback)."""

    url: str = Field(min_length=1, max_length=2000)
    max_urls: int = Field(default=100, ge=1, le=1000)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=60_000)


class MapResponse(BaseModel):
    root: str
    urls: list[str] = Field(default_factory=list)
    source: str  # "sitemap" | "html" | "sitemap+html"
    truncated: bool = False


class CrawlRequest(BaseModel):
    """docs/20 P1-7: BFS-crawl a site (same-domain, robots+SSRF respected)."""

    url: str = Field(min_length=1, max_length=2000)
    depth: int = Field(default=1, ge=1, le=3)
    max_pages: int = Field(default=20, ge=1, le=100)
    max_chars: int = Field(default=100_000, ge=1, le=1_000_000)
    timeout_ms: int | None = Field(default=None, ge=1_000, le=60_000)


class CrawlPageItem(BaseModel):
    url: str
    status: str
    title: str = ""
    markdown: str = ""
    truncated: bool = False
    error: str | None = None


class CrawlResponse(BaseModel):
    root: str
    visited: int
    pages: list[CrawlPageItem] = Field(default_factory=list)
    indexed: bool = False  # pages were persisted to the document/chunk index
    latency_ms: int = 0


class GraphBuildResponse(BaseModel):
    """docs/20 P2-2: knowledge-graph build job for a Space.

    POST /graph/build enqueues the build and returns 202 with
    ``status="running"`` (counts 0); poll GET /graph/status — when the job
    finishes it reports ``status="ready"`` with the real counts (also derived
    from the kg tables, so it survives a restart).
    """

    space: str
    status: str = "ready"  # running | ready | error | none
    docs: int = 0
    nodes: int = 0
    edges: int = 0
    error: str | None = None
    created_at: str | None = None


class GraphQueryRequest(BaseModel):
    """docs/20 P2-2: entity-centric (local) graph query within a Space."""

    query: str = Field(min_length=1, max_length=2000)
    answer: bool = False  # also synthesize an answer over the subgraph with the LLM


class GraphQueryResponse(BaseModel):
    space: str
    matched_labels: list[str] = Field(default_factory=list)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    answer: str | None = None  # present when answer=true and the LLM answered
