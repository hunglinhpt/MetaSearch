"""Comprehensive unit & integration tests for the v3.3 feature & hardening wave."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import worker as worker_mod
from app.api import deps
from app.api.v1 import podcast as podcast_mod
from app.api.v1 import site as site_mod
from app.config import get_settings
from app.core.tracing import setup_tracing
from app.db.bootstrap import ensure_schema
from app.db.models import Chunk as DbChunk
from app.db.models import Document, Space
from app.db.session import dispose_engine, get_sessionmaker
from app.extraction.scraper import PageContent, PageFailure
from app.extraction.sitemap import MapResult
from app.llm.embeddings import EmbeddingError
from app.llm.gateway import Usage
from app.main import create_app
from app.orchestration.scheduler import RUN_DIGEST_TOPIC, AutomatedScanner
from app.retrieval.models import EngineConfig, RawResult
from app.retrieval.sentiment_quant import (
    fetch_live_market_proxies,
    fetch_sentiment_quant_context,
)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_site_map_and_crawl_routes(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /v1/map & /v1/crawl: sitemap discovery, BFS depth 2, truncation & fail rows."""
    monkeypatch.setattr(get_settings(), "crawl_max_pages", 5)
    monkeypatch.setattr(get_settings(), "crawl_max_depth", 2)
    monkeypatch.setattr(get_settings(), "map_max_urls", 50)

    class _FakeScraper:
        async def scrape_many(self, urls: list[str], timeout_s: float | None = None) -> list[Any]:
            out: list[Any] = []
            for u in urls:
                if "broken" in u:
                    out.append(PageFailure(url=u, status="failed", error="timeout"))
                elif u.rstrip("/") == "https://docs.example.com":
                    out.append(
                        PageContent(
                            url=u,
                            final_url=u,
                            title="Root Page",
                            markdown=(
                                "# Welcome\nSee [Guide](https://docs.example.com/guide) "
                                "and [Bad](https://docs.example.com/broken) "
                                "and [Off](https://other.example.net/x)\n" + ("word " * 40)
                            ),
                            extractor_used="trafilatura",
                            fetched_at=datetime.now(UTC),
                        )
                    )
                else:
                    out.append(
                        PageContent(
                            url=u,
                            final_url=u,
                            title="Guide Page",
                            markdown="# Guide\nStep by step content for indexing.",
                            extractor_used="trafilatura",
                            fetched_at=datetime.now(UTC),
                        )
                    )
            return out

    persisted_batches: list[tuple[int, int]] = []

    async def _fake_map(_scraper: Any, url: str, **_kw: Any) -> MapResult:
        return MapResult(
            root=url,
            urls=["https://docs.example.com/a", "https://docs.example.com/b"],
            source="sitemap",
            truncated=False,
        )

    from app.orchestration import persist as persist_mod

    monkeypatch.setattr(site_mod, "map_site", _fake_map)
    monkeypatch.setattr(deps, "get_scraper", lambda: _FakeScraper())
    monkeypatch.setattr(
        persist_mod,
        "persist_pages",
        lambda pages, chunks, **kw: persisted_batches.append((len(pages), len(chunks))),
    )

    with TestClient(create_app()) as client:
        m_resp = client.post("/v1/map", json={"url": "https://docs.example.com", "max_urls": 20})
        assert m_resp.status_code == 200
        m_body = m_resp.json()
        assert m_body["source"] == "sitemap"
        assert len(m_body["urls"]) == 2

        c_resp = client.post(
            "/v1/crawl",
            json={"url": "https://docs.example.com", "depth": 2, "max_pages": 5, "max_chars": 80},
        )
        assert c_resp.status_code == 200
        c_body = c_resp.json()
        assert c_body["indexed"] is True
        assert c_body["visited"] >= 3
        assert any(p.get("truncated") for p in c_body["pages"])
        assert any(p.get("status") == "failed" for p in c_body["pages"])
        assert persisted_batches and persisted_batches[0][0] >= 2


async def test_hybrid_route_dense_fallback_and_space_scope(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /v1/hybrid supports dense+sparse, Space scoping, 404, and fallback."""
    db_file = tmp_path / "hybrid_wave.db"
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setattr(get_settings(), "hybrid_search_enabled", True)
    await dispose_engine()
    try:
        await ensure_schema()
        doc_id = uuid.uuid4()
        async with get_sessionmaker()() as session:
            session.add(
                Space(
                    id=uuid.uuid4(),
                    api_key_id="anonymous",
                    slug="vn-fin",
                    name="VN Finance",
                    domains=["cafef.vn"],
                    pinned_urls=[],
                )
            )
            session.add(
                Document(
                    id=doc_id,
                    canonical_url="https://cafef.vn/fpt-q3",
                    url_hash="h1",
                    domain="cafef.vn",
                    title="FPT Q3 Earnings",
                    content_md="FPT revenue and AI cloud growth",
                    content_hash="ch1",
                    fetched_at=datetime.now(UTC),
                    fetch_status="ok",
                    extractor_used="trafilatura",
                )
            )
            session.add(
                DbChunk(
                    id=uuid.uuid4(),
                    document_id=doc_id,
                    position=1,
                    heading_path="Overview",
                    content="FPT AI factory revenue accelerated 28% YoY in Q3.",
                    token_count=9,
                    tsv="fpt ai factory revenue accelerated 28% yoy in q3.",
                    embedding=[1.0, 0.0, 0.0],
                )
            )
            await session.commit()

        class _OkEmbedder:
            enabled = True

            async def embed(self, _text: str) -> list[float]:
                return [1.0, 0.0, 0.0]

        class _ErrEmbedder:
            enabled = True

            async def embed(self, _text: str) -> list[float]:
                raise EmbeddingError("embedder timeout")

        monkeypatch.setattr(deps, "get_embedder", lambda: _OkEmbedder())
        with TestClient(create_app()) as client:
            r_ok = client.post(
                "/v1/hybrid",
                json={"query": "FPT AI revenue", "space": "vn-fin", "k": 5, "dense": True},
            )
            assert r_ok.status_code == 200
            body_ok = r_ok.json()
            assert body_ok["dense_used"] is True
            assert len(body_ok["chunks"]) == 1
            assert body_ok["chunks"][0]["domain"] == "cafef.vn"

            r_404 = client.post(
                "/v1/hybrid",
                json={"query": "FPT", "space": "missing-space", "k": 5},
            )
            assert r_404.status_code == 404

            # Embedder error falls back cleanly to sparse BM25
            monkeypatch.setattr(deps, "get_embedder", lambda: _ErrEmbedder())
            r_fb = client.post("/v1/hybrid", json={"query": "FPT revenue", "k": 5, "dense": True})
            assert r_fb.status_code == 200
            assert r_fb.json()["dense_used"] is False
            assert len(r_fb.json()["chunks"]) == 1

            # Disabled toggle returns 400
            monkeypatch.setattr(get_settings(), "hybrid_search_enabled", False)
            r_off = client.post("/v1/hybrid", json={"query": "FPT"})
            assert r_off.status_code == 400
    finally:
        monkeypatch.undo()
        await dispose_engine()


async def test_worker_probe_retention_and_tracing(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise worker._probe_adapter, _retention_cadence, _heartbeat, and core.tracing."""
    from app.retrieval.adapters.searxng import SearXNGAdapter

    class _FakeHttp:
        async def get(self, _url: str, **_kwargs: Any) -> Any:
            return httpx.Response(200)

    class _FakeSearchAdapter:
        async def search(self, _q: str, max_results: int = 1, timeout_s: float = 5.0) -> list[str]:
            return ["ok"]

    class _BrokenSearchAdapter:
        async def search(self, _q: str, max_results: int = 1, timeout_s: float = 5.0) -> list[str]:
            raise RuntimeError("boom")

    monkeypatch.setattr(worker_mod, "get_http_client", lambda *_a: _FakeHttp())
    monkeypatch.setattr(
        deps,
        "build_adapters",
        lambda _s: {
            "searxng": SearXNGAdapter("http://localhost:8080"),
            "ddg": _FakeSearchAdapter(),
            "bad": _BrokenSearchAdapter(),
        },
    )
    assert await worker_mod._probe_adapter("missing") is False
    assert await worker_mod._probe_adapter("searxng") is True
    assert await worker_mod._probe_adapter("ddg") is True
    assert await worker_mod._probe_adapter("bad") is False

    # Retention cadence + heartbeat exit cleanly once stop is set
    import app.orchestration.retention as ret_mod

    monkeypatch.setattr(ret_mod, "run_if_due", lambda **_kw: asyncio.sleep(0, result={"purged": 0}))
    monkeypatch.chdir(tmp_path)
    stop = asyncio.Event()
    stop.set()
    await worker_mod._retention_cadence(stop)
    await worker_mod._heartbeat(stop)

    # Tracing off and ImportError fail-open
    import app.core.tracing as tr_mod

    monkeypatch.setattr(tr_mod, "_configured", False)
    monkeypatch.setattr(get_settings(), "otel_enabled", False)
    assert setup_tracing(object()) is False

    monkeypatch.setattr(get_settings(), "otel_enabled", True)
    # Without optional opentelemetry installed (or mocked missing), returns False cleanly
    import builtins

    real_import = builtins.__import__

    def _no_otel(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("opentelemetry"):
            raise ImportError("missing otel")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_otel)
    assert setup_tracing(object()) is False


async def test_scheduler_guarded_executive_synthesis(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional guarded executive brief uses SEC-07 delimiters, validates citations, fails open."""
    db_file = tmp_path / "sched_llm.db"
    monkeypatch.setattr(get_settings(), "database_url", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    await dispose_engine()

    class _FakeAdapter:
        async def search(
            self,
            _q: str,
            max_results: int = 5,
            timeout_s: float = 1.0,
            freshness_days: int | None = None,
            **_kw: Any,
        ) -> list[RawResult]:
            return [
                RawResult(
                    url="https://vnexpress.net/fpt-ai",
                    title="FPT mở rộng nhà máy AI",
                    snippet="Doanh thu mảng AI của FPT tăng trưởng mạnh.",
                    adapter="searxng",
                    source_engines=["searxng"],
                )
            ]

    class _StubReg:
        async def enabled(self) -> list[tuple[EngineConfig, Any]]:
            return [
                (
                    EngineConfig(
                        id="searxng",
                        name="SearXNG",
                        category="financial",
                        weight=1.0,
                        enabled=True,
                        timeout_ms=1000,
                        max_results=5,
                        requires_key=False,
                    ),
                    _FakeAdapter(),
                )
            ]

    captured_prompts: list[list[dict[str, str]]] = []

    class _GoodGateway:
        default_quick_model = "fake-quick"

        async def complete(self, messages: list[dict[str, str]], **_kw: Any) -> tuple[str, Usage]:
            captured_prompts.append(messages)
            return ("- FPT mở rộng đầu tư AI mạnh mẽ [1].", Usage(input_tokens=10, output_tokens=8))

    class _HallucinatingGateway:
        default_quick_model = "fake-quick"

        async def complete(self, _messages: list[dict[str, str]], **_kw: Any) -> tuple[str, Usage]:
            return (
                "- Tin bịa không có trong danh sách [999].",
                Usage(input_tokens=5, output_tokens=5),
            )

    try:
        await ensure_schema()
        monkeypatch.setattr(get_settings(), "scheduler_llm_synthesis_enabled", True)
        scanner = AutomatedScanner(_StubReg(), gateway=_GoodGateway())  # type: ignore[arg-type]
        scanner.topics = {"viet_finance": scanner.topics["viet_finance"]}
        posts = await scanner.scan_all()
        digest = posts[-1]
        assert digest.topic == RUN_DIGEST_TOPIC
        assert "## Tóm Tắt Điều Hành (AI Grounded)" in digest.content_md
        assert "[1]" in digest.content_md
        assert captured_prompts and "<<<" in captured_prompts[0][1]["content"]

        # Ungrounded hallucination fails open cleanly back to pure extractive digest
        scanner_bad = AutomatedScanner(_StubReg(), gateway=_HallucinatingGateway())  # type: ignore[arg-type]
        scanner_bad.topics = {"viet_finance": scanner_bad.topics["viet_finance"]}
        posts_bad = await scanner_bad.scan_all()
        digest_bad = posts_bad[-1]
        assert "## Tóm Tắt Điều Hành (AI Grounded)" not in digest_bad.content_md
    finally:
        monkeypatch.undo()
        await dispose_engine()


async def test_live_vix_proxies_and_simplize_fa_fallback() -> None:
    """fetch_live_market_proxies parses ^VIX/^VIX3M and _fetch_fa falls back to Simplize cleanly."""
    now_ts = int(datetime.now(UTC).timestamp())
    vix_closes = [18.0 + (i % 5) * 0.2 for i in range(55)]
    vix_payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [now_ts - (54 - i) * 86400 for i in range(55)],
                    "indicators": {
                        "quote": [
                            {
                                "open": vix_closes,
                                "high": [x + 0.5 for x in vix_closes],
                                "low": [x - 0.5 for x in vix_closes],
                                "close": vix_closes,
                                "volume": [1000.0] * 55,
                            }
                        ]
                    },
                }
            ]
        }
    }
    vix3m_payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [now_ts - 86400, now_ts],
                    "indicators": {
                        "quote": [
                            {
                                "open": [20.0, 20.5],
                                "high": [21.0, 21.0],
                                "low": [19.5, 20.0],
                                "close": [20.2, 20.4],
                                "volume": [1000.0, 1000.0],
                            }
                        ]
                    },
                }
            ]
        }
    }

    def _yahoo_route(request: httpx.Request) -> httpx.Response:
        if "%5EVIX3M" in str(request.url) or "^VIX3M" in str(request.url):
            return httpx.Response(200, json=vix3m_payload)
        if "%5EVIX" in str(request.url) or "^VIX" in str(request.url):
            return httpx.Response(200, json=vix_payload)
        return httpx.Response(404)

    async with _client(_yahoo_route) as client:
        proxies = await fetch_live_market_proxies("NVDA", is_vietnam=False, client=client)
    assert proxies is not None
    assert proxies.vix == vix_closes[-1]
    assert proxies.vix_ma50 is not None
    assert proxies.vix_3m == 20.4
    assert "Yahoo Finance" in (proxies.source_note or "")

    # Simplize FA fallback when fa_aggregator=None
    def _vn_route(request: httpx.Request) -> httpx.Response:
        if "simplize.vn" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "data": {"peRatio": 16.4, "pbRatio": 3.1, "roe": 24.5, "marketCap": 1.8e14},
                },
            )
        if "dchart-api.vndirect.com.vn" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "s": "ok",
                    "t": [now_ts - (29 - i) * 86400 for i in range(30)],
                    "o": [100.0] * 30,
                    "h": [102.0] * 30,
                    "l": [99.0] * 30,
                    "c": [101.0] * 30,
                    "v": [500_000.0] * 30,
                },
            )
        return httpx.Response(404)

    async with _client(_vn_route) as client:
        bars, src, _u, _as_of, fa = await fetch_sentiment_quant_context(
            "FPT", is_vietnam=True, fa_aggregator=None, client=client
        )
    assert len(bars) == 30 and src == "VNDIRECT"
    assert fa is not None
    assert fa.pe == 16.4 and fa.pb == 3.1 and fa.roe == 24.5 and fa.source == "Simplize"


def test_personal_podcast_routes_disabled_and_enabled(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise /v1/podcast/personal* endpoints including 400/404/ready audio paths."""
    podcast_mod.reset_podcast_state()
    monkeypatch.setattr(get_settings(), "podcast_data_dir", str(tmp_path))
    monkeypatch.setattr(get_settings(), "voicestudio_url", "http://localhost:8765")
    try:
        with TestClient(create_app()) as client:
            monkeypatch.setattr(get_settings(), "podcast_personal_enabled", False)
            r_dis = client.post("/v1/podcast/personal")
            assert r_dis.status_code == 400

            monkeypatch.setattr(get_settings(), "podcast_personal_enabled", True)
            r_miss = client.get("/v1/podcast/personal/latest")
            assert r_miss.status_code == 404

            # Simulate an already-written personal mp3 file on disk for anonymous owner
            owner = podcast_mod._personal_owner()
            prefix = podcast_mod._personal_prefix(owner)
            day = podcast_mod._today()
            mp3_file = Path(tmp_path) / f"{prefix}-{day.isoformat()}.mp3"
            mp3_file.write_bytes(b"ID3" + b"\x00" * 64)

            st = client.get("/v1/podcast/personal/status")
            assert st.status_code == 200
            assert st.json()["status"] == "ready"

            aud = client.get("/v1/podcast/personal/latest")
            assert aud.status_code == 200
            assert aud.headers["cache-control"] == "private, no-store"
            assert json.loads(json.dumps({"ok": True}))["ok"] is True
    finally:
        podcast_mod.reset_podcast_state()


async def test_yahoo_empty_volume_index_and_market_vix_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index payloads omitting volume still parse; market_sentiment merges live VIX proxies."""
    from types import SimpleNamespace

    import app.api.v1.market as market_mod
    from app.retrieval.sentiment_quant import MarketProxiesInput, parse_yahoo_chart_bars

    now_ts = int(datetime.now(UTC).timestamp())
    index_payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [now_ts - 86400, now_ts],
                    "indicators": {
                        "quote": [
                            {
                                "open": [20.0, 21.0],
                                "high": [21.5, 22.0],
                                "low": [19.8, 20.5],
                                "close": [21.0, 21.8],
                                "volume": [],
                            }
                        ]
                    },
                }
            ]
        }
    }
    bars, as_of = parse_yahoo_chart_bars(index_payload)
    assert len(bars) == 2 and bars[-1].close == 21.8 and as_of is not None

    # Unsafe symbol blocked at entry without network access
    unsafe = await fetch_live_market_proxies("../etc", is_vietnam=False)
    assert unsafe is None
    unsafe_ctx = await fetch_sentiment_quant_context("../etc", is_vietnam=True)
    assert unsafe_ctx == ([], None, None, None, None)

    async def _fake_quant(*_a: Any, **_kw: Any) -> tuple[list[Any], None, None, None, None]:
        return [], None, None, None, None

    async def _fake_proxies(*_a: Any, **_kw: Any) -> MarketProxiesInput:
        return MarketProxiesInput(
            vix=16.5,
            vix_ma50=18.2,
            vix_3m=19.8,
            source_note="Yahoo Finance (^VIX, ^VIX3M)",
        )

    monkeypatch.setattr(market_mod, "fetch_sentiment_quant_context", _fake_quant)
    monkeypatch.setattr(market_mod, "fetch_live_market_proxies", _fake_proxies)
    monkeypatch.setattr(
        deps,
        "get_search_pipeline",
        lambda: SimpleNamespace(
            run=lambda _p: asyncio.sleep(0, result=SimpleNamespace(results=[]))
        ),
    )

    with TestClient(create_app()) as client:
        resp = client.post(
            "/v1/market/sentiment",
            json={
                "ticker": "NVDA",
                "fetch_market_data": True,
                "retrieved_documents": [
                    {
                        "url": "https://reuters.com/nvda-1",
                        "title": "NVDA guidance raises outlook",
                        "published_at": datetime.now(UTC).isoformat(),
                    }
                ],
                "market_proxies": {"cboe_put_call_equity": 0.55},
            },
        )
    assert resp.status_code == 200
    mp = resp.json()["benchmark_standards"]["quantitative_telemetry"]["market_proxies"]
    assert mp["cboe_put_call_ratio"] == 0.55
    assert mp["vix_term_regime"] == "CALM_CONTANGO"
    assert mp["vix_vs_ma50_pct"] is not None


async def test_live_vix_proxies_and_yahoo_corrupt_bars() -> None:
    """fetch_live_market_proxies computes VIX/MA50/VIX3M from Yahoo charts and skips bad bars."""
    now_ts = int(datetime.now(UTC).timestamp())
    vix_ts = [now_ts - (55 - i) * 86400 for i in range(55)] + [now_ts + 86400]
    vix_vals: list[Any] = [18.0 for _ in range(54)] + [15.5, "NaN"]

    def handler(request: httpx.Request) -> httpx.Response:
        if "%5EVIX3M" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "chart": {
                        "result": [
                            {
                                "timestamp": [now_ts - 86400, now_ts],
                                "indicators": {
                                    "quote": [
                                        {
                                            "open": [19.0, 19.2],
                                            "high": [19.5, 19.6],
                                            "low": [18.8, 19.0],
                                            "close": [19.1, 19.4],
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "timestamp": vix_ts,
                            "indicators": {
                                "quote": [
                                    {
                                        "open": vix_vals,
                                        "high": vix_vals,
                                        "low": vix_vals,
                                        "close": vix_vals,
                                        "volume": [],
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        )

    async with _client(handler) as cli:
        proxies = await fetch_live_market_proxies("NVDA", is_vietnam=False, client=cli)
        vn_skip = await fetch_live_market_proxies("FPT", is_vietnam=True, client=cli)

    assert vn_skip is None
    assert proxies is not None
    assert proxies.vix == 15.5
    assert proxies.vix_ma50 is not None
    assert proxies.vix_3m == 19.4


async def test_executive_brief_rejects_bare_citations_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_synthesize_executive_brief returns None on bare markers or gateway timeout."""
    from app.orchestration.scheduler import _synthesize_executive_brief

    citations = [
        {"n": 1, "title": "Lãi suất ổn định", "url": "https://cafef.vn/macro", "snippet": "CPI hạ"}
    ]
    monkeypatch.setattr(get_settings(), "scheduler_llm_synthesis_enabled", True)

    class _BareMarkersGw:
        default_quick_model = "fake-quick"

        async def complete(self, _messages: list[dict[str, str]], **_kw: Any) -> tuple[str, Usage]:
            return ("[1] [1]", Usage(input_tokens=2, output_tokens=2))

    assert await _synthesize_executive_brief(_BareMarkersGw(), citations) is None

    class _TimeoutGw:
        default_quick_model = "fake-quick"

        async def complete(self, _messages: list[dict[str, str]], **_kw: Any) -> tuple[str, Usage]:
            raise TimeoutError("llm timeout")

    assert await _synthesize_executive_brief(_TimeoutGw(), citations) is None


def test_tracing_otel_enabled_missing_and_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """setup_tracing handles missing optional deps cleanly and instruments when present."""
    import builtins
    import sys
    from types import ModuleType, SimpleNamespace

    import app.core.tracing as tracing_mod

    monkeypatch.setattr(get_settings(), "otel_enabled", True)
    monkeypatch.setattr(tracing_mod, "_configured", False)

    real_import = builtins.__import__

    def _no_otel(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("opentelemetry"):
            raise ImportError("no otel")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_otel)
    assert setup_tracing(SimpleNamespace()) is False

    # Provide stub opentelemetry hierarchy
    monkeypatch.setattr(builtins, "__import__", real_import)
    called: list[str] = []
    otel = ModuleType("opentelemetry")
    otel.trace = SimpleNamespace(set_tracer_provider=lambda _p: called.append("set"))  # type: ignore[attr-defined]
    exporter = ModuleType("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")
    exporter.OTLPSpanExporter = lambda: object()  # type: ignore[attr-defined]
    fastapi_inst = ModuleType("opentelemetry.instrumentation.fastapi")
    fastapi_inst.FastAPIInstrumentor = SimpleNamespace(  # type: ignore[attr-defined]
        instrument_app=lambda _a: called.append("fastapi")
    )
    httpx_inst = ModuleType("opentelemetry.instrumentation.httpx")
    httpx_inst.HTTPXClientInstrumentor = lambda: SimpleNamespace(  # type: ignore[attr-defined]
        instrument=lambda: called.append("httpx")
    )
    sdk_res = ModuleType("opentelemetry.sdk.resources")
    sdk_res.Resource = SimpleNamespace(create=lambda d: d)  # type: ignore[attr-defined]
    sdk_trace = ModuleType("opentelemetry.sdk.trace")
    sdk_trace.TracerProvider = lambda resource=None: SimpleNamespace(  # type: ignore[attr-defined]
        add_span_processor=lambda _sp: None
    )
    sdk_exp = ModuleType("opentelemetry.sdk.trace.export")
    sdk_exp.BatchSpanProcessor = lambda _e: object()  # type: ignore[attr-defined]
    for mod_name, mod_obj in {
        "opentelemetry": otel,
        "opentelemetry.exporter": ModuleType("opentelemetry.exporter"),
        "opentelemetry.exporter.otlp": ModuleType("opentelemetry.exporter.otlp"),
        "opentelemetry.exporter.otlp.proto": ModuleType("opentelemetry.exporter.otlp.proto"),
        "opentelemetry.exporter.otlp.proto.grpc": ModuleType(
            "opentelemetry.exporter.otlp.proto.grpc"
        ),
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": exporter,
        "opentelemetry.instrumentation": ModuleType("opentelemetry.instrumentation"),
        "opentelemetry.instrumentation.fastapi": fastapi_inst,
        "opentelemetry.instrumentation.httpx": httpx_inst,
        "opentelemetry.sdk": ModuleType("opentelemetry.sdk"),
        "opentelemetry.sdk.resources": sdk_res,
        "opentelemetry.sdk.trace": sdk_trace,
        "opentelemetry.sdk.trace.export": sdk_exp,
    }.items():
        monkeypatch.setitem(sys.modules, mod_name, mod_obj)

    assert setup_tracing(SimpleNamespace()) is True
    assert called == ["set", "fastapi", "httpx"]


async def test_yahoo_response_size_cap_and_global_symbols() -> None:
    """PASS-2 P2-4 & P3-8: cap Yahoo response bytes and accept BRK.B / BTC-USD / ^GSPC."""
    from app.retrieval.fa_sources.base import MAX_RESPONSE_BYTES, ensure_global_symbol
    from app.retrieval.sentiment_quant import _fetch_yahoo_chart_series

    assert ensure_global_symbol("BRK.B") == "BRK.B"
    assert ensure_global_symbol("BTC-USD") == "BTC-USD"
    assert ensure_global_symbol("^GSPC") == "^GSPC"
    assert ensure_global_symbol("../etc") is None

    def oversized_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    async with _client(oversized_handler) as cli:
        bars, as_of = await _fetch_yahoo_chart_series(
            "BRK.B", range_str="1y", timeout_s=2.0, client=cli
        )
    assert bars == [] and as_of is None
