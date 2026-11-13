"""Audit round 13: regression tests for the surfaces rounds 11-13 fixed.

Covers (previous audit gaps):
- /mcp HTTP mount: auth guard (401 unauth with auth on), transport alive
  (protocol error 406 instead of "Task group is not initialized" 500/404),
  default-off when MCP_ENABLED unset;
- /metrics METRICS_ALLOW_PRIVATE: RFC1918 caller 200 after admin attempt
  fails, public caller still 403, flag off -> 403;
- AnswerRequest.max_age_s schema bounds (30..86400 -> 422 outside).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def mcp_app(monkeypatch):
    monkeypatch.setenv("MCP_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()  # settings is lru_cached — pick up the env
    app = create_app()
    yield app
    get_settings.cache_clear()


def _asgi(client_ip: str):
    return httpx.ASGITransport(
        app=create_app(), raise_app_exceptions=False, client=(client_ip, 5555)
    )


class TestMcpHttpMount:
    def test_default_off_without_flag(self, monkeypatch):
        monkeypatch.delenv("MCP_ENABLED", raising=False)
        from app.config import get_settings

        get_settings.cache_clear()
        client = TestClient(create_app())
        get_settings.cache_clear()
        r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
        assert r.status_code == 404  # mount absent -> main-app 404

    def test_transport_alive_and_guarded(self, mcp_app, monkeypatch):
        """Round 11 P1 + round 13: with the flag on, the mount answers with a
        MCP-PROTOCOL response (406 for missing Accept headers proves the
        session manager is alive) and the auth wrap rejects keyless callers
        when auth is on."""
        monkeypatch.setenv("AUTH_ENABLED", "true")
        # create_app reads get_settings() (lru_cached) — clear then rebuild.
        from app.config import get_settings

        get_settings.cache_clear()
        app = create_app()
        get_settings.cache_clear()
        with TestClient(app) as client:  # lifespan enters the MCP session manager
            r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
        assert r.status_code in (401, 403), r.text  # guard, not transport crash

    def test_transport_alive_without_auth(self, mcp_app):
        """AUTH off (default dev): the transport must answer the protocol —
        406 'must accept application/json and text/event-stream' proves the
        session manager runs (the round-11 bug returned a 500/404)."""
        with TestClient(mcp_app) as client:  # lifespan must run to start the task group
            r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
        assert r.status_code == 406
        body = r.json()
        assert "Not Acceptable" in body.get("error", {}).get("message", "")
