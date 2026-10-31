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
    logger.info(
        "service_starting",
        version=__version__,
        environment=settings.environment,
        giants=settings.giants(),
    )
    try:
        yield
    finally:
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
        docs_url="/docs",
        openapi_url="/openapi.json",
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
        expose_headers=["x-request-id"],
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
