"""Per-key memory CRUD (docs/20 P1-5 — Mem0-lite, v2.6).

GET    /v1/memory            -> list the current key's memory entries
POST   /v1/memory            -> record {kind: query|feedback, text?, url?, weight?}
DELETE /v1/memory            -> wipe ALL of the current key's memory (privacy)
DELETE /v1/memory/{id}       -> delete one entry (404 unless owner)

Feedback (👍/👎 on a result URL) is aggregated per domain into rerank boosts
at search time (app.retrieval.memory.load_memory_boosts). Tenancy by
``current_api_key_id``; DB down -> list returns empty with storage="memory",
writes are refused with 503 rather than silently lost.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter, Path, Query
from sqlalchemy import Table

from app.api.v1.schemas import (
    MemoryCreateRequest,
    MemoryIndexItem,
    MemoryIndexResponse,
    MemoryListResponse,
    MemoryObservationDetail,
    MemoryObservationsRequest,
    MemoryObservationsResponse,
    MemoryOut,
    MemoryTimelineItem,
    MemoryTimelineResponse,
)
from app.core.auth import current_api_key_id
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.db.models import MemoryItem
from app.db.session import get_engine
from app.retrieval import memory as mem

router = APIRouter(tags=["memory"])
logger = get_logger(__name__)

_table_ready = False


async def _ensure_table() -> bool:
    global _table_ready
    if _table_ready:
        return True
    try:
        table = cast(Table, MemoryItem.__table__)
        async with get_engine().begin() as conn:
            await conn.run_sync(table.create, checkfirst=True)
        _table_ready = True
        return True
    except Exception as exc:
        logger.warning("memory_table_ensure_failed", error=type(exc).__name__)
        return False


def _owner() -> str:
    return current_api_key_id.get() or "anonymous"


@router.get("/memory", response_model=MemoryListResponse)
async def list_memory(
    limit: int = Query(default=100, ge=1, le=500),
    kind: Literal["query", "feedback", "observation"] | None = Query(default=None),
) -> MemoryListResponse:
    owner = _owner()
    if not await _ensure_table():
        return MemoryListResponse(items=[], storage="memory")
    try:
        items = await mem.list_memory(owner, limit=limit, kind=kind)
        return MemoryListResponse(items=[MemoryOut(**item) for item in items], storage="db")
    except Exception as exc:
        logger.warning("memory_list_failed", error=type(exc).__name__)
        return MemoryListResponse(items=[], storage="memory")


@router.get(
    "/memory/search",
    response_model=MemoryIndexResponse,
    summary="Layer 1 Progressive Disclosure: search compact memory index (~40 tokens/item)",
)
async def search_memory(
    q: str = Query(default="", max_length=500),
    kind: Literal["query", "feedback", "observation"] | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
) -> MemoryIndexResponse:
    owner = _owner()
    if not await _ensure_table():
        return MemoryIndexResponse(query=q, items=[], storage="memory")
    try:
        items = await mem.search_memory_index(owner, q, kind=kind, limit=limit)
        return MemoryIndexResponse(
            query=q,
            items=[MemoryIndexItem(**item) for item in items],
            storage="db",
        )
    except Exception as exc:
        logger.warning("memory_search_failed", error=type(exc).__name__)
        return MemoryIndexResponse(query=q, items=[], storage="memory")


@router.get(
    "/memory/timeline",
    response_model=MemoryTimelineResponse,
    summary="Layer 2 Progressive Disclosure: chronological timeline around an anchor ID",
)
async def get_memory_timeline(
    anchor_id: str | None = Query(default=None, max_length=64),
    before: int = Query(default=3, ge=0, le=20),
    after: int = Query(default=3, ge=0, le=20),
) -> MemoryTimelineResponse:
    owner = _owner()
    if not await _ensure_table():
        return MemoryTimelineResponse(
            anchor_id=anchor_id, anchor_found=False, items=[], storage="memory"
        )
    try:
        res = await mem.memory_timeline(owner, anchor_id=anchor_id, before=before, after=after)
        return MemoryTimelineResponse(
            anchor_id=res["anchor_id"],
            anchor_found=bool(res["anchor_found"]),
            items=[MemoryTimelineItem(**item) for item in res["items"]],
            storage="db",
        )
    except Exception as exc:
        logger.warning("memory_timeline_failed", error=type(exc).__name__)
        return MemoryTimelineResponse(
            anchor_id=anchor_id, anchor_found=False, items=[], storage="memory"
        )


@router.post(
    "/memory/observations",
    response_model=MemoryObservationsResponse,
    summary="Layer 3 Progressive Disclosure: fetch full memory details for selected IDs",
)
async def fetch_memory_observations(
    request: MemoryObservationsRequest,
) -> MemoryObservationsResponse:
    owner = _owner()
    if not await _ensure_table():
        return MemoryObservationsResponse(items=[], storage="memory")
    try:
        items = await mem.get_memory_observations(owner, ids=request.ids)
        return MemoryObservationsResponse(
            items=[MemoryObservationDetail(**item) for item in items],
            storage="db",
        )
    except Exception as exc:
        logger.warning("memory_observations_failed", error=type(exc).__name__)
        return MemoryObservationsResponse(items=[], storage="memory")


@router.post("/memory", response_model=MemoryOut, status_code=201)
async def add_memory(request: MemoryCreateRequest) -> MemoryOut:
    owner = _owner()
    if request.kind == "feedback" and not request.url:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            "feedback memory requires a url",
        )
    # Default a bare feedback POST to +1 so `{"kind":"feedback","url":...}` works.
    weight = 1.0 if request.kind == "feedback" and request.weight == 0.0 else request.weight
    if not await _ensure_table():
        raise MetaSearchError(
            ErrorCode.INTERNAL,
            "memory storage unavailable",
            status_code=503,
        )
    from app.config import get_settings

    settings = get_settings()
    ok = await mem.record_memory(
        owner,
        kind=request.kind,
        text=request.text,
        url=request.url,
        weight=weight,
        payload=request.payload,
        max_per_key=settings.memory_max_per_key,
    )
    if not ok:
        raise MetaSearchError(
            ErrorCode.INTERNAL,
            "memory cap reached for this key (delete entries or raise MEMORY_MAX_PER_KEY)",
            status_code=507,
        )
    mem.invalidate(owner)
    items = await mem.list_memory(owner, limit=10)
    for item in items:
        if (
            item.get("kind") == request.kind
            and item.get("text") == request.text[:4000]
            and item.get("url") == (request.url[:2000] if request.url else None)
        ):
            return MemoryOut(**item)
    if items:
        return MemoryOut(**items[0])
    return MemoryOut(id="", kind=request.kind, text=request.text, url=request.url, weight=weight)


@router.delete("/memory")
async def wipe_memory() -> dict[str, Any]:
    owner = _owner()
    if not await _ensure_table():
        raise MetaSearchError(ErrorCode.INTERNAL, "memory storage unavailable", status_code=503)
    deleted = await mem.delete_memory(owner)
    mem.invalidate(owner)
    return {"deleted": deleted, "scope": "all"}


@router.delete("/memory/{memory_id}")
async def remove_memory(memory_id: str = Path(max_length=64)) -> dict[str, Any]:
    owner = _owner()
    if not await _ensure_table():
        raise MetaSearchError(ErrorCode.INTERNAL, "memory storage unavailable", status_code=503)
    deleted = await mem.delete_memory(owner, memory_id=memory_id)
    if deleted == 0:
        raise MetaSearchError(ErrorCode.NOT_FOUND, "memory entry not found", status_code=404)
    mem.invalidate(owner)
    return {"deleted": deleted, "id": memory_id}
