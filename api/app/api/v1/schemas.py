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


class ResponseFormat(BaseModel):
    """docs/20 P2-6: request a structured JSON answer over the search results.

    Defined BEFORE SearchRequest so the `response_format` forward reference
    resolves in this module's namespace (subclasses in other modules rebuild
    the parent with their own namespace — an unresolved ref there breaks
    OpenAPI generation).
    """

    type: Literal["json_schema"] = "json_schema"
    json_schema: dict[str, Any] = Field(min_length=1)
    # Optional human-readable name (Exa-style); echoed back in the response.
    name: str = Field(default="result", max_length=100)


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


def new_id() -> str:
    return str(uuid.uuid4())


# --- v2.6 wave P1 (docs/20 §4, docs/22) -----------------------------------------


class MemoryCreateRequest(BaseModel):
    """docs/20 P1-5: record a memory entry for the current key.

    kind="query" remembers a past query; kind="feedback" attaches a 👍/👎
    (weight ±1) to a result URL and turns into a rerank boost.
    """

    kind: Literal["query", "feedback"]
    text: str = Field(default="", max_length=4000)
    url: str | None = Field(default=None, max_length=2000)
    weight: float = Field(default=0.0, ge=-1.0, le=1.0)
    # docs/20 P2-4: feedback may carry {"variant": "a"|"b", "query_id": ...}
    # so /v1/ops/interleave can aggregate per-variant 👍/👎.
    payload: dict[str, Any] | None = None


class MemoryOut(BaseModel):
    id: str
    kind: str
    text: str = ""
    url: str | None = None
    weight: float = 0.0
    created_at: str | None = None


class MemoryListResponse(BaseModel):
    items: list[MemoryOut] = Field(default_factory=list)
    storage: str = "db"


class SpaceCreateRequest(BaseModel):
    """docs/20 P1-6: create a Space (named collection) for the current key."""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    # Pass 3 P2: per-item bounds + charset — a multi-MB "domain" was persisted
    # and echoed; good.com@evil.com would redirect the space crawl off-scope.
    domains: list[Annotated[str, StringConstraints(max_length=253)]] = Field(
        default_factory=list, max_length=50
    )
    pinned_urls: list[Annotated[str, StringConstraints(max_length=2000)]] = Field(
        default_factory=list, max_length=100
    )
    query_terms: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list, max_length=50
    )
    restrict: bool = True

    @field_validator("domains")
    @classmethod
    def _validate_domains(cls, value: list[str]) -> list[str]:
        import re as _re

        out = []
        for d in value:
            d = d.strip().lower().rstrip(".").removeprefix("www.")
            if not d or not _re.fullmatch(r"[a-z0-9.-]+", d) or ".." in d:
                raise ValueError(f"invalid domain: {d!r}")
            out.append(d)
        return out

    @field_validator("pinned_urls")
    @classmethod
    def _validate_pinned_urls(cls, value: list[str]) -> list[str]:
        from urllib.parse import urlsplit

        for u in value:
            parts = urlsplit(u.strip())
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"pinned URL must be http(s): {u!r}")
        return value


class SpaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    domains: list[str] | None = Field(default=None, max_length=50)
    pinned_urls: list[str] | None = Field(default=None, max_length=100)
    query_terms: list[str] | None = Field(default=None, max_length=50)
    restrict: bool | None = None


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
    """docs/20 P2-2: result of building a Space's knowledge graph."""

    space: str
    docs: int
    nodes: int
    edges: int


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
