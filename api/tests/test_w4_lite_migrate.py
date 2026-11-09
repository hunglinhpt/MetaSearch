"""Lite brings its SQLite schema to the Alembic head, including databases made by create_all."""

from __future__ import annotations

import sqlite3

import pytest

import app.db.bootstrap as bootstrap
from app.api.ops import schema_version
from app.config import get_settings
from app.db.base import Base
from app.db.session import dispose_engine


@pytest.fixture
def db_file(tmp_path, monkeypatch):
    path = tmp_path / "lite.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


async def _boot() -> None:
    await dispose_engine()
    assert await bootstrap.ensure_schema() is True
    await dispose_engine()


def _version(path) -> str | None:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def _indexes(path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        con.close()


def _columns(path, table) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _model_indexes() -> set[str]:
    # pgvector HNSW indexes exist only on Postgres; the migrations skip them on SQLite.
    return {
        i.name
        for t in Base.metadata.tables.values()
        for i in t.indexes
        if i.name and "using" not in i.dialect_options["postgresql"]._non_defaults
    }


async def test_fresh_database_is_migrated_to_head(db_file):
    await _boot()
    assert _version(db_file) == schema_version() is not None
    assert _model_indexes() <= _indexes(db_file)


async def test_second_boot_is_a_no_op(db_file):
    await _boot()
    await _boot()
    assert _version(db_file) == schema_version()


async def test_legacy_create_all_database_is_repaired_and_stamped(db_file):
    # An older build: tables from create_all, no alembic_version, a column and an index missing.
    con = sqlite3.connect(db_file)
    con.execute("CREATE TABLE api_keys (id TEXT PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE queries (id TEXT PRIMARY KEY, text TEXT, normalized_text TEXT)")
    con.commit()
    con.close()
    await _boot()
    assert _version(db_file) == schema_version()
    assert "idx_queries_normalized" in _indexes(db_file)
    assert {c.name for c in Base.metadata.tables["queries"].columns} <= _columns(db_file, "queries")
    assert set(Base.metadata.tables) <= {
        r[0]
        for r in sqlite3.connect(db_file).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


async def test_failed_migration_falls_back_to_create_all(db_file, monkeypatch):
    def broken(action):
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(bootstrap, "_alembic", broken)
    await _boot()
    assert set(Base.metadata.tables) <= {
        r[0]
        for r in sqlite3.connect(db_file).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


async def test_in_memory_database_does_not_run_alembic(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    get_settings.cache_clear()
    called = []
    monkeypatch.setattr(bootstrap, "_alembic", lambda action: called.append(action))
    try:
        await _boot()
    finally:
        get_settings.cache_clear()
    assert called == []
