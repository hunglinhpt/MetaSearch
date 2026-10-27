"""POST /v1/scrape (docs/06-api-spec.md §3)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import deps
from app.api.v1.schemas import ScrapeItem, ScrapeRequest

router = APIRouter(tags=["scrape"])


@router.post("/scrape", response_model=list[ScrapeItem], response_model_exclude_none=True)
async def scrape(request: ScrapeRequest) -> list[ScrapeItem]:
    pages = await deps.get_scraper().scrape_many(request.urls, timeout_s=request.timeout_s)
    items: list[ScrapeItem] = []
    for page in pages:
        if hasattr(page, "markdown"):  # PageContent
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
