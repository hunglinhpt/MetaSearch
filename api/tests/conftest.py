"""Shared test fixtures.

Environment variables are set *before* ``app`` is imported because
``get_settings()`` is ``lru_cache``d.
"""

from __future__ import annotations

import os
import socket

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("SEARXNG_URL", "http://localhost:8080")
os.environ.setdefault("LLM_PROVIDER", "none")

import pytest
from fastapi.testclient import TestClient

from app.api.deps import reset_pipelines
from app.config import get_settings
from app.main import create_app

_PUBLIC_TEST_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Resolve every hostname to a public IP so SSRF/scrape tests stay offline.

    Individual tests may re-patch ``socket.getaddrinfo`` to simulate private
    DNS responses; their patch wins because it runs after this fixture.
    """

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_TEST_IP, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture(autouse=True)
def _reset_pipelines():
    reset_pipelines()
    yield
    reset_pipelines()


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as test_client:
        yield test_client
