"""SQLite bootstrap for the Lite profile (docs/08).

Postgres uses Alembic migrations; SQLite (no Docker) creates its schema
directly from the models at startup. For existing databases, missing columns
are auto-detected by comparing ``Base.metadata`` against the live schema
(via ``PRAGMA table_info``) and added with ``ALTER TABLE``.

Idempotent — cheap to call every boot.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.base import Base
from app.db.session import get_engine

logger = get_logger(__name__)

# SQLite type mapping from SQLAlchemy column types.
_DDL = {
    "TEXT": "TEXT",
    "INTEGER": "INTEGER",
    "REAL": "REAL",
}


def _column_ddl(column) -> str:
    """Best-effort SQLite DDL for a SQLAlchemy column."""
    from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text

    for py_type in (String, Text):
        if isinstance(column.type, py_type):
            return "TEXT"
    if isinstance(column.type, (Integer,)) or (
        hasattr(column.type, "python_type") and column.type.python_type is int
    ):
        return "INTEGER"
    if isinstance(column.type, (Float,)) or (
        hasattr(column.type, "python_type") and column.type.python_type is float
    ):
        return "REAL"
    if isinstance(column.type, Boolean):
        return "INTEGER"
    if isinstance(column.type, DateTime):
        return "DATETIME"
    return "TEXT"  # safe default


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
    """Compare Base.metadata against live schema; ALTER TABLE for gaps."""
    for table_name, table in Base.metadata.tables.items():
        result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
        existing = {row[1] for row in result.fetchall()}
        if not existing:
            continue  # table doesn't exist yet — create_all handles it
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = _column_ddl(column)
            default = ""
            if column.server_default is not None and column.server_default.arg is not None:
                default = f" DEFAULT {column.server_default.arg}"
            await conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {column.name} {ddl}{default}")
            )
            logger.info("sqlite_column_added", table=table_name, column=column.name)
