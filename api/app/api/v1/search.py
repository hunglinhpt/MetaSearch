"""POST /v1/search (docs/06-api-spec.md §1)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from app.api import deps
from app.api.v1.schemas import (
    BatchSearchRequest,
    BatchSearchResponse,
    BatchSearchResultOut,
    SearchRequest,
    SearchResponse,
)
from app.core.admission import admit
from app.core.budget import create_budget_for_route, use_budget
from app.core.errors import ErrorCode, MetaSearchError
from app.orchestration.pipeline import SearchParams, degraded_engines

router = APIRouter(tags=["search"])


def _age_s(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - value).total_seconds()))


def result_dict(i: int, r: Any, *, include_snippets: bool = True) -> dict[str, Any]:
    """API shape of one scored result (shared by /search and /search/batch)."""
    return {
        "rank": i,
        "url": r.url,
        "title": r.title,
        "snippet": r.snippet if include_snippets else "",
        "score": round(r.score, 4),
        "consensus": r.consensus,
        "found_by": r.adapters,
        "engines": r.engines,
        "domain": r.domain,
        # v3.0 P2-7: domain reputation tier (official/primary/.../unknown)
        "domain_tier": r.domain_tier,
        "published_at": r.published_at.isoformat() if r.published_at else None,
        # Provenance: None means unknown, never a guess (D3).
        "source_url": r.source_url,
        "source_name": r.source_name,
        "data_kind": r.data_kind,
        "as_of": r.as_of.isoformat() if r.as_of else None,
        # as_of is the data timestamp; news items only carry published_at.
        "age_s": _age_s(r.as_of or r.published_at),
    }


@router.post("/search", response_model=SearchResponse, response_model_exclude_none=True)
async def search(request: SearchRequest) -> SearchResponse:
    budget = create_budget_for_route(
        "search", timeout_ms=request.timeout_ms, settings=deps.get_settings()
    )
    async with admit("search"):
        with use_budget(budget):
            outcome = await deps.get_search_pipeline().run(
                SearchParams(
                    query=request.query,
                    lang=request.lang,
                    k=request.k,
                    engines=request.engines,
                    categories=request.categories,
                    freshness_days=request.freshness_days,
                    include_snippets=request.include_snippets,
                    news=request.news,
                    timeout_ms=request.timeout_ms,
                    include_domains=request.include_domains,
                    exclude_domains=request.exclude_domains,
                    deep=request.deep,
                    include_fields=tuple(request.extras or ()),
                    space=request.space,
                )
            )
            # WP-15: empty results with healthy engines = 200 complete:true;
            # all engines failed = 503. Agents must distinguish the two.
            all_failed = all(s.status != "ok" for s in outcome.statuses)
            if not outcome.results and all_failed:
                # Deadline only when time is what failed: the request budget ran out, or every
                # engine was cut by the stage deadline. Any real engine error stays NO_RESULTS.
                all_cut = all(s.status in ("timeout", "cancelled") for s in outcome.statuses)
                if budget is not None and (budget.is_expired() or all_cut):
                    raise MetaSearchError(
                        ErrorCode.DEADLINE_EXCEEDED,
                        "Request budget exceeded before search could return results",
                        status_code=504,
                        details={"engines_status": {s.engine: s.status for s in outcome.statuses}},
                    )
                raise MetaSearchError(
                    ErrorCode.NO_RESULTS,
                    "All engines failed or returned no results",
                    details={"engines_status": {s.engine: s.status for s in outcome.statuses}},
                )

            # v2.5 P0-7: agent extras are computed on top of the (possibly cached)
            # base results — they are deliberately NOT cached with them.
            extras: dict[str, dict] = {}
            instant_answer: str | None = None
            if request.extras and outcome.results:
                from app.api.v1.search_extras import build_extras as _be

                extras, instant_answer = await _be(
                    outcome.results,
                    request.query,
                    list(request.extras or ()),
                    settings=deps.get_settings(),
                    scraper=deps.get_scraper(),
                    gateway=deps.get_gateway(),
                )

            # v3.0 P2-6: structured JSON output over the top results (Exa-style
            # response_format). Post-cache, validated with jsonschema; failures
            # are reported in structured_errors, never fabricated.
            structured: dict | None = None
            structured_errors: list[str] = []
            if request.response_format and outcome.results:
                from app.llm.structured import extract_structured

                gateway = deps.get_gateway()
                settings = deps.get_settings()
                model = getattr(gateway, "default_quick_model", settings.llm_quick_model)
                structured, structured_errors, _raw = await extract_structured(
                    gateway,
                    request.query,
                    request.response_format.json_schema,
                    outcome.results,
                    model=model,
                )
            # v3.0 P2-4: expose the interleaving variant so clients can attach
            # feedback to the right arm (POST /v1/memory payload.variant).
            interleave = (
                {"variant": outcome.interleave_variant} if outcome.interleave_variant else None
            )

            return SearchResponse(
                query_id=str(outcome.query_id),
                answer=instant_answer,
                structured=structured,
                structured_errors=structured_errors,
                interleave=interleave,
                results=[
                    {
                        **result_dict(i, r, include_snippets=request.include_snippets),
                        # v2.5 P0-7: per-result agent extras (absent unless requested).
                        **(extras.get(r.url) or {}),
                    }
                    for i, r in enumerate(outcome.results, start=1)
                ],
                engines_used=[s.engine for s in outcome.statuses if s.status == "ok"],
                engines_failed=[
                    s.engine for s in outcome.statuses if s.status not in ("ok", "quarantined")
                ],
                degraded_engines=degraded_engines(outcome.statuses),
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
                cached=outcome.cached,
                complete=outcome.complete,
                age_s=outcome.age_s,
            )


@contextlib.asynccontextmanager
async def _budget_scope(budget: Any):
    """Async wrapper so one ``async with`` can hold admission + budget."""
    with use_budget(budget):
        yield


# Bounded fan-in for /search/batch: each item already fans out to many engines,
# so items run at most 3 at a time (the schema caps the batch at 5).
_BATCH_CONCURRENCY = 3


@router.post("/search/batch", response_model=BatchSearchResponse, response_model_exclude_none=True)
async def search_batch(request: BatchSearchRequest) -> BatchSearchResponse:
    """Run up to 5 independent searches in one round trip (agent-friendly).

    Partial by design: one failing item reports its own ``error`` and never
    fails the batch. Each item goes through the normal pipeline (cache,
    breakers, admission) — batching is never cheaper than looping (billing:
    one rate-limit unit per item)."""
    started = time.perf_counter()
    settings = deps.get_settings()
    sem = asyncio.Semaphore(_BATCH_CONCURRENCY)
    pipeline = deps.get_search_pipeline()

    async def _one(idx: int) -> BatchSearchResultOut:
        item = request.queries[idx]
        async with sem:
            # Per-item budget, started when the item actually runs: a shared
            # deadline would starve items 4-5 queued behind the semaphore.
            budget = create_budget_for_route(
                "search", timeout_ms=request.timeout_ms, settings=settings
            )
            try:
                async with admit("search"), _budget_scope(budget):
                    outcome = await pipeline.run(
                        SearchParams(
                            query=item.query,
                            lang=item.lang,
                            k=item.k,
                            categories=list(item.categories),
                            freshness_days=item.freshness_days,
                            news=item.news,
                            timeout_ms=request.timeout_ms,
                            include_domains=item.include_domains,
                            exclude_domains=item.exclude_domains,
                        )
                    )
            except MetaSearchError as exc:
                return BatchSearchResultOut(
                    index=idx,
                    query=item.query,
                    ok=False,
                    error={"code": str(exc.code.value), "message": exc.message[:300]},
                )
            except Exception as exc:  # one item never fails the whole batch
                return BatchSearchResultOut(
                    index=idx,
                    query=item.query,
                    ok=False,
                    error={"code": ErrorCode.INTERNAL.value, "message": type(exc).__name__},
                )
        if not outcome.results and all(s.status != "ok" for s in outcome.statuses):
            return BatchSearchResultOut(
                index=idx,
                query=item.query,
                ok=False,
                complete=False,
                error={
                    "code": ErrorCode.NO_RESULTS.value,
                    "message": "All engines failed or returned no results",
                },
            )
        return BatchSearchResultOut(
            index=idx,
            query=item.query,
            ok=True,
            complete=outcome.complete,
            cached=outcome.cached,
            results=[result_dict(i, r) for i, r in enumerate(outcome.results[: item.k], start=1)],
        )

    items = await asyncio.gather(*(_one(i) for i in range(len(request.queries))))
    return BatchSearchResponse(
        items=list(items), latency_ms=int((time.perf_counter() - started) * 1000)
    )
