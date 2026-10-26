"""POST /v1/scrape (docs/06-api-spec.md §3) and GET /v1/engines (§5)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import get_registry, get_scraper
from app.api.v1.schemas import ScrapeItem, ScrapeRequest

router = APIRouter(tags=["scrape"])


@router.post("/scrape", response_model=list[ScrapeItem], response_model_exclude_none=True)
async def scrape(request: ScrapeRequest) -> list[ScrapeItem]:
    pages = await get_scraper().scrape_many(request.urls, timeout_s=request.timeout_s)
    items: list[ScrapeItem] = []
    for page in pages:
        if hasattr(page, "markdown"):
            items.append(
                ScrapeItem(
                    url=page.url,
                    status="ok",
                    markdown=page.markdown,
                    title=page.title,
                    extractor_used=page.extractor_used,
                )
            )
        else:  # PageFailure
            items.append(ScrapeItem(url=page.url, status=page.status, error=page.error))
    return items


@router.get("/engines")
async def engines() -> list[dict]:
    registry = get_registry()
    return [
        {
            "id": cfg.id,
            "name": cfg.name,
            "category": cfg.category,
            "weight": cfg.weight,
            "enabled": cfg.enabled,
            "timeout_ms": cfg.timeout_ms,
            "max_results": cfg.max_results,
            "requires_key": cfg.requires_key,
            "adapter_ready": not getattr(adapter, "is_null", False),
        }
        for cfg, adapter in registry.all_entries()
    ]
