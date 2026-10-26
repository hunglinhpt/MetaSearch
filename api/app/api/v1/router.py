"""v1 router.

Phase 0 only exposes ``GET /v1/meta`` so the service can be smoke-tested
without a database. Search, answer, scrape, research and engine-registry
routes land in Phase 1-3 (see docs/12-roadmap-plan.md).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app import __version__
from app.config import get_settings

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get("/meta", summary="Service metadata and giants configuration")
async def meta() -> dict[str, Any]:
    settings = get_settings()
    return {
        "name": settings.app_name,
        "version": __version__,
        "environment": settings.environment,
        "giants": settings.giants(),
        "limits": {
            "rate_limit_rpm": settings.rate_limit_rpm,
            "searxng_timeout_s": settings.searxng_timeout_s,
            "scrape_timeout_s": settings.scrape_timeout_s,
            "scrape_concurrency": settings.scrape_concurrency,
        },
    }
