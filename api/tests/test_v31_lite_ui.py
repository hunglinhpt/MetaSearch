"""v3.1.0 tests: Lite Web UI static mount (serve_ui / ui_static_dir) served by
the API process itself, and the graph/build background-job contract bits that
live outside test_v30_wave_p2."""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings


@pytest.fixture
async def v31_db(tmp_path, monkeypatch):
    db_file = tmp_path / "v31.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    monkeypatch.setenv("DEV_ANON_ADMIN", "false")
    get_settings.cache_clear()
    from app.db.bootstrap import ensure_schema
    from app.db.session import dispose_engine

    await dispose_engine()
    await ensure_schema()
    from app.core.auth import create_key, reset_rate_limit_windows

    reset_rate_limit_windows()
    key, _ = await create_key("v31_user", role="user", rate_limit_rpm=6000)
    yield key
    get_settings.cache_clear()


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class TestLiteUiMount:
    @pytest.mark.asyncio
    async def test_ui_served_on_same_port_when_out_exists(self, v31_db, tmp_path, monkeypatch):
        from app.main import create_app

        ui_dir = tmp_path / "out"
        ui_dir.mkdir()
        (ui_dir / "index.html").write_text(
            "<html><body>metasearch-lite-ui-marker</body></html>", encoding="utf-8"
        )
        monkeypatch.setenv("SERVE_UI", "true")
        monkeypatch.setenv("UI_STATIC_DIR", str(ui_dir))
        get_settings.cache_clear()
        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # UI is public (page must load before the key is entered)...
            r = await c.get("/")
            assert r.status_code == 200
            assert "metasearch-lite-ui-marker" in r.text
            assert "text/html" in r.headers["content-type"]
            # ...but the API stays behind auth on the same port.
            r = await c.get("/v1/ops/status")
            assert r.status_code == 401
            r = await c.get("/v1/ops/status", headers=_h(v31_db))
            assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_ui_not_mounted_when_dir_missing(self, v31_db, tmp_path, monkeypatch):
        from app.main import create_app

        monkeypatch.setenv("SERVE_UI", "true")
        monkeypatch.setenv("UI_STATIC_DIR", str(tmp_path / "nope"))
        get_settings.cache_clear()
        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/")
            assert r.status_code == 404  # API-only mode

    @pytest.mark.asyncio
    async def test_ui_disabled_by_flag(self, v31_db, tmp_path, monkeypatch):
        from app.main import create_app

        ui_dir = tmp_path / "out"
        ui_dir.mkdir()
        (ui_dir / "index.html").write_text("<html></html>", encoding="utf-8")
        monkeypatch.setenv("SERVE_UI", "false")
        monkeypatch.setenv("UI_STATIC_DIR", str(ui_dir))
        get_settings.cache_clear()
        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/")
            assert r.status_code == 404
