"""FIX1 regression tests — answer persist, chunk IDs, breaker recovery, YAML validity."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import pytest
import yaml


class TestAnswerPersist:
    async def test_persist_answer_roundtrip_sqlite(self, tmp_path, monkeypatch):
        """FIX1: _persist_query + _persist_answer on SQLite — no TypeError, rows exist."""
        from sqlalchemy import select

        from app.config import get_settings
        from app.db.bootstrap import ensure_schema
        from app.db.models import Answer, Query
        from app.db.session import dispose_engine, get_sessionmaker

        db = tmp_path / "fix1.db"
        monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db}")
        await dispose_engine()
        await ensure_schema()

        from app.orchestration import persist

        qid = uuid.uuid4()
        aid = uuid.uuid4()
        await persist._persist_query(qid, "test q", "test q", "vi", {})
        await persist._persist_answer(
            aid, qid, "answer [1].", "model", 10, 5, 0.0, 100, [(1, "https://x.com", "T", None)]
        )

        async with get_sessionmaker()() as session:
            queries = (await session.execute(select(Query))).scalars().all()
            answers = (await session.execute(select(Answer))).scalars().all()
        assert len(queries) == 1
        assert len(answers) == 1
        assert answers[0].content_md == "answer [1]."
        assert answers[0].status == "complete"
        await dispose_engine()


class TestChunkIds:
    def test_chunk_id_is_valid_uuid(self):
        from app.extraction.chunker import chunk_markdown

        chunks = chunk_markdown("# H\n\ntext here", document_url="https://x.com", source_title="T")
        for c in chunks:
            uuid.UUID(c.id)  # must not raise

    def test_chunk_id_deterministic(self):
        from app.extraction.chunker import chunk_markdown

        c1 = chunk_markdown("# H\n\nsame text", document_url="https://x.com", source_title="T")
        c2 = chunk_markdown("# H\n\nsame text", document_url="https://x.com", source_title="T")
        assert [c.id for c in c1] == [c.id for c in c2]

    def test_tracking_url_and_clean_url_same_id(self):
        """FIX1 R1.3: canonicalize strips tracking params → same chunk ID."""
        from app.extraction.chunker import chunk_markdown

        c1 = chunk_markdown(
            "# H\n\ntext", document_url="https://x.com/page?utm_source=fb", source_title="T"
        )
        c2 = chunk_markdown("# H\n\ntext", document_url="https://x.com/page", source_title="T")
        assert c1[0].id == c2[0].id

    def test_content_change_produces_different_id(self):
        """FIX1 audit: content change at same canonical URL+position → different chunk ID."""
        from app.extraction.chunker import chunk_markdown

        # Deterministic: same input → same output
        c1 = chunk_markdown("# H\n\nfirst content", document_url="https://x.com", source_title="T")
        c1b = chunk_markdown("# H\n\nfirst content", document_url="https://x.com", source_title="T")
        assert c1[0].id == c1b[0].id

        # Content change → different ID
        c2 = chunk_markdown(
            "# H\n\ncompletely different text", document_url="https://x.com", source_title="T"
        )
        assert c1[0].id != c2[0].id

        # Tracking and clean URL → SAME canonical URL → SAME ID
        c3 = chunk_markdown(
            "# H\n\nfirst content",
            document_url="https://x.com/page?utm_source=fb",
            source_title="T",
        )
        c4 = chunk_markdown(
            "# H\n\nfirst content", document_url="https://x.com/page", source_title="T"
        )
        assert c3[0].id == c4[0].id


class TestBreakerRecovery:
    def test_half_open_not_stuck_after_disconnect(self):
        """WP-08 FIX1: client disconnect during half-open trial → retried after deadline."""

        import time as _time

        from app.retrieval.health import EngineBreaker

        b = EngineBreaker(threshold=1)
        from app.retrieval.health import Outcome

        b.record(Outcome.UPSTREAM_ERROR)  # trip open
        # Force retry_at to the past so allow() moves to half_open
        b.retry_at = _time.monotonic() - 1
        assert b.allow() is True  # half_open trial starts
        # Simulate client disconnect (no record call, time passes beyond deadline)
        b.half_open_since = _time.monotonic() - b.half_open_deadline_s - 1
        # Next allow() should give a NEW trial (not stuck)
        assert b.allow() is True


class TestAlertsYaml:
    def test_alerts_yaml_valid(self):
        """FIX1: alerts.yml must be valid YAML (was Python docstring)."""
        with open("../deploy/prometheus/alerts.yml") as f:
            data = yaml.safe_load(f)
        assert "groups" in data

    def test_slow_answers_filter_matches_real_labels(self):
        with open("../deploy/prometheus/alerts.yml") as f:
            content = f.read()
        # B4: the alert must use the actual metric label value
        assert 'path="/answer"' in content
        assert 'path="/v1/answer"' not in content


class TestBootstrapMigration:
    async def test_ensure_schema_adds_missing_columns(self, tmp_path, monkeypatch):
        """FIX1 R1.2: ensure_schema adds api_key_id to an old SQLite DB."""
        import sqlite3

        from app.config import get_settings
        from app.db.bootstrap import ensure_schema
        from app.db.session import dispose_engine

        db_path = tmp_path / "old_schema.db"
        # Create DB with OLD schema (no api_key_id)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE queries (id TEXT PRIMARY KEY, raw_text TEXT,"
            " normalized_text TEXT, lang TEXT, intent TEXT, params TEXT,"
            " session_id TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE answers (id TEXT PRIMARY KEY, query_id TEXT,"
            " kind TEXT, content_md TEXT, model TEXT, input_tokens INTEGER,"
            " output_tokens INTEGER, cost_usd REAL, latency_ms INTEGER,"
            " status TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db_path}")
        await dispose_engine()
        await ensure_schema()

        # Verify columns were added
        conn = sqlite3.connect(str(db_path))
        cols_q = {row[1] for row in conn.execute("PRAGMA table_info(queries)").fetchall()}
        cols_a = {row[1] for row in conn.execute("PRAGMA table_info(answers)").fetchall()}
        conn.close()
        assert "api_key_id" in cols_q
        assert "api_key_id" in cols_a


class TestOwnerWrite:
    async def test_persist_stamps_api_key_id(self, tmp_path, monkeypatch):
        """FIX1 R1.7: persist must stamp api_key_id from context var."""
        from sqlalchemy import select

        from app.config import get_settings
        from app.core.auth import current_api_key_id
        from app.db.bootstrap import ensure_schema
        from app.db.models import Query
        from app.db.session import dispose_engine, get_sessionmaker

        db = tmp_path / "owner.db"
        monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db}")
        await dispose_engine()
        await ensure_schema()

        token = current_api_key_id.set("msk_test_owner_1234")
        try:
            from app.orchestration import persist

            qid = uuid.uuid4()
            await persist._persist_query(qid, "test", "test", "vi", {})
            async with get_sessionmaker()() as session:
                row = (await session.execute(select(Query).where(Query.id == qid))).scalar_one()
            assert row.api_key_id == "msk_test_owner_1234"
        finally:
            current_api_key_id.reset(token)
            await dispose_engine()


class TestDepsGatewayPool:
    def test_gateway_uses_llm_pool(self):
        """FIX1 R1.7: deps.py must create gateway with the llm pool."""
        import inspect

        import app.api.deps as deps

        source = inspect.getsource(deps.get_gateway)
        assert "llm" in source  # pool name in the source


# --------------------------------------------------------------------------
# Locking tests (each fails when the corresponding FIX1 change is reverted)
# --------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "deploy" / "docker-compose.yml"
_ALERTS = _REPO / "deploy" / "prometheus" / "alerts.yml"
_PS1 = _REPO / "scripts" / "windows" / "metasearch-lite.ps1"


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects a duplicate key at ANY nesting level."""


def _construct_unique_mapping(loader, node, deep=False):
    seen: set = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise ValueError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    lambda loader, node: _construct_unique_mapping(loader, node, deep=True),
)


def _strict_load(text_or_stream):
    return yaml.load(text_or_stream, Loader=_StrictLoader)


class TestYamlDuplicateKeys:
    def test_detector_catches_nested_duplicate(self):
        """The detector itself must see a duplicate inside services.<name>."""
        bad = "services:\n  redis:\n    image: r\n    mem_limit: 1m\n    mem_limit: 2m\n"
        with pytest.raises(ValueError, match="duplicate key 'mem_limit'"):
            _strict_load(bad)

    def test_compose_has_no_duplicate_key_at_any_level(self):
        _strict_load(_COMPOSE.read_text(encoding="utf-8"))

    def test_alerts_has_no_duplicate_key_at_any_level(self):
        data = _strict_load(_ALERTS.read_text(encoding="utf-8"))
        assert "groups" in data


class TestComposeContent:
    def test_every_service_has_mem_limit(self):
        services = _strict_load(_COMPOSE.read_text(encoding="utf-8"))["services"]
        assert len(services) == 9
        missing = [name for name, svc in services.items() if not svc.get("mem_limit")]
        assert missing == []

    def test_image_tags_are_exact(self):
        services = _strict_load(_COMPOSE.read_text(encoding="utf-8"))["services"]
        assert services["searxng"]["image"] == "searxng/searxng:2026.10.30-1cd13d98d"
        assert services["postgres"]["image"] == "pgvector/pgvector:0.8.7-pg16-trixie"
        assert services["crawl4ai"]["image"] == "unclecode/crawl4ai:0.6.0-r2"

    def test_no_latest_tag(self):
        services = _strict_load(_COMPOSE.read_text(encoding="utf-8"))["services"]
        assert all(":latest" not in svc.get("image", "") for svc in services.values())


class TestPowerShellLiteScript:
    def test_ascii_only(self):
        raw = _PS1.read_bytes()
        bad = [i for i, b in enumerate(raw) if b > 127]
        assert bad == [], f"non-ASCII bytes at offsets {bad[:5]}"

    def test_never_assigns_read_only_pid_variable(self):
        """$PID is a read-only automatic variable in PowerShell."""
        text = _PS1.read_text(encoding="ascii")
        assert re.search(r"\$pid\s*=", text, flags=re.IGNORECASE) is None


class TestVnTickersMissingFileLogs:
    def test_missing_file_logs_error_and_returns_empty(self, monkeypatch, tmp_path):
        from app.retrieval import vn_tickers

        events: list[tuple[str, dict]] = []

        class _Recorder:
            def error(self, event, **kw):
                events.append((event, kw))

            def __getattr__(self, name):  # warning/info/... are not under test
                return lambda *a, **k: None

        missing = tmp_path / "nope" / "tickers.json"
        monkeypatch.setattr(vn_tickers, "DATA_FILE", missing)
        monkeypatch.setattr(vn_tickers, "logger", _Recorder())
        vn_tickers.load_vn_tickers.cache_clear()
        try:
            assert vn_tickers.load_vn_tickers() == {}
        finally:
            vn_tickers.load_vn_tickers.cache_clear()
        assert [e for e, _ in events] == ["vn_tickers_file_missing"]
        assert events[0][1]["path"] == str(missing)


class TestAnswerOwner:
    async def test_answer_row_stamped_with_api_key_id(self, tmp_path, monkeypatch):
        from sqlalchemy import select

        from app.config import get_settings
        from app.core.auth import current_api_key_id
        from app.db.bootstrap import ensure_schema
        from app.db.models import Answer
        from app.db.session import dispose_engine, get_sessionmaker
        from app.orchestration import persist

        db = tmp_path / "answer_owner.db"
        monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db}")
        await dispose_engine()
        await ensure_schema()
        token = current_api_key_id.set("msk_owner_of_answer")
        try:
            qid, aid = uuid.uuid4(), uuid.uuid4()
            await persist._persist_query(qid, "q", "q", "en", {})
            await persist._persist_answer(aid, qid, "a [1].", "m", 1, 1, 0.0, 5, [])
            async with get_sessionmaker()() as session:
                row = (await session.execute(select(Answer).where(Answer.id == aid))).scalar_one()
            assert row.api_key_id == "msk_owner_of_answer"
        finally:
            current_api_key_id.reset(token)
            await dispose_engine()


class TestBootstrapDefaults:
    def test_default_rendering(self):
        from sqlalchemy import Column, Integer, String, text
        from sqlalchemy.sql import func

        from app.db.bootstrap import _column_default

        assert _column_default(Column("a", String, server_default="user")) == " DEFAULT 'user'"
        assert _column_default(Column("a", String, server_default="it's")) == " DEFAULT 'it''s'"
        assert _column_default(Column("a", String, server_default="{}")) == " DEFAULT '{}'"
        assert _column_default(Column("a", Integer, server_default="5")) == " DEFAULT 5"
        assert _column_default(Column("a", Integer, server_default="true")) == " DEFAULT 1"
        assert _column_default(Column("a", Integer, server_default="false")) == " DEFAULT 0"
        assert _column_default(Column("a", String, server_default=func.now())) == ""
        assert _column_default(Column("a", String, server_default=text("now()"))) == ""
        assert _column_default(Column("a", String, server_default="now()")) == ""
        assert _column_default(Column("a", String)) == ""

    async def test_every_missing_non_pk_column_is_added_back(self, tmp_path, monkeypatch):
        """Drop each droppable non-PK column in turn; ensure_schema must restore all."""
        import sqlite3

        from app.config import get_settings
        from app.db.base import Base
        from app.db.bootstrap import ensure_schema
        from app.db.session import dispose_engine

        db = tmp_path / "every_column.db"
        monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db}")
        await dispose_engine()
        await ensure_schema()
        await dispose_engine()

        dropped: list[tuple[str, str]] = []
        conn = sqlite3.connect(str(db))
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if column.primary_key:
                    continue
                try:
                    conn.execute(f'ALTER TABLE "{table.name}" DROP COLUMN "{column.name}"')
                    dropped.append((table.name, column.name))
                except sqlite3.OperationalError:
                    pass  # indexed / foreign key column: SQLite refuses to drop it
        conn.commit()
        conn.close()
        # The risky shapes must have been exercised.
        assert ("api_keys", "created_at") in dropped
        assert ("engines", "config") in dropped
        assert ("api_keys", "role") in dropped
        assert len(dropped) >= 50

        await ensure_schema()  # must not raise
        await dispose_engine()
        conn = sqlite3.connect(str(db))
        try:
            for table_name, column_name in dropped:
                cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table_name}")')}
                assert column_name in cols, f"{table_name}.{column_name} not restored"
        finally:
            conn.close()

    async def test_old_database_keeps_existing_rows(self, tmp_path, monkeypatch):
        """v1.3.1-shaped DB with data: columns appear, rows and keys survive."""
        import sqlite3

        from app.config import get_settings
        from app.db.bootstrap import ensure_schema
        from app.db.session import dispose_engine

        db = tmp_path / "old_with_data.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE api_keys (id CHAR(32) PRIMARY KEY, user_id CHAR(32),
              key_hash TEXT NOT NULL, key_prefix TEXT, name TEXT,
              role TEXT NOT NULL DEFAULT 'user', rate_limit_rpm INTEGER NOT NULL DEFAULT 60,
              enabled BOOLEAN NOT NULL DEFAULT 1, created_at DATETIME NOT NULL);
            CREATE TABLE queries (id CHAR(32) PRIMARY KEY, session_id CHAR(32),
              raw_text TEXT NOT NULL, normalized_text TEXT, lang TEXT, intent TEXT,
              params JSON, created_at DATETIME NOT NULL);
            CREATE TABLE answers (id CHAR(32) PRIMARY KEY, query_id CHAR(32),
              kind TEXT NOT NULL, content_md TEXT, model TEXT, input_tokens INTEGER,
              output_tokens INTEGER, cost_usd REAL, latency_ms INTEGER, status TEXT,
              created_at DATETIME NOT NULL);
            INSERT INTO api_keys VALUES ('k1', NULL, 'hash-1', 'msk_aaaa', 'old key', 'admin',
              120, 1, '2025-01-01 00:00:00');
            INSERT INTO queries VALUES ('q1', NULL, 'old question', 'old question', 'en', NULL,
              NULL, '2025-01-01 00:00:00');
            INSERT INTO answers VALUES ('a1', 'q1', 'answer', 'old answer', 'm', 1, 2, 0.5, 9,
              'complete', '2025-01-01 00:00:00');
            """
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db}")
        await dispose_engine()
        await ensure_schema()
        await ensure_schema()  # idempotent
        await dispose_engine()

        conn = sqlite3.connect(str(db))
        try:
            key = conn.execute(
                "SELECT key_hash, role, rate_limit_rpm, max_concurrent, expires_at "
                "FROM api_keys WHERE id='k1'"
            ).fetchone()
            assert key == ("hash-1", "admin", 120, 5, None)
            assert conn.execute("SELECT raw_text, api_key_id FROM queries").fetchall() == [
                ("old question", None)
            ]
            assert conn.execute("SELECT content_md, api_key_id FROM answers").fetchall() == [
                ("old answer", None)
            ]
        finally:
            conn.close()


class TestHalfOpenThroughRealGateway:
    """Every way a caller can leave must release the half-open trial.

    Reverting `cancelled = True` in LLMGateway.stream (or the finally-block that
    records Outcome.CANCELLED) leaves in_flight=True and the next request fails.
    """

    DELTA = b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'

    @staticmethod
    def _settings():
        from app.config import Settings

        return Settings(
            ENVIRONMENT="test",
            LLM_PROVIDER="openai",
            OPENAI_API_KEY="sk-test",
            OPENAI_BASE_URL="http://llm.test/v1",
        )

    def _gateway(self, mode: str):
        import httpx

        from app.llm.gateway import LLMGateway

        delta = self.DELTA

        class Hang(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield delta
                await asyncio.sleep(3600)
                yield b""

        class Steady(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(400):
                    yield delta
                    await asyncio.sleep(0.005)
                yield b"data: [DONE]\n\n"

        class Short(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield delta
                yield b"data: [DONE]\n\n"

        async def handler(request: httpx.Request) -> httpx.Response:
            if mode == "hang_before_headers":
                await asyncio.sleep(3600)
            stream = {"hang": Hang, "steady": Steady}.get(mode, Short)()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return LLMGateway(self._settings(), client=client, use_runtime_store=False)

    async def _half_open_breaker(self):
        import time

        from app.llm import gateway as gw
        from app.retrieval.health import Outcome

        gw.llm_breakers._breakers.clear()
        async for _ in self._gateway("short").stream([{"role": "user", "content": "x"}]):
            pass
        breaker = gw.llm_breakers.get("llm:openai")
        for _ in range(5):
            breaker.record(Outcome.UPSTREAM_ERROR)
        assert breaker.state == "open"
        breaker.retry_at = time.monotonic() - 1
        return breaker

    async def _next_request_succeeds(self):
        got = 0
        async for _ in self._gateway("short").stream([{"role": "user", "content": "x"}]):
            got += 1
        assert got >= 1

    async def _consume(self, mode: str, *, block: bool = False):
        async for _ in self._gateway(mode).stream([{"role": "user", "content": "x"}]):
            if block:
                await asyncio.sleep(3600)

    async def _cancel_and_check(self, mode: str, *, block: bool = False):
        breaker = await self._half_open_breaker()
        task = asyncio.create_task(self._consume(mode, block=block))
        await asyncio.sleep(0.2)
        assert breaker.half_open_in_flight is True  # the trial really is running
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        assert breaker.half_open_in_flight is False
        await self._next_request_succeeds()

    async def test_aclose_mid_stream_releases_trial(self):
        breaker = await self._half_open_breaker()
        agen = self._gateway("steady").stream([{"role": "user", "content": "x"}])
        async for _ in agen:
            break
        await agen.aclose()
        await asyncio.sleep(0.1)
        assert breaker.half_open_in_flight is False
        await self._next_request_succeeds()

    async def test_task_cancelled_when_upstream_stalls_after_first_delta(self):
        await self._cancel_and_check("hang")

    async def test_task_cancelled_before_headers(self):
        await self._cancel_and_check("hang_before_headers")

    async def test_task_cancelled_while_consumer_is_busy(self):
        await self._cancel_and_check("steady", block=True)

    async def test_wait_for_timeout_releases_trial(self):
        breaker = await self._half_open_breaker()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(self._consume("hang"), timeout=0.2)
        await asyncio.sleep(0.1)
        assert breaker.half_open_in_flight is False
        await self._next_request_succeeds()

    async def test_complete_cancelled_releases_trial(self):
        breaker = await self._half_open_breaker()
        task = asyncio.create_task(
            self._gateway("hang").complete([{"role": "user", "content": "x"}])
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        assert breaker.half_open_in_flight is False
        await self._next_request_succeeds()
