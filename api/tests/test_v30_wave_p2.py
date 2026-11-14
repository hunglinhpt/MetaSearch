"""v3.0.0 (docs/20 P2 wave) tests: vi-first tokenize/folding, domain reputation
tiers, structured JSON output, online interleaving, PDF page extraction,
per-space knowledge graph, personalized podcast helpers/endpoints."""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime

import httpx
import pytest

from app.cache import search_cache_key
from app.config import get_settings
from app.db.models import Document, KgEdge, KgNode, MemoryItem, Space
from app.extraction.pdf import is_pdf_bytes, pdf_pages_to_markdown
from app.llm.structured import extract_structured, validate_instance
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import SearchParams, SearchPipeline
from app.retrieval.graph import build_space_graph, parse_graph_json, query_graph
from app.retrieval.interleave import assign_variant, interleave_stats, variant_signature
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.registry import EngineRegistry
from app.retrieval.scoring import (
    apply_signals,
    domain_authority,
    fold_diacritics,
    tokenize,
)
from app.retrieval.tiers import TIER_SCORES, domain_tier, tier_score
from app.tts.briefing import (
    _PERSONAL_FILE_RE,
    BRIEFING_PRESETS,
    cleanup_retention,
    latest_briefing,
    write_briefing,
)

# --- helpers ------------------------------------------------------------------


def _make_pdf(texts: list[str]) -> bytes:
    """Build a real multi-page PDF with extractable text via pypdf."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        content = DecodedStreamObject()
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content.set_data(f"BT /F1 12 Tf 50 250 Td ({escaped}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
        resources = DictionaryObject()
        resources[NameObject("/Font")] = DictionaryObject(
            {NameObject("/F1"): writer._add_object(font)}
        )
        page[NameObject("/Resources")] = writer._add_object(resources)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class _FakeGateway:
    """Minimal gateway stub: returns a canned completion per task."""

    def __init__(self, reply: str | None = None, exc: Exception | None = None):
        self.reply = reply
        self.exc = exc
        self.calls: list[dict] = []

    async def complete(self, messages, *, model=None, max_tokens=None, task=None):
        self.calls.append({"task": task, "messages": messages})
        if self.exc is not None:
            raise self.exc
        return self.reply or "", {"total_tokens": 1}


class _RegAdapter:
    def __init__(self, eid: str, results: list[RawResult]):
        self.id = eid
        self.results = results

    async def search(self, query, **kwargs):
        return list(self.results)


def _pipeline(adapters, settings=None) -> SearchPipeline:
    configs = {
        a.id: EngineConfig(id=a.id, name=a.id, category="general", enabled=True) for a in adapters
    }
    registry = EngineRegistry(InMemoryEngineStore(configs), {a.id: a for a in adapters})
    return SearchPipeline(settings or get_settings(), registry, cache=None, gateway=None)


def _raw(url: str, adapter: str = "fake") -> RawResult:
    return RawResult(
        url=url,
        title="title " + url,
        snippet="snippet " + url,
        adapter=adapter,
        source_engines=[adapter],
        rank=0,
    )


@pytest.fixture
async def v30_db(tmp_path, monkeypatch):
    db_file = tmp_path / "v30.db"
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
    key, _ = await create_key("v30_user", role="user", rate_limit_rpm=6000)
    yield key
    get_settings.cache_clear()


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# --- P2-3 vi-first tokenize ----------------------------------------------------


class TestViFirst:
    def test_fold_diacritics(self):
        # fold only strips diacritics + đ; lowercasing happens in tokenize()
        assert fold_diacritics("Đà Nẵng") == "Da Nang"
        assert fold_diacritics("đ") == "d"
        assert fold_diacritics("abasđ") == "abasd"
        assert fold_diacritics("plain ascii") == "plain ascii"

    def test_tokenize_folds_and_drops_stopwords(self):
        toks = tokenize("Đánh giá và Báo cáo")
        assert "danh" in toks and "gia" in toks and "bao" in toks and "cao" in toks
        assert "va" not in toks  # folded stopword removed
        assert all(t.isascii() for t in toks)

    def test_accented_query_matches_unaccented(self):
        a = set(tokenize("Báo cáo tài chính quý 3 năm 2026"))
        b = set(tokenize("Bao cao tai chinh quy 3 nam 2026"))
        assert a == b
        assert "bao" in a and "tai" in a and "chinh" in a

    def test_tokenize_fold_disabled(self):
        toks = tokenize("Đà Nẵng", fold=False)
        assert "đà" in toks or "Đà".lower() in toks


# --- P2-7 domain tiers ---------------------------------------------------------


class TestDomainTiers:
    def test_exact_suffix_www_unknown(self):
        assert domain_tier("vnexpress.net") == "aggregator"
        assert domain_tier("sub.vnexpress.net") == "aggregator"
        assert domain_tier("WWW.HSX.VN") == "official"
        assert domain_tier("sec.gov") == "official"
        assert domain_tier("arxiv.org") == "research"
        assert domain_tier("github.com") == "community"
        assert domain_tier("some-random-blog.xyz") == "unknown"

    def test_tier_score_ordering(self):
        assert TIER_SCORES["official"] > TIER_SCORES["primary"] > TIER_SCORES["research"]
        assert TIER_SCORES["aggregator"] > TIER_SCORES["community"] > TIER_SCORES["unknown"]
        assert tier_score("nope.example") == TIER_SCORES["unknown"]

    def test_curated_trust_table_wins_over_tier(self):
        # sec.gov is tier "official" (0.97) but the curated table pins 0.95.
        assert domain_authority("sec.gov") == pytest.approx(0.95)
        # vnexpress is not curated -> tier fallback 0.72.
        assert domain_authority("vnexpress.net") == pytest.approx(0.72)
        assert domain_authority("unknown-site.example") == pytest.approx(0.50)

    def test_apply_signals_sets_domain_tier(self):
        from app.retrieval.models import ScoredResult

        item = ScoredResult(
            url="https://vnexpress.net/a",
            canonical_url="https://vnexpress.net/a",
            title="t",
            snippet="s",
            domain="vnexpress.net",
            adapters=["a"],
            engines=["a"],
            consensus=1,
            best_rank=0,
        )
        apply_signals(
            item,
            query="q",
            registry={"a": EngineConfig(id="a", name="a", category="general", enabled=True)},
            max_engines=1,
            now=datetime.now(UTC),
        )
        assert item.domain_tier == "aggregator"


# --- P2-6 structured output ----------------------------------------------------


class TestStructured:
    SCHEMA = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "price": {"type": "number"}},
        "required": ["name"],
    }

    def test_validate_instance(self):
        assert validate_instance({"name": "x"}, self.SCHEMA) == []
        errs = validate_instance({"price": 1}, self.SCHEMA)
        assert errs and "required" in errs[0]
        assert validate_instance({"name": 5}, self.SCHEMA)

    @pytest.mark.asyncio
    async def test_extract_structured_valid(self):
        gw = _FakeGateway(reply='prefix {"name": "ACME", "price": 12.5} suffix')
        obj, errs, _rawtxt = await extract_structured(
            gw, "acme price", self.SCHEMA, [_raw("https://a.com")], model="m"
        )
        assert obj == {"name": "ACME", "price": 12.5}
        assert errs == []
        assert gw.calls[0]["task"] == "structured"

    @pytest.mark.asyncio
    async def test_extract_structured_invalid_reported_not_fabricated(self):
        gw = _FakeGateway(reply='{"price": 3}')  # missing required "name"
        obj, errs, _rawtxt = await extract_structured(
            gw, "q", self.SCHEMA, [_raw("https://a.com")], model="m"
        )
        assert obj is None
        assert errs and "required" in errs[0]

    @pytest.mark.asyncio
    async def test_extract_structured_llm_failure_fail_open(self):
        gw = _FakeGateway(exc=RuntimeError("boom"))
        obj, errs, _rawtxt = await extract_structured(gw, "q", self.SCHEMA, [], model="m")
        assert obj is None and errs and _rawtxt == ""

    @pytest.mark.asyncio
    async def test_extract_structured_no_json(self):
        gw = _FakeGateway(reply="no json here")
        obj, errs, _raw = await extract_structured(gw, "q", self.SCHEMA, [], model="m")
        assert obj is None and errs


# --- P2-4 online interleaving --------------------------------------------------


class TestInterleave:
    def test_ratio_bounds(self):
        assert assign_variant("q", "u", ratio=0.0) == "a"
        assert assign_variant("q", "u", ratio=1.0) == "b"
        assert assign_variant("q", "u", ratio=-1.0) == "a"
        assert assign_variant("q", "u", ratio=2.0) == "b"

    def test_deterministic_per_query_owner(self):
        a = assign_variant("hello", "owner1", ratio=0.5, salt="v3")
        b = assign_variant("hello", "owner1", ratio=0.5, salt="v3")
        assert a == b
        # different salt or owner may flip, but stays in {a, b}
        assert assign_variant("hello", "owner2", ratio=0.5, salt="v3") in ("a", "b")
        assert assign_variant("hello", "owner1", ratio=0.5, salt="v4") in ("a", "b")

    def test_distribution_roughly_matches_ratio(self):
        n = 400
        bs = sum(
            assign_variant(f"query {i}", "owner", ratio=0.5, salt="v3") == "b" for i in range(n)
        )
        assert 0.35 < bs / n < 0.65

    def test_variant_signature_distinct(self):
        assert variant_signature("a") != variant_signature("b")

    def test_cache_key_includes_variant(self):
        p_a = SearchParams(query="q", interleave_sig=variant_signature("a"))
        p_b = SearchParams(query="q", interleave_sig=variant_signature("b"))
        p_none = SearchParams(query="q")
        assert search_cache_key("q", p_a) != search_cache_key("q", p_b)
        assert search_cache_key("q", p_a) != search_cache_key("q", p_none)
        assert search_cache_key("q", p_a) == search_cache_key(
            "q", SearchParams(query="q", interleave_sig="il:a")
        )

    @pytest.mark.asyncio
    async def test_interleave_stats_aggregates_feedback(self, v30_db):
        from app.api import deps

        async with deps.get_session_factory()() as session:
            session.add_all(
                [
                    MemoryItem(
                        api_key_id="k",
                        kind="feedback",
                        url="u1",
                        weight=1.0,
                        payload={"variant": "a"},
                    ),
                    MemoryItem(
                        api_key_id="k",
                        kind="feedback",
                        url="u2",
                        weight=1.0,
                        payload={"variant": "a"},
                    ),
                    MemoryItem(
                        api_key_id="k",
                        kind="feedback",
                        url="u3",
                        weight=-1.0,
                        payload={"variant": "a"},
                    ),
                    MemoryItem(
                        api_key_id="k",
                        kind="feedback",
                        url="u4",
                        weight=1.0,
                        payload={"variant": "b"},
                    ),
                    MemoryItem(
                        api_key_id="k", kind="query", text="q", weight=0.0, payload={"variant": "a"}
                    ),
                ]
            )
            await session.commit()
            stats = await interleave_stats(session)
        variants = stats["variants"]
        assert variants["a"]["feedback_total"] == 3
        assert variants["a"]["up"] == 2 and variants["a"]["down"] == 1
        assert variants["a"]["up_rate"] == pytest.approx(round(2 / 3, 4))
        assert variants["b"]["up_rate"] == 1.0
        assert "query" not in variants  # only kind=feedback counted

    @pytest.mark.asyncio
    async def test_pipeline_assigns_variant(self):
        pipe = _pipeline([_RegAdapter("fake", [_raw("https://vnexpress.net/a")])])
        outcome = await pipe.run(SearchParams(query="tin moi", k=5))
        assert outcome.interleave_variant in ("a", "b")
        assert outcome.results[0].domain_tier == "aggregator"


# --- P2-1 PDF page extraction ---------------------------------------------------


class TestPdf:
    def test_is_pdf_bytes(self):
        assert is_pdf_bytes(b"%PDF-1.7 ...")
        assert not is_pdf_bytes(b"<html>")
        assert not is_pdf_bytes(b"")

    def test_pages_to_markdown(self):
        data = _make_pdf(["Chao the gioi", "Trang hai noi dung"])
        md = pdf_pages_to_markdown(data)
        assert "## p.1" in md and "Chao the gioi" in md
        assert "## p.2" in md and "Trang hai" in md

    def test_max_pages_bound(self):
        data = _make_pdf(["one", "two", "three"])
        md = pdf_pages_to_markdown(data, max_pages=2)
        assert "## p.2" in md and "## p.3" not in md

    def test_garbage_returns_empty(self):
        assert pdf_pages_to_markdown(b"%PDF-broken") == ""
        assert pdf_pages_to_markdown(b"not a pdf at all") == ""


# --- P2-2 per-space knowledge graph ---------------------------------------------


class TestGraph:
    def test_parse_graph_json_sanitizes(self):
        text = (
            'noise before {"nodes": ['
            '{"label": "Đà Nẵng", "type": "city", "description": "thanh pho"},'
            '{"label": "da nang"},'  # folded duplicate -> dropped
            '{"label": ""},'
            '{"label": "FPT"}'
            '], "edges": ['
            '{"source": "Đà Nẵng", "target": "FPT", "relation": "Liên quan"},'
            '{"source": "FPT", "target": "nowhere", "relation": "x"},'  # unknown target -> dropped
            '{"source": "FPT", "target": "FPT", "relation": "self"}'  # self loop -> dropped
            "]} trailing"
        )
        g = parse_graph_json(text)
        labels = [n["label"] for n in g["nodes"]]
        assert labels == ["da nang", "fpt"]
        assert g["edges"] == [{"source": "da nang", "target": "fpt", "relation": "lien quan"}]

    def test_parse_graph_json_never_raises(self):
        assert parse_graph_json("") == {"nodes": [], "edges": []}
        assert parse_graph_json("no json") == {"nodes": [], "edges": []}
        assert parse_graph_json("{broken") == {"nodes": [], "edges": []}

    def test_parse_graph_json_max_nodes(self):
        nodes = ",".join(f'{{"label": "e{i}"}}' for i in range(50))
        g = parse_graph_json('{"nodes": [' + nodes + '], "edges": []}', max_nodes=10)
        assert len(g["nodes"]) == 10

    @pytest.mark.asyncio
    async def test_build_and_query_space_graph(self, v30_db):
        from app.api import deps

        owner = "v30_user"
        graph_json = json.dumps(
            {
                "nodes": [
                    {"label": "FPT Corporation", "type": "company", "description": "VN tech corp"},
                    {"label": "VNG", "type": "company", "description": "VN internet corp"},
                ],
                "edges": [{"source": "FPT Corporation", "target": "VNG", "relation": "peer"}],
            }
        )
        gw = _FakeGateway(reply=graph_json)
        async with deps.get_session_factory()() as session:
            space = Space(api_key_id=owner, name="tech", slug="tech", domains=["example.com"])
            session.add(space)
            await session.flush()
            session.add(
                Document(
                    canonical_url="https://example.com/a",
                    url_hash="h1",
                    domain="example.com",
                    content_md="# doc about FPT and VNG",
                    fetched_at=datetime.now(UTC),
                )
            )
            await session.commit()
            stats = await build_space_graph(
                session, space, gw, settings=get_settings(), max_docs=5, max_nodes=50, model="m"
            )
            assert stats["docs"] == 1 and stats["nodes"] == 2 and stats["edges"] == 1
            from sqlalchemy import select

            n_rows = (await session.execute(select(KgNode))).scalars().all()
            e_rows = (await session.execute(select(KgEdge))).scalars().all()
            assert {n.label for n in n_rows} == {"fpt corporation", "vng"}
            assert len(e_rows) == 1
            result = await query_graph(session, space, "FPT peer?")
            assert "fpt corporation" in result["matched_labels"]
            assert result["edges"] == [
                {"source": "fpt corporation", "target": "vng", "relation": "peer"}
            ]
            assert "fpt corporation" in result["context"]
            # Rebuild is idempotent: unique constraints (space_id, label) /
            # (space_id, source, target, relation) must not duplicate rows.
            stats2 = await build_space_graph(
                session, space, gw, settings=get_settings(), max_docs=5, max_nodes=50, model="m"
            )
            assert stats2["docs"] == 1  # extraction ran again...
            n_rows2 = (await session.execute(select(KgNode))).scalars().all()
            e_rows2 = (await session.execute(select(KgEdge))).scalars().all()
            assert len(n_rows2) == 2  # ...but no duplicate rows
            assert len(e_rows2) == 1

    @pytest.mark.asyncio
    async def test_vision_embed_dim_guard(self, monkeypatch):
        from app.retrieval import vision

        class _S:
            vision_embed_api_base_url = "https://embed.example.com"
            vision_embed_model = "jina-embeddings-v4"

        assert vision.vision_embed_available(_S())
        assert not vision.vision_embed_available(
            type("X", (), {"vision_embed_api_base_url": "ftp://x"})()
        )

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}  # wrong dim

        class _Client:
            async def post(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(vision, "get_http_client", lambda name: _Client())
        with pytest.raises(ValueError, match="dim"):
            await vision.embed_texts_via_vision_api(_S(), ["hello"])


# --- P2-5 personalized podcast ---------------------------------------------------


class TestPersonalPodcast:
    def test_prefix_is_hashed_and_stable(self):
        from app.api.v1 import podcast as pc

        p1 = pc._personal_prefix("key-abc")
        assert p1 == pc._personal_prefix("key-abc")
        assert p1.startswith("personal-") and len(p1) == len("personal-") + 8
        assert "key-abc" not in p1  # raw key never leaks into file names
        assert pc._personal_prefix("key-abc") != pc._personal_prefix("key-abd")

    def test_personal_key_differs_from_daily_key(self):
        from app.api.v1 import podcast as pc

        day = date(2026, 11, 14)
        daily = day.isoformat()
        personal = pc._personal_key("key-abc", day)
        assert personal != daily
        assert personal.endswith(":2026-11-14")

    def test_personal_file_regex(self):
        assert _PERSONAL_FILE_RE.match("personal-0123abcd-2026-11-14.mp3")
        assert not _PERSONAL_FILE_RE.match("briefing-2026-11-14.mp3")
        assert not _PERSONAL_FILE_RE.match("personal-xyz-2026-11-14.mp3")
        assert not _PERSONAL_FILE_RE.match("personal-0123abcd-2026-11-14.mp3.tmp")
        assert not _PERSONAL_FILE_RE.match("personal-0123abcd-14-11-2026.mp3")

    def test_personal_presets_fallback_and_mapping(self, monkeypatch):
        from app.api.v1 import podcast as pc

        monkeypatch.setenv("PODCAST_PERSONAL_ENABLED", "true")
        get_settings.cache_clear()
        assert pc._personal_presets([]) == BRIEFING_PRESETS
        presets = pc._personal_presets(["cổ phiếu FPT", "thị trường chứng khoán"])
        assert len(presets) == 2
        assert presets[0][1] == "cổ phiếu FPT" and presets[0][2] == 2
        get_settings.cache_clear()

    def test_write_and_latest_and_retention_personal(self, tmp_path):
        day = date(2026, 11, 14)
        old = date(2026, 10, 1)
        prefix = "personal-0123abcd"
        write_briefing(tmp_path, day, b"audio-new", prefix)
        write_briefing(tmp_path, old, b"audio-old", prefix)
        write_briefing(tmp_path, day, b"daily-new")  # daily file untouched by prefix lookup
        write_briefing(tmp_path, old, b"daily-old")
        assert latest_briefing(tmp_path, prefix) is not None
        assert latest_briefing(tmp_path, prefix).name == f"{prefix}-2026-11-14.mp3"
        assert latest_briefing(tmp_path).name == "briefing-2026-11-14.mp3"
        removed = cleanup_retention(tmp_path, retention_days=7, today=day)
        assert removed == 2  # old personal + old daily
        assert not (tmp_path / f"{prefix}-2026-10-01.mp3").exists()
        assert not (tmp_path / "briefing-2026-10-01.mp3").exists()
        assert (tmp_path / f"{prefix}-2026-11-14.mp3").exists()

    @pytest.mark.asyncio
    async def test_personal_podcast_requires_voicestudio(self, v30_db):
        from app.api.v1 import podcast as pc
        from app.main import create_app

        pc.reset_podcast_state()
        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/podcast/personal", headers=_h(v30_db))
            assert r.status_code == 503
            assert r.json()["error"]["code"] == "tts_disabled"
            r = await c.get("/v1/podcast/personal/latest", headers=_h(v30_db))
            assert r.status_code == 503


# --- endpoint smoke ---------------------------------------------------------------


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_ops_interleave(self, v30_db):
        from app.main import create_app

        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/v1/ops/interleave", headers=_h(v30_db))
            assert r.status_code == 200
            body = r.json()
            assert "variants" in body and "ratio" in body
            assert body["ratio"] == pytest.approx(get_settings().interleave_ratio)

    @pytest.mark.asyncio
    async def test_graph_build_unknown_space_404(self, v30_db):
        from app.main import create_app

        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/spaces/nope/graph/build", headers=_h(v30_db))
            assert r.status_code == 404
            r = await c.post("/v1/spaces/nope/graph/query", json={"query": "x"}, headers=_h(v30_db))
            assert r.status_code == 404
