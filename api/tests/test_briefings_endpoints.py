"""docs/33 briefing endpoint coverage: POST success + persistence, honest 503
no-evidence, per-channel single-flight 429, and /latest tag discrimination."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

import app.api.deps as deps
from app.config import get_settings
from app.main import create_app
from tests.test_briefing_engine import (
    _CaptureAdapter,
    _Registry,
    _result,
    _StubGateway,
)


def _working_engine_mocks() -> tuple[_CaptureAdapter, _StubGateway]:
    adapter = _CaptureAdapter(
        [
            _result(
                "https://m.example/1",
                "Lạm phát giảm về 2,9%",
                "CPI tháng này giảm về mức 2,9% theo tổng cục thống kê",
                published_at=datetime.now(UTC) - timedelta(hours=8),
            )
        ]
    )
    gateway = _StubGateway(
        "## Bức tranh tổng quan\nLạm phát giảm về 2,9% [1].\n"
        "## Các diễn biến trọng yếu\nNhóm CPI: What -> Why -> tác động [1].\n"
        "## Góc nhìn chuyên gia & Điểm cần theo dõi\nTheo dõi CPI tháng sau.",
        reviews=[
            '{"hallucination_free": true, "depth_ok": true, "issues": []}',
            '{"hallucination_free": true, "depth_ok": true, "issues": []}',
        ],
    )
    return adapter, gateway


def _install_sqlite(tmp_path, monkeypatch):
    db_path = tmp_path / "briefings.db"
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db_path}")
    return db_path


def test_post_briefing_success_persists_and_latest_serves_it(tmp_path, monkeypatch):
    _install_sqlite(tmp_path, monkeypatch)

    from app.db.bootstrap import ensure_schema
    from app.db.session import dispose_engine

    async def _setup():
        await dispose_engine()
        deps.reset_pipelines()
        await ensure_schema()

    asyncio.run(_setup())
    adapter, gateway = _working_engine_mocks()
    monkeypatch.setattr(deps, "get_registry", lambda: _Registry([("google_news", "news", adapter)]))
    monkeypatch.setattr(deps, "get_gateway", lambda: gateway)
    try:
        # `with` keeps one portal loop alive across requests — required so the
        # background persist task is not cancelled at request teardown.
        with TestClient(create_app()) as client:
            resp = client.post("/v1/briefings/macro_economy")
            assert resp.status_code == 200, resp.text
            detail = resp.json()
            import time

            assert detail["topic"] == "macro_economy"
            assert "ai-briefing" in detail["tags"]
            assert "council-reviewed" in detail["tags"]
            assert "Nguồn Dữ Liệu" in detail["content_md"]
            # background persist lands before the next request (portal loop
            # alive between requests); poll instead of assuming exact timing
            latest = None
            for _ in range(40):
                latest = client.get("/v1/briefings/macro_economy/latest")
                if latest.status_code == 200:
                    break
                time.sleep(0.05)
            assert latest.status_code == 200
            assert latest.json()["slug"] == detail["slug"]
            assert latest.json()["tags"] == detail["tags"]
    finally:
        monkeypatch.undo()
        asyncio.run(_teardown())


async def _teardown():
    from app.db.session import dispose_engine

    await dispose_engine()
    deps.reset_pipelines()


def test_post_briefing_no_evidence_returns_honest_503(client, monkeypatch):
    adapter = _CaptureAdapter([])  # no fresh articles in window
    monkeypatch.setattr(deps, "get_registry", lambda: _Registry([("google_news", "news", adapter)]))
    monkeypatch.setattr(deps, "get_gateway", lambda: _StubGateway("ignored"))
    resp = client.post("/v1/briefings/macro_economy")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["message"].startswith("Không đủ bài viết")
    assert body["error"]["details"] == {"channel": "macro_economy", "window_hours": 72}


def test_post_briefing_inflight_returns_429_single_flight():
    from app.api.v1 import briefings as bmod

    loop = asyncio.new_event_loop()
    blocker = loop.create_task(asyncio.sleep(60))
    bmod._active["macro_economy"] = blocker
    try:
        with TestClient(create_app()) as client:
            resp = client.post("/v1/briefings/macro_economy")
        assert resp.status_code == 429
        assert resp.headers.get("retry-after") == "20"
        assert "already being generated" in resp.json()["error"]["message"]
    finally:
        bmod._active.pop("macro_economy", None)
        blocker.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()


def test_latest_prefers_briefing_tag_over_newer_scanner_digest(tmp_path, monkeypatch):
    _install_sqlite(tmp_path, monkeypatch)
    from app.db.bootstrap import ensure_schema
    from app.db.session import dispose_engine

    async def _setup():
        await dispose_engine()
        deps.reset_pipelines()
        await ensure_schema()

    asyncio.run(_setup())
    try:
        # Seed: newer scanner digest (no briefing tag) + older real briefing.
        async def _seed():
            import uuid

            from sqlalchemy import insert

            from app.api.deps import get_session_factory
            from app.db.models import BlogPost

            async with get_session_factory()() as session:
                await session.execute(
                    insert(BlogPost).values(
                        id=uuid.uuid4(),
                        slug="scanner-macro-2026-11-21",
                        topic="macro_economy",
                        title="Bản tin scanner",
                        summary="s",
                        content_md="scanner body",
                        tags=["digest"],
                        sources=[],
                        published_at=datetime.now(UTC),
                        created_at=datetime.now(UTC),
                    )
                )
                await session.execute(
                    insert(BlogPost).values(
                        id=uuid.uuid4(),
                        slug="briefing-macro-older",
                        topic="macro_economy",
                        title="Chuyên sâu vĩ mô",
                        summary="s2",
                        content_md="## Bức tranh tổng quan\nnội dung chuyên sâu",
                        tags=["vi-mo", "ai-briefing"],
                        sources=[{"title": "x", "url": "https://a.example/1"}],
                        published_at=datetime.now(UTC) - timedelta(hours=2),
                        created_at=datetime.now(UTC) - timedelta(hours=2),
                    )
                )
                await session.commit()

        asyncio.run(_seed())
        with TestClient(create_app()) as client:
            resp = client.get("/v1/briefings/macro_economy/latest")
        assert resp.status_code == 200
        body = resp.json()
        # The newer scanner post must NOT be served as the expert briefing (P3-7)
        assert body["slug"] == "briefing-macro-older"
        assert body["title"] == "Chuyên sâu vĩ mô"
    finally:
        monkeypatch.undo()
        asyncio.run(_teardown())


def test_latest_404_when_only_non_briefing_rows_exist(tmp_path, monkeypatch):
    _install_sqlite(tmp_path, monkeypatch)
    from app.db.bootstrap import ensure_schema
    from app.db.session import dispose_engine

    async def _setup():
        await dispose_engine()
        deps.reset_pipelines()
        await ensure_schema()

    asyncio.run(_setup())
    try:

        async def _seed():
            import uuid

            from sqlalchemy import insert

            from app.api.deps import get_session_factory
            from app.db.models import BlogPost

            async with get_session_factory()() as session:
                await session.execute(
                    insert(BlogPost).values(
                        id=uuid.uuid4(),
                        slug="scanner-only",
                        topic="macro_economy",
                        title="Bản tin scanner",
                        summary="s",
                        content_md="scanner body",
                        tags=["digest"],
                        sources=[],
                        published_at=datetime.now(UTC),
                        created_at=datetime.now(UTC),
                    )
                )
                await session.commit()

        asyncio.run(_seed())
        with TestClient(create_app()) as client:
            resp = client.get("/v1/briefings/macro_economy/latest")
        assert resp.status_code == 404
        assert "no persisted AI briefing" in resp.json()["error"]["message"]

        # unknown channel -> 400 (not 404)
        resp2 = client.get("/v1/briefings/no_such_channel/latest")
        assert resp2.status_code == 400
    finally:
        monkeypatch.undo()
        asyncio.run(_teardown())


def test_latest_db_outage_is_500_not_404(client, monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(deps, "get_session_factory", _boom)
    resp = client.get("/v1/briefings/macro_economy/latest")
    assert resp.status_code == 500
    assert "briefing store unavailable" in resp.json()["error"]["message"]
