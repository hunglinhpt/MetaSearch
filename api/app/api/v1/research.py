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

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.config import get_settings
from app.core.auth import current_api_key_id, current_role
from app.core.errors import ErrorCode, MetaSearchError
from app.llm.providers import LLMOverride
from app.orchestration.research import ResearchParams
from app.orchestration.research_jobs import get_job_store, run_job

router = APIRouter(tags=["research"])

_active_research_tasks: set[asyncio.Task[None]] = set()

_PROFILE_PATTERN = r"^(auto|general|developer|academic|comparative|financial|news|fengshui|health)$"


class ResearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    lang: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    breadth: int = Field(default=4, ge=2, le=5)
    depth: int = Field(default=3, ge=1, le=5)
    max_time_s: int | None = Field(default=None, ge=10, le=600)
    k: int = Field(default=6, ge=1, le=20)
    profile: str = Field(default="auto", pattern=_PROFILE_PATTERN)
    space: str | None = Field(default=None, max_length=64)
    llm: LLMOverride | None = None


@router.get("/research")
async def list_research(limit: int = Query(default=20, ge=1, le=50)) -> dict[str, Any]:
    """List recent Deep Research jobs scoped to the caller (admin sees all)."""
    is_admin = (not get_settings().auth_enabled) or current_role.get() == "admin"
    jobs = await get_job_store().list_jobs(
        owner_key_id=current_api_key_id.get(),
        is_admin=is_admin,
        limit=limit,
    )
    return {"jobs": jobs}


@router.post("/research", status_code=202)
async def create_research(request: ResearchRequest) -> dict[str, Any]:
    store = get_job_store()
    job = await store.create(request.query)
    job.owner_key_id = current_api_key_id.get()
    params = ResearchParams(
        query=request.query,
        lang=request.lang,
        breadth=request.breadth,
        depth=request.depth,
        max_time_s=request.max_time_s,
        k=request.k,
        profile=request.profile,
        space=request.space,
        research_id=job.id,
        llm_override=request.llm,
    )
    task = asyncio.create_task(run_job(job, params))
    job.task = task
    _active_research_tasks.add(task)
    task.add_done_callback(_log_task)
    return {"research_id": str(job.id), "status": job.status}


def _check_job_owner(job) -> None:
    if not get_settings().auth_enabled or current_role.get() == "admin":
        return
    owner = current_api_key_id.get()
    if job.owner_key_id is None or job.owner_key_id != owner:
        raise MetaSearchError(ErrorCode.INVALID_REQUEST, "research job not found", status_code=404)


@router.get("/research/{research_id}")
async def get_research(research_id: str = Path(max_length=64)) -> dict[str, Any]:
    job = await get_job_store().get(research_id)
    _check_job_owner(job)
    return {
        "research_id": str(job.id),
        "status": job.status,
        "progress": job.progress,
        "report_md": job.result.get("report_md", ""),
        "citations": job.result.get("citations", []),
        "learnings": job.result.get("learnings", []),
        "outline": job.result.get("outline", []),
        "conflicts": job.result.get("conflicts", []),
        "verification": job.result.get("verification"),
        "tools": job.result.get("tools", []),
        "profile": job.result.get("profile"),
        "cost_usd": job.result.get("cost_usd", 0.0),
        "error": job.error,
    }


@router.post("/research/{research_id}/cancel")
async def cancel_research(research_id: str = Path(max_length=64)) -> dict[str, Any]:
    store = get_job_store()
    job = await store.get(research_id)
    _check_job_owner(job)
    job = await store.cancel(research_id)
    return {"research_id": str(job.id), "status": job.status}


@router.get("/research/{research_id}/events")
async def research_events(
    request: Request,
    research_id: str = Path(max_length=64),
    after_seq: int = Query(default=0, ge=0),
) -> EventSourceResponse:
    job = await get_job_store().get(research_id)
    _check_job_owner(job)
    last_ev = request.headers.get("last-event-id", "").strip()
    try:
        header_seq = max(0, int(last_ev)) if last_ev else 0
    except ValueError:
        header_seq = 0
    return EventSourceResponse(_event_stream(job, start_seq=max(after_seq, header_seq)))


async def _event_stream(job, *, start_seq: int = 0) -> AsyncIterator[dict]:
    """Replay buffered events from ``start_seq``, then follow live until termination."""
    index = start_seq
    while True:
        index, event = await job.wait_event(index, 15.0)
        if event is not None:
            name, data = event
            yield {
                "id": str(index),
                "event": name,
                "data": json.dumps(data, ensure_ascii=False),
            }
            if name in ("done", "error"):
                return
        else:
            if job.finished:
                if job.terminal is not None and index < job.terminal[0]:
                    t_seq, t_event = job.terminal
                    yield {
                        "id": str(t_seq),
                        "event": t_event[0],
                        "data": json.dumps(t_event[1], ensure_ascii=False),
                    }
                return
        await asyncio.sleep(0)


def _log_task(task: asyncio.Task) -> None:
    _active_research_tasks.discard(task)
    if task.done() and not task.cancelled():
        exc = task.exception()
        if exc is not None:
            from app.core.logging import get_logger

            get_logger(__name__).warning("research_task_failed", error=type(exc).__name__)
