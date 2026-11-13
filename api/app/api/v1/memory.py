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

from typing import Any, cast

from fastapi import APIRouter
from sqlalchemy import Table

from app.api.v1.schemas import MemoryCreateRequest, MemoryListResponse, MemoryOut
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
async def list_memory(limit: int = 100) -> MemoryListResponse:
    owner = _owner()
    if not await _ensure_table():
        return MemoryListResponse(items=[], storage="memory")
    try:
        items = await mem.list_memory(owner, limit=limit)
        return MemoryListResponse(items=[MemoryOut(**item) for item in items], storage="db")
    except Exception as exc:
        logger.warning("memory_list_failed", error=type(exc).__name__)
        return MemoryListResponse(items=[], storage="memory")


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
        max_per_key=settings.memory_max_per_key,
    )
    if not ok:
        raise MetaSearchError(
            ErrorCode.INTERNAL,
            "memory cap reached for this key (delete entries or raise MEMORY_MAX_PER_KEY)",
            status_code=507,
        )
    mem.invalidate(owner)
    items = await mem.list_memory(owner, limit=1)
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
async def remove_memory(memory_id: str) -> dict[str, Any]:
    owner = _owner()
    if not await _ensure_table():
        raise MetaSearchError(ErrorCode.INTERNAL, "memory storage unavailable", status_code=503)
    deleted = await mem.delete_memory(owner, memory_id=memory_id)
    if deleted == 0:
        raise MetaSearchError(ErrorCode.NOT_FOUND, "memory entry not found", status_code=404)
    mem.invalidate(owner)
    return {"deleted": deleted, "id": memory_id}
