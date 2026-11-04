"""SQLite bootstrap for the Lite profile (docs/08).

Postgres uses Alembic migrations; SQLite (no Docker) creates its schema
directly from the models at startup. For existing databases, missing columns
are added via ALTER TABLE (WP-11 FIX1: api_key_id on queries/answers).
Idempotent — cheap to call every boot.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.base import Base
from app.db.session import get_engine

logger = get_logger(__name__)

# Columns added after the initial create_all, per table.
# Each entry: (table, column_name, column_ddl)
_MIGRATION_COLUMNS: list[tuple[str, str, str]] = [
    ("queries", "api_key_id", "TEXT"),
    ("answers", "api_key_id", "TEXT"),
]


async def ensure_schema() -> bool:
    """Create all tables and add missing columns. Returns True if applied."""
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return False
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _add_missing_columns(conn)
        # WAL keeps concurrent reads cheap on a laptop disk.
        await conn.execute(text("PRAGMA journal_mode=WAL"))
    logger.info("sqlite_schema_ready")
    return True


async def _add_missing_columns(conn) -> None:
    for table, column, ddl in _MIGRATION_COLUMNS:
        # Check if column already exists
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        existing = {row[1] for row in result.fetchall()}
        if column in existing:
            continue
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        logger.info("sqlite_column_added", table=table, column=column)
