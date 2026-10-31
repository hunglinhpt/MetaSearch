"""Async engine / session factory.

Everything is created lazily so importing the app never requires a live
database (important for unit tests and for ``/healthz``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        is_sqlite = settings.database_url.startswith("sqlite")
        kwargs: dict = {"echo": False}
        if is_sqlite:
            from sqlalchemy.pool import StaticPool

            if ":memory:" in settings.database_url or settings.database_url.endswith(
                ":///:memory:"
            ):
                # A single shared connection: in-memory SQLite is per-connection
                # otherwise, which would give every pool checkout a fresh DB.
                kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
        else:
            kwargs.update(
                pool_pre_ping=True,
                # Fan-out health writes, persistence and query endpoints run
                # concurrently; keep headroom so requests never wait on the pool.
                pool_size=10,
                max_overflow=10,
                pool_timeout=settings.db_pool_timeout_s,
                # Fail fast on an unreachable Postgres (fallback paths rely on it).
                connect_args={"timeout": 3, "command_timeout": 10},
            )
        _engine = create_async_engine(settings.database_url, **kwargs)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    """Release pooled connections on shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
