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

MEMORY_KINDS = frozenset({"query", "feedback", "observation"})

_LOAD_TTL_S = 30.0
_RECALL_BACKOFF_S = 30.0
_recall_backoff_until = 0.0
_boost_cache: dict[str, tuple[float, list[BoostRule]]] = {}
# Feedback weight per domain is clamped to this band before scaling.
_MAX_DOMAIN_FEEDBACK = 5.0


def reset_recall_backoff() -> None:
    global _recall_backoff_until
    _recall_backoff_until = 0.0


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
    timeout_s: float = 2.0,
) -> bool:
    """Insert one memory row. Returns False (never raises) when memory is
    unavailable or the per-key cap is reached."""
    if kind not in MEMORY_KINDS:
        return False
    try:
        import uuid

        from app.db.models import MemoryItem
        from app.db.session import get_sessionmaker, sqlite_write_lock

        async def _write() -> bool:
            async with sqlite_write_lock(), get_sessionmaker()() as session:
                count = (
                    await session.execute(
                        select(func.count())
                        .select_from(MemoryItem)
                        .where(MemoryItem.api_key_id == owner)
                    )
                ).scalar_one()
                if count >= max_per_key:
                    if kind in ("query", "observation"):
                        # Evict oldest query first (or oldest observation if no query rows)
                        # so feedback boosts are never starved by search/session volume.
                        evict_id = (
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
                        if evict_id is None and kind == "observation":
                            evict_id = (
                                await session.execute(
                                    select(MemoryItem.id)
                                    .where(
                                        MemoryItem.api_key_id == owner,
                                        MemoryItem.kind == "observation",
                                    )
                                    .order_by(MemoryItem.created_at.asc())
                                    .limit(1)
                                )
                            ).scalar_one_or_none()
                        if evict_id is None:
                            return False  # cap full of feedback — stay honest
                        await session.execute(delete(MemoryItem).where(MemoryItem.id == evict_id))
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

        ok = await asyncio.wait_for(_write(), timeout=timeout_s)
        if ok and kind == "feedback":
            # Pass 4 P2: only feedback changes boosts — invalidating on every
            # recorded query defeated the 30s TTL on the hot path.
            _boost_cache.pop(owner, None)
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
            record_memory(owner, kind="query", text=query, max_per_key=max_per_key, timeout_s=5.0),
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

        async def _read() -> list[Any]:
            async with get_sessionmaker()() as session:
                return list(
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


def _iso_utc(created: datetime | None) -> str | None:
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created.isoformat()


def _observation_fields(row: Any) -> tuple[str, str, list[str], list[str], str]:
    payload = row.payload if isinstance(row.payload, dict) else {}
    text = str(row.text or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = str(payload.get("title") or (lines[0] if lines else ""))[:160]
    summary = str(payload.get("summary") or (" ".join(lines[1:]) if len(lines) > 1 else text))[:400]
    facts_raw = payload.get("facts") or []
    facts = [str(f)[:240] for f in facts_raw[:6]] if isinstance(facts_raw, list) else []
    tags_raw = payload.get("tags") or []
    tags = [str(t)[:60] for t in tags_raw[:8]] if isinstance(tags_raw, list) else []
    domain_id = str(payload.get("domain") or "")[:60]
    return title, summary, facts, tags, domain_id


async def list_memory(
    owner: str,
    *,
    limit: int = 100,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    from app.db.models import MemoryItem
    from app.db.session import get_sessionmaker

    stmt = select(MemoryItem).where(MemoryItem.api_key_id == owner)
    if kind and kind in MEMORY_KINDS:
        stmt = stmt.where(MemoryItem.kind == kind)
    stmt = stmt.order_by(MemoryItem.created_at.desc()).limit(max(1, min(limit, 500)))
    async with get_sessionmaker()() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "kind": r.kind,
            "text": r.text,
            "url": r.url,
            "weight": float(r.weight or 0.0),
            "payload": r.payload if isinstance(r.payload, dict) else None,
            "created_at": _iso_utc(r.created_at),
        }
        for r in rows
    ]


async def record_observation(
    owner: str,
    *,
    title: str,
    summary: str,
    facts: list[str] | None = None,
    url: str | None = None,
    domain_id: str = "",
    skill_id: str = "",
    tags: list[str] | None = None,
    max_per_key: int = 500,
    timeout_s: float = 2.0,
) -> bool:
    """Capture a distilled cross-session observation (Claude-Mem Layer 0)."""
    clean_title = " ".join((title or "").split())[:160]
    clean_summary = " ".join((summary or "").split())[:400]
    if not clean_title and not clean_summary:
        return False
    clean_facts = [" ".join(str(f).split())[:220] for f in (facts or []) if str(f).strip()][:5]
    clean_tags = [" ".join(str(t).split())[:48] for t in (tags or []) if str(t).strip()][:8]
    payload: dict[str, Any] = {
        "title": clean_title,
        "summary": clean_summary,
        "facts": clean_facts,
        "domain": domain_id[:60],
        "skill": skill_id[:60],
        "tags": clean_tags,
    }
    body_text = f"{clean_title}\n{clean_summary}".strip()
    return await record_memory(
        owner,
        kind="observation",
        text=body_text,
        url=url,
        weight=1.0,
        payload=payload,
        max_per_key=max_per_key,
        timeout_s=timeout_s,
    )


def distill_answer_observation(
    *,
    query: str,
    answer_md: str,
    sources: list[dict[str, Any]],
    domain_id: str = "",
    skill_id: str = "",
) -> dict[str, Any] | None:
    """Extract a concise observation payload from a completed synthesis (zero extra LLM call)."""
    clean_query = " ".join((query or "").split())[:160]
    if not clean_query or not (answer_md or "").strip():
        return None
    lines = [ln.strip().lstrip("#*- ") for ln in answer_md.splitlines() if ln.strip()]
    summary_parts: list[str] = []
    facts: list[str] = []
    for ln in lines:
        if len(ln) < 12:
            continue
        if len(" ".join(summary_parts)) < 260:
            summary_parts.append(ln)
        elif len(facts) < 3:
            facts.append(ln[:200])
    summary = " ".join(summary_parts)[:380]
    top_url = next((str(s.get("url") or "") for s in sources if s.get("url")), None)
    tags = [t for t in (domain_id, skill_id) if t]
    for s in sources[:3]:
        dom = str(s.get("domain") or "").strip()
        if dom and dom not in tags:
            tags.append(dom)
    return {
        "title": clean_query,
        "summary": summary or clean_query,
        "facts": facts,
        "url": top_url,
        "domain_id": domain_id,
        "skill_id": skill_id,
        "tags": tags,
    }


def record_observation_fire_and_forget(
    owner: str,
    *,
    query: str,
    answer_md: str,
    sources: list[dict[str, Any]],
    domain_id: str = "",
    skill_id: str = "",
    max_per_key: int = 500,
) -> None:
    """Best-effort async observation capture after answer completion."""
    distilled = distill_answer_observation(
        query=query,
        answer_md=answer_md,
        sources=sources,
        domain_id=domain_id,
        skill_id=skill_id,
    )
    if distilled is None:
        return
    try:
        from app.orchestration import persist as _persist

        _persist.spawn_task(
            "memory_observation",
            record_observation(
                owner,
                title=distilled["title"],
                summary=distilled["summary"],
                facts=distilled["facts"],
                url=distilled["url"],
                domain_id=distilled["domain_id"],
                skill_id=distilled["skill_id"],
                tags=distilled["tags"],
                max_per_key=max_per_key,
                timeout_s=5.0,
            ),
        )
    except Exception:
        return


async def search_memory_index(
    owner: str,
    query: str,
    *,
    kind: str | None = None,
    limit: int = 10,
    scan_limit: int = 200,
) -> list[dict[str, Any]]:
    """Layer 1 (Claude-Mem Progressive Disclosure): compact scored memory index.

    Returns tiny index stubs (`id`, `kind`, `title`, `snippet`, `score`, `created_at`)
    rather than full blobs so agents inspect relevance cheaply before expanding
    selected IDs via ``memory_timeline`` / ``get_memory_observations``.
    """
    from app.db.models import MemoryItem
    from app.db.session import get_sessionmaker
    from app.retrieval.scoring import tokenize

    q_tokens = set(tokenize(query or "", fold=True))
    stmt = select(MemoryItem).where(MemoryItem.api_key_id == owner)
    if kind and kind in MEMORY_KINDS:
        stmt = stmt.where(MemoryItem.kind == kind)
    stmt = stmt.order_by(MemoryItem.created_at.desc()).limit(max(1, min(scan_limit, 500)))

    async with get_sessionmaker()() as session:
        rows = (await session.execute(stmt)).scalars().all()

    now = datetime.now(UTC)
    scored: list[tuple[float, dict[str, Any]]] = []
    for idx, r in enumerate(rows):
        title, summary, _facts, tags, domain_id = _observation_fields(r)
        haystack = f"{title} {summary} {r.url or ''} {' '.join(tags)} {domain_id}"
        doc_tokens = set(tokenize(haystack, fold=True))
        if q_tokens:
            overlap = len(q_tokens & doc_tokens)
            if overlap == 0:
                continue
            lexical = overlap / max(len(q_tokens), 1)
        else:
            lexical = 0.5
        created = r.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_days = max(0.0, (now - created).total_seconds() / 86400.0) if created else 0.0
        recency = 1.0 / (1.0 + age_days / 14.0)
        kind_bonus = 0.25 if r.kind == "observation" else 0.0
        score = lexical * 0.75 + recency * 0.25 + kind_bonus
        scored.append(
            (
                score,
                {
                    "id": str(r.id),
                    "kind": r.kind,
                    "title": title or (r.text or "")[:80],
                    "snippet": (summary or r.text or "")[:140],
                    "score": round(score, 4),
                    "created_at": _iso_utc(created),
                    "_order": idx,
                },
            )
        )
    scored.sort(key=lambda item: (-item[0], item[1]["_order"]))
    out: list[dict[str, Any]] = []
    for _, item in scored[: max(1, min(limit, 50))]:
        item.pop("_order", None)
        out.append(item)
    return out


async def memory_timeline(
    owner: str,
    *,
    anchor_id: str | None = None,
    before: int = 3,
    after: int = 3,
) -> dict[str, Any]:
    """Layer 2 (Claude-Mem Progressive Disclosure): chronological context around ``anchor_id``."""
    from app.db.models import MemoryItem
    from app.db.session import get_sessionmaker

    before = max(0, min(before, 20))
    after = max(0, min(after, 20))

    async with get_sessionmaker()() as session:
        if anchor_id:
            parsed_anchor = _parse_uuid(anchor_id)
            anchor = (
                await session.execute(
                    select(MemoryItem).where(
                        MemoryItem.api_key_id == owner,
                        MemoryItem.id == parsed_anchor,
                    )
                )
            ).scalar_one_or_none()
            if anchor is None:
                return {"anchor_id": anchor_id, "anchor_found": False, "items": []}

            before_rows = list(
                (
                    await session.execute(
                        select(MemoryItem)
                        .where(
                            MemoryItem.api_key_id == owner,
                            MemoryItem.created_at <= anchor.created_at,
                            MemoryItem.id != anchor.id,
                        )
                        .order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
                        .limit(before + 5)
                    )
                )
                .scalars()
                .all()
            )
            after_rows = list(
                (
                    await session.execute(
                        select(MemoryItem)
                        .where(
                            MemoryItem.api_key_id == owner,
                            MemoryItem.created_at >= anchor.created_at,
                            MemoryItem.id != anchor.id,
                        )
                        .order_by(MemoryItem.created_at.asc(), MemoryItem.id.asc())
                        .limit(after + 5)
                    )
                )
                .scalars()
                .all()
            )
            merged: dict[str, Any] = {str(anchor.id): anchor}
            for r in (*before_rows, *after_rows):
                merged.setdefault(str(r.id), r)
            ordered = sorted(
                merged.values(),
                key=lambda r: (
                    r.created_at.replace(tzinfo=UTC)
                    if r.created_at and r.created_at.tzinfo is None
                    else (r.created_at or datetime.min.replace(tzinfo=UTC)),
                    str(r.id),
                ),
            )
            anchor_idx = next(i for i, r in enumerate(ordered) if str(r.id) == str(anchor.id))
            lo = max(0, anchor_idx - before)
            hi = min(len(ordered), anchor_idx + after + 1)
            window = ordered[lo:hi]
            anchor_found = True
        else:
            total = max(1, before + after + 1)
            recent_desc = list(
                (
                    await session.execute(
                        select(MemoryItem)
                        .where(MemoryItem.api_key_id == owner)
                        .order_by(MemoryItem.created_at.desc(), MemoryItem.id.desc())
                        .limit(total)
                    )
                )
                .scalars()
                .all()
            )
            if not recent_desc:
                return {"anchor_id": anchor_id, "anchor_found": False, "items": []}
            window = list(reversed(recent_desc))
            anchor_found = False

    items = []
    for r in window:
        title, summary, _facts, _tags, _dom = _observation_fields(r)
        items.append(
            {
                "id": str(r.id),
                "kind": r.kind,
                "title": title or (r.text or "")[:80],
                "snippet": (summary or r.text or "")[:140],
                "is_anchor": bool(anchor_id and str(r.id) == anchor_id),
                "created_at": _iso_utc(r.created_at),
            }
        )
    return {"anchor_id": anchor_id, "anchor_found": anchor_found, "items": items}


async def get_memory_observations(
    owner: str,
    *,
    ids: list[str],
    max_items: int = 20,
) -> list[dict[str, Any]]:
    """Layer 3 (Claude-Mem Progressive Disclosure): fetch full observation details for IDs."""
    from app.db.models import MemoryItem
    from app.db.session import get_sessionmaker

    if not ids:
        return []
    parsed_ids = [_parse_uuid(i) for i in ids[: max(1, min(max_items, 50))]]
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(MemoryItem)
                    .where(
                        MemoryItem.api_key_id == owner,
                        MemoryItem.id.in_(parsed_ids),
                    )
                    .order_by(MemoryItem.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        title, summary, facts, tags, domain_id = _observation_fields(r)
        out.append(
            {
                "id": str(r.id),
                "kind": r.kind,
                "title": title,
                "summary": summary,
                "facts": facts,
                "tags": tags,
                "domain_id": domain_id,
                "text": r.text,
                "url": r.url,
                "weight": float(r.weight or 0.0),
                "payload": r.payload if isinstance(r.payload, dict) else None,
                "created_at": _iso_utc(r.created_at),
            }
        )
    return out


async def recall_context_for_query(
    owner: str,
    query: str,
    *,
    max_tokens: int = 250,
) -> str:
    """Best-effort compact episodic recall block injected into synthesis messages.

    Strictly fail-open and bounded by ``0.2s`` timeout + ``max_tokens`` words so
    an offline DB or empty observation table adds near-zero latency.
    """
    global _recall_backoff_until
    if max_tokens <= 0 or not (query or "").strip():
        return ""
    now_mono = time.monotonic()
    if now_mono < _recall_backoff_until:
        return ""
    try:
        hits = await asyncio.wait_for(
            search_memory_index(owner, query, kind="observation", limit=3),
            timeout=0.2,
        )
    except Exception:
        _recall_backoff_until = time.monotonic() + _RECALL_BACKOFF_S
        return ""
    if not hits:
        return ""
    # Skip exact same-query echoes or near-zero scores unless genuinely relevant
    lines: list[str] = []
    words_used = 0
    for h in hits:
        snippet = str(h.get("snippet") or h.get("title") or "").strip()
        if not snippet:
            continue
        entry = f"- [{h.get('title', '')}] {snippet}"
        w = len(entry.split())
        if words_used + w > max_tokens and lines:
            break
        lines.append(entry)
        words_used += w
    return "\n".join(lines)


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
    return int(getattr(result, "rowcount", 0) or 0)


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
