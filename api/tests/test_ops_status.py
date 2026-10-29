"""Operations status + query history endpoints (management surfaces)."""

from __future__ import annotations

import pytest

import app.api.deps as deps
from app.orchestration.engine_store import InMemoryEngineStore
from app.retrieval.models import EngineConfig
from tests.test_api_v1 import FakeAdapter


@pytest.fixture
def engine_stack(monkeypatch):
    """Patch only the engine store + adapters (no scraper/gateway needed)."""

    def _install(adapters: list[FakeAdapter]) -> None:
        configs = {
            a.id: EngineConfig(id=a.id, name=a.id, category="general", enabled=True)
            for a in adapters
        }
        store = InMemoryEngineStore(configs)
        monkeypatch.setattr(deps, "get_engine_store", lambda: store)
        monkeypatch.setattr(deps, "build_adapters", lambda settings: {a.id: a for a in adapters})
        deps.reset_pipelines()

    return _install


class TestOpsStatus:
    def test_status_always_200_with_structure(self, client, engine_stack):
        engine_stack([FakeAdapter("searxng", [])])
        resp = client.get("/v1/ops/status")
        assert resp.status_code == 200  # a dead dependency is data, not an error
        data = resp.json()
        assert data["ready"] is False  # DB/Redis are down in the test rig
        assert set(data["dependencies"]) >= {"postgres", "redis", "searxng"}
        assert "postgres" in data["failing"]
        engines = {e["id"]: e for e in data["engines"]}
        assert engines["searxng"]["adapter_ready"] is True
        assert engines["searxng"]["enabled"] is True
        assert data["version"]

    def test_status_engine_disabled_reason(self, client, engine_stack):
        engine_stack([FakeAdapter("searxng", [])])
        client.patch("/v1/engines/searxng", json={"enabled": False})
        data = client.get("/v1/ops/status").json()
        engines = {e["id"]: e for e in data["engines"]}
        assert engines["searxng"]["enabled"] is False

    def test_status_adapter_missing_flagged(self, client):
        # Default store: serper/brave have no adapter implementation yet.
        resp = client.get("/v1/ops/status")
        assert resp.status_code == 200
        engines = {e["id"]: e for e in resp.json()["engines"]}
        assert engines["serper"]["adapter_ready"] is False


class TestQueriesList:
    def test_db_down_degrades_gracefully(self, client):
        resp = client.get("/v1/queries")
        assert resp.status_code == 200
        data = resp.json()
        assert data["db"] is False
        assert data["queries"] == []
        assert data["total"] == 0

    def test_limit_bounds(self, client):
        assert client.get("/v1/queries?limit=500").status_code == 200
        assert client.get("/v1/queries?offset=-5").status_code == 200
