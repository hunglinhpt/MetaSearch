"""Pydantic request/response models for v1 (docs/06-api-spec.md)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

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
    days: int | None = Field(default=None, ge=1)
    # Upper bound is the server-side clamp to [market_ohlcv_min/max_days] —
    # schema ge=1 only rejects nonsense negatives early (audit 9).
    timeout_ms: int | None = Field(default=None, ge=500, le=30000)


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


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    lang: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    k: int = Field(default=10, ge=1, le=50)
    engines: list[str] | None = None
    categories: list[str] = Field(default_factory=lambda: ["general"])
    freshness_days: int | None = Field(default=None, ge=1, le=3650)
    include_snippets: bool = True
    news: bool = False
    timeout_ms: int | None = Field(default=None, ge=1_000, le=60_000)
    # Keep or drop results by host; "example.com" also matches its subdomains. A filter that
    # removes every result returns complete=false, not a confirmed empty answer.
    include_domains: list[str] | None = Field(default=None, max_length=50)
    exclude_domains: list[str] | None = Field(default=None, max_length=50)
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
    council_endpoints: list[LLMOverride | str] | None = None
    domain: str = Field(default="auto", max_length=60)


class ScrapeRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10)
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
    # requested via include=["answer"] and an LLM is configured).
    answer: str | None = None
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


def new_id() -> str:
    return str(uuid.uuid4())
