"""Integration tests against the docker-compose stack (docs/12-roadmap-plan.md).

Gated: they run only when the compose stack is up AND
``METASEARCH_INTEGRATION=1`` is set, e.g.::

    cd deploy && docker compose up -d
    alembic upgrade head
    METASEARCH_INTEGRATION=1 METASEARCH_URL=http://localhost:8000 pytest tests/integration -q
"""

from __future__ import annotations

import os

import httpx
import pytest

INTEGRATION = os.environ.get("METASEARCH_INTEGRATION") == "1"
BASE_URL = os.environ.get("METASEARCH_URL", "http://localhost:8000")

pytestmark = pytest.mark.skipif(
    not INTEGRATION, reason="set METASEARCH_INTEGRATION=1 with the compose stack running"
)


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    key = os.environ.get("METASEARCH_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    with httpx.Client(base_url=BASE_URL, timeout=30, headers=headers) as c:
        yield c


def test_readyz_all_green(client: httpx.Client):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    checks = resp.json()["checks"]
    assert checks["postgres"]["ok"] is True
    assert checks["redis"]["ok"] is True
    assert checks["searxng"]["ok"] is True


def test_searxng_json_format_enabled(client: httpx.Client):
    # The #1 compose pitfall: missing `formats: [html, json]` in settings.yml.
    searxng = os.environ.get("SEARXNG_URL", "http://localhost:8080")
    resp = httpx.get(f"{searxng}/search", params={"q": "test", "format": "json"}, timeout=10)
    assert resp.status_code == 200
    assert isinstance(resp.json().get("results"), list)


def test_search_returns_results(client: httpx.Client):
    resp = client.post("/v1/search", json={"query": "postgresql replication", "k": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"], "expected real results from the compose stack"
    assert data["engines_used"]
    assert data["results"][0]["url"].startswith("http")


def test_search_cache_second_hit(client: httpx.Client):
    body = {"query": "cache integration probe query", "k": 3}
    first = client.post("/v1/search", json=body)
    assert first.status_code == 200
    second = client.post("/v1/search", json=body)
    assert second.status_code == 200
    assert second.json()["cached"] is True


def test_answer_stream_events(client: httpx.Client):
    resp = client.post(
        "/v1/answer",
        json={"query": "what is postgresql", "stream": True},
        headers={"Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    events = {
        line.split(":", 1)[1].strip()
        for line in resp.text.splitlines()
        if line.startswith("event:")
    }
    # The search half needs no LLM and must always stream.
    assert {"status", "sources"} <= events, events
    if "token" not in events:
        # No usable LLM in this stack (no key, or the keyless provider is down): the stream
        # must say so with an error event instead of ending silently.
        assert "error" in events, events
        pytest.skip("no usable LLM in this compose stack; search half verified")
    assert "done" in events, events


def test_engine_hot_config_roundtrip(client: httpx.Client):
    patch = client.patch("/v1/engines/searxng", json={"weight": 2.0})
    assert patch.status_code == 200
    assert patch.json()["weight"] == 2.0
    restore = client.patch("/v1/engines/searxng", json={"weight": 1.0})
    assert restore.json()["weight"] == 1.0


def test_scrape_endpoint_real_page(client: httpx.Client):
    resp = client.post("/v1/scrape", json={"urls": ["https://example.com/"]})
    assert resp.status_code == 200
    items = resp.json()
    assert items[0]["status"] == "ok"
    assert items[0]["extractor_used"] in {"trafilatura", "crawl4ai", "jina", "cache"}
