"""v2.4.0 global deep search (docs/19): 7 keyless adapters + searxng category
mapping + deep fan-out + category inference + google_news lang locales."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.orchestration.engine_store import InMemoryEngineStore
from app.orchestration.pipeline import (
    SearchParams,
    SearchPipeline,
    infer_categories_from_query,
)
from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.bluesky import BlueskyAdapter
from app.retrieval.adapters.crossref import CrossrefAdapter
from app.retrieval.adapters.europepmc import EuropePmcAdapter
from app.retrieval.adapters.gdelt import GdeltAdapter
from app.retrieval.adapters.hackernews import HackerNewsAdapter
from app.retrieval.adapters.openalex import OpenAlexAdapter
from app.retrieval.adapters.searxng import map_categories
from app.retrieval.adapters.stackexchange import StackExchangeAdapter
from app.retrieval.health import Outcome
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.registry import EngineRegistry

NEW_GLOBAL_IDS = {
    "hackernews",
    "stackexchange",
    "bluesky",
    "openalex",
    "crossref",
    "europepmc",
    "gdelt",
}


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def json_resp(payload, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, text=json.dumps(payload), headers={"Content-Type": "application/json"}
    )


# --- Hacker News -------------------------------------------------------------


class TestHackerNews:
    async def test_parses_hits_and_freshness_filter(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return json_resp(
                {
                    "hits": [
                        {
                            "objectID": "1",
                            "title": "Show HN: Global thing",
                            "url": "https://example.com/hn",
                            "points": 42,
                            "num_comments": 7,
                            "author": "pg",
                            "created_at": "2026-11-12T10:00:00.000Z",
                        },
                        {"objectID": "2", "title": None, "url": None},  # skipped
                        {
                            "objectID": "3",
                            "title": "No external url",
                            "created_at": "bad-date",
                        },  # falls back to item page
                    ]
                }
            )

        ad = HackerNewsAdapter(client=mock_client(handler))
        out = await ad.search("global", freshness_days=7, max_results=10)
        assert seen["tags"] == "story"
        assert seen["numericFilters"].startswith("created_at_i>")
        assert len(out) == 2
        assert out[0].url == "https://example.com/hn"
        assert out[0].published_at is not None and out[0].published_at.year == 2026
        assert "42 points" in out[0].snippet
        assert out[1].url == "https://news.ycombinator.com/item?id=3"
        assert out[1].published_at is None  # bad date -> None, never fabricated

    async def test_empty_query_returns_empty(self):
        ad = HackerNewsAdapter(client=mock_client(lambda r: json_resp({"hits": []})))
        assert await ad.search("   ") == []


# --- Stack Exchange ----------------------------------------------------------


class TestStackExchange:
    async def test_site_extraction_and_fromdate(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return json_resp(
                {
                    "items": [
                        {
                            "title": "How to trade ETF?",
                            "link": "https://quant.stackexchange.com/q/1",
                            "score": 9,
                            "answer_count": 2,
                            "tags": ["etf"],
                            "creation_date": 1762000000,
                        }
                    ]
                }
            )

        ad = StackExchangeAdapter(client=mock_client(handler))
        out = await ad.search("site:quant.stackexchange how to trade etf", freshness_days=30)
        assert seen["site"] == "quant.stackexchange"
        assert "site:" not in seen["q"]
        assert "fromdate" in seen
        assert out[0].source_engines == ["stackexchange-quant.stackexchange"]
        assert out[0].published_at is not None
        assert "[etf]" in out[0].snippet

    async def test_site_param_is_charset_safe(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            assert request.url.host == "api.stackexchange.com"  # host never user-controlled
            return json_resp({"items": []})

        ad = StackExchangeAdapter(client=mock_client(handler))
        await ad.search("site:evil/../../x q")
        # regex captures only [a-z0-9.] — no "/", "?", "&" can reach the param
        import re as _re

        assert _re.fullmatch(r"[a-z0-9.]+", seen["site"])
        assert "/" not in seen["site"]


# --- Bluesky -----------------------------------------------------------------


class TestBluesky:
    async def test_parse_posts_and_since(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return json_resp(
                {
                    "posts": [
                        {
                            "uri": "at://did:plc:abc/app.bsky.feed.post/3k2",
                            "author": {"handle": "alice.bsky.social"},
                            "record": {
                                "text": "hello global markets",
                                "createdAt": "2026-11-12T08:00:00Z",
                            },
                        },
                        {
                            "uri": "at://did:plc:abc/app.bsky.feed.post/3k3",
                            "author": {"handle": "b"},
                            "record": {},
                        },
                    ]
                }
            )

        ad = BlueskyAdapter(client=mock_client(handler))
        out = await ad.search("markets", freshness_days=1)
        assert seen["q"] == "markets"
        assert "since" in seen and seen["since"].endswith("Z")
        assert len(out) == 1  # empty text skipped
        assert out[0].url == "https://bsky.app/profile/alice.bsky.social/post/3k2"
        assert out[0].title == "@alice.bsky.social on Bluesky"
        assert out[0].published_at is not None


# --- OpenAlex ----------------------------------------------------------------


class TestOpenAlex:
    async def test_lang_filter_and_mailto(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return json_resp(
                {
                    "results": [
                        {
                            "id": "https://openalex.org/W1",
                            "doi": "https://doi.org/10.1234/x",
                            "title": "Global fund flows paper",
                            "publication_date": "2026-10-01",
                            "publication_year": 2026,
                            "cited_by_count": 12,
                            "authorships": [{"author": {"display_name": "A. Researcher"}}],
                            "primary_location": {"source": {"display_name": "Journal of Funds"}},
                        }
                    ]
                }
            )

        ad = OpenAlexAdapter(mailto="team@example.com", client=mock_client(handler))
        out = await ad.search("fund flows", lang="en", freshness_days=90, max_results=5)
        assert seen["mailto"] == "team@example.com"
        assert "language:en" in seen["filter"]
        assert "from_publication_date:" in seen["filter"]
        assert out[0].url == "https://doi.org/10.1234/x"
        assert out[0].published_at is not None and out[0].published_at.month == 10
        assert "Journal of Funds" in out[0].snippet

    async def test_no_lang_no_language_filter(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return json_resp({"results": []})

        ad = OpenAlexAdapter(client=mock_client(handler))
        await ad.search("x")
        assert "filter" not in seen


# --- Crossref ----------------------------------------------------------------


class TestCrossref:
    async def test_parse_and_filters(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return json_resp(
                {
                    "message": {
                        "items": [
                            {
                                "DOI": "10.1234/ABC",
                                "title": ["Institutional investors study"],
                                "author": [{"given": "Jane", "family": "Doe"}],
                                "container-title": ["Review of Funds"],
                                "issued": {"date-parts": [[2026, 9, 15]]},
                                "type": "journal-article",
                            }
                        ]
                    }
                }
            )

        ad = CrossrefAdapter(mailto="team@example.com", client=mock_client(handler))
        out = await ad.search("institutional investors", freshness_days=365)
        assert seen["query.bibliographic"] == "institutional investors"
        assert "from-issued-date:" in seen["filter"]
        assert seen["mailto"] == "team@example.com"
        assert out[0].url == "https://doi.org/10.1234/abc"  # DOI lowercased
        assert out[0].published_at is not None and out[0].published_at.day == 15
        assert "Review of Funds" in out[0].snippet


# --- Europe PMC --------------------------------------------------------------


class TestEuropePmc:
    async def test_query_and_url_priority(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return json_resp(
                {
                    "resultList": {
                        "result": [
                            {
                                "title": "CRISPR trial",
                                "pmcid": "PMC123",
                                "doi": "10.1/x",
                                "pmid": "999",
                                "pubYear": "2026",
                                "journalTitle": "Nature",
                                "firstPublicationDate": "2026-11-01",
                                "openAccessFlag": "Y",
                            },
                            {"title": "No ids"},  # skipped
                        ]
                    }
                }
            )

        ad = EuropePmcAdapter(client=mock_client(handler))
        out = await ad.search("crispr", freshness_days=30)
        assert "FIRST_PDATE" in seen["query"]
        assert len(out) == 1
        assert out[0].url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC123/"  # pmcid wins
        assert "open access" in out[0].snippet


# --- GDELT -------------------------------------------------------------------


class TestGdelt:
    async def test_sourcelang_and_time_window(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return json_resp(
                {
                    "articles": [
                        {
                            "title": "Fed holds rates",
                            "url": "https://reuters.com/fed",
                            "domain": "reuters.com",
                            "language": "English",
                            "seendate": "20261112143000",
                        },
                        {"title": "no url"},  # skipped
                    ]
                }
            )

        ad = GdeltAdapter(client=mock_client(handler))
        out = await ad.search("federal reserve", lang="en", freshness_days=1, max_results=10)
        assert "sourcelang:english" in seen["query"]
        assert seen["startdatetime"] < seen["enddatetime"]
        assert seen["mode"] == "artlist"
        assert len(out) == 1
        assert out[0].published_at is not None and out[0].published_at.hour == 14
        assert "reuters.com" in out[0].source_engines

    async def test_vi_lang_maps_vietnamese(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return json_resp({"articles": []})

        ad = GdeltAdapter(client=mock_client(handler))
        await ad.search("kinh tế", lang="vi")
        assert "sourcelang:vietnamese" in seen["query"]


# --- fetch_json error mapping -------------------------------------------------


class TestFetchJsonErrors:
    async def test_status_mapping(self):
        from app.retrieval.adapters._http import fetch_json

        for status, kind, retryable in [
            (429, Outcome.RATE_LIMITED, True),
            (403, Outcome.BLOCKED, False),
            (500, Outcome.UPSTREAM_ERROR, False),
        ]:
            ad_client = mock_client(lambda r, s=status: httpx.Response(s, text="x"))
            with pytest.raises(AdapterError) as ei:
                await fetch_json("t", "https://x.example/", client=ad_client)
            assert ei.value.kind == kind
            assert ei.value.retryable == retryable

    async def test_timeout_kind(self):
        from app.retrieval.adapters._http import fetch_json

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(AdapterError) as ei:
            await fetch_json("t", "https://x.example/", client=mock_client(handler), timeout_s=0.1)
        assert ei.value.kind == Outcome.TIMEOUT

    async def test_non_json(self):
        from app.retrieval.adapters._http import fetch_json

        ad_client = mock_client(lambda r: httpx.Response(200, text="<html>not json</html>"))
        with pytest.raises(AdapterError):
            await fetch_json("t", "https://x.example/", client=ad_client)


# --- SearXNG category mapping -------------------------------------------------


class TestSearxngCategoryMap:
    def test_known_and_unknown(self):
        assert map_categories(["general"]) == ["general"]
        assert map_categories(["news"]) == ["news"]
        assert map_categories(["social"]) == ["social media"]
        assert map_categories(["developer"]) == ["it"]
        assert map_categories(["academic"]) == ["science"]
        # internal ids searxng does not know fall back to general, deduped
        assert map_categories(["financial", "fengshui", "reference"]) == ["general"]
        assert map_categories(["general", "news", "social"]) == ["general", "news", "social media"]
        assert map_categories(None) == ["general"]
        assert map_categories([]) == ["general"]

    async def test_adapter_sends_mapped_categories(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return json_resp({"results": []})

        from app.retrieval.adapters.searxng import SearXNGAdapter

        ad = SearXNGAdapter("http://searxng.local", client=mock_client(handler))
        await ad.search("etf flows", categories=["general", "financial"])
        assert seen["categories"] == "general"  # financial mapped away, not sent raw


# --- google_news lang locale ---------------------------------------------------


class TestGoogleNewsLang:
    async def test_explicit_en_wins(self, monkeypatch):
        import app.retrieval.adapters.google_news as gn

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return httpx.Response(
                200,
                text='<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>',
                headers={"Content-Type": "application/rss+xml"},
            )

        monkeypatch.setattr(gn, "get_http_client", lambda *a, **kw: mock_client(handler))
        ad = gn.GoogleNewsAdapter()
        await ad.search("việt nam chứng khoán", lang="en")  # explicit en beats VI text
        assert seen["hl"] == "en-US" and seen["gl"] == "EN" and seen["ceid"] == "EN:en"

    async def test_heuristic_vi_default(self, monkeypatch):
        import app.retrieval.adapters.google_news as gn

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return httpx.Response(
                200,
                text='<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>',
                headers={"Content-Type": "application/rss+xml"},
            )

        monkeypatch.setattr(gn, "get_http_client", lambda *a, **kw: mock_client(handler))
        ad = gn.GoogleNewsAdapter()
        await ad.search("chứng khoán việt nam hôm nay")  # no lang -> heuristic
        assert seen["hl"] == "vi" and seen["gl"] == "VN" and seen["ceid"] == "VN:vi"


# --- category inference (docs/19 terms) ---------------------------------------


class TestInference:
    def test_social_terms(self):
        assert "social" in infer_categories_from_query("what does reddit think about NVDA")
        assert "social" in infer_categories_from_query("site:news.ycombinator.com rust")
        assert "social" in infer_categories_from_query("bluesky thread on AI regulation")
        assert "social" in infer_categories_from_query("site:news.ycombinator.com rust")

    def test_global_academic_terms(self):
        assert "academic" in infer_categories_from_query("doi:10.1038 nature paper on rates")
        assert "academic" in infer_categories_from_query("ssrn working paper fund flows")
        assert "academic" in infer_categories_from_query("systematic review meta-analysis covid")

    def test_global_finance_terms(self):
        assert "financial" in infer_categories_from_query("13F filing of Berkshire")
        assert "financial" in infer_categories_from_query("IMF world economic outlook")
        assert "financial" in infer_categories_from_query("best ETF for tech sector prospectus")

    def test_news_global_terms(self):
        assert "news" in infer_categories_from_query("breaking news world today")

    def test_neutral_query_stays_general(self):
        assert infer_categories_from_query("how to bake bread") == set()


# --- deep fan-out --------------------------------------------------------------


class _StubAdapter:
    def __init__(self, adapter_id: str):
        self.id = adapter_id

    async def search(self, query, **kwargs):
        return [
            RawResult(
                url=f"https://{self.id}.example.com/1",
                title=f"{self.id} result",
                snippet="s",
                adapter=self.id,
            )
        ]


@pytest.mark.asyncio
async def test_deep_fans_out_to_all_categories():
    from app.config import Settings

    configs = {
        c.id: c
        for c in [
            EngineConfig(id="web", name="web", category="general", enabled=True),
            EngineConfig(id="reddit", name="reddit", category="social", enabled=True),
            EngineConfig(id="openalex", name="openalex", category="academic", enabled=True),
            EngineConfig(id="stooq", name="stooq", category="financial", enabled=True),
        ]
    }
    adapters = {k: _StubAdapter(k) for k in configs}
    registry = EngineRegistry(InMemoryEngineStore(configs), adapters)
    pipeline = SearchPipeline(Settings(), registry)

    shallow = await pipeline.run(SearchParams(query="anything at all"))
    shallow_engines = {s.engine for s in shallow.statuses}
    assert shallow_engines == {"web"}  # only general without inference hits

    deep = await pipeline.run(SearchParams(query="anything at all", deep=True))
    deep_engines = {s.engine for s in deep.statuses}
    assert deep_engines == {"web", "reddit", "openalex", "stooq"}
    assert len(deep.results) >= 4


# --- engine store upgrade path (docs/19): new default engines appear on old DBs


@pytest.mark.asyncio
async def test_db_store_seeds_missing_default_engines_on_upgrade(monkeypatch, tmp_path):
    """An existing DB seeded with the pre-2.4.0 engine set must gain the 7 new
    default engines on first snapshot — without overwriting operator tweaks."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.db.models import Engine as EngineRow
    from app.orchestration.engine_store import DbEngineStore, default_configs

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(EngineRow.__table__.create)
    # Pre-2.4.0 state: only the old engines exist; operator disabled one.
    old = default_configs(Settings())
    async with factory() as session:
        for cfg in old.values():
            if cfg.id in {
                "hackernews",
                "stackexchange",
                "bluesky",
                "openalex",
                "crossref",
                "europepmc",
                "gdelt",
            }:
                continue
            session.add(
                EngineRow(
                    id=cfg.id,
                    name=cfg.name,
                    category=cfg.category,
                    adapter=cfg.adapter_path,
                    weight=cfg.weight,
                    enabled=(cfg.id != "brave"),
                    timeout_ms=cfg.timeout_ms,
                    max_results=cfg.max_results,
                    requires_key=cfg.requires_key,
                )
            )
        await session.commit()

    import app.db.session as db_session

    monkeypatch.setattr(db_session, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(
        "app.orchestration.engine_store.get_sessionmaker", lambda: factory, raising=False
    )
    store = DbEngineStore(refresh_s=0)
    snap = await store.snapshot()
    for new_id in (
        "hackernews",
        "stackexchange",
        "bluesky",
        "openalex",
        "crossref",
        "europepmc",
        "gdelt",
    ):
        assert new_id in snap, new_id
    # operator tweak preserved (brave was disabled in the old DB)
    assert snap["brave"].enabled is False
