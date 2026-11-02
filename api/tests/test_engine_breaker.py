"""Tests for Engine Circuit Breaker, fail-open, and zero DB on search hot path (WP-03).

Covers:
- test_single_engine_recovers_after_cooldown (P14)
- test_all_open_fails_open
- test_local_overload_does_not_open_breaker
- test_retry_after_respected
- test_auto_disabled_legacy_rows_cleared_on_boot
- test_fanout_budget_holds_with_slow_store (P15)
- test_search_hot_path_issues_no_sql
- test_black_hole_db_costs_once_per_30s
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import Base
from app.db.models import Engine as EngineRow
from app.orchestration.engine_store import (
    DbEngineStore,
    InMemoryEngineStore,
    clear_legacy_auto_disabled,
)
from app.orchestration.pipeline import SearchParams, SearchPipeline
from app.retrieval.adapters.base import AdapterError
from app.retrieval.health import EngineBreaker, EngineBreakerRegistry, Outcome
from app.retrieval.models import RawResult
from app.retrieval.registry import EngineRegistry


class DummyAdapter:
    def __init__(self, name: str = "dummy", fail: bool = False, delay: float = 0.0) -> None:
        self.name = name
        self.fail = fail
        self.delay = delay
        self.call_count = 0

    async def search(self, query: str, **kwargs: Any) -> list[RawResult]:
        self.call_count += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise AdapterError(f"{self.name} failed", kind=Outcome.UPSTREAM_ERROR)
        return [
            RawResult(
                title=f"Result from {self.name}",
                url=f"https://example.com/{self.name}",
                snippet=f"Snippet for {query}",
                adapter=self.name,
            )
        ]


def test_local_overload_does_not_open_breaker() -> None:
    breaker = EngineBreaker(threshold=3)
    t = 1000.0
    for _ in range(10):
        breaker.record(Outcome.LOCAL_OVERLOAD, now=t)
        breaker.record(Outcome.CANCELLED, now=t)

    assert breaker.consecutive_failures == 0
    assert breaker.state == "closed"
    assert breaker.allow(now=t) is True


def test_retry_after_respected() -> None:
    breaker = EngineBreaker(threshold=3)
    t0 = 1000.0
    # Trip with retry_after_s = 60
    breaker.record(Outcome.RATE_LIMITED, now=t0, retry_after_s=60.0)
    breaker.record(Outcome.RATE_LIMITED, now=t0, retry_after_s=60.0)
    breaker.record(Outcome.RATE_LIMITED, now=t0, retry_after_s=60.0)

    assert breaker.state == "open"
    assert breaker.retry_at == pytest.approx(1060.0)

    # At t0 + 30s, breaker is still open
    assert breaker.allow(now=t0 + 30.0) is False

    # At t0 + 60s, trial is allowed (half-open)
    assert breaker.allow(now=t0 + 60.1) is True
    assert breaker.state == "half_open"

    # Only 1 trial in-flight allowed in half-open
    assert breaker.allow(now=t0 + 60.2) is False


def test_single_engine_recovers_after_cooldown() -> None:
    """P14: single engine trips, enters cooldown, and recovers cleanly."""
    breaker = EngineBreaker(threshold=3)
    t0 = 1000.0

    # 3 failures -> trips
    for i in range(3):
        breaker.record(Outcome.UPSTREAM_ERROR, now=t0 + i)

    assert breaker.state == "open"
    retry_time = breaker.retry_at
    assert retry_time > t0

    # Before cooldown expires -> rejected
    assert breaker.allow(now=retry_time - 1.0) is False

    # After cooldown expires -> half-open trial allowed
    assert breaker.allow(now=retry_time + 1.0) is True
    assert breaker.state == "half_open"

    # Upstream succeeds -> reset to closed
    breaker.record(Outcome.OK, now=retry_time + 1.5)
    assert breaker.state == "closed"
    assert breaker.consecutive_failures == 0
    assert breaker.allow(now=retry_time + 2.0) is True


@pytest.mark.asyncio
async def test_all_open_fails_open() -> None:
    """When all engines trip, fail-open selects the one with the earliest retry_at."""
    settings = Settings(_env_file=None, searxng_enabled=True)
    store = InMemoryEngineStore.from_settings(settings)
    await store.update("ddg", {"enabled": True})
    await store.update("searxng", {"enabled": True})

    adapter1 = DummyAdapter("ddg", fail=True)
    adapter2 = DummyAdapter("searxng", fail=True)

    registry = EngineRegistry(store, {"ddg": adapter1, "searxng": adapter2})
    health = EngineBreakerRegistry(threshold=3)

    # Trip both breakers
    t0 = time.monotonic()
    for _ in range(3):
        health.record("ddg", Outcome.UPSTREAM_ERROR, now=t0)
        health.record("searxng", Outcome.UPSTREAM_ERROR, now=t0 + 50.0)

    assert health.get("ddg").state == "open"
    assert health.get("searxng").state == "open"
    assert health.get("ddg").retry_at < health.get("searxng").retry_at

    pipe = SearchPipeline(settings, registry, cache=None, health_registry=health)

    # Running search must NOT fail with "No search engine is enabled for this request"
    # It must fail-open and attempt the earliest engine (ddg)
    out = await pipe.run(SearchParams(query="test fail open"))
    statuses = {s.engine: s.status for s in out.statuses}
    assert "skipped_breaker" in statuses.values()
    assert statuses.get("ddg") == "error"
    assert statuses.get("searxng") == "skipped_breaker"


@pytest.mark.asyncio
async def test_fanout_budget_holds_with_slow_store() -> None:
    """P15: Slow store write must NOT delay search fan-out beyond budget + 50ms."""

    class SlowStore(InMemoryEngineStore):
        async def record_result(self, engine_id: str, *, ok: bool, latency_ms: int) -> None:
            await asyncio.sleep(0.5)
            await super().record_result(engine_id, ok=ok, latency_ms=latency_ms)

    settings = Settings(_env_file=None, search_fanout_budget_s=0.2)
    store = SlowStore.from_settings(settings)
    slow_adapter = DummyAdapter("ddg", delay=10.0)  # Will hit the 0.2s budget timeout
    registry = EngineRegistry(store, {"ddg": slow_adapter})
    pipe = SearchPipeline(settings, registry, cache=None)

    t0 = time.perf_counter()
    out = await pipe.run(SearchParams(query="test budget"))
    elapsed = time.perf_counter() - t0

    assert elapsed <= settings.search_fanout_budget_s + 0.05
    assert len(out.statuses) == 1
    assert out.statuses[0].status == "timeout"


@pytest.mark.asyncio
async def test_auto_disabled_legacy_rows_cleared_on_boot(tmp_path) -> None:
    """Legacy auto: disabled rows in DB must be cleared to enabled=True on boot."""
    db_file = tmp_path / "test_legacy.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add(
            EngineRow(
                id="ddg",
                name="DuckDuckGo",
                category="general",
                adapter="app.retrieval.adapters.ddg:DuckDuckGoAdapter",
                weight=1.0,
                enabled=False,
                timeout_ms=4000,
                max_results=15,
                requires_key=False,
                disabled_reason="auto: 3 consecutive failures",
            )
        )
        await session.commit()

    cleared = await clear_legacy_auto_disabled(session_factory)
    assert cleared == 1

    async with session_factory() as session:
        row = (await session.execute(select(EngineRow).where(EngineRow.id == "ddg"))).scalar_one()
        assert row.enabled is True
        assert row.disabled_reason is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_search_hot_path_issues_no_sql(tmp_path) -> None:
    """Search execution must issue 0 SQL queries on the hot path."""
    db_file = tmp_path / "test_hot_path.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    # Seed an enabled engine
    async with session_factory() as session:
        session.add(
            EngineRow(
                id="ddg",
                name="DuckDuckGo",
                category="general",
                adapter="app.retrieval.adapters.ddg:DuckDuckGoAdapter",
                weight=1.0,
                enabled=True,
                timeout_ms=4000,
                max_results=15,
                requires_key=False,
            )
        )
        await session.commit()

    settings = Settings(_env_file=None, database_url=db_url)
    store = DbEngineStore(settings, session_factory=session_factory, refresh_s=60.0)
    # Warm up cache so initial seed/load is done
    await store.snapshot()

    adapter = DummyAdapter("ddg")
    registry = EngineRegistry(store, {"ddg": adapter})
    pipe = SearchPipeline(settings, registry, cache=None)

    sql_statements = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        sql_statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        out = await pipe.run(SearchParams(query="zero sql query"))
        assert len(out.results) == 1
        assert len(sql_statements) == 0, f"Expected 0 SQL statements, got: {sql_statements}"
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
        await engine.dispose()


@pytest.mark.asyncio
async def test_black_hole_db_costs_once_per_30s() -> None:
    """When DB is dead, snapshot must back off 30s and serve stale/fallback without waiting."""
    settings = Settings(_env_file=None, engine_store_db_timeout_s=0.05)
    mock_load = AsyncMock(side_effect=RuntimeError("DB dead"))

    store = DbEngineStore(settings, refresh_s=0.1)
    store._load_rows = mock_load  # type: ignore

    snap1 = await store.snapshot()
    assert "searxng" in snap1
    assert mock_load.call_count == 1

    # Next 10 calls during the 30s backoff must NOT call DB at all
    t1 = time.perf_counter()
    for _ in range(10):
        await store.snapshot()
    dur_next = time.perf_counter() - t1

    assert mock_load.call_count == 1
    assert dur_next < 0.05
