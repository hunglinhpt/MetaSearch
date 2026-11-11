"""Watchlist + news alerts (docs/18 v2.3.0 Wave C).

GET    /v1/watchlist           -> {items, storage} owned by the current key
POST   /v1/watchlist           -> add {query, label?} (cap per key)
DELETE /v1/watchlist/{id}      -> remove (404 unless owner)
GET    /v1/watchlist/alerts    -> poll-on-demand: one search per item (bounded
                                  concurrency + total wall-clock), diff top URLs
                                  against the stored baseline

Design notes:
- Tenancy by ``current_api_key_id`` (anonymous single-operator Lite uses the
  "anonymous" owner, like /v1/queries).
- Alerts are PULL-based: the client calls /alerts on its own schedule. There
  is deliberately no background poller burning search budget silently (docs/18
  §7 decision).
- Concurrency safety: every read-modify-write of one owner's items runs under
  a per-owner asyncio.Lock, and the write is an UPSERT per item id (never a
  delete-all + re-insert), so a concurrent POST/DELETE cannot be clobbered.
- Baseline honesty: the baseline (last_hash/last_urls) only advances when the
  search outcome was complete AND non-empty — a degraded engine must never
  erase the "new" diff of the previous good poll.
- DB down -> bounded in-memory fallback per owner (a sticky per-owner flag
  keeps reads and writes on the SAME storage until the DB recovers, so the two
  never diverge). Every response carries ``storage`` so the UI can show it.
- Nothing is fabricated: URLs come from the search pipeline's real results.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter
from sqlalchemy import Table, delete, select

from app.api import deps
from app.api.v1.schemas import (
    WatchlistAlertItem,
    WatchlistAlertsResponse,
    WatchlistCreateRequest,
    WatchlistItemOut,
    WatchlistListResponse,
)
from app.config import get_settings
from app.core.admission import admit
from app.core.auth import current_api_key_id
from app.core.errors import ErrorCode, MetaSearchError
from app.core.logging import get_logger
from app.db.models import WatchlistItem
from app.orchestration.pipeline import SearchParams

router = APIRouter(tags=["watchlist"])
logger = get_logger(__name__)

# --- storage ---------------------------------------------------------------
# Bounded in-memory fallback used when the DB is unreachable: 100 owners x
# cap items, LRU by owner so one tenant cannot evict everyone else.
_MEM_MAX_OWNERS = 100
_mem_store: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
# Owners currently served from memory (sticky until a DB read succeeds again).
_mem_owners: set[str] = set()
_owner_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()
_table_ready = False


_OWNER_LOCKS_MAX = 1024


async def _owner_lock(owner: str) -> asyncio.Lock:
    async with _locks_guard:
        lock = _owner_locks.get(owner)
        if lock is None:
            lock = asyncio.Lock()
            _owner_locks[owner] = lock
            # Bound the map: evict the oldest UNLOCKED entry when full. Never
            # evict a lock that is held or contended — deleting it would alias
            # two locks to one owner and break mutual exclusion.
            while len(_owner_locks) > _OWNER_LOCKS_MAX:
                for oldest in list(_owner_locks):
                    if oldest != owner and not _owner_locks[oldest].locked():
                        del _owner_locks[oldest]
                        break
                else:
                    break
        return lock


async def _ensure_table() -> bool:
    """Create the watchlist table if missing (works on Postgres AND SQLite;
    the Alembic migration 0006 is the primary path, this is the safety net)."""
    global _table_ready
    if _table_ready:
        return True
    try:
        from app.db.session import get_engine

        table = cast(Table, WatchlistItem.__table__)
        async with get_engine().begin() as conn:
            await conn.run_sync(table.create, checkfirst=True)
        _table_ready = True
        return True
    except Exception as exc:
        logger.warning("watchlist_table_ensure_failed", error=type(exc).__name__)
        return False


def _owner() -> str:
    return current_api_key_id.get() or "anonymous"


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _row_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "query": r.query,
        "label": r.label,
        "created_at": r.created_at,
        "last_checked_at": r.last_checked_at,
        "last_hash": r.last_hash,
        "last_urls": r.last_urls,
    }


def _item_out(it: dict[str, Any]) -> WatchlistItemOut:
    return WatchlistItemOut(
        id=str(it["id"]),
        query=it["query"],
        label=it.get("label"),
        created_at=_iso(it.get("created_at")),
        last_checked_at=_iso(it.get("last_checked_at")),
        last_hash=it.get("last_hash"),
    )


async def _list_db(owner: str) -> list[dict[str, Any]] | None:
    """Return the owner's rows as plain dicts, or None when the DB is down."""
    if not await _ensure_table():
        return None
    try:
        async with deps.get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        select(WatchlistItem)
                        .where(WatchlistItem.api_key_id == owner)
                        .order_by(WatchlistItem.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            return [_row_dict(r) for r in rows]
    except Exception as exc:
        logger.warning("watchlist_db_read_failed", error=type(exc).__name__)
        return None


def _list_mem(owner: str) -> list[dict[str, Any]]:
    items = _mem_store.get(owner)
    if items is not None:
        _mem_store.move_to_end(owner)
    return list(items or [])


def _mem_put(owner: str, items: list[dict[str, Any]]) -> None:
    _mem_store[owner] = items
    _mem_store.move_to_end(owner)
    while len(_mem_store) > _MEM_MAX_OWNERS:
        evicted, _ = _mem_store.popitem(last=False)
        _mem_owners.discard(evicted)
        _mem_probe_at.pop(evicted, None)


# Time-throttled DB re-probe for owners in memory-mode: a short DB outage must
# not pin an owner to RAM for the life of the process (and lose the items on
# restart). At most one probe per owner per interval.
_MEM_PROBE_INTERVAL_S = 30.0
_mem_probe_at: dict[str, float] = {}


async def _list_items(owner: str) -> tuple[list[dict[str, Any]], str]:
    """One coherent storage per owner: memory only when the owner is flagged
    memory-mode AND the DB probe (throttled) still fails; a successful probe
    migrates the memory items back to the DB and clears the flag."""
    if owner in _mem_owners:
        now = time.monotonic()
        last = _mem_probe_at.get(owner, 0.0)
        if now - last >= _MEM_PROBE_INTERVAL_S:
            _mem_probe_at[owner] = now
            rows = await _list_db(owner)
            if rows is not None:
                # DB recovered: the MEMORY list is the authoritative id set —
                # it saw every mutation during the outage, so a DB row whose id
                # is absent from memory was deleted mid-outage and must NOT be
                # resurrected. _save_items upserts these and deletes the rest.
                items = _list_mem(owner)
                await _save_items(owner, items, "db")
                _mem_owners.discard(owner)
                _mem_store.pop(owner, None)
                return items, "db"
        return _list_mem(owner), "memory"
    rows = await _list_db(owner)
    if rows is not None:
        return rows, "db"
    _mem_owners.add(owner)
    _mem_probe_at[owner] = time.monotonic()
    return _list_mem(owner), "memory"


async def _save_items(owner: str, items: list[dict[str, Any]], storage: str) -> None:
    """UPSERT per item id + delete removed ids, in ONE transaction — never a
    delete-all + re-insert, so concurrent requests cannot lose rows."""
    if storage == "memory":
        _mem_put(owner, items)
        return
    try:
        async with deps.get_session_factory()() as session, session.begin():
            existing = (
                (
                    await session.execute(
                        select(WatchlistItem.id).where(WatchlistItem.api_key_id == owner)
                    )
                )
                .scalars()
                .all()
            )
            wanted = {str(it["id"]) for it in items}
            for old_id in existing:
                if str(old_id) not in wanted:
                    await session.execute(delete(WatchlistItem).where(WatchlistItem.id == old_id))
            for it in items:
                item_id = it["id"] if isinstance(it["id"], uuid.UUID) else uuid.UUID(str(it["id"]))
                row = await session.get(WatchlistItem, item_id)
                if row is None:
                    session.add(
                        WatchlistItem(
                            id=item_id,
                            api_key_id=owner,
                            query=it["query"],
                            label=it.get("label"),
                            last_hash=it.get("last_hash"),
                            last_urls=it.get("last_urls"),
                            last_checked_at=it.get("last_checked_at"),
                            created_at=it.get("created_at") or datetime.now(UTC),
                        )
                    )
                else:
                    row.query = it["query"]
                    row.label = it.get("label")
                    row.last_hash = it.get("last_hash")
                    row.last_urls = it.get("last_urls")
                    row.last_checked_at = it.get("last_checked_at")
    except Exception as exc:
        # DB write failed: fall back to memory for THIS owner stickily, so the
        # next read does not silently resurrect stale DB rows (split-brain).
        logger.warning("watchlist_db_write_failed", error=type(exc).__name__)
        _mem_owners.add(owner)
        _mem_put(owner, items)


# --- endpoints ---------------------------------------------------------------


@router.get("/watchlist", response_model=WatchlistListResponse)
async def list_watchlist() -> WatchlistListResponse:
    items, storage = await _list_items(_owner())
    return WatchlistListResponse(items=[_item_out(it) for it in items], storage=storage)


@router.post("/watchlist", response_model=WatchlistItemOut, status_code=201)
async def add_watchlist(request: WatchlistCreateRequest) -> WatchlistItemOut:
    settings = get_settings()
    owner = _owner()
    query = " ".join(request.query.split())
    async with await _owner_lock(owner):
        items, storage = await _list_items(owner)
        if any(it["query"] == query for it in items):
            raise MetaSearchError(
                ErrorCode.INVALID_REQUEST, "query already watched", status_code=409
            )
        if len(items) >= settings.watchlist_max_items:
            raise MetaSearchError(
                ErrorCode.INVALID_REQUEST,
                f"watchlist full: {len(items)} >= cap {settings.watchlist_max_items}",
                status_code=400,
                details={"cap": settings.watchlist_max_items},
            )
        item = {
            "id": uuid.uuid4(),
            "query": query,
            "label": (request.label or "").strip() or None,
            "created_at": datetime.now(UTC),
            "last_checked_at": None,
            "last_hash": None,
            "last_urls": None,
        }
        items.append(item)
        await _save_items(owner, items, storage)
    return _item_out(item)


@router.delete("/watchlist/{item_id}")
async def remove_watchlist(item_id: str) -> dict[str, Any]:
    try:
        uid = uuid.UUID(item_id)
    except ValueError as exc:
        raise MetaSearchError(ErrorCode.INVALID_REQUEST, "invalid id", status_code=400) from exc
    owner = _owner()
    async with await _owner_lock(owner):
        items, storage = await _list_items(owner)
        kept = [it for it in items if str(it["id"]) != str(uid)]
        if len(kept) == len(items):
            # Not found OR owned by another key — same 404, no existence leak.
            raise MetaSearchError(ErrorCode.NOT_FOUND, "watchlist item not found", status_code=404)
        await _save_items(owner, kept, storage)
    return {"deleted": str(uid)}


def _hash_urls(urls: list[str]) -> str:
    return hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()[:32]


@router.get("/watchlist/alerts", response_model=WatchlistAlertsResponse)
async def watchlist_alerts() -> WatchlistAlertsResponse:
    settings = get_settings()
    owner = _owner()
    started = time.perf_counter()
    # Lock for the whole read-check-write so two concurrent /alerts calls for
    # the same owner cannot clobber each other's baseline.
    async with await _owner_lock(owner):
        items, storage = await _list_items(owner)
        alerts: list[WatchlistAlertItem] = []
        total_new = 0

        async with admit("watchlist"):
            pipeline = deps.get_search_pipeline()
            # Items fan out with bounded concurrency under ONE total wall-clock
            # cap: a full watchlist can never hold an admission slot for minutes.
            sem = asyncio.Semaphore(max(1, settings.watchlist_alerts_concurrency))

            async def check(it: dict[str, Any]) -> WatchlistAlertItem:
                checked_at = datetime.now(UTC)
                async with sem:
                    try:
                        outcome = await pipeline.run(
                            SearchParams(
                                query=it["query"],
                                engines=["google_news"],
                                k=settings.watchlist_alerts_k,
                                news=True,
                                freshness_days=settings.watchlist_alerts_freshness_days,
                                timeout_ms=settings.watchlist_alerts_timeout_ms,
                            )
                        )
                        urls = [r.url for r in outcome.results]
                        # Baseline honesty: never advance on an incomplete or
                        # empty outcome — a degraded engine must not erase the
                        # diff, and an empty poll must not reset the baseline.
                        if not outcome.complete or not urls:
                            return WatchlistAlertItem(
                                id=str(it["id"]),
                                query=it["query"],
                                label=it.get("label"),
                                checked_at=_iso(checked_at),
                                error="search_incomplete",
                            )
                        new_hash = _hash_urls(urls)
                        prev_urls = set(it.get("last_urls") or [])
                        baseline_set = it.get("last_hash") is None
                        new_items = (
                            []
                            if baseline_set
                            else [
                                {
                                    "url": r.url,
                                    "title": r.title,
                                    "published_at": _iso(r.published_at),
                                    "domain": r.domain,
                                }
                                for r in outcome.results
                                if r.url not in prev_urls
                            ]
                        )
                        it["last_hash"] = new_hash
                        it["last_urls"] = urls
                        it["last_checked_at"] = checked_at
                        return WatchlistAlertItem(
                            id=str(it["id"]),
                            query=it["query"],
                            label=it.get("label"),
                            baseline_set=baseline_set,
                            new_items=new_items,
                            checked_at=_iso(checked_at),
                        )
                    except Exception as exc:
                        # One item's failure must not sink the others; the raw
                        # exception may carry internal hostnames/URLs, so the
                        # client gets a stable code and the log keeps details.
                        logger.warning(
                            "watchlist_alert_failed",
                            query=it["query"],
                            error=type(exc).__name__,
                            detail=str(exc)[:300],
                        )
                        return WatchlistAlertItem(
                            id=str(it["id"]),
                            query=it["query"],
                            label=it.get("label"),
                            checked_at=_iso(checked_at),
                            error="search_failed",
                        )

            tasks = [asyncio.create_task(check(it)) for it in items]
            try:
                async with asyncio.timeout(settings.watchlist_alerts_total_timeout_s):
                    alerts = list(await asyncio.gather(*tasks))
            except TimeoutError:
                # Partial answer is still an answer: keep the items that
                # finished, mark the rest as not checked (baseline untouched —
                # check() only mutates its item on success).
                logger.warning("watchlist_alerts_deadline", owner=owner, n_items=len(items))
                for t in tasks:
                    if not t.done():
                        t.cancel()
                alerts = []
                for it, t in zip(items, tasks, strict=True):
                    if t.done() and not t.cancelled() and not t.exception():
                        alerts.append(t.result())
                    else:
                        alerts.append(
                            WatchlistAlertItem(
                                id=str(it["id"]),
                                query=it["query"],
                                label=it.get("label"),
                                error="deadline_exceeded",
                            )
                        )
            total_new = sum(len(a.new_items) for a in alerts)
            await _save_items(owner, items, storage)

    return WatchlistAlertsResponse(
        items=alerts,
        total_new=total_new,
        latency_ms=int((time.perf_counter() - started) * 1000),
        storage=storage,
    )
