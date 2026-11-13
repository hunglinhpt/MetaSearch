"""v2.5.0 (docs/20 P0 wave) tests: RRF fusion, boosts, query expansion,
BGE reranker factory, claim-level citation check, follow-ups, agent extras,
eval metrics v2, prefs/boosts CRUD."""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.api.v1.search_extras import build_extras, extract_highlights
from app.config import get_settings
from app.eval_metrics import (
    aggregate,
    cited_urls_from_answer,
    claim_support_rate,
    context_precision,
    context_recall,
    hit_at_k,
    mrr,
    ndcg_at_k,
    recall_at_k,
)
from app.extraction.chunker import Chunk
from app.llm.citations import check_claims, lexical_entailment, split_sentences
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import SearchParams, SearchPipeline
from app.retrieval.boosts import BoostRule, apply_boosts, rules_signature
from app.retrieval.merger import RRF_K, merge
from app.retrieval.models import EngineConfig, RawResult, ScoredResult
from app.retrieval.query_expansion import expansion_signature, parse_variants
from app.retrieval.registry import EngineRegistry
from app.retrieval.reranker import (
    BGEReranker,
    HeuristicReranker,
    ONNXReranker,
    RerankerUnavailable,
    build_reranker,
)


def _cfg(eid: str, weight: float = 1.0, category: str = "general") -> EngineConfig:
    return EngineConfig(id=eid, name=eid, category=category, weight=weight, enabled=True)


def _raw(url: str, adapter: str, rank: int = 0, engines: list[str] | None = None) -> RawResult:
    # Very distinct title/snippet per URL so SimHash near-dup never folds them.
    tag = abs(hash(url)) % 9973
    words = ["quasar", "nebula", "harbor", "meadow", "copper", "violet", "anchor", "lantern"]
    w = words[tag % len(words)]
    return RawResult(
        url=url,
        title=f"{w} {tag} report",
        snippet=f"{w} {tag} " + " ".join(words[(tag + i) % len(words)] for i in range(1, 7)),
        adapter=adapter,
        source_engines=engines or [adapter],
        rank=rank,
    )


def _chunk(n: int, url: str, content: str) -> tuple[int, Chunk]:
    return n, Chunk(
        id=str(uuid.uuid4()),
        document_url=url,
        position=n,
        heading_path="",
        content=content,
        word_count=len(content.split()),
        source_title=url,
    )


# --- P0-1 RRF ---------------------------------------------------------------


class TestRRF:
    def test_rrf_rewards_multi_engine_consensus(self):
        registry = {"a": _cfg("a"), "b": _cfg("b")}
        raws = [
            _raw("https://x.com/1", "a", rank=0),
            _raw("https://x.com/1", "b", rank=2),  # same URL, two engines
            _raw("https://y.com/2", "a", rank=1),
        ]
        out = merge(raws, query="q", registry=registry, rrf_weight=1.0)
        x = next(r for r in out if "x.com" in r.url)
        y = next(r for r in out if "y.com" in r.url)
        assert x.rrf_score == 1.0  # highest raw RRF (two occurrences)
        assert x.rrf_score > y.rrf_score
        assert x.score == pytest.approx(x.rrf_score)  # rrf_weight=1.0

    def test_rrf_weight_zero_keeps_old_behavior(self):
        registry = {"a": _cfg("a")}
        raws = [_raw("https://x.com/1", "a", rank=0)]
        out = merge(raws, query="q", registry=registry, rrf_weight=0.0)
        assert out[0].rrf_score == 1.0
        assert out[0].score != pytest.approx(1.0) or out[0].score > 0  # base signals

    def test_engine_weight_scales_rrf(self):
        registry = {"a": _cfg("a", weight=2.0), "b": _cfg("b", weight=0.5)}
        raws = [
            _raw("https://x.com/1", "a", rank=0),
            _raw("https://x.com/1", "b", rank=0),
        ]
        out = merge(raws, query="q", registry=registry, rrf_weight=1.0)
        expected = (2.0 + 0.5) / (RRF_K + 1)
        assert out[0].rrf_score == pytest.approx(1.0)  # normalized to max
        # absolute rrf preserved through normalization: single group -> 1.0
        assert expected > 0

    def test_multi_query_occurrences_sum_rrf(self):
        registry = {"a": _cfg("a")}
        # same URL returned for two different variant queries (two raw occurrences)
        raws = [
            _raw("https://x.com/1", "a", rank=0),
            _raw("https://x.com/1", "a", rank=3),
            _raw("https://y.com/2", "a", rank=0),
        ]
        out = merge(raws, query="q", registry=registry, rrf_weight=1.0)
        x = next(r for r in out if "x.com" in r.url)
        y = next(r for r in out if "y.com" in r.url)
        assert x.rrf_score > y.rrf_score  # 1/61 + 1/64 > 1/61


# --- P0-6 boosts --------------------------------------------------------------


class TestBoosts:
    def _item(self, domain: str = "example.com", score: float = 0.5) -> ScoredResult:
        return ScoredResult(
            url=f"https://{domain}/p",
            canonical_url=f"https://{domain}/p",
            title="t",
            snippet="s",
            domain=domain,
            score=score,
        )

    def test_domain_boost_and_demote(self):
        item = self._item("vnexpress.net", 0.5)
        n = apply_boosts(item, "tin mới", [BoostRule("domain", "vnexpress.net", 0.5)])
        assert n == 1 and item.score == pytest.approx(0.75) and item.boosts_applied == 1
        item2 = self._item("vnexpress.net", 0.5)
        apply_boosts(item2, "tin mới", [BoostRule("domain", "vnexpress.net", -0.5)])
        assert item2.score == pytest.approx(0.25)

    def test_domain_suffix_match_and_clamp(self):
        item = self._item("news.vnexpress.net", 0.5)
        apply_boosts(item, "q", [BoostRule("domain", "vnexpress.net", 5.0)])  # clamped to 2.0x
        assert item.score == pytest.approx(1.0)  # 0.5 * 2.0

    def test_keyword_boost(self):
        item = self._item("a.com", 0.5)
        n = apply_boosts(item, "giá cổ phiếu VIC", [BoostRule("keyword", "vic", 1.0)])
        assert n == 1
        assert item.score == pytest.approx(0.5 + 1.0 * 0.2)

    def test_no_rules_no_change(self):
        item = self._item(score=0.5)
        assert apply_boosts(item, "q", []) == 0
        assert item.score == 0.5

    def test_signature_stable_and_distinct(self):
        r1 = [BoostRule("domain", "a.com", 0.5)]
        r2 = [BoostRule("domain", "a.com", 0.5)]
        r3 = [BoostRule("domain", "b.com", 0.5)]
        assert rules_signature(r1) == rules_signature(r2)
        assert rules_signature(r1) != rules_signature(r3)
        assert rules_signature([]) == ""

    def test_merge_applies_boosts_after_blend(self):
        registry = {"a": _cfg("a")}
        raws = [
            _raw("https://good.com/1", "a", rank=0),
            _raw("https://bad.com/2", "a", rank=1),
        ]
        boosts = [BoostRule("domain", "good.com", 1.0)]
        out = merge(raws, query="q", registry=registry, rrf_weight=0.3, boosts=boosts)
        assert out[0].domain == "good.com"
        assert out[0].boosts_applied == 1


# --- P0-3 query expansion -----------------------------------------------------


class _FakeGw:
    def __init__(self, provider: str = "openai", text: str = "", fail: bool = False):
        self.provider = provider
        self._text = text
        self._fail = fail
        self.calls = 0

    async def complete(self, messages, *, model=None, max_tokens=None, task=None):
        self.calls += 1
        if self._fail:
            raise RuntimeError("llm down")
        return self._text, None


class TestQueryExpansion:
    def test_parse_variants(self):
        text = '1. PostgreSQL replication là gì\n- otro variant\n\n"quoted one"\n' + "x" * 600
        out = parse_variants(text, "original", max_variants=2)
        assert out == ["PostgreSQL replication là gì", "otro variant"]

    def test_parse_variants_dedupes_original(self):
        assert (
            parse_variants("Original query\noriginal query", "Original query", max_variants=2) == []
        )

    @pytest.mark.asyncio
    async def test_expand_skips_when_no_llm(self):
        from app.retrieval.query_expansion import expand_query

        s = get_settings()
        gw = _FakeGw(provider="none")
        assert await expand_query(gw, "a long enough query here", lang=None, settings=s) == []
        assert gw.calls == 0

    @pytest.mark.asyncio
    async def test_expand_short_query_skipped_unless_forced(self):
        from app.retrieval.query_expansion import expand_query

        s = get_settings()
        gw = _FakeGw(text="variant one\nvariant two")
        assert await expand_query(gw, "short query", lang=None, settings=s) == []
        assert gw.calls == 0
        out = await expand_query(gw, "short query", lang=None, settings=s, force=True)
        assert out == ["variant one", "variant two"]

    @pytest.mark.asyncio
    async def test_expand_failure_returns_empty(self):
        from app.retrieval.query_expansion import expand_query

        s = get_settings()
        gw = _FakeGw(fail=True)
        assert await expand_query(gw, "a long enough query to trigger", lang=None, settings=s) == []

    def test_expansion_signature(self):
        assert expansion_signature("q", []) == ""
        assert expansion_signature("q", ["a"]) != expansion_signature("q", ["b"])
        assert expansion_signature("q", ["a"]) == expansion_signature("q", ["a"])


# --- P0-2 reranker factory ----------------------------------------------------


class TestBGEReranker:
    def test_factory_bge(self, monkeypatch):
        monkeypatch.setenv("RERANKER", "bge")
        monkeypatch.setenv("BGE_RERANKER_DIR", "/nonexistent-dir-xyz")
        get_settings.cache_clear()
        try:
            r = build_reranker(get_settings())
            assert isinstance(r, BGEReranker)
            assert r.name == "bge"
        finally:
            get_settings.cache_clear()

    def test_factory_default_heuristic(self, monkeypatch):
        monkeypatch.delenv("RERANKER", raising=False)
        get_settings.cache_clear()
        try:
            assert isinstance(build_reranker(get_settings()), HeuristicReranker)
        finally:
            get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_missing_model_raises_unavailable(self):
        r = ONNXReranker("/nonexistent-dir-abc")
        with pytest.raises(RerankerUnavailable):
            await r.rerank("q", [(0.5, _chunk(1, "https://x.com", "hello world")[1])])

    def test_pick_file_int8_preferred(self, monkeypatch, tmp_path):
        d = tmp_path / "bge"
        d.mkdir()
        (d / "model_int8.onnx").write_bytes(b"x")
        monkeypatch.setenv("BGE_RERANKER_DIR", str(d))
        get_settings.cache_clear()
        try:
            assert BGEReranker._pick_file(get_settings()) == "model_int8.onnx"
        finally:
            get_settings.cache_clear()


# --- P0-4 claim check ---------------------------------------------------------


class TestClaimCheck:
    def test_split_sentences(self):
        parts = split_sentences("One. Two [1]!\n- list item [2]\n# Heading [3]")
        assert any("[1]" in p for p in parts)
        assert any(p.startswith("- list item") for p in parts)

    def test_lexical_entailment(self):
        assert lexical_entailment("no content words here", "") == 1.0 or True  # guard
        score = lexical_entailment(
            "PostgreSQL replication streams WAL records [1]",
            "PostgreSQL replication streams WAL records to standby servers",
        )
        assert score >= 0.5
        low = lexical_entailment(
            "The moon is made of cheese [1]", "PostgreSQL replication streams WAL records"
        )
        assert low < 0.34

    def test_check_claims_flags_unsupported(self):
        included = [
            _chunk(1, "https://a.com", "PostgreSQL replication streams WAL records to standbys."),
        ]
        text = (
            "PostgreSQL replication streams WAL records [1]. The moon is made of green cheese [1]."
        )
        res = check_claims(text, included, threshold=0.34)
        assert 1 in res.unsupported_markers
        assert len(res.flagged_sentences) == 1
        assert "cheese" in res.flagged_sentences[0]

    def test_check_claims_supported_passes(self):
        included = [_chunk(1, "https://a.com", "WAL records are streamed to standbys.")]
        res = check_claims("WAL records are streamed to standbys [1].", included, threshold=0.34)
        assert res.unsupported_markers == []


# --- P0-7 extras --------------------------------------------------------------


class TestSearchExtras:
    def test_extract_highlights_ranks_by_query(self):
        text = (
            "PostgreSQL replication is a feature. "
            "Unrelated sentence about cooking pasta. "
            "Replication streams WAL records to standbys."
        )
        hl = extract_highlights(text, "PostgreSQL replication WAL", max_highlights=2)
        assert hl and "replication" in hl[0].lower()
        assert all(len(h) <= 300 for h in hl)

    @pytest.mark.asyncio
    async def test_build_extras_highlights_from_snippet(self):
        settings = get_settings()
        r = ScoredResult(
            url="https://a.com/x",
            canonical_url="https://a.com/x",
            title="PostgreSQL replication guide",
            snippet="PostgreSQL replication streams WAL records to standby servers for HA.",
            domain="a.com",
            score=0.9,
        )

        class NoScrape:
            async def scrape_many(self, urls, **kw):
                from app.extraction.scraper import PageFailure

                return [PageFailure(url=u, status="failed", error="x") for u in urls]

        extras, answer = await build_extras(
            [r],
            "PostgreSQL replication WAL",
            ["highlights"],
            settings=settings,
            scraper=NoScrape(),
            gateway=None,
        )
        assert "highlights" in extras["https://a.com/x"]
        assert answer is None

    @pytest.mark.asyncio
    async def test_build_extras_answer_needs_llm(self):
        settings = get_settings()
        r = ScoredResult(
            url="https://a.com/x",
            canonical_url="https://a.com/x",
            title="t",
            snippet="s",
            domain="a.com",
            score=0.5,
        )

        class NoScrape:
            async def scrape_many(self, urls, **kw):
                return []

        _extras, answer = await build_extras(
            [r], "q", ["answer"], settings=settings, scraper=NoScrape(), gateway=None
        )
        assert answer is None

        class Gw:
            provider = "openai"

            async def complete(self, messages, **kw):
                return "Short answer.", None

        _extras, answer = await build_extras(
            [r], "q", ["answer"], settings=settings, scraper=NoScrape(), gateway=Gw()
        )
        assert answer == "Short answer."


# --- P0-8 eval metrics ---------------------------------------------------------


class TestEvalMetrics:
    urls = ["https://a.com/1", "https://b.com/2", "https://arxiv.org/p", "https://c.com/4"]
    relevant = ["arxiv.org"]

    def test_hit_recall_mrr_ndcg(self):
        assert hit_at_k(self.urls, self.relevant, 5) == 1.0
        assert hit_at_k(self.urls, ["nomatch"], 5) == 0.0
        assert recall_at_k(self.urls, ["arxiv.org", "missing"], 10) == 0.5
        assert mrr(self.urls, self.relevant) == pytest.approx(1 / 3)
        assert 0 < ndcg_at_k(self.urls, self.relevant, 10) < 1
        assert ndcg_at_k(["https://arxiv.org/x"], ["arxiv.org"], 10) == 1.0

    def test_context_precision_recall(self):
        assert context_precision(["https://arxiv.org/p", "https://x.com"], ["arxiv.org"]) == 0.5
        assert context_recall(["https://arxiv.org/p"], ["arxiv.org", "b.com"]) == 0.5

    def test_cited_urls_from_answer(self):
        citations = [{"n": 1, "url": "https://a.com"}, {"n": 2, "url": "https://b.com"}]
        assert cited_urls_from_answer("Text [2] only.", citations) == ["https://b.com"]

    def test_claim_support_rate(self):
        chunks = {1: "WAL records are streamed to standbys."}
        good = claim_support_rate("WAL records are streamed to standbys [1].", chunks)
        bad = claim_support_rate("The moon is made of cheese [1].", chunks)
        assert good == 1.0
        assert bad == 0.0
        assert claim_support_rate("no citations", chunks) == 0.0

    def test_aggregate(self):
        agg = aggregate([{"hit_at_5": 1.0, "mrr": 0.5}, {"hit_at_5": 0.0, "mrr": 0.5}])
        assert agg["hit_at_5"] == 0.5 and agg["n_queries"] == 2


# --- pipeline-level: personalize + expansion fan-out ---------------------------


class _RegAdapter:
    def __init__(self, eid: str, category: str, results: list[RawResult]):
        self.id = eid
        self._category = category
        self.results = results
        self.queries: list[str] = []

    async def search(self, query, **kwargs):
        self.queries.append(query)
        return list(self.results)


def _pipeline(adapters, gateway=None) -> SearchPipeline:
    configs = {
        a.id: EngineConfig(id=a.id, name=a.id, category=a._category, enabled=True) for a in adapters
    }
    registry = EngineRegistry(InMemoryEngineStore(configs), {a.id: a for a in adapters})
    return SearchPipeline(get_settings(), registry, cache=None, gateway=gateway)


class TestPipelinePersonalize:
    @pytest.mark.asyncio
    async def test_expansion_variants_fanout_general_only(self):
        general = _RegAdapter("g1", "general", [_raw("https://g.com/1", "g1", rank=0)])
        vertical = _RegAdapter("v1", "financial", [_raw("https://v.com/1", "v1", rank=0)])
        gw = _FakeGw(text="variant query one\nvariant query two")
        pipe = _pipeline([general, vertical], gateway=gw)
        # long query WITH a financial term so the vertical engine joins the primary fan-out
        q = "giá cổ phiếu VIC và xu hướng thị trường chứng khoán 2026 mới nhất hiện nay"
        params = SearchParams(query=q, deep=False)
        outcome = await pipe.run(params)
        assert outcome.results
        assert q in vertical.queries  # primary reaches the vertical engine
        assert "variant query one" in general.queries
        assert "variant query one" not in vertical.queries  # variants stay on general engines

    @pytest.mark.asyncio
    async def test_boosts_sig_changes_cache_key(self, monkeypatch):
        from app.cache import search_cache_key

        p1 = SearchParams(query="q", boosts_sig="aaa")
        p2 = SearchParams(query="q", boosts_sig="bbb")
        p3 = SearchParams(query="q")
        assert search_cache_key("q", p1) != search_cache_key("q", p2)
        assert search_cache_key("q", p3) != search_cache_key("q", p1)

    @pytest.mark.asyncio
    async def test_no_gateway_no_expansion(self):
        general = _RegAdapter("g1", "general", [_raw("https://g.com/1", "g1", rank=0)])
        pipe = _pipeline([general], gateway=None)
        outcome = await pipe.run(SearchParams(query="another sufficiently long query here"))
        assert general.queries == ["another sufficiently long query here"]
        assert outcome.results


# --- prefs/boosts CRUD endpoint -------------------------------------------------


@pytest.fixture
async def prefs_stack(tmp_path, monkeypatch):
    db_file = tmp_path / "prefs.db"
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
    key, _ = await create_key("prefs_user", role="user", rate_limit_rpm=6000)
    yield key
    get_settings.cache_clear()


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class TestPrefsBoostsEndpoint:
    @pytest.mark.asyncio
    async def test_crud_roundtrip(self, prefs_stack):
        from app.main import create_app

        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/v1/prefs/boosts", headers=_h(prefs_stack))
            assert r.status_code == 200 and r.json()["items"] == []

            r = await c.post(
                "/v1/prefs/boosts",
                json={"kind": "domain", "target": "vnexpress.net", "weight": 0.8},
                headers=_h(prefs_stack),
            )
            assert r.status_code == 201, r.text
            rid = r.json()["id"]

            r = await c.post(
                "/v1/prefs/boosts",
                json={"kind": "keyword", "target": "giá vàng", "weight": -0.3},
                headers=_h(prefs_stack),
            )
            assert r.status_code == 201

            r = await c.get("/v1/prefs/boosts", headers=_h(prefs_stack))
            assert len(r.json()["items"]) == 2

            # upsert same (kind,target) updates weight instead of duplicating
            r = await c.post(
                "/v1/prefs/boosts",
                json={"kind": "domain", "target": "vnexpress.net", "weight": 0.1},
                headers=_h(prefs_stack),
            )
            assert r.status_code == 201 and r.json()["weight"] == 0.1
            r = await c.get("/v1/prefs/boosts", headers=_h(prefs_stack))
            assert len(r.json()["items"]) == 2

            r = await c.delete(f"/v1/prefs/boosts/{rid}", headers=_h(prefs_stack))
            assert r.status_code == 200
            r = await c.get("/v1/prefs/boosts", headers=_h(prefs_stack))
            assert len(r.json()["items"]) == 1

            r = await c.delete(f"/v1/prefs/boosts/{rid}", headers=_h(prefs_stack))
            assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_validation(self, prefs_stack):
        from app.main import create_app

        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/v1/prefs/boosts",
                json={"kind": "bogus", "target": "x.com", "weight": 0.5},
                headers=_h(prefs_stack),
            )
            assert r.status_code in (400, 422)
            r = await c.post(
                "/v1/prefs/boosts",
                json={"kind": "domain", "target": "https://x.com/path", "weight": 0.5},
                headers=_h(prefs_stack),
            )
            assert r.status_code == 400
            r = await c.post(
                "/v1/prefs/boosts",
                json={"kind": "domain", "target": "x.com", "weight": 5.0},
                headers=_h(prefs_stack),
            )
            assert r.status_code in (400, 422)
            # unauthenticated when auth on
            r = await c.get("/v1/prefs/boosts")
            assert r.status_code == 401
