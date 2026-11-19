"""Regression: /v1/meta/full must never answer 200 without a real admin key."""

from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.core.auth import create_key, reset_rate_limit_windows
from app.db.bootstrap import ensure_schema
from app.db.session import dispose_engine
from app.main import create_app


def _hdr(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(params=[("false", "true"), ("true", "false"), ("false", "false")])
async def meta_env(request, tmp_path, monkeypatch):
    auth_enabled, anon_admin = request.param
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'meta.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("AUTH_ENABLED", auth_enabled)
    monkeypatch.setenv("DEV_ANON_ADMIN", anon_admin)
    monkeypatch.setenv("ENGINE_STORE_BACKEND", "memory")
    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    get_settings.cache_clear()
    await dispose_engine()
    await ensure_schema()
    reset_rate_limit_windows()
    yield request.param
    await dispose_engine()
    get_settings.cache_clear()


async def test_meta_full_requires_real_admin_key(meta_env):
    user_key, _ = await create_key("meta_user", role="user", rate_limit_rpm=6000)
    admin_key, _ = await create_key("meta_admin", role="admin", rate_limit_rpm=6000)
    transport = httpx.ASGITransport(
        app=create_app(), raise_app_exceptions=False, client=("127.0.0.1", 7777)
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/v1/meta/full")).status_code == 401
        assert (
            await client.get("/v1/meta/full", headers={"Authorization": "Bearer msk_bogus"})
        ).status_code == 401
        assert (await client.get("/v1/meta/full", headers=_hdr(user_key))).status_code == 403
        ok = await client.get("/v1/meta/full", headers=_hdr(admin_key))
        assert ok.status_code == 200
        assert "giants" in ok.json()
        # Public metadata stays open for the Lite profile.
        if meta_env[0] == "false":
            assert (await client.get("/v1/meta")).status_code == 200
