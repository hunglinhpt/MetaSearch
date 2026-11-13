"""Per-API-key memory (docs/20 P1-5 — Mem0/Graphiti-lite, v2.6).

Two kinds of entries live in ``memory_items``:

- ``query``  — every search the key runs is remembered (bounded per key) so
  future phases can contextualize queries; recording is fire-and-forget and
  never fails a request.
- ``feedback`` — per-URL 👍/👎 (``weight`` ±1). At search time the feedback
  rows are aggregated per domain into ``BoostRule(kind="domain")`` entries
  (``load_memory_boosts``) and merged with the explicit ``key_boosts`` rules
  in ``SearchPipeline._personalize`` — and therefore folded into the search
  cache key via ``boosts_sig``, so memory never leaks across keys.

Privacy: DELETE /v1/memory wipes ALL of the key's memory. Storage failure
is always fail-open (no boosts / no recording), never a request error.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select

from app.core.logging import get_logger
from app.retrieval.boosts import BoostRule

logger = get_logger(__name__)

MEMORY_KINDS = frozenset({"query", "feedback"})

_LOAD_TTL_S = 30.0
_boost_cache: dict[str, tuple[float, list[BoostRule]]] = {}
# Feedback weight per domain is clamped to this band before scaling.
_MAX_DOMAIN_FEEDBACK = 5.0


def domain_of(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


async def record_memory(
    owner: str,
    *,
    kind: str,
    text: str = "",
    url: str | None = None,
    weight: float = 0.0,
    payload: dict[str, Any] | None = None,
    max_per_key: int = 500,
) -> bool:
    """Insert one memory row. Returns False (never raises) when memory is
    unavailable or the per-key cap is reached."""
    if kind not in MEMORY_KINDS:
        return False
    try:
        import uuid

        from app.db.models import MemoryItem
        from app.db.session import get_sessionmaker

        async def _write() -> bool:
            async with get_sessionmaker()() as session:
                count = (
                    await session.execute(
                        select(func.count())
                        .select_from(MemoryItem)
                        .where(MemoryItem.api_key_id == owner)
                    )
                ).scalar_one()
                if count >= max_per_key:
                    if kind == "query":
                        # Pass 1 P2: search volume must not starve the 👍/👎
                        # feedback feature — evict the OLDEST query rows to
                        # make room (ring buffer for the query kind only).
                        oldest_q = (
                            await session.execute(
                                select(MemoryItem.id)
                                .where(
                                    MemoryItem.api_key_id == owner,
                                    MemoryItem.kind == "query",
                                )
                                .order_by(MemoryItem.created_at.asc())
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        if oldest_q is None:
                            return False  # cap full of feedback — stay honest
                        await session.execute(delete(MemoryItem).where(MemoryItem.id == oldest_q))
                    else:
                        return False
                session.add(
                    MemoryItem(
                        id=uuid.uuid4(),
                        api_key_id=owner,
                        kind=kind,
                        text=(text or "")[:4000],
                        url=(url or None) and url[:2000],
                        weight=float(weight),
                        payload=payload,
                    )
                )
                await session.commit()
                return True

        ok = await asyncio.wait_for(_write(), timeout=0.5)
        if ok:
            _boost_cache.pop(owner, None)  # new feedback must apply immediately
        return ok
    except Exception as exc:
        logger.warning("memory_record_failed", owner=owner, error=type(exc).__name__)
        return False


def record_query_fire_and_forget(owner: str, query: str, *, max_per_key: int = 500) -> None:
    """Best-effort background recording of a past query (never awaited, never
    raises into the request path). Goes through the bounded persist queue so a
    burst of searches cannot spawn one task per write."""
    if not query.strip():
        return
    try:
        from app.orchestration import persist as _persist

        _persist.spawn_task(
            "memory_record",
            record_memory(owner, kind="query", text=query, max_per_key=max_per_key),
        )
    except Exception:
        return


async def load_memory_boosts(owner: str, *, settings: Any) -> list[BoostRule]:
    """Aggregate the owner's ``feedback`` rows into domain boost rules.

    Fail-open: any DB error returns [] (neutral). Cached briefly (TTL) and
    bounded by a 0.3s timeout so a sick DB never sits in the search path.
    """
    if not getattr(settings, "memory_enabled", True):
        return []
    scale = float(getattr(settings, "memory_feedback_boost", 0.15))
    if scale <= 0:
        return []
    now = time.monotonic()
    hit = _boost_cache.get(owner)
    if hit is not None and now - hit[0] < _LOAD_TTL_S:
        return hit[1]
    rules: list[BoostRule] = []
    try:
        from app.db.models import MemoryItem
        from app.db.session import get_sessionmaker

        async def _read() -> list:
            async with get_sessionmaker()() as session:
                return (
                    (
                        await session.execute(
                            select(MemoryItem).where(
                                MemoryItem.api_key_id == owner,
                                MemoryItem.kind == "feedback",
                                MemoryItem.url.is_not(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

        rows = await asyncio.wait_for(_read(), timeout=0.3)
        per_domain: dict[str, float] = {}
        for row in rows:
            domain = domain_of(row.url or "")
            if not domain:
                continue
            per_domain[domain] = per_domain.get(domain, 0.0) + float(row.weight or 0.0)
        for domain, total in per_domain.items():
            clamped = max(-_MAX_DOMAIN_FEEDBACK, min(total, _MAX_DOMAIN_FEEDBACK))
            w = clamped * scale
            if abs(w) >= 0.01:
                rules.append(BoostRule(kind="domain", target=domain, weight=w))
    except Exception as exc:
        logger.warning("memory_boosts_load_failed", owner=owner, error=type(exc).__name__)
        rules = []
    _boost_cache[owner] = (now, rules)
    if len(_boost_cache) > 256:
        oldest = min(_boost_cache, key=lambda k: _boost_cache[k][0])
        _boost_cache.pop(oldest, None)
    return rules


def invalidate(owner: str) -> None:
    _boost_cache.pop(owner, None)


async def list_memory(owner: str, *, limit: int = 100) -> list[dict[str, Any]]:
    from app.db.models import MemoryItem
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(MemoryItem)
                    .where(MemoryItem.api_key_id == owner)
                    .order_by(MemoryItem.created_at.desc())
                    .limit(max(1, min(limit, 500)))
                )
            )
            .scalars()
            .all()
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        created = r.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        out.append(
            {
                "id": str(r.id),
                "kind": r.kind,
                "text": r.text,
                "url": r.url,
                "weight": float(r.weight or 0.0),
                "created_at": created.isoformat() if created else None,
            }
        )
    return out


async def delete_memory(owner: str, *, memory_id: str | None = None) -> int:
    """Delete one entry (by id, owner-scoped) or ALL of the owner's memory.
    Returns the number of rows deleted."""
    from app.db.models import MemoryItem
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        stmt = delete(MemoryItem).where(MemoryItem.api_key_id == owner)
        if memory_id is not None:
            stmt = stmt.where(MemoryItem.id == _parse_uuid(memory_id))
        result = await session.execute(stmt)
        await session.commit()
    _boost_cache.pop(owner, None)  # deleted feedback must stop applying immediately
    return int(result.rowcount or 0)


def _parse_uuid(value: str):
    import uuid

    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return uuid.UUID(int=0)


def memory_signature(rules: list[BoostRule]) -> str:
    """Included in boosts_sig composition (delegates to the shared hasher)."""
    from app.retrieval.boosts import rules_signature

    return rules_signature(rules)


def utc_now() -> datetime:
    return datetime.now(UTC)
