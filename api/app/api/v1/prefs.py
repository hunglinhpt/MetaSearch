"""Per-key search preferences: custom rerank boosts (docs/20 P0-6, v2.5.0).

GET    /v1/prefs/boosts        -> list the current key's rules
POST   /v1/prefs/boosts        -> add {kind: domain|keyword, target, weight} (cap per key)
DELETE /v1/prefs/boosts/{id}   -> remove (404 unless owner)

Tenancy by ``current_api_key_id`` ("anonymous" for single-operator Lite).
Rules apply only in merger.merge for the owning key and are folded into the
search cache key (boosts_sig), so one key's boosts never leak into another
key's cached results. DB down -> list returns empty with storage="memory"
(fail-open neutral); writes are refused with 503 rather than silently lost.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from sqlalchemy import delete, select

from app.api import deps
from app.api.v1.schemas import (
    BoostCreateRequest,
    BoostListResponse,
    BoostOut,
)
from app.config import get_settings
from app.core.auth import current_api_key_id
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.db.models import KeyBoost
from app.retrieval.boosts import BOOST_KINDS, invalidate

router = APIRouter(tags=["prefs"])
logger = get_logger(__name__)

_table_ready = False


async def _ensure_table() -> bool:
    global _table_ready
    if _table_ready:
        return True
    try:
        from typing import cast

        from sqlalchemy import Table

        from app.db.session import get_engine

        table = cast(Table, KeyBoost.__table__)
        async with get_engine().begin() as conn:
            await conn.run_sync(table.create, checkfirst=True)
        _table_ready = True
        return True
    except Exception as exc:
        logger.warning("key_boosts_table_ensure_failed", error=type(exc).__name__)
        return False


def _owner() -> str:
    return current_api_key_id.get() or "anonymous"


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _out(row: KeyBoost) -> BoostOut:
    return BoostOut(
        id=str(row.id),
        kind=row.kind,
        target=row.target,
        weight=float(row.weight),
        created_at=_iso(row.created_at),
    )


@router.get("/prefs/boosts", response_model=BoostListResponse)
async def list_boosts() -> BoostListResponse:
    owner = _owner()
    if not await _ensure_table():
        return BoostListResponse(items=[], storage="memory")
    try:
        async with deps.get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        select(KeyBoost)
                        .where(KeyBoost.api_key_id == owner)
                        .order_by(KeyBoost.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
        return BoostListResponse(items=[_out(r) for r in rows], storage="db")
    except Exception as exc:
        logger.warning("boosts_list_failed", error=type(exc).__name__)
        return BoostListResponse(items=[], storage="memory")


@router.post("/prefs/boosts", response_model=BoostOut, status_code=201)
async def add_boost(request: BoostCreateRequest) -> BoostOut:
    settings = get_settings()
    owner = _owner()
    kind = request.kind
    target = request.target.strip()
    if kind not in BOOST_KINDS:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST,
            f"kind must be one of {sorted(BOOST_KINDS)}",
            status_code=400,
        )
    if not target or len(target) > 200:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST, "target must be 1..200 chars", status_code=400
        )
    if kind == "domain":
        target = target.lower().removeprefix("www.").rstrip(".")
        if not target or "/" in target or " " in target:
            raise MetaSearchError(
                ErrorCode.INVALID_REQUEST, "domain target must be a bare host", status_code=400
            )
    if not -1.0 <= request.weight <= 1.0:
        raise MetaSearchError(
            ErrorCode.INVALID_REQUEST, "weight must be in [-1.0, 1.0]", status_code=400
        )
    if not await _ensure_table():
        raise MetaSearchError(ErrorCode.INTERNAL, "prefs storage unavailable", status_code=503)
    try:
        async with deps.get_session_factory()() as session, session.begin():
            count = (
                await session.execute(select(KeyBoost.id).where(KeyBoost.api_key_id == owner))
            ).all()
            if len(count) >= settings.boosts_max_per_key:
                raise MetaSearchError(
                    ErrorCode.INVALID_REQUEST,
                    f"boosts full: {len(count)} >= cap {settings.boosts_max_per_key}",
                    status_code=400,
                    details={"cap": settings.boosts_max_per_key},
                )
            # One rule per (kind, target) per owner — update weight instead of duplicating.
            existing = (
                await session.execute(
                    select(KeyBoost).where(
                        KeyBoost.api_key_id == owner,
                        KeyBoost.kind == kind,
                        KeyBoost.target == target,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.weight = float(request.weight)
                invalidate(owner)
                return _out(existing)
            row = KeyBoost(
                id=uuid.uuid4(),
                api_key_id=owner,
                kind=kind,
                target=target,
                weight=float(request.weight),
                created_at=datetime.now(UTC),
            )
            session.add(row)
        invalidate(owner)
        return _out(row)
    except MetaSearchError:
        raise
    except Exception as exc:
        logger.warning("boost_add_failed", error=type(exc).__name__)
        raise MetaSearchError(
            ErrorCode.INTERNAL, "prefs storage unavailable", status_code=503
        ) from exc


@router.delete("/prefs/boosts/{rule_id}")
async def remove_boost(rule_id: str) -> dict[str, Any]:
    try:
        uid = uuid.UUID(rule_id)
    except ValueError as exc:
        raise MetaSearchError(ErrorCode.INVALID_REQUEST, "invalid id", status_code=400) from exc
    owner = _owner()
    if not await _ensure_table():
        raise MetaSearchError(ErrorCode.INTERNAL, "prefs storage unavailable", status_code=503)
    try:
        async with deps.get_session_factory()() as session, session.begin():
            row = await session.get(KeyBoost, uid)
            if row is None or row.api_key_id != owner:
                # Not found OR owned by another key — same 404, no existence leak.
                raise MetaSearchError(ErrorCode.NOT_FOUND, "boost rule not found", status_code=404)
            await session.execute(delete(KeyBoost).where(KeyBoost.id == uid))
        invalidate(owner)
        return {"deleted": str(uid)}
    except MetaSearchError:
        raise
    except Exception as exc:
        logger.warning("boost_delete_failed", error=type(exc).__name__)
        raise MetaSearchError(
            ErrorCode.INTERNAL, "prefs storage unavailable", status_code=503
        ) from exc
