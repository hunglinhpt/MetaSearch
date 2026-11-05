"""SQLite bootstrap for the Lite profile (docs/08).

Postgres uses Alembic migrations; SQLite (no Docker) creates its schema
directly from the models at startup. For existing databases, missing columns
are auto-detected by comparing ``Base.metadata`` against the live schema
(via ``PRAGMA table_info``) and added with ``ALTER TABLE``.

Idempotent — cheap to call every boot.
"""

from __future__ import annotations

from sqlalchemy import func, text, update

from app.core.logging import get_logger
from app.db.base import Base
from app.db.session import get_engine

logger = get_logger(__name__)


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


_NON_CONSTANT_DEFAULTS = {"now()", "current_timestamp", "current_date", "current_time"}


def _column_default(column) -> str:
    """Render a safe DEFAULT clause, or "" when it is not a SQLite constant.

    SQLite rejects non-constant defaults (``now()``) in ALTER TABLE ADD COLUMN, so
    those columns are added without DEFAULT. String defaults are quoted/escaped.
    """
    sd = column.server_default
    arg = getattr(sd, "arg", None) if sd is not None else None
    if arg is None:
        return ""
    if isinstance(arg, bool):
        return f" DEFAULT {int(arg)}"
    if isinstance(arg, (int, float)):
        return f" DEFAULT {arg}"
    if not isinstance(arg, str):
        return ""  # SQL expression object (func.now(), text(...)) is not a constant
    value = arg.strip()
    lowered = value.lower()
    if lowered in _NON_CONSTANT_DEFAULTS or lowered.endswith("()"):
        return ""
    if lowered in {"true", "false"}:
        return f" DEFAULT {1 if lowered == 'true' else 0}"
    try:
        float(value)
        return f" DEFAULT {value}"
    except ValueError:
        pass
    escaped = arg.replace("'", "''")
    return f" DEFAULT '{escaped}'"


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
            default = _column_default(column)
            await conn.execute(
                text(f"ALTER TABLE {table_name} ADD COLUMN {column.name} {ddl}{default}")
            )
            if not default and column.server_default is not None and ddl == "DATETIME":
                # Non-constant default (now()) cannot be part of ALTER TABLE; backfill rows.
                await conn.execute(
                    update(table)
                    .where(column.is_(None))
                    .values({column.name: func.current_timestamp()})
                )
            logger.info("sqlite_column_added", table=table_name, column=column.name)
