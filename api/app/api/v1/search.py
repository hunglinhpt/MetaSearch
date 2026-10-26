"""POST /v1/search (docs/06-api-spec.md §1)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import get_search_pipeline
from app.api.v1.schemas import SearchRequest, SearchResponse
from app.core.errors import ErrorCode, MetaSearchError
from app.orchestration.pipeline import SearchParams

router = APIRouter(tags=["search"])


@router.post("/search", response_model=SearchResponse, response_model_exclude_none=True)
async def search(request: SearchRequest) -> SearchResponse:
    outcome = await get_search_pipeline().run(
        SearchParams(
            query=request.query,
            lang=request.lang,
            k=request.k,
            engines=request.engines,
            categories=request.categories,
            freshness_days=request.freshness_days,
            include_snippets=request.include_snippets,
            news=request.news,
        )
    )
    if not outcome.results:
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            "All engines failed or returned no results",
            details={"engines_status": {s.engine: s.status for s in outcome.statuses}},
        )

    return SearchResponse(
        query_id=str(outcome.query_id),
        results=[
            {
                "rank": i,
                "url": r.url,
                "title": r.title,
                "snippet": r.snippet if request.include_snippets else "",
                "score": round(r.score, 4),
                "consensus": r.consensus,
                "found_by": r.adapters,
                "engines": r.engines,
                "domain": r.domain,
                "published_at": r.published_at.isoformat() if r.published_at else None,
            }
            for i, r in enumerate(outcome.results, start=1)
        ],
        engines_used=[s.engine for s in outcome.statuses if s.status == "ok"],
        engines_failed=[s.engine for s in outcome.statuses if s.status != "ok"],
        engines_status=[
            {
                "engine": s.engine,
                "status": s.status,
                "results": s.results,
                "ms": s.ms,
                "error": s.error,
            }
            for s in outcome.statuses
        ],
        latency_ms=outcome.latency_ms,
    )
