"""Liveness, readiness and metrics endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Response
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


@router.get("/readyz", summary="Readiness probe")
async def readyz(response: Response) -> dict[str, Any]:
    result = await collect_readiness()
    READINESS.set(1 if result["ready"] else 0)
    if not result["ready"]:
        response.status_code = 503
    return result


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")
