"""docs/36 W2 S1/S4/S5: Lite general-web restore, broken-engine defaults, and a
``complete`` flag that means "every engine expected to answer did"."""

from __future__ import annotations

import httpx
import pytest

import app.retrieval.adapters.ddg as ddg_mod
from app.config import Settings
from app.orchestration.engine_store import InMemoryEngineStore, default_configs
from app.orchestration.pipeline import SearchParams, SearchPipeline, degraded_engines
from app.retrieval.adapters.base import AdapterError
from app.retrieval.adapters.mwmbl import MwmblAdapter
from app.retrieval.adapters.semanticscholar import SemanticScholarAdapter
from app.retrieval.health import Outcome
from app.retrieval.models import EngineConfig, EngineStatus, RawResult
from app.retrieval.registry import EngineRegistry

S = Settings(_env_file=None)


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- S1: Mwmbl --------------------------------------------------------------------


async def test_mwmbl_parses_segments_and_skips_bad_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/"  # canonical path: /search answers 301
        assert request.url.params["s"] == "lãi suất"
        return httpx.Response(
            200,
            json=[
                {
                    "url": "https://vnexpress.net/lai-suat-1.html",
                    "title": [{"value": "Lãi suất", "is_bold": True}, {"value": " giảm"}],
                    "extract": [{"value": "Ngân hàng "}, {"value": "giảm", "is_bold": True}],
                    "source": "mwmbl",
                },
                {"url": "javascript:alert(1)", "title": "x"},
                {"url": "https://vnexpress.net/lai-suat-1.html", "title": []},  # duplicate
                "garbage",
            ],
        )

    res = await MwmblAdapter(client=_mock(handler)).search("lãi suất")
    assert len(res) == 1
    assert res[0].title == "Lãi suất giảm" and res[0].snippet == "Ngân hàng giảm"
    assert res[0].adapter == "mwmbl" and res[0].source_engines == ["mwmbl"]


@pytest.mark.parametrize(
    ("status", "kind"), [(429, Outcome.RATE_LIMITED), (403, Outcome.BLOCKED), (500, None)]
)
async def test_mwmbl_errors_are_classified(status, kind):
    adapter = MwmblAdapter(client=_mock(lambda r: httpx.Response(status, text="no")))
    with pytest.raises(AdapterError) as exc:
        await adapter.search("anything")
    assert exc.value.kind == (kind or Outcome.UPSTREAM_ERROR)


async def test_mwmbl_non_list_payload_is_an_error():
    adapter = MwmblAdapter(client=_mock(lambda r: httpx.Response(200, json={"error": "x"})))
    with pytest.raises(AdapterError):
        await adapter.search("anything")


def test_mwmbl_is_a_default_general_engine():
    cfg = default_configs(S)["mwmbl"]
    assert cfg.enabled and cfg.category == "general" and not cfg.requires_key


async def test_ddg_anomaly_page_is_rate_limited_with_long_cooldown(monkeypatch):
    monkeypatch.setattr(ddg_mod, "_min_interval_s", 0.0)
    adapter = ddg_mod.DuckDuckGoAdapter(
        client=_mock(lambda r: httpx.Response(202, text="<html>anomaly</html>"))
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.search("anything", timeout_s=0.5)
    assert exc.value.kind == Outcome.RATE_LIMITED
    assert exc.value.retry_after_s == ddg_mod._BLOCK_COOLDOWN_S and not exc.value.retryable


# --- S4: broken engines are not on by default ----------------------------------------


def test_broken_engines_default_off():
    cfgs = default_configs(S)
    for eid in ("bluesky", "reddit", "gdelt"):
        assert cfgs[eid].enabled is False, eid
    assert cfgs["semanticscholar"].enabled is False  # keyless tier: 429 on every call
    keyed = default_configs(Settings(_env_file=None, semanticscholar_api_key="k"))
    assert keyed["semanticscholar"].enabled is True


async def test_semanticscholar_429_backs_off():
    adapter = SemanticScholarAdapter(
        client=_mock(lambda r: httpx.Response(429, text="slow", headers={"retry-after": "120"}))
    )
    with pytest.raises(AdapterError) as exc:
        await adapter.search("transformers")
    assert exc.value.kind == Outcome.RATE_LIMITED and exc.value.retry_after_s == 120.0
    adapter = SemanticScholarAdapter(client=_mock(lambda r: httpx.Response(429, text="slow")))
    with pytest.raises(AdapterError) as exc:
        await adapter.search("transformers")
    assert exc.value.retry_after_s == 300.0


# --- S5: complete vs degraded_engines -------------------------------------------------


class _Stub:
    def __init__(self, aid: str) -> None:
        self.id = aid
        self.calls = 0

    async def search(self, query, **kwargs):
        self.calls += 1
        return [
            RawResult(
                url=f"https://{self.id}.example.com/1", title="t", snippet="s", adapter=self.id
            )
        ]


async def test_quarantined_engine_is_reported_not_called_and_keeps_complete():
    configs = {
        "web": EngineConfig(id="web", name="web", category="general", enabled=True),
        "ddg": EngineConfig(
            id="ddg",
            name="ddg",
            category="general",
            enabled=False,
            disabled_reason="quarantine:success_7d=0.03 (1/37)",
        ),
        "off": EngineConfig(
            id="off", name="off", category="general", enabled=False, disabled_reason="operator"
        ),
    }
    adapters = {k: _Stub(k) for k in configs}
    pipeline = SearchPipeline(S, EngineRegistry(InMemoryEngineStore(configs), adapters))
    out = await pipeline.run(SearchParams(query="anything at all"))
    status = {s.engine: s.status for s in out.statuses}
    assert status == {"web": "ok", "ddg": "quarantined"}  # operator-disabled stays silent
    assert adapters["ddg"].calls == 0 and adapters["off"].calls == 0
    assert out.complete is True
    assert degraded_engines(out.statuses) == [
        {"engine": "ddg", "status": "quarantined", "reason": "quarantine:success_7d=0.03 (1/37)"}
    ]


def test_open_breaker_is_degraded_and_still_incomplete():
    st = [
        EngineStatus(engine="a", status="ok", results=3),
        EngineStatus(engine="b", status="skipped_breaker"),
    ]
    assert degraded_engines(st) == [{"engine": "b", "status": "skipped_breaker", "reason": None}]


def test_search_response_exposes_degraded_engines(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.main import create_app

    configs = {
        "web": EngineConfig(id="web", name="web", category="general", enabled=True),
        "ddg": EngineConfig(
            id="ddg",
            name="ddg",
            category="general",
            enabled=False,
            disabled_reason="quarantine:success_24h=0.00 (0/29)",
        ),
    }
    store = InMemoryEngineStore(configs)
    monkeypatch.setattr(deps, "get_engine_store", lambda: store)
    monkeypatch.setattr(deps, "build_adapters", lambda s: {k: _Stub(k) for k in configs})
    deps.reset_pipelines()
    try:
        with TestClient(create_app()) as client:
            r = client.post("/v1/search", json={"query": "degraded engines probe"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["complete"] is True
        assert body["degraded_engines"][0]["engine"] == "ddg"
        assert "ddg" not in body["engines_failed"]
    finally:
        deps.reset_pipelines()


# --- W2 ranking hygiene: market headlines must not ride along on long queries ----


async def test_fin_rss_needs_two_shared_terms_on_long_queries():
    from app.retrieval.adapters.financial_rss import GlobalFinancialRssAdapter

    rss = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>Stocks differ on gold demand</title>"
        "<link>https://news.example.com/gold</link><description>markets</description></item>"
        "<item><title>PostgreSQL logical replication arrives in cloud</title>"
        "<link>https://news.example.com/pg</link><description>tech</description></item>"
        "</channel></rss>"
    )
    adapter = GlobalFinancialRssAdapter(
        feeds=(("t", "https://feed.example.com/rss", "en"),),
        client=_mock(lambda r: httpx.Response(200, text=rss)),
    )
    res = await adapter.search(
        "what is PostgreSQL logical replication and how does it differ from physical replication"
    )
    assert [r.url for r in res] == ["https://news.example.com/pg"]
    short = await adapter.search("gold")  # <=2 terms: one shared word still enough
    assert [r.url for r in short] == ["https://news.example.com/gold"]


# --- W2 safe search: keyless engines have none of their own -------------------------


@pytest.mark.parametrize(
    ("url", "title", "adult"),
    [
        ("https://vi.xhamster.com/videos/1", "FastAPI", True),
        ("https://thichsex.org/a", "x", True),
        ("https://example.com/a", "Phim sex mới nhất", True),
        ("https://www.sussex.ac.uk/study", "University of Sussex", False),
        ("https://www.essex.gov.uk/", "Essex County Council", False),
        ("https://www.nhs.uk/sexual-health/", "Sexual health services", False),
        ("https://fastapi.tiangolo.com/tutorial/dependencies/", "Dependencies", False),
    ],
)
def test_adult_filter_is_precise(url, title, adult):
    from app.retrieval.safety import is_adult

    assert is_adult(url, title) is adult


async def test_pipeline_drops_adult_results_unless_disabled():
    class _Mixed:
        id = "web"

        async def search(self, query, **kwargs):
            return [
                RawResult(
                    url="https://vi.xhamster.com/v/1",
                    title="dependency",
                    snippet="s",
                    adapter="web",
                ),
                RawResult(
                    url="https://fastapi.tiangolo.com/tutorial/dependencies/",
                    title="FastAPI dependency injection",
                    snippet="s",
                    adapter="web",
                ),
            ]

    configs = {"web": EngineConfig(id="web", name="web", category="general", enabled=True)}
    for enabled, expected in ((True, 1), (False, 2)):
        settings = Settings(_env_file=None, safe_search_enabled=enabled)
        pipeline = SearchPipeline(
            settings, EngineRegistry(InMemoryEngineStore(configs), {"web": _Mixed()})
        )
        out = await pipeline.run(SearchParams(query="fastapi dependency injection"))
        assert len(out.results) == expected


async def test_lite_fallback_engines_are_capped_and_language_aware():
    seen: dict[str, int] = {}

    class _Rec:
        def __init__(self, aid: str) -> None:
            self.id = aid

        async def search(self, query, **kwargs):
            seen[self.id] = kwargs["max_results"]
            return []

    configs = {
        "web": EngineConfig(id="web", name="web", category="general", enabled=True),
        "wikipedia": EngineConfig(id="wikipedia", name="w", category="reference", enabled=True),
        "google_news": EngineConfig(id="google_news", name="g", category="news", enabled=True),
    }
    settings = Settings(_env_file=None, searxng_enabled=False)
    pipeline = SearchPipeline(
        settings, EngineRegistry(InMemoryEngineStore(configs), {k: _Rec(k) for k in configs})
    )
    await pipeline.run(SearchParams(query="cách nấu phở bò ngon", expansion=False))
    assert seen["google_news"] == 3 and seen["wikipedia"] == 3  # fallback-only: capped
    assert seen["web"] == configs["web"].max_results
    seen.clear()
    await pipeline.run(SearchParams(query="postgres btree index internals", expansion=False))
    assert "google_news" not in seen  # an English technical query gets no news fallback
    seen.clear()
    await pipeline.run(SearchParams(query="tin tức mới nhất hôm nay về phở", expansion=False))
    assert seen["google_news"] == configs["google_news"].max_results  # news intent: uncapped


async def test_fin_rss_keeps_accents_for_accented_queries():
    from app.retrieval.adapters.financial_rss import GlobalFinancialRssAdapter

    rss = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>Vì sao chanh ngón tay có nơi giá 2 triệu</title>"
        "<link>https://vnexpress.net/chanh</link><description>x</description></item>"
        "<item><title>Giá vàng hôm nay đảo chiều</title>"
        "<link>https://vnexpress.net/vang</link><description>x</description></item>"
        "</channel></rss>"
    )
    adapter = GlobalFinancialRssAdapter(
        feeds=(("t", "https://feed.example.com/rss", "vi"),),
        client=_mock(lambda r: httpx.Response(200, text=rss)),
    )
    # folded, "ngon"/"ngón" and "nội"/"nơi" collide; accented they do not
    assert await adapter.search("cách nấu phở bò ngon đúng vị Hà Nội", lang="vi") == []
    gold = await adapter.search("gia vang hom nay", lang="vi")  # unaccented still folds
    assert [r.url for r in gold] == ["https://vnexpress.net/vang"]


async def test_financial_intent_pulls_the_financial_news_wire():
    seen: set[str] = set()

    class _Rec:
        def __init__(self, aid: str) -> None:
            self.id = aid

        async def search(self, query, **kwargs):
            seen.add(self.id)
            return []

    configs = {
        "web": EngineConfig(id="web", name="web", category="general", enabled=True),
        "fin_rss": EngineConfig(id="fin_rss", name="f", category="news", enabled=True),
    }
    pipeline = SearchPipeline(
        S, EngineRegistry(InMemoryEngineStore(configs), {k: _Rec(k) for k in configs})
    )
    await pipeline.run(SearchParams(query="giá cổ phiếu VIC Vingroup", categories=["financial"]))
    assert "fin_rss" in seen
    seen.clear()
    await pipeline.run(SearchParams(query="postgres btree index internals"))
    assert "fin_rss" not in seen


def test_adult_title_signal_ignored_on_reputable_outlets():
    from app.retrieval.safety import is_adult

    news = "Bắt nhóm đối tượng phát tán clip sex trên mạng xã hội"
    assert not is_adult("https://vnexpress.net/bat-nhom-1.html", news)
    assert not is_adult("https://news.google.com/rss/articles/x", news, domain="vnexpress.net")
    assert is_adult("https://random-blog.xyz/a", news)  # no tier: title counts
    assert is_adult("https://news.google.com/rss/articles/y", "x", domain="xhamster.com")
