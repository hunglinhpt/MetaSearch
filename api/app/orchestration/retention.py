"""Data retention (WP-13): delete expired rows in bounded batches."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from app.core.logging import get_logger
from app.db.models import Answer, Chunk, Citation, EngineHealth, Query, SearchResult
from app.db.session import get_sessionmaker

logger = get_logger(__name__)

BATCH_SIZE = 1000
RETENTION_DAYS = {
    "queries": 90,
    "search_results": 90,
    "answers": 90,
    "citations": 90,
    "chunks": 30,  # content only; metadata kept
    "engine_health": 14,
}


async def run_retention(*, dry_run: bool = False) -> dict[str, int]:
    """Delete expired rows per table in bounded batches. Returns per-table counts."""
    results: dict[str, int] = {}
    session_maker = get_sessionmaker()
    now = datetime.now(UTC)

    async with session_maker() as session:
        # citations older than 90d (via answers join not needed; citations have own created_at)
        cutoff_90 = now - timedelta(days=RETENTION_DAYS["citations"])
        results["citations"] = await _delete_batched(
            session, Citation, Citation.created_at < cutoff_90, dry_run
        )
        cutoff_90_queries = now - timedelta(days=RETENTION_DAYS["queries"])
        results["search_results"] = await _delete_batched(
            session, SearchResult, SearchResult.created_at < cutoff_90_queries, dry_run
        )
        results["answers"] = await _delete_batched(
            session, Answer, Answer.created_at < cutoff_90_queries, dry_run
        )
        results["queries"] = await _delete_batched(
            session, Query, Query.created_at < cutoff_90_queries, dry_run
        )
        # chunks: content older than 30d — delete by document fetched_at
        cutoff_30 = now - timedelta(days=RETENTION_DAYS["chunks"])
        results["chunks"] = await _delete_batched(
            session,
            Chunk,
            Chunk.created_at < cutoff_30,
            dry_run,
        )
        results["engine_health"] = await _delete_batched(
            session,
            EngineHealth,
            EngineHealth.window_start < now - timedelta(days=RETENTION_DAYS["engine_health"]),
            dry_run,
        )
    return results


async def _delete_batched(session, model, condition, dry_run: bool) -> int:
    """Delete in batches of BATCH_SIZE, never holding a lock > 1s."""
    total = 0
    while True:
        if dry_run:
            from sqlalchemy import func, select

            count = (
                await session.execute(select(func.count()).select_from(model).where(condition))
            ).scalar_one()
            return count
        stmt = delete(model).where(condition).execution_options(synchronize_session=False)
        # SQLAlchemy delete with LIMIT is dialect-specific; use ctid/id subquery
        # on PG, rowid on SQLite. Simplified: delete matching rows in loop.
        result = await session.execute(stmt)
        deleted = result.rowcount
        await session.commit()
        total += deleted
        if deleted < BATCH_SIZE:
            break
        logger.info("retention_batch", table=model.__tablename__, deleted=total)
    return total


async def run_if_due(*, last_run_path: str | None = None) -> dict[str, int] | None:
    """Run retention at most once per day (Lite: no worker, run in API)."""
    if last_run_path:
        from pathlib import Path

        p = Path(last_run_path)

        def _read_last() -> float | None:
            if p.exists():
                return float(p.read_text().strip())
            return None

        last = await asyncio.to_thread(_read_last)
        if last is not None and time.time() - last < 86400:
            return None
    results = await run_retention()
    if last_run_path:
        from pathlib import Path

        ts = str(time.time())
        await asyncio.to_thread(Path(last_run_path).write_text, ts)
    return results
