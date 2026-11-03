"""FIX1 regression tests — answer persist, chunk IDs, breaker recovery, YAML validity."""

from __future__ import annotations

import uuid

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

    def test_tracking_url_and_clean_url_different_ids(self):
        """FIX1: tracking URL and clean URL must produce different chunk IDs."""
        from app.extraction.chunker import chunk_markdown

        c1 = chunk_markdown(
            "# H\n\ntext", document_url="https://x.com/page?utm_source=fb", source_title="T"
        )
        c2 = chunk_markdown("# H\n\ntext", document_url="https://x.com/page", source_title="T")
        assert c1[0].id != c2[0].id

    def test_content_change_produces_different_id(self):
        """FIX1 audit: content change at same URL+position → different chunk ID."""
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

        # Canonical URL difference → different ID
        c3 = chunk_markdown(
            "# H\n\nfirst content",
            document_url="https://x.com/page?utm_source=fb",
            source_title="T",
        )
        c4 = chunk_markdown(
            "# H\n\nfirst content", document_url="https://x.com/page", source_title="T"
        )
        assert c3[0].id != c4[0].id


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


class TestComposeValid:
    def test_compose_yaml_valid(self):
        """FIX1: docker-compose.yml must parse as valid YAML (was duplicate key)."""
        with open("../deploy/docker-compose.yml") as f:
            data = yaml.safe_load(f)
        assert "services" in data
        # Verify no :latest tags (WP-16)
        for svc in data["services"].values():
            img = svc.get("image", "")
            assert ":latest" not in img
