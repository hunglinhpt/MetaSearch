"""v1 router.

Phase 1 exposes search/answer/scrape/engines; deep research arrives in
Phase 3 (docs/12-roadmap-plan.md).
"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.api.v1 import answer, engines, search
from app.config import get_settings

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get("/meta", summary="Service metadata and giants configuration")
async def meta() -> dict:
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


router.include_router(search.router)
router.include_router(answer.router)
router.include_router(engines.router)
