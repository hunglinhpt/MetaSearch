"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import ops
from app.api.v1.router import router as v1_router
from app.cache import dispose_redis
from app.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.db.session import dispose_engine
from app.http import dispose_http_client

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    from app.core.auth import validate_startup_auth

    problems = validate_startup_auth(
        api_host=settings.api_host,
        auth_enabled=settings.auth_enabled,
        environment=settings.environment,
        dev_anon_admin=settings.dev_anon_admin,
    )
    if problems:
        for problem in problems:
            logger.error("startup_auth_guard", problem=problem)
        raise SystemExit(
            "Refusing to start: "
            + " | ".join(problems)
            + " (bind 127.0.0.1 or set AUTH_ENABLED=true)"
        )
    if settings.database_url.startswith("sqlite"):
        from app.db.bootstrap import ensure_schema

        await ensure_schema()

    from app.api.deps import get_health_writer
    from app.orchestration.engine_store import clear_legacy_auto_disabled

    await clear_legacy_auto_disabled()
    health_writer = get_health_writer()
    if not settings.is_test:
        health_writer.start()

    logger.info(
        "service_starting",
        version=__version__,
        environment=settings.environment,
        giants=settings.giants(),
    )
    if settings.scheduler_enabled and not settings.is_test:
        from app.api.deps import get_scanner

        get_scanner().start_background_loop(settings.scheduler_interval_hours)
    try:
        yield
    finally:
        if settings.scheduler_enabled and not settings.is_test:
            from app.api.deps import get_scanner

            get_scanner().stop_background_loop()
        await health_writer.stop()
        await dispose_engine()
        await dispose_redis()
        await dispose_http_client()
        logger.info("service_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Thin orchestration layer over SearXNG (meta-search) and "
            "crawl4ai/trafilatura (extraction)."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "prod" else None,
        openapi_url="/openapi.json" if settings.environment != "prod" else None,
    )
    from app.core.tracing import setup_tracing

    setup_tracing(app)

    app.add_middleware(RequestContextMiddleware)
    # Browser clients (Web UI on :3000) call this API cross-origin; without
    # CORS every fetch from the UI fails in the browser.
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "x-request-id",
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "retry-after",
        ],
    )
    register_exception_handlers(app)
    app.include_router(ops.router)
    app.include_router(v1_router)
    if settings.mcp_enabled:
        _mount_mcp(app)
    return app


def _mount_mcp(app: FastAPI) -> None:
    """Mount the MCP streamable-HTTP server at /mcp (optional dep)."""
    try:
        from app.mcp.server import build_server

        app.mount("/mcp", build_server().streamable_http_app())
        logger.info("mcp_http_mounted", path="/mcp")
    except SystemExit as exc:
        logger.warning("mcp_mount_skipped", reason=str(exc))
    except Exception as exc:  # pragma: no cover - optional surface
        logger.warning("mcp_mount_failed", error=type(exc).__name__)


app = create_app()
