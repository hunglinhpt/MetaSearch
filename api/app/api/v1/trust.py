"""Trust & Insight layer endpoints (docs/34, v4.5.0).

POST /v1/verify    -> claim verification: evidence for/against + honest verdict
POST /v1/timeline  -> chronological distinct events + source-coverage lens

Both reuse the existing pipelines (search/scrape/rerank) under the same
budget + admission + SSRF rules; neither fabricates: no evidence ->
``insufficient_evidence``; undated results are dropped from the timeline and
counted, never assigned a guessed date.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.api import deps
from app.api.v1.schemas import (
    TimelineRequest,
    TimelineResponse,
    VerifyRequest,
    VerifyResponse,
)
from app.core.admission import admit
from app.core.budget import create_budget_for_route, use_budget
from app.core.errors import ErrorCode, MetaSearchError
from app.core.offload import run_cpu
from app.orchestration.claim_verify import verify_claim
from app.orchestration.pipeline import SearchParams
from app.orchestration.timeline import build_timeline

router = APIRouter(tags=["trust"])


@router.post("/verify", response_model=VerifyResponse, response_model_exclude_none=True)
async def verify(request: VerifyRequest) -> VerifyResponse:
    """Fact-check one claim against live web evidence (verbatim quotes, tiered sources)."""
    budget = create_budget_for_route("context", timeout_ms=request.timeout_ms)
    async with admit("verify"):
        with use_budget(budget):
            params = SearchParams(
                query=request.claim,
                lang=request.lang,
                k=request.k,
                freshness_days=request.freshness_days,
                news=request.news,
                timeout_ms=request.timeout_ms,
                include_domains=request.include_domains,
                exclude_domains=request.exclude_domains,
                # A claim is not a research question: no deep escalation, no
                # LLM query expansion, and it is not the caller's search history.
                skip_router=True,
                expansion=False,
                record_memory=False,
            )
            payload = await verify_claim(
                deps.get_answer_pipeline(),
                params,
                claim=request.claim,
                max_evidence=request.max_evidence,
                gateway=deps.get_gateway() if request.use_llm else None,
                use_llm=request.use_llm,
            )
    return VerifyResponse(**payload)


@router.post("/timeline", response_model=TimelineResponse, response_model_exclude_none=True)
async def timeline(request: TimelineRequest) -> TimelineResponse:
    """Distinct events about a topic, oldest -> newest, from dated results only."""
    budget = create_budget_for_route(
        "search", timeout_ms=request.timeout_ms, settings=deps.get_settings()
    )
    async with admit("search"):
        with use_budget(budget):
            outcome = await deps.get_search_pipeline().run(
                SearchParams(
                    query=request.topic,
                    lang=request.lang,
                    k=request.k,
                    categories=list(request.categories or ["news", "general"]),
                    freshness_days=request.days,
                    news=True,
                    timeout_ms=request.timeout_ms,
                    deep=request.deep,
                    skip_router=True,
                    expansion=False,
                    record_memory=False,
                )
            )
    if not outcome.results and all(s.status != "ok" for s in outcome.statuses):
        raise MetaSearchError(
            ErrorCode.NO_RESULTS,
            "All engines failed or returned no results",
            details={"engines_status": {s.engine: s.status for s in outcome.statuses}},
        )
    data = await run_cpu(
        build_timeline,
        outcome.results,
        now=datetime.now(UTC),
        days=request.days,
        max_events=request.max_events,
    )
    return TimelineResponse(
        topic=request.topic,
        complete=outcome.complete,
        cached=outcome.cached,
        query_id=str(outcome.query_id),
        **data,
    )
