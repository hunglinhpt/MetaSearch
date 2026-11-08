"""Data retention (WP-13): delete expired rows in bounded batches.

Order follows the foreign keys: citations and semantic-cache rows of old answers first, then
the answers, the search results, and only then queries nobody references any more. Chunk
rows stay because citations point at them; only their content is blanked after
``chunks`` days, as ``docs/10-security-compliance.md`` describes.

Each batch is its own short transaction, so a large purge never holds a lock for long.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update

from app.core.logging import get_logger
from app.db.models import Answer, Chunk, Citation, EngineHealth, Query, SearchResult, SemanticCache
from app.db.session import get_sessionmaker

logger = get_logger(__name__)

BATCH_SIZE = 1000
RETENTION_DAYS = {
    "queries": 90,
    "search_results": 90,
    "answers": 90,
    "citations": 90,
    "chunks": 30,  # content only; the row stays for the citations that point at it
    "engine_health": 14,
}


async def run_retention(
    *, dry_run: bool = False, days: dict[str, int] | None = None
) -> dict[str, int]:
    """Delete expired rows table by table. Returns how many rows each step touched."""
    limits = {**RETENTION_DAYS, **(days or {})}
    now = datetime.now(UTC)
    cut_answers = now - timedelta(days=limits["answers"])
    cut_queries = now - timedelta(days=limits["queries"])
    cut_results = now - timedelta(days=limits["search_results"])
    cut_chunks = now - timedelta(days=limits["chunks"])
    cut_health = now - timedelta(days=limits["engine_health"])

    old_answers = select(Answer.id).where(Answer.created_at < cut_answers)
    referenced_queries = (
        select(Answer.query_id)
        .where(Answer.query_id.is_not(None))
        .union(select(SearchResult.query_id).where(SearchResult.query_id.is_not(None)))
    )
    results: dict[str, int] = {}
    results["citations"] = await _purge(
        Citation, Citation.id, Citation.answer_id.in_(old_answers), dry_run
    )
    results["semantic_cache"] = await _purge(
        SemanticCache,
        SemanticCache.id,
        (SemanticCache.answer_id.in_(old_answers)) | (SemanticCache.created_at < cut_answers),
        dry_run,
    )
    results["answers"] = await _purge(Answer, Answer.id, Answer.created_at < cut_answers, dry_run)
    results["search_results"] = await _purge(
        SearchResult, SearchResult.id, SearchResult.created_at < cut_results, dry_run
    )
    results["queries"] = await _purge(
        Query,
        Query.id,
        (Query.created_at < cut_queries) & Query.id.not_in(referenced_queries),
        dry_run,
    )
    results["chunks"] = await _blank_chunks(cut_chunks, dry_run)
    results["engine_health"] = await _purge(
        EngineHealth, None, EngineHealth.window_start < cut_health, dry_run
    )
    logger.info("retention_done", dry_run=dry_run, **results)
    return results


async def _purge(model: Any, key: Any, condition: Any, dry_run: bool) -> int:
    """Delete matching rows, at most BATCH_SIZE per transaction."""
    maker = get_sessionmaker()
    if dry_run:
        async with maker() as session:
            stmt = select(func.count()).select_from(model).where(condition)
            return int((await session.execute(stmt)).scalar_one())
    total = 0
    while True:
        async with maker() as session:
            if key is None:
                # Composite key (engine_health): a small table, one statement.
                result = await session.execute(delete(model).where(condition))
            else:
                ids = select(key).where(condition).limit(BATCH_SIZE)
                stmt = (
                    delete(model).where(key.in_(ids)).execution_options(synchronize_session=False)
                )
                result = await session.execute(stmt)
            await session.commit()
        deleted = int(result.rowcount or 0)
        total += deleted
        if key is None or deleted < BATCH_SIZE:
            return total


async def _blank_chunks(cutoff: datetime, dry_run: bool) -> int:
    condition = (Chunk.created_at < cutoff) & (Chunk.content != "")
    maker = get_sessionmaker()
    if dry_run:
        async with maker() as session:
            stmt = select(func.count()).select_from(Chunk).where(condition)
            return int((await session.execute(stmt)).scalar_one())
    total = 0
    while True:
        async with maker() as session:
            ids = select(Chunk.id).where(condition).limit(BATCH_SIZE)
            stmt = (
                update(Chunk)
                .where(Chunk.id.in_(ids))
                .values(content="", embedding=None)
                .execution_options(synchronize_session=False)
            )
            result = await session.execute(stmt)
            await session.commit()
        changed = int(result.rowcount or 0)
        total += changed
        if changed < BATCH_SIZE:
            return total


async def run_if_due(*, last_run_path: str | None = None) -> dict[str, int] | None:
    """Run retention at most once per day (Lite: no worker, run in API)."""
    if last_run_path:
        from pathlib import Path

        p = Path(last_run_path)

        def _read_last() -> float | None:
            try:
                return float(p.read_text().strip()) if p.exists() else None
            except ValueError:
                return None

        last = await asyncio.to_thread(_read_last)
        if last is not None and time.time() - last < 86400:
            return None
    results = await run_retention()
    if last_run_path:
        from pathlib import Path

        await asyncio.to_thread(Path(last_run_path).write_text, str(time.time()))
    return results


async def retention_loop(*, interval_s: float, last_run_path: str | None) -> None:
    """Background loop for Lite: check once per interval, run at most once a day."""
    while True:
        with contextlib.suppress(Exception):
            await run_if_due(last_run_path=last_run_path)
        await asyncio.sleep(interval_s)
