"""Liveness, readiness and metrics endpoints."""

from __future__ import annotations

import asyncio
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
    settings = get_settings()
    client: Redis | None = None
    try:
        client = Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        await client.ping()
        return {"ok": True}
    except Exception as exc:
        if not settings.searxng_enabled:
            # Lite profile runs with in-process cache/rate-limit fallback when Redis is absent
            return {"ok": True, "skipped": "optional_in_lite_mode", "error": type(exc).__name__}
        return {"ok": False, "error": type(exc).__name__}
    finally:
        if client is not None:
            await client.aclose()


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
    postgres, redis, searxng = await asyncio.gather(
        _check_postgres(), _check_redis(), _check_searxng()
    )
    checks = {"postgres": postgres, "redis": redis, "searxng": searxng}
    ready = all(check["ok"] for check in checks.values())
    return {"ready": ready, "checks": checks}


@router.get("/readyz", summary="Readiness probe with degraded state (WP-14)")
async def readyz(response: Response, strict: bool = False) -> dict[str, Any]:
    result = await collect_readiness()
    if result["ready"]:
        result["status"] = "ready"
        result["schema_version"] = 1
        READINESS.set(1)
        return result
    # WP-14: DB/Redis down = degraded (200), not_ready = 503.
    # "Not ready" means: no search engine usable or the process is shutting down.
    engines_usable = _any_engine_usable()
    if not engines_usable:
        result["status"] = "not_ready"
        READINESS.set(0)
        if strict:
            response.status_code = 503
        return result
    result["status"] = "degraded"
    result["schema_version"] = 1
    READINESS.set(1)  # still serving traffic
    if strict:
        response.status_code = 503
    return result


def _any_engine_usable() -> bool:
    from app.retrieval.registry import build_adapters

    adapters = build_adapters(get_settings())
    return bool(adapters)


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
