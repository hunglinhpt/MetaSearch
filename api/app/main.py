"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
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
    import sys

    settings = get_settings()
    configure_logging()
    from app.core.auth import validate_startup_auth

    bound_host = settings.api_host
    for i, arg in enumerate(sys.argv):
        if arg == "--host" and i + 1 < len(sys.argv):
            bound_host = sys.argv[i + 1]
            break
        elif arg.startswith("--host="):
            bound_host = arg.split("=", 1)[1]
            break

    problems = validate_startup_auth(
        api_host=bound_host,
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
    from app.http import get_ssl_context

    get_ssl_context()  # build it now: lazily it cost the first request ~250 ms

    from app.orchestration.persist_queue import get_persist_queue

    await get_persist_queue().start()
    # F3: retention on Lite (no worker) — run once at boot if due. Guarded by
    # retention_enabled (audit round 13: the boot purge previously ignored the
    # operator flag and deleted data against policy).
    from app.orchestration.retention import run_if_due

    try:
        if settings.retention_enabled:
            results = await run_if_due(
                last_run_path=str(Path(__file__).parent / ".retention_last_run")
            )
        if results:
            logger.info("retention_boot_completed", **results)
    except Exception as exc:
        logger.warning("retention_boot_failed", error=type(exc).__name__)

    from app.core.metrics import runtime_monitor

    monitor = asyncio.create_task(runtime_monitor(), name="runtime-monitor")

    retention_task = None
    if (
        settings.retention_enabled
        and settings.database_url.startswith("sqlite")
        and not settings.is_test
    ):
        from app.orchestration.retention import retention_loop

        db_path = settings.database_url.split("///", 1)[-1]
        marker = str(Path(db_path).with_suffix(".retention"))
        retention_task = asyncio.create_task(
            retention_loop(interval_s=settings.retention_interval_s, last_run_path=marker),
            name="retention-loop",
        )

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
    from app.api import ops as ops_module

    ops_module.reset_readiness_state()
    # MCP streamable-HTTP session manager (audit round 13): entered HERE —
    # the custom lifespan replaces legacy handlers, which is why the
    # add_event_handler wiring never ran and /mcp returned
    # "Task group is not initialized".
    mcp_session_cm = getattr(app.state, "mcp_session_cm", None)
    if mcp_session_cm is not None:
        await mcp_session_cm.__aenter__()
    try:
        yield
    finally:
        ops_module.mark_shutting_down()
        if mcp_session_cm is not None:
            with contextlib.suppress(Exception):
                await mcp_session_cm.__aexit__(None, None, None)
        if settings.scheduler_enabled and not settings.is_test:
            from app.api.deps import get_scanner

            get_scanner().stop_background_loop()
        await health_writer.stop()
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor
        if retention_task is not None:
            retention_task.cancel()
        try:
            # An answer already returned is written by the pipeline's background task after
            # the "done" event: wait for those first, then drain what they enqueued.
            from app.orchestration import persist
            from app.orchestration import pipeline as answer_pipeline
            from app.orchestration.persist_queue import get_persist_queue

            pending = [t for t in list(answer_pipeline._background_tasks) if not t.done()]
            if pending:
                await asyncio.wait(pending, timeout=settings.shutdown_drain_s)
            await persist.wait_background(settings.shutdown_drain_s)
            await get_persist_queue().drain(budget_s=settings.shutdown_drain_s)
            await get_persist_queue().stop()
        except Exception as exc:
            logger.warning("persist_queue_shutdown_error", error=type(exc).__name__)
        await dispose_engine()
        await dispose_redis()
        await ops_module.close_readiness_clients()
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
        docs_url=None,
        openapi_url=None,
    )
    from app.core.tracing import setup_tracing

    setup_tracing(app)

    async def _check_docs_access(request: Request) -> None:
        client_host = request.client.host if request.client else ""
        if client_host in ("127.0.0.1", "::1", "localhost", "testclient"):
            return
        from app.core.auth import authenticate
        from app.core.errors import ErrorCode, MetaSearchError

        raw_auth = request.headers.get("authorization", "").strip()
        bearer = raw_auth[7:].strip() if raw_auth[:7].lower() == "bearer " else raw_auth
        if not bearer:
            raise MetaSearchError(ErrorCode.UNAUTHORIZED, "admin key required", status_code=403)
        try:
            ctx = await authenticate(bearer)
        except Exception as exc:
            raise MetaSearchError(
                ErrorCode.UNAUTHORIZED, "admin key required", status_code=403
            ) from exc
        if ctx.role != "admin":
            raise MetaSearchError(ErrorCode.UNAUTHORIZED, "admin role required", status_code=403)

    if settings.environment != "prod":
        from fastapi.openapi.docs import get_swagger_ui_html
        from fastapi.openapi.utils import get_openapi

        @app.get("/openapi.json", include_in_schema=False)
        async def openapi_endpoint(request: Request):
            await _check_docs_access(request)
            return get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
            )

        @app.get("/docs", include_in_schema=False)
        async def docs_endpoint(request: Request):
            await _check_docs_access(request)
            return get_swagger_ui_html(
                openapi_url="/openapi.json", title=f"{app.title} - Swagger UI"
            )

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


def _mcp_auth_guard(inner):
    """ASGI wrapper enforcing the SAME api-key auth as /v1 routes.

    Audit round 11 P1: the mounted MCP sub-app bypassed router-level
    ``dependencies=_auth`` entirely — with AUTH_ENABLED=true the same port
    served unauthenticated search/scrape/research through the 5 MCP tools.
    require_api_key raises MetaSearchError (headers included) — render it.
    """

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        from starlette.requests import Request

        request = Request(scope, receive)
        try:
            from app.core.auth import require_api_key

            await require_api_key(request)
        except Exception as exc:
            from starlette.responses import JSONResponse

            from app.core.errors import MetaSearchError

            if isinstance(exc, MetaSearchError):
                status = exc.status_code or 401
                headers = getattr(exc, "headers", None) or {}
                payload = {
                    "error": {
                        "code": exc.code.value if hasattr(exc.code, "value") else str(exc.code),
                        "message": exc.message,
                    }
                }
            else:
                status = 401
                headers = {}
                payload = {"error": {"code": "unauthorized", "message": "missing API key"}}
            resp = JSONResponse(payload, status_code=status, headers=headers)
            await resp(scope, receive, send)
            return
        await inner(scope, receive, send)

    return guarded


def _mount_mcp(app: FastAPI) -> None:
    """Mount the MCP streamable-HTTP server at /mcp (optional dep)."""
    try:
        from app.mcp.server import build_server

        server = build_server()
        http_app = server.streamable_http_app()
        app.mount("/mcp", _mcp_auth_guard(http_app))
        # Audit round 11 P1 + round 13: FastAPI never runs MOUNTED sub-app
        # lifespans, and with an asynccontextmanager lifespan Starlette IGNORES
        # legacy startup/shutdown handlers — so add_event_handler was a no-op
        # and the transport stayed dead. The session manager is now entered by
        # the main lifespan (which reads app.state.mcp_session_cm around its
        # yield) and exited in its finally.
        app.state.mcp_session_cm = server.session_manager.run()
        logger.info("mcp_http_mounted", path="/mcp")
    except SystemExit as exc:
        logger.warning("mcp_mount_skipped", reason=str(exc))
    except Exception as exc:  # pragma: no cover - optional surface
        logger.warning("mcp_mount_failed", error=type(exc).__name__)


app = create_app()
