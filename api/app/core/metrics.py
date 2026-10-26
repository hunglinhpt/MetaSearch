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

CACHE_EVENTS = Counter(
    "metasearch_cache_events_total",
    "Cache hits and misses",
    labelnames=("layer", "outcome"),
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


def render_metrics() -> bytes:
    """Serialize the registry in Prometheus text exposition format."""
    return generate_latest(REGISTRY)
