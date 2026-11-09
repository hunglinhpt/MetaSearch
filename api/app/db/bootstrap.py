"""SQLite bootstrap for the Lite profile (docs/08).

Lite runs the same Alembic migrations as Postgres (they are dialect-aware), so an
old Lite database gets new indexes and data changes, not only new columns:

* empty database, or one Alembic already manages: ``alembic upgrade head``;
* database made by an older build with ``create_all`` (tables, no
  ``alembic_version``): bring it to the model shape (create missing tables, add
  missing columns via ``PRAGMA table_info`` + ``ALTER TABLE``), then
  ``alembic stamp head`` so later migrations apply;
* in-memory database, or the migration scripts are missing or fail: the
  ``create_all`` path, so a broken migration never stops Lite from starting.

Idempotent — cheap to call every boot.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import func, inspect, text, update

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


_SCRIPT_DIR = Path(__file__).resolve().parents[2] / "alembic"


def _alembic(action: str) -> None:
    """Run ``alembic upgrade|stamp head`` (in a worker thread: env.py calls asyncio.run)."""
    from alembic.config import Config

    from alembic import command

    if not (_SCRIPT_DIR / "env.py").is_file():
        raise FileNotFoundError(_SCRIPT_DIR)
    cfg = Config()  # no ini file: its logging section would reset the app's logging
    cfg.set_main_option("script_location", str(_SCRIPT_DIR))
    cfg.set_main_option("path_separator", "os")
    if action == "upgrade":
        command.upgrade(cfg, "head")
    else:
        command.stamp(cfg, "head")


def _schema_mode(engine, tables: set[str]) -> str:
    database = engine.url.database or ""
    if database in {"", ":memory:"} or "mode=memory" in str(engine.url):
        return "create_all"  # Alembic opens its own engine: it would not see this database
    if not tables or "alembic_version" in tables:
        return "upgrade"
    return "legacy"


async def ensure_schema() -> bool:
    """Bring the SQLite schema to head. Returns True if applied (False: not SQLite)."""
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return False
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
    mode = _schema_mode(engine, tables)
    if mode == "upgrade":
        try:
            await asyncio.to_thread(_alembic, "upgrade")
        except Exception as exc:  # a broken migration must not stop Lite from starting
            logger.error("sqlite_migration_failed", error=f"{type(exc).__name__}: {exc}")
            mode = "create_all"
    # Always repair drift as well (a table, column or index the models declare but the file
    # lacks); after a clean upgrade this finds nothing.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _add_missing_columns(conn)
        await conn.run_sync(_add_missing_indexes)
    if mode == "legacy":
        try:
            await asyncio.to_thread(_alembic, "stamp")
        except Exception as exc:
            logger.error("sqlite_stamp_failed", error=f"{type(exc).__name__}: {exc}")
    async with engine.begin() as conn:
        # WAL keeps concurrent reads cheap on a laptop disk.
        await conn.execute(text("PRAGMA journal_mode=WAL"))
    logger.info("sqlite_schema_ready", mode=mode)
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


def _add_missing_indexes(sync_conn) -> None:
    """create_all skips indexes of tables that already exist; add the ones a model declares."""
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            index.create(sync_conn, checkfirst=True)
