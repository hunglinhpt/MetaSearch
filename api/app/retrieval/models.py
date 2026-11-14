"""Retrieval domain models shared by adapters, merger and scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(slots=True)
class EngineConfig:
    """Static configuration of one search engine (mirrors the ``engines`` table)."""

    id: str  # "searxng", "tavily", ...
    name: str
    category: str  # general | academic | developer | social | news
    weight: float = 1.0
    enabled: bool = True
    timeout_ms: int = 3000
    max_results: int = 15
    requires_key: bool = False
    adapter_path: str = ""  # import path of the adapter class, for the DB row
    disabled_reason: str | None = None


@dataclass(slots=True)
class RawResult:
    """One result as returned by a single adapter, before merge/score."""

    url: str
    title: str
    snippet: str
    adapter: str  # adapter id, e.g. "searxng"
    source_engines: list[str] = field(default_factory=list)  # underlying engines
    adapter_score: float | None = None  # engine-provided ranking score, if any
    published_at: datetime | None = None
    rank: int = 0  # position within the adapter's result list
    source_url: str | None = None  # nơi kiểm chứng được thật
    source_name: str | None = None  # "VNDIRECT dchart", "Simplize"
    as_of: datetime | None = None  # mốc của dữ liệu, không phải lúc gọi
    data_kind: Literal["observed", "derived", "reference", "computed"] | None = None


@dataclass(slots=True)
class ScoredResult:
    """Merged, deduplicated and scored result (API-facing shape)."""

    url: str
    canonical_url: str
    title: str
    snippet: str
    domain: str
    adapters: list[str] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)  # distinct underlying engines
    consensus: int = 0
    best_rank: int = 0
    adapter_score: float | None = None
    published_at: datetime | None = None
    source_url: str | None = None
    source_name: str | None = None
    as_of: datetime | None = None
    data_kind: Literal["observed", "derived", "reference", "computed"] | None = None

    # scoring signals (0..1 each), kept for observability/debugging
    rrf_score: float = 0.0  # v2.5 P0-1: reciprocal-rank-fusion signal (0..1)
    boosts_applied: int = 0  # v2.5 P0-6: how many per-key boost rules touched this item
    keyword_relevance: float = 0.0
    consensus_score: float = 0.0
    engine_weight_score: float = 0.0
    freshness: float = 0.0
    domain_authority: float = 0.0
    snippet_quality: float = 0.0
    score: float = 0.0
    # v3.0 P2-7: domain reputation tier ("official"|"primary"|"research"|
    # "reference"|"aggregator"|"community"|"unknown") — informational + feeds
    # domain_authority via the tier base score.
    domain_tier: str = "unknown"


@dataclass(slots=True)
class EngineStatus:
    """Outcome of one adapter call during fan-out."""

    engine: str
    status: str  # ok | error | timeout | skipped
    results: int = 0
    ms: int = 0
    error: str | None = None
