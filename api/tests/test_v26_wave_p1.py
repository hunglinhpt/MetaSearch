"""v2.6.0 (docs/20 P1 wave) tests: freshness half-lives, adaptive router,
IRCoT research loop, per-key memory, /v1/map + /v1/crawl, Spaces,
late chunking, hybrid dense+sparse chunk search."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import Settings, get_settings
from app.extraction.chunker import chunk_markdown, late_chunk_markdown, segment_long_document
from app.extraction.sitemap import extract_links, map_site, parse_sitemap
from app.llm.embeddings import EmbeddingClient, EmbeddingError
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import SearchParams, SearchPipeline
from app.orchestration.research import (
    ResearchParams,
    ResearchPipeline,
    _plan_coverage,
    _reformulate_query,
)
from app.retrieval.boosts import BoostRule, apply_boosts, rules_signature
from app.retrieval.hybrid import bm25_scores, hybrid_chunk_search, rrf_fuse
from app.retrieval.merger import merge
from app.retrieval.models import EngineConfig, RawResult, ScoredResult
from app.retrieval.registry import EngineRegistry
from app.retrieval.router import classify_complexity
from app.retrieval.scoring import apply_signals, freshness


def _cfg(eid: str, weight: float = 1.0, category: str = "general") -> EngineConfig:
    return EngineConfig(id=eid, name=eid, category=category, weight=weight, enabled=True)


def _raw(url: str, adapter: str, rank: int = 0, published: datetime | None = None) -> RawResult:
    tag = abs(hash(url)) % 9973
    words = ["quasar", "nebula", "harbor", "meadow", "copper", "violet", "anchor", "lantern"]
    w = words[tag % len(words)]
    return RawResult(
        url=url,
        title=f"{w} {tag} report",
        snippet=f"{w} {tag} " + " ".join(words[(tag + i) % len(words)] for i in range(1, 7)),
        adapter=adapter,
        source_engines=[adapter],
        rank=rank,
        published_at=published,
    )


class _RegAdapter:
    def __init__(self, eid: str, category: str, results: list[RawResult]):
        self.id = eid
        self._category = category
        self.results = results
        self.queries: list[str] = []

    async def search(self, query, **kwargs):
        self.queries.append(query)
        return list(self.results)


def _pipeline(adapters, gateway=None, settings=None) -> SearchPipeline:
    configs = {
        a.id: EngineConfig(id=a.id, name=a.id, category=a._category, enabled=True) for a in adapters
    }
    registry = EngineRegistry(InMemoryEngineStore(configs), {a.id: a for a in adapters})
    return SearchPipeline(settings or get_settings(), registry, cache=None, gateway=gateway)


# --- P1-8 freshness half-life -------------------------------------------------


class TestFreshnessDecay:
    def test_legacy_curve_unchanged_without_half_life(self):
        now = datetime.now(UTC)
        assert freshness(now - timedelta(days=30), now) == pytest.approx(math.exp(-1.0), rel=1e-9)

    def test_half_life_curve(self):
        now = datetime.now(UTC)
        assert freshness(now - timedelta(days=90), now, half_life_days=90.0) == pytest.approx(0.5)
        assert freshness(now - timedelta(days=45), now, half_life_days=45.0) == pytest.approx(0.5)
        assert freshness(None, now, half_life_days=90.0) == 0.3

    def test_half_life_decays_monotonic(self):
        now = datetime.now(UTC)
        a = freshness(now - timedelta(days=1), now, half_life_days=45.0)
        b = freshness(now - timedelta(days=365), now, half_life_days=45.0)
        assert a > b

    def test_apply_signals_other_half_life_passthrough(self):
        item = ScoredResult(
            url="https://example.com/a",
            canonical_url="https://example.com/a",
            title="t",
            snippet="s",
            domain="example.com",
            adapters=["a"],
            engines=["a"],
            consensus=1,
            best_rank=0,
            published_at=datetime.now(UTC) - timedelta(days=90),
        )
        now = datetime.now(UTC)
        s_legacy = apply_signals(
            item, query="q", registry={"a": _cfg("a")}, max_engines=1, now=now, news=False
        )
        fresh_legacy = item.freshness
        s_hl = apply_signals(
            item,
            query="q",
            registry={"a": _cfg("a")},
            max_engines=1,
            now=now,
            news=False,
            other_half_life_days=90.0,
        )
        assert item.freshness == pytest.approx(0.5)
        assert s_hl > s_legacy  # 90d half-life keeps old docs fresher than exp(-age/30)...
        assert fresh_legacy < 0.5

    def test_merge_passes_half_lives(self):
        registry = {"a": _cfg("a")}
        now = datetime.now(UTC)
        raws = [
            _raw("https://x.com/new", "a", published=now - timedelta(days=1)),
            _raw("https://y.com/old", "a", published=now - timedelta(days=365)),
        ]
        out = merge(
            raws,
            query="quasar",
            registry=registry,
            now=now,
            other_half_life_days=45.0,
        )
        by_url = {r.canonical_url: r for r in out}
        assert by_url["https://y.com/old"].freshness == pytest.approx(0.5 ** (365 / 45), rel=1e-6)


# --- P1-2 adaptive router ------------------------------------------------------


class TestAdaptiveRouter:
    def test_markers_route_deep(self):
        assert classify_complexity("so sánh iPhone 16 và iPhone 17") == "deep"
        assert classify_complexity("compare PostgreSQL vs MySQL replication") == "deep"
        assert classify_complexity("phân tích nguyên nhân thị trường giảm") == "deep"

    def test_long_query_routes_deep(self):
        q = " ".join(["word"] * 20)
        assert classify_complexity(q, min_words_deep=14) == "deep"

    def test_short_query_simple(self):
        assert classify_complexity("giá vàng") == "simple"
        assert classify_complexity("what is rust language") == "standard"

    @pytest.mark.asyncio
    async def test_personalize_auto_deep(self):
        general = _RegAdapter("g1", "general", [_raw("https://g.com/1", "g1")])
        pipe = _pipeline([general])
        params = await pipe._personalize(
            SearchParams(query="so sánhorsa hai ngôn ngữ này khác nhau thế nào về hiệu năng")
        )
        assert params.deep is True

    @pytest.mark.asyncio
    async def test_personalize_short_query_stays_shallow(self):
        general = _RegAdapter("g1", "general", [_raw("https://g.com/1", "g1")])
        pipe = _pipeline([general])
        params = await pipe._personalize(SearchParams(query="giá vàng"))
        assert params.deep is False

    @pytest.mark.asyncio
    async def test_router_disabled(self):
        settings = Settings(adaptive_router_enabled=False)
        general = _RegAdapter("g1", "general", [_raw("https://g.com/1", "g1")])
        pipe = _pipeline([general], settings=settings)
        params = await pipe._personalize(
            SearchParams(query="so sánh A và B cái nào tốt hơn hiện nay")
        )
        assert params.deep is False


# --- P1-1 IRCoT research loop ---------------------------------------------------


class _FakeLearning:
    def __init__(self, fact: str):
        self.fact = fact
        self.citation_id = 0


class TestIRCoT:
    def test_reformulate_includes_evidence(self):
        learnings = [_FakeLearning("Fed raised rates by 25bp in March")]
        out = _reformulate_query("Fed policy 2026", "impact on Vietnam exports", learnings)
        assert "Fed raised rates" in out
        assert "impact on Vietnam exports" in out

    def test_reformulate_without_evidence(self):
        out = _reformulate_query("q", "gap x", [])
        assert out == "q — gap x"

    def test_coverage_gate(self):
        plan = [("Fed rates decision", "rates"), ("Vietnam exports outlook", "exports")]
        learnings = [_FakeLearning("Fed rates were hiked and dot plot shifted")]
        cov = _plan_coverage(plan, learnings)
        assert 0.0 < cov < 1.0
        full = [_FakeLearning("Fed rates decision dot plot; Vietnam exports outlook strong")]
        assert _plan_coverage(plan, full) == 1.0
        assert _plan_coverage([], []) == 1.0

    @pytest.mark.asyncio
    async def test_research_loop_emits_coverage(self):
        from app.orchestration.pipeline import SearchOutcome

        settings = Settings(
            research_max_iterations=2,
            research_max_tokens=100_000,
            research_max_time_s=30,
            research_coverage_threshold=0.0,  # never gate-block: stop on no gaps
        )

        class _Search:
            async def run(self, params, on_event=None):
                from app.retrieval.models import ScoredResult

                r = ScoredResult(
                    url="https://g.com/1",
                    canonical_url="https://g.com/1",
                    title="quasar report",
                    snippet="quasar nebula harbor",
                    domain="g.com",
                    adapters=["g1"],
                    engines=["g1"],
                    consensus=1,
                    best_rank=0,
                    score=0.5,
                )
                return SearchOutcome(query_id=uuid.uuid4(), results=[r], statuses=[], latency_ms=1)

        class _Scraper:
            async def scrape_many(self, urls, **kw):
                return []

        class _Gw:
            def __init__(self):
                self.calls = 0

            async def complete(self, messages, *, model=None, max_tokens=None, task=None):
                self.calls += 1
                from app.llm.gateway import Usage

                if task == "planner":
                    return (
                        '{"sub_queries": [{"query": "alpha beta", "purpose": "p1"},'
                        ' {"query": "gamma delta", "purpose": "p2"}], "categories": ["general"]}',
                        Usage(),
                    )
                return (
                    '{"learnings": [{"fact": "alpha beta gamma delta known", "citation_id": 1}],'
                    ' "gaps": []}',
                    Usage(),
                )

            async def stream(self, messages, *, model=None, max_tokens=None):
                class _D:
                    text = "answer [1]"
                    finish_reason = "stop"
                    usage = None

                yield _D()

        pipe = ResearchPipeline(settings, _Search(), _Scraper(), _Gw())
        events = []
        async for ev in pipe.run(ResearchParams(query="alpha beta gamma delta topic")):
            events.append(ev)
        kinds = [k for k, _ in events]
        assert "plan" in kinds and "done" in kinds
        learnings_ev = next(p for k, p in events if k == "learnings")
        assert "coverage" in learnings_ev
        done = next(p for k, p in events if k == "done")
        assert done["coverage"] == 1.0


# --- P1-5 memory ---------------------------------------------------------------


@pytest.fixture
async def v26_db(tmp_path, monkeypatch):
    db_file = tmp_path / "v26.db"
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
    key, _ = await create_key("v26_user", role="user", rate_limit_rpm=6000)
    yield key
    get_settings.cache_clear()


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class TestMemory:
    @pytest.mark.asyncio
    async def test_record_and_boosts(self, v26_db):
        from app.retrieval import memory as mem

        settings = get_settings()
        assert await mem.record_memory(
            "k1", kind="feedback", url="https://vnexpress.net/a", weight=1.0
        )
        assert await mem.record_memory(
            "k1", kind="feedback", url="https://vnexpress.net/b", weight=1.0
        )
        assert await mem.record_memory(
            "k1", kind="feedback", url="https://tuoitre.vn/c", weight=-1.0
        )
        assert await mem.record_memory("k1", kind="query", text="giá vàng hôm nay")
        rules = await mem.load_memory_boosts("k1", settings=settings)
        by_target = {r.target: r.weight for r in rules}
        assert by_target["vnexpress.net"] == pytest.approx(2 * settings.memory_feedback_boost)
        assert by_target["tuoitre.vn"] == pytest.approx(-settings.memory_feedback_boost)
        # invalid kind refused
        assert not await mem.record_memory("k1", kind="bogus", text="x")
        # delete all (privacy)
        n = await mem.delete_memory("k1")
        assert n == 4
        assert await mem.load_memory_boosts("k1", settings=settings) == []

    @pytest.mark.asyncio
    async def test_memory_boost_applies_in_merger(self, v26_db):
        from app.retrieval import memory as mem

        settings = get_settings()
        await mem.record_memory("k1", kind="feedback", url="https://good.com/x", weight=1.0)
        rules = await mem.load_memory_boosts("k1", settings=settings)
        registry = {"a": _cfg("a")}
        raws = [_raw("https://good.com/x", "a"), _raw("https://other.com/y", "a")]
        boosted = merge(raws, query="quasar", registry=registry, boosts=rules)
        plain = merge(raws, query="quasar", registry=registry)
        b = {r.canonical_url: r.score for r in boosted}
        p = {r.canonical_url: r.score for r in plain}
        assert b["https://good.com/x"] > p["https://good.com/x"]
        assert b["https://other.com/y"] == pytest.approx(p["https://other.com/y"])

    @pytest.mark.asyncio
    async def test_memory_endpoint_crud(self, v26_db):
        from app.main import create_app

        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/v1/memory",
                json={"kind": "feedback", "url": "https://a.com/1", "weight": 1},
                headers=_h(v26_db),
            )
            assert r.status_code == 201, r.text
            # feedback without url rejected
            r = await c.post("/v1/memory", json={"kind": "feedback"}, headers=_h(v26_db))
            assert r.status_code == 400
            r = await c.get("/v1/memory", headers=_h(v26_db))
            assert r.status_code == 200 and len(r.json()["items"]) == 1
            r = await c.delete("/v1/memory", headers=_h(v26_db))
            assert r.status_code == 200 and r.json()["deleted"] == 1
            r = await c.get("/v1/memory", headers=_h(v26_db))
            assert r.json()["items"] == []


# --- P1-7 map/crawl -------------------------------------------------------------


SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/a</loc></url>
<url><loc>https://example.com/b</loc></url>
<url><loc>https://evil.com/c</loc></url>
</urlset>"""

SITEMAP_INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>
</sitemapindex>"""


class _FakeScraper:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    async def _guarded_fetch(self, url, timeout_s):
        if url in self.pages:
            return self.pages[url].encode(), url
        raise RuntimeError(f"404 {url}")


class TestMapCrawl:
    def test_parse_sitemap_filters(self):
        pages, children = parse_sitemap(SITEMAP)
        assert "https://example.com/a" in pages
        assert children == []
        _pages2, children2 = parse_sitemap(SITEMAP_INDEX)
        assert children2 == ["https://example.com/sitemap-1.xml"]

    def test_parse_sitemap_garbage(self):
        assert parse_sitemap("not xml") == ([], [])

    def test_extract_links_same_host_only(self):
        html = (
            '<a href="/a">a</a><a href="https://example.com/b">b</a>'
            '<a href="https://evil.com/x">x</a><a href="mailto:a@b.c">m</a>'
            '<a href="#frag">f</a><a href="https://sub.example.com/s">s</a>'
        )
        links = extract_links(html, "https://example.com/")
        assert "https://example.com/a" in links
        assert "https://example.com/b" in links
        assert "https://sub.example.com/s" in links
        assert all("evil.com" not in lnk and "mailto" not in lnk for lnk in links)

    def test_extract_links_markdown(self):
        md = "see [docs](https://example.com/docs) and https://example.com/raw plus https://evil.com/x"
        links = extract_links(md, "https://example.com/", markdown=True)
        assert "https://example.com/docs" in links
        assert "https://example.com/raw" in links
        assert all("evil.com" not in lnk for lnk in links)

    @pytest.mark.asyncio
    async def test_map_site_prefers_sitemap(self):
        scraper = _FakeScraper(
            {
                "https://example.com/sitemap.xml": SITEMAP,
                "https://example.com/": '<a href="/extra">e</a>',
            }
        )
        result = await map_site(scraper, "https://example.com/", max_urls=10)
        assert result.source == "sitemap+html"
        assert "https://example.com/a" in result.urls
        assert "https://example.com/extra" in result.urls
        assert all("evil.com" not in u for u in result.urls)

    @pytest.mark.asyncio
    async def test_map_site_html_fallback_and_cap(self):
        scraper = _FakeScraper({"https://example.com/": '<a href="/p1">1</a><a href="/p2">2</a>'})
        result = await map_site(scraper, "https://example.com/", max_urls=1)
        assert result.source == "html"
        assert len(result.urls) == 1 and result.truncated is True


# --- P1-6 spaces ----------------------------------------------------------------


class TestSpaces:
    def test_slugify(self):
        from app.retrieval.spaces import slugify

        assert slugify("AI Agents 2026!") == "ai-agents-2026"
        assert slugify("   ") == "space"

    @pytest.mark.asyncio
    async def test_space_endpoint_crud_and_search_404(self, v26_db):
        from app.main import create_app

        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/v1/spaces",
                json={
                    "name": "VN Banking",
                    "domains": ["vnexpress.net", "cafef.vn"],
                    "pinned_urls": ["https://vnexpress.net/pinned-1"],
                    "restrict": True,
                },
                headers=_h(v26_db),
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["slug"] == "vn-banking"
            slug = body["slug"]

            r = await c.get("/v1/spaces", headers=_h(v26_db))
            assert len(r.json()["items"]) == 1

            r = await c.patch(
                f"/v1/spaces/{slug}", json={"description": "banks"}, headers=_h(v26_db)
            )
            assert r.status_code == 200 and r.json()["description"] == "banks"

            # search within a missing space -> 404
            r = await c.post(
                "/v1/search", json={"query": "test", "space": "nope"}, headers=_h(v26_db)
            )
            assert r.status_code == 404, r.text

            r = await c.delete(f"/v1/spaces/{slug}", headers=_h(v26_db))
            assert r.status_code == 200
            r = await c.get(f"/v1/spaces/{slug}", headers=_h(v26_db))
            assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_space_restrict_filters_results(self, v26_db, monkeypatch):
        from app.db.models import Space
        from app.db.session import get_sessionmaker
        from app.retrieval.spaces import invalidate

        async with get_sessionmaker()() as session:
            session.add(
                Space(
                    id=uuid.uuid4(),
                    api_key_id="v26_user_key",
                    name="Scope",
                    slug="scope",
                    domains=["good.com"],
                    restrict=True,
                )
            )
            await session.commit()
        invalidate("v26_user_key")
        monkeypatch.setattr(
            "app.orchestration.pipeline.current_api_key_id",
            type("Ctx", (), {"get": staticmethod(lambda: "v26_user_key")})(),
        )
        in_scope = _RegAdapter("g1", "general", [_raw("https://good.com/x", "g1")])
        out_scope = _RegAdapter("g2", "general", [_raw("https://other.com/y", "g2")])
        pipe = _pipeline([in_scope, out_scope])
        outcome = await pipe.run(SearchParams(query="quasar report", space="scope"))
        urls = [r.url for r in outcome.results]
        assert all("good.com" in u for u in urls)
        assert all("other.com" not in u for u in urls)

    def test_url_boost_kind(self):
        from app.retrieval.canonical import canonicalize

        item = ScoredResult(
            url="https://pin.com/a?utm_source=x",
            canonical_url=canonicalize("https://pin.com/a?utm_source=x"),
            title="t",
            snippet="s",
            domain="pin.com",
            adapters=["a"],
            engines=["a"],
            consensus=1,
            best_rank=0,
            score=0.5,
        )
        rules = [BoostRule(kind="url", target=canonicalize("https://pin.com/a"), weight=0.5)]
        applied = apply_boosts(item, "q", rules)
        assert applied == 1
        assert item.score == pytest.approx(0.5 * 1.5)
        assert rules_signature(rules)


# --- P1-3 late chunking ----------------------------------------------------------


class TestLateChunking:
    def test_segment_short_doc_single(self):
        assert segment_long_document("short doc") == ["short doc"]

    def test_segment_long_doc_overlap_and_coverage(self):
        md = "lorem ipsum dolor sit amet. " * 1200  # ~36k chars
        segs = segment_long_document(md, segment_chars=6000, overlap_chars=300)
        assert len(segs) > 3
        assert all(len(s) <= 6000 for s in segs)
        # windows overlap: end of seg i appears in seg i+1
        assert segs[0][-200:] in segs[1] or segs[0][-100:] in segs[1]
        # full coverage: concatenation minus overlaps reconstructs the doc
        rebuilt = segs[0] + "".join(s[300:] for s in segs[1:])
        assert rebuilt.replace(" ", "")[:100] == md.replace(" ", "")[:100]

    def test_late_chunk_short_equals_normal(self):
        md = "# Title\n\nparagraph one.\n\nparagraph two."
        a = late_chunk_markdown(md, document_url="https://x.com/a", source_title="t")
        b = chunk_markdown(md, document_url="https://x.com/a", source_title="t")
        assert [c.content for c in a] == [c.content for c in b]

    def test_late_chunk_long_doc_positions_monotonic(self):
        md = "# H\n\n" + ("word " * 9000)  # > 24k chars
        chunks = late_chunk_markdown(md, document_url="https://x.com/big", source_title="big")
        assert len(chunks) > 1
        positions = [c.position for c in chunks]
        assert positions == sorted(positions)
        assert len({c.id for c in chunks}) == len(chunks)

    @pytest.mark.asyncio
    async def test_embed_many_batch(self, monkeypatch):
        settings = Settings(embedding_l2="bge-m3-api")

        def handler(request: httpx.Request) -> httpx.Response:
            payload = httpx.Request  # noqa
            import json as _json

            body = _json.loads(request.content.decode())
            inputs = body["input"]
            assert body["model"] == settings.embedding_model
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [float(i)] * 1024} for i in range(len(inputs))
                    ]
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        emb = EmbeddingClient(settings, client=client)
        vecs = await emb.embed_many(["a", "b", "c"])
        assert len(vecs) == 3 and len(vecs[0]) == 1024
        assert vecs[1][0] == 1.0

    @pytest.mark.asyncio
    async def test_embed_many_dim_mismatch(self):
        settings = Settings(embedding_l2="bge-m3-api")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        emb = EmbeddingClient(settings, client=client)
        with pytest.raises(EmbeddingError):
            await emb.embed_many(["a"])


# --- P1-4 hybrid -----------------------------------------------------------------


class TestHybrid:
    def test_bm25_ranks_relevant_first(self):
        docs = [
            "the quick brown fox jumps over the lazy dog",
            "quantum entanglement and qubit coherence in quantum computing",
            "quantum computing uses qubits and entanglement for speedup",
        ]
        scores = bm25_scores("quantum entanglement qubit", docs)
        assert scores[2] > scores[0] and scores[1] > scores[0]

    def test_rrf_fuse_dense_missing(self):
        fused = rrf_fuse([], [3.0, 1.0, 2.0])
        assert fused[0] > fused[2] > fused[1]

    def test_rrf_fuse_both_sides(self):
        fused = rrf_fuse([0.1, 0.9, 0.8], [0.1, 0.9, 0.8])
        # doc 1 is top-2 on both lists -> strictly highest rrf
        assert fused[1] > fused[0] and fused[1] > fused[2]

    @pytest.mark.asyncio
    async def test_hybrid_chunk_search_sqlite(self, v26_db):
        from app.db.models import Chunk as ChunkRow
        from app.db.models import Document
        from app.db.session import get_sessionmaker

        async with get_sessionmaker()() as session:
            doc = Document(
                id=uuid.uuid4(),
                canonical_url="https://docs.example.com/page",
                url_hash="h1",
                title="Docs",
                domain="docs.example.com",
                content_md="quantum qubit",
            )
            session.add(doc)
            await session.flush()
            session.add(
                ChunkRow(
                    id=uuid.uuid4(),
                    document_id=doc.id,
                    position=1,
                    content="quantum entanglement uses qubits for computation",
                    token_count=6,
                    tsv="quantum entanglement uses qubits for computation",
                    embedding=[1.0] + [0.0] * 1023,
                )
            )
            session.add(
                ChunkRow(
                    id=uuid.uuid4(),
                    document_id=doc.id,
                    position=2,
                    content="the kitchen recipe for soup",
                    token_count=5,
                    tsv="the kitchen recipe for soup",
                    embedding=[0.0, 1.0] + [0.0] * 1022,
                )
            )
            await session.commit()

            async with get_sessionmaker()() as session2:
                out = await hybrid_chunk_search(
                    session2, "quantum qubit entanglement", k=2, embedding=[1.0] + [0.0] * 1023
                )
            assert out[0]["position"] == 1
            assert out[0]["dense"] == pytest.approx(1.0)
            assert out[0]["sparse"] > out[1]["sparse"]

            # domain filter
            async with get_sessionmaker()() as session3:
                out2 = await hybrid_chunk_search(session3, "quantum", k=2, domains=["other.com"])
            assert out2 == []
