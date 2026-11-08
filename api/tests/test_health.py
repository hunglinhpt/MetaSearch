from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import ops


def test_healthz_is_always_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_request_id_header_is_echoed(client: TestClient) -> None:
    response = client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    response = client.get("/healthz")
    assert len(response.headers["x-request-id"]) == 32


@pytest.mark.parametrize("ok", [True, False])
def test_readyz_reflects_dependency_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, ok: bool
) -> None:
    async def fake_check() -> dict[str, Any]:
        return {"ok": ok}

    monkeypatch.setattr(ops, "_check_postgres", fake_check)
    monkeypatch.setattr(ops, "_check_redis", fake_check)
    monkeypatch.setattr(ops, "_check_searxng", fake_check)

    response = client.get("/readyz")
    # WP-14: dependencies down while engines are usable is degraded, still 200.
    body = response.json()
    assert response.status_code == 200
    assert body["ready"] is ok
    assert body["status"] == ("ready" if ok else "degraded")
    assert set(body["checks"]) == {"postgres", "redis", "searxng"}


def test_metrics_endpoint_exposes_prometheus_text(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "metasearch_http_requests_total" in response.text
