"""POST /v1/scrape (docs/06-api-spec.md §3)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import deps
from app.api.v1.schemas import ScrapeItem, ScrapeRequest
from app.extraction.scraper import PageContent

router = APIRouter(tags=["scrape"])


@router.post("/scrape", response_model=list[ScrapeItem], response_model_exclude_none=True)
async def scrape(request: ScrapeRequest) -> list[ScrapeItem]:
    from app.core.admission import admit
    from app.core.budget import create_budget_for_route, use_budget

    budget = create_budget_for_route("scrape", timeout_ms=request.timeout_ms)
    effective_timeout = (
        request.timeout_s
        if request.timeout_s is not None
        else (float(request.timeout_ms) / 1000.0 if request.timeout_ms else None)
    )
    async with admit("scrape"):
        with use_budget(budget):
            pages = await deps.get_scraper().scrape_many(request.urls, timeout_s=effective_timeout)
    items: list[ScrapeItem] = []
    for page in pages:
        if isinstance(page, PageContent):
            md = page.markdown
            is_truncated = False
            if len(md) > request.max_chars:
                md = md[: request.max_chars]
                is_truncated = True
            items.append(
                ScrapeItem(
                    url=page.url,
                    status="ok",
                    markdown=md,
                    title=page.title,
                    extractor_used=page.extractor_used,
                    truncated=is_truncated,
                )
            )
        else:  # PageFailure
            items.append(ScrapeItem(url=page.url, status=page.status, error=page.error))
    return items
