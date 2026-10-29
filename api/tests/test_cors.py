"""CORS middleware tests — the Web UI (browser) calls the API cross-origin."""

from __future__ import annotations


def test_preflight_options_allowed(client):
    resp = client.options(
        "/v1/search",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "POST" in resp.headers["access-control-allow-methods"]


def test_actual_response_carries_cors_header(client):
    resp = client.get("/healthz", headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_unknown_origin_not_allowed(client):
    resp = client.get("/healthz", headers={"Origin": "http://evil.example.com"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_request_id_header_exposed(client):
    resp = client.get("/healthz", headers={"Origin": "http://localhost:3000"})
    assert "x-request-id" in resp.headers["access-control-expose-headers"]
