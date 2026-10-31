"""Lite profile tests: DuckDuckGo adapter, SQLite persistence, fallbacks."""

from __future__ import annotations

import httpx
import pytest

from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.ddg import DuckDuckGoAdapter, _unwrap

DDG_HTML = """
<div class="result results_links">
 <div class="result__body">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.postgresql.org%2Fdocs%2F&amp;rut=abc">PostgreSQL <b>Docs</b></a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.postgresql.org%2Fdocs%2F">The official <b>docs</b> for PostgreSQL. 03 Jan 2025</a>
 </div>
</div>
"""  # noqa: E501 - fixture HTML mirrors real DDG markup; lines are data, not code

DDG_HTML += """
<div class="result__body">
  <a class="result__a" href="https://direct.example.com/page">Direct link result</a>
  <a class="result__snippet" href="https://direct.example.com/page">snippet two</a>
</div>
"""


class TestUnwrap:
    def test_uddg_redirect(self):
        assert (
            _unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fa&rut=1") == "https://x.com/a"
        )

    def test_direct_https(self):
        assert _unwrap("https://x.com/") == "https://x.com/"

    def test_rejects_non_http(self):
        assert _unwrap("/help/faq") is None


class TestDuckDuckGo:
    async def test_parses_results(self, monkeypatch):
        monkeypatch.setattr("app.retrieval.adapters.ddg._last_call", 0.0)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "html.duckduckgo.com"
            return httpx.Response(200, text=DDG_HTML)

        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(handler))
        results = await DuckDuckGoAdapter().search("postgres docs", max_results=10)
        assert len(results) == 2
        assert results[0].url == "https://www.postgresql.org/docs/"
        assert "PostgreSQL" in results[0].title
        assert results[0].published_at is not None  # date parsed from snippet
        assert results[1].url == "https://direct.example.com/page"

    async def test_bot_block_not_retried(self, monkeypatch):
        monkeypatch.setattr("app.retrieval.adapters.ddg._last_call", 0.0)
        monkeypatch.setattr(
            httpx, "AsyncClient", _mock_client_factory(lambda r: httpx.Response(403))
        )
        with pytest.raises(AdapterError, match="403"):
            await DuckDuckGoAdapter().search("x")

    async def test_challenge_page_detected(self, monkeypatch):
        monkeypatch.setattr("app.retrieval.adapters.ddg._last_call", 0.0)
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _mock_client_factory(lambda r: httpx.Response(200, text="anomaly detected")),
        )
        with pytest.raises(AdapterError, match="challenge"):
            await DuckDuckGoAdapter().search("x")


def _mock_client_factory(handler):
    original = httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    return make_client


class TestSQLiteProfile:
    async def test_end_to_end_persistence(self, tmp_path, monkeypatch):
        """The Lite profile runs the full persistence path on SQLite."""

        db_path = tmp_path / "lite.db"

        import app.api.deps as deps
        from app.config import get_settings
        from app.db.bootstrap import ensure_schema
        from app.db.session import dispose_engine

        # The settings singleton is already built (conftest env) — patch the
        # instance, then drop the cached engine so it re-reads the URL.
        monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db_path}")
        await dispose_engine()
        deps.reset_pipelines()

        try:
            assert await ensure_schema() is True

            from sqlalchemy import select

            from app.core.auth import create_key
            from app.db.models import Answer, ApiKey, Query
            from app.db.session import get_sessionmaker

            _plaintext, prefix = await create_key("lite-key", role="admin", rate_limit_rpm=30)
            async with get_sessionmaker()() as session:
                session.add(Query(id=__import__("uuid").uuid4(), raw_text="q", normalized_text="q"))
                session.add(
                    Answer(
                        query_id=None,
                        kind="quick",
                        content_md="answer [1]",
                        model="m",
                        status="complete",
                    )
                )
                await session.commit()

                keys = (await session.execute(select(ApiKey))).scalars().all()
                queries = (await session.execute(select(Query))).scalars().all()
                answers = (await session.execute(select(Answer))).scalars().all()
            assert len(keys) == 1 and keys[0].key_prefix == prefix
            assert keys[0].role == "admin"
            assert len(queries) == 1
            assert len(answers) == 1
        finally:
            await dispose_engine()
            deps.reset_pipelines()

    async def test_engine_store_upsert_on_sqlite(self, tmp_path, monkeypatch):
        from app.config import Settings
        from app.orchestration.engine_store import DbEngineStore

        db_path = tmp_path / "eng.db"
        store = DbEngineStore(
            Settings(ENVIRONMENT="test", DATABASE_URL=f"sqlite+aiosqlite:///{db_path}"),
            refresh_s=0,
        )
        updated = await store.update("searxng", {"weight": 2.5})
        assert updated is not None
        assert updated.weight == 2.5
        snapshot = await store.snapshot()
        assert snapshot["searxng"].weight == 2.5  # persisted, not just fallback

        # health upsert works on sqlite too
        await store.record_result("searxng", ok=False, latency_ms=5)
        health = await store.health()
        assert health["searxng"].failures == 1


class TestSearxngDisabled:
    def test_flag_disables_searxng_keeps_ddg(self):
        from app.config import Settings
        from app.orchestration.engine_store import default_configs

        configs = default_configs(Settings(ENVIRONMENT="test", SEARXNG_ENABLED="false"))
        assert configs["searxng"].enabled is False
        assert configs["ddg"].enabled is True
        assert configs["searxng"].weight > configs["ddg"].weight  # searxng outranks when both live
