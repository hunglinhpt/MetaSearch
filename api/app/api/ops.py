"""Liveness, readiness and metrics endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import functools
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from redis.asyncio import Redis
from sqlalchemy import text

from app.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import READINESS, render_metrics
from app.db.session import get_engine
from app.http import get_http_client

logger = get_logger(__name__)

router = APIRouter(tags=["ops"])

# Set by the lifespan as soon as shutdown starts: from then on the process is not ready.
_shutting_down = False
_redis_probe: Redis | None = None


def mark_shutting_down() -> None:
    global _shutting_down
    _shutting_down = True


def reset_readiness_state() -> None:
    global _shutting_down
    _shutting_down = False


async def close_readiness_clients() -> None:
    global _redis_probe
    if _redis_probe is not None:
        with contextlib.suppress(Exception):
            await _redis_probe.aclose()
        _redis_probe = None


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _check_postgres() -> dict[str, Any]:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


async def _check_redis() -> dict[str, Any]:
    global _redis_probe
    settings = get_settings()
    url = (settings.redis_url or "").strip()
    if not url:
        # No Redis configured (the Lite example): cache and rate limit run in process.
        return {"ok": True, "skipped": "redis_not_configured"}
    # Below the outer probe limit, so a silent Redis ends here and the Lite branch can run.
    socket_s = settings.readiness_timeout_s * 0.8
    try:
        if _redis_probe is None:
            # One client for every probe; a short socket timeout so a silent Redis cannot hang it.
            _redis_probe = Redis.from_url(
                url, socket_connect_timeout=socket_s, socket_timeout=socket_s
            )
        await _redis_probe.ping()
        return {"ok": True}
    except Exception as exc:
        if not settings.searxng_enabled:
            # Lite profile runs with in-process cache/rate-limit fallback when Redis is absent
            return {"ok": True, "skipped": "optional_in_lite_mode", "error": type(exc).__name__}
        return {"ok": False, "error": type(exc).__name__}


async def _check_searxng() -> dict[str, Any]:
    settings = get_settings()
    if not settings.searxng_enabled:
        return {"ok": True, "skipped": "searxng_disabled"}
    try:
        client = get_http_client()
        resp = await client.get(
            f"{settings.searxng_url.rstrip('/')}/healthz",
            timeout=settings.readiness_timeout_s,
        )
        return {"ok": resp.status_code < 500, "status": resp.status_code}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


async def collect_readiness() -> dict[str, Any]:
    """Probe every critical dependency concurrently."""
    limit = get_settings().readiness_timeout_s

    async def bounded(check: Any) -> dict[str, Any]:
        try:
            async with asyncio.timeout(limit):
                return await check
        except TimeoutError:
            return {"ok": False, "error": "timeout"}

    postgres, redis, searxng = await asyncio.gather(
        bounded(_check_postgres()), bounded(_check_redis()), bounded(_check_searxng())
    )
    checks = {"postgres": postgres, "redis": redis, "searxng": searxng}
    ready = all(check["ok"] for check in checks.values())
    return {"ready": ready, "checks": checks}


@router.get("/readyz", summary="Readiness probe with degraded state (WP-14)")
async def readyz(response: Response, strict: bool = False) -> dict[str, Any]:
    """ready 200; degraded 200 (a dependency is down, search still works; 503 with strict);
    not_ready 503 (no usable engine); shutting_down 503."""
    result = await collect_readiness()
    result["schema_version"] = schema_version()
    if _shutting_down:
        result["status"] = "shutting_down"
        READINESS.set(0)
        response.status_code = 503
        return result
    if not await _any_engine_usable():
        result["status"] = "not_ready"
        READINESS.set(0)
        response.status_code = 503
        return result
    READINESS.set(1)
    if result["ready"]:
        result["status"] = "ready"
        return result
    result["status"] = "degraded"
    if strict:
        response.status_code = 503
    return result


async def _any_engine_usable() -> bool:
    """At least one engine both has an adapter and is enabled in the engine store."""
    from app.api import deps
    from app.retrieval.registry import build_adapters

    adapters = build_adapters(get_settings())
    try:
        snapshot = await deps.get_engine_store().snapshot()
    except Exception:  # store down: fall back to "an adapter exists"
        return bool(adapters)
    return any(cfg.enabled for engine_id, cfg in snapshot.items() if engine_id in adapters)


@functools.lru_cache(maxsize=1)
def schema_version() -> str | None:
    """The Alembic head this build ships, read from the migration scripts."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        api_dir = Path(__file__).resolve().parents[2]
        cfg = Config(str(api_dir / "alembic.ini"))
        cfg.set_main_option("script_location", str(api_dir / "alembic"))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception as exc:  # missing scripts must not break the probe
        logger.warning("schema_version_unknown", error=type(exc).__name__)
        return None


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    settings = get_settings()
    from app.core.errors import ErrorCode, MetaSearchError

    if settings.auth_enabled:
        from app.core.auth import authenticate

        raw_auth = request.headers.get("authorization", "").strip()
        bearer = raw_auth[7:].strip() if raw_auth[:7].lower() == "bearer " else raw_auth
        if not bearer:
            raise MetaSearchError(
                ErrorCode.UNAUTHORIZED, "metrics require an admin key", status_code=403
            )
        try:
            ctx = await authenticate(bearer)
        except Exception as exc:
            raise MetaSearchError(
                ErrorCode.UNAUTHORIZED, "metrics require an admin key", status_code=403
            ) from exc
        if ctx.role != "admin":
            raise MetaSearchError(
                ErrorCode.UNAUTHORIZED, "metrics require an admin key", status_code=403
            )
    else:
        # No-auth deployments keep metrics loopback-only.
        client_host = request.client.host if request.client else "?"
        if client_host not in ("127.0.0.1", "::1", "testclient"):
            raise MetaSearchError(
                ErrorCode.UNAUTHORIZED, "metrics restricted to loopback", status_code=403
            )
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")
