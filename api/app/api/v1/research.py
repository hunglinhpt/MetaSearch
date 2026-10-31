"""Deep Research endpoints (docs/06-api-spec.md §4, docs/01 FR-9).

POST /v1/research                 -> 202 {research_id, status}
GET  /v1/research/{id}            -> status/progress/report/citations/learnings/cost
GET  /v1/research/{id}/events     -> SSE (plan, iteration_start, engine_done,
                                     learnings, report_token, citations, done)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.orchestration.research import ResearchParams
from app.orchestration.research_jobs import get_job_store, run_job

router = APIRouter(tags=["research"])


class ResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    lang: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    breadth: int = Field(default=4, ge=2, le=5)
    depth: int = Field(default=3, ge=1, le=5)
    max_time_s: int | None = Field(default=None, ge=10, le=600)
    k: int = Field(default=6, ge=1, le=20)
    profile: str = Field(default="auto", pattern=r"^(auto|general|developer|academic|comparative)$")


@router.post("/research", status_code=202)
async def create_research(request: ResearchRequest) -> dict[str, Any]:
    store = get_job_store()
    job = await store.create(request.query)
    params = ResearchParams(
        query=request.query,
        lang=request.lang,
        breadth=request.breadth,
        depth=request.depth,
        max_time_s=request.max_time_s,
        k=request.k,
        profile=request.profile,
    )
    task = asyncio.create_task(run_job(job, params))
    task.add_done_callback(_log_task)
    return {"research_id": str(job.id), "status": job.status}


@router.get("/research/{research_id}")
async def get_research(research_id: str) -> dict[str, Any]:
    job = await get_job_store().get(research_id)
    return {
        "research_id": str(job.id),
        "status": job.status,
        "progress": job.progress,
        "report_md": job.result.get("report_md", ""),
        "citations": job.result.get("citations", []),
        "learnings": job.result.get("learnings", []),
        "error": job.error,
    }


@router.get("/research/{research_id}/events")
async def research_events(research_id: str) -> EventSourceResponse:
    job = await get_job_store().get(research_id)
    return EventSourceResponse(_event_stream(job))


async def _event_stream(job) -> AsyncIterator[dict]:
    """Replay buffered events, then follow live until the job terminates."""
    index = 0
    while True:
        index, event = await job.wait_event(index, 15.0)
        if event is not None:
            name, data = event
            yield {"event": name, "data": json.dumps(data, ensure_ascii=False)}
            if name in ("done", "error"):
                return
        # periodic keepalive handled by sse-starlette pings on idle loops


def _log_task(task: asyncio.Task) -> None:
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc is not None:
            from app.core.logging import get_logger

            get_logger(__name__).warning("research_task_failed", error=type(exc).__name__)
