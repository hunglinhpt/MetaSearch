"""POST /v1/scrape (docs/06-api-spec.md §3)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import deps
from app.api.v1.schemas import ScrapeItem, ScrapeRequest
from app.core.errors import ErrorCode, MetaSearchError
from app.extraction.scraper import PageContent
from app.extraction.tls_policy import TLS_VERIFY_FAILED

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
            # Pass the opt-in only when set (scraper doubles/subclasses with the
            # pre-4.6 scrape_many signature keep working).
            extra = {"allow_insecure_escalation": True} if request.allow_insecure_escalation else {}
            pages = await deps.get_scraper().scrape_many(
                request.urls or [], timeout_s=effective_timeout, **extra
            )
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
                    tls_verified=page.url.lower().startswith("https://")
                    and not page.insecure_escalation,
                )
            )
        else:  # PageFailure
            items.append(ScrapeItem(url=page.url, status=page.status, error=page.error))
    # docs/35 M4: when EVERY requested URL was refused for an invalid certificate
    # the request as a whole failed for that reason -> 502 tls_verify_failed.
    # Mixed batches stay 200 with the per-item error (partial by design).
    if items and all(i.error == TLS_VERIFY_FAILED for i in items):
        raise MetaSearchError(
            ErrorCode.TLS_VERIFY_FAILED,
            "TLS certificate verification failed; content was not fetched through a "
            "fallback extractor (set allow_insecure_escalation=true to override)",
            details={"urls": [i.url for i in items][:10]},
        )
    return items
