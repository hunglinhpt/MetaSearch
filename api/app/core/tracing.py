"""Optional OpenTelemetry tracing (docs/09, Phase 4).

Install the extra group ``.[otel]`` and set ``OTEL_ENABLED=true`` plus the
standard OTEL_* env vars; without the packages or the flag everything here
is a no-op, so the dependency stays optional (ADR-007 giants-first).

    pip install '.[otel]'
    OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
"""

from __future__ import annotations

from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_configured = False


def setup_tracing(app) -> bool:
    """Instrument FastAPI + outbound httpx when enabled and installed."""
    global _configured
    settings = get_settings()
    if _configured or not settings.otel_enabled:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("otel_enabled_but_missing", hint="pip install '.[otel]'")
        return False

    provider = TracerProvider(resource=Resource.create({"service.name": "metasearch-api"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    _configured = True
    logger.info("otel_tracing_enabled")
    return True
