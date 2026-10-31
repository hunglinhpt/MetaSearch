"""SQLite bootstrap for the Lite profile (docs/08).

Postgres uses Alembic migrations; SQLite (no Docker) creates its schema
directly from the models at startup. Idempotent — cheap to call every boot.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.base import Base
from app.db.session import get_engine

logger = get_logger(__name__)


async def ensure_schema() -> bool:
    """Create all tables when the database is SQLite. Returns True if applied."""
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return False
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # WAL keeps concurrent reads cheap on a laptop disk.
        await conn.execute(text("PRAGMA journal_mode=WAL"))
    logger.info("sqlite_schema_ready")
    return True
