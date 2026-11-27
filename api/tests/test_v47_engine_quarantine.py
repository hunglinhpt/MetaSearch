"""docs/36 S3: rate-based engine quarantine (persisted history, survives restart)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import deps
from app.config import Settings
from app.db.models import Base
from app.db.models import Engine as EngineRow
from app.db.models import EngineHealth as EngineHealthRow
from app.main import create_app
from app.orchestration.engine_quarantine import (
    QUARANTINE_PREFIX,
    EngineQuarantine,
    EngineRates,
    is_quarantined,
    probe_engine,
    quarantine_reason,
    rates_from_rows,
    release_marker,
    released_at,
)
from app.orchestration.engine_store import DbEngineStore, InMemoryEngineStore

NOW = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)
S = Settings(_env_file=None)


def _rows(engine: str, ok: int, fail: int, *, hours_ago: float) -> list[tuple]:
    ws = NOW - timedelta(hours=hours_ago)
    return [(engine, ws, ok + fail, ok)]


# --------------------------------------------------------------------------------------
# Pure rule
# --------------------------------------------------------------------------------------


def test_24h_evidence_quarantines() -> None:
    reason = quarantine_reason(EngineRates(calls_24h=29, successes_24h=0, calls_7d=29), S)
    assert reason == f"{QUARANTINE_PREFIX}success_24h=0.00 (0/29)"


def test_low_traffic_uses_7d_evidence_with_fresh_24h() -> None:
    r = EngineRates(calls_24h=4, successes_24h=0, calls_7d=37, successes_7d=1)
    reason = quarantine_reason(r, S)
    assert reason is not None and "success_7d=0.03 (1/37)" in reason and "(0/4)" in reason


@pytest.mark.parametrize(
    "rates",
    [
        EngineRates(calls_24h=5, successes_24h=0, calls_7d=5, successes_7d=0),  # too few calls
        EngineRates(calls_24h=40, successes_24h=38, calls_7d=40, successes_7d=38),  # healthy
        EngineRates(calls_24h=0, successes_24h=0, calls_7d=30, successes_7d=0),  # stale: no fresh
        EngineRates(calls_24h=3, successes_24h=3, calls_7d=30, successes_7d=5),  # recovered
    ],
)
def test_no_quarantine(rates: EngineRates) -> None:
    assert quarantine_reason(rates, S) is None


def test_rates_from_rows_windows_and_ignore_before() -> None:
    rows = (
        _rows("ddg", 0, 4, hours_ago=1)
        + _rows("ddg", 0, 10, hours_ago=12)
        + _rows("ddg", 1, 20, hours_ago=72)
        + _rows("ddg", 5, 0, hours_ago=24 * 9)  # outside 7d
    )
    r = rates_from_rows(rows, now=NOW)["ddg"]
    assert (r.calls_6h, r.calls_24h, r.calls_7d) == (4, 14, 35)
    assert r.successes_7d == 1
    cut = rates_from_rows(rows, now=NOW, ignore_before={"ddg": NOW - timedelta(hours=2)})["ddg"]
    assert (cut.calls_24h, cut.calls_7d) == (4, 4)
    naive = [("x", (NOW - timedelta(hours=1)).replace(tzinfo=None), 2, 1)]
    assert rates_from_rows(naive, now=NOW)["x"].calls_6h == 2


def test_is_quarantined() -> None:
    assert is_quarantined("quarantine:success_24h=0.00 (0/9)")
    assert not is_quarantined("auto:legacy") and not is_quarantined(None)


# --------------------------------------------------------------------------------------
# Evaluator with fakes
# --------------------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, configs: dict[str, Any], rates: dict[str, EngineRates]):
        self.configs = configs
        self.rates = rates
        self.history_calls: list[dict] = []

    async def snapshot(self):
        return dict(self.configs)

    async def history_rates(self, *, now=None, ignore_before=None):
        self.history_calls.append(dict(ignore_before or {}))
        return self.rates

    async def quarantine(self, eid: str, reason: str) -> bool:
        self.configs[eid] = replace(self.configs[eid], enabled=False, disabled_reason=reason)
        return True

    async def release(self, eid: str) -> bool:
        self.configs[eid] = replace(self.configs[eid], enabled=True, disabled_reason=None)
        return True


class _Adapter:
    def __init__(self, fail: bool):
        self.fail = fail
        self.calls = 0

    async def search(self, query, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("bot-blocked")
        return []  # empty is fine: no adapter error


def _configs() -> dict[str, Any]:
    base = InMemoryEngineStore.from_settings(S)
    cfgs = {k: v for k, v in base._configs.items() if k in ("ddg", "google_news", "tavily")}
    return cfgs


async def test_evaluator_quarantines_probes_and_releases() -> None:
    cfgs = _configs()
    assert cfgs["tavily"].enabled is False  # key-gated: operator/config disabled
    bad = EngineRates(calls_24h=29, successes_24h=0, calls_7d=29)
    good = EngineRates(calls_24h=50, successes_24h=49, calls_7d=50, successes_7d=49)
    store = _FakeStore(cfgs, {"ddg": bad, "google_news": good, "tavily": bad})
    settings = replace_settings(engine_quarantine_probe_s=60)
    adapter = _Adapter(fail=True)
    q = EngineQuarantine(store, {"ddg": adapter}, settings)

    out = await q.run_once(now=NOW)
    assert out == {"quarantined": ["ddg"], "released": []}
    assert is_quarantined(store.configs["ddg"].disabled_reason)
    assert store.configs["tavily"].disabled_reason is None  # disabled engines are not judged

    # Probe interval not elapsed -> no probe.
    await q.run_once(now=NOW)
    assert adapter.calls == 0

    q._last_probe["ddg"] -= 3600  # pretend the interval elapsed; probe fails
    await q.run_once(now=NOW)
    assert adapter.calls == 1 and store.configs["ddg"].enabled is False

    adapter.fail = False
    q._last_probe["ddg"] -= 3600
    out = await q.run_once(now=NOW)
    assert out["released"] == ["ddg"]
    assert store.configs["ddg"].enabled is True
    # After release only newer calls count (old failures are ignored).
    store.rates = {"ddg": bad}
    await q.run_once(now=NOW)
    assert store.history_calls[-1]["ddg"] == NOW


async def test_operator_reenable_is_respected() -> None:
    cfgs = _configs()
    bad = EngineRates(calls_24h=29, successes_24h=0, calls_7d=29)
    store = _FakeStore(cfgs, {"ddg": bad})
    q = EngineQuarantine(store, {}, S)
    await q.run_once(now=NOW)
    assert store.configs["ddg"].enabled is False
    # Operator PATCH re-enables; the next tick records the release time so the
    # old failures cannot immediately re-quarantine.
    store.configs["ddg"] = replace(store.configs["ddg"], enabled=True, disabled_reason=None)
    later = NOW + timedelta(minutes=10)
    store.rates = {}  # nothing newer than the re-enable yet
    out = await q.run_once(now=later)
    assert out["quarantined"] == [] and store.history_calls[-1]["ddg"] == later


async def test_in_memory_store_is_a_noop() -> None:
    store = InMemoryEngineStore.from_settings(S)
    out = await EngineQuarantine(store, {}, S).run_once(now=NOW)
    assert out == {"quarantined": [], "released": []}


async def test_probe_engine() -> None:
    assert await probe_engine(_Adapter(fail=False)) is True
    assert await probe_engine(_Adapter(fail=True)) is False
    assert await probe_engine(None) is False


def replace_settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# --------------------------------------------------------------------------------------
# DbEngineStore on SQLite: history, quarantine survives the start-up clear
# --------------------------------------------------------------------------------------


async def test_db_store_history_quarantine_survives_restart(tmp_path) -> None:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'q.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(_env_file=None, database_url=db_url)
    store = DbEngineStore(settings, session_factory=sm, refresh_s=0)
    await store.snapshot()  # seeds engine rows

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    async with sm() as session:
        # Same statement shape as EngineHealthWriter.flush (core insert, aware datetimes).
        for i in range(12):
            await session.execute(
                insert(EngineHealthRow).values(
                    engine_id="ddg",
                    window_start=now - timedelta(minutes=5 * i),
                    calls=1,
                    successes=0,
                    failures=1,
                )
            )
        await session.commit()

    rates = await store.history_rates(now=now + timedelta(minutes=1))
    assert rates["ddg"].calls_24h == 12 and rates["ddg"].successes_24h == 0

    assert await store.quarantine("ddg", "quarantine:test") is True
    snap = await store.snapshot()
    assert snap["ddg"].enabled is False and snap["ddg"].disabled_reason == "quarantine:test"

    # A new process seeds rows again: legacy auto: states are cleared, quarantine is not.
    store2 = DbEngineStore(settings, session_factory=sm, refresh_s=0)
    snap2 = await store2.snapshot()
    assert snap2["ddg"].enabled is False and is_quarantined(snap2["ddg"].disabled_reason)

    assert await store2.release("ddg") is True
    async with sm() as session:
        row = (await session.execute(select(EngineRow).where(EngineRow.id == "ddg"))).scalar_one()
        assert row.enabled is True and released_at(row.disabled_reason) is not None
        assert not is_quarantined(row.disabled_reason)

    # Audit A4: a restart after the release must not re-judge the pre-release
    # failures (the in-memory release mark is gone; the persisted one is not).
    store3 = DbEngineStore(settings, session_factory=sm, refresh_s=0)
    evaluator = EngineQuarantine(store3, {"ddg": _Adapter(fail=True)}, settings)
    out = await evaluator.run_once(now=datetime.now(UTC) + timedelta(minutes=2))
    assert "ddg" not in out["quarantined"]
    assert (await store3.snapshot())["ddg"].enabled is True
    await engine.dispose()


async def test_patch_enabled_on_enabled_engine_keeps_evidence(tmp_path) -> None:
    """Audit A6: a weight edit that resends "enabled": true is not a release."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'a6.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(_env_file=None, database_url=db_url)
    store = DbEngineStore(settings, session_factory=sm, refresh_s=0)
    assert (await store.snapshot())["wikipedia"].enabled is True

    await store.update("wikipedia", {"enabled": True, "weight": 0.7})
    snap = await store.snapshot()
    assert snap["wikipedia"].weight == 0.7 and snap["wikipedia"].disabled_reason is None

    # A real re-enable of a disabled engine still records the release time.
    await store.update("wikipedia", {"enabled": False})
    await store.update("wikipedia", {"enabled": True})
    assert released_at((await store.snapshot())["wikipedia"].disabled_reason) is not None
    await engine.dispose()


def test_release_marker_roundtrip() -> None:
    mark = release_marker(NOW)
    assert mark.startswith("released:") and released_at(mark) == NOW
    assert released_at("quarantine:x") is None and released_at("released:garbage") is None


async def test_history_rates_db_down_returns_empty() -> None:
    def broken_sm():
        raise RuntimeError("db down")

    store = DbEngineStore(S, session_factory=broken_sm, refresh_s=0)  # type: ignore[arg-type]
    assert await store.history_rates(now=NOW, timeout_s=0.5) == {}


# --------------------------------------------------------------------------------------
# /v1/engines/health exposes 24h/7d + quarantined
# --------------------------------------------------------------------------------------


def test_engines_health_reports_history(monkeypatch) -> None:
    store = InMemoryEngineStore.from_settings(S)

    async def history_rates(**kwargs):
        return {"ddg": EngineRates(calls_24h=10, successes_24h=1, calls_7d=40, successes_7d=4)}

    store.history_rates = history_rates  # type: ignore[attr-defined]
    store._configs["ddg"] = replace(
        store._configs["ddg"], enabled=False, disabled_reason="quarantine:x"
    )
    monkeypatch.setattr(deps, "get_engine_store", lambda: store)
    body = TestClient(create_app()).get("/v1/engines/health").json()
    ddg = next(e for e in body if e["engine"] == "ddg")
    assert ddg["quarantined"] is True
    assert ddg["calls_24h"] == 10 and ddg["success_rate_24h"] == 0.1
    assert ddg["calls_7d"] == 40 and ddg["success_rate_7d"] == 0.1
    other = next(e for e in body if e["engine"] == "wikipedia")
    assert other["calls_24h"] is None and other["quarantined"] is False
