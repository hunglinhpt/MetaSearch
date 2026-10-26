from __future__ import annotations

from app.core.metrics import (
    CACHE_EVENTS,
    ENGINE_CALLS,
    HTTP_REQUESTS,
    LLM_TOKENS,
    READINESS,
    render_metrics,
)


def test_render_metrics_returns_prometheus_text() -> None:
    HTTP_REQUESTS.labels(method="GET", path="/healthz", status="200").inc()
    text = render_metrics().decode()
    assert "metasearch_http_requests_total" in text
    assert "# HELP" in text


def test_expected_metric_families_are_registered() -> None:
    text = render_metrics().decode()
    for name in (
        "metasearch_engine_calls_total",
        "metasearch_cache_events_total",
        "metasearch_llm_tokens_total",
        "metasearch_readiness",
    ):
        assert name in text


def test_metric_objects_are_usable() -> None:
    ENGINE_CALLS.labels(engine="searxng", outcome="success").inc()
    CACHE_EVENTS.labels(layer="l1", outcome="hit").inc()
    LLM_TOKENS.labels(model="gpt-4o-mini", direction="input").inc(10)
    READINESS.set(1)
    assert "metasearch_readiness 1.0" in render_metrics().decode()
