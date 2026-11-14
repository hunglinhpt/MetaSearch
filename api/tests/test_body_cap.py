"""Audit Phase 4 P1: inbound request-body size cap.

A 100MB JSON body is fully buffered and parsed before pydantic rejects it
field-by-field — a cheap DoS against every /v1 POST route. The cap lives in
RequestContextMiddleware (runs first for every request) and only applies to
JSON body routes; GET/HEAD and non-JSON content types pass through.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def _post_body(client: Any, size: int, *, auth: str | None = None):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    return client.post(
        "/v1/search",
        content=b"{" + b'"x":"' + b"A" * size + b'"}',
        headers=headers,
    )


def _client():
    from fastapi.testclient import TestClient

    return TestClient(create_app())


@pytest.fixture()
def cap_on(monkeypatch):
    monkeypatch.setenv("MAX_BODY_BYTES", "1024")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        yield TestClient(create_app())
    finally:
        get_settings.cache_clear()


def test_oversized_json_body_rejected_with_413(cap_on):
    r = cap_on.post(
        "/v1/search",
        content=b"{" + b'"query":"' + b"A" * 4096 + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_normal_body_passes_the_cap(cap_on):
    r = cap_on.post(
        "/v1/search",
        json={"query": "pháp y BCTC VCB"},
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code != 413  # 400/422/503 — but NOT blocked by the cap


def test_small_body_untouched(cap_on):
    r = cap_on.post(
        "/v1/search",
        json={"query": "x"},
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code != 413
