"""Wave 3 package C: readiness that means something, and Lite run as a Windows service."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from app.api import deps, ops
from app.config import get_settings
from app.db.bootstrap import ensure_schema
from app.db.session import dispose_engine
from app.main import app

REPO = Path(__file__).resolve().parents[2]
# The real engine store factory, captured before other test modules swap it for fakes.
_REAL_ENGINE_STORE = deps.get_engine_store


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'c.db').as_posix()}")
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    monkeypatch.setenv("DEV_ANON_ADMIN", "true")
    get_settings.cache_clear()
    await dispose_engine()
    await ensure_schema()
    deps.reset_pipelines()
    saved_store = deps.get_engine_store
    deps.get_engine_store = _REAL_ENGINE_STORE
    if hasattr(deps.get_engine_store, "cache_clear"):
        deps.get_engine_store.cache_clear()
    ops.reset_readiness_state()
    yield
    ops.reset_readiness_state()
    await ops.close_readiness_clients()
    deps.get_engine_store = saved_store
    await dispose_engine()
    get_settings.cache_clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=20
    )


async def _engine_ids(c) -> list[str]:
    data = (await c.get("/v1/engines")).json()
    return [e["id"] for e in (data if isinstance(data, list) else data.get("engines", []))]


async def test_readyz_not_ready_is_503():
    async with _client() as c:
        ids = await _engine_ids(c)
        assert ids
        for e in ids:
            await c.patch(f"/v1/engines/{e}", json={"enabled": False})
        r = await c.get("/readyz")
        assert r.status_code == 503 and r.json()["status"] == "not_ready"
        for e in ids:
            await c.patch(f"/v1/engines/{e}", json={"enabled": True})
        r = await c.get("/readyz")
        assert r.status_code == 200 and r.json()["status"] in ("ready", "degraded")


async def test_readyz_shutting_down():
    ops.mark_shutting_down()
    async with _client() as c:
        r = await c.get("/readyz")
    assert r.status_code == 503 and r.json()["status"] == "shutting_down"


async def test_lifespan_marks_shutdown():
    async with app.router.lifespan_context(app):
        assert ops._shutting_down is False
    assert ops._shutting_down is True


def _alembic_head() -> str:
    heads, downs = set(), set()
    for f in (REPO / "api" / "alembic" / "versions").glob("*.py"):
        text = f.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("revision"):
                heads.add(line.split("=")[1].strip().strip("\"'"))
            if line.startswith("down_revision"):
                value = line.split("=")[1].strip().strip("\"'")
                if value != "None":
                    downs.add(value)
    (head,) = heads - downs
    return head


async def test_readyz_schema_version_from_alembic():
    async with _client() as c:
        body = (await c.get("/readyz")).json()
    assert body["schema_version"] == _alembic_head()


async def test_readyz_probe_timeout(monkeypatch):
    async def hang():
        await asyncio.sleep(30)
        return {"ok": True}

    monkeypatch.setattr(ops, "_check_postgres", hang)
    monkeypatch.setenv("READINESS_TIMEOUT_S", "0.5")
    get_settings.cache_clear()
    start = time.perf_counter()
    async with _client() as c:
        r = await c.get("/readyz")
    took = time.perf_counter() - start
    assert took < 3.0
    body = r.json()
    assert body["checks"]["postgres"] == {"ok": False, "error": "timeout"}
    assert body["status"] == "degraded" and r.status_code == 200


async def test_readyz_reuses_one_redis_client(monkeypatch):
    made = []
    real = ops.Redis.from_url

    def counting(*a, **k):
        made.append(1)
        return real(*a, **k)

    monkeypatch.setattr(ops.Redis, "from_url", counting)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:9/0")
    monkeypatch.setenv("READINESS_TIMEOUT_S", "0.3")
    get_settings.cache_clear()
    async with _client() as c:
        for _ in range(3):
            await c.get("/readyz")
    assert len(made) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell parser")
def test_windows_scripts_parse_and_are_ascii():
    scripts = sorted((REPO / "scripts" / "windows").glob("*.ps1"))
    assert {p.name for p in scripts} >= {
        "metasearch-lite.ps1",
        "install-task.ps1",
        "measure-lite.ps1",
    }
    for p in scripts:
        assert all(b < 128 for b in p.read_bytes()), p.name
        parser = "[System.Management.Automation.Language.Parser]"
        cmd = f"$e=$null; [void]{parser}::ParseFile('{p}',[ref]$null,[ref]$e); $e.Count"
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True
        ).stdout.strip()
        assert out == "0", (p.name, out)


def test_windows_service_script_contract():
    manager = (REPO / "scripts" / "windows" / "metasearch-lite.ps1").read_text(encoding="ascii")
    task = (REPO / "scripts" / "windows" / "install-task.ps1").read_text(encoding="ascii")
    assert '"check"' in manager and "/readyz" in manager, "self-check after wake"
    assert "Test-PortBusy" in manager, "never takes over a port another process holds"
    assert "Invoke-LogRotation" in manager
    assert "Register-ScheduledTask" in task and "Power-Troubleshooter" in task
    assert "$pid =" not in manager.lower()


def test_compose_migrates_first_and_shuts_down_gracefully():
    compose = yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["migrate"]["command"] == ["alembic", "upgrade", "head"]
    assert services["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "--timeout-graceful-shutdown" in services["api"]["command"]
    assert "readyz" in str(services["api"]["healthcheck"]["test"])


def test_cli_max_concurrent_help():
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "keys", "create", "--help"],
        cwd=REPO / "api",
        capture_output=True,
        text=True,
    ).stdout
    assert "every route" in out and "research jobs" not in out


async def test_redis_probe_has_a_read_timeout(monkeypatch):
    seen = {}
    real = ops.Redis.from_url

    def capture(url, **kwargs):
        seen.update(kwargs)
        return real(url, **kwargs)

    monkeypatch.setattr(ops.Redis, "from_url", capture)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:9/0")
    monkeypatch.setenv("READINESS_TIMEOUT_S", "0.4")
    get_settings.cache_clear()
    async with _client() as c:
        await c.get("/readyz")
    # Below the outer 0.4 s limit, so the probe's own Lite branch decides, not the outer timeout.
    assert 0 < seen.get("socket_timeout", 0) < 0.4, "a silent Redis must not hold the probe"
    assert 0 < seen.get("socket_connect_timeout", 0) < 0.4


async def test_shutdown_closes_the_redis_probe(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:9/0")
    monkeypatch.setenv("READINESS_TIMEOUT_S", "0.3")
    get_settings.cache_clear()
    async with app.router.lifespan_context(app):
        async with _client() as c:
            await c.get("/readyz")
        assert ops._redis_probe is not None
    assert ops._redis_probe is None


async def test_lite_without_redis_is_ready(monkeypatch):
    # The Lite example leaves REDIS_URL empty: that is a supported setup, not a fault.
    made = []
    monkeypatch.setattr(ops.Redis, "from_url", lambda *a, **k: made.append(1))
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    async with _client() as c:
        r = await c.get("/readyz")
    body = r.json()
    assert body["checks"]["redis"] == {"ok": True, "skipped": "redis_not_configured"}
    assert made == []
    assert r.status_code == 200 and body["status"] == "ready", body


async def test_lite_with_a_silent_redis_is_ready(monkeypatch):
    # A Redis that accepts and never answers: the probe's own timeout must fire first,
    # so Lite reports Redis as optional instead of the outer probe reporting "timeout".
    held = []

    async def silent(reader, writer):
        held.append(writer)
        await asyncio.sleep(30)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{port}/0")
    monkeypatch.setenv("READINESS_TIMEOUT_S", "0.5")
    get_settings.cache_clear()
    try:
        async with _client() as c:
            body = (await c.get("/readyz")).json()
    finally:
        for w in held:
            w.close()
        server.close()
    assert body["checks"]["redis"]["ok"] is True, body
    assert body["checks"]["redis"]["skipped"] == "optional_in_lite_mode"
