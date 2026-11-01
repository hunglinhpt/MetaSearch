"""v1 router.

Phase 1 exposes search/answer/scrape/engines; deep research arrives in
Phase 3 (docs/12-roadmap-plan.md).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import __version__
from app.api.v1 import answer, digests, engines, ops_status, queries, research, scrape, search
from app.config import get_settings
from app.core.auth import require_api_key

# AUTH_ENABLED=false (default) keeps local dev + Web UI open; exposed
# deployments set AUTH_ENABLED=true and every /v1 route requires a key.
_auth: list = [Depends(require_api_key)]

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


router.include_router(search.router, dependencies=_auth)
router.include_router(answer.router, dependencies=_auth)
router.include_router(engines.router, dependencies=_auth)
router.include_router(scrape.router, dependencies=_auth)
router.include_router(queries.router, dependencies=_auth)
router.include_router(ops_status.router, dependencies=_auth)
router.include_router(research.router, dependencies=_auth)
router.include_router(digests.router, dependencies=_auth)
