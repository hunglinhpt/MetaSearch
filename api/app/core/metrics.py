"""Prometheus metrics registry (see docs/09-observability-eval.md)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "metasearch_http_requests_total",
    "HTTP requests handled",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

HTTP_LATENCY = Histogram(
    "metasearch_http_request_duration_seconds",
    "HTTP request latency",
    labelnames=("method", "path"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30),
    registry=REGISTRY,
)

ENGINE_CALLS = Counter(
    "metasearch_engine_calls_total",
    "Upstream search engine calls",
    labelnames=("engine", "outcome"),
    registry=REGISTRY,
)

ENGINE_LATENCY = Histogram(
    "metasearch_engine_duration_seconds",
    "Upstream search engine latency",
    labelnames=("engine",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8),
    registry=REGISTRY,
)

# WP-12: single cache counter with a `kind` label so PromQL can compute
# per-kind hit rates (search, answer, page, negative, semantic).
CACHE_EVENTS = Counter(
    "metasearch_cache_events_total",
    "Cache hits and misses",
    labelnames=("kind", "outcome"),
    registry=REGISTRY,
)

LLM_TOKENS = Counter(
    "metasearch_llm_tokens_total",
    "LLM tokens consumed",
    labelnames=("model", "direction"),
    registry=REGISTRY,
)

LLM_COST = Counter(
    "metasearch_llm_cost_usd_total",
    "Estimated LLM spend in USD",
    labelnames=("model",),
    registry=REGISTRY,
)

READINESS = Gauge(
    "metasearch_readiness",
    "1 when the service is ready to serve traffic",
    registry=REGISTRY,
)

# WP-12 observability additions (docs/14 03-work-packages.md).
EVENT_LOOP_LAG = Histogram(
    "metasearch_event_loop_lag_seconds",
    "Event loop lag measured by a periodic probe",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=REGISTRY,
)
INFLIGHT_REQUESTS = Gauge(
    "metasearch_inflight_requests",
    "Requests currently in flight",
    labelnames=("route_class",),
    registry=REGISTRY,
)
STAGE_DURATION = Histogram(
    "metasearch_stage_duration_seconds",
    "Per-stage pipeline duration",
    labelnames=("stage",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 180),
    registry=REGISTRY,
)
SCRAPE_TOTAL = Counter(
    "metasearch_scrape_total",
    "Scrape attempts by tier and outcome",
    labelnames=("tier", "outcome"),
    registry=REGISTRY,
)
LLM_REQUESTS = Counter(
    "metasearch_llm_requests_total",
    "LLM upstream calls by provider and outcome",
    labelnames=("provider", "outcome"),
    registry=REGISTRY,
)
LLM_TTFT = Histogram(
    "metasearch_llm_ttft_seconds",
    "Time to first LLM token",
    labelnames=("provider",),
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
    registry=REGISTRY,
)
HTTP_POOL_IN_USE = Gauge(
    "metasearch_http_pool_in_use",
    "Connections currently checked out of the shared HTTP pool",
    labelnames=("pool",),
    registry=REGISTRY,
)
BACKGROUND_QUEUE_DEPTH = Gauge(
    "metasearch_background_queue_depth",
    "Depth of internal background queues",
    labelnames=("queue",),
    registry=REGISTRY,
)
ADMISSION_REJECTED = Counter(
    "metasearch_admission_rejected_total",
    "Requests rejected by admission control",
    labelnames=("route_class",),
    registry=REGISTRY,
)
BREAKER_STATE = Gauge(
    "metasearch_breaker_state",
    "Circuit breaker state: 0=closed, 1=half_open, 2=open",
    labelnames=("engine",),
    registry=REGISTRY,
)
PROCESS_RSS = Gauge(
    "metasearch_process_rss_bytes",
    "Process resident set size in bytes",
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    """Serialize the registry in Prometheus text exposition format."""
    return generate_latest(REGISTRY)
