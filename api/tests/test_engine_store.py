"""EngineStore tests: hot config, health, auto-disable, DB fallback."""

from __future__ import annotations

from app.config import get_settings
from app.orchestration.engine_store import (
    DbEngineStore,
    InMemoryEngineStore,
    default_configs,
)


def make_store() -> InMemoryEngineStore:
    return InMemoryEngineStore.from_settings(get_settings())


class TestInMemoryStore:
    async def test_defaults_seeded(self):
        store = make_store()
        snapshot = await store.snapshot()
        assert "searxng" in snapshot
        assert snapshot["searxng"].enabled is True
        assert snapshot["tavily"].requires_key is True
        assert snapshot["tavily"].enabled is False  # no key in test env

    async def test_update_hot_config(self):
        store = make_store()
        updated = await store.update("searxng", {"weight": 2.0, "timeout_ms": 1500})
        assert updated.weight == 2.0
        assert updated.timeout_ms == 1500
        assert (await store.snapshot())["searxng"].weight == 2.0

    async def test_update_unknown_returns_none(self):
        assert await make_store().update("ghost", {"weight": 1.0}) is None

    async def test_update_ignores_unknown_fields(self):
        updated = await make_store().update("searxng", {"name": "hacked", "weight": 3.0})
        assert updated.name != "hacked"
        assert updated.weight == 3.0

    async def test_auto_disable_after_threshold(self):
        store = make_store()
        for _ in range(get_settings().engine_auto_disable_threshold):
            await store.record_result("searxng", ok=False, latency_ms=10)
        snapshot = await store.snapshot()
        assert snapshot["searxng"].enabled is False
        assert "auto:" in snapshot["searxng"].disabled_reason
        assert "searxng" in await store.auto_disabled()

    async def test_success_resets_consecutive_failures(self):
        store = make_store()
        await store.record_result("searxng", ok=False, latency_ms=10)
        await store.record_result("searxng", ok=False, latency_ms=10)
        await store.record_result("searxng", ok=True, latency_ms=10)
        await store.record_result("searxng", ok=False, latency_ms=10)
        health = (await store.health())["searxng"]
        assert health.consecutive_failures == 1

    async def test_reenable_clears_reason(self):
        store = make_store()
        for _ in range(3):
            await store.record_result("searxng", ok=False, latency_ms=10)
        updated = await store.update("searxng", {"enabled": True})
        assert updated.enabled is True
        assert updated.disabled_reason is None
        assert await store.auto_disabled() == []

    async def test_health_window(self):
        store = make_store()
        await store.record_result("searxng", ok=True, latency_ms=5)
        await store.record_result("searxng", ok=False, latency_ms=5)
        health = (await store.health())["searxng"]
        assert health.calls == 2
        assert health.successes == 1
        assert health.failures == 1


class TestDbEngineStoreFallback:
    async def test_snapshot_falls_back_when_db_down(self):
        # Test env points Postgres at a dead localhost; the store must degrade
        # to the embedded in-memory defaults without raising.
        store = DbEngineStore(refresh_s=0)
        snapshot = await store.snapshot()
        assert "searxng" in snapshot

    async def test_update_falls_back_when_db_down(self):
        store = DbEngineStore(refresh_s=0)
        updated = await store.update("searxng", {"weight": 4.0})
        assert updated is not None
        assert updated.weight == 4.0
        assert (await store.snapshot())["searxng"].weight == 4.0

    async def test_record_result_falls_back_when_db_down(self):
        store = DbEngineStore(refresh_s=0)
        for _ in range(3):
            await store.record_result("searxng", ok=False, latency_ms=1)
        snapshot = await store.snapshot()
        assert snapshot["searxng"].enabled is False

    async def test_health_falls_back_when_db_down(self):
        store = DbEngineStore(refresh_s=0)
        await store.record_result("searxng", ok=True, latency_ms=1)
        health = await store.health()
        assert health["searxng"].calls == 1


class TestDefaults:
    def test_defaults_shape(self):
        configs = default_configs(get_settings())
        assert set(configs) == {"searxng", "tavily", "serper", "brave"}
        assert configs["searxng"].adapter_path.endswith("SearXNGAdapter")
        assert configs["tavily"].adapter_path.endswith("TavilyAdapter")

    def test_tavily_enabled_with_key(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        from app.config import Settings

        configs = default_configs(Settings(ENVIRONMENT="test", TAVILY_API_KEY="tvly-test"))
        assert configs["tavily"].enabled is True
